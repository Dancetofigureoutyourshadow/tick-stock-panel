import { useState } from 'react'
import { Loader2, Search, RefreshCw, Check } from 'lucide-react'
import { api, type ExtDataConfig } from '@/lib/api'

export function ExtDataPullPanel({ config, onSaved }: {
  config: ExtDataConfig
  onSaved: () => void
}) {
  const pull = config.pull
  const [url, setUrl] = useState(pull?.url ?? '')
  const [method, setMethod] = useState(pull?.method ?? 'GET')
  const [headerStr, setHeaderStr] = useState(
    pull?.headers ? JSON.stringify(pull.headers, null, 2) : ''
  )
  const [body, setBody] = useState(pull?.body ?? '')
  const [responsePath, setResponsePath] = useState(pull?.response_path ?? '')
  const [fieldMapStr, setFieldMapStr] = useState(
    pull?.field_map ? JSON.stringify(pull.field_map, null, 2) : ''
  )
  const [schedule, setSchedule] = useState(pull?.schedule_minutes ?? 1440)
  const [enabled, setEnabled] = useState(pull?.enabled ?? false)
  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState(false)
  const [running, setRunning] = useState(false)
  const [testResult, setTestResult] = useState<{ total_rows: number; preview: Record<string, unknown>[]; has_symbol: boolean } | null>(null)
  const [error, setError] = useState('')

  const handleSave = () => {
    let headers: Record<string, string> | undefined
    if (headerStr.trim()) {
      try { headers = JSON.parse(headerStr) }
      catch { setError('Headers 不是有效 JSON'); return }
    }
    let field_map: Record<string, string> | undefined
    if (fieldMapStr.trim()) {
      try { field_map = JSON.parse(fieldMapStr) }
      catch { setError('字段映射不是有效 JSON'); return }
    }
    setSaving(true); setError('')
    api.extDataPullConfig(config.id, {
      url, method, headers, body: body || undefined,
      response_path: responsePath, field_map,
      schedule_minutes: schedule, enabled,
    }).then(() => onSaved())
      .catch(e => setError(e.message || '保存失败'))
      .finally(() => setSaving(false))
  }

  const handleTest = () => {
    setTesting(true); setError(''); setTestResult(null)
    let headers: Record<string, string> | undefined
    if (headerStr.trim()) {
      try { headers = JSON.parse(headerStr) }
      catch { setError('Headers 不是有效 JSON'); setTesting(false); return }
    }
    let field_map: Record<string, string> | undefined
    if (fieldMapStr.trim()) {
      try { field_map = JSON.parse(fieldMapStr) }
      catch { setError('字段映射不是有效 JSON'); setTesting(false); return }
    }
    api.extDataPullConfig(config.id, {
      url, method, headers, body: body || undefined,
      response_path: responsePath, field_map,
      schedule_minutes: schedule, enabled,
    }).then(() => api.extDataPullTest(config.id))
      .then(r => { setTestResult(r); onSaved() })
      .catch(e => setError(e.message || '测试失败'))
      .finally(() => setTesting(false))
  }

  const handleRun = () => {
    setRunning(true); setError('')
    api.extDataPullRun(config.id)
      .then(() => onSaved())
      .catch(e => setError(e.message || '执行失败'))
      .finally(() => setRunning(false))
  }

  return (
    <div className="space-y-2.5">
      <div className="flex gap-1.5">
        <select
          value={method} onChange={e => setMethod(e.target.value)}
          className="shrink-0 rounded-md border border-border bg-elevated px-2 py-1.5 text-[11px] text-foreground"
        >
          <option value="GET">GET</option>
          <option value="POST">POST</option>
        </select>
        <input
          value={url} onChange={e => setUrl(e.target.value)}
          placeholder="https://api.example.com/data"
          className="flex-1 min-w-0 rounded-md border border-border bg-elevated px-2.5 py-1.5 text-[11px] font-mono text-foreground placeholder:text-muted/50"
        />
      </div>

      <div>
        <div className="text-[10px] text-muted mb-0.5">Headers (JSON，可选)</div>
        <textarea
          value={headerStr} onChange={e => setHeaderStr(e.target.value)}
          placeholder='{"Authorization": "Bearer xxx"}'
          rows={2}
          className="w-full rounded-md border border-border bg-elevated px-2.5 py-1.5 text-[10px] font-mono text-foreground placeholder:text-muted/40 resize-none"
        />
      </div>

      {method === 'POST' && (
        <div>
          <div className="text-[10px] text-muted mb-0.5">请求体 (JSON，可选)</div>
          <textarea
            value={body} onChange={e => setBody(e.target.value)}
            placeholder='{"page": 1}'
            rows={2}
            className="w-full rounded-md border border-border bg-elevated px-2.5 py-1.5 text-[10px] font-mono text-foreground placeholder:text-muted/40 resize-none"
          />
        </div>
      )}

      <div className="grid grid-cols-2 gap-2">
        <div>
          <div className="text-[10px] text-muted mb-0.5">响应数据路径</div>
          <input
            value={responsePath} onChange={e => setResponsePath(e.target.value)}
            placeholder="data.list"
            className="w-full rounded-md border border-border bg-elevated px-2 py-1.5 text-[10px] font-mono text-foreground placeholder:text-muted/40"
          />
        </div>
        <div>
          <div className="text-[10px] text-muted mb-0.5">调度间隔 (分钟)</div>
          <input
            type="number" min={1} value={schedule} onChange={e => setSchedule(Number(e.target.value))}
            className="w-full rounded-md border border-border bg-elevated px-2 py-1.5 text-[10px] font-mono text-foreground"
          />
        </div>
      </div>

      <div>
        <div className="text-[10px] text-muted mb-0.5">字段映射 (外部名 → 内部名，JSON，可选)</div>
        <textarea
          value={fieldMapStr} onChange={e => setFieldMapStr(e.target.value)}
          placeholder='{"code": "symbol", "val": "score"}'
          rows={2}
          className="w-full rounded-md border border-border bg-elevated px-2.5 py-1.5 text-[10px] font-mono text-foreground placeholder:text-muted/40 resize-none"
        />
      </div>

      <div className="flex items-center justify-between">
        <label className="inline-flex items-center gap-1.5 cursor-pointer">
          <input
            type="checkbox" checked={enabled} onChange={e => setEnabled(e.target.checked)}
            className="rounded border-border accent-accent"
          />
          <span className="text-[10px] text-secondary">启用定时拉取</span>
        </label>
        <div className="flex items-center gap-1.5">
          <button
            onClick={handleTest}
            disabled={testing || !url}
            className="inline-flex items-center gap-1 px-2 py-1 rounded-btn border border-border bg-elevated text-[10px] text-foreground hover:bg-border/30 disabled:opacity-40 transition-colors"
          >
            {testing ? <Loader2 className="h-3 w-3 animate-spin" /> : <Search className="h-3 w-3" />}
            测试
          </button>
          <button
            onClick={handleRun}
            disabled={running || !url}
            className="inline-flex items-center gap-1 px-2 py-1 rounded-btn bg-accent/90 text-base text-[10px] font-medium hover:bg-accent disabled:opacity-40 transition-colors"
          >
            {running ? <Loader2 className="h-3 w-3 animate-spin" /> : <RefreshCw className="h-3 w-3" />}
            立即执行
          </button>
        </div>
      </div>

      {testResult && (
        <div className="rounded-md border border-accent/30 bg-accent/[0.04] p-2.5 space-y-1.5">
          <div className="flex items-center justify-between text-[10px]">
            <span className="text-accent font-medium">测试成功</span>
            <span className="text-secondary">{testResult.total_rows} 行</span>
          </div>
          {!testResult.has_symbol && (
            <div className="text-[10px] text-amber-500">数据缺少 symbol 字段，请配置字段映射</div>
          )}
          {testResult.preview.length > 0 && (
            <pre className="text-[9px] font-mono text-muted bg-elevated rounded px-2 py-1.5 overflow-x-auto max-h-32">
              {JSON.stringify(testResult.preview, null, 2)}
            </pre>
          )}
        </div>
      )}

      {pull?.last_run && (
        <div className="flex items-center justify-between text-[10px] border-t border-border/50 pt-2">
          <span className="text-muted">上次执行</span>
          <span className={pull.last_status === 'success' ? 'text-green-500' : 'text-danger'}>
            {pull.last_message}
          </span>
        </div>
      )}

      <button
        onClick={handleSave}
        disabled={saving || !url}
        className="w-full inline-flex items-center justify-center gap-1 py-1.5 rounded-btn bg-accent/90 text-base text-xs font-medium hover:bg-accent disabled:opacity-40 transition-colors"
      >
        {saving ? <Loader2 className="h-3 w-3 animate-spin" /> : <Check className="h-3 w-3" />}
        保存配置
      </button>

      {error && <div className="text-[10px] text-danger text-center">{error}</div>}
    </div>
  )
}
