#!/usr/bin/env python3
"""Install a Mihomo executable the manager (and its tests) will accept.

The manager refuses setuid/setgid, group/world-writable or foreign-owned
binaries. Desktop proxy clients often ship exactly such a copy (for example a
setuid-root ``sidecar/mihomo``), so this script installs a normal user-owned
copy instead of pointing at theirs.

    # Copy an existing Mihomo (drops setuid/setgid bits)
    uv run python scripts/prepare_mihomo.py --from "/Applications/<Client>.app/Contents/Resources/sidecar/mihomo"

    # Or download an official release asset and verify it against a known hash
    uv run python scripts/prepare_mihomo.py \\
        --url https://github.com/MetaCubeX/mihomo/releases/download/v1.19.32/mihomo-darwin-arm64-v1.19.32.gz \\
        --sha256 <hash of that .gz from a source you trust>

Default destination: ``.cache/mihomo/`` in this checkout (git-ignored), which
source runs and tests discover automatically. Use ``--dest`` for the desktop
data folder (``<data dir>/bin``) or set ``CPM_MIHOMO_PATH``.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import os
import shutil
import stat
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = "mihomo.exe" if os.name == "nt" else "mihomo"
MAX_DOWNLOAD = 200 * 1024 * 1024


def _install(source: Path, destination_dir: Path) -> Path:
    destination_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = destination_dir / NAME
    with tempfile.NamedTemporaryFile(dir=destination_dir, delete=False) as handle:
        temporary = Path(handle.name)
        with source.open("rb") as original:
            shutil.copyfileobj(original, handle)
    # A fresh file owned by this user: no setuid/setgid, no group/other writes.
    os.chmod(temporary, stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
    os.replace(temporary, target)
    return target


def _download(url: str, expected: str, workdir: Path) -> Path:
    if not url.startswith("https://"):
        raise SystemExit("Only HTTPS downloads are accepted")
    expected = expected.strip().lower()
    if len(expected) != 64 or any(ch not in "0123456789abcdef" for ch in expected):
        raise SystemExit("--sha256 must be 64 hexadecimal characters")
    archive = workdir / "download"
    digest = hashlib.sha256()
    with urllib.request.urlopen(url, timeout=60) as response, archive.open("wb") as handle:  # noqa: S310
        total = 0
        while chunk := response.read(1024 * 1024):
            total += len(chunk)
            if total > MAX_DOWNLOAD:
                raise SystemExit("Download is larger than expected")
            digest.update(chunk)
            handle.write(chunk)
    if digest.hexdigest() != expected:
        raise SystemExit("SHA-256 mismatch: the download does not match the expected hash")
    if url.endswith(".gz"):
        binary = workdir / NAME
        with gzip.open(archive, "rb") as source, binary.open("wb") as target:
            shutil.copyfileobj(source, target, length=1024 * 1024)
        return binary
    if url.endswith(".zip"):
        import zipfile

        with zipfile.ZipFile(archive) as bundle:
            members = [m for m in bundle.namelist() if Path(m).name.lower().startswith("mihomo")]
            if len(members) != 1:
                raise SystemExit("Could not find exactly one Mihomo executable in the archive")
            binary = workdir / NAME
            with bundle.open(members[0]) as source, binary.open("wb") as target:
                shutil.copyfileobj(source, target)
        return binary
    return archive


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--from", dest="source", help="Path to an existing Mihomo executable")
    group.add_argument("--url", help="HTTPS URL of an official release asset (.gz, .zip or raw)")
    parser.add_argument("--sha256", help="Expected SHA-256 of the --url download")
    parser.add_argument(
        "--dest", default=str(ROOT / ".cache" / "mihomo"), help="Destination folder"
    )
    args = parser.parse_args()

    sys.path.insert(0, str(ROOT / "src"))
    from camoufox_pm.core.mihomo_manager import MihomoUnavailable, validate_binary

    destination = Path(args.dest).expanduser()
    with tempfile.TemporaryDirectory() as workdir:
        if args.source:
            source = Path(args.source).expanduser()
            if not source.is_file():
                raise SystemExit(f"Not a file: {source}")
        else:
            if not args.sha256:
                raise SystemExit("--url requires --sha256")
            source = _download(args.url, args.sha256, Path(workdir))
        target = _install(source, destination)
    try:
        binary = validate_binary(target)
    except MihomoUnavailable as exc:
        target.unlink(missing_ok=True)
        raise SystemExit(f"The installed file is not usable: {exc}") from None
    print(f"Mihomo {binary.version} ready at {binary.path}")
    print("The manager finds it automatically when running from this checkout;")
    print(f"otherwise set CPM_MIHOMO_PATH={binary.path} or choose it in Settings → Network.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
