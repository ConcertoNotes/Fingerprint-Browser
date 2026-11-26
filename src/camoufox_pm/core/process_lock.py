"""Local cross-process file lock for serializing profile operations."""

from __future__ import annotations

import asyncio
import os
import re
import stat
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from .fs_permissions import (
    PermissionProtectionError,
    create_private_directory,
    create_private_file,
    reject_reparse_paths,
    verify_private_path,
)
from .leases import ProfileLocked

_held_locks: ContextVar[frozenset[tuple[object, str]]] = ContextVar(
    "profile_lock", default=frozenset()
)


@asynccontextmanager
async def profile_lock(manager: Any, profile_id: str):
    """Acquire a non-blocking cross-process lock file for ``profile_id``.

    Re-entrant within the same asyncio task so nested operations (e.g., an edit
    invoking a sub-operation) do not deadlock on themselves. Raises
    ``ProfileLocked`` immediately if another process or task holds the lock.
    """
    if type(profile_id) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", profile_id):
        raise ValueError("Invalid profile identity")
    database = getattr(getattr(manager.storage, "db", None), "db_path", None)
    # Resolve the base root so OS-level directory aliases (e.g. macOS /tmp -> /private/tmp,
    # /var -> /private/var) are not mistaken for planted symlinks, while keeping
    # reject_reparse_paths strict on profile-locks/ and <profile_id>.lock below it.
    root = (
        Path(database).parent.resolve()
        if isinstance(database, (str, Path))
        else Path(manager.data_dir).resolve()
    )
    key = (asyncio.current_task(), str(root / profile_id))
    if key in _held_locks.get():
        yield
        return

    directory = root / "profile-locks"
    if not directory.exists():
        try:
            create_private_directory(directory)
        except FileExistsError:
            pass
    path = directory / f"{profile_id}.lock"
    reject_reparse_paths(path)
    try:
        if path.exists():
            raise FileExistsError
        descriptor = create_private_file(path)
    except (FileExistsError, PermissionProtectionError):
        verify_private_path(path)
        descriptor = os.open(
            path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        )

    with os.fdopen(descriptor, "r+b", buffering=0) as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ProfileLocked(profile_id)
        if os.name == "nt":
            import msvcrt

            if not info.st_size:
                handle.write(b"0")
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
            except OSError:
                raise ProfileLocked(profile_id) from None

            def unlock() -> None:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise ProfileLocked(profile_id) from None

            def unlock() -> None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

        token = _held_locks.set(_held_locks.get() | {key})
        try:
            yield
        finally:
            _held_locks.reset(token)
            unlock()
