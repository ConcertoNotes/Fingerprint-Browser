"""Managed browser session lifecycle: track active Electron sessions, proxy runtimes, and leases.

In the embedded Chromium (Electron) architecture, the Electron main process
hosts each profile's ``WebContentsView`` while this Python sidecar owns the
per-profile SQLite lease, lease renewal heartbeat, and dedicated Mihomo proxy
runtime. Sessions are registered via ``register_session`` (from
``POST /api/v1/profiles/{id}/session/prepare``) and torn down via
``close_and_release`` (from ``POST /api/v1/profiles/{id}/session/release`` or
sidecar shutdown).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from loguru import logger

from ..config import get_settings

if TYPE_CHECKING:
    from .database import StorageManager

ExitHandler = Callable[[str], Awaitable[None]]


class BrowserLaunchError(RuntimeError):
    """Raised when a browser session fails to initialize."""


async def _drain_cleanup(task: asyncio.Task[Any]) -> Any:
    """Let owned teardown finish even when its caller is cancelled repeatedly."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


class BrowserSession:
    """A single active isolated browser session tracked by the sidecar."""

    def __init__(
        self,
        profile_id: str,
        *,
        process_id: int | None = None,
        network: Any = None,
        session_payload: dict[str, Any] | None = None,
    ):
        self.profile_id = profile_id
        self.process_id = process_id
        self.started_at = datetime.now()
        self.on_exit: ExitHandler | None = None
        self.on_stopped: ExitHandler | None = None
        self._terminated = False
        self.network = network
        self.session_payload = session_payload
        self._termination_task: asyncio.Task[None] | None = None

    async def terminate(self) -> None:
        """Drain one strongly owned teardown, including after caller cancellation."""
        if self._termination_task is None:
            self._termination_task = asyncio.create_task(self._terminate())
        await _drain_cleanup(self._termination_task)

    async def _terminate(self) -> None:
        logger.info(f"Releasing browser session for profile {self.profile_id}")
        if self.network is not None:
            try:
                await self.network.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"Error stopping the network for {self.profile_id}: {exc}")
        self._terminated = True

    def info(self) -> dict[str, Any]:
        """Return a serializable summary of this session."""
        return {
            "profile_id": self.profile_id,
            "process_id": self.process_id,
            "started_at": self.started_at.isoformat(),
        }


class BrowserSessionManager:
    """Track active profile sessions, maintain SQLite lease heartbeats, and drain teardowns."""

    def __init__(self, storage: StorageManager | None = None, holder: str | None = None) -> None:
        self.active_sessions: dict[str, BrowserSession] = {}
        self._starting: dict[str, int] = {}
        self._closing: dict[str, asyncio.Task[bool]] = {}
        self._release_after_close: set[str] = set()
        self._storage = storage
        self._holder = holder
        self.on_stopped: ExitHandler | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None

    def start_heartbeat(self, interval: float = 30.0) -> None:
        """Begin renewing this sidecar's profile leases in the background."""
        if self._storage is None or self._holder is None:
            return
        if self._heartbeat_task is not None and not self._heartbeat_task.done():
            return
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(interval))

    async def stop_heartbeat(self) -> None:
        """Stop the renewal loop, and wait for the beat in flight to finish."""
        if self._heartbeat_task is None:
            return
        self._heartbeat_task.cancel()
        try:
            await self._heartbeat_task
        except asyncio.CancelledError:
            pass
        self._heartbeat_task = None

    async def _heartbeat_loop(self, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                await self._renew_leases()
            except Exception as exc:  # noqa: BLE001 - one bad beat must not stop the loop
                logger.warning(f"Lease heartbeat failed: {exc}")

    async def _renew_leases(self) -> None:
        """Renew every lease we hold, and release any session whose lease was lost."""
        if self._storage is None or self._holder is None or not self.active_sessions:
            return
        profile_ids = list(self.active_sessions)
        ttl_seconds = get_settings().lease_ttl
        renewed = await self._storage.renew_lease(profile_ids, self._holder, ttl_seconds)
        if renewed == len(profile_ids):
            return
        for profile_id in profile_ids:
            if await self._storage.renew_lease([profile_id], self._holder, ttl_seconds) > 0:
                continue
            logger.warning(
                f"Lost the lease on profile {profile_id} — another instance may be "
                "driving this identity; releasing session runtime."
            )
            await self.close(profile_id)

    def is_running(self, profile_id: str) -> bool:
        """Return whether a session is currently active for the profile."""
        return profile_id in self.active_sessions

    def is_live(self, profile_id: str) -> bool:
        """Whether a session is active or currently preparing."""
        return profile_id in self.active_sessions or profile_id in self._starting

    def list_active(self) -> list[dict[str, Any]]:
        """Return summaries of all active sessions."""
        return [session.info() for session in self.active_sessions.values()]

    def register_session(
        self,
        profile_id: str,
        *,
        network: Any = None,
        session_payload: dict[str, Any] | None = None,
        on_exit: ExitHandler | None = None,
        process_id: int | None = None,
    ) -> BrowserSession:
        """Register an active Electron-managed session and start network tracking."""
        existing = self.active_sessions.get(profile_id)
        if existing is not None:
            return existing
        session = BrowserSession(
            profile_id,
            process_id=process_id,
            network=network,
            session_payload=session_payload,
        )
        session.on_exit = on_exit
        session.on_stopped = self.on_stopped
        self.active_sessions[profile_id] = session
        if network is not None:
            network.mark_running()
        logger.info(f"Registered active session for profile {profile_id}")
        return session

    async def launch(
        self,
        profile_id: str,
        launch_options: dict[str, Any],
        on_exit: ExitHandler | None = None,
        network: Any = None,
    ) -> BrowserSession:
        """Register a managed session (compatibility wrapper for callers expecting ``launch``)."""
        return self.register_session(
            profile_id,
            network=network,
            session_payload=launch_options,
            on_exit=on_exit,
        )

    async def _close_session(
        self, profile_id: str, *, release_lease: bool, notify: bool = False
    ) -> bool:
        task = self._closing.get(profile_id)
        if release_lease:
            self._release_after_close.add(profile_id)
        if task is None:
            session = self.active_sessions.get(profile_id)
            if session is None:
                self._release_after_close.discard(profile_id)
                return False

            async def finish() -> bool:
                try:
                    await session.terminate()
                    stopped = getattr(session, "on_stopped", self.on_stopped)
                    if stopped is not None:
                        await stopped(profile_id)
                    if profile_id in self._release_after_close:
                        await self._release_lease(profile_id)
                    if self.active_sessions.get(profile_id) is session:
                        self.active_sessions.pop(profile_id)
                    if notify and session.on_exit is not None:
                        try:
                            await session.on_exit(profile_id)
                        except Exception as exc:  # noqa: BLE001
                            logger.warning(f"on_exit handler failed for {profile_id}: {exc}")
                    return True
                finally:
                    self._closing.pop(profile_id, None)
                    self._release_after_close.discard(profile_id)

            task = asyncio.create_task(finish())
            self._closing[profile_id] = task
        return await _drain_cleanup(task)

    async def close(self, profile_id: str) -> bool:
        """Close one session, keeping it tracked until teardown completes."""
        return await self._close_session(profile_id, release_lease=False)

    async def close_and_release(self, profile_id: str) -> bool:
        """Drain session teardown, then release its lease in the same owned task."""
        return await self._close_session(profile_id, release_lease=True)

    async def _release_lease(self, profile_id: str) -> None:
        """Best-effort lease release. Teardown must not fail on bookkeeping."""
        if self._storage is None or self._holder is None:
            return
        try:
            await self._storage.release_lease(profile_id, self._holder)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Could not release the lease on {profile_id}: {exc}")

    async def close_all(self) -> int:
        """Close every active session and return how many were closed."""
        count = 0
        for profile_id in list(self.active_sessions.keys()):
            if await self.close_and_release(profile_id):
                count += 1
        return count
