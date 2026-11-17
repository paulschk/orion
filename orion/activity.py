"""Audit trail. Cheap to write, and the first thing you want when a shared
host starts behaving strangely."""

from __future__ import annotations

from . import db
from .util import now


def log(action: str, *, user_id: int | None = None, target: str = "",
        detail: str = "", ip: str = "") -> None:
    db.execute(
        "INSERT INTO activity(user_id, action, target, detail, ip, created_at) "
        "VALUES(?, ?, ?, ?, ?, ?)",
        (user_id, action, target[:120], detail[:1000], ip[:64], now()),
    )


def recent(limit: int = 60, user_id: int | None = None) -> list:
    if user_id is None:
        return db.query(
            "SELECT a.*, u.email FROM activity a LEFT JOIN users u ON u.id = a.user_id "
            "ORDER BY a.id DESC LIMIT ?", (limit,))
    return db.query(
        "SELECT a.*, u.email FROM activity a LEFT JOIN users u ON u.id = a.user_id "
        "WHERE a.user_id = ? ORDER BY a.id DESC LIMIT ?", (user_id, limit))
