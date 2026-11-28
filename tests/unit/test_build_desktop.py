"""Unit tests for local auto-versioning and dist update source registration in scripts/build_desktop.py."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

BUILD_DESKTOP_PATH = Path(__file__).resolve().parents[2] / "scripts" / "build_desktop.py"
SPEC = importlib.util.spec_from_file_location("build_desktop", BUILD_DESKTOP_PATH)
assert SPEC and SPEC.loader
build_desktop = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build_desktop)


def test_resolve_local_auto_version_picks_highest_matching_minor_plus_one(tmp_path: Path) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    (dist_dir / "prism-update-darwin-arm64.json").write_text(
        json.dumps({"version": "0.6.4"}), encoding="utf-8"
    )
    user_data = tmp_path / "userdata"
    user_data.mkdir()
    (user_data / "app-version.json").write_text(
        json.dumps({"version": "0.6.3"}), encoding="utf-8"
    )

    assert (
        build_desktop.resolve_local_auto_version(
            base_version="0.6.0",
            dist_dir=dist_dir,
            user_data_dir=user_data,
            installed_version="0.6.5",
        )
        == "0.6.6"
    )


def test_resolve_local_auto_version_ignores_other_minor_lines(tmp_path: Path) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    (dist_dir / "prism-update-darwin-arm64.json").write_text(
        json.dumps({"version": "0.7.9"}), encoding="utf-8"
    )
    assert (
        build_desktop.resolve_local_auto_version(
            base_version="0.6.0",
            dist_dir=dist_dir,
            user_data_dir=tmp_path / "missing",
            installed_version="",
        )
        == "0.6.1"
    )


def test_register_local_update_source_and_atomic_manifest(tmp_path: Path) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    dmg = dist_dir / "prism-browser-macos-arm64.dmg"
    dmg.write_bytes(b"dmg-payload")

    written = build_desktop.write_update_manifests([dmg], "0.6.4", released_at="2026-11-28T03:00:00Z")
    assert len(written) == 1
    manifest = json.loads(written[0].read_text(encoding="utf-8"))
    assert manifest["version"] == "0.6.4"
    assert manifest["file"] == "prism-browser-macos-arm64.dmg"
    assert manifest["size"] == len(b"dmg-payload")

    user_data = tmp_path / "userdata"
    marker = build_desktop.register_local_update_source(dist_dir, user_data_dir=user_data)
    saved = json.loads(marker.read_text(encoding="utf-8"))
    assert saved["dist_dir"] == str(dist_dir.resolve())
    assert "updated_at" in saved
