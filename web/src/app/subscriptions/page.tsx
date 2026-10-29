'use client'

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import Link from 'next/link'
import { Activity, FileUp, Globe2, LoaderCircle, Pencil, Plus, RefreshCw, Rss, Search, Trash2, TriangleAlert } from 'lucide-react'

import { EmptyState } from '@/components/empty-state'
import { useI18n } from '@/components/i18n-provider'
import { Modal } from '@/components/modal'
import { useToast } from '@/components/toast'
import { ApiRequestError, isStaleWrite } from '@/lib/api'
import { networkAPI, type BatchJob, type NetworkNode, type NetworkStatus, type Subscription, type SyncReport } from '@/lib/network-api'
import { countryName, flag, formatBytes, gradeLabel, maskIp, purityTone, usageSummary } from '@/lib/network-map'

const MAX_FILE = 10 * 1024 * 1024
const TONE_TEXT = { ok: 'text-ok', warn: 'text-warn', danger: 'text-danger', unknown: 'text-ink-dim' } as const

export default function SubscriptionsPage() {
  const { t, locale } = useI18n()
  const toast = useToast()
  const [subscriptions, setSubscriptions] = useState<Subscription[]>([])
  const [nodes, setNodes] = useState<NetworkNode[]>([])
  const [status, setStatus] = useState<NetworkStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [filter, setFilter] = useState<string | null>(null)
  const [search, setSearch] = useState('')
  const [adding, setAdding] = useState<'url' | 'file' | null>(null)
  const [report, setReport] = useState<{ name: string; report: SyncReport } | null>(null)
  const [renaming, setRenaming] = useState<Subscription | null>(null)
  const [deleting, setDeleting] = useState<Subscription | null>(null)
  const [syncing, setSyncing] = useState<Set<string>>(new Set())
  const [checking, setChecking] = useState<Set<string>>(new Set())
  const [job, setJob] = useState<BatchJob | null>(null)
  const refreshFile = useRef<HTMLInputElement>(null)
  const refreshTarget = useRef<Subscription | null>(null)

  const load = useCallback(async () => {
    try {
      const [subs, nodeData, engine] = await Promise.all([networkAPI.subscriptions(), networkAPI.nodes({ limit: 1000 }), networkAPI.status()])
      setSubscriptions(subs); setNodes(nodeData.nodes); setStatus(engine); setError(null)
    } catch (err) { setError(err instanceof Error ? err.message : String(err)) }
    finally { setLoading(false) }
  }, [])
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    load()
  }, [load])

  const maskIps = status?.settings.mask_exit_ips ?? false
  const shown = useMemo(() => nodes.filter(node => (!filter || node.subscription_id === filter) && (!search || [node.name, node.protocol, node.check?.exit_ip, node.check?.country_iso, node.check?.city].some(value => value?.toLowerCase().includes(search.toLowerCase())))), [nodes, filter, search])

  function setBusy(setter: typeof setSyncing, id: string, on: boolean) {
    setter(current => { const next = new Set(current); if (on) next.add(id); else next.delete(id); return next })
  }

  async function sync(subscription: Subscription, content?: string) {
    setBusy(setSyncing, subscription.id, true)
    try {
      const result = await networkAPI.sync(subscription.id, content)
      setReport({ name: subscription.name, report: result.report })
      await load()
    } catch (err) {
      toast('error', t('Sync failed'), t(err instanceof Error ? err.message : String(err)))
      await load()
    } finally { setBusy(setSyncing, subscription.id, false) }
  }

  async function readFile(file: File): Promise<string | null> {
    if (file.size > MAX_FILE) { toast('error', t('File is too large'), t('Subscription files can be at most 10 MB.')); return null }
    return file.text()
  }

  async function check(node: NetworkNode) {
    setBusy(setChecking, node.id, true)
    try {
      const updated = await networkAPI.checkNode(node.id, { include_intel: true })
      setNodes(current => current.map(item => item.id === updated.id ? updated : item))
    } catch (err) { toast('error', t('Could not check the node'), t(err instanceof Error ? err.message : String(err))) }
    finally { setBusy(setChecking, node.id, false) }
  }

  async function checkVisible() {
    const ids = shown.filter(node => node.status === 'active').slice(0, 200).map(node => node.id)
    if (!ids.length) return
    try {
      let current = await networkAPI.checkBatch(ids)
      setJob(current)
      while (current.state === 'queued' || current.state === 'running') {
        await new Promise(resolve => setTimeout(resolve, 800))
        current = await networkAPI.job(current.id)
        setJob(current)
      }
      toast(current.state === 'finished' ? 'ok' : 'error', t('Batch check finished'), t('{ok} reachable, {failed} failed, one engine process.', { ok: current.ok, failed: current.failed }))
      await load()
    } catch (err) { toast('error', t('Could not start the batch check'), t(err instanceof Error ? err.message : String(err))) }
    finally { setJob(null) }
  }

  const jobActive = job && (job.state === 'queued' || job.state === 'running')

  return <>
    <header className="sticky top-0 z-20 flex min-h-[52px] flex-wrap items-center gap-3 border-b border-line bg-canvas/85 px-5 py-2 backdrop-blur">
      <h1 className="text-[14px] font-semibold">{t('Proxy subscriptions')}</h1><span className="font-mono text-ink-faint">{subscriptions.length}</span>
      <div className="ml-auto flex flex-wrap gap-2">
        <Link className="btn btn-default" href="/map/"><Globe2 size={13} aria-hidden="true" />{t('Open map')}</Link>
        <button className="btn btn-default" onClick={() => setAdding('file')}><FileUp size={13} aria-hidden="true" />{t('Import file')}</button>
        <button className="btn btn-primary" onClick={() => setAdding('url')}><Plus size={14} aria-hidden="true" />{t('Add subscription URL')}</button>
      </div>
    </header>
    <div className="space-y-1 border-b border-line px-5 py-4 text-ink-dim">
      <p>{t('Import Clash/Mihomo YAML subscriptions or configuration files. Only the proxies list is used; rules, DNS, TUN and controller settings from providers are ignored.')}</p>
      <p>{t('Subscription links and node passwords are encrypted and never shown again. Locations come only from checks through each node; refreshing a subscription never moves a running profile to another node.')}</p>
    </div>
    {status && !status.encryption_available && <p className="border-b border-line px-5 py-2 text-[12px] text-danger" role="alert">{t('No encryption key is configured, so subscriptions cannot be stored. Restore secret.key or set CPM_SECRET_KEY.')}</p>}

    {error ? <EmptyState icon={<Rss size={18} />} title={t('Cannot load subscriptions')} body={t(error)} action={<button className="btn btn-default" onClick={load}>{t('Retry')}</button>} />
      : loading ? <p className="px-5 py-8 text-ink-dim" role="status">{t('Loading…')}</p>
      : !subscriptions.length ? <EmptyState icon={<Rss size={18} />} title={t('No subscriptions yet')} body={t('Paste a Clash/Mihomo subscription URL from your provider, or import a .yaml configuration file.')} action={<button className="btn btn-primary" onClick={() => setAdding('url')}>{t('Add subscription URL')}</button>} />
      : <>
        <div className="grid gap-3 px-5 py-4 md:grid-cols-2 2xl:grid-cols-3">
          {subscriptions.map(subscription => {
            const usage = usageSummary(subscription.usage)
            const busy = syncing.has(subscription.id)
            return <article key={subscription.id} className={`panel p-4 ${filter === subscription.id ? 'ring-1 ring-signal' : ''}`}>
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <h2 className="truncate font-semibold" title={subscription.name}>{subscription.name}</h2>
                  <p className="truncate font-mono text-[11px] text-ink-dim">{subscription.source_display}</p>
                </div>
                <span className={`shrink-0 rounded px-1.5 py-0.5 text-[11px] ${subscription.status === 'error' ? 'bg-danger-shade text-danger' : 'bg-raised text-ink-dim'}`}>{subscription.status === 'error' ? t('Last sync failed') : subscription.source_kind === 'url' ? t('URL') : t('File')}</span>
              </div>
              <p className="mt-2 text-[12px]">
                <span className="text-ok">{t('{count} active', { count: subscription.node_counts.active })}</span>
                {subscription.node_counts.retired > 0 && <span className="text-ink-dim"> · {t('{count} retired', { count: subscription.node_counts.retired })}</span>}
                {subscription.bound_profiles > 0 && <span className="text-ink-dim"> · {t('{count} bound profiles', { count: subscription.bound_profiles })}</span>}
              </p>
              {usage && <div className="mt-2">
                <div className="flex justify-between text-[11px] text-ink-dim"><span>{formatBytes(usage.used, locale)}{usage.total ? ` / ${formatBytes(usage.total, locale)}` : ''}</span>{usage.expires && <span>{t('Expires {date}', { date: usage.expires.toLocaleDateString(locale) })}</span>}</div>
                {usage.percent != null && <div className="mt-1 h-1 overflow-hidden rounded-full bg-raised" role="meter" aria-valuemin={0} aria-valuemax={100} aria-valuenow={usage.percent} aria-label={t('Traffic used')}><div className={`h-full ${usage.percent > 90 ? 'bg-danger' : usage.percent > 75 ? 'bg-warn' : 'bg-ok'}`} style={{ width: `${usage.percent}%` }} /></div>}
              </div>}
              <p className="mt-2 text-[11px] text-ink-faint">{subscription.last_synced_at ? t('Synced {time}', { time: new Date(subscription.last_synced_at).toLocaleString(locale) }) : t('Never synced')}</p>
              {subscription.last_error && <p className="mt-1 flex items-start gap-1.5 text-[12px] text-danger"><TriangleAlert size={12} className="mt-0.5 shrink-0" aria-hidden="true" />{t(subscription.last_error)}</p>}
              <div className="mt-3 flex flex-wrap gap-1.5">
                <button className="btn btn-default" disabled={busy} onClick={() => { if (subscription.source_kind === 'url') sync(subscription); else { refreshTarget.current = subscription; refreshFile.current?.click() } }}>
                  {busy ? <LoaderCircle size={13} className="animate-spin motion-reduce:animate-none" aria-hidden="true" /> : <RefreshCw size={13} aria-hidden="true" />}
                  {subscription.source_kind === 'url' ? t('Sync now') : t('Re-import file')}
                </button>
                <button className="btn btn-default" onClick={() => setFilter(filter === subscription.id ? null : subscription.id)}>{filter === subscription.id ? t('Show all nodes') : t('Show nodes')}</button>
                <button className="btn btn-ghost h-[30px] w-[30px] p-0" onClick={() => setRenaming(subscription)} aria-label={t('Rename {name}', { name: subscription.name })}><Pencil size={13} aria-hidden="true" /></button>
                <button className="btn btn-ghost h-[30px] w-[30px] p-0 hover:text-danger" onClick={() => setDeleting(subscription)} aria-label={t('Delete {name}', { name: subscription.name })}><Trash2 size={13} aria-hidden="true" /></button>
              </div>
            </article>
          })}
        </div>
        <input ref={refreshFile} type="file" accept=".yaml,.yml,.txt,.conf" className="hidden" aria-hidden="true" tabIndex={-1} onChange={async event => {
          const file = event.target.files?.[0]; event.target.value = ''
          const target = refreshTarget.current
          if (!file || !target) return
          const content = await readFile(file)
          if (content != null) await sync(target, content)
        }} />

        <div className="flex flex-wrap items-center gap-3 border-y border-line px-5 py-2">
          <h2 className="font-semibold">{t('Nodes')}</h2><span className="font-mono text-ink-faint">{shown.length}</span>
          <div className="relative min-w-[160px] max-w-[260px] flex-1"><Search size={13} aria-hidden="true" className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-ink-dim" /><input className="field h-[30px] pl-7" aria-label={t('Search nodes')} placeholder={t('Search nodes')} value={search} onChange={event => setSearch(event.target.value)} /></div>
          <div className="ml-auto">{jobActive
            ? <span className="flex items-center gap-1.5 text-ink-dim" role="status"><LoaderCircle size={13} className="animate-spin motion-reduce:animate-none" aria-hidden="true" />{t('Checking {done}/{total}', { done: job.done, total: job.total })}</span>
            : <button className="btn btn-default" onClick={checkVisible} disabled={!status?.engine.available || !shown.length}><Activity size={13} aria-hidden="true" />{t('Check shown nodes')}</button>}</div>
        </div>
        {status && !status.engine.available && <p className="px-5 py-2 text-[12px] text-warn">{t(status.engine.message ?? 'Mihomo is not available.')}</p>}
        <div className="overflow-x-auto"><table className="w-full min-w-[920px] border-collapse text-left">
          <thead><tr className="border-b border-line text-[11px] uppercase tracking-[0.05em] text-ink-faint">
            <th className="px-5 py-2 font-medium">{t('Node')}</th><th className="px-3 py-2 font-medium">{t('Protocol')}</th><th className="px-3 py-2 font-medium">{t('Exit')}</th><th className="px-3 py-2 font-medium">{t('Location')}</th><th className="px-3 py-2 font-medium">{t('IP quality')}</th><th className="px-5 py-2 text-right font-medium">{t('Actions')}</th>
          </tr></thead>
          <tbody>{shown.map(node => {
            const checkData = node.check
            const composite = node.intel?.composite
            const tone = purityTone(composite?.purity_score)
            const busy = checking.has(node.id)
            return <tr key={node.id} className="border-b border-line/60 hover:bg-surface">
              <td className="px-5 py-2.5"><p className="font-medium">{node.name}</p><p className="text-[11px] text-ink-dim">{node.subscription_name}{node.status !== 'active' ? ` · ${t(node.status === 'retired' ? 'Retired' : 'Invalid')}` : ''}{node.profile_count ? ` · ${t('{count} bound profiles', { count: node.profile_count })}` : ''}</p></td>
              <td className="px-3 py-2.5 font-mono text-[12px]">{node.protocol}</td>
              <td className="px-3 py-2.5">{checkData ? <>
                <p className={`font-mono text-[12px] ${checkData.health === 'ok' ? '' : 'text-danger'}`}>{checkData.health === 'ok' ? maskIp(checkData.exit_ip, maskIps) : t('Unreachable')}</p>
                <p className="text-[11px] text-ink-dim">{checkData.latency_ms != null ? `${checkData.latency_ms} ms` : ''}{node.shared_exit_count > 1 ? ` · ${t('shared by {count}', { count: node.shared_exit_count })}` : ''}</p>
              </> : <span className="text-ink-dim">{t('Not checked')}</span>}</td>
              <td className="px-3 py-2.5 text-[12px]">{checkData?.country_iso ? `${flag(checkData.country_iso)} ${countryName(checkData.country_iso, locale)}${checkData.city ? ` · ${checkData.city}` : ''}` : <span className="text-ink-dim">{t('Unknown')}</span>}</td>
              <td className="px-3 py-2.5 text-[12px]">{composite?.purity_score != null ? <span className={TONE_TEXT[tone]}>{composite.purity_score} · {t(gradeLabel(composite.grade))}</span> : <span className="text-ink-dim">—</span>}</td>
              <td className="px-5 py-2.5"><div className="flex justify-end"><button className="btn btn-default" disabled={busy || node.status !== 'active' || !status?.engine.available} onClick={() => check(node)}>{busy ? <LoaderCircle size={13} className="animate-spin motion-reduce:animate-none" aria-hidden="true" /> : <Activity size={13} aria-hidden="true" />}{busy ? t('Checking…') : t('Check')}</button></div></td>
            </tr>
          })}</tbody>
        </table></div>
      </>}

    {adding && <AddSubscription mode={adding} onClose={() => setAdding(null)} readFile={readFile} onAdded={async (name, syncReport) => { setAdding(null); setReport({ name, report: syncReport }); await load() }} />}
    {report && <Modal open title={t('Import finished')} subtitle={report.name} width={480} onClose={() => setReport(null)} footer={<button className="btn btn-primary" onClick={() => setReport(null)}>{t('Done')}</button>}>
      <dl className="grid grid-cols-2 gap-2 text-[13px]">
        <dt className="text-ink-dim">{t('New nodes')}</dt><dd className="font-mono">{report.report.created}</dd>
        <dt className="text-ink-dim">{t('Updated')}</dt><dd className="font-mono">{report.report.updated}</dd>
        <dt className="text-ink-dim">{t('Retired (no longer listed)')}</dt><dd className="font-mono">{report.report.retired}</dd>
        <dt className="text-ink-dim">{t('Unchanged')}</dt><dd className="font-mono">{report.report.unchanged}</dd>
      </dl>
      {report.report.warnings.length > 0 && <div className="mt-3"><p className="text-warn">{t('{count} entries were skipped:', { count: report.report.warnings.length })}</p><ul className="mt-1 max-h-[160px] overflow-y-auto text-[12px] text-ink-dim">{report.report.warnings.map(warning => <li key={warning.index}>{t('Entry {index}: {reason}', { index: warning.index, reason: t(warning.reason) })}</li>)}</ul></div>}
      <p className="mt-3 text-ink-dim">{t('Check nodes to measure their real exits and place them on the map.')}</p>
    </Modal>}
    {renaming && <RenameSubscription subscription={renaming} onClose={() => setRenaming(null)} onSaved={async () => { setRenaming(null); await load() }} />}
    {deleting && <DeleteSubscription subscription={deleting} onClose={() => setDeleting(null)} onDeleted={async () => { setDeleting(null); setFilter(null); await load() }} />}
  </>
}

function AddSubscription({ mode, onClose, onAdded, readFile }: { mode: 'url' | 'file'; onClose: () => void; onAdded: (name: string, report: SyncReport) => void; readFile: (file: File) => Promise<string | null> }) {
  const { t } = useI18n()
  const [name, setName] = useState('')
  const [url, setUrl] = useState('')
  const [content, setContent] = useState('')
  const [fileName, setFileName] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit() {
    setBusy(true); setError(null)
    try {
      const result = mode === 'url' ? await networkAPI.createFromUrl(url.trim(), name.trim()) : await networkAPI.createFromFile(content, fileName || 'pasted.yaml', name.trim())
      onAdded(result.subscription.name, result.report)
    } catch (err) { setError(t(err instanceof Error ? err.message : String(err))) }
    finally { setBusy(false) }
  }

  const ready = mode === 'url' ? url.trim().startsWith('https://') : content.trim().length > 0
  return <Modal open title={mode === 'url' ? t('Add subscription URL') : t('Import configuration file')} width={560} onClose={() => { if (!busy) onClose() }} footer={<>
    <button className="btn btn-default" onClick={onClose} disabled={busy}>{t('Cancel')}</button>
    <button className="btn btn-primary" onClick={submit} disabled={busy || !ready}>{busy ? <LoaderCircle size={13} className="animate-spin motion-reduce:animate-none" aria-hidden="true" /> : null}{busy ? t('Importing…') : t('Import')}</button>
  </>}>
    <label className="field-label" htmlFor="subscription-name">{t('Name (optional)')}</label>
    <input id="subscription-name" className="field" value={name} maxLength={80} onChange={event => setName(event.target.value)} placeholder={t('Taken from the provider when empty')} />
    {mode === 'url' ? <>
      <label className="field-label mt-3" htmlFor="subscription-url">{t('Subscription URL')}</label>
      <input id="subscription-url" className="field font-mono" type="url" autoComplete="off" spellCheck={false} value={url} onChange={event => setUrl(event.target.value)} placeholder="https://" />
      <p className="mt-2 text-[12px] text-ink-dim">{t('Use the Clash/Mihomo link from your provider. The manager asks for YAML (User-Agent clash.meta) and refuses non-HTTPS links, redirects to private addresses and files over 10 MB. The link is fetched directly from this computer.')}</p>
    </> : <>
      <label className="field-label mt-3" htmlFor="subscription-file">{t('Configuration file')}</label>
      <input id="subscription-file" className="field" type="file" accept=".yaml,.yml,.txt,.conf" onChange={async event => {
        const file = event.target.files?.[0]
        if (!file) return
        const text = await readFile(file)
        if (text != null) { setContent(text); setFileName(file.name) }
      }} />
      <label className="field-label mt-3" htmlFor="subscription-text">{t('Or paste YAML')}</label>
      <textarea id="subscription-text" className="field h-[140px] resize-y font-mono text-[12px]" spellCheck={false} value={content} onChange={event => { setContent(event.target.value); if (!fileName) setFileName('pasted.yaml') }} placeholder="proxies:" />
    </>}
    {error && <p className="mt-3 text-danger" role="alert">{error}</p>}
  </Modal>
}

function RenameSubscription({ subscription, onClose, onSaved }: { subscription: Subscription; onClose: () => void; onSaved: () => void }) {
  const { t } = useI18n()
  const [name, setName] = useState(subscription.name)
  const [error, setError] = useState<string | null>(null)
  async function save() {
    try { await networkAPI.rename(subscription.id, name.trim(), subscription.row_version); onSaved() }
    catch (err) { setError(isStaleWrite(err) ? t('This subscription changed elsewhere. Close and try again.') : t(err instanceof Error ? err.message : String(err))) }
  }
  return <Modal open title={t('Rename subscription')} width={420} onClose={onClose} footer={<><button className="btn btn-default" onClick={onClose}>{t('Cancel')}</button><button className="btn btn-primary" disabled={!name.trim()} onClick={save}>{t('Save')}</button></>}>
    <label className="field-label" htmlFor="rename-subscription">{t('Name')}</label>
    <input id="rename-subscription" className="field" value={name} maxLength={80} onChange={event => setName(event.target.value)} />
    {error && <p className="mt-2 text-danger" role="alert">{error}</p>}
  </Modal>
}

function DeleteSubscription({ subscription, onClose, onDeleted }: { subscription: Subscription; onClose: () => void; onDeleted: () => void }) {
  const { t } = useI18n()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const bound = subscription.bound_profiles > 0
  async function remove() {
    setBusy(true); setError(null)
    try { await networkAPI.removeSubscription(subscription.id, bound); onDeleted() }
    catch (err) { setError(err instanceof ApiRequestError && err.code === 'subscription_bound' ? t('Profiles were bound in the meantime. Close and review.') : t(err instanceof Error ? err.message : String(err))) }
    finally { setBusy(false) }
  }
  return <Modal open title={t('Delete subscription')} width={460} onClose={() => { if (!busy) onClose() }} footer={<><button className="btn btn-default" onClick={onClose} disabled={busy}>{t('Cancel')}</button><button className="btn btn-danger" onClick={remove} disabled={busy}>{bound ? t('Retire and delete') : t('Delete')}</button></>}>
    <p>{t('Delete "{name}" and its nodes?', { name: subscription.name })}</p>
    {bound ? <p className="mt-2 text-warn">{t('{count} profiles are bound to its nodes. Those nodes are kept as retired, so the profiles refuse to launch until you bind another node. They never fall back to another exit.', { count: subscription.bound_profiles })}</p>
      : <p className="mt-2 text-ink-dim">{t('No profile uses these nodes. Measurements are removed with them.')}</p>}
    {error && <p className="mt-2 text-danger" role="alert">{error}</p>}
  </Modal>
}
