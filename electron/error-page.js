"use strict";

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function escapeJsString(value) {
  return JSON.stringify(String(value ?? "")).replace(/</g, "\\u003c").replace(/>/g, "\\u003e");
}

function describeErrorHint(errorCode, errorDescription) {
  const desc = String(errorDescription || "").toUpperCase();
  if (
    desc.includes("PROXY") ||
    desc.includes("TUNNEL") ||
    errorCode === -130 ||
    errorCode === -111
  ) {
    return "独立代理路由通道暂时无法连接到目标站点，请检查当前环境绑定的节点状态或稍后重试。";
  }
  if (desc.includes("NAME_NOT_RESOLVED") || errorCode === -105) {
    return "远程域名解析未返回结果。为防止本地 DNS 泄露，当前隔离环境仅允许通过独立代理通道解析域名。";
  }
  if (desc.includes("TIMED_OUT") || errorCode === -118 || errorCode === -7) {
    return "连接目标站点超时，当前节点网络延迟较高或目标站点暂不可达。";
  }
  if (desc.includes("CERT") || desc.includes("SSL")) {
    return "目标站点的安全证书校验未通过，为保护当前环境安全已阻止继续加载。";
  }
  return "当前隔离环境未能完成页面加载，请确认代理节点连通性后点击重新加载。";
}

function buildPrismSvg(size = 44) {
  return `<svg width="${size}" height="${size}" viewBox="0 0 128 128" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
  <defs>
    <linearGradient id="pbg" x1="14" y1="4" x2="114" y2="126" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#0A0F24"/>
      <stop offset="50%" stop-color="#171642"/>
      <stop offset="100%" stop-color="#2A1D66"/>
    </linearGradient>
    <linearGradient id="pIn" x1="6" y1="73" x2="44" y2="61" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#FFFFFF" stop-opacity="0"/>
      <stop offset="100%" stop-color="#FFFFFF" stop-opacity="1"/>
    </linearGradient>
    <linearGradient id="pCyan" x1="75" y1="53" x2="122" y2="36" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#E0F2FE"/>
      <stop offset="40%" stop-color="#38BDF8"/>
      <stop offset="100%" stop-color="#0284C7" stop-opacity="0"/>
    </linearGradient>
    <linearGradient id="pViolet" x1="79" y1="62" x2="122" y2="62" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#F5F3FF"/>
      <stop offset="45%" stop-color="#818CF8"/>
      <stop offset="100%" stop-color="#9333EA" stop-opacity="0"/>
    </linearGradient>
    <linearGradient id="pAmber" x1="83" y1="71" x2="122" y2="88" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#FEF3C7"/>
      <stop offset="45%" stop-color="#FB923C"/>
      <stop offset="100%" stop-color="#E11D48" stop-opacity="0"/>
    </linearGradient>
    <linearGradient id="pFacetL" x1="34" y1="26" x2="61" y2="86" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#FFFFFF" stop-opacity="0.44"/>
      <stop offset="100%" stop-color="#818CF8" stop-opacity="0.1"/>
    </linearGradient>
    <linearGradient id="pFacetR" x1="61" y1="24" x2="94" y2="95" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#C7D2FE" stop-opacity="0.24"/>
      <stop offset="100%" stop-color="#38BDF8" stop-opacity="0.16"/>
    </linearGradient>
  </defs>
  <rect x="1" y="1" width="126" height="126" rx="28" fill="url(#pbg)" stroke="rgba(255,255,255,0.18)" stroke-width="2"/>
  <polygon points="6,71.8 43.5,59.5 43.5,62.5 6,75" fill="url(#pIn)"/>
  <polygon points="75,52.2 123,33.5 123,40 75.6,55.2" fill="url(#pCyan)"/>
  <polygon points="79,60.8 124,59.5 124,65.2 79.6,64" fill="url(#pViolet)"/>
  <polygon points="83,69.6 123,86 123,92.5 83.8,72.8" fill="url(#pAmber)"/>
  <polygon points="61,23 27,96 61,74" fill="url(#pFacetL)"/>
  <polygon points="61,23 61,74 95,96" fill="url(#pFacetR)"/>
  <polygon points="27,96 61,74 95,96" fill="#A5B4FC" fill-opacity="0.16"/>
  <path d="M61 23 L95 96 L27 96 Z" stroke="#E0E7FF" stroke-opacity="0.9" stroke-width="3" stroke-linejoin="round"/>
  <circle cx="43.5" cy="61" r="3.8" fill="#FFFFFF" fill-opacity="0.94"/>
</svg>`;
}

/**
 * Build a clean white-label Prism Browser HTML error page.
 * Zero upstream/engine branding. Safe against HTML/JS injection.
 */
function buildErrorPageHtml({ errorCode = -2, errorDescription = "ERR_FAILED", validatedUrl = "" }) {
  const safeUrl = escapeHtml(validatedUrl || "目标页面");
  const safeCode = escapeHtml(errorDescription || `ERR_${errorCode}`);
  const hint = escapeHtml(describeErrorHint(errorCode, errorDescription));
  const retryTargetJs = escapeJsString(validatedUrl || "https://www.google.com/");

  return `<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>无法访问此页面 · 棱镜浏览器</title>
  <style>
    :root {
      color-scheme: dark light;
      --bg: #090b10;
      --card: #12151e;
      --border: rgba(255, 255, 255, 0.1);
      --ink: #f1f4fa;
      --dim: #95a2ba;
      --pill: rgba(255, 255, 255, 0.045);
      --btn-bg: linear-gradient(180deg, #8281ff 0%, #5e5ce6 100%);
      --btn-hover: linear-gradient(180deg, #8f8eff 0%, #6866f0 100%);
      --shadow: 0 1px 0 rgba(255, 255, 255, 0.06) inset, 0 28px 80px -20px rgba(0, 0, 0, 0.8);
    }
    @media (prefers-color-scheme: light) {
      :root {
        --bg: #f7f8fa;
        --card: #ffffff;
        --border: rgba(15, 23, 42, 0.09);
        --ink: #0d1220;
        --dim: #4a5568;
        --pill: #f2f4f7;
        --btn-bg: linear-gradient(180deg, #5850ec 0%, #4338ca 100%);
        --btn-hover: linear-gradient(180deg, #4f46e5 0%, #3730a3 100%);
        --shadow: 0 24px 60px -20px rgba(15, 23, 42, 0.16);
      }
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      padding: 24px;
      background:
        radial-gradient(640px 360px at 50% 0%, rgba(124, 123, 255, 0.12), transparent 65%),
        var(--bg);
      color: var(--ink);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
      -webkit-font-smoothing: antialiased;
    }
    .card {
      position: relative;
      width: 100%;
      max-width: 520px;
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 20px;
      padding: 28px 30px;
      box-shadow: var(--shadow);
      overflow: hidden;
    }
    .hairline {
      position: absolute;
      inset: 0 0 auto 0;
      height: 2px;
      background: linear-gradient(90deg, transparent 0%, rgba(56,189,248,0.65) 26%, rgba(129,140,248,0.9) 52%, rgba(251,146,60,0.6) 76%, transparent 100%);
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 12px;
      margin-bottom: 18px;
    }
    .brand-title {
      font-size: 12.5px;
      font-weight: 600;
      letter-spacing: 0.04em;
      color: var(--dim);
    }
    h1 {
      margin: 0 0 10px;
      font-size: 20px;
      font-weight: 650;
      line-height: 1.35;
      letter-spacing: -0.01em;
    }
    p {
      margin: 0 0 18px;
      font-size: 13.5px;
      line-height: 1.65;
      color: var(--dim);
    }
    .meta {
      background: var(--pill);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 12px 14px;
      margin-bottom: 22px;
      font-size: 12px;
      line-height: 1.5;
      word-break: break-all;
    }
    .meta-row { display: flex; gap: 8px; }
    .meta-label { color: var(--dim); flex: none; }
    .code {
      margin-top: 6px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 11.5px;
      color: var(--dim);
    }
    .actions {
      display: flex;
      align-items: center;
      gap: 10px;
    }
    button {
      appearance: none;
      border: 1px solid transparent;
      border-radius: 10px;
      padding: 9px 18px;
      font-size: 13px;
      font-weight: 600;
      cursor: pointer;
      transition: background 140ms ease, transform 100ms ease, border-color 140ms ease;
    }
    button:active { transform: scale(0.98); }
    .btn-primary {
      background: var(--btn-bg);
      color: #ffffff;
      box-shadow: 0 1px 0 rgba(255, 255, 255, 0.22) inset, 0 6px 16px -6px rgba(99, 102, 241, 0.65);
    }
    .btn-primary:hover { background: var(--btn-hover); }
    .btn-secondary {
      background: var(--pill);
      border-color: var(--border);
      color: var(--ink);
    }
    .btn-secondary:hover { opacity: 0.88; }
  </style>
</head>
<body>
  <main class="card" role="main">
    <div class="hairline" aria-hidden="true"></div>
    <div class="brand">
      ${buildPrismSvg(36)}
      <span class="brand-title">棱镜浏览器 · 隔离网络保护</span>
    </div>
    <h1>无法连接到该页面</h1>
    <p>${hint}</p>
    <div class="meta">
      <div class="meta-row"><span class="meta-label">目标地址</span><span>${safeUrl}</span></div>
      <div class="code">${safeCode} (${Number(errorCode) || 0})</div>
    </div>
    <div class="actions">
      <button id="retry-btn" class="btn-primary" type="button">重新加载</button>
      <button id="back-btn" class="btn-secondary" type="button">返回上一页</button>
    </div>
  </main>
  <script>
    (function () {
      var retryUrl = ${retryTargetJs};
      document.getElementById("retry-btn").addEventListener("click", function () {
        if (retryUrl && /^https?:\\/\\//i.test(retryUrl)) {
          window.location.replace(retryUrl);
        } else {
          window.location.reload();
        }
      });
      document.getElementById("back-btn").addEventListener("click", function () {
        if (window.history.length > 1) {
          window.history.back();
        }
      });
    })();
  </script>
</body>
</html>`;
}

function buildErrorPageDataUrl(params) {
  const html = buildErrorPageHtml(params);
  return `data:text/html;charset=utf-8,${encodeURIComponent(html)}`;
}

/**
 * Clean Prism Browser New Tab start page for instant zero-latency tab creation.
 */
function buildNewTabHtml({ exitIp = "", exitCountry = "", exitCity = "", fingerprintHash = "" } = {}) {
  const locationParts = [exitCountry, exitCity].filter(Boolean).join(" · ");
  const exitSummary = exitIp
    ? `${escapeHtml(exitIp)}${locationParts ? ` · ${escapeHtml(locationParts)}` : ""}`
    : "独立隔离环境已就绪";
  const fpPill = fingerprintHash
    ? `<span class="pill">FP-${escapeHtml(String(fingerprintHash).slice(0, 8).toUpperCase())}</span>`
    : "";

  return `<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>新标签页</title>
  <style>
    :root {
      color-scheme: dark light;
      --bg: #090b10;
      --card: #12151e;
      --card-hover: #181d29;
      --border: rgba(255, 255, 255, 0.09);
      --border-hover: rgba(124, 123, 255, 0.42);
      --ink: #f1f4fa;
      --dim: #95a2ba;
      --pill: rgba(124, 123, 255, 0.14);
      --pill-border: rgba(124, 123, 255, 0.28);
      --pill-ink: #c7d2fe;
      --card-shadow: 0 1px 0 rgba(255, 255, 255, 0.05) inset, 0 10px 26px -14px rgba(0, 0, 0, 0.65);
    }
    @media (prefers-color-scheme: light) {
      :root {
        --bg: #f7f8fa;
        --card: #ffffff;
        --card-hover: #fcfdff;
        --border: rgba(15, 23, 42, 0.09);
        --border-hover: rgba(79, 70, 229, 0.38);
        --ink: #0d1220;
        --dim: #526078;
        --pill: rgba(79, 70, 229, 0.08);
        --pill-border: rgba(79, 70, 229, 0.22);
        --pill-ink: #4338ca;
        --card-shadow: 0 2px 10px -4px rgba(15, 23, 42, 0.07);
      }
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      padding: 32px 24px;
      background:
        radial-gradient(680px 340px at 50% 10%, rgba(124, 123, 255, 0.14), transparent 65%),
        radial-gradient(480px 260px at 72% 24%, rgba(56, 189, 248, 0.08), transparent 70%),
        var(--bg);
      color: var(--ink);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
      -webkit-font-smoothing: antialiased;
    }
    .wrap {
      width: 100%;
      max-width: 580px;
      text-align: center;
    }
    .logo {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      margin-bottom: 16px;
      filter: drop-shadow(0 10px 24px rgba(99, 102, 241, 0.35));
    }
    h1 {
      margin: 0 0 8px;
      font-size: 21px;
      font-weight: 650;
      letter-spacing: -0.015em;
    }
    .sub {
      margin: 0 0 20px;
      font-size: 13px;
      color: var(--dim);
    }
    .pills {
      display: flex;
      justify-content: center;
      flex-wrap: wrap;
      gap: 8px;
      margin-bottom: 28px;
    }
    .pill {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 5px 12px;
      border-radius: 999px;
      background: var(--pill);
      border: 1px solid var(--pill-border);
      color: var(--pill-ink);
      font-size: 12px;
      font-weight: 600;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    }
    .links {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
      text-align: left;
    }
    a.shortcut {
      display: block;
      padding: 15px 17px;
      border-radius: 14px;
      background: var(--card);
      border: 1px solid var(--border);
      box-shadow: var(--card-shadow);
      text-decoration: none;
      color: var(--ink);
      transition: background 150ms ease, border-color 150ms ease, transform 150ms ease;
    }
    a.shortcut:hover {
      background: var(--card-hover);
      border-color: var(--border-hover);
      transform: translateY(-2px);
    }
    .shortcut-title {
      font-size: 13.5px;
      font-weight: 600;
      margin-bottom: 3px;
    }
    .shortcut-host {
      font-size: 11.5px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      color: var(--dim);
    }
  </style>
</head>
<body>
  <main class="wrap">
    <div class="logo">${buildPrismSvg(52)}</div>
    <h1>棱镜浏览器 · 隔离工作空间</h1>
    <p class="sub">请在上方地址栏输入网址或搜索词，当前标签页全程运行于独立隔离会话中。</p>
    <div class="pills">
      <span class="pill">${exitSummary}</span>
      ${fpPill}
    </div>
    <div class="links">
      <a class="shortcut" href="https://www.google.com/">
        <div class="shortcut-title">Google 搜索</div>
        <div class="shortcut-host">google.com</div>
      </a>
      <a class="shortcut" href="https://browserleaks.com/ip">
        <div class="shortcut-title">IP 与 DNS 泄露检测</div>
        <div class="shortcut-host">browserleaks.com/ip</div>
      </a>
      <a class="shortcut" href="https://browserleaks.com/webrtc">
        <div class="shortcut-title">WebRTC 隔离检测</div>
        <div class="shortcut-host">browserleaks.com/webrtc</div>
      </a>
      <a class="shortcut" href="https://browserleaks.com/canvas">
        <div class="shortcut-title">硬件指纹一致性检测</div>
        <div class="shortcut-host">browserleaks.com/canvas</div>
      </a>
      <a class="shortcut" href="https://proton.me/">
        <div class="shortcut-title">Proton 邮箱</div>
        <div class="shortcut-host">proton.me</div>
      </a>
      <a class="shortcut" href="https://claude.ai/">
        <div class="shortcut-title">Claude AI</div>
        <div class="shortcut-host">claude.ai</div>
      </a>
    </div>
  </main>
</body>
</html>`;
}

function buildNewTabDataUrl(meta) {
  return `data:text/html;charset=utf-8,${encodeURIComponent(buildNewTabHtml(meta))}`;
}

function buildStartupSplashDataUrl() {
  const html = `<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <title>棱镜浏览器</title>
  <style>
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      background:
        radial-gradient(620px 360px at 50% 34%, rgba(124, 123, 255, 0.16), transparent 68%),
        radial-gradient(420px 260px at 64% 48%, rgba(56, 189, 248, 0.09), transparent 72%),
        #090b10;
      color: #f1f4fa;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", sans-serif;
      -webkit-font-smoothing: antialiased;
    }
    .box {
      display: flex;
      flex-direction: column;
      align-items: center;
      text-align: center;
    }
    .mark {
      filter: drop-shadow(0 12px 28px rgba(99, 102, 241, 0.42));
    }
    .title {
      margin-top: 18px;
      font-size: 17px;
      font-weight: 600;
      letter-spacing: 0.02em;
    }
    .sub {
      margin-top: 6px;
      font-size: 12px;
      color: #95a2ba;
    }
    .bar {
      margin-top: 22px;
      width: 148px;
      height: 2.5px;
      border-radius: 999px;
      background: rgba(255, 255, 255, 0.09);
      overflow: hidden;
      position: relative;
    }
    .bar::after {
      content: "";
      position: absolute;
      inset: 0 auto 0 0;
      width: 56%;
      border-radius: 999px;
      background: linear-gradient(90deg, #38bdf8, #818cf8, #fb923c);
      animation: slide 1.4s cubic-bezier(0.4, 0, 0.2, 1) infinite;
    }
    @keyframes slide {
      0% { transform: translateX(-110%); }
      100% { transform: translateX(220%); }
    }
  </style>
</head>
<body>
  <div class="box">
    <div class="mark">${buildPrismSvg(56)}</div>
    <div class="title">棱镜浏览器</div>
    <div class="sub">正在启动本地隔离核心服务…</div>
    <div class="bar" aria-hidden="true"></div>
  </div>
</body>
</html>`;
  return `data:text/html;charset=utf-8,${encodeURIComponent(html)}`;
}

module.exports = {
  buildErrorPageHtml,
  buildErrorPageDataUrl,
  buildNewTabHtml,
  buildNewTabDataUrl,
  buildStartupSplashDataUrl,
};
