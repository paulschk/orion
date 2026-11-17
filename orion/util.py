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


def slugify(value: str, fallback: str = "server") -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    value = re.sub(r"-{2,}", "-", value)
    return (value or fallback)[:32]


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
    """French formatting: comma decimal separator, symbol after the amount.

    Producing "3,00 EUR" rather than "EUR3.00" matters here - these prices are
    read by French customers on a French site.
    """
    amount = f"{cents / 100:.2f}".replace(".", ",")
    code = (currency or "EUR").upper()
    if code == "EUR":
        return f"{amount}\u00a0\u20ac"          # 3,00 € (non-breaking space)
    symbol = SYMBOLS.get(code, code)
    return f"{symbol}{amount}"
