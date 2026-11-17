"""A deliberately small background worker.

Provisioning a container takes minutes, so it cannot happen inside a request.
One daemon thread per job is enough for a single-host panel and keeps the
implementation readable - no broker, no scheduler process.
"""

from __future__ import annotations

import threading
import traceback
from collections.abc import Callable

from . import db
from .util import now

_threads: dict[int, threading.Thread] = {}
_lock = threading.Lock()


def start(kind: str, instance_id: int | None, fn: Callable[[int], str]) -> int:
    """Queue `fn` and return the job id. `fn` returns the log it produced."""
    job_id = db.execute(
        "INSERT INTO jobs(kind, instance_id, status, created_at) VALUES(?, ?, 'queued', ?)",
        (kind, instance_id, now()),
    )

    def _runner() -> None:
        db.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (job_id,))
        try:
            output = fn(job_id) or ""
            db.execute(
                "UPDATE jobs SET status = 'done', log = ?, finished_at = ? WHERE id = ?",
                (output[-200_000:], now(), job_id),
            )
        except Exception as exc:  # noqa: BLE001 - job boundary, must not kill the panel
            detail = f"{exc}\n\n{traceback.format_exc()}"
            db.execute(
                "UPDATE jobs SET status = 'failed', log = ?, finished_at = ? WHERE id = ?",
                (detail[-200_000:], now(), job_id),
            )
        finally:
            with _lock:
                _threads.pop(job_id, None)

    thread = threading.Thread(target=_runner, name=f"orion-job-{job_id}", daemon=True)
    with _lock:
        _threads[job_id] = thread
    thread.start()
    return job_id


def running() -> int:
    with _lock:
        return len(_threads)


def get(job_id: int):
    return db.one("SELECT * FROM jobs WHERE id = ?", (job_id,))


def append_log(job_id: int, text: str) -> None:
    """Let a long-running job publish progress as it goes."""
    db.execute("UPDATE jobs SET log = log || ? WHERE id = ?", (text, job_id))


def latest_for_instance(instance_id: int):
    return db.one(
        "SELECT * FROM jobs WHERE instance_id = ? ORDER BY id DESC LIMIT 1", (instance_id,))
