"""Terminal into tenant instances, over two transports.

The WebSocket is the better one: one connection, keystrokes straight through. It
is what the panel uses whenever it opens, which is everywhere except behind a
proxy that will not carry the upgrade - measured here: through Cloudflare, a
browser's handshake never reaches the origin while the same handshake written by
hand in HTTP/1.1 is accepted.

So there is a second transport that no intermediary can mistake for anything
unusual: output as server-sent events, input as ordinary POSTs. Not as good a
console - a keystroke costs a request - but it works, and the client is told which
of the two it is on.

Protocol, WebSocket:
  client -> server : binary = raw keystrokes
                     text   = JSON control, e.g. {"type":"resize","cols":..,"rows":..}
  server -> client : binary = raw terminal output

Protocol, HTTP fallback:
  GET  .../console/stream  : text/event-stream; the first event carries the stream
                             token, then one base64 block per chunk of output
  POST .../console/input   : raw bytes, or a `X-Console-Resize` header
  Both carry the session cookie and the CSRF token; bytes stay bytes, base64 is
  only there because an event stream is text.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
from urllib.parse import urlparse

from fastapi import APIRouter, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse, StreamingResponse

from .. import activity, db, security
from ..config import settings
from ..console import ConsoleError, ConsoleSession
from ..deps import client_ip, guard_csrf, require_user

log = logging.getLogger("orion.terminal")

router = APIRouter()

MAX_SESSIONS_PER_USER = 4
_sessions: dict[int, int] = {}
# Le repli HTTP garde ses sessions ici : le flux les crée, les requêtes d'entrée
# les retrouvent par leur jeton. Une session sans flux est une session morte.
_streams: dict[str, tuple[int, ConsoleSession]] = {}
STREAM_KEEPALIVE_SECONDS = 20


def _owned_instance(instance_id: int, session):
    """The instance, only if the caller may open a console on it."""
    instance = db.one("SELECT * FROM instances WHERE id = ?", (instance_id,))
    if instance is None:
        return None
    if session["role"] != "admin" and instance["user_id"] != session["uid"]:
        return None
    return instance


def _open(instance, session, transport: str):
    """A started console, or (None, why not)."""
    if instance["status"] != "running":
        return None, "Instance non démarrée."

    uid = session["uid"]
    if _sessions.get(uid, 0) >= MAX_SESSIONS_PER_USER:
        return None, "Trop de sessions ouvertes."

    try:
        console = ConsoleSession(instance["incus_name"])
        console.start()
    except ConsoleError as exc:
        return None, str(exc)

    _sessions[uid] = _sessions.get(uid, 0) + 1
    return console, ""


def _close(instance, session, transport: str) -> None:
    uid = session["uid"]
    _sessions[uid] = max(0, _sessions.get(uid, 1) - 1)
    activity.log("instance.terminal_close", user_id=uid, target=instance["slug"],
                 ip="", detail="transport:{}".format(transport))


# --------------------------------------------------------------------- WebSocket

def _origin_ok(websocket: WebSocket) -> bool:
    """Reject cross-site WebSocket hijacking.

    Browsers attach cookies to a WebSocket handshake regardless of origin, so
    the Origin header is the only thing between a hostile page and a root shell
    in the user's instances.
    """
    origin = websocket.headers.get("origin")
    if not origin:
        return False
    host = urlparse(origin).hostname or ""
    allowed = {settings.domain, urlparse(settings.base_url).hostname or ""}
    return host in allowed


@router.websocket("/ws/console/{instance_id}")
async def console(websocket: WebSocket, instance_id: int):
    if not _origin_ok(websocket):
        await websocket.close(code=4403)
        return

    session_row = security.get_session(websocket.cookies.get(security.SESSION_COOKIE))
    if session_row is None:
        await websocket.close(code=4401)
        return

    instance = _owned_instance(instance_id, session_row)
    if instance is None:
        await websocket.close(code=4404)
        return
    if instance["status"] != "running":
        await websocket.close(code=4409)
        return

    uid = session_row["uid"]
    if _sessions.get(uid, 0) >= MAX_SESSIONS_PER_USER:
        await websocket.close(code=4429)
        return

    await websocket.accept()

    try:
        console_session = ConsoleSession(instance["incus_name"])
        console_session.start()
    except ConsoleError as exc:
        await websocket.send_text(json.dumps({"type": "error", "message": str(exc)}))
        await websocket.close(code=1011)
        return

    _sessions[uid] = _sessions.get(uid, 0) + 1
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[bytes] = asyncio.Queue()
    state = {"closing": False}

    def on_readable() -> None:
        chunk = console_session.read()
        queue.put_nowait(chunk or b"")

    loop.add_reader(console_session.fd, on_readable)

    activity.log("instance.terminal_open", user_id=uid, target=instance["slug"],
                 ip=(websocket.headers.get("x-forwarded-for", "") or "").split(",")[0].strip(),
                 detail="transport:websocket")

    async def pump_output() -> None:
        while True:
            chunk = await queue.get()
            if not chunk:
                break
            await websocket.send_bytes(chunk)
        state["closing"] = True
        try:
            await websocket.close()
        except RuntimeError:
            pass

    pump = asyncio.create_task(pump_output())

    try:
        while not state["closing"]:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            if message.get("bytes"):
                console_session.write(message["bytes"])
            elif message.get("text"):
                try:
                    control = json.loads(message["text"])
                except (TypeError, ValueError):
                    continue
                if control.get("type") == "resize":
                    console_session.resize(
                        int(control.get("cols") or 80), int(control.get("rows") or 24))
                # "ping" is the client's keepalive: a relay or a NAT will drop a
                # connection that has been silent, and the generic WebSocket API
                # gives the browser no way to send a protocol-level ping. There
                # is nothing to answer - receiving it is the whole point.
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        try:
            loop.remove_reader(console_session.fd)
        except (ValueError, OSError):
            pass
        console_session.close()
        pump.cancel()
        _sessions[uid] = max(0, _sessions.get(uid, 1) - 1)
        activity.log("instance.terminal_close", user_id=uid, target=instance["slug"],
                     ip="", detail="transport:websocket")


# ------------------------------------------------------------------- repli HTTP
#
# Deux requêtes ordinaires, qu'aucun proxy ne peut confondre avec autre chose.
# C'est ce chemin que le module du terminal emprunte quand la poignée de main
# WebSocket n'aboutit pas.


@router.get("/app/instances/{instance_id}/console/stream")
async def console_stream(request: Request, instance_id: int):
    session = require_user(request)
    instance = _owned_instance(instance_id, session)
    if instance is None:
        return PlainTextResponse("Instance introuvable.", status_code=404)

    console, problem = _open(instance, session, "stream")
    if console is None:
        return PlainTextResponse(problem, status_code=409)

    token = secrets.token_urlsafe(18)
    _streams[token] = (session["uid"], console)
    ip = client_ip(request)

    async def events():
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes] = asyncio.Queue()

        def on_readable() -> None:
            queue.put_nowait(console.read() or b"")

        loop.add_reader(console.fd, on_readable)
        try:
            yield "event: ready\ndata: {}\n\n".format(json.dumps({"token": token}))
            while True:
                try:
                    chunk = await asyncio.wait_for(queue.get(), timeout=STREAM_KEEPALIVE_SECONDS)
                except asyncio.TimeoutError:
                    # Un proxy coupe une connexion muette. Ce commentaire ne veut
                    # rien dire pour le client, et garde le flux en vie.
                    yield ": keepalive\n\n"
                    continue
                if not chunk:
                    break
                yield "data: {}\n\n".format(base64.b64encode(chunk).decode("ascii"))
        finally:
            try:
                loop.remove_reader(console.fd)
            except (ValueError, OSError):
                pass
            console.close()
            _streams.pop(token, None)
            _close(instance, session, "stream")

    activity.log("instance.terminal_open", user_id=session["uid"], target=instance["slug"],
                 ip=ip, detail="transport:stream")
    return StreamingResponse(
        events(), media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.post("/app/instances/{instance_id}/console/input")
async def console_input(request: Request, instance_id: int):
    """Keystrokes, or a new window size, for the stream opened just above."""
    session = require_user(request)
    guard_csrf(request, session, {})          # le jeton voyage dans l'en-tête

    token = request.headers.get("x-console-token", "")
    entry = _streams.get(token)
    if entry is None or entry[0] != session["uid"]:
        # Le flux est parti : le client doit le savoir plutôt que de taper dans le vide.
        return PlainTextResponse("Flux terminé.", status_code=409)
    console = entry[1]

    size = request.headers.get("x-console-resize")
    if size:
        try:
            cols, rows = (int(part) for part in size.split("x", 1))
        except ValueError:
            return PlainTextResponse("Taille invalide.", status_code=400)
        console.resize(max(20, cols), max(2, rows))
        return Response(status_code=204)

    body = await request.body()
    if body:
        console.write(body)
    return Response(status_code=204)
