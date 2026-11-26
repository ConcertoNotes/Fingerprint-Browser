'use client'

import { useCallback, useEffect, useState } from 'react'
import Link from 'next/link'
import { Activity, Check, Globe2, LoaderCircle, RefreshCw, ShieldAlert, ShieldCheck, Sparkles, TriangleAlert, X } from 'lucide-react'

import { useI18n } from '@/components/i18n-provider'
import { Modal } from '@/components/modal'
import { useToast } from '@/components/toast'
import {
  profilesAPI,
  type IsolationAuditCheck,
  type IsolationAuditReport,
  type Profile,
  type RiskDomainProbeItem,
  type RiskDomainProbeReport,
} from '@/lib/api'
import { networkAPI } from '@/lib/network-api'

interface Props {
  profile: Profile | null
  onClose: () => void
  onChanged?: () => void | Promise<void>
}

const CATEGORY_LABELS: Record<string, string> = {
  core: 'Core service domains',
  antifraud_telemetry: 'Anti-fraud & payments',
  challenge_kyc: 'Challenges & KYC',
}

const CHECK_KEY_FALLBACK_TITLES: Record<string, string> = {
  risk_domains_enforced: 'Risk & verification domain enforcement',
}

export function IsolationAuditModal({ profile, onClose, onChanged }: Props) {
  if (!profile) return null
  return <IsolationAuditContent key={profile.id} profile={profile} onClose={onClose} onChanged={onChanged} />
}

function IsolationAuditContent({
  profile,
  onClose,
  onChanged,
}: {
  profile: Profile
  onClose: () => void
  onChanged?: () => void | Promise<void>
}) {
  const { t, locale } = useI18n()
  const toast = useToast()
  const [report, setReport] = useState<IsolationAuditReport | null>(null)
  const [loading, setLoading] = useState(true)
  const [fixingKey, setFixingKey] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const [probeReport, setProbeReport] = useState<RiskDomainProbeReport | null>(null)
  const [probing, setProbing] = useState(false)
  const [probeError, setProbeError] = useState<string | null>(null)

  const runAudit = useCallback(async (profileId: string) => {
    setLoading(true)
    setError(null)
    try {
      const data = await profilesAPI.auditProfile(profileId)
      setReport(data)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    let active = true
    profilesAPI
      .auditProfile(profile.id)
      .then(data => { if (active) setReport(data) })
      .catch(err => { if (active) setError(err instanceof Error ? err.message : String(err)) })
      .finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [profile.id])

  async function runRiskProbe() {
    if (probing) return
    setProbing(true)
    setProbeError(null)
    try {
      const data = await profilesAPI.probeRiskDomains(profile.id)
      setProbeReport(data)
    } catch (err) {
      setProbeError(t(err instanceof Error ? err.message : String(err)))
    } finally {
      setProbing(false)
    }
  }

  async function applyAction(check: IsolationAuditCheck) {
    setFixingKey(check.key)
    try {
      const act = check.action ?? inferAction(check)
      if (act === 'reoptimize') {
        await profilesAPI.reoptimizeProfile(profile.id)
        toast('ok', t('Cloud isolation policy applied'), profile.name)
      } else if (act === 'refresh_browser') {
        await profilesAPI.refreshBrowserVersion(profile.id)
        toast('ok', t('Browser version refreshed'), profile.name)
      } else if (act === 'reset_fingerprint') {
        await profilesAPI.resetFingerprint(profile.id)
        toast('ok', t('Deduplicated real device reassigned'), profile.name)
      } else if (act === 'reconcile_os') {
        await profilesAPI.reconcileOs(profile.id, true)
        toast('ok', t('OS setting reconciled'), profile.name)
      } else if (act === 'reconnect') {
        await networkAPI.reconnect(profile.id)
        toast('ok', t('Reconnected'), t('{profile} is back on the same node.', { profile: profile.name }))
      }
      await onChanged?.()
      await runAudit(profile.id)
    } catch (err) {
      toast('error', t('Action failed'), t(err instanceof Error ? err.message : String(err)))
    } finally {
      setFixingKey(null)
    }
  }

  const score = report?.score ?? 0
  const scoreTone = score >= 85 ? 'ok' : score >= 60 ? 'warn' : 'danger'
  const passCount = report?.checks.filter(c => c.status === 'pass').length ?? 0
  const warnCount = report?.checks.filter(c => c.status === 'warn').length ?? 0
  const failCount = report?.checks.filter(c => c.status === 'fail').length ?? 0
  const needsReoptimize = report?.checks.some(
    c =>
      c.status !== 'pass' &&
      (c.action === 'reoptimize' ||
        c.key === 'cloud_policy_fresh' ||
        c.key === 'leak_protection' ||
        c.key === 'webrtc_guard_addon' ||
        c.key === 'risk_domains_enforced'),
  )

  const groupedProbes = groupProbesByCategory(probeReport?.probes ?? [])

  return (
    <Modal
      open
      title={t('Isolation health audit')}
      subtitle={profile.name}
      width={720}
      onClose={onClose}
      footer={
        <>
          {needsReoptimize && (
            <button
              type="button"
              className="btn btn-default mr-auto"
              disabled={fixingKey !== null || loading}
              onClick={() => applyAction({ key: 'cloud_policy_fresh', title: '', status: 'warn', summary: '', action: 'reoptimize' })}
            >
              {fixingKey === 'cloud_policy_fresh' ? <LoaderCircle size={13} className="animate-spin" aria-hidden="true" /> : <Sparkles size={13} className="text-signal" aria-hidden="true" />}
              {t('Apply latest cloud policy')}
            </button>
          )}
          <button
            type="button"
            className="btn btn-default"
            disabled={loading || fixingKey !== null}
            onClick={() => runAudit(profile.id)}
          >
            <RefreshCw size={13} className={loading ? 'animate-spin' : ''} aria-hidden="true" />
            {t('Re-check')}
          </button>
          <button type="button" className="btn btn-primary" onClick={onClose}>
            {t('Done')}
          </button>
        </>
      }
    >
      {loading && !report ? (
        <div className="flex flex-col items-center justify-center gap-3 py-14 text-ink-dim">
          <LoaderCircle size={24} className="animate-spin text-signal" aria-hidden="true" />
          <p>{t('Running isolation checks…')}</p>
        </div>
      ) : error ? (
        <div className="rounded-xl border border-danger/25 bg-danger-shade/50 p-4 text-danger" role="alert">
          <p className="font-medium">{t('Audit failed')}</p>
          <p className="mt-1 text-[12px]">{t(error)}</p>
        </div>
      ) : report ? (
        <div className="space-y-4">
          {/* Score Hero Header */}
          <div className="flex items-center gap-5 rounded-xl border border-line bg-canvas/70 p-4">
            <ScoreRing score={score} tone={scoreTone} />
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-2">
                <span className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-[12px] font-medium ${
                  scoreTone === 'ok'
                    ? 'bg-ok-shade text-ok'
                    : scoreTone === 'warn'
                      ? 'bg-warn-shade text-warn'
                      : 'bg-danger-shade text-danger'
                }`}>
                  {scoreTone === 'ok' ? <ShieldCheck size={13} aria-hidden="true" /> : <ShieldAlert size={13} aria-hidden="true" />}
                  {scoreTone === 'ok' ? t('Isolation healthy') : scoreTone === 'warn' ? t('Needs attention') : t('Isolation at risk')}
                </span>
                <span className="text-[11px] text-ink-faint">
                  {t('Checked {time}', { time: new Date(report.checked_at).toLocaleTimeString(locale) })}
                </span>
              </div>

              <div className="mt-2.5 flex flex-wrap gap-3 text-[12px]">
                <span className="inline-flex items-center gap-1.5 text-ok">
                  <span className="h-2 w-2 rounded-full bg-ok-dot" aria-hidden="true" />
                  {t('{count} passed', { count: passCount })}
                </span>
                {warnCount > 0 && (
                  <span className="inline-flex items-center gap-1.5 text-warn">
                    <span className="h-2 w-2 rounded-full bg-warn-dot" aria-hidden="true" />
                    {t('{count} warnings', { count: warnCount })}
                  </span>
                )}
                {failCount > 0 && (
                  <span className="inline-flex items-center gap-1.5 text-danger">
                    <span className="h-2 w-2 rounded-full bg-danger-dot" aria-hidden="true" />
                    {t('{count} failed', { count: failCount })}
                  </span>
                )}
              </div>

              <p className="mt-1.5 text-[11.5px] text-ink-dim">
                {t('Checks hardware fingerprint uniqueness, storage isolation, full-traffic isolated proxy tunnel, WebRTC/DNS guard, and cloud optimization policy.')}
              </p>
            </div>
          </div>

          {/* Detailed Checks List */}
          <ul className="divide-y divide-line overflow-hidden rounded-xl border border-line bg-surface">
            {report.checks.map(check => {
              const action = check.status !== 'pass' ? (check.action ?? inferAction(check)) : null
              const busy = fixingKey === check.key
              return (
                <li key={check.key} className="flex items-start gap-3 px-4 py-3">
                  <span
                    className={`mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full ${
                      check.status === 'pass'
                        ? 'bg-ok-shade text-ok'
                        : check.status === 'warn'
                          ? 'bg-warn-shade text-warn'
                          : 'bg-danger-shade text-danger'
                    }`}
                    aria-hidden="true"
                  >
                    {check.status === 'pass' ? <Check size={12} strokeWidth={2.5} /> : check.status === 'warn' ? <TriangleAlert size={11} strokeWidth={2.3} /> : <X size={12} strokeWidth={2.5} />}
                  </span>

                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <p className="font-medium text-ink">
                        {(() => {
                          const translated = t(check.title)
                          if (translated !== check.title) return translated
                          const fallback = CHECK_KEY_FALLBACK_TITLES[check.key]
                          return fallback ? t(fallback) : check.title
                        })()}
                      </p>
                      {action === 'bind_node' || action === 'switch_node' ? (
                        <Link
                          href={`/map/?profile=${encodeURIComponent(profile.id)}`}
                          onClick={onClose}
                          className="btn btn-default h-[26px] px-2.5 text-[11.5px]"
                        >
                          <Globe2 size={12} aria-hidden="true" />
                          {t('Choose node on map')}
                        </Link>
                      ) : action && (
                        <button
                          type="button"
                          className="btn btn-default h-[26px] px-2.5 text-[11.5px]"
                          disabled={busy || fixingKey !== null}
                          onClick={() => applyAction(check)}
                        >
                          {busy ? <LoaderCircle size={12} className="animate-spin" aria-hidden="true" /> : <Sparkles size={12} className="text-signal" aria-hidden="true" />}
                          {action === 'reoptimize'
                            ? t('Apply latest cloud policy')
                            : action === 'refresh_browser'
                              ? t('Refresh browser version')
                              : action === 'reconcile_os'
                                ? t('Fix OS mismatch')
                                : action === 'reconnect'
                                  ? t('Reconnect')
                                  : t('Reassign real device')}
                        </button>
                      )}
                    </div>
                    <p className="mt-0.5 text-[12px] text-ink-dim">{t(check.summary)}</p>
                    {check.detail && (
                      <p className="mt-0.5 font-mono text-[11px] text-ink-faint">{t(check.detail)}</p>
                    )}
                  </div>
                </li>
              )
            })}
          </ul>

          {/* Live Risk & Verification Domain Probe Panel */}
          <section className="rounded-xl border border-line bg-sidebar/55 p-4" aria-label={t('Risk & verification domain probe')}>
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <h3 className="text-[13px] font-semibold text-ink">{t('Risk & verification domain probe')}</h3>
                <p className="mt-0.5 text-[11.5px] text-ink-dim">
                  {t('Probe core services, anti-fraud/payment telemetry, and challenge/KYC domains directly through this browser exit tunnel.')}
                </p>
              </div>
              <button
                type="button"
                className="btn btn-default h-[30px] px-3 text-[12px]"
                disabled={probing}
                onClick={runRiskProbe}
              >
                {probing ? <LoaderCircle size={13} className="animate-spin" aria-hidden="true" /> : <Activity size={13} className="text-signal" aria-hidden="true" />}
                {probing ? t('Probing domains…') : t('Probe risk domains live')}
              </button>
            </div>

            {probeError && (
              <p className="mt-2.5 rounded-lg border border-danger/25 bg-danger-shade/50 px-3 py-2 text-[12px] text-danger" role="alert">
                {probeError}
              </p>
            )}

            {probeReport && (
              <div className="mt-3 space-y-3">
                <div className="flex flex-wrap items-center gap-2 text-[11.5px]">
                  <span className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 font-medium ${
                    probeReport.summary.ok === probeReport.summary.total
                      ? 'bg-ok-shade text-ok'
                      : probeReport.summary.ok > 0
                        ? 'bg-warn-shade text-warn'
                        : 'bg-danger-shade text-danger'
                  }`}>
                    <span className={`h-1.5 w-1.5 rounded-full ${
                      probeReport.summary.ok === probeReport.summary.total
                        ? 'bg-ok-dot'
                        : probeReport.summary.ok > 0
                          ? 'bg-warn-dot'
                          : 'bg-danger-dot'
                    }`} />
                    {t('{ok}/{total} reachable', { ok: probeReport.summary.ok, total: probeReport.summary.total })}
                  </span>
                  {probeReport.exit_ip && (
                    <span className="rounded-md bg-surface px-2 py-0.5 font-mono text-[11px] text-ink ring-1 ring-line">
                      {probeReport.exit_ip}
                    </span>
                  )}
                  <span className="rounded-md bg-surface px-2 py-0.5 text-[11px] text-ink-dim ring-1 ring-line">
                    {t('{domains} enforced domains · {cidrs} IP CIDRs', {
                      domains: probeReport.total_enforced_domains,
                      cidrs: probeReport.total_enforced_cidrs,
                    })}
                  </span>
                </div>

                {/* Grouped Live Probe Results */}
                <div className="space-y-2.5">
                  {groupedProbes.map(([category, items]) => (
                    <div key={category} className="rounded-lg border border-line bg-surface p-2.5">
                      <p className="mb-1.5 text-[11px] font-semibold text-ink-dim">
                        {t(CATEGORY_LABELS[category] ?? category)}
                      </p>
                      <div className="grid gap-1.5 sm:grid-cols-2">
                        {items.map(item => (
                          <div
                            key={item.domain}
                            className="flex items-center justify-between gap-2 rounded-md border border-line/80 bg-sidebar/40 px-2.5 py-1.5 text-[11.5px]"
                            title={item.error ?? undefined}
                          >
                            <span className="flex min-w-0 items-center gap-1.5">
                              <span
                                className={`h-1.5 w-1.5 shrink-0 rounded-full ${item.reachable ? 'bg-ok-dot' : 'bg-danger-dot'}`}
                                aria-hidden="true"
                              />
                              <span className="truncate font-mono text-ink">{item.domain}</span>
                            </span>
                            <span className="shrink-0 font-mono text-[10.5px] text-ink-dim">
                              {item.reachable
                                ? [item.status_code ? String(item.status_code) : 'OK', item.latency_ms != null ? `${item.latency_ms} ms` : null].filter(Boolean).join(' · ')
                                : t('Failed')}
                            </span>
                          </div>
                        ))}
                      </div>
                    </div>
                  ))}
                </div>

                {/* Full Enforced 36 Domains + 4 CIDRs List */}
                <details className="rounded-lg border border-line bg-surface px-3 py-2 text-[11.5px] text-ink-dim">
                  <summary className="cursor-pointer font-medium text-ink hover:text-signal">
                    {t('View all {domains} enforced risk domains & {cidrs} IP CIDRs', {
                      domains: probeReport.total_enforced_domains,
                      cidrs: probeReport.total_enforced_cidrs,
                    })}
                  </summary>
                  <div className="mt-2.5 space-y-2.5">
                    {Object.entries(probeReport.risk_probe_groups ?? {}).map(([groupKey, domains]) => (
                      <div key={groupKey}>
                        <p className="mb-1 text-[11px] font-medium text-ink">
                          {t(CATEGORY_LABELS[groupKey] ?? groupKey)} ({domains.length})
                        </p>
                        <div className="flex flex-wrap gap-1">
                          {domains.map(domain => (
                            <span key={domain} className="rounded bg-sidebar px-1.5 py-0.5 font-mono text-[10.5px] text-ink-dim ring-1 ring-line">
                              {domain}
                            </span>
                          ))}
                        </div>
                      </div>
                    ))}
                    {(probeReport.risk_probe_cidrs ?? []).length > 0 && (
                      <div>
                        <p className="mb-1 text-[11px] font-medium text-ink">
                          {t('Enforced IP CIDRs')} ({probeReport.risk_probe_cidrs.length})
                        </p>
                        <div className="flex flex-wrap gap-1">
                          {probeReport.risk_probe_cidrs.map(cidr => (
                            <span key={cidr} className="rounded bg-signal-shade/60 px-1.5 py-0.5 font-mono text-[10.5px] text-signal">
                              {cidr}
                            </span>
                          ))}
                        </div>
                      </div>
                    )}
                  </div>
                </details>
              </div>
            )}
          </section>
        </div>
      ) : null}
    </Modal>
  )
}

function groupProbesByCategory(probes: RiskDomainProbeItem[]): [string, RiskDomainProbeItem[]][] {
  const map = new Map<string, RiskDomainProbeItem[]>()
  for (const item of probes) {
    const key = item.category || 'core'
    const list = map.get(key) ?? []
    list.push(item)
    map.set(key, list)
  }
  const order = ['core', 'antifraud_telemetry', 'challenge_kyc']
  return [...map.entries()].sort(([a], [b]) => {
    const ia = order.indexOf(a)
    const ib = order.indexOf(b)
    if (ia !== -1 && ib !== -1) return ia - ib
    if (ia !== -1) return -1
    if (ib !== -1) return 1
    return a.localeCompare(b)
  })
}

function inferAction(check: IsolationAuditCheck): string | null {
  switch (check.key) {
    case 'cloud_policy_fresh':
    case 'leak_protection':
    case 'webrtc_guard_addon':
    case 'full_traffic_tunnel':
    case 'risk_domains_enforced':
      return 'reoptimize'
    case 'browser_version':
      return 'refresh_browser'
    case 'node_bound_and_alive':
    case 'exit_ip_exclusivity':
      return 'switch_node'
    case 'fingerprint_unique':
    case 'fingerprint_pinned':
      return 'reset_fingerprint'
    default:
      return null
  }
}

function ScoreRing({ score, tone }: { score: number; tone: 'ok' | 'warn' | 'danger' }) {
  const clamped = Math.max(0, Math.min(100, Math.round(score)))
  const radius = 32
  const circumference = 2 * Math.PI * radius
  const offset = circumference * (1 - clamped / 100)
  const stroke = tone === 'ok' ? '#10b981' : tone === 'warn' ? '#f59e0b' : '#ef4444'
  const textTone = tone === 'ok' ? 'text-ok' : tone === 'warn' ? 'text-warn' : 'text-danger'

  return (
    <div className="relative flex h-[78px] w-[78px] shrink-0 items-center justify-center">
      <svg width="78" height="78" viewBox="0 0 78 78" className="-rotate-90" aria-hidden="true">
        <circle cx="39" cy="39" r={radius} fill="none" stroke="var(--prism-line-strong)" strokeWidth="6" />
        <circle
          cx="39"
          cy="39"
          r={radius}
          fill="none"
          stroke={stroke}
          strokeWidth="6"
          strokeLinecap="round"
          strokeDasharray={circumference}
          strokeDashoffset={offset}
          style={{
            transition: 'stroke-dashoffset 0.45s cubic-bezier(0.16, 1, 0.3, 1)',
            filter: `drop-shadow(0 0 6px ${stroke}55)`,
          }}
        />
      </svg>
      <div className="absolute inset-0 flex flex-col items-center justify-center">
        <span className={`font-mono text-[20px] font-semibold leading-none ${textTone}`}>{clamped}</span>
        <span className="mt-0.5 text-[9.5px] font-medium text-ink-faint">/ 100</span>
      </div>
    </div>
  )
}
