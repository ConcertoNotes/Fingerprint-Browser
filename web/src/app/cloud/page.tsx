'use client'

import { useCallback, useEffect, useState } from 'react'
import Link from 'next/link'
import {
  ArrowRight,
  CalendarClock,
  Check,
  Cloud,
  CloudDownload,
  CloudUpload,
  Eye,
  EyeOff,
  Globe2,
  KeyRound,
  Laptop,
  LoaderCircle,
  LogOut,
  RefreshCw,
  Rss,
  ShieldCheck,
  Sparkles,
  TriangleAlert,
} from 'lucide-react'

import { EmptyState } from '@/components/empty-state'
import { useI18n } from '@/components/i18n-provider'
import { ConfirmDialog } from '@/components/modal'
import { useToast } from '@/components/toast'
import { OS_LABELS, profileFingerprintHash, profilesAPI, type Profile } from '@/lib/api'
import {
  cloudAPI,
  entitlementDotClass,
  formatCloudAuthError,
  formatLicenseDate,
  isCustomerEdition,
  isEntitlementBlocked,
  type CloudPolicyData,
  type CloudStatus,
} from '@/lib/cloud-api'
import type { Translator } from '@/lib/i18n'
import { networkAPI, type Subscription } from '@/lib/network-api'

function getEntitlementBadge(
  entitlementStatus: string | null | undefined,
  t: Translator,
): { label: string; pillClass: string; dotClass: string } {
  const dotClass = entitlementDotClass(entitlementStatus)
  switch (entitlementStatus) {
    case 'active':
      return { label: t('License valid'), pillClass: 'bg-ok-shade text-ok', dotClass }
    case 'expiring_soon':
      return { label: t('Expiring soon'), pillClass: 'bg-warn-shade text-warn', dotClass }
    case 'expired':
      return { label: t('Expired'), pillClass: 'bg-danger-shade text-danger', dotClass }
    case 'disabled':
      return { label: t('Disabled'), pillClass: 'bg-danger-shade text-danger', dotClass }
    case 'revoked':
      return { label: t('Device revoked'), pillClass: 'bg-danger-shade text-danger', dotClass }
    case 'device_limit':
      return { label: t('Device limit exceeded'), pillClass: 'bg-danger-shade text-danger', dotClass }
    case 'offline_grace_exceeded':
      return { label: t('Offline grace exceeded'), pillClass: 'bg-danger-shade text-danger', dotClass }
    default:
      return { label: t('Not activated'), pillClass: 'bg-sidebar text-ink-dim ring-1 ring-line', dotClass }
  }
}

function getBlockedExplanation(entitlementStatus: string | null | undefined, t: Translator): string | null {
  switch (entitlementStatus) {
    case 'expired':
      return t('Your exclusive node license has expired and cloud nodes are paused (your local nodes remain usable). Please contact your administrator to renew or replace your activation code.')
    case 'disabled':
      return t('Your exclusive node license has been disabled by the administrator and cloud nodes are paused (your local nodes remain usable).')
    case 'revoked':
      return t('This device was logged out by the administrator and cloud nodes are paused (your local nodes remain usable). Please re-activate your license.')
    case 'device_limit':
      return t('Device limit exceeded — cloud nodes are paused on this computer (your local nodes remain usable). Contact your administrator to release a device slot.')
    case 'offline_grace_exceeded':
      return t('Offline verification grace period exceeded — cloud nodes are paused until reconnected online (your local nodes remain usable).')
    default:
      return null
  }
}

export default function CloudCenterPage() {
  const { t, locale } = useI18n()
  const toast = useToast()

  const [status, setStatus] = useState<CloudStatus | null>(null)
  const [policyData, setPolicyData] = useState<CloudPolicyData | null>(null)
  const [cloudSubs, setCloudSubs] = useState<Subscription[]>([])
  const [profiles, setProfiles] = useState<Profile[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  // Standard mode config form state
  const [baseUrl, setBaseUrl] = useState('')
  const [token, setToken] = useState('')
  const [deviceLabel, setDeviceLabel] = useState('')
  const [autoSync, setAutoSync] = useState(true)
  const [syncProfiles, setSyncProfiles] = useState(true)
  const [allowLocal, setAllowLocal] = useState(false)
  const [includeCookiesOnPush, setIncludeCookiesOnPush] = useState(false)

  // Customer mode activation / replace / logout state
  const [activationToken, setActivationToken] = useState('')
  const [showActivationToken, setShowActivationToken] = useState(false)
  const [activationError, setActivationError] = useState<string | null>(null)
  const [activating, setActivating] = useState(false)
  const [replacingCode, setReplacingCode] = useState(false)
  const [confirmLogout, setConfirmLogout] = useState(false)
  const [loggingOut, setLoggingOut] = useState(false)

  // Busy flags
  const [saving, setSaving] = useState(false)
  const [syncingScope, setSyncingScope] = useState<string | null>(null)
  const [pulling, setPulling] = useState(false)
  const [pushingId, setPushingId] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      const statusRes = await cloudAPI.status()
      const customerMode = isCustomerEdition(statusRes)
      const shouldLoadProfiles = !customerMode || statusRes.profiles_sync_allowed === true
      const [policyRes, subsRes, profilesRes] = await Promise.all([
        customerMode ? Promise.resolve(null) : cloudAPI.policy().catch(() => null),
        networkAPI.subscriptions().catch(() => []),
        shouldLoadProfiles
          ? profilesAPI.getProfiles({ page: 1, per_page: 100 }).catch(() => ({
              profiles: [],
              total: 0,
              page: 1,
              per_page: 100,
              has_next: false,
              has_prev: false,
            }))
          : Promise.resolve({
              profiles: [],
              total: 0,
              page: 1,
              per_page: 100,
              has_next: false,
              has_prev: false,
            }),
      ])
      setStatus(statusRes)
      setPolicyData(policyRes)
      setCloudSubs(subsRes.filter(s => s.managed_by === 'cloud'))
      setProfiles(profilesRes.profiles)
      setBaseUrl(statusRes.base_url ?? '')
      setDeviceLabel(statusRes.device_label ?? '')
      setAutoSync(statusRes.auto_sync_on_startup)
      setSyncProfiles(statusRes.sync_profiles_enabled)
      setAllowLocal(Boolean(statusRes.allow_local))
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    load()
  }, [load])

  async function saveConfig(event: React.FormEvent) {
    event.preventDefault()
    setSaving(true)
    try {
      const updated = await cloudAPI.updateConfig({
        base_url: baseUrl.trim() || null,
        ...(token.trim() ? { token: token.trim() } : {}),
        auto_sync_on_startup: autoSync,
        sync_profiles_enabled: syncProfiles,
        device_label: deviceLabel.trim() || null,
        allow_local: allowLocal,
      })
      setStatus(updated)
      setToken('')
      toast('ok', t('Cloud configuration saved'))
    } catch (err) {
      toast('error', t('Could not save cloud configuration'), t(formatCloudAuthError(err)))
    } finally {
      setSaving(false)
    }
  }

  async function clearToken() {
    setSaving(true)
    try {
      const updated = await cloudAPI.updateConfig({ clear_token: true })
      setStatus(updated)
      toast('ok', t('Cloud token cleared'))
    } catch (err) {
      toast('error', t('Could not clear token'), t(err instanceof Error ? err.message : String(err)))
    } finally {
      setSaving(false)
    }
  }

  async function handleActivate(event: React.FormEvent) {
    event.preventDefault()
    const trimmed = activationToken.trim()
    if (!trimmed || activating) return

    setActivating(true)
    setActivationError(null)
    try {
      const res = await cloudAPI.activate(trimmed)
      await load()
      if (isEntitlementBlocked(res.status?.entitlement_status)) {
        const msg = res.status.last_error
          ? t(res.status.last_error)
          : t('Activation succeeded, but your license is currently unavailable.')
        setActivationError(msg)
        return
      }
      setActivationToken('')
      setReplacingCode(false)
      toast(
        'ok',
        t('License activated'),
        res.status?.tenant_name
          ? t('Welcome, {name}. Your exclusive nodes have been synced.', { name: res.status.tenant_name })
          : t('Your exclusive nodes have been synced.'),
      )
    } catch (err) {
      setActivationError(t(formatCloudAuthError(err)))
    } finally {
      setActivating(false)
    }
  }

  async function handleLogout() {
    setLoggingOut(true)
    try {
      await cloudAPI.logout()
      setConfirmLogout(false)
      setReplacingCode(false)
      setActivationToken('')
      setActivationError(null)
      await load()
      toast('ok', t('License logged out'), t('Synced cloud nodes were cleared; your local nodes remain intact.'))
    } catch (err) {
      toast('error', t('Could not log out license'), t(formatCloudAuthError(err)))
    } finally {
      setLoggingOut(false)
    }
  }

  async function triggerSync(scope: 'all' | 'policy' | 'subscriptions') {
    setSyncingScope(scope)
    try {
      const report = await cloudAPI.sync(scope === 'all' ? undefined : [scope])
      await load()
      if (report.errors && report.errors.length > 0) {
        toast('error', t('Sync finished with warnings'), report.errors.join('\n'))
      } else {
        toast('ok', isCustomerMode ? t('Exclusive nodes synced') : t('Cloud sync complete'))
      }
    } catch (err) {
      toast('error', t('Cloud sync failed'), t(formatCloudAuthError(err)))
      await load()
    } finally {
      setSyncingScope(null)
    }
  }

  async function handlePullProfiles() {
    setPulling(true)
    try {
      const res = await cloudAPI.pullProfiles()
      await load()
      toast(
        'ok',
        t('Cloud browser configs pulled'),
        t('{created} created, {updated} updated, {rebound} nodes rebound, {skipped} running skipped.', {
          created: res.created,
          updated: res.updated,
          rebound: res.rebound,
          skipped: res.skipped_running,
        }),
      )
    } catch (err) {
      toast('error', t('Could not pull browser configs'), t(formatCloudAuthError(err)))
    } finally {
      setPulling(false)
    }
  }

  async function handlePushProfile(profile: Profile) {
    setPushingId(profile.id)
    try {
      const res = await cloudAPI.pushProfile(profile.id, includeCookiesOnPush)
      await load()
      toast('ok', t('Pushed to cloud'), t('{name} · rev {rev}', { name: profile.name, rev: res.revision }))
    } catch (err) {
      toast('error', t('Cloud push failed'), t(formatCloudAuthError(err)))
    } finally {
      setPushingId(null)
    }
  }

  const isCustomerMode = isCustomerEdition(status)
  const entitlementStatus = status?.entitlement_status ?? 'none'
  const isActivated = Boolean(status?.has_token && entitlementStatus !== 'none')
  const badge = getEntitlementBadge(entitlementStatus, t)
  const blockedNote = getBlockedExplanation(entitlementStatus, t)
  const formattedExpiry = formatLicenseDate(status?.expires_at, locale)

  const deviceUsageText =
    status?.max_devices && status.max_devices > 0
      ? t('{active} / {max} devices', { active: status.active_devices ?? 1, max: status.max_devices })
      : t('Unlimited')

  // ---------------------------------------------------------------------------
  // CUSTOMER MODE: Clean "My license" view (never exposes URL / policy / prefs)
  // ---------------------------------------------------------------------------
  if (isCustomerMode) {
    return (
      <div className="min-h-full bg-canvas">
        <header className="sticky top-0 z-20 flex min-h-[54px] flex-wrap items-center gap-3 border-b border-line bg-canvas/85 px-6 py-3 backdrop-blur-xl app-drag">
          <div className="flex items-center gap-2.5 app-no-drag">
            <h1 className="text-[16px] font-semibold tracking-tight text-ink">{t('My license')}</h1>
            {status && (
              <span className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-[11px] font-medium ${badge.pillClass}`}>
                <span className={`h-1.5 w-1.5 rounded-full ${badge.dotClass}`} aria-hidden="true" />
                {badge.label}
              </span>
            )}
          </div>

          {isActivated && (
            <div className="ml-auto flex items-center gap-2 app-no-drag">
              <button
                type="button"
                className="btn btn-primary"
                disabled={syncingScope !== null}
                onClick={() => triggerSync('all')}
              >
                {syncingScope !== null ? (
                  <LoaderCircle size={13} className="animate-spin" aria-hidden="true" />
                ) : (
                  <RefreshCw size={13} aria-hidden="true" />
                )}
                {t('Sync exclusive nodes now')}
              </button>
            </div>
          )}
        </header>

        {error ? (
          <EmptyState
            icon={<KeyRound size={18} />}
            title={t('Cannot load license status')}
            body={t(error)}
            action={<button type="button" className="btn btn-default" onClick={load}>{t('Retry')}</button>}
          />
        ) : loading ? (
          <p className="px-6 py-10 text-ink-dim" role="status">{t('Loading…')}</p>
        ) : !isActivated ? (
          <div className="mx-auto max-w-[520px] px-6 py-12">
            <section className="panel relative overflow-hidden p-7">
              <div className="prism-hairline absolute inset-x-0 top-0 h-[2px]" aria-hidden="true" />
              <div className="flex flex-col items-center text-center">
                <span className="flex h-12 w-12 items-center justify-center rounded-2xl bg-signal-shade text-signal shadow-[0_8px_24px_-6px_rgba(124,123,255,0.4)] ring-1 ring-signal/25">
                  <KeyRound size={22} strokeWidth={1.9} aria-hidden="true" />
                </span>
                <h2 className="mt-4 text-[17px] font-semibold tracking-tight text-ink">
                  {t('Activate exclusive node license')}
                </h2>
                <p className="mt-2 text-[12.5px] leading-relaxed text-ink-dim">
                  {t('Enter the exclusive activation code provided by your administrator to automatically sync dedicated proxy nodes. You can still freely add your own local nodes in Subscriptions & nodes.')}
                </p>
              </div>

              <form onSubmit={handleActivate} className="mt-6 space-y-4">
                <div>
                  <label className="field-label" htmlFor="customer-activation-token">
                    {t('Activation code')}
                  </label>
                  <div className="relative">
                    <input
                      id="customer-activation-token"
                      type={showActivationToken ? 'text' : 'password'}
                      autoComplete="off"
                      spellCheck={false}
                      value={activationToken}
                      onChange={e => {
                        setActivationToken(e.target.value)
                        if (activationError) setActivationError(null)
                      }}
                      placeholder={t('Paste your activation code (e.g. prism_…)')}
                      className="field pr-9 font-mono text-[12.5px]"
                      disabled={activating}
                    />
                    <button
                      type="button"
                      onClick={() => setShowActivationToken(prev => !prev)}
                      aria-label={showActivationToken ? t('Hide activation code') : t('Show activation code')}
                      className="absolute right-2 top-1/2 -translate-y-1/2 cursor-pointer rounded p-1 text-ink-faint hover:bg-raised hover:text-ink"
                    >
                      {showActivationToken ? <EyeOff size={14} aria-hidden="true" /> : <Eye size={14} aria-hidden="true" />}
                    </button>
                  </div>
                </div>

                {activationError && (
                  <div
                    className="flex items-start gap-2 rounded-lg border border-danger/25 bg-danger-shade/60 px-3 py-2 text-[12px] text-danger"
                    role="alert"
                  >
                    <TriangleAlert size={14} className="mt-0.5 shrink-0" aria-hidden="true" />
                    <span>{activationError}</span>
                  </div>
                )}

                <button
                  type="submit"
                  className="btn btn-primary h-[38px] w-full text-[13px]"
                  disabled={!activationToken.trim() || activating}
                >
                  {activating ? (
                    <LoaderCircle size={14} className="animate-spin" aria-hidden="true" />
                  ) : (
                    <ShieldCheck size={14} aria-hidden="true" />
                  )}
                  {activating ? t('Activating & syncing…') : t('Activate license & sync exclusive nodes')}
                </button>
              </form>

              {!status?.require_activation && (
                <div className="mt-5 border-t border-line pt-4 text-center">
                  <Link
                    href="/subscriptions/"
                    className="inline-flex items-center gap-1 text-[12px] font-medium text-ink-dim transition-colors hover:text-signal"
                  >
                    <span>{t('Manage local subscriptions & static IPs')}</span>
                    <ArrowRight size={12} aria-hidden="true" />
                  </Link>
                </div>
              )}
            </section>
          </div>
        ) : (
          <div className="mx-auto max-w-[920px] space-y-6 px-6 py-6">
            {/* License Overview Card */}
            <section className="panel relative overflow-hidden p-6">
              <div className="prism-hairline absolute inset-x-0 top-0 h-[2px] opacity-80" aria-hidden="true" />
              <div className="flex flex-wrap items-start justify-between gap-4">
                <div className="flex items-center gap-3.5">
                  <span
                    className={`flex h-11 w-11 shrink-0 items-center justify-center rounded-2xl ${
                      isEntitlementBlocked(entitlementStatus)
                        ? 'bg-danger-shade text-danger'
                        : entitlementStatus === 'expiring_soon'
                          ? 'bg-warn-shade text-warn'
                          : 'bg-ok-shade text-ok'
                    }`}
                  >
                    <ShieldCheck size={20} aria-hidden="true" />
                  </span>
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <h2 className="truncate text-[16px] font-semibold text-ink">
                        {status?.tenant_name || t('Exclusive customer')}
                      </h2>
                      <span className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-[11px] font-medium ${badge.pillClass}`}>
                        <span className={`h-1.5 w-1.5 rounded-full ${badge.dotClass}`} aria-hidden="true" />
                        {badge.label}
                      </span>
                    </div>
                    <p className="mt-0.5 text-[12px] text-ink-dim">
                      {status?.last_synced_at
                        ? t('Last synced {time}', { time: new Date(status.last_synced_at).toLocaleString(locale) })
                        : t('Never synced')}
                    </p>
                  </div>
                </div>

                <div className="flex flex-wrap items-center gap-2">
                  <button
                    type="button"
                    className="btn btn-default"
                    disabled={syncingScope !== null}
                    onClick={() => triggerSync('all')}
                  >
                    {syncingScope !== null ? (
                      <LoaderCircle size={13} className="animate-spin" aria-hidden="true" />
                    ) : (
                      <RefreshCw size={13} aria-hidden="true" />
                    )}
                    {t('Sync exclusive nodes now')}
                  </button>
                  <button
                    type="button"
                    className="btn btn-default"
                    onClick={() => {
                      setReplacingCode(prev => !prev)
                      setActivationError(null)
                    }}
                  >
                    <KeyRound size={13} aria-hidden="true" />
                    {t('Replace activation code')}
                  </button>
                  <button
                    type="button"
                    className="btn btn-danger"
                    disabled={loggingOut}
                    onClick={() => setConfirmLogout(true)}
                  >
                    <LogOut size={13} aria-hidden="true" />
                    {t('Log out license')}
                  </button>
                </div>
              </div>

              {blockedNote && (
                <div
                  className="mt-4 flex items-start gap-2 rounded-lg border border-danger/25 bg-danger-shade/60 px-3.5 py-2.5 text-[12.5px] text-danger"
                  role="alert"
                >
                  <TriangleAlert size={15} className="mt-0.5 shrink-0" aria-hidden="true" />
                  <span>{blockedNote}</span>
                </div>
              )}

              {!blockedNote && status?.last_error && (
                <div
                  className="mt-4 flex items-start gap-2 rounded-lg border border-warn/25 bg-warn-shade/70 px-3.5 py-2.5 text-[12px] text-warn"
                  role="alert"
                >
                  <TriangleAlert size={14} className="mt-0.5 shrink-0" aria-hidden="true" />
                  <span>{t(status.last_error)}</span>
                </div>
              )}

              {/* Metrics Strip */}
              <div className="mt-5 grid gap-3 sm:grid-cols-3">
                <div className="rounded-xl border border-line bg-canvas/70 p-3.5">
                  <div className="flex items-center gap-1.5 text-[11px] font-medium text-ink-faint">
                    <CalendarClock size={12} aria-hidden="true" />
                    <span>{t('License validity')}</span>
                  </div>
                  <p className="mt-1.5 font-mono text-[15px] font-semibold text-ink">
                    {formattedExpiry ?? t('Permanent')}
                  </p>
                  <p className="mt-0.5 text-[11.5px] text-ink-dim">
                    {formattedExpiry
                      ? entitlementStatus === 'expired'
                        ? t('Expired')
                        : status?.days_remaining != null
                          ? t('{days} days remaining', { days: Math.max(0, status.days_remaining) })
                          : t('Scheduled expiry')
                      : t('No expiration date')}
                  </p>
                </div>

                <div className="rounded-xl border border-line bg-canvas/70 p-3.5">
                  <div className="flex items-center gap-1.5 text-[11px] font-medium text-ink-faint">
                    <Laptop size={12} aria-hidden="true" />
                    <span>{t('Device usage')}</span>
                  </div>
                  <p className="mt-1.5 font-mono text-[15px] font-semibold text-ink">{deviceUsageText}</p>
                  <p className="mt-0.5 text-[11.5px] text-ink-dim">
                    {status?.max_devices && status.max_devices > 0
                      ? t('Active devices linked to this license')
                      : status?.active_devices != null
                        ? t('{count} active devices', { count: status.active_devices })
                        : t('No device count limit')}
                  </p>
                </div>

                <div className="rounded-xl border border-line bg-canvas/70 p-3.5">
                  <div className="flex items-center gap-1.5 text-[11px] font-medium text-ink-faint">
                    <Rss size={12} aria-hidden="true" />
                    <span>{t('Exclusive node pools')}</span>
                  </div>
                  <p className="mt-1.5 font-mono text-[15px] font-semibold text-ink">
                    {t('{count} pools', { count: cloudSubs.length })}
                  </p>
                  <p className="mt-0.5 text-[11.5px] text-ink-dim">
                    {t('{count} active nodes', {
                      count: cloudSubs.reduce((sum, sub) => sum + sub.node_counts.active, 0),
                    })}
                  </p>
                </div>
              </div>

              {/* Inline Replace Activation Code Form */}
              {replacingCode && (
                <form
                  onSubmit={handleActivate}
                  className="mt-5 rounded-xl border border-line bg-sidebar/60 p-4"
                >
                  <div className="flex items-center justify-between gap-2">
                    <label className="field-label mb-0" htmlFor="customer-replace-token">
                      {t('New activation code')}
                    </label>
                    <button
                      type="button"
                      className="text-[11.5px] text-ink-faint hover:text-ink"
                      onClick={() => {
                        setReplacingCode(false)
                        setActivationToken('')
                        setActivationError(null)
                      }}
                    >
                      {t('Cancel')}
                    </button>
                  </div>
                  <div className="mt-2 flex flex-wrap gap-2">
                    <div className="relative min-w-[220px] flex-1">
                      <input
                        id="customer-replace-token"
                        type={showActivationToken ? 'text' : 'password'}
                        autoComplete="off"
                        spellCheck={false}
                        value={activationToken}
                        onChange={e => {
                          setActivationToken(e.target.value)
                          if (activationError) setActivationError(null)
                        }}
                        placeholder={t('Paste your new activation code (e.g. prism_…)')}
                        className="field pr-9 font-mono text-[12.5px]"
                        disabled={activating}
                      />
                      <button
                        type="button"
                        onClick={() => setShowActivationToken(prev => !prev)}
                        aria-label={showActivationToken ? t('Hide activation code') : t('Show activation code')}
                        className="absolute right-2 top-1/2 -translate-y-1/2 cursor-pointer rounded p-1 text-ink-faint hover:bg-raised hover:text-ink"
                      >
                        {showActivationToken ? <EyeOff size={14} aria-hidden="true" /> : <Eye size={14} aria-hidden="true" />}
                      </button>
                    </div>
                    <button
                      type="submit"
                      className="btn btn-primary shrink-0"
                      disabled={!activationToken.trim() || activating}
                    >
                      {activating ? <LoaderCircle size={13} className="animate-spin" aria-hidden="true" /> : <Check size={13} aria-hidden="true" />}
                      {activating ? t('Activating & syncing…') : t('Activate new code')}
                    </button>
                  </div>
                  {activationError && (
                    <p className="mt-2 text-[12px] text-danger" role="alert">
                      {activationError}
                    </p>
                  )}
                </form>
              )}
            </section>

            {/* Synced Exclusive Cloud Node Pools */}
            <section className="panel p-5">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div>
                  <h2 className="text-[14px] font-semibold text-ink">{t('Synced exclusive node pools')}</h2>
                  <p className="mt-0.5 text-[12px] text-ink-dim">
                    {t('Dedicated encrypted proxy pools synced to this license. You can also manage local nodes independently.')}
                  </p>
                </div>
                <div className="flex items-center gap-2">
                  <Link href="/subscriptions/" className="btn btn-default h-[28px] px-2.5 text-[12px]">
                    <Rss size={12} aria-hidden="true" />
                    {t('Subscriptions & nodes')}
                  </Link>
                  <Link href="/map/" className="btn btn-default h-[28px] px-2.5 text-[12px]">
                    <Globe2 size={12} aria-hidden="true" />
                    {t('Node map')}
                  </Link>
                </div>
              </div>

              {cloudSubs.length === 0 ? (
                <div className="mt-4 rounded-xl border border-dashed border-line-strong bg-sidebar/40 px-4 py-8 text-center">
                  <p className="text-[12.5px] font-medium text-ink-dim">
                    {isEntitlementBlocked(entitlementStatus)
                      ? t('Exclusive cloud nodes are paused')
                      : t('No exclusive cloud nodes synced yet')}
                  </p>
                  <p className="mt-1 text-[11.5px] text-ink-faint">
                    {isEntitlementBlocked(entitlementStatus)
                      ? t('Renew your license or enter a new activation code above to restore exclusive nodes.')
                      : t('Click Sync exclusive nodes now above, or ask your administrator to assign node pools.')}
                  </p>
                </div>
              ) : (
                <ul className="mt-4 divide-y divide-line overflow-hidden rounded-xl border border-line">
                  {cloudSubs.map(sub => (
                    <li key={sub.id} className="flex items-center justify-between gap-3 px-4 py-3">
                      <div className="min-w-0">
                        <p className="truncate font-medium text-ink">{sub.name}</p>
                        <p className="mt-0.5 text-[11.5px] text-ink-dim">
                          <span className="font-medium text-ok">{t('{count} active', { count: sub.node_counts.active })}</span>
                          {sub.last_synced_at ? ` · ${new Date(sub.last_synced_at).toLocaleString(locale)}` : ''}
                        </p>
                      </div>
                      <div className="flex shrink-0 items-center gap-1.5">
                        <Link href={`/map/?source=${sub.id}`} className="btn btn-ghost h-[28px] px-2.5 text-[12px]">
                          <Globe2 size={12} aria-hidden="true" />
                          {t('Map')}
                        </Link>
                        <Link href="/subscriptions/" className="btn btn-ghost h-[28px] px-2.5 text-[12px]">
                          <Rss size={12} aria-hidden="true" />
                          {t('Nodes')}
                        </Link>
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </section>

            {/* Only render Cloud Browser Configurations panel if profiles_sync_allowed === true */}
            {status?.profiles_sync_allowed === true && (
              <CloudBrowserConfigsPanel
                profiles={profiles}
                configured={Boolean(status?.configured)}
                pulling={pulling}
                pushingId={pushingId}
                includeCookiesOnPush={includeCookiesOnPush}
                onToggleIncludeCookies={setIncludeCookiesOnPush}
                onPullProfiles={handlePullProfiles}
                onPushProfile={handlePushProfile}
              />
            )}
          </div>
        )}

        <ConfirmDialog
          open={confirmLogout}
          title={t('Log out license')}
          body={t('Log out this activation code on this computer? Synced exclusive cloud nodes will be cleared, while all your local subscriptions and static IPs remain untouched.')}
          confirmLabel={loggingOut ? t('Logging out…') : t('Log out license')}
          destructive
          onCancel={() => { if (!loggingOut) setConfirmLogout(false) }}
          onConfirm={() => { void handleLogout() }}
        />
      </div>
    )
  }

  // ---------------------------------------------------------------------------
  // STANDARD MODE: Full developer / self-hosted Cloud Center UI + tenant strip
  // ---------------------------------------------------------------------------
  const policy = policyData?.policy
  const isCloudPolicy = policyData?.source === 'cloud' || status?.policy_source === 'cloud'
  const hardeningEntries = Object.entries(policy?.hardening_prefs ?? {})
  const enforcedDomainCount = Object.values(policy?.risk_probe_groups ?? {}).reduce((acc, list) => acc + list.length, 0)
  const enforcedCidrCount = policy?.risk_probe_cidrs?.length ?? 0
  const showTenantStrip = Boolean(
    status && (status.tenant_name || status.expires_at || (status.entitlement_status && status.entitlement_status !== 'none')),
  )

  return (
    <div className="min-h-full bg-canvas">
      <header className="sticky top-0 z-20 flex min-h-[54px] flex-wrap items-center gap-3 border-b border-line bg-canvas/85 px-6 py-3 backdrop-blur-xl app-drag">
        <div className="flex items-center gap-2.5 app-no-drag">
          <h1 className="text-[16px] font-semibold tracking-tight text-ink">{t('Cloud center')}</h1>
          {status && (
            <span className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-[11px] font-medium ${
              status.configured ? 'bg-ok-shade text-ok ring-1 ring-ok/20' : 'bg-surface text-ink-dim ring-1 ring-line'
            }`}>
              <span className={`h-1.5 w-1.5 rounded-full ${status.configured ? 'bg-ok-dot' : 'bg-slate-300'}`} />
              {status.configured ? t('Connected') : t('Local baseline mode')}
            </span>
          )}
        </div>

        <div className="ml-auto flex items-center gap-2 app-no-drag">
          <button
            type="button"
            className="btn btn-primary"
            disabled={!status?.configured || syncingScope !== null}
            onClick={() => triggerSync('all')}
          >
            {syncingScope === 'all' ? <LoaderCircle size={13} className="animate-spin" aria-hidden="true" /> : <RefreshCw size={13} aria-hidden="true" />}
            {t('Sync all now')}
          </button>
        </div>
      </header>

      {error ? (
        <EmptyState icon={<Cloud size={18} />} title={t('Cannot load Cloud center')} body={t(error)} action={<button type="button" className="btn btn-default" onClick={load}>{t('Retry')}</button>} />
      ) : loading ? (
        <p className="px-6 py-10 text-ink-dim" role="status">{t('Loading…')}</p>
      ) : (
        <div className="mx-auto max-w-[1040px] space-y-6 px-6 py-6">
          {/* Compact Tenant License Badge Strip (when tenant_name or expires_at is present) */}
          {showTenantStrip && status && (
            <section className="panel flex flex-wrap items-center justify-between gap-3 px-5 py-3.5">
              <div className="flex flex-wrap items-center gap-2.5">
                <span className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-[11px] font-medium ${badge.pillClass}`}>
                  <span className={`h-1.5 w-1.5 rounded-full ${badge.dotClass}`} aria-hidden="true" />
                  {badge.label}
                </span>
                {status.tenant_name && (
                  <span className="text-[13px] font-semibold text-ink">{status.tenant_name}</span>
                )}
                <span className="text-line-strong" aria-hidden="true">·</span>
                <span className="text-[12px] text-ink-dim">
                  {formattedExpiry
                    ? status.days_remaining != null && entitlementStatus !== 'expired'
                      ? t('Expires {date} ({days}d left)', { date: formattedExpiry, days: Math.max(0, status.days_remaining) })
                      : t('Expires {date}', { date: formattedExpiry })
                    : t('Permanent')}
                </span>
                <span className="text-line-strong" aria-hidden="true">·</span>
                <span className="text-[12px] text-ink-dim">
                  {t('Devices: {usage}', { usage: deviceUsageText })}
                </span>
              </div>
              {status.profiles_sync_allowed === false && (
                <span className="rounded-full bg-sidebar px-2.5 py-0.5 text-[11px] text-ink-faint ring-1 ring-line">
                  {t('Browser config sync disabled by tenant')}
                </span>
              )}
            </section>
          )}

          {/* 1. Connection & Sync Config Panel */}
          <section className="panel p-5">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <h2 className="text-[14px] font-semibold text-ink">{t('Cloud server connection')}</h2>
                <p className="mt-0.5 text-[12px] text-ink-dim">
                  {t('Deliver fixed anti-detection policies, managed airport subscriptions, and encrypted browser configs from your cloud server.')}
                </p>
              </div>
              {status?.last_synced_at && (
                <span className="text-[11.5px] text-ink-faint">
                  {t('Last synced {time}', { time: new Date(status.last_synced_at).toLocaleString(locale) })}
                </span>
              )}
            </div>

            {status?.last_error && (
              <div className="mt-3 flex items-start gap-2 rounded-lg border border-danger/25 bg-danger-shade/50 px-3 py-2 text-[12px] text-danger" role="alert">
                <TriangleAlert size={14} className="mt-0.5 shrink-0" aria-hidden="true" />
                <span>{t(status.last_error)}</span>
              </div>
            )}

            <form onSubmit={saveConfig} className="mt-4 space-y-4">
              <div className="grid gap-3 md:grid-cols-2">
                <div>
                  <label className="field-label" htmlFor="cloud-url">{t('Cloud server URL')}</label>
                  <input
                    id="cloud-url"
                    type="url"
                    className="field font-mono text-[12.5px]"
                    value={baseUrl}
                    onChange={e => setBaseUrl(e.target.value)}
                    placeholder="https://cloud.example.com"
                    spellCheck={false}
                  />
                </div>
                <div>
                  <label className="field-label" htmlFor="cloud-token">{t('Bearer token (encrypted at rest)')}</label>
                  <div className="flex gap-2">
                    <input
                      id="cloud-token"
                      type="password"
                      autoComplete="off"
                      className="field font-mono text-[12.5px]"
                      value={token}
                      onChange={e => setToken(e.target.value)}
                      placeholder={status?.has_token ? t('Token stored — type to replace') : t('Optional access token')}
                    />
                    {status?.has_token && (
                      <button type="button" className="btn btn-ghost shrink-0 text-[12px]" disabled={saving} onClick={clearToken}>
                        {t('Clear')}
                      </button>
                    )}
                  </div>
                </div>
              </div>

              <div className="grid gap-3 md:grid-cols-3">
                <div>
                  <label className="field-label" htmlFor="cloud-device-label">{t('Device label')}</label>
                  <input
                    id="cloud-device-label"
                    className="field text-[12.5px]"
                    value={deviceLabel}
                    onChange={e => setDeviceLabel(e.target.value)}
                    placeholder={t('e.g. MacBook Pro Office')}
                  />
                </div>
                <label className="flex cursor-pointer items-center gap-2 self-end rounded-lg border border-line bg-sidebar/60 px-3 py-2 text-[12.5px]">
                  <input type="checkbox" checked={autoSync} onChange={e => setAutoSync(e.target.checked)} />
                  <span>{t('Auto-sync on startup')}</span>
                </label>
                <label className="flex cursor-pointer items-center gap-2 self-end rounded-lg border border-line bg-sidebar/60 px-3 py-2 text-[12.5px]">
                  <input type="checkbox" checked={syncProfiles} onChange={e => setSyncProfiles(e.target.checked)} />
                  <span>{t('Enable browser config sync')}</span>
                </label>
              </div>

              <div className="flex flex-wrap items-center justify-between gap-3 pt-1">
                <button type="submit" className="btn btn-primary" disabled={saving}>
                  {saving ? <LoaderCircle size={13} className="animate-spin" aria-hidden="true" /> : <Check size={13} aria-hidden="true" />}
                  {t('Save configuration')}
                </button>
                <label className="flex cursor-pointer items-center gap-2 text-[12px] text-ink-dim">
                  <input type="checkbox" checked={allowLocal} onChange={e => setAllowLocal(e.target.checked)} />
                  <span>{t('Allow local HTTP cloud server (127.0.0.1 / localhost)')}</span>
                </label>
              </div>
            </form>
          </section>

          {/* 2. Policy & Cloud Subscriptions Grid */}
          <div className="grid gap-5 lg:grid-cols-2">
            {/* Active Fixed Optimization Policy Card */}
            <section className="panel flex flex-col p-5">
              <div className="flex items-start justify-between gap-3">
                <div className="flex items-center gap-2">
                  <span className="flex h-8 w-8 items-center justify-center rounded-xl bg-ok-shade text-ok">
                    <ShieldCheck size={16} aria-hidden="true" />
                  </span>
                  <div>
                    <h2 className="text-[14px] font-semibold text-ink">{t('Active isolation policy')}</h2>
                    <p className="text-[11.5px] text-ink-dim">
                      {isCloudPolicy ? t('Delivered from cloud · cached offline') : t('Built-in fixed optimization baseline')}
                    </p>
                  </div>
                </div>
                <button
                  type="button"
                  className="btn btn-default h-[28px] px-2.5 text-[12px]"
                  disabled={!status?.configured || syncingScope !== null}
                  onClick={() => triggerSync('policy')}
                >
                  {syncingScope === 'policy' ? <LoaderCircle size={12} className="animate-spin" /> : <Sparkles size={12} className="text-signal" />}
                  {t('Update policy')}
                </button>
              </div>

              <div className="mt-4 flex flex-wrap items-center gap-2">
                <span className="rounded-lg bg-sidebar px-2.5 py-1 font-mono text-[12px] font-semibold text-ink ring-1 ring-line">
                  {policy?.policy_version ?? status?.policy_version ?? 'builtin'}
                </span>
                <span className={`rounded-full px-2.5 py-0.5 text-[11px] font-medium ${isCloudPolicy ? 'bg-ok-shade text-ok' : 'bg-signal-shade text-signal'}`}>
                  {isCloudPolicy ? t('Cloud policy') : t('Built-in baseline')}
                </span>
                {policyData?.sha256 && (
                  <span className="font-mono text-[11px] text-ink-faint" title={policyData.sha256}>
                    SHA {policyData.sha256.slice(0, 10)}
                  </span>
                )}
              </div>

              {/* Fixed Defaults Summary */}
              <dl className="mt-4 grid grid-cols-2 gap-x-4 gap-y-2 rounded-xl border border-line bg-sidebar/60 p-3.5 text-[12px]">
                <div>
                  <dt className="text-ink-faint">{t('Default timezone')}</dt>
                  <dd className="font-mono font-medium text-ink">
                    {policy?.default_timezone ?? 'America/Los_Angeles'} ({policy?.default_timezone_mode === 'follow_exit' ? t('Follow exit') : t('Fixed')})
                  </dd>
                </div>
                <div>
                  <dt className="text-ink-faint">{t('Default language')}</dt>
                  <dd className="font-mono font-medium text-ink">
                    {(policy?.default_languages ?? ['en-US', 'en']).join(', ')}
                  </dd>
                </div>
                <div>
                  <dt className="text-ink-faint">{t('WebRTC / DNS guard')}</dt>
                  <dd className="font-medium text-ok">
                    {policy?.install_webrtc_guard === false ? t('Disabled') : t('Bundled extension enabled')}
                  </dd>
                </div>
                <div>
                  <dt className="text-ink-faint">{t('Traffic tunnel')}</dt>
                  <dd className="font-medium text-ok">{t('Full-traffic fail-closed')}</dd>
                </div>
                <div>
                  <dt className="text-ink-faint">{t('Default tier / WebRTC')}</dt>
                  <dd className="font-mono text-ink">
                    {policy?.default_tier ?? 'strict'} · {policy?.default_webrtc_policy ?? 'block'}
                  </dd>
                </div>
                <div>
                  <dt className="text-ink-faint">{t('Hardening prefs')}</dt>
                  <dd className="font-mono text-ink">
                    {t('{count} locked rules', { count: hardeningEntries.length })}
                  </dd>
                </div>
                <div className="col-span-2">
                  <dt className="text-ink-faint">{t('Enforced risk domain suite')}</dt>
                  <dd className="font-mono font-medium text-ink">
                    {t('{domains} enforced domains · {cidrs} IP CIDRs', {
                      domains: enforcedDomainCount,
                      cidrs: enforcedCidrCount,
                    })}
                  </dd>
                </div>
              </dl>

              {hardeningEntries.length > 0 && (
                <details className="mt-3 text-[11.5px] text-ink-dim">
                  <summary className="cursor-pointer font-medium hover:text-ink">{t('Inspect hardening preferences')}</summary>
                  <ul className="mt-2 max-h-[160px] space-y-1 overflow-y-auto rounded-lg border border-line bg-sidebar/50 p-2.5 font-mono text-[11px]">
                    {hardeningEntries.map(([k, v]) => (
                      <li key={k} className="flex justify-between gap-2">
                        <span className="truncate text-ink-dim">{k}</span>
                        <span className="shrink-0 text-ink">{String(v)}</span>
                      </li>
                    ))}
                  </ul>
                </details>
              )}
            </section>

            {/* Cloud-Managed Subscriptions Card */}
            <section className="panel flex flex-col p-5">
              <div className="flex items-start justify-between gap-3">
                <div className="flex items-center gap-2">
                  <span className="flex h-8 w-8 items-center justify-center rounded-xl bg-signal-shade text-signal">
                    <Rss size={16} aria-hidden="true" />
                  </span>
                  <div>
                    <h2 className="text-[14px] font-semibold text-ink">{t('Cloud-managed subscriptions')}</h2>
                    <p className="text-[11.5px] text-ink-dim">
                      {t('Airport node sources pushed by cloud; local subscriptions remain untouched.')}
                    </p>
                  </div>
                </div>
                <button
                  type="button"
                  className="btn btn-default h-[28px] px-2.5 text-[12px]"
                  disabled={!status?.configured || syncingScope !== null}
                  onClick={() => triggerSync('subscriptions')}
                >
                  {syncingScope === 'subscriptions' ? <LoaderCircle size={12} className="animate-spin" /> : <RefreshCw size={12} />}
                  {t('Sync subscriptions')}
                </button>
              </div>

              {cloudSubs.length === 0 ? (
                <div className="mt-4 flex flex-1 flex-col items-center justify-center rounded-xl border border-dashed border-line-strong bg-sidebar/40 px-4 py-8 text-center">
                  <p className="text-[12.5px] font-medium text-ink-dim">{t('No cloud subscriptions synced yet')}</p>
                  <p className="mt-1 text-[11.5px] text-ink-faint">{t('Configure a cloud server above and click Sync subscriptions.')}</p>
                </div>
              ) : (
                <ul className="mt-4 divide-y divide-line overflow-hidden rounded-xl border border-line">
                  {cloudSubs.map(sub => (
                    <li key={sub.id} className="flex items-center justify-between gap-3 px-3.5 py-2.5">
                      <div className="min-w-0">
                        <p className="truncate font-medium text-ink">{sub.name}</p>
                        <p className="text-[11px] text-ink-dim">
                          <span className="text-ok">{t('{count} active', { count: sub.node_counts.active })}</span>
                          {sub.last_synced_at ? ` · ${new Date(sub.last_synced_at).toLocaleString(locale)}` : ''}
                        </p>
                      </div>
                      <Link href={`/map/?source=${sub.id}`} className="btn btn-ghost h-[28px] px-2 text-[12px]">
                        <Globe2 size={12} aria-hidden="true" />
                        {t('Map')}
                      </Link>
                    </li>
                  ))}
                </ul>
              )}
            </section>
          </div>

          {/* 3. Cloud Browser Config Push / Pull Panel */}
          <CloudBrowserConfigsPanel
            profiles={profiles}
            configured={Boolean(status?.configured)}
            pulling={pulling}
            pushingId={pushingId}
            includeCookiesOnPush={includeCookiesOnPush}
            onToggleIncludeCookies={setIncludeCookiesOnPush}
            onPullProfiles={handlePullProfiles}
            onPushProfile={handlePushProfile}
          />
        </div>
      )}
    </div>
  )
}

function CloudBrowserConfigsPanel({
  profiles,
  configured,
  pulling,
  pushingId,
  includeCookiesOnPush,
  onToggleIncludeCookies,
  onPullProfiles,
  onPushProfile,
}: {
  profiles: Profile[]
  configured: boolean
  pulling: boolean
  pushingId: string | null
  includeCookiesOnPush: boolean
  onToggleIncludeCookies: (next: boolean) => void
  onPullProfiles: () => void
  onPushProfile: (profile: Profile) => void
}) {
  const { t, locale } = useI18n()
  return (
    <section className="panel overflow-hidden">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-line px-5 py-4">
        <div>
          <h2 className="text-[14px] font-semibold text-ink">{t('Cloud browser configurations')}</h2>
          <p className="mt-0.5 text-[12px] text-ink-dim">
            {t('Push pinned fingerprints, memos, node bindings, and optional client-encrypted cookies to cloud, or pull onto another device. Running browsers are never overwritten.')}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <label className="flex cursor-pointer items-center gap-1.5 text-[12px] text-ink-dim">
            <input type="checkbox" checked={includeCookiesOnPush} onChange={e => onToggleIncludeCookies(e.target.checked)} />
            <span>{t('Include encrypted cookies when pushing')}</span>
          </label>
          <button
            type="button"
            className="btn btn-primary"
            disabled={!configured || pulling}
            onClick={onPullProfiles}
          >
            {pulling ? <LoaderCircle size={13} className="animate-spin" aria-hidden="true" /> : <CloudDownload size={14} aria-hidden="true" />}
            {t('Pull configs from cloud')}
          </button>
        </div>
      </div>

      {profiles.length === 0 ? (
        <p className="px-5 py-8 text-center text-[12.5px] text-ink-dim">{t('No local browsers yet. Pull from cloud or create one on the Browsers page.')}</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[700px] border-collapse text-left">
            <thead>
              <tr className="border-b border-line bg-sidebar/50 text-[11px] font-medium uppercase tracking-[0.04em] text-ink-faint">
                <th className="px-5 py-2">{t('Browser')}</th>
                <th className="px-3 py-2">{t('Fingerprint')}</th>
                <th className="px-3 py-2">{t('Cloud revision')}</th>
                <th className="px-3 py-2">{t('Last synced')}</th>
                <th className="px-5 py-2 text-right">{t('Action')}</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {profiles.map(profile => {
                const fp = profileFingerprintHash(profile)
                const osName = profile.isolation_summary?.os ?? profile.isolation?.os ?? profile.browser_settings?.os ?? 'macos'
                const busy = pushingId === profile.id
                return (
                  <tr key={profile.id} className="transition-colors hover:bg-raised/45">
                    <td className="px-5 py-2.5 font-medium text-ink">{profile.name}</td>
                    <td className="px-3 py-2.5 font-mono text-[11.5px] text-ink-dim">
                      {fp ?? '—'} · {OS_LABELS[osName] ?? osName}
                    </td>
                    <td className="px-3 py-2.5 font-mono text-[12px] text-ink-dim">
                      {profile.cloud_revision ? `rev ${profile.cloud_revision}` : '—'}
                    </td>
                    <td className="px-3 py-2.5 text-[12px] text-ink-dim">
                      {profile.cloud_synced_at ? new Date(profile.cloud_synced_at).toLocaleString(locale) : t('Not pushed')}
                    </td>
                    <td className="px-5 py-2.5 text-right">
                      <button
                        type="button"
                        className="btn btn-default h-[28px] px-2.5 text-[12px]"
                        disabled={!configured || busy}
                        onClick={() => onPushProfile(profile)}
                      >
                        {busy ? <LoaderCircle size={12} className="animate-spin" aria-hidden="true" /> : <CloudUpload size={12} aria-hidden="true" />}
                        {t('Push to cloud')}
                      </button>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}
