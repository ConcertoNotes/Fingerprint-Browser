"""A new profile's browser speaks English, whatever language the manager uses.

The manager's own interface defaults to Simplified Chinese; that choice must
never leak into profiles. Camoufox selects the browser's UI locale from the
profile's fingerprint languages, so browser-generated text — here a form
validation message, which Firefox writes in its UI language — is English for a
default profile.
"""

import pytest

from tests.browser.support import GEOIP_ADDRESS

pytestmark = [pytest.mark.browser, pytest.mark.asyncio]


async def test_a_default_profile_browser_is_english(profile_manager):
    profile = await profile_manager.create_profile("Default language")
    assert profile.browser_settings.languages[:1] == ["en-US"]
    await profile_manager.launch_browser(profile.id, headless=True, geoip=GEOIP_ADDRESS)
    try:
        context = profile_manager.browser_sessions.active_sessions[profile.id].context
        page = await context.new_page()
        await page.set_content("<!doctype html><meta charset=utf-8><form><input required></form>")
        observed = await page.evaluate(
            """() => ({
                language: navigator.language,
                validation: document.querySelector('input').validationMessage,
                date: new Date(Date.UTC(2026, 0, 15)).toLocaleDateString(undefined, {month: 'long', timeZone: 'UTC'}),
            })"""
        )
    finally:
        await profile_manager.close_browser(profile.id)
    assert observed["language"] == "en-US"
    assert observed["validation"] == "Please fill out this field."
    assert observed["date"] == "January"
