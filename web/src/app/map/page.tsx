'use client'

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import Link from 'next/link'
import { Activity, ArrowLeft, Cloud, Globe2, LoaderCircle, MapPin, Play, Plus, RefreshCw, Search, Square, TriangleAlert, X } from 'lucide-react'

import { CreateBrowserModal } from '@/components/create-browser-modal'
import { EmptyState } from '@/components/empty-state'
import { ExitIpDriftModal } from '@/components/exit-ip-drift-modal'
import { useI18n } from '@/components/i18n-provider'
import { NodeDetail } from '@/components/network/node-detail'
import { WorldMap } from '@/components/network/world-map'
import { useToast } from '@/components/toast'
import { useWorkspace } from '@/components/workspace-provider'
import { ApiRequestError, browsersAPI, groupsAPI, isStaleWrite, profilesAPI, readExitIpDrift, type ExitIpDriftDetail, type Group, type Profile } from '@/lib/api'
import { getDesktopBridge } from '@/lib/desktop-bridge'
import {
  filterLocalStaticGroups,
  networkAPI,
  nodeProtocolLabel,
  type BatchJob,
  type Binding,
  type CheckSite,
  type NetworkMap,
  type NetworkNode,
  type NetworkStatus,
  type Subscription,
} from '@/lib/network-api'
import { countryName, flag, groupByLocation, groupTone, maskIp, purityTone, type LocationGroup } from '@/lib/network-map'

const TONE_DOT = { ok: 'bg-ok-dot', warn: 'bg-warn-dot', danger: 'bg-danger-dot', unknown: 'bg-slate-300' } as const
const TONE_TEXT = { ok: 'text-ok', warn: 'text-warn', danger: 'text-danger', unknown: 'text-ink-dim' } as const
const SOURCE_KEY = 'cpm.map.source'

export default function NetworkMapPage() {
  const { t, locale } = useI18n()
  const toast = useToast()
  const { workspaces, openProfileInWorkspace, closeProfileWorkspace, activateWorkspace } = useWorkspace()
  const [map, setMap] = useState<NetworkMap | null>(null)
  const [subscriptions, setSubscriptions] = useState<Subscription[]>([])
  const [source, setSource] = useState<string>('all')
  const [nodes, setNodes] = useState<Record<string, NetworkNode>>({})
  const [profiles, setProfiles] = useState<Profile[]>([])
  const [groups, setGroups] = useState<Group[]>([])
  const [bindings, setBindings] = useState<Record<string, Binding>>({})
  const [running, setRunning] = useState<Set<string>>(new Set())
  const [status, setStatus] = useState<NetworkStatus | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [selectedProfileId, setSelectedProfileId] = useState<string | null>(null)
  const [selectedKey, setSelectedKey] = useState<string | null>(null)
  const [focusToken, setFocusToken] = useState(0)
  const [expanded, setExpanded] = useState<string | null>(null)
  const [search, setSearch] = useState('')
  const [health, setHealth] = useState<'all' | 'ok' | 'error'>('all')
  const [busyNodes, setBusyNodes] = useState<Set<string>>(new Set())
  const [busyProfiles, setBusyProfiles] = useState<Set<string>>(new Set())
  const [job, setJob] = useState<BatchJob | null>(null)
  const [createNodeId, setCreateNodeId] = useState<string | null>(null)
  const [createOpen, setCreateOpen] = useState(false)
  const [driftBlock, setDriftBlock] = useState<{ profile: Profile; drift: ExitIpDriftDetail } | null>(null)
  const [driftRetrying, setDriftRetrying] = useState(false)
  const jobTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  const load = useCallback(async () => {
    try {
      const [mapData, nodeData, profileData, bindingData, active, engine, subscriptionData, groupData] = await Promise.all([
        networkAPI.map(),
        networkAPI.nodes({ limit: 1000 }),
        allProfiles(),
        networkAPI.bindings(),
        browsersAPI.active(),
        networkAPI.status(),
        networkAPI.subscriptions(),
        groupsAPI.list().catch(() => ({ groups: [], total: 0 })),
      ])
      setMap(mapData)
      setSubscriptions(subscriptionData)
      setNodes(Object.fromEntries(nodeData.nodes.map(node => [node.id, node])))
      setProfiles(profileData)
      setGroups(groupData.groups)
      setBindings(Object.fromEntries(bindingData.map(binding => [binding.profile_id, binding])))
      setRunning(new Set(active.active_browsers.map(browser => browser.profile_id)))
      setStatus(engine)
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
    const params = new URLSearchParams(window.location.search)
    const preselected = params.get('profile')
    if (preselected) setSelectedProfileId(preselected)
    const wanted = params.get('source') ?? readSource()
    if (wanted) setSource(wanted)
    return () => { if (jobTimer.current) clearTimeout(jobTimer.current) }
  }, [load])

  const activeSource = source === 'all' || subscriptions.some(entry => entry.id === source) || !subscriptions.length ? source : 'all'

  function chooseSource(value: string) {
    setSource(value)
    setSelectedKey(null)
    try { window.localStorage.setItem(SOURCE_KEY, value) } catch { /* storage unavailable */ }
  }

  const maskIps = status?.settings.mask_exit_ips ?? false
  const query = search.trim().toLowerCase()

  const features = useMemo(() => (map?.features.features ?? []).filter(feature => {
    const p = feature.properties
    if (activeSource !== 'all' && p.subscription_id !== activeSource) return false
    if (health !== 'all' && p.health !== health) return false
    if (!query) return true
    return [p.label, p.city, p.region, p.country_iso, p.exit_ip, p.subscription_name, countryName(p.country_iso, locale)]
      .some(value => value && value.toLowerCase().includes(query))
  }), [map, health, query, locale, activeSource])

  const locationGroups = useMemo(() => groupByLocation(features), [features])
  const selectedGroup = locationGroups.find(group => group.key === selectedKey) ?? null
  const unknown = useMemo(() => (map?.unknown_location ?? []).filter(item =>
    (activeSource === 'all' || item.subscription_id === activeSource)
    && (health === 'all' || item.health === health)
    && (!query || item.label.toLowerCase().includes(query) || (item.exit_ip ?? '').includes(query)),
  ), [map, health, query, activeSource])

  const counts = useMemo(() => {
    const inSource = (id: string) => activeSource === 'all' || id === activeSource
    const placed = (map?.features.features ?? []).filter(feature => inSource(feature.properties.subscription_id)).length
    const unplaced = (map?.unknown_location ?? []).filter(item => inSource(item.subscription_id)).length
    return { placed, total: placed + unplaced }
  }, [map, activeSource])

  const cloudSubs = useMemo(() => subscriptions.filter(s => s.managed_by === 'cloud'), [subscriptions])
  const localSubs = useMemo(() => subscriptions.filter(s => !s.manual && s.managed_by !== 'cloud'), [subscriptions])
  const manualSubs = useMemo(() => filterLocalStaticGroups(subscriptions), [subscriptions])
  const subMap = useMemo(() => Object.fromEntries(subscriptions.map(s => [s.id, s])), [subscriptions])

  const activeRunning = useMemo(
    () => new Set([...running, ...workspaces.map(ws => ws.profileId)]),
    [running, workspaces],
  )

  const selectedProfile = profiles.find(profile => profile.id === selectedProfileId) ?? null
  const selectedBinding = selectedProfileId ? bindings[selectedProfileId] : undefined

  function selectGroup(key: string, fly = false) {
    setSelectedKey(key)
    const group = locationGroups.find(entry => entry.key === key)
    setExpanded(group?.nodes[0]?.node_id ?? null)
    if (fly) setFocusToken(token => token + 1)
  }

  function markBusy(setter: typeof setBusyNodes, id: string, on: boolean) {
    setter(current => { const next = new Set(current); if (on) next.add(id); else next.delete(id); return next })
  }

  async function checkNode(nodeId: string, refreshIntel: boolean) {
    markBusy(setBusyNodes, nodeId, true)
    try {
      const node = await networkAPI.checkNode(nodeId, { include_intel: true, refresh_intel: refreshIntel })
      setNodes(current => ({ ...current, [node.id]: node }))
      const check = node.check
      toast(
        check?.health === 'ok' ? 'ok' : 'error',
        t('Node checked'),
        check?.health === 'ok'
          ? `${node.name}: ${maskIp(check.exit_ip, maskIps)}${check.country_iso ? ` · ${countryName(check.country_iso, locale)}` : ''}`
          : `${node.name}: ${t(check?.error ?? 'Unreachable')}`,
      )
      setMap(await networkAPI.map())
    } catch (err) {
      toast('error', t('Could not check the node'), t(err instanceof Error ? err.message : String(err)))
    } finally {
      markBusy(setBusyNodes, nodeId, false)
    }
  }

  async function bind(nodeId: string) {
    if (!selectedProfile) return
    try {
      const binding = await networkAPI.bind(selectedProfile.id, nodeId, selectedBinding?.row_version ?? null)
      setBindings(current => ({ ...current, [selectedProfile.id]: binding }))
      toast('ok', t('Node bound'), t('{profile} uses {node} from its next launch.', { profile: selectedProfile.name, node: binding.node.name }))
    } catch (err) {
      if (isStaleWrite(err)) {
        toast('error', t('The binding changed elsewhere'), t('Reloaded. Review the current node and try again.'))
        await load()
      } else if (err instanceof ApiRequestError && err.status === 409) {
        toast('error', t('Browser is running'), t('Stop the browser before changing its node. A running browser is never re-routed.'))
      } else {
        toast('error', t('Could not bind the node'), t(err instanceof Error ? err.message : String(err)))
      }
    }
  }

  async function unbind() {
    if (!selectedProfile || !selectedBinding) return
    try {
      await networkAPI.unbind(selectedProfile.id, selectedBinding.row_version)
      setBindings(current => { const next = { ...current }; delete next[selectedProfile.id]; return next })
      toast('ok', t('Binding removed'), selectedProfile.name)
    } catch (err) {
      toast('error', t('Could not remove the binding'), t(err instanceof Error ? err.message : String(err)))
    }
  }

  async function toggleProfile(profile: Profile) {
    const wasRunning = activeRunning.has(profile.id)
    markBusy(setBusyProfiles, profile.id, true)
    try {
      if (wasRunning) {
        await closeProfileWorkspace(profile.id)
        toast('ok', t('Browser stopped'), profile.name)
      } else {
        const res = await openProfileInWorkspace(profile.id)
        if (!res.ok) {
          const drift = readExitIpDrift(res)
          if (drift) {
            setDriftBlock({ profile, drift })
          } else {
            toast('error', t('Could not start the browser'), t(res.message ?? 'Could not start the browser'))
          }
          return
        }
        toast('ok', t('Browser started'), profile.name)
      }
    } catch (err) {
      const drift = readExitIpDrift(err)
      if (!wasRunning && drift) {
        setDriftBlock({ profile, drift })
      } else {
        toast('error', wasRunning ? t('Could not stop the browser') : t('Could not start the browser'), t(err instanceof Error ? err.message : String(err)))
      }
    } finally {
      markBusy(setBusyProfiles, profile.id, false)
      const [active, bindingData] = await Promise.all([browsersAPI.active(), networkAPI.bindings()]).catch(() => [null, null] as const)
      if (active) setRunning(new Set(active.active_browsers.map(browser => browser.profile_id)))
      if (bindingData) setBindings(Object.fromEntries(bindingData.map(binding => [binding.profile_id, binding])))
    }
  }

  async function proceedAfterDrift() {
    if (!driftBlock || driftRetrying) return
    const { profile } = driftBlock
    setDriftRetrying(true)
    try {
      const res = await openProfileInWorkspace(profile.id, { allowIpDrift: true })
      if (!res.ok) {
        toast('error', t('Could not start the browser'), t(res.message ?? 'Could not start the browser'))
        return
      }
      setDriftBlock(null)
      toast('ok', t('Browser started'), profile.name)
      const [active, bindingData] = await Promise.all([browsersAPI.active(), networkAPI.bindings()]).catch(() => [null, null] as const)
      if (active) setRunning(new Set(active.active_browsers.map(browser => browser.profile_id)))
      if (bindingData) setBindings(Object.fromEntries(bindingData.map(binding => [binding.profile_id, binding])))
    } catch (err) {
      toast('error', t('Could not start the browser'), t(err instanceof Error ? err.message : String(err)))
    } finally {
      setDriftRetrying(false)
    }
  }

  async function openSite(site: CheckSite) {
    if (!selectedProfile) return
    try {
      const bridge = getDesktopBridge()
      if (bridge && workspaces.some(ws => ws.profileId === selectedProfile.id)) {
        const ws = workspaces.find(item => item.profileId === selectedProfile.id)
        const ip = ws?.exitIp ?? selectedBinding?.runtime?.exit_ip ?? selectedBinding?.node.check?.exit_ip ?? null
        const url = checkSiteUrl(site, ip)
        await bridge.createTab(selectedProfile.id, url)
        await activateWorkspace(selectedProfile.id)
        toast('ok', t('Opened in the browser'), selectedProfile.name)
        return
      }
      await networkAPI.openSite(selectedProfile.id, site)
      toast('ok', t('Opened in the browser'), selectedProfile.name)
    } catch (err) {
      toast('error', t('Could not open the page'), t(err instanceof Error ? err.message : String(err)))
    }
  }

  async function reconnect() {
    if (!selectedProfile) return
    try {
      await networkAPI.reconnect(selectedProfile.id)
      toast('ok', t('Reconnected'), t('{profile} is back on the same node.', { profile: selectedProfile.name }))
      setBindings(Object.fromEntries((await networkAPI.bindings()).map(binding => [binding.profile_id, binding])))
    } catch (err) {
      toast('error', t('Could not reconnect'), t(err instanceof Error ? err.message : String(err)))
    }
  }

  async function startBatch(onlyUnchecked: boolean) {
    const candidates = Object.values(nodes).filter(node => node.status === 'active'
      && (activeSource === 'all' || node.subscription_id === activeSource)
      && (!onlyUnchecked || !node.check || node.check.stale))
    const ids = candidates.slice(0, 500).map(node => node.id)
    if (!ids.length) { toast('info', t('Nothing to check'), t('Every active node has a recent measurement.')); return }
    try {
      const started = await networkAPI.checkBatch(ids)
      setJob(started)
      poll(started.id)
    } catch (err) {
      if (err instanceof ApiRequestError && err.status === 409) {
        const current = await networkAPI.currentJob().catch(() => null)
        if (current) { setJob(current); poll(current.id) }
        toast('info', t('A check is already running'), t('The map fills in as its results arrive.'))
      } else {
        toast('error', t('Could not start the batch check'), t(err instanceof Error ? err.message : String(err)))
      }
    }
  }

  function poll(id: string, tick = 0) {
    if (jobTimer.current) clearTimeout(jobTimer.current)
    jobTimer.current = setTimeout(async () => {
      try {
        const current = await networkAPI.job(id)
        setJob(current)
        if (current.state === 'queued' || current.state === 'running') {
          if (tick % 3 === 2) setMap(await networkAPI.map())
          poll(id, tick + 1)
          return
        }
        await load()
        toast(current.state === 'finished' ? 'ok' : 'error', t('Batch check finished'),
          t('{ok} reachable, {failed} failed, one engine process.', { ok: current.ok, failed: current.failed }))
      } catch { /* next reload shows the result */ }
    }, 800)
  }

  useEffect(() => {
    networkAPI.currentJob().then(current => { if (current) { setJob(current); poll(current.id) } }).catch(() => {})
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  async function cancelJob() {
    if (!job) return
    try { setJob(await networkAPI.cancelJob(job.id)) } catch { /* already finished */ }
  }

  function openNode(group: LocationGroup, nodeId: string) {
    setSelectedKey(group.key)
    setExpanded(nodeId)
    requestAnimationFrame(() => document.getElementById(`node-${nodeId}`)?.scrollIntoView({ block: 'nearest', behavior: 'smooth' }))
  }

  function openCreateWithNode(nodeId: string) {
    setCreateNodeId(nodeId)
    setCreateOpen(true)
  }

  function renderPopup(group: LocationGroup, close: () => void) {
    const shown = group.nodes.slice(0, 6)
    return (
      <div className="py-2">
        <div className="px-3.5 pb-2 pr-8">
          <p className="truncate text-[13px] font-semibold text-ink">{groupTitle(group, locale, t)}</p>
          <p className="text-[11px] text-ink-dim">{t('{ok}/{count} up', { ok: group.health.ok, count: group.nodes.length })}</p>
        </div>
        <ul className="max-h-[260px] divide-y divide-line overflow-y-auto border-t border-line">
          {shown.map(item => {
            const intel = nodes[item.node_id]?.intel?.composite
            const purity = intel?.purity_score ?? item.purity_score
            const isCloud = item.subscription_managed_by === 'cloud' || subMap[item.subscription_id]?.managed_by === 'cloud'
            const bound = item.bound_count ?? item.profile_count ?? 0
            return (
              <li key={item.node_id} className="px-3.5 py-2 transition-colors hover:bg-raised/60">
                <button type="button" className="block w-full cursor-pointer text-left" onClick={() => { openNode(group, item.node_id); close() }}>
                  <span className="flex items-center gap-1.5">
                    <span className={`h-2 w-2 shrink-0 rounded-full ${TONE_DOT[item.health === 'ok' ? 'ok' : item.health === 'error' ? 'danger' : 'unknown']}`} aria-hidden="true" />
                    <span className="min-w-0 flex-1 truncate font-medium text-ink" title={item.label}>{item.label}</span>
                    <span className="shrink-0 font-mono text-[11px] text-ink-dim">
                      {item.health === 'ok' && item.latency_ms != null ? `${item.latency_ms} ms` : item.health === 'error' ? t('Failed') : ''}
                    </span>
                  </span>
                  <span className="mt-0.5 flex flex-wrap items-center gap-1.5 text-[11px] text-ink-dim">
                    <span className="font-mono text-ink">{maskIp(item.exit_ip, maskIps)}</span>
                    {purity != null && <span className={`shrink-0 font-mono ${TONE_TEXT[purityTone(purity)]}`}>{t('Purity {score}', { score: Math.round(purity) })}</span>}
                    {bound > 0 && <span className="rounded-full bg-warn-shade px-1.5 text-[10px] text-warn">{t('Used by {count}', { count: bound })}</span>}
                    {isCloud && <span className="inline-flex items-center gap-0.5 rounded-full bg-ok-shade px-1.5 text-[10px] text-ok"><Cloud size={9} />{t('Cloud')}</span>}
                  </span>
                </button>
                <div className="mt-1.5 flex items-center justify-between gap-2">
                  <span className="truncate text-[10.5px] text-ink-faint">
                    {item.subscription_name} · {nodeProtocolLabel(item, t, subMap[item.subscription_id])}
                  </span>
                  <button
                    type="button"
                    onClick={() => { close(); openCreateWithNode(item.node_id) }}
                    className="inline-flex shrink-0 cursor-pointer items-center gap-1 rounded-md bg-signal-shade px-2 py-0.5 text-[11px] font-medium text-signal hover:bg-signal hover:text-white"
                  >
                    <Plus size={10} aria-hidden="true" />
                    {t('New browser')}
                  </button>
                </div>
              </li>
            )
          })}
        </ul>
        {group.nodes.length > shown.length && (
          <p className="px-3.5 pt-1.5 text-[11px] text-ink-dim">{t('{count} more in the side panel', { count: group.nodes.length - shown.length })}</p>
        )}
      </div>
    )
  }

  const detailNodes = selectedGroup ? selectedGroup.nodes.map(item => nodes[item.node_id]).filter(Boolean) : []
  const profileCard = selectedProfile
    ? { id: selectedProfile.id, name: selectedProfile.name, running: activeRunning.has(selectedProfile.id), boundNodeId: selectedBinding?.node_id ?? null }
    : null
  const jobActive = job && (job.state === 'queued' || job.state === 'running')

  return (
    <div className="flex h-full flex-col bg-canvas">
      <header className="sticky top-0 z-20 flex min-h-[54px] flex-wrap items-center gap-2.5 border-b border-line bg-canvas/85 px-5 py-2 backdrop-blur-xl app-drag">
        <div className="flex flex-wrap items-center gap-2.5 flex-1 app-no-drag">
        <h1 className="text-[15px] font-semibold tracking-tight text-ink">{t('Node map')}</h1>
        <select className="field h-[30px] w-auto max-w-[220px] bg-surface/90 py-0 text-[12.5px]" aria-label={t('Node source')} value={activeSource} onChange={event => chooseSource(event.target.value)}>
          <option value="all">{t('All node sources')}</option>
          {cloudSubs.length > 0 && (
            <optgroup label={t('Cloud subscriptions')}>
              {cloudSubs.map(entry => <option key={entry.id} value={entry.id}>{entry.name} ({entry.node_counts.active})</option>)}
            </optgroup>
          )}
          {localSubs.length > 0 && (
            <optgroup label={t('Local airports')}>
              {localSubs.map(entry => <option key={entry.id} value={entry.id}>{entry.name} ({entry.node_counts.active})</option>)}
            </optgroup>
          )}
          {manualSubs.length > 0 && (
            <optgroup label={t('Dedicated static IPs')}>
              {manualSubs.map(entry => <option key={entry.id} value={entry.id}>{entry.name} ({entry.node_counts.active})</option>)}
            </optgroup>
          )}
        </select>

        <span className="rounded-full bg-surface px-2 py-0.5 font-mono text-[11px] text-ink-dim ring-1 ring-line" title={t('Located nodes / all nodes in this view')}>
          {map ? `${counts.placed}/${counts.total}` : '—'}
        </span>

        <div className="relative ml-1 min-w-[160px] max-w-[260px] flex-1">
          <Search size={13} aria-hidden="true" className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-ink-faint" />
          <input className="field h-[30px] bg-surface/90 py-0 pl-8 text-[12.5px]" aria-label={t('Search nodes, countries, cities or IPs')} placeholder={t('Search nodes, countries, cities or IPs')} value={search} onChange={event => setSearch(event.target.value)} />
        </div>

        <select className="field h-[30px] w-auto bg-surface/90 py-0 text-[12.5px]" aria-label={t('Filter by health')} value={health} onChange={event => setHealth(event.target.value as typeof health)}>
          <option value="all">{t('All nodes')}</option>
          <option value="ok">{t('Reachable only')}</option>
          <option value="error">{t('Failed only')}</option>
        </select>

        <div className="ml-auto flex flex-wrap items-center gap-2">
          {jobActive ? (
            <>
              <span className="inline-flex items-center gap-1.5 rounded-full bg-signal-shade px-2.5 py-1 text-[12px] text-signal" role="status">
                <LoaderCircle size={12} className="animate-spin motion-reduce:animate-none" aria-hidden="true" />
                {t('Checking {done}/{total}', { done: job.done, total: job.total })}
              </span>
              <button type="button" className="btn btn-ghost h-[30px]" onClick={cancelJob}><X size={13} aria-hidden="true" />{t('Cancel')}</button>
            </>
          ) : (
            <>
              <button type="button" className="btn btn-default h-[30px] text-[12px]" onClick={() => startBatch(true)} disabled={!status?.engine.available}>
                <Activity size={13} aria-hidden="true" />{t('Check unmeasured')}
              </button>
              <button type="button" className="btn btn-default h-[30px] text-[12px]" onClick={() => startBatch(false)} disabled={!status?.engine.available}>
                {activeSource === 'all' ? t('Check all') : t('Check this source')}
              </button>
            </>
          )}
          <button type="button" className="btn btn-ghost h-[30px] w-[30px] p-0" onClick={load} aria-label={t('Reload')} title={t('Reload')}>
            <RefreshCw size={13} aria-hidden="true" />
          </button>
        </div>
        </div>
      </header>

      {status && !status.engine.available && (
        <div className="flex items-start gap-2 border-b border-line bg-danger-shade/50 px-5 py-2 text-[12px] text-danger" role="alert">
          <TriangleAlert size={13} className="mt-0.5 shrink-0" aria-hidden="true" />
          <span>{t(status.engine.message ?? 'Proxy routing engine is not ready.')} <Link className="underline" href="/settings/">{t('Open settings')}</Link></span>
        </div>
      )}
      {status && !status.geoip.available && (
        <div className="flex items-start gap-2 border-b border-line bg-warn-shade/60 px-5 py-2 text-[12px] text-warn" role="status">
          <TriangleAlert size={13} className="mt-0.5 shrink-0" aria-hidden="true" />
          {t('The GeoIP database is not installed, so exits cannot be placed. Install the browser resources from Settings.')}
        </div>
      )}

      {error ? (
        <EmptyState icon={<Globe2 size={18} />} title={t('Cannot load the network map')} body={t(error)} action={<button type="button" className="btn btn-default" onClick={load}>{t('Retry')}</button>} />
      ) : loading ? (
        <p className="px-5 py-8 text-ink-dim" role="status">{t('Loading…')}</p>
      ) : !map?.total_nodes ? (
        <EmptyState
          icon={<Globe2 size={18} />}
          title={t('No nodes yet')}
          body={t('Import an airport subscription, sync cloud subscriptions, or add dedicated static IPs to place their measured exits on the world map.')}
          action={<Link className="btn btn-primary" href="/subscriptions/">{t('Add a subscription')}</Link>}
        />
      ) : (
        <div className="flex min-h-0 flex-1 flex-col xl:flex-row">
          {/* Left Browser List for quick binding */}
          <aside className="order-2 max-h-[36vh] shrink-0 overflow-y-auto border-b border-line bg-sidebar/80 xl:order-none xl:max-h-none xl:w-[244px] xl:border-b-0 xl:border-r" aria-label={t('Browsers')}>
            <p className="sticky top-0 z-10 border-b border-line bg-sidebar/95 px-4 py-2 text-[10.5px] font-semibold uppercase tracking-[0.06em] text-ink-faint backdrop-blur">
              {t('Browsers')}
            </p>
            <ul>
              {profiles.map(profile => {
                const binding = bindings[profile.id]
                const isRunning = activeRunning.has(profile.id)
                const runtime = binding?.runtime
                const busy = busyProfiles.has(profile.id)
                const isSelected = profile.id === selectedProfileId
                return (
                  <li key={profile.id} className={`relative border-b border-line/60 transition-colors ${isSelected ? 'bg-signal-shade/85' : 'hover:bg-raised/45'}`}>
                    {isSelected && <span className="absolute bottom-1.5 left-0 top-1.5 w-[2.5px] rounded-r-full bg-signal" aria-hidden="true" />}
                    <div className="flex items-start gap-2 px-3 py-2.5">
                      <button type="button" className="min-w-0 flex-1 cursor-pointer text-left" onClick={() => setSelectedProfileId(isSelected ? null : profile.id)} aria-pressed={isSelected}>
                        <span className="flex items-center gap-1.5">
                          <span className={`h-2 w-2 shrink-0 rounded-full ${isRunning ? (runtime?.state === 'degraded' ? 'bg-danger-dot' : 'bg-ok-dot signal-pulse') : 'bg-slate-300'}`} aria-hidden="true" />
                          <span className="truncate font-medium text-ink">{profile.name}</span>
                        </span>
                        <span className="mt-0.5 block truncate text-[11px] text-ink-dim">
                          {binding ? `${flag(binding.node.check?.country_iso)} ${binding.node.name}` : t('No node bound')}
                        </span>
                        {binding && !binding.usable && <span className="block text-[11px] text-danger">{t('Node retired: bind another')}</span>}
                        {isRunning && runtime?.state === 'degraded' && <span className="block text-[11px] text-danger">{t('Network down: traffic is blocked')}</span>}
                        {isRunning && runtime?.exit_ip && <span className="block font-mono text-[11px] text-ink-faint">{maskIp(runtime.exit_ip, maskIps)}</span>}
                      </button>
                      <button
                        type="button"
                        className="btn btn-ghost h-[26px] w-[26px] shrink-0 p-0"
                        disabled={busy}
                        onClick={() => toggleProfile(profile)}
                        aria-label={isRunning ? t('Stop {name}', { name: profile.name }) : t('Start {name}', { name: profile.name })}
                        title={isRunning ? t('Stop') : t('Start')}
                      >
                        {busy ? <LoaderCircle size={12} className="animate-spin motion-reduce:animate-none" /> : isRunning ? <Square size={10} /> : <Play size={12} />}
                      </button>
                    </div>
                  </li>
                )
              })}
              {!profiles.length && <li className="px-4 py-3 text-[12px] text-ink-dim">{t('No isolated browsers yet')}</li>}
            </ul>
          </aside>

          {/* Center Map */}
          <section className="relative order-1 h-[54vh] min-h-[340px] flex-1 border-b border-line xl:order-none xl:h-auto xl:border-b-0" aria-label={t('Map')}>
            <WorldMap
              mode="manage"
              groups={locationGroups}
              selectedKey={selectedKey}
              onSelect={key => selectGroup(key)}
              focusToken={focusToken}
              fitKey={activeSource}
              maskIps={maskIps}
              renderPopup={renderPopup}
            />
            <div className="modal-sheet pointer-events-none absolute bottom-3 left-3 flex flex-wrap gap-3 rounded-xl px-3 py-1.5 text-[11px] text-ink-dim backdrop-blur-xl" aria-hidden="true">
              <span className="flex items-center gap-1.5"><span className="h-2 w-2 rounded-full bg-ok-dot" />{t('Reachable')}</span>
              <span className="flex items-center gap-1.5"><span className="h-2 w-2 rounded-full bg-warn-dot" />{t('Mixed')}</span>
              <span className="flex items-center gap-1.5"><span className="h-2 w-2 rounded-full bg-danger-dot" />{t('Failed')}</span>
              <span className="flex items-center gap-1.5"><span className="h-2 w-2 rounded-full bg-signal-shade ring-1 ring-signal" />{t('Area cluster')}</span>
            </div>
          </section>

          {/* Right Location & Node Details */}
          <aside className="order-3 flex flex-col overflow-hidden bg-surface xl:order-none xl:w-[392px] xl:border-l xl:border-line" aria-label={t('Node details')}>
            {selectedProfile && (
              <div className="border-b border-line bg-sidebar/80 px-4 py-2.5">
                <p className="text-[10.5px] font-semibold uppercase tracking-[0.06em] text-ink-faint">{t('Selected browser')}</p>
                <div className="mt-0.5 flex items-center justify-between gap-2">
                  <span className="truncate font-semibold text-ink">{selectedProfile.name}</span>
                  <div className="flex shrink-0 gap-1">
                    {selectedBinding?.runtime?.state === 'degraded' && <button type="button" className="btn btn-default h-[24px] px-2 text-[11.5px]" onClick={reconnect}>{t('Reconnect')}</button>}
                    {selectedBinding && <button type="button" className="btn btn-ghost h-[24px] px-2 text-[11.5px]" onClick={unbind} disabled={activeRunning.has(selectedProfile.id)}>{t('Unbind')}</button>}
                  </div>
                </div>
                <p className="text-[11.5px] text-ink-dim">
                  {selectedBinding ? t('Bound to {node}', { node: selectedBinding.node.name }) : t('Choose a location, then bind one of its nodes.')}
                </p>
              </div>
            )}
            <div className="min-h-0 flex-1 overflow-y-auto">
              {selectedGroup ? (
                <>
                  <div className="sticky top-0 z-10 flex items-center gap-2 border-b border-line bg-sidebar/95 px-4 py-2.5 backdrop-blur">
                    <button type="button" className="btn btn-ghost h-[26px] w-[26px] p-0" onClick={() => setSelectedKey(null)} aria-label={t('All locations')} title={t('All locations')}>
                      <ArrowLeft size={13} />
                    </button>
                    <div className="min-w-0">
                      <p className="truncate font-semibold text-ink">{groupTitle(selectedGroup, locale, t)}</p>
                      <p className="text-[11px] text-ink-dim">{t('{count} nodes at these coordinates', { count: selectedGroup.nodes.length })}</p>
                    </div>
                  </div>
                  {detailNodes.map(node => (
                    <NodeDetail
                      key={node.id}
                      node={node}
                      maskIps={maskIps}
                      expanded={expanded === node.id}
                      onToggle={() => setExpanded(expanded === node.id ? null : node.id)}
                      busy={busyNodes.has(node.id)}
                      selectedProfile={profileCard}
                      onCheck={refresh => checkNode(node.id, refresh)}
                      onBind={() => bind(node.id)}
                      onCreateWithNode={() => openCreateWithNode(node.id)}
                      onOpenSite={openSite}
                    />
                  ))}
                </>
              ) : (
                <>
                  <p className="sticky top-0 z-10 border-b border-line bg-sidebar/95 px-4 py-2 text-[10.5px] font-semibold uppercase tracking-[0.06em] text-ink-faint backdrop-blur">
                    {t('Locations ({count})', { count: locationGroups.length })}
                  </p>
                  <ul>
                    {locationGroups.map(group => (
                      <li key={group.key}>
                        <button type="button" onClick={() => selectGroup(group.key, true)} className="flex w-full cursor-pointer items-center gap-2.5 border-b border-line/60 px-4 py-2.5 text-left transition-colors hover:bg-raised/55">
                          <span className={`h-2 w-2 shrink-0 rounded-full ${TONE_DOT[groupTone(group)]}`} aria-hidden="true" />
                          <span className="min-w-0 flex-1 truncate text-ink">{groupTitle(group, locale, t)}</span>
                          <span className="shrink-0 font-mono text-[11px] text-ink-dim">{t('{ok}/{count} up', { ok: group.health.ok, count: group.nodes.length })}</span>
                        </button>
                      </li>
                    ))}
                  </ul>
                  {unknown.length > 0 && (
                    <>
                      <p className="border-b border-line bg-sidebar px-4 py-2 text-[11px] font-medium uppercase tracking-[0.05em] text-ink-faint">
                        {t('Not located ({count})', { count: unknown.length })}
                      </p>
                      <p className="px-4 py-2 text-[11px] text-ink-dim">{t('Unchecked, unreachable, or not in the GeoIP database. They are never placed from their names.')}</p>
                      <ul>
                        {unknown.map(item => {
                          const node = nodes[item.node_id]
                          return node ? (
                            <NodeDetail
                              key={item.node_id}
                              node={node}
                              maskIps={maskIps}
                              expanded={expanded === item.node_id}
                              onToggle={() => setExpanded(expanded === item.node_id ? null : item.node_id)}
                              busy={busyNodes.has(item.node_id)}
                              selectedProfile={profileCard}
                              onCheck={refresh => checkNode(item.node_id, refresh)}
                              onBind={() => bind(item.node_id)}
                              onCreateWithNode={() => openCreateWithNode(item.node_id)}
                              onOpenSite={openSite}
                            />
                          ) : null
                        })}
                      </ul>
                    </>
                  )}
                  {!locationGroups.length && !unknown.length && (
                    <p className="px-4 py-6 text-ink-dim"><MapPin size={13} className="mr-1 inline" aria-hidden="true" />{t('No nodes match these filters.')}</p>
                  )}
                </>
              )}
            </div>
          </aside>
        </div>
      )}

      <CreateBrowserModal
        open={createOpen}
        initialNodeId={createNodeId}
        groups={groups}
        onGroupCreated={created => setGroups(prev => [...prev, created])}
        onClose={() => { setCreateOpen(false); setCreateNodeId(null) }}
        onCreated={async (created, launched) => {
          toast('ok', launched ? t('Browser created and launched') : t('Browser created'), created.name)
          await load()
        }}
      />

      <ExitIpDriftModal
        profile={driftBlock?.profile ?? null}
        drift={driftBlock?.drift ?? null}
        maskIps={maskIps}
        busy={driftRetrying}
        onCancel={() => setDriftBlock(null)}
        onProceed={proceedAfterDrift}
      />
    </div>
  )
}

async function allProfiles(): Promise<Profile[]> {
  const collected: Profile[] = []
  for (let page = 1; page <= 200; page += 1) {
    const response = await profilesAPI.getProfiles({ page, per_page: 100 })
    collected.push(...response.profiles)
    if (!response.has_next) break
  }
  return collected
}

function groupTitle(group: LocationGroup, locale: string, t: (message: string, values?: Record<string, string | number>) => string): string {
  const country = group.countries.map(iso => `${flag(iso)} ${countryName(iso, locale)}`).join(' / ')
  const city = group.cities.join(' / ')
  return [city, country].filter(Boolean).join(' · ') || t('Unnamed location')
}

function readSource(): string | null {
  try { return window.localStorage.getItem(SOURCE_KEY) } catch { return null }
}

function checkSiteUrl(site: CheckSite, exitIp: string | null): string {
  switch (site) {
    case 'ippure': return 'https://ippure.com/'
    case 'ipip': return 'https://ipip.la/'
    case 'ipinfo': return 'https://ipinfo.io/'
    case 'scamalytics': return exitIp ? `https://scamalytics.com/ip/${encodeURIComponent(exitIp)}` : 'https://scamalytics.com/'
  }
}
