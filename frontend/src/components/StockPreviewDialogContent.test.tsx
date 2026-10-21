// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { StockPreviewDialogContent } from './StockPreviewDialogContent'
import { TradeKlineModal } from '@/pages/backtest/components/TradeKlineModal'
import { PicksSymbolKlineModal } from '@/pages/backtest/components/PicksSymbolKlineModal'
import type { ChartMarker } from './EChartsCandlestick'
import type { StrategyBacktestResult } from '@/lib/api'

const panel = vi.hoisted(() => vi.fn())
vi.mock('./StockPanel', () => ({
  getDefaultRange: () => ({ start: '2026-09-01', end: '2026-10-16' }),
  StockPanel: (props: unknown) => { panel(props); return null },
}))
vi.mock('@/lib/useCustomSignalNames', () => ({ useCustomSignalNames: () => ({}) }))
vi.mock('@/lib/useSharedQueries', () => ({ usePreferences: () => ({ data: {} }) }))
vi.mock('@/lib/useQuoteStream', () => ({ setFocusSymbol: vi.fn(), clearFocusSymbol: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: {
  watchlistList: async () => ({ symbols: [] }),
  watchlistGroups: async () => ({ groups: [] }),
  monitorRulesList: async () => ({ rules: [] }),
} }))
vi.mock('@/extensions/ExtensionSlot', () => ({ ExtensionSlot: () => null }))
vi.mock('./StockMultiDayIntradayChart', () => ({ StockMultiDayIntradayChart: () => null }))

let host: HTMLDivElement
let root: ReturnType<typeof createRoot>
let qc: QueryClient
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  panel.mockClear()
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
})
afterEach(async () => {
  await act(async () => root.unmount())
  qc.clear()
  host.remove()
})

it('把账户成交标记传入日K，切股及清空后不保留上一只的标记', async () => {
  const render = async (symbol: string, markers: ChartMarker[]) => {
    await act(async () => root.render(
      <QueryClientProvider client={qc}>
        <StockPreviewDialogContent {...{ symbol, markers, onClose: vi.fn() }} />
      </QueryClientProvider>,
    ))
  }
  const markers: ChartMarker[] = [{ date: '2026-10-09', kind: 'buy', label: '买入' }]
  await render('603995.SH', markers)
  expect(panel.mock.calls.at(-1)?.[0]).toMatchObject({ symbol: '603995.SH', markers })
  await render('002012.SZ', [])
  expect(panel.mock.calls.at(-1)?.[0]).toMatchObject({ symbol: '002012.SZ', markers: [] })
})

it('回测交易和标的详情沿用自选详情的弹窗尺寸及图表比例', async () => {
  const render = async (content: React.ReactNode) => {
    await act(async () => root.render(<QueryClientProvider client={qc}>{content}</QueryClientProvider>))
  }
  const sizing = () => host.querySelector('div.relative.flex.flex-col')!.className.split(' ')
    .filter(cls => /^(w-\[|max-w-\[|max-h-\[)/.test(cls)).sort()
  await render(<StockPreviewDialogContent symbol="603236.SH" onClose={vi.fn()} />)
  const previewSizing = sizing()
  const { height, dailyKlineFlex } = panel.mock.calls.at(-1)![0] as { height: number; dailyKlineFlex: string }
  const trade = { symbol: '603236.SH', name: '移远通信', entry_date: '2026-10-15', exit_date: '2026-10-21',
    entry_price: 68, exit_price: 81.6, pnl_pct: 0.2, duration: 4, exit_reason: 'signal' }
  await render(<TradeKlineModal trade={trade} onClose={vi.fn()} />)
  expect(sizing()).toEqual(previewSizing)
  expect(panel.mock.calls.at(-1)?.[0]).toMatchObject({ height, dailyKlineFlex })
  await render(<PicksSymbolKlineModal symbol={trade.symbol} result={null}
    periodStart="2026-10-01" periodEnd="2026-10-21" onClose={vi.fn()} />)
  expect(sizing()).toEqual(previewSizing)
  expect(panel.mock.calls.at(-1)?.[0]).toMatchObject({ height, dailyKlineFlex })
})

it('策略分析按标的展示所有持仓区间和买卖价，切股后清除旧交易且只显示日K', async () => {
  const trade = { symbol: '603236.SH', entry_date: '2026-10-15', exit_date: '2026-10-21',
    entry_price: 68, exit_price: 81.6, pnl_pct: 0.2, duration: 4, exit_reason: 'signal' }
  const result = { trades: [trade, { ...trade, entry_date: '2026-09-01', exit_date: '2026-09-07',
    entry_price: 60, exit_price: 65 }, { ...trade, symbol: '002012.SZ', entry_price: 10, exit_price: 11 }],
    per_symbol_stats: [] } as unknown as StrategyBacktestResult
  const render = async (symbol: string) => {
    await act(async () => root.render(<PicksSymbolKlineModal {...{ symbol, result }}
      periodStart="2026-09-01" periodEnd="2026-10-21" onClose={vi.fn()} />))
  }
  await render(trade.symbol)
  const props = panel.mock.calls.at(-1)![0] as { ranges: unknown[]; priceLines: unknown[] }
  expect(props).toMatchObject({ showIntraday: false, visibleBars: 'all' })
  expect(props.ranges).toEqual([
    { start: '2026-10-15', end: '2026-10-21', label: '持仓区间', color: 'rgba(59,130,246,0.07)' },
    { start: '2026-09-01', end: '2026-09-07', label: '持仓区间', color: 'rgba(59,130,246,0.07)' },
  ])
  expect(props.priceLines).toHaveLength(4)
  expect(props.priceLines.slice(0, 2)).toEqual([
    { start: trade.entry_date, end: trade.exit_date, value: 68, label: '买入价 68.00', color: '#C74040' },
    { start: trade.entry_date, end: trade.exit_date, value: 81.6, label: '卖出价 81.60', color: '#2D9B65' },
  ])
  await render('002012.SZ')
  expect(panel.mock.calls.at(-1)?.[0]).toMatchObject({ priceLines: [
    { value: 10, label: '买入价 10.00' }, { value: 11, label: '卖出价 11.00' },
  ], showIntraday: false })
  await render('600000.SH')
  expect(panel.mock.calls.at(-1)?.[0]).toMatchObject({ ranges: [], priceLines: [] })
})
