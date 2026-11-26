"""Offline bundled runtime discovery and zero-network first-run bootstrap.

When packaged with the Electron desktop shell, Prism Browser ships a compact
``runtime/`` directory (~62 MB total) holding zstd-compressed archives for:

1. The dedicated local proxy routing engine (``bin/mihomo(.exe).zst``)
2. The offline IP geolocation database (``geoip/geoip.mmdb.zst``)

Both archives are verified against ``runtime/manifest.json`` SHA-256 checksums
and extracted atomically into ``<data_dir>`` on first boot with zero outbound
network access.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from loguru import logger

_CHUNK_SIZE = 1024 * 1024  # 1 MiB streaming buffer


def _mihomo_exe_name() -> str:
    return "mihomo.exe" if os.name == "nt" else "mihomo"


def bundled_runtime_dir() -> Path | None:
    """Locate the packaged ``runtime/`` directory if present on disk."""
    candidates: list[Path] = []
    override = os.environ.get("CPM_BUNDLED_RUNTIME_DIR")
    if override:
        candidates.append(Path(override).expanduser())

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        mp = Path(meipass)
        candidates.append(mp / "runtime")
        candidates.append(mp.parent / "runtime")
        candidates.append(mp.parent / "Resources" / "runtime")

    if getattr(sys, "frozen", False):
        exe_parent = Path(sys.executable).resolve().parent
        # Electron extraResources places sidecar in resources/backend/ and runtime in resources/runtime/.
        candidates.append(exe_parent.parent / "runtime")
        candidates.append(exe_parent / "runtime")
        candidates.append(exe_parent / "_internal" / "runtime")
        candidates.append(exe_parent.parent / "Resources" / "runtime")

    for candidate in candidates:
        try:
            if candidate.is_dir() and (
                (candidate / "manifest.json").is_file()
                or (candidate / "bin").is_dir()
                or (candidate / "geoip").is_dir()
            ):
                return candidate.resolve()
        except OSError:
            continue
    return None


def load_manifest(runtime_dir: Path | None = None) -> dict[str, Any]:
    """Read ``manifest.json`` from the bundled runtime directory, if present."""
    root = runtime_dir or bundled_runtime_dir()
    if root is None:
        return {}
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        return {}
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError) as exc:
        logger.warning(f"Could not read bundled runtime manifest: {exc}")
        return {}


def bundled_status(runtime_dir: Path | None = None) -> dict[str, bool]:
    """Report which offline runtime artifacts are bundled in this build."""
    root = runtime_dir or bundled_runtime_dir()
    if root is None:
        return {
            "bundled_mihomo": False,
            "bundled_geoip": False,
        }
    exe = _mihomo_exe_name()
    return {
        "bundled_mihomo": (root / "bin" / f"{exe}.zst").is_file() or (root / "bin" / exe).is_file(),
        "bundled_geoip": (root / "geoip" / "geoip.mmdb.zst").is_file()
        or (root / "geoip" / "geoip.mmdb").is_file(),
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_sha256(path: Path, expected: str | None, label: str) -> None:
    if not expected:
        return
    actual = _sha256_file(path)
    if actual.lower() != expected.lower():
        raise RuntimeError(
            f"Bundled {label} integrity verification failed (expected {expected[:12]}, got {actual[:12]})."
        )


def _decompress_zst_atomic(
    src: Path,
    dest: Path,
    *,
    expected_compressed_sha256: str | None = None,
    expected_raw_sha256: str | None = None,
    mode: int | None = None,
) -> str:
    """Verify and stream-decompress a ``.zst`` file atomically to ``dest``."""
    import zstandard

    _verify_sha256(src, expected_compressed_sha256, src.name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{dest.name}.", suffix=".tmp", dir=str(dest.parent))
    tmp_path = Path(tmp_name)
    fd_owned = True
    raw_digest = hashlib.sha256()
    try:
        dctx = zstandard.ZstdDecompressor()
        fd_owned = False
        with os.fdopen(fd, "wb") as out_fh, src.open("rb") as in_fh:
            with dctx.stream_reader(in_fh) as reader:
                while chunk := reader.read(_CHUNK_SIZE):
                    out_fh.write(chunk)
                    raw_digest.update(chunk)
            out_fh.flush()
            os.fsync(out_fh.fileno())
        actual_raw = raw_digest.hexdigest()
        if expected_raw_sha256 and actual_raw.lower() != expected_raw_sha256.lower():
            raise RuntimeError(f"Bundled {dest.name} decompressed checksum mismatch.")
        if mode is not None and os.name != "nt":
            os.chmod(tmp_path, mode)
        os.replace(tmp_path, dest)
        return actual_raw
    except BaseException:
        if fd_owned:
            os.close(fd)
        tmp_path.unlink(missing_ok=True)
        raise


def _copy_atomic(
    src: Path,
    dest: Path,
    *,
    expected_sha256: str | None = None,
    mode: int | None = None,
) -> str:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{dest.name}.", suffix=".tmp", dir=str(dest.parent))
    tmp_path = Path(tmp_name)
    fd_owned = True
    digest = hashlib.sha256()
    try:
        fd_owned = False
        with os.fdopen(fd, "wb") as out_fh, src.open("rb") as in_fh:
            while chunk := in_fh.read(_CHUNK_SIZE):
                out_fh.write(chunk)
                digest.update(chunk)
            out_fh.flush()
            os.fsync(out_fh.fileno())
        actual = digest.hexdigest()
        if expected_sha256 and actual.lower() != expected_sha256.lower():
            raise RuntimeError(f"Bundled {dest.name} checksum mismatch.")
        if mode is not None and os.name != "nt":
            os.chmod(tmp_path, mode)
        os.replace(tmp_path, dest)
        return actual
    except BaseException:
        if fd_owned:
            os.close(fd)
        tmp_path.unlink(missing_ok=True)
        raise


def _maybe_adhoc_codesign(binary: Path) -> None:
    """Ad-hoc sign an extracted macOS binary if it does not already carry a valid signature."""
    if sys.platform != "darwin":
        return
    codesign = shutil.which("codesign")
    if not codesign:
        return
    try:
        verify = subprocess.run(
            [codesign, "-v", str(binary)],
            capture_output=True,
            timeout=10,
            check=False,
        )
        if verify.returncode != 0:
            subprocess.run(
                [codesign, "--force", "-s", "-", str(binary)],
                capture_output=True,
                timeout=15,
                check=False,
            )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _resolve_data_dir(data_dir: Path | None) -> Path:
    if data_dir is not None:
        return data_dir.expanduser().resolve()
    from camoufox_pm.config import get_settings

    return Path(get_settings().db_path).expanduser().resolve().parent


def ensure_bundled_mihomo(
    data_dir: Path | None = None,
    *,
    runtime_dir: Path | None = None,
) -> Path | None:
    """Ensure the bundled proxy routing engine binary is extracted into ``<data_dir>/bin``."""
    root = runtime_dir or bundled_runtime_dir()
    if root is None:
        return None
    exe = _mihomo_exe_name()
    zst_src = root / "bin" / f"{exe}.zst"
    raw_src = root / "bin" / exe
    if not zst_src.is_file() and not raw_src.is_file():
        return None

    target_data = _resolve_data_dir(data_dir)
    manifest = load_manifest(root)
    raw_entry = manifest.get("mihomo")
    entry: dict[str, Any] = raw_entry if isinstance(raw_entry, dict) else {}
    expected_raw = entry.get("sha256") if isinstance(entry.get("sha256"), str) else None
    expected_zst = (
        entry.get("compressed_sha256") if isinstance(entry.get("compressed_sha256"), str) else None
    )

    bin_dir = target_data / "bin"
    dest = bin_dir / exe
    stamp = bin_dir / f".{exe}.stamp"
    expected_stamp = (
        expected_raw
        or expected_zst
        or str(int((zst_src if zst_src.is_file() else raw_src).stat().st_mtime))
    )

    if dest.is_file() and stamp.is_file():
        try:
            if stamp.read_text(encoding="utf-8").strip() == expected_stamp:
                if os.name == "nt" or os.access(dest, os.X_OK):
                    return dest
        except OSError:
            pass

    bin_dir.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(bin_dir, 0o700)

    if zst_src.is_file():
        raw_hash = _decompress_zst_atomic(
            zst_src,
            dest,
            expected_compressed_sha256=expected_zst,
            expected_raw_sha256=expected_raw,
            mode=0o755,
        )
    else:
        raw_hash = _copy_atomic(raw_src, dest, expected_sha256=expected_raw, mode=0o755)

    _maybe_adhoc_codesign(dest)
    stamp.write_text(expected_raw or expected_zst or raw_hash, encoding="utf-8")
    logger.info(f"Initialized bundled proxy routing engine at {dest}")
    return dest


def ensure_bundled_geoip(
    data_dir: Path | None = None,
    *,
    runtime_dir: Path | None = None,
) -> bool:
    """Ensure the offline GeoIP database is extracted into ``<data_dir>/geoip/geoip.mmdb``."""
    root = runtime_dir or bundled_runtime_dir()
    if root is None:
        return False
    zst_src = root / "geoip" / "geoip.mmdb.zst"
    raw_src = root / "geoip" / "geoip.mmdb"
    if not zst_src.is_file() and not raw_src.is_file():
        return False

    from . import geoip

    dest = geoip.target_geoip_path(_resolve_data_dir(data_dir))
    dest.parent.mkdir(parents=True, exist_ok=True)
    stamp = dest.parent / ".geoip.stamp"

    manifest = load_manifest(root)
    raw_entry = manifest.get("geoip")
    entry: dict[str, Any] = raw_entry if isinstance(raw_entry, dict) else {}
    expected_raw = entry.get("sha256") if isinstance(entry.get("sha256"), str) else None
    expected_zst = (
        entry.get("compressed_sha256") if isinstance(entry.get("compressed_sha256"), str) else None
    )
    expected_stamp = expected_raw or expected_zst or ""

    if dest.is_file() and dest.stat().st_size > 1024:
        if not expected_stamp:
            return True
        try:
            if stamp.is_file() and stamp.read_text(encoding="utf-8").strip() == expected_stamp:
                return True
        except OSError:
            pass

    geoip.close_reader()
    if zst_src.is_file():
        raw_hash = _decompress_zst_atomic(
            zst_src,
            dest,
            expected_compressed_sha256=expected_zst,
            expected_raw_sha256=expected_raw,
            mode=0o644,
        )
    else:
        raw_hash = _copy_atomic(raw_src, dest, expected_sha256=expected_raw, mode=0o644)

    stamp.write_text(expected_stamp or raw_hash, encoding="utf-8")
    logger.info(f"Initialized offline GeoIP database at {dest} ({dest.stat().st_size} bytes)")
    return dest.is_file()


def bootstrap_bundled_runtime(
    data_dir: Path | None = None,
    *,
    runtime_dir: Path | None = None,
) -> dict[str, Any]:
    """Synchronously extract bundled proxy engine and offline GeoIP database."""
    mihomo_path = ensure_bundled_mihomo(data_dir, runtime_dir=runtime_dir)
    geoip_ok = ensure_bundled_geoip(data_dir, runtime_dir=runtime_dir)
    return {
        "mihomo": str(mihomo_path) if mihomo_path else None,
        "geoip": geoip_ok,
    }
