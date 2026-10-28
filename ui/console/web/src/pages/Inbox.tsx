import { ArrowLeftIcon, CopyIcon } from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { toast } from 'sonner'
import { IdChip } from '@/components/IdChip'
import { Button } from '@/components/ui/button'
import { api, ApiError, type Decision, type Preview, type ReportBrief, type SharedCategory } from '@/lib/api'
import { copyText, formatTime } from '@/lib/format'
import { useLoad, useStatus } from '@/lib/status'
import { cn } from '@/lib/utils'

type Tab = 'pending' | 'in-progress' | 'log'
const TABS: { key: Tab; label: string }[] = [
  { key: 'pending', label: '待处理' }, { key: 'in-progress', label: '处理中' }, { key: 'log', label: 'Decision log' },
]

interface InboxData { open: ReportBrief[]; closed: ReportBrief[]; decisions: Decision[]; lastDigest: string | null }

async function loadInbox(): Promise<InboxData> {
  const [open, closed, decisions, digests] = await Promise.all([
    api.inbox(), api.inbox(['resolved', 'rejected']), api.decisions(), api.inbox(['pending', 'in-progress', 'resolved', 'rejected'], 'digest'),
  ])
  return { open, closed, decisions, lastDigest: digests[0]?.at ?? null }
}

const RECENT = 5

export default function Inbox() {
  const { data, error, reload } = useLoad(loadInbox)
  const status = useStatus()
  const [tab, setTab] = useState<Tab>('pending')
  const [selected, setSelected] = useState<string | null>(null)
  // 按 id 查找、点最近处理或 Decision log 打开的 Report,不受当前 tab 列表限制
  const [lookup, setLookup] = useState<string | null>(null)
  const [query, setQuery] = useState('')

  const lists = useMemo(() => {
    const open = data?.open ?? []
    // Proposal 在前,其余按时间倒序 (接口已按时间倒序)
    const pending = open.filter((r) => r.status === 'pending').sort((a, b) => Number(b.proposal) - Number(a.proposal))
    return { pending, 'in-progress': open.filter((r) => r.status === 'in-progress') }
  }, [data])

  // 最近关掉的 Report: 按 Decision 时间 (接口已新的在前),每条只取最近一次
  const recent = useMemo(() => {
    const byId = new Map((data?.closed ?? []).map((r) => [r.id, r]))
    const out: { r: ReportBrief; d: Decision }[] = []
    for (const d of data?.decisions ?? []) {
      const r = byId.get(d.target)
      if (r && !out.some((x) => x.r.id === r.id)) out.push({ r, d })
      if (out.length === RECENT) break
    }
    return out
  }, [data])

  const reports = useMemo(() => new Map([...(data?.open ?? []), ...(data?.closed ?? [])].map((r) => [r.id, r])), [data])

  const find = () => {
    const id = query.trim()
    if (!id) return
    const all = [...(data?.open ?? []), ...(data?.closed ?? [])]
    const hit = all.find((r) => r.id === id) ?? (all.filter((r) => r.id.startsWith(id)).length === 1 ? all.find((r) => r.id.startsWith(id)) : undefined)
    if (hit) setLookup(hit.id)
    else toast.error(`没有找到 ${id} · 需要完整 id 或唯一前缀`)
  }

  useEffect(() => {
    if (tab === 'log' || !data) return
    const list = lists[tab]
    if (!selected || !list.some((r) => r.id === selected)) setSelected(list[0]?.id ?? null)
  }, [tab, lists, data, selected])

  const after = async () => { await reload(); await status.refresh() }

  if (error) return <div className="grid h-full place-items-center font-mono text-xs text-muted-foreground">读取失败 · {error}</div>
  if (!data) return <div className="grid h-full place-items-center font-mono text-xs text-muted-foreground">…</div>

  return (
    <div className="flex h-full">
      <aside className="flex w-[340px] shrink-0 flex-col border-r border-border bg-card">
        <div className="border-b border-border p-3">
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && find()}
            placeholder="按 Report id 查找,回车打开"
            className="h-8 w-full rounded-lg border border-border bg-background px-3 font-mono text-[12px] outline-none focus:border-primary/60"
          />
        </div>
        <div role="tablist" className="flex gap-1 border-b border-border px-3 pt-3">
          {TABS.map((t) => {
            const n = t.key === 'log' ? null : lists[t.key].length
            return (
              <button
                key={t.key}
                role="tab"
                aria-selected={tab === t.key}
                onClick={() => { setTab(t.key); setLookup(null) }}
                className={cn('-mb-px border-b-2 px-2.5 pb-2 text-[13px]', tab === t.key ? 'border-primary text-foreground' : 'border-transparent text-muted-foreground hover:text-foreground')}
              >
                {t.label}{n !== null && <span className="ml-1 font-mono text-[11px] text-muted-foreground">{n}</span>}
              </button>
            )
          })}
        </div>
        {tab !== 'log' && (
          <div className="min-h-0 flex-1 overflow-auto p-2">
            {lists[tab].map((r) => (
              <button
                key={r.id}
                onClick={() => { setSelected(r.id); setLookup(null) }}
                className={cn('mb-1 w-full rounded-lg px-3 py-2.5 text-left transition-colors', selected === r.id && !lookup ? 'bg-secondary' : 'hover:bg-secondary/50', r.status === 'in-progress' && 'opacity-70')}
              >
                <div className="flex items-center gap-2 font-mono text-[11px] whitespace-nowrap text-muted-foreground">
                  <span className="text-foreground/80">{r.id}</span>
                  <span className="ml-auto">{formatTime(r.at)}</span>
                </div>
                <div className="mt-0.5 flex gap-2 font-mono text-[11px] text-muted-foreground">
                  {r.proposal && <span className="text-primary">◆ Proposal</span>}
                  <span>{r.kind}</span>
                </div>
                <div className="mt-1 text-[13px]">{r.title}</div>
                {r.status === 'in-progress' && <div className="mt-1 text-xs text-muted-foreground">处理中({r.claimed_by})</div>}
              </button>
            ))}
            {lists[tab].length === 0 && (
              <div className="px-3 py-8 text-center text-sm text-muted-foreground">
                {tab === 'pending' ? (
                  <>没有待处理的事项<div className="mt-1 text-xs">{data.lastDigest ? `上次 Digest: ${formatTime(data.lastDigest)}` : '还没有 Digest'}</div></>
                ) : '没有正在处理的事项'}
              </div>
            )}
            {tab === 'pending' && lists.pending.length === 0 && recent.length > 0 && (
              <div className="mt-2">
                <div className="eyebrow px-3 pb-1.5">最近处理</div>
                {recent.map(({ r, d }) => (
                  <button
                    key={r.id}
                    onClick={() => setLookup(r.id)}
                    className={cn('mb-1 w-full rounded-lg px-3 py-2 text-left transition-colors', lookup === r.id ? 'bg-secondary' : 'hover:bg-secondary/50')}
                  >
                    <div className="flex items-center gap-2 font-mono text-[11px] whitespace-nowrap text-muted-foreground">
                      <span className="text-foreground/80">{r.id}</span>
                      <span className={resultClass(d.result)}>{resultLabel(d.result, r.kind)}</span>
                      <span className="ml-auto">{formatTime(d.at)}</span>
                    </div>
                    <div className="mt-1 text-[13px] text-muted-foreground">{r.title}</div>
                  </button>
                ))}
              </div>
            )}
          </div>
        )}
        {tab === 'log' && <div className="p-4 text-xs text-muted-foreground">共 {data.decisions.length} 条决定,只读。右侧可按对象或时间筛选。</div>}
      </aside>
      <section className="min-w-0 flex-1 overflow-auto">
        {lookup ? (
          <div>
            {tab === 'log' && (
              <button onClick={() => setLookup(null)} className="mx-6 mt-4 flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground">
                <ArrowLeftIcon className="size-3.5" /> 回到 Decision log
              </button>
            )}
            <Detail key={lookup} id={lookup} locked={status.locked} onDecided={after} />
          </div>
        ) : tab === 'log' ? (
          <DecisionLog decisions={data.decisions} reports={reports} onOpen={setLookup} />
        ) : selected ? (
          <Detail key={selected} id={selected} locked={status.locked} onDecided={after} />
        ) : (
          <div className="grid h-full place-items-center text-sm text-muted-foreground">
            {tab === 'pending' ? '没有待处理的事项 · Report 与 Digest 到了会出现在左侧' : '没有正在处理的事项'}
          </div>
        )}
      </section>
    </div>
  )
}

function Detail({ id, locked, onDecided }: { id: string; locked: boolean; onDecided: () => Promise<void> }) {
  const load = useCallback(() => Promise.all([api.report(id), api.preview(id)]), [id])
  const { data, error } = useLoad(load)
  const [opinion, setOpinion] = useState('')
  const [copied, setCopied] = useState<string | null>(null)
  const [rejecting, setRejecting] = useState(false)
  const [reason, setReason] = useState('')
  const [conflict, setConflict] = useState<string | null>(null)

  if (error) return <div className="p-6 text-sm text-destructive">读取失败 · {error}</div>
  if (!data) return null
  const [r, pv] = data
  const digest = r.kind === 'digest'
  const closed = r.status === 'resolved' || r.status === 'rejected'
  const words = digest ? { ok: '采纳', no: '否决' } : { ok: '批准', no: '驳回' }

  const copy = async () => {
    const { instruction } = await api.instruction(r.id, opinion)
    if (await copyText(instruction, '已复制指令 · 粘贴到任意 agent 会话')) setCopied(instruction)
  }
  const decide = async (decision: 'approve' | 'reject') => {
    try {
      await api.decide(r.id, decision, decision === 'reject' ? reason : opinion)
      toast.success(decision === 'approve'
        ? `已${words.ok} ${r.id}${r.change ? ` · 改动已写入 ${r.change.target} · 点「应用」生效` : ''}`
        : `已${words.no} ${r.id} · 理由记入 Decision log`)
      await onDecided()
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) setConflict(e.message)
      else toast.error(`${decision === 'approve' ? words.ok : words.no}失败 · ${e instanceof Error ? e.message : e}`)
    }
  }

  return (
    <div className="mx-auto max-w-4xl space-y-5 p-6">
      <header className="space-y-2">
        <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
          <IdChip id={r.id} />
          <span className="font-mono">{r.kind}</span>
          {r.change && <span className="text-primary">◆ Proposal</span>}
          <span>来自 {r.from.client} / {r.from.position} @ {r.from.host}</span>
          <span>{formatTime(r.at)}</span>
          {r.status === 'in-progress' && <span className="text-primary">处理中({r.claimed_by})</span>}
          {closed && (r.resolution?.outcome === 'superseded'
            ? <span className="text-muted-foreground">已取代</span>
            : <span className={r.status === 'rejected' ? 'text-destructive' : 'text-primary'}>{r.status === 'rejected' ? `已${words.no}` : `已${words.ok}`}</span>)}
        </div>
        <h1 className="text-lg font-semibold">{r.title}</h1>
      </header>
      <Block label="理由"><p className="text-sm leading-relaxed whitespace-pre-wrap">{r.reason}</p></Block>
      <Block label="证据"><pre className="rounded-lg bg-secondary/60 p-3 font-mono text-[12px] leading-relaxed whitespace-pre-wrap">{r.evidence}</pre></Block>
      {r.change && (
        <Block label={`${closed ? '提交的改动' : '改动预览'} · ${r.change.target}`}>
          {/* 关闭后改动已应用或已放弃,对当前文件重放只会报冲突 */}
          {!closed ? <ChangePreview pv={pv} />
            : r.change.replace ? <TextDiff before={r.change.replace.old} after={r.change.replace.new} />
            : <pre className="rounded-lg bg-secondary/40 p-3 font-mono text-[12px] leading-relaxed whitespace-pre-wrap">{r.change.diff}</pre>}
        </Block>
      )}

      {closed && r.resolution && (
        <Block label="处理结果">
          <div className="space-y-1 rounded-lg bg-secondary/60 p-3 text-sm">
            <div className="text-xs text-muted-foreground">{formatTime(r.resolution.at)} · 决定人 {r.resolution.decided_by}{r.resolution.outcome && ` · ${r.resolution.outcome}`}</div>
            {r.resolution.instruction && <p className="whitespace-pre-wrap">“{r.resolution.instruction}”</p>}
            {r.resolution.changes.length > 0 && <p className="font-mono text-[12px] text-muted-foreground">改动: {r.resolution.changes.join(', ')}</p>}
          </div>
        </Block>
      )}

      {!closed && <>
      <div className="rounded-xl border border-primary/30 bg-card p-4">
        <div className="eyebrow mb-2">交给 agent 处理</div>
        {r.status === 'in-progress' && <p className="mb-2 text-xs text-muted-foreground">另一个 agent ({r.claimed_by}) 正在处理这一条。</p>}
        <div className="flex gap-2">
          <input
            value={opinion}
            onChange={(e) => setOpinion(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && copy()}
            placeholder={pv.trees ? '你的意见,例如: coding-agents 不要动,其余照做' : '你的意见 (可空)'}
            className="h-9 min-w-0 flex-1 rounded-lg border border-border bg-background px-3 text-sm outline-none focus:border-primary/60"
          />
          <Button size="lg" onClick={copy}><CopyIcon /> 复制指令</Button>
        </div>
        {copied && <p className="mt-2 rounded bg-secondary/60 px-2 py-1.5 font-mono text-[12px] text-muted-foreground">已复制: {copied}</p>}
        {!copied && <p className="mt-2 text-xs text-muted-foreground">粘贴到任意 agent 会话;agent 会读这条 Report、按你的意见处理并记入 Decision log。</p>}
      </div>

      <div className="space-y-2">
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          简单的直接点:
          <Button variant="outline" size="sm" disabled={locked} onClick={() => decide('approve')}>{words.ok}</Button>
          <Button variant="destructive" size="sm" disabled={locked} onClick={() => setRejecting((v) => !v)}>{words.no}</Button>
          {locked && <span>正在应用 · 暂时不能处理</span>}
        </div>
        {conflict && (
          <p className="text-sm text-destructive">改动已与当前源文件冲突 · 没有应用,Report 保持原样 · 改走「复制指令」交给 agent ({conflict})</p>
        )}
        {rejecting && (
          <div className="flex gap-2">
            <input
              value={reason}
              autoFocus
              onChange={(e) => setReason(e.target.value)}
              placeholder="填写理由,提交它的 agent 之后能看到"
              className="h-8 min-w-0 flex-1 rounded-lg border border-destructive/40 bg-background px-3 text-sm outline-none"
            />
            <Button variant="destructive" size="sm" disabled={!reason.trim() || locked} onClick={() => decide('reject')}>确认{words.no}</Button>
          </div>
        )}
      </div>
      </>}
    </div>
  )
}

function Block({ label, children }: { label: string; children: React.ReactNode }) {
  return <div className="space-y-1.5"><div className="eyebrow">{label}</div>{children}</div>
}

function ChangePreview({ pv }: { pv: Preview }) {
  if (pv.conflict) return <p className="text-sm text-destructive">改动已与当前源文件冲突 · 批准不会生效 · 改走「复制指令」({pv.conflict})</p>
  if (pv.trees) return <TreeDiff before={pv.trees.before} after={pv.trees.after} />
  return <TextDiff before={pv.before ?? ''} after={pv.after ?? ''} />
}

function TreeDiff({ before, after }: { before: SharedCategory[]; after: SharedCategory[] }) {
  const b = new Map(before.map((c) => [c.id, c]))
  const a = new Map(after.map((c) => [c.id, c]))
  const ids = [...new Set([...b.keys(), ...a.keys()])].sort()
  const changed = ids.filter((id) => JSON.stringify(b.get(id)) !== JSON.stringify(a.get(id)))
  const side = (mine: Map<string, SharedCategory>, other: Map<string, SharedCategory>, sign: '+' | '-') => (
    <div className="space-y-2 rounded-lg bg-secondary/40 p-3 text-[13px]">
      {changed.map((id) => {
        const c = mine.get(id)
        const o = other.get(id)
        if (!c) return <div key={id} className="text-muted-foreground/50 italic">{id} (无)</div>
        return (
          <div key={id}>
            <div className={cn('font-medium', !o && (sign === '+' ? 'text-primary' : 'text-destructive line-through'))}>
              {c.name} <span className="font-mono text-[11px] text-muted-foreground">{id}</span>
            </div>
            <div className="mt-0.5 flex flex-wrap gap-1 pl-3">
              {c.members.map((m) => {
                const diff = !o?.members.includes(m)
                return (
                  <span key={m} className={cn('rounded px-1.5 font-mono text-[11px]', diff ? (sign === '+' ? 'bg-primary/20 text-primary' : 'bg-destructive/20 text-destructive line-through') : 'text-muted-foreground')}>
                    {m}
                  </span>
                )
              })}
              {c.members.length === 0 && <span className="text-xs text-muted-foreground/60">(没有成员)</span>}
            </div>
          </div>
        )
      })}
    </div>
  )
  return (
    <div className="space-y-2">
      <div className="grid grid-cols-2 gap-3">
        <div><div className="mb-1 text-xs text-muted-foreground">改动前</div>{side(b, a, '-')}</div>
        <div><div className="mb-1 text-xs text-muted-foreground">改动后</div>{side(a, b, '+')}</div>
      </div>
      <p className="text-xs text-muted-foreground">只列有变化的 {changed.length} 个 Category,其余 {ids.length - changed.length} 个不变。</p>
    </div>
  )
}

/** 行级 LCS diff,只显示改动处前后 3 行 */
function lineDiff(a: string[], b: string[]) {
  const n = a.length, m = b.length
  const dp = Array.from({ length: n + 1 }, () => new Int32Array(m + 1))
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1])
  const out: { t: ' ' | '-' | '+'; s: string }[] = []
  let i = 0, j = 0
  while (i < n || j < m) {
    if (i < n && j < m && a[i] === b[j]) { out.push({ t: ' ', s: a[i] }); i++; j++ }
    else if (i < n && (j === m || dp[i + 1][j] >= dp[i][j + 1])) out.push({ t: '-', s: a[i++] })
    else out.push({ t: '+', s: b[j++] })
  }
  return out.map((x, k) => ({ ...x, keep: out.slice(Math.max(0, k - 3), k + 4).some((y) => y.t !== ' ') }))
}

function TextDiff({ before, after }: { before: string; after: string }) {
  const lines = useMemo(() => lineDiff(before.split('\n'), after.split('\n')), [before, after])
  return (
    <pre className="rounded-lg bg-secondary/40 p-3 font-mono text-[12px] leading-relaxed whitespace-pre-wrap">
      {lines.map((l, k) => l.keep ? (
        <div key={k} className={cn(l.t === '+' && 'bg-primary/15 text-primary', l.t === '-' && 'bg-destructive/15 text-destructive')}>
          {l.t} {l.s}
        </div>
      ) : (k > 0 && lines[k - 1].keep ? <div key={k} className="text-muted-foreground/50">⋯</div> : null))}
    </pre>
  )
}

function DecisionLog({ decisions, reports, onOpen }: { decisions: Decision[]; reports: Map<string, ReportBrief>; onOpen: (id: string) => void }) {
  const [q, setQ] = useState('')
  const [days, setDays] = useState(0)
  const [open, setOpen] = useState<Set<string>>(new Set())
  const now = useMemo(() => Date.now(), [])
  const shown = decisions.filter((d) => {
    if (q && !`${d.target} ${d.instruction ?? ''} ${d.note ?? ''} ${d.by}`.toLowerCase().includes(q.toLowerCase())) return false
    if (days && now - new Date(d.at).getTime() > days * 86400000) return false
    return true
  })
  // 同一次批量操作折叠成一行: 相邻、同一结果、同一句原话、同一执行者,且与这批第一条相隔不到 10 分钟
  const groups: { key: string; items: Decision[] }[] = []
  for (const d of shown) {
    const sig = [d.result, d.instruction ?? '', d.note ?? '', d.by].join('\u0000')
    const last = groups[groups.length - 1]
    const head = last?.items[0]
    if (head && last.key.endsWith(sig) && Math.abs(new Date(head.at).getTime() - new Date(d.at).getTime()) < 600000) last.items.push(d)
    else groups.push({ key: `${d.at}\u0000${sig}`, items: [d] })
  }
  const toggle = (key: string) => setOpen((s) => { const n = new Set(s); if (n.has(key)) n.delete(key); else n.add(key); return n })

  const row = (d: Decision, i: number, lead?: { n: number; key: string }) => {
    const r = reports.get(d.target)
    const decider = r?.decided_by ?? (d.by === 'user' ? 'user' : null)
    const clickable = !lead && d.target.startsWith('R-')
    return (
      <tr key={i} onClick={() => lead ? toggle(lead.key) : clickable && onOpen(d.target)}
        className={cn('border-b border-border/60 align-top', (lead || clickable) && 'cursor-pointer hover:bg-secondary/40')}>
        <td className="py-2 pr-3 font-mono text-[12px] whitespace-nowrap text-muted-foreground">{formatTime(d.at)}</td>
        <td className="pr-3 whitespace-nowrap">
          {lead
            ? <span className="text-[12px] text-muted-foreground">{open.has(lead.key) ? '▾' : '▸'} {lead.n} 条 · <span className="font-mono">{d.target.slice(0, 10)}…</span></span>
            : <IdChip id={d.target} />}
        </td>
        <td className={cn('pr-3 whitespace-nowrap', resultClass(d.result))}>{resultLabel(d.result, r?.kind)}</td>
        <td className="pr-3 text-[12px] whitespace-nowrap" title={d.session ? `会话 ${d.session}` : undefined}>
          {decider && decider !== d.by
            ? <>{decider} <span className="text-muted-foreground">· 经 <span className="font-mono">{d.by}</span></span></>
            : <span className="font-mono">{d.by}</span>}
          {d.session && <div className="font-mono text-[11px] text-muted-foreground">{d.session.slice(0, 8)}</div>}
        </td>
        <td className="whitespace-pre-wrap">{d.instruction ? `“${d.instruction}”` : <span className="text-muted-foreground">{d.note}</span>}</td>
      </tr>
    )
  }

  return (
    <div className="p-6">
      <div className="mb-3 flex gap-2">
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="按对象筛选,如 R-20200101 或 coding-agents"
          className="h-8 w-80 rounded-lg border border-border bg-background px-3 text-sm outline-none focus:border-primary/60" />
        <select value={days} onChange={(e) => setDays(Number(e.target.value))} className="h-8 rounded-lg border border-border bg-background px-2 text-sm">
          <option value={0}>全部时间</option><option value={1}>最近 1 天</option><option value={7}>最近 7 天</option><option value={30}>最近 30 天</option>
        </select>
      </div>
      {shown.length === 0 ? <p className="text-sm text-muted-foreground">{decisions.length ? '没有匹配的决定' : '还没有决定 · 批准、驳回或 agent 处理后会记在这里'}</p> : (
        <table className="w-full text-[13px]">
          <thead className="text-left text-xs text-muted-foreground">
            <tr className="border-b border-border"><th className="py-2 font-normal">时间</th><th className="font-normal">对象</th><th className="font-normal">结果</th><th className="font-normal">决定人 · 执行者</th><th className="font-normal">原话或备注</th></tr>
          </thead>
          <tbody>
            {groups.flatMap((g, gi) => g.items.length === 1
              ? [row(g.items[0], gi * 100000)]
              : [row(g.items[0], gi * 100000, { n: g.items.length, key: g.key }),
                 ...(open.has(g.key) ? g.items.map((d, k) => row(d, gi * 100000 + k + 1)) : [])])}
          </tbody>
        </table>
      )}
    </div>
  )
}

const RESULT: Record<string, string> = { approved: '批准', rejected: '驳回', adopted: '采纳', dismissed: '否决', modified: '按意见修改', superseded: '已取代' }

/** Digest 的批准与驳回在界面上叫采纳与否决,与详情页一致 */
function resultLabel(result: string, kind?: string) {
  if (kind === 'digest' && result === 'approved') return RESULT.adopted
  if (kind === 'digest' && result === 'rejected') return RESULT.dismissed
  return RESULT[result] ?? result
}

function resultClass(result: string) {
  if (result === 'superseded') return 'text-muted-foreground'
  return result === 'rejected' || result === 'dismissed' ? 'text-destructive' : 'text-primary'
}
