"use strict";

/**
 * Background ("seamless") updates for the desktop shell.
 *
 * Flow: check BOTH local `dist/` (when present on a developer machine) and the
 * remote release feed -> pick whichever source has the newest version ->
 * download/copy the installer in the background -> verify size + SHA-256 ->
 * stage and install when the app quits (or when the user chooses "restart now").
 * User data is never touched: it lives in the stable userData directory
 * (see data-dir.js), outside the app bundle, and the next start snapshots the
 * database before the new version opens it.
 *
 * macOS builds are not Developer-ID signed, so Squirrel.Mac cannot be used;
 * the staged .app is swapped in by a small script after the app has exited.
 * Windows uses the NSIS installer's silent mode. Linux is left to the package
 * manager.
 */

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const { spawn, execFileSync } = require("node:child_process");
const { pipeline } = require("node:stream/promises");

const DEFAULT_FEED_REPO = "https://github.com/ConcertoNotes/Fingerprint-Browser/releases/download";
const CHANNEL_TAGS = Object.freeze({ darwin: "macos-latest", win32: "windows-latest" });
const FIRST_CHECK_DELAY_MS = 6 * 1000;
const CHECK_INTERVAL_MS = 4 * 60 * 60 * 1000;
const LOCAL_CHECK_INTERVAL_MS = 15 * 1000;
const MANIFEST_TIMEOUT_MS = 15 * 1000;
const DOWNLOAD_IDLE_TIMEOUT_MS = 45 * 1000;
const MAX_MANIFEST_BYTES = 64 * 1024;
const MAX_LOG_BYTES = 128 * 1024;
const LOCAL_SOURCE_FILE = "local-update-source.json";
const INSTALL_ERROR_FILE = "last-install-error.txt";
const UPDATER_LOG_FILE = "updater.log";
const SWAP_LOG_FILE = "apply-update.log";

function parseVersion(raw) {
  const match = /^v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$/.exec(String(raw || "").trim());
  return match ? match.slice(1, 4).map(Number) : null;
}

/** >0 when a is newer than b; null when either is not x.y.z. */
function compareVersions(a, b) {
  const pa = parseVersion(a);
  const pb = parseVersion(b);
  if (!pa || !pb) {
    return null;
  }
  for (let i = 0; i < 3; i += 1) {
    if (pa[i] !== pb[i]) {
      return pa[i] - pb[i];
    }
  }
  return 0;
}

function parseTimestampMs(raw) {
  if (!raw || typeof raw !== "string") {
    return 0;
  }
  const ms = Date.parse(raw);
  return Number.isFinite(ms) ? ms : 0;
}

/**
 * Compare two validated update candidates `{ source: "local" | "remote", manifest }`.
 * Returns >0 if `a` should be preferred over `b`.
 * Tie-breaker order:
 *   1) Higher semantic version (`x.y.z`)
 *   2) Newer `releasedAt` timestamp
 *   3) Local `dist/` over remote (instant copy, zero network wait)
 */
function compareCandidates(a, b) {
  const verCmp = compareVersions(a.manifest.version, b.manifest.version);
  if (verCmp !== null && verCmp !== 0) {
    return verCmp;
  }
  const tsDiff = parseTimestampMs(a.manifest.releasedAt) - parseTimestampMs(b.manifest.releasedAt);
  if (tsDiff !== 0) {
    return tsDiff;
  }
  if (a.source !== b.source) {
    return a.source === "local" ? 1 : -1;
  }
  return 0;
}

/** Pick the highest candidate strictly newer than `baselineVersion`, or null. */
function pickLatestCandidate(candidates, baselineVersion) {
  const eligible = (candidates || []).filter(
    (c) => c && c.manifest && compareVersions(c.manifest.version, baselineVersion) > 0
  );
  if (eligible.length === 0) {
    return null;
  }
  eligible.sort((a, b) => compareCandidates(b, a));
  return eligible[0];
}

function manifestName(platform = process.platform, arch = process.arch) {
  return `prism-update-${platform}-${arch}.json`;
}

function defaultFeedUrl(platform = process.platform) {
  const tag = CHANNEL_TAGS[platform];
  return tag ? `${DEFAULT_FEED_REPO}/${tag}` : null;
}

function validateManifest(raw, { platform = process.platform } = {}) {
  if (!raw || typeof raw !== "object") {
    throw new Error("Update manifest is not an object");
  }
  const version = String(raw.version || "");
  if (!parseVersion(version)) {
    throw new Error(`Update manifest has an invalid version: ${version}`);
  }
  const file = String(raw.file || "");
  if (!file || file !== path.basename(file) || file.startsWith(".")) {
    throw new Error("Update manifest has an invalid file name");
  }
  const expectedExt = platform === "darwin" ? ".dmg" : platform === "win32" ? ".exe" : null;
  if (expectedExt && !file.toLowerCase().endsWith(expectedExt)) {
    throw new Error(`Update file must be a ${expectedExt}`);
  }
  const sha256 = String(raw.sha256 || "").toLowerCase();
  if (!/^[0-9a-f]{64}$/.test(sha256)) {
    throw new Error("Update manifest has an invalid sha256");
  }
  const size = Number(raw.size);
  if (!Number.isSafeInteger(size) || size <= 0) {
    throw new Error("Update manifest has an invalid size");
  }
  return {
    version,
    file,
    sha256,
    size,
    notes: typeof raw.notes === "string" ? raw.notes.slice(0, 2000) : "",
    releasedAt: typeof raw.released_at === "string" ? raw.released_at : "",
  };
}

/** Bundle path for the running macOS app (…/X.app). */
function macBundlePath(execPath = process.execPath) {
  return path.resolve(execPath, "..", "..", "..");
}

function isProxyConfigured(proxySpec) {
  const text = String(proxySpec || "").trim().toUpperCase();
  if (!text) {
    return false;
  }
  return text.split(";").some((entry) => {
    const trimmed = entry.trim();
    return Boolean(trimmed) && trimmed !== "DIRECT";
  });
}

function sourceLabel(source) {
  return source === "local" ? "本机 dist" : "远端 GitHub";
}

function buildMacSwapScript() {
  // Args: <pid> <targetApp> <stagedApp> <relaunch 0|1> [errorFile] [logFile]
  return `#!/bin/bash
set -u
PID="$1"; TARGET="$2"; STAGED="$3"; RELAUNCH="$4"
ERR_FILE="\${5:-}"
LOG_FILE="\${6:-}"
log() {
  if [ -n "$LOG_FILE" ]; then
    printf '[%s] %s\\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" >> "$LOG_FILE" 2>/dev/null || true
  fi
}
fail() {
  log "ERROR: $1"
  if [ -n "$ERR_FILE" ]; then
    printf '%s\\n' "$1" > "$ERR_FILE" 2>/dev/null || true
  fi
  if [ "$RELAUNCH" = "1" ] && [ -d "$TARGET" ]; then
    open "$TARGET" 2>/dev/null || true
  fi
  exit 1
}

log "Waiting for PID $PID to exit before replacing $TARGET"
WAITED=0
for _ in $(seq 1 600); do
  kill -0 "$PID" 2>/dev/null || break
  sleep 0.5
  WAITED=1
done
if kill -0 "$PID" 2>/dev/null; then
  fail "旧版本进程 ($PID) 长时间未退出，已取消替换。"
fi
if [ "$WAITED" = "1" ]; then
  sleep 0.5
fi

if [ ! -d "$STAGED" ]; then
  fail "未找到已准备好的新版本应用目录：$STAGED"
fi

BACKUP="$TARGET.prism-update-old"
rm -rf "$BACKUP" 2>/dev/null || true
if ! mv "$TARGET" "$BACKUP" 2>/dev/null; then
  fail "无法移动旧版本应用（可能缺少目录写入权限或应用被占用）：$TARGET"
fi

if mv "$STAGED" "$TARGET" 2>/dev/null || ditto "$STAGED" "$TARGET" 2>/dev/null; then
  rm -rf "$BACKUP" 2>/dev/null || true
  rm -rf "$STAGED" 2>/dev/null || true
  if [ -n "$ERR_FILE" ]; then
    rm -f "$ERR_FILE" 2>/dev/null || true
  fi
  xattr -dr com.apple.quarantine "$TARGET" 2>/dev/null || true
  log "Replaced $TARGET successfully"
else
  rm -rf "$TARGET" 2>/dev/null || true
  mv "$BACKUP" "$TARGET" 2>/dev/null || true
  fail "写入新版本应用失败，已自动回滚到旧版本：$TARGET"
fi

if [ "$RELAUNCH" = "1" ]; then
  log "Relaunching $TARGET"
  open "$TARGET" 2>/dev/null || true
fi
exit 0
`;
}

async function writeResponseBodyToFile(
  res,
  destPath,
  { expectedSize = 0, idleTimeoutMs = DOWNLOAD_IDLE_TIMEOUT_MS, onProgress = null } = {}
) {
  const out = fs.createWriteStream(destPath, { mode: 0o600 });
  let downloaded = 0;

  const waitWritable = () =>
    new Promise((resolve, reject) => {
      const cleanup = () => {
        out.off("drain", onDrain);
        out.off("error", onErr);
      };
      const onDrain = () => {
        cleanup();
        resolve();
      };
      const onErr = (err) => {
        cleanup();
        reject(err);
      };
      out.once("drain", onDrain);
      out.once("error", onErr);
    });

  const closeStream = () =>
    new Promise((resolve, reject) => {
      out.end((err) => (err ? reject(err) : resolve()));
    });

  try {
    if (res.body && typeof res.body.getReader === "function") {
      const reader = res.body.getReader();
      try {
        while (true) {
          let timer = null;
          let step;
          try {
            step = await Promise.race([
              reader.read(),
              new Promise((_, reject) => {
                timer = setTimeout(
                  () => reject(new Error(`Download stalled for ${Math.round(idleTimeoutMs / 1000)}s`)),
                  idleTimeoutMs
                );
                timer.unref?.();
              }),
            ]);
          } finally {
            if (timer) {
              clearTimeout(timer);
            }
          }
          if (step.done) {
            break;
          }
          const chunk = Buffer.from(step.value);
          downloaded += chunk.length;
          if (!out.write(chunk)) {
            await waitWritable();
          }
          if (onProgress) {
            onProgress(downloaded, expectedSize);
          }
        }
      } finally {
        try {
          await reader.cancel();
        } catch {
          // ignore
        }
      }
      await closeStream();
      return;
    }

    if (res.body && typeof res.body[Symbol.asyncIterator] === "function") {
      for await (const rawChunk of res.body) {
        const chunk = Buffer.isBuffer(rawChunk) ? rawChunk : Buffer.from(rawChunk);
        downloaded += chunk.length;
        if (!out.write(chunk)) {
          await waitWritable();
        }
        if (onProgress) {
          onProgress(downloaded, expectedSize);
        }
      }
      await closeStream();
      return;
    }

    const buf = Buffer.from(await res.arrayBuffer());
    downloaded = buf.length;
    if (!out.write(buf)) {
      await waitWritable();
    }
    if (onProgress) {
      onProgress(downloaded, expectedSize);
    }
    await closeStream();
  } catch (err) {
    await new Promise((resolve) => {
      out.once("close", resolve);
      out.destroy();
    });
    throw err;
  }
}

async function copyFileWithProgress(srcPath, destPath, { expectedSize = 0, onProgress = null } = {}) {
  const input = fs.createReadStream(srcPath);
  const output = fs.createWriteStream(destPath, { mode: 0o600 });
  let copied = 0;
  input.on("data", (chunk) => {
    copied += chunk.length;
    if (onProgress) {
      onProgress(copied, expectedSize);
    }
  });
  await pipeline(input, output);
}

class UpdateManager {
  constructor({
    app,
    dialog = null,
    getWindow = () => null,
    userDataDir,
    brandName = "",
    feedUrl = undefined,
    localDistDir = undefined,
    repoRoot = null,
    enabled = true,
    fetchImpl = globalThis.fetch,
    netFetch = null,
    resolveProxy = null,
    platform = process.platform,
    arch = process.arch,
    log = () => {},
  }) {
    this.app = app;
    this.dialog = dialog;
    this.getWindow = getWindow;
    this.userDataDir = userDataDir;
    this.brandName = brandName;
    this.updatesDir = path.join(userDataDir, "updates");
    this.localSourceFile = path.join(userDataDir, LOCAL_SOURCE_FILE);
    this.installErrorFile = path.join(this.updatesDir, INSTALL_ERROR_FILE);
    this.logFile = path.join(this.updatesDir, UPDATER_LOG_FILE);
    this.swapLogFile = path.join(this.updatesDir, SWAP_LOG_FILE);
    this.platform = platform;
    this.arch = arch;
    this.feedUrl = (feedUrl === undefined ? defaultFeedUrl(this.platform) : feedUrl) || null;
    this.localDistDirOption = localDistDir; // undefined => auto-discover, null => disabled, string => fixed dir
    this.repoRoot = repoRoot;
    this.fetch = fetchImpl;
    this.netFetch = typeof netFetch === "function" ? netFetch : null;
    this.resolveProxy = typeof resolveProxy === "function" ? resolveProxy : null;
    this._externalLog = log;
    this.enabled = Boolean(
      enabled &&
        CHANNEL_TAGS[this.platform] &&
        (Boolean(this.feedUrl && (this.fetch || this.netFetch)) || this.localDistDirOption !== null)
    );
    this.state = "idle"; // idle | checking | downloading | ready | error
    this.ready = null; // { version, source, installerPath, stagedApp, notes, releasedAt }
    this.activeVersion = null;
    this.downloadProgress = null; // 0..1 while transferring
    this.timer = null;
    this.localTimer = null;
    this.firstCheckTimer = null;
    this.promptedVersion = null;
    this.installing = false;
  }

  _log(message) {
    const line = String(message || "");
    try {
      this._externalLog(line);
    } catch {
      // ignore
    }
    try {
      fs.mkdirSync(this.updatesDir, { recursive: true, mode: 0o700 });
      try {
        const st = fs.statSync(this.logFile);
        if (st.size > MAX_LOG_BYTES) {
          const tail = fs.readFileSync(this.logFile, "utf8").slice(-Math.floor(MAX_LOG_BYTES / 2));
          fs.writeFileSync(this.logFile, tail, { mode: 0o600 });
        }
      } catch {
        // log file does not exist yet
      }
      fs.appendFileSync(this.logFile, `[${new Date().toISOString()}] ${line}\n`, { mode: 0o600 });
    } catch {
      // best-effort logging
    }
  }

  _setWindowProgress(ratio) {
    try {
      const win = this.getWindow();
      if (win && typeof win.setProgressBar === "function") {
        win.setProgressBar(ratio);
      }
    } catch {
      // ignore
    }
  }

  start() {
    if (!this.enabled) {
      return;
    }
    this._surfacePreviousInstallError();
    if (!this._canSelfUpdate()) {
      this._log("Self-update unavailable for this install location; checks only.");
    }
    this._cleanupStaleDownloads();
    this.firstCheckTimer = setTimeout(
      () => this.checkNow({ interactive: false }).catch(() => {}),
      FIRST_CHECK_DELAY_MS
    );
    this.firstCheckTimer.unref?.();

    this.timer = setInterval(
      () => this.checkNow({ interactive: false }).catch(() => {}),
      CHECK_INTERVAL_MS
    );
    this.timer.unref?.();

    this.localTimer = setInterval(
      () => this.checkNow({ interactive: false, localOnly: true }).catch(() => {}),
      LOCAL_CHECK_INTERVAL_MS
    );
    this.localTimer.unref?.();
  }

  stop() {
    if (this.firstCheckTimer) {
      clearTimeout(this.firstCheckTimer);
      this.firstCheckTimer = null;
    }
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
    if (this.localTimer) {
      clearInterval(this.localTimer);
      this.localTimer = null;
    }
    this._setWindowProgress(-1);
  }

  _surfacePreviousInstallError() {
    try {
      if (!fs.existsSync(this.installErrorFile)) {
        return null;
      }
      const text = fs.readFileSync(this.installErrorFile, "utf8").trim();
      fs.rmSync(this.installErrorFile, { force: true });
      if (!text) {
        return null;
      }
      this._log(`Previous update install failed: ${text}`);
      this._message(
        "warning",
        "上次自动更新未能完成",
        `${text}\n\n请确认应用已安装在「应用程序」目录且未被只读挂载；您也可以手动覆盖安装（数据不会丢失）。\n详细日志：${this.swapLogFile}`
      );
      return text;
    } catch {
      return null;
    }
  }

  _canSelfUpdate() {
    if (!this.app.isPackaged) {
      return false;
    }
    if (this.platform === "darwin") {
      const bundle = macBundlePath();
      if (!bundle.endsWith(".app") || bundle.startsWith("/Volumes/") || bundle.includes("/AppTranslocation/")) {
        return false;
      }
      try {
        fs.accessSync(path.dirname(bundle), fs.constants.W_OK);
        fs.accessSync(bundle, fs.constants.W_OK);
        return true;
      } catch {
        return false;
      }
    }
    return this.platform === "win32";
  }

  _cleanupStaleDownloads() {
    try {
      const current = this.app.getVersion();
      for (const entry of fs.readdirSync(this.updatesDir)) {
        if (
          entry === UPDATER_LOG_FILE ||
          entry === SWAP_LOG_FILE ||
          entry === INSTALL_ERROR_FILE ||
          entry === "apply-update.sh"
        ) {
          continue;
        }
        const cmp = compareVersions(entry, current);
        if (cmp === null || cmp <= 0) {
          fs.rmSync(path.join(this.updatesDir, entry), { recursive: true, force: true });
        }
      }
    } catch {
      // nothing downloaded yet
    }
  }

  _resolveLocalDistDir() {
    if (this.localDistDirOption === null) {
      return null;
    }
    if (typeof this.localDistDirOption === "string" && this.localDistDirOption.trim()) {
      return path.resolve(this.localDistDirOption.trim());
    }
    const envDir = process.env.PRISM_LOCAL_UPDATE_DIR && process.env.PRISM_LOCAL_UPDATE_DIR.trim();
    if (envDir) {
      return path.resolve(envDir);
    }
    try {
      if (fs.existsSync(this.localSourceFile)) {
        const parsed = JSON.parse(fs.readFileSync(this.localSourceFile, "utf8"));
        if (parsed && typeof parsed.dist_dir === "string" && parsed.dist_dir.trim()) {
          return path.resolve(parsed.dist_dir.trim());
        }
      }
    } catch {
      // ignore malformed local marker
    }
    if (!this.app.isPackaged && this.repoRoot) {
      const devDist = path.join(this.repoRoot, "dist");
      if (fs.existsSync(devDist)) {
        return devDist;
      }
    }
    return null;
  }

  _readLocalCandidate() {
    const distDir = this._resolveLocalDistDir();
    if (!distDir) {
      return null;
    }
    const manifestPath = path.join(distDir, manifestName(this.platform, this.arch));
    if (!fs.existsSync(manifestPath)) {
      return null;
    }
    try {
      const rawText = fs.readFileSync(manifestPath, "utf8");
      if (rawText.length > MAX_MANIFEST_BYTES) {
        return null;
      }
      const manifest = validateManifest(JSON.parse(rawText), { platform: this.platform });
      const installerPath = path.join(distDir, manifest.file);
      const stat = fs.statSync(installerPath);
      if (!stat.isFile() || stat.size !== manifest.size) {
        return null;
      }
      return {
        source: "local",
        distDir,
        installerPath,
        manifest,
      };
    } catch (err) {
      this._log(
        `Ignored unreadable local dist manifest at ${manifestPath}: ${err instanceof Error ? err.message : String(err)}`
      );
      return null;
    }
  }

  async _orderedFetchers(url) {
    const hasNet = typeof this.netFetch === "function";
    const hasNode = typeof this.fetch === "function";
    if (!hasNet && !hasNode) {
      return [];
    }
    if (!hasNet) {
      return [{ name: "node", fn: this.fetch }];
    }
    if (!hasNode) {
      return [{ name: "electron-net", fn: this.netFetch }];
    }
    let useProxyFirst = false;
    if (this.resolveProxy) {
      try {
        const proxySpec = await this.resolveProxy(url);
        useProxyFirst = isProxyConfigured(proxySpec);
      } catch {
        useProxyFirst = false;
      }
    }
    return useProxyFirst
      ? [
          { name: "electron-net", fn: this.netFetch },
          { name: "node", fn: this.fetch },
        ]
      : [
          { name: "node", fn: this.fetch },
          { name: "electron-net", fn: this.netFetch },
        ];
  }

  async _fetchWithFallback(url, { timeoutMs = MANIFEST_TIMEOUT_MS, cache = undefined, retries = 1 } = {}) {
    const fetchers = await this._orderedFetchers(url);
    if (fetchers.length === 0) {
      throw new Error("No HTTP fetch implementation available");
    }
    let lastError = null;
    for (const fetcher of fetchers) {
      for (let attempt = 0; attempt <= retries; attempt += 1) {
        const controller = typeof AbortController === "function" ? new AbortController() : null;
        let timer = null;
        if (controller && timeoutMs > 0) {
          timer = setTimeout(() => controller.abort(new Error(`Request timed out after ${timeoutMs}ms`)), timeoutMs);
          timer.unref?.();
        }
        try {
          const opts = { redirect: "follow" };
          if (cache) {
            opts.cache = cache;
          }
          if (controller) {
            opts.signal = controller.signal;
          }
          const res = await fetcher.fn(url, opts);
          if (timer) {
            clearTimeout(timer);
          }
          return { res, transport: fetcher.name };
        } catch (err) {
          if (timer) {
            clearTimeout(timer);
          }
          lastError = err instanceof Error ? err : new Error(String(err));
          this._log(`Fetch via ${fetcher.name} failed (${url}): ${lastError.message}`);
        }
      }
    }
    throw lastError || new Error("Update request failed");
  }

  async _downloadRemoteToFile(url, destPath, { expectedSize = 0, onProgress = null, retries = 1 } = {}) {
    const fetchers = await this._orderedFetchers(url);
    if (fetchers.length === 0) {
      throw new Error("No HTTP fetch implementation available");
    }
    let lastError = null;
    for (const fetcher of fetchers) {
      for (let attempt = 0; attempt <= retries; attempt += 1) {
        const controller = typeof AbortController === "function" ? new AbortController() : null;
        let timer = null;
        if (controller) {
          timer = setTimeout(
            () => controller.abort(new Error(`Download headers timed out after ${MANIFEST_TIMEOUT_MS}ms`)),
            MANIFEST_TIMEOUT_MS
          );
          timer.unref?.();
        }
        try {
          fs.rmSync(destPath, { force: true });
          const opts = { redirect: "follow" };
          if (controller) {
            opts.signal = controller.signal;
          }
          const res = await fetcher.fn(url, opts);
          if (timer) {
            clearTimeout(timer);
            timer = null;
          }
          if (!res.ok || !res.body) {
            throw new Error(`Update download failed (HTTP ${res.status})`);
          }
          await writeResponseBodyToFile(res, destPath, {
            expectedSize,
            idleTimeoutMs: DOWNLOAD_IDLE_TIMEOUT_MS,
            onProgress,
          });
          return;
        } catch (err) {
          if (timer) {
            clearTimeout(timer);
          }
          fs.rmSync(destPath, { force: true });
          lastError = err instanceof Error ? err : new Error(String(err));
          this._log(`Download via ${fetcher.name} failed (${url}): ${lastError.message}`);
        }
      }
    }
    throw lastError || new Error("Update download failed");
  }

  async _fetchRemoteCandidate() {
    if (!this.feedUrl || (!this.fetch && !this.netFetch)) {
      return null;
    }
    const url = `${this.feedUrl.replace(/\/+$/, "")}/${manifestName(this.platform, this.arch)}`;
    const { res } = await this._fetchWithFallback(url, {
      timeoutMs: MANIFEST_TIMEOUT_MS,
      cache: "no-store",
      retries: 1,
    });
    if (res.status === 404) {
      return null;
    }
    if (!res.ok) {
      throw new Error(`Update check failed (HTTP ${res.status})`);
    }
    const text = await res.text();
    if (text.length > MAX_MANIFEST_BYTES) {
      throw new Error("Update manifest is too large");
    }
    const manifest = validateManifest(JSON.parse(text), { platform: this.platform });
    return {
      source: "remote",
      url: `${this.feedUrl.replace(/\/+$/, "")}/${encodeURIComponent(manifest.file)}`,
      manifest,
    };
  }

  async _discoverLatestCandidate({ localOnly = false } = {}) {
    const candidates = [];
    const localCandidate = this._readLocalCandidate();
    if (localCandidate) {
      candidates.push(localCandidate);
    }

    let remoteError = null;
    if (!localOnly && this.feedUrl && (this.fetch || this.netFetch)) {
      try {
        const remoteCandidate = await this._fetchRemoteCandidate();
        if (remoteCandidate) {
          candidates.push(remoteCandidate);
        }
      } catch (err) {
        remoteError = err instanceof Error ? err : new Error(String(err));
      }
    }

    const baseline = this.ready ? this.ready.version : this.app.getVersion();
    const winner = pickLatestCandidate(candidates, baseline);
    return { winner, candidates, remoteError };
  }

  async checkNow({ interactive = false, localOnly = false } = {}) {
    if (!this.enabled) {
      if (interactive) {
        this._message("info", "当前版本未启用自动更新。");
      }
      return null;
    }
    if (this.state === "checking" || this.state === "downloading") {
      if (interactive) {
        const pct =
          typeof this.downloadProgress === "number"
            ? `（${Math.round(this.downloadProgress * 100)}%）`
            : "";
        const ver = this.activeVersion ? ` ${this.activeVersion}` : "";
        this._message(
          "info",
          this.state === "downloading"
            ? `正在后台准备新版本${ver}${pct}，请稍候。`
            : "正在检查更新，请稍候。"
        );
      }
      return null;
    }

    const previousState = this.state;
    this.state = "checking";
    try {
      const { winner, remoteError } = await this._discoverLatestCandidate({ localOnly });
      const current = this.app.getVersion();

      if (!winner) {
        if (this.ready) {
          this.state = "ready";
          if (interactive) {
            this._promptReady(true);
          }
          return this.ready;
        }
        if (remoteError && !localOnly) {
          throw remoteError;
        }
        this.state = previousState === "ready" ? "ready" : "idle";
        if (interactive) {
          this._message("info", `已是最新版本（${current}）。`);
        }
        return null;
      }

      if (!this._canSelfUpdate()) {
        this.state = this.ready ? "ready" : "idle";
        if (interactive || this.promptedVersion !== winner.manifest.version) {
          this.promptedVersion = winner.manifest.version;
          this._message(
            "info",
            `发现新版本 ${winner.manifest.version}（来源：${sourceLabel(winner.source)}）。`,
            this.platform === "darwin"
              ? "请先把应用拖入「应用程序」文件夹后再启动，即可自动更新；或手动覆盖安装（数据不会丢失）。"
              : "请手动下载安装新版本（数据不会丢失）。"
          );
        }
        return null;
      }

      this.state = "downloading";
      this.activeVersion = winner.manifest.version;
      this.downloadProgress = 0;
      this._log(
        `Preparing update ${winner.manifest.version} from ${winner.source}` +
          (remoteError ? ` (remote check failed: ${remoteError.message})` : "")
      );
      const ready = await this._prepareCandidate(winner);
      this.ready = ready;
      this.activeVersion = null;
      this.downloadProgress = null;
      this._setWindowProgress(-1);
      this.state = "ready";
      this._log(`Update ${ready.version} (${ready.source}) verified and staged`);
      this._promptReady(interactive);
      return ready;
    } catch (err) {
      this.activeVersion = null;
      this.downloadProgress = null;
      this._setWindowProgress(-1);
      this.state = this.ready ? "ready" : "error";
      const message = err instanceof Error ? err.message : String(err);
      this._log(`Update check failed: ${message}`);
      if (interactive) {
        this._message("warning", "检查更新失败", `${message}\n\n日志文件：${this.logFile}`);
      }
      return null;
    }
  }

  async _prepareCandidate(candidate) {
    const { manifest } = candidate;
    const dir = path.join(this.updatesDir, manifest.version);
    fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
    const finalPath = path.join(dir, manifest.file);
    const onProgress = (transferred, total) => {
      if (total > 0) {
        const ratio = Math.max(0, Math.min(1, transferred / total));
        this.downloadProgress = ratio;
        this._setWindowProgress(ratio);
      }
    };

    if (!(fs.existsSync(finalPath) && (await this._verify(finalPath, manifest)))) {
      const partial = `${finalPath}.partial`;
      fs.rmSync(partial, { force: true });
      if (candidate.source === "local") {
        await copyFileWithProgress(candidate.installerPath, partial, {
          expectedSize: manifest.size,
          onProgress,
        });
      } else {
        const url =
          candidate.url || `${this.feedUrl.replace(/\/+$/, "")}/${encodeURIComponent(manifest.file)}`;
        await this._downloadRemoteToFile(url, partial, {
          expectedSize: manifest.size,
          onProgress,
        });
      }

      if (!(await this._verify(partial, manifest))) {
        fs.rmSync(partial, { force: true });
        throw new Error("Downloaded update failed size/SHA-256 verification");
      }
      fs.renameSync(partial, finalPath);
    }

    const ready = {
      version: manifest.version,
      source: candidate.source,
      installerPath: finalPath,
      stagedApp: null,
      notes: manifest.notes,
      releasedAt: manifest.releasedAt,
    };
    if (this.platform === "darwin") {
      ready.stagedApp = this._stageMacApp(finalPath, dir, manifest.version);
    }
    return ready;
  }

  async _verify(file, manifest) {
    try {
      if (fs.statSync(file).size !== manifest.size) {
        return false;
      }
    } catch {
      return false;
    }
    const hash = crypto.createHash("sha256");
    await pipeline(fs.createReadStream(file), hash);
    return hash.digest("hex") === manifest.sha256;
  }

  _readPlistValue(appPath, key) {
    return execFileSync(
      "/usr/bin/plutil",
      ["-extract", key, "raw", "-o", "-", path.join(appPath, "Contents", "Info.plist")],
      { encoding: "utf8" }
    ).trim();
  }

  _stageMacApp(dmgPath, dir, version) {
    const stageRoot = path.join(dir, "staged");
    fs.rmSync(stageRoot, { recursive: true, force: true });
    fs.mkdirSync(stageRoot, { recursive: true, mode: 0o700 });
    const mountPoint = fs.mkdtempSync(path.join(dir, "mnt-"));
    execFileSync(
      "/usr/bin/hdiutil",
      [
        "attach",
        dmgPath,
        "-nobrowse",
        "-readonly",
        "-noautoopen",
        "-mountpoint",
        mountPoint,
      ],
      { stdio: ["ignore", "pipe", "pipe"] }
    );
    try {
      const appName = fs.readdirSync(mountPoint).find((n) => n.endsWith(".app"));
      if (!appName) {
        throw new Error("No .app found inside the update disk image");
      }
      const staged = path.join(stageRoot, appName);
      execFileSync("/usr/bin/ditto", [path.join(mountPoint, appName), staged]);
      const currentId = this._readPlistValue(macBundlePath(), "CFBundleIdentifier");
      const stagedId = this._readPlistValue(staged, "CFBundleIdentifier");
      if (currentId !== stagedId) {
        throw new Error(`Update bundle id mismatch (${stagedId} != ${currentId})`);
      }
      const stagedVersion = this._readPlistValue(staged, "CFBundleShortVersionString");
      if (compareVersions(stagedVersion, version) !== 0) {
        throw new Error(`Update bundle version mismatch (${stagedVersion} != ${version})`);
      }
      try {
        execFileSync("/usr/bin/xattr", ["-dr", "com.apple.quarantine", staged], { stdio: "ignore" });
      } catch {
        // not quarantined
      }
      return staged;
    } finally {
      try {
        execFileSync("/usr/bin/hdiutil", ["detach", mountPoint, "-force"], { stdio: "ignore" });
      } catch {
        // ignore
      }
      fs.rmSync(mountPoint, { recursive: true, force: true });
    }
  }

  hasPendingUpdate() {
    return Boolean(this.ready) && !this.installing;
  }

  /**
   * Launch the installer for the downloaded update. Call only after the
   * sidecar and profile windows are shut down; the helper waits for this
   * process to exit before replacing anything.
   */
  launchInstaller({ relaunch = false } = {}) {
    if (!this.ready || this.installing) {
      return false;
    }
    this.installing = true;
    try {
      if (this.platform === "darwin") {
        fs.mkdirSync(this.updatesDir, { recursive: true, mode: 0o700 });
        const script = path.join(this.updatesDir, "apply-update.sh");
        fs.writeFileSync(script, buildMacSwapScript(), { mode: 0o700 });
        const child = spawn(
          "/bin/bash",
          [
            script,
            String(process.pid),
            macBundlePath(),
            this.ready.stagedApp,
            relaunch ? "1" : "0",
            this.installErrorFile,
            this.swapLogFile,
          ],
          { detached: true, stdio: "ignore" }
        );
        child.unref();
        return true;
      }
      if (this.platform === "win32") {
        const args = ["/S", "--updated"];
        if (relaunch) {
          args.push("--force-run");
        }
        const child = spawn(this.ready.installerPath, args, { detached: true, stdio: "ignore" });
        child.unref();
        return true;
      }
    } catch (err) {
      this.installing = false;
      this._log(`Failed to launch update installer: ${err instanceof Error ? err.message : String(err)}`);
    }
    return false;
  }

  _message(type, message, detail = "") {
    if (!this.dialog) {
      return;
    }
    const win = this.getWindow();
    const opts = { type, title: this.brandName || "更新", message, detail, buttons: ["好"] };
    (win ? this.dialog.showMessageBox(win, opts) : this.dialog.showMessageBox(opts)).catch(() => {});
  }

  _promptReady(force) {
    if (!this.ready || !this.dialog) {
      return;
    }
    if (!force && this.promptedVersion === this.ready.version) {
      return;
    }
    this.promptedVersion = this.ready.version;
    const win = this.getWindow();
    const origin = sourceLabel(this.ready.source);
    const opts = {
      type: "info",
      title: this.brandName || "更新",
      message: `新版本 ${this.ready.version} 已准备就绪（来源：${origin}）`,
      detail:
        "退出应用时会自动安装，下次打开即是新版本。\n已创建的窗口、节点、订阅等数据都保存在独立的数据目录中，更新不会丢失。",
      buttons: ["立即重启更新", "稍后（退出时自动安装）"],
      defaultId: 1,
      cancelId: 1,
    };
    const shown = win ? this.dialog.showMessageBox(win, opts) : this.dialog.showMessageBox(opts);
    shown
      .then(({ response }) => {
        if (response === 0 && typeof this.onRestartRequested === "function") {
          this.onRestartRequested();
        }
      })
      .catch(() => {});
  }
}

module.exports = {
  UpdateManager,
  compareVersions,
  compareCandidates,
  pickLatestCandidate,
  parseVersion,
  manifestName,
  defaultFeedUrl,
  validateManifest,
  buildMacSwapScript,
  macBundlePath,
  isProxyConfigured,
  LOCAL_SOURCE_FILE,
  INSTALL_ERROR_FILE,
};
