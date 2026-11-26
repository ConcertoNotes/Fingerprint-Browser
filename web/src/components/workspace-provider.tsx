'use client'

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from 'react'

import { ApiRequestError, emitStatusRefresh, profilesAPI, type Profile } from '@/lib/api'
import {
  getDesktopBridge,
  type DesktopEngineInfo,
  type OpenProfileResult,
  type ProfileWorkspaceState,
} from '@/lib/desktop-bridge'

interface WorkspaceContextValue {
  isDesktop: boolean
  engineInfo: DesktopEngineInfo | null
  workspaces: ProfileWorkspaceState[]
  activeWorkspaceId: string | null
  activeWorkspace: ProfileWorkspaceState | null
  profileDirectory: Record<string, Profile>
  refreshProfileDirectory: () => Promise<void>
  openProfileInWorkspace: (
    profileId: string,
    opts?: { allowIpDrift?: boolean; initialUrl?: string },
  ) => Promise<OpenProfileResult>
  closeProfileWorkspace: (profileId: string) => Promise<void>
  activateWorkspace: (profileId: string | null) => Promise<void>
}

const WorkspaceContext = createContext<WorkspaceContextValue>({
  isDesktop: false,
  engineInfo: null,
  workspaces: [],
  activeWorkspaceId: null,
  activeWorkspace: null,
  profileDirectory: {},
  refreshProfileDirectory: async () => {},
  openProfileInWorkspace: async () => ({ ok: false, message: 'Workspace provider not mounted' }),
  closeProfileWorkspace: async () => {},
  activateWorkspace: async () => {},
})

const subscribeNoop = () => () => {}
const getIsDesktopClient = () => getDesktopBridge() !== null
const getIsDesktopServer = () => false

async function fetchProfileDirectory(): Promise<Record<string, Profile>> {
  const collected: Profile[] = []
  for (let page = 1; page <= 200; page += 1) {
    const res = await profilesAPI.getProfiles({ page, per_page: 100 })
    collected.push(...res.profiles)
    if (!res.has_next) break
  }
  return Object.fromEntries(collected.map(profile => [profile.id, profile]))
}

export function WorkspaceProvider({ children }: { children: React.ReactNode }) {
  const isDesktop = useSyncExternalStore(subscribeNoop, getIsDesktopClient, getIsDesktopServer)
  const [engineInfo, setEngineInfo] = useState<DesktopEngineInfo | null>(null)
  const [workspaces, setWorkspaces] = useState<ProfileWorkspaceState[]>([])
  const [activeWorkspaceId, setActiveWorkspaceId] = useState<string | null>(null)
  const [profileDirectory, setProfileDirectory] = useState<Record<string, Profile>>({})
  const activeIdRef = useRef<string | null>(null)

  useEffect(() => {
    activeIdRef.current = activeWorkspaceId
  }, [activeWorkspaceId])

  const refreshProfileDirectory = useCallback(async () => {
    try {
      setProfileDirectory(await fetchProfileDirectory())
    } catch {
      // Best-effort profile metadata cache for sidebar and embedded stage header.
    }
  }, [])

  useEffect(() => {
    const bridge = getDesktopBridge()
    if (!bridge) return
    let active = true
    void bridge.getEngineInfo().then(info => { if (active) setEngineInfo(info) }).catch(() => {})
    void bridge.listWorkspaces().then(list => { if (active) setWorkspaces(list) }).catch(() => {})
    void fetchProfileDirectory().then(map => { if (active) setProfileDirectory(map) }).catch(() => {})

    const unsubscribe = bridge.onWorkspacesChanged(nextList => {
      if (!active) return
      setWorkspaces(nextList)
      emitStatusRefresh()
      if (activeIdRef.current && !nextList.some(ws => ws.profileId === activeIdRef.current)) {
        setActiveWorkspaceId(null)
      }
    })
    return () => {
      active = false
      unsubscribe()
    }
  }, [])

  const activateWorkspace = useCallback(async (profileId: string | null) => {
    const bridge = getDesktopBridge()
    if (bridge) {
      await bridge.activateProfile(profileId)
    }
    setActiveWorkspaceId(profileId)
  }, [])

  const openProfileInWorkspace = useCallback(
    async (
      profileId: string,
      opts?: { allowIpDrift?: boolean; initialUrl?: string },
    ): Promise<OpenProfileResult> => {
      const bridge = getDesktopBridge()
      if (bridge) {
        const result = await bridge.openProfile(profileId, opts)
        if (result.ok) {
          setWorkspaces(prev => {
            const exists = prev.some(ws => ws.profileId === result.workspace.profileId)
            return exists
              ? prev.map(ws => (ws.profileId === result.workspace.profileId ? result.workspace : ws))
              : [...prev, result.workspace]
          })
          await bridge.activateProfile(profileId)
          setActiveWorkspaceId(profileId)
          emitStatusRefresh()
          void refreshProfileDirectory()
        }
        return result
      }

      // Non-desktop plain-browser fallback: call backend launch directly.
      try {
        await profilesAPI.launchProfile(profileId, { allow_ip_drift: opts?.allowIpDrift })
        emitStatusRefresh()
        return {
          ok: true,
          workspace: {
            profileId,
            activeTabId: null,
            exitIp: null,
            exitCountry: null,
            exitCity: null,
            proxyPort: null,
            tabs: [],
          },
        }
      } catch (err) {
        if (err instanceof ApiRequestError) {
          return {
            ok: false,
            status: err.status,
            code: err.code,
            message: err.message,
            details: err.details,
          }
        }
        return {
          ok: false,
          message: err instanceof Error ? err.message : String(err),
        }
      }
    },
    [refreshProfileDirectory],
  )

  const closeProfileWorkspace = useCallback(async (profileId: string) => {
    const bridge = getDesktopBridge()
    if (bridge) {
      await bridge.closeProfile(profileId)
      setWorkspaces(prev => prev.filter(ws => ws.profileId !== profileId))
      if (activeIdRef.current === profileId) {
        setActiveWorkspaceId(null)
      }
      emitStatusRefresh()
      return
    }
    await profilesAPI.closeProfile(profileId)
    emitStatusRefresh()
  }, [])

  const activeWorkspace = useMemo(
    () => (activeWorkspaceId ? workspaces.find(ws => ws.profileId === activeWorkspaceId) ?? null : null),
    [workspaces, activeWorkspaceId],
  )

  const value = useMemo<WorkspaceContextValue>(
    () => ({
      isDesktop,
      engineInfo,
      workspaces,
      activeWorkspaceId,
      activeWorkspace,
      profileDirectory,
      refreshProfileDirectory,
      openProfileInWorkspace,
      closeProfileWorkspace,
      activateWorkspace,
    }),
    [
      isDesktop,
      engineInfo,
      workspaces,
      activeWorkspaceId,
      activeWorkspace,
      profileDirectory,
      refreshProfileDirectory,
      openProfileInWorkspace,
      closeProfileWorkspace,
      activateWorkspace,
    ],
  )

  return <WorkspaceContext.Provider value={value}>{children}</WorkspaceContext.Provider>
}

export function useWorkspace() {
  return useContext(WorkspaceContext)
}
