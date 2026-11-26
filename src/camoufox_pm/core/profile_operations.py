"""Exclusive local profile operations share the same cross-instance leases as launches."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from .leases import ProfileLocked, make_lease_holder
from .process_lock import profile_lock


def profile_data_path(manager: Any, profile: Any) -> Path:
    """Refuse stored paths or symlink directories outside managed profile storage."""
    root = manager.profiles_dir.resolve()
    path = Path(profile.storage_path) if profile.storage_path else root / f"profile_{profile.id}"
    resolved = path.resolve()
    expected = root / f"profile_{profile.id}"
    if (
        not path.is_absolute()
        and path.name == expected.name
        and not resolved.is_relative_to(root)
        and expected.is_dir()
        and not expected.is_symlink()
    ):
        path = expected
        resolved = expected.resolve()
    if resolved == root or not resolved.is_relative_to(root) or path.is_symlink():
        raise ValueError("Profile directory must be inside the managed profiles directory")
    return resolved


async def run_file_operation(function, *args, **kwargs):
    """Drain worker I/O before cancellation releases its protective lease."""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if task.cancelled():
                    raise
        task.result()
        raise


@asynccontextmanager
async def _profile_operation(
    manager: Any,
    profile_id: str,
    include_deleted: bool = False,
    *,
    holder: str | None = None,
):
    """Reserve one closed profile across processes, with renewal during long I/O."""
    profile = await manager.storage.get_profile(profile_id, include_deleted=include_deleted)
    if profile is None:
        raise ValueError(f"Profile with ID {profile_id} not found")
    if manager.browser_sessions.is_live(profile_id):
        raise ProfileLocked(profile_id, manager.lease_holder)
    holder = holder or make_lease_holder()
    ttl = 300
    if not await manager.storage.acquire_lease(profile_id, holder, ttl, include_deleted):
        lease = await manager.storage.get_lease(profile_id)
        raise ProfileLocked(profile_id, lease[0] if lease else None)

    async def renew() -> None:
        while True:
            await asyncio.sleep(30)
            if not await manager.storage.renew_lease([profile_id], holder, ttl):
                raise ProfileLocked(profile_id)

    heartbeat = asyncio.create_task(renew())
    try:
        profile = await manager.storage.get_profile(profile_id, include_deleted=include_deleted)
        if profile is None:
            raise ValueError(f"Profile with ID {profile_id} not found")
        yield profile
        if heartbeat.done():
            heartbeat.result()
    finally:
        heartbeat.cancel()
        try:
            await asyncio.gather(heartbeat, return_exceptions=True)
        finally:
            await manager.storage.release_lease(profile_id, holder)


@asynccontextmanager
async def profile_operation(
    manager: Any,
    profile_id: str,
    include_deleted: bool = False,
    *,
    holder: str | None = None,
):
    async with profile_lock(manager, profile_id):
        async with _profile_operation(
            manager, profile_id, include_deleted, holder=holder
        ) as profile:
            yield profile
