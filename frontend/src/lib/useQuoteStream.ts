import { useEffect, useRef, useCallback } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { SSE_INVALIDATE_PREFIXES } from './queryKeys'
import { getQueryConfig } from './useQueryConfig'
import { toast } from '@/components/Toast'
import type { StrategyAlertEvent } from './api'

/**
 * 全局 SSE hook: 监听后端行情更新推送 + 策略监控通知。
 *
 * - 行情更新 (quotes_updated): 根据 sseRefreshPages 配置过滤 invalidation
 * - 策略监控通知 (strategy_alert): 通过 onAlert 回调弹 toast
 *
 * 应在顶层 Layout 中调用一次。
 */
export function useQuoteStream(
  enabled: boolean,
  sseRefreshPages: Record<string, boolean> | undefined,
  onAlert?: (alerts: StrategyAlertEvent[]) => void,
) {
  const qc = useQueryClient()
  const esRef = useRef<EventSource | null>(null)
  const retryRef = useRef<ReturnType<typeof setTimeout>>()
  const pagesRef = useRef(sseRefreshPages)
  pagesRef.current = sseRefreshPages

  const handleAlerts = useCallback((alerts: StrategyAlertEvent[]) => {
    // depth 系统接管通知: 单独处理, 不走 strategy 回调
    const depthAlerts = alerts.filter(a => a.source === 'depth')
    const strategyAlerts = alerts.filter(a => a.source !== 'depth')

    // depth 通知直接 toast(防刷屏: 后端已在状态切换时才推)
    for (const a of depthAlerts.slice(0, 1)) {
      toast(a.message, 'success')
    }

    if (onAlert && strategyAlerts.length > 0) {
      onAlert(strategyAlerts)
    } else if (strategyAlerts.length > 0) {
      // 默认: 弹 toast
      for (const a of strategyAlerts.slice(0, 3)) {
        const label = a.name ? `${a.symbol} ${a.name}` : a.symbol
        toast(`[${a.strategy_id}] ${label} — ${a.message}`, 'success')
      }
      if (strategyAlerts.length > 3) {
        toast(`...以及另外 ${strategyAlerts.length - 3} 条告警`, 'success')
      }
    }
  }, [onAlert])

  useEffect(() => {
    if (!enabled) {
      if (esRef.current) {
        esRef.current.close()
        esRef.current = null
      }
      return
    }

    const connect = () => {
      const es = new EventSource('/api/intraday/stream')
      esRef.current = es

      // sse-starlette ping 心跳走 SSE comment，不会到达这里

      es.addEventListener('quotes_updated', () => {
        // 根据用户配置过滤 invalidation
        const pages = pagesRef.current
        if (pages) {
          // 只 invalidate 开启的页面对应的 prefix
          const activePrefixes = SSE_INVALIDATE_PREFIXES.filter((p) => {
            // 'quote-status' 始终刷新 (全局状态)
            if (p === 'quote-status') return true
            return pages[p] !== false
          })
          qc.invalidateQueries({
            predicate: (query) =>
              activePrefixes.some(
                (prefix) => String(query.queryKey[0]).startsWith(prefix),
              ),
          })
        } else {
          // 无配置时全部刷新 (向后兼容)
          qc.invalidateQueries({
            predicate: (query) =>
              SSE_INVALIDATE_PREFIXES.some(
                (prefix) => String(query.queryKey[0]).startsWith(prefix),
              ),
          })
        }
      })

      es.addEventListener('strategy_alert', (e: MessageEvent) => {
        try {
          const data = JSON.parse(e.data)
            const alerts: StrategyAlertEvent[] = data.alerts || []
            if (alerts.length > 0) {
              handleAlerts(alerts)
            }
        } catch {
          // 忽略解析错误
        }
      })

      es.onerror = () => {
        es.close()
        esRef.current = null
        const delay = getQueryConfig().sse.reconnectDelay
        retryRef.current = setTimeout(connect, delay)
      }
    }

    connect()

    return () => {
      clearTimeout(retryRef.current)
      if (esRef.current) {
        esRef.current.close()
        esRef.current = null
      }
    }
  }, [enabled, qc, handleAlerts])
}
