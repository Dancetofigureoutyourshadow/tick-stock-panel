import { useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { ArrowDownToLine, ArrowUpFromLine, ArrowUpRight, CalendarClock, Pencil, Plus, RotateCcw, Search, Settings2, ShoppingCart, Trash2 } from 'lucide-react'
import { api, type Lot, type PortfolioPosition, type PortfolioSettings, type PortfolioTrade } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { fmtPct, fmtPrice, priceColorClass } from '@/lib/format'
import { PageHeader } from '@/components/PageHeader'
import { Modal } from '@/components/Modal'
import { DatePicker } from '@/components/DatePicker'
import { DateShortcuts } from '@/components/DateShortcuts'
import { StockPreviewDialog, toNavItems } from '@/components/StockPreviewDialog'
import { boardTag } from '@/components/stock-table/primitives'
import type { ChartMarker, ChartPriceLine } from '@/components/EChartsCandlestick'
import type { IntradayChartMarker } from '@/components/EChartsIntraday'

const emptyDraft = (): Lot => ({
  id: '',
  symbol: '',
  qty: 0,
  cost_price: 0,
  buy_date: '',
  target_pct: 0,
  stop_pct: 0,
  remind_date: '',
  lead_days: 1,
})

/** 剩余天数单元格: 到期日 − 今天, 可为负 = 已超期; 无到期日 → — */
function RemainingDays({ remind }: { remind?: string | null }) {
  if (!remind) return <span className="text-muted/60">—</span>
  const remindMs = new Date(`${remind}T00:00:00`).getTime()
  const today = new Date()
  today.setHours(0, 0, 0, 0)
  const n = Math.floor((remindMs - today.getTime()) / 86400000)
  if (n < 0) return <span className="font-mono text-warning">已超期{-n}天</span>
  return <span className="font-mono text-secondary">{n}天</span>
}

/** 成本 vs 现价的盈亏% (纯价格比例, 无数量参与) */
function CostPnL({ close, cost }: { close?: number; cost: number }) {
  if (close == null || !(cost > 0)) return <span className="text-muted/60">—</span>
  const pnl = (close - cost) / cost
  return <span className={cn('font-mono', priceColorClass(pnl))}>{fmtPct(pnl)}</span>
}

const accountKeys = [QK.portfolioSummary, QK.portfolioPositions, QK.portfolioTransactions] as const

function shanghaiTradeMinute(createdAt: string): string {
  const value = new Date(createdAt)
  if (Number.isNaN(value.getTime())) return '15:00'
  const parts = new Intl.DateTimeFormat('en-GB', {
    timeZone: 'Asia/Shanghai',
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
  }).formatToParts(value)
  const hour = parts.find(part => part.type === 'hour')?.value ?? '15'
  const minute = parts.find(part => part.type === 'minute')?.value ?? '00'
  return `${hour}:${minute}`
}

function portfolioTradeDescription(
  trade: PortfolioTrade,
  position?: PortfolioPosition,
): string {
  const side = trade.side === 'buy' ? '买入' : '卖出'
  const strategy = position?.source_strategy_name ? ` · ${position.source_strategy_name}` : ''
  return `${side} ${trade.quantity} 股 @ ¥${trade.price}${strategy}`
}

function AccountStatus({ position }: { position: PortfolioPosition }) {
  if (!position.has_exit_rules) return <span className="rounded border border-danger/30 bg-danger/10 px-1.5 py-0.5 text-[10px] text-danger">无退出规则</span>
  if (position.status === 'pending_sell') return <span className="rounded border border-warning/30 bg-warning/10 px-1.5 py-0.5 text-[10px] text-warning">待卖出</span>
  return <span className="rounded border border-bull/30 bg-bull/10 px-1.5 py-0.5 text-[10px] text-bull">持有中</span>
}

function PositionTable({
  title,
  rows,
  onSell,
  onContinue,
  onPreview,
}: {
  title: string
  rows: PortfolioPosition[]
  onSell: (position: PortfolioPosition) => void
  onContinue: (position: PortfolioPosition) => void
  onPreview: (symbol: string) => void
}) {
  return (
    <section className="space-y-2">
      <div className="flex items-center gap-2 text-sm font-medium text-foreground"><span>{title}</span><span className="text-[10px] font-normal text-muted">{rows.length} 个批次</span></div>
      {rows.length === 0 ? <div className="rounded-xl border border-dashed border-border px-4 py-8 text-center text-xs text-muted">暂无记录</div> : (
        <div className="overflow-x-auto rounded-xl border border-border bg-surface/40">
          <table className="w-full text-left text-xs">
            <thead><tr className="border-b border-border/60 bg-surface/60 text-[10px] text-muted">
              <th className="px-4 py-2 font-medium">标的 / 策略</th><th className="px-2 py-2 text-right font-medium">数量</th><th className="px-2 py-2 text-right font-medium">可卖</th><th className="px-2 py-2 text-right font-medium">成本</th><th className="px-2 py-2 text-right font-medium">现价</th><th className="px-2 py-2 text-right font-medium">市值</th><th className="px-2 py-2 text-right font-medium">未实现盈亏</th><th className="px-3 py-2" />
            </tr></thead>
            <tbody>{rows.map(position => {
              const pnl = Number(position.unrealized_pnl)
              return <tr key={position.id} className="border-b border-border/40 last:border-0">
                <td className="px-4 py-2.5"><div className="flex items-center gap-1.5"><button type="button" onClick={() => onPreview(position.symbol)} title="打开日 K 与分时详情" className="group inline-flex items-center gap-1.5 rounded text-left hover:text-accent focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent"><span className="font-mono text-foreground group-hover:text-accent">{position.symbol}</span><span className="text-secondary group-hover:text-accent">{position.name}</span></button><AccountStatus position={position} /></div><div className="mt-1 text-[10px] text-muted">{position.source_strategy_name} · {position.buy_trade_date}{position.pending_reason ? ` · ${position.pending_reason}` : ''}</div></td>
                <td className="px-2 py-2.5 text-right font-mono">{position.remaining_qty}</td>
                <td className="px-2 py-2.5 text-right font-mono text-secondary">{position.available_qty}</td>
                <td className="px-2 py-2.5 text-right font-mono">¥{position.remaining_cost}</td>
                <td className="px-2 py-2.5 text-right font-mono text-secondary">{position.current_price ? `¥${position.current_price}` : '—'}</td>
                <td className="px-2 py-2.5 text-right font-mono">¥{position.market_value}</td>
                <td className={cn('px-2 py-2.5 text-right font-mono', priceColorClass(pnl))}>{pnl >= 0 ? '+' : ''}¥{position.unrealized_pnl}</td>
                <td className="px-3 py-2.5"><div className="flex justify-end gap-1">
                  {position.status === 'pending_sell' && <button onClick={() => onContinue(position)} className="rounded-btn border border-border px-2 py-1 text-[10px] text-secondary hover:bg-elevated">继续持有</button>}
                  <button onClick={() => onSell(position)} disabled={position.available_qty <= 0} title={position.available_qty <= 0 ? 'T+1：当日买入暂不可卖' : '确认卖出'} className="rounded-btn border border-warning/40 bg-warning/10 px-2 py-1 text-[10px] text-warning disabled:cursor-not-allowed disabled:opacity-40">确认卖出</button>
                </div></td>
              </tr>
            })}</tbody>
          </table>
        </div>
      )}
    </section>
  )
}

export function Lots() {
  const qc = useQueryClient()
  const [cashType, setCashType] = useState<'deposit' | 'withdrawal' | null>(null)
  const [selling, setSelling] = useState<PortfolioPosition | null>(null)
  const [showSettings, setShowSettings] = useState(false)
  const [previewSymbol, setPreviewSymbol] = useState<string | null>(null)
  const summary = useQuery({ queryKey: QK.portfolioSummary, queryFn: api.portfolioSummary })
  const positions = useQuery({ queryKey: QK.portfolioPositions, queryFn: () => api.portfolioPositions() })
  const transactions = useQuery({ queryKey: QK.portfolioTransactions, queryFn: api.portfolioTransactions })
  const settings = useQuery({ queryKey: QK.portfolioSettings, queryFn: api.portfolioSettings })
  const allPositions = positions.data?.positions ?? []
  const pending = allPositions.filter(position => position.status === 'pending_sell')
  const holding = allPositions.filter(position => position.status !== 'pending_sell')
  const portfolioNavItems = useMemo(() => {
    const bySymbol = new Map<string, { symbol: string; name?: string }>()
    for (const position of positions.data?.positions ?? []) {
      if (!bySymbol.has(position.symbol)) {
        bySymbol.set(position.symbol, { symbol: position.symbol, name: position.name || undefined })
      }
    }
    return toNavItems(Array.from(bySymbol.values()))
  }, [positions.data?.positions])
  const previewOverlay = useMemo(() => {
    if (!previewSymbol) return null
    const symbolPositions = (positions.data?.positions ?? []).filter(
      position => position.symbol === previewSymbol,
    )
    const positionById = new Map(symbolPositions.map(position => [position.id, position]))
    const symbolTrades = (transactions.data?.trades ?? []).filter(
      trade => trade.symbol === previewSymbol,
    )
    const markerLevels = new Map<string, number>()
    const dailyMarkers: ChartMarker[] = symbolTrades.map(trade => {
      const key = `${trade.trade_date}:${trade.side}`
      const level = markerLevels.get(key) ?? 0
      markerLevels.set(key, level + 1)
      const isBuy = trade.side === 'buy'
      return {
        date: trade.trade_date,
        kind: isBuy ? 'buy' : 'sell',
        price: Number(trade.price),
        label: isBuy ? 'B' : 'S',
        circle: true,
        lockToPrice: true,
        offsetY: isBuy ? 18 + level * 20 : -18 - level * 20,
        description: portfolioTradeDescription(trade, positionById.get(trade.position_id)),
      }
    })
    const intradayMarkers: IntradayChartMarker[] = symbolTrades.map(trade => ({
      date: trade.trade_date,
      time: shanghaiTradeMinute(trade.created_at),
      kind: trade.side,
      price: Number(trade.price),
      description: portfolioTradeDescription(trade, positionById.get(trade.position_id)),
    }))
    const quantity = symbolPositions.reduce((sum, position) => sum + position.remaining_qty, 0)
    const remainingCost = symbolPositions.reduce(
      (sum, position) => sum + Number(position.remaining_cost),
      0,
    )
    const averageCost = quantity > 0 ? remainingCost / quantity : 0
    const priceLines: ChartPriceLine[] = averageCost > 0
      ? [{ value: averageCost, label: `持仓成本 ${averageCost.toFixed(2)}`, color: '#F59E0B' }]
      : []
    return {
      name: symbolPositions[0]?.name ?? '',
      dailyMarkers,
      intradayMarkers,
      priceLines,
    }
  }, [positions.data?.positions, previewSymbol, transactions.data?.trades])

  const invalidate = () => {
    for (const queryKey of accountKeys) qc.invalidateQueries({ queryKey })
  }
  const continueHolding = useMutation({
    mutationFn: (id: string) => api.portfolioContinueHolding(id),
    onSuccess: invalidate,
  })
  const reversedIds = new Set((transactions.data?.cash_flows ?? []).map(flow => flow.reverses_id).filter(Boolean))
  const reverse = useMutation({
    mutationFn: (id: string) => api.portfolioCashReverse(id, '页面冲正'),
    onSuccess: invalidate,
  })
  const summaryCards: Array<[string, string | undefined]> = [
    ['可用现金', summary.data?.available_cash], ['持仓市值', summary.data?.market_value],
    ['总资产', summary.data?.total_assets], ['净入金', summary.data?.net_deposits],
    ['已实现盈亏', summary.data?.realized_pnl], ['未实现盈亏', summary.data?.unrealized_pnl],
    ['总盈亏', summary.data?.total_pnl],
  ]

  return <div className="flex h-full flex-col">
    <PageHeader title="持仓账户" subtitle="单一人民币本地模拟账户 · 日线 A 股 · 不连接券商" right={<div className="flex gap-1.5"><button onClick={() => setCashType('deposit')} className="inline-flex h-8 items-center gap-1 rounded-btn border border-bull/30 bg-bull/10 px-2.5 text-xs text-bull"><ArrowDownToLine className="h-3.5 w-3.5" />入金</button><button onClick={() => setCashType('withdrawal')} className="inline-flex h-8 items-center gap-1 rounded-btn border border-warning/30 bg-warning/10 px-2.5 text-xs text-warning"><ArrowUpFromLine className="h-3.5 w-3.5" />出金</button><button onClick={() => setShowSettings(true)} className="inline-flex h-8 items-center gap-1 rounded-btn border border-border px-2.5 text-xs text-secondary"><Settings2 className="h-3.5 w-3.5" />设置</button></div>} />
    <div className="flex-1 overflow-y-auto px-5 py-4"><div className="mx-auto max-w-7xl space-y-6">
      <section className="space-y-2"><div className="text-sm font-medium text-foreground">账户总览</div><div className="grid grid-cols-2 gap-2 md:grid-cols-4 xl:grid-cols-7">{summaryCards.map(([label, value]) => {
        const n = Number(value ?? 0); const isPnl = label.includes('盈亏')
        return <div key={label} className="rounded-xl border border-border bg-surface/40 p-3"><div className="text-[10px] text-muted">{label}</div><div className={cn('mt-1 font-mono text-sm font-medium', isPnl ? priceColorClass(n) : 'text-foreground')}>{value == null ? '—' : `${isPnl && n > 0 ? '+' : ''}¥${value}`}</div></div>
      })}</div></section>
      <PositionTable title="待卖出" rows={pending} onSell={setSelling} onContinue={position => continueHolding.mutate(position.id)} onPreview={setPreviewSymbol} />
      <PositionTable title="当前持仓" rows={holding} onSell={setSelling} onContinue={position => continueHolding.mutate(position.id)} onPreview={setPreviewSymbol} />

      <section className="space-y-2"><div className="text-sm font-medium text-foreground">成交记录</div><div className="overflow-x-auto rounded-xl border border-border bg-surface/40"><table className="w-full text-left text-xs"><thead><tr className="border-b border-border/60 text-[10px] text-muted"><th className="px-4 py-2">日期</th><th className="px-2 py-2">方向 / 标的</th><th className="px-2 py-2 text-right">价格</th><th className="px-2 py-2 text-right">数量</th><th className="px-2 py-2 text-right">佣金</th><th className="px-2 py-2 text-right">印花税</th><th className="px-4 py-2 text-right">已实现盈亏</th></tr></thead><tbody>{(transactions.data?.trades ?? []).map(trade => <tr key={trade.id} className="border-b border-border/40 last:border-0"><td className="px-4 py-2 text-muted">{trade.trade_date}</td><td className="px-2 py-2"><span className={trade.side === 'buy' ? 'text-bull' : 'text-warning'}>{trade.side === 'buy' ? '买入' : '卖出'}</span> · <span className="font-mono">{trade.symbol}</span></td><td className="px-2 py-2 text-right font-mono">¥{trade.price}</td><td className="px-2 py-2 text-right font-mono">{trade.quantity}</td><td className="px-2 py-2 text-right font-mono">¥{trade.commission}</td><td className="px-2 py-2 text-right font-mono">¥{trade.stamp_tax}</td><td className="px-4 py-2 text-right font-mono">{trade.realized_pnl == null ? '—' : `¥${trade.realized_pnl}`}</td></tr>)}</tbody></table>{!transactions.data?.trades.length && <div className="px-4 py-8 text-center text-xs text-muted">暂无成交</div>}</div></section>

      <section className="space-y-2"><div className="text-sm font-medium text-foreground">资金流水</div><div className="overflow-x-auto rounded-xl border border-border bg-surface/40"><table className="w-full text-left text-xs"><thead><tr className="border-b border-border/60 text-[10px] text-muted"><th className="px-4 py-2">时间</th><th className="px-2 py-2">类型</th><th className="px-2 py-2 text-right">金额</th><th className="px-2 py-2">备注</th><th className="px-4 py-2" /></tr></thead><tbody>{(transactions.data?.cash_flows ?? []).map(flow => <tr key={flow.id} className="border-b border-border/40 last:border-0"><td className="px-4 py-2 text-muted">{new Date(flow.created_at).toLocaleString('zh-CN')}</td><td className="px-2 py-2">{flow.type === 'deposit' ? '入金' : flow.type === 'withdrawal' ? '出金' : '冲正'}</td><td className={cn('px-2 py-2 text-right font-mono', Number(flow.cash_delta) >= 0 ? 'text-bull' : 'text-warning')}>{Number(flow.cash_delta) > 0 ? '+' : ''}¥{flow.cash_delta}</td><td className="px-2 py-2 text-muted">{flow.note || '—'}</td><td className="px-4 py-2 text-right">{flow.type !== 'reversal' && !reversedIds.has(flow.id) && <button onClick={() => reverse.mutate(flow.id)} disabled={reverse.isPending} className="inline-flex items-center gap-1 rounded-btn border border-border px-2 py-1 text-[10px] text-secondary"><RotateCcw className="h-3 w-3" />冲正</button>}</td></tr>)}</tbody></table>{!transactions.data?.cash_flows.length && <div className="px-4 py-8 text-center text-xs text-muted">暂无资金流水</div>}</div></section>

      <LegacyLotsSection />
    </div></div>
    {cashType && <CashDialog type={cashType} onClose={() => setCashType(null)} onSaved={invalidate} />}
    {selling && <SellDialog position={selling} onClose={() => setSelling(null)} onSaved={invalidate} />}
    {showSettings && settings.data && <AccountSettingsDialog initial={settings.data} onClose={() => setShowSettings(false)} onSaved={() => { qc.invalidateQueries({ queryKey: QK.portfolioSettings }); invalidate() }} />}
    <StockPreviewDialog
      symbol={previewSymbol}
      name={previewOverlay?.name}
      navList={portfolioNavItems}
      onNavigate={symbol => setPreviewSymbol(symbol)}
      onClose={() => setPreviewSymbol(null)}
      markers={previewOverlay?.dailyMarkers}
      intradayMarkers={previewOverlay?.intradayMarkers}
      priceLines={previewOverlay?.priceLines}
    />
  </div>
}

function LegacyLotsSection() {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const [editing, setEditing] = useState<Lot | null>(null) // null=关闭
  const [confirmId, setConfirmId] = useState<string | null>(null)
  const [previewSymbol, setPreviewSymbol] = useState<string | null>(null)
  const resetTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  const lotsQuery = useQuery({ queryKey: QK.lots, queryFn: api.lotsList })
  const lots = lotsQuery.data?.lots ?? []

  const allSymbols = useMemo(() => Array.from(new Set(lots.map(l => l.symbol))), [lots])
  const namesQuery = useQuery({
    queryKey: ['instrument-names', allSymbols.join(',')],
    queryFn: () => api.instrumentNames(allSymbols),
    enabled: allSymbols.length > 0,
    staleTime: 300000,
  })
  const symbolNames = namesQuery.data?.names ?? {}

  // 9999 哨兵: 未记买入日期的排最后
  const sortedLots = useMemo(() => {
    return [...lots].sort((a, b) => (a.buy_date ?? '9999-12-31').localeCompare(b.buy_date ?? '9999-12-31'))
  }, [lots])
  const lotsNavItems = useMemo(
    () => toNavItems(sortedLots.map(l => ({ symbol: l.symbol, name: symbolNames[l.symbol] }))),
    [sortedLots, symbolNames],
  )

  const dailyQuery = useQuery({
    queryKey: QK.lotsKline(allSymbols.join(',')),
    queryFn: () => api.klineDailyBatch(allSymbols, 5),
    enabled: allSymbols.length > 0,
    staleTime: 60000,
  })
  const lastPrices = useMemo(() => {
    const m: Record<string, number> = {}
    for (const [sym, rows] of Object.entries(dailyQuery.data?.data ?? {})) {
      const last = rows[rows.length - 1]
      if (last?.close != null) m[sym] = Number(last.close)
    }
    return m
  }, [dailyQuery.data])

  const del = useMutation({
    mutationFn: api.lotDelete,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.lots })
      qc.invalidateQueries({ queryKey: QK.monitorRules })
      setConfirmId(null)
    },
  })

  // 删除: 第一次进确认态, 第二次真删, 3 秒后自动复位 (与监控中心一致)
  const handleClickDelete = (id: string) => {
    if (confirmId === id) {
      if (resetTimer.current) clearTimeout(resetTimer.current)
      setConfirmId(null)
      del.mutate(id)
    } else {
      setConfirmId(id)
      if (resetTimer.current) clearTimeout(resetTimer.current)
      resetTimer.current = setTimeout(() => setConfirmId(null), 3000)
    }
  }

  return (
    <section className="space-y-3 border-t border-border/60 pt-5">
      <div className="flex items-center justify-between">
        <div><div className="text-sm font-medium text-foreground">旧批次</div><div className="mt-0.5 text-[10px] text-muted">与新账户现金完全隔离，继续沿用原提醒规则 · {lots.length} 个批次</div></div>
            <button
              onClick={() => setEditing(emptyDraft())}
              className="inline-flex h-9 items-center gap-1.5 rounded-btn border border-accent/30 bg-accent/10 px-3 text-xs font-medium text-accent transition-colors hover:bg-accent/15 cursor-pointer"
            >
              <Plus className="h-3.5 w-3.5" />新增批次
            </button>
      </div>
      <div className="space-y-4">

          {lotsQuery.isLoading ? (
            <div className="rounded-xl border border-dashed border-border px-6 py-12 text-center text-xs text-muted">加载中…</div>
          ) : lotsQuery.isError ? (
            <div className="rounded-xl border border-dashed border-border px-6 py-12 text-center">
              <div className="text-xs text-danger">批次加载失败</div>
              <button
                onClick={() => lotsQuery.refetch()}
                className="mt-2 rounded-btn border border-border px-3 py-1 text-[11px] text-secondary hover:bg-elevated cursor-pointer"
              >
                重试
              </button>
            </div>
          ) : lots.length === 0 ? (
            <div className="rounded-xl border border-dashed border-border px-6 py-12 text-center">
              <div className="text-sm text-muted">还没有批次</div>
              <div className="mt-1 text-[11px] text-muted/70">记录一笔买入后, 系统会按成本价 ± 止盈/止损% 生成价格监控; 填了到期日则自动生成到期提醒。这里只用于生成提醒, 不是持仓记账。</div>
            </div>
          ) : (
            <div className="overflow-hidden rounded-xl border border-border bg-surface/40 shadow-sm">
              <div className="overflow-x-auto">
                <table className="w-full text-left text-xs">
                  <thead>
                    <tr className="border-b border-border/60 bg-surface/60 text-[10px] uppercase tracking-wide text-muted">
                      <th className="px-4 py-2 font-medium">标的</th>
                      <th className="px-2 py-2 font-medium text-right">数量(参考)</th>
                      <th className="px-2 py-2 font-medium text-right">成本价</th>
                      <th className="px-2 py-2 font-medium text-right">现价</th>
                      <th className="px-2 py-2 font-medium text-right">盈亏%</th>
                      <th className="px-2 py-2 font-medium text-right">止盈%</th>
                      <th className="px-2 py-2 font-medium text-right">止损%</th>
                      <th className="px-2 py-2 font-medium">买入日期</th>
                      <th className="px-2 py-2 font-medium text-right">剩余天数</th>
                      <th className="px-2 py-2 font-medium">到期提醒</th>
                      <th className="px-3 py-2" />
                    </tr>
                  </thead>
                  <tbody>
                    {sortedLots.map(lot => (
                      <tr key={lot.id} className="border-b border-border/40 last:border-0 hover:bg-elevated/40">
                        <td className="px-4 py-2.5">
                          <button
                            onClick={() => setPreviewSymbol(lot.symbol)}
                            title={`查看 ${lot.symbol} 日K`}
                            className="inline-flex items-center gap-1.5 min-w-0 hover:bg-elevated/50 rounded px-0.5 py-0.5 transition-colors cursor-pointer"
                          >
                            <span className="font-mono font-medium text-foreground">{lot.symbol}</span>
                            {(() => { const b = boardTag(lot.symbol); return b && <span className={`inline-flex items-center justify-center rounded px-1 text-[9px] font-bold leading-tight border ${b.color}`}>{b.label}</span> })()}
                            {symbolNames[lot.symbol] && <span className="text-secondary truncate max-w-28">{symbolNames[lot.symbol]}</span>}
                          </button>
                        </td>
                        <td className="px-2 py-2.5 text-right font-mono text-secondary">{lot.qty}</td>
                        <td className="px-2 py-2.5 text-right font-mono text-foreground">{lot.cost_price}</td>
                        <td className="px-2 py-2.5 text-right font-mono text-secondary">{lastPrices[lot.symbol] != null ? fmtPrice(lastPrices[lot.symbol]) : '—'}</td>
                        <td className="px-2 py-2.5 text-right"><CostPnL close={lastPrices[lot.symbol]} cost={lot.cost_price} /></td>
                        <td className="px-2 py-2.5 text-right font-mono text-bull">{lot.target_pct > 0 ? `${lot.target_pct}%` : '—'}</td>
                        <td className="px-2 py-2.5 text-right font-mono text-bear">{lot.stop_pct > 0 ? `${lot.stop_pct}%` : '—'}</td>
                        <td className="px-2 py-2.5 text-muted">{lot.buy_date || '—'}</td>
                        <td className="px-2 py-2.5 text-right"><RemainingDays remind={lot.remind_date} /></td>
                        <td className="px-2 py-2.5">
                          {lot.remind_date ? (
                            <span className="inline-flex items-center gap-1 text-rose-400">
                              <CalendarClock className="h-3 w-3" />
                              {lot.remind_date}
                              {lot.lead_days > 0 && <span className="text-muted">· 提前{lot.lead_days}天</span>}
                            </span>
                          ) : <span className="text-muted/60">—</span>}
                        </td>
                        <td className="px-3 py-2.5">
                          <div className="flex items-center justify-end gap-0.5">
                            <button
                              onClick={() => setEditing(lot)}
                              title="编辑"
                              className="p-1.5 rounded-md text-secondary transition-all hover:bg-accent/10 hover:text-accent cursor-pointer"
                            >
                              <Pencil className="h-3.5 w-3.5" />
                            </button>
                            {confirmId === lot.id ? (
                              <button
                                onClick={() => handleClickDelete(lot.id)}
                                title="再次点击确认删除"
                                className="inline-flex items-center gap-1 rounded-md bg-danger/15 px-1.5 py-0.5 text-[9px] font-medium text-danger border border-danger/30 animate-pulse cursor-pointer"
                              >
                                <Trash2 className="h-2.5 w-2.5" />确认
                              </button>
                            ) : (
                              <button
                                onClick={() => handleClickDelete(lot.id)}
                                title="删除 (同步删除生成的监控规则)"
                                className="p-1.5 rounded-md text-secondary transition-all hover:bg-danger/10 hover:text-danger cursor-pointer"
                              >
                                <Trash2 className="h-3.5 w-3.5" />
                              </button>
                            )}
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          <div className="flex items-center justify-center gap-1 text-[11px] text-muted">
            生成的止盈止损 / 到期提醒规则已同步至监控中心
            <button onClick={() => navigate('/monitor')} className="inline-flex items-center gap-0.5 text-accent hover:text-accent/80 cursor-pointer">
              去查看 <ArrowUpRight className="h-3 w-3" />
            </button>
          </div>
      </div>

      {editing && <LotDialog lot={editing} onClose={() => setEditing(null)} />}

      <StockPreviewDialog
        symbol={previewSymbol}
        name={previewSymbol ? symbolNames[previewSymbol] : undefined}
        navList={lotsNavItems}
        onNavigate={(sym) => setPreviewSymbol(sym)}
        onClose={() => setPreviewSymbol(null)}
      />
    </section>
  )
}

function CashDialog({ type, onClose, onSaved }: { type: 'deposit' | 'withdrawal'; onClose: () => void; onSaved: () => void }) {
  const [amount, setAmount] = useState('')
  const [note, setNote] = useState('')
  const [error, setError] = useState('')
  const save = useMutation({
    mutationFn: () => api.portfolioCash({ type, amount, note }),
    onSuccess: () => { onSaved(); onClose() },
    onError: err => setError(String((err as Error).message ?? err)),
  })
  return <Modal onClose={onClose} ariaLabel={type === 'deposit' ? '账户入金' : '账户出金'} panelClassName="w-[92vw] max-w-sm rounded-card border border-border bg-surface shadow-xl">
    <div className="border-b border-border/60 px-4 py-3 text-sm font-medium text-foreground">{type === 'deposit' ? '账户入金' : '账户出金'}</div>
    <div className="space-y-3 px-4 py-4"><label className="block space-y-1.5"><span className="text-[11px] text-muted">金额（人民币）</span><input autoFocus value={amount} onChange={e => setAmount(e.target.value)} type="number" min="0.01" step="0.01" className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground" /></label><label className="block space-y-1.5"><span className="text-[11px] text-muted">备注</span><input value={note} onChange={e => setNote(e.target.value)} className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground" /></label>{error && <div className="text-[11px] text-danger">{error}</div>}<div className="text-[10px] text-muted">资金流水不可编辑或删除；错误记录请通过冲正修正。</div></div>
    <div className="flex justify-end gap-2 border-t border-border/60 px-4 py-3"><button onClick={onClose} className="h-9 rounded-btn border border-border px-3 text-xs text-secondary">取消</button><button onClick={() => save.mutate()} disabled={save.isPending || !(Number(amount) > 0)} className="h-9 rounded-btn bg-accent px-4 text-xs font-medium text-white disabled:opacity-40">确认{type === 'deposit' ? '入金' : '出金'}</button></div>
  </Modal>
}

function SellDialog({ position, onClose, onSaved }: { position: PortfolioPosition; onClose: () => void; onSaved: () => void }) {
  const [price, setPrice] = useState(position.current_price ?? '')
  const [quantity, setQuantity] = useState(String(position.available_qty))
  const [removeWatchlist, setRemoveWatchlist] = useState(false)
  const [error, setError] = useState('')
  const sell = useMutation({
    mutationFn: () => api.portfolioSell(position.id, {
      price: price || null,
      quantity: Number(quantity),
      remove_from_watchlist: removeWatchlist,
    }),
    onSuccess: () => { onSaved(); onClose() },
    onError: err => setError(String((err as Error).message ?? err)),
  })
  const qty = Number(quantity)
  return <Modal onClose={onClose} ariaLabel={`卖出 ${position.symbol}`} panelClassName="w-[92vw] max-w-md rounded-card border border-border bg-surface shadow-xl">
    <div className="flex items-center justify-between border-b border-border/60 px-4 py-3"><div><div className="text-sm font-medium text-foreground">确认卖出</div><div className="text-[10px] text-muted">{position.symbol} · {position.source_strategy_name}</div></div><ShoppingCart className="h-4 w-4 text-warning" /></div>
    <div className="space-y-3 px-4 py-4"><div className="grid grid-cols-2 gap-3"><label className="space-y-1.5"><span className="text-[11px] text-muted">成交价</span><input value={price} onChange={e => setPrice(e.target.value)} type="number" min="0" step="0.01" placeholder="自动取实时价/收盘价" className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground" /></label><label className="space-y-1.5"><span className="text-[11px] text-muted">数量（可卖 {position.available_qty} 股）</span><input value={quantity} onChange={e => setQuantity(e.target.value)} type="number" min="1" max={position.available_qty} step="1" className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground" /></label></div><label className="flex items-center gap-2 text-[11px] text-secondary"><input type="checkbox" checked={removeWatchlist} onChange={e => setRemoveWatchlist(e.target.checked)} />全部卖出后移出普通自选</label><div className="text-[10px] text-muted">部分卖出后，剩余数量继续保持当前待处理状态；卖侧将计佣金和印花税。</div>{error && <div className="text-[11px] text-danger">{error}</div>}</div>
    <div className="flex justify-end gap-2 border-t border-border/60 px-4 py-3"><button onClick={onClose} className="h-9 rounded-btn border border-border px-3 text-xs text-secondary">取消</button><button onClick={() => sell.mutate()} disabled={sell.isPending || !(Number(price) > 0) || !Number.isInteger(qty) || qty <= 0 || qty > position.available_qty} className="h-9 rounded-btn bg-warning px-4 text-xs font-medium text-white disabled:opacity-40">确认成交</button></div>
  </Modal>
}

function AccountSettingsDialog({ initial, onClose, onSaved }: { initial: PortfolioSettings; onClose: () => void; onSaved: () => void }) {
  const [values, setValues] = useState({ ...initial })
  const [error, setError] = useState('')
  const save = useMutation({
    mutationFn: () => api.portfolioSettingsUpdate(values),
    onSuccess: () => { onSaved(); onClose() },
    onError: err => setError(String((err as Error).message ?? err)),
  })
  const field = (key: keyof PortfolioSettings) => ({
    value: values[key],
    onChange: (event: React.ChangeEvent<HTMLInputElement>) => setValues(current => ({ ...current, [key]: key === 'max_positions' ? Number(event.target.value) : event.target.value })),
  })
  return <Modal onClose={onClose} ariaLabel="账户设置" panelClassName="w-[92vw] max-w-md rounded-card border border-border bg-surface shadow-xl"><div className="border-b border-border/60 px-4 py-3 text-sm font-medium text-foreground">账户设置</div><div className="grid grid-cols-2 gap-3 px-4 py-4"><label className="space-y-1.5"><span className="text-[11px] text-muted">最大持仓数</span><input type="number" min="1" step="1" {...field('max_positions')} className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs" /></label><label className="space-y-1.5"><span className="text-[11px] text-muted">最大总仓位（0~1）</span><input type="number" min="0.01" max="1" step="0.01" {...field('max_total_position')} className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs" /></label><label className="space-y-1.5"><span className="text-[11px] text-muted">佣金率</span><input type="number" min="0" step="0.0001" {...field('commission_rate')} className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs" /></label><label className="space-y-1.5"><span className="text-[11px] text-muted">卖侧印花税率</span><input type="number" min="0" step="0.0001" {...field('stamp_tax_rate')} className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs" /></label>{error && <div className="col-span-2 text-[11px] text-danger">{error}</div>}</div><div className="flex justify-end gap-2 border-t border-border/60 px-4 py-3"><button onClick={onClose} className="h-9 rounded-btn border border-border px-3 text-xs text-secondary">取消</button><button onClick={() => save.mutate()} disabled={save.isPending} className="h-9 rounded-btn bg-accent px-4 text-xs font-medium text-white disabled:opacity-40">保存</button></div></Modal>
}

function LotDialog({ lot, onClose }: { lot: Lot; onClose: () => void }) {
  const qc = useQueryClient()
  const [draft, setDraft] = useState<Lot>(() => ({ ...lot }))
  const [symbolQuery, setSymbolQuery] = useState('')
  const [error, setError] = useState('')

  // 数字字段用本地字符串承载 (可先清空再输入), 提交时才解析成数值;
  // 否则受控 number + parseFloat 会在清空瞬间把值塞回 0, 导致「0 去不掉」。
  const [nums, setNums] = useState<Record<string, string>>(() => {
    const f = (v: number | undefined | null) => (v == null || v === 0 ? '' : String(v))
    return {
      qty: f(lot.qty), cost_price: f(lot.cost_price),
      target_pct: f(lot.target_pct), stop_pct: f(lot.stop_pct),
      lead_days: f(lot.lead_days),
    }
  })
  const numField = (key: string) => ({
    value: nums[key] ?? '',
    onChange: (e: React.ChangeEvent<HTMLInputElement>) => setNums(s => ({ ...s, [key]: e.target.value })),
  })

  const symbolSearch = useQuery({
    queryKey: QK.instrumentSearch(symbolQuery, 'stock,etf'),
    queryFn: () => api.instrumentSearch(symbolQuery, 20, 'stock,etf'),
    enabled: symbolQuery.trim().length > 0,
  })

  const save = useMutation({
    mutationFn: (vals: Partial<Lot>) => api.lotSave({ ...draft, ...vals, symbol: draft.symbol.trim() }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.lots })
      qc.invalidateQueries({ queryKey: QK.monitorRules })
      onClose()
    },
    onError: err => setError(String((err as any)?.message ?? err)),
  })

  const parseNum = (s: string | undefined) => {
    const n = parseFloat(s ?? '')
    return Number.isFinite(n) ? n : 0
  }

  const submit = () => {
    setError('')
    if (!draft.symbol.trim()) return setError('请选择标的')
    const vals: Partial<Lot> = {
      qty: parseNum(nums.qty),
      cost_price: parseNum(nums.cost_price),
      target_pct: parseNum(nums.target_pct),
      stop_pct: parseNum(nums.stop_pct),
      lead_days: Math.floor(parseNum(nums.lead_days)),
    }
    if (!((vals.cost_price ?? 0) > 0)) return setError('成本价必须为正数')
    if ((vals.qty ?? 0) < 0 || (vals.target_pct ?? 0) < 0 || (vals.stop_pct ?? 0) < 0 || (vals.lead_days ?? 0) < 0) {
      return setError('数量 / 百分比 / 提前天数不能为负数')
    }
    if (!((vals.target_pct ?? 0) > 0 || (vals.stop_pct ?? 0) > 0 || draft.remind_date)) {
      return setError('止盈% / 止损% / 到期日 至少设置一项')
    }
    save.mutate(vals)
  }

  return (
    <Modal onClose={onClose} ariaLabel={lot.id ? '编辑批次' : '新增批次'} panelClassName="w-[92vw] max-w-md bg-surface border border-border rounded-card shadow-xl">
      <div className="flex items-center justify-between border-b border-border/60 px-4 py-3">
        <span className="text-sm font-medium text-foreground">{lot.id ? '编辑批次' : '新增批次'}</span>
        <span className="text-[10px] text-muted">保存后自动同步监控规则</span>
      </div>
      <div className="space-y-3 px-4 py-4">
        {/* 标的 */}
        <div className="space-y-1.5">
          <span className="text-[11px] text-muted">标的</span>
          {draft.symbol ? (
            <div className="flex items-center gap-2">
              <span className="inline-flex items-center gap-1 rounded bg-elevated px-2 py-1 font-mono text-[11px] text-secondary">
                {draft.symbol}
                <button onClick={() => setDraft(d => ({ ...d, symbol: '' }))} className="text-muted hover:text-danger cursor-pointer"><span className="text-[10px]">✕</span></button>
              </span>
              <span className="text-[10px] text-muted">点 ✕ 可重选</span>
            </div>
          ) : (
            <div className="relative">
              <input
                value={symbolQuery}
                onChange={e => setSymbolQuery(e.target.value)}
                placeholder="搜索代码或名称..."
                autoFocus
                className="h-9 w-full rounded-btn border border-border bg-base pl-8 pr-3 text-xs text-foreground focus:outline-none focus:border-accent/50"
              />
              <Search className="absolute left-2.5 top-2.5 h-3.5 w-3.5 text-muted" />
              {symbolSearch.data && symbolSearch.data.results.length > 0 && (
                <div className="absolute z-10 mt-1 max-h-48 w-full overflow-auto rounded border border-border bg-surface shadow-lg">
                  {symbolSearch.data.results.map(r => (
                    <button
                      key={r.symbol}
                      onClick={() => { setDraft(d => ({ ...d, symbol: r.symbol })); setSymbolQuery('') }}
                      className="block w-full px-2.5 py-1.5 text-left text-[11px] hover:bg-elevated cursor-pointer"
                    >
                      <span className="font-mono text-foreground/80">{r.symbol}</span>
                      <span className="ml-1.5 text-muted">{r.name}</span>
                    </button>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>

        <div className="grid grid-cols-2 gap-3">
          <label className="space-y-1.5">
            <span className="text-[11px] text-muted">数量 (参考)</span>
            <input type="number" min={0} placeholder="0" {...numField('qty')} className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground" />
          </label>
          <label className="space-y-1.5">
            <span className="text-[11px] text-muted">成本价</span>
            <input type="number" min={0} step="any" placeholder="0" {...numField('cost_price')} className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground" />
          </label>
          <label className="space-y-1.5">
            <span className="text-[11px] text-muted">止盈%</span>
            <input type="number" min={0} step="any" placeholder="0" {...numField('target_pct')} className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground" />
          </label>
          <label className="space-y-1.5">
            <span className="text-[11px] text-muted">止损%</span>
            <input type="number" min={0} step="any" placeholder="0" {...numField('stop_pct')} className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground" />
          </label>
        </div>

        <div className="grid grid-cols-2 gap-3">
          <div className="space-y-1.5">
            <span className="text-[11px] text-muted">买入日期 (可选)</span>
            <DateShortcuts value={draft.buy_date ?? ''} onChange={v => setDraft(d => ({ ...d, buy_date: v || null }))} options={[{ label: '今天', days: 0 }]} />
            <DatePicker value={draft.buy_date ?? ''} onChange={v => setDraft(d => ({ ...d, buy_date: v || null }))} placeholder="不记录" />
          </div>
          <div className="space-y-1.5">
            <span className="text-[11px] text-muted">到期日 (可选)</span>
            <DateShortcuts value={draft.remind_date ?? ''} onChange={v => setDraft(d => ({ ...d, remind_date: v || null }))} options={[{ label: '5天', days: 5 }, { label: '10天', days: 10 }, { label: '15天', days: 15 }]} base={draft.buy_date || undefined} />
            <DatePicker value={draft.remind_date ?? ''} onChange={v => setDraft(d => ({ ...d, remind_date: v || null }))} placeholder="不提醒" />
          </div>
        </div>

        {draft.remind_date && (
          <label className="space-y-1.5">
            <span className="text-[11px] text-muted">提前提醒天数</span>
            <input type="number" min={0} placeholder="1" {...numField('lead_days')} className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground" />
            <span className="block text-[10px] text-muted">提醒仅在交易时段评估; 到期日若逢周末或长假, 请把提前天数调大些 (建议 ≥ 2, 长假更大)</span>
          </label>
        )}

        {error && <div className="rounded border border-danger/30 bg-danger/10 px-3 py-2 text-[11px] text-danger">{error}</div>}
      </div>
      <div className="flex items-center justify-end gap-2 border-t border-border/60 px-4 py-3">
        <button onClick={onClose} className="h-9 rounded-btn border border-border px-3 text-xs text-secondary hover:bg-elevated cursor-pointer">取消</button>
        <button
          onClick={submit}
          disabled={save.isPending}
          className={cn('h-9 rounded-btn px-4 text-xs font-medium bg-accent/90 text-white hover:bg-accent cursor-pointer disabled:opacity-50')}
        >
          {save.isPending ? '保存中...' : '保存'}
        </button>
      </div>
    </Modal>
  )
}
