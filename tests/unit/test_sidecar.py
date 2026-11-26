"""Unit tests for the headless Prism Browser sidecar and pure MaxMind GeoIP helper."""

from __future__ import annotations

import json

from camoufox_pm import sidecar
from camoufox_pm.core import geoip


def test_sidecar_version_and_self_check_cli(capsys):
    sidecar.main(["--version"])
    out = capsys.readouterr().out.strip()
    assert out.startswith("Prism Browser Sidecar ")

    sidecar.main(["--self-check"])
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["engine"] == "prism-sidecar"
    assert payload["routes"] > 0
    assert payload["chrome_presets"] >= 60
    assert "bundled_mihomo" in payload
    assert "bundled_geoip" in payload


def test_geoip_extract_record_handles_geolite2_and_flat_schemas():
    rec = geoip._extract_record(
        "203.0.113.5",
        {
            "country": {"iso_code": "us"},
            "city": {"names": {"en": "Los Angeles", "zh-CN": "洛杉矶"}},
            "subdivisions": [{"names": {"en": "California"}}],
            "location": {
                "time_zone": "America/Los_Angeles",
                "latitude": 34.05,
                "longitude": -118.24,
            },
        },
    )
    assert rec.country == "US"
    assert rec.city == "Los Angeles"
    assert rec.region == "California"
    assert rec.timezone == "America/Los_Angeles"
    assert rec.latitude == 34.05 and rec.longitude == -118.24
