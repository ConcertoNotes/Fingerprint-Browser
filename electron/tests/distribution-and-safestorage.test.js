"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const {
  DEFAULT_DISTRIBUTION_CONFIG,
  SidecarManager,
  ensureFernetKey,
  generateFernetKey,
  isCustomerDevToolsShortcut,
  isValidFernetKey,
  loadDistributionConfig,
} = require("../sidecar");

function createMockSafeStorage({ available = true } = {}) {
  const prefix = Buffer.from("MOCK_SAFE_V1:");
  return {
    isEncryptionAvailable() {
      return available;
    },
    encryptString(plaintext) {
      return Buffer.concat([prefix, Buffer.from(String(plaintext), "utf8").reverse()]);
    },
    decryptString(buf) {
      assert.ok(Buffer.isBuffer(buf), "decryptString expects a Buffer");
      assert.ok(
        buf.subarray(0, prefix.length).equals(prefix),
        "Encrypted buffer missing mock safeStorage prefix"
      );
      return Buffer.from(buf.subarray(prefix.length)).reverse().toString("utf8");
    },
  };
}

test("loadDistributionConfig respects env override, packaged nested/flat paths, and defaults", () => {
  const origEnv = process.env.PRISM_DISTRIBUTION_FILE;
  delete process.env.PRISM_DISTRIBUTION_FILE;
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "prism-dist-test-"));

  try {
    // 1. Default when unpackaged and no PRISM_DISTRIBUTION_FILE
    const defCfg = loadDistributionConfig({ isPackaged: false, resourcesPath: tmpDir });
    assert.deepEqual(defCfg, DEFAULT_DISTRIBUTION_CONFIG);

    // 2. Packaged resources/distribution/distribution.json
    const nestedDir = path.join(tmpDir, "pkg1", "distribution");
    fs.mkdirSync(nestedDir, { recursive: true });
    fs.writeFileSync(
      path.join(nestedDir, "distribution.json"),
      JSON.stringify({
        edition: "customer",
        cloud_url: "https://cloud.acme.example///",
        brand_name: "Acme 浏览器",
        require_activation: true,
      }),
      "utf8"
    );
    const nestedCfg = loadDistributionConfig({
      isPackaged: true,
      resourcesPath: path.join(tmpDir, "pkg1"),
    });
    assert.deepEqual(nestedCfg, {
      edition: "customer",
      cloud_url: "https://cloud.acme.example",
      brand_name: "Acme 浏览器",
      require_activation: true,
    });

    // 3. Packaged fallback resources/distribution.json
    const flatDir = path.join(tmpDir, "pkg2");
    fs.mkdirSync(flatDir, { recursive: true });
    fs.writeFileSync(
      path.join(flatDir, "distribution.json"),
      JSON.stringify({
        edition: "customer",
        cloud_url: "https://flat.example.com",
        brand_name: "",
        require_activation: false,
      }),
      "utf8"
    );
    const flatCfg = loadDistributionConfig({ isPackaged: true, resourcesPath: flatDir });
    assert.deepEqual(flatCfg, {
      edition: "customer",
      cloud_url: "https://flat.example.com",
      brand_name: "棱镜浏览器",
      require_activation: false,
    });

    // 4. PRISM_DISTRIBUTION_FILE overrides packaged config
    const envCfgPath = path.join(tmpDir, "override.json");
    fs.writeFileSync(
      envCfgPath,
      JSON.stringify({
        edition: "standard",
        cloud_url: "   ",
        brand_name: "Override Brand",
        require_activation: 0,
      }),
      "utf8"
    );
    process.env.PRISM_DISTRIBUTION_FILE = envCfgPath;
    const overridden = loadDistributionConfig({
      isPackaged: true,
      resourcesPath: path.join(tmpDir, "pkg1"),
    });
    assert.deepEqual(overridden, {
      edition: "standard",
      cloud_url: null,
      brand_name: "Override Brand",
      require_activation: false,
    });

    // 5. Invalid JSON fails closed with explicit error
    const badPath = path.join(tmpDir, "bad.json");
    fs.writeFileSync(badPath, "{invalid-json", "utf8");
    process.env.PRISM_DISTRIBUTION_FILE = badPath;
    assert.throws(() => loadDistributionConfig({ isPackaged: false }), /Invalid distribution\.json/);
  } finally {
    if (origEnv === undefined) {
      delete process.env.PRISM_DISTRIBUTION_FILE;
    } else {
      process.env.PRISM_DISTRIBUTION_FILE = origEnv;
    }
    fs.rmSync(tmpDir, { recursive: true, force: true });
  }
});

test("ensureFernetKey with safeStorage creates, reloads, migrates plaintext secret.key, and handles fallback", () => {
  const origEnvKey = process.env.CPM_SECRET_KEY;
  delete process.env.CPM_SECRET_KEY;
  const tmpRoot = fs.mkdtempSync(path.join(os.tmpdir(), "prism-safe-key-"));
  const safeStorage = createMockSafeStorage({ available: true });
  const unavailableSafeStorage = createMockSafeStorage({ available: false });

  try {
    // 1. Fresh creation encrypts into secret.key.enc (mode 0o600) with no plaintext secret.key
    const freshDir = path.join(tmpRoot, "fresh");
    const created = ensureFernetKey(freshDir, undefined, { safeStorage });
    assert.ok(isValidFernetKey(created));
    const encPath = path.join(freshDir, "secret.key.enc");
    const plainPath = path.join(freshDir, "secret.key");
    assert.equal(fs.existsSync(encPath), true);
    assert.equal(fs.existsSync(plainPath), false);
    assert.equal(fs.readFileSync(encPath).includes(Buffer.from(created, "utf8")), false);
    if (process.platform !== "win32") {
      assert.equal(fs.statSync(encPath).mode & 0o777, 0o600);
    }

    // 2. Reload decrypts identical key from secret.key.enc
    const reloaded = ensureFernetKey(freshDir, undefined, { safeStorage });
    assert.equal(reloaded, created);

    // 3. Refuses to regenerate when secret.key.enc exists but safeStorage becomes unavailable
    assert.throws(
      () => ensureFernetKey(freshDir, undefined, { safeStorage: unavailableSafeStorage }),
      /OS safeStorage is unavailable/
    );

    // 4. Legacy plaintext secret.key migrates atomically to secret.key.enc and removes plaintext
    const legacyDir = path.join(tmpRoot, "legacy");
    fs.mkdirSync(legacyDir, { recursive: true });
    const legacyKey = generateFernetKey();
    fs.writeFileSync(path.join(legacyDir, "secret.key"), legacyKey, { mode: 0o600 });
    const migrated = ensureFernetKey(legacyDir, undefined, { safeStorage });
    assert.equal(migrated, legacyKey);
    assert.equal(fs.existsSync(path.join(legacyDir, "secret.key")), false);
    assert.equal(fs.existsSync(path.join(legacyDir, "secret.key.enc")), true);
    if (process.platform !== "win32") {
      assert.equal(fs.statSync(path.join(legacyDir, "secret.key.enc")).mode & 0o777, 0o600);
    }
    assert.equal(ensureFernetKey(legacyDir, undefined, { safeStorage }), legacyKey);

    // 5. Existing profiles.db with neither key file still throws refusal error
    const orphanDbDir = path.join(tmpRoot, "orphan-db");
    fs.mkdirSync(orphanDbDir, { recursive: true });
    fs.writeFileSync(path.join(orphanDbDir, "profiles.db"), "sqlite-header");
    assert.throws(
      () => ensureFernetKey(orphanDbDir, undefined, { safeStorage }),
      /Missing secret\.key/
    );

    // 6. Unavailable safeStorage seamlessly falls back to plaintext secret.key (mode 0o600)
    const fallbackDir = path.join(tmpRoot, "fallback");
    const fallbackKey = ensureFernetKey(fallbackDir, undefined, {
      safeStorage: unavailableSafeStorage,
    });
    assert.ok(isValidFernetKey(fallbackKey));
    assert.equal(fs.existsSync(path.join(fallbackDir, "secret.key")), true);
    assert.equal(fs.existsSync(path.join(fallbackDir, "secret.key.enc")), false);
    assert.equal(
      ensureFernetKey(fallbackDir, undefined, { safeStorage: unavailableSafeStorage }),
      fallbackKey
    );

    // 7. CPM_SECRET_KEY env var still takes precedence
    const envKey = generateFernetKey();
    process.env.CPM_SECRET_KEY = envKey;
    assert.equal(ensureFernetKey(orphanDbDir, undefined, { safeStorage }), envKey);
  } finally {
    if (origEnvKey === undefined) {
      delete process.env.CPM_SECRET_KEY;
    } else {
      process.env.CPM_SECRET_KEY = origEnvKey;
    }
    fs.rmSync(tmpRoot, { recursive: true, force: true });
  }
});

test("SidecarManager.buildEnv injects distribution env vars for customer and standard editions", () => {
  const origDistEnv = process.env.PRISM_DISTRIBUTION_FILE;
  delete process.env.PRISM_DISTRIBUTION_FILE;
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "prism-sidecar-env-"));

  try {
    // 1. Packaged customer build locks distribution env vars against shell tampering
    const resDir = path.join(tmpDir, "resources");
    fs.mkdirSync(path.join(resDir, "distribution"), { recursive: true });
    fs.writeFileSync(
      path.join(resDir, "distribution", "distribution.json"),
      JSON.stringify({
        edition: "customer",
        cloud_url: "https://cloud.prism.example/",
        brand_name: "Prism Pro",
        require_activation: true,
      }),
      "utf8"
    );

    const customerSidecar = new SidecarManager({
      isPackaged: true,
      resourcesPath: resDir,
      userDataDir: path.join(tmpDir, "userData"),
      repoRoot: tmpDir,
    });
    const customerEnv = customerSidecar.buildEnv({
      port: 18765,
      dbPath: "/tmp/profiles.db",
      secretKey: generateFernetKey(),
      baseEnv: {
        CPM_CLIENT_EDITION: "standard",
        CPM_CLOUD_URL: "http://evil.local",
        CPM_BRAND_NAME: "Tampered",
        CPM_REQUIRE_ACTIVATION: "0",
      },
    });
    assert.equal(customerEnv.CPM_CLIENT_EDITION, "customer");
    assert.equal(customerEnv.CPM_CLOUD_URL, "https://cloud.prism.example");
    assert.equal(customerEnv.CPM_BRAND_NAME, "Prism Pro");
    assert.equal(customerEnv.CPM_REQUIRE_ACTIVATION, "1");
    assert.equal(customerEnv.CPM_BUNDLED_RUNTIME_DIR, path.join(resDir, "runtime"));

    // 2. Packaged standard build omits CPM_CLOUD_URL when null
    const stdResDir = path.join(tmpDir, "std-resources");
    fs.mkdirSync(path.join(stdResDir, "distribution"), { recursive: true });
    fs.writeFileSync(
      path.join(stdResDir, "distribution", "distribution.json"),
      JSON.stringify({
        edition: "standard",
        cloud_url: null,
        brand_name: "棱镜浏览器",
        require_activation: false,
      }),
      "utf8"
    );
    const stdSidecar = new SidecarManager({
      isPackaged: true,
      resourcesPath: stdResDir,
      userDataDir: path.join(tmpDir, "userData"),
      repoRoot: tmpDir,
    });
    const stdEnv = stdSidecar.buildEnv({
      port: 18766,
      dbPath: "/tmp/profiles.db",
      secretKey: generateFernetKey(),
      baseEnv: { CPM_CLOUD_URL: "https://stale.example" },
    });
    assert.equal(stdEnv.CPM_CLIENT_EDITION, "standard");
    assert.equal("CPM_CLOUD_URL" in stdEnv, false);
    assert.equal(stdEnv.CPM_BRAND_NAME, "棱镜浏览器");
    assert.equal(stdEnv.CPM_REQUIRE_ACTIVATION, "0");

    // 3. Unpackaged dev mode defaults cleanly while allowing local env testing
    const devSidecar = new SidecarManager({
      isPackaged: false,
      resourcesPath: "",
      userDataDir: path.join(tmpDir, "userData"),
      repoRoot: tmpDir,
    });
    const devDefaultEnv = devSidecar.buildEnv({
      port: 18767,
      dbPath: "/tmp/profiles.db",
      secretKey: generateFernetKey(),
      baseEnv: {},
    });
    assert.equal(devDefaultEnv.CPM_CLIENT_EDITION, "standard");
    assert.equal(devDefaultEnv.CPM_BRAND_NAME, "棱镜浏览器");
    assert.equal(devDefaultEnv.CPM_REQUIRE_ACTIVATION, "0");
    assert.equal("CPM_CLOUD_URL" in devDefaultEnv, false);
  } finally {
    if (origDistEnv === undefined) {
      delete process.env.PRISM_DISTRIBUTION_FILE;
    } else {
      process.env.PRISM_DISTRIBUTION_FILE = origDistEnv;
    }
    fs.rmSync(tmpDir, { recursive: true, force: true });
  }
});

test("customer DevTools guard blocks F12 / Cmd+Opt+I / Ctrl+Shift+I and main.js + builder config match contract", () => {
  assert.equal(isCustomerDevToolsShortcut({ key: "F12" }), true);
  assert.equal(isCustomerDevToolsShortcut({ key: "f12" }), true);
  assert.equal(isCustomerDevToolsShortcut({ key: "I", meta: true, alt: true }), true);
  assert.equal(isCustomerDevToolsShortcut({ key: "i", control: true, shift: true }), true);
  assert.equal(isCustomerDevToolsShortcut({ key: "J", meta: true, alt: true }), true);
  assert.equal(isCustomerDevToolsShortcut({ key: "j", control: true, shift: true }), true);
  assert.equal(isCustomerDevToolsShortcut({ key: "i" }), false);
  assert.equal(isCustomerDevToolsShortcut({ key: "c", meta: true }), false);
  assert.equal(isCustomerDevToolsShortcut({ key: "r", control: true }), false);

  const mainSrc = fs.readFileSync(path.join(__dirname, "..", "main.js"), "utf8");
  assert.ok(mainSrc.includes("safeStorage"));
  assert.ok(mainSrc.includes("loadDistributionConfig"));
  assert.ok(mainSrc.includes("app.setName(brandName)"));
  assert.ok(mainSrc.includes('distribution.edition === "customer" && app.isPackaged'));
  assert.ok(mainSrc.includes('"devtools-opened"'));
  assert.ok(mainSrc.includes("closeDevTools()"));
  assert.ok(mainSrc.includes('"before-input-event"'));

  const builderYml = fs.readFileSync(path.join(__dirname, "..", "electron-builder.yml"), "utf8");
  assert.ok(builderYml.includes("../build/distribution"));
  assert.ok(builderYml.includes('to: "distribution"'));
});
