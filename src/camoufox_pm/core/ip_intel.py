"""What third parties say about an exit address: location, network type and risk.

Every lookup is made *through the node being described*, so each service sees
the exit asking about itself and never learns which computer is behind it.
Lookups are user-triggered for one node at a time and cached per address;
batch checks never call these services (IPPure's terms forbid unauthorised
bulk access, and the others have rate limits). Only documented interfaces are
used:

* ipinfo.io ``/json`` — location, ASN/organisation; privacy flags with a paid token;
* IPPure ``my.ippure.com/v1/info`` — fraud score, residential, broadcast (non-native);
* Scamalytics API — fraud score and proxy flags, only with the user's own API URL and key.

Sites without a public API (ipip.la, the Scamalytics web page) are offered as
pages to open in the bound profile's own browser instead.

The composite score is a heuristic summary of these inputs, shown with them.
It is not a verdict from any provider and is no promise about how a site will
treat the address.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx

MAX_BODY = 64 * 1024
IPINFO_URL = "https://ipinfo.io/json"
IPPURE_URL = "https://my.ippure.com/v1/info"
_MIN_INTERVAL = 1.5
_pacing: dict[str, float] = {}
_locks: dict[str, asyncio.Lock] = {}

CHECK_SITES = {
    "ippure": "https://ippure.com/",
    "ipip": "https://ipip.la/",
    "ipinfo": "https://ipinfo.io/",
    "scamalytics": "https://scamalytics.com/ip/{ip}",
}


class IntelError(RuntimeError):
    pass


async def _paced(provider: str):
    lock = _locks.setdefault(provider, asyncio.Lock())
    await lock.acquire()
    wait = _pacing.get(provider, 0) + _MIN_INTERVAL - time.monotonic()
    if wait > 0:
        await asyncio.sleep(wait)
    return lock


async def _get_json(client: httpx.AsyncClient, url: str, headers: dict[str, str] | None = None):
    async with client.stream(
        "GET",
        url,
        headers={"Accept": "application/json", "Accept-Encoding": "identity", **(headers or {})},
    ) as response:
        if response.status_code == 429:
            raise IntelError("rate_limited")
        if response.status_code in (401, 403):
            raise IntelError("unauthorized")
        if response.status_code != 200:
            raise IntelError(f"http_{response.status_code}")
        body = bytearray()
        async for chunk in response.aiter_raw():
            body.extend(chunk)
            if len(body) > MAX_BODY:
                raise IntelError("response_too_large")
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise IntelError("invalid_response") from None
    if not isinstance(data, dict):
        raise IntelError("invalid_response")
    return data


def _ip(value: Any) -> str | None:
    try:
        return str(ipaddress.ip_address(str(value).strip()))
    except ValueError:
        return None


def _float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) <= 180 else None


def _score(value: Any) -> int | None:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return None
    return number if 0 <= number <= 100 else None


def _text(value: Any, limit: int = 120) -> str | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    text = " ".join(str(value).split())[:limit]
    return text or None


def parse_ipinfo(data: dict[str, Any]) -> dict[str, Any]:
    latitude = longitude = None
    if isinstance(data.get("loc"), str) and "," in data["loc"]:
        lat, _, lon = data["loc"].partition(",")
        latitude, longitude = _float(lat), _float(lon)
    org = _text(data.get("org"), 200)
    asn = None
    if org and (match := re.match(r"AS(\d+)\s*(.*)", org)):
        asn, org = int(match.group(1)), match.group(2) or None
    privacy = data.get("privacy") if isinstance(data.get("privacy"), dict) else None
    return {
        "ip": _ip(data.get("ip")),
        "hostname": _text(data.get("hostname"), 253),
        "city": _text(data.get("city")),
        "region": _text(data.get("region")),
        "country_iso": (_text(data.get("country"), 2) or "").upper() or None,
        "latitude": latitude,
        "longitude": longitude,
        "timezone": _text(data.get("timezone"), 64),
        "postal": _text(data.get("postal"), 16),
        "asn": asn,
        "as_org": org,
        "anycast": data.get("anycast") is True,
        "privacy": {
            key: privacy.get(key) is True for key in ("vpn", "proxy", "tor", "relay", "hosting")
        }
        if privacy
        else None,
    }


def parse_ippure(data: dict[str, Any]) -> dict[str, Any]:
    def flag(key: str) -> bool | None:
        value = data.get(key)
        return value if isinstance(value, bool) else None

    asn = data.get("asn")
    return {
        "ip": _ip(data.get("ip")),
        "asn": asn if isinstance(asn, int) else None,
        "as_org": _text(data.get("asOrganization"), 200),
        "country": _text(data.get("country")),
        "country_iso": (_text(data.get("countryCode"), 2) or "").upper() or None,
        "region": _text(data.get("region")),
        "city": _text(data.get("city")),
        "timezone": _text(data.get("timezone"), 64),
        "latitude": _float(data.get("latitude")),
        "longitude": _float(data.get("longitude")),
        "fraud_score": _score(data.get("fraudScore")),
        "is_residential": flag("isResidential"),
        "is_broadcast": flag("isBroadcast"),
    }


def parse_scamalytics(data: dict[str, Any]) -> dict[str, Any]:
    nested = data.get("scamalytics")
    block: dict[str, Any] = nested if isinstance(nested, dict) else data
    status = str(block.get("status", "ok")).lower()
    if status not in {"ok", "success"}:
        raise IntelError("provider_error")
    score = _score(block.get("scamalytics_score", block.get("score")))
    flags = block.get("scamalytics_proxy")
    proxy: dict[str, Any] = flags if isinstance(flags, dict) else {}
    return {
        "ip": _ip(block.get("ip")),
        "fraud_score": score,
        "risk": _text(block.get("scamalytics_risk", block.get("risk")), 32),
        "is_datacenter": proxy.get("is_datacenter")
        if isinstance(proxy.get("is_datacenter"), bool)
        else None,
        "is_vpn": proxy.get("is_vpn") if isinstance(proxy.get("is_vpn"), bool) else None,
        "isp": _text(block.get("scamalytics_isp", block.get("ISP Name"))),
    }


GRADES = ((80, "excellent"), (60, "good"), (40, "fair"), (20, "poor"), (0, "bad"))


def combine(sources: dict[str, dict[str, Any]], exit_ip: str) -> dict[str, Any]:
    """A transparent summary. Every number it uses is also returned on its own."""
    ok = {name: value for name, value in sources.items() if value.get("status") == "ok"}
    ipinfo, ippure, scam = ok.get("ipinfo", {}), ok.get("ippure", {}), ok.get("scamalytics", {})
    scores = [
        {"source": name, "score": value["fraud_score"]}
        for name, value in (("ippure", ippure), ("scamalytics", scam))
        if value.get("fraud_score") is not None
    ]
    privacy = ipinfo.get("privacy") or {}
    residential = ippure.get("is_residential")
    datacenter = (
        True
        if privacy.get("hosting") or scam.get("is_datacenter") or residential is False
        else (False if residential is True else None)
    )
    broadcast = ippure.get("is_broadcast")
    flagged = sorted(
        {key for key in ("vpn", "proxy", "tor", "relay") if privacy.get(key)}
        | ({"vpn"} if scam.get("is_vpn") else set())
    )
    signals: list[dict[str, str]] = []
    if datacenter is True:
        signals.append({"level": "warning", "code": "datacenter"})
    elif datacenter is False:
        signals.append({"level": "info", "code": "residential"})
    if broadcast is True:
        signals.append({"level": "warning", "code": "broadcast"})
    elif broadcast is False:
        signals.append({"level": "info", "code": "native"})
    for flag in flagged:
        signals.append({"level": "warning", "code": flag})
    observed = {value.get("ip") for value in ok.values() if value.get("ip")}
    if observed and observed != {exit_ip}:
        signals.append({"level": "warning", "code": "ip_mismatch"})

    risk = max((item["score"] for item in scores), default=None)
    penalty = (10 if datacenter else 0) + (10 if broadcast else 0) + (15 if flagged else 0)
    if risk is None and datacenter is None and broadcast is None and not flagged:
        purity = None
    else:
        purity = max(0, min(100, 100 - (risk or 0) - penalty))
    if risk is not None and risk >= 75:
        signals.append({"level": "danger", "code": "high_fraud"})
    grade = None if purity is None else next(name for floor, name in GRADES if purity >= floor)

    def first(key: str) -> Any:
        for value in (ipinfo, ippure):
            if value.get(key) not in (None, ""):
                return value[key]
        return None

    return {
        "risk_score": risk,
        "purity_score": purity,
        "grade": grade,
        "score_sources": scores,
        "ip_type": "datacenter" if datacenter else ("residential" if datacenter is False else None),
        "native": None if broadcast is None else not broadcast,
        "flags": flagged,
        "signals": signals,
        "location": {
            "city": first("city"),
            "region": first("region"),
            "country_iso": first("country_iso"),
            "timezone": first("timezone"),
            "latitude": first("latitude"),
            "longitude": first("longitude"),
        },
        "asn": first("asn"),
        "as_org": first("as_org"),
        "hostname": ipinfo.get("hostname"),
    }


async def _provider(name: str, call) -> dict[str, Any]:
    lock = await _paced(name)
    try:
        return {"status": "ok", **(await call())}
    except IntelError as exc:
        return {"status": "error", "error": str(exc)}
    except (httpx.TimeoutException, asyncio.TimeoutError):
        return {"status": "error", "error": "timeout"}
    except httpx.HTTPError as exc:
        # Never the message: it can contain the request URL, and with it a key.
        return {"status": "error", "error": type(exc).__name__}
    finally:
        _pacing[name] = time.monotonic()
        lock.release()


async def gather_intel(
    proxy_url: str,
    exit_ip: str,
    settings: dict[str, Any],
    secrets: dict[str, str | None],
    *,
    timeout: float = 10.0,
    urls: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Ask every enabled provider about ``exit_ip``, through ``proxy_url``."""
    urls = {"ipinfo": IPINFO_URL, "ippure": IPPURE_URL, **(urls or {})}
    sources: dict[str, dict[str, Any]] = {}
    async with httpx.AsyncClient(
        proxy=proxy_url, trust_env=False, timeout=timeout, follow_redirects=False
    ) as client:
        calls = {}
        if settings.get("intel_ipinfo_enabled", True):
            token = secrets.get("intel_ipinfo_token")
            headers = {"Authorization": f"Bearer {token}"} if token else None

            async def ipinfo():
                return parse_ipinfo(await _get_json(client, urls["ipinfo"], headers))

            calls["ipinfo"] = ipinfo
        else:
            sources["ipinfo"] = {"status": "disabled"}
        if settings.get("intel_ippure_enabled", True):

            async def ippure():
                return parse_ippure(await _get_json(client, urls["ippure"]))

            calls["ippure"] = ippure
        else:
            sources["ippure"] = {"status": "disabled"}
        base, key = secrets.get("intel_scamalytics_url"), secrets.get("intel_scamalytics_key")
        if settings.get("intel_scamalytics_enabled") and base and key:
            separator = "&" if "?" in base else "?"

            async def scamalytics():
                url = f"{base}{separator}key={quote(key, safe='')}&ip={quote(exit_ip, safe='')}"
                return parse_scamalytics(await _get_json(client, url))

            calls["scamalytics"] = scamalytics
        else:
            sources["scamalytics"] = {
                "status": "not_configured" if not (base and key) else "disabled"
            }
        results = await asyncio.gather(*(_provider(name, call) for name, call in calls.items()))
        sources.update(dict(zip(calls, results, strict=True)))
    return {
        "ip": exit_ip,
        "sources": sources,
        "composite": combine(sources, exit_ip),
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


def check_site_url(site: str, exit_ip: str | None) -> str:
    if site not in CHECK_SITES:
        raise ValueError("Unknown check site")
    template = CHECK_SITES[site]
    if "{ip}" in template:
        ip = _ip(exit_ip)
        if ip is None:
            raise ValueError("The exit address is not known yet")
        return template.format(ip=ip)
    return template
