import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, ShoppingCart, Star } from 'lucide-react'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { Modal } from '@/components/Modal'
import { toast } from '@/components/Toast'

interface PaperManualBuyDialogProps {
  symbol: string
  name?: string
  onClose: () => void
}

const money = (value: number) => Number.isFinite(value)
  ? value.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
  : '—'

/** 从选股页发起的也是手动 Paper 买入，不绑定策略，也不触发策略退出提醒。 */
export function PaperStrategyBuyDialog({ symbol, name = '', onClose }: PaperManualBuyDialogProps) {
  const qc = useQueryClient()
  const [account] = useState(() => localStorage.getItem('paper.account') || 'default')
  const [price, setPrice] = useState('')
  const [quantity, setQuantity] = useState('')
  const [removeError, setRemoveError] = useState('')
  const [buyWarningConfirmation, setBuyWarningConfirmation] = useState<string[] | null>(null)
  const preview = useQuery({
    queryKey: ['paper-manual-buy-preview', account, symbol],
    queryFn: () => api.paperBuyPreview({ items: [{ symbol }] }, account),
    enabled: !!symbol,
    staleTime: 0,
  })
  const accounts = useQuery({ queryKey: QK.paperAccounts, queryFn: api.paperAccounts, staleTime: 30_000 })
  const settings = useQuery({ queryKey: QK.paperOverview(account), queryFn: () => api.paperOverview(account) })
  const item = preview.data?.items[0]
  const accountName = accounts.data?.accounts.find(item => item.id === account)?.name || account

  useEffect(() => {
    if (!item) return
    setPrice(item.price === 0 ? '' : String(item.price))
    setQuantity(item.suggested_qty > 0 ? String(item.suggested_qty) : '')
  }, [item])

  const estimate = useMemo(() => {
    const p = Number(price)
    const q = Number(quantity)
    const gross = p > 0 && q > 0 ? p * q : 0
    const fee = gross * Number(settings.data?.fees?.commission_pct ?? 0)
    return { fee, remaining: Number(preview.data?.available_cash ?? 0) - gross - fee }
  }, [price, quantity, settings.data, preview.data])

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: QK.paperAll })
    qc.invalidateQueries({ queryKey: QK.watchlist })
    qc.invalidateQueries({ queryKey: ['watchlist-enriched'] })
    qc.invalidateQueries({ queryKey: QK.watchlistPerformance })
  }
  const buy = useMutation({
    mutationFn: () => api.paperStrategyBuy({ symbol, price: Number(price), quantity: Number(quantity) }, account),
    onSuccess: result => {
      api.watchlistAdd(symbol).finally(() => {
        invalidate()
        toast(`已提交 ${result.order.qty} 股 Paper 手动买入并加入自选`, 'success')
        onClose()
      })
    },
  })
  const addOnly = useMutation({
    mutationFn: () => api.watchlistAdd(symbol),
    onSuccess: () => { invalidate(); toast('已加入自选', 'success'); onClose() },
    onError: error => setRemoveError(String((error as Error).message ?? error)),
  })

  const qty = Number(quantity)
  const priceNumber = Number(price)
  const grossAmount = priceNumber > 0 && qty > 0 ? priceNumber * qty : 0
  const singlePositionCap = Number(preview.data?.single_position_cap ?? 0)
  const warnings = [
    preview.data?.remaining_slots === 0 || item?.blocked_reason === '持仓名额已满' ? '持仓名额已满' : '',
    item?.blocked_reason === '预算不足一手' ? '当前预算不足一手' : '',
    grossAmount > singlePositionCap && singlePositionCap > 0 ? `买入金额超过单票仓位上限 ¥${money(singlePositionCap)}` : '',
    grossAmount + estimate.fee > Number(preview.data?.available_cash ?? 0) ? '买入后可用现金不足' : '',
  ].filter(Boolean)
  const invalid = !(priceNumber > 0) || !(qty > 0) || !Number.isInteger(qty) || qty % 100 !== 0 || !item
  const submitBuy = () => {
    if (warnings.length > 0) { setBuyWarningConfirmation(warnings); return }
    buy.mutate()
  }

  return (
    <>
      <Modal onClose={onClose} ariaLabel={`买入 ${symbol}`} panelClassName="w-[94vw] max-w-lg rounded-card border border-border bg-surface shadow-xl">
        <div className="flex items-center justify-between border-b border-border/60 px-5 py-4"><div><div className="text-sm font-medium text-foreground">手动买入并加入自选</div><div className="mt-0.5 text-[11px] text-muted">{symbol}{name ? ` · ${name}` : ''}</div></div><ShoppingCart className="h-4 w-4 text-accent" /></div>
        <div className="space-y-4 px-5 py-4">
          <div className="text-[11px] text-muted">目标账户: {accountName}</div>
          <div className="rounded-lg border border-border/60 bg-base/50 px-3 py-2 text-[11px] text-muted">本次交易归为手动批次，不绑定策略，不触发策略退出提醒。</div>
          {preview.isLoading ? <div className="rounded-lg border border-dashed border-border px-4 py-6 text-center text-xs text-muted">正在计算建议仓位…</div> : preview.isError ? <div className="rounded-lg border border-danger/30 bg-danger/10 px-3 py-2 text-xs text-danger">{String((preview.error as Error).message)}</div> : item && <>
            <div className="grid grid-cols-2 gap-3"><label className="space-y-1.5"><span className="text-[11px] text-muted">成交价</span><input value={price} onChange={event => setPrice(event.target.value)} type="number" min="0" step="0.01" className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground" /><span className="block text-[10px] text-muted">{item.price_source === 'latest_close' ? '最新收盘价' : item.price_source === 'missing' ? '行情缺失，请手动填写' : '当前参考价'}</span></label><label className="space-y-1.5"><span className="text-[11px] text-muted">数量（100 股整数倍）</span><input value={quantity} onChange={event => setQuantity(event.target.value)} type="number" min="100" step="100" className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground" /><span className="block text-[10px] text-muted">建议 {item.suggested_qty} 股</span></label></div>
            <div className="grid grid-cols-2 gap-x-5 gap-y-2 rounded-lg border border-border/60 bg-base/50 p-3 text-[11px]"><span className="text-muted">预计佣金</span><span className="text-right font-mono text-foreground">¥{money(estimate.fee)}</span><span className="text-muted">成交后剩余现金</span><span className={`text-right font-mono ${estimate.remaining < 0 ? 'text-danger' : 'text-foreground'}`}>¥{money(estimate.remaining)}</span></div>
            {item.blocked_reason && <div className="flex items-center gap-1.5 rounded border border-warning/30 bg-warning/10 px-3 py-2 text-[11px] text-warning"><AlertTriangle className="h-3.5 w-3.5" />提示：{item.blocked_reason}，确认后仍可买入。</div>}
          </>}
          {removeError && <div className="text-[11px] text-danger">{removeError}</div>}
        </div>
        <div className="flex items-center justify-end gap-2 border-t border-border/60 px-5 py-3"><button type="button" onClick={() => addOnly.mutate()} disabled={addOnly.isPending} className="inline-flex h-9 items-center gap-1.5 rounded-btn border border-border px-3 text-xs text-secondary hover:bg-elevated disabled:opacity-50"><Star className="h-3.5 w-3.5" />仅加入自选</button><button type="button" onClick={submitBuy} disabled={invalid || buy.isPending || preview.isLoading} className="inline-flex h-9 items-center gap-1.5 rounded-btn bg-accent px-4 text-xs font-medium text-white disabled:opacity-40"><ShoppingCart className="h-3.5 w-3.5" />{buy.isPending ? '提交中…' : '确认买入'}</button></div>
      </Modal>
      {buyWarningConfirmation && <Modal onClose={() => setBuyWarningConfirmation(null)} ariaLabel="确认继续买入" panelClassName="w-[92vw] max-w-md rounded-card border border-border bg-surface shadow-xl"><div className="flex items-center gap-2 border-b border-border/60 px-4 py-3"><AlertTriangle className="h-4 w-4 text-warning" /><div className="text-sm font-medium text-foreground">买入提示</div></div><div className="space-y-3 px-4 py-4 text-xs"><div className="text-secondary">本次买入存在以下提示，确认后仍可继续：</div><ul className="list-disc space-y-1.5 pl-5 text-warning">{buyWarningConfirmation.map(warning => <li key={warning}>{warning}</li>)}</ul></div><div className="flex justify-end gap-2 border-t border-border/60 px-4 py-3"><button type="button" onClick={() => setBuyWarningConfirmation(null)} className="h-9 rounded-btn border border-border px-3 text-xs text-secondary hover:text-foreground">取消</button><button type="button" onClick={() => { setBuyWarningConfirmation(null); buy.mutate() }} disabled={buy.isPending} className="h-9 rounded-btn bg-accent px-4 text-xs font-medium text-white disabled:opacity-40">确认继续买入</button></div></Modal>}
    </>
  )
}
