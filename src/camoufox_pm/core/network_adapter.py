"""Per-profile network runtimes: the bridge from a binding to a launch.

``prepare`` runs inside the launch's lease, after the profile is known to be
closed, and before any browser exists. A profile with a binding gets a private
Mihomo process whose single listener leads to the bound node; the exit is then
measured *through that listener* before the browser may start. A profile
without a binding returns ``None`` and launches exactly as it always has, with
its own manual proxy (or none).

The runtime is owned by the browser session from the moment the session exists
and is closed after the browser in its teardown. If the engine dies while the
browser runs, the runtime turns ``degraded``: the browser's proxy now refuses,
which is the intended failure — nothing re-routes it to another node or direct.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from loguru import logger

from .mihomo_manager import MihomoProcess, MihomoUnavailable, discover_binary, reap_orphans
from .models import ProxyConfig, ProxyType
from .network_catalog import NetworkCatalog, NetworkNotFound, NetworkSecretsUnavailable
from .network_settings import NetworkSettings


class NetworkLaunchError(RuntimeError):
    """A bound profile cannot get its network. Launch must not continue."""

    def __init__(self, status: int, code: str, message: str):
        self.status = status
        self.code = code
        super().__init__(message)


class ProfileNetwork:
    """A running profile's private engine and listener."""

    def __init__(
        self, registry: NetworkAdapter, profile_id: str, node: dict[str, Any], engine: MihomoProcess
    ):
        self.registry = registry
        self.profile_id = profile_id
        self.node = node
        self.engine = engine
        self.listener = engine.listeners[0]
        self.state = "ready"
        self.exit_ip: str | None = None
        self.error: dict[str, str] | None = None
        self.started_at = time.time()
        self.adapter_kind = "mihomo"
        self._closed = False
        engine.on_exit(self._engine_exited)

    @property
    def proxy_config(self) -> ProxyConfig:
        return ProxyConfig(
            type=ProxyType.HTTP,
            server=f"127.0.0.1:{self.listener.port}",
            username=self.listener.username,
            password=self.listener.password,
        )

    @property
    def browser_proxy(self) -> dict[str, str]:
        return self.listener.browser_proxy

    async def _engine_exited(self, _engine: MihomoProcess) -> None:
        if self._closed:
            return
        self.state = "degraded"
        code, message = _engine.last_error() or (
            "engine_exited",
            "The network engine stopped. Connections fail until it is reconnected.",
        )
        self.error = {"code": code, "message": message}

    def mark_running(self) -> None:
        if self.state == "ready":
            self.state = "running"

    async def reconnect(self) -> None:
        """Restart the same node on the same port with the same credentials."""
        if self._closed:
            raise NetworkLaunchError(409, "not_running", "The profile is not running.")
        try:
            await self.engine.restart()
        except MihomoUnavailable as exc:
            self.state = "degraded"
            self.error = {"code": exc.code, "message": str(exc)}
            raise NetworkLaunchError(502, exc.code, str(exc)) from None
        self.state = "running"
        self.error = None

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.state = "stopping"
        try:
            await self.engine.close()
        finally:
            self.state = "stopped"
            self.registry._forget(self)

    def info(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "state": self.state,
            "adapter_kind": self.adapter_kind,
            "node_id": self.node["id"],
            "node_name": self.node["name"],
            "listen_port": self.listener.port,
            "exit_ip": self.exit_ip,
            "engine_alive": self.engine.alive,
            "started_at": self.started_at,
            "error": self.error,
        }


class NetworkAdapter:
    """Decides, per launch, between a bound node and the profile's own proxy."""

    def __init__(self, manager: Any):
        self.manager = manager
        self.runtimes: dict[str, ProfileNetwork] = {}

    @property
    def runtime_root(self) -> Path:
        return Path(self.manager.data_dir).absolute() / "network-runtime"

    def catalog(self) -> NetworkCatalog:
        return NetworkCatalog(self.manager)

    def settings(self) -> NetworkSettings:
        return NetworkSettings(self.manager.storage.db._connection)

    def binary(self):
        return discover_binary(self.settings().values()["mihomo_path"], Path(self.manager.data_dir))

    def reap(self) -> int:
        try:
            reaped = reap_orphans(self.runtime_root)
        except OSError as exc:
            logger.warning(f"Could not inspect leftover network runtimes: {exc}")
            return 0
        if reaped:
            logger.info(f"Stopped {reaped} network engine(s) left by a manager that exited")
        return reaped

    def binding_node_id(self, profile_id: str) -> str | None:
        row = self.manager.storage.db._connection.execute(
            "SELECT node_id FROM profile_network_bindings WHERE profile_id = ?", (profile_id,)
        ).fetchone()
        return row[0] if row else None

    async def prepare(self, profile_id: str) -> ProfileNetwork | None:
        node_id = self.binding_node_id(profile_id)
        if node_id is None:
            return None
        catalog = self.catalog()
        try:
            node, outbound = catalog.node_outbound(node_id)
        except NetworkNotFound:
            raise NetworkLaunchError(
                422, "node_missing", "The bound node no longer exists. Bind another node."
            ) from None
        except NetworkSecretsUnavailable as exc:
            raise NetworkLaunchError(409, "secrets_unavailable", str(exc)) from None
        if node["status"] != "active":
            raise NetworkLaunchError(
                422,
                "node_retired" if node["status"] == "retired" else "node_invalid",
                "The bound node is no longer offered by its subscription. Bind another node "
                "before launching; the profile will not fall back to another exit.",
            )
        previous = self.runtimes.get(profile_id)
        if previous is not None:
            if previous.engine.alive:
                raise NetworkLaunchError(
                    409, "already_prepared", "This profile's network is already running."
                )
            await previous.close()
        try:
            binary = self.binary()
            engine = await MihomoProcess(
                binary,
                {"node-0": outbound},
                ["node-0"],
                self.runtime_root,
                purpose=f"profile-{profile_id}",
            ).start()
        except MihomoUnavailable as exc:
            raise NetworkLaunchError(503, exc.code, str(exc)) from None
        runtime = ProfileNetwork(self, profile_id, node, engine)
        self.runtimes[profile_id] = runtime
        return runtime

    async def verify_exit(self, runtime: ProfileNetwork, timeout: float) -> str:
        """Measure the exit through the profile's own listener; failure blocks launch."""
        from . import proxy_check

        try:
            ip, _ = await proxy_check.resolve_exit_ip(runtime.proxy_config, timeout=timeout)
        except ConnectionError:
            code, message = runtime.engine.last_error() or (
                "exit_check_failed",
                "The bound node did not reach the internet.",
            )
            raise NetworkLaunchError(502, code, f"{message} The browser was not started.") from None
        runtime.exit_ip = ip
        return ip

    def _forget(self, runtime: ProfileNetwork) -> None:
        if self.runtimes.get(runtime.profile_id) is runtime:
            self.runtimes.pop(runtime.profile_id, None)

    def info(self, profile_id: str) -> dict[str, Any]:
        runtime = self.runtimes.get(profile_id)
        if runtime is not None:
            return runtime.info()
        node_id = self.binding_node_id(profile_id)
        running = self.manager.browser_sessions.is_running(profile_id)
        return {
            "profile_id": profile_id,
            "state": "running" if running else "stopped",
            "adapter_kind": "native" if node_id is None else "mihomo",
            "node_id": node_id,
            "exit_ip": None,
            "error": None,
        }

    def list_info(self) -> list[dict[str, Any]]:
        return [runtime.info() for runtime in self.runtimes.values()]

    async def shutdown(self) -> None:
        for runtime in list(self.runtimes.values()):
            try:
                await runtime.close()
            except Exception as exc:  # noqa: BLE001 - shutdown must continue
                logger.warning(f"Failed to stop network for {runtime.profile_id}: {exc}")
