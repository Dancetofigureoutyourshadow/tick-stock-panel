import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, ShoppingCart, Star } from 'lucide-react'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { Modal } from '@/components/Modal'
import { toast } from '@/components/Toast'

interface StrategyOption {
  id: string
  name: string
}

interface PortfolioBuyDialogProps {
  symbol: string
  name?: string
  score?: number | null
  scoresByStrategy?: Record<string, number | null>
  strategies: StrategyOption[]
  initialStrategyId?: string | null
  onClose: () => void
}

const money = (value: number) => Number.isFinite(value)
  ? value.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
  : '—'

export function PortfolioBuyDialog({
  symbol,
  name = '',
  score,
  scoresByStrategy,
  strategies,
  initialStrategyId,
  onClose,
}: PortfolioBuyDialogProps) {
  const qc = useQueryClient()
  const initial = strategies.some(item => item.id === initialStrategyId)
    ? initialStrategyId!
    : strategies[0]?.id ?? ''
  const [strategyId, setStrategyId] = useState(initial)
  const [price, setPrice] = useState('')
  const [quantity, setQuantity] = useState('')
  const [removeError, setRemoveError] = useState('')
  const [buyWarningConfirmation, setBuyWarningConfirmation] = useState<string[] | null>(null)
  const selectedScore = scoresByStrategy && Object.prototype.hasOwnProperty.call(scoresByStrategy, strategyId)
    ? scoresByStrategy[strategyId] ?? null
    : score ?? null

  const preview = useQuery({
    queryKey: ['portfolio-buy-preview', symbol, strategyId, selectedScore],
    queryFn: () => api.portfolioBuyPreview({
      items: [{ symbol, strategy_id: strategyId, score: selectedScore }],
    }),
    enabled: !!strategyId,
    staleTime: 0,
  })
  const settings = useQuery({ queryKey: QK.portfolioSettings, queryFn: api.portfolioSettings })
  const item = preview.data?.items[0]

  useEffect(() => {
    if (!item) return
    setPrice(item.price === '0' ? '' : item.price)
    setQuantity(item.suggested_qty > 0 ? String(item.suggested_qty) : '')
  }, [item])

  const estimate = useMemo(() => {
    const p = Number(price)
    const q = Number(quantity)
    const gross = p > 0 && q > 0 ? p * q : 0
    const fee = gross * Number(settings.data?.commission_rate ?? 0)
    return {
      fee,
      remaining: Number(preview.data?.available_cash ?? 0) - gross - fee,
    }
  }, [price, quantity, settings.data, preview.data])

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: QK.portfolioSummary })
    qc.invalidateQueries({ queryKey: QK.portfolioPositions })
    qc.invalidateQueries({ queryKey: QK.portfolioTransactions })
    qc.invalidateQueries({ queryKey: QK.watchlist })
    qc.invalidateQueries({ queryKey: ['watchlist-enriched'] })
    qc.invalidateQueries({ queryKey: QK.watchlistPerformance })
  }

  const buy = useMutation({
    mutationFn: (options: { confirmBuyWarnings: boolean }) => api.portfolioBuy({
      symbol,
      name,
      strategy_id: strategyId,
      buy_score: selectedScore,
      price,
      quantity: Number(quantity),
      confirm_buy_warnings: options.confirmBuyWarnings,
    }),
    onSuccess: result => {
      invalidate()
      toast(`已买入 ${result.trade.quantity} 股并加入自选`, 'success')
      onClose()
    },
  })

  const addOnly = useMutation({
    mutationFn: () => api.watchlistAdd(symbol),
    onSuccess: () => {
      invalidate()
      toast('已加入自选', 'success')
      onClose()
    },
    onError: error => setRemoveError(String((error as Error).message ?? error)),
  })

  const qty = Number(quantity)
  const priceNumber = Number(price)
  const grossAmount = priceNumber > 0 && qty > 0 ? priceNumber * qty : 0
  const singlePositionCap = Number(preview.data?.single_position_cap ?? 0)
  const maxTotalPosition = Number(settings.data?.max_total_position ?? 1)
  const totalAssets = Number(preview.data?.total_assets ?? 0)
  const maxExposure = totalAssets * maxTotalPosition
  const projectedExposure = Number(preview.data?.market_value ?? 0) + grossAmount
  const warnings = [
    preview.data?.remaining_slots === 0 || item?.blocked_reason === '持仓名额已满' ? '持仓名额已满' : '',
    item?.blocked_reason === '预算不足一手' ? '当前预算不足一手' : '',
    grossAmount > singlePositionCap && singlePositionCap > 0
      ? `买入金额超过单票仓位上限 ¥${money(singlePositionCap)}`
      : '',
    projectedExposure > maxExposure && maxExposure >= 0
      ? `买入后将超过最大总仓位 ¥${money(maxExposure)}`
      : '',
    grossAmount + estimate.fee > Number(preview.data?.available_cash ?? 0)
      ? '买入后可用现金不足'
      : '',
  ].filter(Boolean)
  const invalid = !strategyId || !(priceNumber > 0) || !(qty > 0)
    || !Number.isInteger(qty) || qty % 100 !== 0 || !item

  const submitBuy = () => {
    if (warnings.length > 0) {
      setBuyWarningConfirmation(warnings)
      return
    }
    buy.mutate({ confirmBuyWarnings: false })
  }

  return (
    <>
      <Modal onClose={onClose} ariaLabel={`买入 ${symbol}`} panelClassName="w-[94vw] max-w-lg rounded-card border border-border bg-surface shadow-xl">
        <div className="flex items-center justify-between border-b border-border/60 px-5 py-4">
          <div>
            <div className="text-sm font-medium text-foreground">买入并加入自选</div>
            <div className="mt-0.5 text-[11px] text-muted">{symbol}{name ? ` · ${name}` : ''}</div>
          </div>
          <ShoppingCart className="h-4 w-4 text-accent" />
        </div>
        <div className="space-y-4 px-5 py-4">
          <label className="block space-y-1.5">
            <span className="text-[11px] text-muted">本次买入依据</span>
            <select
              value={strategyId}
              onChange={event => setStrategyId(event.target.value)}
              className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground"
            >
              {strategies.map(strategy => (
                <option key={strategy.id} value={strategy.id}>{strategy.name}</option>
              ))}
            </select>
            {strategies.length > 1 && <div className="text-[10px] text-warning">该股票命中多个策略，请明确选择本次买入依据。</div>}
          </label>

          {preview.isLoading ? (
            <div className="rounded-lg border border-dashed border-border px-4 py-6 text-center text-xs text-muted">正在计算建议仓位…</div>
          ) : preview.isError ? (
            <div className="rounded-lg border border-danger/30 bg-danger/10 px-3 py-2 text-xs text-danger">{String((preview.error as Error).message)}</div>
          ) : item && (
            <>
              <div className="grid grid-cols-2 gap-3">
                <label className="space-y-1.5">
                  <span className="text-[11px] text-muted">成交价</span>
                  <input value={price} onChange={event => setPrice(event.target.value)} type="number" min="0" step="0.01" className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground" />
                  <span className="block text-[10px] text-muted">{item.price_source === 'realtime' ? '实时最新价' : item.price_source === 'latest_close' ? '最新收盘价' : item.price_source === 'missing' ? '行情缺失，请手动填写' : '手动价格'}</span>
                </label>
                <label className="space-y-1.5">
                  <span className="text-[11px] text-muted">数量（100 股整数倍）</span>
                  <input value={quantity} onChange={event => setQuantity(event.target.value)} type="number" min="100" step="100" className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground" />
                  <span className="block text-[10px] text-muted">建议 {item.suggested_qty} 股</span>
                </label>
              </div>
              <div className="grid grid-cols-2 gap-x-5 gap-y-2 rounded-lg border border-border/60 bg-base/50 p-3 text-[11px]">
                <span className="text-muted">策略评分</span><span className="text-right font-mono text-foreground">{selectedScore ?? '—'}</span>
                <span className="text-muted">建议仓位</span><span className="text-right font-mono text-foreground">¥{item.suggested_budget} · {preview.data && Number(preview.data.total_assets) > 0 ? `${(Number(item.suggested_budget) / Number(preview.data.total_assets) * 100).toFixed(1)}%` : '—'}</span>
                <span className="text-muted">预计佣金</span><span className="text-right font-mono text-foreground">¥{money(estimate.fee)}</span>
                <span className="text-muted">成交后剩余现金</span><span className={`text-right font-mono ${estimate.remaining < 0 ? 'text-danger' : 'text-foreground'}`}>¥{money(estimate.remaining)}</span>
              </div>
              {item.blocked_reason && <div className="flex items-center gap-1.5 rounded border border-warning/30 bg-warning/10 px-3 py-2 text-[11px] text-warning"><AlertTriangle className="h-3.5 w-3.5" />提示：{item.blocked_reason}，确认后仍可买入。</div>}
              {!item.has_exit_rules && <div className="flex items-center gap-1.5 rounded border border-danger/30 bg-danger/10 px-3 py-2 text-[11px] font-medium text-danger"><AlertTriangle className="h-3.5 w-3.5" />无卖出提醒</div>}
            </>
          )}
          {removeError && <div className="text-[11px] text-danger">{removeError}</div>}
        </div>
        <div className="flex items-center justify-end gap-2 border-t border-border/60 px-5 py-3">
          <button type="button" onClick={() => addOnly.mutate()} disabled={addOnly.isPending} className="inline-flex h-9 items-center gap-1.5 rounded-btn border border-border px-3 text-xs text-secondary hover:bg-elevated disabled:opacity-50"><Star className="h-3.5 w-3.5" />仅加入自选</button>
          <button type="button" onClick={submitBuy} disabled={invalid || buy.isPending || preview.isLoading} className="inline-flex h-9 items-center gap-1.5 rounded-btn bg-accent px-4 text-xs font-medium text-white disabled:opacity-40"><ShoppingCart className="h-3.5 w-3.5" />{buy.isPending ? '成交中…' : '确认买入'}</button>
        </div>
      </Modal>
      {buyWarningConfirmation && (
        <Modal
          onClose={() => setBuyWarningConfirmation(null)}
          ariaLabel="确认继续买入"
          panelClassName="w-[92vw] max-w-md rounded-card border border-border bg-surface shadow-xl"
        >
          <div className="flex items-center gap-2 border-b border-border/60 px-4 py-3">
            <AlertTriangle className="h-4 w-4 text-warning" />
            <div className="text-sm font-medium text-foreground">买入提示</div>
          </div>
          <div className="space-y-3 px-4 py-4 text-xs">
            <div className="text-secondary">本次买入存在以下提示，确认后仍可继续：</div>
            <ul className="list-disc space-y-1.5 pl-5 text-warning">
              {buyWarningConfirmation.map(warning => <li key={warning}>{warning}</li>)}
            </ul>
          </div>
          <div className="flex justify-end gap-2 border-t border-border/60 px-4 py-3">
            <button type="button" onClick={() => setBuyWarningConfirmation(null)} className="h-9 rounded-btn border border-border px-3 text-xs text-secondary hover:text-foreground">取消</button>
            <button type="button" onClick={() => { setBuyWarningConfirmation(null); buy.mutate({ confirmBuyWarnings: true }) }} disabled={buy.isPending} className="h-9 rounded-btn bg-accent px-4 text-xs font-medium text-white disabled:opacity-40">{buy.isPending ? '成交中…' : '确认继续买入'}</button>
          </div>
        </Modal>
      )}
    </>
  )
}
