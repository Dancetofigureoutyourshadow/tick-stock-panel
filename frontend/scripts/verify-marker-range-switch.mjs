import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import * as echarts from 'echarts'

// Exercise the component's retained zoom listener across renders, with actual
// ECharts coordinates. Only React scheduling and browser host nodes are mocked.
const require = createRequire(import.meta.url)
const { build } = require(require.resolve('esbuild', { paths: [require.resolve('vite')] }))
const root = fileURLToPath(new URL('..', import.meta.url))
const slots = []
let cursor = 0
let effects = []
let chart
let chartCreations = 0
const changed = (a, b) => !a || !b || a.length !== b.length || a.some((v, i) => !Object.is(v, b[i]))
const runtime = {
  useRef(value) { const i = cursor++; return slots[i] ??= { current: value } },
  useState(value) { const i = cursor++; slots[i] ??= { value: typeof value === 'function' ? value() : value }; return [slots[i].value, () => {}] },
  useMemo(fn, deps) { const i = cursor++; if (changed(slots[i]?.deps, deps)) slots[i] = { deps, value: fn() }; return slots[i].value },
  useCallback(fn, deps) { return runtime.useMemo(() => fn, deps) },
  useEffect(fn, deps) {
    const i = cursor++
    if (changed(slots[i]?.deps, deps)) effects.push(() => { slots[i]?.cleanup?.(); slots[i] = { deps, cleanup: fn() } })
  },
  init() { chartCreations++; chart = echarts.init(null, null, { renderer: 'svg', ssr: true, width: 960, height: 480 }); return chart },
}
globalThis.__markerRangeRuntime = runtime
globalThis.window = { addEventListener() {}, removeEventListener() {} }
globalThis.ResizeObserver = class { observe() {} disconnect() {} }
const compiled = await build({
  entryPoints: [resolve(root, 'src/components/EChartsCandlestick.tsx')],
  absWorkingDir: root, bundle: true, write: false, platform: 'node', format: 'cjs', packages: 'external',
  alias: { '@': resolve(root, 'src') },
  plugins: [{ name: 'chart-host', setup(b) {
    b.onResolve({ filter: /^(react|echarts)$/ }, args => ({ path: args.path, namespace: 'host' }))
    b.onLoad({ filter: /.*/, namespace: 'host' }, args => ({ contents: args.path === 'react'
      ? 'export const {useRef,useState,useMemo,useCallback,useEffect}=globalThis.__markerRangeRuntime'
      : 'export const {init}=globalThis.__markerRangeRuntime' }))
  } }],
})
const mod = { exports: {} }
new Function('require', 'module', 'exports', compiled.outputFiles[0].text)(require, mod, mod.exports)
const { EChartsCandlestick } = mod.exports
const hostNode = { addEventListener() {}, removeEventListener() {}, innerHTML: '' }
function attach(node) {
  if (!node || typeof node !== 'object') return
  if (node.ref) node.ref.current = hostNode
  for (const child of [node.props?.children].flat()) attach(child)
}
function render(data, markers, extra = {}) {
  cursor = 0; effects = []
  attach(EChartsCandlestick({ data, markers, height: 480, visibleBars: 'all', showMA: false, ...extra }))
  for (const effect of effects) effect()
}
const dates = Array.from({ length: 240 }, (_, i) => new Date(Date.UTC(2025, 0, 1 + i)).toISOString().slice(0, 10))
const rows = dates.map((date, i) => ({ date, open: 20 + i / 10, close: 20.2 + i / 10, low: 19.8 + i / 10, high: 20.5 + i / 10, volume: 100 }))
const marker = { date: dates[200], kind: 'buy', above: true, label: '板', color: '#FACC15' }
function verify(data, expectedMarkers) {
  const points = chart.getOption().series.find(series => series.name === 'K').markPoint?.data ?? []
  assert.equal(points.length, expectedMarkers.length)
  for (const [i, m] of expectedMarkers.entries()) {
    const index = data.findIndex(row => row.date === m.date)
    assert.equal(points[i].coord[0], index, 'marker must follow the current date index after range switch')
    assert.equal(points[i].coord[1], m.price ?? data[index].high, 'marker must follow the current candle price')
    const pixel = chart.convertToPixel({ xAxisIndex: 0, yAxisIndex: 0 }, points[i].coord)
    const candlePixel = chart.convertToPixel({ xAxisIndex: 0, yAxisIndex: 0 }, [m.date, m.price ?? data[index].high])
    assert.deepEqual(pixel, candlePixel)
  }
}
try {
  const halfYear = rows.slice(120)
  render(halfYear, [marker]); verify(halfYear, [marker])
  render(rows, [marker]); verify(rows, [marker])
  assert.equal(chartCreations, 1, 'range changes must reuse the existing chart and listener')
  chart.dispatchAction({ type: 'dataZoom', start: 75, end: 100 }); verify(rows, [marker])
  chart.dispatchAction({ type: 'dataZoom', start: 0, end: 100 }); verify(rows, [marker])
  const refreshed = rows.map(row => ({ ...row, high: row.high + 1 }))
  const newMarker = { ...marker, date: dates[180] }
  render(refreshed, [newMarker]); verify(refreshed, [newMarker])
  chart.dispatchAction({ type: 'dataZoom', start: 75, end: 100 }); verify(refreshed, [newMarker])
  render(halfYear, [marker]); verify(halfYear, [marker])
  chart.dispatchAction({ type: 'dataZoom', start: 0, end: 100 }); verify(halfYear, [marker])
  render(halfYear, [marker], { showMarkers: false }); verify(halfYear, [])
  chart.dispatchAction({ type: 'dataZoom', start: 75, end: 100 }); verify(halfYear, [])
  console.log('PASS: range expansion/contraction, zoom both ways, refreshed prices/markers and hidden markers remain aligned.')
} finally {
  for (const slot of slots) slot?.cleanup?.()
  delete globalThis.__markerRangeRuntime
}
