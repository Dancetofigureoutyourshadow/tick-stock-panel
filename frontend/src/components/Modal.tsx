import { useEffect, useRef, type CSSProperties, type PointerEvent as ReactPointerEvent, type ReactNode } from 'react'

/**
 * 共享模态对话框原语 — 统一处理可访问性:
 * - role="dialog" + aria-modal + aria-labelledby / aria-label
 * - ESC 关闭
 * - 打开时把焦点移入对话框 (initialFocusRef 或首个可聚焦元素)
 * - Tab / Shift+Tab 焦点陷阱 (焦点不会跑出对话框)
 * - 关闭时把焦点还给打开前的元素
 * - 点击遮罩关闭 (可用 closeOnBackdrop 关闭)
 *
 * 视觉: 提供居中遮罩 + 面板容器, 面板样式由 panelClassName 定制。
 */
export interface ModalProps {
  onClose: () => void
  children: ReactNode
  /** 对话框标题元素 id (用于 aria-labelledby) */
  labelledBy?: string
  /** 无可见标题时的无障碍名称 */
  ariaLabel?: string
  /** 面板 className (尺寸/背景/圆角等) */
  panelClassName?: string
  /** 面板内联样式；用于锚点浮窗等动态定位。 */
  panelStyle?: CSSProperties
  /** 需要检测外部点击时，可取得共享面板 DOM。 */
  panelElementRef?: (element: HTMLDivElement | null) => void
  /** 遮罩 className (覆盖默认居中/背景) */
  overlayClassName?: string
  /** 打开时聚焦的元素; 不传则聚焦面板内首个可聚焦元素 */
  initialFocusRef?: React.RefObject<HTMLElement>
  /** 点击遮罩是否关闭 (默认 true) */
  closeOnBackdrop?: boolean
  /** floating 不抢焦点、不锁定 Tab，允许点击后方内容切换锚点。 */
  interactionMode?: 'modal' | 'floating'
  /** 允许通过 dragHandleSelector 指定的头部拖动面板。 */
  draggable?: boolean
  /** 可拖动区域选择器。默认查找 data-modal-drag-handle。 */
  dragHandleSelector?: string
}

const FOCUSABLE = [
  'a[href]',
  'button:not([disabled])',
  'textarea:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

export function Modal({
  onClose,
  children,
  labelledBy,
  ariaLabel,
  panelClassName = 'w-[92vw] max-w-lg bg-surface border border-border rounded-card shadow-xl',
  panelStyle,
  panelElementRef,
  overlayClassName = 'fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm',
  initialFocusRef,
  closeOnBackdrop = true,
  interactionMode = 'modal',
  draggable = false,
  dragHandleSelector = '[data-modal-drag-handle]',
}: ModalProps) {
  const panelRef = useRef<HTMLDivElement | null>(null)
  const dragCleanupRef = useRef<() => void>(() => {})
  // 记录鼠标按下时是否落在遮罩(而非面板)上。
  // 仅当 mousedown 和 mouseup 都在遮罩时才视为"点击遮罩关闭",
  // 避免在面板内拖选文本时鼠标移出面板边缘导致误关 (拖拽穿透)。
  const mouseDownOnBackdrop = useRef(false)
  // onClose 存 ref: 焦点陷阱/ESC effect 只在挂载时装一次。否则父级每次重渲染 (或未 memo 的
  // onClose) 都让 effect 重跑, requestAnimationFrame(focusFirst) 会在每次输入后把焦点抢回
  // 面板首个元素, 导致对话框内文本框无法输入。
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose

  useEffect(() => {
    // 记住打开前的焦点, 关闭时还原
    const prevActive = document.activeElement as HTMLElement | null

    // 初始聚焦；锚点浮窗保留 K 线图上的当前焦点。
    const focusFirst = () => {
      if (interactionMode === 'floating') return
      if (initialFocusRef?.current) {
        initialFocusRef.current.focus()
        return
      }
      const panel = panelRef.current
      if (!panel) return
      const first = panel.querySelector<HTMLElement>(FOCUSABLE)
      ;(first ?? panel).focus()
    }
    // 等一帧确保内容已挂载
    const raf = requestAnimationFrame(focusFirst)

    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation()
        onCloseRef.current()
        return
      }
      if (interactionMode === 'floating') return
      if (e.key !== 'Tab') return
      const panel = panelRef.current
      if (!panel) return
      const nodes = Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE))
        .filter(el => el.offsetParent !== null || el === document.activeElement)
      if (nodes.length === 0) {
        e.preventDefault()
        panel.focus()
        return
      }
      const first = nodes[0]
      const last = nodes[nodes.length - 1]
      const active = document.activeElement as HTMLElement | null
      if (e.shiftKey) {
        if (active === first || !panel.contains(active)) {
          e.preventDefault()
          last.focus()
        }
      } else {
        if (active === last || !panel.contains(active)) {
          e.preventDefault()
          first.focus()
        }
      }
    }

    document.addEventListener('keydown', onKeyDown, true)
    return () => {
      cancelAnimationFrame(raf)
      dragCleanupRef.current()
      document.removeEventListener('keydown', onKeyDown, true)
      // 普通模态框关闭后还原焦点；浮窗从未抢焦点，无需处理。
      if (interactionMode === 'modal') prevActive?.focus?.()
    }
    // 只在挂载时装一次: onClose 走 ref, initialFocusRef 为稳定 ref 对象, 无需进依赖。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const beginDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!draggable || event.button !== 0) return
    const panel = panelRef.current
    const target = event.target
    if (!panel || !(target instanceof Element)) return
    const handle = target.closest(dragHandleSelector)
    if (!handle || !panel.contains(handle)) return
    if (target.closest('button, a, input, textarea, select, [data-modal-drag-ignore]')) return

    event.preventDefault()
    const rect = panel.getBoundingClientRect()
    const startX = event.clientX
    const startY = event.clientY
    const startLeft = rect.left
    const startTop = rect.top
    const previousUserSelect = document.body.style.userSelect
    const previousCursor = document.body.style.cursor
    document.body.style.userSelect = 'none'
    document.body.style.cursor = 'move'
    panel.style.left = `${startLeft}px`
    panel.style.top = `${startTop}px`
    panel.style.right = 'auto'
    panel.style.bottom = 'auto'
    panel.style.transform = 'none'

    const move = (moveEvent: PointerEvent) => {
      const gap = 8
      const maxLeft = Math.max(gap, window.innerWidth - rect.width - gap)
      const maxTop = Math.max(gap, window.innerHeight - rect.height - gap)
      const left = Math.min(maxLeft, Math.max(gap, startLeft + moveEvent.clientX - startX))
      const top = Math.min(maxTop, Math.max(gap, startTop + moveEvent.clientY - startY))
      panel.style.left = `${left}px`
      panel.style.top = `${top}px`
    }
    const cleanup = () => {
      document.removeEventListener('pointermove', move)
      document.removeEventListener('pointerup', cleanup)
      document.removeEventListener('pointercancel', cleanup)
      document.body.style.userSelect = previousUserSelect
      document.body.style.cursor = previousCursor
      dragCleanupRef.current = () => {}
    }
    dragCleanupRef.current()
    dragCleanupRef.current = cleanup
    document.addEventListener('pointermove', move)
    document.addEventListener('pointerup', cleanup)
    document.addEventListener('pointercancel', cleanup)
  }

  return (
    <div
      className={overlayClassName}
      onMouseDown={(e) => {
        // 仅记录"按下时确实在遮罩上"; 在面板内按下时记 false。
        mouseDownOnBackdrop.current = e.target === e.currentTarget
      }}
      onClick={closeOnBackdrop ? (e) => {
        // 只有按下和松开都在遮罩上才关闭, 避免拖选文本误关。
        if (mouseDownOnBackdrop.current && e.target === e.currentTarget) onClose()
      } : undefined}
    >
      <div
        ref={(node) => {
          panelRef.current = node
          panelElementRef?.(node)
        }}
        role="dialog"
        aria-modal={interactionMode === 'modal' ? 'true' : undefined}
        aria-labelledby={labelledBy}
        aria-label={labelledBy ? undefined : ariaLabel}
        tabIndex={-1}
        className={`outline-none ${panelClassName}`}
        style={panelStyle}
        onPointerDown={beginDrag}
        onClick={(e) => e.stopPropagation()}
      >
        {children}
      </div>
    </div>
  )
}
