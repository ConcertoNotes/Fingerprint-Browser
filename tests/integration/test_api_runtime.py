"""Browser runtime API safety: explicit installs and API key protection."""

from unittest.mock import Mock

import pytest

from camoufox_pm.api.routes import runtime
from camoufox_pm.config import get_settings
from camoufox_pm.core.browser_install import BrowserStatus


@pytest.fixture(autouse=True)
def isolate_auth(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CPM_API_KEY", raising=False)
    monkeypatch.delenv("CPM_SECRET_KEY", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.parametrize("prefix", ["/api/v1", "/api"])
async def test_readiness_get_never_starts_installation(client, monkeypatch, prefix):
    installer = Mock()
    installer.status.return_value = BrowserStatus(
        installed=False, state="idle", message="Not installed"
    )
    monkeypatch.setattr(runtime, "browser_installer", installer)
    response = await client.get(f"{prefix}/system/browser")
    assert response.status_code == 200
    assert response.json()["data"]["installed"] is False
    assert response.json()["data"]["state"] == "idle"
    installer.start.assert_not_called()


async def test_default_browser_status_reports_embedded_chromium_ready(client):
    response = await client.get("/api/v1/system/browser")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["installed"] is True
    assert data["state"] == "ready"
    assert data["version"] == "embedded-chromium"


async def test_explicit_post_starts_download_and_returns_pollable_progress(client, monkeypatch):
    installer = Mock()
    installer.start.return_value = BrowserStatus(
        installed=False, state="downloading", progress=42, message="Downloading"
    )
    monkeypatch.setattr(runtime, "browser_installer", installer)
    response = await client.post("/api/v1/system/browser/install")
    assert response.status_code == 200
    assert response.json()["data"]["state"] == "downloading"
    assert response.json()["data"]["progress"] == 42
    installer.start.assert_called_once_with()


async def test_runtime_routes_use_existing_api_auth_guard(client, monkeypatch):
    monkeypatch.setenv("CPM_API_KEY", "test-runtime-key")
    get_settings.cache_clear()
    installer = Mock()
    monkeypatch.setattr(runtime, "browser_installer", installer)
    for path, method in [
        ("/system/browser", "GET"),
        ("/system/browser/install", "POST"),
    ]:
        response = await client.request(method, f"/api/v1{path}")
        assert response.status_code == 401
    installer.start.assert_not_called()
    installer.status.assert_not_called()
