import assert from 'node:assert/strict'
import test from 'node:test'

import type { MapFeature, MapFeatureProperties } from '../src/lib/network-api'
import { flag, groupByLocation, groupTone, lookupUrl, maskIp, purityTone, usageSummary } from '../src/lib/network-map'

function feature(id: string, lon: number, lat: number, extra: Partial<MapFeatureProperties> = {}): MapFeature {
  return {
    type: 'Feature',
    id,
    geometry: { type: 'Point', coordinates: [lon, lat] },
    properties: {
      node_id: id, label: id, subscription_name: 's', protocol: 'ss', status: 'active',
      country_iso: 'JP', city: null, region: null, precision: 'coordinates', location_source: 'geoip',
      timezone: 'Asia/Tokyo', health: 'ok', latency_ms: 100, exit_ip: '203.0.113.1',
      checked_at: '2026-10-29T00:00:00Z', stale: false, profile_count: 0, shared_exit_count: 1,
      purity_score: null, risk_score: null, grade: null, ...extra,
    },
  }
}

test('nodes at identical coordinates become one selectable group listing all of them', () => {
  const groups = groupByLocation([
    feature('jp-slow', 139.6917, 35.6895, { latency_ms: 300 }),
    feature('jp-fast', 139.6917, 35.6895, { latency_ms: 80, purity_score: 70 }),
    feature('jp-down', 139.69171, 35.68951, { health: 'error' }),
    feature('us', -122.08, 37.38, { country_iso: 'US' }),
  ])
  assert.equal(groups.length, 2)
  const tokyo = groups[0]
  assert.deepEqual(tokyo.nodes.map(node => node.node_id), ['jp-fast', 'jp-slow', 'jp-down'])
  assert.deepEqual(tokyo.health, { ok: 2, error: 1, unknown: 0 })
  assert.equal(tokyo.bestPurity, 70)
  assert.equal(groupTone(tokyo), 'warn')
  assert.equal(groupTone(groups[1]), 'ok')
})

test('invalid coordinates are never drawn', () => {
  assert.equal(groupByLocation([feature('x', Number.NaN, 1)]).length, 0)
})

test('ip masking keeps the network and hides the host', () => {
  assert.equal(maskIp('203.0.113.77', true), '203.0.113.x')
  assert.equal(maskIp('203.0.113.77', false), '203.0.113.77')
  assert.equal(maskIp('2001:db8:1:2::77', true), '2001:db8:1::x')
  assert.equal(maskIp(null, false), '—')
})

test('purity tones and flags', () => {
  assert.equal(purityTone(88), 'ok')
  assert.equal(purityTone(45), 'warn')
  assert.equal(purityTone(10), 'danger')
  assert.equal(purityTone(null), 'unknown')
  assert.equal(flag('jp'), '🇯🇵')
  assert.equal(flag('JPN'), '')
})

test('usage summary', () => {
  const usage = usageSummary({ upload: 25, download: 25, total: 200, expire: 1893456000 })
  assert.equal(usage?.percent, 25)
  assert.equal(usage?.expires?.getUTCFullYear(), 2030)
  assert.equal(usageSummary({ upload: 1 })?.percent, null)
})

test('external lookups accept only addresses', () => {
  assert.equal(lookupUrl('scamalytics', '203.0.113.9'), 'https://scamalytics.com/ip/203.0.113.9')
  assert.equal(lookupUrl('ipinfo', '203.0.113.9/../x'), null)
})
