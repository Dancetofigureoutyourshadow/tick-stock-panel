import { useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  ArrowLeft,
  BarChart3,
  BookOpenCheck,
  Eye,
  EyeOff,
  History,
  Loader2,
  Play,
  RefreshCw,
  Sparkles,
  Square,
  Trash2,
  X,
} from 'lucide-react'
import { EChartsCandlestick, type ChartClickAnchor, type ChartMarker, type ChartRange, type ChartStructureLine, type OHLC } from '@/components/EChartsCandlestick'
import { StockIntradayChart } from '@/components/StockIntradayChart'
import { Modal } from '@/components/Modal'
import { toOHLC } from '@/components/StockDailyKChart'
import { MarkdownRenderer } from '@/components/financials/MarkdownRenderer'
import { EmptyState } from '@/components/EmptyState'
import { PageHeader } from '@/components/PageHeader'
import { api, type ChanStructure, type ChanTrainingAction, type ChanTrainingRecord, type ChanTrainingRecordSummary, type ChanTrainingSession, type ChanTrainingStartRequest } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { storage } from '@/lib/storage'
import { toast } from '@/components/Toast'

const POINT_LABELS = { first: '一', second: '二', third: '三' } as const
const BUY_SHORTCUTS = [
  { label: '1/2', value: 50 },
  { label: '1/3', value: 100 / 3 },
  { label: '1/4', value: 25 },
  { label: '全仓', value: 100 },
]
const SELL_SHORTCUTS = [
  { label: '1/2', value: 50 },
  { label: '1/3', value: 100 / 3 },
  { label: '1/4', value: 25 },
  { label: '清仓', value: 100 },
]
const POINT_LABEL_LEVEL = { first: 1, second: 2, third: 3 } as const
const SUB_INDICATORS = [
  { key: 'vol', label: '成交量' },
  { key: 'macd', label: 'MACD' },
  { key: 'rsi', label: 'RSI' },
  { key: 'kdj', label: 'KDJ' },
] as const

const EVIDENCE_INDICATOR_LABELS: Record<string, string> = {
  macd_first_area: '首笔 MACD 面积',
  macd_last_area: '末笔 MACD 面积',
  dif_first_end: '首笔末端 DIF',
  dif_last_start: '末笔起点 DIF',
  dif_last_end: '末笔末端 DIF',
  dif_center_extreme: '中枢 DIF 极值',
  sma21_first_end: '第一笔 SMA21',
  sma21_third_end: '第三笔 SMA21',
  sma21_fifth_start: '第五笔起点 SMA21',
  sma21_fifth_end: '第五笔末端 SMA21',
  sma34_first_end: '第一笔 SMA34',
  sma34_third_end: '第三笔 SMA34',
  sma34_fifth_end: '第五笔 SMA34',
}

function evidenceValue(value: string | number) {
  return typeof value === 'number' ? Number(value.toFixed(6)).toString() : value
}

function pointDescription(point: ChanStructure['points'][number], showDates: boolean) {
  const lines = [[point.signal_name, point.reason].filter(Boolean).join(' · ')]
  const evidence = point.signal_evidence
  if (!evidence) return lines.filter(Boolean).join('\n')
  lines.push(`命中笔窗口：${evidence.stroke_count} 笔`)
  evidence.strokes.forEach((stroke, index) => {
    const period = showDates ? ` ${stroke.start_date}→${stroke.end_date}` : ''
    lines.push(`参与笔${index + 1}：${stroke.direction === 'up' ? '向上' : '向下'}${period} 高 ${evidenceValue(stroke.high)} 低 ${evidenceValue(stroke.low)}`)
  })
  if (evidence.center) {
    lines.push(`中枢：ZD ${evidenceValue(evidence.center.zd)} / ZG ${evidenceValue(evidence.center.zg)} / DD ${evidenceValue(evidence.center.dd)} / GG ${evidenceValue(evidence.center.gg)}`)
  }
  Object.entries(evidence.indicators).forEach(([key, value]) => {
    lines.push(`${EVIDENCE_INDICATOR_LABELS[key] ?? key}：${evidenceValue(value)}`)
  })
  evidence.checks.forEach(check => {
    lines.push(`✓ ${check.label}：${evidenceValue(check.left)} ${check.operator} ${evidenceValue(check.right)}`)
  })
  return lines.filter(Boolean).join('\n')
}

function markersForStructure(chan: ChanStructure | undefined, actions: ChanTrainingAction[], data: OHLC[] = [], showDates = true): ChartMarker[] {
  const barsByDate = new Map(data.map(bar => [bar.date, bar]))
  const railByDateAndSide = new Map<string, number>()
  const nextRailOffset = (date: string, above: boolean) => {
    const key = `${date}:${above ? 'above' : 'below'}`
    const rail = railByDateAndSide.get(key) ?? 0
    railByDateAndSide.set(key, rail + 1)
    const offset = 14 + rail * 22
    return above ? -offset : offset
  }
  const anchorPrice = (date: string, fallback: number, above: boolean) => {
    const bar = barsByDate.get(date)
    return bar ? (above ? bar.high : bar.low) : fallback
  }
  const fractals: ChartMarker[] = (chan?.fractals ?? []).map((point, index) => {
    const above = point.type === 'top'
    return {
      date: point.date,
      kind: 'neutral',
      price: anchorPrice(point.date, point.price, above),
      lockToPrice: true,
      labelLevel: 0,
      above,
      offsetY: nextRailOffset(point.date, above),
      label: above ? '顶' : '底',
      color: above ? '#F97316' : '#38BDF8',
      inspectionId: `fractal:${index}`,
    }
  })
  const points: ChartMarker[] = (chan?.points ?? []).map((point, index) => {
    const above = point.type === 'sell'
    return {
      date: point.date,
      kind: point.type,
      price: anchorPrice(point.date, point.price, above),
      lockToPrice: true,
      labelLevel: POINT_LABEL_LEVEL[point.class],
      above,
      offsetY: nextRailOffset(point.date, above),
      label: `${point.type === 'buy' ? '买' : '卖'}${POINT_LABELS[point.class]}`,
      description: pointDescription(point, showDates),
      color: point.type === 'buy' ? '#FACC15' : '#F472B6',
      inspectionId: `point:${index}`,
    }
  })
  const trades: ChartMarker[] = actions.map((action, index) => {
    const bar = barsByDate.get(action.date)
    const isBuy = action.side === 'buy'
    const above = !isBuy
    return {
      date: action.date,
      kind: action.side,
      // Anchor the circle to the candle edge; execution_price remains in the
      // action log. This keeps the marker attached to the right K line while
      // leaving room for its independent upper/lower rail.
      price: bar ? (isBuy ? bar.low : bar.high) : action.execution_price,
      label: action.side === 'buy' ? 'B' : 'S',
      above,
      color: action.side === 'buy' ? '#EF4444' : '#22C55E',
      circle: true,
      offsetY: nextRailOffset(action.date, above),
      inspectionId: `trade:${index}`,
    }
  })
  return [...fractals, ...points, ...trades]
}

function markersFor(session: ChanTrainingSession, data: OHLC[] = [], showDates = true): ChartMarker[] {
  return markersForStructure(session.chan, session.actions, data, showDates)
}

function SignalEvidenceList({ points, showDates }: { points: ChanStructure['points']; showDates: boolean }) {
  if (points.length === 0) return null
  return (
    <div className="mt-2 space-y-1 border-t border-border/50 pt-1.5 text-[10px] text-muted">
      {points.slice(-3).map(point => (
        <details key={`${point.date}-${point.type}-${point.class}-${point.structure_index ?? ''}`} className="rounded border border-border/50 bg-base px-2 py-1">
          <summary className="cursor-pointer text-secondary">
            {point.type === 'buy' ? '买' : '卖'}{POINT_LABELS[point.class]} · {point.signal_name}
            {showDates ? ` · ${point.date}` : ''}
          </summary>
          <div className="mt-1 whitespace-pre-wrap font-mono leading-relaxed">{pointDescription(point, showDates)}</div>
        </details>
      ))}
    </div>
  )
}

function rangesForStructure(chan: ChanStructure | undefined): ChartRange[] {
  return (chan?.centers ?? []).map((center, index) => ({
    // Draw the three-segment formation zone. The backend still retains the
    // full start/end extension window for analysis and historical records.
    start: center.formation_start_date ?? center.start_date,
    end: center.formation_end_date ?? center.end_date,
    label: `中枢 ${center.low.toFixed(2)}-${center.high.toFixed(2)}`,
    low: center.low,
    high: center.high,
    color: 'rgba(139,92,246,0.22)',
    inspectionId: `center:${index}`,
  }))
}

function rangesFor(session: ChanTrainingSession): ChartRange[] {
  return rangesForStructure(session.chan)
}

function structureLinesFor(chan: ChanStructure | undefined, data: OHLC[] = []): ChartStructureLine[] {
  if (!chan) return []
  const barsByDate = new Map(data.map(bar => [bar.date, bar]))
  const fractalTypes = new Map(chan.fractals.map(point => [point.date, point.type]))
  const anchorPrice = (date: string, fallback: number) => {
    const bar = barsByDate.get(date)
    const type = fractalTypes.get(date)
    return bar && type ? (type === 'top' ? bar.high : bar.low) : fallback
  }
  return [
    ...chan.strokes.map((line, index) => ({
      start: line.start_date,
      end: line.end_date,
      startPrice: anchorPrice(line.start_date, line.start_price),
      endPrice: anchorPrice(line.end_date, line.end_price),
      color: '#38BDF8',
      width: 1.2,
      type: 'dashed' as const,
      inspectionId: `stroke:${index}`,
    })),
  ]
}

interface StructureInspection {
  title: string
  sourceBars: OHLC[]
  confirmationBar?: OHLC
  details: string[]
}

function structureInspection(
  inspectionId: string,
  chan: ChanStructure,
  actions: ChanTrainingAction[],
  data: OHLC[],
  showDates: boolean,
): StructureInspection | null {
  const [kind, rawIndex] = inspectionId.split(':')
  const index = Number(rawIndex)
  if (!Number.isInteger(index) || index < 0) return null
  const byDate = (value?: string) => value ? data.find(bar => bar.date === value || bar.date.slice(0, 10) === value.slice(0, 10)) : undefined
  const byIndex = (value?: number, fallbackDate?: string) => (
    value != null && value >= 0 && value < data.length ? data[value] : byDate(fallbackDate)
  )
  const uniqueBars = (...bars: Array<OHLC | undefined>) => Array.from(new Map(
    bars.filter((bar): bar is OHLC => !!bar).map(bar => [bar.date, bar]),
  ).values())

  if (kind === 'fractal') {
    const item = chan.fractals[index]
    if (!item) return null
    return {
      title: `${item.type === 'top' ? '顶' : '底'}分型`,
      sourceBars: uniqueBars(byIndex(item.source_index, item.date)),
      confirmationBar: byIndex(item.confirmed_at),
      details: [`分型价 ${item.price.toFixed(2)}`, '按 CZSC 三根去包含 K 线分型规则确认'],
    }
  }
  if (kind === 'stroke') {
    const item = chan.strokes[index]
    if (!item) return null
    return {
      title: `${item.direction === 'up' ? '向上' : '向下'}笔`,
      sourceBars: uniqueBars(
        byIndex(item.start_index, item.start_date),
        byIndex(item.end_index, item.end_date),
      ),
      confirmationBar: byIndex(item.confirmed_at),
      details: [
        `起点 ${item.start_price.toFixed(2)} → 终点 ${item.end_price.toFixed(2)}`,
        '相邻反向确认分型按 CZSC 笔回退与最小长度规则连接',
      ],
    }
  }
  if (kind === 'center') {
    const item = chan.centers[index]
    if (!item) return null
    return {
      title: '中枢',
      sourceBars: uniqueBars(
        byIndex(item.formation_start_index ?? item.start_index, item.formation_start_date ?? item.start_date),
        byIndex(item.formation_end_index ?? item.end_index, item.formation_end_date ?? item.end_date),
      ),
      confirmationBar: byIndex(item.confirmed_at),
      details: [
        `ZD ${item.low.toFixed(2)} / ZG ${item.high.toFixed(2)}`,
        item.range_low != null && item.range_high != null ? `DD ${item.range_low.toFixed(2)} / GG ${item.range_high.toFixed(2)}` : '',
        `参与笔 ${item.stroke_start ?? '--'} → ${item.stroke_end ?? '--'} · ${item.kind ?? 'initial'}`,
      ].filter(Boolean),
    }
  }
  if (kind === 'point') {
    const item = chan.points[index]
    if (!item) return null
    return {
      title: `${item.type === 'buy' ? '买' : '卖'}${POINT_LABELS[item.class]} · ${item.signal_name ?? ''}`,
      sourceBars: uniqueBars(byIndex(item.source_index, item.date)),
      confirmationBar: byIndex(item.confirmed_at),
      details: pointDescription(item, showDates).split('\n'),
    }
  }
  if (kind === 'trade') {
    const item = actions[index]
    if (!item) return null
    return {
      title: `实际${item.side === 'buy' ? '买入 B' : '卖出 S'}`,
      sourceBars: uniqueBars(byDate(item.date)),
      details: [
        `收盘成交价 ${item.execution_price.toFixed(2)} · ${item.shares} 股 · ${item.percentage.toFixed(2)}%`,
        `佣金 ${item.commission.toFixed(2)} · 印花税 ${item.stamp_tax.toFixed(2)} · 滑点 ${item.slippage_amount.toFixed(2)}`,
      ],
    }
  }
  return null
}

function StructureInspectionPanel({ inspection, showDates, onClose }: { inspection: StructureInspection | null; showDates: boolean; onClose: () => void }) {
  if (!inspection) return null
  const BarCard = ({ bar, label }: { bar: OHLC; label: string }) => (
    <div className="rounded border border-border/60 bg-base px-2 py-1.5">
      <div className="text-[10px] text-muted">{label}{showDates ? ` · ${bar.date}` : ''}</div>
      <div className="mt-1 whitespace-nowrap font-mono text-[11px] text-secondary">开 {bar.open.toFixed(2)} · 高 {bar.high.toFixed(2)} · 低 {bar.low.toFixed(2)} · 收 {bar.close.toFixed(2)}</div>
    </div>
  )
  return (
    <section className="mx-2 mb-2 rounded border border-accent/30 bg-accent/5 p-2.5">
      <div className="flex items-center justify-between gap-2">
        <div className="text-xs font-medium text-foreground">结构检查 · {inspection.title}</div>
        <button type="button" onClick={onClose} className="text-[10px] text-muted hover:text-foreground">关闭</button>
      </div>
      <div className="mt-2 grid gap-2 lg:grid-cols-3">
        {inspection.sourceBars.map((bar, index) => <BarCard key={`${bar.date}-${index}`} bar={bar} label={`原始 K${inspection.sourceBars.length > 1 ? index + 1 : ''}`} />)}
        {inspection.confirmationBar && <BarCard bar={inspection.confirmationBar} label="确认 K" />}
      </div>
      <div className="mt-2 whitespace-pre-wrap font-mono text-[10px] leading-relaxed text-muted">{inspection.details.join('\n')}</div>
    </section>
  )
}

function money(value: number) {
  return value.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

function pct(value: number) {
  return `${value >= 0 ? '+' : ''}${value.toFixed(2)}%`
}

function visibleTrainingWarning(warning: string, session: ChanTrainingSession, showMetadata: boolean) {
  if (showMetadata) return warning
  const tokens = [session.symbol, session.symbol.split('.')[0], session.name]
    .filter((token, index, values) => token && values.indexOf(token) === index)
  return tokens.reduce((value, token) => value.split(token).join('当前标的'), warning)
}

function addMovingAverages(data: ReturnType<typeof toOHLC>) {
  const closes = data.map(row => row.close)
  const ema = (period: number) => {
    const result: number[] = []
    const multiplier = 2 / (period + 1)
    let value = closes[0] ?? 0
    closes.forEach((close, index) => {
      value = index === 0 ? close : (close - value) * multiplier + value
      result.push(value)
    })
    return result
  }
  const ema12 = ema(12)
  const ema26 = ema(26)
  const dif = data.map((_row, index) => ema12[index] - ema26[index])
  const dea: number[] = []
  const deaMultiplier = 2 / 10
  let deaValue = dif[0] ?? 0
  dif.forEach((value, index) => {
    deaValue = index === 0 ? value : (value - deaValue) * deaMultiplier + deaValue
    dea.push(deaValue)
  })
  let kValue = 50
  let dValue = 50

  return data.map((row, index) => {
    const average = (period: number) => {
      if (index < period - 1) return null
      let total = 0
      for (let offset = 0; offset < period; offset += 1) total += data[index - offset].close
      return total / period
    }
    const rsi = (period: number) => {
      if (index < period) return null
      let gains = 0
      let losses = 0
      for (let offset = 0; offset < period; offset += 1) {
        const change = closes[index - offset] - closes[index - offset - 1]
        if (change >= 0) gains += change
        else losses -= change
      }
      if (losses === 0) return gains === 0 ? 50 : 100
      return 100 - 100 / (1 + gains / losses)
    }
    let kdj = { k: null as number | null, d: null as number | null, j: null as number | null }
    if (index >= 8) {
      const window = data.slice(index - 8, index + 1)
      const high = Math.max(...window.map(item => item.high))
      const low = Math.min(...window.map(item => item.low))
      const rsv = high === low ? 50 : ((row.close - low) / (high - low)) * 100
      kValue = (2 * kValue + rsv) / 3
      dValue = (2 * dValue + kValue) / 3
      kdj = { k: kValue, d: dValue, j: 3 * kValue - 2 * dValue }
    }
    return {
      ...row,
      ma5: average(5),
      ma20: average(20),
      ma60: average(60),
      macd_dif: dif[index],
      macd_dea: dea[index],
      macd_hist: dif[index] - dea[index],
      rsi_6: rsi(6),
      rsi_14: rsi(14),
      rsi_24: rsi(24),
      kdj_k: kdj.k,
      kdj_d: kdj.d,
      kdj_j: kdj.j,
    }
  })
}

function IndicatorSwitcher({ active, onChange }: { active: string[]; onChange: (keys: string[]) => void }) {
  const toggle = (key: string) => {
    onChange(active.includes(key) ? active.filter(item => item !== key) : [...active, key])
  }
  return (
    <div className="flex flex-wrap items-center gap-1.5 px-2 pb-2 text-[10px]">
      <span className="mr-1 text-muted">副图指标</span>
      {SUB_INDICATORS.map(item => {
        const selected = active.includes(item.key)
        return <button key={item.key} type="button" onClick={() => toggle(item.key)} className={`rounded border px-2 py-1 transition-colors ${selected ? 'border-accent/60 bg-accent/15 text-accent' : 'border-border/60 text-muted hover:text-foreground'}`}>{item.label}</button>
      })}
    </div>
  )
}

function RecordChart({ record }: { record: ChanTrainingRecord }) {
  const [activeIndicators, setActiveIndicators] = useState<string[]>(['vol'])
  const [inspectionId, setInspectionId] = useState('')
  const chartData = useMemo(() => addMovingAverages(toOHLC(record.bars)), [record.bars])
  const chan = record.current_structure
  const markers = useMemo(() => markersForStructure(chan, record.actions, chartData), [chan, record.actions, chartData])
  const ranges = useMemo(() => rangesForStructure(chan), [chan])
  const structureLines = useMemo(() => structureLinesFor(chan, chartData), [chan, chartData])
  const inspection = useMemo(
    () => structureInspection(inspectionId, chan, record.actions, chartData, true),
    [inspectionId, chan, record.actions, chartData],
  )
  return (
    <section className="rounded-btn border border-border/70 bg-surface p-2">
      <IndicatorSwitcher active={activeIndicators} onChange={setActiveIndicators} />
      {chartData.length > 0 ? (
        <EChartsCandlestick
          data={chartData}
          markers={markers}
          ranges={ranges}
          structureLines={structureLines}
          height={620}
          visibleBars={60}
          showMA={true}
          centerMovingAverages={true}
          showInfoBar={true}
          showDateLabels={true}
          activeIndicators={activeIndicators}
          onInspectionClick={setInspectionId}
        />
      ) : <EmptyState title="暂无历史 K 线" hint="该训练记录没有可显示的 K 线数据。" />}
      {chartData.length > 0 && <p className="px-2 pb-1 text-[10px] text-muted">历史训练回放 · 可在图表内左右拖动，滚轮缩放查看。</p>}
      <StructureInspectionPanel inspection={inspection} showDates={true} onClose={() => setInspectionId('')} />
    </section>
  )
}

function SummaryCards({ session }: { session: ChanTrainingSession }) {
  const items = [
    ['可用现金', money(session.cash)],
    ['持仓', `${session.position.shares.toLocaleString()} 股`],
    ['总资产', money(session.equity)],
    ['收益率', pct(session.return_pct)],
    ['最大回撤', `${session.max_drawdown_pct.toFixed(2)}%`],
  ]
  return (
    <div className="overflow-x-auto">
      <div className="grid min-w-[760px] grid-cols-5 gap-2">
        {items.map(([label, value]) => (
          <div key={label} className="flex items-center justify-between gap-3 whitespace-nowrap rounded-btn border border-border/70 bg-surface px-3 py-2">
            <span className="text-[10px] text-muted">{label}</span>
            <span className="font-mono text-sm text-foreground">{value}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

function ChanLesson({ session, showDates = false }: { session?: ChanTrainingSession; showDates?: boolean }) {
  const summary = session?.chan.summary
  return (
    <section className="rounded-btn border border-border/70 bg-surface p-2.5">
      <div className="flex items-center gap-2 text-sm font-medium text-foreground">
        {session?.analysis && (
          <span className="rounded border border-accent/40 bg-accent/10 px-1.5 py-0.5 text-[10px] text-accent">日线递归</span>
        )}
        {session?.chan.algorithm_source && (
          <span className="rounded border border-border/70 px-1.5 py-0.5 font-mono text-[10px] text-secondary">{session.chan.algorithm_source}</span>
        )}
        <BookOpenCheck className="h-4 w-4 text-accent" />
        缠论观察卡
      </div>
      <div className="mt-1.5 grid gap-x-3 gap-y-1 text-[11px] leading-snug text-secondary sm:grid-cols-2">
        <div><span className="text-foreground">分型：</span>局部顶点或低点，是识别笔的基础。</div>
        <div><span className="text-foreground">笔：</span>相邻确认分型之间的方向连接。</div>
        <div><span className="text-foreground">线段：</span>当前 CZSC 上游核心只提供分型、笔和中枢，未提供独立线段生成算法，本模块不使用本地近似线段。</div>
        <div><span className="text-foreground">中枢：</span>连续走势重叠形成的价格区间。</div>
      </div>
      {summary && (
        <div className="mt-2 flex flex-wrap gap-2 text-[10px] font-mono text-muted">
          <span>分型 {summary.fractals}</span>
          <span>笔 {summary.strokes}</span>
          <span>线段：上游未提供</span>
          <span>中枢 {summary.centers}</span>
        </div>
      )}
      <div className="mt-2 flex items-center gap-x-3 overflow-x-auto whitespace-nowrap border-t border-border/50 pt-1.5 text-[10px] leading-snug text-muted">
        {session?.analysis && (
          <>
            {session.analysis.daily_quality && <span className={session.analysis.daily_quality.valid ? 'text-emerald-400' : 'text-danger'}>日线质量：{session.analysis.daily_quality.valid ? '通过' : `${session.analysis.daily_quality.error_count} 错误`}</span>}
            {session.analysis.data_warnings.map(warning => <span key={warning} className="text-warning/80">{visibleTrainingWarning(warning, session, showDates)}</span>)}
          </>
        )}
        <span><span className="mr-1 inline-block w-4 align-middle border-t border-dashed border-sky-400" />笔</span>
        <span>线段：上游未提供</span>
        <span><span className="mr-1 inline-block h-3 w-4 align-middle rounded border border-violet-400/70 bg-violet-400/10" />中枢</span>
        <span>顶/底标记 = 分型</span>
        {session?.chan.signal_catalog?.map(signal => <span key={signal.key} title={`${signal.description}；依赖 ${signal.dependencies.join('/')}`}>{POINT_LABELS[signal.class as keyof typeof POINT_LABELS] ?? signal.class}类：{signal.name}</span>)}
        {session?.chan.events && <span>教学事件 {session.chan.events.length}</span>}
        {session?.chan.points.slice(-3).map(point => <span key={`${point.date}-${point.type}-${point.class}`} title={`${point.signal_key ?? point.signal_name ?? ''} · ${point.reason ?? ''}`}>最近信号：{point.type === 'buy' ? '买' : '卖'}{POINT_LABELS[point.class]} · CZSC 已触发</span>)}
        <span className="text-warning/80">分型、笔、中枢与买卖辅助信号按移植的 CZSC 核心计算；当前上游未提供独立线段算法。仅用于训练和复盘，不构成投资建议。</span>
      </div>
      <SignalEvidenceList points={session?.chan.points ?? []} showDates={showDates} />
    </section>
  )
}

function ActionList({ session }: { session: ChanTrainingSession }) {
  return (
    <section className="rounded-btn border border-border/70 bg-surface p-2.5">
      <div className="flex items-center gap-2 text-sm font-medium text-foreground">
        <History className="h-4 w-4 text-accent" />
        操作记录
      </div>
      {session.actions.length === 0 ? <p className="mt-2 text-[11px] text-muted">操作将在这里按揭示顺序记录。</p> : (
        <div className="mt-1.5 max-h-28 space-y-0.5 overflow-y-auto">
          {session.actions.map((item, index) => <div key={`${item.date}-${index}`} className="flex items-center gap-2 text-xs"><span className={item.side === 'buy' ? 'text-bull' : 'text-bear'}>{item.side === 'buy' ? '买入' : '卖出'}</span><span className="font-mono text-secondary">{item.percentage}% · {item.shares}股</span><span className="ml-auto font-mono text-muted">{item.execution_price.toFixed(2)}</span></div>)}
        </div>
      )}
    </section>
  )
}

export function ChanTrainingPage() {
  const qc = useQueryClient()
  const [session, setSession] = useState<ChanTrainingSession | null>(null)
  const [record, setRecord] = useState<ChanTrainingRecord | null>(null)
  const [side, setSide] = useState<'buy' | 'sell'>('buy')
  const [percentage, setPercentage] = useState('25')
  const [metadataVisible, setMetadataVisible] = useState(false)
  const [aiContent, setAiContent] = useState('')
  const [aiState, setAiState] = useState<'idle' | 'working' | 'done' | 'error'>('idle')
  const [selectedRecordId, setSelectedRecordId] = useState('')
  const [commissionPct, setCommissionPct] = useState('0.02')
  const [stampTaxPct, setStampTaxPct] = useState('0.05')
  const [slippageBps, setSlippageBps] = useState('5')
  const [restoring, setRestoring] = useState(true)
  const [activeIndicators, setActiveIndicators] = useState<string[]>(['vol'])
  const [inspectionId, setInspectionId] = useState('')
  const [intradaySelection, setIntradaySelection] = useState<{
    sessionId: string
    date: string
    anchor: ChartClickAnchor
  } | null>(null)
  const intradayPopupRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    const sessionId = storage.chanTrainingSessionId.get('')
    if (!sessionId) {
      setRestoring(false)
      return
    }
    api.chanTrainingSession(sessionId)
      .then(result => {
        if (result.session.status !== 'active') {
          storage.chanTrainingSessionId.set('')
          return
        }
        setSession(result.session)
        setCommissionPct((result.session.cost_model.commission_pct * 100).toString())
        setStampTaxPct((result.session.cost_model.stamp_tax_pct * 100).toString())
        setSlippageBps(result.session.cost_model.slippage_bps.toString())
      })
      .catch(() => storage.chanTrainingSessionId.set(''))
      .finally(() => setRestoring(false))
  }, [])

  const historyQuery = useQuery({
    queryKey: QK.chanTrainingRecords,
    queryFn: api.chanTrainingRecords,
  })
  const selectedHistory = useQuery({
    queryKey: QK.chanTrainingRecord(selectedRecordId),
    queryFn: () => api.chanTrainingRecord(selectedRecordId),
    enabled: !!selectedRecordId && !record,
  })

  const start = useMutation({
    mutationFn: api.chanTrainingStart,
    onSuccess: result => {
      setSession(result.session)
      storage.chanTrainingSessionId.set(result.session.id)
      setRecord(null)
      setSelectedRecordId('')
      setMetadataVisible(false)
      setAiContent('')
      setAiState('idle')
      setInspectionId('')
    },
  })
  const next = useMutation({
    mutationFn: (id: string) => api.chanTrainingNext(id),
    onSuccess: result => {
      setSession(result.session)
    },
  })
  const action = useMutation({
    mutationFn: ({ id, value, quickSide }: { id: string; value: number; quickSide?: 'buy' | 'sell' }) => api.chanTrainingAction(id, quickSide ?? side, value),
    onSuccess: result => setSession(result.session),
  })
  const finish = useMutation({
    mutationFn: (id: string) => api.chanTrainingFinish(id),
    onSuccess: result => {
      setSession(result.session)
      setRecord(result.record)
      storage.chanTrainingSessionId.set('')
      setMetadataVisible(true)
      setAiContent('')
      setAiState('idle')
      setInspectionId('')
      qc.invalidateQueries({ queryKey: QK.chanTrainingRecords })
    },
  })
  const replay = useMutation({
    mutationFn: api.chanTrainingReplay,
    onSuccess: result => {
      setSession(result.session)
      storage.chanTrainingSessionId.set(result.session.id)
      setRecord(null)
      setSelectedRecordId('')
      setMetadataVisible(false)
      setAiContent('')
      setAiState('idle')
      setInspectionId('')
      setCommissionPct((result.session.cost_model.commission_pct * 100).toString())
      setStampTaxPct((result.session.cost_model.stamp_tax_pct * 100).toString())
      setSlippageBps(result.session.cost_model.slippage_bps.toString())
      toast('已按历史记录加载同一训练题目', 'success')
    },
    onError: error => toast(String(error instanceof Error ? error.message : error), 'error'),
  })
  const discard = useMutation({
    mutationFn: ({ id }: { id: string; costs: ChanTrainingStartRequest }) => api.chanTrainingDiscard(id),
    onSuccess: (_result, variables) => {
      storage.chanTrainingSessionId.set('')
      setSession(null)
      setRecord(null)
      setInspectionId('')
      start.mutate(variables.costs)
    },
    onError: error => toast(String(error instanceof Error ? error.message : error), 'error'),
  })

  const current = session
  const chartRows = current?.rows ?? []
  const chartData = useMemo(() => addMovingAverages(toOHLC(chartRows)), [chartRows])
  const markers = useMemo(() => current ? markersFor(current, chartData, metadataVisible) : [], [current, chartData, metadataVisible])
  const ranges = useMemo(() => current ? rangesFor(current) : [], [current])
  const structureLines = useMemo(() => current ? structureLinesFor(current.chan, chartData) : [], [current, chartData])
  const selectedIntradayDate = current && intradaySelection?.sessionId === current.id
    ? intradaySelection.date
    : null
  const selectedIntradayIndex = selectedIntradayDate
    ? chartData.findIndex(row => row.date === selectedIntradayDate)
    : -1
  const selectedIntradayBar = selectedIntradayIndex >= 0 ? chartData[selectedIntradayIndex] : undefined
  const selectedIntradayPrevClose = selectedIntradayIndex > 0
    ? chartData[selectedIntradayIndex - 1].close
    : undefined
  const intradayPopupStyle = useMemo(() => {
    if (!intradaySelection || typeof window === 'undefined') return undefined
    const gap = 12
    const width = Math.min(620, Math.max(320, window.innerWidth - gap * 2))
    const height = 410
    const hasRoomOnRight = window.innerWidth - intradaySelection.anchor.clientX >= width + gap
    const left = hasRoomOnRight
      ? intradaySelection.anchor.clientX + gap
      : Math.max(gap, intradaySelection.anchor.clientX - width - gap)
    const top = Math.min(
      Math.max(gap, intradaySelection.anchor.clientY - 100),
      Math.max(gap, window.innerHeight - height - gap),
    )
    return { left, top, width }
  }, [intradaySelection])
  const historyRecord = record ?? selectedHistory.data?.record ?? null
  const currentInspection = useMemo(
    () => current ? structureInspection(inspectionId, current.chan, current.actions, chartData, metadataVisible) : null,
    [inspectionId, current, chartData, metadataVisible],
  )
  const busy = start.isPending || next.isPending || action.isPending || finish.isPending || replay.isPending || discard.isPending

  useEffect(() => {
    if (!selectedIntradayDate) return
    const closeOutside = (event: PointerEvent) => {
      const target = event.target
      if (target instanceof Node && intradayPopupRef.current?.contains(target)) return
      setIntradaySelection(null)
    }
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setIntradaySelection(null)
    }
    const closeOnResize = () => setIntradaySelection(null)
    document.addEventListener('pointerdown', closeOutside)
    document.addEventListener('keydown', closeOnEscape)
    window.addEventListener('resize', closeOnResize)
    return () => {
      document.removeEventListener('pointerdown', closeOutside)
      document.removeEventListener('keydown', closeOnEscape)
      window.removeEventListener('resize', closeOnResize)
    }
  }, [selectedIntradayDate])

  const currentCosts = (): ChanTrainingStartRequest | null => {
    const commission = Number(commissionPct)
    const stampTax = Number(stampTaxPct)
    const slippage = Number(slippageBps)
    if (!Number.isFinite(commission) || commission < 0 || commission > 5
      || !Number.isFinite(stampTax) || stampTax < 0 || stampTax > 5
      || !Number.isFinite(slippage) || slippage < 0 || slippage > 1000) {
      toast('佣金和印花税应为 0 到 5%，滑点应为 0 到 1000 bps', 'error')
      return null
    }
    return { commission_pct: commission / 100, stamp_tax_pct: stampTax / 100, slippage_bps: slippage }
  }

  const startTraining = () => {
    const costs = currentCosts()
    if (costs) start.mutate(costs)
  }

  const restartActiveTraining = () => {
    if (!session || session.status !== 'active' || busy) return
    const costs = currentCosts()
    if (!costs) return
    if (window.confirm('放弃当前未结束训练并随机更换一只股票吗？当前进度不会保存。')) {
      discard.mutate({ id: session.id, costs })
    }
  }

  const runAction = () => {
    if (!session) return
    const value = Number(percentage)
    if (!Number.isFinite(value) || value <= 0 || value > 100) {
      toast('百分比必须在 1 到 100 之间', 'error')
      return
    }
    action.mutate({ id: session.id, value, quickSide: side })
  }

  const runQuickAction = (quickSide: 'buy' | 'sell', value: number) => {
    if (!session || busy) return
    action.mutate({ id: session.id, value, quickSide })
  }

  const runAi = async () => {
    if (!historyRecord) return
    setAiContent('')
    setAiState('working')
    try {
      for await (const event of api.chanTrainingAnalyze(historyRecord.training_id)) {
        if (event.type === 'delta') setAiContent(value => value + (event.content ?? ''))
        if (event.type === 'error') throw new Error(event.message || 'AI 分析失败')
        if (event.type === 'done') setAiState('done')
      }
      qc.invalidateQueries({ queryKey: QK.chanTrainingRecord(historyRecord.training_id) })
      qc.invalidateQueries({ queryKey: QK.chanTrainingRecords })
    } catch (error) {
      setAiState('error')
      toast(String(error instanceof Error ? error.message : error), 'error')
    }
  }

  const returnToTrainingHome = () => {
    setSession(null)
    setRecord(null)
    setSelectedRecordId('')
    setMetadataVisible(false)
    setAiContent('')
    setAiState('idle')
    setInspectionId('')
  }

  const deleteHistory = useMutation({
    mutationFn: api.chanTrainingDeleteRecord,
    onSuccess: (_result, trainingId) => {
      if (record?.training_id === trainingId || selectedRecordId === trainingId) returnToTrainingHome()
      qc.invalidateQueries({ queryKey: QK.chanTrainingRecords })
      qc.removeQueries({ queryKey: QK.chanTrainingRecord(trainingId) })
      toast('训练记录已删除', 'success')
    },
    onError: error => toast(String(error instanceof Error ? error.message : error), 'error'),
  })

  const confirmDeleteHistory = (trainingId: string) => {
    if (deleteHistory.isPending) return
    if (window.confirm('确定删除这条训练记录及其 AI 分析吗？删除后无法恢复。')) deleteHistory.mutate(trainingId)
  }

  if (!session) {
    return (
      <div className="flex h-full min-h-0 flex-col overflow-hidden bg-base">
        <PageHeader
          title="缠论训练"
          subtitle="随机题目 · 逐根揭示 · 交易复盘"
          right={selectedHistory.data?.record ? (
            <button type="button" onClick={returnToTrainingHome} className="inline-flex h-8 items-center gap-1.5 rounded-btn border border-border bg-surface px-2.5 text-xs text-secondary hover:text-foreground">
              <ArrowLeft className="h-3.5 w-3.5" />
              返回训练首页
            </button>
          ) : undefined}
        />
        <div className="min-h-0 flex-1 overflow-hidden px-3 pb-5 pt-3 lg:px-5">
          <div className="grid h-full min-h-0 gap-3 lg:grid-cols-[16rem_minmax(0,1fr)]">
            <HistorySidebar records={historyQuery.data?.records ?? []} onSelect={id => setSelectedRecordId(id)} onDelete={confirmDeleteHistory} />
            <main className="min-h-0 overflow-y-auto">
        <div className="grid min-h-full place-items-center px-4 py-10">
          {!selectedHistory.data?.record && (
          <div className="w-full max-w-2xl rounded-btn border border-border/70 bg-surface p-6 text-center">
            <BarChart3 className="mx-auto h-10 w-10 text-accent" strokeWidth={1.5} />
            <h2 className="mt-4 text-lg font-semibold text-foreground">开始一局缠论训练</h2>
            <p className="mx-auto mt-2 max-w-md text-sm leading-relaxed text-secondary">
              系统会从全市场有效股票中随机抽取一段历史日线。初始展示训练日前最多两年 K 线，之后最多逐根揭示 60 根。
            </p>
            <div className="mx-auto mt-6 grid max-w-lg gap-2 rounded border border-border/60 bg-base p-3 text-left sm:grid-cols-3">
              <label className="text-xs text-secondary">
                佣金 (%)
                <input value={commissionPct} onChange={event => setCommissionPct(event.target.value)} inputMode="decimal" min="0" max="5" step="0.01" className="mt-1 h-8 w-full rounded border border-border bg-surface px-2 font-mono text-xs text-foreground outline-none focus:border-accent" />
              </label>
              <label className="text-xs text-secondary">
                印花税 (%)
                <input value={stampTaxPct} onChange={event => setStampTaxPct(event.target.value)} inputMode="decimal" min="0" max="5" step="0.01" className="mt-1 h-8 w-full rounded border border-border bg-surface px-2 font-mono text-xs text-foreground outline-none focus:border-accent" />
              </label>
              <label className="text-xs text-secondary">
                滑点 (bps)
                <input value={slippageBps} onChange={event => setSlippageBps(event.target.value)} inputMode="decimal" min="0" max="1000" step="1" className="mt-1 h-8 w-full rounded border border-border bg-surface px-2 font-mono text-xs text-foreground outline-none focus:border-accent" />
              </label>
            </div>
            <p className="mt-2 text-[10px] text-muted">默认佣金 0.02% · 印花税 0.05%（卖出收取）· 滑点 5 bps，可在开始前修改。</p>
            <button
              type="button"
              onClick={startTraining}
              disabled={start.isPending || restoring}
              className="mt-6 inline-flex h-9 items-center gap-2 rounded-btn bg-accent px-4 text-sm font-medium text-white transition-colors hover:bg-accent/90 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {start.isPending || restoring ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
              {restoring ? '恢复训练中' : '随机开始训练'}
            </button>
            <div className="mt-6 grid gap-2 text-left text-xs text-secondary sm:grid-cols-3">
              <div className="rounded border border-border/60 p-2">分型、笔、中枢</div>
              <div className="rounded border border-border/60 p-2">百分比买卖、T+1 约束</div>
              <div className="rounded border border-border/60 p-2">结束后保存回测记录</div>
            </div>
          </div>
          )}
          {selectedHistory.data?.record && (
          <div className="w-full max-w-none space-y-3">
              <RecordChart record={selectedHistory.data.record} />
              <RecordPanel record={selectedHistory.data.record} aiContent={aiContent} aiState={aiState} onAnalyze={runAi} onReplay={() => replay.mutate(selectedHistory.data.record.training_id)} replaying={replay.isPending} />
            </div>
          )}
        </div>
        {record && <div className="w-full max-w-5xl"><RecordPanel record={record} aiContent={aiContent} aiState={aiState} onAnalyze={runAi} onReplay={() => replay.mutate(record.training_id)} replaying={replay.isPending} /></div>}
        {selectedRecordId && selectedHistory.isLoading && <div className="px-4 pb-4 text-xs text-muted">正在读取训练记录…</div>}
            </main>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden bg-base">
      <PageHeader
        title="缠论训练"
        subtitle={metadataVisible ? `${session.name} ${session.symbol}` : '盲训模式'}
        right={(
          <div className="flex items-center gap-2">
            <span className="font-mono text-xs text-secondary">第 {session.progress.revealed}/{session.progress.total} 根</span>
            {session.status === 'active' && (
              <button type="button" onClick={restartActiveTraining} disabled={busy} className="inline-flex h-8 items-center gap-1.5 rounded-btn border border-border bg-surface px-2.5 text-xs text-secondary hover:text-foreground disabled:opacity-50" title="放弃当前进度并随机换题">
                <RefreshCw className="h-3.5 w-3.5" />
                重新开始
              </button>
            )}
            {session.status === 'finished' && (
              <>
                <button type="button" onClick={returnToTrainingHome} disabled={busy} className="inline-flex h-8 items-center gap-1.5 rounded-btn border border-border bg-surface px-2.5 text-xs text-secondary hover:text-foreground disabled:opacity-50">
                  <ArrowLeft className="h-3.5 w-3.5" />
                  返回训练首页
                </button>
                <button type="button" onClick={startTraining} disabled={busy} className="inline-flex h-8 items-center gap-1.5 rounded-btn border border-border bg-surface px-2.5 text-xs text-secondary hover:text-foreground disabled:opacity-50">
                  <RefreshCw className="h-3.5 w-3.5" />
                  重新开始
                </button>
              </>
            )}
            <button
              type="button"
              onClick={() => setMetadataVisible(value => !value)}
              className="inline-flex h-8 w-8 items-center justify-center rounded-btn border border-border bg-surface text-muted hover:text-foreground"
              title={metadataVisible ? '隐藏股票信息和时间' : '显示股票信息和时间'}
              aria-label={metadataVisible ? '隐藏股票信息和时间' : '显示股票信息和时间'}
            >
              {metadataVisible ? <Eye className="h-4 w-4" /> : <EyeOff className="h-4 w-4" />}
            </button>
          </div>
        )}
        className="shrink-0 flex-wrap gap-x-4 gap-y-2 px-3 lg:px-5"
      />

      <div className="min-h-0 flex-1 overflow-hidden px-3 pb-5 pt-3 lg:px-5">
        <div className="grid h-full min-h-0 gap-3 lg:grid-cols-[16rem_minmax(0,1fr)]">
        {session.status !== 'active' && <HistorySidebar records={historyQuery.data?.records ?? []} onSelect={id => setSelectedRecordId(id)} onDelete={confirmDeleteHistory} />}
      <main className={`min-h-0 space-y-3 overflow-y-auto ${session.status === 'active' ? 'lg:col-span-2' : ''}`}>
        <div className="rounded-btn border border-border/70 bg-surface p-2">
          <IndicatorSwitcher active={activeIndicators} onChange={setActiveIndicators} />
          {chartData.length > 0 ? (
            <EChartsCandlestick
              data={chartData}
              markers={markers}
              ranges={ranges}
              structureLines={structureLines}
              height={620}
              visibleBars={60}
              showMA={true}
              centerMovingAverages={true}
              showInfoBar={true}
              showInfoDate={metadataVisible}
              showSinceLatest={true}
              showDateLabels={metadataVisible}
              selectedDate={selectedIntradayDate}
              activeIndicators={activeIndicators}
              onInspectionClick={setInspectionId}
              onDateClick={(date, anchor) => {
                if (!anchor) return
                setIntradaySelection({ sessionId: session.id, date, anchor })
              }}
            />
          ) : <EmptyState title="暂无训练 K 线" hint="当前题目没有可显示的日线数据。" />}
          {chartData.length > 0 && <p className="px-2 pb-1 text-[10px] text-muted">默认展示最新揭露的 60 根 K 线；两年历史均已载入，可向左拖动或滚轮缩放查看。点击任意日 K 可在蜡烛旁查看该日分时。</p>}
          <StructureInspectionPanel inspection={currentInspection} showDates={metadataVisible} onClose={() => setInspectionId('')} />
        </div>

        {selectedIntradayDate && selectedIntradayBar && intradayPopupStyle && createPortal(
          <Modal
            onClose={() => setIntradaySelection(null)}
            ariaLabel="所选日分时"
            closeOnBackdrop={false}
            interactionMode="floating"
            draggable={true}
            panelElementRef={(element) => { intradayPopupRef.current = element }}
            overlayClassName="pointer-events-none fixed inset-0 z-[80]"
            panelClassName="pointer-events-auto fixed overflow-hidden rounded-btn border border-accent/50 bg-surface p-2 shadow-2xl"
            panelStyle={intradayPopupStyle}
          >
            <div data-modal-drag-handle className="flex cursor-move select-none items-center justify-between gap-3 border-b border-border/60 px-1 pb-2">
              <div className="flex items-center gap-2">
                <h2 className="text-sm font-medium text-foreground">所选日分时</h2>
                {metadataVisible && <span className="font-mono text-xs text-secondary">{selectedIntradayDate}</span>}
              </div>
              <button
                type="button"
                onClick={() => setIntradaySelection(null)}
                className="inline-flex h-7 w-7 items-center justify-center rounded border border-border text-muted transition-colors hover:bg-elevated hover:text-foreground"
                title="关闭分时窗口"
                aria-label="关闭分时窗口"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            </div>
            <StockIntradayChart
              symbol={session.symbol}
              date={selectedIntradayDate}
              height={360}
              prevClose={selectedIntradayPrevClose}
              currentPrice={chartData[chartData.length - 1]?.close}
              dailyOhlc={selectedIntradayBar}
              showDate={metadataVisible}
              alignToDailyClose={true}
            />
          </Modal>,
          document.body,
        )}

        <SummaryCards session={session} />

        {session.status === 'active' && (
          <div className="grid items-start gap-3 lg:grid-cols-3">
            <section className="rounded-btn border border-border/70 bg-surface p-2.5">
              <div className="flex flex-wrap items-center gap-1.5">
                <select value={side} onChange={event => setSide(event.target.value as 'buy' | 'sell')} className="h-8 rounded-btn border border-border bg-base px-2 text-xs text-foreground">
                  <option value="buy">买入</option>
                  <option value="sell">卖出</option>
                </select>
                <div className="flex h-8 items-center rounded-btn border border-border bg-base">
                  <input value={percentage} onChange={event => setPercentage(event.target.value)} inputMode="decimal" className="w-16 bg-transparent px-2 text-right font-mono text-xs text-foreground outline-none" aria-label="交易百分比" />
                  <span className="pr-2 text-xs text-muted">%</span>
                </div>
                <button type="button" onClick={runAction} disabled={busy} className="inline-flex h-8 items-center gap-1 rounded-btn bg-accent px-2.5 text-xs font-medium text-white hover:bg-accent/90 disabled:cursor-not-allowed disabled:opacity-50">
                  {action.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />}
                  执行
                </button>
                <button type="button" onClick={() => next.mutate(session.id)} disabled={busy || !session.can_next} className="inline-flex h-8 items-center gap-1 rounded-btn border border-border px-2.5 text-xs text-secondary hover:bg-elevated hover:text-foreground disabled:cursor-not-allowed disabled:opacity-40" title="揭示下一根 K 线">
                  {next.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />}
                  下一根
                </button>
                <button type="button" onClick={() => finish.mutate(session.id)} disabled={busy} className="inline-flex h-8 items-center gap-1 rounded-btn border border-danger/40 px-2.5 text-xs text-danger hover:bg-danger/10 disabled:cursor-not-allowed disabled:opacity-40">
                  {finish.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Square className="h-3.5 w-3.5" />}
                  结束训练
                </button>
              </div>
              <div className="mt-2 grid gap-1.5 border-t border-border/50 pt-2 text-xs">
                <div className="grid grid-cols-[2.75rem_repeat(4,minmax(0,1fr))] items-center gap-1.5">
                  <span className="text-bull" title="按当前可用现金比例买入">加仓</span>
                  {BUY_SHORTCUTS.map(item => <button key={item.label} type="button" onClick={() => runQuickAction('buy', item.value)} disabled={busy} title={`使用当前可用现金的 ${item.label}`} className="h-7 rounded border border-bull/30 bg-bull/5 px-1.5 font-mono text-bull hover:bg-bull/15 disabled:cursor-not-allowed disabled:opacity-40">{item.label}</button>)}
                </div>
                <div className="grid grid-cols-[2.75rem_repeat(4,minmax(0,1fr))] items-center gap-1.5">
                  <span className="text-bear" title="按当前 T+1 可卖持仓比例卖出">减仓</span>
                  {SELL_SHORTCUTS.map(item => <button key={item.label} type="button" onClick={() => runQuickAction('sell', item.value)} disabled={busy} title={`卖出当前 T+1 可卖持仓的 ${item.label}`} className="h-7 rounded border border-bear/30 bg-bear/5 px-1.5 font-mono text-bear hover:bg-bear/15 disabled:cursor-not-allowed disabled:opacity-40">{item.label}</button>)}
                </div>
              </div>
              <p className="mt-1.5 text-[10px] leading-snug text-muted">每次加仓按现有可用现金计算，减仓按现有 T+1 可卖持仓计算；不是按总资产计算，股数按 100 股/1 手向下取整。</p>
            </section>
            <ChanLesson session={session} showDates={metadataVisible} />
            <ActionList session={session} />
          </div>
        )}

        {record && <RecordPanel record={record} aiContent={aiContent} aiState={aiState} onAnalyze={runAi} onReplay={() => replay.mutate(record.training_id)} replaying={replay.isPending} />}
        {selectedHistory.data?.record && !record && <RecordPanel record={selectedHistory.data.record} aiContent={aiContent} aiState={aiState} onAnalyze={runAi} onReplay={() => replay.mutate(selectedHistory.data.record.training_id)} replaying={replay.isPending} />}
      </main>
        </div>
      </div>
    </div>
  )
}

function HistorySidebar({ records, onSelect, onDelete }: { records: ChanTrainingRecordSummary[]; onSelect: (id: string) => void; onDelete: (id: string) => void }) {
  return (
    <aside className="flex min-h-0 flex-col rounded-btn border border-border/70 bg-surface p-3 lg:h-full">
      <div className="flex shrink-0 items-center gap-2 text-sm font-medium text-foreground"><History className="h-4 w-4 text-accent" />历史训练</div>
      <div className="mt-3 min-h-0 flex-1 space-y-2 overflow-y-auto pr-1">
        {records.length === 0 ? <p className="text-xs text-muted">完成训练后，记录会显示在这里。</p> : records.map(item => (
          <div key={item.training_id} className="rounded-btn border border-border/60 bg-base p-2 hover:border-accent/50">
            <div className="flex items-start gap-1">
              <button type="button" onClick={() => onSelect(item.training_id)} className="min-w-0 flex-1 text-left">
                <div className="flex items-center justify-between gap-2"><span className="truncate text-xs text-foreground">{item.name} <span className="font-mono text-muted">{item.symbol}</span></span><span className={item.summary.total_return_pct >= 0 ? 'shrink-0 font-mono text-xs text-bull' : 'shrink-0 font-mono text-xs text-bear'}>{pct(item.summary.total_return_pct)}</span></div>
                <div className="mt-1 text-[10px] text-muted">{item.start_date} → {item.end_date}</div>
                <div className="mt-1 text-[10px] text-muted">{item.summary.trade_count} 次交易动作</div>
              </button>
              <button type="button" onClick={() => onDelete(item.training_id)} className="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded border border-transparent text-muted hover:border-danger/30 hover:bg-danger/10 hover:text-danger" title="删除训练记录" aria-label={`删除 ${item.name} 训练记录`}>
                <Trash2 className="h-3.5 w-3.5" />
              </button>
            </div>
          </div>
        ))}
      </div>
    </aside>
  )
}

function RecordPanel({ record, aiContent, aiState, onAnalyze, onReplay, replaying }: { record: ChanTrainingRecord; aiContent: string; aiState: 'idle' | 'working' | 'done' | 'error'; onAnalyze: () => void; onReplay: () => void; replaying: boolean }) {
  const [diagnosticsOpen, setDiagnosticsOpen] = useState(false)
  const diagnostics = useQuery({
    queryKey: ['chan-training-diagnostics', record.training_id],
    queryFn: () => api.chanTrainingRecordDiagnostics(record.training_id),
    enabled: diagnosticsOpen,
  })
  const reportContent = aiContent || record.ai_reports?.[0]?.content || ''
  return (
    <section className="rounded-btn border border-border/70 bg-surface p-4">
      <div className="mb-2 flex flex-wrap items-center gap-2 text-[10px] text-muted">
        {record.chan_algorithm_source && <span className="rounded border border-border/60 px-1.5 py-0.5 font-mono">{record.chan_algorithm_source}</span>}
        {record.data_version && <span title={record.data_version.training_rows_sha256} className="rounded border border-border/60 px-1.5 py-0.5 font-mono">数据 {record.data_version.training_rows_sha256.slice(0, 12)} · {record.data_version.training_row_count} 根</span>}
        {record.analysis && <><span className="rounded border border-accent/40 bg-accent/10 px-1.5 py-0.5 text-accent">日线递归</span>{record.analysis.daily_quality && <span>日线质量：{record.analysis.daily_quality.valid ? '通过' : `${record.analysis.daily_quality.error_count} 个错误`}</span>}{record.analysis.data_warnings.map(warning => <span key={warning} className="text-warning/80">{warning}</span>)}</>}
      </div>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div><h2 className="text-sm font-semibold text-foreground">训练回测记录</h2><p className="mt-1 text-xs text-secondary">{record.name} {record.symbol} · {record.start_date} → {record.end_date}</p></div>
        <div className="flex flex-wrap items-center gap-2">
        {record.training_plan && <button type="button" onClick={onReplay} disabled={replaying} className="inline-flex h-8 items-center gap-1.5 rounded-btn border border-accent/40 bg-accent/10 px-3 text-xs text-accent hover:bg-accent/20 disabled:opacity-50">
          {replaying ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
          {replaying ? '加载原题中' : '重练本题'}
        </button>}
        <button type="button" onClick={() => setDiagnosticsOpen(value => !value)} className="inline-flex h-8 items-center gap-1.5 rounded-btn border border-border/70 px-3 text-xs text-secondary hover:text-foreground">
          <BarChart3 className="h-3.5 w-3.5" />
          {diagnosticsOpen ? '收起结构诊断' : '结构诊断'}
        </button>
        <button type="button" onClick={onAnalyze} disabled={aiState === 'working'} className="inline-flex h-8 items-center gap-1.5 rounded-btn border border-purple-400/40 bg-purple-400/10 px-3 text-xs text-purple-300 hover:bg-purple-400/20 disabled:opacity-50">
          {aiState === 'working' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5" />}
          {aiState === 'working' ? '分析中' : 'AI 分析交易习惯'}
        </button>
        </div>
      </div>
        <p className="mt-3 text-[10px] text-muted">成本：佣金 {(record.cost_model.commission_pct * 100).toFixed(3)}% · 印花税 {(record.cost_model.stamp_tax_pct * 100).toFixed(3)}% · 滑点 {record.cost_model.slippage_bps} bps</p>
        <div className="mt-3 grid grid-cols-2 gap-2 text-xs sm:grid-cols-4">
        <div className="rounded border border-border/60 p-2"><span className="text-muted">最终资产</span><div className="mt-1 font-mono">{money(record.summary.final_equity)}</div></div>
        <div className="rounded border border-border/60 p-2"><span className="text-muted">已实现盈亏</span><div className="mt-1 font-mono">{money(record.summary.realized_pnl)}</div></div>
        <div className="rounded border border-border/60 p-2"><span className="text-muted">最大回撤</span><div className="mt-1 font-mono">{record.summary.max_drawdown_pct.toFixed(2)}%</div></div>
        <div className="rounded border border-border/60 p-2"><span className="text-muted">胜率</span><div className="mt-1 font-mono">{record.summary.win_rate_pct.toFixed(2)}%</div></div>
      </div>
      {record.actions.length > 0 && <div className="mt-3 overflow-x-auto"><table className="w-full min-w-[620px] text-left text-xs"><thead className="text-muted"><tr><th className="px-2 py-1">方向</th><th className="px-2 py-1">日期</th><th className="px-2 py-1">比例</th><th className="px-2 py-1">成交价</th><th className="px-2 py-1">股数</th><th className="px-2 py-1">盈亏</th></tr></thead><tbody>{record.actions.map((item, index) => <tr key={`${item.date}-${index}`} className="border-t border-border/40"><td className={item.side === 'buy' ? 'px-2 py-1 text-bull' : 'px-2 py-1 text-bear'}>{item.side === 'buy' ? '买入' : '卖出'}{item.trigger === 'auto_liquidation' ? '·自动' : ''}</td><td className="px-2 py-1 font-mono text-secondary">{item.date}</td><td className="px-2 py-1 font-mono">{item.percentage}%</td><td className="px-2 py-1 font-mono">{item.execution_price.toFixed(2)}</td><td className="px-2 py-1 font-mono">{item.shares}</td><td className="px-2 py-1 font-mono">{item.pnl_amount == null ? '--' : money(item.pnl_amount)}</td></tr>)}</tbody></table></div>}
      <SignalEvidenceList points={record.current_structure.points} showDates={true} />
      {diagnosticsOpen && <div className="mt-3 border-t border-border/60 pt-3">
        {diagnostics.isLoading && <p className="text-xs text-muted">正在重放结构并检查数据质量…</p>}
        {diagnostics.isError && <p className="text-xs text-danger">结构诊断读取失败，请稍后重试。</p>}
        {diagnostics.data && <div className="grid gap-2 text-[10px] text-muted sm:grid-cols-2">
          <div className="rounded border border-border/60 p-2">
            <div className="font-medium text-foreground">计算来源</div>
            <div className="mt-1 font-mono">{diagnostics.data.structure.algorithm_source ?? '当前缠论结构'} · profile {record.chan_signal_profile_id ?? 'default'}</div>
            <div className="mt-1">逐根变化：{diagnostics.data.structure.replay?.length ?? 0} 次 · 分型 {diagnostics.data.structure.summary?.fractals ?? 0} · 笔 {diagnostics.data.structure.summary?.strokes ?? 0} · 中枢 {diagnostics.data.structure.summary?.centers ?? 0}</div>
            {diagnostics.data.structure.indicator_cache && <div className="mt-1">指标缓存：{diagnostics.data.structure.indicator_cache.cached_bars} 根 K</div>}
          </div>
          <div className="rounded border border-border/60 p-2">
            <div className="font-medium text-foreground">数据质量</div>
            <div className="mt-1">日线：{diagnostics.data.data_quality.daily?.valid ? '通过' : `${diagnostics.data.data_quality.daily?.error_count ?? 0} 个错误`}</div>
            {diagnostics.data.data_quality.daily?.issues.slice(0, 3).map(issue => <div key={issue.code} className="mt-1 text-warning/80">{issue.code}：{issue.count}</div>)}
          </div>
        </div>}
      </div>}
      {aiState === 'error' && <p className="mt-3 text-xs text-danger">AI 分析失败，训练记录仍已保存，可以重新点击分析。</p>}
      {reportContent && <div className="mt-4 border-t border-border/60 pt-3"><div className="mb-2 flex items-center gap-2 text-sm font-medium text-foreground"><Sparkles className="h-4 w-4 text-purple-300" />AI 交易习惯分析</div><MarkdownRenderer content={reportContent} /></div>}
    </section>
  )
}
