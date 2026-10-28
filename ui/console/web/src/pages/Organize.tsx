import { FolderPlusIcon, SparklesIcon, XIcon } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { api, type CatalogEntry, type MemberKind, type Organize as OrganizeData, type Tier } from '@/lib/api'
import { IdChip } from '@/components/IdChip'
import { formatTime, writeFailed } from '@/lib/format'
import { useLoad, useStatus } from '@/lib/status'
import { cn } from '@/lib/utils'

const UNCATEGORIZED = '__uncategorized'
const VIEWED_KEY = 'organize.viewed'
const KIND_LABEL: Record<MemberKind, string> = { skill: 'skill', sop: 'SOP', lesson: 'Lesson' }
// 数据色: 只用在成员类型标记上
const KIND_COLOR: Record<MemberKind, string> = { skill: '#ff7a59', sop: '#4fd1b8', lesson: '#a08cff' }
const TIER_LABEL: Record<Tier, string> = { core: 'Core', router: 'Router', leaf: 'Leaf' }

interface Row {
  ref: string
  kind: MemberKind
  trait: string
  entry: CatalogEntry | null
  categories: string[]
  /** 这一行所在的 Category (拖动时的来源);未分类为 null */
  from: string | null
  recentKey: string | null
}

function loadViewed(): Set<string> {
  try { return new Set(JSON.parse(localStorage.getItem(VIEWED_KEY) ?? '[]')) } catch { return new Set() }
}
function saveViewed(s: Set<string>) {
  try { localStorage.setItem(VIEWED_KEY, JSON.stringify([...s])) } catch { /* 无痕模式等: 只是不记住 */ }
}

const kindOf = (ref: string): MemberKind => (ref.startsWith('sop:') ? 'sop' : ref.startsWith('lesson:') ? 'lesson' : 'skill')

export default function Organize({ find }: { find: string | null }) {
  const load = useCallback(() => api.organize(), [])
  const { data, error, reload } = useLoad(load)
  const status = useStatus()
  const [selected, setSelected] = useState<string | null>(null)
  const [recentOnly, setRecentOnly] = useState(false)
  const [viewed, setViewed] = useState(loadViewed)
  const [highlight, setHighlight] = useState<string | null>(null)

  const byId = useMemo(() => new Map(data?.categories.map((c) => [c.id, c]) ?? []), [data])
  const memberOf = useMemo(() => {
    const m = new Map<string, string[]>()
    for (const c of data?.categories ?? []) for (const ref of c.members) m.set(ref, [...(m.get(ref) ?? []), c.id])
    return m
  }, [data])
  const recentByMember = useMemo(() => {
    const m = new Map<string, string>()
    for (const r of data?.recent ?? []) {
      const key = `${r.member}@${r.category}@${r.commit}`
      if (!viewed.has(key) && !m.has(r.member)) m.set(r.member, key)
    }
    return m
  }, [data, viewed])

  useEffect(() => {
    if (!data || selected) return
    if (find) {
      const cat = data.categories.find((c) => c.name === find || c.id === find)
      const holder = data.categories.find((c) => c.members.includes(find))
      if (cat || holder) {
        setSelected((cat ?? holder)!.id)
        if (!cat) setHighlight(find)
        return
      }
      if (data.uncategorized.includes(find)) { setSelected(UNCATEGORIZED); setHighlight(find); return }
    }
    setSelected(data.categories[0]?.id ?? UNCATEGORIZED)
  }, [data, find, selected])

  const markViewed = useCallback((key: string | null) => {
    if (!key) return
    setViewed((v) => {
      if (v.has(key)) return v
      const next = new Set(v).add(key)
      saveViewed(next)
      return next
    })
  }, [])

  const after = useCallback(async () => { await reload(); await status.refresh() }, [reload, status])
  const drop = useCallback(async (member: string, from: string | null, to: string, copy: boolean) => {
    markViewed(recentByMember.get(member) ?? null)
    try {
      const op = copy || !from ? 'copy' : 'move'
      await api.membership(op, member, from ?? undefined, to)
      toast.success(`${member} ${op === 'copy' ? '已复制到' : '已移动到'} ${byId.get(to)?.name ?? to}`)
    } catch (e) { writeFailed('移动', e) }
    after()
  }, [byId, after, markViewed, recentByMember])
  const { drag, start } = useMemberDrag(status.locked, drop)

  if (error) return <Center>{`读取失败 · ${error}`}</Center>
  if (!data) return <Center>…</Center>

  const row = (ref: string, from: string | null, trait = ''): Row => ({
    ref, kind: kindOf(ref), trait, entry: kindOf(ref) === 'skill' ? data.entries[ref] ?? null : null,
    categories: memberOf.get(ref) ?? [], from, recentKey: recentByMember.get(ref) ?? null,
  })
  let rows: Row[]
  if (recentOnly) {
    const seen = new Set<string>()
    rows = data.recent.filter((r) => {
      const key = `${r.member}@${r.category}@${r.commit}`
      if (viewed.has(key) || seen.has(r.member)) return false
      seen.add(r.member)
      return true
    }).map((r) => row(r.member, r.category, byId.get(r.category)?.member_details.find((d) => d.ref === r.member)?.trait))
  } else if (selected === UNCATEGORIZED) {
    rows = data.uncategorized.map((n) => row(n, null))
  } else {
    const c = selected ? byId.get(selected) : undefined
    rows = c ? c.member_details.map((d) => row(d.ref, c.id, d.trait)) : []
  }
  const current = selected && selected !== UNCATEGORIZED ? byId.get(selected) ?? null : null

  return (
    <div className="flex h-full">
      <CategoryTree
        data={data}
        selected={recentOnly ? null : selected}
        locked={status.locked}
        onSelect={(id) => { setSelected(id); setRecentOnly(false); setHighlight(null) }}
        over={drag?.target ?? null}
        onCreate={async (id, name) => {
          await api.createCategory(id, name)
          await after()
          setSelected(id)
          setRecentOnly(false)
        }}
      />
      <section className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center gap-3 border-b border-border px-5 py-3">
          {recentOnly ? (
            <h1 className="text-[15px] font-semibold">新归类的成员</h1>
          ) : current ? (
            <>
              <h1 className="text-[15px] font-semibold">{current.name}</h1>
              <IdChip id={current.id} />
              {current.router && <span className="text-xs text-muted-foreground">Router <IdChip id={current.router} /></span>}
            </>
          ) : (
            <h1 className="text-[15px] font-semibold">未分类</h1>
          )}
          <span className="font-mono text-xs text-muted-foreground">{rows.length}</span>
          <button
            onClick={() => setRecentOnly((v) => !v)}
            aria-pressed={recentOnly}
            className={cn(
              'ml-auto flex h-7 items-center gap-1.5 rounded-full border px-3 text-xs transition-colors',
              recentOnly ? 'border-primary/60 bg-primary/10 text-primary' : 'border-border text-muted-foreground hover:text-foreground',
            )}
          >
            <SparklesIcon className="size-3.5" />
            只看新归类的 · <span className="font-mono">{recentByMember.size}</span>
          </button>
        </header>
        {status.locked && <div className="border-b border-border bg-secondary px-5 py-2 text-xs text-muted-foreground">正在应用 · 暂时不能修改 · 应用结束后自动恢复</div>}
        <div className="min-h-0 flex-1 overflow-auto">
          {rows.length === 0 ? (
            <EmptyState recentOnly={recentOnly} category={current} hasChildren={!!current && data.categories.some((c) => c.id.startsWith(`${current.id}/`))} locked={status.locked} onDeleted={async (id) => { setSelected(null); await after(); toast.success(`已删除 Category ${id}`) }} />
          ) : (
            <MemberTable
              rows={rows}
              data={data}
              locked={status.locked}
              highlight={highlight}
              uncategorized={selected === UNCATEGORIZED && !recentOnly}
              onTouch={markViewed}
              onDragStart={start}
              after={after}
            />
          )}
        </div>
        {drag && <DragHint drag={drag} names={new Map(data.categories.map((c) => [c.id, c.name]))} />}
        <footer className="border-t border-border px-5 py-2 text-xs text-muted-foreground">
          拖一行到左侧 Category: 默认移动,按住 ⌥ 复制。改动即时写入,点右上角「应用」生效。
        </footer>
      </section>
    </div>
  )
}

function Center({ children }: { children: React.ReactNode }) {
  return <div className="grid h-full place-items-center font-mono text-xs text-muted-foreground">{children}</div>
}

// ---- 树 ----

// count 含子 Category 的成员 (同一成员在多处时重复计)
interface TreeNode { id: string; name: string; router: string | null; count: number; children: TreeNode[] }

function buildTree(data: OrganizeData): TreeNode[] {
  const nodes = new Map<string, TreeNode>()
  for (const c of data.categories) nodes.set(c.id, { id: c.id, name: c.name, router: c.router, count: c.members.length, children: [] })
  const roots: TreeNode[] = []
  for (const c of data.categories) {
    const parent = c.id.includes('/') ? nodes.get(c.id.slice(0, c.id.lastIndexOf('/'))) : undefined
    ;(parent ? parent.children : roots).push(nodes.get(c.id)!)
  }
  const total = (n: TreeNode): number => (n.count += n.children.reduce((k, c) => k + total(c), 0))
  roots.forEach(total)
  return roots
}

interface DragState { member: string; from: string | null; x: number; y: number; target: string | null; copy: boolean; active: boolean }

/** 成员拖到树节点上: 用 pointer 事件实现,光标旁始终显示将要发生的动作;⌥ 在拖动中途按下或松开也立即生效 */
function useMemberDrag(locked: boolean, onDrop: (member: string, from: string | null, to: string, copy: boolean) => void) {
  const [drag, setDrag] = useState<DragState | null>(null)
  const ref = useRef<DragState | null>(null)
  const set = (d: DragState | null) => { ref.current = d; setDrag(d) }

  useEffect(() => {
    if (!drag) return
    const targetAt = (x: number, y: number) =>
      (document.elementFromPoint(x, y)?.closest('[data-category]') as HTMLElement | null)?.dataset.category ?? null
    const move = (e: PointerEvent) => {
      const d = ref.current!
      const active = d.active || Math.hypot(e.clientX - d.x, e.clientY - d.y) > 4
      set({ ...d, x: e.clientX, y: e.clientY, active, target: active ? targetAt(e.clientX, e.clientY) : null, copy: e.altKey })
    }
    const up = () => {
      const d = ref.current!
      set(null)
      document.body.style.cursor = ''
      if (d.active && d.target && d.target !== d.from) onDrop(d.member, d.from, d.target, d.copy)
    }
    const key = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { set(null); document.body.style.cursor = '' }
      else if (e.key === 'Alt') set({ ...ref.current!, copy: e.type === 'keydown' })
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
    window.addEventListener('keydown', key)
    window.addEventListener('keyup', key)
    return () => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
      window.removeEventListener('keydown', key)
      window.removeEventListener('keyup', key)
    }
  }, [drag !== null, onDrop]) // eslint-disable-line react-hooks/exhaustive-deps

  const start = (member: string, from: string | null) => (e: React.PointerEvent) => {
    if (locked || e.button !== 0 || (e.target as HTMLElement).closest('button, input, select, a')) return
    e.preventDefault()
    set({ member, from, x: e.clientX, y: e.clientY, target: null, copy: e.altKey, active: false })
  }
  return { drag: drag?.active ? drag : null, start }
}

function DragHint({ drag, names }: { drag: DragState; names: Map<string, string> }) {
  const to = drag.target
  const same = to !== null && to === drag.from
  const copy = drag.copy || !drag.from
  const text = !to ? (copy ? '复制到…' : '移动到…') : same ? `已在 ${names.get(to)} 中` : `${copy ? '复制到' : '移动到'} ${names.get(to)}`
  useEffect(() => { document.body.style.cursor = to && !same ? (copy ? 'copy' : 'grabbing') : 'no-drop' }, [to, same, copy])
  return (
    <div
      role="status"
      className={cn('pointer-events-none fixed z-50 rounded-md px-2 py-1 text-xs shadow-lg', to && !same ? 'bg-primary text-primary-foreground' : 'bg-secondary text-muted-foreground')}
      style={{ left: drag.x + 14, top: drag.y + 14 }}
    >
      <span className="font-mono">{drag.member}</span> · {text}
    </div>
  )
}

function CategoryTree({ data, selected, locked, over, onSelect, onCreate }: {
  data: OrganizeData
  selected: string | null
  locked: boolean
  over: string | null
  onSelect: (id: string) => void
  onCreate: (id: string, name: string) => Promise<void>
}) {
  const roots = useMemo(() => buildTree(data), [data])
  const [menu, setMenu] = useState<{ x: number; y: number; id: string | null } | null>(null)
  const [naming, setNaming] = useState<string | null | undefined>(undefined) // undefined: 不在命名;null: 顶层

  useEffect(() => {
    const close = () => setMenu(null)
    window.addEventListener('click', close)
    return () => window.removeEventListener('click', close)
  }, [])

  const render = (n: TreeNode, depth: number): React.ReactNode => (
    <div key={n.id}>
      <div
        role="treeitem"
        aria-selected={selected === n.id}
        data-category={n.id}
        onClick={() => onSelect(n.id)}
        onContextMenu={(e) => { e.preventDefault(); if (!locked) setMenu({ x: e.clientX, y: e.clientY, id: n.id }) }}
        className={cn(
          'group flex h-8 cursor-pointer items-center gap-2 rounded-md pr-2 text-[13px] transition-colors',
          selected === n.id ? 'bg-secondary text-foreground' : 'text-foreground/80 hover:bg-secondary/60',
          over === n.id && 'bg-primary/15 ring-1 ring-primary/60',
        )}
        style={{ paddingLeft: 8 + depth * 16 }}
      >
        <span className="min-w-0 flex-1 truncate">{n.name}</span>
        {n.router && <span title={`Router: ${n.router}`} className="rounded border border-primary/40 px-1 font-mono text-[10px] leading-4 text-primary">R</span>}
        <span className={cn('w-6 text-right font-mono text-[11px]', n.count ? 'text-muted-foreground' : 'text-muted-foreground/40')}>{n.count}</span>
        <button
          aria-label={`在 ${n.name} 下新建子 Category`}
          disabled={locked}
          onClick={(e) => { e.stopPropagation(); setNaming(n.id) }}
          className="invisible text-muted-foreground group-hover:visible hover:text-foreground"
        >
          <FolderPlusIcon className="size-3.5" />
        </button>
      </div>
      {n.children.map((c) => render(c, depth + 1))}
      {naming === n.id && <NewCategory parent={n.id} depth={depth + 1} onCancel={() => setNaming(undefined)} onCreate={onCreate} onDone={() => setNaming(undefined)} />}
    </div>
  )

  return (
    <aside className="flex w-[320px] shrink-0 flex-col border-r border-border bg-card">
      <div className="eyebrow px-4 pt-4 pb-2">Category</div>
      <div role="tree" className="min-h-0 flex-1 overflow-auto px-2 pb-2">
        {roots.map((r) => render(r, 0))}
        {naming === null && <NewCategory parent={null} depth={0} onCancel={() => setNaming(undefined)} onCreate={onCreate} onDone={() => setNaming(undefined)} />}
        <div
          role="treeitem"
          aria-selected={selected === UNCATEGORIZED}
          onClick={() => onSelect(UNCATEGORIZED)}
          className={cn(
            'mt-2 flex h-8 cursor-pointer items-center gap-2 rounded-md border-t border-border px-2 text-[13px] text-muted-foreground',
            selected === UNCATEGORIZED ? 'bg-secondary text-foreground' : 'hover:bg-secondary/60',
          )}
        >
          <span className="flex-1">未分类</span>
          <span className="font-mono text-[11px]">{data.uncategorized.length}</span>
        </div>
      </div>
      <div className="border-t border-border p-2">
        <Button variant="ghost" size="sm" className="w-full justify-start text-muted-foreground" disabled={locked} onClick={() => setNaming(null)}>
          <FolderPlusIcon /> 新建 Category
        </Button>
      </div>
      {menu && (
        <div className="fixed z-50 min-w-40 rounded-lg border border-border bg-popover p-1 text-sm shadow-xl" style={{ left: menu.x, top: menu.y }}>
          <button className="w-full rounded px-2.5 py-1.5 text-left hover:bg-secondary" onClick={() => setNaming(menu.id)}>新建子 Category</button>
        </div>
      )}
    </aside>
  )
}

function NewCategory({ parent, depth, onCancel, onCreate, onDone }: {
  parent: string | null; depth: number; onCancel: () => void; onCreate: (id: string, name: string) => Promise<void>; onDone: () => void
}) {
  const [name, setName] = useState('')
  const [slug, setSlug] = useState('')
  const [err, setErr] = useState<string | null>(null)
  const ref = useRef<HTMLInputElement>(null)
  useEffect(() => ref.current?.focus(), [])
  const submit = async () => {
    if (!name.trim() || !slug.trim()) { setErr('名称与 id 都要填'); return }
    if (!/^[a-z0-9]+(-[a-z0-9]+)*$/.test(slug)) { setErr('id 只能用小写字母、数字与 -'); return }
    try {
      await onCreate(parent ? `${parent}/${slug}` : slug, name.trim())
      onDone()
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      setErr(msg.includes('已存在') ? `重名: ${parent ? `${parent}/` : ''}${slug} 已存在,换一个 id` : msg)
    }
  }
  const key = (e: React.KeyboardEvent) => { if (e.key === 'Enter') submit(); if (e.key === 'Escape') onCancel() }
  return (
    <div className="space-y-1 py-1 pr-2" style={{ paddingLeft: 8 + depth * 16 }}>
      <div className="flex gap-1">
        <input ref={ref} value={name} onChange={(e) => setName(e.target.value)} onKeyDown={key} placeholder="名称"
          className="h-7 min-w-0 flex-1 rounded border border-primary/50 bg-background px-2 text-[13px] outline-none" />
        <input value={slug} onChange={(e) => setSlug(e.target.value)} onKeyDown={key} placeholder="id"
          className="h-7 w-24 rounded border border-border bg-background px-2 font-mono text-[12px] outline-none focus:border-primary/50" />
      </div>
      <div className={cn('text-[11px]', err ? 'text-destructive' : 'text-muted-foreground')}>
        {err ?? `Enter 新建 · Esc 取消${parent ? ` · 建在 ${parent}/ 下` : ''}`}
      </div>
    </div>
  )
}

// ---- 成员表 ----

function MemberTable({ rows, data, locked, highlight, uncategorized, onTouch, onDragStart, after }: {
  rows: Row[]; data: OrganizeData; locked: boolean; highlight: string | null; uncategorized: boolean
  onTouch: (key: string | null) => void; onDragStart: (member: string, from: string | null) => (e: React.PointerEvent) => void
  after: () => Promise<void>
}) {
  const names = new Map(data.categories.map((c) => [c.id, c.name]))
  const patch = async (name: string, p: { tier?: Tier; manual_only?: boolean }) => {
    try { await api.patchCatalog(name, p) } catch (e) { writeFailed('修改', e) }
    after()
  }
  const remove = async (r: Row, cid: string) => {
    onTouch(r.recentKey)
    try {
      const m = await api.membership('remove', r.ref, cid)
      if (m.categories.length === 0) toast.warning(`${r.ref} 已移出最后一个 Category · 现在只能经 Index 检索到`)
      else toast.success(`已从 ${names.get(cid)} 移出`)
    } catch (e) { writeFailed('移出', e) }
    after()
  }
  return (
    <table className="w-full text-[13px]">
      <thead className="sticky top-0 bg-background text-left text-xs text-muted-foreground">
        <tr className="border-b border-border">
          <th className="py-2 pr-3 pl-5 font-normal">名字</th>
          <th className="px-3 font-normal">类型</th>
          <th className="px-3 font-normal">Tier</th>
          <th className="px-3 font-normal">Manual-only</th>
          <th className="px-3 font-normal">来源</th>
          <th className="px-3 pr-5 font-normal">Category</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr
            key={`${r.from}:${r.ref}`}
            data-member={r.ref}
            onPointerDown={onDragStart(r.ref, r.from)}
            onClick={() => onTouch(r.recentKey)}
            className={cn(
              'border-b border-border/60 align-top transition-colors hover:bg-secondary/50',
              !locked && 'cursor-grab select-none',
              highlight === r.ref && 'bg-primary/10',
            )}
          >
            <td className="py-2 pr-3 pl-5">
              <div className="flex items-center gap-2">
                <span className="font-medium break-all">{r.ref}</span>
                {r.recentKey && <span className="rounded-full bg-primary px-1.5 text-[10px] leading-4 font-semibold text-primary-foreground">新</span>}
              </div>
              {r.trait && <div className="mt-0.5 text-xs text-muted-foreground">{r.trait}</div>}
              {uncategorized && <div className="mt-0.5 text-xs text-muted-foreground/70">不在任何 Category 中 · 只能经 Index 检索到 · 拖到左侧归类</div>}
            </td>
            <td className="px-3 py-2">
              <span className="inline-flex items-center gap-1.5 font-mono text-[11px]">
                <span className="size-1.5 rounded-full" style={{ background: KIND_COLOR[r.kind] }} />
                {KIND_LABEL[r.kind]}
              </span>
            </td>
            <td className="px-3 py-1.5">
              {r.entry ? (
                <select
                  aria-label={`${r.ref} 的 Tier`}
                  value={r.entry.tier}
                  disabled={locked}
                  onChange={(e) => patch(r.ref, { tier: e.target.value as Tier })}
                  className="h-7 rounded border border-border bg-background px-1.5 text-[12px] disabled:opacity-50"
                >
                  {(Object.keys(TIER_LABEL) as Tier[]).map((t) => <option key={t} value={t}>{TIER_LABEL[t]}</option>)}
                </select>
              ) : <span className="text-muted-foreground/50">—</span>}
            </td>
            <td className="px-3 py-2">
              {r.entry ? (
                <input
                  type="checkbox"
                  aria-label={`${r.ref} Manual-only`}
                  checked={r.entry.manual_only}
                  disabled={locked}
                  onChange={(e) => patch(r.ref, { manual_only: e.target.checked })}
                  className="size-4 accent-[var(--primary)]"
                />
              ) : <span className="text-muted-foreground/50">—</span>}
            </td>
            <td className="max-w-56 px-3 py-2 text-xs text-muted-foreground">
              {r.entry ? (
                <>
                  <span className="font-mono">{r.entry.entry}</span>
                  <div className="truncate" title={r.entry.source}>{r.entry.source}</div>
                  <div className="text-muted-foreground/60">加入 {formatTime(r.entry.added + 'T00:00:00').split(' ')[0]}</div>
                </>
              ) : r.kind === 'skill' ? <span className="text-destructive">不在 Catalog 中</span> : <span>Reference</span>}
            </td>
            <td className="px-3 py-2 pr-5">
              <div className="flex flex-wrap gap-1">
                {r.categories.map((cid) => (
                  <span key={cid} className="inline-flex items-center gap-1 rounded-full border border-border py-0.5 pr-1 pl-2 text-[11px]">
                    {names.get(cid) ?? cid}
                    <button aria-label={`从 ${names.get(cid)} 移出 ${r.ref}`} disabled={locked} onClick={(e) => { e.stopPropagation(); remove(r, cid) }}
                      className="rounded-full p-0.5 text-muted-foreground hover:bg-destructive/20 hover:text-destructive disabled:opacity-40">
                      <XIcon className="size-3" />
                    </button>
                  </span>
                ))}
              </div>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function EmptyState({ recentOnly, category, hasChildren, locked, onDeleted }: {
  recentOnly: boolean; category: { id: string; name: string } | null; hasChildren: boolean; locked: boolean; onDeleted: (id: string) => void
}) {
  const [confirm, setConfirm] = useState(false)
  if (recentOnly) return <Center>没有新归类的成员 · agent 最近 7 天的归类都看过了</Center>
  if (!category) return <Center>所有成员都已归类</Center>
  if (hasChildren) return <Center>{`${category.name} 自己没有成员 · 成员在子 Category 里,在左侧选择`}</Center>
  const del = async () => {
    setConfirm(false)
    try { await api.deleteCategory(category.id); onDeleted(category.id) } catch (e) { writeFailed('删除', e) }
  }
  return (
    <div className="grid h-full place-items-center">
      <div className="flex flex-col items-center gap-3 text-sm text-muted-foreground">
        <div className="rounded-xl border border-dashed border-border px-10 py-8 text-center">
          拖成员到这里
          <div className="mt-1 text-xs text-muted-foreground/70">打开其他 Category,把成员拖到左侧「{category.name}」上</div>
        </div>
        <Button variant="ghost" size="sm" disabled={locked} className="text-muted-foreground hover:text-destructive" onClick={() => setConfirm(true)}>
          删除这个 Category
        </Button>
      </div>
      <Dialog open={confirm} onOpenChange={setConfirm}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>删除 Category {category.name}?</DialogTitle>
            <DialogDescription>只有空的 Category 能删。删除写入 categories.yaml,应用前可用「撤销」恢复。</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setConfirm(false)}>取消</Button>
            <Button variant="destructive" onClick={del}>删除</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
