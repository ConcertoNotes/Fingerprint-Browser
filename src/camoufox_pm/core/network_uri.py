"""Turn share links and plain proxy lines into Clash/Mihomo outbound mappings.

Two kinds of input end up here:

* share links that subscriptions and providers hand out — ``ss://`` (SIP002 and
  the legacy Base64 form), ``vmess://`` (v2rayN JSON), ``vless://``,
  ``trojan://``, ``hysteria2://``/``hy2://``, ``socks5://`` and ``http(s)://``;
* the lines proxy sellers send for a fixed (static) proxy: ``host:port``,
  ``host:port:user:pass``, ``user:pass:host:port`` and ``user:pass@host:port``.

The result is only a candidate mapping. Every one still goes through
:func:`network_subscription.validate_node`, so the same whitelist, field
stripping and limits apply as for Clash YAML.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

SIMPLE_TYPES = ("http", "https", "socks5")
_SCHEMES = {
    "ss": "ss",
    "vmess": "vmess",
    "vless": "vless",
    "trojan": "trojan",
    "hysteria2": "hysteria2",
    "hy2": "hysteria2",
    "socks5": "socks5",
    "socks5h": "socks5",
    "socks": "socks5",
    "http": "http",
    "https": "https",
}


class LinkError(ValueError):
    """A line that is not a usable proxy. The message never repeats the line."""


def _b64decode(text: str) -> bytes:
    compact = "".join(text.split())
    try:
        return base64.b64decode(compact + "=" * (-len(compact) % 4), altchars=b"-_", validate=False)
    except (binascii.Error, ValueError):
        raise LinkError("invalid Base64") from None


def _port(value: Any) -> int:
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        raise LinkError("missing or invalid port") from None
    if not 1 <= port <= 65535:
        raise LinkError("missing or invalid port")
    return port


def _query(parts) -> dict[str, str]:
    return {
        key: values[-1] for key, values in parse_qs(parts.query, keep_blank_values=True).items()
    }


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes"}


def _name(parts, fallback: str) -> str:
    return unquote(parts.fragment).strip() or fallback


def _transport(node: dict[str, Any], network: str, host: str, path: str, service: str) -> None:
    network = (network or "tcp").lower()
    if network in {"ws", "websocket"}:
        node["network"] = "ws"
        opts: dict[str, Any] = {"path": path or "/"}
        if host:
            opts["headers"] = {"Host": host}
        node["ws-opts"] = opts
    elif network == "grpc":
        node["network"] = "grpc"
        node["grpc-opts"] = {"grpc-service-name": service or path}
    elif network in {"h2", "http"}:
        node["network"] = "h2"
        node["h2-opts"] = {"path": path or "/", **({"host": [host]} if host else {})}
    elif network not in {"tcp", ""}:
        raise LinkError("unsupported transport")


def _host_port(parts) -> tuple[str, int]:
    try:
        host, port = parts.hostname, parts.port
    except ValueError:
        raise LinkError("missing or invalid port") from None
    if not host:
        raise LinkError("missing or invalid server")
    if port is None:
        raise LinkError("missing or invalid port")
    return host, port


def _ss(link: str) -> dict[str, Any]:
    body = link[5:]
    body, _, fragment = body.partition("#")
    name = unquote(fragment).strip()
    if "@" not in body:  # legacy: ss://BASE64(method:password@host:port)
        decoded = _b64decode(body.split("?", 1)[0]).decode("utf-8", "replace")
        userinfo, _, endpoint = decoded.rpartition("@")
        method, _, password = userinfo.partition(":")
        parts = urlsplit("//" + endpoint)
        host, port = _host_port(parts)
        return {
            "name": name or f"ss {host}:{port}",
            "type": "ss",
            "server": host,
            "port": port,
            "cipher": method,
            "password": password,
        }
    parts = urlsplit("ss://" + body)
    host, port = _host_port(parts)
    userinfo = unquote(body.rsplit("@", 1)[0])
    if ":" not in userinfo:
        userinfo = _b64decode(userinfo).decode("utf-8", "replace")
    method, _, password = userinfo.partition(":")
    node: dict[str, Any] = {
        "name": name or f"ss {host}:{port}",
        "type": "ss",
        "server": host,
        "port": port,
        "cipher": method,
        "password": password,
    }
    plugin = _query(parts).get("plugin")
    if plugin:
        kind, *options = unquote(plugin).split(";")
        opts: dict[str, str] = {}
        for option in options:
            key, separator, value = option.partition("=")
            opts[key] = value if separator else "true"
        if kind in {"obfs-local", "simple-obfs"}:
            node["plugin"] = "obfs"
            node["plugin-opts"] = {
                "mode": opts.get("obfs", "http"),
                "host": opts.get("obfs-host", ""),
            }
        elif kind == "v2ray-plugin":
            node["plugin"] = "v2ray-plugin"
            node["plugin-opts"] = {
                "mode": opts.get("mode", "websocket"),
                "tls": "tls" in opts,
                "host": opts.get("host", ""),
                "path": opts.get("path", "/"),
            }
        else:
            raise LinkError("unsupported Shadowsocks plugin")
    return node


def _vmess(link: str) -> dict[str, Any]:
    try:
        data = json.loads(_b64decode(link[8:]).decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise LinkError("invalid vmess link") from None
    if not isinstance(data, dict):
        raise LinkError("invalid vmess link")
    server = str(data.get("add", "")).strip()
    port = _port(data.get("port"))
    node: dict[str, Any] = {
        "name": str(data.get("ps") or f"vmess {server}:{port}"),
        "type": "vmess",
        "server": server,
        "port": port,
        "uuid": str(data.get("id", "")),
        "alterId": int(str(data.get("aid") or 0)) if str(data.get("aid") or "0").isdigit() else 0,
        "cipher": str(data.get("scy") or "auto"),
    }
    if str(data.get("tls", "")).lower() == "tls":
        node["tls"] = True
        if data.get("sni"):
            node["servername"] = str(data["sni"])
    _transport(
        node,
        str(data.get("net") or "tcp"),
        str(data.get("host") or ""),
        str(data.get("path") or ""),
        str(data.get("path") or ""),
    )
    return node


def _vless_or_trojan(link: str, kind: str) -> dict[str, Any]:
    parts = urlsplit(link)
    host, port = _host_port(parts)
    query = _query(parts)
    secret = unquote(parts.username or "")
    node: dict[str, Any] = {
        "name": _name(parts, f"{kind} {host}:{port}"),
        "type": kind,
        "server": host,
        "port": port,
        "udp": True,
    }
    if kind == "vless":
        node["uuid"] = secret
        if query.get("flow"):
            node["flow"] = query["flow"]
    else:
        node["password"] = secret
    security = query.get("security", "tls" if kind == "trojan" else "none").lower()
    sni = query.get("sni") or query.get("peer")
    if security in {"tls", "reality"}:
        if kind == "vless":
            node["tls"] = True
        if sni:
            node["servername" if kind == "vless" else "sni"] = sni
        if query.get("fp"):
            node["client-fingerprint"] = query["fp"]
        if security == "reality":
            node["reality-opts"] = {
                "public-key": query.get("pbk", ""),
                "short-id": query.get("sid", ""),
            }
    if _truthy(query.get("allowInsecure")) or _truthy(query.get("insecure")):
        node["skip-cert-verify"] = True
    _transport(
        node,
        query.get("type", "tcp"),
        query.get("host", ""),
        unquote(query.get("path", "")),
        query.get("serviceName", ""),
    )
    return node


def _hysteria2(link: str) -> dict[str, Any]:
    parts = urlsplit(re.sub(r"^hy2://", "hysteria2://", link, flags=re.I))
    host, port = _host_port(parts)
    query = _query(parts)
    password = unquote(parts.username or "")
    if parts.password:
        password = f"{password}:{unquote(parts.password)}"
    node: dict[str, Any] = {
        "name": _name(parts, f"hysteria2 {host}:{port}"),
        "type": "hysteria2",
        "server": host,
        "port": port,
        "password": password,
    }
    if query.get("sni"):
        node["sni"] = query["sni"]
    if _truthy(query.get("insecure")):
        node["skip-cert-verify"] = True
    if query.get("obfs"):
        node["obfs"] = query["obfs"]
        node["obfs-password"] = query.get("obfs-password", "")
    if query.get("mport"):
        node["ports"] = query["mport"]
    return node


def _simple_url(link: str, kind: str) -> dict[str, Any]:
    parts = urlsplit(link)
    host, port = _host_port(parts)
    node: dict[str, Any] = {
        "name": _name(parts, f"{kind} {host}:{port}"),
        "type": "http" if kind == "https" else kind,
        "server": host,
        "port": port,
    }
    if kind == "https":
        node["tls"] = True
    if parts.username:
        node["username"] = unquote(parts.username)
    if parts.password:
        node["password"] = unquote(parts.password)
    return node


_HOST = r"(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]+)"


def parse_plain(line: str, default_type: str = "http") -> dict[str, Any]:
    """``host:port[:user:pass]``, ``user:pass:host:port`` or ``user:pass@host:port``."""
    if default_type not in SIMPLE_TYPES:
        raise LinkError("unsupported protocol")
    line = line.strip()
    if "@" in line:
        credentials, _, endpoint = line.rpartition("@")
        user, _, password = credentials.partition(":")
        match = re.fullmatch(_HOST + r":(\d+)", endpoint)
        if not match or not user:
            raise LinkError("unrecognised proxy line")
        host, port = match.group(1), match.group(2)
    else:
        match = re.fullmatch(_HOST + r":(\d+)(?::([^:]*):(.*))?", line)
        reverse = re.fullmatch(r"([^:]+):([^:]*):" + _HOST + r":(\d+)", line)
        if match:
            host, port, user, password = (
                match.group(1),
                match.group(2),
                match.group(3),
                match.group(4),
            )
        elif reverse:
            user, password, host, port = reverse.groups()
        else:
            raise LinkError("unrecognised proxy line")
    host = host.strip("[]")
    port_number = _port(port)
    node: dict[str, Any] = {
        "name": f"{host}:{port_number}",
        "type": "http" if default_type == "https" else default_type,
        "server": host,
        "port": port_number,
    }
    if default_type == "https":
        node["tls"] = True
    if user:
        node["username"] = user
    if password:
        node["password"] = password
    return node


def parse_link(line: str, default_type: str = "http") -> dict[str, Any]:
    """One share link or plain proxy line as a candidate Clash mapping."""
    line = line.strip()
    scheme = line.split("://", 1)[0].lower() if "://" in line else None
    if scheme is None:
        return parse_plain(line, default_type)
    kind = _SCHEMES.get(scheme)
    if kind is None:
        raise LinkError("unsupported protocol")
    if kind == "ss":
        return _ss(line)
    if kind == "vmess":
        return _vmess(line)
    if kind in {"vless", "trojan"}:
        return _vless_or_trojan(line, kind)
    if kind == "hysteria2":
        return _hysteria2(line)
    return _simple_url(line, kind)


def decode_link_list(text: str) -> list[str] | None:
    """The lines of a share-link list, decoding a Base64 subscription first."""
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    if lines and all("://" in line for line in lines[:20]):
        return lines
    compact = "".join(text.split())
    if len(compact) >= 16 and re.fullmatch(r"[A-Za-z0-9+/=_-]+", compact):
        try:
            decoded = _b64decode(compact).decode("utf-8")
        except (LinkError, UnicodeDecodeError):
            return None
        decoded_lines = [line.strip() for line in decoded.splitlines() if line.strip()]
        if decoded_lines and all("://" in line for line in decoded_lines[:20]):
            return decoded_lines
    return None


def parse_manual_text(text: str, default_type: str = "http") -> list[tuple[int, Any, str | None]]:
    """Pasted nodes, one per line, or a Clash/Mihomo snippet with ``proxies:``.

    Returns ``(line, mapping, None)`` or ``(line, None, reason)`` per entry so a
    caller can report every skipped line without repeating its contents.
    """
    if re.search(r"^\s*proxies\s*:", text, re.M):
        from .network_subscription import SubscriptionError, parse_subscription

        try:
            parsed = parse_subscription(text)
        except SubscriptionError as exc:
            return [(1, None, str(exc))]
        entries: list[tuple[int, Any, str | None]] = [
            (index, {**node.config, "name": node.name, "type": node.protocol}, None)
            for index, node in enumerate(parsed.nodes, start=1)
        ]
        entries += [(warning["index"], None, warning["reason"]) for warning in parsed.warnings]
        return entries
    links = decode_link_list(text) or [line.strip() for line in text.splitlines()]
    entries = []
    for index, line in enumerate(links, start=1):
        if not line or line.startswith("#"):
            continue
        try:
            entries.append((index, parse_link(line, default_type), None))
        except LinkError as exc:
            entries.append((index, None, str(exc)))
    return entries
