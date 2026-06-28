/**
 * 复盘生成状态的全局单例 store —— 脱离 Review 组件生命周期。
 *
 * 解决的问题:生成中切换到其他页面,Review 组件卸载会丢失 phase/content。
 * 本 store 把流式生成的状态提到模块级,组件卸载后流仍在后台继续跑,
 * 回到页面订阅即可恢复显示。
 *
 * 设计:
 *  - 模块级 state(phase/content/meta/focus),唯一的生成实例
 *  - AbortController 存模块级 ref,组件卸载不中断流
 *  - 订阅者列表(notify 机制),Review mount 时订阅、unmount 时退订
 */
import { api } from '@/lib/api'

export type ReviewPhase = 'idle' | 'loading' | 'streaming' | 'done' | 'error'

export interface ReviewMeta {
  as_of?: string
  emotion_score?: number
  emotion_label?: string
  summary?: string
}

export interface ReviewState {
  phase: ReviewPhase
  content: string
  error: string
  meta: ReviewMeta | null
  focus: string
}

const INITIAL: ReviewState = { phase: 'idle', content: '', error: '', meta: null, focus: '' }

// ===== 模块级单例状态(组件卸载不销毁)=====
let state: ReviewState = { ...INITIAL }
let abortCtrl: AbortController | null = null

// ===== 订阅机制 =====
type Listener = () => void
const listeners = new Set<Listener>()

function notify() {
  for (const l of listeners) l()
}

export function getReviewState(): ReviewState {
  return state
}

export function subscribeReview(listener: Listener): () => void {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}

// 暴露给组件直接读取最新 meta(用于自动归档,避免闭包取旧值)
export function getReviewMeta(): ReviewMeta | null {
  return state.meta
}

/** 是否正在生成(loading 或 streaming) */
export function isReviewGenerating(): boolean {
  return state.phase === 'loading' || state.phase === 'streaming'
}

/**
 * 启动复盘生成。返回后流在后台独立运行,组件卸载不影响。
 * @param asOf 复盘日期
 * @param focus 用户追加的复盘关注点
 * @param onDone 完成回调(供调用方做自动归档)
 */
export async function startReviewGeneration(
  asOf: string | undefined,
  focus: string,
  onDone?: (fullContent: string, meta: ReviewMeta | null) => void,
): Promise<void> {
  // 已在生成中,不重复启动
  if (isReviewGenerating()) return

  state = { phase: 'loading', content: '', error: '', meta: null, focus }
  notify()

  abortCtrl = new AbortController()
  let buf = ''
  let failed = false
  let doneMeta: ReviewMeta | null = null

  try {
    for await (const evt of api.reviewStream(asOf, focus)) {
      if (abortCtrl.signal.aborted) break
      if (evt.type === 'meta') {
        doneMeta = evt
        state = { ...state, meta: evt }
        notify()
      } else if (evt.type === 'delta' && evt.content) {
        buf += evt.content
        state = { ...state, content: buf, phase: 'streaming' }
        notify()
      } else if (evt.type === 'error') {
        failed = true
        state = { ...state, error: evt.message ?? '复盘失败', phase: 'error' }
        notify()
        return
      } else if (evt.type === 'done') {
        state = { ...state, phase: 'done' }
        notify()
      }
    }
    // 流正常结束但无 done 事件,按 done 处理
    if (buf && !failed) {
      state = { ...state, phase: 'done' }
      notify()
      // 自动归档
      if (buf && !failed) {
        onDone?.(buf, doneMeta)
      }
    }
  } catch (e: any) {
    if (!abortCtrl.signal.aborted) {
      state = { ...state, error: e?.message ?? '复盘失败', phase: 'error' }
      notify()
    }
  } finally {
    abortCtrl = null
  }
}

/** 中断当前生成(供"查看历史"等场景主动中断流)。 */
export function abortReviewGeneration(): void {
  abortCtrl?.abort()
  abortCtrl = null
}

/** 设置当前查看的历史报告(把 store 状态切到 done + 该报告内容)。 */
export function setViewingReport(report: {
  content: string
  as_of?: string
  emotion_score?: number | null
  emotion_label?: string
  summary?: string
}): void {
  abortCtrl?.abort()
  abortCtrl = null
  state = {
    phase: 'done',
    content: report.content,
    error: '',
    meta: {
      as_of: report.as_of,
      emotion_score: report.emotion_score ?? undefined,
      emotion_label: report.emotion_label,
      summary: report.summary,
    },
    focus: state.focus,
  }
  notify()
}

/** 重置到 idle(清空当前显示)。 */
export function resetReview(): void {
  state = { ...INITIAL }
  notify()
}
