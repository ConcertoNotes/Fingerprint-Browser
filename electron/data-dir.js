"use strict";

/**
 * Stable user-data location + upgrade safety.
 *
 * Electron derives `userData` from the app name. The shell sets the app name
 * from the (re-brandable) distribution brand, so every rename used to start the
 * app on an empty directory and the user's profiles, nodes and windows looked
 * "lost" after installing a new build. This module pins the data directory to
 * a fixed name, adopts data left behind by earlier names, and snapshots the
 * database whenever the installed version changes.
 */

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

// Never derive this from the brand name: it must stay identical across
// renames, customer editions and upgrades.
const STABLE_DATA_DIR_NAME = "棱镜浏览器";

// Earlier directory names this app's data has lived under.
const LEGACY_APP_DATA_NAMES = Object.freeze([
  "噜噜指纹浏览器",
  "Prism Browser",
  "prism-browser-desktop",
]);

const UPGRADE_BACKUP_DIR = "upgrade-backups";
const VERSION_MARKER = "app-version.json";
const MIGRATION_MARKER = "migrated-from.json";

// Volatile Chromium caches and process locks are not worth copying.
const SKIP_ON_ADOPT = new Set([
  "SingletonLock",
  "SingletonCookie",
  "SingletonSocket",
  "desktop.lock",
  "Cache",
  "Code Cache",
  "GPUCache",
  "DawnGraphiteCache",
  "DawnWebGPUCache",
  "ShaderCache",
  "Crashpad",
  UPGRADE_BACKUP_DIR,
]);

const DB_FILES = ["profiles.db", "profiles.db-wal", "profiles.db-shm"];
const KEY_FILES = ["secret.key", "secret.key.enc"];

/** Data directory used by the earlier Python desktop build (see config.desktop_data_dir). */
function pythonDesktopDataDir({ platform = process.platform, homeDir = os.homedir(), env = process.env } = {}) {
  if (platform === "darwin") {
    return path.join(homeDir, "Library", "Application Support", "Camoufox Profile Manager");
  }
  if (platform === "win32") {
    const local = env.LOCALAPPDATA || path.join(homeDir, "AppData", "Local");
    return path.join(local, "Camoufox Profile Manager");
  }
  const xdg = env.XDG_DATA_HOME || path.join(homeDir, ".local", "share");
  return path.join(xdg, "camoufox-profile-manager");
}

function stableUserDataDir(appDataDir) {
  return path.join(appDataDir, STABLE_DATA_DIR_NAME);
}

function legacyDataDirCandidates({ appDataDir, brandName = "", platform, homeDir, env } = {}) {
  const names = [];
  const brand = String(brandName || "").trim();
  if (brand && brand !== STABLE_DATA_DIR_NAME) {
    names.push(brand);
  }
  for (const name of LEGACY_APP_DATA_NAMES) {
    if (!names.includes(name) && name !== STABLE_DATA_DIR_NAME) {
      names.push(name);
    }
  }
  const dirs = names.map((name) => path.join(appDataDir, name));
  dirs.push(pythonDesktopDataDir({ platform, homeDir, env }));
  const stable = path.resolve(stableUserDataDir(appDataDir));
  return [...new Set(dirs.map((d) => path.resolve(d)))].filter((d) => d !== stable);
}

function _mtimeMs(file) {
  try {
    return fs.statSync(file).mtimeMs;
  } catch {
    return 0;
  }
}

function hasDatabase(dir) {
  try {
    return fs.statSync(path.join(dir, "profiles.db")).isFile();
  } catch {
    return false;
  }
}

function lastWriteMs(dir) {
  return Math.max(...DB_FILES.map((name) => _mtimeMs(path.join(dir, name))));
}

/** True when a Chromium SingletonLock in `dir` belongs to a live process on this host. */
function isDirInUse(dir) {
  let target;
  try {
    target = fs.readlinkSync(path.join(dir, "SingletonLock"));
  } catch {
    return false;
  }
  const match = /^(.*)-(\d+)$/.exec(String(target));
  if (!match) {
    return false;
  }
  if (match[1] && match[1] !== os.hostname()) {
    return false;
  }
  const pid = Number(match[2]);
  if (!Number.isInteger(pid) || pid <= 0 || pid === process.pid) {
    return false;
  }
  try {
    process.kill(pid, 0);
    return true;
  } catch (err) {
    return Boolean(err && err.code === "EPERM");
  }
}

/** Pick the most recently written legacy directory that has a database and is not in use. */
function pickLegacyDataDir(candidates) {
  let best = null;
  let bestMs = -1;
  for (const dir of candidates) {
    if (!hasDatabase(dir) || isDirInUse(dir)) {
      continue;
    }
    const ms = lastWriteMs(dir);
    if (ms > bestMs) {
      best = dir;
      bestMs = ms;
    }
  }
  return best;
}

/**
 * Recursive copy built from basic fs calls. fs.cpSync is avoided on purpose:
 * its native implementation mangles non-ASCII paths (such as this app's
 * Chinese directory name) on Windows.
 */
function copyTree(src, dest, { overwrite = false } = {}) {
  const st = fs.lstatSync(src);
  if (st.isSymbolicLink()) {
    if (!fs.existsSync(dest)) {
      fs.symlinkSync(fs.readlinkSync(src), dest);
    }
    return;
  }
  if (st.isDirectory()) {
    fs.mkdirSync(dest, { recursive: true, mode: st.mode & 0o777 });
    for (const entry of fs.readdirSync(src)) {
      copyTree(path.join(src, entry), path.join(dest, entry), { overwrite });
    }
    return;
  }
  if (!st.isFile()) {
    return; // sockets, FIFOs
  }
  if (!overwrite && fs.existsSync(dest)) {
    return;
  }
  fs.copyFileSync(src, dest);
  try {
    fs.chmodSync(dest, st.mode & 0o777);
    fs.utimesSync(dest, st.atime, st.mtime);
  } catch {
    // best effort
  }
}

/**
 * Copy (never move) a legacy data directory into the stable one. The source
 * is left untouched as a fallback copy. Refuses to run when the stable
 * directory already holds a database, so existing data is never overwritten.
 */
function adoptLegacyDataDir(sourceDir, stableDir) {
  if (hasDatabase(stableDir)) {
    return false;
  }
  fs.mkdirSync(stableDir, { recursive: true, mode: 0o700 });
  for (const entry of fs.readdirSync(sourceDir)) {
    if (SKIP_ON_ADOPT.has(entry)) {
      continue;
    }
    const target = path.join(stableDir, entry);
    // Database and key must come from the same source; replace any stray
    // half-initialised copies left by a previous empty start.
    const isCore = DB_FILES.includes(entry) || KEY_FILES.includes(entry);
    if (!isCore && fs.existsSync(target)) {
      continue;
    }
    copyTree(path.join(sourceDir, entry), target, { overwrite: isCore });
  }
  // A WAL/SHM from the stable dir must not be paired with the legacy DB.
  for (const name of ["profiles.db-wal", "profiles.db-shm"]) {
    if (!fs.existsSync(path.join(sourceDir, name))) {
      fs.rmSync(path.join(stableDir, name), { force: true });
    }
  }
  fs.writeFileSync(
    path.join(stableDir, MIGRATION_MARKER),
    JSON.stringify({ source: sourceDir, at: new Date().toISOString() }, null, 2) + "\n",
    { mode: 0o600 }
  );
  return true;
}

/**
 * Resolve the userData directory for this launch.
 * Returns `{ userDataDir, adoptedFrom }`.
 */
function resolveUserDataDir({ appDataDir, brandName, overrideDir, platform, homeDir, env, log = () => {} } = {}) {
  if (overrideDir) {
    return { userDataDir: path.resolve(overrideDir), adoptedFrom: null };
  }
  const userDataDir = stableUserDataDir(appDataDir);
  if (hasDatabase(userDataDir)) {
    return { userDataDir, adoptedFrom: null };
  }
  const source = pickLegacyDataDir(
    legacyDataDirCandidates({ appDataDir, brandName, platform, homeDir, env })
  );
  if (!source) {
    return { userDataDir, adoptedFrom: null };
  }
  try {
    if (adoptLegacyDataDir(source, userDataDir)) {
      log(`Adopted existing data from ${source}`);
      return { userDataDir, adoptedFrom: source };
    }
  } catch (err) {
    log(`Failed to adopt data from ${source}: ${err instanceof Error ? err.message : String(err)}`);
  }
  return { userDataDir, adoptedFrom: null };
}

function _readVersionMarker(userDataDir) {
  try {
    const raw = JSON.parse(fs.readFileSync(path.join(userDataDir, VERSION_MARKER), "utf8"));
    return typeof raw.version === "string" ? raw.version : null;
  } catch {
    return null;
  }
}

function _sanitize(label) {
  return String(label || "unknown").replace(/[^0-9A-Za-z._-]+/g, "_");
}

function _pruneBackups(root, keep) {
  let entries;
  try {
    entries = fs
      .readdirSync(root, { withFileTypes: true })
      .filter((d) => d.isDirectory())
      .map((d) => d.name)
      .sort();
  } catch {
    return;
  }
  while (entries.length > keep) {
    const oldest = entries.shift();
    fs.rmSync(path.join(root, oldest), { recursive: true, force: true });
  }
}

/**
 * Snapshot the database and key when the installed version differs from the
 * version that last ran on this data directory. Must run before the sidecar
 * opens the database (so the copy is consistent and pre-migration).
 * Returns the backup directory, or null when nothing was needed.
 */
function backupOnVersionChange(userDataDir, currentVersion, { keep = 5, now = new Date() } = {}) {
  const previous = _readVersionMarker(userDataDir);
  let backupDir = null;
  if (previous !== currentVersion && hasDatabase(userDataDir)) {
    const stamp = now.toISOString().replace(/[-:]/g, "").replace(/\..*$/, "");
    const root = path.join(userDataDir, UPGRADE_BACKUP_DIR);
    backupDir = path.join(root, `${stamp}_${_sanitize(previous || "earlier")}_to_${_sanitize(currentVersion)}`);
    fs.mkdirSync(backupDir, { recursive: true, mode: 0o700 });
    for (const name of [...DB_FILES, ...KEY_FILES]) {
      const src = path.join(userDataDir, name);
      if (fs.existsSync(src)) {
        fs.copyFileSync(src, path.join(backupDir, name));
        try {
          fs.chmodSync(path.join(backupDir, name), 0o600);
        } catch {
          // ignore on platforms without POSIX modes
        }
      }
    }
    _pruneBackups(root, keep);
  }
  if (previous !== currentVersion) {
    fs.mkdirSync(userDataDir, { recursive: true, mode: 0o700 });
    fs.writeFileSync(
      path.join(userDataDir, VERSION_MARKER),
      JSON.stringify({ version: currentVersion, previous, at: now.toISOString() }, null, 2) + "\n"
    );
  }
  return backupDir;
}

module.exports = {
  STABLE_DATA_DIR_NAME,
  UPGRADE_BACKUP_DIR,
  pythonDesktopDataDir,
  stableUserDataDir,
  legacyDataDirCandidates,
  pickLegacyDataDir,
  adoptLegacyDataDir,
  resolveUserDataDir,
  backupOnVersionChange,
  isDirInUse,
};
