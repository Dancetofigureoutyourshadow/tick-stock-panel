import { useState } from 'react'
import { RefreshCw, Lock } from 'lucide-react'
import { PageHeader } from '@/components/PageHeader'
import { EmptyState } from '@/components/EmptyState'
import { useCapabilities } from '@/lib/useSharedQueries'
import { useFinancialStatus, useFinancialSync } from '@/lib/useFinancials'

const TABLE_LABELS: Record<string, string> = {
  metrics: '核心指标',
  income: '利润表',
  balance_sheet: '资产负债表',
  cash_flow: '现金流量表',
}

const METRIC_LABELS: Record<string, string> = {
  eps_basic: '基本EPS',
  eps_diluted: '稀释EPS',
  bps: '每股净资产',
  ocfps: '每股经营现金流',
  roe: 'ROE',
  roe_diluted: '稀释ROE',
  roa: 'ROA',
  gross_margin: '毛利率',
  net_margin: '净利率',
  debt_to_asset_ratio: '负债率',
  revenue_yoy: '营收增速',
  net_income_yoy: '净利增速',
  operating_cash_to_revenue: '经营现金/营收',
  inventory_turnover: '存货周转率',
}

export function Financials() {
  const { data: caps } = useCapabilities()
  const hasFinancial = caps?.capabilities?.['financial'] != null
  const { data: status, isLoading } = useFinancialStatus()
  const syncMut = useFinancialSync()
  const [syncing, setSyncing] = useState<string | null>(null)

  if (!hasFinancial) {
    return (
      <>
        <PageHeader title="财务" subtitle="利润表 / 资负表 / 现金流 / 关键指标" />
        <EmptyState
          icon={Lock}
          title="需要 Expert 套餐"
          hint="财务数据接口仅 Expert 套餐可用。升级后此页自动显示财务数据面板。"
        />
      </>
    )
  }

  const handleSync = async (table: string) => {
    setSyncing(table)
    try {
      await syncMut.mutateAsync(table)
    } finally {
      setSyncing(null)
    }
  }

  const tables = status?.tables ?? {}
  const available = status?.available ?? false

  return (
    <>
      <PageHeader
        title="财务"
        subtitle="利润表 / 资负表 / 现金流 / 关键指标 · Expert"
        right={
          <div className="flex gap-2">
            <button
              className="px-3 py-1.5 text-xs bg-card border border-border rounded-md hover:bg-accent transition-colors disabled:opacity-50"
              onClick={() => handleSync('all')}
              disabled={!!syncing}
            >
              <RefreshCw className={`inline w-3 h-3 mr-1 ${syncing ? 'animate-spin' : ''}`} />
              {syncing ? '同步中…' : '全部同步'}
            </button>
          </div>
        }
      />

      {!available || isLoading ? (
        <div className="p-5 text-sm text-muted">
          {isLoading ? '加载中…' : '暂无数据，点击"全部同步"从 TickFlow 拉取财务数据'}
        </div>
      ) : (
        <div className="p-5 space-y-6">
          {/* 各表状态卡片 */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            {Object.entries(TABLE_LABELS).map(([key, label]) => {
              const info = tables[key]
              return (
                <div key={key} className="bg-card border border-border rounded-lg p-3">
                  <div className="flex items-center justify-between">
                    <span className="text-sm font-medium">{label}</span>
                    <button
                      className="text-muted hover:text-foreground transition-colors disabled:opacity-50"
                      onClick={() => handleSync(key)}
                      disabled={!!syncing}
                      title={`同步${label}`}
                    >
                      <RefreshCw className={`w-3.5 h-3.5 ${syncing === key ? 'animate-spin' : ''}`} />
                    </button>
                  </div>
                  <div className="mt-2 text-2xl font-semibold tabular-nums">
                    {info?.rows ?? 0}
                    <span className="text-xs text-muted ml-1">行</span>
                  </div>
                  <div className="text-xs text-muted mt-0.5">
                    {info?.symbols ?? 0} 只标的
                  </div>
                </div>
              )
            })}
          </div>

          {/* 指标说明 */}
          <div className="bg-card border border-border rounded-lg p-4">
            <h3 className="text-sm font-medium mb-3">核心指标字段说明</h3>
            <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-x-6 gap-y-1.5 text-xs">
              {Object.entries(METRIC_LABELS).map(([key, label]) => (
                <div key={key} className="flex gap-2">
                  <code className="text-primary/80 font-mono">{key}</code>
                  <span className="text-muted">{label}</span>
                </div>
              ))}
            </div>
          </div>

          {/* 最后同步时间 */}
          {status?.last_sync && Object.keys(status.last_sync).length > 0 && (
            <div className="text-xs text-muted">
              最后同步: {Object.entries(status.last_sync).map(([k, v]) =>
                `${TABLE_LABELS[k] || k}: ${new Date(v).toLocaleString()}`
              ).join(' / ')}
            </div>
          )}
        </div>
      )}
    </>
  )
}
