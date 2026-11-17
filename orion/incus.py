"""Driver for the Incus daemon, over its command line client.

The panel runs in a container with the host's Incus unix socket bind-mounted.
That makes ``incus`` behave exactly as it does on the host, and keeps the
privileged part (the daemon) outside the panel's blast radius.

Reads go through ``incus query`` which returns the daemon's raw JSON, so we
never have to parse human-readable tables.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from .config import settings


class IncusError(RuntimeError):
    def __init__(self, message: str, *, rc: int | None = None, stderr: str = "", stdout: str = ""):
        super().__init__(message)
        self.rc = rc
        self.stderr = (stderr or "").strip()
        self.stdout = (stdout or "").strip()

    @property
    def detail(self) -> str:
        return self.stderr or self.stdout or str(self)


class Incus:
    def __init__(self, cfg=settings):
        self.cfg = cfg

    # ------------------------------------------------------------------ plumbing

    #: subcommands that do not accept the global --project flag
    _NO_PROJECT = {"query"}

    def _argv(self, args: list[str]) -> list[str]:
        if args and args[0] in self._NO_PROJECT:
            # `incus query` rejects --project outright; the project has to go in
            # the query string instead, and we only ever use the default one.
            return [self.cfg.incus_bin, *args]
        return [self.cfg.incus_bin, "--project", self.cfg.incus_project, *args]

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["INCUS_DIR"] = str(Path(self.cfg.incus_socket).parent)
        env.setdefault("HOME", "/root")
        return env

    def run(self, args: list[str], *, timeout: int = 300, check: bool = True,
            stdin_text: str | None = None) -> subprocess.CompletedProcess:
        try:
            proc = subprocess.run(
                self._argv(args),
                capture_output=True,
                text=True,
                timeout=timeout,
                env=self._env(),
                input=stdin_text,
            )
        except FileNotFoundError as exc:
            raise IncusError(f"incus binary not found ({self.cfg.incus_bin})") from exc
        except subprocess.TimeoutExpired as exc:
            raise IncusError(f"incus {' '.join(args)} timed out after {timeout}s") from exc
        if check and proc.returncode != 0:
            raise IncusError(
                f"incus {' '.join(args)} failed (rc={proc.returncode})",
                rc=proc.returncode, stderr=proc.stderr, stdout=proc.stdout,
            )
        return proc

    def query(self, path: str, *, timeout: int = 30):
        """GET a daemon API path and return decoded JSON."""
        proc = self.run(["query", path], timeout=timeout)
        text = (proc.stdout or "").strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise IncusError(f"incus query {path}: invalid JSON") from exc

    # ------------------------------------------------------------------ host

    def available(self) -> tuple[bool, str]:
        try:
            info = self.query("/1.0", timeout=15)
            env = (info or {}).get("environment", {})
            return True, env.get("server_version", "unknown")
        except IncusError as exc:
            return False, exc.detail

    def project_arg_ok(self) -> bool:
        """Whether this client version tolerates --project on query subcommands."""
        proc = self.run(["query", "/1.0"], timeout=15, check=False)
        return proc.returncode == 0

    def server_info(self) -> dict:
        info = self.query("/1.0") or {}
        return info.get("environment", {}) or {}

    def host_resources(self) -> dict:
        return self.query("/1.0/resources") or {}

    def pool_resources(self) -> dict:
        return self.query(f"/1.0/storage-pools/{self.cfg.incus_pool}/resources") or {}

    def pool_info(self) -> dict:
        return self.query(f"/1.0/storage-pools/{self.cfg.incus_pool}") or {}

    # ------------------------------------------------------------------ instances

    def list_instances(self) -> list[dict]:
        proc = self.run(["list", "--format", "json"], timeout=30)
        try:
            data = json.loads(proc.stdout or "[]")
        except json.JSONDecodeError:
            return []
        return data if isinstance(data, list) else []

    def exists(self, name: str) -> bool:
        proc = self.run(["info", name], timeout=20, check=False)
        return proc.returncode == 0

    def state(self, name: str) -> dict:
        return self.query(f"/1.0/instances/{name}/state") or {}

    def config(self, name: str) -> dict:
        return self.query(f"/1.0/instances/{name}") or {}

    def primary_ip(self, name: str) -> str:
        """First global IPv4 address of a running instance.

        The plain instance record carries no runtime state at all - network
        information only exists on the /state endpoint. Reading it from
        `config()` silently returns nothing, which looks exactly like a
        container that failed to get an address.
        """
        state = self.query(f"/1.0/instances/{name}/state") or {}
        for net_name, net in (state.get("network") or {}).items():
            if net_name == "lo":
                continue
            for addr in net.get("addresses", []):
                if addr.get("family") == "inet" and addr.get("scope") == "global":
                    return addr.get("address", "")
        return ""

    def create(self, name: str, *, vcpu: int, memory_mb: int, disk_gb: int,
               image: str | None = None, timeout: int = 600) -> None:
        args = [
            "launch", image or self.cfg.incus_image, name,
            "-c", f"limits.cpu={vcpu}",
            "-c", f"limits.memory={memory_mb}MiB",
            "-c", "boot.autostart=true",
            "-d", f"root,size={disk_gb}GiB",
        ]
        self.run(args, timeout=timeout)

    def apply_limits(self, name: str, *, vcpu: int, memory_mb: int, disk_gb: int) -> None:
        for key, value in (("limits.cpu", str(vcpu)), ("limits.memory", f"{memory_mb}MiB")):
            self.run(["config", "set", name, key, value], timeout=30)
        self.run(["config", "device", "set", name, "root", "size", f"{disk_gb}GiB"], timeout=30)

    def start(self, name: str, timeout: int = 180) -> None:
        self.run(["start", name], timeout=timeout)

    def stop(self, name: str, timeout: int = 180) -> None:
        self.run(["stop", name, "--force"], timeout=timeout)

    def restart(self, name: str, timeout: int = 240) -> None:
        self.run(["restart", name], timeout=timeout)

    def delete(self, name: str, timeout: int = 240) -> None:
        self.run(["delete", "--force", name], timeout=timeout)

    def exec(self, name: str, command: str | list[str], *, timeout: int = 900) -> tuple[int, str, str]:
        """Run a command inside an instance.

        A string is executed with ``bash -lc``; a list is passed as argv.
        """
        if isinstance(command, str):
            args = ["exec", name, "--", "bash", "-lc", command]
        else:
            args = ["exec", name, "--", *command]
        proc = self.run(args, timeout=timeout, check=False)
        return proc.returncode, proc.stdout or "", proc.stderr or ""

    def push_text(self, name: str, content: str, dest: str, *, mode: str = "0644") -> None:
        fd, tmp = tempfile.mkstemp(prefix="orion-push-")
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(content)
            os.chmod(tmp, 0o600)
            self.run(["file", "push", "--mode", mode, "--create-dirs", tmp, f"{name}{dest}"], timeout=120)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    # ------------------------------------------------------------------ snapshots

    def snapshots(self, name: str) -> list[dict]:
        return self.query(f"/1.0/instances/{name}/snapshots?recursion=1") or []

    def snapshot_create(self, name: str, snapshot: str, *, stateful: bool = False) -> None:
        args = ["snapshot", "create", name, snapshot]
        if stateful:
            args.append("--stateful")
        self.run(args, timeout=300)

    def snapshot_restore(self, name: str, snapshot: str) -> None:
        self.run(["snapshot", "restore", name, snapshot], timeout=300)

    def snapshot_delete(self, name: str, snapshot: str) -> None:
        self.run(["snapshot", "delete", name, snapshot], timeout=120)

    # ------------------------------------------------------------------ devices

    def set_ssh_proxy(self, name: str, port: int, *, device: str = "ssh-access") -> None:
        """Publish the instance's sshd on the host, so clients can `ssh -p PORT`."""
        self.run(["config", "device", "remove", name, device], timeout=30, check=False)
        self.run([
            "config", "device", "add", name, device, "proxy",
            f"listen=tcp:0.0.0.0:{port}",
            "connect=tcp:127.0.0.1:22",
        ], timeout=60)

    def remove_device(self, name: str, device: str) -> None:
        self.run(["config", "device", "remove", name, device], timeout=30, check=False)


incus = Incus()
