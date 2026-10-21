import type { ChartPriceLine, ChartRange } from '@/components/EChartsCandlestick'
import type { StrategyBacktestTrade } from '@/lib/api'
import { fmtPrice } from '@/lib/format'

/** 两个回放入口使用相同的持仓区间和成交价样式，价格线只覆盖对应持仓期。 */
export function buildTradeChartOverlays(trades: StrategyBacktestTrade[]) {
  const ranges: ChartRange[] = []
  const priceLines: ChartPriceLine[] = []
  for (const trade of trades) {
    const start = String(trade.entry_date).slice(0, 10)
    const end = String(trade.exit_date).slice(0, 10)
    ranges.push({ start, end, label: '持仓区间', color: 'rgba(59,130,246,0.07)' })
    priceLines.push(
      { value: Number(trade.entry_price), label: `买入价 ${fmtPrice(trade.entry_price)}`,
        color: '#C74040', start, end },
      { value: Number(trade.exit_price), label: `卖出价 ${fmtPrice(trade.exit_price)}`,
        color: '#2D9B65', start, end },
    )
  }
  return { ranges, priceLines }
}
