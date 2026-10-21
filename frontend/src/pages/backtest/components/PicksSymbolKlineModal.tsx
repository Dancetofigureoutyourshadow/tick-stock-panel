import { useEffect, useMemo, useRef } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { X } from 'lucide-react'
import { StockPanel } from '@/components/StockPanel'
import { NavPager, NavWrapToast } from '@/components/NavPager'
import type { StrategyBacktestResult, StrategyBacktestTrade } from '@/lib/api'
import { navItemKey, type NavItem } from '@/lib/listNav'
import { useListNav } from '@/lib/useListNav'
import { fmtPct, priceColorClass } from '@/lib/format'
import { boardTag } from '@/lib/board'
import { useDialogBackdrop } from '@/lib/useDialogBackdrop'
import { buildTradeChartOverlays } from './tradeChartOverlays'

interface Props {
  /** 选中的标的 (null = 关闭) */
  symbol: string | null
  /** 回测结果, 用于取该标的所有交易与聚合统计 */
  result: StrategyBacktestResult | null
  /** 回测有效区间 (图默认展示范围) */
  periodStart: string
  periodEnd: string
  /** 有序候选列表 (策略分析全部标的): 提供后支持左右键/顶栏按钮切标的 */
  navList?: NavItem[]
  /** 切标的回调: 收到目标 symbol/name, 由调用方更新选中状态 */
  onNavigate?: (symbol: string, name?: string) => void
  onClose: () => void
}

/** 「策略分析」行的标的级K线弹窗 (单笔回放见 TradeKlineModal) */
export function PicksSymbolKlineModal({ symbol, result, periodStart, periodEnd, navList, onNavigate, onClose }: Props) {
  const backdrop = useDialogBackdrop(onClose)

  // onClose 只有 ESC 用; onNavigate 由 useListNav 内部承接 (支持内联 lambda)
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose

  const nav = useListNav<NavItem>({
    items: navList ?? [],
    keyOf: navItemKey,
    currentKey: symbol,
    onNavigate: n => onNavigate?.(n.symbol, n.name),
    wrapHints: { head: '已到榜首', tail: '已到末尾' },
  })

  useEffect(() => {
    if (!symbol) return
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onCloseRef.current()
    }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [symbol])

  const view = useMemo(() => {
    if (!symbol || !result) return { trades: [] as StrategyBacktestTrade[], stat: null, name: '' }
    const trades = result.trades.filter(t => t.symbol === symbol)
    return {
      trades,
      stat: result.per_symbol_stats.find(p => p.symbol === symbol) ?? null,
      name: trades.find(t => t.name)?.name ?? '',
    }
  }, [symbol, result])
  const { trades, stat, name } = view

  const { ranges, priceLines } = useMemo(() => buildTradeChartOverlays(trades), [trades])

  const tag = symbol ? boardTag(symbol) : ''
  const dateRange = useMemo(() => ({ start: periodStart, end: periodEnd }), [periodStart, periodEnd])

  return (
    <AnimatePresence>
      {symbol && (
        <div className="fixed inset-0 z-[70] flex items-center justify-center">
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.15 }}
            className="absolute inset-0 bg-black/60 backdrop-blur-sm"
            {...backdrop}
          />
          <motion.div
            initial={{ opacity: 0, scale: 0.95, y: 12 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.97, y: 8 }}
            transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
            className="relative flex max-h-[95vh] w-[92vw] max-w-[1200px] flex-col overflow-hidden rounded-card border border-border bg-base shadow-2xl"
          >
            <div className="shrink-0 border-b border-border px-5 py-3">
              <div className="flex flex-wrap items-start justify-between gap-4">
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="font-mono text-sm font-semibold text-foreground">{symbol}</span>
                    {name && <span className="truncate text-sm text-foreground">{name}</span>}
                    {tag && (
                      <span className="rounded border border-accent/30 bg-accent/10 px-1.5 py-0.5 text-[10px] text-accent">
                        {tag}
                      </span>
                    )}
                    <span className="rounded border border-accent/30 bg-accent/10 px-1.5 py-0.5 text-[10px] text-accent">标的回放</span>

                    {/* 切标的: 上一只 / n·N / 下一只 */}
                    <NavPager nav={nav} prevLabel="上一只" nextLabel="下一只" />
                  </div>
                  <div className="mt-1 text-[11px] text-muted">
                    回测 {periodStart} ~ {periodEnd} · {trades.length} 笔交易
                  </div>
                  <div className="mt-1 text-[10px] text-muted/70">
                    持仓区间底色 · 买入价(红) · 卖出价(绿)
                  </div>
                </div>

                <div className="flex shrink-0 items-center gap-3 text-xs">
                  {stat && (
                    <div className="flex items-center gap-3">
                      <div className="text-right">
                        <div className="text-muted">总收益</div>
                        <div className={`num font-semibold ${priceColorClass(stat.total_return)}`}>{fmtPct(stat.total_return)}</div>
                      </div>
                      <div className="text-right">
                        <div className="text-muted">胜率</div>
                        <div className="num text-secondary">{fmtPct(stat.win_rate)}</div>
                      </div>
                      <div className="text-right">
                        <div className="text-muted">最佳 / 最差</div>
                        <div className="num">
                          <span className="text-bull">{fmtPct(stat.best)}</span>
                          <span className="text-muted"> / </span>
                          <span className="text-bear">{fmtPct(stat.worst)}</span>
                        </div>
                      </div>
                    </div>
                  )}
                  <button
                    onClick={onClose}
                    className="rounded-btn p-1 text-secondary transition-colors hover:bg-elevated hover:text-foreground"
                    aria-label="关闭标的回放"
                  >
                    <X className="h-4 w-4" />
                  </button>
                </div>
              </div>
            </div>

            <div className="flex-1 overflow-auto p-3 sm:p-4">
              <div className="rounded border border-border/50 bg-base/30 p-3">
              <StockPanel
                symbol={symbol}
                height={420}
                dailyKlineFlex="flex-[1.4]"
                dateRange={dateRange}
                ranges={ranges}
                priceLines={priceLines}
                showLimitMarkers={false}
                showMarkerToggle={false}
                showIntraday={false}
                visibleBars="all"
              />
              </div>
            </div>

            {/* 首↔尾循环弱提示 */}
            <NavWrapToast message={nav.wrapMsg} />
          </motion.div>
        </div>
      )}
    </AnimatePresence>
  )
}
