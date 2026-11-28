"use strict";

const { contextBridge, ipcRenderer } = require("electron");

const prismDesktop = Object.freeze({
  isDesktop: true,

  getEngineInfo() {
    return ipcRenderer.invoke("prism:get-engine-info");
  },

  openProfile(profileId, opts) {
    return ipcRenderer.invoke("prism:open-profile", profileId, opts || {});
  },

  closeProfile(profileId) {
    return ipcRenderer.invoke("prism:close-profile", profileId);
  },

  activateProfile(profileId) {
    return ipcRenderer.invoke("prism:activate-profile", profileId ?? null);
  },

  setViewportBounds(bounds) {
    return ipcRenderer.invoke("prism:set-viewport-bounds", bounds);
  },

  setViewportVisible(visible) {
    return ipcRenderer.invoke("prism:set-viewport-visible", Boolean(visible));
  },

  listWorkspaces() {
    return ipcRenderer.invoke("prism:list-workspaces");
  },

  createTab(profileId, url) {
    return ipcRenderer.invoke("prism:create-tab", profileId, url);
  },

  closeTab(profileId, tabId) {
    return ipcRenderer.invoke("prism:close-tab", profileId, tabId);
  },

  activateTab(profileId, tabId) {
    return ipcRenderer.invoke("prism:activate-tab", profileId, tabId);
  },

  navigate(profileId, tabId, input) {
    return ipcRenderer.invoke("prism:navigate", profileId, tabId, input);
  },

  goBack(profileId, tabId) {
    return ipcRenderer.invoke("prism:go-back", profileId, tabId);
  },

  goForward(profileId, tabId) {
    return ipcRenderer.invoke("prism:go-forward", profileId, tabId);
  },

  reload(profileId, tabId) {
    return ipcRenderer.invoke("prism:reload", profileId, tabId);
  },

  stop(profileId, tabId) {
    return ipcRenderer.invoke("prism:stop", profileId, tabId);
  },

  toggleDevTools(profileId, tabId) {
    return ipcRenderer.invoke("prism:toggle-devtools", profileId, tabId);
  },

  exportCookies(profileId) {
    return ipcRenderer.invoke("prism:export-cookies", profileId);
  },

  importCookies(profileId, cookies, mode) {
    return ipcRenderer.invoke("prism:import-cookies", profileId, cookies, mode || "merge");
  },

  clearProfileStorage(profileId) {
    return ipcRenderer.invoke("prism:clear-profile-storage", profileId);
  },

  onWorkspacesChanged(cb) {
    if (typeof cb !== "function") {
      return () => {};
    }
    const listener = (_event, workspaces) => {
      cb(workspaces);
    };
    ipcRenderer.on("prism:workspaces-changed", listener);
    return () => {
      ipcRenderer.removeListener("prism:workspaces-changed", listener);
    };
  },
});

contextBridge.exposeInMainWorld("prismDesktop", prismDesktop);
