"""Generated Mihomo configuration, binary discovery and orphan reaping (no real engine)."""

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from camoufox_pm.core import mihomo_manager as mm
from camoufox_pm.core.mihomo_manager import (
    Listener,
    MihomoUnavailable,
    classify_log,
    reap_orphans,
    runtime_config,
    validate_binary,
    validate_runtime_config,
)

OUTBOUND = {
    "type": "ss",
    "server": "jp.example",
    "port": 443,
    "cipher": "aes-128-gcm",
    "password": "pw",
}


def listener(port=28001, outbound="node-0"):
    return Listener("in-0", outbound, port, "cpm-user", "cpm-pass")


def test_generated_config_is_loopback_only_authenticated_and_fail_closed():
    config = runtime_config({"node-0": OUTBOUND}, [listener()])
    assert config["allow-lan"] is False and config["bind-address"] == "127.0.0.1"
    assert config["rules"] == ["MATCH,REJECT"]
    assert config["dns"]["enhanced-mode"] == "redir-host"
    assert "fake-ip-range" not in config["dns"] and config["profile"]["store-fake-ip"] is False
    for key in ("external-controller", "mixed-port", "port", "socks-port", "proxy-groups"):
        assert key not in config
    assert config["tun"] == {"enable": False}
    (entry,) = config["listeners"]
    assert entry["listen"] == "127.0.0.1" and entry["proxy"] == "node-0"
    assert entry["users"] == [{"username": "cpm-user", "password": "cpm-pass"}]
    assert [proxy["type"] for proxy in config["proxies"]] == ["ss"]
    assert not any(proxy["type"].lower() == "direct" for proxy in config["proxies"])


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c.update({"external-controller": "127.0.0.1:9090"}),
        lambda c: c.update({"mixed-port": 7890}),
        lambda c: c.update({"allow-lan": True}),
        lambda c: c["tun"].update({"enable": True}),
        lambda c: c["dns"].update({"enhanced-mode": "fake-ip"}),
        lambda c: c.update({"rules": ["MATCH,DIRECT"]}),
        lambda c: c["proxies"].append({"name": "d", "type": "direct"}),
        lambda c: c["proxies"][0].update({"dialer-proxy": "d"}),
        lambda c: c["listeners"][0].update({"listen": "0.0.0.0"}),
        lambda c: c["listeners"][0].update({"users": []}),
        lambda c: c["listeners"][0].update({"proxy": "GLOBAL"}),
        lambda c: c.update({"proxy-groups": [{"name": "G", "type": "select"}]}),
    ],
)
def test_every_safety_invariant_is_enforced(mutate):
    config = runtime_config({"node-0": OUTBOUND}, [listener()])
    mutate(config)
    with pytest.raises(ValueError):
        validate_runtime_config(config)


def test_batch_listeners_each_name_their_own_node():
    outbounds = {f"node-{i}": dict(OUTBOUND) for i in range(3)}
    listeners = [Listener(f"in-{i}", f"node-{i}", 28000 + i, f"u{i}", f"p{i}") for i in range(3)]
    config = runtime_config(outbounds, listeners)
    assert [entry["proxy"] for entry in config["listeners"]] == ["node-0", "node-1", "node-2"]
    assert len({entry["port"] for entry in config["listeners"]}) == 3


def fake_binary(directory: Path, version_line: str) -> Path:
    path = directory / "mihomo"
    path.write_text(f"#!/bin/sh\necho '{version_line}'\n")
    path.chmod(0o755)
    return path


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_binary_validation_refuses_unsafe_files(tmp_path):
    good = fake_binary(tmp_path, "Mihomo Meta v1.19.3 darwin arm64 with go1.24")
    assert validate_binary(good).version == "1.19.3"

    (tmp_path / "old").mkdir()
    old = fake_binary(tmp_path / "old", "Mihomo Meta v1.17.0 linux amd64")
    with pytest.raises(MihomoUnavailable) as error:
        validate_binary(old)
    assert error.value.code == "binary_outdated"

    other = tmp_path / "other"
    other.mkdir()
    wrong = fake_binary(other, "something else 1.0")
    with pytest.raises(MihomoUnavailable) as error:
        validate_binary(wrong)
    assert error.value.code == "binary_invalid"

    writable = tmp_path / "writable"
    writable.mkdir()
    shared = fake_binary(writable, "Mihomo Meta v1.19.3")
    shared.chmod(0o777)
    with pytest.raises(MihomoUnavailable) as error:
        validate_binary(shared)
    assert error.value.code == "binary_writable"

    with pytest.raises(MihomoUnavailable) as error:
        validate_binary(tmp_path / "missing")
    assert error.value.code == "binary_missing"


@pytest.mark.skipif(os.name == "nt", reason="POSIX setuid")
def test_setuid_binaries_are_refused(tmp_path):
    path = fake_binary(tmp_path, "Mihomo Meta v1.19.3")
    path.chmod(0o755 | stat.S_ISUID)
    if not path.stat().st_mode & stat.S_ISUID:
        pytest.fail("This filesystem cannot set the setuid bit; run the test elsewhere")
    with pytest.raises(MihomoUnavailable) as error:
        validate_binary(path)
    assert error.value.code == "binary_setuid"


def test_an_explicitly_configured_broken_binary_is_the_answer(tmp_path, monkeypatch):
    broken = tmp_path / "mihomo"
    broken.write_text("not executable")
    monkeypatch.delenv("CPM_MIHOMO_PATH", raising=False)
    with pytest.raises(MihomoUnavailable) as error:
        mm.discover_binary(str(broken), None)
    assert error.value.code == "binary_invalid"


@pytest.mark.parametrize(
    "line, code",
    [
        ("listen tcp 127.0.0.1:28001: bind: address already in use", "port_conflict"),
        ("Parse config error: proxy 1: ss a:1 cipher: x initialize error", "config_invalid"),
        (
            "dial node-0 --> a:443 error: x509: certificate signed by unknown authority",
            "node_tls_failed",
        ),
        ("dial tcp 127.0.0.1:1: connect: connection refused", "node_unreachable"),
        ("context deadline exceeded", "node_timeout"),
        ("authentication failed", "node_auth_failed"),
    ],
)
def test_engine_output_is_classified_without_being_echoed(line, code):
    classified = classify_log([line])
    assert classified is not None and classified[0] == code
    assert "127.0.0.1" not in classified[1] and "a:443" not in classified[1]


def _dead_pid() -> tuple[int, float]:
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    started = psutil.Process(process.pid).create_time()
    process.wait()
    return process.pid, started


def _write_owner(directory: Path, manager: tuple[int, float], child: subprocess.Popen, config: str):
    directory.mkdir(parents=True)
    (directory / "owner.json").write_text(
        json.dumps(
            {
                "manager_pid": manager[0],
                "manager_started": manager[1],
                "pid": child.pid,
                "pid_started": psutil.Process(child.pid).create_time(),
                "config": config,
            }
        )
    )


def test_orphans_of_a_dead_manager_are_stopped_and_live_owners_kept(tmp_path):
    root = tmp_path / "network-runtime"
    orphan_dir, live_dir = root / "profile-a", root / "profile-b"
    orphan_config = str(orphan_dir / "config.yaml")
    sleeper = [sys.executable, "-c", "import time; time.sleep(60)"]
    orphan = subprocess.Popen([*sleeper, orphan_config])
    kept = subprocess.Popen([*sleeper, str(live_dir / "config.yaml")])
    try:
        _write_owner(orphan_dir, _dead_pid(), orphan, orphan_config)
        me = psutil.Process()
        _write_owner(live_dir, (me.pid, me.create_time()), kept, str(live_dir / "config.yaml"))
        assert reap_orphans(root) == 1
        orphan.wait(5)
        assert not orphan_dir.exists()
        assert kept.poll() is None and live_dir.exists()
    finally:
        for process in (orphan, kept):
            if process.poll() is None:
                process.kill()
            process.wait()


def test_reaping_never_touches_a_process_that_merely_reused_the_pid(tmp_path):
    root = tmp_path / "network-runtime"
    directory = root / "profile-x"
    # A user's own process, whose command line does not name our configuration.
    user_process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        _write_owner(directory, _dead_pid(), user_process, str(directory / "config.yaml"))
        assert reap_orphans(root) == 0
        assert user_process.poll() is None
        assert not directory.exists()
    finally:
        user_process.kill()
        user_process.wait()


def test_fresh_unstamped_directories_belong_to_a_manager_still_starting(tmp_path):
    root = tmp_path / "network-runtime"
    (root / "starting").mkdir(parents=True)
    old = root / "abandoned"
    old.mkdir()
    stale = time.time() - 3600
    os.utime(old, (stale, stale))
    reap_orphans(root)
    assert (root / "starting").exists() and not old.exists()
