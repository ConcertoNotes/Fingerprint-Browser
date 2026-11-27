"use strict";

/**
 * Background ("seamless") updates for the desktop shell.
 *
 * Flow: check a small JSON manifest -> download the installer in the
 * background -> verify size + SHA-256 -> install when the app quits (or when
 * the user chooses "restart now"). User data is never touched: it lives in the
 * stable userData directory (see data-dir.js), outside the app bundle, and the
 * next start snapshots the database before the new version opens it.
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
const { Readable } = require("node:stream");

const DEFAULT_FEED_REPO = "https://github.com/ConcertoNotes/Fingerprint-Browser/releases/download";
const CHANNEL_TAGS = Object.freeze({ darwin: "macos-latest", win32: "windows-latest" });
const FIRST_CHECK_DELAY_MS = 20 * 1000;
const CHECK_INTERVAL_MS = 4 * 60 * 60 * 1000;
const MAX_MANIFEST_BYTES = 64 * 1024;

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

function buildMacSwapScript() {
  // Args: <pid> <targetApp> <stagedApp> <relaunch 0|1>
  return `#!/bin/bash
set -u
PID="$1"; TARGET="$2"; STAGED="$3"; RELAUNCH="$4"
for _ in $(seq 1 600); do kill -0 "$PID" 2>/dev/null || break; sleep 0.5; done
[ -d "$STAGED" ] || exit 1
BACKUP="$TARGET.prism-update-old"
rm -rf "$BACKUP"
if mv "$TARGET" "$BACKUP" 2>/dev/null; then
  if mv "$STAGED" "$TARGET" 2>/dev/null || ditto "$STAGED" "$TARGET"; then
    rm -rf "$BACKUP"
  else
    rm -rf "$TARGET"; mv "$BACKUP" "$TARGET"
  fi
fi
xattr -dr com.apple.quarantine "$TARGET" 2>/dev/null || true
if [ "$RELAUNCH" = "1" ]; then open "$TARGET"; fi
exit 0
`;
}

class UpdateManager {
  constructor({
    app,
    dialog = null,
    getWindow = () => null,
    userDataDir,
    brandName = "",
    feedUrl = undefined,
    enabled = true,
    fetchImpl = globalThis.fetch,
    log = () => {},
  }) {
    this.app = app;
    this.dialog = dialog;
    this.getWindow = getWindow;
    this.brandName = brandName;
    this.updatesDir = path.join(userDataDir, "updates");
    this.feedUrl = (feedUrl === undefined ? defaultFeedUrl() : feedUrl) || null;
    this.fetch = fetchImpl;
    this.log = log;
    this.platform = process.platform;
    this.enabled = Boolean(enabled && this.feedUrl && fetchImpl && CHANNEL_TAGS[this.platform]);
    this.state = "idle"; // idle | checking | downloading | ready | error
    this.ready = null; // { version, installerPath, stagedApp }
    this.timer = null;
    this.promptedVersion = null;
    this.installing = false;
  }

  start() {
    if (!this.enabled) {
      return;
    }
    if (!this._canSelfUpdate()) {
      this.log("Self-update unavailable for this install location; checks only.");
    }
    this._cleanupStaleDownloads();
    setTimeout(() => this.checkNow({ interactive: false }).catch(() => {}), FIRST_CHECK_DELAY_MS).unref?.();
    this.timer = setInterval(
      () => this.checkNow({ interactive: false }).catch(() => {}),
      CHECK_INTERVAL_MS
    );
    this.timer.unref?.();
  }

  stop() {
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
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
        const cmp = compareVersions(entry, current);
        if (cmp === null || cmp <= 0) {
          fs.rmSync(path.join(this.updatesDir, entry), { recursive: true, force: true });
        }
      }
    } catch {
      // nothing downloaded yet
    }
  }

  async _fetchManifest() {
    const url = `${this.feedUrl.replace(/\/+$/, "")}/${manifestName()}`;
    const res = await this.fetch(url, { redirect: "follow", cache: "no-store" });
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
    return validateManifest(JSON.parse(text), { platform: this.platform });
  }

  async checkNow({ interactive = false } = {}) {
    if (!this.enabled) {
      if (interactive) {
        this._message("info", "当前版本未启用自动更新。");
      }
      return null;
    }
    if (this.state === "checking" || this.state === "downloading") {
      if (interactive) {
        this._message("info", "正在后台检查/下载更新，请稍候。");
      }
      return null;
    }
    if (this.ready) {
      if (interactive) {
        this._promptReady(true);
      }
      return this.ready;
    }
    this.state = "checking";
    try {
      const manifest = await this._fetchManifest();
      const current = this.app.getVersion();
      if (!manifest || !(compareVersions(manifest.version, current) > 0)) {
        this.state = "idle";
        if (interactive) {
          this._message("info", `已是最新版本（${current}）。`);
        }
        return null;
      }
      if (!this._canSelfUpdate()) {
        this.state = "idle";
        if (interactive || this.promptedVersion !== manifest.version) {
          this.promptedVersion = manifest.version;
          this._message(
            "info",
            `发现新版本 ${manifest.version}。`,
            this.platform === "darwin"
              ? "请先把应用拖入「应用程序」文件夹后再启动，即可自动更新；或手动下载安装（数据不会丢失）。"
              : "请手动下载安装新版本（数据不会丢失）。"
          );
        }
        return null;
      }
      this.state = "downloading";
      const ready = await this._download(manifest);
      this.ready = ready;
      this.state = "ready";
      this.log(`Update ${manifest.version} downloaded and verified`);
      this._promptReady(interactive);
      return ready;
    } catch (err) {
      this.state = "error";
      const message = err instanceof Error ? err.message : String(err);
      this.log(`Update check failed: ${message}`);
      if (interactive) {
        this._message("warning", "检查更新失败", message);
      }
      return null;
    }
  }

  async _download(manifest) {
    const dir = path.join(this.updatesDir, manifest.version);
    fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
    const finalPath = path.join(dir, manifest.file);
    if (!(fs.existsSync(finalPath) && (await this._verify(finalPath, manifest)))) {
      const partial = `${finalPath}.partial`;
      const url = `${this.feedUrl.replace(/\/+$/, "")}/${encodeURIComponent(manifest.file)}`;
      const res = await this.fetch(url, { redirect: "follow" });
      if (!res.ok || !res.body) {
        throw new Error(`Update download failed (HTTP ${res.status})`);
      }
      await pipeline(Readable.fromWeb(res.body), fs.createWriteStream(partial, { mode: 0o600 }));
      if (!(await this._verify(partial, manifest))) {
        fs.rmSync(partial, { force: true });
        throw new Error("Downloaded update failed size/SHA-256 verification");
      }
      fs.renameSync(partial, finalPath);
    }
    const ready = { version: manifest.version, installerPath: finalPath, stagedApp: null, notes: manifest.notes };
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
    execFileSync("/usr/bin/hdiutil", [
      "attach",
      dmgPath,
      "-nobrowse",
      "-readonly",
      "-noautoopen",
      "-mountpoint",
      mountPoint,
    ], { stdio: ["ignore", "pipe", "pipe"] });
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
        const script = path.join(this.updatesDir, "apply-update.sh");
        fs.writeFileSync(script, buildMacSwapScript(), { mode: 0o700 });
        const child = spawn(
          "/bin/bash",
          [script, String(process.pid), macBundlePath(), this.ready.stagedApp, relaunch ? "1" : "0"],
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
      this.log(`Failed to launch update installer: ${err instanceof Error ? err.message : String(err)}`);
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
    const opts = {
      type: "info",
      title: this.brandName || "更新",
      message: `新版本 ${this.ready.version} 已在后台下载完成`,
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
  parseVersion,
  manifestName,
  defaultFeedUrl,
  validateManifest,
  buildMacSwapScript,
  macBundlePath,
};
