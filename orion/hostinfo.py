"""Discover the machine Orion lives on, and decide what it can sell.

This is the "recognise which machine it is deployed on" part: instead of an
operator typing in a capacity table, Orion asks Incus for the real CPU, memory
and storage figures and derives the tiers from them.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, asdict

from .config import settings
from .incus import IncusError, incus
from .util import now

_CACHE_TTL = 20
_cache: dict[str, object] = {"at": 0.0, "value": None}

MIB = 1024 * 1024


@dataclass
class HostInfo:
    reachable: bool
    error: str
    host_name: str
    incus_version: str
    kernel: str
    os_name: str
    firewall: str
    cpu_total: int
    cpu_model: str
    ram_total_mb: int
    swap_total_mb: int
    disk_total_gb: int
    disk_used_gb: int
    pool_name: str
    pool_total_gb: int
    pool_used_gb: int
    reserve_vcpu: int
    reserve_ram_mb: int
    reserve_disk_gb: int
    sellable_vcpu: int
    sellable_ram_mb: int
    sellable_disk_gb: int
    detected_at: str

    def as_dict(self) -> dict:
        return asdict(self)


def _cpu_model(resources: dict) -> str:
    for socket in (resources.get("cpu") or {}).get("sockets", []) or []:
        product = (socket.get("product") or "").strip()
        if product:
            return product
    arch = (resources.get("cpu") or {}).get("architecture", "")
    return arch or "unknown"


def detect(force: bool = False) -> HostInfo:
    if not force and _cache["value"] is not None and (time.time() - float(_cache["at"])) < _CACHE_TTL:
        return _cache["value"]  # type: ignore[return-value]

    info = HostInfo(
        reachable=False, error="", host_name="unknown", incus_version="", kernel="",
        os_name="", firewall="", cpu_total=0, cpu_model="", ram_total_mb=0, swap_total_mb=0,
        disk_total_gb=0, disk_used_gb=0, pool_name=settings.incus_pool, pool_total_gb=0,
        pool_used_gb=0, reserve_vcpu=settings.reserve_vcpu, reserve_ram_mb=settings.reserve_ram_mb,
        reserve_disk_gb=settings.reserve_disk_gb, sellable_vcpu=0, sellable_ram_mb=0,
        sellable_disk_gb=0, detected_at=now(),
    )

    try:
        env = incus.server_info()
        resources = incus.host_resources()
        pool_res = incus.pool_resources()
    except IncusError as exc:
        info.error = exc.detail
        _cache.update(at=time.time(), value=info)
        return info

    info.reachable = True
    info.host_name = env.get("server_name", "unknown")
    info.incus_version = env.get("server_version", "")
    info.kernel = env.get("kernel_version", "")
    info.os_name = f"{env.get('os_name', '')} {env.get('os_version', '')}".strip()
    info.firewall = env.get("firewall", "")

    cpu = resources.get("cpu") or {}
    info.cpu_total = int(cpu.get("total") or 0)
    info.cpu_model = _cpu_model(resources)
    memory = resources.get("memory") or {}
    info.ram_total_mb = int((memory.get("total") or 0) // MIB)
    info.swap_total_mb = int(((memory.get("swap") or [{}])[0].get("size") or 0) // MIB)

    # Prefer the Incus pool (that is the space we actually hand out), fall back
    # to the host's total storage when the pool is not visible.
    space = (pool_res.get("space") or {})
    if space.get("total"):
        info.pool_total_gb = int(space["total"] // (1024 ** 3))
        info.pool_used_gb = int((space.get("used") or 0) // (1024 ** 3))
    info.disk_total_gb = info.pool_total_gb
    info.disk_used_gb = info.pool_used_gb

    # Operator overrides win, so a host can be deliberately oversold or capped.
    if settings.cpu_total_override:
        info.cpu_total = settings.cpu_total_override
    if settings.ram_total_override:
        info.ram_total_mb = settings.ram_total_override
    if settings.disk_total_override:
        info.disk_total_gb = settings.disk_total_override

    info.sellable_vcpu = max(0, info.cpu_total - info.reserve_vcpu)
    info.sellable_ram_mb = max(0, info.ram_total_mb - info.reserve_ram_mb)
    info.sellable_disk_gb = max(0, info.disk_total_gb - info.reserve_disk_gb)

    _cache.update(at=time.time(), value=info)
    return info


def invalidate() -> None:
    _cache.update(at=0.0, value=None)


@dataclass
class Allocation:
    vcpu: int = 0
    ram_mb: int = 0
    disk_gb: int = 0
    instances: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


#: instance statuses that still hold host resources
LIVE_STATUSES = ("provisioning", "running", "stopped", "suspended")


def allocation() -> Allocation:
    """What has already been promised to tenants."""
    from . import db

    rows = db.query(
        "SELECT p.vcpu, p.memory_mb, p.disk_gb FROM instances i "
        "LEFT JOIN plans p ON p.id = i.plan_id "
        f"WHERE i.status IN ({','.join('?' * len(LIVE_STATUSES))})",
        LIVE_STATUSES,
    )
    alloc = Allocation()
    for row in rows:
        alloc.instances += 1
        alloc.vcpu += int(row["vcpu"] or 0)
        alloc.ram_mb += int(row["memory_mb"] or 0)
        alloc.disk_gb += int(row["disk_gb"] or 0)
    return alloc


def plan_capacity(info: HostInfo, plan) -> int:
    """How many instances of `plan` the host can still carry."""
    return max(0, min(
        info.sellable_vcpu // max(1, plan["vcpu"]),
        info.sellable_ram_mb // max(1, plan["memory_mb"]),
        info.sellable_disk_gb // max(1, plan["disk_gb"]),
    ))


def headroom(info: HostInfo) -> Allocation:
    alloc = allocation()
    return Allocation(
        vcpu=max(0, info.sellable_vcpu - alloc.vcpu),
        ram_mb=max(0, info.sellable_ram_mb - alloc.ram_mb),
        disk_gb=max(0, info.sellable_disk_gb - alloc.disk_gb),
        instances=alloc.instances,
    )


def fits(info: HostInfo, vcpu: int, memory_mb: int, disk_gb: int) -> tuple[bool, str]:
    alloc = allocation()
    checks = (
        (alloc.vcpu + vcpu, info.sellable_vcpu, "vCPU"),
        (alloc.ram_mb + memory_mb, info.sellable_ram_mb, "RAM (MB)"),
        (alloc.disk_gb + disk_gb, info.sellable_disk_gb, "disque (GB)"),
    )
    for wanted, available, label in checks:
        if wanted > available:
            return False, label
    return True, ""


def fingerprint(info: HostInfo) -> str:
    return f"{info.host_name}:{info.cpu_total}:{info.ram_total_mb}:{info.pool_total_gb}"
