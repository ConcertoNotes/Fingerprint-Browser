'use client'

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Activity,
  Check,
  ChevronDown,
  ChevronUp,
  Cpu,
  Globe2,
  List,
  LoaderCircle,
  Lock,
  Monitor,
  Play,
  Plus,
  RefreshCw,
  Rss,
  Search,
  Server,
  ShieldCheck,
  Sparkles,
  TriangleAlert,
  X,
} from 'lucide-react'

import { useI18n } from '@/components/i18n-provider'
import { WorldMap } from '@/components/network/world-map'
import { useToast } from '@/components/toast'
import { useWorkspace } from '@/components/workspace-provider'
import {
  ApiRequestError,
  groupsAPI,
  guessHostOs,
  OS_LABELS,
  profilesAPI,
  shortGpuName,
  type CanvasPolicy,
  type Group,
  type IsolationOs,
  type IsolationPreview,
  type IsolationTier,
  type LocaleMode,
  type Profile,
  type TimezoneMode,
  type WebRtcPolicy,
} from '@/lib/api'
import { acquireViewportOverlay } from '@/lib/desktop-bridge'
import {
  filterLocalStaticGroups,
  isProtectedNodeProtocol,
  networkAPI,
  nodeProtocolLabel,
  type BatchJob,
  type NetworkMap,
  type NetworkNode,
  type NetworkStatus,
  type SimpleProxyType,
  type Subscription,
} from '@/lib/network-api'
import { countryName, flag, groupByLocation, maskIp, purityTone } from '@/lib/network-map'

export interface CreateBrowserModalProps {
  open: boolean
  onClose: () => void
  onCreated: (profile: Profile, launched: boolean) => void | Promise<void>
  groups: Group[]
  onGroupCreated?: (group: Group) => void
  initialNodeId?: string | null
}

const OS_CHOICES: { value: IsolationOs; label: string }[] = [
  { value: 'macos', label: 'macOS' },
  { value: 'windows', label: 'Windows' },
  { value: 'linux', label: 'Linux' },
]

export function CreateBrowserModal(props: CreateBrowserModalProps) {
  if (!props.open) return null
  return <CreateBrowserModalContent {...props} />
}

function CreateBrowserModalContent({
  onClose,
  onCreated,
  groups,
  onGroupCreated,
  initialNodeId = null,
}: CreateBrowserModalProps) {
  const { t, locale } = useI18n()
  const toast = useToast()
  const { openProfileInWorkspace, refreshProfileDirectory } = useWorkspace()

  // Map & nodes state
  const [map, setMap] = useState<NetworkMap | null>(null)
  const [subscriptions, setSubscriptions] = useState<Subscription[]>([])
  const [nodes, setNodes] = useState<Record<string, NetworkNode>>({})
  const [netStatus, setNetStatus] = useState<NetworkStatus | null>(null)
  const [loadingMap, setLoadingMap] = useState(true)
  const [source, setSource] = useState<string>('all')
  const [search, setSearch] = useState('')
  const [health, setHealth] = useState<'all' | 'ok'>('all')
  const [selectedKey, setSelectedKey] = useState<string | null>(null)
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(initialNodeId)
  const [focusToken, setFocusToken] = useState(0)
  const [listOpen, setListOpen] = useState(false)
  const [job, setJob] = useState<BatchJob | null>(null)
  const jobTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  // Quick-add inside empty map state
  const [quickTab, setQuickTab] = useState<'url' | 'static'>('url')
  const [quickUrl, setQuickUrl] = useState('')
  const [quickStatic, setQuickStatic] = useState('')
  const [quickProto, setQuickProto] = useState<SimpleProxyType>('socks5')
  const [quickBusy, setQuickBusy] = useState(false)
  const [quickError, setQuickError] = useState<string | null>(null)

  // Browser details state
  const [name, setName] = useState('')
  const [group, setGroup] = useState('')
  const [creatingGroup, setCreatingGroup] = useState(false)
  const [newGroupName, setNewGroupName] = useState('')
  const [tagsText, setTagsText] = useState('')
  const [notes, setNotes] = useState('')

  // Fixed-optimization isolation state (defaults match Requirement #7)
  const [os, setOs] = useState<IsolationOs>(() => guessHostOs())
  const [tier, setTier] = useState<IsolationTier>('strict')
  const [webrtcPolicy, setWebrtcPolicy] = useState<WebRtcPolicy>('block')
  const [canvasPolicy, setCanvasPolicy] = useState<CanvasPolicy>('stable_hardware')
  const [timezoneMode, setTimezoneMode] = useState<TimezoneMode>('fixed')
  const [localeMode, setLocaleMode] = useState<LocaleMode>('fixed')
  const [installWebrtcGuard, setInstallWebrtcGuard] = useState(true)
  const [ipDriftGuard, setIpDriftGuard] = useState(true)
  const [advancedOpen, setAdvancedOpen] = useState(false)
  const [preview, setPreview] = useState<IsolationPreview | null>(null)
  const [loadingPreview, setLoadingPreview] = useState(true)
  const [previewError, setPreviewError] = useState<string | null>(null)

  // Submission state
  const [submitting, setSubmitting] = useState<'create' | 'launch' | null>(null)
  const [submitError, setSubmitError] = useState<string | null>(null)

  const panelRef = useRef<HTMLDivElement>(null)

  const loadMapData = useCallback(async () => {
    setLoadingMap(true)
    try {
      const [mapData, nodeData, subData, statusData] = await Promise.all([
        networkAPI.map(),
        networkAPI.nodes({ limit: 1000 }),
        networkAPI.subscriptions(),
        networkAPI.status(),
      ])
      setMap(mapData)
      setNodes(Object.fromEntries(nodeData.nodes.map(item => [item.id, item])))
      setSubscriptions(subData)
      setNetStatus(statusData)
    } catch {
      // Keep UI usable even if network subsystem is not configured yet.
    } finally {
      setLoadingMap(false)
    }
  }, [])

  const fetchPreview = useCallback(async (nextOs: IsolationOs, nextTier: IsolationTier, excludePresetId?: string | null) => {
    setLoadingPreview(true)
    setPreviewError(null)
    try {
      const data = await profilesAPI.isolationPreview({
        os: nextOs,
        tier: nextTier,
        exclude_preset_id: excludePresetId ?? undefined,
      })
      setPreview(data)
    } catch (err) {
      setPreviewError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoadingPreview(false)
    }
  }, [])

  // Hydrate map & initial isolation preview once when modal mounts
  useEffect(() => {
    let active = true
    Promise.all([
      networkAPI.map(),
      networkAPI.nodes({ limit: 1000 }),
      networkAPI.subscriptions(),
      networkAPI.status(),
    ])
      .then(([mapData, nodeData, subData, statusData]) => {
        if (!active) return
        setMap(mapData)
        setNodes(Object.fromEntries(nodeData.nodes.map(item => [item.id, item])))
        setSubscriptions(subData)
        setNetStatus(statusData)
      })
      .catch(() => { /* Keep UI usable even if network subsystem is not configured yet. */ })
      .finally(() => { if (active) setLoadingMap(false) })

    profilesAPI
      .isolationPreview({ os, tier })
      .then(data => { if (active) setPreview(data) })
      .catch(err => { if (active) setPreviewError(err instanceof Error ? err.message : String(err)) })
      .finally(() => { if (active) setLoadingPreview(false) })

    networkAPI.currentJob().then(current => { if (active && current) { setJob(current); pollJob(current.id) } }).catch(() => {})
    return () => {
      active = false
      if (jobTimer.current) clearTimeout(jobTimer.current)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Hide any active native embedded browser viewport while this split-pane modal is open
  useEffect(() => acquireViewportOverlay(), [])

  // Escape & body scroll lock
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !submitting) onClose()
    }
    document.addEventListener('keydown', onKeyDown)
    const prevOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      document.removeEventListener('keydown', onKeyDown)
      document.body.style.overflow = prevOverflow
    }
  }, [submitting, onClose])

  function pollJob(id: string, tick = 0) {
    if (jobTimer.current) clearTimeout(jobTimer.current)
    jobTimer.current = setTimeout(async () => {
      try {
        const current = await networkAPI.job(id)
        setJob(current)
        if (current.state === 'queued' || current.state === 'running') {
          if (tick % 3 === 2) {
            const [mapData, nodeData] = await Promise.all([networkAPI.map(), networkAPI.nodes({ limit: 1000 })])
            setMap(mapData)
            setNodes(Object.fromEntries(nodeData.nodes.map(item => [item.id, item])))
          }
          pollJob(id, tick + 1)
          return
        }
        await loadMapData()
        setJob(null)
        toast(
          current.state === 'finished' ? 'ok' : 'error',
          t('Batch check finished'),
          t('{ok} reachable, {failed} failed, one engine process.', { ok: current.ok, failed: current.failed }),
        )
      } catch {
        setJob(null)
      }
    }, 800)
  }

  async function checkUnmeasured() {
    const candidates = Object.values(nodes).filter(
      node => node.status === 'active'
        && (source === 'all' || node.subscription_id === source)
        && (!node.check || node.check.stale),
    )
    const ids = (candidates.length ? candidates : Object.values(nodes).filter(n => n.status === 'active' && (source === 'all' || n.subscription_id === source)))
      .slice(0, 500)
      .map(node => node.id)
    if (!ids.length) {
      toast('info', t('Nothing to check'), t('Every active node has a recent measurement.'))
      return
    }
    try {
      const started = await networkAPI.checkBatch(ids)
      setJob(started)
      pollJob(started.id)
    } catch (err) {
      if (err instanceof ApiRequestError && err.status === 409) {
        const current = await networkAPI.currentJob().catch(() => null)
        if (current) { setJob(current); pollJob(current.id) }
      } else {
        toast('error', t('Could not start the batch check'), t(err instanceof Error ? err.message : String(err)))
      }
    }
  }

  async function handleQuickImport() {
    setQuickBusy(true)
    setQuickError(null)
    try {
      if (quickTab === 'url') {
        const res = await networkAPI.createFromUrl(quickUrl.trim())
        setQuickUrl('')
        await loadMapData()
        const freshNodes = (await networkAPI.nodes({ limit: 1000 })).nodes
        const toCheck = freshNodes.filter(n => n.subscription_id === res.subscription.id && n.status === 'active').map(n => n.id)
        if (toCheck.length && netStatus?.engine.available) {
          const started = await networkAPI.checkBatch(toCheck.slice(0, 500))
          setJob(started)
          pollJob(started.id)
        }
        toast('ok', t('Subscription imported'), res.subscription.name)
      } else {
        const res = await networkAPI.addManualText({ group_name: t('Dedicated static IPs') }, quickStatic.trim(), quickProto)
        setQuickStatic('')
        await loadMapData()
        if (res.created.length) {
          setSelectedNodeId(res.created[0].id)
          if (netStatus?.engine.available) {
            const started = await networkAPI.checkBatch(res.created.map(n => n.id))
            setJob(started)
            pollJob(started.id)
          }
          toast('ok', t('Fixed nodes added'), t('{count} added to {group}. Checking them now.', { count: res.created.length, group: res.group.name }))
        }
      }
    } catch (err) {
      setQuickError(t(err instanceof Error ? err.message : String(err)))
    } finally {
      setQuickBusy(false)
    }
  }

  async function createGroupInline() {
    const trimmed = newGroupName.trim()
    if (!trimmed) return
    try {
      const created = await groupsAPI.create({ name: trimmed })
      onGroupCreated?.(created)
      setGroup(created.id)
      setNewGroupName('')
      setCreatingGroup(false)
      toast('ok', t('Group created'), created.name)
    } catch (err) {
      toast('error', t('Could not create group'), t(err instanceof Error ? err.message : String(err)))
    }
  }

  const maskIps = netStatus?.settings.mask_exit_ips ?? false
  const query = search.trim().toLowerCase()

  const features = useMemo(() => (map?.features.features ?? []).filter(feature => {
    const p = feature.properties
    if (source !== 'all' && p.subscription_id !== source) return false
    if (health === 'ok' && p.health !== 'ok') return false
    if (!query) return true
    return [p.label, p.city, p.region, p.country_iso, p.exit_ip, p.subscription_name, countryName(p.country_iso, locale)]
      .some(value => value && value.toLowerCase().includes(query))
  }), [map, source, health, query, locale])

  const locationGroups = useMemo(() => groupByLocation(features), [features])

  // Flat list for quick search drawer (includes unlocated nodes too)
  const filteredNodeList = useMemo(() => Object.values(nodes)
    .filter(node => {
      if (node.status !== 'active') return false
      if (source !== 'all' && node.subscription_id !== source) return false
      if (health === 'ok' && node.check?.health !== 'ok') return false
      if (!query) return true
      return [node.name, node.subscription_name, node.check?.exit_ip, node.check?.city, node.check?.country_iso, countryName(node.check?.country_iso, locale)]
        .some(value => value && value.toLowerCase().includes(query))
    })
    .sort((a, b) => {
      const aOk = a.check?.health === 'ok' ? 0 : 1
      const bOk = b.check?.health === 'ok' ? 0 : 1
      if (aOk !== bOk) return aOk - bOk
      return (a.check?.latency_ms ?? 99999) - (b.check?.latency_ms ?? 99999)
    }), [nodes, source, health, query, locale])

  const selectedNode = selectedNodeId ? nodes[selectedNodeId] ?? null : null
  const selectedFeature = selectedNodeId
    ? (map?.features.features ?? []).find(f => f.properties.node_id === selectedNodeId)?.properties ?? null
    : null

  const selectedBoundCount = selectedNode
    ? (selectedNode.bound_count ?? selectedNode.profile_count ?? 0)
    : (selectedFeature?.bound_count ?? selectedFeature?.profile_count ?? 0)
  const selectedSharedExitCount = selectedNode?.shared_exit_count ?? selectedFeature?.shared_exit_count ?? 1

  const cloudSubs = useMemo(() => subscriptions.filter(s => s.managed_by === 'cloud'), [subscriptions])
  const localAirports = useMemo(() => subscriptions.filter(s => !s.manual && s.managed_by !== 'cloud'), [subscriptions])
  const staticGroups = useMemo(() => filterLocalStaticGroups(subscriptions), [subscriptions])
  const selectedSub = selectedNode ? subscriptions.find(s => s.id === selectedNode.subscription_id) ?? null : null
  const selectedProtected = selectedNode ? isProtectedNodeProtocol(selectedNode, selectedSub) : false

  function handleSelectNode(nodeId: string) {
    setSelectedNodeId(nodeId)
    const foundGroup = locationGroups.find(g => g.nodes.some(n => n.node_id === nodeId))
    if (foundGroup) setSelectedKey(foundGroup.key)
  }

  async function handleSubmit(launchNow: boolean) {
    const trimmedName = name.trim()
    if (!trimmedName) {
      setSubmitError(t('Please enter a browser name.'))
      return
    }
    setSubmitting(launchNow ? 'launch' : 'create')
    setSubmitError(null)
    try {
      const tags = tagsText
        .split(/[,，]/)
        .map(tag => tag.trim())
        .filter(Boolean)

      const created = await profilesAPI.createProfile({
        name: trimmedName,
        group: group || null,
        tags,
        notes: notes.trim() || null,
        node_id: selectedNodeId || null,
        ip_drift_guard: ipDriftGuard,
        isolation: {
          os,
          tier,
          preset_id: preview?.isolation.preset_id ?? preview?.summary.preset_id ?? null,
          webrtc_policy: webrtcPolicy,
          canvas_policy: canvasPolicy,
          locale_mode: localeMode,
          languages: localeMode === 'fixed' ? ['en-US', 'en'] : (preview?.isolation.languages ?? ['en-US', 'en']),
          timezone_mode: timezoneMode,
          timezone: timezoneMode === 'fixed' ? 'America/Los_Angeles' : null,
          install_webrtc_guard: installWebrtcGuard,
        },
      })

      void refreshProfileDirectory()

      let launched = false
      if (launchNow) {
        try {
          const res = await openProfileInWorkspace(created.id)
          if (res.ok) {
            launched = true
          } else {
            toast(
              'error',
              t('Browser created, but launch failed'),
              t(res.message ?? 'Could not start the browser'),
            )
          }
        } catch (launchErr) {
          toast(
            'error',
            t('Browser created, but launch failed'),
            t(launchErr instanceof Error ? launchErr.message : String(launchErr)),
          )
        }
      }
      await onCreated(created, launched)
      onClose()
    } catch (err) {
      setSubmitError(t(err instanceof Error ? err.message : String(err)))
    } finally {
      setSubmitting(null)
    }
  }

  const jobActive = Boolean(job && (job.state === 'queued' || job.state === 'running'))
  const totalNodes = map?.total_nodes ?? Object.keys(nodes).length
  const summary = preview?.summary
  const purityScore = selectedNode?.intel?.composite?.purity_score ?? selectedFeature?.purity_score ?? null
  const pTone = purityTone(purityScore)

  return (
    <div
      className="overlay-in modal-scrim fixed inset-0 z-40 flex items-center justify-center p-4 backdrop-blur-[6px]"
      onMouseDown={event => {
        if (event.target === event.currentTarget && !submitting) onClose()
      }}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label={t('Create isolated browser')}
        className="dialog-in modal-sheet flex h-[84vh] min-h-[560px] w-[calc(100vw-32px)] max-w-[1160px] flex-col overflow-hidden rounded-2xl"
      >
        <div className="prism-hairline h-[1.5px] w-full shrink-0 opacity-80" aria-hidden="true" />
        {/* Top Modal Header */}
        <header className="flex shrink-0 items-center justify-between gap-4 border-b border-line bg-surface px-5 py-3">
          <div className="flex min-w-0 items-center gap-3">
            <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-xl bg-signal-shade text-signal ring-1 ring-signal/20">
              <Globe2 size={16} strokeWidth={2} aria-hidden="true" />
            </span>
            <div className="min-w-0">
              <h2 className="text-[14.5px] font-semibold tracking-tight text-ink">
                {t('Create isolated browser')}
              </h2>
              <p className="truncate text-[12px] text-ink-dim">
                {t('Pick an exit node on the world map, auto-assign a deduplicated real device, and launch in one step.')}
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={() => { if (!submitting) onClose() }}
            aria-label={t('Close')}
            className="btn btn-ghost h-8 w-8 p-0"
          >
            <X size={16} />
          </button>
        </header>

        {/* Split Pane Body */}
        <div className="flex min-h-0 flex-1 flex-col lg:flex-row">
          {/* LEFT PANE (~64%): Embedded WorldMap Picker */}
          <section
            aria-label={t('World node selector')}
            className="relative flex min-h-[300px] flex-1 flex-col border-b border-line bg-canvas lg:w-[64%] lg:flex-none lg:border-b-0 lg:border-r"
          >
            {/* Floating Filter Bar */}
            {totalNodes > 0 && (
              <div className="pointer-events-none absolute left-3 right-14 top-3 z-10 flex flex-wrap items-center gap-2">
                <div className="modal-sheet pointer-events-auto flex flex-wrap items-center gap-1.5 rounded-xl p-1.5 backdrop-blur-xl">
                  <select
                    aria-label={t('Node source')}
                    value={source}
                    onChange={event => { setSource(event.target.value); setSelectedKey(null) }}
                    className="field h-[28px] w-auto max-w-[175px] border-0 bg-raised/70 py-0 text-[12px]"
                  >
                    <option value="all">{t('All node sources')}</option>
                    {cloudSubs.length > 0 && (
                      <optgroup label={t('Cloud subscriptions')}>
                        {cloudSubs.map(sub => <option key={sub.id} value={sub.id}>{sub.name} ({sub.node_counts.active})</option>)}
                      </optgroup>
                    )}
                    {localAirports.length > 0 && (
                      <optgroup label={t('Local airports')}>
                        {localAirports.map(sub => <option key={sub.id} value={sub.id}>{sub.name} ({sub.node_counts.active})</option>)}
                      </optgroup>
                    )}
                    {staticGroups.length > 0 && (
                      <optgroup label={t('Dedicated static IPs')}>
                        {staticGroups.map(sub => <option key={sub.id} value={sub.id}>{sub.name} ({sub.node_counts.active})</option>)}
                      </optgroup>
                    )}
                  </select>

                  <div className="relative w-[170px]">
                    <Search size={12} aria-hidden="true" className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-ink-faint" />
                    <input
                      aria-label={t('Search country, city or node')}
                      placeholder={t('Search country or node…')}
                      value={search}
                      onChange={event => setSearch(event.target.value)}
                      className="field h-[28px] border-0 bg-raised/70 py-0 pl-7 text-[12px]"
                    />
                  </div>

                  <div className="inline-flex rounded-lg bg-raised/70 p-0.5 text-[11.5px]">
                    <button
                      type="button"
                      onClick={() => setHealth('all')}
                      className={`cursor-pointer rounded-md px-2 py-0.5 transition-colors ${health === 'all' ? 'bg-surface font-medium text-ink shadow-xs' : 'text-ink-dim hover:text-ink'}`}
                    >
                      {t('All')}
                    </button>
                    <button
                      type="button"
                      onClick={() => setHealth('ok')}
                      className={`cursor-pointer rounded-md px-2 py-0.5 transition-colors ${health === 'ok' ? 'bg-surface font-medium text-ok shadow-xs' : 'text-ink-dim hover:text-ink'}`}
                    >
                      {t('Online')}
                    </button>
                  </div>

                  {jobActive && job ? (
                    <span className="inline-flex items-center gap-1.5 rounded-lg bg-signal-shade px-2.5 py-1 text-[11.5px] font-medium text-signal">
                      <LoaderCircle size={12} className="animate-spin" aria-hidden="true" />
                      {t('Checking {done}/{total}', { done: job.done, total: job.total })}
                    </span>
                  ) : (
                    <button
                      type="button"
                      onClick={checkUnmeasured}
                      disabled={!netStatus?.engine.available}
                      className="btn btn-default h-[28px] px-2.5 text-[11.5px]"
                      title={netStatus?.engine.available ? t('Probe unmeasured nodes to place them on the map') : t('Proxy routing engine unavailable')}
                    >
                      <Activity size={12} aria-hidden="true" />
                      {t('Check unmeasured')}
                    </button>
                  )}
                </div>
              </div>
            )}

            {/* Map Canvas */}
            <div className="relative min-h-0 flex-1">
              <WorldMap
                mode="picker"
                groups={locationGroups}
                selectedKey={selectedKey}
                onSelect={key => setSelectedKey(key)}
                selectedNodeId={selectedNodeId}
                onSelectNode={handleSelectNode}
                maskIps={maskIps}
                focusToken={focusToken}
                fitKey={source}
              />

              {/* Empty-state overlay when no nodes exist yet */}
              {!loadingMap && totalNodes === 0 && (
                <div className="absolute inset-0 z-20 flex items-center justify-center bg-canvas/75 p-6 backdrop-blur-sm">
                  <div className="modal-sheet w-full max-w-[440px] rounded-2xl p-5">
                    <div className="flex items-center gap-2.5">
                      <span className="flex h-8 w-8 items-center justify-center rounded-xl bg-signal-shade text-signal">
                        <Rss size={15} aria-hidden="true" />
                      </span>
                      <div>
                        <h3 className="text-[14px] font-semibold text-ink">{t('No proxy nodes imported yet')}</h3>
                        <p className="text-[12px] text-ink-dim">{t('Quickly import a subscription or paste a static IP to pick it right here.')}</p>
                      </div>
                    </div>

                    <div className="mt-4 inline-flex rounded-lg bg-sidebar p-0.5 text-[12px]">
                      <button
                        type="button"
                        onClick={() => { setQuickTab('url'); setQuickError(null) }}
                        className={`flex cursor-pointer items-center gap-1.5 rounded-md px-3 py-1 transition-colors ${quickTab === 'url' ? 'bg-surface font-medium text-ink shadow-xs' : 'text-ink-dim hover:text-ink'}`}
                      >
                        <Rss size={12} aria-hidden="true" />
                        {t('Airport subscription URL')}
                      </button>
                      <button
                        type="button"
                        onClick={() => { setQuickTab('static'); setQuickError(null) }}
                        className={`flex cursor-pointer items-center gap-1.5 rounded-md px-3 py-1 transition-colors ${quickTab === 'static' ? 'bg-surface font-medium text-ink shadow-xs' : 'text-ink-dim hover:text-ink'}`}
                      >
                        <Server size={12} aria-hidden="true" />
                        {t('Static IP (IP:port:user:pass)')}
                      </button>
                    </div>

                    {quickTab === 'url' ? (
                      <div className="mt-3 space-y-2.5">
                        <input
                          type="url"
                          className="field font-mono text-[12px]"
                          value={quickUrl}
                          onChange={event => setQuickUrl(event.target.value)}
                          placeholder="https://example.com/subscription"
                          spellCheck={false}
                        />
                        <button
                          type="button"
                          className="btn btn-primary w-full"
                          disabled={quickBusy || !quickUrl.trim().startsWith('https://')}
                          onClick={handleQuickImport}
                        >
                          {quickBusy && <LoaderCircle size={13} className="animate-spin" aria-hidden="true" />}
                          {t('Import and test nodes')}
                        </button>
                      </div>
                    ) : (
                      <div className="mt-3 space-y-2.5">
                        <div className="flex items-center gap-2">
                          <select
                            className="field h-[30px] w-[110px] py-0 text-[12px]"
                            value={quickProto}
                            onChange={event => setQuickProto(event.target.value as SimpleProxyType)}
                            aria-label={t('Protocol')}
                          >
                            <option value="socks5">SOCKS5</option>
                            <option value="http">HTTP</option>
                            <option value="https">HTTPS</option>
                          </select>
                          <input
                            className="field h-[30px] flex-1 font-mono text-[12px]"
                            value={quickStatic}
                            onChange={event => setQuickStatic(event.target.value)}
                            placeholder="1.2.3.4:1080:user:pass"
                            spellCheck={false}
                          />
                        </div>
                        <button
                          type="button"
                          className="btn btn-primary w-full"
                          disabled={quickBusy || !quickStatic.trim()}
                          onClick={handleQuickImport}
                        >
                          {quickBusy && <LoaderCircle size={13} className="animate-spin" aria-hidden="true" />}
                          {t('Add static IP and select')}
                        </button>
                      </div>
                    )}

                    {quickError && <p className="mt-2 text-[12px] text-danger" role="alert">{quickError}</p>}
                    <p className="mt-3 text-center text-[11px] text-ink-faint">
                      {t('You can also create the browser now and bind a node later from the Node map.')}
                    </p>
                  </div>
                </div>
              )}

              {/* Bottom Left Legend + Quick List Toggle */}
              {totalNodes > 0 && (
                <div className="absolute bottom-3 left-3 z-10 flex flex-wrap items-center gap-2">
                  <button
                    type="button"
                    onClick={() => setListOpen(v => !v)}
                    className="modal-sheet inline-flex cursor-pointer items-center gap-1.5 rounded-xl px-2.5 py-1.5 text-[11.5px] font-medium text-ink backdrop-blur-xl hover:bg-raised"
                  >
                    <List size={12} aria-hidden="true" />
                    {t('Node list ({count})', { count: filteredNodeList.length })}
                  </button>
                  <div className="modal-sheet pointer-events-none hidden items-center gap-3 rounded-xl px-3 py-1.5 text-[11px] text-ink-dim backdrop-blur-xl sm:flex" aria-hidden="true">
                    <span className="flex items-center gap-1.5"><span className="h-2 w-2 rounded-full bg-ok-dot" />{t('Reachable')}</span>
                    <span className="flex items-center gap-1.5"><span className="h-2 w-2 rounded-full bg-warn-dot" />{t('Mixed')}</span>
                    <span className="flex items-center gap-1.5"><span className="h-2 w-2 rounded-full bg-danger-dot" />{t('Failed')}</span>
                    <span className="flex items-center gap-1.5"><span className="h-2 w-2 rounded-full bg-signal" />{t('Selected')}</span>
                  </div>
                </div>
              )}

              {/* Sliding Quick Node List Drawer (for unlocated nodes or list selection) */}
              {listOpen && totalNodes > 0 && (
                <div className="dialog-in modal-sheet absolute bottom-12 left-3 z-20 flex max-h-[64%] w-[330px] max-w-[calc(100%-24px)] flex-col overflow-hidden rounded-xl backdrop-blur-xl">
                  <div className="flex items-center justify-between border-b border-line px-3.5 py-2">
                    <span className="text-[12px] font-semibold text-ink">{t('All matching nodes')}</span>
                    <button type="button" onClick={() => setListOpen(false)} className="rounded p-0.5 text-ink-faint hover:bg-raised hover:text-ink" aria-label={t('Close')}>
                      <X size={13} />
                    </button>
                  </div>
                  <ul className="divide-y divide-line overflow-y-auto">
                    {filteredNodeList.slice(0, 120).map(node => {
                      const isSel = selectedNodeId === node.id
                      const bound = node.bound_count ?? node.profile_count ?? 0
                      return (
                        <li key={node.id}>
                          <button
                            type="button"
                            onClick={() => {
                              handleSelectNode(node.id)
                              setFocusToken(v => v + 1)
                              setListOpen(false)
                            }}
                            className={`flex w-full items-start gap-2.5 px-3.5 py-2 text-left transition-colors ${isSel ? 'bg-signal-shade/70' : 'hover:bg-sidebar'}`}
                          >
                            <span className="pt-0.5 text-[14px] leading-none" aria-hidden="true">
                              {flag(node.check?.country_iso) || '·'}
                            </span>
                            <span className="min-w-0 flex-1">
                              <span className="flex items-center gap-1.5">
                                <span className="truncate font-medium text-ink">{node.name}</span>
                                {isSel && <Check size={12} className="shrink-0 text-signal" />}
                              </span>
                              <span className="mt-0.5 flex flex-wrap items-center gap-1.5 text-[11px] text-ink-dim">
                                <span className="font-mono">{maskIp(node.check?.exit_ip, maskIps)}</span>
                                {bound > 0 && <span className="rounded-full bg-warn-shade px-1.5 text-[10px] text-warn">{t('Used by {count}', { count: bound })}</span>}
                              </span>
                            </span>
                            <span className={`shrink-0 font-mono text-[11px] ${node.check?.health === 'ok' ? 'text-ok' : 'text-ink-faint'}`}>
                              {node.check?.health === 'ok' && node.check.latency_ms != null ? `${node.check.latency_ms} ms` : t('Unchecked')}
                            </span>
                          </button>
                        </li>
                      )
                    })}
                    {filteredNodeList.length === 0 && (
                      <li className="px-4 py-6 text-center text-[12px] text-ink-dim">{t('No nodes match these filters.')}</li>
                    )}
                  </ul>
                </div>
              )}
            </div>
          </section>

          {/* RIGHT PANE (~36%): Selected Node + Browser Form + Fixed-Optimization Isolation Card */}
          <aside className="flex min-h-0 flex-1 flex-col bg-surface lg:w-[36%] lg:flex-none">
            <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-4">
              {/* 1. Selected IP Node Preview Card */}
              <section aria-label={t('Selected IP node')}>
                <div className="mb-1.5 flex items-center justify-between">
                  <span className="text-[11px] font-semibold tracking-wide text-ink-faint">{t('Selected IP node')}</span>
                  {selectedNodeId && (
                    <button
                      type="button"
                      onClick={() => setSelectedNodeId(null)}
                      className="text-[11px] text-ink-faint hover:text-ink"
                    >
                      {t('Clear selection')}
                    </button>
                  )}
                </div>

                {selectedNode ? (
                  <div className="rounded-xl border border-signal/25 bg-signal-shade/35 p-3">
                    <div className="flex items-start gap-2.5">
                      <span className="text-[18px] leading-none" aria-hidden="true">
                        {flag(selectedNode.check?.country_iso) || '🌐'}
                      </span>
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-1.5">
                          <p className="truncate text-[13px] font-semibold text-ink" title={selectedNode.name}>
                            {selectedNode.name}
                          </p>
                          <span
                            className={
                              selectedProtected
                                ? 'inline-flex shrink-0 items-center gap-1 rounded-full bg-ok-shade px-2 py-0.5 text-[10.5px] font-medium text-ok'
                                : 'shrink-0 rounded-md bg-surface px-1.5 py-0.5 font-mono text-[10px] uppercase text-ink-dim ring-1 ring-line'
                            }
                          >
                            {selectedProtected && <Lock size={10} aria-hidden="true" />}
                            {nodeProtocolLabel(selectedNode, t, selectedSub)}
                          </span>
                        </div>
                        <p className="mt-0.5 truncate text-[11.5px] text-ink-dim">
                          {[selectedNode.check?.city, countryName(selectedNode.check?.country_iso, locale)].filter(Boolean).join(' · ') || selectedNode.subscription_name}
                        </p>
                      </div>
                    </div>

                    <div className="mt-2.5 flex flex-wrap items-center gap-2 text-[11.5px]">
                      <span className="rounded-md bg-surface px-2 py-0.5 font-mono text-ink ring-1 ring-line">
                        {maskIp(selectedNode.check?.exit_ip, maskIps)}
                      </span>
                      {selectedNode.check?.latency_ms != null && (
                        <span className="rounded-full bg-ok-shade px-2 py-0.5 font-mono text-[11px] font-medium text-ok">
                          {selectedNode.check.latency_ms} ms
                        </span>
                      )}
                      {purityScore != null && (
                        <span className={`rounded-full px-2 py-0.5 font-mono text-[11px] font-medium ${
                          pTone === 'ok' ? 'bg-ok-shade text-ok' : pTone === 'warn' ? 'bg-warn-shade text-warn' : 'bg-danger-shade text-danger'
                        }`}>
                          {t('Purity {score}', { score: Math.round(purityScore) })}
                        </span>
                      )}
                    </div>

                    {(selectedBoundCount > 0 || selectedSharedExitCount > 1) && (
                      <div className="mt-2.5 flex items-start gap-1.5 rounded-lg border border-warn/25 bg-warn-shade px-2.5 py-1.5 text-[11.5px] text-warn" role="status">
                        <TriangleAlert size={13} className="mt-0.5 shrink-0" aria-hidden="true" />
                        <span>
                          {selectedBoundCount > 0
                            ? t('This node is already bound to {count} browsers. Pick an unused node to avoid IP linkage.', { count: selectedBoundCount })
                            : t('This exit IP is shared by {count} nodes.', { count: selectedSharedExitCount })}
                        </span>
                      </div>
                    )}
                  </div>
                ) : (
                  <div className="rounded-xl border border-dashed border-line-strong bg-sidebar/60 px-3.5 py-3 text-center">
                    <p className="text-[12px] font-medium text-ink-dim">{t('Click any pin on the map to bind an isolated exit IP')}</p>
                    <p className="mt-0.5 text-[11px] text-ink-faint">{t('Optional — you can also bind or switch nodes later.')}</p>
                  </div>
                )}
              </section>

              {/* 2. Browser Details */}
              <section aria-label={t('Browser details')} className="space-y-3">
                <span className="block text-[11px] font-semibold tracking-wide text-ink-faint">{t('Browser details')}</span>

                <div>
                  <label className="field-label" htmlFor="create-browser-name">{t('Browser name')}</label>
                  <input
                    id="create-browser-name"
                    className="field"
                    value={name}
                    maxLength={100}
                    onChange={event => setName(event.target.value)}
                    placeholder={t('e.g. US Store #01')}
                    data-modal-autofocus
                  />
                </div>

                <div className="grid grid-cols-2 gap-2.5">
                  <div>
                    <div className="mb-1 flex items-center justify-between">
                      <label className="field-label mb-0" htmlFor="create-browser-group">{t('Group')}</label>
                      <button
                        type="button"
                        onClick={() => setCreatingGroup(v => !v)}
                        className="inline-flex items-center gap-0.5 text-[11px] font-medium text-signal hover:underline"
                      >
                        <Plus size={10} aria-hidden="true" />
                        {t('New group')}
                      </button>
                    </div>
                    {creatingGroup ? (
                      <div className="flex gap-1">
                        <input
                          className="field h-[32px] px-2 text-[12px]"
                          value={newGroupName}
                          onChange={event => setNewGroupName(event.target.value)}
                          placeholder={t('Name')}
                          maxLength={60}
                        />
                        <button type="button" className="btn btn-primary h-[32px] px-2 text-[11.5px]" onClick={createGroupInline} disabled={!newGroupName.trim()}>
                          {t('Add')}
                        </button>
                      </div>
                    ) : (
                      <select
                        id="create-browser-group"
                        className="field h-[32px] py-0 text-[12.5px]"
                        value={group}
                        onChange={event => setGroup(event.target.value)}
                      >
                        <option value="">{t('Ungrouped')}</option>
                        {groups.map(item => (
                          <option key={item.id} value={item.id}>{item.name}</option>
                        ))}
                      </select>
                    )}
                  </div>

                  <div>
                    <label className="field-label" htmlFor="create-browser-tags">{t('Tags')}</label>
                    <input
                      id="create-browser-tags"
                      className="field h-[32px] py-0 text-[12.5px]"
                      value={tagsText}
                      onChange={event => setTagsText(event.target.value)}
                      placeholder={t('shop, ads')}
                    />
                  </div>
                </div>

                <div>
                  <label className="field-label" htmlFor="create-browser-notes">{t('Memo')}</label>
                  <textarea
                    id="create-browser-notes"
                    rows={2}
                    className="field resize-none text-[12.5px]"
                    value={notes}
                    onChange={event => setNotes(event.target.value)}
                    placeholder={t('Optional notes or account label…')}
                  />
                </div>
              </section>

              {/* 3. Fixed-Optimization Isolation Card */}
              <section aria-label={t('Fixed-optimization isolation')} className="rounded-xl border border-line bg-canvas/65 p-3.5">
                <div className="flex items-center justify-between gap-2">
                  <div className="flex items-center gap-1.5">
                    <ShieldCheck size={14} className="text-ok" aria-hidden="true" />
                    <span className="text-[12px] font-semibold text-ink">{t('Fixed-optimization fingerprint')}</span>
                  </div>
                  <button
                    type="button"
                    className="inline-flex cursor-pointer items-center gap-1 rounded-md px-2 py-0.5 text-[11.5px] font-medium text-signal transition-colors hover:bg-signal-shade"
                    disabled={loadingPreview}
                    onClick={() => fetchPreview(os, tier, preview?.isolation.preset_id ?? preview?.summary.preset_id ?? null)}
                  >
                    <RefreshCw size={11} className={loadingPreview ? 'animate-spin' : ''} aria-hidden="true" />
                    {t('Switch real device')}
                  </button>
                </div>

                {/* OS + Tier Selectors */}
                <div className="mt-2.5 grid grid-cols-2 gap-2">
                  <div>
                    <span className="mb-1 block text-[10.5px] font-medium text-ink-faint">{t('Operating system')}</span>
                    <div className="grid grid-cols-3 rounded-lg border border-line bg-surface p-0.5 text-[11px]">
                      {OS_CHOICES.map(choice => (
                        <button
                          key={choice.value}
                          type="button"
                          onClick={() => { setOs(choice.value); void fetchPreview(choice.value, tier) }}
                          className={`cursor-pointer rounded-md py-1 font-medium transition-colors ${
                            os === choice.value ? 'bg-signal text-white' : 'text-ink-dim hover:text-ink'
                          }`}
                        >
                          {choice.label}
                        </button>
                      ))}
                    </div>
                  </div>
                  <div>
                    <span className="mb-1 block text-[10.5px] font-medium text-ink-faint">{t('Isolation tier')}</span>
                    <div className="grid grid-cols-2 rounded-lg border border-line bg-surface p-0.5 text-[11px]">
                      {(['strict', 'standard'] as IsolationTier[]).map(choice => (
                        <button
                          key={choice}
                          type="button"
                          onClick={() => { setTier(choice); void fetchPreview(os, choice) }}
                          className={`cursor-pointer rounded-md py-1 font-medium transition-colors ${
                            tier === choice ? 'bg-signal text-white' : 'text-ink-dim hover:text-ink'
                          }`}
                        >
                          {choice === 'strict' ? t('Strict') : t('Standard')}
                        </button>
                      ))}
                    </div>
                  </div>
                </div>

                {/* Auto-Assigned Deduplicated Hardware Preview */}
                <div className="mt-2.5 rounded-lg border border-line bg-surface p-2.5">
                  {loadingPreview && !preview ? (
                    <div className="flex items-center gap-2 py-2 text-[12px] text-ink-dim">
                      <LoaderCircle size={13} className="animate-spin text-signal" aria-hidden="true" />
                      {t('Allocating unique real device…')}
                    </div>
                  ) : previewError ? (
                    <p className="text-[11.5px] text-danger">{t(previewError)}</p>
                  ) : summary ? (
                    <>
                      <div className="flex flex-wrap items-center justify-between gap-1.5">
                        <div className="flex items-center gap-1.5">
                          <span className="rounded-md bg-sidebar px-1.5 py-0.5 font-mono text-[11px] font-semibold text-ink ring-1 ring-line">
                            {summary.fingerprint_hash}
                          </span>
                          <span className="inline-flex items-center gap-1 rounded-full bg-ok-shade px-2 py-0.5 text-[10.5px] font-medium text-ok">
                            <Sparkles size={10} aria-hidden="true" />
                            {t('Deduplicated')}
                          </span>
                        </div>
                        <span className="font-mono text-[10.5px] text-ink-faint">
                          {preview?.policy_version ?? summary.policy_version}
                        </span>
                      </div>
                      <div className="mt-2 grid grid-cols-2 gap-x-3 gap-y-1 text-[11.5px] text-ink-dim">
                        <span className="flex items-center gap-1.5 truncate">
                          <Monitor size={11} className="shrink-0 text-ink-faint" aria-hidden="true" />
                          <span className="font-mono text-ink">{summary.screen ?? '1440×900'}</span>
                        </span>
                        <span className="flex items-center gap-1.5 truncate">
                          <Cpu size={11} className="shrink-0 text-ink-faint" aria-hidden="true" />
                          <span className="font-mono text-ink">
                            {summary.hardware_concurrency ? t('{count} cores', { count: summary.hardware_concurrency }) : '—'}
                          </span>
                        </span>
                        <span className="col-span-2 truncate font-mono text-[11px] text-ink-dim" title={summary.gpu ?? undefined}>
                          {OS_LABELS[summary.os] ?? summary.os} · {shortGpuName(summary.gpu)}
                        </span>
                      </div>
                    </>
                  ) : null}
                </div>

                {/* Fixed Security Defaults Pills (Requirement #7) */}
                <div className="mt-2.5 flex flex-wrap gap-1.5">
                  <span className="pill bg-surface text-[10.5px] text-ink-dim">
                    <span className="h-1.5 w-1.5 rounded-full bg-signal" aria-hidden="true" />
                    {timezoneMode === 'fixed' ? t('LA timezone (America/Los_Angeles)') : t('Timezone follows exit IP')}
                  </span>
                  <span className="pill bg-surface text-[10.5px] text-ink-dim">
                    <span className="h-1.5 w-1.5 rounded-full bg-signal" aria-hidden="true" />
                    {localeMode === 'fixed' ? t('English (en-US)') : t('Language follows exit IP')}
                  </span>
                  <span className={`pill text-[10.5px] ${installWebrtcGuard ? 'border-ok/25 bg-ok-shade/70 text-ok' : 'bg-surface text-ink-faint'}`}>
                    <span className={`h-1.5 w-1.5 rounded-full ${installWebrtcGuard ? 'bg-ok-dot' : 'bg-slate-300'}`} aria-hidden="true" />
                    {installWebrtcGuard ? t('WebRTC/DNS guard active') : t('WebRTC/DNS guard off')}
                  </span>
                  <span className="pill border-ok/25 bg-ok-shade/70 text-[10.5px] text-ok">
                    <span className="h-1.5 w-1.5 rounded-full bg-ok-dot" aria-hidden="true" />
                    {t('Full-traffic fail-closed tunnel')}
                  </span>
                </div>

                {/* Exit IP Drift Guard Toggle (default ON) */}
                <label className="mt-2.5 flex cursor-pointer items-center gap-2 rounded-lg border border-line bg-surface px-2.5 py-2 text-[11.5px] text-ink">
                  <input
                    type="checkbox"
                    checked={ipDriftGuard}
                    onChange={e => setIpDriftGuard(e.target.checked)}
                    className="accent-signal"
                  />
                  <span>{t('Exit IP drift guard (block launch if exit IP or country changes)')}</span>
                </label>

                {/* Collapsible Advanced Tweaks */}
                <button
                  type="button"
                  onClick={() => setAdvancedOpen(v => !v)}
                  className="mt-2.5 flex w-full cursor-pointer items-center justify-between text-[11.5px] font-medium text-ink-dim hover:text-ink"
                >
                  <span>{t('Fine-tune isolation policy')}</span>
                  {advancedOpen ? <ChevronUp size={13} /> : <ChevronDown size={13} />}
                </button>

                {advancedOpen && (
                  <div className="mt-2.5 space-y-2.5 border-t border-line pt-2.5 text-[12px]">
                    <div className="grid grid-cols-2 gap-2">
                      <div>
                        <label className="field-label" htmlFor="iso-webrtc">{t('WebRTC policy')}</label>
                        <select
                          id="iso-webrtc"
                          className="field h-[28px] py-0 text-[12px]"
                          value={webrtcPolicy}
                          onChange={e => setWebrtcPolicy(e.target.value as WebRtcPolicy)}
                        >
                          <option value="block">{t('Block completely')}</option>
                          <option value="spoof_exit">{t('Spoof as exit IP')}</option>
                        </select>
                      </div>
                      <div>
                        <label className="field-label" htmlFor="iso-canvas">{t('Canvas protection')}</label>
                        <select
                          id="iso-canvas"
                          className="field h-[28px] py-0 text-[12px]"
                          value={canvasPolicy}
                          onChange={e => setCanvasPolicy(e.target.value as CanvasPolicy)}
                        >
                          <option value="stable_hardware">{t('Stable hardware')}</option>
                          <option value="session_noise">{t('Session noise')}</option>
                        </select>
                      </div>
                    </div>

                    <div className="grid grid-cols-2 gap-2">
                      <div>
                        <label className="field-label" htmlFor="iso-tz">{t('Timezone mode')}</label>
                        <select
                          id="iso-tz"
                          className="field h-[28px] py-0 text-[12px]"
                          value={timezoneMode}
                          onChange={e => setTimezoneMode(e.target.value as TimezoneMode)}
                        >
                          <option value="fixed">{t('Fixed (Los Angeles)')}</option>
                          <option value="follow_exit">{t('Follow exit IP')}</option>
                        </select>
                      </div>
                      <div>
                        <label className="field-label" htmlFor="iso-lang">{t('Language mode')}</label>
                        <select
                          id="iso-lang"
                          className="field h-[28px] py-0 text-[12px]"
                          value={localeMode}
                          onChange={e => setLocaleMode(e.target.value as LocaleMode)}
                        >
                          <option value="fixed">{t('Fixed (en-US)')}</option>
                          <option value="follow_exit">{t('Follow exit IP')}</option>
                        </select>
                      </div>
                    </div>

                    <label className="flex cursor-pointer items-center gap-2 text-[12px] text-ink">
                      <input
                        type="checkbox"
                        checked={installWebrtcGuard}
                        onChange={e => setInstallWebrtcGuard(e.target.checked)}
                      />
                      <span>{t('Install built-in WebRTC & DNS leak guard extension')}</span>
                    </label>
                  </div>
                )}
              </section>
            </div>

            {/* Sticky Right-Pane Footer Actions */}
            <footer className="shrink-0 border-t border-line bg-sidebar/80 px-4 py-3">
              {submitError && (
                <p className="mb-2 text-[12px] text-danger" role="alert">{submitError}</p>
              )}
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  className="btn btn-default flex-1"
                  disabled={submitting !== null || !name.trim()}
                  onClick={() => handleSubmit(false)}
                >
                  {submitting === 'create' ? <LoaderCircle size={13} className="animate-spin" aria-hidden="true" /> : <Plus size={14} aria-hidden="true" />}
                  {t('Create browser')}
                </button>
                <button
                  type="button"
                  className="btn btn-primary flex-[1.2]"
                  disabled={submitting !== null || !name.trim()}
                  onClick={() => handleSubmit(true)}
                >
                  {submitting === 'launch' ? <LoaderCircle size={13} className="animate-spin" aria-hidden="true" /> : <Play size={13} aria-hidden="true" />}
                  {t('Create & launch now')}
                </button>
              </div>
            </footer>
          </aside>
        </div>
      </div>
    </div>
  )
}
