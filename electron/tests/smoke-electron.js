"use strict";

/**
 * Live Electron 35 Chromium verification harness.
 * Verifies real process-level anti-leak switches, fail-closed DNS, authenticated
 * HTTP proxy tunneling, per-partition cookie isolation, CDP timezone/locale/UA/CH,
 * deterministic Canvas/WebGL/WebRTC spoofing, and zero prismDesktop leak to guest views.
 */
const assert = require("node:assert/strict");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const fs = require("node:fs");
const { app, BrowserWindow, WebContentsView, session } = require("electron");
const {
  buildChromeIdentity,
  configureSessionIdentity,
  attachFingerprintToWebContents,
} = require("../fingerprint-cdp");

const tmpUserData = fs.mkdtempSync(path.join(os.tmpdir(), "prism-electron-smoke-"));
app.setPath("userData", tmpUserData);

// Apply exact production anti-leak switches BEFORE app.whenReady()
app.commandLine.appendSwitch("disable-quic");
app.commandLine.appendSwitch("force-webrtc-ip-handling-policy", "disable_non_proxied_udp");
app.commandLine.appendSwitch(
  "disable-features",
  "AsyncDns,DnsOverHttps,WebRtcHideLocalIpsWithMdns"
);
app.commandLine.appendSwitch("host-resolver-rules", "MAP * ~NOTFOUND , EXCLUDE 127.0.0.1");

function startMockAuthProxy(expectedUser, expectedPass) {
  const capturedRequests = [];
  const expectedAuth = `Basic ${Buffer.from(`${expectedUser}:${expectedPass}`).toString("base64")}`;

  const server = http.createServer((req, res) => {
    const proxyAuth = req.headers["proxy-authorization"];
    if (proxyAuth !== expectedAuth) {
      res.writeHead(407, {
        "Proxy-Authenticate": 'Basic realm="Prism Local Proxy"',
        "Content-Type": "text/plain",
      });
      res.end("Proxy Authentication Required");
      return;
    }

    capturedRequests.push({
      url: req.url,
      userAgent: req.headers["user-agent"],
      acceptLanguage: req.headers["accept-language"],
      secChUa: req.headers["sec-ch-ua"],
      secChUaPlatform: req.headers["sec-ch-ua-platform"],
    });

    res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
    res.end(`<!DOCTYPE html>
<html>
<head><title>Prism Probe</title></head>
<body>
  <canvas id="c" width="220" height="60"></canvas>
  <script>
    const c = document.getElementById("c");
    const ctx = c.getContext("2d");
    ctx.fillStyle = "#1e293b";
    ctx.fillRect(0, 0, 220, 60);
    ctx.fillStyle = "#38bdf8";
    ctx.font = "16px sans-serif";
    ctx.fillText("Prism Isolation Probe 123", 10, 35);
  </script>
</body>
</html>`);
  });

  return new Promise((resolve) => {
    server.listen(0, "127.0.0.1", () => {
      resolve({ server, port: server.address().port, capturedRequests });
    });
  });
}

async function probeGuestProfile(win, proxyInfo, profileId, seeds) {
  const ses = session.fromPartition(`persist:prism_${profileId}`);
  await ses.setProxy({
    proxyRules: `http://127.0.0.1:${proxyInfo.port}`,
    proxyBypassRules: "<-loopback>",
  });

  const identity = buildChromeIdentity(
    {
      os: "macos",
      timezone: "America/Los_Angeles",
      languages: ["en-US", "en"],
      webrtc_policy: "block",
      canvas_policy: "stable_hardware",
      fingerprint_hash: `fp-${profileId}`,
      pinned_fingerprint: {
        platform: "MacIntel",
        platform_version: "14.6.1",
        architecture: "arm",
        bitness: "64",
        screen_width: 1512,
        screen_height: 982,
        avail_width: 1512,
        avail_height: 944,
        color_depth: 24,
        device_pixel_ratio: 2,
        hardware_concurrency: 10,
        device_memory: 8,
        webgl_vendor: "Google Inc. (Apple)",
        webgl_renderer: "ANGLE (Apple, ANGLE Metal Renderer: Apple M3 Pro, Unspecified Version)",
        canvas_seed: seeds.canvas,
        audio_seed: seeds.audio,
        webgl_seed: seeds.webgl,
      },
    },
    process.versions.chrome
  );
  configureSessionIdentity(ses, identity);

  const view = new WebContentsView({
    webPreferences: {
      session: ses,
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      spellcheck: false,
    },
  });
  win.contentView.addChildView(view);
  view.setBounds({ x: 0, y: 0, width: 800, height: 600 });

  const wc = view.webContents;
  wc.on("login", (event, _details, authInfo, callback) => {
    if (authInfo.isProxy && authInfo.host === "127.0.0.1" && authInfo.port === proxyInfo.port) {
      event.preventDefault();
      callback("prism-u", "prism-p");
    }
  });

  await attachFingerprintToWebContents(wc, identity);
  await wc.loadURL("http://remote-via-proxy.prism.test/probe");

  const result = await wc.executeJavaScript(`(() => {
    const canvas = document.getElementById("c");
    const png1 = canvas.toDataURL();
    const png2 = canvas.toDataURL();

    const glCanvas = document.createElement("canvas");
    const gl = glCanvas.getContext("webgl") || glCanvas.getContext("experimental-webgl");
    const dbg = gl ? gl.getExtension("WEBGL_debug_renderer_info") : null;
    const glVendor = gl && dbg ? gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL) : null;
    const glRenderer = gl && dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : null;

    document.cookie = "profile_marker=${profileId}; path=/";

    return {
      ua: navigator.userAgent,
      platform: navigator.platform,
      webdriver: navigator.webdriver,
      getBatteryType: typeof navigator.getBattery,
      hardwareConcurrency: navigator.hardwareConcurrency,
      deviceMemory: navigator.deviceMemory,
      language: navigator.language,
      languages: [...navigator.languages],
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
      intlLocale: Intl.DateTimeFormat().resolvedOptions().locale,
      webrtcType: typeof window.RTCPeerConnection,
      webkitWebrtcType: typeof window.webkitRTCPeerConnection,
      dataChannelType: typeof window.RTCDataChannel,
      screen: {
        width: screen.width,
        height: screen.height,
        availWidth: screen.availWidth,
        availHeight: screen.availHeight,
        colorDepth: screen.colorDepth,
      },
      glVendor,
      glRenderer,
      canvasStable: png1 === png2,
      canvasHash: png1.slice(-64),
      hasPrismDesktop: typeof window.prismDesktop !== "undefined",
      brands: navigator.userAgentData ? navigator.userAgentData.brands : [],
      cookie: document.cookie,
    };
  })()`);

  win.contentView.removeChildView(view);
  wc.debugger.detach();
  wc.close();
  return result;
}

app.whenReady().then(async () => {
  const proxyInfo = await startMockAuthProxy("prism-u", "prism-p");
  const win = new BrowserWindow({
    width: 1000,
    height: 700,
    show: false,
    webPreferences: {
      preload: path.join(__dirname, "..", "preload-shell.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  try {
    // 1. Verify fail-closed DNS without proxy blocks remote resolution (ERR_NAME_NOT_RESOLVED = -105)
    const directSes = session.fromPartition("persist:prism_direct_test");
    await directSes.setProxy({ mode: "direct" });
    const directView = new WebContentsView({
      webPreferences: { session: directSes, sandbox: true, contextIsolation: true },
    });
    const dnsFail = await new Promise((resolve) => {
      directView.webContents.once("did-fail-load", (_e, code, desc) => resolve({ code, desc }));
      directView.webContents.loadURL("http://should-never-resolve.example.invalid/").catch(() => {});
    });
    assert.equal(dnsFail.code, -105, `Expected ERR_NAME_NOT_RESOLVED (-105), got ${JSON.stringify(dnsFail)}`);
    directView.webContents.close();

    // 2. Verify proxied profile A (run twice to verify deterministic Canvas across restarts!)
    const resA1 = await probeGuestProfile(win, proxyInfo, "smoke_a", {
      canvas: "seed-A-canvas",
      audio: "seed-A-audio",
      webgl: "seed-A-webgl",
    });
    const resA2 = await probeGuestProfile(win, proxyInfo, "smoke_a", {
      canvas: "seed-A-canvas",
      audio: "seed-A-audio",
      webgl: "seed-A-webgl",
    });
    // 3. Verify proxied profile B (different seeds & separate cookie partition!)
    const resB = await probeGuestProfile(win, proxyInfo, "smoke_b", {
      canvas: "seed-B-canvas",
      audio: "seed-B-audio",
      webgl: "seed-B-webgl",
    });

    // Assertions on real Chromium behavior
    assert.ok(!/Electron|Prism/i.test(resA1.ua), `UA leaked Electron/Prism: ${resA1.ua}`);
    assert.equal(resA1.platform, "MacIntel");
    assert.equal(resA1.webdriver, undefined);
    assert.equal(resA1.getBatteryType, "undefined");
    assert.equal(resA1.hardwareConcurrency, 10);
    assert.equal(resA1.deviceMemory, 8);
    assert.equal(resA1.language, "en-US");
    assert.deepEqual(resA1.languages, ["en-US", "en"]);
    assert.equal(resA1.timezone, "America/Los_Angeles");
    assert.equal(resA1.intlLocale, "en-US");
    assert.equal(resA1.webrtcType, "undefined");
    assert.equal(resA1.webkitWebrtcType, "undefined");
    assert.equal(resA1.dataChannelType, "undefined");
    assert.equal(resA1.screen.width, 1512);
    assert.equal(resA1.screen.height, 982);
    assert.equal(resA1.hasPrismDesktop, false);
    if (resA1.glVendor !== null) {
      assert.equal(resA1.glVendor, "Google Inc. (Apple)");
      assert.equal(
        resA1.glRenderer,
        "ANGLE (Apple, ANGLE Metal Renderer: Apple M3 Pro, Unspecified Version)"
      );
    }

    // Canvas determinism & uniqueness across real Chromium runs
    assert.equal(resA1.canvasStable, true);
    assert.equal(resA1.canvasHash, resA2.canvasHash, "Same profile must keep identical Canvas hash");
    assert.notEqual(resA1.canvasHash, resB.canvasHash, "Different profiles must produce unique Canvas hashes");

    // Partition cookie isolation
    assert.ok(resA2.cookie.includes("profile_marker=smoke_a"));
    assert.ok(!resB.cookie.includes("profile_marker=smoke_a"), `Cookie leaked across partitions: ${resB.cookie}`);
    assert.ok(resB.cookie.includes("profile_marker=smoke_b"));

    // Wire-level proxy request headers
    assert.ok(proxyInfo.capturedRequests.length >= 3);
    for (const req of proxyInfo.capturedRequests) {
      assert.ok(!/Electron|Prism/i.test(req.userAgent), `Wire UA leaked: ${req.userAgent}`);
      assert.equal(req.acceptLanguage, "en-US,en;q=0.9");
    }

    // 4. Verify detached DevTools coexists with CDP fingerprint attachment on guest WebContentsView
    const dtSes = session.fromPartition("persist:prism_smoke_devtools");
    await dtSes.setProxy({
      proxyRules: `http://127.0.0.1:${proxyInfo.port}`,
      proxyBypassRules: "<-loopback>",
    });
    const dtIdentity = buildChromeIdentity(
      {
        os: "macos",
        timezone: "America/New_York",
        languages: ["en-US", "en"],
        webrtc_policy: "block",
        canvas_policy: "stable_hardware",
        fingerprint_hash: "fp-smoke-devtools",
        pinned_fingerprint: { platform: "MacIntel", hardware_concurrency: 12 },
      },
      process.versions.chrome
    );
    configureSessionIdentity(dtSes, dtIdentity);
    const dtView = new WebContentsView({
      webPreferences: {
        session: dtSes,
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
        spellcheck: false,
      },
    });
    win.contentView.addChildView(dtView);
    dtView.setBounds({ x: 0, y: 0, width: 800, height: 600 });
    const dtWc = dtView.webContents;
    dtWc.on("login", (event, _details, authInfo, callback) => {
      if (authInfo.isProxy && authInfo.host === "127.0.0.1" && authInfo.port === proxyInfo.port) {
        event.preventDefault();
        callback("prism-u", "prism-p");
      }
    });
    await attachFingerprintToWebContents(dtWc, dtIdentity);
    const dtOpened = new Promise((resolve) => dtWc.once("devtools-opened", resolve));
    dtWc.openDevTools({ mode: "detach", activate: false });
    await dtOpened;
    assert.equal(dtWc.isDevToolsOpened(), true, "Guest tab must open detached DevTools");
    assert.equal(dtWc.debugger.isAttached(), true, "CDP debugger must remain attached with DevTools open");
    await dtWc.loadURL("http://remote-via-proxy.prism.test/devtools-probe");
    const dtFp = await dtWc.executeJavaScript(`({
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
      hardwareConcurrency: navigator.hardwareConcurrency,
      webrtcType: typeof window.RTCPeerConnection,
    })`);
    assert.equal(dtFp.timezone, "America/New_York");
    assert.equal(dtFp.hardwareConcurrency, 12);
    assert.equal(dtFp.webrtcType, "undefined");
    const dtClosed = new Promise((resolve) => dtWc.once("devtools-closed", resolve));
    dtWc.closeDevTools();
    await dtClosed;
    assert.equal(dtWc.isDevToolsOpened(), false);
    assert.equal(dtWc.debugger.isAttached(), true);
    win.contentView.removeChildView(dtView);
    dtWc.debugger.detach();
    dtWc.close();

    // Verify preload exposes window.prismDesktop on main window ONLY
    await win.loadURL("data:text/html,<html><body>Shell</body></html>");
    const shellBridge = await win.webContents.executeJavaScript(
      "Boolean(window.prismDesktop && window.prismDesktop.isDesktop === true && typeof window.prismDesktop.openProfile === 'function')"
    );
    assert.equal(shellBridge, true, "Main shell must expose window.prismDesktop");

    process.stdout.write(
      `ELECTRON_SMOKE_PASS chrome=${process.versions.chrome} electron=${process.versions.electron} requests=${proxyInfo.capturedRequests.length} glVendor=${resA1.glVendor}\n`
    );
    proxyInfo.server.close();
    win.destroy();
    fs.rmSync(tmpUserData, { recursive: true, force: true });
    app.exit(0);
  } catch (err) {
    process.stderr.write(`ELECTRON_SMOKE_FAIL: ${err && err.stack ? err.stack : err}\n`);
    proxyInfo.server.close();
    win.destroy();
    fs.rmSync(tmpUserData, { recursive: true, force: true });
    app.exit(1);
  }
});
