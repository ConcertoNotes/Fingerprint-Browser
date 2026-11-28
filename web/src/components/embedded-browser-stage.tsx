'use client'

import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import {
  ArrowLeft,
  ArrowRight,
  Check,
  Code2,
  Cookie,
  Copy,
  Globe,
  LayoutGrid,
  LoaderCircle,
  Lock,
  Plus,
  Power,
  RotateCw,
  ShieldCheck,
  Terminal,
  X,
} from 'lucide-react'

import { CookieTransfer } from '@/components/cookie-transfer'
import { useI18n } from '@/components/i18n-provider'
import { IsolationAuditModal } from '@/components/isolation-audit-modal'
import { useToast } from '@/components/toast'
import { useWorkspace } from '@/components/workspace-provider'
import { profileFingerprintHash, profilesAPI, type Profile } from '@/lib/api'
import {
  getDesktopBridge,
  isViewportOccluded,
  subscribeViewportOcclusion,
  type ProfileWorkspaceState,
  type TabState,
} from '@/lib/desktop-bridge'
import { networkAPI } from '@/lib/network-api'
import { flag, maskIp } from '@/lib/network-map'

const getOcclusionServer = () => false

/** Streamlined 2-tier single-window embedded browser stage (40px Context+Tab strip + 42px Nav/Omnibox). */
export function EmbeddedBrowserStage({ workspace }: { workspace: ProfileWorkspaceState }) {
  const { t } = useI18n()
  const toast = useToast()
  const { profileDirectory, activateWorkspace, closeProfileWorkspace, refreshProfileDirectory } = useWorkspace()

  const [fetchedProfile, setFetchedProfile] = useState<Profile | null>(null)
  const [maskIps, setMaskIps] = useState(false)
  const [copiedIp, setCopiedIp] = useState(false)
  const [openingTerminal, setOpeningTerminal] = useState(false)
  const [closingWindow, setClosingWindow] = useState(false)
  const [auditOpen, setAuditOpen] = useState(false)
  const [cookiesOpen, setCookiesOpen] = useState(false)
  const [brokenFavicons, setBrokenFavicons] = useState<Set<string>>(() => new Set())

  const [editingUrl, setEditingUrl] = useState(false)
  const [draftUrl, setDraftUrl] = useState('')
  const omniboxRef = useRef<HTMLInputElement>(null)
  const viewportHostRef = useRef<HTMLDivElement>(null)
  const lastBoundsRef = useRef<string>('')

  const occluded = useSyncExternalStore(subscribeViewportOcclusion, isViewportOccluded, getOcclusionServer)

  const cachedProfile = profileDirectory[workspace.profileId] ?? null
  const profile = cachedProfile ?? (fetchedProfile?.id === workspace.profileId ? fetchedProfile : null)

  useEffect(() => {
    if (cachedProfile) return
    let active = true
    profilesAPI
      .getProfile(workspace.profileId)
      .then(data => { if (active) setFetchedProfile(data) })
      .catch(() => {})
    return () => { active = false }
  }, [workspace.profileId, cachedProfile])

  useEffect(() => {
    let active = true
    networkAPI
      .status()
      .then(status => { if (active) setMaskIps(status.settings.mask_exit_ips) })
      .catch(() => {})
    return () => { active = false }
  }, [])

  const activeTab: TabState | null = useMemo(() => {
    if (!workspace.tabs.length) return null
    return workspace.tabs.find(tab => tab.id === workspace.activeTabId) ?? workspace.tabs[0]
  }, [workspace.tabs, workspace.activeTabId])

  const syncBounds = useCallback((force = false) => {
    const bridge = getDesktopBridge()
    const host = viewportHostRef.current
    if (!bridge || !host) return
    const rect = host.getBoundingClientRect()
    const bounds = {
      x: Math.max(0, Math.round(rect.left)),
      y: Math.max(0, Math.round(rect.top)),
      width: Math.max(0, Math.round(rect.width)),
      height: Math.max(0, Math.round(rect.height)),
    }
    if (bounds.width <= 0 || bounds.height <= 0) return
    const key = `${bounds.x}:${bounds.y}:${bounds.width}:${bounds.height}`
    if (!force && key === lastBoundsRef.current) return
    lastBoundsRef.current = key
    void bridge.setViewportBounds(bounds).catch(() => {})
  }, [])

  useEffect(() => {
    const host = viewportHostRef.current
    if (!host) return

    syncBounds(true)
    const raf = requestAnimationFrame(() => syncBounds(true))

    const observer = new ResizeObserver(() => syncBounds(false))
    observer.observe(host)

    const onResize = () => syncBounds(false)
    window.addEventListener('resize', onResize)

    return () => {
      cancelAnimationFrame(raf)
      observer.disconnect()
      window.removeEventListener('resize', onResize)
    }
  }, [syncBounds, workspace.profileId])

  useEffect(() => {
    if (occluded) return
    const bridge = getDesktopBridge()
    if (!bridge) return
    syncBounds(true)
    void bridge.setViewportVisible(true).catch(() => {})
  }, [occluded, syncBounds, workspace.profileId])

  const fpHash = profile ? profileFingerprintHash(profile) : null
  const exitIp = workspace.exitIp ?? null
  const displayIp = maskIp(exitIp, maskIps)
  const displayUrl = editingUrl ? draftUrl : (activeTab?.url ?? '')
  const isSecure = Boolean(activeTab?.url?.startsWith('https://'))

  async function handleCopyIp() {
    if (!exitIp) return
    try {
      await navigator.clipboard.writeText(exitIp)
      setCopiedIp(true)
      setTimeout(() => setCopiedIp(false), 1500)
    } catch {
      // Clipboard may be unavailable.
    }
  }

  async function handleOpenTerminal() {
    if (openingTerminal) return
    setOpeningTerminal(true)
    try {
      const res = await profilesAPI.openTerminal(workspace.profileId)
      toast(
        'ok',
        t('Isolated terminal opened'),
        t('{name} · Exit {ip} · HOME {home}', {
          name: profile?.name ?? workspace.profileId,
          ip: maskIp(res.exit_ip, maskIps),
          home: res.isolated_home,
        }),
      )
    } catch (err) {
      toast('error', t('Could not open isolated terminal'), t(err instanceof Error ? err.message : String(err)))
    } finally {
      setOpeningTerminal(false)
    }
  }

  async function handleCloseWindow() {
    if (closingWindow) return
    setClosingWindow(true)
    try {
      await closeProfileWorkspace(workspace.profileId)
      toast('ok', t('Browser stopped'), profile?.name ?? workspace.profileId)
    } catch (err) {
      toast('error', t('Could not stop the browser'), t(err instanceof Error ? err.message : String(err)))
    } finally {
      setClosingWindow(false)
    }
  }

  async function handleNavigateSubmit(event: React.FormEvent) {
    event.preventDefault()
    const bridge = getDesktopBridge()
    if (!bridge) return
    const input = draftUrl.trim()
    if (!input) return
    setEditingUrl(false)
    omniboxRef.current?.blur()
    if (activeTab) {
      await bridge.navigate(workspace.profileId, activeTab.id, input).catch(() => {})
    } else {
      await bridge.createTab(workspace.profileId, input).catch(() => {})
    }
  }

  async function handleCreateTab() {
    const bridge = getDesktopBridge()
    if (!bridge) return
    await bridge.createTab(workspace.profileId).catch(() => {})
  }

  async function handleActivateTab(tabId: string) {
    const bridge = getDesktopBridge()
    if (!bridge) return
    setEditingUrl(false)
    await bridge.activateTab(workspace.profileId, tabId).catch(() => {})
  }

  async function handleCloseTab(event: React.MouseEvent, tabId: string) {
    event.stopPropagation()
    const bridge = getDesktopBridge()
    if (!bridge) return
    await bridge.closeTab(workspace.profileId, tabId).catch(() => {})
  }

  async function handleBack() {
    const bridge = getDesktopBridge()
    if (!bridge || !activeTab) return
    await bridge.goBack(workspace.profileId, activeTab.id).catch(() => {})
  }

  async function handleForward() {
    const bridge = getDesktopBridge()
    if (!bridge || !activeTab) return
    await bridge.goForward(workspace.profileId, activeTab.id).catch(() => {})
  }

  async function handleReloadOrStop() {
    const bridge = getDesktopBridge()
    if (!bridge || !activeTab) return
    if (activeTab.loading) {
      await bridge.stop(workspace.profileId, activeTab.id).catch(() => {})
    } else {
      await bridge.reload(workspace.profileId, activeTab.id).catch(() => {})
    }
  }

  async function handleToggleDevTools() {
    const bridge = getDesktopBridge()
    if (!bridge || !activeTab || typeof bridge.toggleDevTools !== 'function') return
    await bridge.toggleDevTools(workspace.profileId, activeTab.id).catch(() => {})
  }

  return (
    <div className="flex h-full min-h-0 flex-col bg-canvas select-none">
      {/* Row 1 (40px): Integrated Profile Identity Chip + Multi-Tab Strip + Window Controls */}
      <header className="flex h-[40px] shrink-0 items-center gap-2 border-b border-line bg-sidebar px-2.5 app-drag">
        {/* Compact Profile Context Pill */}
        <div className="flex shrink-0 items-center gap-1.5 rounded-lg border border-line bg-surface/90 px-2 py-1 app-no-drag">
          <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-ok-dot signal-pulse" aria-hidden="true" />
          <span className="shrink-0 text-[13px] leading-none" aria-hidden="true">
            {flag(workspace.exitCountry) || '🌐'}
          </span>
          <h1
            className="max-w-[130px] truncate text-[12px] font-semibold tracking-tight text-ink"
            title={profile?.name ?? workspace.profileId}
          >
            {profile?.name ?? workspace.profileId}
          </h1>

          {exitIp ? (
            <button
              type="button"
              onClick={handleCopyIp}
              title={workspace.exitCity ? `${workspace.exitCity} · ${t('Copy exit IP')}` : t('Copy exit IP')}
              className="inline-flex shrink-0 cursor-pointer items-center gap-1 rounded-md bg-raised/85 px-1.5 py-0.2 font-mono text-[10.5px] text-ink-dim transition-colors hover:bg-raised hover:text-ink"
            >
              <span>{displayIp}</span>
              {copiedIp ? (
                <Check size={10} className="text-ok" aria-hidden="true" />
              ) : (
                <Copy size={9.5} className="text-ink-faint" aria-hidden="true" />
              )}
            </button>
          ) : (
            <span className="shrink-0 rounded-md bg-raised/70 px-1.5 py-0.2 text-[10.5px] text-ink-faint">
              {t('Direct connection')}
            </span>
          )}

          {fpHash && (
            <span
              className="hidden shrink-0 rounded bg-canvas/80 px-1 py-0.2 font-mono text-[10px] font-medium text-ink-faint ring-1 ring-line 2xl:inline-block"
              title={t('Hardware fingerprint identity')}
            >
              {fpHash}
            </span>
          )}
        </div>

        <span className="h-4 w-px shrink-0 bg-line" aria-hidden="true" />

        {/* Horizontal Tab Strip */}
        <div
          role="tablist"
          aria-label={t('Browser tabs')}
          className="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto app-no-drag"
        >
          {workspace.tabs.map(tab => {
            const isActive = tab.id === activeTab?.id
            const faviconUrl = tab.favicon && !brokenFavicons.has(tab.favicon) ? tab.favicon : null
            const title = tab.title?.trim() || formatTabHost(tab.url) || t('New tab')
            return (
              <div
                key={tab.id}
                role="tab"
                aria-selected={isActive}
                tabIndex={0}
                onClick={() => { void handleActivateTab(tab.id) }}
                onKeyDown={event => {
                  if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault()
                    void handleActivateTab(tab.id)
                  }
                }}
                title={tab.title || tab.url}
                className={`group flex h-[28px] max-w-[210px] min-w-[108px] shrink-0 cursor-pointer items-center gap-2 rounded-lg px-2.5 text-[12px] transition-all ${
                  isActive
                    ? 'bg-surface font-medium text-ink shadow-[0_1px_0_var(--prism-specular)_inset,0_2px_6px_-2px_rgba(0,0,0,0.3)] ring-1 ring-line-strong'
                    : 'text-ink-dim hover:bg-raised/70 hover:text-ink'
                }`}
              >
                {tab.loading ? (
                  <LoaderCircle size={12} className="shrink-0 animate-spin text-signal" aria-hidden="true" />
                ) : faviconUrl ? (
                  /* eslint-disable-next-line @next/next/no-img-element */
                  <img
                    src={faviconUrl}
                    alt=""
                    width={13}
                    height={13}
                    className="h-[13px] w-[13px] shrink-0 rounded-[3px] object-contain"
                    onError={() => {
                      setBrokenFavicons(prev => {
                        const next = new Set(prev)
                        next.add(faviconUrl)
                        return next
                      })
                    }}
                  />
                ) : (
                  <Globe size={12} className="shrink-0 text-ink-faint" aria-hidden="true" />
                )}
                <span className="min-w-0 flex-1 truncate">{title}</span>
                <button
                  type="button"
                  aria-label={t('Close tab')}
                  onClick={event => { void handleCloseTab(event, tab.id) }}
                  className={`flex h-4 w-4 shrink-0 cursor-pointer items-center justify-center rounded transition-colors hover:bg-raised hover:text-ink ${
                    isActive ? 'text-ink-faint opacity-80 hover:opacity-100' : 'text-ink-faint opacity-0 group-hover:opacity-100'
                  }`}
                >
                  <X size={11} aria-hidden="true" />
                </button>
              </div>
            )
          })}

          <button
            type="button"
            onClick={() => { void handleCreateTab() }}
            aria-label={t('New tab')}
            title={t('New tab')}
            className="flex h-[26px] w-[26px] shrink-0 cursor-pointer items-center justify-center rounded-lg text-ink-dim transition-colors hover:bg-raised hover:text-ink"
          >
            <Plus size={14} strokeWidth={2} aria-hidden="true" />
          </button>
        </div>

        {/* Right Window Controls */}
        <div className="flex shrink-0 items-center gap-1 app-no-drag">
          <button
            type="button"
            onClick={() => { void activateWorkspace(null) }}
            className="btn btn-default h-[26px] px-2.5 text-[11.5px]"
            title={t('Return to browser workspace list')}
          >
            <LayoutGrid size={12} aria-hidden="true" />
            <span className="hidden sm:inline">{t('Back to list')}</span>
          </button>

          <button
            type="button"
            disabled={closingWindow}
            onClick={handleCloseWindow}
            className="btn btn-ghost h-[26px] w-[26px] p-0 text-ink-dim hover:bg-danger-shade hover:text-danger"
            title={t('Close this browser window')}
            aria-label={t('Close window')}
          >
            {closingWindow ? (
              <LoaderCircle size={12} className="animate-spin" aria-hidden="true" />
            ) : (
              <Power size={13} aria-hidden="true" />
            )}
          </button>
        </div>
      </header>

      {/* Row 2 (42px): Navigation + Smart Omnibox + Isolation Quick Tools */}
      <div className="flex h-[42px] shrink-0 items-center gap-2 border-b border-line bg-surface px-3">
        <div className="flex shrink-0 items-center gap-0.5">
          <button
            type="button"
            disabled={!activeTab?.canGoBack}
            onClick={() => { void handleBack() }}
            aria-label={t('Go back')}
            title={t('Go back')}
            className="btn btn-ghost h-[28px] w-[28px] p-0"
          >
            <ArrowLeft size={14} aria-hidden="true" />
          </button>
          <button
            type="button"
            disabled={!activeTab?.canGoForward}
            onClick={() => { void handleForward() }}
            aria-label={t('Go forward')}
            title={t('Go forward')}
            className="btn btn-ghost h-[28px] w-[28px] p-0"
          >
            <ArrowRight size={14} aria-hidden="true" />
          </button>
          <button
            type="button"
            disabled={!activeTab}
            onClick={() => { void handleReloadOrStop() }}
            aria-label={activeTab?.loading ? t('Stop loading') : t('Reload page')}
            title={activeTab?.loading ? t('Stop loading') : t('Reload page')}
            className="btn btn-ghost h-[28px] w-[28px] p-0"
          >
            {activeTab?.loading ? (
              <X size={14} aria-hidden="true" />
            ) : (
              <RotateCw size={13} aria-hidden="true" />
            )}
          </button>
        </div>

        <form onSubmit={handleNavigateSubmit} className="min-w-0 flex-1">
          <div className="flex h-[30px] items-center gap-2 rounded-full border border-line bg-canvas/85 px-3 transition-all focus-within:border-signal/65 focus-within:bg-canvas focus-within:ring-2 focus-within:ring-signal/20">
            {isSecure ? (
              <Lock size={12} className="shrink-0 text-ok" aria-hidden="true" />
            ) : (
              <Globe size={12} className="shrink-0 text-ink-faint" aria-hidden="true" />
            )}
            <input
              ref={omniboxRef}
              type="text"
              value={displayUrl}
              aria-label={t('Address bar')}
              placeholder={t('Enter URL or search…')}
              spellCheck={false}
              autoComplete="off"
              onFocus={event => {
                setEditingUrl(true)
                setDraftUrl(activeTab?.url ?? '')
                event.currentTarget.select()
              }}
              onBlur={() => setEditingUrl(false)}
              onChange={event => setDraftUrl(event.target.value)}
              onKeyDown={event => {
                if (event.key === 'Escape') {
                  setEditingUrl(false)
                  event.currentTarget.blur()
                }
              }}
              className="w-full min-w-0 bg-transparent font-mono text-[12px] text-ink outline-none placeholder:font-sans placeholder:text-ink-faint select-text"
            />
          </div>
        </form>

        {/* Isolation Action Group */}
        <div className="flex shrink-0 items-center gap-1">
          {workspace.proxyPort && (
            <span
              className="hidden shrink-0 items-center gap-1.5 rounded-full border border-ok/20 bg-ok-shade px-2.5 py-0.5 text-[11px] font-medium text-ok xl:inline-flex"
              title={t('Isolated proxy tunnel active')}
            >
              <span className="h-1.5 w-1.5 rounded-full bg-ok-dot" aria-hidden="true" />
              {t('Isolated tunnel')}
            </span>
          )}

          <span className="mx-0.5 hidden h-4 w-px bg-line xl:block" aria-hidden="true" />

          <button
            type="button"
            disabled={!profile}
            onClick={() => setAuditOpen(true)}
            className="btn btn-ghost h-[28px] px-2 text-[12px]"
            title={t('Isolation health audit')}
          >
            <ShieldCheck size={13} className="text-ok" aria-hidden="true" />
            <span className="hidden md:inline">{t('Audit')}</span>
          </button>

          <button
            type="button"
            disabled={openingTerminal}
            onClick={handleOpenTerminal}
            className="btn btn-ghost h-[28px] px-2 text-[12px]"
            title={t('Open isolated terminal (shares this browser exit tunnel & isolated HOME)')}
          >
            {openingTerminal ? (
              <LoaderCircle size={13} className="animate-spin text-signal" aria-hidden="true" />
            ) : (
              <Terminal size={13} className="text-signal" aria-hidden="true" />
            )}
            <span className="hidden lg:inline">{t('Isolated terminal')}</span>
          </button>

          <button
            type="button"
            disabled={!profile}
            onClick={() => setCookiesOpen(true)}
            className="btn btn-ghost h-[28px] w-[28px] p-0"
            title={t('Import / export cookies')}
            aria-label={t('Cookies')}
          >
            <Cookie size={13} className="text-ink-dim" aria-hidden="true" />
          </button>

          <button
            type="button"
            disabled={!activeTab}
            onClick={() => { void handleToggleDevTools() }}
            className={`btn btn-ghost h-[28px] w-[28px] p-0 ${
              activeTab?.devToolsOpened ? 'bg-signal/12 text-signal' : ''
            }`}
            title={t('Developer tools (F12)')}
            aria-label={t('Developer tools')}
            aria-pressed={Boolean(activeTab?.devToolsOpened)}
          >
            <Code2 size={13} className={activeTab?.devToolsOpened ? 'text-signal' : 'text-ink-dim'} aria-hidden="true" />
          </button>
        </div>
      </div>

      {/* Native WebContentsView Viewport Host */}
      <div
        ref={viewportHostRef}
        data-testid="embedded-browser-viewport"
        className="relative min-h-0 flex-1 bg-canvas"
      >
        {occluded ? (
          <div className="flex h-full w-full flex-col items-center justify-center gap-2.5 px-6 text-center">
            <span className="flex h-11 w-11 items-center justify-center rounded-2xl border border-line-strong bg-surface text-signal shadow-[0_8px_24px_-8px_rgba(124,123,255,0.35)]">
              <ShieldCheck size={20} aria-hidden="true" />
            </span>
            <p className="text-[13.5px] font-medium text-ink">{t('Browser view paused while dialog is open')}</p>
            <p className="text-[12px] text-ink-dim">{t('Close the active dialog to immediately resume browsing.')}</p>
          </div>
        ) : workspace.tabs.length === 0 ? (
          <div className="flex h-full w-full flex-col items-center justify-center gap-3 px-6 text-center">
            <p className="text-[13px] text-ink-dim">{t('No open tabs in this isolated window.')}</p>
            <button type="button" className="btn btn-primary" onClick={() => { void handleCreateTab() }}>
              <Plus size={14} aria-hidden="true" />
              {t('New tab')}
            </button>
          </div>
        ) : null}
      </div>

      <IsolationAuditModal
        profile={auditOpen ? profile : null}
        onClose={() => setAuditOpen(false)}
        onChanged={refreshProfileDirectory}
      />

      {cookiesOpen && profile && (
        <CookieTransfer
          profile={profile}
          onClose={() => setCookiesOpen(false)}
          onChanged={refreshProfileDirectory}
        />
      )}
    </div>
  )
}

function formatTabHost(url?: string | null): string {
  if (!url) return ''
  try {
    const parsed = new URL(url)
    return parsed.hostname || url
  } catch {
    return url
  }
}
