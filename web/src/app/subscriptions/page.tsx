'use client'

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import Link from 'next/link'
import { Activity, FileUp, Globe2, LoaderCircle, Pencil, Plus, RefreshCw, Rss, Search, Server, Trash2, TriangleAlert } from 'lucide-react'

import { EmptyState } from '@/components/empty-state'
import { useI18n } from '@/components/i18n-provider'
import { Modal } from '@/components/modal'
import { useToast } from '@/components/toast'
import { ApiRequestError, isStaleWrite } from '@/lib/api'
import { networkAPI, type BatchJob, type ManualNodeInput, type ManualNodesResult, type NetworkNode, type NetworkStatus, type SimpleProxyType, type Subscription, type SyncReport } from '@/lib/network-api'
import { countryName, flag, formatBytes, gradeLabel, maskIp, purityTone, usageSummary } from '@/lib/network-map'

const MAX_FILE = 10 * 1024 * 1024
const TONE_TEXT = { ok: 'text-ok', warn: 'text-warn', danger: 'text-danger', unknown: 'text-ink-dim' } as const
/** Matches the catalog's invalid_reason for panel lines such as "到期時間：…". */
const INFORMATIONAL = 'informational entry'

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
  const [addingManual, setAddingManual] = useState<{ groupId: string | null } | null>(null)
  const [manualReport, setManualReport] = useState<ManualNodesResult | null>(null)
  const [editing, setEditing] = useState<NetworkNode | null>(null)
  const [removingNode, setRemovingNode] = useState<NetworkNode | null>(null)
  const [report, setReport] = useState<{ name: string; report: SyncReport; checking: boolean } | null>(null)
  const [renaming, setRenaming] = useState<Subscription | null>(null)
  const [deleting, setDeleting] = useState<Subscription | null>(null)
  const [syncing, setSyncing] = useState<Set<string>>(new Set())
  const [checking, setChecking] = useState<Set<string>>(new Set())
  const [job, setJob] = useState<BatchJob | null>(null)
  const refreshFile = useRef<HTMLInputElement>(null)
  const refreshTarget = useRef<Subscription | null>(null)

  const load = useCallback(async (): Promise<{ nodes: NetworkNode[]; status: NetworkStatus } | null> => {
    try {
      const [subs, nodeData, engine] = await Promise.all([networkAPI.subscriptions(), networkAPI.nodes({ limit: 1000 }), networkAPI.status()])
      setSubscriptions(subs); setNodes(nodeData.nodes); setStatus(engine); setError(null)
      return { nodes: nodeData.nodes, status: engine }
    } catch (err) { setError(err instanceof Error ? err.message : String(err)); return null }
    finally { setLoading(false) }
  }, [])
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    load()
  }, [load])
  const groups = useMemo(() => Object.fromEntries(subscriptions.map(entry => [entry.id, entry])), [subscriptions])

  const maskIps = status?.settings.mask_exit_ips ?? false
  const shown = useMemo(() => nodes.filter(node => (!filter || node.subscription_id === filter) && (!search || [node.name, node.protocol, node.check?.exit_ip, node.check?.country_iso, node.check?.city].some(value => value?.toLowerCase().includes(search.toLowerCase())))), [nodes, filter, search])

  function setBusy(setter: typeof setSyncing, id: string, on: boolean) {
    setter(current => { const next = new Set(current); if (on) next.add(id); else next.delete(id); return next })
  }

  async function sync(subscription: Subscription, content?: string) {
    setBusy(setSyncing, subscription.id, true)
    try {
      const result = await networkAPI.sync(subscription.id, content)
      const fresh = await load()
      const checking = autoCheck(fresh, node => node.subscription_id === subscription.id)
      setReport({ name: subscription.name, report: result.report, checking })
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

  /** Check new nodes right away so they reach the map without another click. */
  function autoCheck(fresh: { nodes: NetworkNode[]; status: NetworkStatus } | null, pick: (node: NetworkNode) => boolean): boolean {
    if (!fresh?.status.engine.available) return false
    const ids = fresh.nodes.filter(node => node.status === 'active' && !node.check && pick(node)).map(node => node.id)
    if (!ids.length) return false
    void runBatch(ids)
    return true
  }

  async function runBatch(ids: string[]) {
    if (!ids.length) return
    try {
      let current: BatchJob
      try {
        current = await networkAPI.checkBatch(ids.slice(0, 500))
      } catch (err) {
        if (!(err instanceof ApiRequestError && err.status === 409)) throw err
        // Only one batch runs at a time; follow the one already running.
        const running = await networkAPI.currentJob()
        if (!running) throw err
        toast('info', t('A check is already running'), t('Its progress is shown here; start the next one when it ends.'))
        current = running
      }
      setJob(current)
      for (let tick = 0; current.state === 'queued' || current.state === 'running'; tick += 1) {
        await new Promise(resolve => setTimeout(resolve, 800))
        current = await networkAPI.job(current.id)
        setJob(current)
        if (tick % 4 === 3) await load()
      }
      toast(current.state === 'finished' ? 'ok' : 'error', t('Batch check finished'), t('{ok} reachable, {failed} failed, one engine process.', { ok: current.ok, failed: current.failed }))
      await load()
    } catch (err) { toast('error', t('Could not start the batch check'), t(err instanceof Error ? err.message : String(err))) }
    finally { setJob(null) }
  }

  function checkVisible() {
    return runBatch(shown.filter(node => node.status === 'active').map(node => node.id))
  }

  const jobActive = job && (job.state === 'queued' || job.state === 'running')

  return <>
    <header className="sticky top-0 z-20 flex min-h-[52px] flex-wrap items-center gap-3 border-b border-line bg-canvas/85 px-5 py-2 backdrop-blur">
      <h1 className="text-[14px] font-semibold">{t('Proxy subscriptions')}</h1><span className="font-mono text-ink-faint">{subscriptions.length}</span>
      <div className="ml-auto flex flex-wrap gap-2">
        <Link className="btn btn-default" href="/map/"><Globe2 size={13} aria-hidden="true" />{t('Open map')}</Link>
        <button className="btn btn-default" onClick={() => setAddingManual({ groupId: null })}><Server size={13} aria-hidden="true" />{t('Add fixed nodes')}</button>
        <button className="btn btn-default" onClick={() => setAdding('file')}><FileUp size={13} aria-hidden="true" />{t('Import file')}</button>
        <button className="btn btn-primary" onClick={() => setAdding('url')}><Plus size={14} aria-hidden="true" />{t('Add subscription URL')}</button>
      </div>
    </header>
    <div className="space-y-1 border-b border-line px-5 py-4 text-ink-dim">
      <p>{t('Import airport subscriptions (Clash/Mihomo YAML, share links or Base64 lists), or add fixed proxies you bought as IP, port, username and password. Only the proxies are used; rules, DNS, TUN and controller settings from providers are ignored.')}</p>
      <p>{t('Subscription links and node passwords are encrypted and never shown again. Locations come only from checks through each node; refreshing a subscription never moves a running profile to another node.')}</p>
    </div>
    {status && !status.encryption_available && <p className="border-b border-line px-5 py-2 text-[12px] text-danger" role="alert">{t('No encryption key is configured, so subscriptions cannot be stored. Restore secret.key or set CPM_SECRET_KEY.')}</p>}

    {error ? <EmptyState icon={<Rss size={18} />} title={t('Cannot load subscriptions')} body={t(error)} action={<button className="btn btn-default" onClick={load}>{t('Retry')}</button>} />
      : loading ? <p className="px-5 py-8 text-ink-dim" role="status">{t('Loading…')}</p>
      : !subscriptions.length ? <EmptyState icon={<Rss size={18} />} title={t('No subscriptions yet')} body={t('Paste a subscription URL from your airport, import a configuration file, or add fixed proxies you bought.')} action={<div className="flex flex-wrap justify-center gap-2"><button className="btn btn-primary" onClick={() => setAdding('url')}>{t('Add subscription URL')}</button><button className="btn btn-default" onClick={() => setAddingManual({ groupId: null })}>{t('Add fixed nodes')}</button></div>} />
      : <>
        <div className="grid gap-3 px-5 py-4 md:grid-cols-2 2xl:grid-cols-3">
          {subscriptions.map(subscription => {
            const usage = usageSummary(subscription.usage)
            const busy = syncing.has(subscription.id)
            return <article key={subscription.id} className={`panel p-4 ${filter === subscription.id ? 'ring-1 ring-signal' : ''}`}>
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <h2 className="truncate font-semibold" title={subscription.name}>{subscription.name}</h2>
                  <p className="truncate font-mono text-[11px] text-ink-dim">{subscription.manual ? t('Added by hand') : subscription.source_display}</p>
                </div>
                <span className={`shrink-0 rounded px-1.5 py-0.5 text-[11px] ${subscription.status === 'error' ? 'bg-danger-shade text-danger' : subscription.manual ? 'bg-signal-shade text-signal-soft' : 'bg-raised text-ink-dim'}`}>{subscription.status === 'error' ? t('Last sync failed') : subscription.manual ? t('Fixed nodes') : subscription.source_kind === 'url' ? t('URL') : t('File')}</span>
              </div>
              <p className="mt-2 text-[12px]">
                <span className="text-ok">{t('{count} active', { count: subscription.node_counts.active })}</span>
                {subscription.node_counts.retired > 0 && <span className="text-ink-dim"> · {t('{count} retired', { count: subscription.node_counts.retired })}</span>}
                {subscription.node_counts.invalid > 0 && <span className="text-ink-dim"> · {t('{count} info or invalid entries', { count: subscription.node_counts.invalid })}</span>}
                {subscription.bound_profiles > 0 && <span className="text-ink-dim"> · {t('{count} bound profiles', { count: subscription.bound_profiles })}</span>}
              </p>
              {usage && <div className="mt-2">
                <div className="flex justify-between text-[11px] text-ink-dim"><span>{formatBytes(usage.used, locale)}{usage.total ? ` / ${formatBytes(usage.total, locale)}` : ''}</span>{usage.expires && <span>{t('Expires {date}', { date: usage.expires.toLocaleDateString(locale) })}</span>}</div>
                {usage.percent != null && <div className="mt-1 h-1 overflow-hidden rounded-full bg-raised" role="meter" aria-valuemin={0} aria-valuemax={100} aria-valuenow={usage.percent} aria-label={t('Traffic used')}><div className={`h-full ${usage.percent > 90 ? 'bg-danger' : usage.percent > 75 ? 'bg-warn' : 'bg-ok'}`} style={{ width: `${usage.percent}%` }} /></div>}
              </div>}
              {!subscription.manual && <p className="mt-2 text-[11px] text-ink-faint">{subscription.last_synced_at ? t('Synced {time}', { time: new Date(subscription.last_synced_at).toLocaleString(locale) }) : t('Never synced')}</p>}
              {subscription.last_error && <p className="mt-1 flex items-start gap-1.5 text-[12px] text-danger"><TriangleAlert size={12} className="mt-0.5 shrink-0" aria-hidden="true" />{t(subscription.last_error)}</p>}
              <div className="mt-3 flex flex-wrap gap-1.5">
                {subscription.manual
                  ? <button className="btn btn-default" onClick={() => setAddingManual({ groupId: subscription.id })}><Plus size={13} aria-hidden="true" />{t('Add nodes')}</button>
                  : <button className="btn btn-default" disabled={busy} onClick={() => { if (subscription.source_kind === 'url') sync(subscription); else { refreshTarget.current = subscription; refreshFile.current?.click() } }}>
                    {busy ? <LoaderCircle size={13} className="animate-spin motion-reduce:animate-none" aria-hidden="true" /> : <RefreshCw size={13} aria-hidden="true" />}
                    {subscription.source_kind === 'url' ? t('Sync now') : t('Re-import file')}
                  </button>}
                <Link className="btn btn-ghost" href={`/map/?source=${subscription.id}`}><Globe2 size={13} aria-hidden="true" />{t('Map')}</Link>
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
            const informational = node.invalid_reason === INFORMATIONAL
            const manual = groups[node.subscription_id]?.manual ?? false
            const statusText = node.status === 'active' ? '' : informational ? t('Provider info, not a node') : t(node.status === 'retired' ? 'Retired' : 'Invalid')
            return <tr key={node.id} className={`border-b border-line/60 hover:bg-surface ${informational ? 'text-ink-dim' : ''}`}>
              <td className="px-5 py-2.5"><p className="font-medium">{node.name}</p><p className="text-[11px] text-ink-dim">{node.subscription_name}{statusText ? ` · ${statusText}` : ''}{node.profile_count ? ` · ${t('{count} bound profiles', { count: node.profile_count })}` : ''}{manual ? <span className="font-mono"> · {node.entry}</span> : null}</p></td>
              <td className="px-3 py-2.5 font-mono text-[12px]">{node.protocol}</td>
              <td className="px-3 py-2.5">{checkData ? <>
                <p className={`font-mono text-[12px] ${checkData.health === 'ok' ? '' : 'text-danger'}`}>{checkData.health === 'ok' ? maskIp(checkData.exit_ip, maskIps) : t('Unreachable')}</p>
                <p className="text-[11px] text-ink-dim">{checkData.latency_ms != null ? `${checkData.latency_ms} ms` : ''}{node.shared_exit_count > 1 ? ` · ${t('shared by {count}', { count: node.shared_exit_count })}` : ''}</p>
              </> : <span className="text-ink-dim">{t('Not checked')}</span>}</td>
              <td className="px-3 py-2.5 text-[12px]">{checkData?.country_iso ? `${flag(checkData.country_iso)} ${countryName(checkData.country_iso, locale)}${checkData.city ? ` · ${checkData.city}` : ''}` : <span className="text-ink-dim">{t('Unknown')}</span>}</td>
              <td className="px-3 py-2.5 text-[12px]">{composite?.purity_score != null ? <span className={TONE_TEXT[tone]}>{composite.purity_score} · {t(gradeLabel(composite.grade))}</span> : <span className="text-ink-dim">—</span>}</td>
              <td className="px-5 py-2.5"><div className="flex justify-end gap-1">
                {!informational && <button className="btn btn-default" disabled={busy || node.status !== 'active' || !status?.engine.available} onClick={() => check(node)}>{busy ? <LoaderCircle size={13} className="animate-spin motion-reduce:animate-none" aria-hidden="true" /> : <Activity size={13} aria-hidden="true" />}{busy ? t('Checking…') : t('Check')}</button>}
                {manual && <>
                  <button className="btn btn-ghost h-[30px] w-[30px] p-0" onClick={() => setEditing(node)} aria-label={t('Edit {name}', { name: node.name })} title={t('Edit')}><Pencil size={13} aria-hidden="true" /></button>
                  <button className="btn btn-ghost h-[30px] w-[30px] p-0 hover:text-danger" onClick={() => setRemovingNode(node)} aria-label={t('Delete {name}', { name: node.name })} title={t('Delete')}><Trash2 size={13} aria-hidden="true" /></button>
                </>}
              </div></td>
            </tr>
          })}</tbody>
        </table></div>
      </>}

    {adding && <AddSubscription mode={adding} onClose={() => setAdding(null)} readFile={readFile} onAdded={async (subscription, syncReport) => {
      setAdding(null)
      const fresh = await load()
      const checking = autoCheck(fresh, node => node.subscription_id === subscription.id)
      setReport({ name: subscription.name, report: syncReport, checking })
    }} />}
    {addingManual && <AddManualNodes groups={subscriptions.filter(entry => entry.manual)} initialGroupId={addingManual.groupId} onClose={() => setAddingManual(null)} onAdded={async result => {
      setAddingManual(null)
      const created = new Set(result.created.map(node => node.id))
      const fresh = await load()
      autoCheck(fresh, node => created.has(node.id))
      if (result.skipped.length) setManualReport(result)
      else toast('ok', t('Fixed nodes added'), t('{count} added to {group}. Checking them now.', { count: result.created.length, group: result.group.name }))
    }} />}
    {manualReport && <Modal open title={t('Fixed nodes added')} subtitle={manualReport.group.name} width={480} onClose={() => setManualReport(null)} footer={<button className="btn btn-primary" onClick={() => setManualReport(null)}>{t('Done')}</button>}>
      <p>{t('{count} added. They are being checked now.', { count: manualReport.created.length })}</p>
      <p className="mt-3 text-warn">{t('{count} lines were skipped:', { count: manualReport.skipped.length })}</p>
      <ul className="mt-1 max-h-[180px] overflow-y-auto text-[12px] text-ink-dim">{manualReport.skipped.map(item => <li key={item.line}>{t('Line {line}: {reason}', { line: item.line, reason: t(item.reason) })}</li>)}</ul>
    </Modal>}
    {editing && <EditManualNode node={editing} onClose={() => setEditing(null)} onSaved={async () => { setEditing(null); await load() }} />}
    {removingNode && <DeleteManualNode node={removingNode} onClose={() => setRemovingNode(null)} onDeleted={async () => { setRemovingNode(null); await load() }} />}
    {report && <Modal open title={t('Import finished')} subtitle={report.name} width={480} onClose={() => setReport(null)} footer={<button className="btn btn-primary" onClick={() => setReport(null)}>{t('Done')}</button>}>
      <dl className="grid grid-cols-2 gap-2 text-[13px]">
        <dt className="text-ink-dim">{t('New nodes')}</dt><dd className="font-mono">{report.report.created}</dd>
        <dt className="text-ink-dim">{t('Updated')}</dt><dd className="font-mono">{report.report.updated}</dd>
        <dt className="text-ink-dim">{t('Retired (no longer listed)')}</dt><dd className="font-mono">{report.report.retired}</dd>
        <dt className="text-ink-dim">{t('Unchanged')}</dt><dd className="font-mono">{report.report.unchanged}</dd>
      </dl>
      {report.report.warnings.length > 0 && <div className="mt-3"><p className="text-warn">{t('{count} entries were skipped:', { count: report.report.warnings.length })}</p><ul className="mt-1 max-h-[160px] overflow-y-auto text-[12px] text-ink-dim">{report.report.warnings.map(warning => <li key={warning.index}>{t('Entry {index}: {reason}', { index: warning.index, reason: t(warning.reason) })}</li>)}</ul></div>}
      <p className="mt-3 text-ink-dim">{report.checking ? t('The new nodes are being checked now; their real exits appear on the map as results arrive.') : t('Check nodes to measure their real exits and place them on the map.')}</p>
    </Modal>}
    {renaming && <RenameSubscription subscription={renaming} onClose={() => setRenaming(null)} onSaved={async () => { setRenaming(null); await load() }} />}
    {deleting && <DeleteSubscription subscription={deleting} onClose={() => setDeleting(null)} onDeleted={async () => { setDeleting(null); setFilter(null); await load() }} />}
  </>
}

function AddSubscription({ mode, onClose, onAdded, readFile }: { mode: 'url' | 'file'; onClose: () => void; onAdded: (subscription: Subscription, report: SyncReport) => void; readFile: (file: File) => Promise<string | null> }) {
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
      onAdded(result.subscription, result.report)
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
      <p className="mt-2 text-[12px] text-ink-dim">{t('Use the Clash/Mihomo link from your provider; share-link and Base64 subscriptions also work. The manager asks for YAML (User-Agent clash.meta) and refuses non-HTTPS links, redirects to private addresses and files over 10 MB. The link is fetched directly from this computer.')}</p>
    </> : <>
      <label className="field-label mt-3" htmlFor="subscription-file">{t('Configuration file')}</label>
      <input id="subscription-file" className="field" type="file" accept=".yaml,.yml,.txt,.conf" onChange={async event => {
        const file = event.target.files?.[0]
        if (!file) return
        const text = await readFile(file)
        if (text != null) { setContent(text); setFileName(file.name) }
      }} />
      <label className="field-label mt-3" htmlFor="subscription-text">{t('Or paste YAML, share links or Base64')}</label>
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

const PLACEHOLDER = `1.2.3.4:8080
1.2.3.4:8080:user:password
user:password@proxy.example.com:1080
socks5://user:password@1.2.3.4:1080
ss://… vmess://… vless://… trojan://… hy2://…`

function AddManualNodes({ groups, initialGroupId, onClose, onAdded }: { groups: Subscription[]; initialGroupId: string | null; onClose: () => void; onAdded: (result: ManualNodesResult) => void }) {
  const { t } = useI18n()
  const [groupId, setGroupId] = useState<string>(initialGroupId ?? groups[0]?.id ?? '')
  const [groupName, setGroupName] = useState('')
  const [tab, setTab] = useState<'form' | 'paste'>('form')
  const [node, setNode] = useState<{ name: string; type: SimpleProxyType; server: string; port: string; username: string; password: string }>({ name: '', type: 'http', server: '', port: '', username: '', password: '' })
  const [text, setText] = useState('')
  const [defaultType, setDefaultType] = useState<SimpleProxyType>('http')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const port = Number(node.port)
  const formReady = node.server.trim().length > 0 && Number.isInteger(port) && port >= 1 && port <= 65535
  const groupReady = groupId !== '' || groupName.trim().length > 0
  const ready = groupReady && (tab === 'form' ? formReady : text.trim().length > 0)

  async function submit() {
    setBusy(true); setError(null)
    const target = groupId ? { group_id: groupId } : { group_name: groupName.trim() }
    try {
      let result: ManualNodesResult
      if (tab === 'form') {
        const input: ManualNodeInput = { type: node.type, server: node.server.trim(), port }
        if (node.name.trim()) input.name = node.name.trim()
        if (node.username) input.username = node.username
        if (node.password) input.password = node.password
        result = await networkAPI.addManualNode(target, input)
      } else {
        result = await networkAPI.addManualText(target, text, defaultType)
      }
      if (!result.created.length) {
        setError(result.skipped.length ? t('Nothing was added. {reason}', { reason: result.skipped.map(item => t('Line {line}: {reason}', { line: item.line, reason: t(item.reason) })).join('; ') }) : t('Nothing was added.'))
        return
      }
      onAdded(result)
    } catch (err) { setError(t(err instanceof Error ? err.message : String(err))) }
    finally { setBusy(false) }
  }

  const tabClass = (active: boolean) => `rounded px-2.5 py-1 text-[12px] ${active ? 'bg-surface font-medium text-ink shadow-[0_0_0_1px_var(--color-line)]' : 'text-ink-dim hover:text-ink'}`
  return <Modal open title={t('Add fixed nodes')} subtitle={t('Proxies you bought: IP, port, username and password')} width={580} onClose={() => { if (!busy) onClose() }} footer={<>
    <button className="btn btn-default" onClick={onClose} disabled={busy}>{t('Cancel')}</button>
    <button className="btn btn-primary" onClick={submit} disabled={busy || !ready}>{busy ? <LoaderCircle size={13} className="animate-spin motion-reduce:animate-none" aria-hidden="true" /> : null}{busy ? t('Adding…') : t('Add and check')}</button>
  </>}>
    <div className="grid gap-3 sm:grid-cols-2">
      <div>
        <label className="field-label" htmlFor="manual-group">{t('Group')}</label>
        <select id="manual-group" className="field" value={groupId} onChange={event => setGroupId(event.target.value)}>
          {groups.map(group => <option key={group.id} value={group.id}>{group.name}</option>)}
          <option value="">{t('New group…')}</option>
        </select>
      </div>
      {groupId === '' && <div>
        <label className="field-label" htmlFor="manual-group-name">{t('New group name')}</label>
        <input id="manual-group-name" className="field" value={groupName} maxLength={80} onChange={event => setGroupName(event.target.value)} placeholder={t('For example: Static residential US')} />
      </div>}
    </div>

    <div className="mt-4 inline-flex gap-1 rounded-md bg-raised p-1" role="tablist" aria-label={t('How to add')}>
      <button type="button" role="tab" aria-selected={tab === 'form'} className={tabClass(tab === 'form')} onClick={() => setTab('form')}>{t('One node')}</button>
      <button type="button" role="tab" aria-selected={tab === 'paste'} className={tabClass(tab === 'paste')} onClick={() => setTab('paste')}>{t('Paste many')}</button>
    </div>

    {tab === 'form' ? <div className="mt-3 grid gap-3 sm:grid-cols-[140px_1fr_110px]">
      <div>
        <label className="field-label" htmlFor="manual-type">{t('Protocol')}</label>
        <select id="manual-type" className="field" value={node.type} onChange={event => setNode({ ...node, type: event.target.value as SimpleProxyType })}>
          <option value="http">HTTP</option>
          <option value="https">HTTPS</option>
          <option value="socks5">SOCKS5</option>
        </select>
      </div>
      <div>
        <label className="field-label" htmlFor="manual-server">{t('Server (IP or host)')}</label>
        <input id="manual-server" className="field font-mono" autoComplete="off" spellCheck={false} value={node.server} onChange={event => setNode({ ...node, server: event.target.value })} placeholder="203.0.113.10" />
      </div>
      <div>
        <label className="field-label" htmlFor="manual-port">{t('Port')}</label>
        <input id="manual-port" className="field font-mono" inputMode="numeric" value={node.port} onChange={event => setNode({ ...node, port: event.target.value.replace(/\D/g, '').slice(0, 5) })} placeholder="8080" />
      </div>
      <div className="sm:col-span-3 grid gap-3 sm:grid-cols-2">
        <div>
          <label className="field-label" htmlFor="manual-username">{t('Username (optional)')}</label>
          <input id="manual-username" className="field font-mono" autoComplete="off" spellCheck={false} value={node.username} onChange={event => setNode({ ...node, username: event.target.value })} />
        </div>
        <div>
          <label className="field-label" htmlFor="manual-password">{t('Password (optional)')}</label>
          <input id="manual-password" className="field font-mono" type="password" autoComplete="new-password" value={node.password} onChange={event => setNode({ ...node, password: event.target.value })} />
        </div>
      </div>
      <div className="sm:col-span-3">
        <label className="field-label" htmlFor="manual-name">{t('Name (optional)')}</label>
        <input id="manual-name" className="field" maxLength={128} value={node.name} onChange={event => setNode({ ...node, name: event.target.value })} placeholder={t('Defaults to server:port')} />
      </div>
    </div> : <div className="mt-3">
      <div className="flex items-end justify-between gap-3">
        <label className="field-label" htmlFor="manual-text">{t('One proxy per line')}</label>
        <label className="flex items-center gap-2 text-[12px] text-ink-dim">{t('Lines without a scheme are')}
          <select className="field h-[28px] w-auto" value={defaultType} onChange={event => setDefaultType(event.target.value as SimpleProxyType)} aria-label={t('Protocol for lines without a scheme')}>
            <option value="http">HTTP</option>
            <option value="socks5">SOCKS5</option>
          </select>
        </label>
      </div>
      <textarea id="manual-text" className="field mt-1 h-[160px] resize-y font-mono text-[12px]" spellCheck={false} value={text} onChange={event => setText(event.target.value)} placeholder={PLACEHOLDER} />
      <p className="mt-1 text-[12px] text-ink-dim">{t('Accepted: host:port, host:port:user:pass, user:pass:host:port, user:pass@host:port, scheme URLs and share links (ss, vmess, vless, trojan, hysteria2). Lines starting with # are ignored; duplicates are skipped.')}</p>
    </div>}
    <p className="mt-3 text-[12px] text-ink-dim">{t('Passwords are encrypted and never shown again. Each node is checked through itself, so the map shows where it really exits.')}</p>
    {error && <p className="mt-3 text-danger" role="alert">{error}</p>}
  </Modal>
}

function EditManualNode({ node, onClose, onSaved }: { node: NetworkNode; onClose: () => void; onSaved: () => void }) {
  const { t } = useI18n()
  const connectionEditable = node.protocol === 'http' || node.protocol === 'socks5'
  const split = node.entry.lastIndexOf(':')
  const [name, setName] = useState(node.name)
  const [server, setServer] = useState(split > 0 ? node.entry.slice(0, split).replace(/^\[|\]$/g, '') : node.entry)
  const [port, setPort] = useState(split > 0 ? node.entry.slice(split + 1) : '')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [clear, setClear] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function save() {
    setBusy(true); setError(null)
    const changes: Partial<ManualNodeInput> = {}
    if (name.trim() && name.trim() !== node.name) changes.name = name.trim()
    if (connectionEditable) {
      const original = node.entry
      if (`${server.includes(':') ? `[${server}]` : server}:${port}` !== original) { changes.server = server.trim(); changes.port = Number(port) }
      if (clear) { changes.username = ''; changes.password = '' }
      else {
        if (username) changes.username = username
        if (password) changes.password = password
      }
    }
    try { await networkAPI.updateNode(node.id, changes); onSaved() }
    catch (err) { setError(t(err instanceof Error ? err.message : String(err))) }
    finally { setBusy(false) }
  }

  return <Modal open title={t('Edit fixed node')} subtitle={node.subscription_name} width={480} onClose={() => { if (!busy) onClose() }} footer={<>
    <button className="btn btn-default" onClick={onClose} disabled={busy}>{t('Cancel')}</button>
    <button className="btn btn-primary" onClick={save} disabled={busy || !name.trim() || (connectionEditable && (!server.trim() || !/^\d{1,5}$/.test(port)))}>{t('Save')}</button>
  </>}>
    <label className="field-label" htmlFor="edit-node-name">{t('Name')}</label>
    <input id="edit-node-name" className="field" maxLength={128} value={name} onChange={event => setName(event.target.value)} />
    {connectionEditable ? <>
      <div className="mt-3 grid grid-cols-[1fr_100px] gap-3">
        <div><label className="field-label" htmlFor="edit-node-server">{t('Server (IP or host)')}</label><input id="edit-node-server" className="field font-mono" spellCheck={false} value={server} onChange={event => setServer(event.target.value)} /></div>
        <div><label className="field-label" htmlFor="edit-node-port">{t('Port')}</label><input id="edit-node-port" className="field font-mono" inputMode="numeric" value={port} onChange={event => setPort(event.target.value.replace(/\D/g, '').slice(0, 5))} /></div>
      </div>
      <div className="mt-3 grid grid-cols-2 gap-3">
        <div><label className="field-label" htmlFor="edit-node-username">{t('New username')}</label><input id="edit-node-username" className="field font-mono" autoComplete="off" disabled={clear} value={username} onChange={event => setUsername(event.target.value)} placeholder={t('Unchanged')} /></div>
        <div><label className="field-label" htmlFor="edit-node-password">{t('New password')}</label><input id="edit-node-password" className="field font-mono" type="password" autoComplete="new-password" disabled={clear} value={password} onChange={event => setPassword(event.target.value)} placeholder={t('Unchanged')} /></div>
      </div>
      <label className="mt-2 flex items-center gap-2 text-[12px]"><input type="checkbox" checked={clear} onChange={event => setClear(event.target.checked)} />{t('Remove the username and password')}</label>
      <p className="mt-2 text-[12px] text-ink-dim">{t('Changing the connection forgets the last measurement; check the node again afterwards.')}</p>
    </> : <p className="mt-2 text-[12px] text-ink-dim">{t('Share-link nodes can only be renamed. Delete and add the node again to change its connection.')}</p>}
    {error && <p className="mt-3 text-danger" role="alert">{error}</p>}
  </Modal>
}

function DeleteManualNode({ node, onClose, onDeleted }: { node: NetworkNode; onClose: () => void; onDeleted: () => void }) {
  const { t } = useI18n()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  async function remove() {
    setBusy(true); setError(null)
    try { await networkAPI.deleteNode(node.id); onDeleted() }
    catch (err) { setError(err instanceof ApiRequestError && err.code === 'node_bound' ? t('Profiles use this node. Bind them to another node first.') : t(err instanceof Error ? err.message : String(err))) }
    finally { setBusy(false) }
  }
  return <Modal open title={t('Delete fixed node')} width={440} onClose={() => { if (!busy) onClose() }} footer={<><button className="btn btn-default" onClick={onClose} disabled={busy}>{t('Cancel')}</button><button className="btn btn-danger" onClick={remove} disabled={busy || node.profile_count > 0}>{t('Delete')}</button></>}>
    <p>{t('Delete "{name}"?', { name: node.name })}</p>
    {node.profile_count > 0 ? <p className="mt-2 text-warn">{t('{count} profiles are bound to this node. Bind them to another node first.', { count: node.profile_count })}</p>
      : <p className="mt-2 text-ink-dim">{t('Its password and measurements are removed with it.')}</p>}
    {error && <p className="mt-2 text-danger" role="alert">{error}</p>}
  </Modal>
}
