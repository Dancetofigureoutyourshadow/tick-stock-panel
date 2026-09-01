import { useEffect, useState, useCallback, useRef, useMemo } from 'react'
import { X } from 'lucide-react'
import { type DailyKlinePeriod, type KlineRow, type FinancialMetricRecord } from '@/lib/api'
import { StockInfoBar } from '@/components/StockInfoBar'
import { StockDailyKChart, getDefaultRange, type StockDailyKChartResult } from '@/components/StockDailyKChart'
import { StockIntradayChart } from '@/components/StockIntradayChart'
import { StockDepth5Panel } from '@/components/StockDepth5Panel'
import { StockTransactionsPanel } from '@/components/StockTransactionsPanel'
import { useFocusMarketStream } from '@/lib/useFocusMarketStream'
import { useFinancialMetrics } from '@/lib/useFinancials'
import { useCapabilities } from '@/lib/useSharedQueries'
import type { ChartMarker, ChartPriceLine, ChartRange } from '@/components/EChartsCandlestick'
import {
  loadInfoFields,
  saveInfoFields,
  buildInfoExtColumnsParam,
  type ColumnConfig,
} from '@/lib/stock-info-fields'

interface Props {
  symbol: string
  period?: DailyKlinePeriod
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
  /** 加监控回调 (传入后信息条显示 RadioTower 图标) */
  onMonitor?: () => void
  onPriceDoubleClick?: (price: number, currentPrice: number) => void
  /** 自选操作（传入后信息条显示 Star 图标） */
  inWatchlist?: boolean
  onAddToWatchlist?: (groupId: string | null) => void
  onRemoveFromWatchlist?: () => void
  watchlistPending?: boolean
  /** 分时图自动刷新间隔(ms)。undefined = 不轮询。个股对话框盘中实时刷新时传入。 */
  refetchIntervalMs?: number
  /** 当前日分时右侧显示实时五档盘口。 */
  showDepth5?: boolean
  /** 只渲染信息条, 隐藏图表 (用于分时 tab 共享信息条) */
  infoBarOnly?: boolean
}

export { getDefaultRange }

export function StockPanel({
  symbol,
  period = 'day',
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
  onMonitor,
  onPriceDoubleClick,
  inWatchlist,
  onAddToWatchlist,
  onRemoveFromWatchlist,
  watchlistPending,
  refetchIntervalMs,
  showDepth5 = false,
  infoBarOnly = false,
}: Props) {
  const [linkedPrice, setLinkedPrice] = useState<number | null>(null)
  const [selectedDate, setSelectedDate] = useState<string | null>(null)
  const [intradayDismissed, setIntradayDismissed] = useState(false)
  const [dailyResult, setDailyResult] = useState<StockDailyKChartResult | null>(null)
  // 信息条指标配置提升到此层：同时供 StockInfoBar 渲染与 StockDailyKChart 请求 ext 数据
  const [fields, setFields] = useState<ColumnConfig[]>(loadInfoFields)
  const extColumns = useMemo(() => buildInfoExtColumnsParam(fields), [fields])

  const handleFieldsChange = useCallback((next: ColumnConfig[]) => {
    setFields(next)
    saveInfoFields(next)
  }, [])

  // 财务指标：仅当信息条配置含可见的财务字段且用户具备财务数据能力 (financial) 时才请求
  // 无能力时跳过请求, 避免后端抛 CapabilityDenied (403) 导致 free/starter 档弹错误提示
  const { data: caps } = useCapabilities()
  const hasFinancialCap = !!caps?.capabilities?.['financial']
  const hasFinanceField = useMemo(
    () => fields.some(f => f.visible && f.source.type === 'builtin'
      && ['eps', 'bps', 'roe', 'pe_ttm', 'pb', 'gross_margin', 'net_margin', 'debt_ratio', 'revenue_yoy', 'net_income_yoy'].includes(f.source.key)),
    [fields],
  )
  const financials = useFinancialMetrics(hasFinanceField && hasFinancialCap ? symbol : undefined)

  const dateRange = externalDateRange ?? getDefaultRange()

  const handleDateClick = useCallback((date: string) => {
    setSelectedDate(date)
    setIntradayDismissed(false)
    onSelectDate?.(date)
  }, [onSelectDate])

  const rows = dailyResult?.rows ?? []
  const stockInfo = dailyResult?.stockInfo
  const rawRows: KlineRow[] = dailyResult?.rawRows ?? []

  // symbol 变化时重置分时相关状态，避免切股后残留旧日期。
  // 注意：必须跳过首次挂载——重开弹窗时 kline 命中 react-query 缓存，
  // 子组件 onDataChange effect（先于父 effect 执行）会把 dailyResult 置为有效数据，
  // 若此处再无条件清空，会把刚加载的数据抹掉，导致信息条整行消失。
  const prevSymbol = useRef<string | null>(symbol)
  useEffect(() => {
    if (prevSymbol.current === symbol) return
    prevSymbol.current = symbol
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
  const focusStream = useFocusMarketStream({
    symbol,
    date: selectedDate,
    enabled: showDepth5 && showIntraday && !intradayDismissed,
  })
  if (!symbol) return null

  // 财务指标最新一期（metrics 按 period_end 排序，取首项）
  const financialMetrics: FinancialMetricRecord | undefined = financials.data?.data?.[0]

  return (
    <div className={className}>
      <StockInfoBar
        symbol={symbol}
        name={dailyResult?.name}
        stockInfo={stockInfo}
        rows={rawRows}
        fields={fields}
        onFieldsChange={handleFieldsChange}
        financialMetrics={financialMetrics}
        onMonitor={onMonitor}
        inWatchlist={inWatchlist}
        onAddToWatchlist={onAddToWatchlist}
        onRemoveFromWatchlist={onRemoveFromWatchlist}
        watchlistPending={watchlistPending}
      />

      {infoBarOnly ? null : (
      <div className={`flex items-start gap-3 ${showDepth5 && focusStream.isCurrentDate ? 'min-w-max' : ''}`}>
        <div className={showDepth5 && focusStream.isCurrentDate ? 'flex min-w-[1040px] shrink-0 items-start gap-3' : 'contents'}>
        <StockDailyKChart
          symbol={symbol}
          period={period}
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
          onPriceDoubleClick={onPriceDoubleClick}
          onDataChange={setDailyResult}
          visibleBars={showIntraday ? 40 : 60}
          extColumns={extColumns}
          refetchIntervalMs={refetchIntervalMs}
          />

        {showIntraday && selectedDate && !intradayDismissed && (
          <div className="flex min-w-0 flex-1 flex-col gap-3 border-l border-border pl-3">
            <div className="relative min-w-0">
              <button
                onClick={() => setIntradayDismissed(true)}
                className="absolute -left-1.5 -top-1.5 z-10 flex h-5 w-5 items-center justify-center rounded-full border border-border bg-surface text-muted shadow-sm transition-colors hover:text-foreground hover:bg-elevated"
                title="收起分时图"
                aria-label="收起分时图"
              >
                <X className="h-3 w-3" />
              </button>
              <StockIntradayChart
                symbol={symbol}
                date={selectedDate}
                height={height}
                prevClose={prevClose}
                onPriceHover={setLinkedPrice}
                onPriceDoubleClick={onPriceDoubleClick}
                currentPrice={rows[rows.length - 1]?.close}
                dailyOhlc={selectedIdx >= 0 ? rows[selectedIdx] : undefined}
                priceLines={priceLines}
                refetchIntervalMs={refetchIntervalMs}
                live={focusStream.streamEnabled || refetchIntervalMs != null}
                disablePolling={focusStream.streamEnabled}
              />
            </div>
            {showDepth5 && focusStream.isCurrentDate && (
              <div className="w-full shrink-0">
                <StockDepth5Panel
                  snapshot={focusStream.depth}
                  status={focusStream.depthStatus}
                  error={focusStream.depthError}
                  provider={focusStream.depthProvider}
                  updatedAt={focusStream.updatedAt}
                  className="min-h-0"
                />
              </div>
            )}
          </div>
        )}
        </div>

        {showDepth5 && focusStream.isCurrentDate && focusStream.marketPhase !== 'closed' && (
          <StockTransactionsPanel
            rows={focusStream.transactions}
            status={focusStream.transactionsStatus}
            error={focusStream.transactionsError}
            updatedAt={focusStream.transactionsUpdatedAt}
            prevClose={prevClose}
            height={height}
            className="w-[320px] shrink-0"
          />
        )}

      </div>
      )}
    </div>
  )
}
