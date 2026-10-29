"""IP intelligence: provider parsing, the composite score, and routing through the node."""

import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from camoufox_pm.core import ip_intel
from camoufox_pm.core.ip_intel import (
    check_site_url,
    combine,
    gather_intel,
    parse_ipinfo,
    parse_ippure,
    parse_scamalytics,
)

IPINFO = {
    "ip": "203.0.113.9",
    "hostname": "h.example",
    "city": "Tokyo",
    "region": "Tokyo",
    "country": "JP",
    "loc": "35.6895,139.6917",
    "org": "AS2516 KDDI CORPORATION",
    "postal": "100-0001",
    "timezone": "Asia/Tokyo",
}
IPPURE = {
    "ip": "203.0.113.9",
    "asn": 2516,
    "asOrganization": "KDDI",
    "country": "Japan",
    "countryCode": "JP",
    "region": "Tokyo",
    "city": "Tokyo",
    "timezone": "Asia/Tokyo",
    "longitude": "139.6917",
    "latitude": "35.6895",
    "fraudScore": 12,
    "isResidential": True,
    "isBroadcast": False,
}


def ok(**values):
    return {"status": "ok", **values}


def test_ipinfo_and_ippure_are_parsed_to_one_vocabulary():
    info = parse_ipinfo(IPINFO)
    assert (info["asn"], info["as_org"]) == (2516, "KDDI CORPORATION")
    assert (info["latitude"], info["longitude"]) == (35.6895, 139.6917)
    assert info["country_iso"] == "JP" and info["privacy"] is None
    pure = parse_ippure(IPPURE)
    assert pure["fraud_score"] == 12 and pure["is_residential"] is True
    assert pure["is_broadcast"] is False and pure["latitude"] == 35.6895


def test_hostile_values_are_reduced_not_trusted():
    weird = parse_ippure(
        {
            "ip": "not-an-ip",
            "fraudScore": 900,
            "latitude": "1e9",
            "isResidential": "yes",
            "city": {"x": 1},
        }
    )
    assert weird["ip"] is None and weird["fraud_score"] is None
    assert weird["latitude"] is None and weird["is_residential"] is None and weird["city"] is None


def test_scamalytics_v3_and_legacy_shapes():
    v3 = parse_scamalytics(
        {
            "scamalytics": {
                "status": "ok",
                "ip": "203.0.113.9",
                "scamalytics_score": 55,
                "scamalytics_risk": "medium",
                "scamalytics_proxy": {"is_datacenter": True, "is_vpn": False},
            }
        }
    )
    assert (v3["fraud_score"], v3["risk"], v3["is_datacenter"]) == (55, "medium", True)
    legacy = parse_scamalytics({"score": "7", "risk": "low", "ip": "203.0.113.9"})
    assert legacy["fraud_score"] == 7
    with pytest.raises(ip_intel.IntelError):
        parse_scamalytics({"scamalytics": {"status": "error", "error": "bad key"}})


def test_a_clean_residential_native_ip_scores_high():
    composite = combine(
        {"ipinfo": ok(**parse_ipinfo(IPINFO)), "ippure": ok(**parse_ippure(IPPURE))},
        "203.0.113.9",
    )
    assert composite["risk_score"] == 12 and composite["purity_score"] == 88
    assert composite["grade"] == "excellent"
    assert composite["ip_type"] == "residential" and composite["native"] is True
    assert {s["code"] for s in composite["signals"]} == {"residential", "native"}
    assert composite["location"]["city"] == "Tokyo" and composite["asn"] == 2516


def test_datacenter_broadcast_and_flags_lower_purity_and_the_worst_score_counts():
    sources = {
        "ipinfo": ok(**parse_ipinfo({**IPINFO, "privacy": {"vpn": True, "hosting": True}})),
        "ippure": ok(
            **parse_ippure(
                {**IPPURE, "fraudScore": 30, "isResidential": False, "isBroadcast": True}
            )
        ),
        "scamalytics": ok(
            fraud_score=80, risk="high", is_datacenter=True, is_vpn=False, ip="203.0.113.9"
        ),
    }
    composite = combine(sources, "203.0.113.9")
    assert composite["risk_score"] == 80  # the higher of the two fraud scores
    assert composite["purity_score"] == 0  # 100 - 80 - (10 + 10 + 15), clamped
    assert composite["grade"] == "bad"
    codes = {s["code"] for s in composite["signals"]}
    assert {"datacenter", "broadcast", "vpn", "high_fraud"} <= codes
    assert composite["score_sources"] == [
        {"source": "ippure", "score": 30},
        {"source": "scamalytics", "score": 80},
    ]


def test_nothing_known_means_no_score_rather_than_a_perfect_one():
    composite = combine({"ipinfo": {"status": "error", "error": "timeout"}}, "203.0.113.9")
    assert composite["purity_score"] is None and composite["grade"] is None


def test_a_different_ip_seen_by_a_provider_is_flagged():
    composite = combine(
        {"ippure": ok(**parse_ippure({**IPPURE, "ip": "198.51.100.1"}))}, "203.0.113.9"
    )
    assert "ip_mismatch" in {s["code"] for s in composite["signals"]}


def test_check_sites_are_a_fixed_allowlist():
    assert check_site_url("ipip", None) == "https://ipip.la/"
    assert check_site_url("scamalytics", "203.0.113.9") == "https://scamalytics.com/ip/203.0.113.9"
    with pytest.raises(ValueError):
        check_site_url("scamalytics", "203.0.113.9/../../evil")
    with pytest.raises(ValueError):
        check_site_url("https://evil.example", "203.0.113.9")


@asynccontextmanager
async def recording_proxy(responses):
    """A plain HTTP proxy that answers absolute-URI requests itself, and records them."""
    seen = []

    async def handle(reader, writer):
        head = (await reader.readuntil(b"\r\n\r\n")).decode()
        target = head.split()[1]
        seen.append(target)
        status, body = responses(target)
        payload = json.dumps(body).encode() if not isinstance(body, bytes) else body
        writer.write(
            f"HTTP/1.1 {status}\r\nContent-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode()
            + payload
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}", seen
    finally:
        server.close()
        await server.wait_closed()


URLS = {"ipinfo": "http://ipinfo.test/json", "ippure": "http://ippure.test/v1/info"}


@pytest.fixture(autouse=True)
def no_pacing(monkeypatch):
    monkeypatch.setattr(ip_intel, "_MIN_INTERVAL", 0)


@pytest.mark.asyncio
async def test_every_lookup_goes_through_the_node():
    def responses(target):
        return ("200 OK", IPINFO if "ipinfo" in target else IPPURE)

    async with recording_proxy(responses) as (proxy, seen):
        intel = await gather_intel(proxy, "203.0.113.9", {}, {}, urls=URLS, timeout=5)
    assert sorted(seen) == sorted(URLS.values())
    assert intel["sources"]["ipinfo"]["status"] == "ok"
    assert intel["sources"]["ippure"]["fraud_score"] == 12
    assert intel["sources"]["scamalytics"] == {"status": "not_configured"}
    assert intel["composite"]["grade"] == "excellent"


@pytest.mark.asyncio
async def test_provider_failures_are_contained_and_keys_never_echoed():
    key = "SCAMALYTICS-SECRET-KEY"

    def responses(target):
        if "ipinfo" in target:
            return ("429 Too Many Requests", {})
        if "ippure" in target:
            return ("200 OK", b"<html>not json</html>")
        return ("200 OK", {"scamalytics": {"status": "error", "error": f"bad {key}"}})

    async with recording_proxy(responses) as (proxy, seen):
        intel = await gather_intel(
            proxy,
            "203.0.113.9",
            {"intel_scamalytics_enabled": True},
            {"intel_scamalytics_url": "http://scam.test/v3/user/", "intel_scamalytics_key": key},
            urls=URLS,
            timeout=5,
        )
    sources = intel["sources"]
    assert sources["ipinfo"] == {"status": "error", "error": "rate_limited"}
    assert sources["ippure"] == {"status": "error", "error": "invalid_response"}
    assert sources["scamalytics"] == {"status": "error", "error": "provider_error"}
    assert key not in json.dumps(intel)
    assert any("ip=203.0.113.9" in target for target in seen)


@pytest.mark.asyncio
async def test_disabled_providers_are_not_contacted():
    async with recording_proxy(lambda target: ("200 OK", IPPURE)) as (proxy, seen):
        intel = await gather_intel(
            proxy,
            "203.0.113.9",
            {"intel_ipinfo_enabled": False, "intel_ippure_enabled": True},
            {},
            urls=URLS,
        )
    assert intel["sources"]["ipinfo"] == {"status": "disabled"}
    assert seen == [URLS["ippure"]]


@pytest.mark.asyncio
async def test_oversized_answers_are_refused(monkeypatch):
    monkeypatch.setattr(ip_intel, "MAX_BODY", 100)
    async with recording_proxy(lambda target: ("200 OK", {"pad": "x" * 500})) as (proxy, _):
        intel = await gather_intel(
            proxy, "203.0.113.9", {"intel_ippure_enabled": False}, {}, urls=URLS
        )
    assert intel["sources"]["ipinfo"] == {"status": "error", "error": "response_too_large"}


def test_a_provider_city_only_names_coordinates_it_agrees_with():
    from camoufox_pm.core.network_probe import _apply_intel_location

    milwaukee = {"precision": "coordinates", "latitude": 43.319, "longitude": -88.375}
    near = {
        "composite": {
            "location": {
                "city": "Milwaukee",
                "region": "Wisconsin",
                "latitude": 43.04,
                "longitude": -87.91,
            }
        }
    }
    far = {
        "composite": {
            "location": {
                "city": "Irvine",
                "region": "California",
                "latitude": 33.68,
                "longitude": -117.79,
            }
        }
    }
    agreed = dict(milwaukee)
    _apply_intel_location(agreed, near)
    assert (agreed["city"], agreed["region"]) == ("Milwaukee", "Wisconsin")
    disputed = dict(milwaukee)
    _apply_intel_location(disputed, far)
    assert disputed["city"] is None and disputed["latitude"] == 43.319  # never relabelled
    unplaced = {"precision": "unknown"}
    _apply_intel_location(unplaced, far)
    assert (unplaced["precision"], unplaced["location_source"], unplaced["city"]) == (
        "city",
        "intel",
        "Irvine",
    )
