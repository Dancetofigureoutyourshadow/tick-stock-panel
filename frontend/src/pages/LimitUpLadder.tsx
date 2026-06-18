import { useState, useCallback, useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { motion, AnimatePresence } from 'framer-motion'
import { RefreshCw, ChevronDown, Flame, Settings2, X } from 'lucide-react'
import { DatePicker } from '@/components/DatePicker'
import { api, type LimitLadderTier, type LimitLadderStock } from '@/lib/api'
import { StockPreviewDialog } from '@/components/StockPreviewDialog'
import { QK } from '@/lib/queryKeys'
import { storage } from '@/lib/storage'
import { fmtPct, priceColorClass } from '@/lib/format'
import { PageHeader } from '@/components/PageHeader'
import { EmptyState } from '@/components/EmptyState'
import type { ExtColumnDisplayConfig } from '@/lib/watchlist-columns'

// ===== Ext 字段配置 =====

/** 每个字段的完整配置：字段来源 + 渲染方式 */
interface ExtFieldItem {
  /** "config_id.field_name"，空=不显示 */
  field?: string
  /** 渲染配置（分隔符、显示模式、maxTags 等） */
  display?: ExtColumnDisplayConfig
}

interface BrokenFailedConfig {
  /** 炸板：计算N板以上（0=不限，即首板炸板也算） */
  brokenMinBoards?: number
  /** 断板：计算N板以上 */
  failedMinBoards?: number
  /** 是否计算炸板数 */
  brokenCount?: boolean
  /** 是否计算断板数 */
  failedCount?: boolean
  /** 是否显示炸板股票 */
  brokenShow?: boolean
  /** 是否显示断板股票 */
  failedShow?: boolean
}

interface ExtFieldConfig {
  concept?: ExtFieldItem
  industry?: ExtFieldItem
  /** 炸板/断板过滤配置 */
  bf?: BrokenFailedConfig
  /** 显示概念分布统计 */
  showConceptStats?: boolean
  /** 显示行业分布统计 */
  showIndustryStats?: boolean
  /** 显示分组概念分布统计 */
  showConceptGroupStats?: boolean
  /** 显示分组行业分布统计 */
  showIndustryGroupStats?: boolean
}

const DEFAULT_BF: BrokenFailedConfig = {
  brokenMinBoards: 0,
  failedMinBoards: 0,
  brokenCount: true,
  failedCount: true,
  brokenShow: true,
  failedShow: true,
}

function loadExtFields(): ExtFieldConfig {
  const raw = storage.limitLadderExtFields.get({}) as any
  if (!raw) return {}
  // 兼容旧格式 { concept: "id.field", conceptSep: "x" }
  if (typeof raw.concept === 'string') {
    return {
      concept: raw.concept ? { field: raw.concept, display: { displayMode: 'tag', separator: raw.conceptSep } } : undefined,
      industry: raw.industry ? { field: raw.industry, display: { displayMode: 'tag', separator: raw.industrySep } } : undefined,
    }
  }
  return raw
}

/** 根据显示开关过滤 extFields */
function resolveExtFields(fields: ExtFieldConfig, showConcept: boolean, showIndustry: boolean): ExtFieldConfig {
  return {
    concept: showConcept ? fields.concept : undefined,
    industry: showIndustry ? fields.industry : undefined,
    showConceptGroupStats: fields.showConceptGroupStats,
    showIndustryGroupStats: fields.showIndustryGroupStats,
  }
}

function buildExtColumnsParam(fields: ExtFieldConfig): string | undefined {
  const parts = [fields.concept?.field, fields.industry?.field].filter(Boolean)
  return parts.length > 0 ? parts.join(',') : undefined
}

/** 从 stock row 中取出 ext 字段值，按配置渲染 */
function getExtTags(stock: LimitLadderStock, item?: ExtFieldItem): string[] {
  if (!item?.field) return []
  const key = item.field.replace('.', '__')
  const v = (stock as unknown as Record<string, unknown>)[key]
  if (v == null) return []
  const str = String(v)
  if (!str) return []

  const cfg = item.display
  if (cfg?.displayMode === 'text') return [str]

  const sep = cfg?.separator?.trim() || null
  const tags = sep
    ? str.split(sep).map(s => s.trim()).filter(Boolean)
    : str.split(/[、,，;；\-]/).map(s => s.trim()).filter(Boolean)

  const maxTags = cfg?.maxTags ?? 0
  const sliced = maxTags > 0 ? tags.slice(0, maxTags) : tags
  const hiddenIndices = maxTags > 0 ? cfg?.hiddenIndices : undefined
  return hiddenIndices?.length
    ? sliced.filter((_, i) => !hiddenIndices.includes(i))
    : sliced
}

// ===== 板块标识 =====

function boardTag(symbol: string): { label: string; cls: string } | null {
  if (/^(300|301)/.test(symbol)) return { label: '创', cls: 'text-[#f97316] bg-[#f97316]/12 border-[#f97316]/25' }
  if (/^688/.test(symbol))       return { label: '科', cls: 'text-cyan-400 bg-cyan-400/12 border-cyan-400/25' }
  if (/\.BJ$/.test(symbol))      return { label: '北', cls: 'text-purple-400 bg-purple-400/12 border-purple-400/25' }
  return null
}

// ===== 状态标识 + 卡片样式 =====

const STATUS_STYLE: Record<string, { bg: string; bar: string; nameCls: string; codeCls: string; badge: string; badgeText: string; cardStyle?: React.CSSProperties }> = {
  limit_up: {
    bg: '',
    bar: 'border-l-2 border-bull/50',
    nameCls: 'text-rose-50 text-[13px]',
    codeCls: 'text-muted/80',
    badge: '',
    badgeText: '',
    cardStyle: {
      background: 'linear-gradient(105deg, hsl(4 60% 45% / 0.14) 0%, hsl(6 50% 30% / 0.09) 40%, hsl(220 15% 12% / 0.0) 100%)',
      boxShadow: 'inset 1px 0 0 hsl(4 80% 55% / 0.12), 0 0 10px -4px hsl(4 80% 50% / 0.10)',
    },
  },
  broken: {
    bg: 'opacity-75',
    bar: 'border-l border-purple-400/30',
    nameCls: 'text-foreground/70 text-xs',
    codeCls: 'text-muted/60',
    badge: 'text-purple-400',
    badgeText: '炸',
  },
  failed: {
    bg: 'opacity-75',
    bar: 'border-l border-muted/25',
    nameCls: 'text-foreground/70 text-xs',
    codeCls: 'text-muted/60',
    badge: 'text-muted/80',
    badgeText: '断',
  },
}

// ===== 单只股票卡片 =====

function StockCard({ stock, extFields, onClick }: {
  stock: LimitLadderStock
  extFields: ExtFieldConfig
  onClick: () => void
}) {
  const code = stock.symbol.replace(/\.BJ$/, '').replace(/\.SZ$/, '').replace(/\.SH$/, '')
  const tag = boardTag(stock.symbol)
  const status = stock.status || 'limit_up'
  const style = STATUS_STYLE[status] || STATUS_STYLE.limit_up
  const isLimitUp = status === 'limit_up'
  const conceptTags = getExtTags(stock, extFields.concept)
  const industryTags = getExtTags(stock, extFields.industry)
  const isTextConcept = extFields.concept?.display?.displayMode === 'text'
  const isTextIndustry = extFields.industry?.display?.displayMode === 'text'
  const conceptLayout = extFields.concept?.display?.tagLayout ?? 'horizontal'
  const industryLayout = extFields.industry?.display?.tagLayout ?? 'horizontal'

  const tagCls = 'text-[9px] leading-none px-1 py-px rounded-sm'
  const conceptCls = 'text-[10px] leading-none px-1.5 py-0.5 rounded-sm text-orange-200/60 bg-orange-400/[0.05]'
  const industryCls = 'text-[10px] leading-none px-1.5 py-0.5 rounded-sm text-sky-300/90 bg-sky-400/10'
  const textCls = `${tagCls} text-secondary/60 bg-elevated/60`

  const hasTags = conceptTags.length > 0 || industryTags.length > 0

  return (
    <button
      onClick={onClick}
      className={`group flex flex-col items-start gap-1 px-2.5 py-2 rounded-md transition-all duration-200 cursor-pointer hover:opacity-100 ${style.bg} ${style.bar}`}
      style={style.cardStyle ? { ...style.cardStyle } : undefined}
      onMouseEnter={e => {
        if (!style.cardStyle) return
        e.currentTarget.style.boxShadow = 'inset 1px 0 0 hsl(4 80% 55% / 0.30), 0 0 18px -4px hsl(4 80% 50% / 0.28)'
      }}
      onMouseLeave={e => {
        if (!style.cardStyle) return
        e.currentTarget.style.boxShadow = style.cardStyle.boxShadow ?? ''
      }}
    >
      {/* 名称行 */}
      <div className="flex items-center gap-1.5 w-full min-w-0">
        <span className={`${style.nameCls} font-medium truncate`}>{stock.name}</span>
        {tag && (
          <span className={`shrink-0 text-[9px] px-1 py-px rounded-full border leading-none ${tag.cls}`}>{tag.label}</span>
        )}
      </div>
      {/* 代码 + 数字行 */}
      <div className="flex items-center gap-1.5 w-full">
        <span className={`${style.codeCls} font-mono text-[10px] tracking-tight`}>{code}</span>
        <span className="ml-auto flex items-center gap-1">
          {!isLimitUp ? (
            <span className={`text-[10px] font-semibold tabular-nums ${priceColorClass(stock.change_pct)}`}>
              {fmtPct(stock.change_pct)}
            </span>
          ) : (
            <span className="text-[10px] font-semibold tabular-nums text-accent/80">
              {stock.consecutive_limit_ups}
            </span>
          )}
          {style.badgeText && (
            <span className={`text-[9px] font-medium ${style.badge}`}>{style.badgeText}</span>
          )}
        </span>
      </div>
      {/* 标签行 */}
      {hasTags && (
        <div className="flex flex-col gap-0.5 w-full">
          {conceptTags.length > 0 && (
            <div className={`flex gap-0.5 ${conceptLayout === 'vertical' ? 'flex-col items-start' : 'flex-wrap'}`}>
              {conceptTags.map((t, i) => (
                <span key={i} className={isTextConcept ? textCls : conceptCls}>{t}</span>
              ))}
            </div>
          )}
          {industryTags.length > 0 && (
            <div className={`flex gap-0.5 ${industryLayout === 'vertical' ? 'flex-col items-start' : 'flex-wrap'}`}>
              {industryTags.map((t, i) => (
                <span key={i} className={isTextIndustry ? textCls : industryCls}>{t}</span>
              ))}
            </div>
          )}
        </div>
      )}
    </button>
  )
}

// ===== 过滤（多选） =====

type FilterKey = 'limit_up' | 'broken' | 'failed' | 'main' | 'chinext' | 'star' | 'bj' | 'st'

const STATUS_TABS: { key: FilterKey; label: string }[] = [
  { key: 'limit_up', label: '涨停' },
  { key: 'broken', label: '炸板' },
  { key: 'failed', label: '断板' },
]

const BOARD_TABS: { key: FilterKey; label: string }[] = [
  { key: 'main', label: 'A主板' },
  { key: 'chinext', label: '创业板' },
  { key: 'star', label: '科创板' },
  { key: 'bj', label: '北交所' },
  { key: 'st', label: 'ST' },
]

function matchFilter(stock: LimitLadderStock, key: FilterKey): boolean {
  const s = stock.symbol
  const n = (stock.name ?? '').toUpperCase()
  switch (key) {
    case 'limit_up':
      return stock.status === 'limit_up' || !stock.status
    case 'broken':
      return stock.status === 'broken'
    case 'failed':
      return stock.status === 'failed'
    case 'main':
      return !/^(300|301|688)/.test(s) && !/\.BJ$/.test(s) && !n.includes('ST')
    case 'chinext':
      return /^(300|301)/.test(s)
    case 'star':
      return /^688/.test(s)
    case 'bj':
      return /\.BJ$/.test(s)
    case 'st':
      return n.includes('ST')
  }
}

function isStatusKey(key: FilterKey): key is 'limit_up' | 'broken' | 'failed' {
  return key === 'limit_up' || key === 'broken' || key === 'failed'
}

function filterTiers(tiers: LimitLadderTier[], keys: Set<FilterKey>, bf?: BrokenFailedConfig): LimitLadderTier[] {
  const cfg = { ...DEFAULT_BF, ...bf }
  if (keys.size === 0) return tiers

  const statusKeys = [...keys].filter(isStatusKey)
  const boardKeys = [...keys].filter(k => !isStatusKey(k))

  return tiers
    .map(t => ({
      ...t,
      stocks: t.stocks.filter(s => {
        // 炸板/断板：先按 boards 阈值过滤
        if (s.status === 'broken' && (cfg.brokenMinBoards ?? 0) > 0 && t.boards < (cfg.brokenMinBoards ?? 0)) return false
        if (s.status === 'failed' && (cfg.failedMinBoards ?? 0) > 0 && t.boards < (cfg.failedMinBoards ?? 0)) return false
        // 炸板/断板：是否显示
        if (s.status === 'broken' && !cfg.brokenShow) return false
        if (s.status === 'failed' && !cfg.failedShow) return false
        // 状态组 AND 板块组：两组各至少匹配一个
        const statusOk = statusKeys.length === 0 || statusKeys.some(k => matchFilter(s, k))
        if (!statusOk) return false
        const boardOk = boardKeys.length === 0 || boardKeys.some(k => matchFilter(s, k))
        if (!boardOk) return false
        return true
      }),
    }))
    .map(t => ({ ...t, count: t.stocks.length }))
    .filter(t => t.count > 0)
}

// ===== 过滤持久化 =====

const DEFAULT_FILTERS = new Set<FilterKey>(['limit_up', 'main', 'chinext', 'star', 'bj'])

function loadFilterKeys(): Set<FilterKey> {
  const arr = storage.limitLadderBoard.get([])
  const allTabs = [...STATUS_TABS, ...BOARD_TABS]
  const valid = arr.filter((k): k is FilterKey => allTabs.some(t => t.key === k))
  return valid.length > 0 ? new Set(valid) : new Set(DEFAULT_FILTERS)
}

// ===== 梯队颜色 =====

const TIER_COLORS: Record<number, string> = {
  1: 'border-border',
  2: 'border-yellow-600/40',
  3: 'border-orange-500/50',
}

const TIER_TEXT: Record<number, string> = {
  1: 'text-muted',
  2: 'text-yellow-500',
  3: 'text-orange-400',
}

function tierBorder(n: number): string {
  for (let i = Math.min(n, 20); i >= 1; i--) {
    if (TIER_COLORS[i]) return TIER_COLORS[i]
  }
  return 'border-border'
}

function tierTextCls(n: number): string {
  for (let i = Math.min(n, 20); i >= 1; i--) {
    if (TIER_TEXT[i]) return TIER_TEXT[i]
  }
  return 'text-muted'
}

function tierLabel(n: number): string {
  return n === 1 ? '首板' : `${n}板`
}

// ===== 梯队总览条 =====

function OverviewBar({ tiers, dateValue, onDateChange, filterKeys, bf }: {
  tiers: LimitLadderTier[]
  dateValue: string
  onDateChange: (v: string) => void
  filterKeys: Set<FilterKey>
  bf?: BrokenFailedConfig
}) {
  if (tiers.length === 0) return null
  const cfg = { ...DEFAULT_BF, ...bf }
  const limitUpCounts = tiers.map(t => t.stocks.filter(s => s.status === 'limit_up' || !s.status).length)
  const maxCount = Math.max(...limitUpCounts, 1)
  const showBroken = filterKeys.has('broken') && cfg.brokenShow
  const showFailed = filterKeys.has('failed') && cfg.failedShow
  const totalBroken = cfg.brokenCount
    ? tiers.reduce((s, t) => s + t.stocks.filter(st => st.status === 'broken').length, 0)
    : 0
  const totalFailed = cfg.failedCount
    ? tiers.reduce((s, t) => s + t.stocks.filter(st => st.status === 'failed').length, 0)
    : 0

  return (
    <div className="flex items-center gap-4 px-5 py-2">
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-secondary">
        {tiers.map((t, idx) => {
          const luCount = limitUpCounts[idx]
          return (
            <div key={t.boards} className="flex items-center gap-1">
              <span className={`font-medium ${tierTextCls(t.boards)}`}>{tierLabel(t.boards)}</span>
              <div
                className="h-2 rounded-sm bg-accent/40"
                style={{ width: `${Math.max(8, (luCount / maxCount) * 48)}px` }}
              />
              <span className="text-muted">{luCount}</span>
            </div>
          )
        })}
        {showBroken && totalBroken > 0 && (
          <span className="text-purple-400 font-medium">炸板 {totalBroken}</span>
        )}
        {showFailed && totalFailed > 0 && (
          <span className="text-yellow-500 font-medium">断板 {totalFailed}</span>
        )}
      </div>
      <div className="ml-auto">
        <DatePicker value={dateValue} onChange={onDateChange} />
      </div>
    </div>
  )
}

// ===== 标签统计面板 =====

function TagStats({ title, tiers, extFields, fieldKey, color, selectedTag, onSelect }: {
  title: string
  tiers: LimitLadderTier[]
  extFields: ExtFieldConfig
  fieldKey: 'concept' | 'industry'
  color: { text: [number, number, number]; bg: [number, number, number] }
  selectedTag: { fieldKey: 'concept' | 'industry'; tag: string } | null
  onSelect: (sel: { fieldKey: 'concept' | 'industry'; tag: string } | null) => void
}) {
  const [expanded, setExpanded] = useState(false)

  const stats = useMemo(() => {
    const item = extFields[fieldKey]
    if (!item?.field) return [] as [string, number][]
    const counts = new Map<string, number>()
    for (const t of tiers) {
      for (const s of t.stocks) {
        if (s.status && s.status !== 'limit_up') continue
        const tags = getExtTags(s, item)
        for (const tag of tags) {
          counts.set(tag, (counts.get(tag) || 0) + 1)
        }
      }
    }
    return [...counts.entries()].sort((a, b) => b[1] - a[1])
  }, [tiers, extFields, fieldKey])

  if (stats.length === 0) return null

  const maxCount = stats[0]?.[1] ?? 1
  const [r, g, b] = color.text
  const [br, bg, bb] = color.bg
  const needsExpand = stats.length > 10

  return (
    <div className="px-4">
      <button
        onClick={() => needsExpand && setExpanded(v => !v)}
        className={`flex items-center gap-1.5 mb-1.5 w-full group ${needsExpand ? 'cursor-pointer' : 'cursor-default'}`}
      >
        <span className="text-[10px] tracking-wider text-muted">{title}</span>
        <span className="text-[10px] text-muted/50">{stats.length}</span>
        {needsExpand && (
          <span className="text-[10px] text-muted/60 group-hover:text-muted ml-auto flex items-center gap-0.5 transition-colors">
            {expanded ? '收起' : '展开'}
            <ChevronDown className={`h-3 w-3 transition-transform ${expanded ? 'rotate-180' : ''}`} />
          </span>
        )}
      </button>
      <div className="relative">
        <div
          className={`flex flex-wrap gap-1.5 pt-0.5 pl-1 transition-all duration-300 ${
            expanded ? 'pb-2.5' : 'pb-0.5 max-h-[3.5rem] overflow-hidden'
          }`}
        >
          {stats.map(([name, count]) => {
            const intensity = Math.max(0.15, count / maxCount)
            const isSelected = selectedTag?.fieldKey === fieldKey && selectedTag?.tag === name
            return (
              <button
                key={name}
                onClick={() => onSelect(isSelected ? null : { fieldKey, tag: name })}
                className="text-[11px] px-2 py-1 rounded-sm whitespace-nowrap cursor-pointer hover:brightness-110 transition-all"
                style={{
                  color: isSelected ? '#fff' : `rgba(${r},${g},${b},${0.6 + intensity * 0.4})`,
                  backgroundColor: isSelected
                    ? `rgba(${br},${bg},${bb},0.7)`
                    : `rgba(${br},${bg},${bb},${intensity * 0.2})`,
                  outline: isSelected ? `1px solid rgba(${r},${g},${b},0.8)` : 'none',
                  outlineOffset: 1,
                }}
              >
                {name}
                <span className="ml-1" style={{ opacity: isSelected ? 0.8 : 0.6 }}>{count}</span>
              </button>
            )
          })}
        </div>
        {/* 折叠渐变遮罩 */}
        {needsExpand && !expanded && (
          <div
            className="absolute bottom-0 left-0 right-0 h-4 pointer-events-none"
            style={{ background: 'linear-gradient(to bottom, transparent 0%, hsl(var(--surface)) 100%)' }}
          />
        )}
      </div>
    </div>
  )
}

// ===== 梯队分组 =====

function TierGroup({ tier, defaultOpen, extFields, filterKeys, bf, onStockClick, selectedTag, onSelectTag }: {
  tier: LimitLadderTier
  defaultOpen: boolean
  extFields: ExtFieldConfig
  filterKeys: Set<FilterKey>
  bf?: BrokenFailedConfig
  onStockClick: (symbol: string, name?: string) => void
  selectedTag: { fieldKey: 'concept' | 'industry'; tag: string } | null
  onSelectTag: (sel: { fieldKey: 'concept' | 'industry'; tag: string } | null) => void
}) {
  const [open, setOpen] = useState(defaultOpen)
  const cfg = { ...DEFAULT_BF, ...bf }
  const showBroken = filterKeys.has('broken') && cfg.brokenShow
  const showFailed = filterKeys.has('failed') && cfg.failedShow

  const luCount = tier.stocks.filter(s => s.status === 'limit_up' || !s.status).length
  const brCount = cfg.brokenCount ? tier.stocks.filter(s => s.status === 'broken').length : 0
  const faCount = cfg.failedCount ? tier.stocks.filter(s => s.status === 'failed').length : 0

  // 分组概念/行业统计
  const groupConceptStats = useMemo(() => {
    if (!extFields.showConceptGroupStats || !extFields.concept?.field) return []
    const counts = new Map<string, number>()
    for (const s of tier.stocks) {
      if (s.status && s.status !== 'limit_up') continue
      for (const tag of getExtTags(s, extFields.concept)) {
        counts.set(tag, (counts.get(tag) || 0) + 1)
      }
    }
    return [...counts.entries()].sort((a, b) => b[1] - a[1])
  }, [tier.stocks, extFields])

  const groupIndustryStats = useMemo(() => {
    if (!extFields.showIndustryGroupStats || !extFields.industry?.field) return []
    const counts = new Map<string, number>()
    for (const s of tier.stocks) {
      if (s.status && s.status !== 'limit_up') continue
      for (const tag of getExtTags(s, extFields.industry)) {
        counts.set(tag, (counts.get(tag) || 0) + 1)
      }
    }
    return [...counts.entries()].sort((a, b) => b[1] - a[1])
  }, [tier.stocks, extFields])

  const hasGroupStats = groupConceptStats.length > 0 || groupIndustryStats.length > 0

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      className={`border-l-2 ${tierBorder(tier.boards)} rounded-r-lg bg-surface/50`}
    >
      {/* 头部 */}
      <button
        onClick={() => setOpen(v => !v)}
        className="w-full flex items-center gap-2 px-3 py-2 hover:bg-surface/80 transition-colors"
      >
        <Flame className={`h-3.5 w-3.5 ${tier.boards >= 5 ? 'text-orange-500' : tier.boards >= 3 ? 'text-yellow-500' : 'text-muted'}`} />
        <span className={`text-sm font-bold tabular-nums ${tierTextCls(tier.boards)}`}>{tierLabel(tier.boards)}<span className="text-muted/40 mx-1">·</span>{luCount}</span>
        {(showBroken && brCount > 0) || (showFailed && faCount > 0) ? (
          <span className="text-[11px] text-muted/60">
            {showBroken && brCount > 0 && <span className="text-purple-400">{brCount}炸</span>}
            {showBroken && brCount > 0 && showFailed && faCount > 0 && <span className="text-muted/40"> · </span>}
            {showFailed && faCount > 0 && <span className="text-muted/80">{faCount}断</span>}
          </span>
        ) : null}
        <ChevronDown
          className={`h-3.5 w-3.5 ml-auto text-muted transition-transform ${open ? 'rotate-180' : ''}`}
        />
      </button>

      {/* 股票列表 */}
      <AnimatePresence>
        {open && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.15 }}
            className="overflow-hidden"
          >
            {/* 分组统计 */}
            {hasGroupStats && (
              <div className="px-3 pt-1 pb-2 space-y-1">
                {groupConceptStats.length > 0 && (
                  <div className="flex flex-wrap gap-1 items-center">
                    <span className="text-[9px] tracking-wider text-yellow-400/70 mr-0.5">概念</span>
                    {groupConceptStats.slice(0, 20).map(([name, count]) => {
                      const isSelected = selectedTag?.fieldKey === 'concept' && selectedTag?.tag === name
                      return (
                        <button
                          key={name}
                          onClick={() => onSelectTag(isSelected ? null : { fieldKey: 'concept', tag: name })}
                          className="text-[10px] px-1.5 py-0.5 rounded-sm whitespace-nowrap cursor-pointer hover:brightness-110 transition-all"
                          style={{
                            color: isSelected ? '#fff' : 'rgba(250,204,21,0.8)',
                            backgroundColor: isSelected ? 'rgba(234,179,8,0.7)' : 'rgba(234,179,8,0.12)',
                            outline: isSelected ? '1px solid rgba(250,204,21,0.8)' : 'none',
                            outlineOffset: 1,
                          }}
                        >
                          {name}<span className="ml-0.5" style={{ opacity: isSelected ? 0.8 : 0.6 }}>{count}</span>
                        </button>
                      )
                    })}
                  </div>
                )}
                {groupIndustryStats.length > 0 && (
                  <div className="flex flex-wrap gap-1 items-center">
                    <span className="text-[9px] tracking-wider text-blue-400/70 mr-0.5">行业</span>
                    {groupIndustryStats.slice(0, 20).map(([name, count]) => {
                      const isSelected = selectedTag?.fieldKey === 'industry' && selectedTag?.tag === name
                      return (
                        <button
                          key={name}
                          onClick={() => onSelectTag(isSelected ? null : { fieldKey: 'industry', tag: name })}
                          className="text-[10px] px-1.5 py-0.5 rounded-sm whitespace-nowrap cursor-pointer hover:brightness-110 transition-all"
                          style={{
                            color: isSelected ? '#fff' : 'rgba(96,165,250,0.8)',
                            backgroundColor: isSelected ? 'rgba(59,130,246,0.7)' : 'rgba(59,130,246,0.12)',
                            outline: isSelected ? '1px solid rgba(96,165,250,0.8)' : 'none',
                            outlineOffset: 1,
                          }}
                        >
                          {name}<span className="ml-0.5" style={{ opacity: isSelected ? 0.8 : 0.6 }}>{count}</span>
                        </button>
                      )
                    })}
                  </div>
                )}
              </div>
            )}
            <div className="grid grid-cols-[repeat(auto-fill,minmax(140px,1fr))] gap-3 px-3 pb-3">
              {[...tier.stocks]
                .filter(s => {
                  if (!selectedTag) return true
                  const item = extFields[selectedTag.fieldKey]
                  if (!item) return true
                  const tags = getExtTags(s, item)
                  return tags.includes(selectedTag.tag)
                })
                .sort((a, b) => {
                  const ord = (s: string) => s === 'limit_up' || !s ? 0 : s === 'broken' ? 1 : 2
                  return ord(a.status ?? '') - ord(b.status ?? '')
                }).map(s => (
                <StockCard
                  key={`${s.symbol}-${s.status}`}
                  stock={s}
                  extFields={extFields}
                  onClick={() => onStockClick(s.symbol, s.name ?? undefined)}
                />
              ))}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  )
}

// ===== 字段配置弹窗 =====

type SchemaOption = { id: string; label: string; columns: { name: string; label: string }[] }

/** 开关行 */
function Toggle({ label, checked, onChange }: { label: string; checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <label className="flex items-center justify-between gap-2 text-xs cursor-pointer">
      <span className="text-secondary">{label}</span>
      <span
        role="switch"
        aria-checked={checked}
        onClick={() => onChange(!checked)}
        style={{ width: 32, height: 18, position: 'relative', borderRadius: 9, transition: 'background-color 0.15s', cursor: 'pointer' }}
        className={checked ? 'bg-accent/60' : 'bg-border'}
      >
        <span
          style={{
            position: 'absolute', top: 2, left: 0, width: 14, height: 14, borderRadius: '50%', background: '#fff',
            transition: 'transform 0.15s', transform: checked ? 'translateX(16px)' : 'translateX(2px)',
          }}
        />
      </span>
    </label>
  )
}

/** 数字输入行 */
function NumInput({ label, value, onChange, min, max, placeholder }: {
  label: string; value: number | undefined; onChange: (v: number | undefined) => void; min?: number; max?: number; placeholder?: string
}) {
  return (
    <label className="flex items-center justify-between gap-2 text-xs">
      <span className="text-secondary">{label}</span>
      <input
        type="number"
        min={min}
        max={max}
        value={value ?? ''}
        onChange={e => {
          let v = e.target.value ? Number(e.target.value) : undefined
          if (v != null) {
            if (min != null && v < min) v = min
            if (max != null && v > max) v = max
          }
          onChange(v)
        }}
        placeholder={placeholder}
        className="w-16 h-7 bg-elevated border border-border rounded text-xs text-foreground text-center px-1 placeholder:text-muted focus:outline-none focus:border-accent/50"
      />
    </label>
  )
}

/** 字段选择下拉 */
function FieldSelect({ value, onChange, options }: {
  value: string; onChange: (v: string) => void; options: SchemaOption[]
}) {
  return (
    <div className="flex-1 min-w-0 overflow-hidden">
      <select
        value={value}
        onChange={e => onChange(e.target.value)}
        className="w-full h-7 bg-elevated border border-border rounded text-xs text-foreground px-2 focus:outline-none focus:border-accent/50"
      >
      <option value="">不显示</option>
      {options.map(o => (
        <optgroup key={o.id} label={o.label}>
          {o.columns.map(col => (
            <option key={`${o.id}.${col.name}`} value={`${o.id}.${col.name}`}>
              {col.label || col.name}
            </option>
          ))}
        </optgroup>
      ))}
    </select>
    </div>
  )
}

/** 扩展字段配置区（概念/行业） */
function ExtFieldSection({ item, onChange, options }: {
  item: ExtFieldItem | undefined
  onChange: (item: ExtFieldItem | undefined) => void
  options: SchemaOption[]
}) {
  const field = item?.field ?? ''
  const cfg = item?.display
  const displayMode = cfg?.displayMode ?? 'tag'

  const updateDisplay = (patch: Partial<ExtColumnDisplayConfig>) => {
    onChange({ field, display: { displayMode: 'tag', ...cfg, ...patch } })
  }

  return (
    <div className="space-y-2.5">
      <div className="flex items-center gap-2">
        <span className="text-xs text-secondary shrink-0 w-16">选择字段</span>
        <FieldSelect value={field} onChange={v => onChange(v ? { field: v, display: { displayMode: 'tag' } } : undefined)} options={options} />
      </div>
      {!field ? null : (
        <>
          <div className="flex items-center gap-2">
            <span className="text-xs text-secondary shrink-0 w-16">显示模式</span>
            <div className="flex flex-1 min-w-0 rounded overflow-hidden border border-border">
              <button onClick={() => updateDisplay({ displayMode: 'tag' })} className={`flex-1 py-1 text-xs transition-colors ${displayMode === 'tag' ? 'bg-accent/15 text-accent' : 'bg-elevated text-secondary'}`}>标签</button>
              <button onClick={() => updateDisplay({ displayMode: 'text' })} className={`flex-1 py-1 text-xs transition-colors border-l border-border ${displayMode === 'text' ? 'bg-accent/15 text-accent' : 'bg-elevated text-secondary'}`}>文本</button>
            </div>
          </div>
          {displayMode === 'tag' && (
            <div>
              <div className="flex items-center gap-2">
                <span className="text-xs text-secondary shrink-0 w-16">分隔符</span>
                <input
                  type="text"
                  value={cfg?.separator ?? ''}
                  onChange={e => updateDisplay({ separator: e.target.value })}
                  placeholder="留空"
                  className="flex-1 min-w-0 h-7 bg-elevated border border-border rounded text-xs text-foreground px-2 placeholder:text-muted focus:outline-none focus:border-accent/50"
                />
              </div>
              <div className="text-[10px] text-muted mt-1" style={{ paddingLeft: 72 }}>
                留空自动识别：、 , ， ; ； -
              </div>
            </div>
          )}
          {displayMode === 'tag' && (
            <div className="flex items-center gap-2">
              <span className="text-xs text-secondary shrink-0 w-16">显示前N个</span>
              <input
                type="number" min={0}
                value={cfg?.maxTags ?? ''}
                onChange={e => {
                  const v = e.target.value ? Number(e.target.value) : undefined
                  updateDisplay({ maxTags: v, ...(v ? {} : { hiddenIndices: undefined }) })
                }}
                placeholder="不限制"
                className="flex-1 min-w-0 h-7 bg-elevated border border-border rounded text-xs text-foreground px-2 placeholder:text-muted focus:outline-none focus:border-accent/50"
              />
            </div>
          )}
          {displayMode === 'tag' && (cfg?.maxTags ?? 0) > 0 && (
            <div className="flex items-center gap-2">
              <span className="text-xs text-secondary shrink-0 w-16">显示位置</span>
              <div className="flex flex-wrap gap-1">
                {Array.from({ length: cfg!.maxTags! }, (_, i) => {
                  const hidden = cfg?.hiddenIndices?.includes(i)
                  return (
                    <button
                      key={i}
                      onClick={() => {
                        const cur = cfg?.hiddenIndices ?? []
                        const next = hidden ? cur.filter(x => x !== i) : [...cur, i]
                        updateDisplay({ hiddenIndices: next.length ? next : undefined })
                      }}
                      className={`w-6 h-6 rounded text-[10px] font-medium transition-colors ${hidden ? 'bg-elevated text-muted line-through' : 'bg-accent/15 text-accent'}`}
                    >{i + 1}</button>
                  )
                })}
              </div>
            </div>
          )}
          {displayMode === 'tag' && (
            <div className="flex items-center gap-2">
              <span className="text-xs text-secondary shrink-0 w-16">排列方向</span>
              <div className="flex flex-1 min-w-0 rounded overflow-hidden border border-border">
                <button onClick={() => updateDisplay({ tagLayout: 'horizontal' })} className={`flex-1 py-1 text-xs transition-colors ${(cfg?.tagLayout ?? 'horizontal') === 'horizontal' ? 'bg-accent/15 text-accent' : 'bg-elevated text-secondary'}`}>横</button>
                <button onClick={() => updateDisplay({ tagLayout: 'vertical' })} className={`flex-1 py-1 text-xs transition-colors border-l border-border ${cfg?.tagLayout === 'vertical' ? 'bg-accent/15 text-accent' : 'bg-elevated text-secondary'}`}>竖</button>
              </div>
            </div>
          )}
          <div className="flex justify-end">
            <button onClick={() => onChange({ field, display: { displayMode: 'tag' } })} className="text-[10px] text-muted hover:text-foreground">恢复默认</button>
          </div>
        </>
      )}
    </div>
  )
}

/** 炸板/断板配置区 */
function BrokenFailedSection({ bf, onChange }: {
  bf: BrokenFailedConfig
  onChange: (bf: BrokenFailedConfig) => void
}) {
  const update = (patch: Partial<BrokenFailedConfig>) => onChange({ ...bf, ...patch })

  return (
    <div className="space-y-3">
      {/* 炸板 */}
      <div className="space-y-2">
        <span className="text-[10px] font-semibold text-purple-400 uppercase tracking-wider">炸板</span>
        <Toggle label="显示炸板股票" checked={bf.brokenShow ?? true} onChange={v => update({ brokenShow: v })} />
        <Toggle label="计入炸板数量" checked={bf.brokenCount ?? true} onChange={v => update({ brokenCount: v })} />
        <NumInput label="最低板数（含）" value={bf.brokenMinBoards ?? 0} onChange={v => update({ brokenMinBoards: v ?? 0 })} min={0} max={50} placeholder="0=不限" />
        {(bf.brokenMinBoards ?? 0) > 0 && (
          <span className="text-[10px] text-muted pl-1">低于 {bf.brokenMinBoards} 板的炸板不显示也不计数</span>
        )}
      </div>
      <div className="h-px bg-border" />
      {/* 断板 */}
      <div className="space-y-2">
        <span className="text-[10px] font-semibold text-yellow-500 uppercase tracking-wider">断板</span>
        <Toggle label="显示断板股票" checked={bf.failedShow ?? true} onChange={v => update({ failedShow: v })} />
        <Toggle label="计入断板数量" checked={bf.failedCount ?? true} onChange={v => update({ failedCount: v })} />
        <NumInput label="最低板数（含）" value={bf.failedMinBoards ?? 0} onChange={v => update({ failedMinBoards: v ?? 0 })} min={0} max={50} placeholder="0=不限" />
        {(bf.failedMinBoards ?? 0) > 0 && (
          <span className="text-[10px] text-muted pl-1">低于 {bf.failedMinBoards} 板的断板不显示也不计数</span>
        )}
      </div>
    </div>
  )
}

function ExtConfigDialog({ fields, onSave, onClose }: {
  fields: ExtFieldConfig
  onSave: (f: ExtFieldConfig) => void
  onClose: () => void
}) {
  const [draft, setDraft] = useState(fields)
  const { data: schemaData } = useQuery({
    queryKey: QK.extDataSchemaAll,
    queryFn: api.extDataSchemaAll,
  })

  const options = useMemo((): SchemaOption[] => {
    if (!schemaData?.items) return []
    return schemaData.items.filter(item =>
      item.columns.some(c => c.name !== 'symbol' && c.name !== 'code' && c.name !== 'date')
    ).map(item => ({
      id: item.id,
      label: item.label,
      columns: item.columns.filter(c =>
        c.name !== 'symbol' && c.name !== 'code' && c.name !== 'date'
        && (!c.type || c.type === 'VARCHAR' || c.type === 'STRING' || c.type === 'TEXT' || c.type.toLowerCase().includes('char') || c.type.toLowerCase().includes('string'))
      ),
    })).filter(item => item.columns.length > 0)
  }, [schemaData])

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50" onClick={onClose}>
      <motion.div
        initial={{ opacity: 0, scale: 0.95 }}
        animate={{ opacity: 1, scale: 1 }}
        exit={{ opacity: 0, scale: 0.95 }}
        className="bg-surface border border-border rounded-lg shadow-xl max-w-[95vw] overflow-hidden"
        onClick={e => e.stopPropagation()}
      >
        {/* 头部 */}
        <div className="flex items-center justify-between px-4 pt-3 pb-1">
          <span className="text-sm font-medium">配置</span>
          <button onClick={onClose} className="p-0.5 text-muted hover:text-foreground"><X className="h-4 w-4" /></button>
        </div>
        {/* 三列平铺 */}
        <div className="flex gap-0 border-b border-border px-2 overflow-hidden">
          <div className="flex-1 min-w-0 p-3 border-r border-border" style={{ minWidth: 180 }}>
            <span className="text-[10px] font-semibold text-sky-400 uppercase tracking-wider mb-2 block">概念</span>
            <ExtFieldSection item={draft.concept} onChange={v => setDraft(d => ({ ...d, concept: v }))} options={options} />
            <div className="h-px bg-border my-3" />
            <Toggle label="显示概念分布统计" checked={draft.showConceptStats ?? true} onChange={v => setDraft(d => ({ ...d, showConceptStats: v }))} />
            <div className="h-px bg-border my-2" />
            <Toggle label="显示分组概念统计" checked={draft.showConceptGroupStats ?? false} onChange={v => setDraft(d => ({ ...d, showConceptGroupStats: v }))} />
          </div>
          <div className="flex-1 min-w-0 p-3 border-r border-border" style={{ minWidth: 180 }}>
            <span className="text-[10px] font-semibold text-blue-400 uppercase tracking-wider mb-2 block">行业</span>
            <ExtFieldSection item={draft.industry} onChange={v => setDraft(d => ({ ...d, industry: v }))} options={options} />
            <div className="h-px bg-border my-3" />
            <Toggle label="显示行业分布统计" checked={draft.showIndustryStats ?? true} onChange={v => setDraft(d => ({ ...d, showIndustryStats: v }))} />
            <div className="h-px bg-border my-2" />
            <Toggle label="显示分组行业统计" checked={draft.showIndustryGroupStats ?? false} onChange={v => setDraft(d => ({ ...d, showIndustryGroupStats: v }))} />
          </div>
          <div className="flex-1 min-w-0 p-3" style={{ minWidth: 160 }}>
            <span className="text-[10px] font-semibold text-muted uppercase tracking-wider mb-2 block">炸板/断板</span>
            <BrokenFailedSection bf={{ ...DEFAULT_BF, ...draft.bf }} onChange={v => setDraft(d => ({ ...d, bf: v }))} />
          </div>
        </div>
        {/* 底部按钮 */}
        <div className="flex justify-end gap-2 px-4 py-3">
          <button onClick={onClose} className="px-3 py-1.5 text-xs text-secondary hover:text-foreground">取消</button>
          <button
            onClick={() => { onSave(draft); onClose() }}
            className="px-3 py-1.5 text-xs bg-accent/15 text-accent rounded hover:bg-accent/25"
          >保存</button>
        </div>
      </motion.div>
    </div>
  )
}

// ===== 主页面 =====

export function LimitUpLadder() {
  const [asOf, setAsOf] = useState('')
  const [filterKeys, setFilterKeys] = useState<Set<FilterKey>>(loadFilterKeys)
  const [extFields, setExtFields] = useState<ExtFieldConfig>(loadExtFields)
  const [showExtConfig, setShowExtConfig] = useState(false)
  const [showConcept, setShowConcept] = useState(() => storage.limitLadderShowExt.get({ concept: true, industry: true }).concept)
  const [showIndustry, setShowIndustry] = useState(() => storage.limitLadderShowExt.get({ concept: true, industry: true }).industry)

  const toggleConcept = useCallback(() => {
    setShowConcept(prev => {
      const next = !prev
      storage.limitLadderShowExt.set({ concept: next, industry: showIndustry })
      return next
    })
  }, [showIndustry])
  const toggleIndustry = useCallback(() => {
    setShowIndustry(prev => {
      const next = !prev
      storage.limitLadderShowExt.set({ concept: showConcept, industry: next })
      return next
    })
  }, [showConcept])
  const [previewSymbol, setPreviewSymbol] = useState<string | null>(null)
  const [previewName, setPreviewName] = useState('')
  const [selectedTag, setSelectedTag] = useState<{ fieldKey: 'concept' | 'industry'; tag: string } | null>(null)
  const handleSelectTag = useCallback((sel: { fieldKey: 'concept' | 'industry'; tag: string } | null) => {
    setSelectedTag(prev => prev?.fieldKey === sel?.fieldKey && prev?.tag === sel?.tag ? null : sel)
  }, [])

  const toggleFilter = useCallback((key: FilterKey) => {
    setFilterKeys(prev => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      storage.limitLadderBoard.set([...next])
      return next
    })
  }, [])

  const handleSaveExtFields = useCallback((f: ExtFieldConfig) => {
    setExtFields(f)
    storage.limitLadderExtFields.set(f)
  }, [])

  const handleStockClick = (symbol: string, name?: string) => {
    setPreviewSymbol(symbol)
    setPreviewName(name ?? '')
  }

  const extColumnsParam = useMemo(() => buildExtColumnsParam(extFields), [extFields])

  const { data, isLoading, refetch, isFetching } = useQuery({
    queryKey: [QK.limitLadder(asOf || undefined), extColumnsParam],
    queryFn: () => api.limitLadder(asOf || undefined, extColumnsParam),
    staleTime: 5 * 60_000,
  })

  const rawTiers = data?.tiers ?? []
  const tiers = filterTiers(rawTiers, filterKeys, extFields.bf)
  const totalStocks = tiers.reduce((sum, t) => sum + t.stocks.filter(s => s.status === 'limit_up' || !s.status).length, 0)
  const displayDate = data?.as_of ?? asOf

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-full">
        <RefreshCw className="h-5 w-5 animate-spin text-muted" />
      </div>
    )
  }

  const dateValue = displayDate || new Date().toISOString().slice(0, 10)

  if (!data || rawTiers.length === 0) {
    return (
      <div className="flex flex-col h-full">
        <PageHeader title="连板梯队" />
        <EmptyState icon={Flame} title="暂无连板数据" hint="该日期无涨停股或 enriched 数据未就绪" />
      </div>
    )
  }

  return (
    <div className="flex flex-col h-full">
      <PageHeader
        title="连板梯队"
        subtitle={`${totalStocks}只`}
        right={
          <div className="flex items-center gap-1">
            {/* 状态组: 涨停/炸板/断板 */}
            {STATUS_TABS.map(tab => (
              <button
                key={tab.key}
                onClick={() => toggleFilter(tab.key)}
                className={`px-2 py-1 text-xs transition-colors ${
                  filterKeys.has(tab.key)
                    ? 'bg-accent/15 text-accent font-medium'
                    : 'text-secondary hover:text-foreground hover:bg-surface'
                }`}
              >
                {tab.label}
              </button>
            ))}

            <div className="w-px h-4 bg-border mx-1" />

            {/* 显示组: 概念/行业 */}
            <button
              onClick={toggleConcept}
              className={`px-2 py-1 text-xs transition-colors ${
                showConcept
                  ? 'bg-yellow-500/15 text-yellow-400 font-medium'
                  : 'text-secondary hover:text-foreground hover:bg-surface'
              }`}
            >
              概念
            </button>
            <button
              onClick={toggleIndustry}
              className={`px-2 py-1 text-xs transition-colors ${
                showIndustry
                  ? 'bg-blue-500/15 text-blue-400 font-medium'
                  : 'text-secondary hover:text-foreground hover:bg-surface'
              }`}
            >
              行业
            </button>

            <div className="w-px h-4 bg-border mx-1" />

            {/* 板块组 */}
            {BOARD_TABS.map(tab => (
              <button
                key={tab.key}
                onClick={() => toggleFilter(tab.key)}
                className={`px-2 py-1 text-xs transition-colors ${
                  filterKeys.has(tab.key)
                    ? 'bg-accent/15 text-accent font-medium'
                    : 'text-secondary hover:text-foreground hover:bg-surface'
                }`}
              >
                {tab.label}
              </button>
            ))}

            <div className="w-px h-4 bg-border mx-1" />
            <button
              onClick={() => setShowExtConfig(true)}
              className="p-1.5 hover:bg-surface text-muted hover:text-accent"
              title="配置"
            >
              <Settings2 className="h-3.5 w-3.5" />
            </button>
            <button
              onClick={() => refetch()}
              disabled={isFetching}
              className="p-1.5 hover:bg-surface text-muted disabled:opacity-50"
            >
              <RefreshCw className={`h-4 w-4 ${isFetching ? 'animate-spin' : ''}`} />
            </button>
          </div>
        }
      />

      {/* 总览条 + 日期 */}
      <OverviewBar tiers={tiers} dateValue={dateValue} onDateChange={setAsOf} filterKeys={filterKeys} bf={extFields.bf} />

      {/* 概念统计 */}
      {(extFields.showConceptStats ?? true) && (
        <TagStats
          title="概念分布"
          tiers={tiers}
          extFields={resolveExtFields(extFields, showConcept, showIndustry)}
          fieldKey="concept"
          color={{ text: [250, 204, 21], bg: [234, 179, 8] }}
          selectedTag={selectedTag}
          onSelect={handleSelectTag}
        />
      )}
      {/* 行业统计 */}
      {(extFields.showIndustryStats ?? true) && (
        <TagStats
          title="行业分布"
          tiers={tiers}
          extFields={resolveExtFields(extFields, showConcept, showIndustry)}
          fieldKey="industry"
          color={{ text: [96, 165, 250], bg: [59, 130, 246] }}
          selectedTag={selectedTag}
          onSelect={handleSelectTag}
        />
      )}

      {/* 梯队列表 */}
      <div className="flex-1 overflow-y-auto px-4 py-3 space-y-2">
        {tiers.map(t => (
          <TierGroup
            key={t.boards}
            tier={t}
            defaultOpen={t.boards >= 1 || t.count <= 8}
            extFields={resolveExtFields(extFields, showConcept, showIndustry)}
            filterKeys={filterKeys}
            bf={extFields.bf}
            onStockClick={handleStockClick}
            selectedTag={selectedTag}
            onSelectTag={handleSelectTag}
          />
        ))}
      </div>

      {/* 个股K线弹窗 */}
      <StockPreviewDialog
        symbol={previewSymbol}
        name={previewName}
        onClose={() => setPreviewSymbol(null)}
      />

      {/* 字段配置弹窗 */}
      <AnimatePresence>
        {showExtConfig && (
          <ExtConfigDialog
            fields={extFields}
            onSave={handleSaveExtFields}
            onClose={() => setShowExtConfig(false)}
          />
        )}
      </AnimatePresence>
    </div>
  )
}
