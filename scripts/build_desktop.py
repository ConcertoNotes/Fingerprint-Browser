#!/usr/bin/env python3
"""Build the standalone Prism Browser desktop app (Electron + slim Python Core Sidecar).

Pipeline:
  1. ``scripts/build_webui.py`` -> static WebUI in ``src/camoufox_pm/webui/``
  2. ``scripts/prepare_bundled_runtime.py`` -> ``.cache/bundle-runtime/<platform>-<arch>/`` (~62 MB)
  3. PyInstaller ``packaging/prism-backend.spec`` -> ``build/prism-backend/`` (~25 MB)
  4. Write ``build/distribution/distribution.json`` (standard or customer edition metadata)
  5. ``electron-builder`` in ``electron/`` -> compact installer in ``dist/`` + ``.sha256``
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
DIST = ROOT / "dist"
ELECTRON_DIR = ROOT / "electron"
BACKEND_DIR = BUILD / "prism-backend"
DISTRIBUTION_DIR = BUILD / "distribution"

DEFAULT_BRAND_NAME = "棱镜浏览器"
INSTALLER_SUFFIXES = (".dmg", ".exe", ".deb", ".tar.gz", ".AppImage")
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})


def architecture() -> str:
    """Return Electron-compatible architecture identifier (``arm64`` or ``x64``)."""
    machine = platform.machine().lower()
    return {
        "amd64": "x64",
        "x64": "x64",
        "x86_64": "x64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }.get(machine, machine)


def bundled_runtime_source() -> Path:
    return ROOT / ".cache" / "bundle-runtime" / f"{sys.platform}-{architecture()}"


def _npm_executable() -> str:
    return shutil.which("npm.cmd" if sys.platform == "win32" else "npm") or "npm"


def _npx_executable() -> str:
    return shutil.which("npx.cmd" if sys.platform == "win32" else "npx") or "npx"


def _electron_builder_cmd() -> list[str]:
    bin_name = "electron-builder.cmd" if sys.platform == "win32" else "electron-builder"
    local_bin = ELECTRON_DIR / "node_modules" / ".bin" / bin_name
    if local_bin.is_file():
        return [str(local_bin)]
    return [_npx_executable(), "electron-builder"]


def validate_cloud_url(
    cloud_url: str | None,
    *,
    edition: str,
    allow_insecure_dev_url: bool = False,
) -> str | None:
    """Validate and normalize ``--cloud-url`` for desktop distribution packaging."""
    cleaned = (cloud_url or "").strip()
    if not cleaned:
        if edition == "customer":
            raise ValueError(
                "--cloud-url is required when --edition customer is selected "
                "(example: --cloud-url https://cloud.example.com)."
            )
        return None

    parsed = urlparse(cleaned)
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()

    if not scheme or not host:
        raise ValueError(f"Invalid --cloud-url {cleaned!r}: must include scheme and hostname.")
    if parsed.username or parsed.password:
        raise ValueError("--cloud-url must not embed credentials.")
    if parsed.query or parsed.fragment:
        raise ValueError("--cloud-url must not contain query parameters or URL fragments.")

    if allow_insecure_dev_url:
        if scheme not in {"http", "https"}:
            raise ValueError(f"Invalid --cloud-url scheme {scheme!r}: expected http or https.")
    else:
        if scheme != "https":
            raise ValueError(
                f"Refusing non-HTTPS --cloud-url {cleaned!r}. Use https:// "
                "or pass --allow-insecure-dev-url for local development only."
            )
        if host in LOCAL_HOSTS or host.endswith(".localhost") or host.endswith(".local"):
            raise ValueError(
                f"Refusing local/loopback --cloud-url host {host!r}. Use a public HTTPS domain "
                "or pass --allow-insecure-dev-url for local development only."
            )

    return cleaned.rstrip("/")


def write_distribution_config(
    *,
    edition: str,
    cloud_url: str | None,
    brand_name: str,
    require_activation: bool,
    output_dir: Path | None = None,
) -> Path:
    """Write ``build/distribution/distribution.json`` packaged into ``resources/distribution/``."""
    target_dir = output_dir or DISTRIBUTION_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    config_path = target_dir / "distribution.json"
    payload = {
        "edition": edition,
        "cloud_url": cloud_url,
        "brand_name": brand_name.strip() or DEFAULT_BRAND_NAME,
        "require_activation": bool(require_activation),
    }
    config_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"[distribution] Wrote {config_path} "
        f"(edition={payload['edition']}, cloud_url={payload['cloud_url'] or 'none'}, "
        f"brand_name={payload['brand_name']}, require_activation={payload['require_activation']})"
    )
    return config_path


def build_electron_builder_command(
    *,
    package: bool,
    edition: str = "standard",
    brand_name: str = DEFAULT_BRAND_NAME,
) -> list[str]:
    """Construct the ``electron-builder`` CLI command with optional customer/brand overrides."""
    cmd = [*_electron_builder_cmd(), "--config", "electron-builder.yml"]
    if not package:
        cmd.append("--dir")
    elif sys.platform == "darwin":
        cmd.append("--mac")
    elif sys.platform == "win32":
        cmd.append("--win")
    elif sys.platform.startswith("linux"):
        cmd.append("--linux")

    if edition == "customer":
        cmd.extend(
            [
                "-c.mac.artifactName=prism-browser-customer-macos-${arch}.${ext}",
                "-c.dmg.artifactName=prism-browser-customer-macos-${arch}.dmg",
                "-c.win.artifactName=prism-browser-customer-windows-${arch}-setup.${ext}",
                "-c.nsis.artifactName=prism-browser-customer-windows-${arch}-setup.exe",
                "-c.linux.artifactName=prism-browser-customer-linux-${arch}.${ext}",
            ]
        )

    clean_brand = (brand_name or "").strip()
    if clean_brand and clean_brand != DEFAULT_BRAND_NAME:
        cmd.extend(
            [
                f"-c.productName={clean_brand}",
                f"-c.dmg.title={clean_brand}",
                f"-c.nsis.shortcutName={clean_brand}",
            ]
        )

    return cmd


def build_sidecar() -> Path:
    """Freeze the slim Python Core Sidecar into ``build/prism-backend/``."""
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            str(ROOT / "packaging" / "prism-backend.spec"),
            "--distpath",
            str(BUILD),
            "--workpath",
            str(BUILD / "pyinstaller"),
            "--noconfirm",
            "--clean",
        ],
        cwd=ROOT,
        check=True,
    )
    exe_name = "prism-backend.exe" if sys.platform == "win32" else "prism-backend"
    binary = BACKEND_DIR / exe_name
    if not binary.is_file():
        raise SystemExit(f"Expected frozen sidecar executable at {binary}")
    print(f"[sidecar] Built slim sidecar at {binary}")
    return binary


def ensure_electron_dependencies() -> None:
    if not (ELECTRON_DIR / "package.json").is_file():
        raise SystemExit(f"Electron shell not found at {ELECTRON_DIR}")
    if not (ELECTRON_DIR / "node_modules").is_dir():
        print("[electron] Installing electron dependencies...")
        subprocess.run([_npm_executable(), "ci"], cwd=ELECTRON_DIR, check=True)


def run_electron_builder(
    *,
    package: bool,
    edition: str = "standard",
    brand_name: str = DEFAULT_BRAND_NAME,
) -> list[Path]:
    """Run ``electron-builder`` to produce either an unpacked dir or final OS installers."""
    ensure_electron_dependencies()

    runtime_dir = bundled_runtime_source()
    if not (runtime_dir / "manifest.json").is_file():
        raise SystemExit(
            f"Prepared offline runtime not found at {runtime_dir}. "
            "Run `python scripts/prepare_bundled_runtime.py` first."
        )
    if (runtime_dir / "browser-kernel.tar.zst").exists():
        raise SystemExit(
            f"Legacy browser-kernel.tar.zst found in {runtime_dir}; "
            "re-run `python scripts/prepare_bundled_runtime.py` to prune it."
        )

    cmd = build_electron_builder_command(
        package=package,
        edition=edition,
        brand_name=brand_name,
    )

    # Clean stale prior installers from previous architectures/builds.
    DIST.mkdir(parents=True, exist_ok=True)
    if package:
        for stale in list(DIST.iterdir()):
            if stale.name.endswith((".sha256", ".blockmap", ".yml")):
                stale.unlink(missing_ok=True)
        stale_dirs = {f"{DEFAULT_BRAND_NAME}.app", "camoufox-pm"}
        if brand_name.strip():
            stale_dirs.add(f"{brand_name.strip()}.app")
        for stale_dir in stale_dirs:
            target = DIST / stale_dir
            if target.is_dir():
                shutil.rmtree(target)

    env = os.environ.copy()
    if sys.platform == "win32" and not env.get("CSC_LINK") and not env.get("WINDOWS_CERT_PFX"):
        env.setdefault("CSC_IDENTITY_AUTO_DISCOVERY", "false")

    print(f"[electron] Running: {' '.join(cmd)}")
    subprocess.run(cmd, cwd=ELECTRON_DIR, env=env, check=True)

    return _collect_installers()


def _collect_installers() -> list[Path]:
    if not DIST.is_dir():
        return []
    return sorted(
        p
        for p in DIST.iterdir()
        if p.is_file()
        and any(p.name.endswith(suffix) for suffix in INSTALLER_SUFFIXES)
        and not p.name.endswith(".blockmap")
    )


def write_checksums(files: list[Path]) -> None:
    for path in files:
        checksum = path.with_name(path.name + ".sha256")
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        checksum.write_text(f"{digest.hexdigest()}  {path.name}\n", encoding="utf-8")
        print(f"[sha256] {checksum.name}: {digest.hexdigest()}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--package",
        action="store_true",
        help="Produce final OS installer(s) (.dmg / -setup.exe / .deb / .tar.gz) + SHA-256",
    )
    parser.add_argument(
        "--package-only",
        action="store_true",
        help="Run electron-builder packaging against an existing build/prism-backend/ sidecar",
    )
    parser.add_argument(
        "--sidecar-only",
        action="store_true",
        help="Build only the slim Python Core Sidecar in build/prism-backend/",
    )
    parser.add_argument(
        "--checksums-only",
        action="store_true",
        help="Refresh .sha256 files for installers currently in dist/",
    )
    parser.add_argument(
        "--skip-webui",
        action="store_true",
        help="Reuse existing src/camoufox_pm/webui without rebuilding Next.js",
    )
    parser.add_argument(
        "--skip-runtime",
        action="store_true",
        help="Reuse existing .cache/bundle-runtime/<platform>-<arch>/ without running prepare script",
    )
    parser.add_argument(
        "--edition",
        choices=["standard", "customer"],
        default="standard",
        help="Distribution edition to package (default: standard; customer locks cloud URL and simplifies UI)",
    )
    parser.add_argument(
        "--cloud-url",
        default=None,
        help="Pre-configured HTTPS Prism Cloud Server URL (required for --edition customer)",
    )
    parser.add_argument(
        "--allow-insecure-dev-url",
        action="store_true",
        help="Allow http:// or localhost URLs in --cloud-url (local development/testing only)",
    )
    parser.add_argument(
        "--brand-name",
        default=DEFAULT_BRAND_NAME,
        help=f"Desktop application brand / window title (default: {DEFAULT_BRAND_NAME})",
    )
    parser.add_argument(
        "--require-activation",
        action="store_true",
        help="Require license activation before dismissing first-run activation modal in customer edition",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.checksums_only:
        write_checksums(_collect_installers())
        return 0

    try:
        normalized_cloud_url = validate_cloud_url(
            args.cloud_url,
            edition=args.edition,
            allow_insecure_dev_url=args.allow_insecure_dev_url,
        )
    except ValueError as exc:
        parser.error(str(exc))

    brand_name = (args.brand_name or "").strip() or DEFAULT_BRAND_NAME

    if not args.package_only:
        if not args.skip_webui:
            subprocess.run([sys.executable, str(ROOT / "scripts" / "build_webui.py")], check=True)
        if not args.skip_runtime:
            subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "prepare_bundled_runtime.py")],
                check=True,
            )
        build_sidecar()
        if args.sidecar_only:
            print(f"Sidecar build complete - see {BACKEND_DIR}")
            return 0

    write_distribution_config(
        edition=args.edition,
        cloud_url=normalized_cloud_url,
        brand_name=brand_name,
        require_activation=args.require_activation,
    )

    files = run_electron_builder(
        package=args.package or args.package_only,
        edition=args.edition,
        brand_name=brand_name,
    )
    if args.package or args.package_only:
        write_checksums(files)
    print("Desktop build complete - see dist/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
