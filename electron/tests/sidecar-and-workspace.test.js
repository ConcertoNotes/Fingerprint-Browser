"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { EventEmitter } = require("node:events");
const {
  ensureFernetKey,
  generateFernetKey,
  isValidFernetKey,
  parseErrorPayload,
} = require("../sidecar");
const {
  cookieToPortableItem,
  portableItemToElectronSetDetails,
} = require("../cookies");
const { WorkspaceManager, partitionNameForProfile } = require("../workspace-manager");

test("Fernet secret key generator creates 44-char Python-compatible keys and persists with 0600", () => {
  const key = generateFernetKey();
  assert.equal(key.length, 44);
  assert.ok(isValidFernetKey(key));
  assert.equal(isValidFernetKey("not-a-key"), false);

  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "prism-key-test-"));
  const origEnvKey = process.env.CPM_SECRET_KEY;
  delete process.env.CPM_SECRET_KEY;
  try {
    const first = ensureFernetKey(tmpDir);
    const second = ensureFernetKey(tmpDir);
    assert.equal(first, second);
    assert.ok(isValidFernetKey(first));
    if (process.platform !== "win32") {
      const mode = fs.statSync(path.join(tmpDir, "secret.key")).mode & 0o777;
      assert.equal(mode, 0o600);
    }

    // Refuses to silently fabricate a key if profiles.db already exists without secret.key
    const tmpDir2 = fs.mkdtempSync(path.join(os.tmpdir(), "prism-key-db-"));
    fs.writeFileSync(path.join(tmpDir2, "profiles.db"), "sqlite-stub");
    assert.throws(() => ensureFernetKey(tmpDir2), /Missing secret\.key/);
    fs.rmSync(tmpDir2, { recursive: true, force: true });
  } finally {
    if (origEnvKey !== undefined) {
      process.env.CPM_SECRET_KEY = origEnvKey;
    }
    fs.rmSync(tmpDir, { recursive: true, force: true });
  }
});

test("parseErrorPayload preserves structured 409 exit_ip_drift details cleanly", () => {
  const parsed = parseErrorPayload(
    409,
    {
      error: {
        code: "exit_ip_drift",
        message: "Exit IP changed from US to JP",
        details: [
          {
            previous_ip: "1.2.3.4",
            previous_country: "US",
            current_ip: "5.6.7.8",
            current_country: "JP",
            country_changed: true,
          },
        ],
      },
    },
    ""
  );
  assert.deepEqual(parsed, {
    ok: false,
    status: 409,
    code: "exit_ip_drift",
    message: "Exit IP changed from US to JP",
    details: [
      {
        previous_ip: "1.2.3.4",
        previous_country: "US",
        current_ip: "5.6.7.8",
        current_country: "JP",
        country_changed: true,
      },
    ],
  });
});

test("cookie conversion preserves hostOnly (__Host-*) vs domain cookies and SameSite", () => {
  const hostOnlyDetails = portableItemToElectronSetDetails({
    name: "__Host-sid",
    value: "abc123",
    domain: "example.com",
    path: "/",
    secure: true,
    httpOnly: true,
    sameSite: "Strict",
    hostOnly: true,
    expires: -1,
  });
  assert.equal(hostOnlyDetails.url, "https://example.com/");
  assert.equal("domain" in hostOnlyDetails, false);
  assert.equal("expirationDate" in hostOnlyDetails, false);
  assert.equal(hostOnlyDetails.sameSite, "strict");

  const domainDetails = portableItemToElectronSetDetails({
    name: "pref",
    value: "1",
    domain: ".amazon.com",
    path: "/",
    secure: true,
    httpOnly: false,
    sameSite: "None",
    expires: 1900000000,
  });
  assert.equal(domainDetails.domain, ".amazon.com");
  assert.equal(domainDetails.sameSite, "no_restriction");
  assert.equal(domainDetails.expirationDate, 1900000000);

  const exported = cookieToPortableItem({
    name: "pref",
    value: "1",
    domain: ".amazon.com",
    path: "/",
    secure: true,
    httpOnly: false,
    sameSite: "no_restriction",
    hostOnly: false,
    session: false,
    expirationDate: 1900000000,
  });
  assert.equal(exported.sameSite, "None");
  assert.equal(exported.expires, 1900000000);
});

function createMockElectronHarness() {
  const attachedViews = [];
  const sentMessages = [];
  const sessions = new Map();

  const mainWindow = {
    isDestroyed: () => false,
    webContents: {
      isDestroyed: () => false,
      send(channel, payload) {
        sentMessages.push({ channel, payload });
      },
    },
    contentView: {
      addChildView(view) {
        attachedViews.push(view);
      },
      removeChildView(view) {
        const idx = attachedViews.indexOf(view);
        if (idx >= 0) attachedViews.splice(idx, 1);
      },
    },
  };

  const sessionModule = {
    fromPartition(partition) {
      if (sessions.has(partition)) {
        return sessions.get(partition);
      }
      const cookieStore = [];
      const ses = {
        partition,
        proxyConfig: null,
        userAgent: null,
        acceptLanguage: null,
        beforeSendHandler: null,
        cleared: false,
        async setProxy(cfg) {
          this.proxyConfig = cfg;
        },
        setUserAgent(ua, acceptLang) {
          this.userAgent = ua;
          this.acceptLanguage = acceptLang;
        },
        webRequest: {
          onBeforeSendHeaders(fn) {
            ses.beforeSendHandler = fn;
          },
        },
        cookies: {
          async get() {
            return [...cookieStore];
          },
          async set(details) {
            cookieStore.push({
              name: details.name,
              value: details.value,
              domain: details.domain || new URL(details.url).hostname,
              path: details.path || "/",
              secure: Boolean(details.secure),
              httpOnly: Boolean(details.httpOnly),
              sameSite: details.sameSite || "lax",
              hostOnly: !details.domain,
              session: details.expirationDate == null,
              expirationDate: details.expirationDate,
            });
          },
          async remove(url, name) {
            for (let i = cookieStore.length - 1; i >= 0; i -= 1) {
              if (cookieStore[i].name === name) {
                cookieStore.splice(i, 1);
              }
            }
          },
          async flushStore() {},
        },
        async clearStorageData() {
          cookieStore.length = 0;
          this.cleared = true;
        },
        async clearCache() {},
        async clearAuthCache() {},
      };
      sessions.set(partition, ses);
      return ses;
    },
  };

  class MockWebContentsView {
    constructor(options) {
      this.options = options;
      this.bounds = null;
      const wc = new EventEmitter();
      wc._destroyed = false;
      wc.loadedUrls = [];
      wc.cdpCommands = [];
      wc.windowOpenHandler = null;
      wc.webrtcPolicy = null;
      wc.isDestroyed = () => wc._destroyed;
      wc.setWebRTCIPHandlingPolicy = (p) => {
        wc.webrtcPolicy = p;
      };
      wc.setUserAgent = (ua) => {
        wc.ua = ua;
      };
      wc.setWindowOpenHandler = (fn) => {
        wc.windowOpenHandler = fn;
      };
      wc.navigationHistory = {
        canGoBack: () => false,
        canGoForward: () => false,
        goBack: () => {},
        goForward: () => {},
      };
      wc.loadURL = async (url) => {
        wc.loadedUrls.push(url);
      };
      wc.reload = () => {
        wc.reloaded = (wc.reloaded || 0) + 1;
      };
      wc.stop = () => {};
      wc.close = () => {
        wc._destroyed = true;
      };
      wc.debugger = {
        _attached: false,
        isAttached: () => wc.debugger._attached,
        attach: () => {
          wc.debugger._attached = true;
        },
        detach: () => {
          wc.debugger._attached = false;
        },
        sendCommand: async (method, params) => {
          wc.cdpCommands.push({ method, params });
          return {};
        },
      };
      this.webContents = wc;
    }

    setBounds(bounds) {
      this.bounds = bounds;
    }
  }

  return { mainWindow, sessionModule, MockWebContentsView, attachedViews, sentMessages, sessions };
}

test("WorkspaceManager enforces per-profile proxy, scoped login auth, CDP order, viewport visibility, and error page", async () => {
  const harness = createMockElectronHarness();
  const released = [];
  const mockSidecar = {
    async prepareProfileSession(profileId, { allowIpDrift }) {
      if (profileId === "drift-profile" && !allowIpDrift) {
        return {
          ok: false,
          status: 409,
          code: "exit_ip_drift",
          message: "Exit IP changed",
          details: [{ previous_ip: "1.1.1.1", current_ip: "2.2.2.2", country_changed: true }],
        };
      }
      return {
        ok: true,
        data: {
          profile_id: profileId,
          status: "ready",
          proxy: {
            server: "http://127.0.0.1:41234",
            host: "127.0.0.1",
            port: 41234,
            username: "prism-u",
            password: "prism-secret-password",
          },
          exit_ip: "45.77.10.22",
          exit_country: "US",
          exit_city: "Los Angeles",
          isolation: {
            os: "macos",
            timezone: "America/Los_Angeles",
            languages: ["en-US", "en"],
            webrtc_policy: "block",
            canvas_policy: "stable_hardware",
            fingerprint_hash: "deadbeef1234",
            pinned_fingerprint: {
              platform: "MacIntel",
              hardware_concurrency: 8,
              device_memory: 8,
              canvas_seed: "seed-1",
              audio_seed: "seed-2",
              webgl_seed: "seed-3",
            },
          },
        },
      };
    },
    async releaseProfileSession(profileId) {
      released.push(profileId);
      return { ok: true };
    },
  };

  const mgr = new WorkspaceManager({
    mainWindow: harness.mainWindow,
    sidecar: mockSidecar,
    userDataDir: "",
    sessionModule: harness.sessionModule,
    WebContentsViewClass: harness.MockWebContentsView,
    chromiumVersion: "135.0.7049.115",
  });

  // 1. 409 exit_ip_drift flows through cleanly without opening workspace
  const driftRes = await mgr.openProfile("drift-profile");
  assert.equal(driftRes.ok, false);
  assert.equal(driftRes.status, 409);
  assert.equal(driftRes.code, "exit_ip_drift");
  assert.equal(mgr.listWorkspaces().length, 0);

  // 2. Opening profile configures partition, fail-closed proxy, CDP, and zero preload
  mgr.setViewportBounds({ x: 240, y: 96, width: 1100, height: 780 });
  const openRes = await mgr.openProfile("prof-1");
  assert.equal(openRes.ok, true);
  assert.equal(openRes.workspace.profileId, "prof-1");
  assert.equal(openRes.workspace.proxyPort, 41234);
  assert.equal(harness.attachedViews.length, 1);

  const ses = harness.sessions.get(partitionNameForProfile("prof-1"));
  assert.deepEqual(ses.proxyConfig, {
    proxyRules: "http://127.0.0.1:41234",
    proxyBypassRules: "<-loopback>",
  });

  const view = harness.attachedViews[0];
  assert.equal(view.options.webPreferences.preload, undefined);
  assert.equal(view.options.webPreferences.sandbox, true);
  assert.equal(view.options.webPreferences.contextIsolation, true);
  assert.equal(view.options.webPreferences.nodeIntegration, false);
  assert.deepEqual(view.bounds, { x: 240, y: 96, width: 1100, height: 780 });
  assert.equal(view.webContents.cdpCommands.length, 6);

  // 3. Login auth ONLY answers for matching 127.0.0.1:<proxyPort> proxy
  let authProvided = null;
  view.webContents.emit(
    "login",
    { preventDefault() {} },
    {},
    { isProxy: true, host: "evil.com", port: 41234 },
    (u, p) => {
      authProvided = [u, p];
    }
  );
  assert.equal(authProvided, null);

  view.webContents.emit(
    "login",
    { preventDefault() {} },
    {},
    { isProxy: true, host: "127.0.0.1", port: 41234 },
    (u, p) => {
      authProvided = [u, p];
    }
  );
  assert.deepEqual(authProvided, ["prism-u", "prism-secret-password"]);

  // 4. Viewport visibility hides/restores native view cleanly without closing tabs
  mgr.setViewportVisible(false);
  assert.equal(harness.attachedViews.length, 0);
  assert.equal(mgr.listWorkspaces().length, 1);

  mgr.setViewportVisible(true);
  assert.equal(harness.attachedViews.length, 1);

  // 5. did-fail-load loads white-label error page (and ignores -3 ERR_ABORTED)
  const countBefore = view.webContents.loadedUrls.length;
  view.webContents.emit("did-fail-load", {}, -3, "ERR_ABORTED", "https://example.com/", true);
  assert.equal(view.webContents.loadedUrls.length, countBefore);

  view.webContents.emit(
    "did-fail-load",
    {},
    -130,
    "ERR_PROXY_CONNECTION_FAILED",
    "https://example.com/",
    true
  );
  assert.equal(view.webContents.loadedUrls.length, countBefore + 1);
  assert.ok(view.webContents.loadedUrls.at(-1).startsWith("data:text/html;charset=utf-8,"));
  const tabId = openRes.workspace.activeTabId;
  mgr.reload("prof-1", tabId);
  assert.equal(view.webContents.loadedUrls.at(-1), "https://example.com/");

  // 6. window.open is denied and spawns a new tab inside the same profile
  const openAction = view.webContents.windowOpenHandler({ url: "https://whoer.net/" });
  assert.deepEqual(openAction, { action: "deny" });
  await new Promise((r) => setTimeout(r, 10));
  assert.equal(mgr.listWorkspaces()[0].tabs.length, 2);

  // 7. Cookie import/export round-trip
  const imported = await mgr.importCookies(
    "prof-1",
    [
      {
        name: "sid",
        value: "xyz",
        domain: ".example.com",
        path: "/",
        secure: true,
        httpOnly: true,
        sameSite: "Lax",
        expires: 1900000000,
      },
    ],
    "replace"
  );
  assert.deepEqual(imported, { imported: 1, skipped: 0 });
  const exported = await mgr.exportCookies("prof-1");
  assert.equal(exported.length, 1);
  assert.equal(exported[0].name, "sid");

  // 8. Closing profile detaches view and releases sidecar lease
  await mgr.closeProfile("prof-1");
  assert.equal(harness.attachedViews.length, 0);
  assert.equal(mgr.listWorkspaces().length, 0);
  assert.deepEqual(released, ["prof-1"]);
});

test("main.js declares all 4 anti-leak Chromium switches and electron-builder.yml matches packaging spec", () => {
  const mainSrc = fs.readFileSync(path.join(__dirname, "..", "main.js"), "utf8");
  assert.ok(mainSrc.includes('appendSwitch("disable-quic")'));
  assert.ok(mainSrc.includes('"force-webrtc-ip-handling-policy", "disable_non_proxied_udp"'));
  assert.ok(mainSrc.includes("AsyncDns,DnsOverHttps,WebRtcHideLocalIpsWithMdns"));
  assert.ok(mainSrc.includes("MAP * ~NOTFOUND , EXCLUDE 127.0.0.1"));

  const builderYml = fs.readFileSync(path.join(__dirname, "..", "electron-builder.yml"), "utf8");
  assert.ok(builderYml.includes("appId: com.prismbrowser.desktop"));
  assert.ok(builderYml.includes("productName: 棱镜浏览器"));
  assert.ok(builderYml.includes("output: ../dist"));
  assert.ok(builderYml.includes("../build/prism-backend"));
  assert.ok(builderYml.includes("identity: null"));
  assert.ok(builderYml.includes("prism-browser-macos-${arch}.dmg"));
  assert.ok(builderYml.includes("prism-browser-windows-${arch}-setup.exe"));
  assert.ok(builderYml.includes("oneClick: false"));
  assert.ok(builderYml.includes("allowToChangeInstallationDirectory: true"));
});
