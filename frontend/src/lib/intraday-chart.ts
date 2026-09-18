import type { MinuteKlineRow } from '@/lib/api'

/** 从 datetime 串取 HH:MM。契约: 分钟K datetime 已在后端入口统一为北京墙钟, 前端不做时区换算。 */
export function formatMinuteTime(datetime: string): string {
  const match = datetime.match(/(\d{2}):(\d{2})/)
  if (!match) return datetime.slice(11, 16)
  // API 的 datetime 是北京时间墙上时间（不是 UTC 时间戳）。
  // 这里仅提取 HH:mm，不能再额外加 8 小时，否则 09:30 会被映射到
  // 17:30，导致所有分钟数据都匹配不到 FULL_DAY_TIMES，图表为空。
  return `${match[1]}:${match[2]}`
}

export function computeIntradayAverage(data: MinuteKlineRow[], priceScale = 1): number[] {
  const result: number[] = []
  let amount = 0
  let volume = 0
  for (const row of data) {
    if (typeof row.amount === 'number' && Number.isFinite(row.amount)) {
      amount += row.amount
    }
    volume += row.volume * 100
    result.push(volume > 0 ? amount / volume * priceScale : row.close)
  }
  return result
}

export interface MinutePriceAlignment {
  rows: MinuteKlineRow[]
  priceScale: number
}

/**
 * 将分钟 OHLC 对齐到所选日 K 的复权价格基准。
 *
 * 分钟 amount 保持真实成交额不变；均价线通过 priceScale 单独换算。
 */
export function alignMinutePricesToDailyClose(
  data: MinuteKlineRow[],
  dailyClose: number | null | undefined,
): MinutePriceAlignment {
  if (data.length === 0 || dailyClose == null || !Number.isFinite(dailyClose) || dailyClose <= 0) {
    return { rows: data, priceScale: 1 }
  }
  const minuteClose = [...data].reverse().find(row => Number.isFinite(row.close) && row.close > 0)?.close
  if (minuteClose == null) return { rows: data, priceScale: 1 }
  const priceScale = dailyClose / minuteClose
  if (!Number.isFinite(priceScale) || priceScale <= 0) return { rows: data, priceScale: 1 }
  if (Math.abs(priceScale - 1) < 1e-8) return { rows: data, priceScale: 1 }
  return {
    priceScale,
    rows: data.map(row => ({
      ...row,
      open: row.open == null ? null : row.open * priceScale,
      high: row.high * priceScale,
      low: row.low * priceScale,
      close: row.close * priceScale,
    })),
  }
}

/**
 * 返回分钟成交量柱的涨跌方向。
 *
 * 部分数据源（包括 MooTDX 历史分时）只提供每分钟成交价，没有真实的
 * open；适配层会让 open === close。此时用上一分钟价格作为比较基准，
 * 第一根则使用昨收，避免所有柱子都被误判为横盘。
 */
export function minuteBarDelta(row: MinuteKlineRow, previousClose?: number | null): number {
  if (row.open != null && row.close !== row.open) return row.close - row.open
  if (previousClose == null || !Number.isFinite(previousClose)) return 0
  return row.close - previousClose
}

function generateFullDayTimes(): string[] {
  const times: string[] = []
  for (let hour = 9; hour <= 11; hour++) {
    const startMinute = hour === 9 ? 30 : 0
    const endMinute = hour === 11 ? 30 : 59
    for (let minute = startMinute; minute <= endMinute; minute++) {
      times.push(`${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`)
    }
  }
  for (let hour = 13; hour <= 15; hour++) {
    const endMinute = hour === 15 ? 0 : 59
    for (let minute = 0; minute <= endMinute; minute++) {
      times.push(`${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`)
    }
  }
  return times
}

export const FULL_DAY_TIMES = generateFullDayTimes()
