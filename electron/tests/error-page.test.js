"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const {
  buildErrorPageHtml,
  buildErrorPageDataUrl,
  buildNewTabHtml,
  buildStartupSplashDataUrl,
} = require("../error-page");

const FORBIDDEN_BRAND_RE =
  /camoufox|chromium|electron|firefox|mihomo|playwright|daijro|lulu|orange\s*fox/i;

test("error page is 100% white-label Prism Browser with retry button and no forbidden strings", () => {
  const html = buildErrorPageHtml({
    errorCode: -130,
    errorDescription: "ERR_PROXY_CONNECTION_FAILED",
    validatedUrl: "https://sellercentral.amazon.com/",
  });

  assert.ok(html.includes("棱镜浏览器"), "Missing Prism Browser Chinese title");
  assert.ok(html.includes("ERR_PROXY_CONNECTION_FAILED"));
  assert.ok(html.includes("https://sellercentral.amazon.com/"));
  assert.ok(html.includes('id="retry-btn"'));
  assert.ok(!FORBIDDEN_BRAND_RE.test(html), "Forbidden upstream branding leaked into error page");

  const dataUrl = buildErrorPageDataUrl({
    errorCode: -105,
    errorDescription: "ERR_NAME_NOT_RESOLVED",
    validatedUrl: "https://example.com/",
  });
  assert.ok(dataUrl.startsWith("data:text/html;charset=utf-8,"));
  const decoded = decodeURIComponent(dataUrl.slice("data:text/html;charset=utf-8,".length));
  assert.ok(!FORBIDDEN_BRAND_RE.test(decoded));
});

test("error page safely escapes XSS payloads in URL and errorDescription", () => {
  const html = buildErrorPageHtml({
    errorCode: -2,
    errorDescription: `<script>alert("xss")</script>`,
    validatedUrl: `https://evil.test/"'><script>alert(1)</script>`,
  });
  assert.ok(!html.includes(`<script>alert("xss")</script>`));
  assert.ok(!html.includes(`<script>alert(1)</script>`));
  assert.ok(html.includes("&lt;script&gt;"));
});

test("new tab and startup splash pages contain zero forbidden upstream strings", () => {
  const newTab = buildNewTabHtml({
    exitIp: "45.77.10.22",
    exitCountry: "US",
    exitCity: "Los Angeles",
    fingerprintHash: "ab12cd34ef56",
  });
  assert.ok(newTab.includes("棱镜浏览器"));
  assert.ok(newTab.includes("45.77.10.22"));
  assert.ok(newTab.includes("FP-AB12CD34"));
  assert.ok(newTab.includes('href="https://proton.me/"'));
  assert.ok(newTab.includes('href="https://claude.ai/"'));
  assert.ok(!FORBIDDEN_BRAND_RE.test(newTab));

  const splashUrl = buildStartupSplashDataUrl();
  const splashHtml = decodeURIComponent(splashUrl.slice("data:text/html;charset=utf-8,".length));
  assert.ok(splashHtml.includes("棱镜浏览器"));
  assert.ok(!FORBIDDEN_BRAND_RE.test(splashHtml));
});
