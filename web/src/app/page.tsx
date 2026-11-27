'use client'

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import Link from 'next/link'
import {
  ArrowUpRight,
  Cloud,
  CloudUpload,
  Cookie,
  Copy,
  Download,
  Ellipsis,
  FolderPlus,
  Globe2,
  KeyRound,
  LayoutGrid,
  List,
  LoaderCircle,
  Pencil,
  Play,
  Plus,
  RefreshCw,
  Search,
  ShieldCheck,
  Square,
  Tags,
  Terminal,
  Trash2,
  TriangleAlert,
  Upload,
  X,
} from 'lucide-react'

import { BatchProfileOperations } from '@/components/batch-profile-operations'
import { BrowserSetup } from '@/components/browser-setup'
import { CookieTransfer } from '@/components/cookie-transfer'
import { CreateBrowserModal } from '@/components/create-browser-modal'
import { EditBrowserModal } from '@/components/edit-browser-modal'
import { EmptyState } from '@/components/empty-state'
import { ExitIpDriftModal } from '@/components/exit-ip-drift-modal'
import { useI18n } from '@/components/i18n-provider'
import { IsolationAuditModal } from '@/components/isolation-audit-modal'
import { ConfirmDialog, Modal } from '@/components/modal'
import { ProfileTagsDialog } from '@/components/profile-tags-dialog'
import { useToast } from '@/components/toast'
import { useWorkspace } from '@/components/workspace-provider'
import {
  browsersAPI,
  formatLastUsed,
  groupsAPI,
  OS_LABELS,
  profileFingerprintHash,
  profilesAPI,
  readExitIpDrift,
  shortGpuName,
  type ExitIpDriftDetail,
  type Group,
  type Profile,
} from '@/lib/api'
import type { BatchTarget } from '@/lib/batch-queue'
import {
  cloudAPI,
  isCustomerEdition,
  isEntitlementActive,
  isEntitlementBlocked,
  type CloudStatus,
} from '@/lib/cloud-api'
import { getDesktopBridge } from '@/lib/desktop-bridge'
import { networkAPI, type Binding } from '@/lib/network-api'
import { flag, maskIp } from '@/lib/network-map'

type ViewMode = 'grid' | 'table'
type StatusFilter = 'all' | 'running' | 'stopped'

function latencyTone(ms: number): string {
  if (ms < 140) return 'bg-ok-shade text-ok'
  if (ms < 320) return 'bg-warn-shade text-warn'
  return 'bg-raised text-ink-dim'
}

export default function BrowsersWorkspacePage() {
  const { t, locale } = useI18n()
  const toast = useToast()
  const {
    isDesktop,
    workspaces,
    openProfileInWorkspace,
    closeProfileWorkspace,
    activateWorkspace,
    refreshProfileDirectory,
  } = useWorkspace()

  const [profiles, setProfiles] = useState<Profile[]>([])
  const [groups, setGroups] = useState<Group[]>([])
  const [running, setRunning] = useState<Set<string>>(new Set())
  const [bindings, setBindings] = useState<Record<string, Binding>>({})
  const [maskIps, setMaskIps] = useState(false)
  const [cloudStatus, setCloudStatus] = useState<CloudStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const [viewMode, setViewMode] = useState<ViewMode>('grid')
  const [search, setSearch] = useState('')
  const [groupFilter, setGroupFilter] = useState<string | null>(null)
  const [tagFilter, setTagFilter] = useState<string>('')
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all')

  const [busyIds, setBusyIds] = useState<Set<string>>(new Set())
  const [pushingIds, setPushingIds] = useState<Set<string>>(new Set())
  const [terminalIds, setTerminalIds] = useState<Set<string>>(new Set())
  const [cloudSyncing, setCloudSyncing] = useState(false)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [menuFor, setMenuFor] = useState<string | null>(null)

  // Modals & popovers
  const [createOpen, setCreateOpen] = useState(false)
  const [editing, setEditing] = useState<Profile | null>(null)
  const [auditFor, setAuditFor] = useState<Profile | null>(null)
  const [driftBlock, setDriftBlock] = useState<{ profile: Profile; drift: ExitIpDriftDetail } | null>(null)
  const [driftRetrying, setDriftRetrying] = useState(false)
  const [cookiesFor, setCookiesFor] = useState<Profile | null>(null)
  const [tagsFor, setTagsFor] = useState<Profile[] | null>(null)
  const [batch, setBatch] = useState<{ action: 'launch' | 'close'; targets: BatchTarget[] } | null>(null)
  const [cloneFor, setCloneFor] = useState<Profile | null>(null)
  const [cloneName, setCloneName] = useState('')
  const [groupManagerOpen, setGroupManagerOpen] = useState(false)
  const [newGroupName, setNewGroupName] = useState('')
  const [confirm, setConfirm] = useState<{ title: string; body: string; label: string; run: () => Promise<void> } | null>(null)

  const archiveInput = useRef<HTMLInputElement>(null)

  const loadProfiles = useCallback(async () => {
    try {
      setError(null)
      const collected: Profile[] = []
      for (let page = 1; page <= 200; page += 1) {
        const res = await profilesAPI.getProfiles({ page, per_page: 100 })
        collected.push(...res.profiles)
        if (!res.has_next) break
      }
      setProfiles(collected)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }, [])

  const loadLive = useCallback(async () => {
    try {
      const [activeRes, bindingList, netStatus] = await Promise.all([
        browsersAPI.active().catch(() => null),
        networkAPI.bindings().catch(() => null),
        networkAPI.status().catch(() => null),
      ])
      if (activeRes) setRunning(new Set(activeRes.active_browsers.map(b => b.profile_id)))
      if (bindingList) setBindings(Object.fromEntries(bindingList.map(b => [b.profile_id, b])))
      if (netStatus) setMaskIps(netStatus.settings.mask_exit_ips)
    } catch {
      // Best-effort live poll
    }
  }, [])

  const loadAux = useCallback(async () => {
    const [groupRes, cloudRes] = await Promise.all([
      groupsAPI.list().catch(() => null),
      cloudAPI.status().catch(() => null),
    ])
    if (groupRes) setGroups(groupRes.groups)
    if (cloudRes) setCloudStatus(cloudRes)
  }, [])

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    loadProfiles()
    loadLive()
    loadAux()
    const timer = setInterval(loadLive, 5000)
    const onRefresh = () => { void loadAux() }
    window.addEventListener('cpm-status-refresh', onRefresh)
    return () => {
      clearInterval(timer)
      window.removeEventListener('cpm-status-refresh', onRefresh)
    }
  }, [loadProfiles, loadLive, loadAux])

  useEffect(() => {
    const closeMenu = (event: MouseEvent) => {
      const target = event.target as Element | null
      if (target?.closest('[data-menu-trigger], [role="menu"]')) return
      setMenuFor(null)
    }
    document.addEventListener('click', closeMenu)
    return () => document.removeEventListener('click', closeMenu)
  }, [])

  const workspaceIds = useMemo(() => new Set(workspaces.map(ws => ws.profileId)), [workspaces])
  const activeRunning = useMemo(() => new Set([...running, ...workspaceIds]), [running, workspaceIds])

  const groupNames = useMemo(() => new Map(groups.map(g => [g.id, g.name])), [groups])

  const allTags = useMemo(() => {
    const set = new Set<string>()
    profiles.forEach(p => (p.tags ?? []).forEach(tag => set.add(tag)))
    return [...set].sort((a, b) => a.localeCompare(b))
  }, [profiles])

  const visible = useMemo(() => {
    const q = search.trim().toLowerCase()
    return profiles
      .filter(profile => {
        const isRunning = activeRunning.has(profile.id)
        if (statusFilter === 'running' && !isRunning) return false
        if (statusFilter === 'stopped' && isRunning) return false
        if (groupFilter === '__ungrouped__' && profile.group) return false
        if (groupFilter && groupFilter !== '__ungrouped__' && profile.group !== groupFilter) return false
        if (tagFilter && !(profile.tags ?? []).includes(tagFilter)) return false
        if (!q) return true
        const binding = bindings[profile.id]
        const hash = profileFingerprintHash(profile)
        return [
          profile.name,
          profile.notes,
          profile.id,
          hash,
          ...(profile.tags ?? []),
          binding?.node.name,
          binding?.node.check?.exit_ip,
          binding?.node.check?.city,
        ].some(v => v && v.toLowerCase().includes(q))
      })
      .sort((a, b) => {
        const aRun = activeRunning.has(a.id) ? 0 : 1
        const bRun = activeRunning.has(b.id) ? 0 : 1
        if (aRun !== bRun) return aRun - bRun
        return a.name.localeCompare(b.name)
      })
  }, [profiles, activeRunning, statusFilter, groupFilter, tagFilter, search, bindings])

  function markBusy(setter: typeof setBusyIds, id: string, on: boolean) {
    setter(prev => { const next = new Set(prev); if (on) next.add(id); else next.delete(id); return next })
  }

  async function toggleLaunch(profile: Profile) {
    const isRunning = activeRunning.has(profile.id)
    markBusy(setBusyIds, profile.id, true)
    try {
      if (isRunning) {
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
      await Promise.all([loadLive(), loadProfiles()])
    } catch (err) {
      const drift = readExitIpDrift(err)
      if (!isRunning && drift) {
        setDriftBlock({ profile, drift })
      } else {
        toast('error', isRunning ? t('Could not stop the browser') : t('Could not start the browser'), t(err instanceof Error ? err.message : String(err)))
      }
    } finally {
      markBusy(setBusyIds, profile.id, false)
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
      await Promise.all([loadLive(), loadProfiles()])
    } catch (err) {
      toast('error', t('Could not start the browser'), t(err instanceof Error ? err.message : String(err)))
    } finally {
      setDriftRetrying(false)
    }
  }

  async function openIsolatedTerminal(profile: Profile) {
    markBusy(setTerminalIds, profile.id, true)
    try {
      const res = await profilesAPI.openTerminal(profile.id)
      const ipDisplay = maskIp(res.exit_ip, maskIps)
      toast(
        'ok',
        t('Isolated terminal opened'),
        t('{name} · Exit {ip} · HOME {home}', { name: profile.name, ip: ipDisplay, home: res.isolated_home }),
      )
    } catch (err) {
      toast('error', t('Could not open isolated terminal'), t(err instanceof Error ? err.message : String(err)))
    } finally {
      markBusy(setTerminalIds, profile.id, false)
    }
  }

  async function reconnectNode(profile: Profile) {
    markBusy(setBusyIds, profile.id, true)
    try {
      await networkAPI.reconnect(profile.id)
      toast('ok', t('Reconnected'), t('{profile} is back on the same node.', { profile: profile.name }))
      await loadLive()
    } catch (err) {
      toast('error', t('Could not reconnect'), t(err instanceof Error ? err.message : String(err)))
    } finally {
      markBusy(setBusyIds, profile.id, false)
    }
  }

  async function pushToCloud(profile: Profile) {
    markBusy(setPushingIds, profile.id, true)
    try {
      const res = await cloudAPI.pushProfile(profile.id, false)
      toast('ok', t('Pushed to cloud'), t('{name} · rev {rev}', { name: profile.name, rev: res.revision }))
      await loadProfiles()
    } catch (err) {
      toast('error', t('Cloud push failed'), t(err instanceof Error ? err.message : String(err)))
    } finally {
      markBusy(setPushingIds, profile.id, false)
    }
  }

  async function handleQuickCloudSync() {
    setCloudSyncing(true)
    try {
      await cloudAPI.sync()
      await Promise.all([loadProfiles(), loadLive(), loadAux()])
      toast('ok', isCustomerEdition(cloudStatus) ? t('Exclusive nodes synced') : t('Cloud sync complete'))
    } catch (err) {
      toast('error', t('Cloud sync failed'), t(err instanceof Error ? err.message : String(err)))
    } finally {
      setCloudSyncing(false)
    }
  }

  async function handleClone() {
    if (!cloneFor || !cloneName.trim()) return
    try {
      await profilesAPI.cloneProfile(cloneFor.id, cloneName.trim())
      toast('ok', t('Browser cloned'), cloneName.trim())
      setCloneFor(null)
      await loadProfiles()
      void refreshProfileDirectory()
    } catch (err) {
      toast('error', t('Could not clone browser'), t(err instanceof Error ? err.message : String(err)))
    }
  }

  async function handleExportArchive(profile: Profile) {
    try {
      const blob = await profilesAPI.exportArchive(profile.id)
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = `${profile.name.replace(/[^\w.-]+/g, '_')}.zip`
      document.body.appendChild(link)
      link.click()
      link.remove()
      setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch (err) {
      toast('error', t('Export failed'), t(err instanceof Error ? err.message : String(err)))
    }
  }

  async function handleImportArchive(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!file) return
    try {
      const created = await profilesAPI.importArchive(file)
      toast('ok', t('Browser imported'), created.name)
      await loadProfiles()
      void refreshProfileDirectory()
    } catch (err) {
      toast('error', t('Import failed'), t(err instanceof Error ? err.message : String(err)))
    }
  }

  async function createGroupFromPopover(event: React.FormEvent) {
    event.preventDefault()
    if (!newGroupName.trim()) return
    try {
      const created = await groupsAPI.create({ name: newGroupName.trim() })
      setGroups(prev => [...prev, created])
      setNewGroupName('')
      toast('ok', t('Group created'), created.name)
    } catch (err) {
      toast('error', t('Could not create group'), t(err instanceof Error ? err.message : String(err)))
    }
  }

  async function deleteGroupFromPopover(group: Group) {
    try {
      await groupsAPI.remove(group.id)
      setGroups(prev => prev.filter(g => g.id !== group.id))
      if (groupFilter === group.id) setGroupFilter(null)
      toast('ok', t('Group deleted'), group.name)
      await loadProfiles()
    } catch (err) {
      toast('error', t('Could not delete group'), t(err instanceof Error ? err.message : String(err)))
    }
  }

  function toggleSelect(id: string) {
    setSelected(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const selectedProfiles = profiles.filter(p => selected.has(p.id))
  const ungroupedCount = profiles.filter(p => !p.group).length
  const runningCount = profiles.filter(p => activeRunning.has(p.id)).length
  const isCustomerMode = isCustomerEdition(cloudStatus)
  const customerActivated = isCustomerMode && Boolean(cloudStatus?.has_token) && isEntitlementActive(cloudStatus?.entitlement_status)
  const customerBlocked = isCustomerMode && Boolean(cloudStatus?.has_token) && isEntitlementBlocked(cloudStatus?.entitlement_status)
  const showCloudPushAction = !isCustomerMode || cloudStatus?.profiles_sync_allowed === true

  return (
    <div className="flex min-h-full flex-col bg-canvas">
      {/* Top Frosted Glass Workspace Header */}
      <header className="sticky top-0 z-20 border-b border-line bg-canvas/85 px-6 py-3.5 backdrop-blur-xl app-drag">
        <div className="flex flex-wrap items-center gap-2.5 app-no-drag">
          <div className="flex items-center gap-2">
            <h1 className="text-[16px] font-semibold tracking-tight text-ink">{t('Browsers')}</h1>
            <span className="rounded-full bg-surface px-2 py-0.5 font-mono text-[11px] font-medium text-ink-dim ring-1 ring-line">
              {profiles.length}
            </span>
            {runningCount > 0 && (
              <span className="inline-flex items-center gap-1.5 rounded-full bg-ok-shade px-2 py-0.5 font-mono text-[11px] font-medium text-ok ring-1 ring-ok/20">
                <span className="h-1.5 w-1.5 rounded-full bg-ok-dot signal-pulse" aria-hidden="true" />
                {t('{count} running', { count: runningCount })}
              </span>
            )}
          </div>

          <div className="relative ml-2 min-w-[200px] max-w-[300px] flex-1">
            <Search size={13} aria-hidden="true" className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-ink-faint" />
            <input
              aria-label={t('Search browsers, notes, IPs or fingerprints')}
              placeholder={t('Search browsers, notes, IPs…')}
              value={search}
              onChange={e => setSearch(e.target.value)}
              className="field h-[32px] bg-surface/90 pl-8 pr-7 text-[12.5px]"
            />
            {search && (
              <button
                type="button"
                onClick={() => setSearch('')}
                aria-label={t('Clear')}
                className="absolute right-2 top-1/2 -translate-y-1/2 cursor-pointer rounded p-0.5 text-ink-faint hover:text-ink"
              >
                <X size={12} aria-hidden="true" />
              </button>
            )}
          </div>

          {/* Status Filter Segmented Control */}
          <div className="inline-flex rounded-lg border border-line bg-surface/85 p-0.5 text-[12px]" role="group" aria-label={t('Filter by running state')}>
            {([
              { value: 'all', label: t('All') },
              { value: 'running', label: `${t('Running')} (${runningCount})` },
              { value: 'stopped', label: t('Stopped') },
            ] as { value: StatusFilter; label: string }[]).map(opt => (
              <button
                key={opt.value}
                type="button"
                onClick={() => setStatusFilter(opt.value)}
                className={`cursor-pointer rounded-md px-2.5 py-1 transition-all ${
                  statusFilter === opt.value
                    ? 'bg-raised font-medium text-ink shadow-[0_1px_2px_rgba(0,0,0,0.18)] ring-1 ring-line'
                    : 'text-ink-dim hover:text-ink'
                }`}
              >
                {opt.label}
              </button>
            ))}
          </div>

          {allTags.length > 0 && (
            <select
              aria-label={t('Filter by tag')}
              value={tagFilter}
              onChange={e => setTagFilter(e.target.value)}
              className="field h-[32px] w-auto max-w-[150px] bg-surface/90 py-0 text-[12px]"
            >
              <option value="">{t('All tags')}</option>
              {allTags.map(tag => <option key={tag} value={tag}>#{tag}</option>)}
            </select>
          )}

          <div className="ml-auto flex flex-wrap items-center gap-2">
            {/* Cloud Sync / License Quick Pill */}
            {isCustomerMode ? (
              customerActivated ? (
                <button
                  type="button"
                  onClick={handleQuickCloudSync}
                  disabled={cloudSyncing}
                  className="inline-flex h-[32px] cursor-pointer items-center gap-1.5 rounded-lg border border-ok/25 bg-ok-shade px-3 text-[12px] font-medium text-ok transition-colors hover:border-ok/40"
                  title={t('Sync your exclusive cloud proxy nodes')}
                >
                  {cloudSyncing ? <LoaderCircle size={12} className="animate-spin" aria-hidden="true" /> : <Cloud size={12} aria-hidden="true" />}
                  <span>{t('Sync exclusive nodes')}</span>
                </button>
              ) : customerBlocked ? (
                <Link
                  href="/cloud/"
                  className="inline-flex h-[32px] items-center gap-1.5 rounded-lg border border-danger/30 bg-danger-shade px-3 text-[12px] font-medium text-danger transition-colors hover:border-danger/50"
                >
                  <KeyRound size={12} aria-hidden="true" />
                  <span>{t('My license')}</span>
                </Link>
              ) : (
                <Link
                  href="/cloud/"
                  className="inline-flex h-[32px] items-center gap-1.5 rounded-lg border border-signal/35 bg-signal-shade px-3 text-[12px] font-medium text-signal transition-colors hover:border-signal/60"
                >
                  <KeyRound size={12} aria-hidden="true" />
                  <span>{t('Activate license')}</span>
                </Link>
              )
            ) : cloudStatus?.configured ? (
              <button
                type="button"
                onClick={handleQuickCloudSync}
                disabled={cloudSyncing}
                className="inline-flex h-[32px] cursor-pointer items-center gap-1.5 rounded-lg border border-ok/25 bg-ok-shade px-3 text-[12px] font-medium text-ok transition-colors hover:border-ok/40"
                title={t('Sync cloud isolation policy and subscriptions')}
              >
                {cloudSyncing ? <LoaderCircle size={12} className="animate-spin" aria-hidden="true" /> : <Cloud size={12} aria-hidden="true" />}
                <span>{t('Cloud sync')}</span>
              </button>
            ) : (
              <Link
                href="/cloud/"
                className="inline-flex h-[32px] items-center gap-1.5 rounded-lg border border-line bg-surface px-3 text-[12px] text-ink-dim transition-colors hover:border-line-strong hover:text-ink"
              >
                <Cloud size={12} aria-hidden="true" />
                <span>{t('Cloud center')}</span>
              </Link>
            )}

            {/* Grid / Table Toggle */}
            <div className="inline-flex rounded-lg border border-line bg-surface/85 p-0.5" role="group" aria-label={t('View mode')}>
              <button
                type="button"
                onClick={() => setViewMode('grid')}
                aria-pressed={viewMode === 'grid'}
                aria-label={t('Card grid view')}
                title={t('Card grid view')}
                className={`cursor-pointer rounded-md p-1.5 transition-all ${viewMode === 'grid' ? 'bg-raised text-ink shadow-xs ring-1 ring-line' : 'text-ink-faint hover:text-ink'}`}
              >
                <LayoutGrid size={13} />
              </button>
              <button
                type="button"
                onClick={() => setViewMode('table')}
                aria-pressed={viewMode === 'table'}
                aria-label={t('Compact table view')}
                title={t('Compact table view')}
                className={`cursor-pointer rounded-md p-1.5 transition-all ${viewMode === 'table' ? 'bg-raised text-ink shadow-xs ring-1 ring-line' : 'text-ink-faint hover:text-ink'}`}
              >
                <List size={13} />
              </button>
            </div>

            {/* Import Archive */}
            <input ref={archiveInput} type="file" accept=".zip" className="hidden" onChange={handleImportArchive} />
            <button
              type="button"
              onClick={() => archiveInput.current?.click()}
              className="btn btn-default h-[32px] px-2.5 text-[12.5px]"
              title={t('Import browser archive (.zip)')}
            >
              <Upload size={13} aria-hidden="true" />
              <span className="hidden sm:inline">{t('Import')}</span>
            </button>

            {/* Primary CTA: Create Browser on World Map */}
            <button type="button" className="btn btn-primary h-[32px] px-3.5" onClick={() => setCreateOpen(true)}>
              <Plus size={14} strokeWidth={2.2} aria-hidden="true" />
              {t('New browser')}
            </button>
          </div>
        </div>

        {/* Secondary Strip: Inline Group Filter Pills + Batch Action Bar */}
        <div className="mt-3 flex flex-wrap items-center gap-1.5 app-no-drag">
          <button
            type="button"
            onClick={() => setGroupFilter(null)}
            className={`cursor-pointer rounded-full px-3 py-1 text-[11.5px] transition-all ${
              groupFilter === null
                ? 'bg-signal font-medium text-white shadow-[0_2px_10px_-3px_rgba(124,123,255,0.55)]'
                : 'border border-line bg-surface/80 text-ink-dim hover:border-line-strong hover:text-ink'
            }`}
          >
            {t('All groups')} <span className="ml-1 font-mono opacity-80">{profiles.length}</span>
          </button>

          {groups.map(g => {
            const count = profiles.filter(p => p.group === g.id).length
            const active = groupFilter === g.id
            return (
              <button
                key={g.id}
                type="button"
                onClick={() => setGroupFilter(active ? null : g.id)}
                className={`cursor-pointer rounded-full px-3 py-1 text-[11.5px] transition-all ${
                  active
                    ? 'bg-signal font-medium text-white shadow-[0_2px_10px_-3px_rgba(124,123,255,0.55)]'
                    : 'border border-line bg-surface/80 text-ink-dim hover:border-line-strong hover:text-ink'
                }`}
              >
                {g.name} <span className="ml-1 font-mono opacity-80">{count}</span>
              </button>
            )
          })}

          {ungroupedCount > 0 && groups.length > 0 && (
            <button
              type="button"
              onClick={() => setGroupFilter(groupFilter === '__ungrouped__' ? null : '__ungrouped__')}
              className={`cursor-pointer rounded-full px-3 py-1 text-[11.5px] transition-all ${
                groupFilter === '__ungrouped__'
                  ? 'bg-signal font-medium text-white shadow-[0_2px_10px_-3px_rgba(124,123,255,0.55)]'
                  : 'border border-line bg-surface/80 text-ink-dim hover:border-line-strong hover:text-ink'
              }`}
            >
              {t('Ungrouped')} <span className="ml-1 font-mono opacity-80">{ungroupedCount}</span>
            </button>
          )}

          <button
            type="button"
            onClick={() => setGroupManagerOpen(true)}
            className="inline-flex cursor-pointer items-center gap-1 rounded-full border border-dashed border-line-strong px-2.5 py-1 text-[11.5px] text-ink-faint transition-colors hover:border-signal/60 hover:text-signal"
          >
            <FolderPlus size={12} aria-hidden="true" />
            {t('Manage groups')}
          </button>

          {/* Multi-select batch bar */}
          {selectedProfiles.length > 0 && (
            <div className="ml-auto flex flex-wrap items-center gap-1.5 rounded-full border border-signal/35 bg-signal-shade px-3 py-0.5 text-[12px]">
              <span className="font-medium text-signal">{t('{count} selected', { count: selectedProfiles.length })}</span>
              <button
                type="button"
                className="cursor-pointer rounded-md px-2 py-0.5 font-medium text-ink transition-colors hover:bg-surface"
                onClick={() => setBatch({ action: 'launch', targets: selectedProfiles.map(p => ({ id: p.id, name: p.name })) })}
              >
                {t('Launch')}
              </button>
              <button
                type="button"
                className="cursor-pointer rounded-md px-2 py-0.5 font-medium text-ink transition-colors hover:bg-surface"
                onClick={() => setBatch({ action: 'close', targets: selectedProfiles.map(p => ({ id: p.id, name: p.name })) })}
              >
                {t('Stop')}
              </button>
              <button
                type="button"
                className="cursor-pointer rounded-md px-2 py-0.5 font-medium text-ink transition-colors hover:bg-surface"
                onClick={() => setTagsFor(selectedProfiles)}
              >
                <Tags size={11} className="mr-1 inline" aria-hidden="true" />
                {t('Tags')}
              </button>
              <button type="button" className="cursor-pointer px-1 text-ink-dim hover:text-ink" onClick={() => setSelected(new Set())}>
                {t('Clear')}
              </button>
            </div>
          )}
        </div>
      </header>

      {/* Isolation Browser Kernel Banner (auto-hides once ready) */}
      <BrowserSetup firstProfile={profiles.length === 0} onCreate={() => setCreateOpen(true)} />

      {/* Workspace Body */}
      <div className="flex-1 px-6 py-5">
        {error ? (
          <EmptyState
            icon={<TriangleAlert size={18} />}
            title={t('Cannot load browsers')}
            body={t(error)}
            action={<button type="button" className="btn btn-default" onClick={loadProfiles}>{t('Retry')}</button>}
          />
        ) : loading ? (
          <div className="flex items-center justify-center gap-2 py-20 text-ink-dim" role="status">
            <LoaderCircle size={16} className="animate-spin text-signal" aria-hidden="true" />
            <span>{t('Loading…')}</span>
          </div>
        ) : profiles.length === 0 ? (
          <EmptyState
            icon={<Globe2 size={20} />}
            title={t('No isolated browsers yet')}
            body={t('Create your first isolated browser directly on the world node map with an automatically deduplicated real device fingerprint.')}
            action={
              <button type="button" className="btn btn-primary" onClick={() => setCreateOpen(true)}>
                <Plus size={14} aria-hidden="true" />
                {t('Create browser on map')}
              </button>
            }
          />
        ) : visible.length === 0 ? (
          <EmptyState
            icon={<Search size={18} />}
            title={t('No browsers match your filters')}
            body={t('Try clearing your search, group, tag, or status filter.')}
            action={
              <button
                type="button"
                className="btn btn-default"
                onClick={() => { setSearch(''); setGroupFilter(null); setTagFilter(''); setStatusFilter('all') }}
              >
                {t('Reset filters')}
              </button>
            }
          />
        ) : viewMode === 'grid' ? (
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            {visible.map(profile => {
              const isRunning = activeRunning.has(profile.id)
              const isInWorkspace = isDesktop && workspaceIds.has(profile.id)
              const isBusy = busyIds.has(profile.id)
              const isPushing = pushingIds.has(profile.id)
              const isOpeningTerminal = terminalIds.has(profile.id)
              const binding = bindings[profile.id]
              const canOpenTerminal = isRunning && Boolean(binding) && binding?.runtime?.state !== 'degraded'
              const degraded = isRunning && binding?.runtime?.state === 'degraded'
              const fpHash = profileFingerprintHash(profile)
              const osName = profile.isolation_summary?.os ?? profile.isolation?.os ?? profile.browser_settings?.os ?? 'macos'
              const gpu = shortGpuName(profile.isolation_summary?.gpu ?? profile.fingerprint?.gpu)
              const screen = profile.isolation_summary?.screen ?? profile.fingerprint?.screen ?? profile.browser_settings?.screen ?? '—'
              const policyVer = profile.isolation_summary?.policy_version ?? profile.isolation?.policy_version ?? 'builtin'
              const tzMode = profile.isolation_summary?.timezone_mode ?? profile.isolation?.timezone_mode ?? 'fixed'
              const tzShort = tzMode === 'fixed' ? (profile.isolation_summary?.timezone ?? profile.isolation?.timezone ?? 'America/Los_Angeles').split('/').pop()?.replace('_', ' ') : t('Exit TZ')
              const guardOn = profile.isolation_summary?.install_webrtc_guard ?? profile.isolation?.install_webrtc_guard ?? true
              const groupLabel = profile.group ? groupNames.get(profile.group) : null
              const isSelected = selected.has(profile.id)
              const latency = binding?.node.check?.latency_ms ?? null

              return (
                <article
                  key={profile.id}
                  className={`panel-interactive row-in group relative flex flex-col overflow-hidden p-4 ${
                    isSelected
                      ? 'border-signal/70 ring-2 ring-signal/20'
                      : degraded
                        ? 'border-danger/45'
                        : isRunning
                          ? 'border-ok/35 shadow-[0_1px_0_var(--prism-specular)_inset,0_12px_30px_-14px_rgba(16,185,129,0.28)]'
                          : ''
                  }`}
                >
                  {/* Subtle Top State Accent Hairline */}
                  {(isSelected || isRunning || degraded) && (
                    <div
                      aria-hidden="true"
                      className={`absolute inset-x-0 top-0 h-[2px] ${
                        isSelected
                          ? 'bg-gradient-to-r from-signal via-signal/75 to-transparent'
                          : degraded
                            ? 'bg-gradient-to-r from-danger-dot via-danger/60 to-transparent'
                            : 'bg-gradient-to-r from-ok-dot via-ok/55 to-transparent'
                      }`}
                    />
                  )}

                  {/* Card Top Header */}
                  <div className="flex items-start gap-2.5">
                    <div className="relative mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center">
                      <span
                        className={`h-2.5 w-2.5 rounded-full transition-opacity ${
                          isSelected ? 'opacity-0' : 'group-hover:opacity-0'
                        } ${degraded ? 'bg-danger-dot' : isRunning ? 'bg-ok-dot signal-pulse' : 'bg-slate-300'}`}
                        title={degraded ? t('Network degraded') : isRunning ? t('Running') : t('Stopped')}
                        aria-hidden="true"
                      />
                      <input
                        type="checkbox"
                        checked={isSelected}
                        onChange={() => toggleSelect(profile.id)}
                        aria-label={t('Select {name}', { name: profile.name })}
                        className={`absolute inset-0 m-auto h-3.5 w-3.5 cursor-pointer accent-signal transition-opacity ${
                          isSelected ? 'opacity-100' : 'opacity-0 group-hover:opacity-100 focus-visible:opacity-100'
                        }`}
                      />
                    </div>

                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-1.5">
                        <h2 className="truncate text-[14px] font-semibold tracking-tight text-ink" title={profile.name}>
                          {profile.name}
                        </h2>
                        {profile.cloud_synced_at && (
                          <span title={t('Synced with cloud')} className="shrink-0 text-ok">
                            <Cloud size={12} aria-hidden="true" />
                          </span>
                        )}
                      </div>
                      {profile.notes ? (
                        <p className="mt-0.5 line-clamp-1 text-[12px] text-ink-dim" title={profile.notes}>
                          {profile.notes}
                        </p>
                      ) : (
                        <p className="mt-0.5 text-[11px] text-ink-faint">
                          {t('Last used {time}', { time: formatLastUsed(profile.last_used, locale) })}
                        </p>
                      )}
                    </div>

                    {/* Overflow Menu Button */}
                    <div className="relative">
                      <button
                        type="button"
                        data-menu-trigger
                        onClick={() => setMenuFor(menuFor === profile.id ? null : profile.id)}
                        aria-label={t('More actions for {name}', { name: profile.name })}
                        className="btn btn-ghost -mr-1 h-7 w-7 p-0"
                      >
                        <Ellipsis size={15} />
                      </button>
                      {menuFor === profile.id && (
                        <div
                          role="menu"
                          className="dialog-in modal-sheet absolute right-0 top-8 z-30 w-[168px] rounded-xl p-1"
                        >
                          <button type="button" role="menuitem" onClick={() => { setMenuFor(null); setEditing(profile) }} className="flex w-full cursor-pointer items-center gap-2 rounded-lg px-2.5 py-1.5 text-left text-[12.5px] text-ink transition-colors hover:bg-raised">
                            <Pencil size={13} className="text-ink-faint" />{t('Edit details')}
                          </button>
                          <button type="button" role="menuitem" onClick={() => { setMenuFor(null); setCloneFor(profile); setCloneName(`${profile.name} Copy`) }} className="flex w-full cursor-pointer items-center gap-2 rounded-lg px-2.5 py-1.5 text-left text-[12.5px] text-ink transition-colors hover:bg-raised">
                            <Copy size={13} className="text-ink-faint" />{t('Clone browser')}
                          </button>
                          <button type="button" role="menuitem" onClick={() => { setMenuFor(null); void handleExportArchive(profile) }} className="flex w-full cursor-pointer items-center gap-2 rounded-lg px-2.5 py-1.5 text-left text-[12.5px] text-ink transition-colors hover:bg-raised">
                            <Download size={13} className="text-ink-faint" />{t('Export archive')}
                          </button>
                          <div className="my-1 border-t border-line" />
                          <button
                            type="button"
                            role="menuitem"
                            disabled={isRunning}
                            onClick={() => {
                              setMenuFor(null)
                              setConfirm({
                                title: t('Delete browser'),
                                body: t('Permanently delete "{name}" and its local browser data? This cannot be undone.', { name: profile.name }),
                                label: t('Delete'),
                                run: async () => {
                                  await profilesAPI.deleteProfile(profile.id)
                                  await getDesktopBridge()?.clearProfileStorage(profile.id).catch(() => {})
                                  toast('ok', t('Browser deleted'), profile.name)
                                  await loadProfiles()
                                  void refreshProfileDirectory()
                                },
                              })
                            }}
                            className="flex w-full cursor-pointer items-center gap-2 rounded-lg px-2.5 py-1.5 text-left text-[12.5px] text-danger transition-colors hover:bg-danger-shade disabled:opacity-40"
                          >
                            <Trash2 size={13} />{t('Delete')}
                          </button>
                        </div>
                      )}
                    </div>
                  </div>

                  {/* Group & Tags Row */}
                  {(groupLabel || (profile.tags && profile.tags.length > 0)) && (
                    <div className="mt-2.5 flex flex-wrap items-center gap-1.5">
                      {groupLabel && (
                        <span className="rounded-full bg-signal-shade px-2 py-0.5 text-[10.5px] font-medium text-signal">
                          {groupLabel}
                        </span>
                      )}
                      {(profile.tags ?? []).slice(0, 4).map(tag => (
                        <span key={tag} className="rounded-full border border-line bg-canvas/60 px-2 py-0.5 text-[10.5px] text-ink-dim">
                          #{tag}
                        </span>
                      ))}
                    </div>
                  )}

                  {/* Bound Exit Node Inset Module */}
                  <div className={`mt-3 rounded-xl border px-3 py-2.5 ${
                    degraded
                      ? 'border-danger/35 bg-danger-shade/60'
                      : binding
                        ? 'border-line bg-canvas/75'
                        : 'border-dashed border-line-strong bg-canvas/40'
                  }`}>
                    {binding ? (
                      <div className="flex items-center justify-between gap-2">
                        <Link
                          href={`/map/?profile=${encodeURIComponent(profile.id)}`}
                          className="group/node min-w-0 flex-1 rounded-md"
                          aria-label={t('Show {node} on map', { node: binding.node.name })}
                          title={t('Show {node} on map', { node: binding.node.name })}
                        >
                          <span className="flex items-center gap-1.5">
                            <span className="text-[14px] leading-none" aria-hidden="true">{flag(binding.node.check?.country_iso) || '🌐'}</span>
                            <span className="truncate text-[12.5px] font-medium text-signal underline-offset-2 group-hover/node:underline">
                              {binding.node.name}
                            </span>
                          </span>
                          <span className="mt-1 flex flex-wrap items-center gap-2 text-[11px] text-ink-dim">
                            <span className="font-mono text-ink">
                              {maskIp(binding.runtime?.exit_ip ?? binding.node.check?.exit_ip, maskIps)}
                            </span>
                            {binding.node.check?.city && <span className="truncate text-ink-faint">{binding.node.check.city}</span>}
                          </span>
                        </Link>
                        {degraded ? (
                          <button type="button" className="btn btn-danger h-[26px] px-2 text-[11px]" onClick={() => reconnectNode(profile)} disabled={isBusy}>
                            <RefreshCw size={11} aria-hidden="true" />
                            {t('Reconnect')}
                          </button>
                        ) : !binding.usable ? (
                          <span className="shrink-0 rounded-full bg-danger-shade px-2 py-0.5 text-[10.5px] font-medium text-danger">
                            {t('Retired')}
                          </span>
                        ) : latency != null ? (
                          <span className={`shrink-0 rounded-full px-2 py-0.5 font-mono text-[10.5px] font-medium ${latencyTone(latency)}`}>
                            {latency} ms
                          </span>
                        ) : null}
                      </div>
                    ) : (
                      <div className="flex items-center justify-between gap-2 text-[12px]">
                        <span className="text-ink-faint">{t('No proxy node bound')}</span>
                        <Link href={`/map/?profile=${encodeURIComponent(profile.id)}`} className="font-medium text-signal hover:underline">
                          {t('Bind on map')}
                        </Link>
                      </div>
                    )}
                  </div>

                  {/* Consolidated Hardware & Isolation Spec Box */}
                  <div
                    className="mt-2.5 space-y-1 rounded-xl border border-line/75 bg-canvas/45 px-2.5 py-2 text-[11px] text-ink-dim"
                    title={`${OS_LABELS[osName] ?? osName} · ${screen} · ${gpu} · ${policyVer} · ${tzShort} · ${guardOn ? t('Guard ON') : t('Guard OFF')}`}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <div className="flex min-w-0 items-center gap-1.5">
                        {fpHash && (
                          <span className="shrink-0 rounded bg-surface px-1.5 py-0.2 font-mono text-[10.5px] font-semibold text-ink ring-1 ring-line">
                            {fpHash}
                          </span>
                        )}
                        <span className="truncate font-mono text-[11px] text-ink">
                          {OS_LABELS[osName] ?? osName} · {screen}
                        </span>
                      </div>
                      <span
                        className={`inline-flex shrink-0 items-center gap-1 rounded-full px-1.5 py-0.2 text-[10px] font-medium ${
                          guardOn ? 'bg-ok-shade text-ok' : 'bg-warn-shade text-warn'
                        }`}
                      >
                        <span className={`h-1.5 w-1.5 rounded-full ${guardOn ? 'bg-ok-dot' : 'bg-warn-dot'}`} aria-hidden="true" />
                        {guardOn ? t('Guard ON') : t('Guard OFF')}
                      </span>
                    </div>
                    <p className="truncate font-mono text-[10.5px] text-ink-faint">
                      {`${gpu} · ${tzShort} · ${policyVer}`}
                    </p>
                  </div>

                  {/* Card Actions Footer */}
                  <div className="mt-3.5 flex items-center gap-1.5 border-t border-line pt-3">
                    {isInWorkspace ? (
                      <>
                        <button
                          type="button"
                          onClick={() => { void activateWorkspace(profile.id) }}
                          className="btn btn-primary h-[30px] min-w-fit flex-1 shrink-0 whitespace-nowrap px-2.5 text-[12px]"
                        >
                          <ArrowUpRight size={13} className="shrink-0" aria-hidden="true" />
                          {t('Enter window')}
                        </button>
                        <button
                          type="button"
                          disabled={isBusy}
                          onClick={() => toggleLaunch(profile)}
                          className="btn btn-danger h-[30px] w-[30px] shrink-0 p-0"
                          title={t('Stop {name}', { name: profile.name })}
                          aria-label={t('Stop {name}', { name: profile.name })}
                        >
                          {isBusy ? <LoaderCircle size={12} className="animate-spin" aria-hidden="true" /> : <Square size={11} aria-hidden="true" />}
                        </button>
                      </>
                    ) : (
                      <button
                        type="button"
                        disabled={isBusy}
                        onClick={() => toggleLaunch(profile)}
                        className={`btn h-[30px] min-w-fit flex-1 shrink-0 whitespace-nowrap px-2.5 text-[12px] ${isRunning ? 'btn-danger' : 'btn-primary'}`}
                      >
                        {isBusy ? (
                          <LoaderCircle size={13} className="animate-spin shrink-0" aria-hidden="true" />
                        ) : isRunning ? (
                          <Square size={11} className="shrink-0" aria-hidden="true" />
                        ) : (
                          <Play size={12} className="shrink-0" aria-hidden="true" />
                        )}
                        {isRunning ? t('Stop') : t('Launch')}
                      </button>
                    )}

                    <button
                      type="button"
                      className="btn btn-default h-[30px] shrink-0 whitespace-nowrap px-2 text-[11.5px]"
                      onClick={() => setAuditFor(profile)}
                      title={t('Isolation health audit')}
                    >
                      <ShieldCheck size={13} className="shrink-0 text-ok" aria-hidden="true" />
                      <span>{t('Audit')}</span>
                    </button>

                    {/* Unified Compact Icon Tool Dock */}
                    <div className="flex shrink-0 items-center gap-0.5 rounded-lg border border-line bg-canvas/65 p-0.5">
                      <button
                        type="button"
                        className={`btn btn-ghost h-[24px] w-[24px] rounded-md p-0 ${canOpenTerminal ? 'text-signal hover:bg-signal-shade' : ''}`}
                        disabled={!canOpenTerminal || isOpeningTerminal}
                        onClick={() => openIsolatedTerminal(profile)}
                        title={
                          canOpenTerminal
                            ? t('Open isolated terminal (shares this browser exit tunnel & isolated HOME)')
                            : t('Launch a node-bound browser first to open a same-tunnel isolated terminal')
                        }
                        aria-label={t('Isolated terminal')}
                      >
                        {isOpeningTerminal ? <LoaderCircle size={12} className="animate-spin" aria-hidden="true" /> : <Terminal size={12.5} aria-hidden="true" />}
                      </button>

                      <Link
                        href={`/map/?profile=${encodeURIComponent(profile.id)}`}
                        className="btn btn-ghost h-[24px] w-[24px] rounded-md p-0"
                        title={t('Switch node on map')}
                        aria-label={t('Switch node on map')}
                      >
                        <Globe2 size={12.5} aria-hidden="true" />
                      </Link>

                      <button
                        type="button"
                        className="btn btn-ghost h-[24px] w-[24px] rounded-md p-0"
                        onClick={() => setCookiesFor(profile)}
                        title={t('Import / export cookies')}
                        aria-label={t('Import / export cookies')}
                      >
                        <Cookie size={12.5} aria-hidden="true" />
                      </button>

                      {showCloudPushAction && (
                        <button
                          type="button"
                          className="btn btn-ghost h-[24px] w-[24px] rounded-md p-0"
                          disabled={isPushing || !cloudStatus?.configured}
                          onClick={() => pushToCloud(profile)}
                          title={cloudStatus?.configured ? t('Push browser config to cloud') : t('Configure cloud server in Cloud center first')}
                          aria-label={t('Push browser config to cloud')}
                        >
                          {isPushing ? <LoaderCircle size={12} className="animate-spin" aria-hidden="true" /> : <CloudUpload size={12.5} aria-hidden="true" />}
                        </button>
                      )}
                    </div>
                  </div>
                </article>
              )
            })}
          </div>
        ) : (
          /* Compact Table View */
          <div className="panel overflow-x-auto">
            <table className="w-full min-w-[960px] border-collapse text-left">
              <thead>
                <tr className="border-b border-line bg-sidebar/80 text-[11px] font-semibold uppercase tracking-[0.05em] text-ink-faint">
                  <th className="w-9 px-3 py-2.5" />
                  <th className="px-3 py-2.5">{t('Browser')}</th>
                  <th className="px-3 py-2.5">{t('Group & Tags')}</th>
                  <th className="px-3 py-2.5">{t('Exit node')}</th>
                  <th className="px-3 py-2.5">{t('Fingerprint identity')}</th>
                  <th className="px-4 py-2.5 text-right">{t('Actions')}</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {visible.map(profile => {
                  const isRunning = activeRunning.has(profile.id)
                  const isInWorkspace = isDesktop && workspaceIds.has(profile.id)
                  const isBusy = busyIds.has(profile.id)
                  const binding = bindings[profile.id]
                  const fpHash = profileFingerprintHash(profile)
                  const osName = profile.isolation_summary?.os ?? profile.isolation?.os ?? profile.browser_settings?.os ?? 'macos'
                  const screen = profile.isolation_summary?.screen ?? profile.fingerprint?.screen ?? '—'
                  const groupLabel = profile.group ? groupNames.get(profile.group) : null
                  return (
                    <tr key={profile.id} className="transition-colors hover:bg-raised/45">
                      <td className="px-3 py-2.5">
                        <input type="checkbox" checked={selected.has(profile.id)} onChange={() => toggleSelect(profile.id)} aria-label={t('Select {name}', { name: profile.name })} className="accent-signal" />
                      </td>
                      <td className="px-3 py-2.5">
                        <div className="flex items-center gap-2">
                          <span className={`h-2 w-2 shrink-0 rounded-full ${isRunning ? 'bg-ok-dot signal-pulse' : 'bg-slate-300'}`} />
                          <span className="font-medium text-ink">{profile.name}</span>
                        </div>
                        {profile.notes && <p className="mt-0.5 max-w-[240px] truncate pl-4 text-[11.5px] text-ink-dim">{profile.notes}</p>}
                      </td>
                      <td className="px-3 py-2.5">
                        <div className="flex flex-wrap gap-1">
                          {groupLabel && <span className="rounded-full bg-signal-shade px-2 py-0.5 text-[11px] font-medium text-signal">{groupLabel}</span>}
                          {(profile.tags ?? []).slice(0, 3).map(tag => <span key={tag} className="rounded-full border border-line bg-canvas/60 px-1.5 py-0.5 text-[10.5px] text-ink-dim">#{tag}</span>)}
                        </div>
                      </td>
                      <td className="px-3 py-2.5 text-[12px]">
                        {binding ? (
                          <Link
                            href={`/map/?profile=${encodeURIComponent(profile.id)}`}
                            className="group/node block rounded-md"
                            aria-label={t('Show {node} on map', { node: binding.node.name })}
                            title={t('Show {node} on map', { node: binding.node.name })}
                          >
                            <span className="block font-medium text-signal underline-offset-2 group-hover/node:underline">{flag(binding.node.check?.country_iso)} {binding.node.name}</span>
                            <span className="block font-mono text-[11px] text-ink-dim">
                              {maskIp(binding.runtime?.exit_ip ?? binding.node.check?.exit_ip, maskIps)}
                              {binding.node.check?.latency_ms != null ? ` · ${binding.node.check.latency_ms} ms` : ''}
                            </span>
                          </Link>
                        ) : (
                          <Link href={`/map/?profile=${encodeURIComponent(profile.id)}`} className="text-[12px] text-signal hover:underline">{t('Bind on map')}</Link>
                        )}
                      </td>
                      <td className="px-3 py-2.5 font-mono text-[11.5px] text-ink-dim">
                        <span className="font-semibold text-ink">{fpHash ?? '—'}</span> · {OS_LABELS[osName] ?? osName} · {screen}
                      </td>
                      <td className="px-4 py-2.5">
                        <div className="flex justify-end gap-1.5">
                          {isInWorkspace ? (
                            <>
                              <button
                                type="button"
                                onClick={() => { void activateWorkspace(profile.id) }}
                                className="btn btn-primary h-[28px] px-2.5 text-[12px]"
                              >
                                <ArrowUpRight size={12} aria-hidden="true" />
                                {t('Enter window')}
                              </button>
                              <button
                                type="button"
                                disabled={isBusy}
                                onClick={() => toggleLaunch(profile)}
                                className="btn btn-danger h-[28px] px-2 text-[12px]"
                              >
                                {isBusy ? <LoaderCircle size={12} className="animate-spin" /> : <Square size={10} />}
                                {t('Stop')}
                              </button>
                            </>
                          ) : (
                            <button type="button" disabled={isBusy} onClick={() => toggleLaunch(profile)} className={`btn h-[28px] px-2.5 text-[12px] ${isRunning ? 'btn-danger' : 'btn-primary'}`}>
                              {isBusy ? <LoaderCircle size={12} className="animate-spin" /> : isRunning ? <Square size={10} /> : <Play size={11} />}
                              {isRunning ? t('Stop') : t('Launch')}
                            </button>
                          )}
                          <button type="button" className="btn btn-default h-[28px] px-2 text-[12px]" onClick={() => setAuditFor(profile)}>
                            <ShieldCheck size={12} className="text-ok" />{t('Audit')}
                          </button>
                          <button
                            type="button"
                            className={`btn btn-ghost h-[28px] w-[28px] p-0 ${isRunning && Boolean(binding) && binding?.runtime?.state !== 'degraded' ? 'text-signal' : ''}`}
                            disabled={!(isRunning && Boolean(binding) && binding?.runtime?.state !== 'degraded') || terminalIds.has(profile.id)}
                            onClick={() => openIsolatedTerminal(profile)}
                            title={
                              isRunning && Boolean(binding) && binding?.runtime?.state !== 'degraded'
                                ? t('Open isolated terminal (shares this browser exit tunnel & isolated HOME)')
                                : t('Launch a node-bound browser first to open a same-tunnel isolated terminal')
                            }
                            aria-label={t('Isolated terminal')}
                          >
                            {terminalIds.has(profile.id) ? <LoaderCircle size={12} className="animate-spin" /> : <Terminal size={13} />}
                          </button>
                          <button type="button" className="btn btn-ghost h-[28px] w-[28px] p-0" onClick={() => setEditing(profile)} aria-label={t('Edit {name}', { name: profile.name })}>
                            <Pencil size={13} />
                          </button>
                          <button type="button" className="btn btn-ghost h-[28px] w-[28px] p-0" onClick={() => setCookiesFor(profile)} aria-label={t('Import / export cookies')}>
                            <Cookie size={13} />
                          </button>
                        </div>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Flagship World-Map Browser Creation Modal */}
      <CreateBrowserModal
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        groups={groups}
        onGroupCreated={created => setGroups(prev => [...prev, created])}
        onCreated={async (created, launched) => {
          toast('ok', launched ? t('Browser created and launched') : t('Browser created'), created.name)
          await Promise.all([loadProfiles(), loadLive()])
        }}
      />

      {/* Isolation Audit Modal */}
      <IsolationAuditModal
        profile={auditFor}
        onClose={() => setAuditFor(null)}
        onChanged={async () => { await Promise.all([loadProfiles(), loadLive()]) }}
      />

      {/* Exit IP Drift Guard Modal */}
      <ExitIpDriftModal
        profile={driftBlock?.profile ?? null}
        drift={driftBlock?.drift ?? null}
        maskIps={maskIps}
        busy={driftRetrying}
        onCancel={() => setDriftBlock(null)}
        onProceed={proceedAfterDrift}
      />

      {/* Edit Browser Modal */}
      {editing && (
        <EditBrowserModal
          profile={editing}
          groups={groups}
          onClose={() => setEditing(null)}
          onGroupCreated={created => setGroups(prev => [...prev, created])}
          onSaved={async () => { await loadProfiles(); void refreshProfileDirectory() }}
        />
      )}

      {/* Cookie Transfer Modal */}
      {cookiesFor && (
        <CookieTransfer
          profile={cookiesFor}
          onClose={() => setCookiesFor(null)}
          onChanged={loadProfiles}
        />
      )}

      {/* Bulk Tags Dialog */}
      {tagsFor && (
        <ProfileTagsDialog
          profiles={tagsFor}
          onClose={() => setTagsFor(null)}
          onUpdated={async () => { setTagsFor(null); setSelected(new Set()); await loadProfiles() }}
        />
      )}

      {/* Batch Launch / Stop Modal */}
      {batch && (
        <BatchProfileOperations
          action={batch.action}
          targets={batch.targets}
          onClose={() => setBatch(null)}
          onUpdated={async () => { await Promise.all([loadProfiles(), loadLive()]) }}
        />
      )}

      {/* Clone Modal */}
      <Modal
        open={!!cloneFor}
        title={t('Clone browser')}
        subtitle={cloneFor?.name}
        width={420}
        onClose={() => setCloneFor(null)}
        footer={
          <>
            <button type="button" className="btn btn-default" onClick={() => setCloneFor(null)}>{t('Cancel')}</button>
            <button type="button" className="btn btn-primary" disabled={!cloneName.trim()} onClick={handleClone}>{t('Clone')}</button>
          </>
        }
      >
        <label className="field-label" htmlFor="clone-browser-name">{t('New browser name')}</label>
        <input
          id="clone-browser-name"
          className="field"
          value={cloneName}
          onChange={e => setCloneName(e.target.value)}
          data-modal-autofocus
        />
        <p className="mt-2 text-[12px] text-ink-dim">
          {t('Cloning assigns a fresh deduplicated real device fingerprint so cloned browsers never collide.')}
        </p>
      </Modal>

      {/* Inline Group Manager Modal */}
      <Modal
        open={groupManagerOpen}
        title={t('Manage groups')}
        subtitle={t('Organize browsers by project, team, or region')}
        width={460}
        onClose={() => setGroupManagerOpen(false)}
        footer={<button type="button" className="btn btn-primary" onClick={() => setGroupManagerOpen(false)}>{t('Done')}</button>}
      >
        <form onSubmit={createGroupFromPopover} className="flex gap-2">
          <input
            className="field"
            value={newGroupName}
            onChange={e => setNewGroupName(e.target.value)}
            placeholder={t('New group name…')}
            maxLength={60}
            data-modal-autofocus
          />
          <button type="submit" className="btn btn-primary shrink-0" disabled={!newGroupName.trim()}>
            <Plus size={13} aria-hidden="true" />
            {t('Add')}
          </button>
        </form>
        <ul className="mt-4 divide-y divide-line rounded-xl border border-line bg-canvas/50">
          {groups.length === 0 ? (
            <li className="px-3.5 py-5 text-center text-[12px] text-ink-dim">{t('No groups created yet.')}</li>
          ) : groups.map(g => (
            <li key={g.id} className="flex items-center justify-between gap-3 px-3.5 py-2.5">
              <div className="min-w-0">
                <p className="truncate font-medium text-ink">{g.name}</p>
                <p className="text-[11px] text-ink-faint">{t('{count} browsers', { count: profiles.filter(p => p.group === g.id).length })}</p>
              </div>
              <button
                type="button"
                className="btn btn-ghost h-7 w-7 p-0 hover:text-danger"
                onClick={() => deleteGroupFromPopover(g)}
                aria-label={t('Delete {name}', { name: g.name })}
              >
                <Trash2 size={13} />
              </button>
            </li>
          ))}
        </ul>
      </Modal>

      {/* Confirm Destructive Action */}
      <ConfirmDialog
        open={!!confirm}
        title={confirm?.title ?? ''}
        body={confirm?.body ?? ''}
        confirmLabel={confirm?.label}
        destructive
        onCancel={() => setConfirm(null)}
        onConfirm={async () => {
          const action = confirm?.run
          setConfirm(null)
          if (action) await action()
        }}
      />
    </div>
  )
}
