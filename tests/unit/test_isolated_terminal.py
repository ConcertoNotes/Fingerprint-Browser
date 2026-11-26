"""Unit tests for one-click same-tunnel isolated OS terminal launching."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from camoufox_pm.core.isolated_terminal import TerminalLaunchError, open_isolated_terminal


def _attach_fake_runtime(
    profile_manager,
    profile_id: str,
    *,
    alive: bool = True,
    port: int = 23456,
    username: str = "prism-user",
    password: str = "TopSecret#Pass/42",
    exit_ip: str = "203.0.113.88",
    exit_country: str = "US",
):
    runtime = SimpleNamespace(
        profile_id=profile_id,
        node={
            "id": "n-term",
            "name": "LA-Static",
            "check": {"exit_ip": exit_ip, "country_iso": exit_country},
        },
        listener=SimpleNamespace(port=port, username=username, password=password),
        engine=SimpleNamespace(alive=alive),
        exit_ip=exit_ip,
        exit_country=exit_country,
    )
    profile_manager.network.runtimes[profile_id] = runtime
    return runtime


@pytest.mark.asyncio
async def test_isolated_terminal_requires_existing_profile_and_running_bound_node(profile_manager):
    with pytest.raises(TerminalLaunchError) as missing_exc:
        await open_isolated_terminal(profile_manager, "nonexistent")
    assert missing_exc.value.status == 404
    assert missing_exc.value.code == "profile_not_found"

    profile = await profile_manager.create_profile(name="Stopped Profile")
    with pytest.raises(TerminalLaunchError) as stopped_exc:
        await open_isolated_terminal(profile_manager, profile.id)
    assert stopped_exc.value.status == 409
    assert stopped_exc.value.code == "terminal_requires_running_node"

    # Degraded (dead engine) runtime must also be rejected cleanly.
    _attach_fake_runtime(profile_manager, profile.id, alive=False)
    with pytest.raises(TerminalLaunchError) as dead_exc:
        await open_isolated_terminal(profile_manager, profile.id)
    assert dead_exc.value.status == 409
    assert dead_exc.value.code == "terminal_requires_running_node"


@pytest.mark.asyncio
async def test_isolated_terminal_macos_creates_private_dirs_and_self_deleting_script(
    profile_manager,
):
    profile = await profile_manager.create_profile(name="Claude Work")
    secret_pw = "TopSecret#Pass/42"
    _attach_fake_runtime(
        profile_manager,
        profile.id,
        port=24567,
        username="u-term",
        password=secret_pw,
        exit_ip="203.0.113.88",
        exit_country="US",
    )

    spawned: list[tuple[Path, list[str], str]] = []

    def fake_spawner(script_path: Path, command: list[str]) -> None:
        spawned.append((script_path, command, script_path.read_text(encoding="utf-8")))

    result = await open_isolated_terminal(
        profile_manager,
        profile.id,
        spawner=fake_spawner,
        platform_override="darwin",
    )

    assert result["profile_id"] == profile.id
    assert result["timezone"] == "America/Los_Angeles"
    assert result["exit_ip"] == "203.0.113.88"
    assert result["exit_country"] == "US"
    assert result["proxy_port"] == 24567
    assert result["launched"] is True
    assert result["platform"] == "darwin"

    # Credentials must NEVER appear in the API response payload.
    serialized = str(result)
    assert secret_pw not in serialized
    assert "TopSecret" not in serialized
    assert "u-term" not in serialized

    assert len(spawned) == 1
    script_path, command, script_text = spawned[0]
    assert script_path.suffix == ".command"
    assert command[:3] == ["open", "-a", "Terminal"]

    # Directory layout and 0700 permissions on POSIX.
    home_dir = Path(result["isolated_home"])
    term_root = home_dir.parent
    expected_dirs = [
        term_root,
        home_dir,
        home_dir / ".claude",
        term_root / "config",
        term_root / "cache",
        term_root / "data",
        term_root / "tmp",
    ]
    for directory in expected_dirs:
        assert directory.is_dir()
        if os.name != "nt":
            assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    if os.name != "nt":
        assert stat.S_IMODE(script_path.stat().st_mode) == 0o700

    # Launcher script deletes itself on line 2 before setting env and exec'ing shell.
    lines = script_text.splitlines()
    assert lines[0] == "#!/usr/bin/env bash"
    assert lines[1] == 'rm -f -- "$0"'

    expected_proxy = "http://u-term:TopSecret%23Pass%2F42@127.0.0.1:24567"
    assert f"export HOME={home_dir}" in script_text or f"export HOME='{home_dir}'" in script_text
    assert "CLAUDE_CONFIG_DIR=" in script_text and str(home_dir / ".claude") in script_text
    assert "XDG_CONFIG_HOME=" in script_text
    assert "XDG_CACHE_HOME=" in script_text
    assert "XDG_DATA_HOME=" in script_text
    assert "TMPDIR=" in script_text
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        assert f"export {var}=" in script_text
        assert expected_proxy in script_text
    assert "export NO_PROXY=''" in script_text
    assert "export no_proxy=''" in script_text
    assert (
        "export TZ=America/Los_Angeles" in script_text
        or "export TZ='America/Los_Angeles'" in script_text
    )
    assert "export LANG=en_US.UTF-8" in script_text or "export LANG='en_US.UTF-8'" in script_text
    assert (
        "export LC_ALL=en_US.UTF-8" in script_text or "export LC_ALL='en_US.UTF-8'" in script_text
    )
    assert 'exec "${SHELL:-/bin/zsh}" -i' in script_text

    # Banner lines must be white-labeled and contain zero secrets.
    banner_lines = [line for line in lines if line.startswith("printf ")]
    banner_joined = "\n".join(banner_lines)
    assert "Prism Browser Isolated Terminal" in banner_joined
    assert secret_pw not in banner_joined
    assert "TopSecret" not in banner_joined
    for forbidden in ("Camoufox", "Mihomo", "daijro", "LuLu"):
        assert forbidden.lower() not in script_text.lower()


@pytest.mark.asyncio
async def test_isolated_terminal_windows_and_linux_scripts_and_cleanup_on_spawn_error(
    profile_manager,
):
    profile = await profile_manager.create_profile(name="Multi OS")
    _attach_fake_runtime(profile_manager, profile.id, port=25555, password="pw!secret")

    # Windows PowerShell script check.
    captured_win: list[tuple[Path, str]] = []
    win_result = await open_isolated_terminal(
        profile_manager,
        profile.id,
        spawner=lambda path, _cmd: captured_win.append((path, path.read_text(encoding="utf-8"))),
        platform_override="windows",
    )
    assert win_result["platform"] == "windows"
    assert "pw!secret" not in str(win_result)
    win_path, win_script = captured_win[0]
    assert win_path.suffix == ".ps1"
    assert win_script.startswith("Remove-Item -LiteralPath $PSCommandPath -Force")
    assert "$env:CLAUDE_CONFIG_DIR =" in win_script
    assert "$env:USERPROFILE =" in win_script
    assert "$env:NO_PROXY = ''" in win_script
    assert "$env:TZ = 'America/Los_Angeles'" in win_script

    # Linux script check.
    captured_linux: list[tuple[Path, str]] = []
    linux_result = await open_isolated_terminal(
        profile_manager,
        profile.id,
        spawner=lambda path, _cmd: captured_linux.append((path, path.read_text(encoding="utf-8"))),
        platform_override="linux",
    )
    assert linux_result["platform"] == "linux"
    linux_path, linux_script = captured_linux[0]
    assert linux_path.suffix == ".sh"
    assert 'exec "${SHELL:-/bin/bash}" -i' in linux_script

    # If spawner fails, secret-bearing launcher script MUST be unlinked.
    created_scripts: list[Path] = []

    def failing_spawner(path: Path, _cmd: list[str]) -> None:
        created_scripts.append(path)
        assert path.exists()
        raise OSError("terminal app missing")

    with pytest.raises(TerminalLaunchError) as exc:
        await open_isolated_terminal(
            profile_manager,
            profile.id,
            spawner=failing_spawner,
            platform_override="darwin",
        )
    assert exc.value.status == 500
    assert exc.value.code == "terminal_launch_failed"
    assert created_scripts and not created_scripts[0].exists()
