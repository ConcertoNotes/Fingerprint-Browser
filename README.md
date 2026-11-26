# 棱镜浏览器 (Prism Browser)

[![CI](https://github.com/ConcertoNotes/Fingerprint-Browser/actions/workflows/ci.yml/badge.svg)](https://github.com/ConcertoNotes/Fingerprint-Browser/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

A slim, local-first, all-in-one antidetect isolation browser desktop application —
designed for strict multi-account isolation with zero-setup offline runtime
packaging, map-first dedicated proxy node selection, fixed-optimization hardware
fingerprints, and optional self-hosted cloud delivery.

> **Open-source attribution (MIT):** 棱镜浏览器 (Prism Browser) is a derivative work
> based on [Camoufox Profile Manager](https://github.com/polyackiy/camoufox-profile-manager)
> and built with open-source browser isolation and proxy routing engines. Internal
> package identifiers (`camoufox_pm`, `camoufox-pm`, `CPM_*`) remain backwards-compatible
> so existing local `data/` directories and SQLite databases (v0–v8) migrate cleanly.

---

## Core Capabilities

### 1. All-in-One Offline Desktop Runtime (Zero Setup)

Desktop installers bundle the complete offline runtime inside a single lossless
`zstd`-compressed payload:

- **Paired Isolation Browser Kernel** (`browser-kernel.tar.zst` with full cross-OS
  anti-detection font packs, hardlink deduplication, and `zstd -19 --long=27`)
- **Dedicated Local Proxy Routing Engine** (`bin/mihomo(.exe).zst`, per-window process
  isolation, zero global TUN or shared selectors)
- **Offline IP Geolocation Database** (`geoip/geoip.mmdb.zst`)
- **Bundled Prism WebRTC & DNS Guard Extension** (`prism-webrtc-guard/`) and pinned
  fingerprint model

On first launch, Prism Browser verifies SHA-256 checksums and extracts the local
runtime in 2–5 seconds with **zero network downloads, zero terminal commands, and
zero missing-component warnings**. Subsequent launches open instantly.

### 2. Fixed-Optimization Isolation Engine (`src/camoufox_pm/core/isolation/`)

Every browser window is **one independent, long-lived machine**:

- **Immediate Deduplicated Real-Device Pinning:** Creating, cloning, or rotating
  a window allocates and pins a quality-filtered real device preset (`FP-XXXXXXXX`)
  while avoiding hardware collisions with other local windows.
- **Unified Anti-Leak Defaults:** Windows default to **Los Angeles time
  (`America/Los_Angeles`)** and **English (`["en-US", "en"]`)** so browser
  fingerprint locale and clock stay consistent regardless of exit IP (unless
  explicitly switched to follow-exit mode).
- **Bundled WebRTC & DNS Leak Guard (`prism-webrtc-guard`):** Enforces
  `webRTCIPHandlingPolicy = disable_non_proxied_udp`, disables
  `peerConnectionEnabled`, and disables `networkPredictionEnabled` inside every
  isolated window, while excluding third-party online addons.
- **Full-Traffic Fail-Closed Tunnel & Storage Isolation:**
  - Private `0700` `user_data_dir` + isolated `.runtime/{tmp,cache,config}` per window.
  - Pre-launch `user.js` locks proxy settings (`bypass=""`), disables HTTP/3,
    disables DoH fallback (`network.trr.mode=5`), disables IPv6, and blocks telemetry.
  - Host proxy env vars (`HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, `NO_PROXY`) are
    scrubbed from child browser processes.
- **10-Point Isolation Audit & One-Click Reoptimize:** `POST /api/v1/profiles/{id}/audit`
  scores every window (0–100) across 10 checks; `POST /api/v1/profiles/{id}/reoptimize`
  upgrades anti-leak policy settings without changing pinned hardware.

### 3. Map-First Dedicated Proxy Selection

- **World Map Picker inside Window Creation:** Pick a measured proxy exit directly
  on the interactive world map (with shared-IP and occupied-node warnings), preview
  the deduplicated real device, and atomically create + pin + bind in one step.
- **Per-Window Dedicated Proxy Routing:** Each running bound window spawns its own
  authenticated loopback proxy process (`MATCH,REJECT`, zero `DIRECT`). If a node
  or process fails, connections **fail closed** with zero fallback to direct traffic.
- **Subscriptions & Static IPs:** Manage cloud-delivered subscriptions, local
  subscriptions (YAML / Base64 / share links), and dedicated static IPs with
  measured exit coordinates and IP purity checks.

### 4. Self-Hosted Cloud Delivery Center (`prism-cloud-server`)

Deploy the lightweight companion server (`cloud_server/`) on any VPS or Docker
host to centrally deliver verified isolation policies, managed subscriptions, and
optional client-side encrypted window sync (see [docs/cloud-protocol.md](docs/cloud-protocol.md)):

```bash
PRISM_CLOUD_TOKEN="your-client-token" \
PRISM_CLOUD_ADMIN_TOKEN="your-admin-token" \
uv run prism-cloud-server --host 0.0.0.0 --port 8787
```

---

## Install & Run

### Desktop Installers (macOS & Windows)

| Platform | Artifact |
| --- | --- |
| macOS Apple Silicon (M1+) | `prism-browser-macos-arm64.dmg` |
| macOS Intel | `prism-browser-macos-x86_64.dmg` |
| Windows 10/11 (x64) | `prism-browser-windows-x86_64-setup.exe` |
| Linux (Debian/Ubuntu x64) | `prism-browser-linux-x86_64.deb` |

1. Install and open **棱镜浏览器 (Prism Browser)** (on macOS, drag into Applications).
2. Wait 2–5 seconds on first boot while the bundled offline runtime initializes.
3. Import a subscription, add a static IP, or connect your Cloud Delivery server, then
   click **新建浏览器 (New Browser)**.

> Releases are unsigned under [docs/signing.md](docs/signing.md); macOS/Windows may
> show a standard unsigned-app prompt on first launch.

### From Source (Developers)

Requires [uv](https://docs.astral.sh/uv/) (Python 3.10+) and Node.js 20.9+.

```bash
git clone https://github.com/ConcertoNotes/Fingerprint-Browser.git
cd Fingerprint-Browser
uv sync --extra desktop
uv run python scripts/build_webui.py
uv run python -m camoufox_pm.desktop
```

Or run the local web server directly:

```bash
uv run camoufox-pm              # http://127.0.0.1:8000 (uses data/profiles.db)
uv run camoufox-pm --desktop    # opens native desktop window
```

### Build All-in-One Offline Desktop Installers (macOS / Windows)

```bash
uv sync --extra build
uv run python scripts/prepare_bundled_runtime.py
uv run python scripts/build_desktop.py --package
```

`scripts/prepare_bundled_runtime.py` prepares `.cache/bundle-runtime/<platform>-<arch>/`
with `zstd -19` proxy engine + GeoIP and `zstd -19 --long=27` hardlink-deduplicated
browser kernel. `scripts/build_desktop.py --package` builds the slimmed PyInstaller
app, injects `runtime/`, and packages `prism-browser-macos-<arch>.dmg` (`ULMO` LZMA)
or `prism-browser-windows-<arch>-setup.exe` (`NSIS SOLID LZMA`).

---

## Configuration

The desktop app automatically creates its persistent data directory and `secret.key`.
CLI/server deployments read `CPM_*` environment variables (or `.env`):

| Variable | Default | Description |
| --- | --- | --- |
| `CPM_HOST` | `127.0.0.1` | Server bind host |
| `CPM_PORT` | `8000` | Server bind port |
| `CPM_DB_PATH` | `data/profiles.db` | SQLite database path (desktop uses OS data dir) |
| `CPM_LEASE_TTL` | `120` | Profile lease TTL in seconds (minimum `60`) |
| `CPM_SECRET_KEY` | *(empty)* | Fernet key encrypting proxy, subscription, and cloud secrets at rest |
| `CPM_API_KEY` | *(empty)* | Optional `X-API-Key` header guard for API clients |
| `CPM_CORS_ORIGINS` | `http://localhost:3000` | Comma-separated allowed CORS origins |
| `CPM_WEBUI_DIR` | *(auto)* | Override directory for bundled web UI assets |
| `CPM_BUNDLED_RUNTIME_DIR` | *(auto)* | Override directory for bundled offline runtime |
| `CPM_MIHOMO_PATH` | *(auto)* | Optional custom proxy routing engine binary path |
| `CPM_CLOUD_URL` | *(empty)* | Default Cloud Delivery server HTTPS URL |
| `CPM_CLOUD_TOKEN` | *(empty)* | Default Cloud Delivery Bearer token |
| `CPM_CLOUD_ALLOW_LOCAL` | `0` | Allow `http://127.0.0.1` / loopback cloud server for local dev/tests |

### Multi-Instance Safety & Leases

```bash
camoufox-pm leases               # inspect active profile leases
camoufox-pm unlock <id> [--yes]  # force-release a stuck lease after verifying closure
```

---

## Development & Verification

```bash
uv sync --extra dev
uv run ruff check src cloud_server scripts tests
uv run ruff format --check src cloud_server scripts tests
uv run mypy src/camoufox_pm cloud_server
uv run pytest --no-network -m "not browser"
uv run pytest -m browser

cd web
npm run lint
npm test
npx tsc --noEmit
```

---

## Documentation

| Document | Topic |
| --- | --- |
| [docs/cloud-protocol.md](docs/cloud-protocol.md) | Cloud Delivery wire protocol, policy whitelist, and VPS deployment |
| [docs/network-architecture.md](docs/network-architecture.md) | Subscriptions, dedicated per-window proxy runtimes, exit checks, and world map |
| [docs/network-api.md](docs/network-api.md) | `/api/v1/network` endpoint reference |
| [docs/profile-settings.md](docs/profile-settings.md) | Pinned hardware identity and fingerprint behavior |
| [docs/cookies.md](docs/cookies.md) | JSON & Netscape cookie import/export safety |
| [docs/cli.md](docs/cli.md) | CLI commands and flags |
| [docs/releasing.md](docs/releasing.md) | Building and cutting desktop releases |
| [docs/signing.md](docs/signing.md) | Unsigned release policy and checksums |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Development workflow |
| [SECURITY.md](SECURITY.md) | Security model and reporting |

---

## License

[MIT](LICENSE). Original work © Camoufox Profile Manager Contributors
([polyackiy/camoufox-profile-manager](https://github.com/polyackiy/camoufox-profile-manager));
modifications © ConcertoNotes.
