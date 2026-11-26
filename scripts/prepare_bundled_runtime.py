#!/usr/bin/env python3
"""Prepare compact lossless-compressed offline runtime assets for Electron packaging.

Outputs ``.cache/bundle-runtime/<platform>-<arch>/`` (~62 MB total) containing:
  - ``bin/mihomo(.exe).zst``  (zstd level 19, ~15 MB)
  - ``geoip/geoip.mmdb.zst``  (zstd level 19, ~47 MB)
  - ``manifest.json``         (exact SHA-256 checksums, versions & sizes)

The directory naming uses Node/Electron conventions (``darwin-arm64``,
``darwin-x64``, ``win32-x64``, ``linux-x64``) to match
``electron/electron-builder.yml`` ``${platform}-${arch}`` directly.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import platform
import re
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import zstandard

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from camoufox_pm.core import geoip  # noqa: E402
from camoufox_pm.core.mihomo_manager import discover_binary, validate_binary  # noqa: E402

CHUNK_SIZE = 1024 * 1024
MIHOMO_RELEASES_API = "https://api.github.com/repos/MetaCubeX/mihomo/releases/latest"
DEFAULT_GEOIP_URLS = (
    "https://github.com/P3TERX/GeoLite.mmdb/releases/latest/download/GeoLite2-City.mmdb",
    "https://raw.githubusercontent.com/P3TERX/GeoLite.mmdb/download/GeoLite2-City.mmdb",
)


def normalized_arch() -> str:
    """Return Electron-compatible architecture identifier (``arm64`` or ``x64``)."""
    machine = platform.machine().lower()
    return {
        "amd64": "x64",
        "x64": "x64",
        "x86_64": "x64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }.get(machine, machine)


def runtime_platform_key() -> str:
    return f"{sys.platform}-{normalized_arch()}"


def bundle_runtime_output_dir() -> Path:
    return ROOT / ".cache" / "bundle-runtime" / runtime_platform_key()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def format_mb(num_bytes: int) -> str:
    return f"{num_bytes / (1024 * 1024):.1f} MB"


def compress_file_zst(src: Path, dest: Path, *, level: int = 19) -> tuple[str, str, int, int]:
    """Compress ``src`` to ``dest`` with zstd; return (raw_sha, zst_sha, raw_size, zst_size)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    raw_sha = sha256_file(src)
    raw_size = src.stat().st_size

    fd, tmp_name = tempfile.mkstemp(prefix=f".{dest.name}.", suffix=".tmp", dir=str(dest.parent))
    tmp_path = Path(tmp_name)
    fd_owned = True
    try:
        params = zstandard.ZstdCompressionParameters.from_level(level, threads=-1)
        cctx = zstandard.ZstdCompressor(compression_params=params)
        fd_owned = False
        with os.fdopen(fd, "wb") as out_fh, src.open("rb") as in_fh:
            with cctx.stream_writer(out_fh, size=raw_size, closefd=False) as writer:
                while chunk := in_fh.read(CHUNK_SIZE):
                    writer.write(chunk)
            out_fh.flush()
            os.fsync(out_fh.fileno())
        os.replace(tmp_path, dest)
    except BaseException:
        if fd_owned:
            os.close(fd)
        tmp_path.unlink(missing_ok=True)
        raise

    zst_sha = sha256_file(dest)
    zst_size = dest.stat().st_size
    return raw_sha, zst_sha, raw_size, zst_size


def _mihomo_asset_patterns() -> list[re.Pattern[str]]:
    arch = normalized_arch()
    go_arch = "arm64" if arch == "arm64" else "amd64"
    if sys.platform == "darwin":
        return [
            re.compile(rf"^mihomo-darwin-{go_arch}-v\d+\.\d+\.\d+\.gz$"),
            re.compile(rf"^mihomo-darwin-{go_arch}-compatible-v\d+\.\d+\.\d+\.gz$"),
        ]
    if sys.platform == "win32":
        return [
            re.compile(rf"^mihomo-windows-{go_arch}-compatible-v\d+\.\d+\.\d+\.zip$"),
            re.compile(rf"^mihomo-windows-{go_arch}-v\d+\.\d+\.\d+\.zip$"),
        ]
    return [
        re.compile(rf"^mihomo-linux-{go_arch}-compatible-v\d+\.\d+\.\d+\.gz$"),
        re.compile(rf"^mihomo-linux-{go_arch}-v\d+\.\d+\.\d+\.gz$"),
    ]


def _download_mihomo_to_cache() -> Path:
    """Download the official verified Mihomo binary for the current OS/arch into .cache/mihomo/."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "prism-browser-builder"}
    gh_token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if gh_token:
        headers["Authorization"] = f"Bearer {gh_token}"

    with httpx.Client(follow_redirects=True, timeout=120, headers=headers) as client:
        release_resp = client.get(MIHOMO_RELEASES_API)
        release_resp.raise_for_status()
        release = release_resp.json()
        assets = release.get("assets") or []

        chosen: dict[str, Any] | None = None
        for pattern in _mihomo_asset_patterns():
            for asset in assets:
                if pattern.fullmatch(str(asset.get("name", ""))):
                    chosen = asset
                    break
            if chosen is not None:
                break
        if chosen is None:
            raise RuntimeError(
                f"Could not find an official proxy engine release asset for {runtime_platform_key()}"
            )

        asset_name = str(chosen["name"])
        asset_url = str(chosen["browser_download_url"])
        expected_digest: str | None = None
        raw_digest = chosen.get("digest")
        if isinstance(raw_digest, str) and raw_digest.startswith("sha256:"):
            expected_digest = raw_digest.split(":", 1)[1].strip()

        if not expected_digest:
            for asset in assets:
                name = str(asset.get("name", "")).lower()
                if "sha256" in name or "checksum" in name:
                    txt = client.get(str(asset["browser_download_url"])).text
                    for line in txt.splitlines():
                        parts = line.strip().split()
                        if len(parts) >= 2 and parts[-1].lstrip("*") == asset_name:
                            expected_digest = parts[0]
                            break
                    if expected_digest:
                        break

        cache_dir = ROOT / ".cache" / "mihomo"
        cache_dir.mkdir(parents=True, exist_ok=True)
        exe_name = "mihomo.exe" if sys.platform == "win32" else "mihomo"
        dest = cache_dir / exe_name

        with tempfile.TemporaryDirectory(dir=str(cache_dir)) as tmp_dir:
            archive = Path(tmp_dir) / asset_name
            print(f"[mihomo] Downloading {asset_name}...")
            digest = hashlib.sha256()
            with client.stream("GET", asset_url) as stream:
                stream.raise_for_status()
                with archive.open("wb") as out_fh:
                    for chunk in stream.iter_bytes(CHUNK_SIZE):
                        out_fh.write(chunk)
                        digest.update(chunk)
            if expected_digest and digest.hexdigest().lower() != expected_digest.lower():
                raise RuntimeError(
                    f"Downloaded {asset_name} SHA-256 mismatch "
                    f"(expected {expected_digest[:12]}, got {digest.hexdigest()[:12]})"
                )

            extracted = Path(tmp_dir) / exe_name
            if asset_name.endswith(".gz"):
                with gzip.open(archive, "rb") as gz_fh, extracted.open("wb") as out_fh:
                    shutil.copyfileobj(gz_fh, out_fh, CHUNK_SIZE)
            elif asset_name.endswith(".zip"):
                with zipfile.ZipFile(archive) as zf:
                    exe_members = [m for m in zf.namelist() if m.lower().endswith(".exe")]
                    if not exe_members:
                        raise RuntimeError(f"No .exe found inside {asset_name}")
                    with zf.open(exe_members[0]) as src_fh, extracted.open("wb") as out_fh:
                        shutil.copyfileobj(src_fh, out_fh, CHUNK_SIZE)
            else:
                raise RuntimeError(f"Unsupported archive format: {asset_name}")

            if os.name != "nt":
                os.chmod(extracted, 0o755)
            os.replace(extracted, dest)

    validate_binary(dest)
    return dest


def prepare_mihomo(
    out_dir: Path,
    prior_manifest: dict[str, Any],
    *,
    force: bool = False,
) -> dict[str, Any]:
    exe_name = "mihomo.exe" if sys.platform == "win32" else "mihomo"
    zst_dest = out_dir / "bin" / f"{exe_name}.zst"

    prior = prior_manifest.get("mihomo") if isinstance(prior_manifest.get("mihomo"), dict) else {}
    if (
        not force
        and zst_dest.is_file()
        and prior.get("compressed_sha256")
        and prior.get("compressed_sha256") == sha256_file(zst_dest)
    ):
        print(
            f"[mihomo] Reusing cached {zst_dest.name} "
            f"(v{prior.get('version', '?')}, {format_mb(zst_dest.stat().st_size)})"
        )
        return prior

    try:
        binary = discover_binary(None, None)
    except Exception:
        binary = validate_binary(_download_mihomo_to_cache())

    raw_sha = sha256_file(binary.path)
    if (
        not force
        and zst_dest.is_file()
        and prior.get("sha256") == raw_sha
        and prior.get("compressed_sha256") == sha256_file(zst_dest)
    ):
        print(
            f"[mihomo] Reusing cached {zst_dest.name} "
            f"(v{binary.version}, {format_mb(zst_dest.stat().st_size)})"
        )
        return prior

    raw_sha, zst_sha, raw_size, zst_size = compress_file_zst(binary.path, zst_dest, level=19)
    print(f"[mihomo] Compressed v{binary.version}: {format_mb(raw_size)} -> {format_mb(zst_size)}")
    return {
        "file": f"bin/{exe_name}.zst",
        "version": binary.version,
        "sha256": raw_sha,
        "compressed_sha256": zst_sha,
        "raw_bytes": raw_size,
        "compressed_bytes": zst_size,
    }


def _download_geoip_to_cache() -> Path:
    import maxminddb

    override = os.environ.get("CPM_GEOIP_DOWNLOAD_URL")
    urls = (override,) if override else DEFAULT_GEOIP_URLS
    cache_dir = ROOT / ".cache" / "geoip"
    cache_dir.mkdir(parents=True, exist_ok=True)
    dest = cache_dir / "geoip.mmdb"

    last_error: Exception | None = None
    for url in urls:
        for attempt in range(1, 3):
            fd, tmp_name = tempfile.mkstemp(
                prefix=".geoip.mmdb.", suffix=".tmp", dir=str(cache_dir)
            )
            tmp_path = Path(tmp_name)
            fd_owned = True
            try:
                print(f"[geoip] Downloading offline GeoIP database from {url} (try {attempt})...")
                headers = {"User-Agent": "prism-browser-builder"}
                with httpx.Client(follow_redirects=True, timeout=180, headers=headers) as client:
                    with client.stream("GET", url) as resp:
                        resp.raise_for_status()
                        fd_owned = False
                        with os.fdopen(fd, "wb") as out_fh:
                            for chunk in resp.iter_bytes(CHUNK_SIZE):
                                out_fh.write(chunk)
                            out_fh.flush()
                            os.fsync(out_fh.fileno())
                if tmp_path.stat().st_size < 1024 * 100:
                    raise RuntimeError("Downloaded GeoIP database appears truncated or invalid.")
                with maxminddb.open_database(str(tmp_path)) as reader:
                    reader.get("8.8.8.8")
                os.replace(tmp_path, dest)
                return dest
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if fd_owned:
                    os.close(fd)
                tmp_path.unlink(missing_ok=True)
                print(f"[geoip] Attempt failed ({url}): {exc}", file=sys.stderr)
    raise RuntimeError(f"Failed to download offline GeoIP database: {last_error}")


def prepare_geoip(
    out_dir: Path,
    prior_manifest: dict[str, Any],
    *,
    force: bool = False,
) -> dict[str, Any]:
    geoip_dir = out_dir / "geoip"
    geoip_dir.mkdir(parents=True, exist_ok=True)
    # Remove any legacy config.yml left over from older bundles.
    (geoip_dir / "config.yml").unlink(missing_ok=True)

    zst_dest = geoip_dir / "geoip.mmdb.zst"
    prior = prior_manifest.get("geoip") if isinstance(prior_manifest.get("geoip"), dict) else {}
    if (
        not force
        and zst_dest.is_file()
        and prior.get("compressed_sha256")
        and prior.get("compressed_sha256") == sha256_file(zst_dest)
    ):
        print(f"[geoip] Reusing cached {zst_dest.name} ({format_mb(zst_dest.stat().st_size)})")
        return prior

    mmdb_path = geoip.geoip_db_path()
    if mmdb_path is None or not mmdb_path.is_file():
        mmdb_path = _download_geoip_to_cache()

    raw_sha = sha256_file(mmdb_path)
    if (
        not force
        and zst_dest.is_file()
        and prior.get("sha256") == raw_sha
        and prior.get("compressed_sha256") == sha256_file(zst_dest)
    ):
        print(f"[geoip] Reusing cached {zst_dest.name} ({format_mb(zst_dest.stat().st_size)})")
        return prior

    raw_sha, zst_sha, raw_size, zst_size = compress_file_zst(mmdb_path, zst_dest, level=19)
    print(f"[geoip] Compressed geoip.mmdb: {format_mb(raw_size)} -> {format_mb(zst_size)}")
    return {
        "file": "geoip/geoip.mmdb.zst",
        "sha256": raw_sha,
        "compressed_sha256": zst_sha,
        "raw_bytes": raw_size,
        "compressed_bytes": zst_size,
    }


def verify_existing(out_dir: Path) -> int:
    manifest_path = out_dir / "manifest.json"
    if not manifest_path.is_file():
        print(f"Missing manifest: {manifest_path}", file=sys.stderr)
        return 1
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for key in ("mihomo", "geoip"):
        entry = manifest.get(key)
        if not isinstance(entry, dict):
            print(f"Missing manifest entry: {key}", file=sys.stderr)
            return 1
        artifact = out_dir / str(entry["file"])
        if not artifact.is_file():
            print(f"Missing bundled artifact: {artifact}", file=sys.stderr)
            return 1
        expected = entry.get("compressed_sha256") or entry.get("sha256")
        actual = sha256_file(artifact)
        if expected and actual != expected:
            print(f"Checksum mismatch for {artifact}: {actual} != {expected}", file=sys.stderr)
            return 1
        print(f"[verify] OK {entry['file']} ({format_mb(artifact.stat().st_size)})")
    if (out_dir / "browser-kernel.tar.zst").exists():
        print(
            f"Stale legacy archive present: {out_dir / 'browser-kernel.tar.zst'}",
            file=sys.stderr,
        )
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Verify existing bundled runtime manifest & SHA-256 checksums without rebuilding.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force re-compression even if cached artifacts match current checksums.",
    )
    args = parser.parse_args()

    out_dir = bundle_runtime_output_dir()
    if args.verify_only:
        return verify_existing(out_dir)

    out_dir.mkdir(parents=True, exist_ok=True)
    # Remove any legacy 969 MB Firefox kernel archive so Electron never bundles it.
    stale_browser = out_dir / "browser-kernel.tar.zst"
    if stale_browser.exists():
        stale_browser.unlink()
        print(f"[cleanup] Removed legacy {stale_browser.name}")

    manifest_path = out_dir / "manifest.json"
    prior_manifest: dict[str, Any] = {}
    if manifest_path.is_file():
        try:
            prior_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            prior_manifest = {}

    manifest: dict[str, Any] = {
        "schema_version": 2,
        "platform": sys.platform,
        "arch": normalized_arch(),
        "built_at": datetime.now(timezone.utc).isoformat(),
        "mihomo": prepare_mihomo(out_dir, prior_manifest, force=args.force),
        "geoip": prepare_geoip(out_dir, prior_manifest, force=args.force),
    }

    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    total_compressed = sum(
        int(manifest[k]["compressed_bytes"]) for k in ("mihomo", "geoip") if k in manifest
    )
    print(f"Bundled runtime ready at {out_dir} (total {format_mb(total_compressed)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
