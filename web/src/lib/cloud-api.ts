import {
  apiRequest,
  ApiRequestError,
  emitStatusRefresh,
  unwrapEnvelope,
  type ClientEdition,
  type EntitlementStatus,
} from './api'

const BASE = '/api/v1/cloud'

export interface CloudConfigInput {
  base_url?: string | null
  token?: string | null
  clear_token?: boolean
  auto_sync_on_startup?: boolean
  sync_profiles_enabled?: boolean
  device_label?: string | null
  allow_local?: boolean
}

export interface CloudStatus {
  configured: boolean
  connected?: boolean
  base_url: string | null
  has_token: boolean
  auto_sync_on_startup: boolean
  sync_profiles_enabled: boolean
  device_label: string | null
  allow_local?: boolean
  policy_source: 'cloud' | 'builtin' | string
  policy_version: string
  policy_fetched_at?: string | null
  cloud_subscription_count: number
  last_synced_at?: string | null
  last_error?: string | null
  edition?: ClientEdition
  brand_name?: string
  cloud_url_locked?: boolean
  require_activation?: boolean
  tenant_id?: string | null
  tenant_name?: string | null
  entitlement_status?: EntitlementStatus
  expires_at?: string | null
  days_remaining?: number | null
  max_devices?: number | null
  active_devices?: number | null
  offline_grace_hours?: number | null
  profiles_sync_allowed?: boolean
  device_id?: string | null
}

export interface CloudActivateResult {
  status: CloudStatus
  report: CloudSyncReport
}

export interface CloudPolicyData {
  source: 'cloud' | 'builtin' | string
  fetched_at?: string | null
  sha256?: string | null
  policy: {
    schema_version?: number
    policy_version: string
    default_tier?: string
    default_webrtc_policy?: string
    default_canvas_policy?: string
    default_locale_mode?: string
    default_languages?: string[]
    default_timezone_mode?: string
    default_timezone?: string
    install_webrtc_guard?: boolean
    min_chrome_major?: number | null
    recommended_chrome_major?: number | null
    recommended_camoufox_major?: number | null
    hardening_prefs?: Record<string, unknown>
    preset_filters?: Record<string, unknown>
    curated_presets?: unknown[]
    risk_probe_groups?: Record<string, string[]>
    risk_probe_cidrs?: string[]
    risk_probe_targets?: { domain: string; category: string }[]
  }
}

export interface CloudSyncReport {
  synced_at?: string
  ok?: boolean
  policy?: {
    updated?: boolean
    version?: string
    error?: string | null
  } | null
  subscriptions?: {
    created?: number
    updated?: number
    retired?: number
    unchanged?: number
    total?: number
    error?: string | null
  } | null
  profiles?: {
    created?: number
    updated?: number
    skipped_running?: number
    rebound?: number
    error?: string | null
  } | null
  errors?: string[]
}

export interface CloudProfilePushResult {
  profile_id: string
  revision: number
  synced_at: string
  included_cookies?: boolean
}

export interface CloudProfilesPullResult {
  created: number
  updated: number
  skipped_running: number
  rebound: number
  errors?: string[]
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const body = await apiRequest<unknown>(BASE + path, init)
  return unwrapEnvelope<T>(body)
}

function json(method: string, body?: unknown): RequestInit {
  return { method, body: body === undefined ? undefined : JSON.stringify(body) }
}

export const BLOCKED_ENTITLEMENT_STATUSES = new Set([
  'expired',
  'disabled',
  'revoked',
  'device_limit',
  'offline_grace_exceeded',
])

export function isCustomerEdition(
  status?: { edition?: string; client_edition?: string; cloud_url_locked?: boolean } | null,
): boolean {
  return status?.edition === 'customer' || status?.client_edition === 'customer' || Boolean(status?.cloud_url_locked)
}

export function isEntitlementActive(entitlementStatus?: string | null): boolean {
  return entitlementStatus === 'active' || entitlementStatus === 'expiring_soon'
}

export function isEntitlementBlocked(entitlementStatus?: string | null): boolean {
  return Boolean(entitlementStatus && BLOCKED_ENTITLEMENT_STATUSES.has(entitlementStatus))
}

export function entitlementDotClass(entitlementStatus?: string | null): string {
  if (entitlementStatus === 'active') return 'bg-ok-dot'
  if (entitlementStatus === 'expiring_soon') return 'bg-warn-dot'
  if (isEntitlementBlocked(entitlementStatus)) return 'bg-danger-dot'
  return 'bg-slate-300'
}

export function formatLicenseDate(value: string | null | undefined, locale: string): string | null {
  if (!value) return null
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return null
  return date.toLocaleDateString(locale)
}

export function formatCloudAuthError(error: unknown): string {
  if (error instanceof ApiRequestError) {
    switch (error.code) {
      case 'invalid_token':
      case 'unauthorized':
        return 'Invalid activation code. Please check with your administrator.'
      case 'tenant_expired':
        return 'This activation code has expired. Please contact your administrator to renew.'
      case 'tenant_disabled':
        return 'This license has been disabled by your administrator.'
      case 'device_revoked':
        return 'This device was logged out by your administrator.'
      case 'device_limit_exceeded':
        return 'Device limit exceeded. Contact your administrator to release a device slot.'
      case 'offline_grace_exceeded':
        return 'Offline verification grace period exceeded. Please reconnect to the internet and try again.'
      case 'profiles_sync_disabled':
        return 'Cloud browser configuration sync is not enabled for this license.'
      default:
        return error.message
    }
  }
  return error instanceof Error ? error.message : String(error)
}

export const cloudAPI = {
  status: () => call<CloudStatus>('/status'),

  updateConfig: async (config: CloudConfigInput) => {
    const res = await call<CloudStatus>('/config', json('PUT', config))
    emitStatusRefresh()
    return res
  },

  activate: async (token: string) => {
    const res = await call<CloudActivateResult>('/activate', json('POST', { token }))
    emitStatusRefresh()
    return res
  },

  logout: async () => {
    const res = await call<CloudStatus>('/logout', json('POST', {}))
    emitStatusRefresh()
    return res
  },

  sync: async (scopes?: ('policy' | 'subscriptions' | 'profiles')[]) => {
    const res = await call<CloudSyncReport>('/sync', json('POST', scopes ? { scopes } : {}))
    emitStatusRefresh()
    return res
  },

  policy: () => call<CloudPolicyData>('/policy'),

  pushProfile: async (profileId: string, includeCookies = false) => {
    const res = await call<CloudProfilePushResult>(
      `/profiles/${encodeURIComponent(profileId)}/push`,
      json('POST', { include_cookies: includeCookies }),
    )
    emitStatusRefresh()
    return res
  },

  pullProfiles: async () => {
    const res = await call<CloudProfilesPullResult>('/profiles/pull', json('POST', {}))
    emitStatusRefresh()
    return res
  },
}
