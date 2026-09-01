import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Loader2, RefreshCw } from 'lucide-react'
import { api, type MinuteKlineFrequency } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { EChartsCandlestick, type ChartPriceLine, type OHLC } from '@/components/EChartsCandlestick'

interface Props {
  symbol: string
  days: number
  frequency: MinuteKlineFrequency
  height?: number
  priceLines?: ChartPriceLine[]
  onPriceDoubleClick?: (price: number, currentPrice: number) => void
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : '分钟 K 数据获取失败'
}

function toOHLC(sessions: Awaited<ReturnType<typeof api.klineMinuteRange>>['sessions']): OHLC[] {
  return sessions.flatMap(session => session.rows
    .filter(row => row.datetime && Number.isFinite(Number(row.open)) && Number.isFinite(Number(row.close)))
    .map(row => ({
      date: row.datetime.replace('T', ' ').slice(0, 16),
      open: Number(row.open),
      high: Number(row.high),
      low: Number(row.low),
      close: Number(row.close),
      volume: Number(row.volume ?? 0),
    })))
}

export function StockMinuteKChart({
  symbol,
  days,
  frequency,
  height = 480,
  priceLines,
  onPriceDoubleClick,
}: Props) {
  const query = useQuery({
    queryKey: QK.klineMinuteRange(symbol, days, frequency),
    queryFn: () => api.klineMinuteRange(symbol, days, frequency),
    enabled: !!symbol,
  })
  const data = useMemo(() => toOHLC(query.data?.sessions ?? []), [query.data?.sessions])

  if (query.isLoading) {
    return (
      <div className="flex items-center justify-center gap-2 text-xs text-muted" style={{ height }}>
        <Loader2 className="h-4 w-4 animate-spin text-accent" />
        正在加载 {days} 日分钟 K…
      </div>
    )
  }

  if (query.error) {
    return (
      <div className="flex flex-col items-center justify-center gap-3 text-xs" style={{ height }}>
        <span className="text-danger">{errorMessage(query.error)}</span>
        <button
          type="button"
          onClick={() => { void query.refetch() }}
          className="inline-flex items-center gap-1.5 rounded-btn border border-border bg-elevated px-3 py-1.5 text-secondary hover:text-foreground"
        >
          <RefreshCw className="h-3.5 w-3.5" />
          重新加载
        </button>
      </div>
    )
  }

  if (data.length === 0) {
    return (
      <div className="flex items-center justify-center text-xs text-muted" style={{ height }}>
        本地暂无可展示的分钟 K 数据，请先使用“同步”补齐分钟数据
      </div>
    )
  }

  return (
    <div>
      <EChartsCandlestick
        data={data}
        height={height}
        visibleBars={Math.min(data.length, 120)}
        showMA={false}
        showInfoBar
        showInfoDate
        priceLines={priceLines}
        onPriceDoubleClick={onPriceDoubleClick}
      />
      <div className="mt-1 px-1 text-[10px] text-muted">
        {frequency} · {query.data?.sessions.length ?? 0} 个交易日 · {data.length} 根
      </div>
    </div>
  )
}
