"use strict";

function mapElectronSameSiteOut(sameSite) {
  const lower = String(sameSite || "lax").toLowerCase();
  if (lower === "strict") {
    return "Strict";
  }
  if (lower === "no_restriction" || lower === "none") {
    return "None";
  }
  return "Lax";
}

function mapSameSiteToElectron(sameSite) {
  const lower = String(sameSite || "lax").toLowerCase();
  if (lower === "strict") {
    return "strict";
  }
  if (lower === "none" || lower === "no_restriction") {
    return "no_restriction";
  }
  return "lax";
}

function cookieToPortableItem(cookie) {
  const domain = String(cookie.domain || "");
  const hostOnly = Boolean(cookie.hostOnly ?? !domain.startsWith("."));
  const isSession = Boolean(cookie.session || cookie.expirationDate == null);
  const item = {
    name: String(cookie.name || ""),
    value: String(cookie.value ?? ""),
    domain,
    path: String(cookie.path || "/"),
    expires: isSession ? -1 : Number(cookie.expirationDate),
    httpOnly: Boolean(cookie.httpOnly),
    secure: Boolean(cookie.secure),
    sameSite: mapElectronSameSiteOut(cookie.sameSite),
    hostOnly,
    session: isSession,
  };
  if (!isSession) {
    item.expirationDate = Number(cookie.expirationDate);
  }
  return item;
}

function buildCookieUrl(cookie) {
  const rawDomain = String(cookie.domain || "").trim();
  const host = rawDomain.replace(/^\.+/, "");
  if (!host) {
    throw new Error("Cookie domain is required");
  }
  const scheme = cookie.secure ? "https" : "http";
  const cookiePath =
    typeof cookie.path === "string" && cookie.path.startsWith("/") ? cookie.path : "/";
  return `${scheme}://${host}${cookiePath}`;
}

function portableItemToElectronSetDetails(item) {
  if (!item || typeof item !== "object") {
    throw new Error("Cookie item must be an object");
  }
  const name = typeof item.name === "string" ? item.name.trim() : "";
  if (!name) {
    throw new Error("Cookie name is required");
  }
  const value = typeof item.value === "string" ? item.value : String(item.value ?? "");
  const rawDomain = typeof item.domain === "string" ? item.domain.trim() : "";
  const secure = Boolean(item.secure);
  const cookiePath =
    typeof item.path === "string" && item.path.startsWith("/") ? item.path : "/";

  let url;
  if (typeof item.url === "string" && /^https?:\/\//i.test(item.url)) {
    url = item.url;
  } else {
    url = buildCookieUrl({ domain: rawDomain, secure, path: cookiePath });
  }

  const hostOnly =
    typeof item.hostOnly === "boolean" ? item.hostOnly : !rawDomain.startsWith(".");

  const details = {
    url,
    name,
    value,
    path: cookiePath,
    secure,
    httpOnly: Boolean(item.httpOnly),
    sameSite: mapSameSiteToElectron(item.sameSite),
  };

  // In Electron cookies.set(), passing `domain` creates a domain cookie;
  // omitting `domain` creates a host-only cookie (required for __Host-* cookies).
  if (!hostOnly && rawDomain) {
    details.domain = rawDomain.startsWith(".") ? rawDomain : `.${rawDomain}`;
  }

  const rawExpiry =
    item.session === true
      ? -1
      : item.expires !== undefined
        ? item.expires
        : item.expirationDate !== undefined
          ? item.expirationDate
          : -1;
  if (typeof rawExpiry === "number" && Number.isFinite(rawExpiry) && rawExpiry > 0) {
    details.expirationDate = rawExpiry;
  }

  return details;
}

module.exports = {
  mapElectronSameSiteOut,
  mapSameSiteToElectron,
  cookieToPortableItem,
  buildCookieUrl,
  portableItemToElectronSetDetails,
};
