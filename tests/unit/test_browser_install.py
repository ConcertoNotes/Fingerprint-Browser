"""Embedded isolation browser readiness and compact offline runtime bootstrap tests."""

from __future__ import annotations

from camoufox_pm.core import browser_install, geoip


def test_embedded_browser_is_always_installed_without_downloads(tmp_path, monkeypatch):
    monkeypatch.setenv("CPM_DB_PATH", str(tmp_path / "profiles.db"))
    monkeypatch.delenv("CPM_GEOIP_DB_PATH", raising=False)
    monkeypatch.setattr(geoip, "_dev_fallback_paths", lambda: [])

    assert browser_install.installed_browser() == (True, "embedded-chromium")
    assert browser_install.browser_resources_ready(tmp_path) is False
    browser_install.ensure_browser_ready()

    installer = browser_install.BrowserInstaller()
    status = installer.status(tmp_path)
    assert status.installed is True
    assert status.bundled is True
    assert status.version == "embedded-chromium"
    assert status.state == "ready"
    assert status.progress == 100
    assert status.error is None


def test_browser_resources_ready_tracks_local_geoip_database(tmp_path, monkeypatch):
    monkeypatch.delenv("CPM_GEOIP_DB_PATH", raising=False)
    monkeypatch.setattr(geoip, "_dev_fallback_paths", lambda: [])
    assert browser_install.browser_resources_ready(tmp_path) is False

    mmdb = tmp_path / "geoip" / "geoip.mmdb"
    mmdb.parent.mkdir(parents=True, exist_ok=True)
    mmdb.write_bytes(b"M" * 4096)
    assert browser_install.browser_resources_ready(tmp_path) is True
