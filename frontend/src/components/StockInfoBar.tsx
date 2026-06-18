import type { KlineRow } from '@/lib/api'
import { fmtPrice, fmtBigNum } from '@/lib/format'

const BULL = '#C74040'
const BEAR = '#2D9B65'

interface Props {
  symbol: string
  name?: string
  stockInfo?: { name?: string; total_shares?: number; float_shares?: number }
  rows: KlineRow[]
}

export function StockInfoBar({ symbol, name, stockInfo, rows }: Props) {
  if (rows.length === 0) return null

  const latest = rows[rows.length - 1]
  const prev = rows.length >= 2 ? rows[rows.length - 2] : null
  const close = Number(latest.close)
  const chg = prev ? close - Number(prev.close) : 0
  const chgPct = prev ? chg / Number(prev.close) * 100 : 0
  const isUp = chg >= 0
  const clr = isUp ? BULL : BEAR

  const totalShares = stockInfo?.total_shares
  const floatShares = stockInfo?.float_shares
  const marketCap = totalShares ? close * totalShares : null
  const floatMarketCap = floatShares ? close * floatShares : null
  const turnoverRate = floatShares && latest.volume
    ? (Number(latest.volume) * 100 / floatShares * 100)
    : null

  const displayName = stockInfo?.name ?? name ?? ''

  return (
    <div className="px-2 pb-3 font-mono text-[12px] select-none space-y-1">
      {/* Row 1: code, name, price, change, change% */}
      <div className="flex items-baseline gap-x-3 flex-wrap">
        <span className="text-foreground font-bold text-sm tracking-wide">{symbol}</span>
        <span className="text-secondary font-medium">{displayName}</span>
        <span style={{ color: clr }} className="text-lg font-bold tabular-nums">
          {fmtPrice(close)}
        </span>
        <span style={{ color: clr }} className="tabular-nums">
          {isUp ? '+' : ''}{fmtPrice(chg)}
        </span>
        <span style={{ color: clr }} className="tabular-nums">
          {isUp ? '+' : ''}{fmtPrice(chgPct)}%
        </span>
      </div>

      {/* Row 2: market cap, float market cap, turnover rate */}
      <div className="flex items-center gap-x-4 text-[11px] flex-wrap text-muted">
        {marketCap != null && (
          <span>市值 <span className="text-secondary">{fmtBigNum(marketCap)}</span></span>
        )}
        {floatMarketCap != null && (
          <span>流通值 <span className="text-secondary">{fmtBigNum(floatMarketCap)}</span></span>
        )}
        {turnoverRate != null && (
          <span>换手 <span className="text-secondary">{turnoverRate.toFixed(2)}%</span></span>
        )}
      </div>
    </div>
  )
}
