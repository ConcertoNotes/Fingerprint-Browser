'use client'

import { useCallback, useEffect, useId, useState } from 'react'
import { CheckCircle2, Globe2, LoaderCircle, RefreshCw, ShieldCheck, TriangleAlert } from 'lucide-react'

import { useI18n } from '@/components/i18n-provider'
import { useToast } from '@/components/toast'
import { networkAPI, type NetworkStatus } from '@/lib/network-api'

/** Proxy routing engine, offline GeoIP, exit checks, and IP-quality providers. Keys are write-only. */
export function NetworkSettingsPanel() {
  const { t, locale } = useI18n()
  const toast = useToast()
  const titleId = useId()
  const [status, setStatus] = useState<NetworkStatus | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [customEnginePath, setCustomEnginePath] = useState('')
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
      setCustomEnginePath(s.mihomo_path ?? '')
      setConcurrency(s.probe_concurrency)
      setTimeoutSeconds(s.probe_timeout_seconds)
      setCacheHours(s.intel_cache_hours)
      setIpinfo(s.intel_ipinfo_enabled)
      setIppure(s.intel_ippure_enabled)
      setScamalytics(s.intel_scamalytics_enabled)
      setMask(s.mask_exit_ips)
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }, [])

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    load()
  }, [load])

  async function save() {
    setBusy(true)
    const changes: Record<string, unknown> = {
      mihomo_path: customEnginePath.trim() || null,
      probe_concurrency: concurrency,
      probe_timeout_seconds: timeout,
      intel_cache_hours: cacheHours,
      intel_ipinfo_enabled: ipinfo,
      intel_ippure_enabled: ippure,
      intel_scamalytics_enabled: scamalytics,
      mask_exit_ips: mask,
    }
    if (ipinfoToken) changes.intel_ipinfo_token = ipinfoToken
    if (scamUrl) changes.intel_scamalytics_url = scamUrl
    if (scamKey) changes.intel_scamalytics_key = scamKey
    try {
      await networkAPI.updateSettings(changes)
      setIpinfoToken('')
      setScamUrl('')
      setScamKey('')
      toast('ok', t('Network settings saved'))
      await load()
    } catch (err) {
      toast('error', t('Could not save network settings'), t(err instanceof Error ? err.message : String(err)))
    } finally {
      setBusy(false)
    }
  }

  async function clear(key: string) {
    try {
      await networkAPI.updateSettings({ [key]: null })
      await load()
    } catch (err) {
      toast('error', t('Could not save network settings'), t(err instanceof Error ? err.message : String(err)))
    }
  }

  const engine = status?.engine
  const geoip = status?.geoip
  const engineVer = engine?.version ? engine.version.replace(/^v/i, '') : null
  const geoipDate = geoip?.modified_at ? new Date(geoip.modified_at * 1000).toLocaleDateString(locale) : null
  const geoipSizeMb = geoip?.size ? `${(geoip.size / (1024 * 1024)).toFixed(1)} MB` : null

  return (
    <section aria-labelledby={titleId}>
      <h2 id={titleId} className="mb-2 text-[11px] font-semibold uppercase tracking-[0.06em] text-ink-faint">
        {t('Proxy routing & geolocation')}
      </h2>
      <div className="panel space-y-4 p-4">
        {error && <p className="text-danger" role="alert">{t(error)}</p>}

        {/* Built-in Runtime Status Cards (Proxy Engine + Offline GeoIP) */}
        <div className="grid gap-3 sm:grid-cols-2">
          {engine && (
            <div
              className={`flex items-start gap-2.5 rounded-xl border p-3 ${
                engine.available
                  ? 'border-ok/25 bg-ok-shade/40'
                  : 'border-danger/25 bg-danger-shade/50 text-danger'
              }`}
              role="status"
            >
              <span className={`mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg ${engine.available ? 'bg-ok-shade text-ok' : 'bg-danger-shade text-danger'}`} aria-hidden="true">
                {engine.available ? <ShieldCheck size={15} /> : <TriangleAlert size={15} />}
              </span>
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-1.5">
                  <p className="text-[12.5px] font-semibold text-ink">
                    {engine.available
                      ? (engine.bundled ? t('Proxy routing engine · Built-in & ready') : t('Proxy routing engine · Ready'))
                      : t('Proxy routing engine is not ready')}
                  </p>
                  {engine.available && engineVer && (
                    <span className="rounded-full bg-surface px-1.5 py-0.2 font-mono text-[10.5px] text-ok ring-1 ring-ok/20">
                      {`v${engineVer}`}
                    </span>
                  )}
                </div>
                <p className="mt-0.5 text-[11.5px] text-ink-dim">
                  {engine.available
                    ? t('Per-window fail-closed loopback routing active.')
                    : t('Reinstall the app package or specify a custom engine binary below.')}
                </p>
              </div>
            </div>
          )}

          {geoip && (
            <div
              className={`flex items-start gap-2.5 rounded-xl border p-3 ${
                geoip.available
                  ? 'border-ok/25 bg-ok-shade/40'
                  : 'border-warn/25 bg-warn-shade/50'
              }`}
              role="status"
            >
              <span className={`mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg ${geoip.available ? 'bg-ok-shade text-ok' : 'bg-warn-shade text-warn'}`} aria-hidden="true">
                {geoip.available ? <CheckCircle2 size={15} /> : <Globe2 size={15} />}
              </span>
              <div className="min-w-0">
                <p className="text-[12.5px] font-semibold text-ink">
                  {geoip.available
                    ? (geoip.bundled ? t('Offline GeoIP database · Built-in & ready') : t('Offline GeoIP database · Ready'))
                    : t('Offline GeoIP database not ready')}
                </p>
                <p className="mt-0.5 text-[11.5px] text-ink-dim">
                  {geoip.available
                    ? [
                        geoipDate ? t('Updated {date}', { date: geoipDate }) : t('Local offline lookup enabled'),
                        geoipSizeMb,
                      ].filter(Boolean).join(' · ')
                    : t('Exits will appear on the world map once offline geolocation data is initialized.')}
                </p>
              </div>
            </div>
          )}
        </div>

        {status && !status.encryption_available && (
          <p className="text-[12px] text-danger">
            {t('No encryption key is configured, so subscriptions cannot be stored. Restore secret.key or set CPM_SECRET_KEY.')}
          </p>
        )}

        <div className="grid gap-3 sm:grid-cols-3">
          <label className="flex flex-col gap-1.5 text-ink-dim">
            <span>{t('Parallel checks')}</span>
            <input className="field" type="number" min={1} max={32} value={concurrency} onChange={event => setConcurrency(Number(event.target.value))} />
          </label>
          <label className="flex flex-col gap-1.5 text-ink-dim">
            <span>{t('Check timeout (seconds)')}</span>
            <input className="field" type="number" min={3} max={60} value={timeout} onChange={event => setTimeoutSeconds(Number(event.target.value))} />
          </label>
          <label className="flex flex-col gap-1.5 text-ink-dim">
            <span>{t('Keep IP quality for (hours)')}</span>
            <input className="field" type="number" min={1} max={720} value={cacheHours} onChange={event => setCacheHours(Number(event.target.value))} />
          </label>
        </div>

        <label className="flex cursor-pointer items-center gap-2">
          <input type="checkbox" checked={mask} onChange={event => setMask(event.target.checked)} />
          <span>{t('Partially hide exit IPs on screen')}</span>
        </label>

        <fieldset className="space-y-3 border-t border-line pt-3">
          <legend className="font-medium text-ink">{t('IP quality providers')}</legend>
          <p className="text-[12px] text-ink-dim">
            {t('Asked only when you check a single node, through that node, and cached per IP. Batch checks never contact them.')}
          </p>
          <label className="flex cursor-pointer items-center gap-2">
            <input type="checkbox" checked={ipinfo} onChange={event => setIpinfo(event.target.checked)} />
            <span>{t('ipinfo.io (location, network; privacy flags with a paid token)')}</span>
          </label>
          <div className="flex gap-2 pl-6">
            <input
              className="field font-mono"
              type="password"
              autoComplete="off"
              value={ipinfoToken}
              onChange={event => setIpinfoToken(event.target.value)}
              placeholder={status?.settings.has_intel_ipinfo_token ? t('Token stored — type to replace') : t('Token (optional)')}
              aria-label={t('ipinfo.io token')}
            />
            {status?.settings.has_intel_ipinfo_token && (
              <button type="button" className="btn btn-ghost" onClick={() => clear('intel_ipinfo_token')}>
                {t('Clear')}
              </button>
            )}
          </div>
          <label className="flex cursor-pointer items-center gap-2">
            <input type="checkbox" checked={ippure} onChange={event => setIppure(event.target.checked)} />
            <span>{t('IPPure (fraud score, residential, native/broadcast)')}</span>
          </label>
          <label className="flex cursor-pointer items-center gap-2">
            <input type="checkbox" checked={scamalytics} onChange={event => setScamalytics(event.target.checked)} />
            <span>{t('Scamalytics (fraud score; needs your own API account)')}</span>
          </label>
          <div className="grid gap-2 pl-6 sm:grid-cols-2">
            <input
              className="field font-mono"
              type="password"
              autoComplete="off"
              value={scamUrl}
              onChange={event => setScamUrl(event.target.value)}
              placeholder={status?.settings.has_intel_scamalytics_url ? t('API URL stored — type to replace') : 'https://api11.scamalytics.com/v3/USER/'}
              aria-label={t('Scamalytics API URL')}
            />
            <input
              className="field font-mono"
              type="password"
              autoComplete="off"
              value={scamKey}
              onChange={event => setScamKey(event.target.value)}
              placeholder={status?.settings.has_intel_scamalytics_key ? t('Key stored — type to replace') : t('API key')}
              aria-label={t('Scamalytics API key')}
            />
          </div>
          {(status?.settings.has_intel_scamalytics_url || status?.settings.has_intel_scamalytics_key) && (
            <button
              type="button"
              className="btn btn-ghost ml-6"
              onClick={async () => { await clear('intel_scamalytics_url'); await clear('intel_scamalytics_key') }}
            >
              {t('Clear Scamalytics credentials')}
            </button>
          )}
        </fieldset>

        {/* Collapsed Advanced Custom Engine Path */}
        <details className="rounded-xl border border-line bg-canvas/65 px-3.5 py-2.5 text-[12px]">
          <summary className="cursor-pointer font-medium text-ink-dim hover:text-ink">
            {t('Advanced: custom proxy engine path')}
          </summary>
          <div className="mt-2.5 space-y-1.5">
            <input
              className="field font-mono text-[12px]"
              value={customEnginePath}
              onChange={event => setCustomEnginePath(event.target.value)}
              placeholder={t('Leave empty to use the built-in proxy routing engine')}
              aria-label={t('Custom proxy engine path')}
              spellCheck={false}
            />
            <p className="text-[11px] text-ink-faint">
              {t('Leave empty to use the built-in proxy routing engine automatically. Only owner-private executables are accepted.')}
            </p>
          </div>
        </details>

        <div className="flex gap-2">
          <button type="button" className="btn btn-primary" disabled={busy} onClick={save}>
            {busy ? <LoaderCircle size={13} className="animate-spin motion-reduce:animate-none" aria-hidden="true" /> : null}
            {t('Save network settings')}
          </button>
          <button type="button" className="btn btn-ghost" onClick={load}>
            <RefreshCw size={13} aria-hidden="true" />
            {t('Check again')}
          </button>
        </div>
      </div>
    </section>
  )
}
