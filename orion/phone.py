"""Phone confirmation, by one-time code.

A new account is not confirmed until a code sent to its number comes back. The
code is stored hashed - never in clear - expires, and is limited both in attempts
and in how often it may be sent: a confirmation that can be brute-forced, or used
as somebody else's SMS cannon, confirms nothing.

No provider is configured by default. `ORION_SMS_BACKEND=log` writes the code to
the panel's log, so the whole flow can be exercised and reviewed end to end while
nothing leaves the machine, and `send()` is the one place a real one plugs in.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone

from . import activity, db
from .config import settings
from .util import now

log = logging.getLogger("orion.phone")

CODE_TTL_SECONDS = 600
MAX_ATTEMPTS = 5
MAX_SENDS_PER_HOUR = 5


def normalise(raw: str) -> str:
    """A dialable number in E.164, or "" when the input makes no sense.

    French numbers are the default: `06 65 42 39 89` becomes `+33665423989`.
    Written out because a phone check that accepts `azerty` checks nothing.
    """
    digits = re.sub(r"[^\d+]", "", raw or "")
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    elif digits.startswith("0"):
        digits = "+33" + digits[1:]
    elif not digits.startswith("+"):
        digits = "+" + digits
    body = digits[1:]
    if not body.isdigit() or not 8 <= len(body) <= 15:
        return ""
    return digits


def pretty(phone: str) -> str:
    """Display form: +33 6 65 42 39 89. Never used to compare."""
    if phone.startswith("+33") and len(phone) == 12:
        return "+33 {} {} {} {} {}".format(phone[3], phone[4:6], phone[6:8],
                                           phone[8:10], phone[10:12])
    return phone


def confirmed(user_row) -> bool:
    return bool(user_row is not None and user_row["phone_verified_at"])


def _hash(user_id: int, code: str) -> str:
    return hmac.new(settings.secret_key.encode(), "{}:{}".format(user_id, code).encode(),
                    hashlib.sha256).hexdigest()


def _sent_recently(user_id: int) -> int:
    """How many codes this account asked for in the last hour.

    The cutoff is formatted like `util.now()`, not by SQLite: `created_at` holds
    Python's ISO form, and comparing it against SQLite's space-separated
    `datetime('now')` would make every row look recent - which would throttle an
    account forever after its fifth code.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
    return db.scalar("SELECT COUNT(*) FROM phone_codes WHERE user_id = ? AND created_at > ?",
                     (user_id, cutoff), 0)


def send(user_id: int, phone: str, code: str) -> bool:
    """Hand the code to the configured backend.

    Returns True when it really went somewhere. `log` returns False: the code is
    written to the panel's log for the operator, and nobody received anything.
    """
    backend = (settings.sms_backend or "log").strip().lower()

    if backend in ("", "log"):
        log.warning("[orion] code de confirmation pour %s (%s) : %s — aucun envoi reel, "
                    "ce code n'est lisible que dans ce journal", user_id, pretty(phone), code)
        activity.log("phone.code_issued", user_id=user_id, target="backend:log")
        return False

    raise NotImplementedError(
        "ORION_SMS_BACKEND={!r} n'est pas implemente. Implementez send() pour ce "
        "fournisseur (OVH SMS, Twilio, ...) : c'est le seul endroit qui parle a "
        "l'exterieur.".format(backend))


def start(user_id: int, phone: str) -> tuple[bool, str]:
    """Issue a code for this account's number. Returns (ok, message)."""
    if not phone:
        return False, "Numéro de téléphone invalide."

    if _sent_recently(user_id) >= MAX_SENDS_PER_HOUR:
        activity.log("phone.code_throttled", user_id=user_id)
        return False, ("Trop de demandes de code. Patientez une heure, ou écrivez à "
                       "l'administrateur.")

    code = "{:06d}".format(secrets.randbelow(1000000))
    # The previous codes stay: they are the record the hourly limit is counted
    # from, and `verify()` only ever reads the newest one, so an old code cannot
    # confirm anything. Deleting them here made the limit unreachable - a fresh
    # row every time, and a counter that never rose above one.
    db.execute("INSERT INTO phone_codes(user_id, code_hash, expires_at, attempts, created_at) "
               "VALUES(?, ?, datetime('now', ?), 0, ?)",
               (user_id, _hash(user_id, code), "+{} seconds".format(CODE_TTL_SECONDS), now()))
    delivered = send(user_id, phone, code)
    activity.log("phone.code_sent", user_id=user_id, target="delivered" if delivered else "log")

    if delivered:
        return True, "Code envoyé par SMS au {}.".format(pretty(phone))
    return True, ("Aucun fournisseur SMS n'est configuré : le code a été écrit dans le "
                  "journal du panneau, pas envoyé. Voir ORION_SMS_BACKEND.")


def verify(user_id: int, code: str) -> tuple[bool, str]:
    """Check a code. Returns (ok, message)."""
    row = db.one("SELECT * FROM phone_codes WHERE user_id = ? ORDER BY id DESC", (user_id,))
    if row is None:
        return False, "Aucun code en attente. Demandez-en un nouveau."

    expired = db.scalar("SELECT 1 FROM phone_codes WHERE id = ? AND expires_at < datetime('now')",
                        (row["id"],), None)
    if expired:
        return False, "Ce code a expiré. Demandez-en un nouveau."

    if row["attempts"] >= MAX_ATTEMPTS:
        activity.log("phone.code_exhausted", user_id=user_id)
        return False, "Trop de tentatives sur ce code. Demandez-en un nouveau."

    submitted = re.sub(r"\D", "", code or "")
    if not hmac.compare_digest(row["code_hash"], _hash(user_id, submitted)):
        db.execute("UPDATE phone_codes SET attempts = attempts + 1 WHERE id = ?", (row["id"],))
        activity.log("phone.code_wrong", user_id=user_id,
                     detail="tentative {}".format(row["attempts"] + 1))
        return False, "Code incorrect."

    db.execute("DELETE FROM phone_codes WHERE user_id = ?", (user_id,))
    db.execute("UPDATE users SET phone_verified_at = ? WHERE id = ?",
               (now(), user_id))
    activity.log("phone.confirmed", user_id=user_id)
    return True, "Numéro confirmé."
