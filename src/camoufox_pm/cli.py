"""Command-line launcher: run the API + bundled web UI as one process."""

import argparse
import asyncio
import os
import sys
import threading
import webbrowser
from typing import NoReturn

import uvicorn

from camoufox_pm.config import get_settings
from camoufox_pm.core.database import StorageManager
from camoufox_pm.core.leases import lease_expired


def main() -> None:
    if "--desktop" in sys.argv[1:]:
        from camoufox_pm.config import bootstrap_desktop

        bootstrap_desktop()
    settings = get_settings()
    parser = argparse.ArgumentParser(
        prog="camoufox-pm",
        description="Run Prism Browser (API + web UI) on one port.",
    )
    parser.add_argument("--host", default=settings.host, help="Bind address")
    parser.add_argument("--port", type=int, default=settings.port, help="Port")
    parser.add_argument("--no-browser", action="store_true", help="Do not open a browser")
    parser.add_argument(
        "--desktop",
        action="store_true",
        help="Bootstrap desktop defaults and run the headless Prism Browser sidecar",
    )

    subcommands = parser.add_subparsers(dest="command")

    unlock = subcommands.add_parser(
        "unlock",
        help="Force-release the lease on a profile",
        description=(
            "Clear the lease holding a profile, whatever it says. Needed only when a "
            "machine died in a way its lease cannot notice, or when you have checked "
            "that the holder is really gone: a live lease means another instance may "
            "be driving this profile right now."
        ),
    )
    unlock.add_argument("profile_id")
    unlock.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")

    subcommands.add_parser(
        "leases",
        help="List the profiles currently leased, and by whom",
    )

    args = parser.parse_args()

    if args.command == "unlock":
        asyncio.run(_run_unlock_command(args))
        return

    if args.command == "leases":
        asyncio.run(_run_leases_command())
        return

    os.environ["CPM_HOST"] = args.host
    os.environ["CPM_PORT"] = str(args.port)
    get_settings.cache_clear()

    if args.desktop:
        from camoufox_pm.sidecar import run_sidecar

        run_sidecar(host=args.host, port=args.port)
        return

    if not args.no_browser:
        url = f"http://{'localhost' if args.host in ('0.0.0.0', '127.0.0.1') else args.host}:{args.port}/"
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    # Import the app object (not an import string) so this works inside a frozen
    # PyInstaller bundle where uvicorn cannot resolve the module by name.
    from camoufox_pm.main import app

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


def _fail(message: str) -> NoReturn:
    print(message, file=sys.stderr)
    raise SystemExit(1)


async def _run_unlock_command(args: argparse.Namespace) -> None:
    """Force-release one profile's lease; shell-only."""
    storage = StorageManager(get_settings().db_path)
    await storage.initialize()
    try:
        lease = await storage.get_lease(args.profile_id)
        if lease is None:
            _fail(f"No profile with ID '{args.profile_id}'.")
        holder, expires = lease
        if holder is None:
            print(f"Profile {args.profile_id} is not leased.")
            return
        state = "expired" if lease_expired(expires) else "live"
        print(
            f"Profile {args.profile_id} is leased by {holder}"
            + (f" until {expires} UTC ({state})" if expires else " (no expiry)")
        )
        if not args.yes and input("Force-release this lease? (yes/no): ").strip().lower() not in (
            "yes",
            "y",
        ):
            print("Cancelled; the lease stands.")
            return
        previous = await storage.force_release_lease(args.profile_id)
        print(f"Lease released (was held by {previous}).")
    finally:
        await storage.close()


async def _run_leases_command() -> None:
    """Show who holds what, so ``unlock`` is a decision and not a guess."""
    storage = StorageManager(get_settings().db_path)
    await storage.initialize()
    try:
        holders = await storage.get_lease_holders()
        if not holders:
            print("No profiles are leased.")
            return
        for entry in holders:
            state = "expired" if entry["expired"] else "live"
            print(
                f"{entry['id']}  {entry['name']}  {entry['locked_by']}  "
                f"expires {entry['lock_expires']} UTC ({state})"
            )
    finally:
        await storage.close()


if __name__ == "__main__":
    main()
