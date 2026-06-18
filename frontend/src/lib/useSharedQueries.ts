/**
 * 共享 query hooks — 消除多页面重复的 useQuery 调用。
 *
 * 实时数据走 SSE invalidation，无需前端轮询。
 * 只有管线进度等非 SSE 数据才用 refetchInterval。
 */
import { useQuery } from '@tanstack/react-query'
import { api } from './api'
import { QK } from './queryKeys'

// ===== 全局共享 =====

/** 能力检测 — Layout / Data / Keys 共用 */
export function useCapabilities() {
  return useQuery({
    queryKey: QK.capabilities,
    queryFn: api.capabilities,
  })
}

/** 设置状态 — Layout / Data / Keys 共用 */
export function useSettings() {
  return useQuery({
    queryKey: QK.settings,
    queryFn: api.settings,
  })
}

/** 用户偏好 — Layout / Data / Intraday 共用 */
export function usePreferences() {
  return useQuery({
    queryKey: QK.preferences,
    queryFn: api.preferences,
  })
}

/** 行情状态 — SSE quotes_updated 自动刷新 */
export function useQuoteStatus(opts?: { enabled?: boolean }) {
  return useQuery({
    queryKey: QK.quoteStatus,
    queryFn: api.quoteStatus,
    enabled: opts?.enabled ?? true,
  })
}

/** 行情间隔 — Layout / Data 共用 */
export function useQuoteInterval() {
  return useQuery({
    queryKey: QK.quoteInterval,
    queryFn: api.quoteInterval,
  })
}

/** 版本号 — Layout 专用 */
export function useVersion() {
  return useQuery({
    queryKey: QK.version,
    queryFn: api.version,
    staleTime: Infinity,
  })
}

/** 数据状态 — Data / Screener 共用 */
export function useDataStatus(opts?: { staleTime?: number; refetchInterval?: number | false }) {
  return useQuery({
    queryKey: QK.dataStatus,
    queryFn: api.dataStatus,
    staleTime: opts?.staleTime,
    refetchInterval: opts?.refetchInterval,
  })
}
