"""Mihomo as a private, per-purpose protocol engine.

Each running profile gets its own Mihomo process with exactly one outbound and
one loopback listener bound to it; a probe gets one process with one listener
per node. Nothing here uses a global selector, so changing what one profile
uses can never re-route another.

The configuration is generated, never taken from a subscription, and checked by
:func:`validate_runtime_config` before it is written:

* listeners bind 127.0.0.1 only and require their own random credentials, so a
  reused port or another local program cannot ride a profile's exit unnoticed;
* every listener names its outbound directly; the only rule is ``MATCH,REJECT``
  and no ``DIRECT`` outbound exists, so a dead node fails instead of going direct;
* no controller, TUN, sniffer, fake-ip or global inbound port;
* DNS uses ``redir-host`` with encrypted upstreams for resolving node servers;
  browser destinations travel to the node as hostnames and resolve remotely.

Processes run with a scrubbed environment (no proxy variables), a private 0700
working directory and a 0600 configuration, and are reaped on the next start if
the manager that launched them died.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psutil
from loguru import logger

MIN_VERSION = (1, 18, 0)
READY_TIMEOUT = 8.0
START_ATTEMPTS = 3
# Encrypted resolvers for node *server* names; addresses avoid a bootstrap lookup.
DNS_NAMESERVERS = ["https://1.1.1.1/dns-query", "https://8.8.8.8/dns-query"]
DNS_PROXY_SERVER_NAMESERVERS = ["https://223.5.5.5/dns-query", "https://1.12.12.12/dns-query"]
_VERSION = re.compile(r"Mihomo(?: Meta)?\s+v?(\d+)\.(\d+)\.(\d+)", re.I)


class MihomoUnavailable(RuntimeError):
    """The engine cannot run. ``code`` is stable and the message is safe to show."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class MihomoBinary:
    path: Path
    version: str


@dataclass
class Listener:
    name: str
    outbound: str
    port: int
    username: str
    password: str

    @property
    def browser_proxy(self) -> dict[str, str]:
        return {
            "server": f"http://127.0.0.1:{self.port}",
            "username": self.username,
            "password": self.password,
        }


# --- Binary discovery --------------------------------------------------------

_binary_cache: dict[tuple[str, int, int], MihomoBinary] = {}


def _executable_name() -> str:
    return "mihomo.exe" if os.name == "nt" else "mihomo"


def candidate_paths(configured: str | None, data_dir: Path | None) -> list[Path]:
    """Where a binary may come from, most explicit first."""
    candidates: list[Path] = []
    for value in (configured, os.environ.get("CPM_MIHOMO_PATH")):
        if value:
            candidates.append(Path(value).expanduser())
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        candidates.append(Path(bundle) / "mihomo" / _executable_name())
    if data_dir is not None:
        candidates.append(Path(data_dir) / "bin" / _executable_name())
    # Source checkouts: scripts/prepare_mihomo.py installs here (git-ignored).
    candidates.append(
        Path(__file__).resolve().parents[3] / ".cache" / "mihomo" / _executable_name()
    )
    found = shutil.which("mihomo")
    if found:
        candidates.append(Path(found))
    return candidates


def validate_binary(path: Path) -> MihomoBinary:
    """Refuse binaries that would run with other privileges or could be swapped."""
    try:
        resolved = path.resolve(strict=True)
        info = resolved.stat()
    except (OSError, RuntimeError):
        raise MihomoUnavailable("binary_missing", "The Mihomo executable was not found.") from None
    if not stat.S_ISREG(info.st_mode):
        raise MihomoUnavailable("binary_invalid", "The Mihomo path is not a regular file.")
    if os.name != "nt":
        if info.st_mode & (stat.S_ISUID | stat.S_ISGID):
            # A setuid engine would run each profile's proxy with its owner's
            # privileges, and its children could not be stopped by this manager.
            raise MihomoUnavailable(
                "binary_setuid",
                "This Mihomo executable is setuid/setgid. Copy it with "
                "scripts/prepare_mihomo.py, which installs a normal user-owned copy.",
            )
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise MihomoUnavailable(
                "binary_writable", "The Mihomo executable is writable by other users."
            )
        if info.st_uid not in (os.geteuid(), 0):
            raise MihomoUnavailable(
                "binary_owner", "The Mihomo executable is owned by another user."
            )
        if not os.access(resolved, os.X_OK):
            raise MihomoUnavailable("binary_invalid", "The Mihomo file is not executable.")
    key = (str(resolved), info.st_size, int(info.st_mtime))
    if key in _binary_cache:
        return _binary_cache[key]
    try:
        result = subprocess.run(
            [str(resolved), "-v"],
            capture_output=True,
            timeout=10,
            env=_child_environment(None),
            check=False,
            **_creation_flags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        raise MihomoUnavailable("binary_invalid", "The Mihomo executable did not run.") from None
    match = _VERSION.search(result.stdout.decode("utf-8", "replace"))
    if not match:
        raise MihomoUnavailable("binary_invalid", "The file does not report a Mihomo version.")
    version = tuple(int(part) for part in match.groups())
    if version < MIN_VERSION:
        raise MihomoUnavailable(
            "binary_outdated",
            f"Mihomo {'.'.join(map(str, version))} is too old; "
            f"{'.'.join(map(str, MIN_VERSION))} or newer is required.",
        )
    binary = MihomoBinary(resolved, ".".join(map(str, version)))
    _binary_cache[key] = binary
    return binary


def discover_binary(configured: str | None, data_dir: Path | None) -> MihomoBinary:
    last: MihomoUnavailable | None = None
    explicit = bool(configured or os.environ.get("CPM_MIHOMO_PATH"))
    for index, candidate in enumerate(candidate_paths(configured, data_dir)):
        if not candidate.exists():
            continue
        try:
            return validate_binary(candidate)
        except MihomoUnavailable as exc:
            # An explicitly configured binary that fails is an answer, not a hint.
            if explicit and index == 0:
                raise
            last = exc
    raise last or MihomoUnavailable(
        "binary_missing",
        "Mihomo is not installed. Set its path in Settings → Network, or run "
        "scripts/prepare_mihomo.py.",
    )


# --- Configuration -----------------------------------------------------------


def runtime_config(outbounds: dict[str, dict[str, Any]], listeners: list[Listener]) -> dict:
    config = {
        "allow-lan": False,
        "bind-address": "127.0.0.1",
        "mode": "rule",
        "log-level": "warning",
        "ipv6": False,
        "unified-delay": False,
        "tcp-concurrent": False,
        "find-process-mode": "off",
        "geodata-mode": False,
        "geo-auto-update": False,
        "profile": {"store-selected": False, "store-fake-ip": False},
        "sniffer": {"enable": False},
        "tun": {"enable": False},
        "dns": {
            "enable": True,
            "ipv6": False,
            "enhanced-mode": "redir-host",
            "use-hosts": False,
            "default-nameserver": ["223.5.5.5", "1.1.1.1"],
            "nameserver": list(DNS_NAMESERVERS),
            "proxy-server-nameserver": list(DNS_PROXY_SERVER_NAMESERVERS),
        },
        "proxies": [{**outbound, "name": name} for name, outbound in outbounds.items()],
        "listeners": [
            {
                "name": listener.name,
                "type": "mixed",
                "listen": "127.0.0.1",
                "port": listener.port,
                "proxy": listener.outbound,
                "udp": False,
                "users": [{"username": listener.username, "password": listener.password}],
            }
            for listener in listeners
        ],
        "rules": ["MATCH,REJECT"],
    }
    validate_runtime_config(config)
    return config


def validate_runtime_config(config: dict[str, Any]) -> None:
    """The safety invariants of every generated configuration, as assertions."""
    forbidden = {
        "external-controller",
        "external-controller-tls",
        "external-controller-unix",
        "external-controller-pipe",
        "port",
        "socks-port",
        "mixed-port",
        "redir-port",
        "tproxy-port",
        "proxy-groups",
        "proxy-providers",
        "rule-providers",
        "script",
    }
    present = forbidden & set(config)
    if present:
        raise ValueError(f"Generated configuration contains {sorted(present)}")
    if config.get("allow-lan") is not False or config.get("bind-address") != "127.0.0.1":
        raise ValueError("Generated configuration must stay on loopback")
    if config.get("tun", {}).get("enable"):
        raise ValueError("Generated configuration must not enable TUN")
    if config.get("dns", {}).get("enhanced-mode") != "redir-host":
        raise ValueError("Generated configuration must not use fake-ip")
    if config.get("rules") != ["MATCH,REJECT"]:
        raise ValueError("Generated configuration must reject unmatched traffic")
    names = {proxy["name"] for proxy in config.get("proxies", [])}
    for proxy in config.get("proxies", []):
        if str(proxy.get("type", "")).lower() in {"direct", "reject", "dns", "pass"}:
            raise ValueError("Generated configuration must not contain local outbounds")
        if "dialer-proxy" in proxy:
            raise ValueError("Generated configuration must not chain outbounds")
    listeners = config.get("listeners", [])
    if not listeners:
        raise ValueError("Generated configuration needs a listener")
    for listener in listeners:
        if listener.get("listen") != "127.0.0.1":
            raise ValueError("Listeners must bind 127.0.0.1")
        if listener.get("proxy") not in names:
            raise ValueError("Each listener must name its own outbound")
        if not listener.get("users"):
            raise ValueError("Listeners must require credentials")


# --- Processes ---------------------------------------------------------------


def _creation_flags() -> dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def _child_environment(workdir: Path | None) -> dict[str, str]:
    """No inherited proxies, no inherited configuration paths."""
    env = {"PATH": os.environ.get("PATH", os.defpath)}
    for key in ("SystemRoot", "WINDIR", "LANG", "LC_ALL"):
        if key in os.environ:
            env[key] = os.environ[key]
    if workdir is not None:
        env["HOME"] = str(workdir)
        env["TMPDIR"] = str(workdir)
        env["XDG_CONFIG_HOME"] = str(workdir)
    return env


def _reserve_ports(count: int) -> list[int]:
    sockets = []
    try:
        for _ in range(count):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(("127.0.0.1", 0))
            sockets.append(sock)
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


def _listening_ports(pid: int) -> set[int] | None:
    try:
        connections = psutil.Process(pid).net_connections(kind="tcp")
    except (psutil.AccessDenied, psutil.ZombieProcess):
        return None
    except psutil.NoSuchProcess:
        return set()
    return {
        conn.laddr.port
        for conn in connections
        if conn.status == psutil.CONN_LISTEN and conn.laddr and conn.laddr.ip == "127.0.0.1"
    }


def classify_log(lines: list[str]) -> tuple[str, str] | None:
    """Turn Mihomo output into a stable error class without echoing it."""
    text = "\n".join(lines).lower()
    patterns = [
        (
            "port_conflict",
            ("address already in use", "only one usage of each socket"),
            "A local port was taken while Mihomo started.",
        ),
        (
            "config_invalid",
            (
                "parse config error",
                "yaml:",
                "unmarshal",
                "invalid config",
                "proxy not found",
                "unsupported type",
            ),
            "Mihomo rejected the node configuration.",
        ),
        (
            "node_auth_failed",
            (
                "authentication failed",
                "auth failed",
                "unauthorized",
                "bad password",
                "invalid user",
            ),
            "The node rejected its credentials.",
        ),
        (
            "node_tls_failed",
            ("x509", "tls:", "certificate"),
            "The TLS connection to the node failed.",
        ),
        (
            "node_timeout",
            ("i/o timeout", "context deadline exceeded", "timeout"),
            "The node did not answer in time.",
        ),
        (
            "node_unreachable",
            (
                "connection refused",
                "no route to host",
                "network is unreachable",
                "connect error",
                "dial tcp",
                "no such host",
            ),
            "The node could not be reached.",
        ),
    ]
    for code, needles, message in patterns:
        if any(needle in text for needle in needles):
            return code, message
    return None


@dataclass
class _Owner:
    manager_pid: int
    manager_started: float
    pid: int
    pid_started: float
    config: str


class MihomoProcess:
    """One Mihomo instance with its listeners. Owns its process and directory."""

    def __init__(
        self,
        binary: MihomoBinary,
        outbounds: dict[str, dict[str, Any]],
        listener_outbounds: list[str],
        runtime_root: Path,
        *,
        purpose: str,
    ):
        self.binary = binary
        self.outbounds = outbounds
        self._listener_outbounds = listener_outbounds
        self.runtime_root = runtime_root
        self.purpose = purpose
        self.listeners: list[Listener] = []
        self.process: asyncio.subprocess.Process | None = None
        self.workdir: Path | None = None
        self._log: deque[str] = deque(maxlen=80)
        self._reader: asyncio.Task[None] | None = None
        self._waiter: asyncio.Task[int] | None = None
        self._closed = False
        self._exit_callbacks: list[Callable[[MihomoProcess], Awaitable[None]]] = []
        self.started_at: float | None = None

    # Lifecycle ------------------------------------------------------------

    async def start(self, ports: list[int] | None = None) -> MihomoProcess:
        last: MihomoUnavailable | None = None
        for attempt in range(START_ATTEMPTS if ports is None else 1):
            try:
                await self._start_once(ports)
                return self
            except MihomoUnavailable as exc:
                last = exc
                await self._stop_process()
                if exc.code != "port_conflict":
                    break
                logger.info(f"Mihomo port conflict, retrying ({attempt + 1}/{START_ATTEMPTS})")
        await self.close()
        assert last is not None
        raise last

    async def _start_once(self, ports: list[int] | None) -> None:
        from .update_permissions import create_private_directory, create_private_file

        self.runtime_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Resolved so platform aliases (macOS /var → /private/var) are not
        # mistaken for planted symlinks by the private-path checks.
        root = self.runtime_root.resolve()
        if self.workdir is None:
            workdir = root / f"{self.purpose}-{secrets.token_hex(6)}"
            create_private_directory(workdir)
            self.workdir = workdir
        chosen = ports or _reserve_ports(len(self._listener_outbounds))
        if self.listeners and ports:
            for listener, port in zip(self.listeners, chosen, strict=True):
                listener.port = port
        else:
            self.listeners = [
                Listener(
                    name=f"in-{index}",
                    outbound=outbound,
                    port=port,
                    username="cpm-" + secrets.token_hex(4),
                    password=secrets.token_urlsafe(18),
                )
                for index, (outbound, port) in enumerate(
                    zip(self._listener_outbounds, chosen, strict=True)
                )
            ]
        config = runtime_config(self.outbounds, self.listeners)
        config_path = self.workdir / "config.yaml"
        if config_path.exists():
            config_path.unlink()
        descriptor = create_private_file(config_path)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            # JSON is YAML: no tags, anchors or implicit typing for provider strings.
            json.dump(config, handle, ensure_ascii=False)
        self._log.clear()
        self.process = await asyncio.create_subprocess_exec(
            str(self.binary.path),
            "-d",
            str(self.workdir),
            "-f",
            str(config_path),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=_child_environment(self.workdir),
            cwd=str(self.workdir),
            **_creation_flags(),
        )
        self._reader = asyncio.create_task(self._read_output(self.process))
        self._write_owner(config_path)
        await self._wait_ready()
        self.started_at = time.time()
        self._waiter = asyncio.create_task(self._watch_exit(self.process))

    async def _read_output(self, process: asyncio.subprocess.Process) -> None:
        assert process.stdout is not None
        secrets_seen = self._secrets()
        while line := await process.stdout.readline():
            text = line.decode("utf-8", "replace").rstrip()
            for secret in secrets_seen:
                text = text.replace(secret, "[redacted]")
            self._log.append(text[:500])

    def _secrets(self) -> list[str]:
        values: list[str] = []
        for listener in self.listeners:
            values.extend([listener.password, listener.username])

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    if (
                        key
                        in {
                            "password",
                            "uuid",
                            "psk",
                            "private-key",
                            "auth",
                            "auth-str",
                            "token",
                            "obfs-password",
                            "pre-shared-key",
                            "username",
                        }
                        and isinstance(item, str)
                        and len(item) >= 4
                    ):
                        values.append(item)
                    walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)

        walk(self.outbounds)
        return sorted(set(values), key=len, reverse=True)

    def _write_owner(self, config_path: Path) -> None:
        assert self.workdir is not None and self.process is not None
        try:
            child_started = psutil.Process(self.process.pid).create_time()
        except psutil.Error:
            child_started = 0.0
        owner = _Owner(
            manager_pid=os.getpid(),
            manager_started=psutil.Process(os.getpid()).create_time(),
            pid=self.process.pid,
            pid_started=child_started,
            config=str(config_path),
        )
        (self.workdir / "owner.json").write_text(json.dumps(owner.__dict__))

    async def _wait_ready(self) -> None:
        assert self.process is not None
        expected = {listener.port for listener in self.listeners}
        deadline = time.monotonic() + READY_TIMEOUT
        while time.monotonic() < deadline:
            if self.process.returncode is not None:
                break
            ports = _listening_ports(self.process.pid)
            if ports is not None and expected <= ports:
                return
            if ports is None and all(_port_open(port) for port in expected):
                # Without socket ownership data, an open port could belong to
                # another program; listener credentials still prevent use of it.
                return
            classified = classify_log(list(self._log))
            if classified and classified[0] in {"port_conflict", "config_invalid"}:
                raise MihomoUnavailable(*classified)
            await asyncio.sleep(0.05)
        classified = classify_log(list(self._log))
        if classified and classified[0] in {"port_conflict", "config_invalid"}:
            raise MihomoUnavailable(*classified)
        if self.process.returncode is not None:
            raise MihomoUnavailable("engine_exited", "Mihomo stopped while starting.")
        raise MihomoUnavailable("engine_timeout", "Mihomo did not become ready in time.")

    async def _watch_exit(self, process: asyncio.subprocess.Process) -> int:
        code = await process.wait()
        if not self._closed and process is self.process:
            logger.warning(f"Mihomo ({self.purpose}) exited unexpectedly with code {code}")
            for callback in list(self._exit_callbacks):
                try:
                    await callback(self)
                except Exception as exc:  # noqa: BLE001 - a callback must not break teardown
                    logger.warning(f"Mihomo exit handler failed: {exc}")
        return code

    def on_exit(self, callback: Callable[[MihomoProcess], Awaitable[None]]) -> None:
        self._exit_callbacks.append(callback)

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.returncode is None and not self._closed

    def last_error(self) -> tuple[str, str] | None:
        return classify_log(list(self._log))

    async def restart(self) -> None:
        """Start again on the same ports and credentials (reconnect after a crash)."""
        if self.alive:
            return
        await self._stop_process()
        ports = [listener.port for listener in self.listeners]
        self._closed = False
        await self._start_once(ports)

    async def _stop_process(self) -> None:
        process, self.process = self.process, None
        waiter, self._waiter = self._waiter, None
        if process is not None:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 3)
                except asyncio.TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
                    await process.wait()
            else:
                await process.wait()
        if waiter is not None:
            await asyncio.gather(waiter, return_exceptions=True)
        reader, self._reader = self._reader, None
        if reader is not None:
            await asyncio.gather(reader, return_exceptions=True)

    async def close(self) -> None:
        self._closed = True
        task = asyncio.ensure_future(self._close())
        # Teardown completes even if the caller is cancelled meanwhile.
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    async def _close(self) -> None:
        await self._stop_process()
        workdir, self.workdir = self.workdir, None
        if workdir is not None:
            await asyncio.to_thread(shutil.rmtree, workdir, True)


def _port_open(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def reap_orphans(runtime_root: Path) -> int:
    """Stop engines whose launching manager is gone, then remove their directories.

    Only processes recorded by this manager are candidates, and only when the PID,
    its start time and its command line still match: a user's own Mihomo, or a
    recycled PID, is never touched.
    """
    if not runtime_root.is_dir():
        return 0
    reaped = 0
    for directory in runtime_root.iterdir():
        owner_path = directory / "owner.json"
        if not directory.is_dir() or directory.is_symlink():
            continue
        try:
            owner = _Owner(**json.loads(owner_path.read_text()))
        except (OSError, ValueError, TypeError):
            owner = None
            try:
                # Another live manager may have created it a moment ago and not
                # recorded its owner yet.
                if time.time() - directory.stat().st_mtime < 60:
                    continue
            except OSError:
                continue
        if owner is not None and _process_matches(owner.manager_pid, owner.manager_started):
            continue  # another live manager (or this one) still owns it
        if owner is not None and _process_matches(owner.pid, owner.pid_started):
            try:
                process = psutil.Process(owner.pid)
                if owner.config in " ".join(process.cmdline()):
                    process.terminate()
                    try:
                        process.wait(3)
                    except psutil.TimeoutExpired:
                        process.kill()
                    reaped += 1
            except psutil.Error:
                pass
        shutil.rmtree(directory, ignore_errors=True)
    return reaped


def _process_matches(pid: int, started: float) -> bool:
    try:
        return abs(psutil.Process(pid).create_time() - started) < 1.0
    except psutil.Error:
        return False


@dataclass
class EngineStatus:
    available: bool
    path: str | None = None
    version: str | None = None
    code: str | None = None
    message: str | None = None
    candidates: list[str] = field(default_factory=list)
