import type { Depth5Snapshot, Depth5Status } from '@/lib/useFocusMarketStream'

interface Props {
  snapshot: Depth5Snapshot | null
  status: Depth5Status
  error?: string | null
  provider?: string | null
  updatedAt?: number | null
  className?: string
}

function formatValue(value: number | null | undefined): string {
  return value == null
    ? '—'
    : value.toLocaleString('zh-CN', { maximumFractionDigits: 2 })
}

function statusText(status: Depth5Status): string {
  switch (status) {
    case 'loading': return '获取中'
    case 'ready': return '实时'
    case 'closed': return '收盘'
    case 'reconnecting': return '重连中'
    case 'error': return '更新失败'
    case 'unavailable': return '不可用'
    default: return ''
  }
}

export function StockDepth5Panel({
  snapshot,
  status,
  error,
  updatedAt,
  className,
}: Props) {
  const asks = snapshot?.ask_prices ?? []
  const askVolumes = snapshot?.ask_volumes ?? []
  const bids = snapshot?.bid_prices ?? []
  const bidVolumes = snapshot?.bid_volumes ?? []
  const displayTime = updatedAt ? new Date(updatedAt).toLocaleTimeString('zh-CN', { hour12: false }) : null

  return (
    <section className={`h-full min-h-[170px] rounded-card border border-border bg-surface/50 p-3 ${className ?? ''}`}>
      <div className="mb-2 flex items-center justify-between gap-2">
        <div className="text-sm font-medium text-foreground">五档盘口</div>
        <div className={`text-[10px] ${status === 'ready' ? 'text-bull' : 'text-muted'}`}>
          {statusText(status)}
        </div>
      </div>

      {!snapshot ? (
        <div className="flex min-h-[120px] items-center justify-center text-xs text-muted">
          {error ?? (status === 'unavailable' ? '五档盘口不可用' : '正在连接实时盘口')}
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 text-xs font-mono sm:grid-cols-2">
          <div>
            <div className="mb-1 grid grid-cols-[38px_1fr_1fr] gap-2 text-[10px] text-muted">
              <span>买五档</span>
              <span>价格</span>
              <span className="text-right">量</span>
            </div>
            <div className="space-y-1">
              {[0, 1, 2, 3, 4].map(level => (
                <div key={`bid-${level}`} className="grid grid-cols-[38px_1fr_1fr] items-center gap-2">
                  <span className="text-muted">买{level + 1}</span>
                  <span className="text-bull">{formatValue(bids[level])}</span>
                  <span className="text-right text-secondary">{formatValue(bidVolumes[level])}</span>
                </div>
              ))}
            </div>
          </div>
          <div>
            <div className="mb-1 grid grid-cols-[38px_1fr_1fr] gap-2 text-[10px] text-muted">
              <span>卖五档</span>
              <span>价格</span>
              <span className="text-right">量</span>
            </div>
            <div className="space-y-1">
              {[0, 1, 2, 3, 4].map(level => (
                <div key={`ask-${level}`} className="grid grid-cols-[38px_1fr_1fr] items-center gap-2">
                  <span className="text-muted">卖{level + 1}</span>
                  <span className="text-danger">{formatValue(asks[level])}</span>
                  <span className="text-right text-secondary">{formatValue(askVolumes[level])}</span>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      <div className="mt-3 flex items-center justify-between gap-2 text-[10px] text-muted">
        <span>{displayTime ? `更新 ${displayTime}` : ''}</span>
      </div>
    </section>
  )
}
