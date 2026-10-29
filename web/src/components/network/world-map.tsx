'use client'

import 'maplibre-gl/dist/maplibre-gl.css'

import { useEffect, useRef, useState, type ReactNode } from 'react'
import type { Map as MapLibreMap, Marker } from 'maplibre-gl'
import type Supercluster from 'supercluster'
import type { PointFeature } from 'supercluster'
import { X } from 'lucide-react'

import { useI18n } from '@/components/i18n-provider'
import { countryName, groupTone, prepareCountries, type LocationGroup } from '@/lib/network-map'

interface Props {
  groups: LocationGroup[]
  selectedKey: string | null
  onSelect: (key: string) => void
  /** Changes when the map should fly to the selected location. */
  focusToken?: number
  /** Changes when the shown set changes (another airport): fit the map to it again. */
  fitKey?: string
  /** The card shown beside a clicked location. */
  renderPopup?: (group: LocationGroup, close: () => void) => ReactNode
}

type Point = PointFeature<{ key: string; count: number; tone: string }>
type ClusterProps = { key: string; count: number; tone: string }

const TONE_CLASS: Record<string, string> = {
  ok: 'border-ok bg-[#e6f4ea] text-ok',
  warn: 'border-warn bg-[#fff4d6] text-warn',
  danger: 'border-danger bg-danger-shade text-danger',
  unknown: 'border-line-strong bg-surface text-ink-dim',
  cluster: 'border-signal bg-signal-shade text-signal-soft',
}

/**
 * The world, drawn from bundled Natural Earth country shapes (public domain): no
 * tile server, no API key, and it works offline. Markers are HTML buttons so
 * they can be focused, read and labelled like any other control.
 */
export function WorldMap({ groups, selectedKey, onSelect, focusToken, fitKey, renderPopup }: Props) {
  const { t, locale } = useI18n()
  const container = useRef<HTMLDivElement>(null)
  const mapRef = useRef<MapLibreMap | null>(null)
  const markers = useRef(new Map<string, Marker>())
  const index = useRef<Supercluster<ClusterProps, ClusterProps> | null>(null)
  const maplibre = useRef<typeof import('maplibre-gl') | null>(null)
  const groupsRef = useRef(groups)
  const selectRef = useRef(onSelect)
  const selectedRef = useRef(selectedKey)
  const fitted = useRef<string | null>(null)
  const popupRef = useRef<string | null>(null)
  const [ready, setReady] = useState(false)
  const [failure, setFailure] = useState<string | null>(null)
  const [popup, setPopup] = useState<{ key: string; x: number; y: number } | null>(null)

  useEffect(() => { groupsRef.current = groups }, [groups])
  useEffect(() => { selectRef.current = onSelect }, [onSelect])

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
        const raw = topojson.feature(topology, (topology as unknown as { objects: { countries: Parameters<typeof topojson.feature>[1] } }).objects.countries)
        // Shapes made for a globe cross ±180°; flattened as-is they paint bands across the map.
        const countries = prepareCountries(raw as unknown as Parameters<typeof prepareCountries>[0])
        map = new library.Map({
          container: container.current,
          style: {
            version: 8,
            sources: { countries: { type: 'geojson', data: countries as unknown as GeoJSON.FeatureCollection, tolerance: 0.2 } },
            layers: [
              { id: 'sea', type: 'background', paint: { 'background-color': '#d6e3ef' } },
              { id: 'land', type: 'fill', source: 'countries', paint: { 'fill-color': '#fbfcfd' } },
              { id: 'borders', type: 'line', source: 'countries', paint: { 'line-color': '#b4bfcc', 'line-width': ['interpolate', ['linear'], ['zoom'], 1, 0.5, 6, 1.2] } },
            ],
          },
          center: [20, 30],
          zoom: 1.3,
          minZoom: 0.8,
          maxZoom: 11,
          // No maxBounds: wider than ±180° pins fitted views to the east edge, and a
          // latitude window shorter than a tall panel makes MapLibre fail to start.
          renderWorldCopies: false,
          dragRotate: false,
          pitchWithRotate: false,
          attributionControl: { compact: true, customAttribution: 'Natural Earth' },
        })
        map.touchZoomRotate.disableRotation()
        map.addControl(new library.NavigationControl({ showCompass: false }), 'top-right')
        map.on('error', () => { /* Shapes are local; nothing to retry. */ })
        map.on('load', () => { if (!cancelled) { mapRef.current = map; setReady(true) } })
        map.on('moveend', () => render())
        map.on('move', () => { if (popupRef.current) placePopup(popupRef.current) })
        // A click on the map itself (not a marker) closes the card.
        map.on('click', () => placePopup(null))
      } catch (err) {
        console.error('Map failed to start', err)
        if (!cancelled) setFailure('The map cannot be drawn here (WebGL unavailable). Use the location list instead.')
      }
    }
    start()
    return () => {
      cancelled = true
      for (const marker of currentMarkers.values()) marker.remove()
      currentMarkers.clear()
      map?.remove()
      mapRef.current = null
    }
    // render and placePopup are stable through refs.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  /**
   * MapLibre positions a marker by setting `transform` on its element. Any hover
   * scale on that same element replaces the translate, so the marker jumped to
   * the corner and animated back. The outer element stays MapLibre's; only the
   * inner button scales.
   */
  function markerElement(id: string, count: number, tone: string, label: string, cluster: boolean): { root: HTMLDivElement; button: HTMLButtonElement } {
    const root = document.createElement('div')
    const button = document.createElement('button')
    button.type = 'button'
    const size = Math.min(44, (cluster ? 26 : 20) + Math.round(Math.log2(count + 1) * 4))
    button.style.width = button.style.height = `${size}px`
    button.className = `flex cursor-pointer items-center justify-center rounded-full border font-mono text-[11px] font-semibold shadow-[0_0_0_3px_rgb(255_255_255/0.9),0_1px_4px_rgb(27_31_36/0.25)] transition-transform duration-100 hover:scale-110 focus-visible:scale-110 ${TONE_CLASS[cluster ? 'cluster' : tone]}`
    button.textContent = String(count)
    button.setAttribute('aria-label', label)
    button.title = label
    button.dataset.key = id
    root.appendChild(button)
    return { root, button }
  }

  function render() {
    const map = mapRef.current
    const library = maplibre.current
    if (!map || !library || !index.current) return
    const bounds = map.getBounds()
    const zoom = Math.round(map.getZoom())
    const items = index.current.getClusters([Math.max(-180, bounds.getWest()), bounds.getSouth(), Math.min(180, bounds.getEast()), bounds.getNorth()], zoom)
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
      if (id === selectedRef.current) created.button.classList.add('ring-2', 'ring-signal')
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
        radius: 46,
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
        // Show every measured exit of the shown set once; refreshes keep the view.
        fitted.current = wanted
        const lons = groups.map(group => group.coordinates[0])
        const lats = groups.map(group => group.coordinates[1])
        map.fitBounds([[Math.min(...lons), Math.min(...lats)], [Math.max(...lons), Math.max(...lats)]], { padding: 80, maxZoom: 3.5, duration: 0 })
      }
      render()
      if (popupRef.current) placePopup(popupRef.current)
    })
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, groups, t, locale, fitKey])

  useEffect(() => {
    selectedRef.current = selectedKey
    for (const [id, marker] of markers.current) {
      const button = marker.getElement().firstElementChild
      button?.classList.toggle('ring-2', id === selectedKey)
      button?.classList.toggle('ring-signal', id === selectedKey)
    }
    if (selectedKey === null) placePopup(null)
  }, [selectedKey])

  useEffect(() => {
    const map = mapRef.current
    const group = groups.find(entry => entry.key === selectedKey)
    if (!map || !group || focusToken === undefined) return
    map.easeTo({ center: group.coordinates, zoom: Math.max(map.getZoom(), 4.5) })
    placePopup(group.key)
    // Fly only when asked (selection from the list), not on every data refresh.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusToken])

  const popupGroup = popup ? groups.find(entry => entry.key === popup.key) : undefined
  const width = container.current?.clientWidth ?? 0
  return (
    <div className="relative h-full min-h-[320px] w-full overflow-hidden bg-canvas">
      {/* Inline position: MapLibre's own .maplibregl-map rule sets position: relative,
          which would collapse an absolutely-positioned container to zero height. */}
      <div ref={container} style={{ position: 'absolute', inset: 0 }} aria-label={t('World map of measured exit locations')} role="region" />
      {popup && popupGroup && renderPopup && (
        <div
          className="absolute z-10 w-[300px] max-w-[calc(100%-24px)] rounded-lg border border-line bg-surface shadow-[0_8px_24px_rgb(27_31_36/0.16)]"
          style={{ left: Math.max(12, Math.min(popup.x - 150, width - 312)), top: popup.y + 18 }}
          role="dialog"
          aria-label={t('Nodes at this location')}
        >
          <button type="button" className="absolute right-1.5 top-1.5 rounded p-1 text-ink-faint hover:bg-raised hover:text-ink" onClick={() => placePopup(null)} aria-label={t('Close')}><X size={13} /></button>
          {renderPopup(popupGroup, () => placePopup(null))}
        </div>
      )}
      {!ready && !failure && <p className="absolute inset-0 flex items-center justify-center text-ink-dim" role="status">{t('Loading map…')}</p>}
      {failure && <p className="absolute inset-0 flex items-center justify-center px-6 text-center text-warn" role="alert">{t(failure)}</p>}
    </div>
  )
}
