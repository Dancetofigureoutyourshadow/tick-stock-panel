import type { MinuteKlineRow } from '@/lib/api'

export type DailySummary = { date: string; open: number | null; high: number; low: number; close: number }

/** 仅汇总实际收到的分钟，不将缺失开盘价或成交额伪装为真实值。 */
export function summarizeMinutes(data: MinuteKlineRow[]): MinuteKlineRow | null {
  if (!data.length) return null
  return {
    datetime: data[0].datetime,
    open: data[0].open,
    high: Math.max(...data.map(row => row.high)),
    low: Math.min(...data.map(row => row.low)),
    close: data[data.length - 1].close,
    volume: data.reduce((total, row) => total + row.volume, 0),
    amount: data.every(row => row.amount != null && Number.isFinite(row.amount))
      ? data.reduce((total, row) => total + row.amount!, 0) : null,
  }
}

/** 从 datetime 串取 HH:MM。契约: 分钟K datetime 已在后端入口统一为北京墙钟, 前端不做时区换算。 */
export function formatMinuteTime(datetime: string): string {
  const match = datetime.match(/(\d{2}):(\d{2})/)
  if (!match) return datetime.slice(11, 16)
  return `${match[1]}:${match[2]}`
}

export function computeIntradayAverage(data: MinuteKlineRow[], priceScale = 1): (number | null)[] {
  const result: (number | null)[] = []
  let amount = 0
  let volume = 0
  let hasAmount = true
  for (const row of data) {
    if (typeof row.amount === 'number' && Number.isFinite(row.amount)) {
      amount += row.amount
    } else {
      hasAmount = false
    }
    volume += row.volume * 100
    result.push(hasAmount && volume > 0 ? amount / volume * priceScale : null)
  }
  return result
}

export function minuteBarDelta(row: MinuteKlineRow, previousClose?: number | null): number {
  if (row.open != null && row.close !== row.open) return row.close - row.open
  if (previousClose == null || !Number.isFinite(previousClose)) return 0
  return row.close - previousClose
}

export function alignMinutePricesToDailyClose(data: MinuteKlineRow[], dailyClose: number | null | undefined): { rows: MinuteKlineRow[]; priceScale: number } {
  if (!data.length || dailyClose == null || !Number.isFinite(dailyClose) || dailyClose <= 0) return { rows: data, priceScale: 1 }
  const latest = [...data].reverse().find(row => Number.isFinite(row.close) && row.close > 0)?.close
  if (latest == null) return { rows: data, priceScale: 1 }
  const priceScale = dailyClose / latest
  if (!Number.isFinite(priceScale) || priceScale <= 0 || Math.abs(priceScale - 1) < 1e-8) return { rows: data, priceScale: 1 }
  return { priceScale, rows: data.map(row => ({ ...row, open: row.open == null ? null : row.open * priceScale, high: row.high * priceScale, low: row.low * priceScale, close: row.close * priceScale })) }
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
