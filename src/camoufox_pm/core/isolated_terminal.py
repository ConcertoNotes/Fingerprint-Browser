"""One-click same-tunnel isolated OS terminal launcher for Prism Browser.

Spawns an interactive native terminal (Terminal.app on macOS, Windows Terminal /
PowerShell on Windows, or the desktop terminal emulator on Linux) whose ``HOME``,
``CLAUDE_CONFIG_DIR``, ``XDG_*``, ``TMPDIR``, timezone, locale, and proxy
environment variables are strictly scoped to one running profile's private
directory and live authenticated loopback proxy tunnel.

Proxy credentials are written only into a ``0700``/``0600`` self-deleting
launcher script inside ``<profile_dir>/.terminal/tmp/`` that deletes itself on
its very first line before exec'ing the interactive shell, and are never
returned by the API or printed in the terminal banner.
"""

from __future__ import annotations

import os
import secrets
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

from loguru import logger

from . import proxy_check
from .fs_permissions import (
    create_private_directory,
    create_private_file,
    protect_path,
    verify_private_path,
)
from .isolation import parse_isolation
from .profile_operations import profile_data_path

Spawner = Callable[[Path, list[str]], None]


class TerminalLaunchError(RuntimeError):
    """Raised when an isolated terminal cannot be opened for a profile."""

    def __init__(self, status: int, code: str, message: str):
        self.status = status
        self.code = code
        super().__init__(message)


def _normalize_platform(platform_override: str | None = None) -> str:
    raw = (platform_override or sys.platform).lower()
    if raw.startswith("darwin") or raw == "macos":
        return "darwin"
    if raw.startswith("win") or raw == "cygwin":
        return "windows"
    return "linux"


def _ps_quote(value: str) -> str:
    """Single-quote a literal string safely for PowerShell."""
    return "'" + value.replace("'", "''") + "'"


def _safe_banner_value(value: str, max_len: int = 80) -> str:
    cleaned = "".join(ch for ch in value if ch.isprintable() and ch not in "\r\n\t")
    return cleaned[:max_len].strip() or "-"


def _resolve_timezone(profile: Any, policy: Any, runtime: Any) -> str:
    iso = parse_isolation(profile.isolation_config)
    if iso is not None and iso.timezone_mode == "fixed" and iso.timezone:
        return iso.timezone
    if profile.browser_settings and profile.browser_settings.timezone:
        return profile.browser_settings.timezone
    if runtime.exit_ip:
        try:
            location = proxy_check.locate(runtime.exit_ip)
            if location.timezone:
                return location.timezone
        except proxy_check.LocationUnavailable:
            pass
    return policy.default_timezone or "America/Los_Angeles"


def _resolve_exit_country(profile: Any, runtime: Any) -> str | None:
    country = getattr(runtime, "exit_country", None)
    if country:
        return str(country)
    if runtime.exit_ip:
        try:
            location = proxy_check.locate(runtime.exit_ip)
            if location.country:
                return location.country
        except proxy_check.LocationUnavailable:
            pass
    node_check = (runtime.node or {}).get("check") or {}
    if node_check.get("country_iso") and (
        not runtime.exit_ip or node_check.get("exit_ip") == runtime.exit_ip
    ):
        return str(node_check["country_iso"])
    if profile.last_exit_country and (
        not runtime.exit_ip or profile.last_exit_ip == runtime.exit_ip
    ):
        return str(profile.last_exit_country)
    return None


def _build_env_map(
    *,
    profile_id: str,
    home_dir: Path,
    claude_dir: Path,
    config_dir: Path,
    cache_dir: Path,
    data_dir: Path,
    tmp_dir: Path,
    proxy_url: str,
    timezone_name: str,
    platform_name: str,
) -> dict[str, str]:
    env = {
        "HOME": str(home_dir),
        "CLAUDE_CONFIG_DIR": str(claude_dir),
        "XDG_CONFIG_HOME": str(config_dir),
        "XDG_CACHE_HOME": str(cache_dir),
        "XDG_DATA_HOME": str(data_dir),
        "TMPDIR": str(tmp_dir),
        "HTTP_PROXY": proxy_url,
        "HTTPS_PROXY": proxy_url,
        "ALL_PROXY": proxy_url,
        "http_proxy": proxy_url,
        "https_proxy": proxy_url,
        "all_proxy": proxy_url,
        "NO_PROXY": "",
        "no_proxy": "",
        "TZ": timezone_name,
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "PRISM_PROFILE_ID": profile_id,
    }
    if platform_name == "windows":
        env["USERPROFILE"] = str(home_dir)
        env["TEMP"] = str(tmp_dir)
        env["TMP"] = str(tmp_dir)
    return env


def _build_posix_script(
    *,
    env: dict[str, str],
    home_dir: Path,
    profile_name: str,
    profile_id: str,
    exit_ip: str | None,
    exit_country: str | None,
    proxy_port: int,
    timezone_name: str,
    platform_name: str,
) -> str:
    default_shell = "/bin/zsh" if platform_name == "darwin" else "/bin/bash"
    safe_name = _safe_banner_value(profile_name)
    safe_exit = f"{exit_ip or 'pending'} ({exit_country or '??'}) · 127.0.0.1:{proxy_port}"
    lines = [
        "#!/usr/bin/env bash",
        'rm -f -- "$0"',
    ]
    for key, value in env.items():
        lines.append(f"export {key}={shlex.quote(value)}")
    lines.extend(
        [
            f"cd {shlex.quote(str(home_dir))} || true",
            f"printf '%s\\n' {shlex.quote('=== Prism Browser Isolated Terminal ===')}",
            f"printf '%s\\n' {shlex.quote(f'  Profile : {safe_name} ({profile_id})')}",
            f"printf '%s\\n' {shlex.quote(f'  Tunnel  : {safe_exit}')}",
            f"printf '%s\\n' {shlex.quote(f'  TZ/Lang : {timezone_name} · en_US.UTF-8')}",
            f"printf '%s\\n\\n' {shlex.quote(f'  Home    : {home_dir}')}",
            f'exec "${{SHELL:-{default_shell}}}" -i',
        ]
    )
    return "\n".join(lines) + "\n"


def _build_windows_script(
    *,
    env: dict[str, str],
    home_dir: Path,
    profile_name: str,
    profile_id: str,
    exit_ip: str | None,
    exit_country: str | None,
    proxy_port: int,
    timezone_name: str,
) -> str:
    safe_name = _safe_banner_value(profile_name)
    safe_exit = f"{exit_ip or 'pending'} ({exit_country or '??'}) · 127.0.0.1:{proxy_port}"
    lines = [
        "Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue",
    ]
    for key, value in env.items():
        lines.append(f"$env:{key} = {_ps_quote(value)}")
    lines.extend(
        [
            f"Set-Location -LiteralPath {_ps_quote(str(home_dir))}",
            f"Write-Host {_ps_quote('=== Prism Browser Isolated Terminal ===')} -ForegroundColor Cyan",
            f"Write-Host {_ps_quote(f'  Profile : {safe_name} ({profile_id})')}",
            f"Write-Host {_ps_quote(f'  Tunnel  : {safe_exit}')}",
            f"Write-Host {_ps_quote(f'  TZ/Lang : {timezone_name} · en_US.UTF-8')}",
            f"Write-Host {_ps_quote(f'  Home    : {home_dir}')}",
            "Write-Host ''",
        ]
    )
    return "\r\n".join(lines) + "\r\n"


def _build_spawn_command(platform_name: str, script_path: Path) -> list[str]:
    if platform_name == "darwin":
        return ["open", "-a", "Terminal", str(script_path)]
    if platform_name == "windows":
        ps = shutil.which("pwsh") or shutil.which("powershell") or "powershell.exe"
        ps_args = [
            ps,
            "-NoLogo",
            "-NoExit",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script_path),
        ]
        wt = shutil.which("wt")
        if wt:
            return [wt, "new-tab", "--", *ps_args]
        return ps_args
    candidates: tuple[tuple[str, list[str]], ...] = (
        ("x-terminal-emulator", ["-e", str(script_path)]),
        ("gnome-terminal", ["--", str(script_path)]),
        ("konsole", ["-e", str(script_path)]),
        ("xfce4-terminal", ["-x", str(script_path)]),
        ("xterm", ["-e", str(script_path)]),
    )
    for binary, args in candidates:
        resolved = shutil.which(binary)
        if resolved:
            return [resolved, *args]
    raise TerminalLaunchError(
        503,
        "terminal_unavailable",
        "No supported terminal emulator was found on this system.",
    )


def spawn_terminal(script_path: Path, command: list[str], *, platform_name: str) -> None:
    """Launch the native OS terminal emulator detached from the backend server."""
    if platform_name == "darwin":
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            raise OSError(result.stderr.strip() or f"open exited with status {result.returncode}")
        return

    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if platform_name == "windows":
        if Path(command[0]).name.lower() != "wt.exe":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(command, **kwargs)


def _ensure_private_dir(path: Path) -> None:
    if path.exists():
        protect_path(path)
    else:
        create_private_directory(path)


def _write_private_script(script_path: Path, content: str, *, executable: bool) -> None:
    fd = create_private_file(script_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
    except BaseException:
        script_path.unlink(missing_ok=True)
        raise
    if os.name != "nt" and executable:
        os.chmod(script_path, 0o700)
    verify_private_path(script_path)


async def open_isolated_terminal(
    manager: Any,
    profile_id: str,
    *,
    spawner: Spawner | None = None,
    platform_override: str | None = None,
) -> dict[str, Any]:
    """Open an isolated native terminal routed through a running profile's proxy tunnel."""
    profile = await manager.get_profile(profile_id)
    if profile is None:
        raise TerminalLaunchError(
            404, "profile_not_found", f"Profile with ID {profile_id} not found"
        )

    runtime = manager.network.runtimes.get(profile_id)
    if runtime is None or not runtime.engine.alive or runtime.listener is None:
        raise TerminalLaunchError(
            409,
            "terminal_requires_running_node",
            "Start this profile with a bound proxy node before opening an isolated terminal.",
        )

    platform_name = _normalize_platform(platform_override)
    policy, _ = manager.active_policy()

    profile_dir = profile_data_path(manager, profile).resolve()
    profile_dir.parent.mkdir(parents=True, exist_ok=True)
    _ensure_private_dir(profile_dir)

    term_root = profile_dir / ".terminal"
    home_dir = term_root / "home"
    claude_dir = home_dir / ".claude"
    config_dir = term_root / "config"
    cache_dir = term_root / "cache"
    data_dir = term_root / "data"
    tmp_dir = term_root / "tmp"
    for directory in (term_root, home_dir, claude_dir, config_dir, cache_dir, data_dir, tmp_dir):
        _ensure_private_dir(directory)

    listener = runtime.listener
    port = int(listener.port)
    user_enc = quote(str(listener.username), safe="")
    pass_enc = quote(str(listener.password), safe="")
    proxy_url = f"http://{user_enc}:{pass_enc}@127.0.0.1:{port}"

    timezone_name = _resolve_timezone(profile, policy, runtime)
    exit_country = _resolve_exit_country(profile, runtime)

    env = _build_env_map(
        profile_id=profile.id,
        home_dir=home_dir,
        claude_dir=claude_dir,
        config_dir=config_dir,
        cache_dir=cache_dir,
        data_dir=data_dir,
        tmp_dir=tmp_dir,
        proxy_url=proxy_url,
        timezone_name=timezone_name,
        platform_name=platform_name,
    )

    token = secrets.token_hex(8)
    if platform_name == "darwin":
        script_path = tmp_dir / f"launch-{token}.command"
        script_body = _build_posix_script(
            env=env,
            home_dir=home_dir,
            profile_name=profile.name,
            profile_id=profile.id,
            exit_ip=runtime.exit_ip,
            exit_country=exit_country,
            proxy_port=port,
            timezone_name=timezone_name,
            platform_name=platform_name,
        )
        executable = True
    elif platform_name == "windows":
        script_path = tmp_dir / f"launch-{token}.ps1"
        script_body = _build_windows_script(
            env=env,
            home_dir=home_dir,
            profile_name=profile.name,
            profile_id=profile.id,
            exit_ip=runtime.exit_ip,
            exit_country=exit_country,
            proxy_port=port,
            timezone_name=timezone_name,
        )
        executable = False
    else:
        script_path = tmp_dir / f"launch-{token}.sh"
        script_body = _build_posix_script(
            env=env,
            home_dir=home_dir,
            profile_name=profile.name,
            profile_id=profile.id,
            exit_ip=runtime.exit_ip,
            exit_country=exit_country,
            proxy_port=port,
            timezone_name=timezone_name,
            platform_name=platform_name,
        )
        executable = True

    _write_private_script(script_path, script_body, executable=executable)

    try:
        if spawner is not None:
            command = (
                ["open", "-a", "Terminal", str(script_path)]
                if platform_name == "darwin"
                else [str(script_path)]
            )
            spawner(script_path, command)
        else:
            command = _build_spawn_command(platform_name, script_path)
            spawn_terminal(script_path, command, platform_name=platform_name)
    except TerminalLaunchError:
        script_path.unlink(missing_ok=True)
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        script_path.unlink(missing_ok=True)
        logger.warning(f"Failed to spawn isolated terminal for {profile_id}: {type(exc).__name__}")
        raise TerminalLaunchError(
            500,
            "terminal_launch_failed",
            "Could not launch the system terminal emulator.",
        ) from None

    return {
        "profile_id": profile_id,
        "isolated_home": str(home_dir),
        "timezone": timezone_name,
        "exit_ip": runtime.exit_ip,
        "exit_country": exit_country,
        "proxy_port": port,
        "launched": True,
        "platform": platform_name,
    }
