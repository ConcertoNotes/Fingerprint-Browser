// Typed IPC bridge connecting the Prism Browser UI to the native desktop host (window.prismDesktop).

export interface DesktopEngineInfo {
  engine: 'chromium'
  chromiumVersion: string
  electronVersion: string
  platform: string
  arch: string
}

export interface TabState {
  id: string
  url: string
  title: string
  favicon: string | null
  loading: boolean
  canGoBack: boolean
  canGoForward: boolean
}

export interface ProfileWorkspaceState {
  profileId: string
  activeTabId: string | null
  exitIp: string | null
  exitCountry: string | null
  exitCity: string | null
  proxyPort: number | null
  tabs: TabState[]
}

export interface DesktopCookieItem {
  name: string
  value: string
  domain?: string
  path?: string
  secure?: boolean
  httpOnly?: boolean
  sameSite?: string
  expirationDate?: number
  session?: boolean
  url?: string
}

export type OpenProfileResult =
  | { ok: true; workspace: ProfileWorkspaceState }
  | { ok: false; status?: number; code?: string; message?: string; details?: unknown }

export interface PrismDesktopBridge {
  isDesktop: true
  getEngineInfo(): Promise<DesktopEngineInfo>
  openProfile(
    profileId: string,
    opts?: { allowIpDrift?: boolean; initialUrl?: string },
  ): Promise<OpenProfileResult>
  closeProfile(profileId: string): Promise<{ ok: boolean }>
  activateProfile(profileId: string | null): Promise<void>
  setViewportBounds(bounds: { x: number; y: number; width: number; height: number }): Promise<void>
  setViewportVisible(visible: boolean): Promise<void>
  listWorkspaces(): Promise<ProfileWorkspaceState[]>
  createTab(profileId: string, url?: string): Promise<TabState>
  closeTab(profileId: string, tabId: string): Promise<void>
  activateTab(profileId: string, tabId: string): Promise<void>
  navigate(profileId: string, tabId: string, input: string): Promise<void>
  goBack(profileId: string, tabId: string): Promise<void>
  goForward(profileId: string, tabId: string): Promise<void>
  reload(profileId: string, tabId: string): Promise<void>
  stop(profileId: string, tabId: string): Promise<void>
  exportCookies(profileId: string): Promise<DesktopCookieItem[]>
  importCookies(
    profileId: string,
    cookies: DesktopCookieItem[],
    mode: 'merge' | 'replace',
  ): Promise<{ imported: number; skipped: number }>
  clearProfileStorage(profileId: string): Promise<void>
  onWorkspacesChanged(cb: (workspaces: ProfileWorkspaceState[]) => void): () => void
}

declare global {
  interface Window {
    prismDesktop?: PrismDesktopBridge
  }
}

export function getDesktopBridge(): PrismDesktopBridge | null {
  if (typeof window === 'undefined') return null
  const bridge = window.prismDesktop
  return bridge && bridge.isDesktop ? bridge : null
}

// --- Ref-counted native viewport occlusion for modals & overlays --------------

let overlayCount = 0
const overlayListeners = new Set<(occluded: boolean) => void>()

export function isViewportOccluded(): boolean {
  return overlayCount > 0
}

export function subscribeViewportOcclusion(cb: (occluded: boolean) => void): () => void {
  overlayListeners.add(cb)
  return () => { overlayListeners.delete(cb) }
}

/**
 * Hides the native embedded browser viewport while any HTML modal/dialog is open,
 * restoring it cleanly once all overlays have closed.
 */
export function acquireViewportOverlay(): () => void {
  let released = false
  overlayCount += 1
  if (overlayCount === 1) {
    void getDesktopBridge()?.setViewportVisible(false).catch(() => {})
    overlayListeners.forEach(cb => cb(true))
  }
  return () => {
    if (released) return
    released = true
    overlayCount = Math.max(0, overlayCount - 1)
    if (overlayCount === 0) {
      void getDesktopBridge()?.setViewportVisible(true).catch(() => {})
      overlayListeners.forEach(cb => cb(false))
    }
  }
}

// --- Client-side cookie formatting/parsing for native desktop IPC -------------

export function cookiesToNetscape(cookies: DesktopCookieItem[]): string {
  const lines = [
    '# Netscape HTTP Cookie File',
    '# Exported by Prism Browser',
    '',
  ]
  for (const cookie of cookies) {
    const domain = cookie.domain ?? ''
    if (!domain || !cookie.name) continue
    const includeSubdomains = domain.startsWith('.') ? 'TRUE' : 'FALSE'
    const path = cookie.path || '/'
    const secure = cookie.secure ? 'TRUE' : 'FALSE'
    const expiry = cookie.expirationDate && Number.isFinite(cookie.expirationDate)
      ? Math.max(0, Math.floor(cookie.expirationDate))
      : 0
    lines.push([domain, includeSubdomains, path, secure, String(expiry), cookie.name, cookie.value ?? ''].join('\t'))
  }
  return lines.join('\n') + '\n'
}

export interface ParsedCookiesPreview {
  cookies: DesktopCookieItem[]
  total: number
  sessionCount: number
  skipped: { row: number; reason: string }[]
}

export function parseDesktopCookies(text: string, format: 'json' | 'netscape'): ParsedCookiesPreview {
  const cookies: DesktopCookieItem[] = []
  const skipped: { row: number; reason: string }[] = []
  let sessionCount = 0

  if (format === 'json') {
    let parsed: unknown
    try {
      parsed = JSON.parse(text)
    } catch {
      throw new Error('Invalid JSON cookie payload')
    }
    const list = Array.isArray(parsed)
      ? parsed
      : (parsed && typeof parsed === 'object' && Array.isArray((parsed as { cookies?: unknown }).cookies))
        ? (parsed as { cookies: unknown[] }).cookies
        : null
    if (!list) throw new Error('Cookie JSON must be an array of cookie objects')

    list.forEach((raw, idx) => {
      if (!raw || typeof raw !== 'object') {
        skipped.push({ row: idx + 1, reason: 'Row is not a cookie object' })
        return
      }
      const item = raw as Record<string, unknown>
      const name = typeof item.name === 'string' ? item.name.trim() : ''
      const value = typeof item.value === 'string' ? item.value : String(item.value ?? '')
      const domain = typeof item.domain === 'string' ? item.domain.trim() : ''
      if (!name || !domain) {
        skipped.push({ row: idx + 1, reason: 'Missing required cookie name or domain' })
        return
      }
      const rawExpiry = item.expirationDate ?? item.expires ?? item.expiry
      const exp = typeof rawExpiry === 'number' && Number.isFinite(rawExpiry) && rawExpiry > 0
        ? rawExpiry
        : undefined
      const isSession = Boolean(item.session) || exp === undefined
      if (isSession) sessionCount += 1
      cookies.push({
        name,
        value,
        domain,
        path: typeof item.path === 'string' && item.path ? item.path : '/',
        secure: Boolean(item.secure),
        httpOnly: Boolean(item.httpOnly),
        sameSite: typeof item.sameSite === 'string' ? item.sameSite : undefined,
        expirationDate: exp,
        session: isSession,
      })
    })
    return { cookies, total: list.length, sessionCount, skipped }
  }

  // Netscape format
  const lines = text.split(/\r?\n/)
  let total = 0
  lines.forEach((rawLine, idx) => {
    const trimmed = rawLine.trim()
    if (!trimmed) return
    const isHttpOnlyComment = trimmed.startsWith('#HttpOnly_')
    if (trimmed.startsWith('#') && !isHttpOnlyComment) return
    total += 1
    const clean = isHttpOnlyComment ? trimmed.slice('#HttpOnly_'.length) : trimmed
    const parts = clean.split('\t')
    if (parts.length < 7) {
      skipped.push({ row: idx + 1, reason: 'Expected 7 tab-separated columns' })
      return
    }
    const [domain, , path, secureRaw, expiryRaw, name, ...rest] = parts
    const value = rest.join('\t')
    if (!domain.trim() || !name.trim()) {
      skipped.push({ row: idx + 1, reason: 'Missing required cookie name or domain' })
      return
    }
    const expNum = Number(expiryRaw)
    const exp = Number.isFinite(expNum) && expNum > 0 ? expNum : undefined
    if (exp === undefined) sessionCount += 1
    cookies.push({
      name: name.trim(),
      value,
      domain: domain.trim(),
      path: path || '/',
      secure: secureRaw.toUpperCase() === 'TRUE',
      httpOnly: isHttpOnlyComment,
      expirationDate: exp,
      session: exp === undefined,
    })
  })
  return { cookies, total, sessionCount, skipped }
}
