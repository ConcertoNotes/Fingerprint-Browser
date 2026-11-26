'use client'

import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react'
import Link from 'next/link'
import { usePathname } from 'next/navigation'
import {
  ArrowRight,
  Check,
  Clock,
  Cloud,
  Globe2,
  KeyRound,
  LayoutGrid,
  Rss,
  Settings,
  TriangleAlert,
  X,
} from 'lucide-react'

import { ActivationModal } from '@/components/activation-modal'
import { EmbeddedBrowserStage } from '@/components/embedded-browser-stage'
import { useI18n } from '@/components/i18n-provider'
import { LanguageSelector } from '@/components/language-selector'
import { ThemeToggle } from '@/components/theme-toggle'
import { useWorkspace } from '@/components/workspace-provider'
import { systemAPI, type SystemStatus } from '@/lib/api'
import {
  entitlementDotClass,
  formatLicenseDate,
  isEntitlementBlocked,
} from '@/lib/cloud-api'
import { flag } from '@/lib/network-map'

const ACTIVATION_DISMISSED_KEY = 'prism.activation_modal_dismissed'
const ACTIVATION_DISMISSED_EVENT = 'prism.activation_modal_dismissed_changed'

function readActivationDismissed(): boolean {
  if (typeof window === 'undefined') return true
  try {
    return window.sessionStorage.getItem(ACTIVATION_DISMISSED_KEY) === '1'
  } catch {
    return false
  }
}

function dismissActivationForSession(): void {
  if (typeof window === 'undefined') return
  try {
    window.sessionStorage.setItem(ACTIVATION_DISMISSED_KEY, '1')
  } catch {
    // Embedded storage may be disabled.
  }
  window.dispatchEvent(new Event(ACTIVATION_DISMISSED_EVENT))
}

function subscribeActivationDismissed(onChange: () => void): () => void {
  if (typeof window === 'undefined') return () => {}
  window.addEventListener(ACTIVATION_DISMISSED_EVENT, onChange)
  return () => window.removeEventListener(ACTIVATION_DISMISSED_EVENT, onChange)
}

interface NavItem {
  href: string
  label: string
  key: 'browsers' | 'map' | 'subscriptions' | 'cloud'
}

const WORKSPACE_NAV: NavItem[] = [
  { href: '/', label: 'Browsers', key: 'browsers' },
  { href: '/map/', label: 'Node map', key: 'map' },
]

const RESOURCE_NAV: NavItem[] = [
  { href: '/subscriptions/', label: 'Subscriptions & nodes', key: 'subscriptions' },
  { href: '/cloud/', label: 'Cloud center', key: 'cloud' },
]

/** Obsidian/Porcelain layered navigation rail + elevated workspace / embedded browser stage + bottom telemetry bar. */
export function AppShell({ children }: { children: React.ReactNode }) {
  const { t } = useI18n()
  const pathname = usePathname()
  const current = pathname.replace(/\/+$/, '') || '/'
  const [status, setStatus] = useState<SystemStatus | null>(null)
  const {
    isDesktop,
    engineInfo,
    workspaces,
    activeWorkspaceId,
    activeWorkspace,
    profileDirectory,
    activateWorkspace,
    closeProfileWorkspace,
  } = useWorkspace()

  const prevPathnameRef = useRef(pathname)

  // Automatically dismiss the active native viewport whenever the user navigates to a new route
  useEffect(() => {
    if (prevPathnameRef.current !== pathname) {
      prevPathnameRef.current = pathname
      if (activeWorkspaceId !== null) {
        void activateWorkspace(null)
      }
    }
  }, [pathname, activeWorkspaceId, activateWorkspace])

  const refreshStatus = useCallback(async () => {
    try {
      setStatus(await systemAPI.status())
    } catch {
      // Transient backend poll failures leave the last known reading in place.
    }
  }, [])

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    refreshStatus()
    const timer = setInterval(refreshStatus, 5000)
    const onRefresh = () => { void refreshStatus() }
    window.addEventListener('cpm-status-refresh', onRefresh)
    return () => {
      clearInterval(timer)
      window.removeEventListener('cpm-status-refresh', onRefresh)
    }
  }, [refreshStatus])

  const leaveEmbeddedStage = useCallback(() => {
    if (activeWorkspaceId !== null) {
      void activateWorkspace(null)
    }
  }, [activeWorkspaceId, activateWorkspace])

  const isMacDesktop = isDesktop && engineInfo?.platform === 'darwin'
  const runningCount = Math.max(status?.running_browsers ?? 0, workspaces.length)
  const totalProfiles = status?.total_profiles ?? 0
  const isCustomer = status?.client_edition === 'customer'
  const entitlementStatus = status?.entitlement_status
  const requireActivation = Boolean(status?.require_activation)
  const brandName = isCustomer && status?.brand_name?.trim() ? status.brand_name.trim() : undefined
  const cloudActive = status?.cloud_policy_source === 'cloud'
  const settingsActive = !activeWorkspace && current === '/settings'

  const activationDismissed = useSyncExternalStore(
    subscribeActivationDismissed,
    readActivationDismissed,
    () => true,
  )
  const showActivationModal =
    isCustomer &&
    entitlementStatus === 'none' &&
    current !== '/cloud' &&
    (requireActivation || !activationDismissed)

  function renderNavIcon(item: NavItem, active: boolean) {
    const iconClass = `shrink-0 transition-colors ${active ? 'text-signal' : 'text-ink-faint group-hover:text-ink-dim'}`
    switch (item.key) {
      case 'browsers':
        return <LayoutGrid size={15} strokeWidth={1.85} className={iconClass} aria-hidden="true" />
      case 'map':
        return <Globe2 size={15} strokeWidth={1.85} className={iconClass} aria-hidden="true" />
      case 'subscriptions':
        return <Rss size={15} strokeWidth={1.85} className={iconClass} aria-hidden="true" />
      case 'cloud':
        return isCustomer
          ? <KeyRound size={15} strokeWidth={1.85} className={iconClass} aria-hidden="true" />
          : <Cloud size={15} strokeWidth={1.85} className={iconClass} aria-hidden="true" />
    }
  }

  return (
    <div className="flex h-screen w-screen overflow-hidden bg-canvas text-ink">
      {/* Left Navigation Sidebar */}
      <aside className="relative flex w-[232px] shrink-0 flex-col border-r border-line bg-sidebar select-none">
        {/* macOS native hiddenInset traffic-light safe drag strip */}
        {isMacDesktop && <div className="h-[36px] shrink-0 app-drag" aria-hidden="true" />}

        <div className={`flex items-center gap-2.5 px-4 app-drag ${isMacDesktop ? 'pb-3.5 pt-1' : 'h-[60px]'}`}>
          <div className="min-w-0 flex-1 app-no-drag">
            <PrismMark onNavigate={leaveEmbeddedStage} brandName={brandName} isCustomer={isCustomer} />
          </div>
        </div>

        <div className="mx-3 h-px bg-line" aria-hidden="true" />

        <nav className="flex flex-1 flex-col gap-5 overflow-y-auto px-3 py-3.5" aria-label={t('Main navigation')}>
          <div>
            <p className="mb-1.5 px-2.5 text-[10.5px] font-semibold uppercase tracking-[0.07em] text-ink-faint">
              {t('Workspace')}
            </p>
            <ul className="space-y-1">
              {WORKSPACE_NAV.map(item => {
                const target = item.href.replace(/\/+$/, '') || '/'
                const active = !activeWorkspace && current === target
                return (
                  <li key={item.href}>
                    <Link
                      href={item.href}
                      onClick={leaveEmbeddedStage}
                      aria-current={active ? 'page' : undefined}
                      className={`group relative flex items-center gap-2.5 rounded-xl px-2.5 py-2 text-[13px] transition-all ${
                        active
                          ? 'bg-surface font-medium text-ink shadow-[0_1px_0_var(--prism-specular)_inset,0_4px_12px_-6px_rgba(0,0,0,0.35)] ring-1 ring-line-strong'
                          : 'text-ink-dim hover:bg-raised/65 hover:text-ink'
                      }`}
                    >
                      {active && (
                        <span
                          className="absolute left-0 top-1/2 h-4 w-[2.5px] -translate-y-1/2 rounded-r-full bg-signal shadow-[0_0_8px_var(--prism-signal)]"
                          aria-hidden="true"
                        />
                      )}
                      {renderNavIcon(item, active)}
                      <span className="min-w-0 flex-1 truncate">{t(item.label)}</span>
                      {item.key === 'browsers' && runningCount > 0 && (
                        <span className="inline-flex items-center gap-1 rounded-full bg-ok-shade px-1.5 py-0.5 font-mono text-[10px] font-semibold text-ok ring-1 ring-ok/20">
                          <span className="h-1.5 w-1.5 rounded-full bg-ok-dot signal-pulse" aria-hidden="true" />
                          {runningCount}
                        </span>
                      )}
                      {item.key === 'browsers' && runningCount === 0 && totalProfiles > 0 && (
                        <span className="rounded-md bg-raised/80 px-1.5 py-0.5 font-mono text-[10.5px] text-ink-faint">
                          {totalProfiles}
                        </span>
                      )}
                    </Link>
                  </li>
                )
              })}
            </ul>
          </div>

          {/* Running Isolated Browser Windows (Single-Window Embedded Desktop Mode) */}
          {isDesktop && workspaces.length > 0 && (
            <div>
              <div className="mb-1.5 flex items-center justify-between px-2.5">
                <p className="text-[10.5px] font-semibold uppercase tracking-[0.07em] text-ink-faint">
                  {t('Running windows')}
                </p>
                <span className="rounded-full bg-ok-shade px-1.5 py-0.2 font-mono text-[10px] font-semibold text-ok">
                  {workspaces.length}
                </span>
              </div>
              <ul className="space-y-1">
                {workspaces.map(ws => {
                  const isActive = activeWorkspaceId === ws.profileId
                  const label = profileDirectory[ws.profileId]?.name ?? ws.profileId
                  const countryFlag = flag(ws.exitCountry)
                  return (
                    <li key={ws.profileId}>
                      <div
                        role="button"
                        tabIndex={0}
                        onClick={() => { void activateWorkspace(ws.profileId) }}
                        onKeyDown={event => {
                          if (event.key === 'Enter' || event.key === ' ') {
                            event.preventDefault()
                            void activateWorkspace(ws.profileId)
                          }
                        }}
                        aria-current={isActive ? 'page' : undefined}
                        title={ws.exitIp ? `${label} · ${ws.exitIp}` : label}
                        className={`group relative flex cursor-pointer items-center gap-2 rounded-xl px-2.5 py-1.5 text-[12.5px] transition-all ${
                          isActive
                            ? 'bg-surface font-medium text-ink shadow-[0_1px_0_var(--prism-specular)_inset,0_4px_12px_-6px_rgba(0,0,0,0.35)] ring-1 ring-ok/35'
                            : 'text-ink-dim hover:bg-raised/65 hover:text-ink'
                        }`}
                      >
                        {isActive && (
                          <span
                            className="absolute left-0 top-1/2 h-4 w-[2.5px] -translate-y-1/2 rounded-r-full bg-ok-dot shadow-[0_0_8px_var(--prism-ok-dot)]"
                            aria-hidden="true"
                          />
                        )}
                        <span
                          className={`h-2 w-2 shrink-0 rounded-full bg-ok-dot ${isActive ? 'signal-pulse' : ''}`}
                          aria-hidden="true"
                        />
                        {countryFlag && (
                          <span className="shrink-0 text-[13px] leading-none" aria-hidden="true">
                            {countryFlag}
                          </span>
                        )}
                        <span className="min-w-0 flex-1 truncate">{label}</span>
                        {ws.tabs.length > 1 && (
                          <span className="shrink-0 rounded bg-raised px-1 py-0.2 font-mono text-[10px] text-ink-faint group-hover:hidden">
                            {ws.tabs.length}
                          </span>
                        )}
                        <button
                          type="button"
                          aria-label={t('Stop {name}', { name: label })}
                          title={t('Close window')}
                          onClick={event => {
                            event.stopPropagation()
                            void closeProfileWorkspace(ws.profileId)
                          }}
                          className="hidden h-4 w-4 shrink-0 cursor-pointer items-center justify-center rounded text-ink-faint hover:bg-danger-shade hover:text-danger group-hover:flex"
                        >
                          <X size={11} aria-hidden="true" />
                        </button>
                      </div>
                    </li>
                  )
                })}
              </ul>
            </div>
          )}

          <div>
            <p className="mb-1.5 px-2.5 text-[10.5px] font-semibold uppercase tracking-[0.07em] text-ink-faint">
              {isCustomer ? t('Nodes & License') : t('Resources & Cloud')}
            </p>
            <ul className="space-y-1">
              {RESOURCE_NAV.map(item => {
                const target = item.href.replace(/\/+$/, '') || '/'
                const active = !activeWorkspace && current === target
                const label = item.key === 'cloud' && isCustomer ? t('My license') : t(item.label)
                return (
                  <li key={item.href}>
                    <Link
                      href={item.href}
                      onClick={leaveEmbeddedStage}
                      aria-current={active ? 'page' : undefined}
                      className={`group relative flex items-center gap-2.5 rounded-xl px-2.5 py-2 text-[13px] transition-all ${
                        active
                          ? 'bg-surface font-medium text-ink shadow-[0_1px_0_var(--prism-specular)_inset,0_4px_12px_-6px_rgba(0,0,0,0.35)] ring-1 ring-line-strong'
                          : 'text-ink-dim hover:bg-raised/65 hover:text-ink'
                      }`}
                    >
                      {active && (
                        <span
                          className="absolute left-0 top-1/2 h-4 w-[2.5px] -translate-y-1/2 rounded-r-full bg-signal shadow-[0_0_8px_var(--prism-signal)]"
                          aria-hidden="true"
                        />
                      )}
                      {renderNavIcon(item, active)}
                      <span className="min-w-0 flex-1 truncate">{label}</span>
                      {item.key === 'cloud' && isCustomer && (
                        <span
                          className={`h-2 w-2 shrink-0 rounded-full ${entitlementDotClass(entitlementStatus)}`}
                          aria-hidden="true"
                        />
                      )}
                      {item.key === 'cloud' && !isCustomer && cloudActive && (
                        <span className="h-2 w-2 shrink-0 rounded-full bg-ok-dot" aria-hidden="true" />
                      )}
                    </Link>
                  </li>
                )
              })}
            </ul>
          </div>
        </nav>

        {/* Sidebar Footer */}
        <div className="space-y-2.5 border-t border-line bg-sidebar/90 px-3 py-3">
          <Link
            href="/settings/"
            onClick={leaveEmbeddedStage}
            aria-current={settingsActive ? 'page' : undefined}
            className={`group relative flex items-center gap-2.5 rounded-xl px-2.5 py-2 text-[13px] transition-all ${
              settingsActive
                ? 'bg-surface font-medium text-ink shadow-[0_1px_0_var(--prism-specular)_inset,0_4px_12px_-6px_rgba(0,0,0,0.35)] ring-1 ring-line-strong'
                : 'text-ink-dim hover:bg-raised/65 hover:text-ink'
            }`}
          >
            {settingsActive && (
              <span
                className="absolute left-0 top-1/2 h-4 w-[2.5px] -translate-y-1/2 rounded-r-full bg-signal shadow-[0_0_8px_var(--prism-signal)]"
                aria-hidden="true"
              />
            )}
            <Settings
              size={15}
              strokeWidth={1.85}
              className={settingsActive ? 'text-signal' : 'text-ink-faint group-hover:text-ink-dim'}
              aria-hidden="true"
            />
            <span className="flex-1 truncate">{t('System settings')}</span>
          </Link>

          <div className="flex items-center justify-between gap-2 px-1">
            <ThemeToggle compact />
            <LanguageSelector />
          </div>
        </div>
      </aside>

      {/* Main Content or Active Embedded Browser Stage + Persistent Bottom StatusBar */}
      <div className="flex min-w-0 flex-1 flex-col bg-canvas">
        <EntitlementBanner status={status} isCustomer={isCustomer} onNavigate={leaveEmbeddedStage} />
        {activeWorkspace ? (
          <main className="min-h-0 flex-1 overflow-hidden">
            <EmbeddedBrowserStage workspace={activeWorkspace} />
          </main>
        ) : (
          <main className="min-h-0 flex-1 overflow-y-auto">{children}</main>
        )}
        <StatusBar status={status} runningCount={runningCount} onNavigate={leaveEmbeddedStage} />
      </div>

      {/* First-run Activation Modal for Customer Edition */}
      <ActivationModal
        open={showActivationModal}
        brandName={brandName}
        requireActivation={requireActivation}
        onDismiss={dismissActivationForSession}
        onActivated={() => { void refreshStatus() }}
      />
    </div>
  )
}

function EntitlementBanner({
  status,
  isCustomer,
  onNavigate,
}: {
  status: SystemStatus | null
  isCustomer: boolean
  onNavigate: () => void
}) {
  const { t, locale } = useI18n()
  const entitlementStatus = status?.entitlement_status
  if (!entitlementStatus) return null

  if (entitlementStatus === 'expiring_soon') {
    const dateStr = formatLicenseDate(status?.entitlement_expires_at, locale)
    const days = Math.max(0, status?.entitlement_days_remaining ?? 0)
    return (
      <div
        role="status"
        className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-b border-warn/25 bg-warn-shade px-5 py-2 text-[12px] text-warn"
      >
        <div className="flex min-w-0 items-center gap-2">
          <Clock size={14} className="shrink-0" aria-hidden="true" />
          <span className="font-medium">
            {dateStr
              ? t('Your exclusive node license expires on {date} ({days}d remaining). Please contact your administrator to renew.', {
                  date: dateStr,
                  days,
                })
              : t('Your exclusive node license is expiring soon ({days}d remaining). Please contact your administrator to renew.', {
                  days,
                })}
          </span>
        </div>
        <Link
          href="/cloud/"
          onClick={onNavigate}
          className="inline-flex shrink-0 items-center gap-1 rounded-md border border-warn/30 bg-surface/85 px-2.5 py-0.5 text-[11.5px] font-medium text-warn transition-colors hover:bg-surface"
        >
          <span>{isCustomer ? t('My license') : t('Cloud center')}</span>
          <ArrowRight size={11} aria-hidden="true" />
        </Link>
      </div>
    )
  }

  if (!isEntitlementBlocked(entitlementStatus)) return null

  let message: string
  switch (entitlementStatus) {
    case 'expired':
      message = t('Your exclusive node license has expired and cloud nodes are paused (your local nodes remain usable). Please contact your administrator to renew or replace your activation code.')
      break
    case 'disabled':
      message = t('Your exclusive node license has been disabled by the administrator and cloud nodes are paused (your local nodes remain usable).')
      break
    case 'revoked':
      message = t('This device was logged out by the administrator and cloud nodes are paused (your local nodes remain usable). Please re-activate your license.')
      break
    case 'device_limit':
      message = t('Device limit exceeded — cloud nodes are paused on this computer (your local nodes remain usable). Contact your administrator to release a device slot.')
      break
    case 'offline_grace_exceeded':
      message = t('Offline verification grace period exceeded — cloud nodes are paused until reconnected online (your local nodes remain usable).')
      break
    default:
      message = t('Cloud nodes are currently paused due to license status (your local nodes remain usable).')
      break
  }

  return (
    <div
      role="alert"
      className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-b border-danger/25 bg-danger-shade px-5 py-2 text-[12px] text-danger"
    >
      <div className="flex min-w-0 items-center gap-2">
        <TriangleAlert size={14} className="shrink-0" aria-hidden="true" />
        <span className="font-medium">{message}</span>
      </div>
      <Link
        href="/cloud/"
        onClick={onNavigate}
        className="inline-flex shrink-0 items-center gap-1 rounded-md border border-danger/30 bg-surface/90 px-2.5 py-0.5 text-[11.5px] font-medium text-danger transition-colors hover:bg-surface"
      >
        <span>{isCustomer ? t('My license') : t('Cloud center')}</span>
        <ArrowRight size={11} aria-hidden="true" />
      </Link>
    </div>
  )
}

function StatusBar({
  status,
  runningCount,
  onNavigate,
}: {
  status: SystemStatus | null
  runningCount: number
  onNavigate: () => void
}) {
  const { t } = useI18n()
  const running = runningCount
  const cpu = status?.cpu_percent != null ? Math.round(status.cpu_percent) : null
  const memGb = status?.memory_used_mb != null ? (status.memory_used_mb / 1024).toFixed(1) : null
  const memPct = status?.memory_percent != null ? Math.round(status.memory_percent) : (status?.memory_usage != null ? Math.round(status.memory_usage) : null)
  const proxyEngineOk = status?.mihomo_available ?? false
  const proxyEngineVer = status?.mihomo_version ? status.mihomo_version.replace(/^v/i, '') : null
  const policyVer = status?.cloud_policy_version ?? null
  const policyCloud = status?.cloud_policy_source === 'cloud'
  const isCustomer = status?.client_edition === 'customer'
  const entitlementStatus = status?.entitlement_status

  let licenseLabel = t('Not activated')
  let licenseTone = 'text-ink-dim'
  if (isCustomer) {
    if (entitlementStatus === 'active') {
      licenseLabel =
        status?.entitlement_days_remaining != null
          ? t('License active · {days}d left', { days: Math.max(0, status.entitlement_days_remaining) })
          : t('License active · Permanent')
      licenseTone = 'text-ink-dim'
    } else if (entitlementStatus === 'expiring_soon') {
      licenseLabel = t('Expiring soon · {days}d left', { days: Math.max(0, status?.entitlement_days_remaining ?? 0) })
      licenseTone = 'text-warn'
    } else if (entitlementStatus === 'expired') {
      licenseLabel = t('License expired')
      licenseTone = 'text-danger'
    } else if (entitlementStatus === 'disabled') {
      licenseLabel = t('License disabled')
      licenseTone = 'text-danger'
    } else if (entitlementStatus === 'revoked') {
      licenseLabel = t('Device revoked')
      licenseTone = 'text-danger'
    } else if (entitlementStatus === 'device_limit') {
      licenseLabel = t('Device limit reached')
      licenseTone = 'text-danger'
    } else if (entitlementStatus === 'offline_grace_exceeded') {
      licenseLabel = t('Online check required')
      licenseTone = 'text-danger'
    }
  }

  return (
    <footer
      aria-label={t('System status bar')}
      className="flex h-[30px] shrink-0 items-center justify-between gap-4 border-t border-line bg-sidebar/95 px-4 text-[11px] text-ink-dim backdrop-blur-md select-none"
    >
      <div className="flex min-w-0 items-center gap-3 overflow-hidden">
        <Link href="/" onClick={onNavigate} className="inline-flex shrink-0 items-center gap-1.5 transition-colors hover:text-ink">
          <span
            className={`h-1.5 w-1.5 rounded-full ${running > 0 ? 'bg-ok-dot signal-pulse' : 'bg-slate-300'}`}
            aria-hidden="true"
          />
          <span className={running > 0 ? 'font-medium text-ink' : ''}>
            {t('{count} running', { count: running })}
          </span>
        </Link>

        <span className="h-3 w-px bg-line" aria-hidden="true" />

        <span className="shrink-0 font-mono text-ink-faint">
          {t('CPU {value}', { value: cpu != null ? `${cpu}%` : '—' })}
        </span>

        <span className="h-3 w-px bg-line" aria-hidden="true" />

        <span className="shrink-0 font-mono text-ink-faint">
          {memGb != null
            ? t('Memory {value} GB', { value: memGb })
            : memPct != null
              ? t('Memory {value}%', { value: memPct })
              : t('Memory {value}', { value: '—' })}
        </span>
      </div>

      <div className="flex shrink-0 items-center gap-3">
        <Link
          href="/settings/"
          onClick={onNavigate}
          className={`inline-flex items-center gap-1.5 transition-colors hover:text-ink ${proxyEngineOk ? 'text-ink-dim' : 'text-warn'}`}
        >
          {proxyEngineOk ? (
            <Check size={11.5} strokeWidth={2.4} className="text-ok" aria-hidden="true" />
          ) : (
            <TriangleAlert size={11.5} strokeWidth={2} className="text-warn" aria-hidden="true" />
          )}
          <span className="font-mono">
            {proxyEngineOk
              ? (proxyEngineVer ? t('Proxy Engine v{version}', { version: proxyEngineVer }) : t('Proxy Engine ready'))
              : t('Proxy Engine unavailable')}
          </span>
        </Link>

        <span className="h-3 w-px bg-line" aria-hidden="true" />

        {isCustomer ? (
          <Link
            href="/cloud/"
            onClick={onNavigate}
            className={`inline-flex items-center gap-1.5 transition-colors hover:text-ink ${licenseTone}`}
            title={status?.entitlement_tenant_name || t('My license')}
          >
            <span
              className={`h-1.5 w-1.5 rounded-full ${entitlementDotClass(entitlementStatus)}`}
              aria-hidden="true"
            />
            <span className="font-medium">{licenseLabel}</span>
          </Link>
        ) : (
          <Link
            href="/cloud/"
            onClick={onNavigate}
            className="inline-flex items-center gap-1.5 transition-colors hover:text-ink"
            title={policyCloud ? t('Using cloud-delivered policy') : t('Using built-in baseline policy')}
          >
            <span
              className={`h-1.5 w-1.5 rounded-full ${policyCloud ? 'bg-ok-dot' : 'bg-signal'}`}
              aria-hidden="true"
            />
            <span className="font-mono">
              {policyVer
                ? t('Policy {version}', { version: policyVer })
                : t('Policy baseline')}
            </span>
          </Link>
        )}
      </div>
    </footer>
  )
}

/** Brand mark matching packaging/prism-browser.svg: deep obsidian-indigo squircle with refracting crystal prism. */
function PrismMark({
  onNavigate,
  brandName,
  isCustomer,
}: {
  onNavigate: () => void
  brandName?: string
  isCustomer?: boolean
}) {
  const { t } = useI18n()
  const displayName = brandName || t('Prism')
  const isCustomBrand = Boolean(isCustomer && brandName && brandName !== '棱镜浏览器' && brandName !== 'Prism')
  return (
    <Link href="/" onClick={onNavigate} className="group flex min-w-0 items-center gap-2.5 text-ink">
      <span className="relative flex h-8 w-8 shrink-0 items-center justify-center overflow-hidden rounded-[9px] shadow-[0_4px_16px_-2px_rgba(99,102,241,0.45)] ring-1 ring-white/15 transition-transform duration-150 group-hover:scale-[1.03]">
        <svg width="32" height="32" viewBox="0 0 128 128" fill="none" aria-hidden="true">
          <defs>
            <linearGradient id="pmBg" x1="14" y1="4" x2="114" y2="126" gradientUnits="userSpaceOnUse">
              <stop offset="0%" stopColor="#0A0F24" />
              <stop offset="50%" stopColor="#171642" />
              <stop offset="100%" stopColor="#2A1D66" />
            </linearGradient>
            <linearGradient id="pmBeamIn" x1="8" y1="73" x2="44" y2="61" gradientUnits="userSpaceOnUse">
              <stop offset="0%" stopColor="#FFFFFF" stopOpacity="0" />
              <stop offset="100%" stopColor="#FFFFFF" stopOpacity="1" />
            </linearGradient>
            <linearGradient id="pmCyan" x1="75" y1="53" x2="122" y2="36" gradientUnits="userSpaceOnUse">
              <stop offset="0%" stopColor="#E0F2FE" />
              <stop offset="40%" stopColor="#38BDF8" />
              <stop offset="100%" stopColor="#0284C7" stopOpacity="0" />
            </linearGradient>
            <linearGradient id="pmViolet" x1="79" y1="62" x2="122" y2="62" gradientUnits="userSpaceOnUse">
              <stop offset="0%" stopColor="#F5F3FF" />
              <stop offset="45%" stopColor="#818CF8" />
              <stop offset="100%" stopColor="#9333EA" stopOpacity="0" />
            </linearGradient>
            <linearGradient id="pmAmber" x1="83" y1="71" x2="122" y2="88" gradientUnits="userSpaceOnUse">
              <stop offset="0%" stopColor="#FEF3C7" />
              <stop offset="45%" stopColor="#FB923C" />
              <stop offset="100%" stopColor="#E11D48" stopOpacity="0" />
            </linearGradient>
            <linearGradient id="pmFacetL" x1="34" y1="26" x2="61" y2="86" gradientUnits="userSpaceOnUse">
              <stop offset="0%" stopColor="#FFFFFF" stopOpacity="0.44" />
              <stop offset="100%" stopColor="#818CF8" stopOpacity="0.10" />
            </linearGradient>
            <linearGradient id="pmFacetR" x1="61" y1="24" x2="94" y2="95" gradientUnits="userSpaceOnUse">
              <stop offset="0%" stopColor="#C7D2FE" stopOpacity="0.24" />
              <stop offset="100%" stopColor="#38BDF8" stopOpacity="0.16" />
            </linearGradient>
          </defs>
          <rect width="128" height="128" rx="28" fill="url(#pmBg)" />
          <polygon points="6,71.8 43.5,59.5 43.5,62.5 6,75" fill="url(#pmBeamIn)" />
          <polygon points="75,52.2 123,33.5 123,40 75.6,55.2" fill="url(#pmCyan)" />
          <polygon points="79,60.8 124,59.5 124,65.2 79.6,64" fill="url(#pmViolet)" />
          <polygon points="83,69.6 123,86 123,92.5 83.8,72.8" fill="url(#pmAmber)" />
          <polygon points="61,23 27,96 61,74" fill="url(#pmFacetL)" />
          <polygon points="61,23 61,74 95,96" fill="url(#pmFacetR)" />
          <polygon points="27,96 61,74 95,96" fill="#A5B4FC" fillOpacity="0.16" />
          <path d="M61 23 L95 96 L27 96 Z" stroke="#E0E7FF" strokeOpacity="0.88" strokeWidth="3.2" strokeLinejoin="round" />
          <circle cx="43.5" cy="61" r="4" fill="#FFFFFF" fillOpacity="0.92" />
        </svg>
      </span>
      <span className="flex min-w-0 flex-col leading-tight">
        <span className="truncate text-[14px] font-semibold tracking-tight text-ink" title={displayName}>
          {displayName}
        </span>
        {isCustomBrand ? (
          <span className="truncate text-[9.5px] font-medium text-ink-faint">{t('Exclusive Edition')}</span>
        ) : (
          <span className="text-[9.5px] font-semibold tracking-[0.2em] text-ink-faint">PRISM</span>
        )}
      </span>
    </Link>
  )
}
