// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, expect, it, vi } from 'vitest'
import { ScreenerTable } from './ScreenerTable'

vi.mock('@/components/WatchlistAddMenu', () => ({ WatchlistAddMenu: () => null }))
vi.mock('@/components/DimensionMembersDialog', () => ({
  DimensionMembersDialog: () => null, dimensionKindForSourceField: () => null,
}))
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
let cleanup = async () => {}
afterEach(async () => { await cleanup() })

function render(expired = false) {
  const host = document.createElement('div')
  const root = createRoot(host)
  cleanup = async () => { await act(async () => root.unmount()) }
  const onBuy = vi.fn()
  const onPreview = vi.fn()
  const row = { symbol: '603995.SH', name: '甬金股份', auction__price: 23, _expired: expired }
  act(() => root.render(<ScreenerTable
    rows={[row]}
    columns={[
      { id: 'symbol', label: '股票', visible: true, source: { type: 'builtin', key: 'symbol' } },
      { id: 'auction', label: '竞价', visible: true, source: { type: 'ext', configId: 'auction', fieldName: 'price' },
        extDisplay: { displayMode: 'text', snapshotRender: 'line', chartWidth: 160, chartHeight: 60 } },
    ]}
    strategyIdToName={{}} symbolStrategyMap={new Map()} activeStrategy={null}
    watchlistSet={new Set()} onPreview={onPreview} onBuy={onBuy}
    onAddToWatchlist={vi.fn()} onRemoveFromWatchlist={vi.fn()} watchlistPending={false}
    extSeriesData={{ 'auction.price': { '603995.SH': [['09:25:00', 22], ['09:30:00', 23]] } }}
  />))
  return { host, onBuy, onPreview, row }
}

it('扩展迷你图与本地买入入口、K线预览同时可用', () => {
  const { host, onBuy, onPreview, row } = render()
  expect(host.querySelector('svg[width="160"][height="60"] polyline')).not.toBeNull()
  const buy = host.querySelector('[aria-label="买入 603995.SH 并加入自选"]') as HTMLButtonElement
  act(() => buy.click())
  expect(onBuy).toHaveBeenCalledWith(row)
  const preview = [...host.querySelectorAll('button')].find(b => b.textContent?.includes('甬金股份'))!
  act(() => preview.click())
  expect(onPreview).toHaveBeenCalledWith('603995.SH', '甬金股份', expect.any(Array))
})

it('失效股票保留扩展图但不提供买入入口', () => {
  const { host } = render(true)
  expect(host.querySelector('[aria-label="买入 603995.SH 并加入自选"]')).toBeNull()
  expect(host.querySelector('svg[width="160"] polyline')).not.toBeNull()
})
