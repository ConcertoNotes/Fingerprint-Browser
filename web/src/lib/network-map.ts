import type { Health, IntelComposite, MapFeature, MapFeatureProperties, SubscriptionUsage } from './network-api'

/**
 * Nodes of one city usually resolve to exactly the same coordinates. Drawn one
 * marker per node they would stack on a single pixel and only the top one could
 * be clicked, so the map draws one marker per *location* and lists every node
 * there when it is chosen.
 */
export interface LocationGroup {
  key: string
  coordinates: [number, number]
  nodes: MapFeatureProperties[]
  countries: string[]
  cities: string[]
  health: { ok: number; error: number; unknown: number }
  bestPurity: number | null
  stale: boolean
}

export function locationKey(lon: number, lat: number): string {
  return `${lat.toFixed(4)},${lon.toFixed(4)}`
}

export function groupByLocation(features: MapFeature[]): LocationGroup[] {
  const groups = new Map<string, LocationGroup>()
  for (const feature of features) {
    const [lon, lat] = feature.geometry.coordinates
    if (!Number.isFinite(lon) || !Number.isFinite(lat)) continue
    const key = locationKey(lon, lat)
    let group = groups.get(key)
    if (!group) {
      group = { key, coordinates: [lon, lat], nodes: [], countries: [], cities: [], health: { ok: 0, error: 0, unknown: 0 }, bestPurity: null, stale: false }
      groups.set(key, group)
    }
    const node = feature.properties
    group.nodes.push(node)
    if (node.country_iso && !group.countries.includes(node.country_iso)) group.countries.push(node.country_iso)
    if (node.city && !group.cities.includes(node.city)) group.cities.push(node.city)
    group.health[node.health in group.health ? node.health : 'unknown'] += 1
    if (node.purity_score != null) group.bestPurity = Math.max(group.bestPurity ?? 0, node.purity_score)
    group.stale ||= node.stale
  }
  for (const group of groups.values()) {
    group.nodes.sort((a, b) => healthRank(a.health) - healthRank(b.health) || (a.latency_ms ?? 1e9) - (b.latency_ms ?? 1e9) || a.label.localeCompare(b.label))
  }
  return [...groups.values()].sort((a, b) => b.nodes.length - a.nodes.length || a.key.localeCompare(b.key))
}

function healthRank(health: Health): number {
  return health === 'ok' ? 0 : health === 'unknown' ? 1 : 2
}

/** The dominant state of a group, for its marker colour. Text always accompanies it. */
export function groupTone(group: Pick<LocationGroup, 'health'>): 'ok' | 'warn' | 'danger' | 'unknown' {
  const { ok, error, unknown } = group.health
  if (ok && !error) return 'ok'
  if (ok) return 'warn'
  if (error) return 'danger'
  return unknown ? 'unknown' : 'unknown'
}

export function purityTone(score: number | null | undefined): 'ok' | 'warn' | 'danger' | 'unknown' {
  if (score == null) return 'unknown'
  if (score >= 60) return 'ok'
  if (score >= 40) return 'warn'
  return 'danger'
}

export function gradeLabel(grade: IntelComposite['grade']): string {
  switch (grade) {
    case 'excellent': return 'Very clean'
    case 'good': return 'Clean'
    case 'fair': return 'Average'
    case 'poor': return 'Risky'
    case 'bad': return 'High risk'
    default: return 'Not assessed'
  }
}

/** Hide the host part of an exit address when the instance asks for it. */
export function maskIp(ip: string | null | undefined, mask: boolean): string {
  if (!ip) return '—'
  if (!mask) return ip
  if (ip.includes(':')) {
    const head = ip.split(':').filter(Boolean).slice(0, 3)
    return `${head.join(':')}::x`
  }
  const parts = ip.split('.')
  return parts.length === 4 ? `${parts[0]}.${parts[1]}.${parts[2]}.x` : ip
}

export function countryName(iso: string | null | undefined, locale: string): string {
  if (!iso) return ''
  try {
    return new Intl.DisplayNames([locale], { type: 'region' }).of(iso.toUpperCase()) ?? iso
  } catch {
    return iso
  }
}

/** Regional-indicator flag for a measured country code; never derived from a node's name. */
export function flag(iso: string | null | undefined): string {
  if (!iso || !/^[A-Za-z]{2}$/.test(iso)) return ''
  return String.fromCodePoint(...[...iso.toUpperCase()].map(char => 0x1f1e6 + char.charCodeAt(0) - 65))
}

export function formatBytes(bytes: number | undefined, locale: string): string {
  if (bytes == null || !Number.isFinite(bytes)) return '—'
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
  let value = bytes
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1 }
  return `${new Intl.NumberFormat(locale, { maximumFractionDigits: value < 10 && unit ? 2 : 1 }).format(value)} ${units[unit]}`
}

export function usageSummary(usage: SubscriptionUsage | null | undefined): { used: number; total: number | null; percent: number | null; expires: Date | null } | null {
  if (!usage) return null
  const used = (usage.upload ?? 0) + (usage.download ?? 0)
  const total = usage.total && usage.total > 0 ? usage.total : null
  return {
    used,
    total,
    percent: total ? Math.min(100, Math.round((used / total) * 100)) : null,
    expires: usage.expire ? new Date(usage.expire * 1000) : null,
  }
}

/** External pages that take an address in their URL. Others have no per-IP page. */
export function lookupUrl(site: 'ipinfo' | 'scamalytics', ip: string): string | null {
  if (!/^[0-9a-fA-F:.]+$/.test(ip)) return null
  return site === 'ipinfo' ? `https://ipinfo.io/${ip}` : `https://scamalytics.com/ip/${ip}`
}

/** Great-circle distance, used to tell whether two location sources agree. */
export function distanceKm(lat1: number, lon1: number, lat2: number, lon2: number): number {
  const rad = Math.PI / 180
  const a = Math.sin(((lat2 - lat1) * rad) / 2) ** 2 + Math.cos(lat1 * rad) * Math.cos(lat2 * rad) * Math.sin(((lon2 - lon1) * rad) / 2) ** 2
  return 2 * Math.asin(Math.sqrt(a)) * 6371
}

type Ring = [number, number][]

/**
 * Country shapes made for spherical rendering cross the ±180° meridian (Russia,
 * Fiji); drawn flat, such a ring jumps across the whole world and paints
 * horizontal bands. Unwrap each ring so neighbouring points never jump more than
 * 180°, and drop Antarctica, whose ring encircles the pole and cannot be drawn
 * flat at all.
 */
export function unwrapRing(ring: Ring): Ring {
  const out: Ring = []
  let shift = 0
  ring.forEach(([lon, lat], index) => {
    if (index > 0) {
      const previous = ring[index - 1][0]
      if (lon - previous > 180) shift -= 360
      else if (previous - lon > 180) shift += 360
    }
    out.push([lon + shift, lat])
  })
  return out
}

interface CountryFeature {
  type: 'Feature'
  id?: string | number
  properties: { name?: string } | null
  geometry: { type: 'Polygon'; coordinates: Ring[] } | { type: 'MultiPolygon'; coordinates: Ring[][] } | null
}

export function prepareCountries<T extends { features: CountryFeature[] }>(collection: T): T {
  const features = collection.features
    .filter(feature => feature.properties?.name !== 'Antarctica' && String(feature.id) !== '010' && feature.geometry)
    .map(feature => {
      const geometry = feature.geometry!
      return geometry.type === 'Polygon'
        ? { ...feature, geometry: { ...geometry, coordinates: geometry.coordinates.map(unwrapRing) } }
        : { ...feature, geometry: { ...geometry, coordinates: geometry.coordinates.map(polygon => polygon.map(unwrapRing)) } }
    })
  return { ...collection, features }
}
