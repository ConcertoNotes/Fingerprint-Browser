"use strict";

const path = require("node:path");
const {
  app,
  BrowserWindow,
  WebContentsView,
  ipcMain,
  Menu,
  session,
  dialog,
  shell,
  safeStorage,
} = require("electron");
const pkg = require("./package.json");
const {
  SidecarManager,
  loadDistributionConfig,
  isCustomerDevToolsShortcut,
} = require("./sidecar");
const { WorkspaceManager } = require("./workspace-manager");
const { buildStartupSplashDataUrl } = require("./error-page");

// CLI fast-path for --version (useful for packaging verification)
if (process.argv.includes("--version")) {
  process.stdout.write(`Prism Browser ${pkg.version}\n`);
  process.exit(0);
}

const distribution = loadDistributionConfig({
  isPackaged: app.isPackaged,
  resourcesPath: process.resourcesPath,
  repoRoot: path.resolve(__dirname, ".."),
});
const brandName = distribution.brand_name || "棱镜浏览器";
const isCustomerPackaged = distribution.edition === "customer" && app.isPackaged;

app.setName(brandName);

if (process.env.PRISM_USER_DATA_DIR) {
  app.setPath("userData", path.resolve(process.env.PRISM_USER_DATA_DIR));
}

// =============================================================================
// CRITICAL PROCESS-LEVEL ANTI-LEAK SWITCHES (MUST BE APPLIED BEFORE whenReady)
// =============================================================================
// 1. Disable QUIC/HTTP3 UDP transport so traffic never bypasses HTTP proxy tunnels.
app.commandLine.appendSwitch("disable-quic");
// 2. Force WebRTC to never emit non-proxied UDP / local host ICE candidates.
app.commandLine.appendSwitch("force-webrtc-ip-handling-policy", "disable_non_proxied_udp");
// 3. Disable Chromium built-in AsyncDns, DoH, and mDNS local IP probing.
app.commandLine.appendSwitch(
  "disable-features",
  "AsyncDns,DnsOverHttps,WebRtcHideLocalIpsWithMdns"
);
// 4. Fail-closed DNS: block all non-loopback local OS DNS lookups so 100% of
//    remote hostnames resolve remotely through each profile's Mihomo proxy tunnel.
app.commandLine.appendSwitch("host-resolver-rules", "MAP * ~NOTFOUND , EXCLUDE 127.0.0.1");

let mainWindow = null;
let sidecar = null;
let workspaceManager = null;
let trustedShellOrigin = null;
let isShuttingDown = false;

function isTrustedShellSender(event) {
  if (!mainWindow || mainWindow.isDestroyed()) {
    return false;
  }
  if (!event || event.sender !== mainWindow.webContents) {
    return false;
  }
  if (!trustedShellOrigin) {
    return false;
  }
  const frameUrl = event.senderFrame && event.senderFrame.url ? event.senderFrame.url : "";
  try {
    return new URL(frameUrl).origin === trustedShellOrigin;
  } catch {
    return false;
  }
}

function handleTrusted(channel, handler) {
  ipcMain.handle(channel, async (event, ...args) => {
    if (!isTrustedShellSender(event)) {
      throw new Error("Unauthorized IPC sender");
    }
    return handler(...args);
  });
}

function registerIpcHandlers() {
  handleTrusted("prism:get-engine-info", () => ({
    engine: "chromium",
    chromiumVersion: process.versions.chrome || "",
    electronVersion: process.versions.electron || "",
    platform: process.platform,
    arch: process.arch,
  }));

  handleTrusted("prism:open-profile", (profileId, opts) =>
    workspaceManager.openProfile(profileId, opts)
  );
  handleTrusted("prism:close-profile", (profileId) => workspaceManager.closeProfile(profileId));
  handleTrusted("prism:activate-profile", (profileId) => {
    workspaceManager.activateProfile(profileId);
  });
  handleTrusted("prism:set-viewport-bounds", (bounds) => {
    workspaceManager.setViewportBounds(bounds);
  });
  handleTrusted("prism:set-viewport-visible", (visible) => {
    workspaceManager.setViewportVisible(visible);
  });
  handleTrusted("prism:list-workspaces", () => workspaceManager.listWorkspaces());

  handleTrusted("prism:create-tab", (profileId, url) => workspaceManager.createTab(profileId, url));
  handleTrusted("prism:close-tab", (profileId, tabId) => workspaceManager.closeTab(profileId, tabId));
  handleTrusted("prism:activate-tab", (profileId, tabId) => {
    workspaceManager.activateTab(profileId, tabId);
  });
  handleTrusted("prism:navigate", (profileId, tabId, input) =>
    workspaceManager.navigate(profileId, tabId, input)
  );
  handleTrusted("prism:go-back", (profileId, tabId) => {
    workspaceManager.goBack(profileId, tabId);
  });
  handleTrusted("prism:go-forward", (profileId, tabId) => {
    workspaceManager.goForward(profileId, tabId);
  });
  handleTrusted("prism:reload", (profileId, tabId) => {
    workspaceManager.reload(profileId, tabId);
  });
  handleTrusted("prism:stop", (profileId, tabId) => {
    workspaceManager.stop(profileId, tabId);
  });

  handleTrusted("prism:export-cookies", (profileId) => workspaceManager.exportCookies(profileId));
  handleTrusted("prism:import-cookies", (profileId, cookies, mode) =>
    workspaceManager.importCookies(profileId, cookies, mode)
  );
  handleTrusted("prism:clear-profile-storage", (profileId) =>
    workspaceManager.clearProfileStorage(profileId)
  );
}

function buildAppMenu() {
  const isMac = process.platform === "darwin";
  const template = [
    ...(isMac
      ? [
          {
            label: brandName,
            submenu: [
              { role: "about", label: `关于${brandName}` },
              { type: "separator" },
              { role: "services", label: "服务" },
              { type: "separator" },
              { role: "hide", label: `隐藏${brandName}` },
              { role: "hideOthers", label: "隐藏其他" },
              { role: "unhide", label: "全部显示" },
              { type: "separator" },
              { role: "quit", label: `退出${brandName}` },
            ],
          },
        ]
      : []),
    {
      label: "编辑",
      submenu: [
        { role: "undo", label: "撤销" },
        { role: "redo", label: "重做" },
        { type: "separator" },
        { role: "cut", label: "剪切" },
        { role: "copy", label: "拷贝" },
        { role: "paste", label: "粘贴" },
        { role: "selectAll", label: "全选" },
      ],
    },
    {
      label: "视图",
      submenu: [
        ...(!app.isPackaged ? [{ role: "toggleDevTools", label: "开发者工具" }] : []),
        { role: "resetZoom", label: "实际大小" },
        { role: "zoomIn", label: "放大" },
        { role: "zoomOut", label: "缩小" },
        { type: "separator" },
        { role: "togglefullscreen", label: "切换全屏" },
      ],
    },
    {
      label: "窗口",
      submenu: [
        { role: "minimize", label: "最小化" },
        { role: "zoom", label: "缩放" },
        ...(isMac ? [{ type: "separator" }, { role: "front", label: "前置全部窗口" }] : []),
      ],
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

function createMainWindow() {
  const isMac = process.platform === "darwin";
  const win = new BrowserWindow({
    width: 1440,
    height: 920,
    minWidth: 1024,
    minHeight: 680,
    title: brandName,
    backgroundColor: "#090B10",
    ...(isMac
      ? {
          titleBarStyle: "hiddenInset",
          trafficLightPosition: { x: 16, y: 16 },
        }
      : {}),
    show: false,
    webPreferences: {
      preload: path.join(__dirname, "preload-shell.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      spellcheck: false,
      ...(isCustomerPackaged ? { devTools: false } : {}),
    },
  });

  if (isCustomerPackaged) {
    win.webContents.on("devtools-opened", () => {
      win.webContents.closeDevTools();
    });
    win.webContents.on("before-input-event", (event, input) => {
      if (isCustomerDevToolsShortcut(input)) {
        event.preventDefault();
      }
    });
  }

  // Never allow the main shell to navigate away to untrusted origins or pop privileged windows.
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (typeof url === "string" && /^https?:\/\//i.test(url)) {
      shell.openExternal(url).catch(() => {});
    }
    return { action: "deny" };
  });

  win.webContents.on("will-navigate", (event, targetUrl) => {
    if (!trustedShellOrigin) {
      return;
    }
    try {
      if (new URL(targetUrl).origin !== trustedShellOrigin) {
        event.preventDefault();
      }
    } catch {
      event.preventDefault();
    }
  });

  win.once("ready-to-show", () => {
    win.show();
  });

  win.on("closed", () => {
    mainWindow = null;
  });

  win.loadURL(buildStartupSplashDataUrl()).catch(() => {});
  return win;
}

async function shutdownGracefully() {
  if (isShuttingDown) {
    return;
  }
  isShuttingDown = true;
  try {
    if (workspaceManager) {
      await workspaceManager.closeAll();
    }
  } catch {
    // ignore
  }
  try {
    if (sidecar) {
      await sidecar.stop();
    }
  } catch {
    // ignore
  }
}

const singleLock = app.requestSingleInstanceLock();
if (!singleLock) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (mainWindow && !mainWindow.isDestroyed()) {
      if (mainWindow.isMinimized()) {
        mainWindow.restore();
      }
      mainWindow.focus();
    }
  });

  app.whenReady().then(async () => {
    buildAppMenu();
    mainWindow = createMainWindow();

    const userDataDir = app.getPath("userData");
    sidecar = new SidecarManager({
      isPackaged: app.isPackaged,
      resourcesPath: process.resourcesPath,
      userDataDir,
      repoRoot: path.resolve(__dirname, ".."),
      safeStorage,
      distribution,
    });

    workspaceManager = new WorkspaceManager({
      mainWindow,
      sidecar,
      userDataDir,
      sessionModule: session,
      WebContentsViewClass: WebContentsView,
      chromiumVersion: process.versions.chrome,
    });

    registerIpcHandlers();

    try {
      const baseUrl = await sidecar.start();
      trustedShellOrigin = new URL(baseUrl).origin;
      if (mainWindow && !mainWindow.isDestroyed()) {
        await mainWindow.loadURL(`${baseUrl}/`);
      }
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      dialog.showErrorBox(`${brandName}启动失败`, `本地隔离核心服务启动异常：\n\n${message}`);
      await shutdownGracefully();
      app.quit();
    }
  });

  app.on("before-quit", (event) => {
    if (!isShuttingDown) {
      event.preventDefault();
      shutdownGracefully().finally(() => {
        app.quit();
      });
    }
  });

  app.on("window-all-closed", () => {
    app.quit();
  });
}
