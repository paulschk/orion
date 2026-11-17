"""WebSocket terminal.

Protocol (deliberately small):
  client -> server : binary = raw keystrokes
                     text   = JSON control, e.g. {"type":"resize","cols":..,"rows":..}
  server -> client : binary = raw terminal output

Bytes stay bytes in both directions, so nothing has to survive a UTF-8 round
trip and the client can hand a Uint8Array straight to xterm.
"""

from __future__ import annotations

import asyncio
import json
import logging
from urllib.parse import urlparse

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .. import activity, db, security
from ..config import settings
from ..console import ConsoleError, ConsoleSession

log = logging.getLogger("orion.terminal")

router = APIRouter()

MAX_SESSIONS_PER_USER = 4
_sessions: dict[int, int] = {}


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

    instance = db.one("SELECT * FROM instances WHERE id = ?", (instance_id,))
    if instance is None or (
        session_row["role"] != "admin" and instance["user_id"] != session_row["uid"]
    ):
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

    client_ip = (websocket.headers.get("x-forwarded-for", "") or "").split(",")[0].strip()
    activity.log("instance.terminal_open", user_id=uid, target=instance["slug"], ip=client_ip)

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
        activity.log("instance.terminal_close", user_id=uid, target=instance["slug"], ip=client_ip)
