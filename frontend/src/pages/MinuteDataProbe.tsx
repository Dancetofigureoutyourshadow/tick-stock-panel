import { useState } from 'react'
import { Loader2, Search, AlertTriangle, CheckCircle2, XCircle } from 'lucide-react'
import { api } from '@/lib/api'

interface ProbeResult {
  date: string
  rows: number
  source: string
  ok: boolean
}

/**
 * 分钟K数据探测页（隐藏路由，不暴露在菜单）。
 * 用于排查"点击日K蜡烛加载分钟K为 0 条"类问题。
 * 直接访问: /minute-probe
 */
export function MinuteDataProbe() {
  const [symbol, setSymbol] = useState('603261.SH')
  const [days, setDays] = useState(10)
  const [loading, setLoading] = useState(false)
  const [results, setResults] = useState<ProbeResult[]>([])
  const [error, setError] = useState<string | null>(null)

  const runProbe = async () => {
    const sym = symbol.trim().toUpperCase()
    if (!sym) return
    setLoading(true)
    setError(null)
    setResults([])

    // 生成最近 N 天的日期（含非交易日，由后端返回行数判定）
    const dates: string[] = []
    const today = new Date()
    for (let i = 0; i < days; i++) {
      const d = new Date(today)
      d.setDate(d.getDate() - i)
      dates.push(d.toISOString().slice(0, 10))
    }

    const out: ProbeResult[] = []
    try {
      for (const date of dates) {
        const r = await api.klineMinute(sym, date)
        const rows = r.rows?.length ?? 0
        out.push({
          date,
          rows,
          source: r.source ?? (rows > 0 ? 'local' : 'none'),
          ok: rows > 0,
        })
        setResults([...out])
      }
    } catch (e: any) {
      setError(e?.message ?? String(e))
    } finally {
      setLoading(false)
    }
  }

  const total = results.length
  const hasData = results.filter((r) => r.ok).length
  const missing = results.filter((r) => !r.ok)

  return (
    <div className="min-h-screen bg-base p-6">
      <div className="mx-auto max-w-3xl space-y-6">
        <div>
          <h1 className="text-xl font-semibold text-foreground">分钟K数据探测</h1>
          <p className="mt-1 text-xs text-muted">
            逐日调用 <code className="px-1 rounded bg-elevated text-secondary">/api/kline/minute</code> 接口，
            检测每只股票最近若干天的分钟K数据是否齐全。本地无数据时会自动走 TickFlow 实时拉取。
          </p>
        </div>

        {/* 输入区 */}
        <div className="flex flex-wrap items-end gap-3 rounded-btn bg-elevated p-4">
          <div className="flex flex-col gap-1">
            <label className="text-xs text-muted">股票代码</label>
            <input
              value={symbol}
              onChange={(e) => setSymbol(e.target.value)}
              placeholder="603261.SH"
              className="w-44 rounded-btn border border-line bg-base px-3 py-1.5 text-sm text-foreground outline-none focus:border-accent"
              onKeyDown={(e) => e.key === 'Enter' && !loading && runProbe()}
            />
          </div>
          <div className="flex flex-col gap-1">
            <label className="text-xs text-muted">回溯天数</label>
            <input
              type="number"
              min={1}
              max={30}
              value={days}
              onChange={(e) => setDays(Math.max(1, Math.min(30, Number(e.target.value) || 1)))}
              className="w-24 rounded-btn border border-line bg-base px-3 py-1.5 text-sm text-foreground outline-none focus:border-accent"
            />
          </div>
          <button
            onClick={runProbe}
            disabled={loading || !symbol.trim()}
            className="flex items-center gap-1.5 rounded-btn bg-accent px-4 py-1.5 text-sm font-medium text-base hover:bg-accent/90 disabled:opacity-50"
          >
            {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Search className="h-4 w-4" />}
            {loading ? '探测中…' : '开始探测'}
          </button>
        </div>

        {error && (
          <div className="flex items-center gap-2 rounded-btn border border-danger/40 bg-danger/10 p-3 text-sm text-danger">
            <AlertTriangle className="h-4 w-4 shrink-0" />
            <span>{error}</span>
          </div>
        )}

        {/* 汇总 */}
        {total > 0 && (
          <div className="grid grid-cols-3 gap-3">
            <div className="rounded-btn bg-elevated p-3">
              <div className="text-xs text-muted">检测天数</div>
              <div className="mt-1 text-lg font-semibold text-foreground">{total}</div>
            </div>
            <div className="rounded-btn bg-elevated p-3">
              <div className="text-xs text-muted">有数据</div>
              <div className="mt-1 text-lg font-semibold text-success">{hasData}</div>
            </div>
            <div className="rounded-btn bg-elevated p-3">
              <div className="text-xs text-muted">缺失</div>
              <div className="mt-1 text-lg font-semibold text-danger">{missing.length}</div>
            </div>
          </div>
        )}

        {/* 明细表 */}
        {results.length > 0 && (
          <div className="overflow-hidden rounded-btn border border-line">
            <table className="w-full text-sm">
              <thead className="bg-elevated text-xs text-muted">
                <tr>
                  <th className="px-4 py-2 text-left font-medium">日期</th>
                  <th className="px-4 py-2 text-right font-medium">分钟K条数</th>
                  <th className="px-4 py-2 text-left font-medium">数据来源</th>
                  <th className="px-4 py-2 text-center font-medium">状态</th>
                </tr>
              </thead>
              <tbody>
                {results.map((r) => (
                  <tr key={r.date} className="border-t border-line/60">
                    <td className="px-4 py-2 text-foreground">{r.date}</td>
                    <td className="px-4 py-2 text-right tabular-nums text-foreground">{r.rows}</td>
                    <td className="px-4 py-2 text-secondary">
                      <span className="rounded bg-elevated px-1.5 py-0.5 text-xs">
                        {r.source}
                      </span>
                    </td>
                    <td className="px-4 py-2 text-center">
                      {r.ok ? (
                        <span className="inline-flex items-center gap-1 text-success">
                          <CheckCircle2 className="h-4 w-4" /> 有
                        </span>
                      ) : (
                        <span className="inline-flex items-center gap-1 text-danger">
                          <XCircle className="h-4 w-4" /> 缺失
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {missing.length > 0 && (
          <div className="rounded-btn border border-warning/40 bg-warning/10 p-3 text-xs text-foreground">
            <div className="mb-1 flex items-center gap-1.5 font-medium text-warning">
              <AlertTriangle className="h-4 w-4" /> 缺失日期的诊断
            </div>
            <p className="leading-relaxed text-secondary">
              缺失日期若为<span className="text-foreground">周末/节假日</span>属正常；
              若为<span className="text-foreground">停牌日</span>（成交量为 0）也属正常；
              若为<span className="text-foreground">正常交易日</span>（日K有成交量）却缺失分钟K，
              则是 TickFlow 数据源未提供该日分钟数据（常见于停牌后复牌首日，存在补数延迟）。
            </p>
          </div>
        )}
      </div>
    </div>
  )
}
