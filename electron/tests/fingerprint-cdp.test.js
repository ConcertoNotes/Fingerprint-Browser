"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const {
  buildChromeIdentity,
  buildFingerprintInjectionScript,
  attachFingerprintToWebContents,
  sanitizeChromeUserAgent,
} = require("../fingerprint-cdp");

function makeSampleIsolation(overrides = {}) {
  return {
    os: "macos",
    tier: "standard",
    timezone: "America/Los_Angeles",
    languages: ["en-US", "en"],
    webrtc_policy: "block",
    canvas_policy: "stable_hardware",
    fingerprint_hash: "a1b2c3d4e5f60708",
    policy_version: "prism-cloud-2026.11.26-r3",
    pinned_fingerprint: {
      user_agent:
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Electron/35.7.5 Prism/0.6.0 Safari/537.36",
      chrome_major: 120,
      platform: "MacIntel",
      platform_version: "14.6.1",
      architecture: "arm",
      bitness: "64",
      model: "",
      mobile: false,
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
      canvas_seed: "seed-canvas-profile-A",
      audio_seed: "seed-audio-profile-A",
      webgl_seed: "seed-webgl-profile-A",
      fonts: ["SF Pro Text", "Helvetica Neue"],
    },
    ...overrides,
  };
}

test("Chrome UA and Client Hints synthesis strips Electron/Prism tokens and aligns Chrome version", () => {
  const identity = buildChromeIdentity(makeSampleIsolation(), "135.0.7049.115");

  assert.ok(!/Electron|Prism|Camoufox|Headless/i.test(identity.userAgent), identity.userAgent);
  assert.ok(identity.userAgent.includes("Chrome/135.0.0.0"), identity.userAgent);
  assert.equal(identity.chromeMajor, 135);
  assert.equal(identity.navigatorPlatform, "MacIntel");
  assert.equal(identity.userAgentMetadata.platform, "macOS");
  assert.equal(identity.userAgentMetadata.platformVersion, "14.6.1");
  assert.equal(identity.userAgentMetadata.architecture, "arm");
  assert.equal(identity.userAgentMetadata.bitness, "64");
  assert.equal(identity.userAgentMetadata.mobile, false);
  assert.equal(identity.acceptLanguage, "en-US,en;q=0.9");
  assert.equal(identity.timezone, "America/Los_Angeles");

  const brandNames = identity.userAgentMetadata.brands.map((b) => b.brand);
  assert.ok(brandNames.includes("Google Chrome"));
  assert.ok(brandNames.includes("Chromium"));
  assert.ok(!brandNames.some((b) => /Electron|Prism/i.test(b)));
  const fullChrome = identity.userAgentMetadata.fullVersionList.find(
    (b) => b.brand === "Google Chrome"
  );
  assert.equal(fullChrome.version, "135.0.7049.115");
});

test("OS platform mapping distinguishes navigator.platform from Sec-CH-UA-Platform across Windows/Linux", () => {
  const winId = buildChromeIdentity(
    { os: "windows", pinned_fingerprint: { platform: "Win32" } },
    "135.0.7049.115"
  );
  assert.equal(winId.navigatorPlatform, "Win32");
  assert.equal(winId.userAgentMetadata.platform, "Windows");
  assert.ok(winId.userAgent.includes("Windows NT 10.0; Win64; x64"));
  assert.ok(!winId.userAgent.includes("Electron"));

  const linuxId = buildChromeIdentity(
    { os: "linux", pinned_fingerprint: { platform: "Linux x86_64" } },
    "135.0.7049.115"
  );
  assert.equal(linuxId.navigatorPlatform, "Linux x86_64");
  assert.equal(linuxId.userAgentMetadata.platform, "Linux");
  assert.ok(linuxId.userAgent.includes("X11; Linux x86_64"));

  const cleaned = sanitizeChromeUserAgent(
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.1.2.3 Electron/35.0.0 Safari/537.36",
    "windows",
    135
  );
  assert.ok(!cleaned.includes("Electron"));
  assert.ok(cleaned.includes("Chrome/135.0.0.0"));
});

function createBrowserSandbox(scriptSource) {
  function Navigator() {}
  Navigator.prototype.webdriver = true;
  Navigator.prototype.getBattery = () => Promise.resolve({ level: 1 });
  Navigator.prototype.platform = "WrongPlatform";
  Navigator.prototype.languages = ["zh-CN"];
  Navigator.prototype.language = "zh-CN";
  Navigator.prototype.deviceMemory = 16;
  Navigator.prototype.hardwareConcurrency = 32;

  function Screen() {}
  Screen.prototype.width = 2560;
  Screen.prototype.height = 1440;
  Screen.prototype.availWidth = 2560;
  Screen.prototype.availHeight = 1400;
  Screen.prototype.colorDepth = 30;
  Screen.prototype.pixelDepth = 30;

  function CanvasRenderingContext2D() {}
  CanvasRenderingContext2D.prototype.getImageData = function (sx, sy, sw, sh) {
    const data = new Uint8ClampedArray(sw * sh * 4);
    for (let i = 0; i < data.length; i += 4) {
      data[i] = 120;
      data[i + 1] = 140;
      data[i + 2] = 160;
      data[i + 3] = i === 0 ? 0 : 255; // pixel 0 transparent, rest opaque
    }
    return { data, width: sw, height: sh };
  };

  function HTMLCanvasElement() {
    this.width = 16;
    this.height = 16;
  }
  HTMLCanvasElement.prototype.getContext = function () {
    return new CanvasRenderingContext2D();
  };
  HTMLCanvasElement.prototype.toDataURL = function () {
    return "data:image/png;base64,MOCK";
  };

  function WebGLRenderingContext() {}
  WebGLRenderingContext.prototype.getParameter = function (param) {
    if (param === 37445) return "REAL_HOST_VENDOR";
    if (param === 37446) return "REAL_HOST_RENDERER";
    return 4096;
  };
  WebGLRenderingContext.prototype.getExtension = function () {
    return null;
  };
  WebGLRenderingContext.prototype.readPixels = function () {};

  function WebGL2RenderingContext() {}
  WebGL2RenderingContext.prototype.getParameter = WebGLRenderingContext.prototype.getParameter;
  WebGL2RenderingContext.prototype.getExtension = WebGLRenderingContext.prototype.getExtension;
  WebGL2RenderingContext.prototype.readPixels = WebGLRenderingContext.prototype.readPixels;

  function AudioBuffer() {
    this._channel = new Float32Array(256).fill(0.25);
  }
  AudioBuffer.prototype.getChannelData = function () {
    return this._channel;
  };

  const sandbox = {
    Navigator,
    navigator: new Navigator(),
    Screen,
    screen: new Screen(),
    CanvasRenderingContext2D,
    HTMLCanvasElement,
    WebGLRenderingContext,
    WebGL2RenderingContext,
    AudioBuffer,
    RTCPeerConnection: function FakeRTC() {},
    webkitRTCPeerConnection: function FakeWebkitRTC() {},
    mozRTCPeerConnection: function FakeMozRTC() {},
    RTCDataChannel: function FakeDataChannel() {},
    document: { createElement: () => new HTMLCanvasElement() },
    Uint8ClampedArray,
    Uint8Array,
    Float32Array,
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;

  const ctx = vm.createContext(sandbox);
  const compiled = new vm.Script(scriptSource);
  compiled.runInContext(ctx);
  sandbox.__eval = (expr) => vm.runInContext(expr, ctx);
  return sandbox;
}

test("injection script compiles cleanly and enforces Navigator, Screen, WebGL, WebRTC, Canvas, and Audio", () => {
  const idA = buildChromeIdentity(makeSampleIsolation(), "135.0.7049.115");
  const scriptA = buildFingerprintInjectionScript(idA);

  const boxA1 = createBrowserSandbox(scriptA);
  const boxA2 = createBrowserSandbox(scriptA);

  // Navigator & Stealth
  assert.equal(boxA1.navigator.webdriver, undefined);
  assert.equal(boxA1.navigator.getBattery, undefined);
  assert.equal(boxA1.navigator.deviceMemory, 8);
  assert.equal(boxA1.navigator.hardwareConcurrency, 10);
  assert.equal(boxA1.navigator.platform, "MacIntel");
  assert.equal(boxA1.navigator.language, "en-US");
  assert.deepEqual([...boxA1.navigator.languages], ["en-US", "en"]);
  assert.ok(Object.isFrozen(boxA1.navigator.languages));

  // Screen metrics
  assert.equal(boxA1.screen.width, 1512);
  assert.equal(boxA1.screen.height, 982);
  assert.equal(boxA1.screen.availWidth, 1512);
  assert.equal(boxA1.screen.availHeight, 944);
  assert.equal(boxA1.screen.colorDepth, 24);
  assert.equal(boxA1.screen.pixelDepth, 24);
  assert.equal(boxA1.devicePixelRatio, 2);

  // WebGL1 & WebGL2 spoofing + native toString masking
  const gl1 = new boxA1.WebGLRenderingContext();
  const gl2 = new boxA1.WebGL2RenderingContext();
  assert.equal(gl1.getParameter(37445), "Google Inc. (Apple)");
  assert.equal(
    gl1.getParameter(37446),
    "ANGLE (Apple, ANGLE Metal Renderer: Apple M3 Pro, Unspecified Version)"
  );
  assert.equal(gl2.getParameter(37445), "Google Inc. (Apple)");
  assert.equal(gl1.getParameter(9999), 4096);
  assert.equal(gl1.getExtension("WEBGL_debug_renderer_info").UNMASKED_VENDOR_WEBGL, 37445);
  assert.ok(
    boxA1
      .__eval("Function.prototype.toString.call(WebGLRenderingContext.prototype.getParameter)")
      .includes("[native code]")
  );

  // WebRTC blocked when policy === "block"
  assert.equal(boxA1.RTCPeerConnection, undefined);
  assert.equal(boxA1.webkitRTCPeerConnection, undefined);
  assert.equal(boxA1.mozRTCPeerConnection, undefined);
  assert.equal(boxA1.RTCDataChannel, undefined);

  // Deterministic Canvas 2D noise: identical for same profile across restarts
  const imgA1 = new boxA1.CanvasRenderingContext2D().getImageData(0, 0, 32, 32);
  const imgA2 = new boxA2.CanvasRenderingContext2D().getImageData(0, 0, 32, 32);
  assert.deepEqual(Array.from(imgA1.data), Array.from(imgA2.data));
  // Transparent pixel 0 remains untouched
  assert.equal(imgA1.data[3], 0);
  assert.equal(imgA1.data[0], 120);

  // Different profile seed produces different Canvas & Audio fingerprints
  const isoB = makeSampleIsolation();
  isoB.pinned_fingerprint = {
    ...isoB.pinned_fingerprint,
    canvas_seed: "seed-canvas-profile-B",
    audio_seed: "seed-audio-profile-B",
    webgl_seed: "seed-webgl-profile-B",
  };
  const idB = buildChromeIdentity(isoB, "135.0.7049.115");
  const boxB = createBrowserSandbox(buildFingerprintInjectionScript(idB));
  const imgB = new boxB.CanvasRenderingContext2D().getImageData(0, 0, 32, 32);
  assert.notDeepEqual(Array.from(imgA1.data), Array.from(imgB.data));

  // Audio deterministic across restarts, idempotent on repeated calls, unique across profiles
  const bufA1 = new boxA1.AudioBuffer();
  const chA1First = Array.from(bufA1.getChannelData(0));
  const chA1Second = Array.from(bufA1.getChannelData(0));
  assert.deepEqual(chA1First, chA1Second);

  const bufA2 = new boxA2.AudioBuffer();
  assert.deepEqual(chA1First, Array.from(bufA2.getChannelData(0)));

  const bufB = new boxB.AudioBuffer();
  assert.notDeepEqual(chA1First, Array.from(bufB.getChannelData(0)));
});

test("WebRTC APIs remain intact when webrtc_policy is not block", () => {
  const id = buildChromeIdentity(makeSampleIsolation({ webrtc_policy: "default" }), "135.0.7049.115");
  const box = createBrowserSandbox(buildFingerprintInjectionScript(id));
  assert.equal(typeof box.RTCPeerConnection, "function");
});

test("attachFingerprintToWebContents sends all required CDP 1.3 emulation commands", async () => {
  const commands = [];
  let attachedVersion = null;
  let webrtcPolicy = null;
  let userAgentSet = null;

  const mockWc = {
    setWebRTCIPHandlingPolicy(policy) {
      webrtcPolicy = policy;
    },
    setUserAgent(ua) {
      userAgentSet = ua;
    },
    debugger: {
      _attached: false,
      isAttached() {
        return this._attached;
      },
      attach(version) {
        this._attached = true;
        attachedVersion = version;
      },
      async sendCommand(method, params) {
        commands.push({ method, params });
        return {};
      },
    },
  };

  const identity = buildChromeIdentity(makeSampleIsolation(), "135.0.7049.115");
  await attachFingerprintToWebContents(mockWc, identity);

  assert.equal(webrtcPolicy, "disable_non_proxied_udp");
  assert.equal(attachedVersion, "1.3");
  assert.equal(userAgentSet, identity.userAgent);

  const methods = commands.map((c) => c.method);
  assert.deepEqual(methods, [
    "Page.enable",
    "Emulation.setTimezoneOverride",
    "Emulation.setLocaleOverride",
    "Emulation.setUserAgentOverride",
    "Emulation.setHardwareConcurrencyOverride",
    "Page.addScriptToEvaluateOnNewDocument",
  ]);
  assert.equal(commands[1].params.timezoneId, "America/Los_Angeles");
  assert.equal(commands[2].params.locale, "en-US");
  assert.equal(commands[3].params.userAgent, identity.userAgent);
  assert.equal(commands[3].params.platform, "MacIntel");
  assert.equal(commands[3].params.userAgentMetadata.platform, "macOS");
  assert.equal(commands[4].params.hardwareConcurrency, 10);
  assert.ok(commands[5].params.source.includes("UNMASKED_VENDOR_WEBGL"));
});
