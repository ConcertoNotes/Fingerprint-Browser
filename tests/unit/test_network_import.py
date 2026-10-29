"""Subscription fetching and parsing: formats, limits, SSRF and secret handling."""

import asyncio
import gzip
import ssl
from contextlib import asynccontextmanager

import httpcore
import pytest
import yaml

from camoufox_pm.core import network_subscription as ns
from camoufox_pm.core.network_subscription import (
    GuardedBackend,
    SubscriptionError,
    address_is_public,
    fetch_subscription,
    node_key,
    parse_subscription,
    parse_usage,
    validate_subscription_url,
)
from tests.support.proxy_protocols import certificates

SECRET = "node-secret-p@ss:word"

VALID = f"""
mixed-port: 7890
allow-lan: true
external-controller: 0.0.0.0:9090
tun: {{enable: true}}
dns: {{enhanced-mode: fake-ip}}
rules: ["MATCH,DIRECT"]
proxies:
  - {{name: "🇯🇵 Tokyo 01", type: ss, server: jp.example.com, port: 443, cipher: aes-128-gcm, password: "{SECRET}"}}
  - name: US vmess
    type: vmess
    server: us.example.com
    port: "8443"
    uuid: 2b8c1b5e-8f1e-4c1b-9b1a-2b8c1b5e8f1e
    alterId: 0
    cipher: auto
    tls: true
    skip-cert-verify: true
    dialer-proxy: DIRECT
    interface-name: en0
    ws-opts: {{path: /ws, headers: {{Host: us.example.com}}}}
  - {{name: tr, type: trojan, server: "2001:db8::1", port: 443, password: "{SECRET}", ca: /etc/ssl/private.pem}}
  - {{name: wg, type: wireguard, server: 203.0.113.9, port: 51820, private-key: "wgkey=", ip: 10.0.0.2}}
  - {{name: bad-proto, type: direct, server: a.example, port: 1}}
  - {{name: no-port, type: ss, server: a.example, cipher: x, password: y}}
proxy-groups:
  - {{name: PROXY, type: select, proxies: [DIRECT]}}
"""


def test_valid_yaml_yields_safe_outbounds_only():
    parsed = parse_subscription(VALID.encode())
    names = [node.name for node in parsed.nodes]
    assert names == ["🇯🇵 Tokyo 01", "US vmess", "tr", "wg"]
    vmess = parsed.nodes[1]
    assert vmess.port == 8443 and vmess.insecure
    # Fields that reach outside one outbound never survive parsing.
    assert "dialer-proxy" not in vmess.config and "interface-name" not in vmess.config
    assert vmess.config["ws-opts"]["headers"]["Host"] == "us.example.com"
    trojan = parsed.nodes[2]
    assert trojan.server == "2001:db8::1" and trojan.entry == "[2001:db8::1]:443"
    assert "ca" not in trojan.config  # a local file path, not inline PEM
    assert parsed.nodes[3].config["private-key"] == "wgkey="  # a key, not a path, for WireGuard
    reasons = {warning["index"]: warning["reason"] for warning in parsed.warnings}
    assert reasons == {5: "unsupported protocol", 6: "missing or invalid port"}
    # Warnings never repeat the document's values.
    assert SECRET not in repr(parsed.warnings)


def test_inline_pem_is_kept_and_name_defaults_are_generated():
    text = """
proxies:
  - {type: hysteria2, server: h.example, port: 443, password: pw, ca: "-----BEGIN CERTIFICATE-----\\nMII\\n-----END CERTIFICATE-----"}
"""
    node = parse_subscription(text).nodes[0]
    assert node.name == "hysteria2 h.example:443"
    assert node.config["ca"].startswith("-----BEGIN CERTIFICATE-----")


@pytest.mark.parametrize(
    "content, code",
    [
        (b"", "empty"),
        (b"   \n", "empty"),
        (b"\xff\xfe\x00", "invalid_encoding"),
        (b"proxies: [unclosed", "invalid_yaml"),
        (b"- just\n- a list\n", "invalid_yaml"),
        (b"port: 7890\n", "no_proxies"),
        (b"proxies: []\nproxy-providers: {p: {type: http, url: 'https://x'}}\n", "providers_only"),
        (b"proxies: {a: 1}\n", "invalid_yaml"),
        (b"proxies:\n  - {name: x, type: direct, server: a, port: 1}\n", "no_valid_proxies"),
        (
            b"ss://YWVzLTEyOC1nY206cGFzcw@1.2.3.4:443#a\nvmess://eyJ2IjoiMiJ9\n",
            "uri_list_unsupported",
        ),
    ],
)
def test_unusable_documents_are_rejected_with_a_code(content, code):
    with pytest.raises(SubscriptionError) as error:
        parse_subscription(content)
    assert error.value.code == code


def test_base64_share_link_lists_are_named_not_imported_as_zero_nodes():
    import base64

    encoded = base64.b64encode(b"ss://YWVzOnBhc3M@1.2.3.4:443#node\ntrojan://pw@h:443#t\n")
    with pytest.raises(SubscriptionError) as error:
        parse_subscription(encoded)
    assert error.value.code == "base64_unsupported"


def test_custom_tags_cannot_construct_objects():
    payload = b"proxies: !!python/object/apply:os.system ['echo pwned']\n"
    with pytest.raises(SubscriptionError) as error:
        parse_subscription(payload)
    assert error.value.code == "invalid_yaml"


def test_alias_expansion_is_bounded():
    lines = ["a0: &a0 [x, x, x, x, x, x, x, x, x, x]"]
    for level in range(1, 260):
        lines.append(f"a{level}: &a{level} [*a{level - 1}, *a{level - 1}]")
    lines.append("proxies: [*a259]")
    with pytest.raises(SubscriptionError) as error:
        parse_subscription("\n".join(lines))
    assert error.value.code == "yaml_too_complex"


def test_yaml_errors_never_quote_the_document():
    broken = f'proxies:\n  - {{name: a, password: "{SECRET}", type: ss\n'
    with pytest.raises(SubscriptionError) as error:
        parse_subscription(broken)
    assert SECRET not in str(error.value)


def test_node_count_and_entry_size_are_bounded(monkeypatch):
    monkeypatch.setattr(ns, "MAX_NODES", 3)
    many = "proxies:\n" + "".join(
        f"  - {{name: n{i}, type: socks5, server: s{i}.example, port: 1080}}\n" for i in range(4)
    )
    with pytest.raises(SubscriptionError) as error:
        parse_subscription(many)
    assert error.value.code == "too_many_nodes"
    huge = yaml.safe_dump(
        {
            "proxies": [
                {
                    "name": "x",
                    "type": "socks5",
                    "server": "a.example",
                    "port": 1,
                    "ws-opts": {"headers": {f"H{i}": "v" * 4000 for i in range(20)}},
                }
            ]
        }
    )
    with pytest.raises(SubscriptionError) as error:
        parse_subscription(huge)
    assert error.value.code == "no_valid_proxies"


def test_same_name_from_two_sources_and_repeats_get_distinct_stable_keys():
    text = """
proxies:
  - {name: same, type: socks5, server: a.example, port: 1080}
  - {name: same, type: socks5, server: a.example, port: 1080}
  - {name: same, type: socks5, server: b.example, port: 1080}
"""
    first = parse_subscription(text)
    second = parse_subscription(text)
    keys = [node.key for node in first.nodes]
    assert len(set(keys)) == 3
    assert keys == [node.key for node in second.nodes]
    assert keys[1] == f"{keys[0]}#2"


def test_node_key_ignores_secrets():
    a = parse_subscription(
        f"proxies: [{{name: n, type: trojan, server: t.example, port: 443, password: '{SECRET}'}}]"
    ).nodes[0]
    b = parse_subscription(
        "proxies: [{name: n, type: trojan, server: t.example, port: 443, password: other}]"
    ).nodes[0]
    assert node_key(a) == node_key(b)
    assert SECRET not in node_key(a)


def test_usage_header_parsing():
    assert parse_usage("upload=1; download=2.0; total=30; expire=1893456000") == {
        "upload": 1,
        "download": 2,
        "total": 30,
        "expire": 1893456000,
    }
    assert parse_usage("garbage") is None
    assert parse_usage(None) is None


@pytest.mark.parametrize(
    "url, code",
    [
        ("http://sub.example/a", "https_required"),
        ("ftp://sub.example/a", "https_required"),
        ("https://user:pw@sub.example/a", "invalid_url"),
        ("https://sub.example/a b", "invalid_url"),
        ("https:///nohost", "invalid_url"),
        ("https://sub.example:99999/", "invalid_url"),
    ],
)
def test_urls_are_validated_before_any_network_access(url, code):
    with pytest.raises(SubscriptionError) as error:
        validate_subscription_url(url)
    assert error.value.code == code


@pytest.mark.parametrize(
    "address, public",
    [
        ("8.8.8.8", True),
        ("2001:4860:4860::8888", True),
        ("127.0.0.1", False),
        ("10.1.2.3", False),
        ("172.16.0.1", False),
        ("192.168.1.1", False),
        ("100.64.0.1", False),
        ("169.254.169.254", False),
        ("0.0.0.0", False),
        ("224.0.0.1", False),
        ("::1", False),
        ("fc00::1", False),
        ("fe80::1", False),
        ("::ffff:127.0.0.1", False),
        ("::ffff:10.0.0.1", False),
        ("not-an-ip", False),
    ],
)
def test_address_policy(address, public):
    assert address_is_public(address) is public


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answers",
    [["127.0.0.1"], ["10.0.0.5"], ["169.254.169.254"], ["8.8.8.8", "192.168.0.1"], []],
)
async def test_guarded_backend_refuses_any_private_answer(answers):
    async def resolver(host, port):
        return answers

    backend = GuardedBackend(resolver=resolver)
    with pytest.raises(SubscriptionError) as error:
        await backend.connect_tcp("rebind.example", 443)
    assert error.value.code == "blocked_address"


@pytest.mark.asyncio
async def test_guarded_backend_refuses_private_literals_without_resolving():
    async def resolver(host, port):
        pytest.fail("literal addresses are not resolved")

    with pytest.raises(SubscriptionError):
        await GuardedBackend(resolver=resolver).connect_tcp("127.0.0.1", 443)
    with pytest.raises(SubscriptionError):
        await GuardedBackend(resolver=resolver).connect_tcp("[::1]", 443)


# --- A real local HTTPS server, reached through the guarded backend ----------------


@asynccontextmanager
async def https_server(tmp_path, handler):
    ca, cert, key = certificates(tmp_path, dns_names=("sub.test", "other.test"))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    requests = []

    async def serve(reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            requests.append(head.decode())
            writer.write(handler(head.decode()))
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, ssl.SSLError):
                pass

    server = await asyncio.start_server(serve, "127.0.0.1", 0, ssl=context)
    port = server.sockets[0].getsockname()[1]
    client = ssl.create_default_context(cafile=str(ca))

    async def resolver(host, _port):
        return ["127.0.0.1"] if host in {"sub.test", "other.test"} else []

    # The loopback test server is allowed only for these test names.
    backend = GuardedBackend(resolver=resolver, policy=lambda address: address == "127.0.0.1")
    try:
        yield port, backend, client, requests
    finally:
        server.close()
        await server.wait_closed()


def response(body: bytes, status: str = "200 OK", headers: str = "") -> bytes:
    return (
        f"HTTP/1.1 {status}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n{headers}\r\n"
    ).encode() + body


MINIMAL = b"proxies:\n  - {name: a, type: socks5, server: a.example, port: 1080}\n"


@pytest.mark.asyncio
async def test_fetch_sends_the_clash_user_agent_and_reads_usage(tmp_path):
    async with https_server(
        tmp_path,
        lambda _: response(
            MINIMAL,
            headers="subscription-userinfo: upload=5; download=6; total=100; expire=1893456000\r\n"
            'content-disposition: attachment; filename="My%20Airport.yaml"\r\n',
        ),
    ) as (port, backend, context, requests):
        fetched = await fetch_subscription(
            f"https://sub.test:{port}/link?token=abc", backend=backend, ssl_context=context
        )
    assert "User-Agent: clash.meta/v1.18.0" in requests[0]
    assert fetched.usage == {"upload": 5, "download": 6, "total": 100, "expire": 1893456000}
    assert fetched.suggested_name == "My Airport"
    assert parse_subscription(fetched.content).nodes[0].name == "a"


@pytest.mark.asyncio
async def test_fetch_decompresses_gzip_within_bounds(tmp_path, monkeypatch):
    body = gzip.compress(MINIMAL)
    async with https_server(
        tmp_path, lambda _: response(body, headers="Content-Encoding: gzip\r\n")
    ) as (port, backend, context, _):
        fetched = await fetch_subscription(
            f"https://sub.test:{port}/", backend=backend, ssl_context=context
        )
        assert fetched.content == MINIMAL
        monkeypatch.setattr(ns, "MAX_DECOMPRESSED_BYTES", 1000)
        bomb = gzip.compress(b"a" * 100_000)
    async with https_server(
        tmp_path, lambda _: response(bomb, headers="Content-Encoding: gzip\r\n")
    ) as (port, backend, context, _):
        with pytest.raises(SubscriptionError) as error:
            await fetch_subscription(
                f"https://sub.test:{port}/", backend=backend, ssl_context=context
            )
    assert error.value.code == "too_large"


@pytest.mark.asyncio
async def test_oversized_bodies_are_cut_off(tmp_path, monkeypatch):
    monkeypatch.setattr(ns, "MAX_DOWNLOAD_BYTES", 1024)
    async with https_server(tmp_path, lambda _: response(b"x" * 5000)) as (
        port,
        backend,
        context,
        _,
    ):
        with pytest.raises(SubscriptionError) as error:
            await fetch_subscription(
                f"https://sub.test:{port}/", backend=backend, ssl_context=context
            )
    assert error.value.code == "too_large"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "location, code",
    [
        ("http://sub.test/plain", "https_required"),
        ("https://127.0.0.1/admin", "blocked_address"),
        ("https://169.254.169.254/latest/meta-data", "blocked_address"),
    ],
)
async def test_redirects_are_revalidated_at_every_hop(tmp_path, location, code):
    async with https_server(
        tmp_path, lambda _: response(b"", "302 Found", f"Location: {location}\r\n")
    ) as (port, _backend, context, _):
        # The real policy: loopback itself is refused on the redirect hop. The first
        # hop is allowed by name only, through the test resolver.
        async def resolver(host, _port):
            return ["127.0.0.1"] if host == "sub.test" else []

        backend = GuardedBackend(
            resolver=resolver,
            policy=lambda address, first=[True]: (
                first.pop() if first else address_is_public(address)
            ),
        )
        with pytest.raises(SubscriptionError) as error:
            await fetch_subscription(
                f"https://sub.test:{port}/", backend=backend, ssl_context=context
            )
    assert error.value.code == code


@pytest.mark.asyncio
async def test_redirect_loops_stop(tmp_path):
    async with https_server(
        tmp_path, lambda head: response(b"", "302 Found", "Location: /again\r\n")
    ) as (port, backend, context, requests):
        with pytest.raises(SubscriptionError) as error:
            await fetch_subscription(
                f"https://sub.test:{port}/", backend=backend, ssl_context=context
            )
    assert error.value.code == "too_many_redirects"
    assert len(requests) == ns.MAX_REDIRECTS + 1


@pytest.mark.asyncio
async def test_untrusted_certificates_fail_without_echoing_the_url(tmp_path):
    async with https_server(tmp_path, lambda _: response(MINIMAL)) as (port, backend, _ctx, _):
        with pytest.raises(SubscriptionError) as error:
            await fetch_subscription(
                f"https://sub.test:{port}/token-XYZ",
                backend=backend,
                ssl_context=ssl.create_default_context(),
            )
    assert error.value.code in {"tls_failed", "connection_failed"}
    assert "token-XYZ" not in str(error.value)


@pytest.mark.asyncio
async def test_http_errors_and_timeouts_have_codes(tmp_path):
    async with https_server(tmp_path, lambda _: response(b"no", "403 Forbidden")) as (
        port,
        backend,
        context,
        _,
    ):
        with pytest.raises(SubscriptionError) as error:
            await fetch_subscription(
                f"https://sub.test:{port}/", backend=backend, ssl_context=context
            )
    assert error.value.code == "http_status"

    class Stalling(httpcore.AsyncNetworkBackend):
        async def connect_tcp(self, *args, **kwargs):
            await asyncio.sleep(10)

        async def sleep(self, seconds):
            await asyncio.sleep(seconds)

    with pytest.raises(SubscriptionError) as error:
        await fetch_subscription("https://sub.test/", backend=Stalling(), timeout=0.2)
    assert error.value.code == "timeout"
