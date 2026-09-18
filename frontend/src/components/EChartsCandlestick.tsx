import { useEffect, useRef, useCallback, useMemo } from 'react'
import { chartTheme, getTheme, useTheme } from '@/lib/theme'
import { fmtPct } from '@/lib/format'
import * as echarts from 'echarts'
import type { ECharts, EChartsOption } from 'echarts'
import { getKlineLimitColor } from '@/lib/kline-colors'
import { chartInspectionId } from '@/lib/chart-inspection'
import { formatPriceAxisLabel } from '@/lib/chart-axis'

export interface OHLC {
  date: string
  open: number
  high: number
  low: number
  close: number
  volume?: number
  signal_limit_up?: boolean | null
  signal_limit_down?: boolean | null
  ma5?: number | null
  ma10?: number | null
  ma20?: number | null
  ma60?: number | null
  macd_dif?: number | null
  macd_dea?: number | null
  macd_hist?: number | null
  rsi_6?: number | null
  rsi_14?: number | null
  rsi_24?: number | null
  kdj_k?: number | null
  kdj_d?: number | null
  kdj_j?: number | null
  boll_upper?: number | null
  boll_lower?: number | null
}

export interface ChartMarker {
  date: string
  kind: 'buy' | 'sell' | 'neutral'
  price?: number
  label?: string
  /** 固定图形锚点在对应 K 线价格，文字仍可放在价格上下方。 */
  lockToPrice?: boolean
  /** 圆形交易标记（如训练页的 B/S）。 */
  circle?: boolean
  /** 标记相对 K 线的垂直像素偏移。 */
  offsetY?: number
  /** 同一根 K 线上同侧标签的层间距索引。 */
  labelLevel?: number
  /** 若为 true，标记放在蜡烛上方（如涨停连板标签）。 */
  above?: boolean
  /** 自定义标签颜色，覆盖默认的 kind 对应色。 */
  color?: string
  /** Tooltip/inspection text; label remains the compact chart glyph. */
  description?: string
  /** Optional opaque id reported when this marker is clicked. */
  inspectionId?: string
}

export interface ChartRange {
  start: string
  end: string
  label?: string
  color?: string
  low?: number
  high?: number
  inspectionId?: string
}

function markerLabelDistance(markers: ChartMarker[] | undefined, markerIndex: number, marker: ChartMarker): number {
  if (marker.circle) return 0
  const isAbove = marker.above ?? marker.kind === 'sell'
  const occupiedLevel = (markers ?? [])
    .slice(0, markerIndex)
    .filter(peer => !peer.circle
      && peer.date === marker.date
      && (peer.above ?? peer.kind === 'sell') === isAbove)
    .length
  const level = Math.max(marker.labelLevel ?? 0, occupiedLevel)
  return 8 + level * 18
}

function markerTooltip(marker: ChartMarker) {
  if (!marker.description) return undefined
  return {
    show: true,
    trigger: 'item',
    renderMode: 'richText',
    formatter: marker.description,
    backgroundColor: CT().tooltipBg,
    borderColor: CT().border,
    borderWidth: 1,
    textStyle: { color: CT().tooltipText, fontSize: 11, lineHeight: 17 },
    padding: [7, 9],
  }
}

export interface ChartStructureLine {
  start: string
  end: string
  startPrice: number
  endPrice: number
  color?: string
  width?: number
  type?: 'solid' | 'dashed'
  inspectionId?: string
}

export interface ChartPriceLine {
  value: number
  label?: string
  /** 在左侧价格轴显示数值, 而非图内说明文字。 */
  axisLabel?: boolean
  color?: string
  start?: string
  end?: string
}

export interface StockInfo {
  name?: string
  total_shares?: number
  float_shares?: number
  /** 扩展数据（key: configId__fieldName），来自 klineDaily 的 ext_columns */
  ext?: Record<string, unknown>
}

export interface VolumeCompareConfig {
  enabled: boolean
  days: number
}

interface SubChartContext {
  compact: boolean
  volumeCompare: VolumeCompareConfig
}

/** 子图定义 */
export interface SubChartDef {
  key: string
  label: string
  /** 子图固定高度 px */
  height: number
  /** 构建 series 数组 */
  buildSeries: (data: OHLC[], context: SubChartContext) => any[]
  /** 构建信息栏文字 (当前数据行 -> 显示内容) */
  buildInfo: (d: OHLC | null) => { label: string; color: string; value: string }[]
  /** Y 轴特殊配置 */
  yAxisConfig?: Record<string, any>
}

// ===== 成交量 N 日均量 =====
function volMaN(data: OHLC[], n: number): (number | null)[] {
  const result: (number | null)[] = []
  for (let i = 0; i < data.length; i++) {
    if (i < n - 1) { result.push(null); continue }
    let sum = 0
    for (let j = i - n + 1; j <= i; j++) sum += data[j].volume ?? 0
    result.push(sum / n)
  }
  return result
}

function fmtVol(v: number | null | undefined): string {
  if (v == null) return '—'
  if (v >= 1e8) return (v / 1e8).toFixed(2) + '亿'
  if (v >= 1e4) return (v / 1e4).toFixed(0) + '万'
  return v.toFixed(0)
}

function volumeRatioAt(data: OHLC[], index: number, days: number): number | null {
  const window = Math.max(1, Math.min(20, Math.round(days)))
  if (index < window) return null
  let sum = 0
  for (let offset = 1; offset <= window; offset++) {
    const volume = data[index - offset]?.volume
    if (volume == null || !Number.isFinite(volume)) return null
    sum += volume
  }
  const average = sum / window
  const current = data[index]?.volume
  if (current == null || !Number.isFinite(current) || average <= 0) return null
  return current / average
}

function fmtVolumeRatio(value: number | null, digits = 2): string {
  return value == null ? '—' : `${value.toFixed(digits)}x`
}

export const SUB_CHARTS: SubChartDef[] = [
  {
    key: 'vol',
    label: '成交量',
    height: 84,
    yAxisConfig: { min: 0 },
    buildSeries: (data, context) => {
      const ma5Data = volMaN(data, 5)
      const ma10Data = volMaN(data, 10)
      const compareDays = context.volumeCompare.days
      return [
        {
          name: '成交量',
          type: 'bar',
          data: data.map((d, index) => {
            const ratio = volumeRatioAt(data, index, compareDays)
            return {
              value: d.volume ?? 0,
              volumeRatioLabel: ratio == null ? '' : fmtVolumeRatio(ratio, 1),
              itemStyle: {
                color: d.close >= d.open ? 'rgba(240,68,56,0.6)' : 'rgba(18,183,106,0.6)',
              },
            }
          }),
          barWidth: '60%',
          label: {
            show: context.volumeCompare.enabled && !context.compact,
            position: 'top',
            distance: 2,
            color: CT().text,
            fontSize: 8,
            fontFamily: 'JetBrains Mono, monospace',
            formatter: (params: any) => params.data?.volumeRatioLabel ?? '',
          },
          labelLayout: { hideOverlap: true },
          animation: false,
        },
        {
          name: 'VOL5',
          type: 'line',
          data: ma5Data,
          smooth: true, symbol: 'none', animation: false,
          lineStyle: { width: 1, color: '#FACC15' },
          itemStyle: { color: '#FACC15' },
        },
        {
          name: 'VOL10',
          type: 'line',
          data: ma10Data,
          smooth: true, symbol: 'none', animation: false,
          lineStyle: { width: 1, color: '#8B5CF6' },
          itemStyle: { color: '#8B5CF6' },
        },
      ]
    },
    buildInfo: (d) => {
      if (!d) return []
      return [
        { label: '量', color: d.close >= d.open ? '#C74040' : '#2D9B65', value: fmtVol(d.volume) },
      ]
    },
  },
  {
    key: 'macd',
    label: 'MACD',
    height: 72,
    buildSeries: (data) => [
      {
        name: 'DIF',
        type: 'line',
        data: data.map(d => d.macd_dif != null ? Number(d.macd_dif) : '-'),
        smooth: true, symbol: 'none', animation: false,
        lineStyle: { width: 1, color: '#FACC15' },
        itemStyle: { color: '#FACC15' },
      },
      {
        name: 'DEA',
        type: 'line',
        data: data.map(d => d.macd_dea != null ? Number(d.macd_dea) : '-'),
        smooth: true, symbol: 'none', animation: false,
        lineStyle: { width: 1, color: '#8B5CF6' },
        itemStyle: { color: '#8B5CF6' },
      },
      {
        name: 'MACD',
        type: 'bar',
        data: data.map(d => {
          const v = d.macd_hist
          if (v == null) return '-'
          return {
            value: Number(v),
            itemStyle: { color: Number(v) >= 0 ? 'rgba(240,68,56,0.6)' : 'rgba(18,183,106,0.6)' },
          }
        }),
        barWidth: '40%',
        animation: false,
      },
    ],
    buildInfo: (d) => {
      if (!d) return []
      return [
        { label: 'DIF', color: '#FACC15', value: d.macd_dif != null ? d.macd_dif.toFixed(3) : '—' },
        { label: 'DEA', color: '#8B5CF6', value: d.macd_dea != null ? d.macd_dea.toFixed(3) : '—' },
        { label: 'MACD', color: d.macd_hist != null && d.macd_hist >= 0 ? '#C74040' : '#2D9B65', value: d.macd_hist != null ? d.macd_hist.toFixed(3) : '—' },
      ]
    },
  },
  {
    key: 'rsi',
    label: 'RSI',
    height: 72,
    yAxisConfig: { min: 0, max: 100 },
    buildSeries: (data) => [
      {
        name: 'RSI6',
        type: 'line',
        data: data.map(d => d.rsi_6 != null ? Number(d.rsi_6) : '-'),
        smooth: true, symbol: 'none', animation: false,
        lineStyle: { width: 1, color: '#FACC15' },
        itemStyle: { color: '#FACC15' },
      },
      {
        name: 'RSI14',
        type: 'line',
        data: data.map(d => d.rsi_14 != null ? Number(d.rsi_14) : '-'),
        smooth: true, symbol: 'none', animation: false,
        lineStyle: { width: 1, color: '#3B82F6' },
        itemStyle: { color: '#3B82F6' },
      },
      {
        name: 'RSI24',
        type: 'line',
        data: data.map(d => d.rsi_24 != null ? Number(d.rsi_24) : '-'),
        smooth: true, symbol: 'none', animation: false,
        lineStyle: { width: 1, color: '#8B5CF6' },
        itemStyle: { color: '#8B5CF6' },
      },
    ],
    buildInfo: (d) => {
      if (!d) return []
      return [
        { label: 'RSI6', color: '#FACC15', value: d.rsi_6 != null ? d.rsi_6.toFixed(1) : '—' },
        { label: 'RSI14', color: '#3B82F6', value: d.rsi_14 != null ? d.rsi_14.toFixed(1) : '—' },
        { label: 'RSI24', color: '#8B5CF6', value: d.rsi_24 != null ? d.rsi_24.toFixed(1) : '—' },
      ]
    },
  },
  {
    key: 'kdj',
    label: 'KDJ',
    height: 72,
    buildSeries: (data) => [
      {
        name: 'K',
        type: 'line',
        data: data.map(d => d.kdj_k != null ? Number(d.kdj_k) : '-'),
        smooth: true, symbol: 'none', animation: false,
        lineStyle: { width: 1, color: '#FACC15' },
        itemStyle: { color: '#FACC15' },
      },
      {
        name: 'D',
        type: 'line',
        data: data.map(d => d.kdj_d != null ? Number(d.kdj_d) : '-'),
        smooth: true, symbol: 'none', animation: false,
        lineStyle: { width: 1, color: '#3B82F6' },
        itemStyle: { color: '#3B82F6' },
      },
      {
        name: 'J',
        type: 'line',
        data: data.map(d => d.kdj_j != null ? Number(d.kdj_j) : '-'),
        smooth: true, symbol: 'none', animation: false,
        lineStyle: { width: 1, color: '#8B5CF6' },
        itemStyle: { color: '#8B5CF6' },
      },
    ],
    buildInfo: (d) => {
      if (!d) return []
      return [
        { label: 'K', color: '#FACC15', value: d.kdj_k != null ? d.kdj_k.toFixed(1) : '—' },
        { label: 'D', color: '#3B82F6', value: d.kdj_d != null ? d.kdj_d.toFixed(1) : '—' },
        { label: 'J', color: '#8B5CF6', value: d.kdj_j != null ? d.kdj_j.toFixed(1) : '—' },
      ]
    },
  },
]

/** 向后兼容的 INDICATORS 导出 (不含 vol) */
export const INDICATORS = SUB_CHARTS.filter(s => s.key !== 'vol')

/** 主图叠加指标 (画在 K 线上方, 不占副图空间) */
export const OVERLAY_INDICATORS: { key: string; label: string }[] = [
  { key: 'boll', label: 'BOLL' },
]

interface Props {
  data: OHLC[]
  markers?: ChartMarker[]
  ranges?: ChartRange[]
  structureLines?: ChartStructureLine[]
  priceLines?: ChartPriceLine[]
  height?: number
  showMA?: boolean
  /** 是否将 MA5/MA20/MA60 放在顶部信息栏第一行居中显示。 */
  centerMovingAverages?: boolean
  showInfoBar?: boolean
  /** 是否在主图信息栏显示当前 K 线日期；默认显示。 */
  showInfoDate?: boolean
  /** 是否显示从所选 K 线开盘价到最新 K 线收盘价的涨跌；默认隐藏。 */
  showSinceLatest?: boolean
  /** 是否显示日期轴与十字线日期标签；默认保持显示。 */
  showDateLabels?: boolean
  showMarkers?: boolean
  onToggleMarkers?: () => void
  stockInfo?: StockInfo
  symbol?: string
  linkedPrice?: number | null
  /** 当前分时浮窗对应的日 K；显示竖向定位线。 */
  selectedDate?: string | null
  onDateClick?: (date: string, anchor?: ChartClickAnchor) => void
  onInspectionClick?: (inspectionId: string) => void
  onPriceDoubleClick?: (price: number, currentPrice: number) => void
  /** 默认可见蜡烛根数, 默认 60; 'all' = 初始适配显示全部返回数据 */
  visibleBars?: number | 'all'
  /** 已激活的子图 key 列表 (含 vol, 按点击顺序) */
  activeIndicators?: string[]
  /** 成交量柱相对前 N 个交易日均量的显示设置 */
  volumeCompare?: VolumeCompareConfig
}

export interface ChartClickAnchor {
  clientX: number
  clientY: number
}

function chartClickAnchor(params: any, container: HTMLDivElement): ChartClickAnchor | undefined {
  const nativeEvent = params?.event?.event
  const clientX = Number(nativeEvent?.clientX)
  const clientY = Number(nativeEvent?.clientY)
  if (Number.isFinite(clientX) && Number.isFinite(clientY)) {
    return { clientX, clientY }
  }
  const offsetX = Number(params?.event?.offsetX)
  const offsetY = Number(params?.event?.offsetY)
  if (!Number.isFinite(offsetX) || !Number.isFinite(offsetY)) return undefined
  const rect = container.getBoundingClientRect()
  return { clientX: rect.left + offsetX, clientY: rect.top + offsetY }
}

// 序列颜色 (双主题通用); 画布轴/网格/文字等主题相关色走 CT() 动态取
const THEME = {
  bull: '#C74040',
  bear: '#2D9B65',
  bullAlpha: 'rgba(240,68,56,0.7)',
  bearAlpha: 'rgba(18,183,106,0.7)',
  ma5: '#A1A1AA',
  ma10: '#3B82F6',
  ma20: '#F97316',
  ma60: '#8B5CF6',
  bg: 'transparent',
}

/** 当前主题的图表调色板 (buildOption/信息栏在渲染时调用; 主题切换由组件 effect 触发重建)。 */
const CT = () => chartTheme(getTheme())

/** 可见蜡烛超过此数量时，涨停/炸板标签切换为小圆点。 */
const COMPACT_THRESHOLD = 60

/** 子图上方信息栏高度 (px) */
const INFO_BAR_H = 16
/** 子图之间的间距 (px) */
const SUB_GAP_PX = 4

function buildSubInfoGraphics(
  data: OHLC[],
  infoIdx: number,
  activeIndicators: string[],
  subStartTop: number,
  volumeCompare: VolumeCompareConfig,
): any[] {
  const d = infoIdx >= 0 && infoIdx < data.length ? data[infoIdx] : null
  const graphics: any[] = []
  let curTop = subStartTop

  activeIndicators.forEach((key) => {
    const def = SUB_CHARTS.find(s => s.key === key)
    if (!def) return

    const items = def.buildInfo(d)
    if (def.key === 'vol' && d) {
      const calcVolMa = (n: number) => {
        if (infoIdx < n - 1) return null
        let sum = 0
        for (let j = infoIdx - n + 1; j <= infoIdx; j++) sum += data[j].volume ?? 0
        return sum / n
      }
      const vol5 = calcVolMa(5)
      const vol10 = calcVolMa(10)
      items.push({ label: 'VOL5', color: '#FACC15', value: fmtVol(vol5) })
      items.push({ label: 'VOL10', color: '#8B5CF6', value: fmtVol(vol10) })
      if (volumeCompare.enabled) {
        const ratio = volumeRatioAt(data, infoIdx, volumeCompare.days)
        items.push({
          label: `量比${volumeCompare.days}`,
          color: ratio != null && ratio >= 1 ? '#C74040' : '#2D9B65',
          value: fmtVolumeRatio(ratio),
        })
      }
    }

    // 每个元素加固定 id，确保 ECharts 增量更新时能正确匹配
    graphics.push({
      id: `sub-sep-${key}`,
      type: 'line',
      shape: { x1: 0, y1: curTop, x2: 2000, y2: curTop },
      style: { stroke: 'rgba(255,255,255,0.08)', lineWidth: 1 },
      silent: true, z: 0,
    })
    graphics.push({
      id: `sub-label-${key}`,
      type: 'text',
      style: {
        text: def.label,
        x: 4, y: curTop + 4,
        fill: '#8E8E96',
        fontSize: 10, fontFamily: 'JetBrains Mono, monospace',
        fontWeight: 'bold',
      },
      silent: true, z: 10,
    })

    const richTextParts: string[] = []
    const rich: Record<string, any> = {}
    items.forEach((item, idx) => {
      const styleKey = `s${idx}`
      richTextParts.push(`{${styleKey}|${item.label}:${item.value}}`)
      rich[styleKey] = {
        fill: item.color,
        fontSize: 10,
        fontFamily: 'JetBrains Mono, monospace',
      }
    })
    graphics.push({
      id: `sub-val-${key}`,
      type: 'text',
      right: 24,
      style: {
        text: richTextParts.join(`{gap|  }`),
        y: curTop + 3,
        rich: {
          gap: { fill: 'transparent', fontSize: 10 },
          ...rich,
        },
        fontSize: 10,
        fontFamily: 'JetBrains Mono, monospace',
        textAlign: 'right',
        textVerticalAlign: 'top',
      },
      silent: true, z: 10,
    })

    curTop += INFO_BAR_H + def.height + SUB_GAP_PX
  })

  return graphics
}

function buildOption(
  data: OHLC[],
  dates: string[],
  dateIndexMap: Map<string, number>,
  markers: ChartMarker[] | undefined,
  ranges: ChartRange[] | undefined,
  structureLines: ChartStructureLine[] | undefined,
  priceLines: ChartPriceLine[] | undefined,
  showMA: boolean,
  compact: boolean,
  activeIndicators: string[],
  containerHeight: number,
  infoIdx: number,
  linkedPrice: number | null | undefined,
  volumeCompare: VolumeCompareConfig,
  showDateLabels: boolean,
  selectedDate: string | null | undefined,
): EChartsOption {
  const candleData = data.map(d => {
    const limitColor = getKlineLimitColor(d)
    return {
      value: [d.open, d.close, d.low, d.high],
      ...(limitColor ? {
        itemStyle: {
          color: limitColor,
          color0: limitColor,
          borderColor: limitColor,
          borderColor0: limitColor,
        },
      } : {}),
    }
  })

  const hasMA = showMA && data.some(d => d.ma5 != null || d.ma10 != null || d.ma20 != null || d.ma60 != null)

  const markPointData: any[] = []
  if (markers && markers.length > 0) {
    for (const [markerIndex, m] of markers.entries()) {
      const idx = dateIndexMap.get(m.date)
      if (idx == null) continue
      const d = data[idx]
      const isBuy = m.kind === 'buy'
      const isSell = m.kind === 'sell'
      const isAbove = m.above ?? isSell
      const labelDistance = markerLabelDistance(markers, markerIndex, m)

      if (isAbove && !m.circle) {
        const dotColor = m.color ?? (isBuy ? '#FACC15' : CT().text)
        if (compact) {
          markPointData.push({
            name: m.date, value: m.description ?? m.date, inspectionId: m.inspectionId, coord: [idx, m.price ?? d.high],
            symbol: 'circle', symbolSize: 4, symbolOffset: [0, m.lockToPrice ? 0 : (m.offsetY ?? (m.kind === 'neutral' ? 0 : -10))],
            itemStyle: { color: dotColor, cursor: 'pointer' },
            tooltip: markerTooltip(m),
            label: { show: false }, z: 100, zlevel: 10,
          })
        } else {
          markPointData.push({
            name: m.date, value: m.description ?? m.date, inspectionId: m.inspectionId, coord: [idx, m.price ?? d.high],
            symbol: 'circle', symbolSize: m.lockToPrice ? 8 : 12, symbolOffset: [0, m.lockToPrice ? 0 : (m.offsetY ?? (m.kind === 'neutral' ? 0 : -2))],
            itemStyle: { color: m.lockToPrice ? dotColor : 'transparent' },
            tooltip: markerTooltip(m),
            label: {
              show: true, formatter: m.label ?? '', position: 'top', distance: labelDistance,
              color: dotColor, fontSize: 10, fontWeight: 'normal',
              fontFamily: 'JetBrains Mono, monospace',
            },
            z: 100, zlevel: 10,
          })
        }
      } else if (m.circle) {
        markPointData.push({
          name: m.date, value: m.description ?? m.label ?? '', inspectionId: m.inspectionId,
          coord: [idx, m.price ?? (isBuy ? d.low : d.high)],
          symbol: 'circle',
          symbolSize: 18,
          symbolOffset: [0, m.offsetY ?? (isBuy ? 18 : -18)],
          itemStyle: { color: m.color ?? (isBuy ? THEME.bull : THEME.bear), borderColor: CT().tooltipBg, borderWidth: 1 },
          tooltip: markerTooltip(m),
          label: {
            show: !!m.label,
            formatter: m.label ?? '',
            position: 'inside',
            color: '#FFFFFF',
            fontSize: 11,
            fontWeight: 'bold',
            fontFamily: 'JetBrains Mono, monospace',
          },
          z: 120,
          zlevel: 20,
        })
      } else {
        markPointData.push({
          name: m.date, value: m.description ?? m.label ?? '', inspectionId: m.inspectionId,
          coord: [idx, m.price ?? (isAbove ? d.high : d.low)],
          symbol: m.circle ? 'circle' : m.kind === 'neutral' ? 'circle' : 'arrow', symbolSize: m.circle ? 18 : m.kind === 'neutral' ? 8 : 12,
          symbolRotate: m.circle || m.kind === 'neutral' ? undefined : isBuy ? 0 : 180,
          symbolOffset: [
            0,
            m.lockToPrice ? 0 : (m.offsetY ?? (m.circle ? (isBuy ? 18 : -18) : (m.kind === 'neutral' ? 0 : (isAbove ? -60 : 60)))),
          ],
          itemStyle: { color: m.circle ? (m.color ?? (isBuy ? THEME.bull : THEME.bear)) : (isBuy ? THEME.bull : isSell ? THEME.bear : CT().text) },
          tooltip: markerTooltip(m),
          label: {
            show: !!m.label, formatter: m.label ?? '',
            position: m.circle ? 'inside' : (isAbove ? 'top' : 'bottom'), distance: labelDistance,
            color: m.circle ? '#FFFFFF' : CT().text, fontSize: m.circle ? 11 : 10,
            fontWeight: m.circle ? 'bold' : 'normal',
            fontFamily: 'JetBrains Mono, monospace',
          },
        })
      }
    }
  }

  // ====== 布局计算 ======
  const left = 60
  const right = 20
  const topPad = 8
  const candleBottomPad = 22

  let subTotalH = 0
  const activeSubDefs: SubChartDef[] = []
  activeIndicators.forEach(key => {
    const def = SUB_CHARTS.find(s => s.key === key)
    if (!def) return
    activeSubDefs.push(def)
    subTotalH += INFO_BAR_H + def.height
  })
  if (activeSubDefs.length > 0) subTotalH += activeSubDefs.length * SUB_GAP_PX

  const candleAvail = Math.max(containerHeight - topPad - candleBottomPad - subTotalH, 100)
  const markerOffsetPx = Math.max(
    28,
    ...(markers ?? []).map((marker, markerIndex) => (
      Math.abs(marker.offsetY ?? 0)
      + (marker.circle ? 12 : markerLabelDistance(markers, markerIndex, marker) + 14)
    )),
  )
  const safeMarkerOffsetPx = Math.min(markerOffsetPx, candleAvail * 0.12)
  // Markers use pixel offsets, so convert that space to a data-range ratio
  // against the available plot height. Dividing by the remaining height
  // compounds the ratio when markers are dense and leaves large empty bands
  // above and below the candles.
  const markerPaddingRatio = safeMarkerOffsetPx / Math.max(1, candleAvail)

  const grids: any[] = []
  const xAxes: any[] = []
  const yAxes: any[] = []
  const series: any[] = []
  const xAxisIndices: number[] = []

  const priceLineValues = (priceLines ?? [])
    .map(line => line.value)
    .filter(value => Number.isFinite(value) && value > 0)
  const axisBounds = ({ min, max }: { min: number; max: number }) => {
    const nextMin = Math.min(min, ...priceLineValues)
    const nextMax = Math.max(max, ...priceLineValues)
    const span = Math.max(nextMax - nextMin, Math.abs(nextMax) * 0.01, 0.01)
    const padding = span * Math.max(0.03, markerPaddingRatio)
    return { min: nextMin - padding, max: nextMax + padding }
  }

  // ===== grid 0: K线主图 =====
  grids.push({ left, right, top: topPad, height: candleAvail })
  xAxes.push({
    type: 'category', data: dates, boundaryGap: true,
    axisLine: { lineStyle: { color: CT().border } },
    axisLabel: { show: showDateLabels, color: CT().text, fontSize: 10, fontFamily: 'JetBrains Mono, monospace' },
    axisTick: { show: false },
    splitLine: { show: false },
  })
  yAxes.push({
    scale: true,
    min: (params: { min: number; max: number }) => axisBounds(params).min,
    max: (params: { min: number; max: number }) => axisBounds(params).max,
    // 价格边界根据最外侧标记轨道动态留白，缩放后顶/底标记仍不会被图表边界压叠。
    boundaryGap: [0, 0],
    splitArea: { show: false },
    axisLine: { show: false }, axisTick: { show: false },
    splitLine: { lineStyle: { color: CT().grid } },
    axisLabel: {
      color: CT().text,
      fontSize: 10,
      fontFamily: 'JetBrains Mono, monospace',
      // axisBounds deliberately keeps fractional padding for structure markers.
      // Do not expose that calculation precision as a long, clipped price label.
      formatter: formatPriceAxisLabel,
    },
  })
  xAxisIndices.push(0)

  const markAreaData = (ranges ?? [])
    .filter(r => dateIndexMap.has(r.start) && dateIndexMap.has(r.end))
    .map(r => {
      const startIndex = dateIndexMap.get(r.start) as number
      const endIndex = dateIndexMap.get(r.end) as number
      const hasPriceBounds = Number.isFinite(r.low) && Number.isFinite(r.high)
      return [
        {
          name: r.label ?? '',
          inspectionId: r.inspectionId,
          xAxis: startIndex,
          ...(hasPriceBounds ? { yAxis: r.high } : {}),
          itemStyle: {
            color: r.color ?? 'rgba(139,92,246,0.18)',
            borderColor: 'rgba(167,139,250,0.75)',
            borderWidth: hasPriceBounds ? 1 : 0,
          },
          label: {
            show: !!r.label,
            position: 'insideTop',
            distance: 8,
            color: CT().tooltipText,
            backgroundColor: CT().tooltipBg,
            borderColor: 'rgba(167,139,250,0.75)',
            borderWidth: 1,
            borderRadius: 4,
            padding: [2, 6],
            fontSize: 10,
            fontFamily: 'JetBrains Mono, monospace',
          },
        },
        { xAxis: endIndex, inspectionId: r.inspectionId, ...(hasPriceBounds ? { yAxis: r.low } : {}) },
      ]
    })

  const markLineData: any[] = (priceLines ?? [])
    .filter(line => Number.isFinite(line.value))
    .map(line => {
      const lineStyle = {
        color: line.color ?? CT().text,
        type: 'dashed' as const,
        width: 1,
        opacity: 0.92,
      }
      const label = {
        show: !!line.label || !!line.axisLabel,
        formatter: line.axisLabel ? line.value.toFixed(2) : line.label ?? '',
        position: line.axisLabel ? 'start' as const : 'insideEndTop' as const,
        distance: line.axisLabel ? 8 : 5,
        color: line.color ?? CT().text,
        backgroundColor: CT().tooltipBg,
        borderRadius: 4,
        padding: line.axisLabel ? [2, 0] : [2, 6],
        fontSize: 10,
        fontFamily: 'JetBrains Mono, monospace',
      }
      if (line.start && line.end && dateIndexMap.has(line.start) && dateIndexMap.has(line.end)) {
        return [
          { xAxis: line.start, yAxis: line.value },
          { xAxis: line.end, yAxis: line.value, lineStyle, label, symbol: 'none' },
        ]
      }
      return { yAxis: line.value, lineStyle, label, symbol: 'none' }
    })

  for (const line of structureLines ?? []) {
    const startIndex = dateIndexMap.get(line.start)
    const endIndex = dateIndexMap.get(line.end)
    if (startIndex == null || endIndex == null) continue
    if (!Number.isFinite(line.startPrice) || !Number.isFinite(line.endPrice)) continue
    markLineData.push([
      { xAxis: startIndex, yAxis: line.startPrice, inspectionId: line.inspectionId },
      {
        xAxis: endIndex,
        yAxis: line.endPrice,
        inspectionId: line.inspectionId,
        symbol: 'none',
        lineStyle: {
          color: line.color ?? '#38BDF8',
          width: line.width ?? 1.5,
          type: line.type ?? 'solid',
        },
        label: { show: false },
      },
    ])
  }

  if (linkedPrice != null) {
    markLineData.push({
      yAxis: linkedPrice,
      lineStyle: { color: '#3B82F6', type: 'dashed', width: 1, opacity: 0.7 },
      label: {
        show: true,
        formatter: linkedPrice.toFixed(2),
        position: 'insideEndTop',
        color: '#3B82F6',
        fontSize: 10,
        fontFamily: 'JetBrains Mono, monospace',
        backgroundColor: CT().tooltipBg,
        borderColor: '#3B82F6',
        borderWidth: 1,
        padding: [1, 4],
        borderRadius: 2,
      },
      symbol: 'none',
    })
  }

  const selectedDateIndex = selectedDate ? dateIndexMap.get(selectedDate) : undefined
  if (selectedDateIndex != null) {
    markLineData.push({
      xAxis: selectedDateIndex,
      lineStyle: { color: '#60A5FA', type: 'dashed', width: 1.5, opacity: 0.95 },
      label: { show: false },
      symbol: 'none',
    })
  }

  series.push({
    name: 'K', type: 'candlestick', data: candleData,
    animation: false,
    itemStyle: {
      color: THEME.bull, color0: THEME.bear,
      borderColor: THEME.bull, borderColor0: THEME.bear,
      cursor: 'pointer',
    },
    markPoint: markPointData.length > 0 ? { data: markPointData, animation: false } : undefined,
    markArea: markAreaData.length > 0 ? { silent: !(ranges ?? []).some(range => !!range.inspectionId), data: markAreaData } : undefined,
    markLine: markLineData.length > 0 ? { silent: !(structureLines ?? []).some(line => !!line.inspectionId), symbol: 'none', data: markLineData, animation: false } : undefined,
  })

  if (hasMA) {
    const maLine = (key: keyof OHLC, color: string, name: string) => ({
      name, type: 'line',
      data: data.map(d => (d[key] != null ? Number(d[key]) : '-')),
      smooth: true, symbol: 'none', animation: false,
      silent: true,
      lineStyle: { width: 1, color }, itemStyle: { color },
    })
    series.push(maLine('ma5', THEME.ma5, 'MA5'))
    series.push(maLine('ma10', THEME.ma10, 'MA10'))
    series.push(maLine('ma20', THEME.ma20, 'MA20'))
    series.push(maLine('ma60', THEME.ma60, 'MA60'))
  }

  // BOLL 布林带 — 需在 activeIndicators 中激活
  const showBOLL = activeIndicators.includes('boll') && data.some(d => d.boll_upper != null || d.boll_lower != null)
  if (showBOLL) {
    const bollLine = (key: keyof OHLC, color: string, name: string) => ({
      name, type: 'line',
      data: data.map(d => (d[key] != null ? Number(d[key]) : '-')),
      smooth: true, symbol: 'none', animation: false,
      silent: true,
      lineStyle: { width: 1, color, type: 'dashed' as const }, itemStyle: { color },
    })
    series.push(bollLine('boll_upper', '#E879F9', 'BOLL上'))
    series.push(bollLine('boll_lower', '#E879F9', 'BOLL下'))
  }

  // ===== 子图区域 =====
  let curTop = topPad + candleAvail + candleBottomPad

  activeSubDefs.forEach((def, i) => {
    const gridIdx = i + 1
    const xAxisIdx = i + 1
    const yAxisIdx = i + 1

    const chartTop = curTop + INFO_BAR_H
    grids.push({
      left, right,
      top: chartTop,
      height: def.height,
      show: true,
      borderColor: CT().grid,
      borderWidth: 1,
    })

    xAxes.push({
      type: 'category', gridIndex: gridIdx, data: dates, boundaryGap: true,
      axisLine: { show: false }, axisLabel: { show: false },
      axisTick: { show: false }, splitLine: { show: false },
      axisPointer: { label: { show: false } },
    })

    const isFixedRange = !!def.yAxisConfig
    yAxes.push({
      scale: !isFixedRange,
      ...(isFixedRange ? def.yAxisConfig : {}),
      gridIndex: gridIdx,
      splitNumber: 2,
      axisLine: { show: false }, axisTick: { show: false },
      splitLine: { lineStyle: { color: CT().grid } },
      axisLabel: {
        show: true, color: CT().text, fontSize: 9,
        fontFamily: 'JetBrains Mono, monospace',
      },
    })

    xAxisIndices.push(xAxisIdx)

    const subSeries = def.buildSeries(data, { compact, volumeCompare })
    subSeries.forEach((s: any) => {
      series.push({ ...s, xAxisIndex: xAxisIdx, yAxisIndex: yAxisIdx })
    })

    curTop += INFO_BAR_H + def.height + SUB_GAP_PX
  })

  // 子图信息栏 graphic
  const subStartTop = topPad + candleAvail + candleBottomPad
  const infoGraphics = buildSubInfoGraphics(data, infoIdx, activeIndicators, subStartTop, volumeCompare)

  return {
    animation: false,
    backgroundColor: THEME.bg,
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross', crossStyle: { color: CT().crosshair } },
      backgroundColor: 'transparent',
      borderWidth: 0,
      textStyle: { fontSize: 0 },
      formatter: () => '',
    },
    axisPointer: {
      link: [{ xAxisIndex: 'all' }],
      label: {
        show: showDateLabels,
        backgroundColor: CT().crosshairLabelBg,
        fontFamily: 'JetBrains Mono, monospace',
        fontSize: 10,
      },
    },
    graphic: infoGraphics.length > 0 ? infoGraphics : undefined,
    grid: grids,
    xAxis: xAxes,
    yAxis: yAxes,
    dataZoom: [
      {
        type: 'inside',
        xAxisIndex: xAxisIndices,
        start: 0,
        end: 100,
        moveOnMouseMove: true,
        zoomOnMouseWheel: true,
      },
    ],
    series,
  }
}


export function EChartsCandlestick({
  data,
  markers,
  ranges,
  structureLines,
  priceLines,
  height = 480,
  showMA = true,
  centerMovingAverages = false,
  showInfoBar = true,
  showInfoDate = true,
  showSinceLatest = false,
  showDateLabels = true,
  showMarkers: showMarkersProp = true,
  onToggleMarkers: _onToggleMarkers,
  stockInfo,
  symbol: _symbol,
  linkedPrice,
  selectedDate,
  onDateClick,
  onInspectionClick,
  onPriceDoubleClick,
  visibleBars = 60,
  activeIndicators = [],
  volumeCompare = { enabled: true, days: 1 },
}: Props) {
  const hoverSurfaceRef = useRef<HTMLDivElement>(null)
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<ECharts | null>(null)
  const dataRef = useRef(data)
  dataRef.current = data
  const onDateClickRef = useRef(onDateClick)
  onDateClickRef.current = onDateClick
  const onInspectionClickRef = useRef(onInspectionClick)
  onInspectionClickRef.current = onInspectionClick
  const onPriceDoubleClickRef = useRef(onPriceDoubleClick)
  onPriceDoubleClickRef.current = onPriceDoubleClick
  // 主题: buildOption/信息栏内部通过 CT() 动态取调色板, 这里只负责切换时触发重建
  const theme = useTheme()

  // --- 全部用 ref，避免高频交互触发 React 重渲染 ---
  const infoIdxRef = useRef<number>(data.length - 1)
  const compactRef = useRef(false)
  // 图表的缩放监听器长期保留, 标记更新必须使用本次渲染的日期索引和数据。
  const updateCompactPresentationRef = useRef<() => void>(() => {})
  updateCompactPresentationRef.current = updateCompactPresentation
  const userZoomRef = useRef<{ start: number; end: number } | null>(null)
  // 竖虚线(crosshair)是否可见: 控制信息栏「至今」字段的显隐。鼠标移出图表区即 false。
  const hoverActiveRef = useRef(false)

  // 需要在闭包中访问最新值的变量 — 先声明占位，后面赋值
  const activeIndicatorsRef = useRef(activeIndicators)
  activeIndicatorsRef.current = activeIndicators
  const volumeCompareRef = useRef(volumeCompare)
  volumeCompareRef.current = volumeCompare
  const chartHeightRef = useRef(300)
  const subTotalHRef = useRef(0)
  const getInfoBarHTMLRef = useRef<() => string>(() => '')

  // 强制刷新信息栏 DOM 的回调
  const infoBarRef = useRef<HTMLDivElement>(null)
  const triggerInfoBarUpdate = useRef(() => {
    const idx = infoIdxRef.current
    const curData = dataRef.current
    const d = idx >= 0 && idx < curData.length ? curData[idx] : null
    if (!d) return
    const chart = chartRef.current
    if (!chart) return
    const subStartTop = chartHeightRef.current - subTotalHRef.current
    const infoGraphics = buildSubInfoGraphics(
      curData,
      idx,
      activeIndicatorsRef.current,
      subStartTop,
      volumeCompareRef.current,
    )
    if (infoGraphics.length > 0) {
      chart.setOption({ graphic: infoGraphics }, { lazyUpdate: true })
    }
  }).current

  // 计算子图总高度
  const activeSubDefs = activeIndicators
    .map(key => SUB_CHARTS.find(s => s.key === key))
    .filter((d): d is SubChartDef => !!d)

  let subTotalH = 0
  activeSubDefs.forEach(def => { subTotalH += INFO_BAR_H + def.height })
  if (activeSubDefs.length > 0) subTotalH += activeSubDefs.length * SUB_GAP_PX

  const mainInfoBarH = showInfoBar ? (centerMovingAverages ? 20 : 40) : 0
  const minCandleH = 120

  const chartHeight = Math.max(height - mainInfoBarH, 8 + minCandleH + 14 + subTotalH)
  chartHeightRef.current = chartHeight
  subTotalHRef.current = subTotalH

  // 预计算 date→index Map (O(1) 查找)
  const dates = useMemo(() => data.map(d => d.date), [data])
  const dateIndexMap = useMemo(() => {
    const m = new Map<string, number>()
    dates.forEach((d, i) => m.set(d, i))
    return m
  }, [dates])

  // dataZoom 初始范围: 'all' = 显示整段数据, 否则取末尾 visibleBars 根
  const initialZoom = useMemo(() => {
    const start = visibleBars === 'all'
      ? 0
      : Math.max(0, 100 - (visibleBars / Math.max(data.length, 1)) * 100)
    return { start, end: 100 }
  }, [visibleBars, data.length])

  // ===== 信息栏 HTML 内容 (基于 infoIdxRef.current) =====
  const getInfoBarHTML = useCallback(() => {
    let idx = infoIdxRef.current
    let d = idx >= 0 && idx < data.length ? data[idx] : null
    // fallback: 如果当前 idx 无数据，取最后一根 K 线
    if (!d && data.length > 0) {
      idx = data.length - 1
      d = data[idx]
    }
    if (!d) return ''
    const prev = idx > 0 ? data[idx - 1] : null
    const chg = prev ? d.close - prev.close : 0
    const isUp = chg >= 0
    const clr = getKlineLimitColor(d) ?? (isUp ? THEME.bull : THEME.bear)
    const floatShares = stockInfo?.float_shares
    const turnoverRate = floatShares && d.volume ? (d.volume * 100 / floatShares * 100) : null

    let html = `<div style="display:flex;align-items:center;gap:6px;padding:0 8px;font:11px 'JetBrains Mono',monospace;select:none;min-height:20px;flex-wrap:wrap">`
    html += `<span style="color:${CT().text}">${d.date}</span>`
    html += `<span style="color:${CT().text}">开</span>`
    html += `<span style="color:${d.open >= d.close ? THEME.bear : THEME.bull}">${d.open.toFixed(2)}</span>`
    html += `<span style="color:${CT().text}">高</span>`
    html += `<span style="color:${THEME.bull}">${d.high.toFixed(2)}</span>`
    html += `<span style="color:${CT().text}">低</span>`
    html += `<span style="color:${THEME.bear}">${d.low.toFixed(2)}</span>`
    html += `<span style="color:${CT().text}">收</span>`
    html += `<span style="color:${clr};font-weight:600">${d.close.toFixed(2)}</span>`
    // 涨跌幅 (收盘后, 换手前; 和收间隔一些距离)
    if (prev) {
      const chgPct = (chg / prev.close * 100)
      html += `<span style="color:${clr};margin-left:8px">${isUp ? '+' : ''}${chgPct.toFixed(2)}%</span>`
    }
    if (showSinceLatest && d.open > 0) {
      const latestClose = data[data.length - 1]?.close ?? d.close
      const sinceAmount = latestClose - d.open
      const sincePct = sinceAmount / d.open * 100
      const sinceColor = sinceAmount >= 0 ? THEME.bull : THEME.bear
      const sinceSign = sinceAmount > 0 ? '+' : ''
      html += `<span style="color:${CT().text};margin-left:8px">至今涨跌</span>`
      html += `<span style="color:${sinceColor};font-weight:600">${sinceSign}${sinceAmount.toFixed(2)} / ${sinceSign}${sincePct.toFixed(2)}%</span>`
    }
    if (turnoverRate != null) {
      html += `<span style="color:${CT().text}">换手</span>`
      html += `<span style="color:${CT().text}">${turnoverRate.toFixed(2)}%</span>`
    }
    // 至今: 仅当竖虚线(crosshair)在图上且鼠标悬停某根 K 线时显示。
    // 最新价取最后一根K线收盘 (后端 _maybe_inject_live_candle 盘中注入实时价, 收盘后即最近收盘)。
    // 基准取该K线昨收(前一日收盘), 与同花顺及全市场涨幅口径一致; 数据第一根K线无昨收则跳过。
    if (hoverActiveRef.current && prev && Number.isFinite(prev.close) && prev.close > 0) {
      const latestPrice = data[data.length - 1].close
      if (Number.isFinite(latestPrice)) {
        const sinceRatio = (latestPrice - prev.close) / prev.close
        const sinceClr = sinceRatio >= 0 ? THEME.bull : THEME.bear
        html += `<span style="color:${CT().text}">至今</span>`
        html += `<span style="color:${sinceClr}">${fmtPct(sinceRatio)}</span>`
        // 周期数: 从该K线(含)到最新一根K线共多少根; 悬停最后一根时为 1
        html += `<span style="color:${CT().text}">周期 ${data.length - idx}</span>`
      }
    }
    html += `</div>`
    if (centerMovingAverages && showMA && (d.ma5 != null || d.ma20 != null || d.ma60 != null)) {
      html += `<div style="position:absolute;left:50%;transform:translateX(-50%);display:flex;align-items:center;gap:10px;white-space:nowrap;pointer-events:none">`
      if (d.ma5 != null) html += `<span style="color:${THEME.ma5}">MA5:${Number(d.ma5).toFixed(2)}</span>`
      if (d.ma20 != null) html += `<span style="color:${THEME.ma20}">MA20:${Number(d.ma20).toFixed(2)}</span>`
      if (d.ma60 != null) html += `<span style="color:${THEME.ma60}">MA60:${Number(d.ma60).toFixed(2)}</span>`
      html += `</div>`
    }
    html += `</div>`

    // 第二行: MA + BOLL
    if (showMA) {
      html += `<div style="display:flex;align-items:center;gap:10px;padding:0 8px;font:11px 'JetBrains Mono',monospace;select:none;min-height:20px;flex-wrap:wrap">`
      if (d.ma5 != null) html += `<span style="color:${THEME.ma5}">MA5:${Number(d.ma5).toFixed(2)}</span>`
      if (d.ma10 != null) html += `<span style="color:${THEME.ma10}">MA10:${Number(d.ma10).toFixed(2)}</span>`
      if (d.ma20 != null) html += `<span style="color:${THEME.ma20}">MA20:${Number(d.ma20).toFixed(2)}</span>`
      if (d.ma60 != null) html += `<span style="color:${THEME.ma60}">MA60:${Number(d.ma60).toFixed(2)}</span>`
      if (d.boll_upper != null && activeIndicators.includes('boll')) {
        html += `<span style="color:#E879F9">BOLL:${Number(d.boll_upper).toFixed(2)}/${Number(d.ma20).toFixed(2)}/${Number(d.boll_lower).toFixed(2)}</span>`
      }
      html += `</div>`
    }
    return html
  }, [data, stockInfo, showMA, showInfoDate, showSinceLatest, activeIndicators, centerMovingAverages])
  getInfoBarHTMLRef.current = getInfoBarHTML

  // data/symbol 变化时重置 infoIdx:
  // symbol(_symbol) 进依赖是必要的——预取切股到同长度邻股时 data.length 不变,
  // 但悬停上下文来自上一只股票, 必须清掉 hoverActiveRef 以免「至今/周期」残留显示。
  // (同一股的实时刷新 symbol 不变, 不触发, 悬停位置与「至今」保持实时)
  useEffect(() => {
    infoIdxRef.current = data.length - 1
    compactRef.current = false
    userZoomRef.current = null
    // 新数据无悬停上下文, 隐藏「至今」; 下次鼠标移动时由 updateAxisPointer 重新置位
    hoverActiveRef.current = false
  }, [_symbol, data.length])

  // ===== 初始化 chart (只在 chartHeight 变化时重建) =====
  useEffect(() => {
    const el = containerRef.current
    const hoverEl = hoverSurfaceRef.current
    if (!el || !hoverEl) return

    const chart = echarts.init(el, undefined, { renderer: 'canvas' })
    chartRef.current = chart

    const updateHoverVisibility = (active: boolean) => {
      if (active === hoverActiveRef.current) return
      hoverActiveRef.current = active
      const infoEl = infoBarRef.current
      if (!infoEl) return
      const html = getInfoBarHTMLRef.current()
      if (html) infoEl.innerHTML = html
    }

    // The outer chart surface stays under the pointer when the info bar wraps and
    // pushes the canvas down, so hover visibility cannot oscillate at that boundary.
    const handlePointerEnter = () => updateHoverVisibility(true)
    const handlePointerLeave = () => updateHoverVisibility(false)
    hoverEl.addEventListener('mouseenter', handlePointerEnter)
    hoverEl.addEventListener('mouseleave', handlePointerLeave)

    // 鼠标移动 → 只更新 ref + DOM，不触发 React re-render
    // 设计原则: 找不到有效数据时保持上次显示，永远不清空信息栏。
    chart.on('updateAxisPointer', (event: any) => {
      const axesInfo = event.axesInfo
      const d = dataRef.current
      // 竖虚线是否正落在某根有效 K 线上 (鼠标在图表数据区内)
      let foundIdx = -1
      if (axesInfo) {
        for (const info of Object.values(axesInfo)) {
          const val = (info as any)?.value
          if (val == null) continue
          const idx = typeof val === 'number' ? val : d.findIndex(x => x.date === val)
          if (idx >= 0 && idx < d.length) { foundIdx = idx; break }
        }
      }
      if (foundIdx < 0) return
      const idxChanged = infoIdxRef.current !== foundIdx
      if (idxChanged) infoIdxRef.current = foundIdx
      // 悬停 K 线变化 → 重绘一次信息栏; 显隐由外层图表区域 enter/leave 负责。
      if (idxChanged) {
        const infoEl = infoBarRef.current
        if (infoEl) {
          const html = getInfoBarHTMLRef.current()
          if (html) infoEl.innerHTML = html  // 只在有内容时更新
        }
      }
      // 更新子图 graphic (仅悬停 K 线变化时; 纯显隐切换不影响副图)
      if (idxChanged) triggerInfoBarUpdate()
    })

    chart.on('click', (params: any) => {
      const inspectionId = chartInspectionId(params)
      if (inspectionId) {
        onInspectionClickRef.current?.(inspectionId)
        return
      }
      if (params.componentType === 'markPoint' && params.name) {
        onDateClickRef.current?.(params.name, chartClickAnchor(params, el))
        return
      }
      if (params.seriesName !== 'K' || params.dataIndex == null) return
      const d = dataRef.current
      const idx = params.dataIndex
      if (idx >= 0 && idx < d.length) {
        onDateClickRef.current?.(d[idx].date, chartClickAnchor(params, el))
      }
    })

    const handlePriceDoubleClick = (event: { offsetX: number; offsetY: number }) => {
      const pixel: [number, number] = [event.offsetX, event.offsetY]
      if (!chart.containPixel({ gridIndex: 0 }, pixel)) return
      const coordinate = chart.convertFromPixel({ xAxisIndex: 0, yAxisIndex: 0 }, pixel)
      const price = Array.isArray(coordinate) ? Number(coordinate[1]) : NaN
      const currentPrice = dataRef.current[dataRef.current.length - 1]?.close
      if (Number.isFinite(price) && price > 0 && Number.isFinite(currentPrice) && currentPrice > 0) {
        onPriceDoubleClickRef.current?.(price, currentPrice)
      }
    }
    chart.getZr().on('dblclick', handlePriceDoubleClick)

    // dataZoom → 只更新 ref，不触发 React re-render
    // compact 变化时需要增量更新 markPoint
    chart.on('dataZoom', () => {
      const opt = chart.getOption() as any
      const zoom = opt?.dataZoom?.[0]
      if (!zoom) return
      userZoomRef.current = { start: zoom.start, end: zoom.end }

      const d = dataRef.current
      const total = d.length
      const visibleCount = Math.round(total * (zoom.end - zoom.start) / 100)
      const newCompact = visibleCount > COMPACT_THRESHOLD
      if (newCompact !== compactRef.current) {
        compactRef.current = newCompact
        updateCompactPresentationRef.current()
      }
    })

    const ro = new ResizeObserver(() => { chart.resize() })
    ro.observe(el)

    return () => {
      chart.off('updateAxisPointer')
      chart.off('click')
      chart.off('dataZoom')
      hoverEl.removeEventListener('mouseenter', handlePointerEnter)
      hoverEl.removeEventListener('mouseleave', handlePointerLeave)
      chart.getZr().off('dblclick', handlePriceDoubleClick)
      ro.disconnect()
      chart.dispose()
      chartRef.current = null
    }
  }, [chartHeight]) // eslint-disable-line react-hooks/exhaustive-deps

  // 缩放跨过紧凑阈值时，仅增量更新标签，不重建整张图。
  function updateCompactPresentation() {
    const chart = chartRef.current
    if (!chart) return
    const mkrs = showMarkersProp ? markers : undefined
    const compact = compactRef.current
    const seriesUpdates: any[] = []
    const markPointData: any[] = []
    for (const [markerIndex, m] of (mkrs ?? []).entries()) {
      const idx = dateIndexMap.get(m.date)
      if (idx == null) continue
      const d = data[idx]
      const isBuy = m.kind === 'buy'
      const isSell = m.kind === 'sell'
      const isAbove = m.above ?? isSell
      const labelDistance = markerLabelDistance(mkrs, markerIndex, m)
      if (isAbove && !m.circle) {
        const dotColor = m.color ?? (isBuy ? '#FACC15' : CT().text)
        if (compact) {
          markPointData.push({
            name: m.date, value: m.description ?? m.date, inspectionId: m.inspectionId, coord: [idx, m.price ?? d.high],
            symbol: 'circle', symbolSize: 4, symbolOffset: [0, m.lockToPrice ? 0 : (m.offsetY ?? (m.kind === 'neutral' ? 0 : -10))],
            itemStyle: { color: dotColor, cursor: 'pointer' },
            tooltip: markerTooltip(m),
            label: { show: false }, z: 100, zlevel: 10,
          })
        } else {
          markPointData.push({
            name: m.date, value: m.description ?? m.date, inspectionId: m.inspectionId, coord: [idx, m.price ?? d.high],
            symbol: 'circle', symbolSize: m.lockToPrice ? 8 : 12, symbolOffset: [0, m.lockToPrice ? 0 : (m.offsetY ?? (m.kind === 'neutral' ? 0 : -2))],
            itemStyle: { color: m.lockToPrice ? dotColor : 'transparent' },
            tooltip: markerTooltip(m),
            label: {
              show: true, formatter: m.label ?? '', position: 'top', distance: labelDistance,
              color: dotColor, fontSize: 10, fontWeight: 'normal',
              fontFamily: 'JetBrains Mono, monospace',
            },
            z: 100, zlevel: 10,
          })
        }
      } else {
        markPointData.push({
          name: m.date, value: m.description ?? m.label ?? '', inspectionId: m.inspectionId,
          coord: [idx, m.price ?? (isAbove ? d.high : d.low)],
          symbol: m.circle ? 'circle' : m.kind === 'neutral' ? 'circle' : 'arrow', symbolSize: m.circle ? 18 : m.kind === 'neutral' ? 8 : 12,
          symbolRotate: m.circle || m.kind === 'neutral' ? undefined : isBuy ? 0 : 180,
          symbolOffset: [
            0,
            m.lockToPrice ? 0 : (m.offsetY ?? (m.circle ? (isBuy ? 18 : -18) : (m.kind === 'neutral' ? 0 : (isAbove ? -60 : 60)))),
          ],
          itemStyle: { color: m.circle ? (m.color ?? (isBuy ? THEME.bull : THEME.bear)) : (isBuy ? THEME.bull : isSell ? THEME.bear : CT().text) },
          tooltip: markerTooltip(m),
          label: {
            show: !!m.label, formatter: m.label ?? '',
            position: m.circle ? 'inside' : (isAbove ? 'top' : 'bottom'), distance: labelDistance,
            color: m.circle ? '#FFFFFF' : CT().text, fontSize: m.circle ? 11 : 10,
            fontWeight: m.circle ? 'bold' : 'normal',
            fontFamily: 'JetBrains Mono, monospace',
          },
        })
      }
    }
    if (mkrs?.length) {
      seriesUpdates.push({
        name: 'K',
        markPoint: markPointData.length > 0 ? { data: markPointData, animation: false } : undefined,
      })
    }
    if (activeIndicatorsRef.current.includes('vol')) {
      seriesUpdates.push({
        name: '成交量',
        label: { show: volumeCompareRef.current.enabled && !compact },
      })
    }
    if (seriesUpdates.length > 0) chart.setOption({ series: seriesUpdates })
  }

  // ===== 核心: 仅在数据/配置变更时全量 setOption =====
  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return

    const option = buildOption(
      data, dates, dateIndexMap,
      showMarkersProp ? markers : undefined,
      ranges,
      structureLines,
      priceLines,
      showMA, compactRef.current,
      activeIndicators, chartHeight,
      infoIdxRef.current,
      linkedPrice,
      volumeCompare,
      showDateLabels,
      selectedDate,
    )

    chart.setOption(option, true)

    // 恢复用户缩放位置
    const zoom = userZoomRef.current
    if (zoom) {
      chart.dispatchAction({ type: 'dataZoom', start: zoom.start, end: zoom.end })
    } else {
      chart.dispatchAction({ type: 'dataZoom', start: initialZoom.start, end: initialZoom.end })
    }

    // 初始信息栏
    const infoEl = infoBarRef.current
    if (infoEl) {
      infoEl.innerHTML = getInfoBarHTML()
    }
  }, [data, markers, ranges, structureLines, priceLines, linkedPrice, selectedDate, showMA, showMarkersProp, activeIndicators, volumeCompare, showDateLabels, chartHeight, dates, dateIndexMap, initialZoom, getInfoBarHTML, theme])

  // 渲染信息栏容器 (内容由 JS 直接写入)
  const initialHTML = useMemo(() => {
    const idx = data.length - 1
    const d = idx >= 0 && idx < data.length ? data[idx] : null
    if (!d) return ''
    const floatShares = stockInfo?.float_shares
    const turnoverRate = floatShares && d.volume ? (d.volume * 100 / floatShares * 100) : null
    let html = `<div style="display:flex;align-items:center;gap:6px;padding:0 8px;font:11px 'JetBrains Mono',monospace;min-height:20px;flex-wrap:wrap">`
    html += `<span style="color:${CT().text}">${d.date}</span>`
    html += `<span style="color:${CT().text}">开</span>`
    html += `<span style="color:${d.open >= d.close ? THEME.bear : THEME.bull}">${d.open.toFixed(2)}</span>`
    html += `<span style="color:${CT().text}">高</span>`
    html += `<span style="color:${THEME.bull}">${d.high.toFixed(2)}</span>`
    html += `<span style="color:${CT().text}">低</span>`
    html += `<span style="color:${THEME.bear}">${d.low.toFixed(2)}</span>`
    html += `<span style="color:${CT().text}">收</span>`
    const prevClose0 = data[idx-1]?.close ?? d.close
    const clr0 = d.close >= prevClose0 ? THEME.bull : THEME.bear
    html += `<span style="color:${clr0};font-weight:600">${d.close.toFixed(2)}</span>`
    // 涨跌幅 (收盘后, 换手前; 和收间隔一些距离)
    if (idx > 0) {
      const chgPct0 = ((d.close - prevClose0) / prevClose0 * 100)
      html += `<span style="color:${clr0};margin-left:8px">${chgPct0 >= 0 ? '+' : ''}${chgPct0.toFixed(2)}%</span>`
    }
    if (showSinceLatest && d.open > 0) {
      const latestClose = data[data.length - 1]?.close ?? d.close
      const sinceAmount = latestClose - d.open
      const sincePct = sinceAmount / d.open * 100
      const sinceColor = sinceAmount >= 0 ? THEME.bull : THEME.bear
      const sinceSign = sinceAmount > 0 ? '+' : ''
      html += `<span style="color:${CT().text};margin-left:8px">至今涨跌</span>`
      html += `<span style="color:${sinceColor};font-weight:600">${sinceSign}${sinceAmount.toFixed(2)} / ${sinceSign}${sincePct.toFixed(2)}%</span>`
    }
    if (turnoverRate != null) {
      html += `<span style="color:${CT().text}">换手</span>`
      html += `<span style="color:${CT().text}">${turnoverRate.toFixed(2)}%</span>`
    }
    html += `</div>`
    if (showMA) {
      html += `<div style="display:flex;align-items:center;gap:10px;padding:0 8px;font:11px 'JetBrains Mono',monospace;min-height:20px;flex-wrap:wrap">`
      if (d.ma5 != null) html += `<span style="color:${THEME.ma5}">MA5:${Number(d.ma5).toFixed(2)}</span>`
      if (d.ma10 != null) html += `<span style="color:${THEME.ma10}">MA10:${Number(d.ma10).toFixed(2)}</span>`
      if (d.ma20 != null) html += `<span style="color:${THEME.ma20}">MA20:${Number(d.ma20).toFixed(2)}</span>`
      if (d.ma60 != null) html += `<span style="color:${THEME.ma60}">MA60:${Number(d.ma60).toFixed(2)}</span>`
      if (d.boll_upper != null && activeIndicators.includes('boll')) {
        html += `<span style="color:#E879F9">BOLL:${Number(d.boll_upper).toFixed(2)}/${Number(d.ma20).toFixed(2)}/${Number(d.boll_lower).toFixed(2)}</span>`
      }
      html += `</div>`
    }
    return html
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <div ref={hoverSurfaceRef} className="w-full">
      {/* 主图信息栏 — 内容由 JS 直接操作 innerHTML */}
      {showInfoBar && (
        <div ref={infoBarRef} style={{ backgroundColor: CT().infoBarBg }}
          dangerouslySetInnerHTML={{ __html: initialHTML }} />
      )}

      {/* ECharts canvas */}
      <div ref={containerRef} className="w-full" style={{ height: chartHeight }} />
    </div>
  )
}
