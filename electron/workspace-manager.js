"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const { normalizeOmniboxInput } = require("./omnibox");
const { buildErrorPageDataUrl, buildNewTabDataUrl } = require("./error-page");
const {
  buildChromeIdentity,
  configureSessionIdentity,
  attachFingerprintToWebContents,
} = require("./fingerprint-cdp");
const {
  cookieToPortableItem,
  buildCookieUrl,
  portableItemToElectronSetDetails,
} = require("./cookies");

const PARTITION_PREFIX = "persist:prism_";

function partitionNameForProfile(profileId) {
  const safe = String(profileId || "").replace(/[^a-zA-Z0-9_-]/g, "_");
  if (!safe) {
    throw new Error("Invalid profileId");
  }
  return `${PARTITION_PREFIX}${safe}`;
}

function partitionDirNameForProfile(profileId) {
  const safe = String(profileId || "").replace(/[^a-zA-Z0-9_-]/g, "_");
  return `prism_${safe}`;
}

function canGoBack(wc) {
  if (wc.navigationHistory && typeof wc.navigationHistory.canGoBack === "function") {
    return wc.navigationHistory.canGoBack();
  }
  return typeof wc.canGoBack === "function" ? wc.canGoBack() : false;
}

function canGoForward(wc) {
  if (wc.navigationHistory && typeof wc.navigationHistory.canGoForward === "function") {
    return wc.navigationHistory.canGoForward();
  }
  return typeof wc.canGoForward === "function" ? wc.canGoForward() : false;
}

function navigateBack(wc) {
  if (wc.navigationHistory && typeof wc.navigationHistory.goBack === "function") {
    if (wc.navigationHistory.canGoBack()) {
      wc.navigationHistory.goBack();
    }
    return;
  }
  if (typeof wc.goBack === "function" && wc.canGoBack()) {
    wc.goBack();
  }
}

function navigateForward(wc) {
  if (wc.navigationHistory && typeof wc.navigationHistory.goForward === "function") {
    if (wc.navigationHistory.canGoForward()) {
      wc.navigationHistory.goForward();
    }
    return;
  }
  if (typeof wc.goForward === "function" && wc.canGoForward()) {
    wc.goForward();
  }
}

class WorkspaceManager {
  constructor({
    mainWindow,
    sidecar,
    userDataDir,
    sessionModule,
    WebContentsViewClass,
    chromiumVersion = process.versions.chrome,
    onWorkspacesChanged = null,
  }) {
    this.mainWindow = mainWindow;
    this.sidecar = sidecar;
    this.userDataDir = userDataDir;
    this.sessionModule = sessionModule;
    this.WebContentsViewClass = WebContentsViewClass;
    this.chromiumVersion = chromiumVersion;
    this.onWorkspacesChangedCallback = onWorkspacesChanged;

    this.workspaces = new Map(); // profileId -> workspace
    this.openingLocks = new Map(); // profileId -> Promise
    this.activeProfileId = null;
    this.viewportBounds = { x: 0, y: 0, width: 0, height: 0 };
    this.viewportVisible = true;
    this.attachedView = null;
  }

  serializeTab(tab) {
    return {
      id: tab.id,
      url: tab.displayUrl,
      title: tab.title,
      favicon: tab.favicon,
      loading: tab.loading,
      canGoBack: tab.canGoBack,
      canGoForward: tab.canGoForward,
    };
  }

  serializeWorkspace(ws) {
    return {
      profileId: ws.profileId,
      activeTabId: ws.activeTabId,
      exitIp: ws.exitIp,
      exitCountry: ws.exitCountry,
      exitCity: ws.exitCity,
      proxyPort: ws.proxyPort,
      tabs: Array.from(ws.tabs.values()).map((tab) => this.serializeTab(tab)),
    };
  }

  listWorkspaces() {
    return Array.from(this.workspaces.values()).map((ws) => this.serializeWorkspace(ws));
  }

  emitWorkspacesChanged() {
    const snapshot = this.listWorkspaces();
    if (typeof this.onWorkspacesChangedCallback === "function") {
      this.onWorkspacesChangedCallback(snapshot);
    }
    if (
      this.mainWindow &&
      !this.mainWindow.isDestroyed() &&
      this.mainWindow.webContents &&
      !this.mainWindow.webContents.isDestroyed()
    ) {
      this.mainWindow.webContents.send("prism:workspaces-changed", snapshot);
    }
  }

  syncActiveView() {
    if (!this.mainWindow || this.mainWindow.isDestroyed()) {
      return;
    }

    let targetView = null;
    if (
      this.viewportVisible &&
      this.activeProfileId &&
      this.viewportBounds.width > 0 &&
      this.viewportBounds.height > 0
    ) {
      const ws = this.workspaces.get(this.activeProfileId);
      const tab = ws && ws.activeTabId ? ws.tabs.get(ws.activeTabId) : null;
      if (tab && tab.view && tab.view.webContents && !tab.view.webContents.isDestroyed()) {
        targetView = tab.view;
      }
    }

    if (this.attachedView && this.attachedView !== targetView) {
      try {
        this.mainWindow.contentView.removeChildView(this.attachedView);
      } catch {
        // View may already be detached.
      }
      this.attachedView = null;
    }

    if (targetView) {
      if (this.attachedView !== targetView) {
        this.mainWindow.contentView.addChildView(targetView);
        this.attachedView = targetView;
      }
      targetView.setBounds({
        x: Math.max(0, Math.round(this.viewportBounds.x)),
        y: Math.max(0, Math.round(this.viewportBounds.y)),
        width: Math.max(0, Math.round(this.viewportBounds.width)),
        height: Math.max(0, Math.round(this.viewportBounds.height)),
      });
    }
  }

  async openProfile(profileId, opts = {}) {
    const id = String(profileId || "").trim();
    if (!id) {
      return {
        ok: false,
        status: 400,
        code: "invalid_profile_id",
        message: "无效的环境 ID",
        details: null,
      };
    }

    if (this.openingLocks.has(id)) {
      return this.openingLocks.get(id);
    }

    const run = this._openProfileInternal(id, opts || {});
    this.openingLocks.set(id, run);
    try {
      return await run;
    } finally {
      this.openingLocks.delete(id);
    }
  }

  async _openProfileInternal(profileId, opts) {
    const existing = this.workspaces.get(profileId);
    if (existing) {
      if (opts.initialUrl && String(opts.initialUrl).trim()) {
        const tabs = Array.from(existing.tabs.values());
        if (tabs.length === 1 && tabs[0].isNewTab) {
          await this.navigate(profileId, tabs[0].id, opts.initialUrl);
        } else {
          await this.createTab(profileId, opts.initialUrl);
        }
      }
      this.activeProfileId = profileId;
      this.syncActiveView();
      this.emitWorkspacesChanged();
      return { ok: true, workspace: this.serializeWorkspace(existing) };
    }

    const prepared = await this.sidecar.prepareProfileSession(profileId, {
      allowIpDrift: Boolean(opts.allowIpDrift),
    });
    if (!prepared.ok) {
      return prepared;
    }

    const data = prepared.data || {};
    const partition = partitionNameForProfile(profileId);
    const ses = this.sessionModule.fromPartition(partition);
    const proxy = data.proxy && typeof data.proxy === "object" ? data.proxy : null;

    if (proxy && proxy.port) {
      await ses.setProxy({
        proxyRules: `http://127.0.0.1:${Number(proxy.port)}`,
        proxyBypassRules: "<-loopback>",
      });
    } else {
      await ses.setProxy({ mode: "direct" });
    }

    const isolation = data.isolation && typeof data.isolation === "object" ? data.isolation : {};
    const identity = buildChromeIdentity(isolation, this.chromiumVersion);
    configureSessionIdentity(ses, identity);

    const workspace = {
      profileId,
      partition,
      session: ses,
      proxy,
      exitIp: data.exit_ip ?? null,
      exitCountry: data.exit_country ?? null,
      exitCity: data.exit_city ?? null,
      proxyPort: proxy && proxy.port ? Number(proxy.port) : null,
      isolation,
      identity,
      activeTabId: null,
      tabs: new Map(),
    };

    this.workspaces.set(profileId, workspace);

    try {
      await this._createTabInternal(workspace, opts.initialUrl);
    } catch (err) {
      this.workspaces.delete(profileId);
      await this.sidecar.releaseProfileSession(profileId).catch(() => {});
      return {
        ok: false,
        status: 500,
        code: "workspace_init_failed",
        message: err instanceof Error ? err.message : "初始化隔离窗口失败",
        details: null,
      };
    }

    this.activeProfileId = profileId;
    this.syncActiveView();
    this.emitWorkspacesChanged();
    return { ok: true, workspace: this.serializeWorkspace(workspace) };
  }

  async _createTabInternal(ws, rawUrl) {
    const tabId = `tab_${crypto.randomBytes(6).toString("hex")}`;
    const view = new this.WebContentsViewClass({
      webPreferences: {
        session: ws.session,
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
        spellcheck: false,
        // Strictly NO preload on guest views!
      },
    });
    const wc = view.webContents;

    const tab = {
      id: tabId,
      view,
      displayUrl: "",
      title: "新标签页",
      favicon: null,
      loading: false,
      canGoBack: false,
      canGoForward: false,
      isNewTab: true,
      isErrorPage: false,
      failedUrl: "",
    };

    // Strict per-profile localhost proxy credential binding ONLY
    wc.on("login", (event, _details, authInfo, callback) => {
      if (
        ws.proxy &&
        authInfo &&
        authInfo.isProxy &&
        authInfo.host === "127.0.0.1" &&
        Number(authInfo.port) === Number(ws.proxy.port)
      ) {
        event.preventDefault();
        callback(ws.proxy.username || "", ws.proxy.password || "");
      }
    });

    // Never allow guest pages to pop external OS windows; open links as tabs in same profile
    wc.setWindowOpenHandler(({ url }) => {
      if (typeof url === "string" && /^https?:\/\//i.test(url)) {
        this.createTab(ws.profileId, url).catch(() => {});
      }
      return { action: "deny" };
    });

    // Attach CDP + fingerprint spoofing BEFORE first loadURL
    await attachFingerprintToWebContents(wc, ws.identity);

    const syncHistory = () => {
      if (wc.isDestroyed()) return;
      tab.canGoBack = canGoBack(wc);
      tab.canGoForward = canGoForward(wc);
    };

    wc.on("did-start-loading", () => {
      tab.loading = true;
      this.emitWorkspacesChanged();
    });

    wc.on("did-stop-loading", () => {
      tab.loading = false;
      syncHistory();
      this.emitWorkspacesChanged();
    });

    const handleNavigate = (_event, url) => {
      const urlStr = String(url || "");
      if (urlStr.startsWith("data:text/html")) {
        if (tab.isErrorPage) {
          tab.displayUrl = tab.failedUrl || "";
        } else if (tab.isNewTab) {
          tab.displayUrl = "";
        }
      } else {
        tab.isNewTab = false;
        tab.isErrorPage = false;
        tab.failedUrl = "";
        tab.displayUrl = urlStr;
      }
      syncHistory();
      this.emitWorkspacesChanged();
    };

    wc.on("did-navigate", handleNavigate);
    wc.on("did-navigate-in-page", handleNavigate);

    wc.on("page-title-updated", (_event, title) => {
      if (!tab.isErrorPage) {
        tab.title = title || tab.displayUrl || "新标签页";
        this.emitWorkspacesChanged();
      }
    });

    wc.on("page-favicon-updated", (_event, favicons) => {
      if (Array.isArray(favicons) && favicons.length > 0) {
        tab.favicon = String(favicons[0] || "");
        this.emitWorkspacesChanged();
      }
    });

    wc.on("did-fail-load", (_event, errorCode, errorDescription, validatedURL, isMainFrame) => {
      if (!isMainFrame || errorCode === -3) {
        return;
      }
      const failed = String(validatedURL || "");
      if (failed.startsWith("data:")) {
        return;
      }
      tab.isNewTab = false;
      tab.isErrorPage = true;
      tab.failedUrl = failed;
      tab.displayUrl = failed;
      tab.title = "无法访问此页面";
      tab.loading = false;
      const errUrl = buildErrorPageDataUrl({
        errorCode,
        errorDescription,
        validatedUrl: failed,
      });
      wc.loadURL(errUrl).catch(() => {});
      this.emitWorkspacesChanged();
    });

    ws.tabs.set(tabId, tab);
    ws.activeTabId = tabId;

    const hasInitial = typeof rawUrl === "string" && rawUrl.trim().length > 0;
    if (hasInitial) {
      const targetUrl = normalizeOmniboxInput(rawUrl);
      tab.isNewTab = false;
      tab.displayUrl = targetUrl;
      tab.title = targetUrl;
      wc.loadURL(targetUrl).catch(() => {});
    } else {
      tab.isNewTab = true;
      tab.displayUrl = "";
      tab.title = "新标签页";
      const startUrl = buildNewTabDataUrl({
        exitIp: ws.exitIp,
        exitCountry: ws.exitCountry,
        exitCity: ws.exitCity,
        fingerprintHash: ws.identity.fingerprintHash,
      });
      wc.loadURL(startUrl).catch(() => {});
    }

    return tab;
  }

  _destroyTabView(tab) {
    if (!tab || !tab.view) {
      return;
    }
    if (this.attachedView === tab.view) {
      try {
        if (this.mainWindow && !this.mainWindow.isDestroyed()) {
          this.mainWindow.contentView.removeChildView(tab.view);
        }
      } catch {
        // ignore
      }
      this.attachedView = null;
    }
    const wc = tab.view.webContents;
    if (wc && !wc.isDestroyed()) {
      try {
        if (wc.debugger && wc.debugger.isAttached()) {
          wc.debugger.detach();
        }
      } catch {
        // ignore
      }
      try {
        wc.close({ waitForBeforeUnload: false });
      } catch {
        try {
          wc.destroy();
        } catch {
          // ignore
        }
      }
    }
  }

  async closeProfile(profileId) {
    const ws = this.workspaces.get(profileId);
    if (!ws) {
      await this.sidecar.releaseProfileSession(profileId).catch(() => {});
      return { ok: true };
    }

    this.workspaces.delete(profileId);
    if (this.activeProfileId === profileId) {
      this.activeProfileId = null;
    }

    for (const tab of ws.tabs.values()) {
      this._destroyTabView(tab);
    }
    ws.tabs.clear();

    this.syncActiveView();
    this.emitWorkspacesChanged();

    await this.sidecar.releaseProfileSession(profileId).catch(() => {});
    return { ok: true };
  }

  activateProfile(profileId) {
    if (!profileId) {
      this.activeProfileId = null;
      this.syncActiveView();
      return;
    }
    if (this.workspaces.has(profileId)) {
      this.activeProfileId = profileId;
      this.syncActiveView();
    }
  }

  setViewportBounds(bounds) {
    if (!bounds || typeof bounds !== "object") {
      return;
    }
    const x = Number(bounds.x);
    const y = Number(bounds.y);
    const width = Number(bounds.width);
    const height = Number(bounds.height);
    if (![x, y, width, height].every(Number.isFinite)) {
      return;
    }
    this.viewportBounds = {
      x: Math.max(0, x),
      y: Math.max(0, y),
      width: Math.max(0, width),
      height: Math.max(0, height),
    };
    this.syncActiveView();
  }

  setViewportVisible(visible) {
    this.viewportVisible = Boolean(visible);
    this.syncActiveView();
  }

  async createTab(profileId, url) {
    const ws = this.workspaces.get(profileId);
    if (!ws) {
      throw new Error(`Workspace ${profileId} is not open`);
    }
    const tab = await this._createTabInternal(ws, url);
    if (this.activeProfileId === profileId) {
      this.syncActiveView();
    }
    this.emitWorkspacesChanged();
    return this.serializeTab(tab);
  }

  async closeTab(profileId, tabId) {
    const ws = this.workspaces.get(profileId);
    if (!ws) {
      return;
    }
    const tab = ws.tabs.get(tabId);
    if (!tab) {
      return;
    }

    const keys = Array.from(ws.tabs.keys());
    const idx = keys.indexOf(tabId);
    ws.tabs.delete(tabId);
    this._destroyTabView(tab);

    if (ws.tabs.size === 0) {
      await this.closeProfile(profileId);
      return;
    }

    if (ws.activeTabId === tabId) {
      const remaining = Array.from(ws.tabs.keys());
      const nextIdx = Math.min(idx, remaining.length - 1);
      ws.activeTabId = remaining[Math.max(0, nextIdx)];
    }

    this.syncActiveView();
    this.emitWorkspacesChanged();
  }

  activateTab(profileId, tabId) {
    const ws = this.workspaces.get(profileId);
    if (!ws || !ws.tabs.has(tabId)) {
      return;
    }
    ws.activeTabId = tabId;
    if (this.activeProfileId === profileId) {
      this.syncActiveView();
    }
    this.emitWorkspacesChanged();
  }

  _resolveTab(profileId, tabId) {
    const ws = this.workspaces.get(profileId);
    if (!ws) {
      return null;
    }
    const targetId = tabId || ws.activeTabId;
    const tab = targetId ? ws.tabs.get(targetId) : null;
    if (!tab || !tab.view || !tab.view.webContents || tab.view.webContents.isDestroyed()) {
      return null;
    }
    return { ws, tab, wc: tab.view.webContents };
  }

  async navigate(profileId, tabId, input) {
    const resolved = this._resolveTab(profileId, tabId);
    if (!resolved) {
      return;
    }
    const { tab, wc } = resolved;
    const targetUrl = normalizeOmniboxInput(input);
    tab.isNewTab = false;
    tab.isErrorPage = false;
    tab.failedUrl = "";
    tab.displayUrl = targetUrl;
    tab.favicon = null;
    this.emitWorkspacesChanged();
    wc.loadURL(targetUrl).catch(() => {});
  }

  goBack(profileId, tabId) {
    const resolved = this._resolveTab(profileId, tabId);
    if (resolved) {
      navigateBack(resolved.wc);
    }
  }

  goForward(profileId, tabId) {
    const resolved = this._resolveTab(profileId, tabId);
    if (resolved) {
      navigateForward(resolved.wc);
    }
  }

  reload(profileId, tabId) {
    const resolved = this._resolveTab(profileId, tabId);
    if (!resolved) {
      return;
    }
    const { tab, wc } = resolved;
    if (tab.isErrorPage && tab.failedUrl) {
      tab.isErrorPage = false;
      wc.loadURL(tab.failedUrl).catch(() => {});
      return;
    }
    wc.reload();
  }

  stop(profileId, tabId) {
    const resolved = this._resolveTab(profileId, tabId);
    if (resolved) {
      resolved.wc.stop();
    }
  }

  async exportCookies(profileId) {
    const ses = this.sessionModule.fromPartition(partitionNameForProfile(profileId));
    const raw = await ses.cookies.get({});
    return raw.map(cookieToPortableItem);
  }

  async importCookies(profileId, cookies, mode = "merge") {
    const ses = this.sessionModule.fromPartition(partitionNameForProfile(profileId));
    const list = Array.isArray(cookies) ? cookies : [];

    if (mode === "replace") {
      const existing = await ses.cookies.get({});
      for (const c of existing) {
        try {
          await ses.cookies.remove(buildCookieUrl(c), c.name);
        } catch {
          // ignore individual removal failure
        }
      }
    }

    let imported = 0;
    let skipped = 0;
    for (const item of list) {
      try {
        const details = portableItemToElectronSetDetails(item);
        await ses.cookies.set(details);
        imported += 1;
      } catch {
        skipped += 1;
      }
    }

    if (typeof ses.cookies.flushStore === "function") {
      await ses.cookies.flushStore().catch(() => {});
    }
    return { imported, skipped };
  }

  async clearProfileStorage(profileId) {
    if (this.workspaces.has(profileId)) {
      await this.closeProfile(profileId);
    }
    const ses = this.sessionModule.fromPartition(partitionNameForProfile(profileId));
    await ses.clearStorageData().catch(() => {});
    await ses.clearCache().catch(() => {});
    if (typeof ses.clearAuthCache === "function") {
      await ses.clearAuthCache().catch(() => {});
    }

    if (this.userDataDir) {
      const partitionDir = path.join(
        this.userDataDir,
        "Partitions",
        partitionDirNameForProfile(profileId)
      );
      try {
        await fs.promises.rm(partitionDir, { recursive: true, force: true });
      } catch {
        // Best-effort cleanup if Chromium still holds handles until exit.
      }
    }
  }

  async closeAll() {
    const ids = Array.from(this.workspaces.keys());
    await Promise.allSettled(ids.map((id) => this.closeProfile(id)));
  }
}

module.exports = {
  WorkspaceManager,
  partitionNameForProfile,
  partitionDirNameForProfile,
};
