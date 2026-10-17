import type { ChartMarker } from '@/components/EChartsCandlestick'
import type { PaperFill } from '@/lib/api'

/** fills 必须来自当前账户的成交台账；ts 是入账时间，历史回放应使用 date 交易日。 */
export function buildPaperTradeMarkers(fills: readonly PaperFill[], symbol: string | null): ChartMarker[] {
  if (!symbol) return []
  return fills
    .filter(fill => fill.symbol === symbol
      && (fill.kind == null || fill.kind === 'fill')
      && (fill.side === 'buy' || fill.side === 'sell')
      && /^\d{4}-\d{2}-\d{2}$/.test(fill.date)
      && Number.isFinite(fill.qty) && fill.qty! > 0
      && Number.isFinite(fill.price) && fill.price! > 0)
    .slice()
    .sort((a, b) => a.date.localeCompare(b.date) || a.seq - b.seq)
    .map(fill => {
      const buy = fill.side === 'buy'
      const label = buy ? '买入' : '卖出'
      return {
        date: fill.date,
        kind: buy ? 'buy' : 'sell',
        label,
        color: buy ? '#C74040' : '#2D9B65',
        // 日K采用前复权价，成交价是原始价。只按交易日锚定蜡烛上下沿，勿混用价格坐标。
        description: `${fill.date} ${label}\n成交价 ${fill.price!.toFixed(2)} 元 · ${fill.qty} 股`,
      }
    })
}
