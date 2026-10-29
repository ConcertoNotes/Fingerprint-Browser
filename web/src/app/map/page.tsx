'use client'

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import Link from 'next/link'
import { Activity, ArrowLeft, Globe2, LoaderCircle, MapPin, Play, RefreshCw, Search, Square, TriangleAlert, X } from 'lucide-react'

import { EmptyState } from '@/components/empty-state'
import { useI18n } from '@/components/i18n-provider'
import { Modal } from '@/components/modal'
import { NodeDetail } from '@/components/network/node-detail'
import { WorldMap } from '@/components/network/world-map'
import { useToast } from '@/components/toast'
import { ApiRequestError, browsersAPI, hasGeography, isStaleWrite, profilesAPI, type Profile } from '@/lib/api'
import { networkAPI, type BatchJob, type Binding, type CheckSite, type NetworkMap, type NetworkNode, type NetworkStatus } from '@/lib/network-api'
import { countryName, flag, groupByLocation, groupTone, maskIp, type LocationGroup } from '@/lib/network-map'

const TONE_DOT = { ok: 'bg-ok', warn: 'bg-warn', danger: 'bg-danger', unknown: 'bg-ink-faint' } as const

export default function NetworkMapPage() {
  const { t, locale } = useI18n()
  const toast = useToast()
  const [map, setMap] = useState<NetworkMap | null>(null)
  const [nodes, setNodes] = useState<Record<string, NetworkNode>>({})
  const [profiles, setProfiles] = useState<Profile[]>([])
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
  const [geography, setGeography] = useState<Profile | null>(null)
  const jobTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  const load = useCallback(async () => {
    try {
      const [mapData, nodeData, profileData, bindingData, active, engine] = await Promise.all([
        networkAPI.map(),
        networkAPI.nodes({ limit: 1000 }),
        allProfiles(),
        networkAPI.bindings(),
        browsersAPI.active(),
        networkAPI.status(),
      ])
      setMap(mapData)
      setNodes(Object.fromEntries(nodeData.nodes.map(node => [node.id, node])))
      setProfiles(profileData)
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
    return () => { if (jobTimer.current) clearTimeout(jobTimer.current) }
  }, [load])

  const maskIps = status?.settings.mask_exit_ips ?? false
  const query = search.trim().toLowerCase()
  const features = useMemo(() => (map?.features.features ?? []).filter(feature => {
    const p = feature.properties
    if (health !== 'all' && p.health !== health) return false
    if (!query) return true
    return [p.label, p.city, p.region, p.country_iso, p.exit_ip, p.subscription_name, countryName(p.country_iso, locale)]
      .some(value => value && value.toLowerCase().includes(query))
  }), [map, health, query, locale])
  const groups = useMemo(() => groupByLocation(features), [features])
  const selectedGroup = groups.find(group => group.key === selectedKey) ?? null
  const unknown = useMemo(() => (map?.unknown_location ?? []).filter(item =>
    (health === 'all' || item.health === health) && (!query || item.label.toLowerCase().includes(query) || (item.exit_ip ?? '').includes(query))), [map, health, query])
  const selectedProfile = profiles.find(profile => profile.id === selectedProfileId) ?? null
  const selectedBinding = selectedProfileId ? bindings[selectedProfileId] : undefined

  function selectGroup(key: string, fly = false) {
    setSelectedKey(key)
    const group = groups.find(entry => entry.key === key)
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
      toast(check?.health === 'ok' ? 'ok' : 'error', t('Node checked'),
        check?.health === 'ok' ? `${node.name}: ${maskIp(check.exit_ip, maskIps)}${check.country_iso ? ` · ${countryName(check.country_iso, locale)}` : ''}` : `${node.name}: ${t(check?.error ?? 'Unreachable')}`)
      const mapData = await networkAPI.map()
      setMap(mapData)
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
      if (binding.geography_conflict) setGeography(selectedProfile)
      if (binding.overrides_manual_proxy) toast('info', t('Manual proxy is now unused'), t('The bound node replaces this profile\'s own proxy settings while the binding exists.'))
    } catch (err) {
      if (isStaleWrite(err)) {
        toast('error', t('The binding changed elsewhere'), t('Reloaded. Review the current node and try again.'))
        await load()
      } else if (err instanceof ApiRequestError && err.status === 409) {
        toast('error', t('Profile is running'), t('Stop the profile before changing its node. A running profile is never re-routed.'))
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
      toast('ok', t('Binding removed'), t('{profile} uses its own proxy settings again.', { profile: selectedProfile.name }))
    } catch (err) {
      toast('error', t('Could not remove the binding'), t(err instanceof Error ? err.message : String(err)))
    }
  }

  async function toggleProfile(profile: Profile) {
    markBusy(setBusyProfiles, profile.id, true)
    try {
      if (running.has(profile.id)) {
        await profilesAPI.closeProfile(profile.id)
        toast('ok', t('Browser stopped'), profile.name)
      } else {
        await profilesAPI.startProfile(profile.id)
        toast('ok', t('Browser started'), profile.name)
      }
    } catch (err) {
      toast('error', running.has(profile.id) ? t('Could not stop the browser') : t('Could not start the browser'), t(err instanceof Error ? err.message : String(err)))
    } finally {
      markBusy(setBusyProfiles, profile.id, false)
      const [active, bindingData] = await Promise.all([browsersAPI.active(), networkAPI.bindings()]).catch(() => [null, null] as const)
      if (active) setRunning(new Set(active.active_browsers.map(browser => browser.profile_id)))
      if (bindingData) setBindings(Object.fromEntries(bindingData.map(binding => [binding.profile_id, binding])))
    }
  }

  async function openSite(site: CheckSite) {
    if (!selectedProfile) return
    try {
      await networkAPI.openSite(selectedProfile.id, site)
      toast('ok', t('Opened in the profile\'s browser'), selectedProfile.name)
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
    const candidates = Object.values(nodes).filter(node => node.status === 'active' && (!onlyUnchecked || !node.check || node.check.stale))
    const ids = candidates.slice(0, 200).map(node => node.id)
    if (!ids.length) { toast('info', t('Nothing to check'), t('Every active node has a recent measurement.')); return }
    try {
      const started = await networkAPI.checkBatch(ids)
      setJob(started)
      poll(started.id)
    } catch (err) {
      toast('error', t('Could not start the batch check'), t(err instanceof Error ? err.message : String(err)))
    }
  }

  function poll(id: string) {
    jobTimer.current = setTimeout(async () => {
      try {
        const current = await networkAPI.job(id)
        setJob(current)
        if (current.state === 'queued' || current.state === 'running') { poll(id); return }
        await load()
        toast(current.state === 'finished' ? 'ok' : 'error', t('Batch check finished'),
          t('{ok} reachable, {failed} failed, one engine process.', { ok: current.ok, failed: current.failed }))
      } catch { /* the next manual reload shows the result */ }
    }, 800)
  }

  async function cancelJob() {
    if (!job) return
    try { setJob(await networkAPI.cancelJob(job.id)) } catch { /* already finished */ }
  }

  const detailNodes = selectedGroup ? selectedGroup.nodes.map(item => nodes[item.node_id]).filter(Boolean) : []
  const profileCard = selectedProfile ? { id: selectedProfile.id, name: selectedProfile.name, running: running.has(selectedProfile.id), boundNodeId: selectedBinding?.node_id ?? null } : null
  const jobActive = job && (job.state === 'queued' || job.state === 'running')

  return <>
    <header className="sticky top-0 z-20 flex min-h-[52px] flex-wrap items-center gap-3 border-b border-line bg-canvas/85 px-5 py-2 backdrop-blur">
      <h1 className="text-[14px] font-semibold">{t('Network map')}</h1>
      <span className="font-mono text-ink-faint">{map ? `${map.checked_count}/${map.total_nodes}` : ''}</span>
      <div className="relative ml-2 min-w-[160px] max-w-[280px] flex-1">
        <Search size={13} aria-hidden="true" className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-ink-dim" />
        <input className="field h-[30px] pl-7" aria-label={t('Search nodes, countries, cities or IPs')} placeholder={t('Search nodes, countries, cities or IPs')} value={search} onChange={event => setSearch(event.target.value)} />
      </div>
      <select className="field h-[30px] w-auto" aria-label={t('Filter by health')} value={health} onChange={event => setHealth(event.target.value as typeof health)}>
        <option value="all">{t('All nodes')}</option>
        <option value="ok">{t('Reachable only')}</option>
        <option value="error">{t('Failed only')}</option>
      </select>
      <div className="ml-auto flex flex-wrap items-center gap-2">
        {jobActive ? <>
          <span className="flex items-center gap-1.5 text-ink-dim" role="status"><LoaderCircle size={13} className="animate-spin motion-reduce:animate-none" aria-hidden="true" />{t('Checking {done}/{total}', { done: job.done, total: job.total })}</span>
          <button className="btn btn-ghost" onClick={cancelJob}><X size={13} aria-hidden="true" />{t('Cancel')}</button>
        </> : <>
          <button className="btn btn-default" onClick={() => startBatch(true)} disabled={!status?.engine.available}><Activity size={13} aria-hidden="true" />{t('Check unmeasured')}</button>
          <button className="btn btn-default" onClick={() => startBatch(false)} disabled={!status?.engine.available}>{t('Check all')}</button>
        </>}
        <button className="btn btn-ghost h-[30px] w-[30px] p-0" onClick={load} aria-label={t('Reload')} title={t('Reload')}><RefreshCw size={13} aria-hidden="true" /></button>
      </div>
    </header>

    {status && !status.engine.available && (
      <div className="flex items-start gap-2 border-b border-line bg-danger-shade/40 px-5 py-2 text-[12px] text-danger" role="alert">
        <TriangleAlert size={13} className="mt-0.5 shrink-0" aria-hidden="true" />
        <span>{t(status.engine.message ?? 'Mihomo is not available.')} <Link className="underline" href="/settings/">{t('Open settings')}</Link></span>
      </div>
    )}
    {status && !status.geoip.available && (
      <div className="flex items-start gap-2 border-b border-line px-5 py-2 text-[12px] text-warn" role="status">
        <TriangleAlert size={13} className="mt-0.5 shrink-0" aria-hidden="true" />{t('The GeoIP database is not installed, so exits cannot be placed. Install the browser resources from Settings.')}
      </div>
    )}

    {error ? <EmptyState icon={<Globe2 size={18} />} title={t('Cannot load the network map')} body={t(error)} action={<button className="btn btn-default" onClick={load}>{t('Retry')}</button>} />
      : loading ? <p className="px-5 py-8 text-ink-dim" role="status">{t('Loading…')}</p>
      : !map?.total_nodes ? <EmptyState icon={<Globe2 size={18} />} title={t('No nodes yet')} body={t('Import a Clash/Mihomo subscription or configuration file first, then check its nodes to place their real exits on the map.')} action={<Link className="btn btn-primary" href="/subscriptions/">{t('Add a subscription')}</Link>} />
      : <div className="flex flex-col xl:h-[calc(100vh-53px)] xl:min-h-[520px] xl:flex-row">
        <aside className="order-2 max-h-[40vh] shrink-0 overflow-y-auto border-b border-line xl:order-none xl:max-h-none xl:w-[248px] xl:border-b-0 xl:border-r" aria-label={t('Profiles')}>
          <p className="sticky top-0 border-b border-line bg-canvas px-4 py-2 text-[11px] uppercase tracking-[0.05em] text-ink-faint">{t('Profiles')}</p>
          <ul>
            {profiles.map(profile => {
              const binding = bindings[profile.id]
              const isRunning = running.has(profile.id)
              const runtime = binding?.runtime
              const busy = busyProfiles.has(profile.id)
              return <li key={profile.id} className={`border-b border-line/60 ${profile.id === selectedProfileId ? 'bg-raised' : ''}`}>
                <div className="flex items-start gap-2 px-3 py-2">
                  <button type="button" className="min-w-0 flex-1 text-left" onClick={() => setSelectedProfileId(profile.id === selectedProfileId ? null : profile.id)} aria-pressed={profile.id === selectedProfileId}>
                    <span className="flex items-center gap-1.5">
                      <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${isRunning ? (runtime?.state === 'degraded' ? 'bg-danger' : 'bg-signal signal-pulse') : 'bg-line-strong'}`} aria-hidden="true" />
                      <span className="truncate font-medium">{profile.name}</span>
                    </span>
                    <span className="mt-0.5 block truncate text-[11px] text-ink-dim">
                      {binding ? `${flag(binding.node.check?.country_iso)} ${binding.node.name}` : profile.proxy_config?.server ? t('Own proxy') : t('No node bound')}
                    </span>
                    {binding && !binding.usable && <span className="block text-[11px] text-danger">{t('Node retired: bind another')}</span>}
                    {isRunning && runtime?.state === 'degraded' && <span className="block text-[11px] text-danger">{t('Network down: traffic is blocked')}</span>}
                    {isRunning && runtime?.exit_ip && <span className="block font-mono text-[11px] text-ink-faint">{maskIp(runtime.exit_ip, maskIps)}</span>}
                  </button>
                  <button type="button" className="btn btn-ghost h-[26px] w-[26px] shrink-0 p-0" disabled={busy || profile.permissions?.launch === false} onClick={() => toggleProfile(profile)}
                    aria-label={isRunning ? t('Stop {name}', { name: profile.name }) : t('Start {name}', { name: profile.name })} title={isRunning ? t('Stop') : t('Start')}>
                    {busy ? <LoaderCircle size={12} className="animate-spin motion-reduce:animate-none" /> : isRunning ? <Square size={11} /> : <Play size={12} />}
                  </button>
                </div>
              </li>
            })}
            {!profiles.length && <li className="px-4 py-3 text-ink-dim">{t('No profiles yet.')}</li>}
          </ul>
        </aside>

        <section className="relative order-1 h-[56vh] min-h-[320px] flex-1 border-b border-line xl:order-none xl:h-auto xl:border-b-0" aria-label={t('Map')}>
          <WorldMap groups={groups} selectedKey={selectedKey} onSelect={key => selectGroup(key)} focusToken={focusToken} />
          <div className="pointer-events-none absolute bottom-3 left-3 flex flex-wrap gap-3 rounded-md border border-line bg-canvas/85 px-3 py-1.5 text-[11px] text-ink-dim backdrop-blur" aria-hidden="true">
            <span className="flex items-center gap-1"><span className="h-2 w-2 rounded-full bg-ok" />{t('Reachable')}</span>
            <span className="flex items-center gap-1"><span className="h-2 w-2 rounded-full bg-warn" />{t('Mixed')}</span>
            <span className="flex items-center gap-1"><span className="h-2 w-2 rounded-full bg-danger" />{t('Failed')}</span>
            <span className="flex items-center gap-1"><span className="h-2 w-2 rounded-full bg-signal-shade ring-1 ring-signal" />{t('Area cluster')}</span>
          </div>
        </section>

        <aside className="order-3 flex flex-col overflow-hidden xl:order-none xl:w-[400px] xl:border-l" aria-label={t('Node details')}>
          {selectedProfile && (
            <div className="border-b border-line px-4 py-2.5">
              <p className="text-[11px] uppercase tracking-[0.05em] text-ink-faint">{t('Selected profile')}</p>
              <div className="mt-0.5 flex items-center justify-between gap-2">
                <span className="truncate font-medium">{selectedProfile.name}</span>
                <div className="flex shrink-0 gap-1">
                  {selectedBinding?.runtime?.state === 'degraded' && <button className="btn btn-default h-[24px] px-2 text-[12px]" onClick={reconnect}>{t('Reconnect')}</button>}
                  {selectedBinding && <button className="btn btn-ghost h-[24px] px-2 text-[12px]" onClick={unbind} disabled={running.has(selectedProfile.id)}>{t('Unbind')}</button>}
                </div>
              </div>
              <p className="text-[11px] text-ink-dim">{selectedBinding ? t('Bound to {node}', { node: selectedBinding.node.name }) : t('Choose a location, then bind one of its nodes.')}</p>
              {hasGeography(selectedProfile) && <p className="mt-1 text-[11px] text-warn">{t('This profile states its own timezone or coordinates.')} <button className="underline" onClick={() => setGeography(selectedProfile)}>{t('Review')}</button></p>}
            </div>
          )}
          <div className="min-h-0 flex-1 overflow-y-auto">
            {selectedGroup ? <>
              <div className="sticky top-0 z-10 flex items-center gap-2 border-b border-line bg-canvas px-4 py-2">
                <button className="btn btn-ghost h-[26px] w-[26px] p-0" onClick={() => setSelectedKey(null)} aria-label={t('All locations')} title={t('All locations')}><ArrowLeft size={13} /></button>
                <div className="min-w-0">
                  <p className="truncate font-medium">{groupTitle(selectedGroup, locale, t)}</p>
                  <p className="text-[11px] text-ink-dim">{t('{count} nodes at these coordinates', { count: selectedGroup.nodes.length })}</p>
                </div>
              </div>
              {detailNodes.map(node => (
                <NodeDetail key={node.id} node={node} maskIps={maskIps} expanded={expanded === node.id} onToggle={() => setExpanded(expanded === node.id ? null : node.id)}
                  busy={busyNodes.has(node.id)} selectedProfile={profileCard} onCheck={refresh => checkNode(node.id, refresh)} onBind={() => bind(node.id)} onOpenSite={openSite} />
              ))}
            </> : <>
              <p className="sticky top-0 z-10 border-b border-line bg-canvas px-4 py-2 text-[11px] uppercase tracking-[0.05em] text-ink-faint">{t('Locations ({count})', { count: groups.length })}</p>
              <ul>
                {groups.map(group => (
                  <li key={group.key}>
                    <button type="button" onClick={() => selectGroup(group.key, true)} className="flex w-full items-center gap-2.5 border-b border-line/60 px-4 py-2 text-left hover:bg-surface">
                      <span className={`h-2 w-2 shrink-0 rounded-full ${TONE_DOT[groupTone(group)]}`} aria-hidden="true" />
                      <span className="min-w-0 flex-1 truncate">{groupTitle(group, locale, t)}</span>
                      <span className="shrink-0 font-mono text-[11px] text-ink-dim">{t('{ok}/{count} up', { ok: group.health.ok, count: group.nodes.length })}</span>
                    </button>
                  </li>
                ))}
              </ul>
              {unknown.length > 0 && <>
                <p className="border-b border-line bg-canvas px-4 py-2 text-[11px] uppercase tracking-[0.05em] text-ink-faint">{t('Not located ({count})', { count: unknown.length })}</p>
                <p className="px-4 py-2 text-[11px] text-ink-dim">{t('Unchecked, unreachable, or not in the GeoIP database. They are never placed from their names.')}</p>
                <ul>
                  {unknown.map(item => {
                    const node = nodes[item.node_id]
                    return node ? <NodeDetail key={item.node_id} node={node} maskIps={maskIps} expanded={expanded === item.node_id} onToggle={() => setExpanded(expanded === item.node_id ? null : item.node_id)}
                      busy={busyNodes.has(item.node_id)} selectedProfile={profileCard} onCheck={refresh => checkNode(item.node_id, refresh)} onBind={() => bind(item.node_id)} onOpenSite={openSite} /> : null
                  })}
                </ul>
              </>}
              {!groups.length && !unknown.length && <p className="px-4 py-6 text-ink-dim"><MapPin size={13} className="mr-1 inline" aria-hidden="true" />{t('No nodes match these filters.')}</p>}
            </>}
          </div>
        </aside>
      </div>}

    <Modal open={!!geography} title={t('Follow the new exit?')} width={480} onClose={() => setGeography(null)} footer={<>
      <button className="btn btn-default" onClick={() => setGeography(null)}>{t('Keep stated location')}</button>
      <button className="btn btn-primary" onClick={async () => {
        if (!geography) return
        try { await profilesAPI.clearGeography([geography.id]); toast('ok', t('Location now follows the exit'), geography.name); await load() }
        catch (err) { toast('error', t('Could not clear the location'), t(err instanceof Error ? err.message : String(err))) }
        finally { setGeography(null) }
      }}>{t('Clear and follow the exit')}</button>
    </>}>
      <p>{t('{name} states its own timezone or coordinates. They will contradict a node in another place, which pages can detect.', { name: geography?.name ?? '' })}</p>
      <p className="mt-2 text-ink-dim">{t('Clearing them lets Camoufox derive timezone, location and WebRTC address from the real exit at each launch. Languages and the hardware fingerprint are not changed. Nothing is cleared unless you choose it.')}</p>
    </Modal>
  </>
}

/** Every visible profile; the API pages at 100. Bounded like the profiles list. */
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
