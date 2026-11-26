"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { normalizeOmniboxInput, DEFAULT_HOME_URL } = require("../omnibox");

test("empty or whitespace input falls back to default home URL", () => {
  assert.equal(normalizeOmniboxInput(""), DEFAULT_HOME_URL);
  assert.equal(normalizeOmniboxInput("   "), DEFAULT_HOME_URL);
  assert.equal(normalizeOmniboxInput(null), DEFAULT_HOME_URL);
  assert.equal(normalizeOmniboxInput("about:blank"), "about:blank");
});

test("explicit http and https URLs navigate directly", () => {
  assert.equal(
    normalizeOmniboxInput("https://browserleaks.com/ip?foo=1#top"),
    "https://browserleaks.com/ip?foo=1#top"
  );
  assert.equal(
    normalizeOmniboxInput("http://example.com/login"),
    "http://example.com/login"
  );
});

test("scheme-less domains normalize to https", () => {
  assert.equal(normalizeOmniboxInput("example.com"), "https://example.com/");
  assert.equal(
    normalizeOmniboxInput("sub.domain.co.uk/path?a=1&b=2#sec"),
    "https://sub.domain.co.uk/path?a=1&b=2#sec"
  );
  assert.equal(normalizeOmniboxInput("browserleaks.com/webrtc"), "https://browserleaks.com/webrtc");
  assert.equal(normalizeOmniboxInput("chatgpt.com:443/chat"), "https://chatgpt.com/chat");
  assert.equal(normalizeOmniboxInput("xn--bcher-kva.example"), "https://xn--bcher-kva.example/");
});

test("localhost and IP literals normalize to http", () => {
  assert.equal(normalizeOmniboxInput("localhost"), "http://localhost/");
  assert.equal(normalizeOmniboxInput("localhost:3000/api"), "http://localhost:3000/api");
  assert.equal(normalizeOmniboxInput("127.0.0.1:8080/health"), "http://127.0.0.1:8080/health");
  assert.equal(normalizeOmniboxInput("192.168.1.1"), "http://192.168.1.1/");
  assert.equal(normalizeOmniboxInput("[::1]:8000"), "http://[::1]:8000/");
});

test("plain text queries search Google and NEVER produce 127.0.0.1/?q=", () => {
  const queries = [
    "what is my ip",
    "amazon seller central",
    "棱镜浏览器 防关联",
    "v1.2",
    "1.5 kg",
    "c++ tutorial",
    "singleword",
    "foo/bar",
    ".env",
    "foo..bar.com",
    "999.999.999.999",
  ];
  for (const q of queries) {
    const out = normalizeOmniboxInput(q);
    assert.equal(
      out,
      `https://www.google.com/search?q=${encodeURIComponent(q)}`,
      `Expected Google search for "${q}", got "${out}"`
    );
    assert.ok(!out.includes("127.0.0.1"), `Must never route search to localhost: ${out}`);
  }
});

test("dangerous internal schemes cannot navigate directly and fall back to Google search", () => {
  const dangerous = [
    "javascript:alert(1)",
    "file:///etc/passwd",
    "chrome://settings",
    "chrome-extension://abcdef/index.html",
    "devtools://devtools/bundled/inspector.html",
    "data:text/html,<script>alert(1)</script>",
    "view-source:https://example.com",
  ];
  for (const input of dangerous) {
    const out = normalizeOmniboxInput(input);
    assert.ok(out.startsWith("https://www.google.com/search?q="), `Blocked scheme leaked: ${out}`);
  }
});
