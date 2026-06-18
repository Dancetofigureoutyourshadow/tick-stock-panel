import { cn } from '@/lib/cn'

interface Props {
  title: string
  subtitle?: string
  right?: React.ReactNode
  className?: string
}

export function PageHeader({ title, subtitle, right, className }: Props) {
  return (
    <header
      className={cn(
        'px-5 pt-3 pb-2 border-b border-border flex items-center justify-between gap-4',
        className,
      )}
    >
      <div className="flex items-baseline gap-2">
        <h1 className="text-lg font-semibold tracking-tight">{title}</h1>
        {subtitle && <span className="text-xs text-muted">{subtitle}</span>}
      </div>
      {right}
    </header>
  )
}
