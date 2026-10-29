"""Share links, fixed-proxy lines, informational entries and manual node groups."""

import base64
import json

import pytest
from cryptography.fernet import Fernet

from camoufox_pm.config import get_settings
from camoufox_pm.core.network_catalog import NetworkCatalog, NetworkNotFound, NodeInUse
from camoufox_pm.core.network_subscription import (
    SubscriptionError,
    is_informational,
    parse_subscription,
    validate_node,
)
from camoufox_pm.core.network_uri import LinkError, parse_link, parse_manual_text, parse_plain

SECRET = "manual-node-secret"


def valid(line, default="http"):
    return validate_node(parse_link(line, default))


@pytest.mark.parametrize(
    "line, expected",
    [
        ("1.2.3.4:8080", {"type": "http", "server": "1.2.3.4", "port": 8080}),
        ("1.2.3.4:8080:alice:s3:cret", {"username": "alice", "password": "s3:cret"}),
        (
            "alice:pw:proxy.example:3128",
            {"server": "proxy.example", "port": 3128, "username": "alice"},
        ),
        ("alice:p@ss@1.2.3.4:1080", {"username": "alice", "password": "p@ss", "port": 1080}),
        ("[2001:db8::1]:1080", {"server": "2001:db8::1", "port": 1080}),
    ],
)
def test_plain_proxy_lines(line, expected):
    node = parse_plain(line, "socks5" if "1080" in line else "http")
    assert expected.items() <= node.items()


@pytest.mark.parametrize("line", ["nonsense", "1.2.3.4", "1.2.3.4:99999", "a:b:c", ":@host:1"])
def test_bad_plain_lines_are_refused_without_echo(line):
    with pytest.raises(LinkError) as error:
        parse_plain(line)
    assert line not in str(error.value)


def test_https_proxies_are_http_with_tls():
    node = valid("https://u:p@secure.example:443")
    assert (node.protocol, node.config["tls"], node.config["username"]) == ("http", True, "u")


def test_shadowsocks_sip002_legacy_and_plugins():
    userinfo = base64.urlsafe_b64encode(b"aes-256-gcm:pw").decode().rstrip("=")
    sip002 = valid(f"ss://{userinfo}@ss.example:8388#Tokyo%2001")
    assert (sip002.name, sip002.config["cipher"], sip002.config["password"]) == (
        "Tokyo 01",
        "aes-256-gcm",
        "pw",
    )
    plain = valid("ss://2022-blake3-aes-128-gcm:a%2Bb%3D@ss.example:443#n")
    assert plain.config["password"] == "a+b="
    legacy = valid(
        "ss://" + base64.b64encode(b"chacha20-ietf-poly1305:pw@1.2.3.4:443").decode() + "#old"
    )
    assert (legacy.server, legacy.port, legacy.name) == ("1.2.3.4", 443, "old")
    obfs = valid(
        f"ss://{userinfo}@ss.example:8388/?plugin=obfs-local%3Bobfs%3Dtls%3Bobfs-host%3Dcdn.example#o"
    )
    assert obfs.config["plugin"] == "obfs" and obfs.config["plugin-opts"] == {
        "mode": "tls",
        "host": "cdn.example",
    }
    with pytest.raises(LinkError):
        parse_link(f"ss://{userinfo}@ss.example:8388/?plugin=unknown-plugin#x")


def test_vmess_json_link():
    data = {
        "v": "2",
        "ps": "US vmess",
        "add": "v.example",
        "port": "443",
        "id": "b831381d-6324-4d53-ad4f-8cda48b30811",
        "aid": "0",
        "scy": "auto",
        "net": "ws",
        "host": "cdn.example",
        "path": "/ray",
        "tls": "tls",
        "sni": "v.example",
    }
    node = valid("vmess://" + base64.b64encode(json.dumps(data).encode()).decode())
    assert (node.name, node.port, node.config["network"]) == ("US vmess", 443, "ws")
    assert node.config["ws-opts"] == {"path": "/ray", "headers": {"Host": "cdn.example"}}
    assert node.config["tls"] is True and node.config["servername"] == "v.example"


def test_vless_reality_and_trojan_grpc():
    vless = valid(
        "vless://b831381d-6324-4d53-ad4f-8cda48b30811@r.example:443?encryption=none&security=reality"
        "&sni=www.example.com&fp=chrome&pbk=PUBKEY&sid=ab12&type=tcp&flow=xtls-rprx-vision#Reality"
    )
    assert vless.config["reality-opts"] == {"public-key": "PUBKEY", "short-id": "ab12"}
    assert (
        vless.config["flow"] == "xtls-rprx-vision"
        and vless.config["client-fingerprint"] == "chrome"
    )
    trojan = valid(
        "trojan://p%40ss@t.example:443?sni=t.example&type=grpc&serviceName=svc&allowInsecure=1#T"
    )
    assert trojan.config["password"] == "p@ss" and trojan.config["grpc-opts"] == {
        "grpc-service-name": "svc"
    }
    assert trojan.insecure


def test_hysteria2_and_hy2():
    node = valid(
        "hy2://secret@h.example:443/?sni=h.example&obfs=salamander&obfs-password=x&insecure=1#H"
    )
    assert (node.protocol, node.config["password"], node.config["obfs"]) == (
        "hysteria2",
        "secret",
        "salamander",
    )


@pytest.mark.parametrize(
    "line", ["wireguard://x@h:1", "vmess://not-base64!!", "trojan://pw@host-without-port"]
)
def test_unusable_links_are_refused(line):
    with pytest.raises(LinkError):
        parse_link(line)


def test_informational_entries_are_kept_but_flagged():
    text = """
proxies:
  - {name: "更新時間：2026-10-29", type: trojan, server: 127.0.0.1, port: 1, password: x}
  - {name: "剩余流量：100 GB", type: trojan, server: 127.0.0.1, port: 2, password: x}
  - {name: "續約專用線路 - user.example", type: trojan, server: r.example, port: 443, password: x}
  - {name: "🇺🇸 美國 01", type: trojan, server: us.example, port: 443, password: x}
"""
    flags = {node.name: node.informational for node in parse_subscription(text).nodes}
    assert flags == {
        "更新時間：2026-10-29": True,
        "剩余流量：100 GB": True,
        "續約專用線路 - user.example": False,
        "🇺🇸 美國 01": False,
    }
    assert not is_informational("Tokyo 01") and is_informational("Expire: 2027-01-01")
    with pytest.raises(SubscriptionError):
        parse_subscription(
            "proxies:\n  - {name: 官网 example.com, type: http, server: a.example, port: 80}\n"
        )


def test_manual_text_accepts_lines_links_and_yaml_and_reports_bad_lines():
    entries = parse_manual_text(
        "1.2.3.4:8080:u:p\n# comment\n\nbad line\ntrojan://pw@t.example:443#T\n", "socks5"
    )
    assert [(line, (raw or {}).get("type"), failure) for line, raw, failure in entries] == [
        (1, "socks5", None),
        (4, None, "unrecognised proxy line"),
        (5, "trojan", None),
    ]
    yaml_entries = parse_manual_text(
        "proxies:\n  - {name: Y, type: socks5, server: s.example, port: 1080}\n"
    )
    assert yaml_entries[0][1]["name"] == "Y"


@pytest.fixture
def catalog(profile_manager, monkeypatch):
    monkeypatch.setenv("CPM_SECRET_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()
    yield NetworkCatalog(profile_manager)
    get_settings.cache_clear()


def test_manual_groups_add_skip_duplicates_and_keep_secrets_sealed(catalog):
    group = catalog.create_manual_group("Static residential")
    assert group["manual"] and group["source_kind"] == "manual"
    result = catalog.add_manual_nodes(
        group["id"],
        parse_manual_text(f"9.9.9.9:3128:buyer:{SECRET}\n9.9.9.9:3128:buyer:{SECRET}\nnope\n"),
    )
    assert len(result["created"]) == 1
    assert result["skipped"] == [
        {"line": 2, "reason": "already exists"},
        {"line": 3, "reason": "unrecognised proxy line"},
    ]
    dump = "\n".join(catalog.connection.iterdump())
    assert SECRET not in dump and SECRET not in repr(catalog.list_nodes())
    node_id = result["created"][0]["id"]
    assert catalog.node_outbound(node_id)[1]["password"] == SECRET
    with pytest.raises(NetworkNotFound):
        catalog.add_manual_nodes("missing", [])


@pytest.mark.asyncio
async def test_manual_nodes_can_be_edited_and_deleted_unless_bound(catalog, profile_manager):
    group = catalog.create_manual_group("Static")
    node_id = catalog.add_manual_nodes(
        group["id"], parse_manual_text("8.8.4.4:1080:u:old", "socks5")
    )["created"][0]["id"]
    catalog.save_check(
        node_id,
        {
            "health": "ok",
            "exit_ip": "8.8.4.4",
            "latitude": 1.0,
            "longitude": 1.0,
            "precision": "coordinates",
        },
    )
    renamed = catalog.update_manual_node(node_id, {"name": "Seller A"})
    assert (
        renamed["name"] == "Seller A" and renamed["check"] is not None
    )  # a rename keeps measurements
    moved = catalog.update_manual_node(node_id, {"server": "8.8.8.8", "password": "new"})
    assert (
        moved["entry"] == "8.8.8.8:1080" and moved["check"] is None
    )  # a new route forgets the old exit
    assert catalog.node_outbound(node_id)[1]["password"] == "new"
    kept = catalog.update_manual_node(node_id, {"port": 1081})
    assert (
        catalog.node_outbound(node_id)[1]["password"] == "new" and kept["entry"] == "8.8.8.8:1081"
    )
    profile = await profile_manager.create_profile("P", generate_fingerprint=False)
    catalog.connection.execute(
        "INSERT INTO profile_network_bindings (profile_id, node_id, bound_at) VALUES (?,?,?)",
        (profile.id, node_id, "2026-10-29T00:00:00+00:00"),
    )
    catalog.connection.commit()
    with pytest.raises(NodeInUse):
        catalog.delete_manual_node(node_id)
    catalog.connection.execute("DELETE FROM profile_network_bindings")
    catalog.connection.commit()
    catalog.delete_manual_node(node_id)
    assert catalog.list_nodes()["total"] == 0


@pytest.mark.asyncio
async def test_subscription_nodes_are_not_edited_by_hand_and_manual_groups_do_not_sync(catalog):
    created = await catalog.create_subscription(
        "Airport", content=b"proxies:\n  - {name: A, type: socks5, server: a.example, port: 1}\n"
    )
    node_id = catalog.list_nodes()["nodes"][0]["id"]
    with pytest.raises(SubscriptionError):
        catalog.update_manual_node(node_id, {"name": "x"})
    with pytest.raises(SubscriptionError):
        catalog.delete_manual_node(node_id)
    group = catalog.create_manual_group("Static")
    with pytest.raises(SubscriptionError) as error:
        await catalog.sync_subscription(group["id"])
    assert error.value.code == "manual_group"
    assert created["subscription"]["manual"] is False


@pytest.mark.asyncio
async def test_informational_entries_are_stored_off_the_map(catalog):
    await catalog.create_subscription(
        "Airport",
        content="proxies:\n  - {name: '到期時間：2027-04-07', type: trojan, server: 127.0.0.1, port: 1, password: x}\n"
        "  - {name: JP, type: trojan, server: jp.example, port: 443, password: x}\n".encode(),
    )
    nodes = {node["name"]: node for node in catalog.list_nodes()["nodes"]}
    assert nodes["到期時間：2027-04-07"]["status"] == "invalid"
    assert nodes["到期時間：2027-04-07"]["invalid_reason"] == "informational entry"
    assert nodes["JP"]["status"] == "active"
    assert catalog.map_data()["total_nodes"] == 1


@pytest.mark.asyncio
async def test_rewritten_informational_lines_do_not_pile_up(catalog):
    def panel(stamp):
        return (
            f"proxies:\n  - {{name: '更新時間：{stamp}', type: trojan, server: 127.0.0.1, port: 1, password: x}}\n"
            "  - {name: JP, type: trojan, server: jp.example, port: 443, password: x}\n"
        ).encode()

    created = await catalog.create_subscription("Airport", content=panel("10:00"))
    subscription_id = created["subscription"]["id"]
    for stamp in ("11:00", "12:00"):
        await catalog.sync_subscription(subscription_id, content=panel(stamp))
    names = sorted(node["name"] for node in catalog.list_nodes()["nodes"])
    assert names == ["JP", "更新時間：12:00"]
