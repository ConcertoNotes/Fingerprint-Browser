import assert from 'node:assert/strict'
import test from 'node:test'

import { ApiRequestError, systemAPI } from '../src/lib/api'
import {
  cloudAPI,
  entitlementDotClass,
  formatCloudAuthError,
  formatLicenseDate,
  isCustomerEdition,
  isEntitlementActive,
  isEntitlementBlocked,
  type CloudStatus,
} from '../src/lib/cloud-api'
import { translate } from '../src/lib/i18n'
import {
  filterLocalStaticGroups,
  isCloudProtectedNode,
  isProtectedNodeProtocol,
  nodeProtocolLabel,
  type NetworkNode,
  type Subscription,
} from '../src/lib/network-api'

function withMockBrowserEnv(
  fetchImpl: typeof globalThis.fetch,
  fn: (events: string[]) => Promise<void>,
): Promise<void> {
  const originalWindow = Object.getOwnPropertyDescriptor(globalThis, 'window')
  const originalFetch = globalThis.fetch
  const events: string[] = []
  Object.defineProperty(globalThis, 'window', {
    configurable: true,
    value: {
      localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
      dispatchEvent: (event: Event) => {
        events.push(event.type)
        return true
      },
    },
  })
  globalThis.fetch = fetchImpl
  return fn(events).finally(() => {
    globalThis.fetch = originalFetch
    if (originalWindow) Object.defineProperty(globalThis, 'window', originalWindow)
    else Reflect.deleteProperty(globalThis, 'window')
  })
}

test('cloudAPI.activate posts activation token, unwraps status+report, and refreshes status', async () => {
  let capturedUrl = ''
  let capturedMethod = ''
  let capturedBody = ''

  await withMockBrowserEnv(
    async (input, init) => {
      capturedUrl = String(input)
      capturedMethod = init?.method ?? 'GET'
      capturedBody = String(init?.body ?? '')
      return Response.json({
        success: true,
        data: {
          status: {
            configured: true,
            connected: true,
            base_url: null,
            has_token: true,
            auto_sync_on_startup: true,
            sync_profiles_enabled: false,
            device_label: 'Customer Mac',
            policy_source: 'cloud',
            policy_version: '2026.11.26',
            cloud_subscription_count: 2,
            edition: 'customer',
            brand_name: 'Prism VIP',
            cloud_url_locked: true,
            require_activation: false,
            tenant_id: 'tenant_1',
            tenant_name: 'Acme Studio',
            entitlement_status: 'active',
            expires_at: '2026-12-31T00:00:00Z',
            days_remaining: 35,
            max_devices: 3,
            active_devices: 1,
            offline_grace_hours: 24,
            profiles_sync_allowed: false,
            device_id: 'dev-uuid-1',
          } satisfies CloudStatus,
          report: {
            ok: true,
            subscriptions: { created: 2, updated: 0, retired: 0, unchanged: 0, total: 2 },
            errors: [],
          },
        },
      })
    },
    async events => {
      const res = await cloudAPI.activate('prism_test_token_12345')
      assert.equal(capturedUrl, '/api/v1/cloud/activate')
      assert.equal(capturedMethod, 'POST')
      assert.deepEqual(JSON.parse(capturedBody), { token: 'prism_test_token_12345' })
      assert.equal(res.status.edition, 'customer')
      assert.equal(res.status.tenant_name, 'Acme Studio')
      assert.equal(res.status.entitlement_status, 'active')
      assert.equal(res.status.max_devices, 3)
      assert.equal(res.status.profiles_sync_allowed, false)
      assert.equal(res.report.subscriptions?.created, 2)
      assert.ok(events.includes('cpm-status-refresh'))
    },
  )
})

test('cloudAPI.logout clears license via POST /api/v1/cloud/logout and emits status refresh', async () => {
  let capturedUrl = ''
  let capturedMethod = ''

  await withMockBrowserEnv(
    async (input, init) => {
      capturedUrl = String(input)
      capturedMethod = init?.method ?? 'GET'
      return Response.json({
        success: true,
        data: {
          configured: true,
          base_url: null,
          has_token: false,
          auto_sync_on_startup: true,
          sync_profiles_enabled: false,
          device_label: null,
          policy_source: 'builtin',
          policy_version: 'builtin',
          cloud_subscription_count: 0,
          edition: 'customer',
          cloud_url_locked: true,
          entitlement_status: 'none',
          tenant_name: null,
          expires_at: null,
          days_remaining: null,
          profiles_sync_allowed: false,
        } satisfies CloudStatus,
      })
    },
    async events => {
      const res = await cloudAPI.logout()
      assert.equal(capturedUrl, '/api/v1/cloud/logout')
      assert.equal(capturedMethod, 'POST')
      assert.equal(res.has_token, false)
      assert.equal(res.entitlement_status, 'none')
      assert.ok(events.includes('cpm-status-refresh'))
    },
  )
})

test('systemAPI.status parses customer edition and entitlement fields', async () => {
  await withMockBrowserEnv(
    async () =>
      Response.json({
        success: true,
        data: {
          total_profiles: 4,
          running_browsers: 1,
          total_groups: 2,
          cpu_percent: 8,
          memory_used_mb: 1024,
          memory_total_mb: 16384,
          memory_percent: 6.25,
          mihomo_available: true,
          uptime_seconds: 120,
          client_edition: 'customer',
          brand_name: 'Prism Exclusive',
          require_activation: true,
          entitlement_status: 'expiring_soon',
          entitlement_tenant_name: 'Team Alpha',
          entitlement_expires_at: '2026-12-01T00:00:00Z',
          entitlement_days_remaining: 5,
        },
      }),
    async () => {
      const status = await systemAPI.status()
      assert.equal(status.client_edition, 'customer')
      assert.equal(status.brand_name, 'Prism Exclusive')
      assert.equal(status.require_activation, true)
      assert.equal(status.entitlement_status, 'expiring_soon')
      assert.equal(status.entitlement_tenant_name, 'Team Alpha')
      assert.equal(status.entitlement_days_remaining, 5)
    },
  )
})

test('edition and entitlement helpers map every license state accurately', () => {
  assert.equal(isCustomerEdition({ edition: 'customer' }), true)
  assert.equal(isCustomerEdition({ client_edition: 'customer' }), true)
  assert.equal(isCustomerEdition({ edition: 'standard', cloud_url_locked: true }), true)
  assert.equal(isCustomerEdition({ edition: 'standard', cloud_url_locked: false }), false)
  assert.equal(isCustomerEdition(null), false)

  assert.equal(isEntitlementActive('active'), true)
  assert.equal(isEntitlementActive('expiring_soon'), true)
  assert.equal(isEntitlementActive('expired'), false)
  assert.equal(isEntitlementActive('none'), false)

  for (const blocked of ['expired', 'disabled', 'revoked', 'device_limit', 'offline_grace_exceeded']) {
    assert.equal(isEntitlementBlocked(blocked), true)
    assert.equal(entitlementDotClass(blocked), 'bg-danger-dot')
  }
  assert.equal(isEntitlementBlocked('active'), false)
  assert.equal(isEntitlementBlocked('expiring_soon'), false)
  assert.equal(isEntitlementBlocked('none'), false)

  assert.equal(entitlementDotClass('active'), 'bg-ok-dot')
  assert.equal(entitlementDotClass('expiring_soon'), 'bg-warn-dot')
  assert.equal(entitlementDotClass('none'), 'bg-slate-300')

  assert.equal(formatLicenseDate(null, 'zh-CN'), null)
  assert.equal(formatLicenseDate('not-a-date', 'zh-CN'), null)
  assert.ok(formatLicenseDate('2026-12-31T00:00:00Z', 'en'))
})

test('cloud credential protection helpers isolate cloud nodes from local static groups', () => {
  const subs: Subscription[] = [
    {
      id: 'sub-cloud-static',
      name: 'Cloud Dedicated IP',
      source_kind: 'manual',
      managed_by: 'cloud',
      readonly: true,
      manual: true,
      source_display: '云端托管',
      source_file_name: null,
      status: 'ok',
      last_synced_at: null,
      last_error_code: null,
      last_error: null,
      usage: null,
      revision: 1,
      row_version: 1,
      created_at: '2026-11-26T00:00:00Z',
      updated_at: '2026-11-26T00:00:00Z',
      node_counts: { active: 1, retired: 0, invalid: 0 },
      bound_profiles: 0,
    },
    {
      id: 'sub-local-static',
      name: 'My Local Residential IP',
      source_kind: 'manual',
      managed_by: 'local',
      readonly: false,
      manual: true,
      source_display: null,
      source_file_name: null,
      status: 'ok',
      last_synced_at: null,
      last_error_code: null,
      last_error: null,
      usage: null,
      revision: 1,
      row_version: 1,
      created_at: '2026-11-26T00:00:00Z',
      updated_at: '2026-11-26T00:00:00Z',
      node_counts: { active: 2, retired: 0, invalid: 0 },
      bound_profiles: 0,
    },
  ]

  const localStatic = filterLocalStaticGroups(subs)
  assert.deepEqual(
    localStatic.map(s => s.id),
    ['sub-local-static'],
  )

  const baseNode: Omit<NetworkNode, 'id' | 'subscription_id' | 'subscription_name' | 'name' | 'protocol'> = {
    entry: null,
    status: 'active',
    invalid_reason: null,
    created_at: '2026-11-26T00:00:00Z',
    updated_at: '2026-11-26T00:00:00Z',
    retired_at: null,
    profile_count: 0,
    shared_exit_count: 1,
    check: null,
    intel: null,
    intel_checked_at: null,
  }

  const cloudByField: NetworkNode = {
    ...baseNode,
    id: 'n1',
    subscription_id: 'sub-any',
    subscription_name: 'Cloud',
    subscription_managed_by: 'cloud',
    name: 'US-1',
    protocol: 'protected',
    credentials_protected: true,
    editable: false,
    deletable: false,
  }
  assert.equal(isCloudProtectedNode(cloudByField), true)

  const cloudByParentSub: NetworkNode = {
    ...baseNode,
    id: 'n2',
    subscription_id: 'sub-cloud-static',
    subscription_name: 'Cloud Dedicated IP',
    name: 'UK-1',
    protocol: 'protected',
  }
  assert.equal(isCloudProtectedNode(cloudByParentSub, subs[0]), true)

  const localStaticNode: NetworkNode = {
    ...baseNode,
    id: 'n3',
    subscription_id: 'sub-local-static',
    subscription_name: 'My Local Residential IP',
    subscription_managed_by: 'local',
    name: 'Local Static 1',
    protocol: 'socks5',
    entry: '203.0.113.10:1080',
    credentials_protected: false,
    editable: true,
    deletable: true,
  }
  assert.equal(isCloudProtectedNode(localStaticNode, subs[1]), false)

  const tZh = (msg: string) => translate('zh-CN', msg)
  assert.equal(isProtectedNodeProtocol(cloudByField), true)
  assert.equal(nodeProtocolLabel(cloudByField, tZh), '云端加密通道')
  assert.equal(nodeProtocolLabel({ protocol: 'protected' }, tZh), '云端加密通道')
  assert.equal(nodeProtocolLabel({ protocol: 'vless', subscription_managed_by: 'cloud' }, tZh), '云端加密通道')
  assert.equal(nodeProtocolLabel(localStaticNode, tZh, subs[1]), 'socks5')
})

test('formatCloudAuthError codes and key customer UI strings have Chinese translations', () => {
  const codes = [
    'invalid_token',
    'unauthorized',
    'tenant_expired',
    'tenant_disabled',
    'device_revoked',
    'device_limit_exceeded',
    'offline_grace_exceeded',
    'profiles_sync_disabled',
  ]
  for (const code of codes) {
    const key = formatCloudAuthError(new ApiRequestError('raw', 403, code))
    const translated = translate('zh-CN', key)
    assert.notEqual(translated, key, `Missing zh-CN translation for ${code}: ${key}`)
  }

  assert.equal(translate('zh-CN', 'My license'), '我的授权')
  assert.equal(translate('zh-CN', 'Encrypted cloud tunnel'), '云端加密通道')
  assert.equal(translate('zh-CN', 'Activate & sync nodes now'), '立即激活并同步节点')
  assert.equal(translate('zh-CN', 'Skip for now, use local nodes'), '暂不激活，先使用本地节点')
  assert.equal(translate('zh-CN', 'Sync exclusive nodes'), '同步专属节点')
  assert.equal(translate('zh-CN', 'Activate license'), '激活授权')
})
