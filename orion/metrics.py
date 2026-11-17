"""What an instance is using, relative to what it was rented.

Every figure here is relative to the instance's own quota: the state endpoint
reports memory and disk totals as the instance sees them, so a client reads
"412 Mo sur 1024" and never the host's headroom.

CPU needs two readings. Incus publishes a cumulative nanosecond counter, so a
percentage only exists as the difference between two samples - one is kept per
instance for the next poll, and `remember()` lets the page render seed it so the
first poll of a freshly opened page already has something to difference against.
"""

import threading
import time

_lock = threading.Lock()
_last_cpu: dict[str, tuple[float, int]] = {}

# Past this, a stored sample says nothing about now; take a fresh pair instead.
MAX_GAP_S = 180.0


def percent(used, total):
    """used/total as a 0-100 figure, or None when there is nothing to divide.

    A missing reading is not a zero: an empty bar and "unknown" mean different
    things to someone watching an instance.
    """
    if used is None or total is None:
        return None
    try:
        used = float(used)
        total = float(total)
    except (TypeError, ValueError):
        return None
    if total <= 0:
        return None
    return max(0.0, min(100.0, used / total * 100.0))


def cpu_percent(prev_ns, now_ns, elapsed_s, vcpus=1):
    """Average CPU use between two readings, as a share of the instance's cores.

    Two vCPUs burning flat out for a second consume 2e9 ns, which is 100% - not
    200%. Anything above 100 is a counter glitch rather than a real burst.
    """
    if prev_ns is None or now_ns is None:
        return None
    try:
        delta = float(now_ns) - float(prev_ns)
        capacity = float(elapsed_s) * max(1, int(vcpus or 1)) * 1e9
    except (TypeError, ValueError):
        return None
    if capacity <= 0 or delta < 0:
        return None
    return max(0.0, min(100.0, delta / capacity * 100.0))


def remember(incus_name: str, cpu_ns) -> None:
    """Record a CPU reading taken elsewhere (the page render, typically)."""
    if not incus_name or cpu_ns is None:
        return
    with _lock:
        _last_cpu[incus_name] = (time.monotonic(), int(cpu_ns))


def forget(incus_name: str) -> None:
    with _lock:
        _last_cpu.pop(incus_name, None)


def sample(incus_name: str, *, memory_mb: int = 0, vcpu: int = 1, state=None) -> dict:
    """CPU, memory and disk for one instance, or {} when it cannot be read.

    The shape is the daemon's, not a guess. Captured from
    `/1.0/instances/<name>/state`:

        memory.total        1073741824        <- the instance's own cap
        disk.root.usage     2623209472        <- plain integers, side by side
        disk.root.total     10737418240

    Two plausible assumptions are wrong here: `disk.root.usage` is not a
    {used, total} mapping, and the memory cap is reported by the daemon so it
    need not come from the plan at all.
    """
    if state is None:
        return {}
    now = time.monotonic()
    cpu_ns = int((state.get("cpu") or {}).get("usage") or 0)

    with _lock:
        prev = _last_cpu.get(incus_name)
        _last_cpu[incus_name] = (now, cpu_ns)

    prev_ns = prev[1] if prev and (now - prev[0]) <= MAX_GAP_S else None
    elapsed = (now - prev[0]) if prev_ns is not None else 0

    memory = state.get("memory") or {}
    root = (state.get("disk") or {}).get("root") or {}

    def whole(value):
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    return {
        "cpu_pct": cpu_percent(prev_ns, cpu_ns, elapsed, vcpu),
        "cpu_ready": prev_ns is not None,
        "memory_used": whole(memory.get("usage")),
        # The daemon reports the instance's own cap; the plan is only a fallback
        # for a state read that came back without one.
        "memory_total": whole(memory.get("total")) or whole(memory_mb) * 1024 * 1024,
        "disk_used": whole(root.get("usage")),
        "disk_total": whole(root.get("total")),
        "processes": whole(state.get("processes")),
    }
