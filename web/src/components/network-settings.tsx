'use client'

import { useCallback, useEffect, useId, useState } from 'react'
import { CheckCircle2, LoaderCircle, RefreshCw, TriangleAlert } from 'lucide-react'

import { useI18n } from '@/components/i18n-provider'
import { useToast } from '@/components/toast'
import { networkAPI, type NetworkStatus } from '@/lib/network-api'

/** Mihomo engine, exit checks and IP-quality providers. Keys are write-only. */
export function NetworkSettingsPanel() {
  const { t, locale } = useI18n()
  const toast = useToast()
  const titleId = useId()
  const [status, setStatus] = useState<NetworkStatus | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [mihomoPath, setMihomoPath] = useState('')
  const [concurrency, setConcurrency] = useState(8)
  const [timeout, setTimeoutSeconds] = useState(10)
  const [cacheHours, setCacheHours] = useState(24)
  const [ipinfo, setIpinfo] = useState(true)
  const [ippure, setIppure] = useState(true)
  const [scamalytics, setScamalytics] = useState(false)
  const [mask, setMask] = useState(false)
  const [ipinfoToken, setIpinfoToken] = useState('')
  const [scamUrl, setScamUrl] = useState('')
  const [scamKey, setScamKey] = useState('')

  const load = useCallback(async () => {
    try {
      const data = await networkAPI.status()
      setStatus(data)
      const s = data.settings
      setMihomoPath(s.mihomo_path ?? ''); setConcurrency(s.probe_concurrency); setTimeoutSeconds(s.probe_timeout_seconds)
      setCacheHours(s.intel_cache_hours); setIpinfo(s.intel_ipinfo_enabled); setIppure(s.intel_ippure_enabled)
      setScamalytics(s.intel_scamalytics_enabled); setMask(s.mask_exit_ips); setError(null)
    } catch (err) { setError(err instanceof Error ? err.message : String(err)) }
  }, [])
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    load()
  }, [load])

  async function save() {
    setBusy(true)
    const changes: Record<string, unknown> = {
      mihomo_path: mihomoPath.trim() || null,
      probe_concurrency: concurrency,
      probe_timeout_seconds: timeout,
      intel_cache_hours: cacheHours,
      intel_ipinfo_enabled: ipinfo,
      intel_ippure_enabled: ippure,
      intel_scamalytics_enabled: scamalytics,
      mask_exit_ips: mask,
    }
    // Empty key fields mean "keep the stored key"; Clear removes one explicitly.
    if (ipinfoToken) changes.intel_ipinfo_token = ipinfoToken
    if (scamUrl) changes.intel_scamalytics_url = scamUrl
    if (scamKey) changes.intel_scamalytics_key = scamKey
    try {
      await networkAPI.updateSettings(changes)
      setIpinfoToken(''); setScamUrl(''); setScamKey('')
      toast('ok', t('Network settings saved'))
      await load()
    } catch (err) { toast('error', t('Could not save network settings'), t(err instanceof Error ? err.message : String(err))) }
    finally { setBusy(false) }
  }

  async function clear(key: string) {
    try { await networkAPI.updateSettings({ [key]: null }); await load() }
    catch (err) { toast('error', t('Could not save network settings'), t(err instanceof Error ? err.message : String(err))) }
  }

  const engine = status?.engine
  return <section aria-labelledby={titleId}>
    <h2 id={titleId} className="mb-2 text-[11px] font-semibold uppercase tracking-[0.08em] text-ink-faint">{t('Network nodes and map')}</h2>
    <div className="panel space-y-4 p-4">
      {error && <p className="text-danger" role="alert">{t(error)}</p>}
      {engine && <div className={`flex items-start gap-2 rounded-md border border-line bg-raised p-3 ${engine.available ? '' : 'text-danger'}`} role="status">
        {engine.available ? <CheckCircle2 size={15} className="mt-0.5 shrink-0 text-ok" aria-hidden="true" /> : <TriangleAlert size={15} className="mt-0.5 shrink-0" aria-hidden="true" />}
        <div className="min-w-0">
          <p className="font-medium">{engine.available ? t('Mihomo {version} ready', { version: engine.version ?? '' }) : t('Mihomo is not available')}</p>
          <p className="break-all font-mono text-[11px] text-ink-dim">{engine.available ? engine.path : t(engine.message ?? '')}</p>
        </div>
      </div>}
      {status && <p className={`text-[12px] ${status.geoip.available ? 'text-ink-dim' : 'text-warn'}`}>{status.geoip.available
        ? t('GeoIP database: {name}, updated {date}', { name: status.geoip.name ?? '', date: status.geoip.modified_at ? new Date(status.geoip.modified_at * 1000).toLocaleDateString(locale) : '—' })
        : t('GeoIP database not installed: exits cannot be placed on the map until the browser resources are installed.')}</p>}
      {status && !status.encryption_available && <p className="text-[12px] text-danger">{t('No encryption key is configured, so subscriptions cannot be stored. Restore secret.key or set CPM_SECRET_KEY.')}</p>}

      <label className="flex flex-col gap-1.5 text-ink-dim"><span>{t('Mihomo executable (optional)')}</span>
        <input className="field font-mono" value={mihomoPath} onChange={event => setMihomoPath(event.target.value)} placeholder={t('Found automatically when empty')} spellCheck={false} />
        <span className="text-[11px]">{t('Setuid, shared-writable or foreign-owned binaries are refused. Copy a client\'s bundled engine with scripts/prepare_mihomo.py instead of pointing at it.')}</span>
      </label>
      <div className="grid gap-3 sm:grid-cols-3">
        <label className="flex flex-col gap-1.5 text-ink-dim"><span>{t('Parallel checks')}</span><input className="field" type="number" min={1} max={32} value={concurrency} onChange={event => setConcurrency(Number(event.target.value))} /></label>
        <label className="flex flex-col gap-1.5 text-ink-dim"><span>{t('Check timeout (seconds)')}</span><input className="field" type="number" min={3} max={60} value={timeout} onChange={event => setTimeoutSeconds(Number(event.target.value))} /></label>
        <label className="flex flex-col gap-1.5 text-ink-dim"><span>{t('Keep IP quality for (hours)')}</span><input className="field" type="number" min={1} max={720} value={cacheHours} onChange={event => setCacheHours(Number(event.target.value))} /></label>
      </div>
      <label className="flex items-center gap-2"><input type="checkbox" checked={mask} onChange={event => setMask(event.target.checked)} />{t('Partially hide exit IPs on screen')}</label>

      <fieldset className="space-y-3 border-t border-line pt-3">
        <legend className="font-medium">{t('IP quality providers')}</legend>
        <p className="text-[12px] text-ink-dim">{t('Asked only when you check a single node, through that node, and cached per IP. Batch checks never contact them.')}</p>
        <label className="flex items-center gap-2"><input type="checkbox" checked={ipinfo} onChange={event => setIpinfo(event.target.checked)} />{t('ipinfo.io (location, network; privacy flags with a paid token)')}</label>
        <div className="flex gap-2 pl-6">
          <input className="field font-mono" type="password" autoComplete="off" value={ipinfoToken} onChange={event => setIpinfoToken(event.target.value)} placeholder={status?.settings.has_intel_ipinfo_token ? t('Token stored — type to replace') : t('Token (optional)')} aria-label={t('ipinfo.io token')} />
          {status?.settings.has_intel_ipinfo_token && <button type="button" className="btn btn-ghost" onClick={() => clear('intel_ipinfo_token')}>{t('Clear')}</button>}
        </div>
        <label className="flex items-center gap-2"><input type="checkbox" checked={ippure} onChange={event => setIppure(event.target.checked)} />{t('IPPure (fraud score, residential, native/broadcast)')}</label>
        <label className="flex items-center gap-2"><input type="checkbox" checked={scamalytics} onChange={event => setScamalytics(event.target.checked)} />{t('Scamalytics (fraud score; needs your own API account)')}</label>
        <div className="grid gap-2 pl-6 sm:grid-cols-2">
          <input className="field font-mono" type="password" autoComplete="off" value={scamUrl} onChange={event => setScamUrl(event.target.value)} placeholder={status?.settings.has_intel_scamalytics_url ? t('API URL stored — type to replace') : 'https://api11.scamalytics.com/v3/USER/'} aria-label={t('Scamalytics API URL')} />
          <input className="field font-mono" type="password" autoComplete="off" value={scamKey} onChange={event => setScamKey(event.target.value)} placeholder={status?.settings.has_intel_scamalytics_key ? t('Key stored — type to replace') : t('API key')} aria-label={t('Scamalytics API key')} />
        </div>
        {(status?.settings.has_intel_scamalytics_url || status?.settings.has_intel_scamalytics_key) && <button type="button" className="btn btn-ghost ml-6" onClick={async () => { await clear('intel_scamalytics_url'); await clear('intel_scamalytics_key') }}>{t('Clear Scamalytics credentials')}</button>}
        <p className="text-[12px] text-ink-dim">{t('IPIP.la has no public API; open it in a running profile\'s browser from the map instead.')}</p>
      </fieldset>

      <div className="flex gap-2">
        <button type="button" className="btn btn-primary" disabled={busy} onClick={save}>{busy ? <LoaderCircle size={13} className="animate-spin motion-reduce:animate-none" aria-hidden="true" /> : null}{t('Save network settings')}</button>
        <button type="button" className="btn btn-ghost" onClick={load}><RefreshCw size={13} aria-hidden="true" />{t('Check again')}</button>
      </div>
    </div>
  </section>
}
