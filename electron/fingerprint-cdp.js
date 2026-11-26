"use strict";

const DEFAULT_CHROME_VERSION = "135.0.7049.115";
const DEFAULT_TIMEZONE = "America/Los_Angeles";
const DEFAULT_LANGUAGES = ["en-US", "en"];

function parseChromeMajor(chromiumVersion) {
  const raw = String(chromiumVersion || DEFAULT_CHROME_VERSION).trim();
  const match = raw.match(/^(\d+)(?:\.\d+){0,3}/);
  return match ? Number(match[1]) : 135;
}

function normalizeFullChromeVersion(chromiumVersion) {
  const raw = String(chromiumVersion || DEFAULT_CHROME_VERSION).trim();
  if (/^\d+\.\d+\.\d+\.\d+$/.test(raw)) {
    return raw;
  }
  const major = parseChromeMajor(raw);
  return `${major}.0.0.0`;
}

function normalizeOsFamily(osHint, platformHint) {
  const raw = `${osHint || ""} ${platformHint || ""}`.toLowerCase();
  if (raw.includes("win")) {
    return "windows";
  }
  if (raw.includes("linux") || raw.includes("x11")) {
    return "linux";
  }
  return "macos";
}

function osDefaults(osFamily) {
  if (osFamily === "windows") {
    return {
      navigatorPlatform: "Win32",
      chPlatform: "Windows",
      platformVersion: "15.0.0",
      architecture: "x86",
      bitness: "64",
      uaPlatformToken: "Windows NT 10.0; Win64; x64",
      webglVendor: "Google Inc. (NVIDIA)",
      webglRenderer: "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    };
  }
  if (osFamily === "linux") {
    return {
      navigatorPlatform: "Linux x86_64",
      chPlatform: "Linux",
      platformVersion: "6.6.0",
      architecture: "x86",
      bitness: "64",
      uaPlatformToken: "X11; Linux x86_64",
      webglVendor: "Google Inc. (AMD)",
      webglRenderer: "ANGLE (AMD, AMD Radeon RX 6700 XT (radeonsi, navi22, LLVM 17.0.6, DRM 3.57), OpenGL 4.6)",
    };
  }
  return {
    navigatorPlatform: "MacIntel",
    chPlatform: "macOS",
    platformVersion: "14.6.1",
    architecture: "arm",
    bitness: "64",
    uaPlatformToken: "Macintosh; Intel Mac OS X 10_15_7",
    webglVendor: "Google Inc. (Apple)",
    webglRenderer: "ANGLE (Apple, ANGLE Metal Renderer: Apple M2, Unspecified Version)",
  };
}

function mapChPlatform(osFamily, rawPlatform) {
  const lower = String(rawPlatform || "").toLowerCase();
  if (lower === "macos" || lower === "darwin" || lower === "macintel") {
    return "macOS";
  }
  if (lower === "windows" || lower === "win32" || lower === "win64") {
    return "Windows";
  }
  if (lower === "linux" || lower.startsWith("linux")) {
    return "Linux";
  }
  return osDefaults(osFamily).chPlatform;
}

function mapNavigatorPlatform(osFamily, rawPlatform) {
  const lower = String(rawPlatform || "").toLowerCase();
  if (lower === "macintel" || lower === "macos" || lower === "darwin") {
    return "MacIntel";
  }
  if (lower === "win32" || lower === "windows" || lower === "win64") {
    return "Win32";
  }
  if (lower.startsWith("linux")) {
    return "Linux x86_64";
  }
  return osDefaults(osFamily).navigatorPlatform;
}

function sanitizeChromeUserAgent(rawUa, osFamily, chromeMajor) {
  const defaults = osDefaults(osFamily);
  const fallback = `Mozilla/5.0 (${defaults.uaPlatformToken}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/${chromeMajor}.0.0.0 Safari/537.36`;
  if (typeof rawUa !== "string" || !rawUa.trim()) {
    return fallback;
  }

  // Strip any Electron, Prism, Camoufox, HeadlessChrome or Chromium wrapper tokens.
  let cleaned = rawUa
    .replace(/\s+(?:Electron|Prism|PrismBrowser|Camoufox|HeadlessChrome)\/[^\s)]+/gi, "")
    .replace(/HeadlessChrome\//gi, "Chrome/")
    .replace(/\s{2,}/g, " ")
    .trim();

  if (!/Chrome\/\d+/i.test(cleaned)) {
    return fallback;
  }

  // Pin UA Chrome version to actual embedded Chromium major so TLS/HTTP2/JS version never drifts.
  cleaned = cleaned.replace(/Chrome\/\d+(?:\.\d+){0,3}/i, `Chrome/${chromeMajor}.0.0.0`);
  return cleaned;
}

function buildAcceptLanguage(languages) {
  const list = Array.isArray(languages) && languages.length > 0
    ? languages.filter((item) => typeof item === "string" && item.trim()).map((item) => item.trim())
    : DEFAULT_LANGUAGES;
  if (list.length === 0) {
    return "en-US,en;q=0.9";
  }
  const unique = [...new Set(list)];
  return unique
    .map((lang, idx) => {
      if (idx === 0) {
        return lang;
      }
      const q = Math.max(0.1, Number((1 - idx * 0.1).toFixed(1)));
      return `${lang};q=${q}`;
    })
    .join(",");
}

function greaseBrandForMajor(major) {
  // Deterministic Chromium GREASE brand consistent with modern Chrome releases.
  const greaseTable = [
    { brand: "Not(A:Brand", version: "99" },
    { brand: "Not/A)Brand", version: "8" },
    { brand: "Not:A-Brand", version: "24" },
    { brand: "Not;A=Brand", version: "99" },
  ];
  return greaseTable[Math.abs(Number(major) || 0) % greaseTable.length];
}

/**
 * Synthesize a complete, self-consistent Chrome identity (UA, Client Hints, locale,
 * hardware, screen, WebGL, seeds) from the backend isolation payload and live Chromium version.
 */
function buildChromeIdentity(isolation = {}, chromiumVersion = process.versions.chrome) {
  const iso = isolation && typeof isolation === "object" ? isolation : {};
  const pinned =
    iso.pinned_fingerprint && typeof iso.pinned_fingerprint === "object"
      ? iso.pinned_fingerprint
      : {};

  const fullChromeVersion = normalizeFullChromeVersion(chromiumVersion);
  const chromeMajor = parseChromeMajor(fullChromeVersion);
  const osFamily = normalizeOsFamily(iso.os, pinned.platform);
  const defaults = osDefaults(osFamily);

  const userAgent = sanitizeChromeUserAgent(pinned.user_agent, osFamily, chromeMajor);
  const navigatorPlatform = mapNavigatorPlatform(osFamily, pinned.platform);
  const chPlatform = mapChPlatform(osFamily, pinned.platform);
  const platformVersion = String(pinned.platform_version || defaults.platformVersion);
  const architecture = String(pinned.architecture || defaults.architecture);
  const bitness = String(pinned.bitness || defaults.bitness);
  const model = String(pinned.model || "");
  const mobile = Boolean(pinned.mobile ?? false);

  const languages =
    Array.isArray(iso.languages) && iso.languages.length > 0
      ? iso.languages.filter((x) => typeof x === "string" && x.trim()).map((x) => x.trim())
      : [...DEFAULT_LANGUAGES];
  const primaryLocale = languages[0] || "en-US";
  const acceptLanguage = buildAcceptLanguage(languages);
  const timezone =
    typeof iso.timezone === "string" && iso.timezone.trim()
      ? iso.timezone.trim()
      : DEFAULT_TIMEZONE;

  const grease = greaseBrandForMajor(chromeMajor);
  const brands = [
    { brand: "Google Chrome", version: String(chromeMajor) },
    { brand: grease.brand, version: grease.version },
    { brand: "Chromium", version: String(chromeMajor) },
  ];
  const fullVersionList = [
    { brand: "Google Chrome", version: fullChromeVersion },
    { brand: grease.brand, version: `${grease.version}.0.0.0` },
    { brand: "Chromium", version: fullChromeVersion },
  ];

  const userAgentMetadata = {
    brands,
    fullVersionList,
    fullVersion: fullChromeVersion,
    platform: chPlatform,
    platformVersion,
    architecture,
    model,
    mobile,
    bitness,
    wow64: false,
  };

  const hardwareConcurrency = Math.max(
    2,
    Math.min(32, Number(pinned.hardware_concurrency) || 8)
  );
  const allowedDeviceMemory = [2, 4, 8];
  const rawMemory = Number(pinned.device_memory) || 8;
  const deviceMemory = allowedDeviceMemory.reduce((best, cand) =>
    Math.abs(cand - rawMemory) < Math.abs(best - rawMemory) ? cand : best
  );

  const screenWidth = Math.max(800, Number(pinned.screen_width) || 1440);
  const screenHeight = Math.max(600, Number(pinned.screen_height) || 900);
  const availWidth = Math.max(800, Math.min(screenWidth, Number(pinned.avail_width) || screenWidth));
  const availHeight = Math.max(
    540,
    Math.min(screenHeight, Number(pinned.avail_height) || screenHeight - 40)
  );
  const colorDepth = Number(pinned.color_depth) || 24;
  const devicePixelRatio = Number(pinned.device_pixel_ratio) || (osFamily === "macos" ? 2 : 1);

  const webglVendor = String(pinned.webgl_vendor || defaults.webglVendor);
  const webglRenderer = String(pinned.webgl_renderer || defaults.webglRenderer);
  const canvasSeed = String(pinned.canvas_seed || iso.fingerprint_hash || "prism-default-canvas");
  const audioSeed = String(pinned.audio_seed || iso.fingerprint_hash || "prism-default-audio");
  const webglSeed = String(pinned.webgl_seed || iso.fingerprint_hash || "prism-default-webgl");
  const webrtcPolicy = String(iso.webrtc_policy || "block").toLowerCase();
  const canvasPolicy = String(iso.canvas_policy || "stable_hardware").toLowerCase();
  const fonts = Array.isArray(pinned.fonts) ? pinned.fonts.filter((f) => typeof f === "string") : [];

  return {
    osFamily,
    chromeMajor,
    fullChromeVersion,
    userAgent,
    navigatorPlatform,
    userAgentMetadata,
    timezone,
    locale: primaryLocale,
    languages,
    acceptLanguage,
    hardwareConcurrency,
    deviceMemory,
    screen: {
      width: screenWidth,
      height: screenHeight,
      availWidth,
      availHeight,
      colorDepth,
      pixelDepth: colorDepth,
      devicePixelRatio,
    },
    webgl: {
      vendor: webglVendor,
      renderer: webglRenderer,
    },
    seeds: {
      canvas: canvasSeed,
      audio: audioSeed,
      webgl: webglSeed,
    },
    webrtcPolicy,
    canvasPolicy,
    fonts,
    fingerprintHash: String(iso.fingerprint_hash || ""),
  };
}

/**
 * Build a self-contained deterministic main-world script evaluated before any page script
 * via CDP Page.addScriptToEvaluateOnNewDocument.
 */
function buildFingerprintInjectionScript(identity) {
  const cfg = {
    languages: identity.languages,
    locale: identity.locale,
    navigatorPlatform: identity.navigatorPlatform,
    hardwareConcurrency: identity.hardwareConcurrency,
    deviceMemory: identity.deviceMemory,
    screen: identity.screen,
    webgl: identity.webgl,
    seeds: identity.seeds,
    webrtcPolicy: identity.webrtcPolicy,
    canvasPolicy: identity.canvasPolicy,
  };

  const serializedConfig = JSON.stringify(cfg).replace(/</g, "\\u003c").replace(/>/g, "\\u003e");

  return `(() => {
  "use strict";
  const CFG = ${serializedConfig};

  const nativeToStringMap = new WeakMap();
  const origFnToString = Function.prototype.toString;
  const markNative = (fn, name) => {
    if (typeof fn === "function") {
      nativeToStringMap.set(fn, "function " + (name || fn.name || "") + "() { [native code] }");
    }
    return fn;
  };
  try {
    const wrappedToString = function toString() {
      if (nativeToStringMap.has(this)) {
        return nativeToStringMap.get(this);
      }
      return origFnToString.call(this);
    };
    markNative(wrappedToString, "toString");
    Object.defineProperty(Function.prototype, "toString", {
      value: wrappedToString,
      writable: true,
      configurable: true,
    });
  } catch (_) {}

  const hashSeed = (str) => {
    let h = 2166136261 >>> 0;
    const s = String(str || "");
    for (let i = 0; i < s.length; i++) {
      h ^= s.charCodeAt(i);
      h = Math.imul(h, 16777619) >>> 0;
    }
    return h >>> 0;
  };

  const makePrng = (seedInt) => {
    let a = (seedInt >>> 0) || 1;
    return () => {
      a = (a + 0x6d2b79f5) >>> 0;
      let t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  };

  const defineGetter = (target, prop, value) => {
    if (!target) return;
    try {
      const getter = markNative(function () { return value; }, "get " + prop);
      Object.defineProperty(target, prop, {
        get: getter,
        set: undefined,
        enumerable: true,
        configurable: true,
      });
    } catch (_) {}
  };

  // 1. Navigator stealth & hardware consistency
  try {
    const navProto = typeof Navigator !== "undefined" ? Navigator.prototype : Object.getPrototypeOf(navigator);
    defineGetter(navProto, "webdriver", undefined);
    defineGetter(navProto, "deviceMemory", CFG.deviceMemory);
    defineGetter(navProto, "hardwareConcurrency", CFG.hardwareConcurrency);
    defineGetter(navProto, "platform", CFG.navigatorPlatform);
    const frozenLangs = Object.freeze([...CFG.languages]);
    defineGetter(navProto, "languages", frozenLangs);
    defineGetter(navProto, "language", CFG.languages[0] || "en-US");
    Object.defineProperty(navProto, "getBattery", {
      value: undefined,
      writable: true,
      enumerable: false,
      configurable: true,
    });
    if ("webdriver" in navigator) {
      try { delete navigator.webdriver; } catch (_) {}
    }
  } catch (_) {}

  // 2. Screen metrics consistency
  try {
    const screenProto = typeof Screen !== "undefined" ? Screen.prototype : Object.getPrototypeOf(window.screen);
    defineGetter(screenProto, "width", CFG.screen.width);
    defineGetter(screenProto, "height", CFG.screen.height);
    defineGetter(screenProto, "availWidth", CFG.screen.availWidth);
    defineGetter(screenProto, "availHeight", CFG.screen.availHeight);
    defineGetter(screenProto, "colorDepth", CFG.screen.colorDepth);
    defineGetter(screenProto, "pixelDepth", CFG.screen.pixelDepth);
    defineGetter(screenProto, "availLeft", 0);
    defineGetter(screenProto, "availTop", 0);
    if (typeof CFG.screen.devicePixelRatio === "number" && CFG.screen.devicePixelRatio > 0) {
      defineGetter(window, "devicePixelRatio", CFG.screen.devicePixelRatio);
    }
  } catch (_) {}

  // 3. Deterministic Canvas 2D pixel noise (stable per profile, unique across profiles)
  if (CFG.canvasPolicy !== "off") {
    const canvasSeedInt = hashSeed("canvas:" + CFG.seeds.canvas);

    const perturbPixels = (data, width, height, tag) => {
      if (!data || data.length < 4) return;
      const totalPixels = Math.floor(data.length / 4);
      const rand = makePrng((canvasSeedInt ^ hashSeed(width + "x" + height + ":" + tag)) >>> 0);
      const sampleCount = Math.min(24, Math.max(4, Math.floor(totalPixels / 64)));
      for (let i = 0; i < sampleCount; i++) {
        const pxIndex = Math.floor(rand() * totalPixels);
        const offset = pxIndex * 4;
        if (data[offset + 3] === 0) continue; // preserve fully transparent pixels
        const channel = Math.floor(rand() * 3); // R, G, or B
        const delta = rand() < 0.5 ? -1 : 1;
        const cur = data[offset + channel];
        data[offset + channel] = cur === 0 ? 1 : cur === 255 ? 254 : cur + delta;
      }
    };

    try {
      if (typeof CanvasRenderingContext2D !== "undefined") {
        const origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
        const wrappedGetImageData = function getImageData(sx, sy, sw, sh, settings) {
          const img = settings !== undefined
            ? origGetImageData.call(this, sx, sy, sw, sh, settings)
            : origGetImageData.call(this, sx, sy, sw, sh);
          try {
            perturbPixels(img.data, img.width, img.height, sx + "," + sy);
          } catch (_) {}
          return img;
        };
        markNative(wrappedGetImageData, "getImageData");
        CanvasRenderingContext2D.prototype.getImageData = wrappedGetImageData;
      }

      if (typeof HTMLCanvasElement !== "undefined") {
        const twoDCanvases = new WeakSet();
        const origGetContext = HTMLCanvasElement.prototype.getContext;
        if (typeof origGetContext === "function") {
          const wrappedGetContext = function getContext(type, ...rest) {
            if (type === "2d") {
              twoDCanvases.add(this);
            }
            return origGetContext.call(this, type, ...rest);
          };
          markNative(wrappedGetContext, "getContext");
          HTMLCanvasElement.prototype.getContext = wrappedGetContext;
        }

        const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
        const origToBlob = HTMLCanvasElement.prototype.toBlob;

        const makePerturbedClone = (canvas) => {
          const w = canvas.width;
          const h = canvas.height;
          if (!w || !h || !twoDCanvases.has(canvas)) return null;
          // Clone so we never mutate the visible canvas and repeated exports stay identical.
          const clone = document.createElement("canvas");
          clone.width = w;
          clone.height = h;
          const cloneCtx = origGetContext.call(clone, "2d");
          if (!cloneCtx) return null;
          cloneCtx.drawImage(canvas, 0, 0);
          // cloneCtx.getImageData calls our wrapped deterministic perturbation once.
          const img = cloneCtx.getImageData(0, 0, w, h);
          cloneCtx.putImageData(img, 0, 0);
          return clone;
        };

        const wrappedToDataURL = function toDataURL(...args) {
          try {
            const clone = makePerturbedClone(this);
            if (clone) {
              return origToDataURL.apply(clone, args);
            }
          } catch (_) {
            // Tainted or WebGL canvas: fall through to native behavior cleanly.
          }
          return origToDataURL.apply(this, args);
        };
        markNative(wrappedToDataURL, "toDataURL");
        HTMLCanvasElement.prototype.toDataURL = wrappedToDataURL;

        if (typeof origToBlob === "function") {
          const wrappedToBlob = function toBlob(callback, ...rest) {
            try {
              const clone = makePerturbedClone(this);
              if (clone) {
                return origToBlob.call(clone, callback, ...rest);
              }
            } catch (_) {}
            return origToBlob.call(this, callback, ...rest);
          };
          markNative(wrappedToBlob, "toBlob");
          HTMLCanvasElement.prototype.toBlob = wrappedToBlob;
        }
      }
    } catch (_) {}
  }

  // 4. WebGL1 & WebGL2 vendor/renderer spoofing + deterministic subtle readPixels noise
  try {
    const webglSeedInt = hashSeed("webgl:" + CFG.seeds.webgl);
    const patchWebGLProto = (proto) => {
      if (!proto) return;
      const origGetParameter = proto.getParameter;
      if (typeof origGetParameter === "function") {
        const wrappedGetParameter = function getParameter(param) {
          if (param === 37445) return CFG.webgl.vendor;
          if (param === 37446) return CFG.webgl.renderer;
          if (param === 7936) return "WebKit";
          if (param === 7937) return "WebKit WebGL";
          return origGetParameter.call(this, param);
        };
        markNative(wrappedGetParameter, "getParameter");
        proto.getParameter = wrappedGetParameter;
      }

      const origGetExtension = proto.getExtension;
      if (typeof origGetExtension === "function") {
        const wrappedGetExtension = function getExtension(name) {
          const ext = origGetExtension.call(this, name);
          if (!ext && name === "WEBGL_debug_renderer_info") {
            return { UNMASKED_VENDOR_WEBGL: 37445, UNMASKED_RENDERER_WEBGL: 37446 };
          }
          return ext;
        };
        markNative(wrappedGetExtension, "getExtension");
        proto.getExtension = wrappedGetExtension;
      }

      const origReadPixels = proto.readPixels;
      if (typeof origReadPixels === "function") {
        const wrappedReadPixels = function readPixels(x, y, w, h, format, type, pixels, ...rest) {
          const res = origReadPixels.call(this, x, y, w, h, format, type, pixels, ...rest);
          try {
            if (pixels instanceof Uint8Array && pixels.length >= 16) {
              const rand = makePrng((webglSeedInt ^ hashSeed(w + "x" + h)) >>> 0);
              const pxCount = Math.floor(pixels.length / 4);
              const picks = Math.min(8, pxCount);
              for (let i = 0; i < picks; i++) {
                const idx = Math.floor(rand() * pxCount) * 4;
                if (pixels[idx + 3] === 0) continue;
                const ch = Math.floor(rand() * 3);
                const cur = pixels[idx + ch];
                pixels[idx + ch] = cur === 0 ? 1 : cur === 255 ? 254 : cur + (rand() < 0.5 ? -1 : 1);
              }
            }
          } catch (_) {}
          return res;
        };
        markNative(wrappedReadPixels, "readPixels");
        proto.readPixels = wrappedReadPixels;
      }
    };

    if (typeof WebGLRenderingContext !== "undefined") {
      patchWebGLProto(WebGLRenderingContext.prototype);
    }
    if (typeof WebGL2RenderingContext !== "undefined") {
      patchWebGLProto(WebGL2RenderingContext.prototype);
    }
  } catch (_) {}

  // 5. AudioContext deterministic subtle noise
  try {
    const audioSeedInt = hashSeed("audio:" + CFG.seeds.audio);
    if (typeof AudioBuffer !== "undefined" && AudioBuffer.prototype.getChannelData) {
      const perturbedBuffers = new WeakSet();
      const origGetChannelData = AudioBuffer.prototype.getChannelData;
      const wrappedGetChannelData = function getChannelData(channel) {
        const data = origGetChannelData.call(this, channel);
        if (data && data.length > 0 && !perturbedBuffers.has(data)) {
          perturbedBuffers.add(data);
          const rand = makePrng((audioSeedInt ^ ((channel + 1) * 2654435761)) >>> 0);
          const stride = Math.max(1, Math.floor(data.length / 64));
          for (let i = 0; i < data.length; i += stride) {
            const delta = (rand() - 0.5) * 1e-7;
            data[i] = data[i] + delta;
          }
        }
        return data;
      };
      markNative(wrappedGetChannelData, "getChannelData");
      AudioBuffer.prototype.getChannelData = wrappedGetChannelData;
    }

    if (typeof AnalyserNode !== "undefined" && AnalyserNode.prototype.getFloatFrequencyData) {
      const origGetFloatFreq = AnalyserNode.prototype.getFloatFrequencyData;
      const wrappedGetFloatFreq = function getFloatFrequencyData(array) {
        origGetFloatFreq.call(this, array);
        if (array && array.length > 0) {
          const rand = makePrng((audioSeedInt ^ 0x9e3779b9) >>> 0);
          const stride = Math.max(1, Math.floor(array.length / 32));
          for (let i = 0; i < array.length; i += stride) {
            if (Number.isFinite(array[i])) {
              array[i] = array[i] + (rand() - 0.5) * 1e-4;
            }
          }
        }
      };
      markNative(wrappedGetFloatFreq, "getFloatFrequencyData");
      AnalyserNode.prototype.getFloatFrequencyData = wrappedGetFloatFreq;
    }
  } catch (_) {}

  // 6. WebRTC hard-block when policy === "block"
  if (CFG.webrtcPolicy === "block") {
    const blockedApis = [
      "RTCPeerConnection",
      "webkitRTCPeerConnection",
      "mozRTCPeerConnection",
      "RTCDataChannel",
    ];
    for (const apiName of blockedApis) {
      try {
        Object.defineProperty(window, apiName, {
          value: undefined,
          writable: false,
          enumerable: false,
          configurable: false,
        });
      } catch (_) {
        try {
          window[apiName] = undefined;
        } catch (_) {}
      }
    }
  }
})();
`;
}

const sessionIdentityMap = new WeakMap();

/**
 * Configure per-profile Electron Session UA and Accept-Language request headers.
 */
function configureSessionIdentity(ses, identity) {
  ses.setUserAgent(identity.userAgent, identity.acceptLanguage);
  const alreadyHooked = sessionIdentityMap.has(ses);
  sessionIdentityMap.set(ses, identity);

  if (!alreadyHooked) {
    ses.webRequest.onBeforeSendHeaders((details, callback) => {
      const current = sessionIdentityMap.get(ses);
      const headers = { ...(details.requestHeaders || {}) };
      if (current) {
        headers["User-Agent"] = current.userAgent;
        headers["Accept-Language"] = current.acceptLanguage;
      }
      callback({ requestHeaders: headers });
    });
  }
}

/**
 * Attach CDP 1.3 and enforce full timezone, locale, UA/Client Hints, hardware concurrency,
 * and deterministic main-world fingerprint injection BEFORE first external loadURL.
 */
async function attachFingerprintToWebContents(webContents, identity) {
  webContents.setWebRTCIPHandlingPolicy("disable_non_proxied_udp");
  webContents.setUserAgent(identity.userAgent);

  // A brand-new WebContentsView has no active RenderFrameHost until its first document
  // loads, causing CDP Page/Emulation commands to hang forever. Bootstrapping local
  // about:blank (zero network) spawns the frame cleanly before any real navigation.
  if (
    typeof webContents.getURL === "function" &&
    typeof webContents.loadURL === "function" &&
    !webContents.getURL()
  ) {
    await webContents.loadURL("about:blank");
  }

  const dbg = webContents.debugger;
  if (!dbg.isAttached()) {
    dbg.attach("1.3");
  }

  await dbg.sendCommand("Page.enable");
  await dbg.sendCommand("Emulation.setTimezoneOverride", {
    timezoneId: identity.timezone || DEFAULT_TIMEZONE,
  });
  await dbg.sendCommand("Emulation.setLocaleOverride", {
    locale: identity.locale || "en-US",
  });
  await dbg.sendCommand("Emulation.setUserAgentOverride", {
    userAgent: identity.userAgent,
    acceptLanguage: identity.acceptLanguage,
    platform: identity.navigatorPlatform,
    userAgentMetadata: identity.userAgentMetadata,
  });
  await dbg.sendCommand("Emulation.setHardwareConcurrencyOverride", {
    hardwareConcurrency: identity.hardwareConcurrency,
  });

  const source = buildFingerprintInjectionScript(identity);
  await dbg.sendCommand("Page.addScriptToEvaluateOnNewDocument", { source });
}

module.exports = {
  buildChromeIdentity,
  buildFingerprintInjectionScript,
  buildAcceptLanguage,
  sanitizeChromeUserAgent,
  configureSessionIdentity,
  attachFingerprintToWebContents,
};
