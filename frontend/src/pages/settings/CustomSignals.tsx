import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Plus, Save, Trash2, X, Zap, ArrowRight, Settings2 } from 'lucide-react'
import { api, type CustomSignal, type CustomSignalCondition } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

const KIND_LABEL: Record<string, string> = { entry: '买入', exit: '卖出', both: '买卖通用' }

const emptySignal = (): CustomSignal => ({
  id: '', name: '', kind: 'exit', enabled: true,
  conditions: [{ left: 'close', op: '>', right: 'ma20' }],
})

export function SettingsCustomSignalsPanel() {
  const qc = useQueryClient()
  const list = useQuery({ queryKey: QK.customSignals, queryFn: api.customSignalsList })
  const options = useQuery({ queryKey: QK.customSignalsOptions, queryFn: api.customSignalsOptions })

  const [showForm, setShowForm] = useState(false)
  const [editing, setEditing] = useState<CustomSignal | null>(null)
  const [draft, setDraft] = useState<CustomSignal>(emptySignal())
  const [error, setError] = useState('')

  const fields = options.data?.fields ?? []
  const operators = options.data?.operators ?? ['>', '>=', '<', '<=', '==', '!=']

  const resetForm = () => {
    setEditing(null)
    setDraft(emptySignal())
    setError('')
  }

  const openNew = () => { resetForm(); setShowForm(true) }
  const openEdit = (sig: CustomSignal) => {
    setEditing(sig)
    setDraft({ ...sig, conditions: sig.conditions.map(c => ({ ...c })) })
    setError('')
    setShowForm(true)
  }

  const save = useMutation({
    mutationFn: () => {
      const d = draft
      if (!d.id.trim()) throw new Error('请输入信号标识')
      if (!/^[a-z0-9_]{1,40}$/.test(d.id)) throw new Error('标识仅允许小写字母、数字、下划线（1-40字符）')
      if (!d.name.trim()) throw new Error('请输入信号名称')
      if (d.conditions.length === 0) throw new Error('至少需要一个条件')
      for (const c of d.conditions) {
        if (!c.left || !c.op || c.right === '') throw new Error('条件填写不完整')
      }
      return api.customSignalSave(d)
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.customSignals })
      setShowForm(false)
      resetForm()
    },
    onError: err => setError(String((err as any)?.message ?? err)),
  })

  const del = useMutation({
    mutationFn: api.customSignalDelete,
    onSuccess: () => qc.invalidateQueries({ queryKey: QK.customSignals }),
  })

  // 条件编辑辅助
  const updateCond = (idx: number, patch: Partial<CustomSignalCondition>) => {
    setDraft(d => ({ ...d, conditions: d.conditions.map((c, i) => i === idx ? { ...c, ...patch } : c) }))
  }
  const addCond = () => setDraft(d => ({ ...d, conditions: [...d.conditions, { left: 'close', op: '>', right: '0' }] }))
  const removeCond = (idx: number) => setDraft(d => ({ ...d, conditions: d.conditions.filter((_, i) => i !== idx) }))

  const toggleEnabled = (sig: CustomSignal) => {
    api.customSignalSave({ ...sig, enabled: !sig.enabled }).then(() => qc.invalidateQueries({ queryKey: QK.customSignals }))
  }

  const signals = list.data?.signals ?? []

  return (
    <div className="max-w-5xl space-y-6">
      <section className="rounded-2xl border border-border bg-surface p-6 bg-[radial-gradient(circle_at_top_right,rgba(234,179,8,0.12),transparent_38%)]">
        <div className="flex flex-col gap-4 md:flex-row md:items-start md:justify-between">
          <div>
            <div className="text-[11px] uppercase tracking-[0.2em] text-amber-400/80">自定义信号</div>
            <h2 className="mt-2 text-2xl font-semibold tracking-tight text-foreground">用「字段 + 运算符 + 值」组合买卖信号</h2>
            <p className="mt-2 max-w-3xl text-sm leading-6 text-secondary">
              无需写代码，挑选已有指标字段组合条件（如 <span className="font-mono text-foreground/80">最低价 ≤ MA5</span>），即可在回测与监控中作为买卖信号使用。多条件间为「且」关系。
            </p>
          </div>
          <button
            onClick={openNew}
            className="inline-flex items-center justify-center gap-1.5 rounded-btn bg-amber-500/90 px-3 py-1.5 text-xs font-medium text-base hover:bg-amber-500 transition-colors"
          >
            <Plus className="h-3.5 w-3.5" />
            新建信号
          </button>
        </div>
      </section>

      {showForm && (
        <section className="rounded-card border border-border bg-surface p-5 space-y-4">
          <div className="flex items-center justify-between gap-3">
            <div>
              <h3 className="text-sm font-medium text-foreground">{editing ? '编辑信号' : '新建信号'}</h3>
              <p className="mt-1 text-[11px] text-muted">标识保存后不可修改，如需更换请新建。</p>
            </div>
            <button onClick={() => { setShowForm(false); setError('') }} className="rounded p-1 text-muted hover:bg-elevated hover:text-foreground">
              <X className="h-4 w-4" />
            </button>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
            <label className="space-y-1.5">
              <span className="text-[11px] text-muted">信号标识</span>
              <input
                value={draft.id}
                disabled={!!editing}
                onChange={e => setDraft(d => ({ ...d, id: e.target.value.replace(/[^a-z0-9_]/g, '') }))}
                placeholder="如 low_touches_ma5"
                className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs font-mono text-foreground disabled:opacity-60"
              />
            </label>
            <label className="space-y-1.5">
              <span className="text-[11px] text-muted">信号名称</span>
              <input value={draft.name} onChange={e => setDraft(d => ({ ...d, name: e.target.value }))} placeholder="如 跌至MA5" className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground" />
            </label>
            <label className="space-y-1.5">
              <span className="text-[11px] text-muted">类型</span>
              <select value={draft.kind} onChange={e => setDraft(d => ({ ...d, kind: e.target.value as CustomSignal['kind'] }))} className="h-9 w-full rounded-btn border border-border bg-base px-3 text-xs text-foreground">
                <option value="entry">买入</option>
                <option value="exit">卖出</option>
                <option value="both">买卖通用</option>
              </select>
            </label>
          </div>

          {/* 条件组 */}
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <span className="text-[11px] text-muted">条件（多条件为「且」关系）</span>
              <button onClick={addCond} className="inline-flex items-center gap-1 text-[11px] text-accent hover:text-accent/80 cursor-pointer">
                <Plus className="h-3 w-3" />添加条件
              </button>
            </div>
            {draft.conditions.map((c, i) => (
              <div key={i} className="flex items-center gap-1.5">
                <span className="text-[10px] text-muted/60 w-6 text-right shrink-0">{i === 0 ? '当' : '且'}</span>
                <select value={c.left} onChange={e => updateCond(i, { left: e.target.value })} className="w-32 h-7 px-1.5 rounded bg-base border border-border text-[11px] text-foreground focus:outline-none focus:border-accent/50">
                  {fields.map(f => <option key={f.key} value={f.key}>{f.label}</option>)}
                </select>
                <select value={c.op} onChange={e => updateCond(i, { op: e.target.value })} className="w-12 h-7 px-1 rounded bg-base border border-border text-[11px] font-mono text-foreground text-center focus:outline-none focus:border-accent/50">
                  {operators.map(op => <option key={op} value={op}>{op}</option>)}
                </select>
                <RightValueInput cond={c} fields={fields} onChange={v => updateCond(i, { right: v })} />
                {draft.conditions.length > 1 && (
                  <button onClick={() => removeCond(i)} className="p-1 rounded text-muted hover:text-danger hover:bg-danger/10 cursor-pointer">
                    <X className="h-3 w-3" />
                  </button>
                )}
              </div>
            ))}
          </div>

          {error && <div className="rounded-btn border border-danger/30 bg-danger/5 px-3 py-2 text-xs text-danger">{error}</div>}

          <div className="flex justify-end gap-2">
            <button onClick={() => { setShowForm(false); setError('') }} className="px-4 py-1.5 rounded-btn bg-elevated text-secondary text-xs">取消</button>
            <button onClick={() => save.mutate()} disabled={save.isPending} className="inline-flex items-center gap-1.5 px-4 py-1.5 rounded-btn bg-amber-500/90 text-base text-xs font-medium disabled:opacity-50">
              <Save className="h-3.5 w-3.5" />保存
            </button>
          </div>
        </section>
      )}

      <section className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {signals.map(sig => (
          <div key={sig.id} className="rounded-card border border-border bg-surface p-4">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <h3 className="text-sm font-medium text-foreground truncate">{sig.name}</h3>
                  <span className={`rounded px-1.5 py-0.5 text-[10px] ${sig.kind === 'entry' ? 'bg-accent/10 text-accent' : sig.kind === 'exit' ? 'bg-warning/10 text-warning' : 'bg-muted/10 text-muted'}`}>
                    {KIND_LABEL[sig.kind]}
                  </span>
                  {!sig.enabled && <span className="rounded bg-muted/10 px-1.5 py-0.5 text-[10px] text-muted">已停用</span>}
                </div>
                <p className="mt-1 text-[11px] text-muted font-mono truncate">{sig.id}</p>
              </div>
              <div className="flex items-center gap-1 shrink-0">
                <button onClick={() => toggleEnabled(sig)} title={sig.enabled ? '停用' : '启用'} className={`p-1 rounded cursor-pointer ${sig.enabled ? 'text-emerald-400 hover:bg-emerald-400/10' : 'text-muted hover:bg-elevated'}`}>
                  <Zap className="h-3.5 w-3.5" />
                </button>
                <button onClick={() => openEdit(sig)} className="p-1 rounded text-muted hover:text-accent hover:bg-accent/10 cursor-pointer" title="编辑">
                  <Settings2 className="h-3.5 w-3.5" />
                </button>
                <button onClick={() => del.mutate(sig.id)} disabled={del.isPending} className="p-1 rounded text-muted hover:text-danger hover:bg-danger/10 cursor-pointer" title="删除">
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
              </div>
            </div>
            <div className="mt-3 space-y-1">
              {sig.conditions.map((c, i) => (
                <div key={i} className="flex items-center gap-1.5 text-[11px] text-secondary">
                  <span className="text-muted/50 w-6 text-right">{i === 0 ? '当' : '且'}</span>
                  <span className="font-mono text-foreground/80">{fieldLabel(c.left, fields)}</span>
                  <span className="font-mono text-muted">{c.op}</span>
                  <span className="font-mono text-foreground/80">{rightDisplay(c.right, fields)}</span>
                </div>
              ))}
            </div>
          </div>
        ))}
        {signals.length === 0 && (
          <div className="rounded-card border border-border bg-surface px-5 py-10 text-center text-sm text-muted md:col-span-2">
            暂无自定义信号，点击右上角「新建信号」。
          </div>
        )}
      </section>
    </div>
  )
}

// ── 右值输入：可填数字，也可选「字段引用」────────────────
function RightValueInput({ cond, fields, onChange }: { cond: CustomSignalCondition; fields: { key: string; label: string }[]; onChange: (v: string) => void }) {
  const isField = cond.right.startsWith('field:')
  const fieldValue = isField ? cond.right.slice(6) : ''
  const numValue = isField ? '' : cond.right

  return (
    <div className="flex items-center gap-1">
      {isField ? (
        <>
          <select value={fieldValue} onChange={e => onChange(`field:${e.target.value}`)} className="w-32 h-7 px-1.5 rounded bg-base border border-border text-[11px] text-foreground focus:outline-none focus:border-accent/50">
            {fields.map(f => <option key={f.key} value={f.key}>{f.label}</option>)}
          </select>
          <button onClick={() => onChange('0')} title="切换为数字" className="p-0.5 rounded text-muted hover:text-accent cursor-pointer">
            <ArrowRight className="h-3 w-3 rotate-90" />
          </button>
        </>
      ) : (
        <>
          <input type="number" value={numValue} onChange={e => onChange(e.target.value)} step="any" className="w-24 h-7 px-1.5 rounded bg-base border border-border text-[11px] font-mono text-foreground text-center focus:outline-none focus:border-accent/50" />
          <button onClick={() => onChange('field:close')} title="切换为字段" className="p-0.5 rounded text-muted hover:text-accent cursor-pointer">
            <ArrowRight className="h-3 w-3 -rotate-90" />
          </button>
        </>
      )}
    </div>
  )
}

function fieldLabel(key: string, fields: { key: string; label: string }[]): string {
  return fields.find(f => f.key === key)?.label ?? key
}

function rightDisplay(right: string, fields: { key: string; label: string }[]): string {
  if (right.startsWith('field:')) return fieldLabel(right.slice(6), fields)
  return right
}
