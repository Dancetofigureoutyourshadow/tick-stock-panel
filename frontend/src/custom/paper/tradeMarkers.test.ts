import { expect, it } from 'vitest'
import type { PaperFill } from '@/lib/api'
import { buildPaperTradeMarkers } from './tradeMarkers'

const fill = (overrides: Partial<PaperFill> = {}): PaperFill => ({
  seq: 1, ts: '2026-10-16T18:00:00+08:00', date: '2026-09-25',
  order_id: 'order-1', symbol: '603995.SH', asset_type: 'stock',
  kind: 'fill', side: 'buy', qty: 200, price: 25.38, ...overrides,
})

it('历史回放按交易日标买卖，保留同日多笔成交，不把原始成交价放到前复权价格坐标', () => {
  const markers = buildPaperTradeMarkers([
    fill({ seq: 3, date: '2026-10-09', side: 'sell', price: 27.12 }),
    fill({ seq: 2, price: 25.4 }),
    fill(),
  ], '603995.SH')
  expect(markers.map(m => [m.date, m.kind, m.label])).toEqual([
    ['2026-09-25', 'buy', '买入'], ['2026-09-25', 'buy', '买入'], ['2026-10-09', 'sell', '卖出'],
  ])
  expect(markers[0].description).toContain('25.38 元 · 200 股')
  expect(markers[2].description).toContain('27.12 元')
  for (const marker of markers) expect(marker.price).toBeUndefined()
})

it('只显示当前股票的真实成交，排除除权、冲正、空价格和零数量', () => {
  const invalid = [
    fill({ symbol: '002012.SZ' }),
    fill({ kind: 'corp_action', side: 'corp_action' }),
    fill({ kind: 'corp_action' }),
    fill({ price: undefined }), fill({ price: NaN }), fill({ price: 0 }),
    fill({ qty: undefined }), fill({ qty: 0 }), fill({ qty: -200 }),
    fill({ date: '' }),
  ]
  expect(buildPaperTradeMarkers(invalid, '603995.SH')).toEqual([])
  expect(buildPaperTradeMarkers([fill({ kind: undefined })], '603995.SH')).toHaveLength(1)
})

it('账户或股票切换时重新取对应成交，关闭及无成交时清空', () => {
  const accountA = [fill()]
  const accountB = [fill({ side: 'sell', date: '2026-10-15' })]
  expect(buildPaperTradeMarkers(accountA, '603995.SH')[0].kind).toBe('buy')
  expect(buildPaperTradeMarkers(accountB, '603995.SH').map(m => m.kind)).toEqual(['sell'])
  expect(buildPaperTradeMarkers(accountA, '002012.SZ')).toEqual([])
  expect(buildPaperTradeMarkers(accountA, null)).toEqual([])
  expect(buildPaperTradeMarkers([], '603995.SH')).toEqual([])
})
