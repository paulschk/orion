"""Email confirmation, and the sending of it.

A new account is not confirmed until the address it gave answers back. The
confirmation is a single-use link carrying a random token: the token is stored
hashed, never in clear, expires, and works once. Sending is capped per account so
the panel cannot be turned into a way to write to somebody's inbox on demand.

Three backends, and the panel is honest about which one it is using:

  log       the link goes to the panel's log; nothing is sent, and the
            confirmation page says so. The default, so the whole flow is
            reviewable on a machine with no mail account anywhere.
  sendmail  hand the message to the local MTA (`sendmail -t`). Whether it lands
            then depends on this host's mail configuration, its DNS and its
            reputation - the panel does not pretend to know.
  smtp      talk to a relay directly (ORION_SMTP_HOST and friends). The one to
            configure for a provider that will actually deliver.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import smtplib
import subprocess
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

from . import activity, db
from .config import settings
from .util import now

log = logging.getLogger("orion.mail")

TOKEN_TTL_SECONDS = 48 * 3600
MAX_SENDS_PER_HOUR = 5


def backend() -> str:
    return (settings.mail_backend or "log").strip().lower()


def delivering() -> bool:
    """True when the backend attempts real delivery."""
    return backend() in ("sendmail", "smtp")


def _message(to: str, subject: str, body: str) -> EmailMessage:
    message = EmailMessage()
    message["From"] = settings.mail_from
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    return message


def send(to: str, subject: str, body: str) -> bool:
    """Send, or write to the log. Returns True when delivery was attempted."""
    which = backend()

    if which in ("", "log"):
        log.warning("[orion] courriel non envoye a %s : %s\n%s", to, subject, body)
        return False

    if which == "sendmail":
        try:
            completed = subprocess.run(
                ["sendmail", "-t", "-i"], input=_message(to, subject, body).as_bytes(),
                capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            log.error("[orion] sendmail indisponible : %s", exc)
            return False
        if completed.returncode != 0:
            log.error("[orion] sendmail a refuse : %s",
                      completed.stderr.decode("utf-8", "replace")[:300])
            return False
        return True

    if which == "smtp":
        try:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
                smtp.ehlo()
                if smtp.has_extn("starttls"):
                    smtp.starttls()
                    smtp.ehlo()
                if settings.smtp_user:
                    smtp.login(settings.smtp_user, settings.smtp_password)
                smtp.send_message(_message(to, subject, body))
        except (OSError, smtplib.SMTPException) as exc:
            log.error("[orion] envoi SMTP refuse : %s", exc)
            return False
        return True

    raise NotImplementedError(
        "ORION_MAIL_BACKEND={!r} n'est pas implemente : log, sendmail ou smtp.".format(which))


def _hash(user_id: int, token: str) -> str:
    return hmac.new(settings.secret_key.encode(), "{}:{}".format(user_id, token).encode(),
                    hashlib.sha256).hexdigest()


def _sent_recently(user_id: int) -> int:
    """How many links this account asked for in the last hour.

    The cutoff is written the way `util.now()` writes it, not by SQLite: the
    column is Python's ISO form, and comparing it with SQLite's space-separated
    `datetime('now')` makes every row look recent.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
    return db.scalar("SELECT COUNT(*) FROM email_tokens WHERE user_id = ? AND created_at > ?",
                     (user_id, cutoff), 0)


def confirmation_url(token: str) -> str:
    return "{}/register/confirmer?token={}".format(settings.base_url.rstrip("/"), token)


def issue(user_row) -> tuple[bool, str]:
    """Create a fresh confirmation link and send it. Returns (ok, message)."""
    email = user_row["email"]
    if _sent_recently(user_row["id"]) >= MAX_SENDS_PER_HOUR:
        activity.log("email.throttled", user_id=user_row["id"], target=email)
        return False, ("Trop d'envois pour ce compte. Patientez une heure, ou écrivez à "
                       "l'administrateur.")

    token = secrets.token_urlsafe(32)
    db.execute(
        "INSERT INTO email_tokens(user_id, token_hash, used_at, expires_at, created_at) "
        "VALUES(?, ?, NULL, datetime('now', ?), ?)",
        (user_row["id"], _hash(user_row["id"], token), "+{} seconds".format(TOKEN_TTL_SECONDS),
         now()))

    url = confirmation_url(token)
    sent = send(email,
                "Confirmez votre adresse — {}".format(settings.app_name),
                "Bonjour,\n\n"
                "Quelqu'un a créé un compte sur {app} avec cette adresse. Pour l'activer, "
                "ouvrez ce lien :\n\n{url}\n\n"
                "Il est valable 48 heures et ne sert qu'une fois. Si vous n'êtes pas à "
                "l'origine de cette demande, ignorez ce message : le compte restera "
                "inutilisable.\n".format(app=settings.app_name, url=url))
    activity.log("email.confirmation_sent", user_id=user_row["id"], target=email,
                 detail="backend:{}".format(backend()))

    if sent:
        return True, "Un lien de confirmation a été envoyé à {}.".format(email)
    return True, ("Le lien n'a pas pu être envoyé : il est dans le journal du panneau, "
                  "personne ne l'a reçu. Vérifiez ORION_MAIL_BACKEND et le service "
                  "d'envoi sur cet hôte.")


def confirm(token: str) -> tuple[bool, str, dict | None]:
    """Spend a confirmation token. Returns (ok, message, user row)."""
    if not token or len(token) < 20:
        return False, "Lien de confirmation invalide.", None

    candidates = db.query("SELECT * FROM email_tokens WHERE used_at IS NULL "
                          "AND expires_at > datetime('now') ORDER BY id DESC LIMIT 200")
    for row in candidates:
        if not hmac.compare_digest(row["token_hash"], _hash(row["user_id"], token)):
            continue
        user = db.one("SELECT * FROM users WHERE id = ?", (row["user_id"],))
        if user is None:
            return False, "Compte introuvable.", None
        db.execute("UPDATE email_tokens SET used_at = ? WHERE id = ?", (now(), row["id"]))
        db.execute("UPDATE users SET email_verified_at = ? WHERE id = ?", (now(), user["id"]))
        activity.log("email.confirmed", user_id=user["id"], target=user["email"])
        return True, "Adresse confirmée.", db.one("SELECT * FROM users WHERE id = ?", (user["id"],))
    return False, "Ce lien n'est plus valable. Demandez-en un nouveau.", None


def confirmed(user_row) -> bool:
    return bool(user_row is not None and user_row["email_verified_at"])
