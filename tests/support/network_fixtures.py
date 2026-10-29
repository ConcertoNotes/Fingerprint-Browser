"""Controlled upstream "nodes" for network tests: each one has its own exit address.

An :class:`ExitUpstream` is a local SOCKS5 server whose tunnel only reaches its
own origin. The origin answers like the services the manager asks about an exit:

* ``/ip`` — the plain exit address (as api.ipify.org would);
* ``/json`` — an ipinfo.io-shaped record;
* ``/v1/info`` — an IPPure-shaped record.

Pointing endpoints at ``http://<any-name>:<origin port>/…`` therefore proves the
request went through *this* node: no other path can reach that port's answers.
"""

import json
from pathlib import Path

import pytest

from tests.support.proxy_protocols import ProxyFixture, certificates


class ExitUpstream(ProxyFixture):
    def __init__(self, certs, exit_ip: str, *, city: str = "Tokyo", fraud: int = 10):
        super().__init__("socks5", certs)
        self.exit_ip = exit_ip
        self.city = city
        self.fraud = fraud

    async def origin(self, reader, writer):
        headers = await reader.readuntil(b"\r\n\r\n")
        line = headers.split(b"\r\n", 1)[0]
        self.events.append(("origin", line))
        path = line.split()[1].decode()
        if path.startswith("/json"):
            body = json.dumps(
                {
                    "ip": self.exit_ip,
                    "city": self.city,
                    "region": "Tokyo",
                    "country": "JP",
                    "loc": "35.6895,139.6917",
                    "org": "AS2516 KDDI CORPORATION",
                    "timezone": "Asia/Tokyo",
                }
            ).encode()
        elif path.startswith("/v1/info"):
            body = json.dumps(
                {
                    "ip": self.exit_ip,
                    "asn": 2516,
                    "asOrganization": "KDDI",
                    "countryCode": "JP",
                    "city": self.city,
                    "latitude": "35.6895",
                    "longitude": "139.6917",
                    "fraudScore": self.fraud,
                    "isResidential": True,
                    "isBroadcast": False,
                }
            ).encode()
        else:
            body = self.exit_ip.encode()
        writer.write(
            b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Type: application/json\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
        )
        await writer.drain()

    def endpoint(self, path: str = "/ip") -> str:
        return f"http://exit-{self.http_port}.test:{self.http_port}{path}"

    def node(self, name: str) -> dict:
        return {"name": name, "type": "socks5", "server": "127.0.0.1", "port": self.proxy_port}


async def start_upstream(tmp_path: Path, exit_ip: str, **kwargs) -> ExitUpstream:
    tmp_path.mkdir(parents=True, exist_ok=True)
    return await ExitUpstream(certificates(tmp_path), exit_ip, **kwargs).start()


def subscription_yaml(*nodes: dict) -> bytes:
    return json.dumps({"proxies": list(nodes)}).encode()


def require_mihomo() -> Path:
    """The real engine for tests that need one, or a failure that says how to get it."""
    from camoufox_pm.core.mihomo_manager import MihomoUnavailable, discover_binary

    try:
        return discover_binary(None, None).path
    except MihomoUnavailable as exc:
        pytest.fail(
            f"These tests need a real Mihomo executable ({exc}). Install one with\n"
            "    uv run python scripts/prepare_mihomo.py --from /path/to/mihomo\n"
            "or set CPM_MIHOMO_PATH. They are not skipped: the behaviour they prove "
            "(per-profile routing, fail-closed, pooled probing) needs the real engine.",
            pytrace=False,
        )


def mihomo_children() -> list:
    import psutil

    me = psutil.Process()
    found = []
    for child in me.children(recursive=True):
        try:
            if "mihomo" in Path(child.exe()).name.lower():
                found.append(child)
        except psutil.Error:
            continue
    return found
