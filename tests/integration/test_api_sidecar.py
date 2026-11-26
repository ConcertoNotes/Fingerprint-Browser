"""Integration tests for Electron desktop-token guarded session prepare/release routes."""

from __future__ import annotations

import pytest

from camoufox_pm.config import get_settings


@pytest.mark.asyncio
async def test_session_prepare_and_release_enforce_desktop_token(client, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CPM_DESKTOP_TOKEN", "deadbeef" * 8)
    get_settings.cache_clear()
    try:
        created = await client.post("/api/v1/profiles", json={"name": "Electron Profile"})
        assert created.status_code == 201
        pid = created.json()["id"]

        # Missing or wrong token returns 403 desktop_token_required.
        no_tok = await client.post(f"/api/v1/profiles/{pid}/session/prepare", json={})
        assert no_tok.status_code == 403
        assert no_tok.json()["error"]["code"] == "desktop_token_required"

        bad_tok = await client.post(
            f"/api/v1/profiles/{pid}/session/prepare",
            json={},
            headers={"X-Prism-Desktop-Token": "wrong"},
        )
        assert bad_tok.status_code == 403

        bad_rel = await client.post(
            f"/api/v1/profiles/{pid}/session/release",
            headers={"X-Prism-Desktop-Token": "wrong"},
        )
        assert bad_rel.status_code == 403

        # Matching token succeeds and returns complete CDP isolation descriptor.
        headers = {"X-Prism-Desktop-Token": "deadbeef" * 8}
        prepared = await client.post(
            f"/api/v1/profiles/{pid}/session/prepare", json={}, headers=headers
        )
        assert prepared.status_code == 200
        data = prepared.json()["data"]
        assert data["profile_id"] == pid
        assert data["status"] == "ready"
        assert data["isolation"]["timezone"] == "America/Los_Angeles"
        assert "Chrome/" in data["isolation"]["pinned_fingerprint"]["user_agent"]
        assert "Electron/" not in data["isolation"]["pinned_fingerprint"]["user_agent"]

        active = (await client.get("/api/v1/browsers/active")).json()
        assert active["count"] == 1 and active["active_browsers"][0]["profile_id"] == pid

        released = await client.post(f"/api/v1/profiles/{pid}/session/release", headers=headers)
        assert released.status_code == 200
        assert released.json()["data"]["status"] == "closed"
        assert (await client.get("/api/v1/browsers/active")).json()["count"] == 0
    finally:
        get_settings.cache_clear()
