import { Sparkles } from 'lucide-react'
import { EmptyState } from '@/components/EmptyState'

export function Onboarding() {
  return (
    <div className="min-h-screen bg-base">
      <EmptyState
        icon={Sparkles}
        title="欢迎使用 TF-Stocks-Panel"
        hint="首次运行向导将在 Phase 1 接入(填 TickFlow Key → 能力探测 → 试用面板)。当前请直接进入面板。"
      />
    </div>
  )
}
