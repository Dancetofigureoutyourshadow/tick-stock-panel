// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { StockPreviewDialogContent } from './StockPreviewDialogContent'
import type { ChartMarker } from './EChartsCandlestick'

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
