// Copies MapLibre's web worker next to the static export. MapLibre resolves the
// worker relative to its own module URL, which bundling changes, so the page
// sets the URL explicitly to this copy. Generated on every dev/build from the
// installed package, so the worker can never drift from the library version.
import { copyFileSync, mkdirSync } from 'node:fs'
import { createRequire } from 'node:module'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = dirname(dirname(fileURLToPath(import.meta.url)))
const require = createRequire(import.meta.url)
const dist = dirname(require.resolve('maplibre-gl/package.json'))
const target = join(root, 'public', 'vendor', 'maplibre')
mkdirSync(target, { recursive: true })
copyFileSync(join(dist, 'dist', 'maplibre-gl-worker.mjs'), join(target, 'maplibre-gl-worker.mjs'))
console.log('Copied the MapLibre worker into public/vendor/maplibre')
