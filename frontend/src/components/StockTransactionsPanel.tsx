import type { TransactionRow, TransactionsStatus } from '@/lib/useFocusMarketStream'

interface Props {
  rows: TransactionRow[]
  status: TransactionsStatus
  error?: string | null
  updatedAt?: number | null
  prevClose?: number | null
  height: number
  className?: string
}

function formatValue(value: number | null | undefined): string {
  return value == null
    ? '-'
    : value.toLocaleString('zh-CN', { maximumFractionDigits: 2 })
}

function statusText(status: TransactionsStatus): string {
  switch (status) {
    case 'loading': return '获取中'
    case 'ready': return '实时'
    case 'closed': return '收盘'
    case 'error': return '更新失败'
    case 'unavailable': return '不可用'
    default: return ''
  }
}

function directionText(direction: TransactionRow['direction']): string {
  switch (direction) {
    case 'buy': return '买'
    case 'sell': return '卖'
    case 'neutral': return '中性'
    default: return '-'
  }
}

function priceColor(price: number, prevClose?: number | null): string {
  if (prevClose == null || !Number.isFinite(prevClose) || !Number.isFinite(price) || price === prevClose) {
    return 'text-muted'
  }
  return price > prevClose ? 'text-bull' : 'text-bear'
}

function directionColor(direction: TransactionRow['direction']): string {
  if (direction === 'buy') return 'text-bull'
  if (direction === 'sell') return 'text-bear'
  return 'text-muted'
}

export function StockTransactionsPanel({ rows, status, error, updatedAt, prevClose, height, className }: Props) {
  const displayTime = updatedAt
    ? new Date(updatedAt).toLocaleTimeString('zh-CN', { hour12: false })
    : null

  return (
    <section
      className={`flex min-h-0 flex-col overflow-hidden rounded-card border border-border bg-surface/50 p-3 ${className ?? ''}`}
      style={{ height }}
    >
      <div className="mb-2 flex shrink-0 items-center justify-between gap-2">
        <div className="text-sm font-medium text-foreground">分时成交</div>
        <div className={`text-[10px] ${status === 'ready' ? 'text-bull' : 'text-muted'}`}>
          {statusText(status)}
        </div>
      </div>

      {rows.length === 0 ? (
        <div className="flex min-h-0 flex-1 items-center justify-center text-xs text-muted">
          {error ?? (status === 'unavailable' ? '分时成交不可用' : '正在连接分时成交')}
        </div>
      ) : (
        <>
          <div className="grid shrink-0 grid-cols-[48px_1fr_1fr_38px_36px] gap-2 border-b border-border/60 pb-1 text-[10px] text-muted">
            <span>时间</span>
            <span className="text-right">价格</span>
            <span className="text-right">成交量</span>
            <span className="text-right">笔数</span>
            <span className="text-right">方向</span>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain pt-1 font-mono text-xs">
            {rows.map((row, index) => (
              <div key={`${row.time}-${index}`} className="grid grid-cols-[48px_1fr_1fr_38px_36px] items-center gap-2 py-1">
                <span className="text-muted">{row.time || '-'}</span>
                <span className={`text-right ${priceColor(row.price, prevClose)}`}>{formatValue(row.price)}</span>
                <span className={`text-right ${priceColor(row.price, prevClose)}`}>{formatValue(row.volume)}</span>
                <span className={`text-right ${priceColor(row.price, prevClose)}`}>{formatValue(row.trade_count)}</span>
                <span className={`text-right ${directionColor(row.direction)}`}>
                  {directionText(row.direction)}
                </span>
              </div>
            ))}
          </div>
        </>
      )}

      <div className="mt-2 flex shrink-0 items-center justify-between text-[10px] text-muted">
        <span>{displayTime ? `更新 ${displayTime}` : ''}</span>
        {rows.length > 0 && <span>{rows.length} 笔</span>}
      </div>
    </section>
  )
}
