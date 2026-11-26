"""Profile Isolation Auditor — 11-point anti-leak & fingerprint health check."""

from __future__ import annotations

import os
import stat
from datetime import datetime, timezone
from typing import Any, Literal

from ..fs_permissions import PermissionProtectionError, reject_reparse_paths
from ..network_binding import NetworkBindings
from ..profile_operations import profile_data_path
from .chrome_presets import (
    DEFAULT_CHROME_MAJOR,
    browser_major,
    pin_cores,
    pin_gpu,
    pin_screen,
    pin_user_agent,
    pinned_os,
)
from .models import (
    compute_fingerprint_hash,
    isolation_from_legacy_row,
    parse_isolation,
)
from .policy import LOCKED_PREF_VALUES, load_active_policy

CheckStatus = Literal["pass", "warn", "fail"]


def _check(
    key: str,
    title: str,
    status: CheckStatus,
    summary: str,
    detail: str,
    action: str | None = None,
) -> dict[str, Any]:
    return {
        "key": key,
        "title": title,
        "status": status,
        "summary": summary,
        "detail": detail,
        "action": action,
    }


async def audit_profile(manager: Any, profile_id: str) -> dict[str, Any]:
    """Run all 11 isolation health checks on one profile and return a scored report."""
    profile = await manager.get_profile(profile_id)
    if profile is None:
        raise ValueError(f"Profile with ID {profile_id} not found")

    connection = manager.storage.db._connection
    policy, policy_source = load_active_policy(connection)

    iso = parse_isolation(profile.isolation_config)
    if iso is None:
        iso = isolation_from_legacy_row(
            profile.browser_settings.model_dump() if profile.browser_settings else {},
            profile.fingerprint,
            policy,
        )

    pin = iso.pinned_fingerprint or profile.fingerprint or {}
    fp_hash = profile.fingerprint_hash or iso.fingerprint_hash or compute_fingerprint_hash(pin)
    checks: list[dict[str, Any]] = []

    # 1. fingerprint_pinned
    pinned_os_name = pinned_os(pin)
    os_mismatch = bool(pinned_os_name and pinned_os_name != iso.os)
    ua = pin_user_agent(pin)
    width, height = pin_screen(pin)
    _, renderer = pin_gpu(pin)
    cores = pin_cores(pin)
    has_clean_ua = bool(ua and "electron/" not in ua.lower())
    has_screen = bool(width and height)
    has_gpu = bool(renderer)
    has_seeds = bool(pin.get("canvas_seed") and pin.get("audio_seed") and pin.get("webgl_seed"))

    if not pin or not has_clean_ua or not has_screen:
        checks.append(
            _check(
                "fingerprint_pinned",
                "Hardware Fingerprint Pinned",
                "fail",
                "Device hardware fingerprint is missing or incomplete",
                "Pin a real device preset so hardware properties never drift across sessions.",
                action="reoptimize",
            )
        )
    elif os_mismatch:
        checks.append(
            _check(
                "fingerprint_pinned",
                "Hardware Fingerprint Pinned",
                "fail",
                f"OS mismatch: configured {iso.os} vs pinned {pinned_os_name}",
                "Reconcile OS setting with pinned hardware to avoid cross-property detection.",
                action="reconcile_os",
            )
        )
    elif not has_gpu or not has_seeds:
        checks.append(
            _check(
                "fingerprint_pinned",
                "Hardware Fingerprint Pinned",
                "warn",
                "Pinned hardware is missing WebGL renderer or deterministic noise seeds",
                "Re-optimize to populate WebGL metadata and pinned Canvas/Audio/WebGL seeds.",
                action="reoptimize",
            )
        )
    else:
        checks.append(
            _check(
                "fingerprint_pinned",
                "Hardware Fingerprint Pinned",
                "pass",
                f"Pinned {iso.os} device ({fp_hash})",
                f"Screen {width}x{height}, {cores or '?'} cores, GPU {renderer}.",
            )
        )

    # 2. fingerprint_unique
    duplicate_ids: list[str] = []
    if fp_hash:
        rows = connection.execute(
            "SELECT id FROM profiles WHERE deleted_at IS NULL AND id != ? AND fingerprint_hash = ?",
            (profile_id, fp_hash),
        ).fetchall()
        duplicate_ids = [row[0] for row in rows]
    if not fp_hash:
        checks.append(
            _check(
                "fingerprint_unique",
                "Cross-Window Fingerprint Uniqueness",
                "warn",
                "No fingerprint hash recorded",
                "Run re-optimization to assign a unique real-device fingerprint hash.",
                action="reoptimize",
            )
        )
    elif duplicate_ids:
        checks.append(
            _check(
                "fingerprint_unique",
                "Cross-Window Fingerprint Uniqueness",
                "fail",
                f"Fingerprint {fp_hash} collides with {len(duplicate_ids)} other profile(s)",
                f"Shared with profile IDs: {', '.join(duplicate_ids[:5])}. Rotate hardware identity.",
                action="reset_fingerprint",
            )
        )
    else:
        checks.append(
            _check(
                "fingerprint_unique",
                "Cross-Window Fingerprint Uniqueness",
                "pass",
                f"Unique across all local profiles ({fp_hash})",
                "No other local browser shares this hardware fingerprint hash.",
            )
        )

    # 3. browser_version
    pinned_major = browser_major(pin)
    target_major = policy.recommended_chrome_major or DEFAULT_CHROME_MAJOR
    if pinned_major is None:
        checks.append(
            _check(
                "browser_version",
                "Browser Kernel Version Alignment",
                "warn",
                "Cannot read browser major version from User-Agent",
                "Refresh browser version to align with the embedded Chromium engine baseline.",
                action="refresh_browser",
            )
        )
    elif pinned_major < target_major:
        checks.append(
            _check(
                "browser_version",
                "Browser Kernel Version Alignment",
                "warn",
                f"Pinned Chrome {pinned_major} trails embedded engine baseline {target_major}",
                "Refresh browser version to update User-Agent & Client Hints while keeping pinned hardware.",
                action="refresh_browser",
            )
        )
    else:
        checks.append(
            _check(
                "browser_version",
                "Browser Kernel Version Alignment",
                "pass",
                f"Chrome {pinned_major} aligned with embedded engine",
                "User-Agent and Client Hints major version match the built-in isolation engine.",
            )
        )

    # 4. storage_isolated
    try:
        storage_dir = profile_data_path(manager, profile)
        reject_reparse_paths(storage_dir)
        if not storage_dir.exists():
            checks.append(
                _check(
                    "storage_isolated",
                    "Isolated Profile Storage",
                    "warn",
                    "Profile directory will be created on next launch",
                    f"Managed path {storage_dir.name} does not exist on disk yet.",
                )
            )
        elif os.name != "nt" and (stat.S_IMODE(storage_dir.stat().st_mode) & 0o077):
            checks.append(
                _check(
                    "storage_isolated",
                    "Isolated Profile Storage",
                    "warn",
                    "Profile directory permissions are broader than 0700",
                    "Re-optimize or launch to lock directory permissions to current user only.",
                    action="reoptimize",
                )
            )
        else:
            checks.append(
                _check(
                    "storage_isolated",
                    "Isolated Profile Storage",
                    "pass",
                    f"Private 0700 partition & storage ({storage_dir.name})",
                    f"Dedicated storage partition (persist:prism_{profile_id}) and private local directory with no symlinks.",
                )
            )
    except (ValueError, PermissionProtectionError, OSError) as exc:
        checks.append(
            _check(
                "storage_isolated",
                "Isolated Profile Storage",
                "fail",
                "Profile storage directory failed isolation check",
                str(exc),
            )
        )

    # 5. node_bound_and_alive
    binding = NetworkBindings(manager).get(profile_id)
    runtime = manager.network.runtimes.get(profile_id)
    if binding is None:
        checks.append(
            _check(
                "node_bound_and_alive",
                "Dedicated Proxy Node Binding",
                "fail",
                "No network node bound to this browser",
                "Bind an exclusive airport or static IP node on the World Map before launching.",
                action="bind_node",
            )
        )
    else:
        node = binding["node"]
        node_check = node.get("check") or {}
        if node.get("status") != "active":
            checks.append(
                _check(
                    "node_bound_and_alive",
                    "Dedicated Proxy Node Binding",
                    "fail",
                    f"Bound node '{node.get('name')}' is {node.get('status')}",
                    "Select a healthy active node on the World Map; retired nodes are fail-closed.",
                    action="bind_node",
                )
            )
        elif runtime is not None and not runtime.engine.alive:
            checks.append(
                _check(
                    "node_bound_and_alive",
                    "Dedicated Proxy Node Binding",
                    "fail",
                    "Per-profile proxy routing engine is degraded",
                    "Reconnect the runtime to restore traffic through the bound node.",
                    action="reconnect",
                )
            )
        elif node_check.get("health") == "error":
            checks.append(
                _check(
                    "node_bound_and_alive",
                    "Dedicated Proxy Node Binding",
                    "warn",
                    f"Node '{node.get('name')}' failed last connectivity probe",
                    str(node_check.get("error") or "Re-test node or switch to an online exit."),
                    action="bind_node",
                )
            )
        elif node_check.get("health") != "ok":
            checks.append(
                _check(
                    "node_bound_and_alive",
                    "Dedicated Proxy Node Binding",
                    "warn",
                    f"Node '{node.get('name')}' has not been speed-tested yet",
                    "Test this node on the World Map to verify its real exit IP and latency.",
                )
            )
        else:
            checks.append(
                _check(
                    "node_bound_and_alive",
                    "Dedicated Proxy Node Binding",
                    "pass",
                    f"Bound to online node '{node.get('name')}' ({node_check.get('latency_ms')}ms)",
                    f"Exit IP {node_check.get('exit_ip')} ({node_check.get('country_iso') or '??'}).",
                )
            )

    # 6. exit_ip_exclusivity
    exit_ip = None
    if runtime is not None and runtime.exit_ip:
        exit_ip = runtime.exit_ip
    elif binding is not None:
        exit_ip = (binding["node"].get("check") or {}).get("exit_ip")

    if not exit_ip:
        checks.append(
            _check(
                "exit_ip_exclusivity",
                "Exit IP Exclusivity",
                "warn",
                "Exit IP not measured yet",
                "Run a node probe or launch the browser to verify no other profile shares its exit IP.",
                action="bind_node" if binding is None else None,
            )
        )
    else:
        sharing_rows = connection.execute(
            """
            SELECT b.profile_id
            FROM profile_network_bindings b
            JOIN network_node_checks c ON c.node_id = b.node_id
            JOIN profiles p ON p.id = b.profile_id
            WHERE p.deleted_at IS NULL
              AND b.profile_id != ?
              AND c.exit_ip = ?
            """,
            (profile_id, exit_ip),
        ).fetchall()
        sharing = [row[0] for row in sharing_rows]
        if sharing:
            checks.append(
                _check(
                    "exit_ip_exclusivity",
                    "Exit IP Exclusivity",
                    "warn",
                    f"Exit IP {exit_ip} is shared with {len(sharing)} other profile(s)",
                    f"Profiles sharing this exit IP: {', '.join(sharing[:5])}. Consider assigning an exclusive node.",
                    action="bind_node",
                )
            )
        else:
            checks.append(
                _check(
                    "exit_ip_exclusivity",
                    "Exit IP Exclusivity",
                    "pass",
                    f"Exclusive exit IP ({exit_ip})",
                    "No other local profile is bound to a node with this measured exit IP.",
                )
            )

    # 7. leak_protection
    prefs_ok = all(
        type(policy.hardening_prefs.get(k)) is type(v) and policy.hardening_prefs.get(k) == v
        for k, v in LOCKED_PREF_VALUES.items()
    )
    tz_desc = (
        f"Fixed TZ {iso.timezone or policy.default_timezone}"
        if iso.timezone_mode == "fixed"
        else "Dynamic TZ (follow exit)"
    )
    lang_desc = ",".join(iso.languages or policy.default_languages)
    if not prefs_ok:
        checks.append(
            _check(
                "leak_protection",
                "DNS / QUIC / IPv6 / WebRTC Leak Protection",
                "fail",
                "Active hardening policy is incomplete",
                "Re-apply isolation policy to restore strict anti-leak enforcement.",
                action="reoptimize",
            )
        )
    else:
        checks.append(
            _check(
                "leak_protection",
                "DNS / QUIC / IPv6 / WebRTC Leak Protection",
                "pass",
                f"Strict anti-leak active ({iso.webrtc_policy} · {tz_desc} · {lang_desc})",
                "QUIC disabled, AsyncDns/DoH disabled, remote proxy DNS enforced via host-resolver-rules, "
                "ECH/OCSP/IPv6 bypasses blocked, and battery API stripped.",
            )
        )

    # 8. webrtc_guard_addon (native embedded WebRTC & DNS Guard)
    if not iso.install_webrtc_guard:
        checks.append(
            _check(
                "webrtc_guard_addon",
                "Built-in WebRTC & DNS Leak Guard",
                "warn",
                "Built-in WebRTC & DNS Guard is disabled on this profile",
                "Enable WebRTC & DNS Guard via Re-optimize for defense-in-depth WebRTC blocking.",
                action="reoptimize",
            )
        )
    else:
        checks.append(
            _check(
                "webrtc_guard_addon",
                "Built-in WebRTC & DNS Leak Guard",
                "pass",
                "Native WebRTC & DNS Guard enabled",
                "Enforces disable_non_proxied_udp, blocks RTCPeerConnection/RTCDataChannel via CDP, and prevents mDNS/local-IP leaks.",
            )
        )

    # 9. full_traffic_tunnel
    if binding is None:
        checks.append(
            _check(
                "full_traffic_tunnel",
                "Total-Traffic Per-Browser Proxy Tunnel",
                "fail",
                "Browser has no isolated proxy tunnel",
                "Without a bound node, traffic is not routed through a per-profile fail-closed proxy engine.",
                action="bind_node",
            )
        )
    elif binding["node"].get("status") != "active":
        checks.append(
            _check(
                "full_traffic_tunnel",
                "Total-Traffic Per-Browser Proxy Tunnel",
                "fail",
                "Tunnel target node is retired/invalid",
                "Re-bind to an active node; fail-closed rules refuse direct fallback.",
                action="bind_node",
            )
        )
    else:
        checks.append(
            _check(
                "full_traffic_tunnel",
                "Total-Traffic Per-Browser Proxy Tunnel",
                "pass",
                "Dedicated loopback proxy tunnel + per-partition session lock",
                "Per-window authenticated loopback listener (MATCH,REJECT, zero DIRECT, no global TUN) "
                "enforced on an isolated browser partition with remote-only DNS.",
            )
        )

    # 10. cloud_policy_fresh
    if iso.policy_version != policy.policy_version:
        checks.append(
            _check(
                "cloud_policy_fresh",
                "Cloud Optimization Policy Version",
                "warn",
                f"Profile uses policy {iso.policy_version}; active is {policy.policy_version} ({policy_source})",
                "Apply latest optimization policy to upgrade anti-leak settings without changing hardware.",
                action="reoptimize",
            )
        )
    else:
        checks.append(
            _check(
                "cloud_policy_fresh",
                "Cloud Optimization Policy Version",
                "pass",
                f"Up to date ({policy.policy_version} · {policy_source})",
                "Profile isolation rules match the latest active fixed-optimization policy.",
            )
        )

    # 11. risk_domains_enforced
    domain_count = policy.total_enforced_domains()
    cidr_count = len(policy.risk_probe_cidrs)
    risk_hardening_ok = (
        policy.hardening_prefs.get("network.proxy.share_proxy_settings") is True
        and policy.hardening_prefs.get("network.dns.echconfig.enabled") is False
        and policy.hardening_prefs.get("network.dns.http3_echconfig.enabled") is False
        and type(policy.hardening_prefs.get("security.OCSP.enabled")) is int
        and policy.hardening_prefs.get("security.OCSP.enabled") == 0
        and policy.hardening_prefs.get("security.OCSP.require") is False
    )
    if binding is None or binding["node"].get("status") != "active":
        checks.append(
            _check(
                "risk_domains_enforced",
                "Risk & Verification Domains Enforced",
                "fail",
                f"{domain_count} risk domains & {cidr_count} CIDRs are not tunnel-enforced",
                "Bind an active proxy node so all core, anti-fraud telemetry, and challenge/KYC "
                "domains route exclusively through the fail-closed tunnel.",
                action="bind_node",
            )
        )
    elif not risk_hardening_ok:
        checks.append(
            _check(
                "risk_domains_enforced",
                "Risk & Verification Domains Enforced",
                "fail",
                "ECH/OCSP/full-protocol proxy hardening is incomplete",
                "Re-apply isolation policy to lock full-protocol proxy sharing and disable ECH/OCSP bypasses.",
                action="reoptimize",
            )
        )
    elif domain_count == 0:
        checks.append(
            _check(
                "risk_domains_enforced",
                "Risk & Verification Domains Enforced",
                "warn",
                "No risk verification domains configured in active policy",
                "Sync cloud policy or re-apply the baseline policy to restore the risk domain suite.",
                action="reoptimize",
            )
        )
    else:
        checks.append(
            _check(
                "risk_domains_enforced",
                "Risk & Verification Domains Enforced",
                "pass",
                f"{domain_count} risk domains & {cidr_count} CIDRs enforced via zero-DIRECT tunnel",
                "All core, anti-fraud telemetry, and challenge/KYC domains route exclusively through "
                "the isolated tunnel with ECH disabled, OCSP disabled, and full-protocol proxy lock.",
            )
        )

    points = sum(10 if c["status"] == "pass" else 5 if c["status"] == "warn" else 0 for c in checks)
    score = round(points * 100 / (len(checks) * 10)) if checks else 0
    has_fail = any(c["status"] == "fail" for c in checks)
    if score >= 85 and not has_fail:
        overall: Literal["healthy", "warning", "critical"] = "healthy"
    elif score >= 60:
        overall = "warning"
    else:
        overall = "critical"

    return {
        "profile_id": profile_id,
        "score": score,
        "status": overall,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "checks": checks,
    }
