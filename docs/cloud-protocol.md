# Prism Browser (棱镜浏览器) Cloud Delivery & Sync Protocol (`v1`)

This document specifies the HTTPS JSON protocol used by **Prism Browser (棱镜浏览器)** — powered by its **Embedded Chromium Single-Window Engine** (per-profile isolated storage partitions + Chrome DevTools Protocol (CDP) hardware fingerprint enforcement) and **Independent Proxy Routing Engine** — to pull **fixed-optimization isolation policies**, **tenant-isolated sealed proxy subscriptions**, **entitlement / expiry / device-limit state**, and optional **client-encrypted browser profile snapshots** from the standalone **Prism Cloud Config Server** (`cloud_server/`).

For a step-by-step Chinese VPS + Docker + Caddy deployment and customer packaging guide, see [cloud-deployment-zh.md](./cloud-deployment-zh.md).

---

## 1. Architecture & Security Guarantees

```text
+-----------------------------------+      HTTPS + Admin Session / Token       +------------------------------------+
|  Operator Browser                 | <--------------------------------------> |  Prism Cloud Config Server         |
|  GET /admin (Visual Admin UI)     |  POST /v1/admin/login                    |  (cloud_server/ behind Caddy TLS)  |
+-----------------------------------+  CRUD /v1/admin/tenants, sources, ...    |                                    |
                                                                               |  SQLite WAL (/data/cloud_server.db)|
+-----------------------------------+      HTTPS + Per-Tenant Bearer Token     |   - tenants (token SHA-256, expiry)|
|  Prism Browser Desktop Client     | <--------------------------------------> |   - tenant_devices (revocation)    |
|  - Embedded Chromium + CDP views  |  Headers: X-Prism-Device-Id / Label /    |   - subscription_sources (Fernet)  |
|  - Independent Proxy Router       |           X-Prism-Client-Version         |   - tenant_assignments (filters)   |
|  - Electron safeStorage key guard |  GET  /v1/manifest (ETag + account)      |   - isolation_policies             |
|  - Entitlement & offline guard    |  GET  /v1/isolation-policy (SHA-256)     |   - cloud_profiles (per tenant_id) |
|  - enc2: HKDF+seal_salt node lock |  GET  /v1/subscriptions (sealed_nodes)   |   - audit_logs                     |
|  - Local nodes stay independent   |  GET/PUT/DELETE /v1/profiles (optional)  |                                    |
+-----------------------------------+                                          |  Server-Side Resolver:             |
                                                                               |   Fetches upstream airport URLs    |
                                                                               |   server-side; NEVER sends URLs!   |
                                                                               +------------------------------------+
```

1. **HTTPS + SSRF Protection**: By default, `CloudClient` accepts only `https://` URLs and dials through `GuardedBackend`, which resolves DNS directly and rejects any private, link-local, loopback, or reserved IP address (`127.0.0.0/8`, `10.0.0.0/8`, `169.254.0.0/16`, `::1`, etc.). HTTP redirects are never followed so Bearer tokens cannot be redirected. For local development against `http://127.0.0.1` or `http://localhost`, explicitly set `CPM_CLOUD_ALLOW_LOCAL=1` (or `allow_local: true` in standard edition).
2. **Multi-Tenant Isolation & Server-Side Subscription Resolution**:
   - Each customer receives a unique token (`prism_<32 urlsafe chars>`). The server stores only `SHA-256(token)` + `token_prefix`; the plaintext token is displayed once on creation or rotation.
   - Upstream airport subscription URLs, inline YAML, and dedicated static IPs are encrypted at rest on the server (`PRISM_CLOUD_SECRET_KEY` / `cloud_secret.key`) and resolved **server-side** through the SSRF-guarded parser.
   - Multi-tenant `GET /v1/subscriptions` delivers `kind: "sealed_nodes"` containing sanitized outbound dicts only — upstream subscription URLs and airport tokens never leave the server.
3. **Full-Chain Client Credential Hardening**:
   - **OS Keychain Key Protection**: On macOS (Keychain) and Windows (DPAPI), the Electron shell encrypts the local Fernet key via `safeStorage` into `secret.key.enc` (mode `0600`) and automatically migrates legacy plaintext `secret.key` files.
   - **Two-Layer Cloud Node Sealing (`enc2:`)**: Cloud-managed nodes are stored locally with `entry = ""`, `source_url_enc = NULL`, and `config_enc` sealed with `HKDF-SHA256(local_secret_key, salt=tenant.seal_salt, info=b"prism-cloud-node-v2")`. Local nodes continue using `enc:` unchanged.
   - **API / UI / Runtime Redaction**: Local APIs redact `entry` (`null`) and `protocol` (`"protected"`) for `managed_by = 'cloud'` nodes and reject mutations with `403 cloud_managed_readonly`. Mihomo shreds its runtime `config.yaml` immediately after becoming ready and redacts `server` / `sni` / `host` / credentials from logs.
   - **Customer Edition Hardening**: Packaged `customer` builds embed `resources/distribution/distribution.json` locking `CPM_CLIENT_EDITION=customer` and `CPM_CLOUD_URL`, disabling FastAPI `/docs` and `/redoc`, and blocking main-window DevTools (`devtools-opened` + `F12` / `Cmd+Opt+I` / `Ctrl+Shift+I`).
4. **Integrity Verification (Canonical SHA-256)**: Every `/v1/isolation-policy` and `/v1/subscriptions` response carries a `sha256` digest computed over the canonical UTF-8 JSON representation of the inner payload:
   ```python
   json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
   ```
   If the computed SHA-256 does not match `sha256`, the client rejects the payload and leaves its local `cloud_cache` untouched.
5. **Security Whitelist Validation**: Before any cloud `isolation_policy` is cached or activated, the client validates it through `validate_policy()` in the Isolation Engine. Cloud policies cannot weaken fail-closed proxy enforcement, disable remote DNS resolution, re-enable QUIC/ECH/WebRTC leaks, or inject unsafe settings.
6. **Entitlement, Expiry, Device Limit & Local Node Safety**:
   - Every tenant has `status` (`active` | `disabled`), optional `expires_at`, `max_devices` (default `3`, `0` = unlimited), `offline_grace_hours` (default `24`), and `profiles_sync_enabled` (default `false`).
   - On expiry, disablement, device revocation, or logout, the client automatically closes active browser sessions bound to `managed_by = 'cloud'` nodes and wipes local cloud node credentials — while leaving all user-created local subscriptions (`managed_by = 'local'`) and local browser sessions completely untouched.
7. **Client-Side Encryption for Profile Snapshots**:
   - When a tenant has `profiles_sync_enabled = true`, profile cookies (`encrypted_cookies_blob`) and memos (`encrypted_metadata_blob`) are encrypted client-side with the local Fernet key before upload and strictly scoped by `tenant_id` on the server.
   - Pulling profiles **never overwrites a currently running or leased browser session**.

---

## 2. Authentication, Multi-Tenant Headers & Error Contract

### Client Request Headers

| Header | Required | Purpose |
| --- | --- | --- |
| `Authorization` | Yes (multi-tenant / tokenized) | `Bearer <tenant_token>` (e.g. `Bearer prism_...`) or legacy `PRISM_CLOUD_TOKEN` / `PRISM_CLOUD_ADMIN_TOKEN`. |
| `X-Prism-Device-Id` | Recommended | Stable anonymous UUID4 generated per installation; used for per-tenant `max_devices` accounting and single-device revocation. |
| `X-Prism-Device-Label` | Optional | Human-readable device name shown in `/admin` (e.g. `Alice-MacBook`). |
| `X-Prism-Client-Version` | Optional | Client application version string (e.g. `0.6.0`). |

### Structured `401` / `403` Auth & Entitlement Error Responses

All client auth and entitlement rejections return a consistent JSON body with `code`, `message`, `detail`, and context fields when applicable:

```json
{
  "code": "tenant_expired",
  "message": "授权已于 2026-12-31T00:00:00+00:00 到期，请联系管理员续期。",
  "detail": "tenant_expired",
  "expires_at": "2026-12-31T00:00:00+00:00"
}
```

| HTTP Status | `code` | Extra Fields | Client Behavior |
| --- | --- | --- | --- |
| `401` | `invalid_token` | — | Marks entitlement invalid and revokes local cloud nodes. |
| `403` | `tenant_disabled` | — | Persists `disabled` state and revokes local cloud nodes. |
| `403` | `tenant_expired` | `expires_at` | Persists `expired` state and revokes local cloud nodes. |
| `403` | `device_revoked` | — | Persists `revoked` state and revokes local cloud nodes. |
| `403` | `device_limit_exceeded` | `max_devices`, `active_devices` | Persists `device_limit` state and prompts user/admin to free a slot. |
| `403` | `profiles_sync_disabled` | — | Returned on `/v1/profiles*` when `tenant.profiles_sync_enabled` is `false`. |

### Admin Authentication

Admin endpoints (`/v1/admin/*`) accept either:
- **Bearer token**: `Authorization: Bearer <PRISM_CLOUD_ADMIN_TOKEN>` (for scripts/automation), or
- **Signed session cookie**: Issued by `POST /v1/admin/login` with `HttpOnly; SameSite=Strict` (used by the visual `/admin` panel, with built-in in-memory rate limiting against brute force).

### Backward Compatibility (Single-Tenant / Local Dev)

- When `PRISM_CLOUD_TOKEN` (or legacy `LULU_CLOUD_TOKEN`) is set, requests using that token map to the built-in `tenant_id = "default"` tenant (`profiles_sync_enabled = true`, `expires_at = null`, `max_devices = 0`), preserving legacy `PUT /v1/admin/subscriptions` and `PUT /v1/admin/isolation-policy` flows.
- When neither `PRISM_CLOUD_TOKEN` nor `PRISM_CLOUD_ADMIN_TOKEN` is set (and no tenants exist), `cloud_server` operates in unauthenticated local dev mode. Always set `PRISM_CLOUD_ADMIN_TOKEN` and `PRISM_CLOUD_SECRET_KEY` in production.

### ETag Conditional Requests

`GET /v1/manifest`, `GET /v1/isolation-policy`, and `GET /v1/subscriptions` return `ETag: "<32-hex>"` and `Cache-Control: no-cache`. In multi-tenant mode, `GET /v1/manifest` and `GET /v1/subscriptions` compute ETags strictly per tenant (incorporating that tenant's assignments and entitlement metadata). Clients send `If-None-Match: "<etag>"` on startup and 15-minute background heartbeats and receive `304 Not Modified` when unchanged.

---

## 3. Wire Protocol Reference

### 3.1 `GET /health`

Unauthenticated liveness probe (used by Docker `HEALTHCHECK`).

```json
{
  "status": "ok",
  "service": "prism-cloud-server",
  "protocol_version": 1
}
```

### 3.2 `GET /v1/manifest`

Returns per-tenant artifact versions, SHA-256 digests, profile sync availability, and the `account` entitlement object.

```json
{
  "protocol_version": 1,
  "server_time": "2026-11-26T00:00:00+00:00",
  "isolation_policy": {
    "version": "prism-cloud-2026.11.26-r3",
    "sha256": "a3f0...91c2",
    "updated_at": "2026-11-26T00:00:00+00:00"
  },
  "subscriptions": {
    "version": "tenant-subs-4",
    "sha256": "9b12...44e0",
    "updated_at": "2026-11-26T00:00:00+00:00",
    "count": 2
  },
  "profiles_sync_enabled": false,
  "profiles_count": 0,
  "account": {
    "tenant_id": "t_8f3a91bc",
    "tenant_name": "Acme Commerce Team",
    "status": "active",
    "expires_at": "2026-12-26T00:00:00+00:00",
    "server_time": "2026-11-26T00:00:00+00:00",
    "max_devices": 3,
    "active_devices": 1,
    "offline_grace_hours": 24,
    "profiles_sync_enabled": false,
    "seal_salt": "7f3a...e910"
  }
}
```

- `account.seal_salt` is a per-tenant random hex salt used by the client to derive `enc2:` HKDF keys for cloud-managed nodes; rotating a tenant's token rotates `seal_salt`.
- Top-level `profiles_sync_enabled` mirrors `account.profiles_sync_enabled`.

### 3.3 `GET /v1/isolation-policy`

Returns the active fixed-optimization isolation policy governing Prism Browser's **Embedded Chromium + CDP fingerprint engine** and **Independent Proxy Routing Engine**, including mandatory anti-leak switches (`ECH` / `OCSP` / `share_proxy_settings` / `TRR` / `HTTP3` / `IPv6` / `Battery API`) and the cloud-deliverable **Risk & Verification Domain Suite** (`risk_probe_groups`, `risk_probe_cidrs`, `risk_probe_targets`). `sha256` is the canonical SHA-256 of the `policy` object.

- **Fixed defaults**: `default_timezone_mode: "fixed"`, `default_timezone: "America/Los_Angeles"`, `default_locale_mode: "fixed"`, `default_languages: ["en-US", "en"]`, `default_webrtc_policy: "block"`, `default_canvas_policy: "stable_hardware"`, `install_webrtc_guard: true`.
- **`hardening_prefs` anti-leak contract**:
  - `network.proxy.share_proxy_settings: true`, `network.proxy.socks_remote_dns: true`, `network.proxy.no_proxies_on: ""`, `network.proxy.failover_direct: false`: enforces full-protocol authenticated loopback proxy routing (`<-loopback>` bypass rules) with zero direct fallback.
  - `network.dns.echconfig.enabled: false`, `network.dns.http3_echconfig.enabled: false`, `network.trr.mode: 5`, `network.dns.disableIPv6: true`, `network.http.http3.enable: false`: enforces remote-only DNS (no AsyncDns/DoH), disables QUIC/HTTP3 UDP leaks, and blocks ECH / IPv6 leakage.
  - `security.OCSP.enabled: 0`, `security.OCSP.require: false`, `dom.battery.enabled: false`: suppresses out-of-band OCSP probes and disables battery API fingerprinting via CDP main-world injection.
- **Cloud-deliverable Risk & Verification Domain Suite**:
  - `risk_probe_groups`: categorized domain inventory (`core`, `antifraud_telemetry`, `challenge_kyc`, 38 domains total) enforced under the zero-DIRECT fail-closed proxy tunnel and audited by `risk_domains_enforced`.
  - `risk_probe_cidrs`: 4 IP CIDR ranges (`34.8.0.0/16`, `160.79.104.0/21`, `153.61.208.0/20`, `153.61.224.0/22`) enforced through the same tunnel.
  - `risk_probe_targets`: 10 representative `{ "domain", "category" }` live-probe targets exercised by `POST /api/v1/profiles/{id}/probe-risk-domains` and `POST /api/v1/network/nodes/{id}/probe-domains`.

```json
{
  "version": "prism-cloud-2026.11.26-r3",
  "sha256": "a3f0...91c2",
  "updated_at": "2026-11-26T00:00:00+00:00",
  "policy": {
    "schema_version": 1,
    "policy_version": "prism-cloud-2026.11.26-r3",
    "default_tier": "strict",
    "default_webrtc_policy": "block",
    "default_canvas_policy": "stable_hardware",
    "default_locale_mode": "fixed",
    "default_languages": ["en-US", "en"],
    "default_timezone_mode": "fixed",
    "default_timezone": "America/Los_Angeles",
    "install_webrtc_guard": true,
    "hardening_prefs": {
      "network.trr.mode": 5,
      "network.dns.disableIPv6": true,
      "network.dns.echconfig.enabled": false,
      "network.dns.http3_echconfig.enabled": false,
      "network.http.http3.enable": false,
      "network.proxy.socks_remote_dns": true,
      "network.proxy.no_proxies_on": "",
      "network.proxy.failover_direct": false,
      "network.proxy.share_proxy_settings": true,
      "security.OCSP.enabled": 0,
      "security.OCSP.require": false,
      "dom.battery.enabled": false
    },
    "preset_filters": {
      "min_width": 1280,
      "min_height": 720,
      "min_cores": 4,
      "min_chrome_major": 125,
      "blocked_renderers": ["SwiftShader", "llvmpipe", "Microsoft Basic Render Driver"]
    },
    "curated_presets": [],
    "risk_probe_groups": {
      "core": ["claude.ai", "anthropic.com", "anthropic.auth0.com"],
      "antifraud_telemetry": ["intercom.io", "siftscience.com", "r.stripe.com", "us5.datadoghq.com"],
      "challenge_kyc": ["challenges.cloudflare.com", "hcaptcha.com", "withpersona.com", "yoti.com"]
    },
    "risk_probe_cidrs": [
      "34.8.0.0/16",
      "160.79.104.0/21",
      "153.61.208.0/20",
      "153.61.224.0/22"
    ],
    "risk_probe_targets": [
      {"domain": "claude.ai", "category": "core"},
      {"domain": "challenges.cloudflare.com", "category": "challenge_kyc"}
    ]
  }
}
```

### 3.4 `GET /v1/subscriptions`

Returns the calling tenant's assigned cloud-managed proxy subscription sources. `sha256` is the canonical SHA-256 of the `subscriptions` array.

Supported item `kind` values:
- `"sealed_nodes"` (**default for multi-tenant delivery**): Server-side resolved, filtered, and sanitized outbound dicts (`nodes` required). Upstream airport URLs and provider credentials never leave the server. The client stores these with `managed_by = 'cloud'`, `source_url_enc = NULL`, `entry = ""`, and `enc2:` double-encrypted config.
- `"url"` (legacy single-tenant): Remote HTTPS proxy subscription URL (`url` required).
- `"content"` (legacy single-tenant): Inline YAML / Base64 / share-link text (`content` required).
- `"manual"` (legacy single-tenant): List of static proxy lines or share links (`manual_nodes` required).

```json
{
  "version": "tenant-subs-4",
  "sha256": "9b12...44e0",
  "updated_at": "2026-11-26T00:00:00+00:00",
  "subscriptions": [
    {
      "cloud_source_id": "src_us_pool",
      "name": "US Dedicated Pool",
      "kind": "sealed_nodes",
      "nodes": [
        {
          "name": "US-LA-01",
          "type": "socks5",
          "server": "203.0.113.10",
          "port": 1080,
          "username": "u_acme",
          "password": "p_secret"
        }
      ],
      "auto_sync": true,
      "enabled": true
    }
  ]
}
```

### 3.5 Browser Profile Snapshots (Per-Tenant Isolated)

Available only when `tenant.profiles_sync_enabled` is `true` (otherwise returns `403 profiles_sync_disabled`). All reads and writes are strictly scoped to the authenticated `tenant_id`.

- `GET /v1/profiles` — Lists all snapshots belonging to the authenticated tenant: `{"profiles": [...], "count": N}`.
- `GET /v1/profiles/{id}` — Fetches one snapshot belonging to the authenticated tenant (or `404`).
- `PUT /v1/profiles/{id}` — Creates or updates a snapshot for the authenticated tenant, incrementing `revision` server-side.
- `DELETE /v1/profiles/{id}` — Deletes a snapshot belonging to the authenticated tenant (`{"deleted": true, "id": "..."}` or `404`).

Snapshot body (preserving the pinned Chromium hardware preset, deterministic Canvas/WebGL/Audio seeds, and cloud node binding):

```json
{
  "id": "p8xk29ab",
  "name": "US Shop Account 01",
  "group": "Commerce",
  "tags": ["us", "tier-1"],
  "notes": null,
  "status": "active",
  "isolation_config": {
    "isolation_version": 1,
    "policy_version": "prism-cloud-2026.11.26-r3",
    "tier": "strict",
    "os": "macos",
    "preset_id": "macos:apple-m3-pro-14",
    "webrtc_policy": "block",
    "canvas_policy": "stable_hardware",
    "locale_mode": "fixed",
    "languages": ["en-US", "en"],
    "timezone_mode": "fixed",
    "timezone": "America/Los_Angeles",
    "install_webrtc_guard": true,
    "window_size": [1366, 860],
    "pinned_fingerprint": {
      "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
      "platform": "MacIntel",
      "screen_width": 1512,
      "screen_height": 982,
      "hardware_concurrency": 12,
      "device_memory": 8,
      "webgl_vendor": "Google Inc. (Apple)",
      "webgl_renderer": "ANGLE (Apple, ANGLE Metal Renderer: Apple M3 Pro, Unspecified Version)",
      "canvas_seed": "9c3e...",
      "audio_seed": "4a7f...",
      "webgl_seed": "e18b..."
    },
    "fingerprint_hash": "FP-A1B2C3D4"
  },
  "fingerprint_hash": "FP-A1B2C3D4",
  "bound_subscription_cloud_id": "src_us_pool",
  "bound_node_key": "5f9d12ab...",
  "encrypted_cookies_blob": "enc:gAAAAAB...",
  "encrypted_metadata_blob": "enc:gAAAAAB...",
  "revision": 2,
  "source_device": "macbook-pro",
  "updated_at": "2026-11-26T00:05:00+00:00"
}
```

When another device of the same tenant pulls this snapshot, Prism Browser matches `(bound_subscription_cloud_id, bound_node_key)` against its local `network_subscriptions` + `network_nodes` and automatically restores the node binding while preserving the exact CDP hardware identity and Canvas/WebGL/Audio seeds.

### 3.6 Admin REST API & Visual `/admin` Panel

The server ships with a zero-CDN, Chinese visual management web UI at **`GET /admin`** (served with `Cache-Control: no-store` and a strict CSP header), backed by the following admin endpoints (all requiring `Authorization: Bearer <PRISM_CLOUD_ADMIN_TOKEN>` or the signed admin session cookie):

| Method & Path | Purpose |
| --- | --- |
| `GET /admin` | Visual Chinese admin console (login, overview, tenants, upstream pools, assignments, policy, audit logs). |
| `POST /v1/admin/login` | Exchange `admin_token` for an `HttpOnly; SameSite=Strict` signed session cookie (rate-limited). |
| `POST /v1/admin/logout` | Clear admin session cookie. |
| `GET /v1/admin/session` | Check whether current request has an authenticated admin session. |
| `GET /v1/admin/overview` | Dashboard summary counters (total/active/expiring/expired tenants, active devices, upstream sources). |
| `GET /v1/admin/tenants` | List tenants (with status, expiry, device counts, assigned source counts — never plaintext tokens). |
| `POST /v1/admin/tenants` | Create a tenant; returns one-time plaintext `token` (`prism_...`) once. |
| `GET /v1/admin/tenants/{id}` | Fetch tenant details, assignments, and device list. |
| `PATCH /v1/admin/tenants/{id}` | Update `name`, `notes`, `status`, `expires_at`, `max_devices`, `offline_grace_hours`, `profiles_sync_enabled`. |
| `DELETE /v1/admin/tenants/{id}` | Delete a tenant and its assignments, private sources, devices, and snapshots. |
| `POST /v1/admin/tenants/{id}/rotate-token` | Invalidate previous token, rotate `seal_salt`, and return a new one-time `token`. |
| `GET /v1/admin/tenants/{id}/devices` | List bound devices (`device_id`, `device_label`, `client_version`, `last_seen_at`, `revoked`). |
| `DELETE /v1/admin/tenants/{id}/devices/{device_id}` | Revoke/kick a device and free its device slot. |
| `GET /v1/admin/sources` | List upstream subscription sources (`shared` and `tenant_private`, payloads never leaked in plaintext). |
| `POST /v1/admin/sources` | Add an upstream source (`url`, `content`, or `manual`) encrypted at rest with Fernet. |
| `PATCH /v1/admin/sources/{id}` | Update source metadata, payload, or `enabled` flag. |
| `DELETE /v1/admin/sources/{id}` | Remove an upstream source and detach tenant assignments. |
| `POST /v1/admin/sources/{id}/refresh` | Trigger server-side SSRF-guarded fetch/parse and update cached resolved nodes & `node_count`. |
| `PUT /v1/admin/tenants/{id}/assignments` | Set shared source assignments for a tenant, including optional `display_name_override` and `node_filter_json` (`{"include_keywords": [...], "exclude_keywords": [...], "node_keys": [...]}`). |
| `POST /v1/admin/tenants/{id}/private-nodes` | Upsert dedicated `tenant_private` manual/static IP nodes exclusively for one tenant. |
| `GET /v1/admin/audit-logs` | Inspect recent admin mutations and client entitlement/device rejection events (pruned to last 2,000 rows). |
| `GET /v1/admin/isolation-policy` | Read current global isolation policy. |
| `PUT /v1/admin/isolation-policy` | Publish an updated global isolation policy. |
| `PUT /v1/admin/subscriptions` | Legacy single-tenant subscription update (writes to `default` tenant for backward compatibility). |

---

## 4. Local Backend Sidecar REST API (`/api/v1/cloud/*`) & `distribution.json`

All local sidecar responses use the standard `{ "success": true, "message": "...", "data": ... }` envelope with `Cache-Control: no-store`. In `customer` edition, FastAPI Swagger (`/docs`) and ReDoc (`/redoc`) are disabled.

| Method & Path | Purpose |
| --- | --- |
| `GET /api/v1/cloud/status` | Edition (`standard` / `customer`), `cloud_url_locked`, `entitlement_status`, `tenant_name`, `expires_at`, `days_remaining`, `max_devices`, `active_devices`, `profiles_sync_allowed`, policy version, cloud subscription count, last sync state (never exposes tokens; masks `base_url` in `customer` edition). |
| `PUT /api/v1/cloud/config` | Configure `base_url`, write-only `token` (or `clear_token: true`), `auto_sync_on_startup`, `sync_profiles_enabled`, `device_label`, `allow_local` (`base_url` and `allow_local` mutations return `403` in `customer` edition). |
| `POST /api/v1/cloud/activate` | Save activation token (`{"token": "prism_..."}`), trigger immediate sync, and return `{ "status": ..., "report": ... }`. |
| `POST /api/v1/cloud/logout` | Clear stored token, revoke local cloud nodes & active cloud-node sessions (preserving local nodes), and return updated status. |
| `POST /api/v1/cloud/sync` | Run sync for optional `scopes` (`["policy", "subscriptions", "profiles"]` or `["all"]`), optional `force`, `include_cookies`. |
| `GET /api/v1/cloud/policy` | Return the currently active `IsolationPolicy` (`source`: `"cloud"` or `"builtin"`), SHA-256, and rule summary. |
| `POST /api/v1/cloud/profiles/{id}/push` | Push one profile snapshot (`{ "include_cookies": false }`). |
| `POST /api/v1/cloud/profiles/pull` | Pull remote profiles (`{ "include_cookies": false, "force": false }`), skipping running sessions and re-binding local nodes. |

### Desktop Packaging `distribution.json` Contract

When packaging the desktop app via `scripts/build_desktop.py`, a `build/distribution/distribution.json` file is generated and bundled into `resources/distribution/distribution.json`:

```json
{
  "edition": "customer",
  "cloud_url": "https://cloud.example.com",
  "brand_name": "棱镜浏览器",
  "require_activation": false
}
```

On startup, `electron/sidecar.js` loads `distribution.json` (or `PRISM_DISTRIBUTION_FILE` during testing) and injects `CPM_CLIENT_EDITION`, `CPM_CLOUD_URL`, `CPM_BRAND_NAME`, and `CPM_REQUIRE_ACTIVATION` into the Python sidecar.

---

## 5. Running & Deploying `cloud_server`

### Production Deployment (Docker Compose + Caddy Automatic HTTPS)

See the complete Chinese walkthrough in [cloud-deployment-zh.md](./cloud-deployment-zh.md). Quick start on any Linux VPS with Docker:

```bash
cd cloud_server
cp .env.example .env && chmod 600 .env
cp Caddyfile.example Caddyfile
# Edit .env: set PRISM_CLOUD_DOMAIN, PRISM_CLOUD_ADMIN_TOKEN, PRISM_CLOUD_SECRET_KEY
mkdir -p cloud_data && chown -R 1000:1000 cloud_data
docker compose up -d
```

Then open `https://<PRISM_CLOUD_DOMAIN>/admin` in your browser.

### Local Development

1. Start the standalone Prism Cloud Config Server (auto-seeds `cloud_server/seed/isolation_policy.json` on first boot; `lulu-cloud-server` also remains available as a compatibility alias):
   ```bash
   PRISM_CLOUD_ADMIN_TOKEN="dev-admin-token" \
   PRISM_CLOUD_TOKEN="dev-client-token" \
   uv run prism-cloud-server --host 127.0.0.1 --port 8787 --db data/cloud_server.db
   ```
2. Allow local HTTP on the Prism backend sidecar during local development:
   ```bash
   CPM_CLOUD_ALLOW_LOCAL=1 uv run python -m camoufox_pm.sidecar
   ```
3. Open `http://127.0.0.1:8787/admin` to manage tenants and nodes, or connect from Prism Browser's **Cloud Delivery Center** (`/cloud/`).

### Environment Variables (`prism-cloud-server`)

Legacy `LULU_CLOUD_*` names remain supported as fallbacks where applicable:

| Variable | Legacy Fallback | Default | Description |
| --- | --- | --- | --- |
| `PRISM_CLOUD_HOST` | `LULU_CLOUD_HOST` | `127.0.0.1` | Bind host (`--host`) |
| `PRISM_CLOUD_PORT` | `LULU_CLOUD_PORT` | `8787` | Bind port (`--port`) |
| `PRISM_CLOUD_DB` | `LULU_CLOUD_DB` | `data/cloud_server.db` | SQLite file path (`--db`) |
| `PRISM_CLOUD_ADMIN_TOKEN` | `LULU_CLOUD_ADMIN_TOKEN` | _(unset)_ | Admin login / Bearer token (`--admin-token`) |
| `PRISM_CLOUD_SECRET_KEY` | — | _(auto-generated `cloud_secret.key`)_ | 44-char Fernet key encrypting upstream sources, resolved nodes, and admin sessions |
| `PRISM_CLOUD_TOKEN` | `LULU_CLOUD_TOKEN` | _(unset)_ | Optional legacy single-tenant Bearer token (`--token`); leave unset in multi-tenant production |
| `PRISM_CLOUD_DOMAIN` | — | _(Caddy only)_ | Public domain name used by `cloud_server/Caddyfile` for automatic Let's Encrypt HTTPS |
