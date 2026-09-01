import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import {
  AlertTriangle,
  CalendarDays,
  ChevronDown,
  ChevronUp,
  Database,
  RefreshCw,
  Trophy,
} from 'lucide-react'
import { PageHeader } from '@/components/PageHeader'
import { StockPreviewDialog } from '@/components/StockPreviewDialog'
import {
  api,
  type DragonTigerHotMoney,
  type DragonTigerStockItem,
} from '@/lib/api'
import { boardTag } from '@/lib/board'
import { cn } from '@/lib/cn'
import { fmtPct, fmtVolume, priceColorClass } from '@/lib/format'

type TabKey = 'all' | 'org' | 'hot_money'
type SortKey =
  | 'change'
  | 'net_value'
  | 'net_rate'
  | 'buy_value'
  | 'sell_value'
  | 'hot_rank'
  | 'org_net_value'
  | 'org_net_rate'
  | 'org_buy_num'
  | 'org_sell_num'

interface SortState {
  key: SortKey
  desc: boolean
}

const TABS: Array<{ key: TabKey; label: string }> = [
  { key: 'all', label: '全部' },
  { key: 'org', label: '机构' },
  { key: 'hot_money', label: '游资' },
]

const INITIAL_SORT: Record<'all' | 'org', SortState> = {
  all: { key: 'net_value', desc: true },
  org: { key: 'org_net_value', desc: true },
}

function sortNullable<T>(items: T[], pick: (item: T) => number | null | undefined, desc = true): T[] {
  return [...items].sort((left, right) => {
    const leftValue = pick(left)
    const rightValue = pick(right)
    if (leftValue == null && rightValue == null) return 0
    if (leftValue == null) return 1
    if (rightValue == null) return -1
    return (leftValue - rightValue) * (desc ? -1 : 1)
  })
}

function CountBadge({ children }: { children: React.ReactNode }) {
  return (
    <span className="rounded-full border border-border/70 bg-elevated/60 px-2 py-0.5 font-mono text-[10px] text-muted">
      {children}
    </span>
  )
}

function SortHeader({ label, sortKey, sort, onSort, align = 'right' }: {
  label: string
  sortKey: SortKey
  sort: SortState
  onSort: (key: SortKey) => void
  align?: 'left' | 'right'
}) {
  const active = sort.key === sortKey
  return (
    <button
      type="button"
      onClick={() => onSort(sortKey)}
      className={cn(
        'inline-flex w-full items-center gap-0.5 whitespace-nowrap transition-colors hover:text-foreground',
        align === 'right' ? 'justify-end' : 'justify-start',
        active && 'text-foreground',
      )}
    >
      {label}
      {active
        ? sort.desc
          ? <ChevronDown className="h-3 w-3" />
          : <ChevronUp className="h-3 w-3" />
        : <ChevronDown className="h-3 w-3 opacity-25" />}
    </button>
  )
}

function StockIdentity({ item }: { item: DragonTigerStockItem }) {
  const tag = boardTag(item.thscode)
  const concepts = (Array.isArray(item.concept_list) ? item.concept_list : [])
    .map(concept => concept.name)
    .filter((name): name is string => Boolean(name))
    .slice(0, 2)

  return (
    <div className="min-w-[11rem] text-left">
      <div className="flex items-center gap-1.5">
        <span className="font-medium text-foreground">{item.name ?? item.thscode}</span>
        {tag && (
          <span className="rounded border border-accent/25 bg-accent/10 px-1 text-[9px] font-semibold text-accent">
            {tag}
          </span>
        )}
      </div>
      <div className="mt-0.5 flex items-center gap-2 text-[10px] text-muted">
        <span className="font-mono">{item.ticker ?? item.thscode}</span>
        {concepts.length > 0 && <span className="max-w-48 truncate">{concepts.join(' · ')}</span>}
      </div>
    </div>
  )
}

function EmptyTab({ label }: { label: string }) {
  return (
    <div className="grid min-h-52 place-items-center rounded-card border border-dashed border-border bg-surface/50 px-6 text-center">
      <div>
        <Trophy className="mx-auto h-8 w-8 text-muted/40" />
        <p className="mt-3 text-sm text-secondary">本期无{label}数据</p>
        <p className="mt-1 text-xs text-muted">这不代表数据源配置异常。</p>
      </div>
    </div>
  )
}

function StockTable({ items, kind, onOpenStock }: {
  items: DragonTigerStockItem[]
  kind: 'all' | 'org'
  onOpenStock: (item: DragonTigerStockItem) => void
}) {
  const [sortByTab, setSortByTab] = useState<Record<'all' | 'org', SortState>>(INITIAL_SORT)
  const sort = sortByTab[kind]
  const sorted = useMemo(
    () => sortNullable(items, item => item[sort.key], sort.desc),
    [items, sort],
  )
  const onSort = (key: SortKey) => {
    setSortByTab(current => ({
      ...current,
      [kind]: current[kind].key === key
        ? { key, desc: !current[kind].desc }
        : { key, desc: true },
    }))
  }

  if (items.length === 0) return <EmptyTab label={kind === 'org' ? '机构榜' : '全部榜'} />

  return (
    <div className="overflow-x-auto rounded-card border border-border bg-surface/80">
      <table className={cn('w-full border-collapse text-xs', kind === 'org' ? 'min-w-[1120px]' : 'min-w-[920px]')}>
        <thead className="bg-elevated/70 text-[10px] uppercase tracking-wide text-muted">
          <tr>
            <th className="w-12 px-3 py-2 text-center font-medium">#</th>
            <th className="px-3 py-2 text-left font-medium">股票</th>
            <th className="w-24 px-3 py-2 font-medium"><SortHeader label="涨跌幅" sortKey="change" sort={sort} onSort={onSort} /></th>
            <th className="w-28 px-3 py-2 font-medium"><SortHeader label="净买额" sortKey="net_value" sort={sort} onSort={onSort} /></th>
            <th className="w-24 px-3 py-2 font-medium"><SortHeader label="净买占比" sortKey="net_rate" sort={sort} onSort={onSort} /></th>
            <th className="w-28 px-3 py-2 font-medium"><SortHeader label="买入额" sortKey="buy_value" sort={sort} onSort={onSort} /></th>
            <th className="w-28 px-3 py-2 font-medium"><SortHeader label="卖出额" sortKey="sell_value" sort={sort} onSort={onSort} /></th>
            {kind === 'org' && (
              <>
                <th className="w-28 px-3 py-2 font-medium"><SortHeader label="机构净买额" sortKey="org_net_value" sort={sort} onSort={onSort} /></th>
                <th className="w-24 px-3 py-2 font-medium"><SortHeader label="机构占比" sortKey="org_net_rate" sort={sort} onSort={onSort} /></th>
                <th className="w-20 px-3 py-2 font-medium"><SortHeader label="机构买入" sortKey="org_buy_num" sort={sort} onSort={onSort} /></th>
                <th className="w-20 px-3 py-2 font-medium"><SortHeader label="机构卖出" sortKey="org_sell_num" sort={sort} onSort={onSort} /></th>
              </>
            )}
            <th className="w-20 px-3 py-2 font-medium"><SortHeader label="人气" sortKey="hot_rank" sort={sort} onSort={onSort} /></th>
            <th className="w-20 px-3 py-2 text-right font-medium">榜期</th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((item, index) => (
            <tr
              key={`${item.thscode}-${item.range_days ?? 'unknown'}-${index}`}
              className="border-t border-border/40 transition-colors hover:bg-accent/[0.04]"
            >
              <td className="px-3 py-2 text-center font-mono text-[10px] text-muted">{index + 1}</td>
              <td className="px-3 py-2">
                <button type="button" onClick={() => onOpenStock(item)} className="hover:text-accent">
                  <StockIdentity item={item} />
                </button>
              </td>
              <td className={cn('px-3 py-2 text-right font-mono tabular-nums', priceColorClass(item.change))}>{fmtPct(item.change)}</td>
              <td className={cn('px-3 py-2 text-right font-mono tabular-nums', priceColorClass(item.net_value))}>{fmtVolume(item.net_value)}</td>
              <td className="px-3 py-2 text-right font-mono tabular-nums text-secondary">{fmtPct(item.net_rate)}</td>
              <td className="px-3 py-2 text-right font-mono tabular-nums text-secondary">{fmtVolume(item.buy_value)}</td>
              <td className="px-3 py-2 text-right font-mono tabular-nums text-secondary">{fmtVolume(item.sell_value)}</td>
              {kind === 'org' && (
                <>
                  <td className={cn('px-3 py-2 text-right font-mono tabular-nums', priceColorClass(item.org_net_value))}>{fmtVolume(item.org_net_value)}</td>
                  <td className="px-3 py-2 text-right font-mono tabular-nums text-secondary">{fmtPct(item.org_net_rate)}</td>
                  <td className="px-3 py-2 text-right font-mono tabular-nums text-secondary">{item.org_buy_num ?? '—'}</td>
                  <td className="px-3 py-2 text-right font-mono tabular-nums text-secondary">{item.org_sell_num ?? '—'}</td>
                </>
              )}
              <td className="px-3 py-2 text-right font-mono tabular-nums text-secondary">{item.hot_rank ?? '—'}</td>
              <td className="px-3 py-2 text-right">
                <span className={cn(
                  'rounded border px-1.5 py-0.5 text-[10px]',
                  item.range_days === 3
                    ? 'border-sky-500/30 bg-sky-500/10 text-sky-400'
                    : 'border-border bg-elevated/50 text-muted',
                )}>
                  {item.range_days === 3 ? '3日' : item.range_days === 1 ? '当日' : '—'}
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function HotMoneyList({ seats, onOpenStock }: {
  seats: DragonTigerHotMoney[]
  onOpenStock: (item: DragonTigerStockItem) => void
}) {
  const sortedSeats = useMemo(() => sortNullable(seats, seat => seat.buying, true), [seats])

  if (seats.length === 0) return <EmptyTab label="游资榜" />

  return (
    <div className="grid gap-3 xl:grid-cols-2">
      {sortedSeats.map((seat, index) => {
        const rows = sortNullable(seat.rows ?? [], row => row.hot_money_item_net_value ?? row.net_value, true)
        return (
          <section key={`${seat.name ?? 'seat'}-${index}`} className="rounded-card border border-border bg-surface/80 p-3">
            <div className="flex items-center gap-3 border-b border-border/50 pb-2.5">
              <span className={cn(
                'grid h-7 w-7 shrink-0 place-items-center rounded-full font-mono text-xs font-bold',
                index === 0 ? 'bg-amber-500/20 text-amber-400'
                  : index === 1 ? 'bg-zinc-400/15 text-zinc-300'
                    : index === 2 ? 'bg-orange-700/20 text-orange-400'
                      : 'bg-elevated text-muted',
              )}>
                {index + 1}
              </span>
              <div className="min-w-0 flex-1">
                <h3 className="truncate text-sm font-medium text-foreground">{seat.name ?? '未命名席位'}</h3>
                <p className="mt-0.5 text-[10px] text-muted">关联 {rows.length} 只股票</p>
              </div>
              <div className="text-right">
                <p className="text-[10px] text-muted">合计净买入</p>
                <p className={cn('font-mono text-sm tabular-nums', priceColorClass(seat.buying))}>{fmtVolume(seat.buying)}</p>
              </div>
            </div>
            {rows.length > 0 ? (
              <div className="mt-2 divide-y divide-border/40">
                {rows.map((row, rowIndex) => {
                  const amount = row.hot_money_item_net_value ?? row.net_value
                  return (
                    <button
                      key={`${row.thscode}-${rowIndex}`}
                      type="button"
                      onClick={() => onOpenStock(row)}
                      className="flex w-full items-center gap-3 py-2 text-left transition-colors hover:text-accent"
                    >
                      <StockIdentity item={row} />
                      <span className={cn('ml-auto shrink-0 font-mono text-xs tabular-nums', priceColorClass(amount))}>
                        {fmtVolume(amount)}
                      </span>
                    </button>
                  )
                })}
              </div>
            ) : (
              <p className="py-5 text-center text-xs text-muted">该席位暂无关联股票明细</p>
            )}
          </section>
        )
      })}
    </div>
  )
}

function LoadingSkeleton() {
  return (
    <div className="space-y-3">
      <div className="h-16 animate-pulse rounded-card border border-border bg-surface/70" />
      <div className="h-10 animate-pulse rounded-card border border-border bg-surface/60" />
      <div className="h-80 animate-pulse rounded-card border border-border bg-surface/50" />
    </div>
  )
}

export function DragonTigerPage() {
  const [selectedDate, setSelectedDate] = useState('')
  const [tab, setTab] = useState<TabKey>('all')
  const [preview, setPreview] = useState<{ symbol: string; name?: string } | null>(null)
  const queryDate = selectedDate || undefined
  const query = useQuery({
    queryKey: ['dragon-tiger', queryDate ?? 'latest'],
    queryFn: () => api.dragonTiger(queryDate),
    staleTime: 5 * 60_000,
    retry: 1,
  })
  const data = query.data
  const allItems = data?.all?.stock_items ?? []
  const orgItems = data?.org?.stock_items ?? []
  const seats = data?.hot_money?.hot_money_items ?? []
  const actualDate = data?.trade_date ?? data?.all?.trade_date ?? data?.org?.trade_date ?? null
  const message = data?.message || (query.error instanceof Error ? query.error.message : '龙虎榜暂不可用')

  const openStock = (item: DragonTigerStockItem) => {
    setPreview({ symbol: item.thscode, name: item.name ?? undefined })
  }

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden bg-base">
      <PageHeader
        title="龙虎榜"
        subtitle={actualDate ? `实际交易日 ${actualDate}` : 'Fuyao 三榜数据'}
        className="flex-wrap"
        right={(
          <div className="flex flex-wrap items-center justify-end gap-2">
            <label className="inline-flex h-8 items-center gap-1.5 rounded-btn border border-border bg-surface px-2 text-xs text-secondary">
              <CalendarDays className="h-3.5 w-3.5 text-muted" />
              <input
                type="date"
                value={selectedDate}
                onChange={event => setSelectedDate(event.target.value)}
                aria-label="龙虎榜日期"
                className="w-[7.8rem] bg-transparent font-mono text-xs text-foreground outline-none [color-scheme:dark]"
              />
            </label>
            <button
              type="button"
              onClick={() => setSelectedDate('')}
              disabled={!selectedDate}
              className="inline-flex h-8 items-center rounded-btn border border-border bg-surface px-2.5 text-xs text-secondary hover:text-foreground disabled:cursor-not-allowed disabled:opacity-40"
            >
              最新
            </button>
            <button
              type="button"
              onClick={() => query.refetch()}
              disabled={query.isFetching}
              className="inline-flex h-8 items-center gap-1.5 rounded-btn border border-border bg-surface px-2.5 text-xs text-secondary hover:text-foreground disabled:opacity-50"
            >
              <RefreshCw className={cn('h-3.5 w-3.5', query.isFetching && 'animate-spin')} />
              <span className="hidden sm:inline">刷新</span>
            </button>
            <Link
              to="/settings?tab=data-sources"
              className="inline-flex h-8 items-center gap-1.5 rounded-btn border border-border bg-surface px-2.5 text-xs text-secondary hover:border-accent/40 hover:text-accent"
            >
              <Database className="h-3.5 w-3.5" />
              <span className="hidden sm:inline">配置 Fuyao</span>
            </Link>
          </div>
        )}
      />

      <main className="min-h-0 flex-1 overflow-y-auto px-3 pb-6 pt-4 lg:px-5">
        {query.isLoading ? (
          <LoadingSkeleton />
        ) : data?.state === 'source_unavailable' ? (
          <div className="grid min-h-72 place-items-center rounded-card border border-dashed border-warning/40 bg-surface/60 px-6 text-center">
            <div className="max-w-md">
              <span className="mx-auto grid h-12 w-12 place-items-center rounded-full bg-warning/10 text-warning">
                <Database className="h-6 w-6" />
              </span>
              <h2 className="mt-4 text-base font-semibold text-foreground">需要配置 Fuyao API Key</h2>
              <p className="mt-2 text-sm leading-relaxed text-secondary">龙虎榜使用 Fuyao 特色数据，不需要切换当前日K、分钟或实时行情数据源。</p>
              <Link to="/settings?tab=data-sources" className="mt-5 inline-flex h-9 items-center rounded-btn bg-accent px-4 text-sm font-medium text-white hover:bg-accent/90">
                前往数据源配置
              </Link>
            </div>
          </div>
        ) : !data || data.state === 'no_data' ? (
          <div className="grid min-h-72 place-items-center rounded-card border border-border bg-surface/60 px-6 text-center">
            <div className="max-w-lg">
              <AlertTriangle className="mx-auto h-9 w-9 text-warning" />
              <h2 className="mt-3 text-base font-semibold text-foreground">龙虎榜暂不可用</h2>
              <p className="mt-2 break-words text-sm text-secondary">{message}</p>
              <button type="button" onClick={() => query.refetch()} className="mt-5 inline-flex h-9 items-center gap-2 rounded-btn border border-border bg-surface px-4 text-sm text-secondary hover:text-foreground">
                <RefreshCw className="h-4 w-4" />
                重试
              </button>
            </div>
          </div>
        ) : (
          <div className="space-y-3">
            {data.state === 'fallback_prev' && (
              <div className="flex flex-wrap items-center gap-x-2 gap-y-1 rounded-btn border border-warning/35 bg-warning/10 px-3 py-2 text-xs text-warning">
                <AlertTriangle className="h-4 w-4 shrink-0" />
                <span>请求日期 {(data.requested_date ?? selectedDate) || '最新'} 的榜单尚不可用，当前展示实际交易日 {actualDate ?? '—'} 的数据。</span>
              </div>
            )}

            <section className="flex flex-wrap items-center gap-2 rounded-card border border-border bg-surface/70 px-3 py-2.5">
              <span className="grid h-8 w-8 place-items-center rounded bg-amber-500/15 text-amber-500 ring-1 ring-amber-500/20">
                <Trophy className="h-4 w-4" />
              </span>
              <div className="mr-auto">
                <p className="text-sm font-medium text-foreground">{actualDate ?? '—'} 榜单</p>
                <p className="text-[10px] text-muted">数据源：Fuyao</p>
              </div>
              <CountBadge>全部 {data.all?.count ?? allItems.length} 条</CountBadge>
              <CountBadge>{data.all?.stock_count ?? allItems.length} 只股票</CountBadge>
              <CountBadge>机构 {data.org?.count ?? orgItems.length} 条</CountBadge>
              <CountBadge>游资 {data.hot_money?.count ?? seats.length} 席位</CountBadge>
            </section>

            <div className="flex items-center gap-1 rounded-btn border border-border bg-surface/70 p-1" role="tablist" aria-label="龙虎榜分类">
              {TABS.map(item => {
                const count = item.key === 'all' ? allItems.length : item.key === 'org' ? orgItems.length : seats.length
                return (
                  <button
                    key={item.key}
                    type="button"
                    role="tab"
                    aria-selected={tab === item.key}
                    onClick={() => setTab(item.key)}
                    className={cn(
                      'inline-flex h-8 items-center gap-1.5 rounded px-3 text-xs transition-colors',
                      tab === item.key ? 'bg-accent/15 font-medium text-accent' : 'text-muted hover:bg-elevated hover:text-foreground',
                    )}
                  >
                    {item.label}
                    <span className="font-mono text-[10px] opacity-70">{count}</span>
                  </button>
                )
              })}
            </div>

            {tab === 'all' && <StockTable items={allItems} kind="all" onOpenStock={openStock} />}
            {tab === 'org' && <StockTable items={orgItems} kind="org" onOpenStock={openStock} />}
            {tab === 'hot_money' && <HotMoneyList seats={seats} onOpenStock={openStock} />}
          </div>
        )}
      </main>

      <StockPreviewDialog
        symbol={preview?.symbol ?? null}
        name={preview?.name}
        onClose={() => setPreview(null)}
      />
    </div>
  )
}
