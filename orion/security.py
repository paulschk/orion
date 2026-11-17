"""Passwords, sessions and CSRF.

Sessions live in SQLite rather than in signed cookies so they can be listed and
revoked from the admin dashboard.
"""

from __future__ import annotations

import hmac
from datetime import datetime, timedelta, timezone

import bcrypt

from . import db
from .util import now, rand_token

SESSION_COOKIE = "orion_session"
SESSION_DAYS = 14


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("ascii"))
    except (ValueError, TypeError):
        return False


def create_session(user_id: int, ip: str = "", user_agent: str = "") -> dict[str, str]:
    token = rand_token(32)
    csrf = rand_token(24)
    expires = (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)).replace(microsecond=0).isoformat()
    db.execute(
        "INSERT INTO sessions(id, user_id, csrf, created_at, expires_at, ip, user_agent) "
        "VALUES(?, ?, ?, ?, ?, ?, ?)",
        (token, user_id, csrf, now(), expires, ip[:64], user_agent[:200]),
    )
    return {"token": token, "csrf": csrf, "expires_at": expires}


def get_session(token: str | None):
    if not token:
        return None
    row = db.one(
        "SELECT s.*, u.id AS uid, u.email, u.name, u.company, u.role, u.is_active, "
        "       u.ssh_public_key, u.created_at AS user_created_at "
        "FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.id = ?",
        (token,),
    )
    if row is None:
        return None
    if row["expires_at"] <= now():
        destroy_session(token)
        return None
    if not row["is_active"]:
        return None
    return row


def destroy_session(token: str | None) -> None:
    if token:
        db.execute("DELETE FROM sessions WHERE id = ?", (token,))


def purge_expired_sessions() -> int:
    with db.cursor(commit=True) as cur:
        cur.execute("DELETE FROM sessions WHERE expires_at <= ?", (now(),))
        return cur.rowcount


def csrf_ok(session, submitted: str | None) -> bool:
    if session is None or not submitted:
        return False
    return hmac.compare_digest(str(session["csrf"]), str(submitted))
