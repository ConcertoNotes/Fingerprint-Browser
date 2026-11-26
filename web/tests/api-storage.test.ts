import assert from 'node:assert/strict'
import test from 'node:test'
import { ApiRequestError, getApiKey, readExitIpDrift, shortGpuName, systemAPI, unwrapEnvelope } from '../src/lib/api'

test('blocked local storage does not break API requests', async () => {
  const originalWindow = Object.getOwnPropertyDescriptor(globalThis, 'window')
  const originalFetch = globalThis.fetch
  Object.defineProperty(globalThis, 'window', {
    configurable: true,
    value: { get localStorage() { throw new Error('Storage disabled') } },
  })
  globalThis.fetch = async (_input, init) => {
    assert.equal((init?.headers as Record<string, string>)['X-API-Key'], undefined)
    return Response.json({
      success: true,
      message: 'ok',
      data: {
        total_profiles: 2,
        running_browsers: 1,
        total_groups: 1,
        cpu_percent: 12.5,
        memory_used_mb: 2048,
        memory_total_mb: 16384,
        memory_percent: 12.5,
        mihomo_available: true,
        mihomo_version: '1.19.0',
        cloud_policy_source: 'builtin',
        cloud_policy_version: 'builtin-2026.11.26',
        browser_installed: true,
        uptime_seconds: 100,
      },
    })
  }
  try {
    assert.equal(getApiKey(), '')
    const status = await systemAPI.status()
    assert.equal(status.running_browsers, 1)
    assert.equal(status.mihomo_version, '1.19.0')
  } finally {
    globalThis.fetch = originalFetch
    if (originalWindow) Object.defineProperty(globalThis, 'window', originalWindow)
    else Reflect.deleteProperty(globalThis, 'window')
  }
})

test('unwrapEnvelope handles both wrapped ApiResponse and raw payloads', () => {
  assert.deepEqual(unwrapEnvelope({ success: true, message: 'ok', data: { score: 95 } }), { score: 95 })
  assert.deepEqual(unwrapEnvelope({ total_profiles: 3 }), { total_profiles: 3 })
})

test('shortGpuName simplifies verbose ANGLE renderer strings', () => {
  assert.equal(shortGpuName('ANGLE (Apple, Apple M2 Pro, OpenGL 4.1)'), 'Apple M2 Pro')
  assert.equal(shortGpuName(null), '—')
})

test('readExitIpDrift extracts structured 409 exit_ip_drift details', () => {
  const err = new ApiRequestError('Exit IP changed', 409, 'exit_ip_drift', [
    {
      previous_ip: '203.0.113.10',
      previous_country: 'US',
      current_ip: '198.51.100.22',
      current_country: 'JP',
      country_changed: true,
    },
  ])
  const parsed = readExitIpDrift(err)
  assert.deepEqual(parsed, {
    previous_ip: '203.0.113.10',
    previous_country: 'US',
    current_ip: '198.51.100.22',
    current_country: 'JP',
    country_changed: true,
  })
  assert.equal(readExitIpDrift(new ApiRequestError('Locked', 409, 'profile_locked')), null)
})
