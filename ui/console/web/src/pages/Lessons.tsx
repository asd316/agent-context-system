import { PlusIcon, SearchIcon } from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { toast } from 'sonner'
import { IdChip } from '@/components/IdChip'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { api, type Lesson, type LessonFields, type LessonStatus } from '@/lib/api'
import { writeFailed } from '@/lib/format'
import { useLoad, useStatus } from '@/lib/status'
import { cn } from '@/lib/utils'

const STATUS_LABEL: Record<LessonStatus, string> = { active: '有效', merged: '已并入 SOP', obsolete: '已过时' }
const SECTIONS: { key: 'situation' | 'problem' | 'fix'; label: string }[] = [
  { key: 'situation', label: '情境' }, { key: 'problem', label: '问题' }, { key: 'fix', label: '正确做法' },
]
const EMPTY: LessonFields = { topic: '', client: 'claude-code', situation: '', problem: '', fix: '' }

type Mode = { kind: 'view' } | { kind: 'edit' } | { kind: 'new' }

export default function Lessons() {
  const load = useCallback(() => api.lessons(), [])
  const { data, error, reload } = useLoad(load)
  const status = useStatus()
  const [query, setQuery] = useState('')
  const [showAll, setShowAll] = useState(false)
  const [selected, setSelected] = useState<string | null>(null)
  const [mode, setMode] = useState<Mode>({ kind: 'view' })

  const list = useMemo(() => {
    const q = query.trim().toLowerCase()
    return (data ?? []).filter((l) => (showAll || l.status === 'active') &&
      (!q || [l.id, l.topic, l.client, l.situation, l.problem, l.fix].some((t) => t.toLowerCase().includes(q))))
  }, [data, query, showAll])

  useEffect(() => {
    if (mode.kind === 'new' || !data) return
    if (!selected || !list.some((l) => l.id === selected)) setSelected(list[0]?.id ?? null)
  }, [list, data, selected, mode.kind])

  if (error) return <div className="grid h-full place-items-center font-mono text-xs text-muted-foreground">读取失败 · {error}</div>
  if (!data) return <div className="grid h-full place-items-center font-mono text-xs text-muted-foreground">…</div>
  const current = data.find((l) => l.id === selected) ?? null

  const saved = async (l: Lesson, what: string) => {
    toast.success(`${what} ${l.id} · 应用后各 Client 才能读到`)
    await reload()  // 先取到新列表再选中,否则选中项不在旧列表里会被重置
    setQuery('')
    setSelected(l.id)
    setMode({ kind: 'view' })
    await status.refresh()
  }

  return (
    <div className="flex h-full">
      <aside className="flex w-[360px] shrink-0 flex-col border-r border-border bg-card">
        <div className="flex items-center gap-2 border-b border-border p-3">
          <div className="relative flex-1">
            <SearchIcon className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground" />
            <Input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="搜主题、正文、Client" aria-label="搜索 Lesson" className="pl-8" />
          </div>
          <Button size="sm" disabled={status.locked} onClick={() => { setMode({ kind: 'new' }); setSelected(null) }}>
            <PlusIcon /> 新建
          </Button>
        </div>
        <label className="flex items-center gap-2 border-b border-border px-3 py-2 text-xs text-muted-foreground">
          <input type="checkbox" checked={showAll} onChange={(e) => setShowAll(e.target.checked)} className="accent-[var(--primary)]" />
          含已并入与已过时 · {list.length} / {data.length}
        </label>
        <div className="min-h-0 flex-1 overflow-auto p-2">
          {list.map((l) => (
            <button
              key={l.id}
              onClick={() => { setSelected(l.id); setMode({ kind: 'view' }) }}
              className={cn('mb-1 w-full rounded-lg px-3 py-2.5 text-left transition-colors', selected === l.id && mode.kind !== 'new' ? 'bg-secondary' : 'hover:bg-secondary/50', l.status !== 'active' && 'opacity-60')}
            >
              <div className="truncate text-[13px]">{l.topic}</div>
              <div className="mt-0.5 flex gap-2 font-mono text-[11px] text-muted-foreground">
                <span>{l.date}</span><span>{l.client}</span>
                {l.host && <span className="text-primary/80">仅 {l.host}</span>}
                {l.status !== 'active' && <span className="ml-auto">{STATUS_LABEL[l.status]}</span>}
              </div>
            </button>
          ))}
          {list.length === 0 && <p className="p-3 text-xs text-muted-foreground">没有匹配的 Lesson</p>}
        </div>
      </aside>
      <section className="min-w-0 flex-1 overflow-auto p-6">
        {status.locked && <div className="mb-4 rounded-md bg-secondary px-3 py-2 text-xs text-muted-foreground">正在应用 · 暂时不能编辑 · 应用结束后自动恢复</div>}
        {mode.kind === 'new' && (
          <LessonForm
            initial={EMPTY}
            creating
            disabled={status.locked}
            onCancel={() => setMode({ kind: 'view' })}
            onSave={async (f, hostOnly) => saved(await api.createLesson({ ...f, host_only: hostOnly }), '已新建')}
          />
        )}
        {mode.kind === 'edit' && current && (
          <LessonForm
            key={current.id}
            initial={current}
            status={current.status}
            disabled={status.locked}
            onCancel={() => setMode({ kind: 'view' })}
            onSave={async (f, _h, st) => saved(await api.updateLesson(current.id, { ...f, status: st }), '已保存')}
          />
        )}
        {mode.kind === 'view' && current && <LessonView lesson={current} locked={status.locked} onEdit={() => setMode({ kind: 'edit' })} />}
        {mode.kind === 'view' && !current && <p className="text-sm text-muted-foreground">选一条 Lesson,或点「新建」。</p>}
      </section>
    </div>
  )
}

function LessonView({ lesson, locked, onEdit }: { lesson: Lesson; locked: boolean; onEdit: () => void }) {
  return (
    <article className="max-w-3xl">
      <div className="flex items-center gap-2">
        <IdChip id={lesson.id} />
        <span className="font-mono text-[11px] text-muted-foreground">{lesson.date} · {lesson.client}{lesson.host ? ` · 仅 ${lesson.host}` : ''}</span>
        <span className={cn('rounded border px-1.5 text-[11px]', lesson.status === 'active' ? 'border-primary/40 text-primary' : 'border-border text-muted-foreground')}>
          {STATUS_LABEL[lesson.status]}{lesson.merged_into ? ` → ${lesson.merged_into}` : ''}
        </span>
        <Button size="sm" variant="outline" className="ml-auto" disabled={locked} onClick={onEdit}>编辑</Button>
      </div>
      <h1 className="mt-3 text-xl font-semibold">{lesson.topic}</h1>
      {SECTIONS.map((s) => (
        <div key={s.key} className="mt-5">
          <div className="eyebrow mb-1.5">{s.label}</div>
          <p className="text-[14px] leading-relaxed whitespace-pre-wrap">{lesson[s.key]}</p>
        </div>
      ))}
      <p className="mt-6 font-mono text-[11px] text-muted-foreground">{lesson.path}</p>
    </article>
  )
}

function LessonForm({ initial, status, creating, disabled, onSave, onCancel }: {
  initial: LessonFields
  status?: LessonStatus
  creating?: boolean
  disabled: boolean
  onSave: (f: LessonFields, hostOnly: boolean, status?: LessonStatus) => Promise<void>
  onCancel: () => void
}) {
  const [f, setF] = useState<LessonFields>({ topic: initial.topic, client: initial.client, situation: initial.situation, problem: initial.problem, fix: initial.fix })
  const [hostOnly, setHostOnly] = useState(false)
  const [st, setSt] = useState<LessonStatus | undefined>(status)
  const [busy, setBusy] = useState(false)
  const set = (k: keyof LessonFields) => (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => setF({ ...f, [k]: e.target.value })
  const missing = (Object.keys(f) as (keyof LessonFields)[]).filter((k) => !f[k].trim())

  const submit = async () => {
    setBusy(true)
    try { await onSave(f, hostOnly, st) } catch (e) { writeFailed(creating ? '新建' : '保存', e) } finally { setBusy(false) }
  }

  return (
    <form className="max-w-3xl space-y-4" onSubmit={(e) => { e.preventDefault(); submit() }}>
      <h1 className="text-lg font-semibold">{creating ? '新建 Lesson' : '编辑 Lesson'}</h1>
      <div className="grid grid-cols-[1fr_200px] gap-3">
        <Field label="主题 · 会进入文件名,不能含 /"><Input value={f.topic} onChange={set('topic')} aria-label="主题" /></Field>
        <Field label="Client"><Input value={f.client} onChange={set('client')} aria-label="Client" /></Field>
      </div>
      {SECTIONS.map((s) => (
        <Field key={s.key} label={s.label}><Textarea value={f[s.key]} onChange={set(s.key)} aria-label={s.label} className="min-h-24" /></Field>
      ))}
      {creating ? (
        <label className="flex items-center gap-2 text-xs text-muted-foreground">
          <input type="checkbox" checked={hostOnly} onChange={(e) => setHostOnly(e.target.checked)} className="accent-[var(--primary)]" />
          只在本机成立 (host 记为本机 Host id)
        </label>
      ) : (
        <Field label="状态">
          <div className="flex gap-1">
            {(['active', 'obsolete'] as const).map((k) => (
              <button
                key={k}
                type="button"
                aria-pressed={st === k}
                onClick={() => setSt(k)}
                className={cn('rounded px-2.5 py-1 text-[13px]', st === k ? 'bg-primary/15 text-primary' : 'text-muted-foreground hover:text-foreground')}
              >
                {STATUS_LABEL[k]}
              </button>
            ))}
            {st === 'merged' && <span className="px-2.5 py-1 text-[13px] text-muted-foreground">已并入 SOP · 由维护 agent 改</span>}
          </div>
        </Field>
      )}
      <div className="flex items-center gap-2">
        <Button type="submit" disabled={disabled || busy || missing.length > 0}>{creating ? '新建' : '保存'}</Button>
        <Button type="button" variant="ghost" onClick={onCancel}>取消</Button>
        {missing.length > 0 && <span className="text-xs text-muted-foreground">还有 {missing.length} 项没填</span>}
      </div>
    </form>
  )
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="eyebrow mb-1.5 block">{label}</span>
      {children}
    </label>
  )
}
