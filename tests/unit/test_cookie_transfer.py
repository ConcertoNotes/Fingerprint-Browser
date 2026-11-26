"""Validation and local vault transfer safety for JSON and Netscape cookies."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from camoufox_pm.core import cookie_transfer as ct
from camoufox_pm.core.database import StaleWriteError
from camoufox_pm.core.leases import ProfileLocked

SECRET = "synthetic-cookie-secret"
COOKIE = {
    "name": "token",
    "value": SECRET,
    "domain": "localhost",
    "path": "/",
    "expires": 2_000_000_000,
    "httpOnly": True,
    "secure": False,
    "sameSite": "Strict",
}


@pytest.mark.parametrize(
    "changes",
    [
        {"name": "invalid name"},
        {"value": "bad\nsecret"},
        {"domain": "secret@localhost"},
        {"path": "secret"},
        {"secure": "true"},
        {"httpOnly": 1},
        {"sameSite": "unknown"},
        {"sameSite": "None", "secure": False},
        {"expires": float("nan")},
        {"expires": True},
        {"expires": -2},
        {"value": "x" * 4097},
        {"partitionKey": "secret"},
        {"originAttributes": "secret"},
        {"storeId": "firefox-container-1"},
        {"domain": "127.0.0.1", "hostOnly": False},
        {"domain": ".localhost"},
        {"domain": "localhost", "hostOnly": False},
        {"value": "\ud800"},
        {"path": "/\ud800"},
    ],
)
def test_invalid_json_has_safe_row_errors_and_never_echoes_values(changes):
    with pytest.raises(ct.CookieTransferError) as failure:
        ct.parse_cookies(json.dumps([{**COOKIE, **changes}]), "json", now=0)
    assert "Row 1" in str(failure.value)
    assert SECRET not in str(failure.value) and "secret@localhost" not in str(failure.value)


def test_complete_validation_precedes_expiry_and_duplicate_skips():
    items = [{**COOKIE, "expires": 1}, COOKIE, {**COOKIE, "value": "updated"}]
    parsed = ct.parse_cookies(json.dumps(items), "json", now=2)
    assert parsed.total == 3 and len(parsed.cookies) == 1
    assert parsed.cookies[0]["value"] == "updated"
    assert parsed.skipped == [
        {"row": 1, "reason": "Expired cookie"},
        {"row": 2, "reason": "Duplicate cookie; later entry wins"},
    ]
    with pytest.raises(ct.CookieTransferError):
        ct.parse_cookies(json.dumps(items + [{**COOKIE, "path": "invalid"}]), "json")


def test_browser_json_and_editthiscookie_fields_are_normalized():
    parsed = ct.parse_cookies(
        json.dumps(
            [
                {
                    "name": "a",
                    "value": "b",
                    "domain": ".LOCALHOST",
                    "hostOnly": True,
                    "session": True,
                    "expirationDate": 123,
                    "sameSite": "unspecified",
                    "storeId": "0",
                }
            ]
        ),
        "json",
    )
    assert parsed.cookies[0]["domain"] == "localhost"
    assert parsed.cookies[0]["expires"] == -1 and parsed.cookies[0]["sameSite"] == "Lax"
    assert parsed.preview()["session_count"] == 1


def test_url_infers_directory_scope_and_secure_scheme():
    cookie = ct.parse_cookies(
        json.dumps(
            [
                {
                    "name": "url-proof",
                    "value": "synthetic",
                    "url": "https://localhost/account/login",
                    "sameSite": "None",
                }
            ]
        ),
        "json",
    ).cookies[0]
    assert cookie["domain"] == "localhost" and cookie["path"] == "/account/"
    assert cookie["secure"] is True and cookie["expires"] == -1
    for fields in (
        {"domain": "localhost"},
        {"path": "/"},
        {"url": "https://secret@localhost/"},
        {"url": "https://localhost:invalid/"},
    ):
        with pytest.raises(ct.CookieTransferError):
            ct.parse_cookies(
                json.dumps(
                    [{"name": "proof", "value": SECRET, "url": "https://localhost/", **fields}]
                ),
                "json",
            )


def test_json_and_netscape_roundtrip_and_httponly_session_flags():
    cookies = [
        COOKIE,
        {
            **COOKIE,
            "name": "session",
            "domain": ".example.test",
            "expires": -1,
            "sameSite": "Lax",
            "httpOnly": True,
        },
    ]
    for fmt in ("json", "netscape"):
        text = ct.serialize_cookies(cookies, fmt)
        normalized = ct.parse_cookies(text, fmt, now=0).cookies
        assert normalized[0]["httpOnly"]
        assert normalized[1]["domain"] == ".example.test" and normalized[1]["expires"] == -1
        assert normalized[0]["value"] == SECRET
    assert "#HttpOnly_.example.test\tTRUE" in ct.serialize_cookies(cookies, "netscape")


@pytest.mark.parametrize(
    "text",
    [
        "not-json-cookie-secret",
        "{}",
        "[123]",
        "[",
        "\ud800",
    ],
)
def test_invalid_or_nonarray_json_does_not_echo_input(text):
    with pytest.raises(ct.CookieTransferError) as failure:
        ct.parse_cookies(text, "json")
    assert "cookie-secret" not in str(failure.value)


def test_bounds_and_netscape_overflow_are_safe():
    with pytest.raises(ct.CookieTransferError):
        ct.parse_cookies(" " * (ct.MAX_BYTES + 1), "json")
    with pytest.raises(ct.CookieTransferError):
        ct.parse_cookies(json.dumps([COOKIE] * (ct.MAX_COOKIES + 1)), "json")
    text = "localhost\tFALSE\t/\tFALSE\t" + "9" * 5000 + "\ttoken\tsecret"
    with pytest.raises(ct.CookieTransferError) as failure:
        ct.parse_cookies(text, "netscape")
    assert "secret" not in str(failure.value)


@pytest.mark.asyncio
async def test_merge_replace_and_portable_export_via_local_vault(profile_manager):
    transfer = ct.CookieTransfer(profile_manager)
    profile = await profile_manager.create_profile("cookies", generate_fingerprint=False)

    first = await transfer.import_cookies(profile.id, json.dumps([COOKIE]), "json", "merge", 0)
    assert first["imported"] == 1 and first["row_version"] == 1 and first["backup_id"] is None

    updated = {**COOKIE, "name": "new", "value": "new-value"}
    merged = await transfer.import_cookies(profile.id, json.dumps([updated]), "json", "merge", 1)
    assert merged["row_version"] == 2

    text = await transfer.export_cookies(profile.id, "json", 2)
    assert {cookie["name"] for cookie in json.loads(text)} == {"token", "new"}

    replaced = await transfer.import_cookies(profile.id, "[]", "json", "replace", 2, True)
    assert replaced["backup_id"] is None and replaced["row_version"] == 3
    assert await transfer.export_cookies(profile.id, "json", 3) == "[]"


@pytest.mark.asyncio
async def test_stale_version_live_lease_and_confirmation_leave_cookie_vault_unchanged(
    profile_manager,
):
    transfer = ct.CookieTransfer(profile_manager)
    profile = await profile_manager.create_profile("closed", generate_fingerprint=False)

    with pytest.raises(ct.CookieTransferError):
        await transfer.import_cookies(profile.id, "[]", "json", "replace", 0)
    with pytest.raises(StaleWriteError):
        await transfer.import_cookies(profile.id, "[]", "json", "merge", 99)

    profile_manager.browser_sessions._starting[profile.id] = 1
    try:
        with pytest.raises(ProfileLocked):
            await transfer.import_cookies(profile.id, "[]", "json", "merge", 0)
    finally:
        profile_manager.browser_sessions._starting.clear()

    await profile_manager.storage.acquire_lease(profile.id, "remote", 300)
    with pytest.raises(ProfileLocked):
        await transfer.export_cookies(profile.id, "json", 0)
    assert not (Path(profile.storage_path) / "cookies.json").exists()
