"use strict";

const DEFAULT_HOME_URL = "https://www.google.com/";
const SEARCH_ENDPOINT = "https://www.google.com/search?q=";

const BLOCKED_SCHEMES = new Set([
  "javascript:",
  "vbscript:",
  "file:",
  "chrome:",
  "chrome-extension:",
  "devtools:",
  "view-source:",
  "data:",
  "blob:",
]);

const IPV4_RE = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(:\d{1,5})?(\/.*)?$/;
const IPV6_RE = /^\[[0-9a-fA-F:]+\](:\d{1,5})?(\/.*)?$/;
const LOCALHOST_RE = /^localhost(:\d{1,5})?(\/.*)?$/i;
const HOST_LABEL_RE = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/i;
const TLD_RE = /^(?:[a-z]{2,63}|xn--[a-z0-9-]{2,59})$/i;

function isValidIpv4(match) {
  for (let i = 1; i <= 4; i += 1) {
    const octet = Number(match[i]);
    if (!Number.isInteger(octet) || octet < 0 || octet > 255) {
      return false;
    }
  }
  if (match[5]) {
    const port = Number(match[5].slice(1));
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      return false;
    }
  }
  return true;
}

function isLikelyDomain(candidate) {
  if (!candidate || /\s/.test(candidate)) {
    return false;
  }
  if (candidate.startsWith(".") || candidate.includes("..")) {
    return false;
  }
  let parsed;
  try {
    parsed = new URL(`https://${candidate}`);
  } catch {
    return false;
  }
  const hostname = parsed.hostname;
  if (!hostname || hostname.endsWith(".") || !hostname.includes(".")) {
    return false;
  }
  const labels = hostname.split(".");
  if (labels.length < 2) {
    return false;
  }
  const tld = labels[labels.length - 1];
  if (!TLD_RE.test(tld)) {
    return false;
  }
  return labels.every((label) => HOST_LABEL_RE.test(label));
}

function buildSearchUrl(query) {
  return `${SEARCH_ENDPOINT}${encodeURIComponent(query)}`;
}

/**
 * Normalize Omnibox input into a safe navigation URL.
 * - Valid http/https URLs navigate directly.
 * - Hostnames / IPs / localhost without scheme get https:// (or http:// for localhost/IP).
 * - Plain text queries search Google (never local 127.0.0.1/?q=).
 * - Dangerous/internal schemes (file:, chrome:, javascript:, etc.) fall back to Google search.
 */
function normalizeOmniboxInput(rawInput, defaultUrl = DEFAULT_HOME_URL) {
  const trimmed = typeof rawInput === "string" ? rawInput.trim() : "";
  if (!trimmed) {
    return defaultUrl;
  }

  if (trimmed.toLowerCase() === "about:blank") {
    return "about:blank";
  }

  // Check explicit scheme prefix (e.g. https://, http://, javascript:, file:)
  const schemeMatch = trimmed.match(/^([a-zA-Z][a-zA-Z0-9+.-]*):(.*)$/);
  if (schemeMatch) {
    const scheme = `${schemeMatch[1].toLowerCase()}:`;
    if (BLOCKED_SCHEMES.has(scheme)) {
      return buildSearchUrl(trimmed);
    }
    if (scheme === "http:" || scheme === "https:") {
      if (/\s/.test(trimmed)) {
        return buildSearchUrl(trimmed);
      }
      try {
        const parsed = new URL(trimmed);
        if (parsed.hostname) {
          return parsed.toString();
        }
      } catch {
        return buildSearchUrl(trimmed);
      }
      return buildSearchUrl(trimmed);
    }
    // Could be host:port without scheme (e.g. localhost:3000 or example.com:8080/foo)
    const rest = schemeMatch[2] || "";
    if (!rest.startsWith("//") && /^\d{1,5}(\/.*)?$/.test(rest)) {
      if (LOCALHOST_RE.test(trimmed)) {
        try {
          return new URL(`http://${trimmed}`).toString();
        } catch {
          return buildSearchUrl(trimmed);
        }
      }
      const ipv4 = trimmed.match(IPV4_RE);
      if (ipv4 && isValidIpv4(ipv4)) {
        try {
          return new URL(`http://${trimmed}`).toString();
        } catch {
          return buildSearchUrl(trimmed);
        }
      }
      if (isLikelyDomain(trimmed)) {
        try {
          return new URL(`https://${trimmed}`).toString();
        } catch {
          return buildSearchUrl(trimmed);
        }
      }
    }
    return buildSearchUrl(trimmed);
  }

  // Scheme-less input with whitespace is always a search query.
  if (/\s/.test(trimmed)) {
    return buildSearchUrl(trimmed);
  }

  if (LOCALHOST_RE.test(trimmed)) {
    try {
      return new URL(`http://${trimmed}`).toString();
    } catch {
      return buildSearchUrl(trimmed);
    }
  }

  const ipv4Match = trimmed.match(IPV4_RE);
  if (ipv4Match && isValidIpv4(ipv4Match)) {
    try {
      return new URL(`http://${trimmed}`).toString();
    } catch {
      return buildSearchUrl(trimmed);
    }
  }

  if (IPV6_RE.test(trimmed)) {
    try {
      return new URL(`http://${trimmed}`).toString();
    } catch {
      return buildSearchUrl(trimmed);
    }
  }

  if (isLikelyDomain(trimmed)) {
    try {
      return new URL(`https://${trimmed}`).toString();
    } catch {
      return buildSearchUrl(trimmed);
    }
  }

  return buildSearchUrl(trimmed);
}

module.exports = {
  DEFAULT_HOME_URL,
  SEARCH_ENDPOINT,
  normalizeOmniboxInput,
  buildSearchUrl,
};
