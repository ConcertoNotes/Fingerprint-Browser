"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const http = require("node:http");
const net = require("node:net");
const path = require("node:path");
const { spawn } = require("node:child_process");

const DEFAULT_DISTRIBUTION_CONFIG = Object.freeze({
  edition: "standard",
  cloud_url: null,
  brand_name: "棱镜浏览器",
  require_activation: false,
});

function normalizeDistributionConfig(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    throw new Error("Distribution config must be a JSON object");
  }
  const edition = raw.edition === "customer" ? "customer" : "standard";
  const cloudUrl =
    typeof raw.cloud_url === "string" && raw.cloud_url.trim()
      ? raw.cloud_url.trim().replace(/\/+$/, "")
      : null;
  const brandName =
    typeof raw.brand_name === "string" && raw.brand_name.trim()
      ? raw.brand_name.trim()
      : DEFAULT_DISTRIBUTION_CONFIG.brand_name;
  const requireActivation = Boolean(raw.require_activation);
  return {
    edition,
    cloud_url: cloudUrl,
    brand_name: brandName,
    require_activation: requireActivation,
  };
}

function _resolveDistributionConfig({ isPackaged = false, resourcesPath = "", repoRoot = "" } = {}) {
  void repoRoot;
  const candidates = [];
  const envFile = process.env.PRISM_DISTRIBUTION_FILE && process.env.PRISM_DISTRIBUTION_FILE.trim();
  if (envFile) {
    candidates.push(path.resolve(envFile));
  }
  if (isPackaged && resourcesPath) {
    candidates.push(path.join(resourcesPath, "distribution", "distribution.json"));
    candidates.push(path.join(resourcesPath, "distribution.json"));
  }

  for (const candidate of candidates) {
    if (!fs.existsSync(candidate)) {
      continue;
    }
    let parsed;
    try {
      parsed = JSON.parse(fs.readFileSync(candidate, "utf8"));
    } catch (err) {
      throw new Error(
        `Invalid distribution.json at ${candidate}: ${err instanceof Error ? err.message : String(err)}`
      );
    }
    return {
      config: normalizeDistributionConfig(parsed),
      fromFile: true,
      sourcePath: candidate,
    };
  }

  return {
    config: { ...DEFAULT_DISTRIBUTION_CONFIG },
    fromFile: false,
    sourcePath: null,
  };
}

/**
 * Load distribution configuration for standard or customer editions.
 * Priority:
 *   1) process.env.PRISM_DISTRIBUTION_FILE (if set and exists)
 *   2) packaged resources/distribution/distribution.json (or resources/distribution.json)
 *   3) default standard edition config
 */
function loadDistributionConfig(opts = {}) {
  return _resolveDistributionConfig(opts).config;
}

function isCustomerDevToolsShortcut(input) {
  if (!input || typeof input !== "object") {
    return false;
  }
  const key = String(input.key || "").toLowerCase();
  if (key === "f12") {
    return true;
  }
  if (input.meta && input.alt && (key === "i" || key === "j")) {
    return true;
  }
  if (input.control && input.shift && (key === "i" || key === "j")) {
    return true;
  }
  return false;
}

function isValidFernetKey(raw) {
  if (typeof raw !== "string") {
    return false;
  }
  const trimmed = raw.trim();
  if (trimmed.length !== 44 || !/^[A-Za-z0-9_-]{43}=$/.test(trimmed)) {
    return false;
  }
  const standard = trimmed.replace(/-/g, "+").replace(/_/g, "/");
  try {
    return Buffer.from(standard, "base64").length === 32;
  } catch {
    return false;
  }
}

function generateFernetKey() {
  // Exact equivalent of Python cryptography.fernet.Fernet.generate_key()
  return crypto.randomBytes(32).toString("base64").replace(/\+/g, "-").replace(/\//g, "_");
}

function _canUseSafeStorage(safeStorage) {
  if (
    !safeStorage ||
    typeof safeStorage.isEncryptionAvailable !== "function" ||
    typeof safeStorage.encryptString !== "function" ||
    typeof safeStorage.decryptString !== "function"
  ) {
    return false;
  }
  try {
    return Boolean(safeStorage.isEncryptionAvailable());
  } catch {
    return false;
  }
}

function _writeBufferAtomically(targetPath, buffer) {
  const dir = path.dirname(targetPath);
  const tmpName = `.${path.basename(targetPath)}.tmp.${process.pid}.${crypto.randomBytes(6).toString("hex")}`;
  const tmpPath = path.join(dir, tmpName);
  const fd = fs.openSync(
    tmpPath,
    fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL,
    0o600
  );
  try {
    fs.writeSync(fd, buffer, 0, buffer.length, 0);
    fs.fsyncSync(fd);
  } catch (err) {
    try {
      fs.closeSync(fd);
    } catch {
      // ignore
    }
    try {
      fs.unlinkSync(tmpPath);
    } catch {
      // ignore
    }
    throw err;
  }
  fs.closeSync(fd);
  try {
    fs.renameSync(tmpPath, targetPath);
  } catch (err) {
    try {
      fs.unlinkSync(tmpPath);
    } catch {
      // ignore
    }
    throw err;
  }
}

function _decryptKeyFile(encKeyPath, safeStorage) {
  const encryptedBuf = fs.readFileSync(encKeyPath);
  let decrypted;
  try {
    decrypted = String(safeStorage.decryptString(encryptedBuf)).trim();
  } catch (err) {
    throw new Error(
      `Failed to decrypt Fernet secret key at ${encKeyPath}: ${err instanceof Error ? err.message : String(err)}`
    );
  }
  if (!isValidFernetKey(decrypted)) {
    throw new Error(`Invalid Fernet secret key at ${encKeyPath}`);
  }
  return decrypted;
}

/**
 * Load or atomically create a Fernet key inside userData.
 * When Electron safeStorage encryption is available, stores the key encrypted
 * in secret.key.enc (mode 0o600) and migrates any legacy plaintext secret.key.
 * Otherwise falls back seamlessly to secret.key (mode 0o600).
 * Never silently replaces a missing key if profiles.db already exists.
 */
function ensureFernetKey(userDataDir, dbPath, { safeStorage } = {}) {
  if (process.env.CPM_SECRET_KEY && isValidFernetKey(process.env.CPM_SECRET_KEY)) {
    return process.env.CPM_SECRET_KEY.trim();
  }

  fs.mkdirSync(userDataDir, { recursive: true, mode: 0o700 });
  const keyPath = path.join(userDataDir, "secret.key");
  const encKeyPath = path.join(userDataDir, "secret.key.enc");
  const resolvedDbPath = dbPath || path.join(userDataDir, "profiles.db");

  if (_canUseSafeStorage(safeStorage)) {
    if (fs.existsSync(encKeyPath)) {
      const decrypted = _decryptKeyFile(encKeyPath, safeStorage);
      // Finish cleanup if an earlier migration crashed after rename but before unlink.
      if (fs.existsSync(keyPath)) {
        try {
          if (fs.readFileSync(keyPath, "utf8").trim() === decrypted) {
            fs.unlinkSync(keyPath);
          }
        } catch {
          // ignore cleanup failure
        }
      }
      return decrypted;
    }

    if (fs.existsSync(keyPath)) {
      const existing = fs.readFileSync(keyPath, "utf8").trim();
      if (!isValidFernetKey(existing)) {
        throw new Error(`Invalid Fernet secret key at ${keyPath}`);
      }
      const encrypted = safeStorage.encryptString(existing);
      if (!Buffer.isBuffer(encrypted) || String(safeStorage.decryptString(encrypted)).trim() !== existing) {
        throw new Error(`OS safeStorage round-trip check failed while migrating ${keyPath}`);
      }
      _writeBufferAtomically(encKeyPath, encrypted);
      fs.unlinkSync(keyPath);
      return existing;
    }

    if (fs.existsSync(resolvedDbPath)) {
      throw new Error(
        `Missing secret.key alongside existing database ${resolvedDbPath}; refusing to generate a replacement key.`
      );
    }

    const generated = generateFernetKey();
    const encrypted = safeStorage.encryptString(generated);
    try {
      const fd = fs.openSync(
        encKeyPath,
        fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL,
        0o600
      );
      try {
        fs.writeSync(fd, encrypted, 0, encrypted.length, 0);
        fs.fsyncSync(fd);
      } finally {
        fs.closeSync(fd);
      }
      return generated;
    } catch (err) {
      if (err && err.code === "EEXIST") {
        return _decryptKeyFile(encKeyPath, safeStorage);
      }
      throw err;
    }
  }

  if (fs.existsSync(encKeyPath) && !fs.existsSync(keyPath)) {
    throw new Error(
      `Encrypted secret key ${encKeyPath} exists but OS safeStorage is unavailable; refusing to generate a replacement key.`
    );
  }

  if (fs.existsSync(keyPath)) {
    const existing = fs.readFileSync(keyPath, "utf8").trim();
    if (!isValidFernetKey(existing)) {
      throw new Error(`Invalid Fernet secret key at ${keyPath}`);
    }
    return existing;
  }

  if (fs.existsSync(resolvedDbPath)) {
    throw new Error(
      `Missing secret.key alongside existing database ${resolvedDbPath}; refusing to generate a replacement key.`
    );
  }

  const generated = generateFernetKey();
  try {
    const fd = fs.openSync(
      keyPath,
      fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL,
      0o600
    );
    try {
      fs.writeSync(fd, generated, 0, "utf8");
      fs.fsyncSync(fd);
    } finally {
      fs.closeSync(fd);
    }
    return generated;
  } catch (err) {
    if (err && err.code === "EEXIST") {
      const raced = fs.readFileSync(keyPath, "utf8").trim();
      if (!isValidFernetKey(raced)) {
        throw new Error(`Invalid Fernet secret key at ${keyPath}`);
      }
      return raced;
    }
    throw err;
  }
}

function allocateFreePort(host = "127.0.0.1") {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.on("error", reject);
    server.listen(0, host, () => {
      const addr = server.address();
      const port = typeof addr === "object" && addr ? addr.port : 0;
      server.close((closeErr) => {
        if (closeErr) {
          reject(closeErr);
        } else {
          resolve(port);
        }
      });
    });
  });
}

function httpJsonRequest(urlString, { method = "GET", headers = {}, body = null, timeoutMs = 15000 } = {}) {
  return new Promise((resolve, reject) => {
    const target = new URL(urlString);
    const payload = body == null ? null : typeof body === "string" ? body : JSON.stringify(body);
    const reqHeaders = { ...headers };
    if (payload !== null) {
      reqHeaders["Content-Type"] = "application/json";
      reqHeaders["Content-Length"] = Buffer.byteLength(payload);
    }

    const req = http.request(
      {
        protocol: target.protocol,
        hostname: target.hostname,
        port: target.port,
        path: `${target.pathname}${target.search}`,
        method,
        headers: reqHeaders,
        timeout: timeoutMs,
      },
      (res) => {
        const chunks = [];
        res.on("data", (chunk) => chunks.push(chunk));
        res.on("end", () => {
          const text = Buffer.concat(chunks).toString("utf8");
          let json = null;
          if (text) {
            try {
              json = JSON.parse(text);
            } catch {
              json = null;
            }
          }
          resolve({ status: res.statusCode || 0, json, text });
        });
      }
    );

    req.on("timeout", () => {
      req.destroy(new Error(`Sidecar request timed out after ${timeoutMs}ms`));
    });
    req.on("error", reject);
    if (payload !== null) {
      req.write(payload);
    }
    req.end();
  });
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function parseErrorPayload(status, json, fallbackText) {
  const errObj = json && typeof json === "object" && json.error && typeof json.error === "object"
    ? json.error
    : null;
  if (errObj) {
    return {
      ok: false,
      status,
      code: String(errObj.code || `http_${status}`),
      message: String(errObj.message || fallbackText || `Request failed (${status})`),
      details: errObj.details ?? null,
    };
  }
  const detail = json && typeof json === "object" ? json.detail : null;
  if (detail && typeof detail === "object") {
    return {
      ok: false,
      status,
      code: String(detail.code || `http_${status}`),
      message: String(detail.message || fallbackText || `Request failed (${status})`),
      details: detail.details ?? null,
    };
  }
  return {
    ok: false,
    status,
    code: `http_${status}`,
    message: typeof detail === "string" ? detail : fallbackText || `Request failed (${status})`,
    details: null,
  };
}

class SidecarManager {
  constructor({
    isPackaged,
    resourcesPath,
    userDataDir,
    repoRoot,
    safeStorage = null,
    distribution = null,
  }) {
    this.isPackaged = Boolean(isPackaged);
    this.resourcesPath = resourcesPath || "";
    this.userDataDir = userDataDir;
    this.repoRoot = repoRoot || path.resolve(__dirname, "..");
    this.safeStorage = safeStorage || null;

    if (distribution && typeof distribution === "object") {
      this.distribution = normalizeDistributionConfig(distribution);
      this._hasExplicitDistribution = true;
    } else {
      const resolved = _resolveDistributionConfig({
        isPackaged: this.isPackaged,
        resourcesPath: this.resourcesPath,
        repoRoot: this.repoRoot,
      });
      this.distribution = resolved.config;
      this._hasExplicitDistribution = resolved.fromFile;
    }

    this.desktopToken =
      (!this.isPackaged && process.env.CPM_DESKTOP_TOKEN) || crypto.randomBytes(32).toString("hex");
    this.baseUrl = null;
    this.port = null;
    this.child = null;
    this.stderrRing = [];
    this.stopping = false;
  }

  buildEnv({ port, dbPath, secretKey, baseEnv = process.env }) {
    const env = {
      ...baseEnv,
      CPM_HOST: "127.0.0.1",
      CPM_PORT: String(port),
      CPM_DB_PATH: dbPath,
      CPM_SECRET_KEY: secretKey,
      CPM_DESKTOP_TOKEN: this.desktopToken,
    };

    const authoritative = this.isPackaged || this._hasExplicitDistribution;
    env.CPM_CLIENT_EDITION = authoritative
      ? this.distribution.edition
      : baseEnv.CPM_CLIENT_EDITION || this.distribution.edition;
    env.CPM_BRAND_NAME = authoritative
      ? this.distribution.brand_name
      : baseEnv.CPM_BRAND_NAME || this.distribution.brand_name;
    env.CPM_REQUIRE_ACTIVATION = authoritative
      ? this.distribution.require_activation
        ? "1"
        : "0"
      : baseEnv.CPM_REQUIRE_ACTIVATION ?? (this.distribution.require_activation ? "1" : "0");

    if (this.distribution.cloud_url) {
      env.CPM_CLOUD_URL = this.distribution.cloud_url;
    } else if (this.isPackaged) {
      delete env.CPM_CLOUD_URL;
    }

    if (this.isPackaged) {
      env.CPM_BUNDLED_RUNTIME_DIR = path.join(this.resourcesPath, "runtime");
    }

    return env;
  }

  _recordStderr(chunk) {
    const text = chunk.toString("utf8");
    this.stderrRing.push(text);
    if (this.stderrRing.length > 40) {
      this.stderrRing.shift();
    }
  }

  getStderrSummary() {
    return this.stderrRing.join("").trim().slice(-2000);
  }

  async start() {
    if (this.baseUrl) {
      return this.baseUrl;
    }

    // External dev backend override (e.g. developer already running uvicorn)
    if (!this.isPackaged && process.env.CPM_DEV_BACKEND_URL) {
      const normalized = process.env.CPM_DEV_BACKEND_URL.replace(/\/+$/, "");
      this.baseUrl = normalized;
      this.port = Number(new URL(normalized).port) || 80;
      await this._waitForHealth(20000);
      return this.baseUrl;
    }

    const dbPath =
      (!this.isPackaged && process.env.CPM_DB_PATH) || path.join(this.userDataDir, "profiles.db");
    const secretKey = ensureFernetKey(this.userDataDir, dbPath, { safeStorage: this.safeStorage });
    const port = await allocateFreePort("127.0.0.1");
    this.port = port;
    this.baseUrl = `http://127.0.0.1:${port}`;

    const env = this.buildEnv({ port, dbPath, secretKey });

    let command;
    let args;
    let cwd;

    if (this.isPackaged) {
      const exeName = process.platform === "win32" ? "prism-backend.exe" : "prism-backend";
      command = path.join(this.resourcesPath, "backend", exeName);
      args = ["--host", "127.0.0.1", "--port", String(port)];
      cwd = this.userDataDir;
    } else {
      command = process.platform === "win32" ? "uv.exe" : "uv";
      args = [
        "run",
        "python",
        "-m",
        "camoufox_pm.sidecar",
        "--host",
        "127.0.0.1",
        "--port",
        String(port),
      ];
      cwd = this.repoRoot;
    }

    this.stopping = false;
    this.stderrRing = [];
    this.child = spawn(command, args, {
      cwd,
      env,
      stdio: ["ignore", "pipe", "pipe"],
      windowsHide: true,
    });

    this.child.stderr.on("data", (chunk) => this._recordStderr(chunk));
    this.child.stdout.on("data", (chunk) => this._recordStderr(chunk));

    await this._waitForHealth(30000);
    return this.baseUrl;
  }

  async _waitForHealth(timeoutMs) {
    const deadline = Date.now() + timeoutMs;
    let childExited = null;

    const onExit = (code, signal) => {
      childExited = { code, signal };
    };
    if (this.child) {
      this.child.once("exit", onExit);
    }

    try {
      while (Date.now() < deadline) {
        if (childExited) {
          throw new Error(
            `Backend sidecar exited prematurely (code=${childExited.code}, signal=${childExited.signal}): ${this.getStderrSummary()}`
          );
        }
        try {
          const res = await httpJsonRequest(`${this.baseUrl}/health`, {
            method: "GET",
            timeoutMs: 2000,
          });
          if (res.status === 200) {
            return;
          }
        } catch {
          // Sidecar still starting up.
        }
        await delay(150);
      }
      throw new Error(`Backend sidecar did not become healthy within ${timeoutMs}ms: ${this.getStderrSummary()}`);
    } finally {
      if (this.child) {
        this.child.removeListener("exit", onExit);
      }
    }
  }

  async prepareProfileSession(profileId, { allowIpDrift = false } = {}) {
    if (!this.baseUrl) {
      return {
        ok: false,
        status: 503,
        code: "sidecar_unavailable",
        message: "本地核心服务尚未就绪",
        details: null,
      };
    }
    const url = `${this.baseUrl}/api/v1/profiles/${encodeURIComponent(profileId)}/session/prepare`;
    try {
      const res = await httpJsonRequest(url, {
        method: "POST",
        headers: {
          "X-Prism-Desktop-Token": this.desktopToken,
        },
        body: { allow_ip_drift: Boolean(allowIpDrift) },
        timeoutMs: 45000,
      });

      if (res.status >= 200 && res.status < 300 && res.json && res.json.data) {
        return { ok: true, data: res.json.data };
      }
      return parseErrorPayload(res.status, res.json, res.text);
    } catch (err) {
      return {
        ok: false,
        status: 502,
        code: "sidecar_request_failed",
        message: err instanceof Error ? err.message : "无法连接本地核心服务",
        details: null,
      };
    }
  }

  async releaseProfileSession(profileId) {
    if (!this.baseUrl) {
      return { ok: false };
    }
    const url = `${this.baseUrl}/api/v1/profiles/${encodeURIComponent(profileId)}/session/release`;
    try {
      const res = await httpJsonRequest(url, {
        method: "POST",
        headers: {
          "X-Prism-Desktop-Token": this.desktopToken,
        },
        body: {},
        timeoutMs: 10000,
      });
      return { ok: res.status >= 200 && res.status < 300 };
    } catch {
      return { ok: false };
    }
  }

  async stop() {
    this.stopping = true;
    const child = this.child;
    this.child = null;
    if (!child || child.exitCode !== null || child.killed) {
      return;
    }

    await new Promise((resolve) => {
      let settled = false;
      const done = () => {
        if (!settled) {
          settled = true;
          resolve();
        }
      };

      const killTimer = setTimeout(() => {
        try {
          child.kill("SIGKILL");
        } catch {
          // ignore
        }
        done();
      }, 4000);
      killTimer.unref();

      child.once("exit", () => {
        clearTimeout(killTimer);
        done();
      });

      try {
        child.kill("SIGTERM");
      } catch {
        clearTimeout(killTimer);
        done();
      }
    });
  }
}

module.exports = {
  DEFAULT_DISTRIBUTION_CONFIG,
  SidecarManager,
  ensureFernetKey,
  generateFernetKey,
  isCustomerDevToolsShortcut,
  isValidFernetKey,
  loadDistributionConfig,
  normalizeDistributionConfig,
  parseErrorPayload,
};
