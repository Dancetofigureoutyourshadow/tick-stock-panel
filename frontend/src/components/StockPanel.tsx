import { useEffect, useState, useCallback } from 'react'
import { type KlineRow } from '@/lib/api'
import { StockInfoBar } from '@/components/StockInfoBar'
import { StockDailyKChart, getDefaultRange, type StockDailyKChartResult } from '@/components/StockDailyKChart'
import { StockIntradayChart } from '@/components/StockIntradayChart'
import type { ChartMarker, ChartPriceLine, ChartRange } from '@/components/EChartsCandlestick'

interface Props {
  symbol: string
  height?: number
  showIntraday?: boolean
  className?: string
  /** 当用户点击蜡烛选中日期时回调（用于外部自动开启分时图）。 */
  onSelectDate?: (date: string) => void
  /** 外部传入的日期范围 */
  dateRange?: { start: string; end: string }
  markers?: ChartMarker[]
  ranges?: ChartRange[]
  priceLines?: ChartPriceLine[]
  showLimitMarkers?: boolean
  showMarkerToggle?: boolean
}

export { getDefaultRange }

export function StockPanel({
  symbol,
  height = 520,
  showIntraday = true,
  className,
  onSelectDate,
  dateRange: externalDateRange,
  markers,
  ranges,
  priceLines,
  showLimitMarkers = true,
  showMarkerToggle = true,
}: Props) {
  const [linkedPrice, setLinkedPrice] = useState<number | null>(null)
  const [selectedDate, setSelectedDate] = useState<string | null>(null)
  const [dailyResult, setDailyResult] = useState<StockDailyKChartResult | null>(null)

  const dateRange = externalDateRange ?? getDefaultRange()

  const handleDateClick = useCallback((date: string) => {
    setSelectedDate(date)
    onSelectDate?.(date)
  }, [onSelectDate])

  const rows = dailyResult?.rows ?? []
  const stockInfo = dailyResult?.stockInfo
  const rawRows: KlineRow[] = dailyResult?.rawRows ?? []

  // symbol 变化时重置分时相关状态，避免切股后残留旧日期
  useEffect(() => {
    setSelectedDate(null)
    setLinkedPrice(null)
    setDailyResult(null)
  }, [symbol])

  // 当分时开启、无选中日期时，自动选中最新日期
  useEffect(() => {
    if (showIntraday && !selectedDate && rows.length > 0) {
      setSelectedDate(rows[rows.length - 1].date)
    }
  }, [showIntraday, selectedDate, rows])

  const selectedIdx = selectedDate ? rows.findIndex(r => r.date === selectedDate) : -1
  const prevClose = selectedIdx > 0
    ? rows[selectedIdx - 1].close
    : rows.length >= 2
      ? rows[rows.length - 2].close
      : undefined
  if (!symbol) return null

  return (
    <div className={className}>
      <StockInfoBar
        symbol={symbol}
        name={dailyResult?.name}
        stockInfo={stockInfo}
        rows={rawRows}
      />

      <div className="flex gap-3 items-start">
        <StockDailyKChart
          symbol={symbol}
          height={height}
          className="flex-1 min-w-0"
          dateRange={dateRange}
          markers={markers}
          ranges={ranges}
          priceLines={priceLines}
          showLimitMarkers={showLimitMarkers}
          showMarkerToggle={showMarkerToggle}
          linkedPrice={linkedPrice}
          onDateClick={handleDateClick}
          onDataChange={setDailyResult}
          visibleBars={showIntraday ? 40 : 60}
        />

        {showIntraday && selectedDate && (
          <StockIntradayChart
            symbol={symbol}
            date={selectedDate}
            height={height}
            prevClose={prevClose}
            onPriceHover={setLinkedPrice}
            className="flex-1 min-w-0 border-l border-border pl-3"
          />
        )}
      </div>
    </div>
  )
}
