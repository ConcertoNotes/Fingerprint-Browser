import { translate, type Locale, type Translator } from './i18n'

// API client for the Prism Browser (棱镜浏览器) backend.
//
// The UI is served by the API on the same origin, so the base URL is empty by
// default; NEXT_PUBLIC_API_URL overrides it for split deployments.

export const API_BASE_URL = process.env.NEXT_PUBLIC_API_URL ?? ''

const API_PREFIX = '/api/v1'

export type IsolationOs = 'macos' | 'windows' | 'linux'
export type IsolationTier = 'strict' | 'standard'
export type WebRtcPolicy = 'block' | 'spoof_exit'
export type CanvasPolicy = 'stable_hardware' | 'session_noise'
export type LocaleMode = 'fixed' | 'follow_exit'
export type TimezoneMode = 'fixed' | 'follow_exit'

export interface BrowserSettings {
  browser_ui?: 'desktop' | 'camoufox'
  os: string
  screen: string
  user_agent?: string | null
  languages?: string[]
  timezone?: string | null
  locale?: string | null
  window_width?: number | null
  window_height?: number | null
  hardware_concurrency?: number | null
  device_memory?: number | null
  max_touch_points?: number
  webrtc_mode?: string
  stable_canvas?: boolean
  canvas_noise?: boolean
  webgl_noise?: boolean
  audio_noise?: boolean
}

export interface ProfileIsolation {
  isolation_version?: number
  policy_version: string
  tier: IsolationTier
  os: IsolationOs
  preset_id?: string | null
  webrtc_policy: WebRtcPolicy
  canvas_policy: CanvasPolicy
  locale_mode: LocaleMode
  languages: string[]
  timezone_mode?: TimezoneMode
  timezone?: string | null
  install_webrtc_guard?: boolean
  window_size?: [number, number] | number[]
  pinned_fingerprint?: Record<string, unknown>
  fingerprint_hash: string
}

export interface IsolationCreateInput {
  os: IsolationOs
  tier?: IsolationTier
  preset_id?: string | null
  webrtc_policy?: WebRtcPolicy
  canvas_policy?: CanvasPolicy
  locale_mode?: LocaleMode
  languages?: string[]
  timezone_mode?: TimezoneMode
  timezone?: string | null
  install_webrtc_guard?: boolean
  window_size?: [number, number]
}

export interface IsolationSummary {
  fingerprint_hash: string
  os: string
  tier: IsolationTier
  policy_version: string
  policy_outdated?: boolean
  screen?: string | null
  gpu?: string | null
  hardware_concurrency?: number | null
  user_agent?: string | null
  browser_major?: number | null
  installed_major?: number | null
  browser_outdated?: boolean
  webrtc_policy?: WebRtcPolicy
  canvas_policy?: CanvasPolicy
  locale_mode?: LocaleMode
  languages?: string[]
  timezone_mode?: TimezoneMode
  timezone?: string | null
  install_webrtc_guard?: boolean
  preset_id?: string | null
}

export interface IsolationPreview {
  isolation: ProfileIsolation
  summary: IsolationSummary
  policy_source?: string
  policy_version?: string
  available_unused_presets?: number
  total_presets?: number
}

export interface IsolationAuditCheck {
  key: string
  title: string
  status: 'pass' | 'warn' | 'fail'
  summary: string
  detail?: string | null
  action?: string | null
}

export interface IsolationAuditReport {
  profile_id: string
  score: number
  status: 'healthy' | 'warning' | 'critical' | 'ok' | 'warn' | 'fail'
  checked_at: string
  checks: IsolationAuditCheck[]
}

export interface FingerprintSummary {
  user_agent?: string | null
  platform?: string | null
  hardware_concurrency?: number | null
  screen?: string | null
  gpu?: string | null
  font_count?: number | null
  property_count?: number
  browser_major?: number | null
  installed_major?: number | null
  browser_outdated?: boolean
  pinned_os?: string | null
  settings_os?: string | null
  os_mismatch?: boolean
}

export interface ProxyConfig {
  type?: string
  server?: string
  username?: string | null
  password?: string | null
  country?: string | null
}

export interface Profile {
  id: string
  name: string
  group?: string | null
  tags?: string[]
  status: string
  browser_settings: BrowserSettings
  proxy_config?: ProxyConfig | null
  storage_path?: string | null
  notes?: string | null
  created_at: string
  updated_at?: string
  last_used?: string | null
  fingerprint?: FingerprintSummary | null
  isolation?: ProfileIsolation | null
  isolation_summary?: IsolationSummary | null
  fingerprint_hash?: string | null
  ip_drift_guard?: boolean
  last_exit_ip?: string | null
  last_exit_country?: string | null
  last_exit_at?: string | null
  cloud_revision?: number
  cloud_synced_at?: string | null
  row_version: number
}

export interface ExitIpDriftDetail {
  previous_ip: string | null
  previous_country: string | null
  current_ip: string | null
  current_country: string | null
  country_changed: boolean
}

export interface IsolatedTerminalResult {
  profile_id: string
  isolated_home: string
  timezone: string
  exit_ip: string | null
  exit_country: string | null
  proxy_port: number
  launched: boolean
  platform: string
}

export interface RiskDomainProbeItem {
  domain: string
  category: string
  reachable: boolean
  status_code: number | null
  latency_ms: number | null
  error: string | null
}

export interface RiskDomainProbeReport {
  node_id: string
  exit_ip: string | null
  checked_at: string
  total_enforced_domains: number
  total_enforced_cidrs: number
  risk_probe_groups: Record<string, string[]>
  risk_probe_cidrs: string[]
  probes: RiskDomainProbeItem[]
  summary: { ok: number; total: number }
}

export interface ProfilesResponse {
  profiles: Profile[]
  total: number
  page: number
  per_page: number
  has_next: boolean
  has_prev: boolean
}

export interface Group {
  id: string
  name: string
  description?: string | null
  created_at: string
  profile_count: number
}

export type ClientEdition = 'standard' | 'customer'

export type EntitlementStatus =
  | 'none'
  | 'active'
  | 'expiring_soon'
  | 'expired'
  | 'disabled'
  | 'revoked'
  | 'device_limit'
  | 'offline_grace_exceeded'
  | string

export interface SystemStatus {
  total_profiles: number
  active_profiles?: number
  running_browsers: number
  total_groups: number
  cpu_percent: number
  memory_used_mb: number
  memory_total_mb: number
  memory_percent: number
  mihomo_available: boolean
  mihomo_version?: string | null
  cloud_policy_source?: string | null
  cloud_policy_version?: string | null
  browser_installed?: boolean
  uptime_seconds: number
  client_edition?: ClientEdition
  brand_name?: string
  require_activation?: boolean
  entitlement_status?: EntitlementStatus
  entitlement_tenant_name?: string | null
  entitlement_expires_at?: string | null
  entitlement_days_remaining?: number | null
  // Fallback legacy fields
  memory_usage?: number
  disk_usage?: number
}

export interface SystemConfig {
  version: string
  host: string
  port: number
  database_path: string
  api_key_set: boolean
  encryption_enabled: boolean
  cors_origins: string[]
  camoufox_available: boolean
  uptime_seconds: number
  host_os?: IsolationOs
}

export interface BrowserReadiness {
  installed: boolean
  bundled?: boolean
  version: string | null
  state: 'idle' | 'downloading' | 'initializing' | 'ready' | 'error'
  progress: number | null
  message: string
  error: string | null
}

export interface DevicePreset {
  id: string
  os: string
  screen?: string | null
  hardware_concurrency?: number | null
  gpu?: string | null
  vendor?: string | null
  user_agent?: string | null
  fingerprint_hash?: string | null
  in_use?: boolean
}

export interface BulkTagsResult {
  updated: Profile[]
  skipped: { id: string; reason: string }[]
}

export class ApiRequestError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code?: string,
    readonly details?: unknown,
  ) {
    super(message)
    this.name = 'ApiRequestError'
  }
}

export function isStaleWrite(error: unknown): boolean {
  return error instanceof ApiRequestError && error.status === 409 && error.code === 'stale_write'
}

export function readExitIpDrift(error: unknown): ExitIpDriftDetail | null {
  if (!error || typeof error !== 'object') {
    return null
  }
  const err = error as { status?: unknown; code?: unknown; details?: unknown }
  if (err.status !== 409 || err.code !== 'exit_ip_drift') {
    return null
  }
  const raw = Array.isArray(err.details) ? err.details[0] : err.details
  if (!raw || typeof raw !== 'object') {
    return {
      previous_ip: null,
      previous_country: null,
      current_ip: null,
      current_country: null,
      country_changed: false,
    }
  }
  const record = raw as Record<string, unknown>
  return {
    previous_ip: typeof record.previous_ip === 'string' ? record.previous_ip : null,
    previous_country: typeof record.previous_country === 'string' ? record.previous_country : null,
    current_ip: typeof record.current_ip === 'string' ? record.current_ip : null,
    current_country: typeof record.current_country === 'string' ? record.current_country : null,
    country_changed: Boolean(record.country_changed),
  }
}

const API_KEY_STORAGE = 'camoufox-pm.api-key'

export function getApiKey(): string {
  if (typeof window === 'undefined') return ''
  try {
    return window.localStorage.getItem(API_KEY_STORAGE) ?? ''
  } catch {
    return ''
  }
}

export function setApiKey(key: string): void {
  if (typeof window === 'undefined') return
  try {
    if (key) window.localStorage.setItem(API_KEY_STORAGE, key)
    else window.localStorage.removeItem(API_KEY_STORAGE)
  } catch {
    // Embedded storage may be disabled.
  }
}

export function authHeaders(): Record<string, string> {
  const key = getApiKey()
  return key ? { 'X-API-Key': key } : {}
}

export function notifyAccessDenied(status: number): void {
  if ((status === 401 || status === 403) && typeof window !== 'undefined') {
    window.dispatchEvent(new Event('camoufox-access-denied'))
  }
}

/** Notify the bottom status bar and sidebar badges to refresh immediately. */
export function emitStatusRefresh(): void {
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new Event('cpm-status-refresh'))
  }
}

export function unwrapEnvelope<T>(body: unknown): T {
  if (
    body !== null &&
    typeof body === 'object' &&
    'data' in body &&
    ('success' in body || Object.keys(body as Record<string, unknown>).length <= 3)
  ) {
    const wrapped = (body as { data: T | undefined }).data
    if (wrapped !== undefined) return wrapped
  }
  return body as T
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...options,
    headers: {
      ...(typeof FormData !== 'undefined' && options?.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }),
      ...authHeaders(),
      ...(options?.headers ?? {}),
    },
  })
  if (!response.ok) {
    notifyAccessDenied(response.status)
    let detail = response.statusText
    let code: string | undefined
    let details: unknown
    try {
      const body = await response.json()
      const raw = body.error?.message ?? body.detail ?? body.message ?? detail
      detail = typeof raw === 'string' ? raw : JSON.stringify(raw)
      code = body.error?.code ?? body.code
      details = body.error?.details ?? body.details
    } catch {
      // response had no JSON body
    }
    throw new ApiRequestError(detail, response.status, code, details)
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

export const apiRequest = request

function toQuery(params: Record<string, unknown>): string {
  const query = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (Array.isArray(value)) value.forEach(item => query.append(key, String(item)))
    else if (value !== undefined && value !== null && value !== '') query.set(key, String(value))
  }
  return query.toString() ? `?${query.toString()}` : ''
}

export const profilesAPI = {
  async bulkTags(profiles: Profile[], add: string[], remove: string[]): Promise<BulkTagsResult> {
    const body = await request<unknown>(`${API_PREFIX}/profiles/tags`, {
      method: 'POST',
      body: JSON.stringify({ profiles: profiles.map(({ id, row_version }) => ({ id, row_version })), add, remove }),
    })
    return unwrapEnvelope<BulkTagsResult>(body)
  },

  async getProfiles(params: Record<string, unknown> = {}): Promise<ProfilesResponse> {
    const body = await request<unknown>(`${API_PREFIX}/profiles${toQuery(params)}`)
    return unwrapEnvelope<ProfilesResponse>(body)
  },

  async getProfile(id: string): Promise<Profile> {
    const body = await request<unknown>(`${API_PREFIX}/profiles/${encodeURIComponent(id)}`)
    return unwrapEnvelope<Profile>(body)
  },

  async createProfile(data: {
    name: string
    group?: string | null
    tags?: string[]
    notes?: string | null
    node_id?: string | null
    ip_drift_guard?: boolean
    isolation?: IsolationCreateInput
  }): Promise<Profile> {
    const body = await request<unknown>(`${API_PREFIX}/profiles`, {
      method: 'POST',
      body: JSON.stringify(data),
    })
    emitStatusRefresh()
    return unwrapEnvelope<Profile>(body)
  },

  async updateProfile(id: string, data: Record<string, unknown>): Promise<Profile> {
    const body = await request<unknown>(`${API_PREFIX}/profiles/${encodeURIComponent(id)}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    })
    return unwrapEnvelope<Profile>(body)
  },

  async deleteProfile(id: string): Promise<void> {
    await request<void>(`${API_PREFIX}/profiles/${encodeURIComponent(id)}`, { method: 'DELETE' })
    emitStatusRefresh()
  },

  async cloneProfile(id: string, newName: string): Promise<Profile> {
    const body = await request<unknown>(`${API_PREFIX}/profiles/${encodeURIComponent(id)}/clone`, {
      method: 'POST',
      body: JSON.stringify({ new_name: newName }),
    })
    emitStatusRefresh()
    return unwrapEnvelope<Profile>(body)
  },

  async launchProfile(id: string, options: { allow_ip_drift?: boolean; headless?: boolean } = {}): Promise<unknown> {
    const res = await request<unknown>(`${API_PREFIX}/profiles/${encodeURIComponent(id)}/launch`, {
      method: 'POST',
      body: JSON.stringify({ headless: options.headless ?? false, allow_ip_drift: options.allow_ip_drift ?? false }),
    })
    emitStatusRefresh()
    return res
  },

  async startProfile(id: string, options?: { allow_ip_drift?: boolean }): Promise<unknown> {
    return profilesAPI.launchProfile(id, options)
  },

  async openTerminal(id: string): Promise<IsolatedTerminalResult> {
    const body = await request<unknown>(`${API_PREFIX}/profiles/${encodeURIComponent(id)}/terminal`, {
      method: 'POST',
    })
    return unwrapEnvelope<IsolatedTerminalResult>(body)
  },

  async probeRiskDomains(id: string): Promise<RiskDomainProbeReport> {
    const body = await request<unknown>(`${API_PREFIX}/profiles/${encodeURIComponent(id)}/probe-risk-domains`, {
      method: 'POST',
    })
    return unwrapEnvelope<RiskDomainProbeReport>(body)
  },

  async closeProfile(id: string): Promise<unknown> {
    const res = await request<unknown>(`${API_PREFIX}/profiles/${encodeURIComponent(id)}/close`, { method: 'POST' })
    emitStatusRefresh()
    return res
  },

  async isolationPreview(params: {
    os?: IsolationOs
    tier?: IsolationTier
    preset_id?: string | null
    exclude_preset_id?: string | null
  } = {}): Promise<IsolationPreview> {
    const body = await request<unknown>(`${API_PREFIX}/profiles/isolation/preview${toQuery(params)}`)
    return unwrapEnvelope<IsolationPreview>(body)
  },

  async fingerprintPresets(os?: IsolationOs): Promise<DevicePreset[]> {
    const body = await request<unknown>(`${API_PREFIX}/profiles/fingerprint-presets${toQuery({ os })}`)
    const data = unwrapEnvelope<{ presets?: DevicePreset[] } | DevicePreset[]>(body)
    return Array.isArray(data) ? data : (data.presets ?? [])
  },

  async auditProfile(id: string): Promise<IsolationAuditReport> {
    const body = await request<unknown>(`${API_PREFIX}/profiles/${encodeURIComponent(id)}/audit`, {
      method: 'POST',
    })
    return unwrapEnvelope<IsolationAuditReport>(body)
  },

  async reoptimizeProfile(id: string): Promise<Profile> {
    const body = await request<unknown>(`${API_PREFIX}/profiles/${encodeURIComponent(id)}/reoptimize`, {
      method: 'POST',
    })
    emitStatusRefresh()
    return unwrapEnvelope<Profile>(body)
  },

  async refreshBrowserVersion(id: string): Promise<Profile> {
    const body = await request<unknown>(`${API_PREFIX}/profiles/${encodeURIComponent(id)}/refresh-browser`, {
      method: 'POST',
    })
    return unwrapEnvelope<Profile>(body)
  },

  async reconcileOs(id: string, keepMachine = false): Promise<Profile> {
    const body = await request<unknown>(`${API_PREFIX}/profiles/${encodeURIComponent(id)}/reconcile-os`, {
      method: 'POST',
      body: JSON.stringify({ keep_machine: keepMachine }),
    })
    return unwrapEnvelope<Profile>(body)
  },

  async resetFingerprint(id: string): Promise<Profile> {
    const body = await request<unknown>(`${API_PREFIX}/profiles/${encodeURIComponent(id)}/reset-fingerprint`, {
      method: 'POST',
    })
    return unwrapEnvelope<Profile>(body)
  },

  async clearGeography(ids: string[]): Promise<{ cleared: string[]; unchanged: string[]; not_found: string[] }> {
    const body = await request<unknown>(`${API_PREFIX}/profiles/clear-geography`, {
      method: 'POST',
      body: JSON.stringify({ profile_ids: ids }),
    })
    return unwrapEnvelope(body)
  },

  async exportArchive(id: string): Promise<Blob> {
    const response = await fetch(`${API_BASE_URL}${API_PREFIX}/profiles/${encodeURIComponent(id)}/export`, {
      headers: authHeaders(),
    })
    if (!response.ok) {
      notifyAccessDenied(response.status)
      const body = await response.json().catch(() => null)
      throw new Error(body?.detail ?? body?.error?.message ?? response.statusText)
    }
    return response.blob()
  },

  async importArchive(file: File): Promise<Profile> {
    const body = new FormData()
    body.append('file', file)
    const response = await fetch(`${API_BASE_URL}${API_PREFIX}/profiles/import`, {
      method: 'POST',
      headers: authHeaders(),
      body,
    })
    if (!response.ok) {
      notifyAccessDenied(response.status)
      const detail = await response.json().catch(() => null)
      throw new Error(detail?.detail ?? detail?.error?.message ?? response.statusText)
    }
    emitStatusRefresh()
    return unwrapEnvelope<Profile>(await response.json())
  },
}

export const browsersAPI = {
  async active(): Promise<{ active_browsers: { profile_id: string }[]; count: number }> {
    const body = await request<unknown>(`${API_PREFIX}/browsers/active`)
    return unwrapEnvelope(body)
  },

  async closeAll(): Promise<{ closed_count: number; message: string }> {
    const body = await request<unknown>(`${API_PREFIX}/browsers/close-all`, { method: 'POST' })
    emitStatusRefresh()
    return unwrapEnvelope(body)
  },
}

export const groupsAPI = {
  async list(): Promise<{ groups: Group[]; total: number }> {
    const body = await request<unknown>(`${API_PREFIX}/groups`)
    return unwrapEnvelope(body)
  },

  async create(data: { name: string; description?: string }): Promise<Group> {
    const body = await request<unknown>(`${API_PREFIX}/groups`, { method: 'POST', body: JSON.stringify(data) })
    emitStatusRefresh()
    return unwrapEnvelope<Group>(body)
  },

  async update(id: string, data: { name?: string; description?: string }): Promise<Group> {
    const body = await request<unknown>(`${API_PREFIX}/groups/${encodeURIComponent(id)}`, { method: 'PUT', body: JSON.stringify(data) })
    return unwrapEnvelope<Group>(body)
  },

  async remove(id: string): Promise<void> {
    await request<void>(`${API_PREFIX}/groups/${encodeURIComponent(id)}`, { method: 'DELETE' })
    emitStatusRefresh()
  },
}

export const systemAPI = {
  async browser(): Promise<BrowserReadiness> {
    const body = await request<unknown>(`${API_PREFIX}/system/browser`)
    return unwrapEnvelope<BrowserReadiness>(body)
  },

  async installBrowser(): Promise<BrowserReadiness> {
    const body = await request<unknown>(`${API_PREFIX}/system/browser/install`, { method: 'POST' })
    return unwrapEnvelope<BrowserReadiness>(body)
  },

  async status(): Promise<SystemStatus> {
    const body = await request<unknown>(`${API_PREFIX}/system/status`)
    return unwrapEnvelope<SystemStatus>(body)
  },

  async config(): Promise<SystemConfig> {
    const body = await request<unknown>(`${API_PREFIX}/system/config`)
    return unwrapEnvelope<SystemConfig>(body)
  },
}

export function guessHostOs(): IsolationOs {
  const platform = typeof navigator === 'undefined' ? '' : `${navigator.platform} ${navigator.userAgent}`
  if (/Mac/i.test(platform)) return 'macos'
  if (/Win/i.test(platform)) return 'windows'
  return 'linux'
}

export function formatLastUsed(value?: string | null, locale: Locale = 'zh-CN'): string {
  const t: Translator = (message, values) => translate(locale, message, values)
  if (!value) return t('Never')
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return t('Never')

  const seconds = Math.round((Date.now() - date.getTime()) / 1000)
  if (seconds < 60) return t('Just now')
  if (seconds < 3600) return t('{count}m ago', { count: Math.floor(seconds / 60) })
  if (seconds < 86400) return t('{count}h ago', { count: Math.floor(seconds / 3600) })
  if (seconds < 604800) return t('{count}d ago', { count: Math.floor(seconds / 86400) })
  return date.toLocaleDateString(locale)
}

export function shortGpuName(gpu?: string | null): string {
  if (!gpu) return '—'
  const angleMatch = gpu.match(/ANGLE\s*\([^,]+,\s*([^,(]+)/i)
  if (angleMatch?.[1]) return angleMatch[1].trim()
  return gpu.replace(/\s*\([^)]*\)/g, '').trim().slice(0, 34) || gpu.slice(0, 34)
}

export function profileFingerprintHash(profile: Profile): string | null {
  return profile.isolation_summary?.fingerprint_hash || profile.isolation?.fingerprint_hash || profile.fingerprint_hash || null
}

export const OS_LABELS: Record<string, string> = {
  windows: 'Windows',
  macos: 'macOS',
  linux: 'Linux',
}
