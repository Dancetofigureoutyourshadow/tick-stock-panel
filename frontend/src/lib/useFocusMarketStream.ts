import { useEffect, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import type { MinuteKlineRow } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

export interface Depth5Snapshot {
  symbol?: string
  bid_prices: Array<number | null>
  ask_prices: Array<number | null>
  bid_volumes: Array<number | null>
  ask_volumes: Array<number | null>
  timestamp?: number | null
  fetched_at?: number | null
  [key: string]: unknown
}

export interface TransactionRow {
  time: string
  price: number
  volume: number
  trade_count?: number | null
  direction: 'buy' | 'sell' | 'neutral' | 'unknown'
  direction_code?: number | null
}

export type FocusStreamStatus = 'disabled' | 'connecting' | 'connected' | 'reconnecting' | 'unavailable'
export type Depth5Status = 'disabled' | 'loading' | 'ready' | 'closed' | 'reconnecting' | 'unavailable' | 'error'
export type TransactionsStatus = 'disabled' | 'loading' | 'ready' | 'closed' | 'unavailable' | 'error'

interface FocusMarketPayload {
  symbol: string
  trade_date: string
  ts?: number
  market_open?: boolean
  minute?: {
    ok?: boolean
    row?: MinuteKlineRow | null
  }
  depth?: {
    ok?: boolean
    available?: boolean
    snapshot?: Depth5Snapshot | null
    provider?: string | null
    error?: string | null
  }
  transactions?: {
    ok?: boolean
    available?: boolean
    rows?: TransactionRow[]
    provider?: string | null
    error?: string | null
  }
  market_phase?: 'pre_open' | 'morning' | 'lunch' | 'afternoon' | 'closed'
}

interface MinuteQueryData {
  symbol: string
  date?: string | null
  rows: MinuteKlineRow[]
  source?: 'local' | 'live' | 'none'
  [key: string]: unknown
}

export function chinaToday(): string {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(new Date())
  const values = Object.fromEntries(parts.map(part => [part.type, part.value]))
  return `${values.year}-${values.month}-${values.day}`
}

function mergeMinuteRow(current: MinuteQueryData | undefined, symbol: string, date: string, row: MinuteKlineRow): MinuteQueryData {
  const rows = [...(current?.rows ?? [])]
  const index = rows.findIndex(item => item.datetime === row.datetime)
  if (index >= 0) rows[index] = row
  else rows.push(row)
  rows.sort((left, right) => left.datetime.localeCompare(right.datetime))
  return {
    ...(current ?? { symbol, date, rows: [] }),
    symbol,
    date,
    rows,
    source: 'live',
  }
}

export function useFocusMarketStream({
  symbol,
  date,
  enabled = true,
}: {
  symbol: string
  date: string | null
  enabled?: boolean
}) {
  const queryClient = useQueryClient()
  const [status, setStatus] = useState<FocusStreamStatus>('disabled')
  const [depth, setDepth] = useState<Depth5Snapshot | null>(null)
  const [depthStatus, setDepthStatus] = useState<Depth5Status>('disabled')
  const [depthError, setDepthError] = useState<string | null>(null)
  const [depthProvider, setDepthProvider] = useState<string | null>(null)
  const [updatedAt, setUpdatedAt] = useState<number | null>(null)
  const [transactions, setTransactions] = useState<TransactionRow[]>([])
  const [transactionsStatus, setTransactionsStatus] = useState<TransactionsStatus>('disabled')
  const [transactionsError, setTransactionsError] = useState<string | null>(null)
  const [transactionsUpdatedAt, setTransactionsUpdatedAt] = useState<number | null>(null)
  const [marketPhase, setMarketPhase] = useState<FocusMarketPayload['market_phase']>(undefined)

  const isCurrentDate = !!date && date === chinaToday()
  const streamEnabled = enabled && !!symbol && isCurrentDate

  useEffect(() => {
    if (!streamEnabled || !date) {
      setStatus('disabled')
      setDepth(null)
      setDepthStatus('disabled')
      setDepthError(null)
      setDepthProvider(null)
      setUpdatedAt(null)
      setTransactions([])
      setTransactionsStatus('disabled')
      setTransactionsError(null)
      setTransactionsUpdatedAt(null)
      setMarketPhase(undefined)
      return
    }

    setStatus('connecting')
    setDepth(null)
    setDepthStatus('loading')
    setDepthError(null)
    setDepthProvider(null)
    setUpdatedAt(null)
    setTransactions([])
    setTransactionsStatus('loading')
    setTransactionsError(null)
    setTransactionsUpdatedAt(null)
    setMarketPhase(undefined)

    const source = new EventSource(`/api/intraday/focus-stream?symbol=${encodeURIComponent(symbol)}`)
    const handleUpdate = (event: Event) => {
      let payload: FocusMarketPayload
      try {
        payload = JSON.parse((event as MessageEvent<string>).data) as FocusMarketPayload
      } catch {
        return
      }
      if (payload.symbol !== symbol || payload.trade_date !== date) return

      setStatus('connected')
      setMarketPhase(payload.market_phase)
      if (payload.market_open === false) {
        setDepthStatus('unavailable')
        setDepthError('当前非交易时段')
      }
      const depthResult = payload.depth
      if (depthResult) {
        setDepthProvider(depthResult.provider ?? null)
        setDepthError(depthResult.error ?? null)
        if (depthResult.available === false) {
          setDepth(null)
          setDepthStatus('unavailable')
        } else if (depthResult.ok && depthResult.snapshot) {
          setDepth(depthResult.snapshot)
          setDepthStatus(payload.market_open === false ? 'closed' : 'ready')
          setUpdatedAt(depthResult.snapshot.fetched_at ?? depthResult.snapshot.timestamp ?? null)
        } else if (depthResult.error) {
          setDepthStatus('error')
        }
      }

      const transactionsResult = payload.transactions
      if (transactionsResult) {
        setTransactionsError(transactionsResult.error ?? null)
        if (transactionsResult.available === false) {
          setTransactions([])
          setTransactionsStatus('unavailable')
        } else if (transactionsResult.ok && transactionsResult.rows) {
          setTransactions(transactionsResult.rows)
          setTransactionsStatus(payload.market_open === false ? 'closed' : 'ready')
          setTransactionsUpdatedAt(payload.ts ?? null)
        } else if (transactionsResult.error) {
          setTransactionsStatus('error')
        }
      }

      const minuteResult = payload.minute
      if (minuteResult?.ok && minuteResult.row) {
        const row = minuteResult.row as MinuteKlineRow
        queryClient.setQueryData<MinuteQueryData>(
          QK.klineMinute(symbol, date),
          current => mergeMinuteRow(current, symbol, date, row),
        )
        // 多日分时组件使用空日期作为“当前日最新”查询键，也同步更新该缓存。
        queryClient.setQueryData<MinuteQueryData>(
          QK.klineMinute(symbol, ''),
          current => mergeMinuteRow(current, symbol, payload.trade_date, row),
        )
      }
    }

    const handleStatus = (event: Event) => {
      try {
        const payload = JSON.parse((event as MessageEvent<string>).data) as { status?: string; message?: string }
        if (payload.status === 'unavailable') {
          setStatus('unavailable')
          setDepth(null)
          setDepthStatus('unavailable')
          setDepthError(payload.message ?? '实时行情流不可用')
          setTransactions([])
          setTransactionsStatus('unavailable')
          setTransactionsError(payload.message ?? '实时行情流不可用')
        }
      } catch {
        setStatus('unavailable')
        setDepth(null)
        setDepthStatus('unavailable')
        setDepthError('实时行情流不可用')
        setTransactions([])
        setTransactionsStatus('unavailable')
        setTransactionsError('实时行情流不可用')
      }
    }

    source.addEventListener('focus_market_updated', handleUpdate)
    source.addEventListener('market_stream_status', handleStatus)
    source.onerror = () => setStatus(current => current === 'unavailable' ? current : 'reconnecting')

    return () => {
      source.removeEventListener('focus_market_updated', handleUpdate)
      source.removeEventListener('market_stream_status', handleStatus)
      source.close()
    }
  }, [date, queryClient, streamEnabled, symbol])

  return {
    streamEnabled,
    isCurrentDate,
    status,
    depth,
    depthStatus,
    depthError,
    depthProvider,
    updatedAt,
    transactions,
    transactionsStatus,
    transactionsError,
    transactionsUpdatedAt,
    marketPhase,
  }
}
