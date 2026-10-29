'use client'

import { useState } from 'react'
import { Activity, Check, Copy, ExternalLink, Globe, Link2, LoaderCircle, RefreshCw, ShieldCheck, TriangleAlert } from 'lucide-react'

import { useI18n } from '@/components/i18n-provider'
import type { CheckSite, IntelSignal, NetworkNode } from '@/lib/network-api'
import { countryName, distanceKm, flag, gradeLabel, lookupUrl, maskIp, purityTone } from '@/lib/network-map'

const TONE_TEXT = { ok: 'text-ok', warn: 'text-warn', danger: 'text-danger', unknown: 'text-ink-dim' } as const
const TONE_BAR = { ok: 'bg-ok', warn: 'bg-warn', danger: 'bg-danger', unknown: 'bg-line-strong' } as const

const SIGNAL_TEXT: Record<IntelSignal['code'], string> = {
  datacenter: 'Datacenter / hosting network',
  residential: 'Residential network',
  broadcast: 'Broadcast IP (announced outside its registered region)',
  native: 'Native IP (registered where it is used)',
  vpn: 'Listed as a VPN',
  proxy: 'Listed as a proxy',
  tor: 'Tor exit',
  relay: 'Privacy relay',
  high_fraud: 'High fraud score',
  ip_mismatch: 'A provider saw a different exit IP; the exit may change per connection',
}

const SOURCE_LABEL: Record<string, string> = { ipinfo: 'ipinfo.io', ippure: 'IPPure', scamalytics: 'Scamalytics' }
const SOURCE_STATUS: Record<string, string> = {
  ok: 'Answered',
  error: 'Failed',
  disabled: 'Disabled',
  not_configured: 'Not configured',
}
const PRECISION_TEXT: Record<string, string> = {
  coordinates: 'Approximate (GeoIP database coordinates)',
  city: 'City reported by an IP data provider (approximate)',
  country: 'Country-level estimate',
  unknown: 'Location unknown',
}

interface Props {
  node: NetworkNode
  maskIps: boolean
  expanded: boolean
  onToggle: () => void
  busy: boolean
  selectedProfile: { id: string; name: string; running: boolean; boundNodeId: string | null } | null
  onCheck: (refreshIntel: boolean) => void
  onBind: () => void
  onOpenSite: (site: CheckSite) => void
}

export function NodeDetail({ node, maskIps, expanded, onToggle, busy, selectedProfile, onCheck, onBind, onOpenSite }: Props) {
  const { t, locale } = useI18n()
  const [copied, setCopied] = useState(false)
  const check = node.check
  const composite = node.intel?.composite ?? null
  const tone = purityTone(composite?.purity_score)
  const ip = check?.exit_ip ?? null
  const healthTone = !check ? 'unknown' : check.health === 'ok' ? 'ok' : check.health === 'error' ? 'danger' : 'unknown'
  const isBound = selectedProfile?.boundNodeId === node.id
  const provider = composite?.location
  const disagreement = provider?.latitude != null && provider.longitude != null && check?.latitude != null && check.longitude != null
    ? Math.round(distanceKm(check.latitude, check.longitude, provider.latitude, provider.longitude)) : null
  const providerPlace = provider ? [provider.city, provider.region, provider.country_iso ? countryName(provider.country_iso, locale) : null].filter(Boolean).join(', ') : ''
  const showProvider = Boolean(providerPlace) && (disagreement == null ? !check?.city : disagreement > 150)
  const canOpenSites = Boolean(selectedProfile?.running)

  async function copy() {
    if (!ip) return
    try { await navigator.clipboard.writeText(ip); setCopied(true); setTimeout(() => setCopied(false), 1500) } catch { /* clipboard unavailable */ }
  }

  return (
    <article className={`border-b border-line ${expanded ? 'bg-surface' : ''}`}>
      <button type="button" onClick={onToggle} aria-expanded={expanded} className="flex w-full items-start gap-2.5 px-4 py-3 text-left hover:bg-surface">
        <span className="pt-0.5 text-[15px] leading-none" aria-hidden="true">{flag(check?.country_iso) || '·'}</span>
        <span className="min-w-0 flex-1">
          <span className="block truncate font-medium" title={node.name}>{node.name}</span>
          <span className="block truncate text-[11px] text-ink-dim">{node.subscription_name} · {node.protocol}{node.status !== 'active' ? ` · ${t(node.status === 'retired' ? 'Retired' : 'Invalid')}` : ''}</span>
        </span>
        <span className="shrink-0 text-right">
          <span className={`block text-[12px] ${TONE_TEXT[healthTone]}`}>{!check ? t('Not checked') : check.health === 'ok' ? (check.latency_ms != null ? `${check.latency_ms} ms` : t('Reachable')) : t('Unreachable')}</span>
          {composite?.purity_score != null && <span className={`block font-mono text-[11px] ${TONE_TEXT[tone]}`}>{t('Purity {score}', { score: composite.purity_score })}</span>}
        </span>
      </button>

      {expanded && (
        <div className="space-y-4 px-4 pb-4">
          <section aria-label={t('Exit IP')}>
            <h4 className="text-[11px] uppercase tracking-[0.05em] text-ink-faint">{t('Exit IP')}</h4>
            <div className="mt-1 flex items-center gap-2">
              <span className="break-all font-mono text-[14px]">{maskIp(ip, maskIps)}</span>
              {ip && <button type="button" className="btn btn-ghost h-[24px] w-[24px] p-0" onClick={copy} aria-label={t('Copy exit IP')} title={t('Copy exit IP')}>{copied ? <Check size={12} /> : <Copy size={12} />}</button>}
            </div>
            {node.shared_exit_count > 1 && <p className="mt-1 flex items-start gap-1.5 text-[12px] text-warn"><TriangleAlert size={12} className="mt-0.5 shrink-0" aria-hidden="true" />{t('Shared by {count} nodes: different nodes are not different public IPs.', { count: node.shared_exit_count })}</p>}
            {check?.health === 'error' && <p className="mt-1 text-[12px] text-danger">{t(check.error ?? 'The node did not reach the internet.')}</p>}
            <p className="mt-1 text-[11px] text-ink-faint">{t('Measured through the node itself, never from its entry server, name or flag.')}</p>
          </section>

          <section aria-label={t('Location')}>
            <h4 className="text-[11px] uppercase tracking-[0.05em] text-ink-faint">{t('Location')}</h4>
            {check?.country_iso || check?.latitude != null ? (
              <dl className="mt-1 grid grid-cols-[88px_1fr] gap-x-3 gap-y-1 text-[12px]">
                <dt className="text-ink-dim">{t('Country')}</dt><dd>{check?.country_iso ? `${countryName(check.country_iso, locale)} (${check.country_iso})` : '—'}</dd>
                <dt className="text-ink-dim">{t('Region')}</dt><dd>{check?.region || '—'}</dd>
                <dt className="text-ink-dim">{t('City')}</dt><dd>{check?.city || '—'}</dd>
                <dt className="text-ink-dim">{t('Timezone')}</dt><dd className="font-mono">{check?.timezone || '—'}</dd>
                <dt className="text-ink-dim">{t('Coordinates')}</dt><dd className="font-mono">{check?.latitude != null && check.longitude != null ? `${check.latitude.toFixed(3)}, ${check.longitude.toFixed(3)}` : '—'}</dd>
                <dt className="text-ink-dim">{t('Precision')}</dt><dd>{t(PRECISION_TEXT[check?.precision ?? 'unknown'])}</dd>
                {showProvider && <><dt className="text-ink-dim">{t('Provider says')}</dt><dd>{providerPlace}</dd></>}
              </dl>
            ) : <p className="mt-1 text-[12px] text-ink-dim">{t('Not located yet. Check the node to measure its exit.')}</p>}
            {showProvider && disagreement != null && <p className="mt-1 flex items-start gap-1.5 text-[12px] text-warn"><TriangleAlert size={12} className="mt-0.5 shrink-0" aria-hidden="true" />{t('Location sources disagree by about {km} km. IP geolocation is approximate; the map uses the local GeoIP point, the same one the browser uses for its timezone.', { km: disagreement })}</p>}
            {check && <p className={`mt-1 text-[11px] ${check.stale ? 'text-warn' : 'text-ink-faint'}`}>{t('Last checked {time}', { time: new Date(check.checked_at).toLocaleString(locale) })}{check.stale ? ` · ${t('older than 24 hours')}` : ''}</p>}
          </section>

          <section aria-label={t('IP quality')}>
            <div className="flex items-center justify-between">
              <h4 className="text-[11px] uppercase tracking-[0.05em] text-ink-faint">{t('IP quality')}</h4>
              {node.intel_checked_at && <span className="text-[11px] text-ink-faint">{new Date(node.intel_checked_at).toLocaleString(locale)}</span>}
            </div>
            {composite ? (
              <div className="mt-2 space-y-3">
                <div className="flex items-end gap-4">
                  <div>
                    <p className={`font-mono text-[28px] font-semibold leading-none ${TONE_TEXT[tone]}`}>{composite.purity_score ?? '—'}</p>
                    <p className="mt-1 text-[11px] text-ink-dim">{t('Purity (0–100, higher is cleaner)')}</p>
                  </div>
                  <div className="flex-1">
                    <p className={`text-[13px] font-medium ${TONE_TEXT[tone]}`}>{t(gradeLabel(composite.grade))}</p>
                    <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-raised" role="meter" aria-valuemin={0} aria-valuemax={100} aria-valuenow={composite.purity_score ?? undefined} aria-label={t('Purity')}>
                      <div className={`h-full ${TONE_BAR[tone]}`} style={{ width: `${composite.purity_score ?? 0}%` }} />
                    </div>
                  </div>
                </div>
                <dl className="grid grid-cols-[88px_1fr] gap-x-3 gap-y-1 text-[12px]">
                  <dt className="text-ink-dim">{t('Fraud score')}</dt>
                  <dd>{composite.score_sources.length ? composite.score_sources.map(item => `${SOURCE_LABEL[item.source] ?? item.source} ${item.score}`).join(' · ') : t('No provider gave a score')}</dd>
                  <dt className="text-ink-dim">{t('IP type')}</dt>
                  <dd>{composite.ip_type === 'residential' ? t('Residential') : composite.ip_type === 'datacenter' ? t('Datacenter') : '—'}</dd>
                  <dt className="text-ink-dim">{t('Native IP')}</dt>
                  <dd>{composite.native == null ? '—' : composite.native ? t('Yes (native)') : t('No (broadcast)')}</dd>
                  <dt className="text-ink-dim">{t('Network')}</dt>
                  <dd className="break-words">{composite.asn ? `AS${composite.asn} ` : ''}{composite.as_org || (composite.asn ? '' : '—')}</dd>
                  {composite.hostname && <><dt className="text-ink-dim">{t('Hostname')}</dt><dd className="break-all font-mono">{composite.hostname}</dd></>}
                </dl>
                {composite.signals.length > 0 && (
                  <ul className="space-y-1 text-[12px]">
                    {composite.signals.map(signal => (
                      <li key={signal.code} className={`flex items-start gap-1.5 ${signal.level === 'info' ? 'text-ink-dim' : signal.level === 'warning' ? 'text-warn' : 'text-danger'}`}>
                        {signal.level === 'info' ? <ShieldCheck size={12} className="mt-0.5 shrink-0" aria-hidden="true" /> : <TriangleAlert size={12} className="mt-0.5 shrink-0" aria-hidden="true" />}
                        {t(SIGNAL_TEXT[signal.code] ?? signal.code)}
                      </li>
                    ))}
                  </ul>
                )}
                <details className="text-[11px] text-ink-dim">
                  <summary className="cursor-pointer">{t('Sources and method')}</summary>
                  <ul className="mt-1 space-y-0.5">
                    {Object.entries(node.intel?.sources ?? {}).map(([name, source]) => (
                      <li key={name}>{SOURCE_LABEL[name] ?? name}: {t(SOURCE_STATUS[source.status] ?? source.status)}{source.error ? ` (${source.error})` : ''}</li>
                    ))}
                  </ul>
                  <p className="mt-1">{t('Purity = 100 − the highest fraud score − 10 for datacenter − 10 for broadcast − 15 for VPN/proxy flags. Lookups go through the node, so providers see the exit, not this computer. A heuristic summary, not a guarantee of how a site treats this IP.')}</p>
                </details>
              </div>
            ) : <p className="mt-1 text-[12px] text-ink-dim">{ip ? t('Not assessed yet. Run an IP quality check.') : t('Check the node first.')}</p>}
          </section>

          <div className="flex flex-wrap gap-1.5">
            <button className="btn btn-default" disabled={busy || node.status !== 'active'} onClick={() => onCheck(false)}>
              {busy ? <LoaderCircle size={13} className="animate-spin motion-reduce:animate-none" aria-hidden="true" /> : <Activity size={13} aria-hidden="true" />}
              {busy ? t('Checking…') : t('Check exit and quality')}
            </button>
            {ip && <button className="btn btn-ghost" disabled={busy || node.status !== 'active'} onClick={() => onCheck(true)} title={t('Ask the IP data providers again, ignoring the cache')}><RefreshCw size={13} aria-hidden="true" />{t('Refresh quality')}</button>}
            <button className="btn btn-primary" disabled={!selectedProfile || isBound || node.status !== 'active' || busy} onClick={onBind}
              title={!selectedProfile ? t('Select a profile on the left first') : selectedProfile.running ? t('Stop the profile before changing its node') : undefined}>
              <Link2 size={13} aria-hidden="true" />
              {isBound ? t('Bound to {name}', { name: selectedProfile?.name ?? '' }) : selectedProfile ? t('Bind to {name}', { name: selectedProfile.name }) : t('Bind to selected profile')}
            </button>
          </div>

          <section aria-label={t('Check sites')}>
            <h4 className="text-[11px] uppercase tracking-[0.05em] text-ink-faint">{t('Check sites')}</h4>
            <div className="mt-1.5 flex flex-wrap gap-1.5">
              {(['ippure', 'ipip', 'ipinfo', 'scamalytics'] as CheckSite[]).map(site => (
                <button key={site} className="btn btn-default h-[26px] px-2 text-[12px]" disabled={!canOpenSites || (site === 'scamalytics' && !ip)} onClick={() => onOpenSite(site)} title={canOpenSites ? t('Opens in {name}\'s own browser, through its exit', { name: selectedProfile?.name ?? '' }) : t('Start the selected profile to open check sites through its exit')}>
                  <Globe size={12} aria-hidden="true" />{site === 'ippure' ? 'IPPure' : site === 'ipip' ? 'IPIP.la' : site === 'ipinfo' ? 'ipinfo.io' : 'Scamalytics'}
                </button>
              ))}
            </div>
            {ip && (
              <p className="mt-2 flex flex-wrap gap-3 text-[11px] text-ink-dim">
                {(['ipinfo', 'scamalytics'] as const).map(site => {
                  const url = lookupUrl(site, ip)
                  return url ? <a key={site} className="inline-flex items-center gap-1 hover:text-ink" href={url} target="_blank" rel="noopener noreferrer">{t('Look up on {site}', { site: site === 'ipinfo' ? 'ipinfo.io' : 'Scamalytics' })}<ExternalLink size={10} aria-hidden="true" /></a> : null
                })}
                <span className="text-ink-faint">{t('(from this computer, not through the node)')}</span>
              </p>
            )}
          </section>
        </div>
      )}
    </article>
  )
}
