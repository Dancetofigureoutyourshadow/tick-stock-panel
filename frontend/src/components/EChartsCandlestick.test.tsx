// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { EChartsCandlestick, type OHLC } from './EChartsCandlestick'

const chartState = vi.hoisted(() => ({ zoom: { start: 0, end: 100 } }))

const chart = vi.hoisted(() => ({
  handlers: {} as Record<string, (event?: any) => void>,
  setOption: vi.fn(),
  on: vi.fn(),
  off: vi.fn(),
  dispatchAction: vi.fn(),
  resize: vi.fn(),
  dispose: vi.fn(),
  getZr: () => ({ on: vi.fn(), off: vi.fn() }),
  getOption: () => ({ dataZoom: [{ ...chartState.zoom }] }),
  containPixel: () => false,
  convertFromPixel: () => [0, 0],
}))

vi.mock('echarts', () => ({ init: () => chart }))

// 超过 COMPACT_THRESHOLD(60): 切标的后 dataZoom 一响就会走紧凑态转换, 进而重写 markPoint
const DAYS = Array.from({ length: 62 }, (_, i) =>
  new Date(Date.UTC(2024, 0, 1 + i)).toISOString().slice(0, 10))
const rows = (base: number): OHLC[] =>
  DAYS.map(date => ({ date, open: base, high: base + 1, low: base - 1, close: base, volume: 1 }))

let host: HTMLDivElement
let root: ReturnType<typeof createRoot>

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  chart.handlers = {}
  chart.setOption.mockClear()
  chartState.zoom = { start: 0, end: 100 }
  chart.on.mockImplementation((name: string, callback: (event?: any) => void) => {
    chart.handlers[name] = callback
  })
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
})

afterEach(async () => {
  await act(async () => root.unmount())
  host.remove()
  vi.unstubAllGlobals()
})

/** 最后一次 setOption 里 K 线的 markPoint (买卖箭头来源) */
function lastMarkPoint(): string {
  const option = chart.setOption.mock.calls.at(-1)?.[0] as any
  const k = option?.series?.find((s: any) => s.name === 'K')
  return JSON.stringify(k?.markPoint ?? null)
}

/** 模拟竖虚线命中某根 K 线 (鼠标在数据区内移动时 echarts 派发的事件) */
async function hoverCandle(index: number) {
  await act(async () => { chart.handlers.updateAxisPointer?.({ axesInfo: [{ value: index }] }) })
}

/** 最后一次写入主图 y 轴边界的 setOption (初始全量或 dataZoom 后的增量) */
function lastYAxisBounds(): { min?: number; max?: number } {
  for (let i = chart.setOption.mock.calls.length - 1; i >= 0; i -= 1) {
    const yAxis = (chart.setOption.mock.calls[i]?.[0] as any)?.yAxis
    const axis = Array.isArray(yAxis) ? yAxis[0] : yAxis
    if (axis && typeof axis.min === 'number' && typeof axis.max === 'number') {
      return { min: axis.min, max: axis.max }
    }
  }
  return {}
}

const SPLIT = 31
const splitRows: OHLC[] = DAYS.map((date, i) => {
  const base = i < SPLIT ? 10 : 100
  return { date, open: base, high: base + 1, low: base - 1, close: base, volume: 1 }
})
const splitLines = [
  { value: 10.5, label: '早段', start: DAYS[2], end: DAYS[8] },
  { value: 100.5, label: '晚段', start: DAYS[52], end: DAYS[58] },
]

it('切标的后响应 dataZoom 仍用当前标的的买卖标记, 不回退到上一只', async () => {
  const render = async (symbol: string, data: OHLC[], markerLabel: string, markerDate: string) => {
    await act(async () => root.render(
      <EChartsCandlestick
        data={data}
        markers={[{ date: markerDate, kind: 'buy', label: markerLabel }]}
        symbol={symbol}
        height={400}
        showInfoBar={false}
        visibleBars="all"
        activeIndicators={['vol']}
      />,
    ))
  }

  await render('600000', rows(10), 'A-BUY', DAYS[10])
  await render('000001', rows(100), 'B-SELL', DAYS[20])

  // 切换标的后图表实例被复用, dataZoom 监听仍是最初注册的那个; 首次 setOption 已是当前标的
  expect(lastMarkPoint()).toContain('B-SELL')

  await act(async () => { chart.handlers.dataZoom?.() })

  expect(lastMarkPoint()).toContain('B-SELL')
  expect(lastMarkPoint()).not.toContain('A-BUY')
})

it('切股后鼠标未离开图表, 竖虚线重新命中即恢复「至今/周期」', async () => {
  const render = async (symbol: string) => {
    await act(async () => root.render(
      <EChartsCandlestick data={rows(10)} symbol={symbol} height={400} visibleBars="all" />,
    ))
  }

  await render('600000')
  const surface = host.firstElementChild as HTMLElement
  await act(async () => { surface.dispatchEvent(new MouseEvent('mouseenter')) })
  await hoverCandle(30)
  expect(host.textContent).toContain('至今')

  // 切股: 上一只的悬停上下文作废, 清掉「至今/周期」
  await render('000001')
  expect(host.textContent).not.toContain('至今')

  // 鼠标没离开图表区 (只是切股), 竖虚线重新命中即恢复, 不靠 mouseenter
  await hoverCandle(20)
  expect(host.textContent).toContain('至今')
  expect(host.textContent).toContain('周期')

  // 移出图表区 → 重新收起
  await act(async () => { surface.dispatchEvent(new MouseEvent('mouseleave')) })
  expect(host.textContent).not.toContain('至今')
})

it('dataZoom 后只按可见窗口内的成交价线定界, 窗口外的价格线不再压扁 K 线', async () => {
  await act(async () => root.render(
    <EChartsCandlestick
      data={splitRows}
      priceLines={splitLines}
      height={400}
      showInfoBar={false}
      showMA={false}
      activeIndicators={[]}
      visibleBars="all"
    />,
  ))

  // 全区间: 早段/晚段价格线都在窗口内
  let bounds = lastYAxisBounds()
  expect(bounds.min).toBeLessThan(9)
  expect(bounds.max).toBeGreaterThan(101)

  // 缩放到早段: 晚段的 100.5 不应再把 y 轴撑到三位数
  chartState.zoom = { start: 0, end: 45 }
  await act(async () => { chart.handlers.dataZoom?.() })
  bounds = lastYAxisBounds()
  expect(bounds.min).toBeGreaterThan(8)
  expect(bounds.max).toBeLessThan(12)

  // 缩放到晚段: 早段的 10.5 不应再把 y 轴压到两位数
  chartState.zoom = { start: 90, end: 100 }
  await act(async () => { chart.handlers.dataZoom?.() })
  bounds = lastYAxisBounds()
  expect(bounds.min).toBeGreaterThan(90)
})

it('初始可见窗口按 visibleBars 定界, 全宽价格线始终参与', async () => {
  await act(async () => root.render(
    <EChartsCandlestick
      data={splitRows}
      priceLines={[...splitLines, { value: 50, label: '全宽线' }]}
      height={400}
      showInfoBar={false}
      showMA={false}
      activeIndicators={[]}
      visibleBars={20}
    />,
  ))

  // 初始只看最后 20 根 (100 附近): 早段 10.5 不参与, 但全宽 50 始终参与
  const bounds = lastYAxisBounds()
  expect(bounds.min).toBeGreaterThan(40)
  expect(bounds.min).toBeLessThan(50)
  expect(bounds.max).toBeGreaterThan(101)
})
