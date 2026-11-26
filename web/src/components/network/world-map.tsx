'use client'

import 'maplibre-gl/dist/maplibre-gl.css'

import { useEffect, useRef, useState, type ReactNode } from 'react'
import type { Map as MapLibreMap, Marker } from 'maplibre-gl'
import type Supercluster from 'supercluster'
import type { PointFeature } from 'supercluster'
import { Check, TriangleAlert, X } from 'lucide-react'

import { useI18n } from '@/components/i18n-provider'
import { useTheme } from '@/components/theme-provider'
import { nodeProtocolLabel } from '@/lib/network-api'
import { countryName, flag, groupTone, maskIp, purityTone, prepareCountries, type LocationGroup } from '@/lib/network-map'

export interface WorldMapProps {
  groups: LocationGroup[]
  selectedKey: string | null
  onSelect: (key: string) => void
  mode?: 'manage' | 'picker'
  selectedNodeId?: string | null
  onSelectNode?: (nodeId: string) => void
  maskIps?: boolean
  /** Changes when the map should fly to the selected location. */
  focusToken?: number
  /** Changes when the shown set changes (another airport): fit the map to it again. */
  fitKey?: string
  /** Custom floating card shown beside a clicked location. */
  renderPopup?: (group: LocationGroup, close: () => void) => ReactNode
}

type Point = PointFeature<{ key: string; count: number; tone: string }>
type ClusterProps = { key: string; count: number; tone: string }

const MAP_PALETTE = {
  dark: {
    sea: '#0a0f1b',
    land: '#161e31',
    border: '#293754',
  },
  light: {
    sea: '#e5eef7',
    land: '#fafbfd',
    border: '#cbd5e1',
  },
} as const

const TONE_CLASS_DARK: Record<string, string> = {
  ok: 'border-[#34d399] bg-[#064e3b]/92 text-[#6ee7b7]',
  warn: 'border-[#fbbf24] bg-[#78350f]/92 text-[#fde68a]',
  danger: 'border-[#f87171] bg-[#7f1d1d]/92 text-[#fecaca]',
  unknown: 'border-[#475569] bg-[#1e293b]/95 text-[#cbd5e1]',
  cluster: 'border-[#818cf8] bg-[#312e81]/92 text-[#e0e7ff]',
}

const TONE_CLASS_LIGHT: Record<string, string> = {
  ok: 'border-[#10b981] bg-[#ecfdf5] text-[#059669]',
  warn: 'border-[#f59e0b] bg-[#fffbeb] text-[#b45309]',
  danger: 'border-[#ef4444] bg-[#fef2f2] text-[#dc2626]',
  unknown: 'border-[#cbd5e1] bg-white text-[#64748b]',
  cluster: 'border-[#4f46e5] bg-[#eef2ff] text-[#4338ca]',
}

const SELECTED_CLASS = 'border-[#7c7bff] bg-[#6366f1] text-white map-pin-pulse'
const SHADOW_DARK = 'shadow-[0_0_0_2.5px_rgba(10,15,27,0.92),0_4px_14px_rgba(0,0,0,0.55)]'
const SHADOW_LIGHT = 'shadow-[0_0_0_3px_rgba(255,255,255,0.92),0_3px_10px_rgba(15,23,42,0.16)]'

/**
 * Theme-aware offline vector world map drawn from bundled Natural Earth country shapes:
 * switches seamlessly between Obsidian Cartography (Dark) and Porcelain Cartography (Light).
 */
export function WorldMap({
  groups,
  selectedKey,
  onSelect,
  mode = 'manage',
  selectedNodeId = null,
  onSelectNode,
  maskIps = false,
  focusToken,
  fitKey,
  renderPopup,
}: WorldMapProps) {
  const { t, locale } = useI18n()
  const { isDark } = useTheme()
  const isDarkRef = useRef(isDark)
  const container = useRef<HTMLDivElement>(null)
  const mapRef = useRef<MapLibreMap | null>(null)
  const markers = useRef(new Map<string, Marker>())
  const index = useRef<Supercluster<ClusterProps, ClusterProps> | null>(null)
  const maplibre = useRef<typeof import('maplibre-gl') | null>(null)
  const groupsRef = useRef(groups)
  const selectRef = useRef(onSelect)
  const selectedRef = useRef(selectedKey)
  const selectedNodeKeyRef = useRef<string | null>(null)
  const fitted = useRef<string | null>(null)
  const popupRef = useRef<string | null>(null)
  const [ready, setReady] = useState(false)
  const [failure, setFailure] = useState<string | null>(null)
  const [popup, setPopup] = useState<{ key: string; x: number; y: number } | null>(null)
  const [boundsSize, setBoundsSize] = useState<{ width: number; height: number }>({ width: 0, height: 0 })

  const selectedNodeGroupKey = selectedNodeId
    ? groups.find(group => group.nodes.some(node => node.node_id === selectedNodeId))?.key ?? null
    : null

  useEffect(() => { groupsRef.current = groups }, [groups])
  useEffect(() => { selectRef.current = onSelect }, [onSelect])
  useEffect(() => { isDarkRef.current = isDark }, [isDark])

  function markerToneClasses(tone: string, dark: boolean): string {
    const map = dark ? TONE_CLASS_DARK : TONE_CLASS_LIGHT
    const shadow = dark ? SHADOW_DARK : SHADOW_LIGHT
    return `${shadow} ${map[tone] ?? map.unknown}`
  }

  function applyMarkerSelectionStyles() {
    const dark = isDarkRef.current
    for (const [id, marker] of markers.current) {
      const button = marker.getElement().firstElementChild as HTMLButtonElement | null
      if (!button) continue
      const isCluster = id.startsWith('cluster-')
      const isActive = !isCluster && (id === selectedRef.current || (selectedNodeKeyRef.current !== null && id === selectedNodeKeyRef.current))
      const tone = button.dataset.tone ?? 'unknown'
      const baseClass = 'flex cursor-pointer items-center justify-center rounded-full border-[1.75px] font-mono text-[11px] font-semibold transition-all duration-150 hover:scale-110 focus-visible:scale-110'
      button.className = isActive
        ? `${baseClass} ${SELECTED_CLASS}`
        : `${baseClass} ${markerToneClasses(tone, dark)}`
    }
  }

  function placePopup(key: string | null) {
    popupRef.current = key
    const map = mapRef.current
    const group = groupsRef.current.find(entry => entry.key === key)
    if (!map || !group || !key) { setPopup(null); return }
    const point = map.project(group.coordinates)
    setPopup({ key, x: point.x, y: point.y })
  }

  useEffect(() => {
    let cancelled = false
    let map: MapLibreMap | null = null
    let resizeObserver: ResizeObserver | null = null
    const currentMarkers = markers.current

    async function start() {
      try {
        const [library, atlas, topojson] = await Promise.all([
          import('maplibre-gl'),
          import('world-atlas/countries-110m.json'),
          import('topojson-client'),
        ])
        if (cancelled || !container.current) return
        maplibre.current = library
        library.setWorkerUrl(`${window.location.origin}/vendor/maplibre/maplibre-gl-worker.mjs`)
        const topology = (atlas.default ?? atlas) as unknown as Parameters<typeof topojson.feature>[0]
        const raw = topojson.feature(
          topology,
          (topology as unknown as { objects: { countries: Parameters<typeof topojson.feature>[1] } }).objects.countries,
        )
        const countries = prepareCountries(raw as unknown as Parameters<typeof prepareCountries>[0])
        const palette = isDarkRef.current ? MAP_PALETTE.dark : MAP_PALETTE.light
        map = new library.Map({
          container: container.current,
          style: {
            version: 8,
            sources: {
              countries: {
                type: 'geojson',
                data: countries as unknown as GeoJSON.FeatureCollection,
                tolerance: 0.2,
              },
            },
            layers: [
              { id: 'sea', type: 'background', paint: { 'background-color': palette.sea } },
              { id: 'land', type: 'fill', source: 'countries', paint: { 'fill-color': palette.land } },
              {
                id: 'borders',
                type: 'line',
                source: 'countries',
                paint: {
                  'line-color': palette.border,
                  'line-width': ['interpolate', ['linear'], ['zoom'], 1, 0.7, 6, 1.3],
                },
              },
            ],
          },
          center: [18, 28],
          zoom: 1.35,
          minZoom: 0.8,
          maxZoom: 11,
          renderWorldCopies: false,
          dragRotate: false,
          pitchWithRotate: false,
          attributionControl: { compact: true, customAttribution: 'Natural Earth' },
        })
        map.touchZoomRotate.disableRotation()
        map.addControl(new library.NavigationControl({ showCompass: false }), 'top-right')
        map.on('error', () => { /* Shapes are local; nothing to retry. */ })
        map.on('load', () => {
          if (!cancelled) {
            mapRef.current = map
            if (container.current) {
              setBoundsSize({ width: container.current.clientWidth, height: container.current.clientHeight })
            }
            setReady(true)
          }
        })
        map.on('moveend', () => render())
        map.on('move', () => { if (popupRef.current) placePopup(popupRef.current) })
        map.on('click', () => placePopup(null))

        if (typeof ResizeObserver !== 'undefined' && container.current) {
          resizeObserver = new ResizeObserver(() => {
            if (!container.current) return
            setBoundsSize({ width: container.current.clientWidth, height: container.current.clientHeight })
            map?.resize()
          })
          resizeObserver.observe(container.current)
        }
      } catch (err) {
        console.error('Map failed to start', err)
        if (!cancelled) setFailure('The map cannot be drawn here (WebGL unavailable). Use the location list instead.')
      }
    }
    start()
    return () => {
      cancelled = true
      resizeObserver?.disconnect()
      for (const marker of currentMarkers.values()) marker.remove()
      currentMarkers.clear()
      map?.remove()
      mapRef.current = null
    }
    // render and placePopup are stable through refs.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Live theme switch without reloading MapLibre or losing viewport coordinates
  useEffect(() => {
    const map = mapRef.current
    if (!ready || !map) return
    const palette = isDark ? MAP_PALETTE.dark : MAP_PALETTE.light
    try {
      map.setPaintProperty('sea', 'background-color', palette.sea)
      map.setPaintProperty('land', 'fill-color', palette.land)
      map.setPaintProperty('borders', 'line-color', palette.border)
    } catch {
      // Ignore if map is mid-teardown.
    }
    isDarkRef.current = isDark
    applyMarkerSelectionStyles()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, isDark])

  function markerElement(
    id: string,
    count: number,
    tone: string,
    label: string,
    cluster: boolean,
  ): { root: HTMLDivElement; button: HTMLButtonElement } {
    const root = document.createElement('div')
    const button = document.createElement('button')
    button.type = 'button'
    const size = Math.min(44, (cluster ? 28 : 22) + Math.round(Math.log2(count + 1) * 4))
    button.style.width = button.style.height = `${size}px`
    const isActive = !cluster && (id === selectedRef.current || (selectedNodeKeyRef.current !== null && id === selectedNodeKeyRef.current))
    const baseClass = 'flex cursor-pointer items-center justify-center rounded-full border-[1.75px] font-mono text-[11px] font-semibold transition-all duration-150 hover:scale-110 focus-visible:scale-110'
    button.className = isActive
      ? `${baseClass} ${SELECTED_CLASS}`
      : `${baseClass} ${markerToneClasses(cluster ? 'cluster' : tone, isDarkRef.current)}`
    button.textContent = String(count)
    button.setAttribute('aria-label', label)
    button.title = label
    button.dataset.key = id
    button.dataset.tone = cluster ? 'cluster' : tone
    root.appendChild(button)
    return { root, button }
  }

  function render() {
    const map = mapRef.current
    const library = maplibre.current
    if (!map || !library || !index.current) return
    const bounds = map.getBounds()
    const zoom = Math.round(map.getZoom())
    const items = index.current.getClusters(
      [Math.max(-180, bounds.getWest()), bounds.getSouth(), Math.min(180, bounds.getEast()), bounds.getNorth()],
      zoom,
    )
    const seen = new Set<string>()
    for (const item of items) {
      const isCluster = Boolean((item.properties as { cluster?: boolean }).cluster)
      const id = isCluster ? `cluster-${item.id}` : (item.properties as ClusterProps).key
      seen.add(id)
      if (markers.current.has(id)) continue
      const [lon, lat] = item.geometry.coordinates as [number, number]
      let created: { root: HTMLDivElement; button: HTMLButtonElement }
      if (isCluster) {
        const count = (item.properties as unknown as { node_total: number }).node_total
        created = markerElement(id, count, 'cluster', t('{count} nodes in this area. Zoom in', { count }), true)
        created.button.addEventListener('click', event => {
          event.stopPropagation()
          const clusterId = Number(item.id)
          const expansion = Math.min(index.current?.getClusterExpansionZoom(clusterId) ?? zoom + 2, 11)
          map.easeTo({ center: [lon, lat], zoom: expansion })
        })
      } else {
        const props = item.properties as ClusterProps
        const group = groupsRef.current.find(entry => entry.key === props.key)
        const place = group ? (group.cities[0] ?? group.countries.map(iso => countryName(iso, locale)).join(', ')) : ''
        created = markerElement(id, props.count, props.tone, t('{place}: {count} nodes', { place: place || t('Location'), count: props.count }), false)
        created.button.addEventListener('click', event => {
          event.stopPropagation()
          selectRef.current(props.key)
          placePopup(props.key)
        })
      }
      markers.current.set(id, new library.Marker({ element: created.root }).setLngLat([lon, lat]).addTo(map))
    }
    for (const [id, marker] of markers.current) {
      if (!seen.has(id)) { marker.remove(); markers.current.delete(id) }
    }
  }

  useEffect(() => {
    if (!ready) return
    let cancelled = false
    import('supercluster').then(({ default: SuperclusterClass }) => {
      if (cancelled) return
      const cluster = new SuperclusterClass<ClusterProps, ClusterProps & { node_total: number }>({
        radius: 44,
        maxZoom: 9,
        map: props => ({ ...props, node_total: props.count }),
        reduce: (accumulated, props) => { accumulated.node_total += (props as ClusterProps & { node_total: number }).node_total },
      })
      const points: Point[] = groups.map(group => ({
        type: 'Feature',
        geometry: { type: 'Point', coordinates: group.coordinates },
        properties: { key: group.key, count: group.nodes.length, tone: groupTone(group) },
      }))
      cluster.load(points)
      index.current = cluster as unknown as Supercluster<ClusterProps, ClusterProps>
      for (const marker of markers.current.values()) marker.remove()
      markers.current.clear()
      const map = mapRef.current
      const wanted = fitKey ?? ''
      if (map && groups.length && fitted.current !== wanted) {
        fitted.current = wanted
        const lons = groups.map(group => group.coordinates[0])
        const lats = groups.map(group => group.coordinates[1])
        map.fitBounds(
          [[Math.min(...lons), Math.min(...lats)], [Math.max(...lons), Math.max(...lats)]],
          { padding: 72, maxZoom: 3.6, duration: 0 },
        )
      }
      render()
      if (popupRef.current) placePopup(popupRef.current)
    })
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, groups, t, locale, fitKey])

  useEffect(() => {
    selectedRef.current = selectedKey
    selectedNodeKeyRef.current = selectedNodeGroupKey
    applyMarkerSelectionStyles()
    if (selectedKey === null) placePopup(null)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedKey, selectedNodeGroupKey])

  useEffect(() => {
    const map = mapRef.current
    const group = groups.find(entry => entry.key === selectedKey)
    if (!map || !group || focusToken === undefined || focusToken === 0) return
    map.easeTo({ center: group.coordinates, zoom: Math.max(map.getZoom(), 4.5) })
    placePopup(group.key)
    // Fly only when explicitly asked via focusToken.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusToken])

  const popupGroup = popup ? groups.find(entry => entry.key === popup.key) : undefined
  const popupWidth = 316
  const leftPos = popup ? Math.max(12, Math.min(popup.x - popupWidth / 2, Math.max(12, boundsSize.width - popupWidth - 12))) : 12
  const flipAbove = popup ? popup.y > Math.max(260, boundsSize.height - 250) : false

  function renderDefaultPickerPopup(group: LocationGroup, close: () => void) {
    const title = [group.cities.join(' / '), group.countries.map(iso => `${flag(iso)} ${countryName(iso, locale)}`).join(' / ')].filter(Boolean).join(' · ') || t('Unnamed location')
    return (
      <div className="py-2">
        <div className="px-3.5 pb-2 pr-8">
          <p className="truncate text-[13px] font-semibold text-ink">{title}</p>
          <p className="text-[11px] text-ink-dim">{t('{ok}/{count} up', { ok: group.health.ok, count: group.nodes.length })}</p>
        </div>
        <ul className="max-h-[260px] divide-y divide-line overflow-y-auto border-t border-line">
          {group.nodes.map(item => {
            const isSelected = selectedNodeId === item.node_id
            const boundCount = item.bound_count ?? item.profile_count ?? 0
            const purity = item.purity_score
            const pTone = purityTone(purity)
            return (
              <li key={item.node_id}>
                <button
                  type="button"
                  onClick={() => { onSelectNode?.(item.node_id); close() }}
                  className={`block w-full cursor-pointer px-3.5 py-2 text-left transition-colors ${isSelected ? 'bg-signal-shade' : 'hover:bg-raised/65'}`}
                >
                  <div className="flex items-center gap-2">
                    <span
                      className={`h-2 w-2 shrink-0 rounded-full ${item.health === 'ok' ? 'bg-ok-dot' : item.health === 'error' ? 'bg-danger-dot' : 'bg-slate-300'}`}
                      aria-hidden="true"
                    />
                    <span className="min-w-0 flex-1 truncate font-medium text-ink" title={item.label}>{item.label}</span>
                    {isSelected && <Check size={13} className="shrink-0 text-signal" aria-hidden="true" />}
                    <span className="shrink-0 font-mono text-[11px] text-ink-dim">
                      {item.health === 'ok' && item.latency_ms != null ? `${item.latency_ms} ms` : item.health === 'error' ? t('Failed') : '—'}
                    </span>
                  </div>
                  <div className="mt-1 flex flex-wrap items-center gap-1.5 text-[11px] text-ink-dim">
                    <span className="font-mono text-ink">{maskIp(item.exit_ip, maskIps)}</span>
                    {purity != null && (
                      <span className={`rounded-full px-1.5 py-0.2 font-mono text-[10px] font-medium ${
                        pTone === 'ok' ? 'bg-ok-shade text-ok' : pTone === 'warn' ? 'bg-warn-shade text-warn' : 'bg-danger-shade text-danger'
                      }`}>
                        {t('Purity {score}', { score: Math.round(purity) })}
                      </span>
                    )}
                    {boundCount > 0 && (
                      <span className="inline-flex items-center gap-1 rounded-full bg-warn-shade px-1.5 py-0.2 text-[10px] font-medium text-warn">
                        <TriangleAlert size={10} aria-hidden="true" />
                        {t('Used by {count}', { count: boundCount })}
                      </span>
                    )}
                    {item.shared_exit_count > 1 && (
                      <span className="rounded-full bg-warn-shade px-1.5 py-0.2 text-[10px] text-warn">
                        {t('Shared IP')}
                      </span>
                    )}
                  </div>
                  <p className="mt-0.5 truncate text-[11px] text-ink-faint">{item.subscription_name} · {nodeProtocolLabel(item, t)}</p>
                </button>
              </li>
            )
          })}
        </ul>
      </div>
    )
  }

  const popupContent = popupGroup
    ? renderPopup
      ? renderPopup(popupGroup, () => placePopup(null))
      : mode === 'picker' && onSelectNode
        ? renderDefaultPickerPopup(popupGroup, () => placePopup(null))
        : null
    : null

  return (
    <div
      className="relative h-full min-h-[320px] w-full overflow-hidden transition-colors duration-200"
      style={{ backgroundColor: isDark ? MAP_PALETTE.dark.sea : MAP_PALETTE.light.sea }}
    >
      <div
        ref={container}
        style={{ position: 'absolute', inset: 0 }}
        aria-label={t('World map of measured exit locations')}
        role="region"
      />
      {popup && popupGroup && popupContent && (
        <div
          className="dialog-in modal-sheet absolute z-20 w-[316px] max-w-[calc(100%-24px)] rounded-xl backdrop-blur-xl"
          style={
            flipAbove
              ? { left: leftPos, bottom: Math.max(12, boundsSize.height - popup.y + 18) }
              : { left: leftPos, top: Math.max(12, popup.y + 18) }
          }
          role="dialog"
          aria-label={t('Nodes at this location')}
        >
          <button
            type="button"
            className="absolute right-2 top-2 cursor-pointer rounded-md p-1 text-ink-faint transition-colors hover:bg-raised hover:text-ink"
            onClick={() => placePopup(null)}
            aria-label={t('Close')}
          >
            <X size={13} />
          </button>
          {popupContent}
        </div>
      )}
      {!ready && !failure && (
        <p className="absolute inset-0 flex items-center justify-center text-ink-dim" role="status">
          {t('Loading map…')}
        </p>
      )}
      {failure && (
        <p className="absolute inset-0 flex items-center justify-center px-6 text-center text-warn" role="alert">
          {t(failure)}
        </p>
      )}
    </div>
  )
}
