"""Unit tests for compact offline bundled runtime discovery and zstd extraction."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest
import zstandard

from camoufox_pm.core import browser_install, bundled_runtime, geoip, mihomo_manager


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_zst(path: Path, raw: bytes) -> tuple[str, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    compressed = zstandard.ZstdCompressor(level=3).compress(raw)
    path.write_bytes(compressed)
    return _sha256_bytes(raw), _sha256_bytes(compressed)


def test_bundled_runtime_status_and_discovery_via_env(tmp_path, monkeypatch):
    rt = tmp_path / "runtime"
    exe = "mihomo.exe" if os.name == "nt" else "mihomo"
    _write_zst(rt / "bin" / f"{exe}.zst", b"fake-mihomo-bin")
    _write_zst(rt / "geoip" / "geoip.mmdb.zst", b"x" * 2048)
    (rt / "manifest.json").write_text("{}", encoding="utf-8")

    monkeypatch.setenv("CPM_BUNDLED_RUNTIME_DIR", str(rt))
    assert bundled_runtime.bundled_runtime_dir() == rt.resolve()
    assert bundled_runtime.bundled_status() == {
        "bundled_mihomo": True,
        "bundled_geoip": True,
    }


def test_ensure_bundled_mihomo_extracts_verifies_and_caches(tmp_path, monkeypatch):
    rt = tmp_path / "runtime"
    data_dir = tmp_path / "data"
    exe = "mihomo.exe" if os.name == "nt" else "mihomo"
    payload = b"#!/bin/sh\necho 'Mihomo Meta v1.19.32 darwin arm64'\n"
    raw_sha, zst_sha = _write_zst(rt / "bin" / f"{exe}.zst", payload)
    (rt / "manifest.json").write_text(
        json.dumps(
            {
                "mihomo": {
                    "file": f"bin/{exe}.zst",
                    "version": "1.19.32",
                    "sha256": raw_sha,
                    "compressed_sha256": zst_sha,
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(bundled_runtime, "_maybe_adhoc_codesign", lambda _p: None)

    extracted = bundled_runtime.ensure_bundled_mihomo(data_dir, runtime_dir=rt)
    assert extracted is not None and extracted.is_file()
    assert extracted.read_bytes() == payload
    if os.name != "nt":
        assert os.access(extracted, os.X_OK)
        assert stat.S_IMODE(extracted.parent.stat().st_mode) == 0o700

    # Second call hits stamp cache immediately without re-decompressing.
    monkeypatch.setattr(
        bundled_runtime,
        "_decompress_zst_atomic",
        lambda *_a, **_kw: pytest.fail("Should reuse cached extracted binary"),
    )
    again = bundled_runtime.ensure_bundled_mihomo(data_dir, runtime_dir=rt)
    assert again == extracted


def test_ensure_bundled_mihomo_rejects_corrupted_sha256(tmp_path):
    rt = tmp_path / "runtime"
    data_dir = tmp_path / "data"
    exe = "mihomo.exe" if os.name == "nt" else "mihomo"
    _write_zst(rt / "bin" / f"{exe}.zst", b"tampered-bytes")
    (rt / "manifest.json").write_text(
        json.dumps({"mihomo": {"sha256": "0" * 64, "compressed_sha256": "f" * 64}}),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="integrity verification failed"):
        bundled_runtime.ensure_bundled_mihomo(data_dir, runtime_dir=rt)
    assert not (data_dir / "bin" / exe).exists()


def test_ensure_bundled_geoip_extracts_into_data_dir_and_caches(tmp_path, monkeypatch):
    rt = tmp_path / "runtime"
    data_dir = tmp_path / "data"
    monkeypatch.delenv("CPM_GEOIP_DB_PATH", raising=False)
    monkeypatch.setattr(geoip, "_dev_fallback_paths", lambda: [])

    payload = b"MMDB-SYNTHETIC-PAYLOAD-" * 100
    raw_sha, zst_sha = _write_zst(rt / "geoip" / "geoip.mmdb.zst", payload)
    (rt / "manifest.json").write_text(
        json.dumps({"geoip": {"sha256": raw_sha, "compressed_sha256": zst_sha}}),
        encoding="utf-8",
    )

    assert not browser_install.browser_resources_ready(data_dir)
    assert bundled_runtime.ensure_bundled_geoip(data_dir, runtime_dir=rt) is True
    dest = data_dir / "geoip" / "geoip.mmdb"
    assert dest.read_bytes() == payload
    assert browser_install.browser_resources_ready(data_dir) is True

    # Second call reuses the verified stamp without re-decompressing.
    monkeypatch.setattr(
        bundled_runtime,
        "_decompress_zst_atomic",
        lambda *_a, **_kw: pytest.fail("Should reuse cached geoip.mmdb"),
    )
    assert bundled_runtime.ensure_bundled_geoip(data_dir, runtime_dir=rt) is True


def test_bootstrap_bundled_runtime_unpacks_mihomo_and_geoip(tmp_path, monkeypatch):
    rt = tmp_path / "runtime"
    data_dir = tmp_path / "data"
    exe = "mihomo.exe" if os.name == "nt" else "mihomo"
    m_raw, m_zst = _write_zst(rt / "bin" / f"{exe}.zst", b"fake-mihomo-bin")
    g_raw, g_zst = _write_zst(rt / "geoip" / "geoip.mmdb.zst", b"G" * 4096)
    (rt / "manifest.json").write_text(
        json.dumps(
            {
                "mihomo": {"sha256": m_raw, "compressed_sha256": m_zst},
                "geoip": {"sha256": g_raw, "compressed_sha256": g_zst},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(bundled_runtime, "_maybe_adhoc_codesign", lambda _p: None)

    result = bundled_runtime.bootstrap_bundled_runtime(data_dir, runtime_dir=rt)
    assert result["mihomo"] == str((data_dir / "bin" / exe).resolve())
    assert result["geoip"] is True


def test_mihomo_error_messages_are_whitelabeled():
    with pytest.raises(mihomo_manager.MihomoUnavailable) as caught:
        mihomo_manager.validate_binary(Path("/nonexistent/proxy-engine-xyz"))
    assert "mihomo" not in str(caught.value).lower()
    assert "prepare_mihomo" not in str(caught.value).lower()
