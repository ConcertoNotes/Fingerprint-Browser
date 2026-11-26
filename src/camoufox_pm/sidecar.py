"""Headless Prism Browser Python Core Sidecar entrypoint for Electron.

Spawned by the Electron desktop shell (or via ``python -m camoufox_pm.sidecar``
in dev mode) to serve the local REST API, static WebUI, per-profile proxy
routing runtimes, offline GeoIP resolution, and isolation engine.

Supports ``--host``, ``--port``, ``--version``, and ``--self-check``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import uvicorn

from camoufox_pm import __version__
from camoufox_pm.config import bootstrap_desktop, get_settings
from camoufox_pm.core import bundled_runtime, geoip
from camoufox_pm.core.isolation.chrome_presets import list_chrome_presets


def run_self_check() -> dict[str, Any]:
    """Verify frozen/dev sidecar imports, WebUI assets, offline runtime, and preset pool."""
    from camoufox_pm.main import _webui_dir, app

    bundled = bundled_runtime.bundled_status()
    return {
        "version": __version__,
        "engine": "prism-sidecar",
        "routes": len(app.routes),
        "webui": _webui_dir() is not None,
        "bundled_mihomo": bundled.get("bundled_mihomo", False),
        "bundled_geoip": bundled.get("bundled_geoip", False),
        "geoip_ready": geoip.geoip_ready(),
        "chrome_presets": len(list_chrome_presets()),
    }


def _bootstrap_runtime_for_sidecar() -> None:
    """Ensure writable data dir, encryption key, and unpacked offline runtime assets."""
    if not os.environ.get("CPM_DB_PATH") or not os.environ.get("CPM_SECRET_KEY"):
        bootstrap_desktop()
    get_settings.cache_clear()
    data_dir = Path(get_settings().db_path).expanduser().resolve().parent
    data_dir.mkdir(parents=True, exist_ok=True)
    bundled_runtime.bootstrap_bundled_runtime(data_dir)


def run_sidecar(host: str, port: int) -> None:
    """Bootstrap offline runtime and serve the FastAPI sidecar on ``host:port``."""
    os.environ["CPM_HOST"] = host
    os.environ["CPM_PORT"] = str(port)
    _bootstrap_runtime_for_sidecar()

    # Import the app object directly so PyInstaller frozen binaries never rely on string resolution.
    from camoufox_pm.main import app

    uvicorn.run(app, host=host, port=port, log_level="info")


def main(argv: list[str] | None = None) -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(
        prog="prism-backend",
        description="Prism Browser Core Sidecar (REST API, WebUI, proxy runtimes & isolation engine).",
    )
    parser.add_argument("--host", default=settings.host, help="Bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=settings.port, help="Bind port (default: 8000)")
    parser.add_argument("--version", action="store_true", help="Print sidecar version and exit")
    parser.add_argument(
        "--self-check",
        action="store_true",
        help="Run packaging & runtime readiness self-check and print JSON",
    )
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    if args.version:
        print(f"Prism Browser Sidecar {__version__}")
        return

    if args.self_check:
        print(json.dumps(run_self_check(), ensure_ascii=False))
        return

    run_sidecar(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
