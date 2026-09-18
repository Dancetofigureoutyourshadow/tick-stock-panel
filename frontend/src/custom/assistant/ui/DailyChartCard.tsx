/**
 * 日线收盘走势小卡 — 纯 SVG 零依赖。
 *
 * 数据来自工具流(tool_result.chart), 是工具真实返回的序列而非模型生成,
 * 符合本模块"结果可核对"铁律; 配色沿用 A 股口径: 区间涨红跌绿。
 */
import { memo } from 'react'
import type { AssistantChart } from '../client'

function fmt(v: number): string {
  return v >= 1000 ? v.toFixed(0) : v.toFixed(2)
}

export const DailyChartCard = memo(function DailyChartCard({ chart }: { chart: AssistantChart }) {
  const points = chart.points
  if (points.length < 2) return null

  const closes = points.map(p => p[1])
  const volumes = points.map(p => p[2])
  const first = closes[0]
  const last = closes[closes.length - 1]
  const chg = first > 0 ? (last - first) / first : 0
  const up = chg >= 0
  const min = Math.min(...closes)
  const max = Math.max(...closes)
  const span = max - min || 1
  const maxVol = Math.max(...volumes, 1)

  const W = 100
  const H = 40
  const px = (i: number) => (i / (points.length - 1)) * W
  // 折线占上方约 72%, 下方留成交量带
  const py = (v: number) => H * 0.78 - ((v - min) / span) * H * 0.66
  const line = closes.map((v, i) => `${i === 0 ? 'M' : 'L'}${px(i).toFixed(2)},${py(v).toFixed(2)}`).join(' ')
  const area = `${line} L${W},${H} L0,${H} Z`
  const barW = Math.max((W / points.length) * 0.55, 0.6)
  const color = up ? 'var(--bull)' : 'var(--bear)'
  const gradId = `assistant-chart-${chart.kind}-${chart.symbol.replace(/[^a-zA-Z0-9]/g, '')}`

  return (
    <div className="overflow-hidden rounded-card border border-border bg-base/60">
      <div className="flex items-center justify-between px-3 py-1.5 text-xs">
        <span className="font-medium text-foreground">
          {chart.name || chart.symbol}
          {chart.name ? <span className="ml-1.5 font-mono text-muted">{chart.symbol}</span> : null}
          <span className="ml-1.5 text-muted">· 收盘走势</span>
        </span>
        <span className="font-mono text-muted">近 {points.length} 日</span>
      </div>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        preserveAspectRatio="none"
        className="h-20 w-full px-3"
        role="img"
        aria-label={`${chart.name || chart.symbol} 近 ${points.length} 个交易日收盘走势`}
      >
        <defs>
          <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={`hsl(${color})`} stopOpacity="0.14" />
            <stop offset="100%" stopColor={`hsl(${color})`} stopOpacity="0" />
          </linearGradient>
        </defs>
        {volumes.map((v, i) => {
          const prev = i > 0 ? closes[i - 1] : closes[0]
          const barColor = closes[i] >= prev ? 'var(--bull)' : 'var(--bear)'
          const h = (v / maxVol) * H * 0.14
          return (
            <rect
              key={points[i][0]}
              x={(px(i) - barW / 2).toFixed(2)}
              y={(H - h).toFixed(2)}
              width={barW.toFixed(2)}
              height={h.toFixed(2)}
              fill={`hsl(${barColor})`}
              opacity="0.28"
            />
          )
        })}
        <path d={area} fill={`url(#${gradId})`} />
        <path
          d={line}
          fill="none"
          stroke={`hsl(${color})`}
          strokeWidth="1.4"
          vectorEffect="non-scaling-stroke"
          strokeLinejoin="round"
        />
      </svg>
      <div className="flex items-center justify-between px-3 py-1.5 text-[11px]">
        <span className="font-mono text-muted">
          低 {fmt(min)} · 高 {fmt(max)}
        </span>
        <span className="font-mono text-muted">
          最新 <span className="text-foreground">{fmt(last)}</span>
          <span className={`ml-1.5 font-medium ${up ? 'text-bull' : 'text-bear'}`}>
            {up ? '+' : ''}{(chg * 100).toFixed(2)}%
          </span>
        </span>
      </div>
    </div>
  )
})
