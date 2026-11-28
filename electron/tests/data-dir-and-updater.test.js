"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const {
  STABLE_DATA_DIR_NAME,
  UPGRADE_BACKUP_DIR,
  resolveUserDataDir,
  backupOnVersionChange,
  legacyDataDirCandidates,
} = require("../data-dir");
const {
  UpdateManager,
  compareVersions,
  compareCandidates,
  pickLatestCandidate,
  validateManifest,
  manifestName,
  isProxyConfigured,
  LOCAL_SOURCE_FILE,
  INSTALL_ERROR_FILE,
} = require("../updater");

function tmp(prefix) {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix));
}

function seed(dir, { db = "db", key = "k".repeat(43) + "=", extra = {} } = {}) {
  fs.mkdirSync(path.join(dir, "profiles", "profile_abc"), { recursive: true });
  fs.writeFileSync(path.join(dir, "profiles.db"), db);
  fs.writeFileSync(path.join(dir, "secret.key"), key);
  fs.writeFileSync(path.join(dir, "profiles", "profile_abc", "state"), "cookies");
  for (const [name, content] of Object.entries(extra)) {
    fs.writeFileSync(path.join(dir, name), content);
  }
}

test("data directory does not follow the brand name", () => {
  const appData = tmp("prism-appdata-");
  const home = tmp("prism-home-");
  const a = resolveUserDataDir({ appDataDir: appData, brandName: "棱镜浏览器", homeDir: home, platform: "darwin" });
  const b = resolveUserDataDir({ appDataDir: appData, brandName: "Acme 浏览器", homeDir: home, platform: "darwin" });
  assert.equal(a.userDataDir, path.join(appData, STABLE_DATA_DIR_NAME));
  assert.equal(b.userDataDir, a.userDataDir);
});

test("data left under an older brand directory is copied in once and kept at the source", () => {
  const appData = tmp("prism-appdata-");
  const home = tmp("prism-home-");
  const legacy = path.join(appData, "Old Brand");
  seed(legacy, { db: "legacy-db", extra: { SingletonLock: "x" } });

  const res = resolveUserDataDir({ appDataDir: appData, brandName: "Old Brand", homeDir: home, platform: "darwin" });
  assert.equal(res.adoptedFrom, path.resolve(legacy));
  assert.equal(fs.readFileSync(path.join(res.userDataDir, "profiles.db"), "utf8"), "legacy-db");
  assert.equal(
    fs.readFileSync(path.join(res.userDataDir, "profiles", "profile_abc", "state"), "utf8"),
    "cookies"
  );
  assert.equal(fs.existsSync(path.join(res.userDataDir, "SingletonLock")), false);
  assert.ok(fs.existsSync(path.join(legacy, "profiles.db")), "source stays as a fallback");

  // Second launch: stable dir now has data, nothing else is adopted.
  fs.writeFileSync(path.join(legacy, "profiles.db"), "newer-legacy");
  const again = resolveUserDataDir({ appDataDir: appData, brandName: "Old Brand", homeDir: home, platform: "darwin" });
  assert.equal(again.adoptedFrom, null);
  assert.equal(fs.readFileSync(path.join(res.userDataDir, "profiles.db"), "utf8"), "legacy-db");
});

test("existing data in the stable directory is never overwritten by legacy data", () => {
  const appData = tmp("prism-appdata-");
  const home = tmp("prism-home-");
  seed(path.join(appData, STABLE_DATA_DIR_NAME), { db: "current" });
  seed(path.join(appData, "噜噜指纹浏览器"), { db: "legacy" });
  const res = resolveUserDataDir({ appDataDir: appData, brandName: "棱镜浏览器", homeDir: home, platform: "darwin" });
  assert.equal(res.adoptedFrom, null);
  assert.equal(fs.readFileSync(path.join(res.userDataDir, "profiles.db"), "utf8"), "current");
});

test("the Python desktop data directory is a legacy candidate", () => {
  const appData = tmp("prism-appdata-");
  const home = tmp("prism-home-");
  const dirs = legacyDataDirCandidates({ appDataDir: appData, brandName: "", homeDir: home, platform: "darwin" });
  assert.ok(dirs.includes(path.join(home, "Library", "Application Support", "Camoufox Profile Manager")));
  assert.ok(!dirs.includes(path.join(appData, STABLE_DATA_DIR_NAME)));
});

test("a version change snapshots the database and key before the new version runs", () => {
  const dir = tmp("prism-userdata-");
  seed(dir, { db: "v1-db", extra: { "profiles.db-wal": "wal" } });

  const first = backupOnVersionChange(dir, "0.6.1");
  assert.ok(first && first.includes(UPGRADE_BACKUP_DIR));
  assert.equal(fs.readFileSync(path.join(first, "profiles.db"), "utf8"), "v1-db");
  assert.equal(fs.readFileSync(path.join(first, "profiles.db-wal"), "utf8"), "wal");
  assert.ok(fs.existsSync(path.join(first, "secret.key")));

  assert.equal(backupOnVersionChange(dir, "0.6.1"), null, "same version: no new backup");

  for (let i = 2; i < 10; i += 1) {
    backupOnVersionChange(dir, `0.6.${i}`, { keep: 3, now: new Date(Date.UTC(2026, 0, i)) });
  }
  assert.equal(fs.readdirSync(path.join(dir, UPGRADE_BACKUP_DIR)).length, 3);
});

test("version comparison and manifest validation", () => {
  assert.ok(compareVersions("0.6.12", "0.6.9") > 0);
  assert.equal(compareVersions("0.6.0", "v0.6.0"), 0);
  assert.ok(compareVersions("0.6.0", "1.0.0") < 0);
  assert.equal(compareVersions("latest", "0.6.0"), null);

  const ok = validateManifest(
    { version: "0.6.5", file: "prism-browser-macos-arm64.dmg", sha256: "a".repeat(64), size: 10 },
    { platform: "darwin" }
  );
  assert.equal(ok.version, "0.6.5");
  assert.throws(() =>
    validateManifest({ version: "0.6.5", file: "../x.dmg", sha256: "a".repeat(64), size: 10 }, { platform: "darwin" })
  );
  assert.throws(() =>
    validateManifest({ version: "0.6.5", file: "x.exe", sha256: "a".repeat(64), size: 10 }, { platform: "darwin" })
  );
  assert.throws(() =>
    validateManifest({ version: "0.6.5", file: "x.dmg", sha256: "nope", size: 10 }, { platform: "darwin" })
  );
});

test("updater downloads and verifies a newer build, ignores older ones", async () => {
  const userData = tmp("prism-userdata-");
  const payload = Buffer.from("installer-bytes");
  const sha256 = crypto.createHash("sha256").update(payload).digest("hex");
  const isMac = process.platform === "darwin";
  const file = isMac ? "prism-browser-macos-arm64.dmg" : "prism-browser-windows-x64-setup.exe";
  let manifestVersion = "9.9.9";
  const requests = [];
  const fetchImpl = async (url) => {
    requests.push(url);
    if (url.endsWith(manifestName())) {
      return new Response(JSON.stringify({ version: manifestVersion, file, sha256, size: payload.length }));
    }
    return new Response(payload);
  };
  const fakeApp = { isPackaged: true, getVersion: () => "0.6.0" };
  const mgr = new UpdateManager({
    app: fakeApp,
    userDataDir: userData,
    feedUrl: "https://example.test/feed",
    localDistDir: null,
    fetchImpl,
  });
  if (!mgr.enabled) {
    return; // platform without an update channel (Linux CI)
  }
  mgr._canSelfUpdate = () => true;
  mgr._stageMacApp = () => "/tmp/staged.app";

  const ready = await mgr.checkNow();
  assert.equal(ready.version, "9.9.9");
  assert.equal(ready.source, "remote");
  assert.equal(fs.readFileSync(ready.installerPath).toString(), "installer-bytes");
  assert.ok(mgr.hasPendingUpdate());

  const older = new UpdateManager({
    app: fakeApp,
    userDataDir: userData,
    feedUrl: "https://example.test/feed",
    localDistDir: null,
    fetchImpl,
  });
  older._canSelfUpdate = () => true;
  manifestVersion = "0.5.9";
  assert.equal(await older.checkNow(), null);
  assert.equal(older.hasPendingUpdate(), false);
});

test("updater rejects a download whose hash does not match", async () => {
  const userData = tmp("prism-userdata-");
  const isMac = process.platform === "darwin";
  const file = isMac ? "a.dmg" : "a.exe";
  const fetchImpl = async (url) =>
    url.endsWith(manifestName())
      ? new Response(JSON.stringify({ version: "9.9.9", file, sha256: "b".repeat(64), size: 3 }))
      : new Response("bad");
  const mgr = new UpdateManager({
    app: { isPackaged: true, getVersion: () => "0.6.0" },
    userDataDir: userData,
    feedUrl: "https://example.test/feed",
    localDistDir: null,
    fetchImpl,
  });
  if (!mgr.enabled) {
    return;
  }
  mgr._canSelfUpdate = () => true;
  assert.equal(await mgr.checkNow(), null);
  assert.equal(mgr.state, "error");
  assert.equal(mgr.hasPendingUpdate(), false);
});

function writeLocalDistArtifact(distDir, { version, payload, releasedAt = "2026-11-28T02:00:00Z" }) {
  const isMac = process.platform === "darwin";
  const file = isMac ? "prism-browser-macos-arm64.dmg" : "prism-browser-windows-x64-setup.exe";
  const buf = Buffer.isBuffer(payload) ? payload : Buffer.from(payload);
  const sha256 = crypto.createHash("sha256").update(buf).digest("hex");
  fs.mkdirSync(distDir, { recursive: true });
  fs.writeFileSync(path.join(distDir, file), buf);
  fs.writeFileSync(
    path.join(distDir, manifestName()),
    JSON.stringify({ version, file, sha256, size: buf.length, released_at: releasedAt }, null, 2)
  );
  return { file, sha256, size: buf.length };
}

test("updater compares local dist and remote feed and always picks the newest version", async () => {
  const isMac = process.platform === "darwin";
  const file = isMac ? "prism-browser-macos-arm64.dmg" : "prism-browser-windows-x64-setup.exe";
  const remoteBuf = Buffer.from("remote-0.6.5-bytes");
  const remoteSha = crypto.createHash("sha256").update(remoteBuf).digest("hex");
  const localBuf = Buffer.from("local-0.6.6-bytes");

  // 1) Local dist (0.6.6) beats remote (0.6.5) via local-update-source.json
  const userData = tmp("prism-userdata-");
  const distDir = tmp("prism-local-dist-");
  writeLocalDistArtifact(distDir, { version: "0.6.6", payload: localBuf });
  fs.writeFileSync(path.join(userData, LOCAL_SOURCE_FILE), JSON.stringify({ dist_dir: distDir }));

  let remoteVersion = "0.6.5";
  const fetchImpl = async (url) => {
    if (url.endsWith(manifestName())) {
      return new Response(
        JSON.stringify({
          version: remoteVersion,
          file,
          sha256: remoteSha,
          size: remoteBuf.length,
          released_at: "2026-11-28T01:00:00Z",
        })
      );
    }
    return new Response(remoteBuf);
  };

  const mgr = new UpdateManager({
    app: { isPackaged: true, getVersion: () => "0.6.3" },
    userDataDir: userData,
    feedUrl: "https://example.test/feed",
    fetchImpl,
  });
  if (!mgr.enabled) {
    return;
  }
  mgr._canSelfUpdate = () => true;
  mgr._stageMacApp = (_dmg, _dir, ver) => `/tmp/staged-${ver}.app`;

  const first = await mgr.checkNow();
  assert.equal(first.version, "0.6.6");
  assert.equal(first.source, "local");
  assert.equal(fs.readFileSync(first.installerPath).toString(), "local-0.6.6-bytes");

  // 2) Even after 0.6.6 is ready, a newer remote 0.6.7 supersedes it
  remoteVersion = "0.6.7";
  const second = await mgr.checkNow();
  assert.equal(second.version, "0.6.7");
  assert.equal(second.source, "remote");
  assert.equal(fs.readFileSync(second.installerPath).toString(), "remote-0.6.5-bytes");

  // 3) If remote fails completely, a newer local dist (0.6.8) still succeeds
  writeLocalDistArtifact(distDir, { version: "0.6.8", payload: Buffer.from("local-0.6.8") });
  mgr.fetch = async () => {
    throw new Error("network down");
  };
  const third = await mgr.checkNow();
  assert.equal(third.version, "0.6.8");
  assert.equal(third.source, "local");
});

test("pickLatestCandidate breaks same-version ties by timestamp then local preference", () => {
  const mk = (source, version, releasedAt) => ({
    source,
    manifest: { version, file: "a.dmg", sha256: "a".repeat(64), size: 10, releasedAt },
  });
  assert.equal(
    pickLatestCandidate(
      [mk("local", "0.6.4", "2026-11-28T01:00:00Z"), mk("remote", "0.6.4", "2026-11-28T03:00:00Z")],
      "0.6.3"
    ).source,
    "remote"
  );
  assert.equal(
    pickLatestCandidate(
      [mk("remote", "0.6.4", "2026-11-28T02:00:00Z"), mk("local", "0.6.4", "2026-11-28T02:00:00Z")],
      "0.6.3"
    ).source,
    "local"
  );
  assert.ok(compareCandidates(mk("local", "0.6.5", ""), mk("remote", "0.6.4", "2026-12-01T00:00:00Z")) > 0);
});

test("updater prefers proxy-aware netFetch when system proxy is active and falls back cleanly", async () => {
  assert.equal(isProxyConfigured("DIRECT"), false);
  assert.equal(isProxyConfigured("PROXY 127.0.0.1:7890; DIRECT"), true);

  const userData = tmp("prism-userdata-");
  const payload = Buffer.from("via-proxy-bytes");
  const sha256 = crypto.createHash("sha256").update(payload).digest("hex");
  const isMac = process.platform === "darwin";
  const file = isMac ? "prism-browser-macos-arm64.dmg" : "prism-browser-windows-x64-setup.exe";
  const calls = [];

  const mgr = new UpdateManager({
    app: { isPackaged: true, getVersion: () => "0.6.0" },
    userDataDir: userData,
    feedUrl: "https://example.test/feed",
    localDistDir: null,
    resolveProxy: async () => "PROXY 127.0.0.1:7890",
    netFetch: async (url) => {
      calls.push(["netFetch", url]);
      if (url.endsWith(manifestName())) {
        return new Response(JSON.stringify({ version: "0.6.4", file, sha256, size: payload.length }));
      }
      // Simulate mid-flight proxy failure on binary download to verify fallback to node fetch
      throw new Error("ERR_TUNNEL_CONNECTION_FAILED");
    },
    fetchImpl: async (url) => {
      calls.push(["nodeFetch", url]);
      return new Response(payload);
    },
  });
  if (!mgr.enabled) {
    return;
  }
  mgr._canSelfUpdate = () => true;
  mgr._stageMacApp = () => "/tmp/staged.app";

  const ready = await mgr.checkNow();
  assert.equal(ready.version, "0.6.4");
  assert.equal(calls[0][0], "netFetch");
  assert.ok(calls.some(([who]) => who === "nodeFetch"), "Should fall back to nodeFetch when netFetch fails");
});

test("updater surfaces and clears previous swap error on next startup", () => {
  const userData = tmp("prism-userdata-");
  const updatesDir = path.join(userData, "updates");
  fs.mkdirSync(updatesDir, { recursive: true });
  fs.writeFileSync(path.join(updatesDir, INSTALL_ERROR_FILE), "权限不足无法替换应用\n");

  const shown = [];
  const mgr = new UpdateManager({
    app: { isPackaged: true, getVersion: () => "0.6.3" },
    dialog: {
      showMessageBox: async (opts) => {
        shown.push(opts);
        return { response: 0 };
      },
    },
    userDataDir: userData,
    localDistDir: null,
  });

  const surfaced = mgr._surfacePreviousInstallError();
  assert.equal(surfaced, "权限不足无法替换应用");
  assert.equal(fs.existsSync(path.join(updatesDir, INSTALL_ERROR_FILE)), false);
  assert.equal(shown.length, 1);
  assert.ok(shown[0].message.includes("未能完成"));
});
