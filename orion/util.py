"""Small shared helpers."""

from __future__ import annotations

import re
import secrets
import string
from datetime import datetime, timedelta, timezone

SYMBOLS = {"EUR": "\u20ac", "USD": "$", "GBP": "\u00a3", "CHF": "CHF"}


def now() -> str:
    """Timestamp used everywhere in the database: ISO-8601, UTC, seconds."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def days_from_now(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).replace(microsecond=0).isoformat()


def pretty_date(iso: str | None) -> str:
    if not iso:
        return "\u2014"
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    return dt.strftime("%d %b %Y")


def pretty_datetime(iso: str | None) -> str:
    if not iso:
        return "\u2014"
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    return dt.strftime("%d %b %Y %H:%M")


def slugify(value: str, fallback: str = "server", limit: int = 32) -> str:
    """A safe token: lowercase, dashes, never ending in one.

    The strip happens *after* the truncation on purpose. Cutting a long name
    first can leave a trailing dash, and Incus refuses a name that does not end
    with a letter or a digit - a snapshot label built from a long slug and a
    label was rejected with a raw daemon error for exactly that reason.
    """
    value = (value or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    value = re.sub(r"-{2,}", "-", value)
    return (value or fallback)[:limit].strip("-") or fallback


def snapshot_label(slug: str, requested: str = "", stamp: str = "",
                   suffix: int = 0, limit: int = 63) -> str:
    """A snapshot name: timestamp kept whole, human part shortened to fit.

    A client reads this name, so the date and time are the parts that must not
    be cut - and the result must still satisfy Incus, which wants it to end
    alphanumerically and to stay within the length cap.
    """
    tail = f"{stamp}-{suffix}" if suffix else (stamp or "")
    if not tail:
        return slugify(f"{slug}-{requested}" if requested else slug, limit=limit)
    room = max(1, limit - len(tail) - 1)
    human = f"{slug}-{requested}" if requested else slug
    prefix = slugify(human, limit=room)
    return f"{prefix}-{tail}"[:limit]


def rand_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def rand_password(length: int = 20) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def human_mb(mb: int) -> str:
    if mb >= 1024:
        gb = mb / 1024
        return f"{gb:.0f} GB" if abs(gb - round(gb)) < 0.05 else f"{gb:.1f} GB"
    return f"{mb} MB"


def human_bytes(num: int | float) -> str:
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < step or unit == "TB":
            return f"{num:.0f} {unit}" if unit in ("B", "KB") else f"{num:.1f} {unit}"
        num /= step
    return f"{num:.1f} TB"


def money(cents: int, currency: str = "EUR") -> str:
    """French formatting for EUR, conventional formatting otherwise.

    French convention is a comma decimal separator with the symbol after the
    amount. That must not leak into other currencies: USD is "$5.00", not
    "$5,00".
    """
    code = (currency or "EUR").upper()
    value = cents / 100
    if code == "EUR":
        return f"{value:.2f}".replace(".", ",") + "\u00a0\u20ac"   # 3,00 €
    symbol = SYMBOLS.get(code, code)
    return f"{symbol}{value:.2f}"
