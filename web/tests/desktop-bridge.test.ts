import assert from 'node:assert/strict'
import test from 'node:test'

import { readExitIpDrift } from '../src/lib/api'
import {
  acquireViewportOverlay,
  cookiesToNetscape,
  getDesktopBridge,
  isViewportOccluded,
  parseDesktopCookies,
  subscribeViewportOcclusion,
  type PrismDesktopBridge,
} from '../src/lib/desktop-bridge'

test('acquireViewportOverlay ref-counts nested modals and calls setViewportVisible only at 0<->1 transitions', () => {
  const calls: boolean[] = []
  const events: boolean[] = []
  const prevWindow = (globalThis as { window?: unknown }).window

  const fakeBridge = {
    isDesktop: true,
    setViewportVisible: async (visible: boolean) => {
      calls.push(visible)
    },
  } as unknown as PrismDesktopBridge

  ;(globalThis as { window?: unknown }).window = { prismDesktop: fakeBridge }
  const unsub = subscribeViewportOcclusion(occluded => events.push(occluded))

  try {
    assert.equal(getDesktopBridge(), fakeBridge)
    assert.equal(isViewportOccluded(), false)

    const releaseA = acquireViewportOverlay()
    assert.equal(isViewportOccluded(), true)
    assert.deepEqual(calls, [false])
    assert.deepEqual(events, [true])

    const releaseB = acquireViewportOverlay()
    assert.equal(isViewportOccluded(), true)
    assert.deepEqual(calls, [false])
    assert.deepEqual(events, [true])

    // Idempotent double-release must not drop refcount prematurely
    releaseA()
    releaseA()
    assert.equal(isViewportOccluded(), true)
    assert.deepEqual(calls, [false])

    releaseB()
    assert.equal(isViewportOccluded(), false)
    assert.deepEqual(calls, [false, true])
    assert.deepEqual(events, [true, false])
  } finally {
    unsub()
    ;(globalThis as { window?: unknown }).window = prevWindow
  }
})

test('parseDesktopCookies parses JSON cookies and reports invalid rows cleanly', () => {
  const payload = JSON.stringify([
    {
      name: 'sid',
      value: 'abc123',
      domain: '.example.com',
      path: '/',
      secure: true,
      httpOnly: true,
      expirationDate: 1900000000,
    },
    {
      name: 'sess',
      value: 'ephemeral',
      domain: 'example.com',
    },
    {
      name: '',
      value: 'bad',
      domain: 'example.com',
    },
  ])

  const parsed = parseDesktopCookies(payload, 'json')
  assert.equal(parsed.total, 3)
  assert.equal(parsed.cookies.length, 2)
  assert.equal(parsed.sessionCount, 1)
  assert.equal(parsed.skipped.length, 1)
  assert.equal(parsed.skipped[0].row, 3)
  assert.equal(parsed.cookies[0].name, 'sid')
  assert.equal(parsed.cookies[0].secure, true)
  assert.equal(parsed.cookies[1].session, true)
})

test('parseDesktopCookies and cookiesToNetscape round-trip Netscape cookie format', () => {
  const netscapeText = cookiesToNetscape([
    {
      name: 'token',
      value: 'xyz=1',
      domain: '.prism.test',
      path: '/app',
      secure: true,
      expirationDate: 1900000000,
    },
    {
      name: 'session_id',
      value: 'live',
      domain: 'prism.test',
      path: '/',
      secure: false,
    },
  ])

  assert.match(netscapeText, /^# Netscape HTTP Cookie File/m)
  const parsed = parseDesktopCookies(netscapeText, 'netscape')
  assert.equal(parsed.total, 2)
  assert.equal(parsed.cookies.length, 2)
  assert.equal(parsed.sessionCount, 1)
  assert.equal(parsed.skipped.length, 0)
  assert.equal(parsed.cookies[0].domain, '.prism.test')
  assert.equal(parsed.cookies[0].path, '/app')
  assert.equal(parsed.cookies[0].secure, true)
  assert.equal(parsed.cookies[0].value, 'xyz=1')
  assert.equal(parsed.cookies[1].session, true)
})

test('readExitIpDrift extracts drift details from plain desktop IPC OpenProfileResult failures', () => {
  const drift = readExitIpDrift({
    ok: false,
    status: 409,
    code: 'exit_ip_drift',
    message: 'Exit IP drifted',
    details: [
      {
        previous_ip: '1.2.3.4',
        previous_country: 'US',
        current_ip: '5.6.7.8',
        current_country: 'JP',
        country_changed: true,
      },
    ],
  })

  assert.deepEqual(drift, {
    previous_ip: '1.2.3.4',
    previous_country: 'US',
    current_ip: '5.6.7.8',
    current_country: 'JP',
    country_changed: true,
  })
})
