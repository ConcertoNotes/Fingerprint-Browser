"""Unit tests for Electron-managed browser session tracking, heartbeats, and teardown."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from camoufox_pm.core.browser_session import BrowserSession, BrowserSessionManager


class FakeNetwork:
    def __init__(self) -> None:
        self.running = False
        self.closed = 0

    def mark_running(self) -> None:
        self.running = True

    async def close(self) -> None:
        self.closed += 1


@pytest.mark.asyncio
async def test_register_list_and_close_session_releases_network_and_lease():
    storage = SimpleNamespace(
        renew_lease=AsyncMock(return_value=1),
        release_lease=AsyncMock(return_value=True),
    )
    manager = BrowserSessionManager(storage=storage, holder="holder-1")
    net = FakeNetwork()

    session = manager.register_session("p1", network=net, session_payload={"status": "ready"})
    assert isinstance(session, BrowserSession)
    assert net.running is True
    assert manager.is_running("p1")
    assert manager.is_live("p1")
    assert len(manager.list_active()) == 1

    # Idempotent registration returns the same session.
    assert manager.register_session("p1") is session

    closed = await manager.close_and_release("p1")
    assert closed is True
    assert net.closed == 1
    storage.release_lease.assert_awaited_once_with("p1", "holder-1")
    assert not manager.is_running("p1")
    assert await manager.close_and_release("p1") is False


@pytest.mark.asyncio
async def test_close_all_drains_all_sessions():
    manager = BrowserSessionManager()
    nets = [FakeNetwork(), FakeNetwork()]
    manager.register_session("p1", network=nets[0])
    manager.register_session("p2", network=nets[1])

    count = await manager.close_all()
    assert count == 2
    assert [n.closed for n in nets] == [1, 1]
    assert manager.list_active() == []


@pytest.mark.asyncio
async def test_lost_lease_during_heartbeat_releases_session_without_stealing_lease():
    storage = SimpleNamespace(
        renew_lease=AsyncMock(return_value=0),
        release_lease=AsyncMock(return_value=True),
    )
    manager = BrowserSessionManager(storage=storage, holder="holder-1")
    net = FakeNetwork()
    manager.register_session("p1", network=net)

    await manager._renew_leases()

    assert net.closed == 1
    assert not manager.is_running("p1")
    # Must not release a lease that now belongs to another holder.
    storage.release_lease.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_close_drains_single_teardown_even_under_cancellation():
    started = asyncio.Event()
    proceed = asyncio.Event()
    calls = 0

    class SlowNetwork(FakeNetwork):
        async def close(self) -> None:
            nonlocal calls
            calls += 1
            started.set()
            await proceed.wait()
            await super().close()

    manager = BrowserSessionManager()
    net = SlowNetwork()
    manager.register_session("p1", network=net)

    task = asyncio.create_task(manager.close_and_release("p1"))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert manager.is_running("p1")  # Still tracked until teardown completes.
    proceed.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == 1
    assert net.closed == 1
    assert not manager.is_running("p1")
