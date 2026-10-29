import { apiRequest } from './api'

// Client for /api/v1/network: subscriptions, nodes, measured exits, bindings.
// Node secrets never reach the browser; these types describe public metadata only.

const BASE = '/api/v1/network'

export type Health = 'ok' | 'error' | 'unknown'
export type Precision = 'unknown' | 'country' | 'coordinates' | 'city'
export type NodeStatus = 'active' | 'retired' | 'invalid'

export interface NodeCheck {
  health: Health
  exit_ip: string | null
  country_iso: string | null
  city: string | null
  region: string | null
  latitude: number | null
  longitude: number | null
  timezone: string | null
  precision: Precision
  location_source: string | null
  latency_ms: number | null
  error_code: string | null
  error: string | null
  checked_at: string
  stale: boolean
}

export type IntelSourceStatus = 'ok' | 'error' | 'disabled' | 'not_configured'

export interface IntelSignal {
  level: 'info' | 'warning' | 'danger'
  code: 'datacenter' | 'residential' | 'broadcast' | 'native' | 'vpn' | 'proxy' | 'tor' | 'relay' | 'high_fraud' | 'ip_mismatch'
}

export interface IntelComposite {
  risk_score: number | null
  purity_score: number | null
  grade: 'excellent' | 'good' | 'fair' | 'poor' | 'bad' | null
  score_sources: { source: string; score: number }[]
  ip_type: 'datacenter' | 'residential' | null
  native: boolean | null
  flags: string[]
  signals: IntelSignal[]
  location: {
    city: string | null
    region: string | null
    country_iso: string | null
    timezone: string | null
    latitude: number | null
    longitude: number | null
  }
  asn: number | null
  as_org: string | null
  hostname: string | null
}

export interface IntelSource {
  status: IntelSourceStatus
  error?: string
  ip?: string | null
  fraud_score?: number | null
  city?: string | null
  region?: string | null
  country_iso?: string | null
  asn?: number | null
  as_org?: string | null
  is_residential?: boolean | null
  is_broadcast?: boolean | null
  risk?: string | null
  hostname?: string | null
  privacy?: Record<string, boolean> | null
}

export interface IpIntel {
  ip: string
  sources: Record<string, IntelSource>
  composite: IntelComposite
}

export interface NetworkNode {
  id: string
  subscription_id: string
  subscription_name: string
  name: string
  protocol: string
  entry: string
  status: NodeStatus
  invalid_reason: string | null
  created_at: string
  updated_at: string
  retired_at: string | null
  profile_count: number
  shared_exit_count: number
  check: NodeCheck | null
  intel: IpIntel | null
  intel_checked_at: string | null
}

export interface SubscriptionUsage {
  upload?: number
  download?: number
  total?: number
  expire?: number
}

export interface Subscription {
  id: string
  name: string
  source_kind: 'url' | 'file' | 'manual'
  /** A group of fixed nodes added by hand; it has no source to refresh. */
  manual: boolean
  source_display: string | null
  source_file_name: string | null
  status: 'idle' | 'syncing' | 'ok' | 'error'
  last_synced_at: string | null
  last_error_code: string | null
  last_error: string | null
  usage: SubscriptionUsage | null
  revision: number
  row_version: number
  created_at: string
  updated_at: string
  node_counts: { active: number; retired: number; invalid: number }
  bound_profiles: number
}

export interface SyncReport {
  created: number
  updated: number
  retired: number
  unchanged: number
  warnings: { index: number; reason: string }[]
  last_synced_at: string
}

export interface MapFeatureProperties {
  node_id: string
  label: string
  subscription_id: string
  subscription_name: string
  protocol: string
  status: NodeStatus
  country_iso: string | null
  city: string | null
  region: string | null
  precision: Precision
  location_source: string | null
  timezone: string | null
  health: Health
  latency_ms: number | null
  exit_ip: string | null
  checked_at: string
  stale: boolean
  profile_count: number
  shared_exit_count: number
  purity_score: number | null
  risk_score: number | null
  grade: IntelComposite['grade']
}

export interface MapFeature {
  type: 'Feature'
  id: string
  geometry: { type: 'Point'; coordinates: [number, number] }
  properties: MapFeatureProperties
}

export interface NetworkMap {
  features: { type: 'FeatureCollection'; features: MapFeature[] }
  unknown_location: { node_id: string; subscription_id: string; label: string; status: NodeStatus; health: Health; country_iso: string | null; exit_ip: string | null }[]
  unknown_location_count: number
  checked_count: number
  total_nodes: number
}

export interface NetworkRuntime {
  profile_id: string
  state: 'stopped' | 'ready' | 'running' | 'degraded' | 'stopping'
  adapter_kind: 'native' | 'mihomo'
  node_id: string | null
  node_name?: string
  listen_port?: number
  exit_ip: string | null
  engine_alive?: boolean
  error: { code: string; message: string } | null
}

export interface Binding {
  profile_id: string
  node_id: string
  row_version: number
  bound_at: string
  node: NetworkNode
  usable: boolean
  runtime?: NetworkRuntime
  geography_conflict?: boolean
  overrides_manual_proxy?: boolean
}

export interface BatchJob {
  id: string
  state: 'queued' | 'running' | 'finished' | 'failed' | 'cancelled'
  total: number
  done: number
  ok: number
  failed: number
  skipped: number
  engine_processes: number
  error: { code: string; message: string } | null
}

export interface NetworkSettings {
  mihomo_path: string | null
  probe_concurrency: number
  probe_timeout_seconds: number
  intel_cache_hours: number
  intel_ipinfo_enabled: boolean
  intel_ippure_enabled: boolean
  intel_scamalytics_enabled: boolean
  mask_exit_ips: boolean
  has_intel_ipinfo_token: boolean
  has_intel_scamalytics_url: boolean
  has_intel_scamalytics_key: boolean
}

export interface NetworkStatus {
  engine: { available: boolean; path?: string; version?: string; code?: string; message?: string }
  geoip: { available: boolean; name?: string; size?: number; modified_at?: number }
  encryption_available: boolean
  settings: NetworkSettings
  active_runtimes: number
}

export type CheckSite = 'ippure' | 'ipip' | 'ipinfo' | 'scamalytics'

export type SimpleProxyType = 'http' | 'https' | 'socks5'

export interface ManualNodeInput {
  name?: string
  type: SimpleProxyType
  server: string
  port: number
  username?: string
  password?: string
}

export interface ManualNodesResult {
  group: Subscription
  created: NetworkNode[]
  skipped: { line: number; reason: string }[]
}

interface Envelope<T> { data: T }

async function data<T>(path: string, init?: RequestInit): Promise<T> {
  const body = await apiRequest<Envelope<T>>(BASE + path, init)
  return body.data
}

function json(method: string, body?: unknown): RequestInit {
  return { method, body: body === undefined ? undefined : JSON.stringify(body) }
}

export const networkAPI = {
  status: () => data<NetworkStatus>('/status'),
  settings: () => data<NetworkSettings>('/settings'),
  updateSettings: (changes: Partial<Record<string, unknown>>) => data<NetworkSettings>('/settings', json('PATCH', changes)),

  subscriptions: async () => (await data<{ subscriptions: Subscription[] }>('/subscriptions')).subscriptions,
  createFromUrl: (url: string, name?: string) =>
    data<{ subscription: Subscription; report: SyncReport }>('/subscriptions', json('POST', { url, name: name || null })),
  createFromFile: (content: string, fileName: string, name?: string) =>
    data<{ subscription: Subscription; report: SyncReport }>('/subscriptions', json('POST', { content, file_name: fileName, name: name || null })),
  sync: (id: string, content?: string) =>
    data<{ subscription: Subscription; report: SyncReport }>(`/subscriptions/${id}/sync`, json('POST', content === undefined ? {} : { content })),
  rename: (id: string, name: string, rowVersion: number) =>
    data<Subscription>(`/subscriptions/${id}`, json('PATCH', { name, row_version: rowVersion })),
  removeSubscription: (id: string, retireBound = false) =>
    data<{ deleted: boolean; retired_bound_nodes: number }>(`/subscriptions/${id}?retire_bound=${retireBound}`, json('DELETE')),

  nodes: (params: Record<string, string | number | boolean | undefined> = {}) => {
    const query = new URLSearchParams()
    for (const [key, value] of Object.entries(params)) if (value !== undefined && value !== '') query.set(key, String(value))
    const suffix = query.toString() ? `?${query}` : ''
    return data<{ nodes: NetworkNode[]; total: number }>(`/nodes${suffix}`)
  },
  node: (id: string) => data<NetworkNode>(`/nodes/${id}`),
  checkNode: (id: string, options: { include_intel?: boolean; refresh_intel?: boolean } = {}) =>
    data<NetworkNode>(`/nodes/${id}/check`, json('POST', options)),
  checkBatch: (nodeIds: string[]) => data<BatchJob>('/nodes/check-batch', json('POST', { node_ids: nodeIds })),
  job: (id: string) => data<BatchJob>(`/jobs/${id}`),
  currentJob: async () => (await data<{ job: BatchJob | null }>('/jobs/current')).job,
  addManualNode: (target: { group_id?: string; group_name?: string }, node: ManualNodeInput) =>
    data<ManualNodesResult>('/manual-nodes', json('POST', { ...target, node })),
  addManualText: (target: { group_id?: string; group_name?: string }, text: string, defaultType: SimpleProxyType) =>
    data<ManualNodesResult>('/manual-nodes', json('POST', { ...target, text, default_type: defaultType })),
  updateNode: (id: string, changes: Partial<ManualNodeInput>) => data<NetworkNode>(`/nodes/${id}`, json('PATCH', changes)),
  deleteNode: (id: string) => data<{ id: string }>(`/nodes/${id}`, json('DELETE')),
  cancelJob: (id: string) => data<BatchJob>(`/jobs/${id}/cancel`, json('POST')),

  map: () => data<NetworkMap>('/map'),

  bindings: async () => (await data<{ bindings: Binding[] }>('/bindings')).bindings,
  binding: (profileId: string) => data<{ binding: Binding | null; runtime: NetworkRuntime }>(`/bindings/${profileId}`),
  bind: (profileId: string, nodeId: string, bindingRowVersion: number | null) =>
    data<Binding>(`/bindings/${profileId}`, json('PUT', { node_id: nodeId, binding_row_version: bindingRowVersion })),
  unbind: (profileId: string, rowVersion: number) =>
    data<{ profile_id: string }>(`/bindings/${profileId}?binding_row_version=${rowVersion}`, json('DELETE')),

  runtimes: async () => (await data<{ runtimes: NetworkRuntime[] }>('/runtime')).runtimes,
  runtime: (profileId: string) => data<NetworkRuntime>(`/runtime/${profileId}`),
  reconnect: (profileId: string) => data<NetworkRuntime>(`/runtime/${profileId}/reconnect`, json('POST')),
  openSite: (profileId: string, site: CheckSite) => data<{ url: string }>(`/runtime/${profileId}/open-site`, json('POST', { site })),
}
