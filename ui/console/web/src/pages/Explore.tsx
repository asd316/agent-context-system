import { SearchIcon } from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { DetailPanel } from '@/components/DetailPanel'
import { Galaxy } from '@/components/Galaxy'
import { SearchCommand } from '@/components/SearchCommand'
import { Kbd } from '@/components/ui/kbd'
import { CATEGORIES, CATEGORY_COLOR, fetchGraph, type Category, type GNode, type Graph } from '@/lib/graph'
import { layoutGraph } from '@/lib/layout'
import { cn } from '@/lib/utils'

export default function Explore() {
  const [graph, setGraph] = useState<Graph | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    fetchGraph().then(layoutGraph).then(setGraph, (e) => setError(String(e.message ?? e)))
  }, [])

  if (error) return <Center>{error}</Center>
  if (!graph) return <Center>…</Center>
  return <Explorer graph={graph} />
}

function Center({ children }: { children: React.ReactNode }) {
  return <div className="grid h-full place-items-center bg-background font-mono text-xs text-muted-foreground">{children}</div>
}

function Explorer({ graph }: { graph: Graph }) {
  const [hidden, setHidden] = useState<Set<Category>>(new Set())
  const [pendingOnly, setPendingOnly] = useState(false)
  const [hovered, setHovered] = useState<GNode | null>(null)
  const [selected, setSelected] = useState<GNode | null>(null)
  const [flyKey, setFlyKey] = useState(0)
  const [searchOpen, setSearchOpen] = useState(false)

  const isVisible = useCallback(
    (n: GNode) => n.virtual || (!hidden.has(n.raw.category) && (!pendingOnly || n.raw.pending)),
    [hidden, pendingOnly],
  )
  const real = useMemo(() => graph.nodes.filter((n) => !n.virtual), [graph])
  const counts = useMemo(() => {
    const c = Object.fromEntries(CATEGORIES.map((k) => [k, 0])) as Record<Category, number>
    for (const n of real) c[n.raw.category]++
    return { ...c, pending: real.filter((n) => n.raw.pending).length, missing: real.filter((n) => n.raw.missing).length }
  }, [real])

  const pick = useCallback((n: GNode) => {
    setHidden((h) => (h.has(n.raw.category) ? new Set([...h].filter((x) => x !== n.raw.category)) : h))
    if (!n.raw.pending && !n.virtual) setPendingOnly(false)
    setSelected(n)
    setFlyKey((k) => k + 1)
  }, [])

  const home = useCallback(() => { setSelected(null); setFlyKey((k) => k + 1) }, [])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'k') { e.preventDefault(); setSearchOpen((v) => !v) }
      else if (e.key === 'Escape' && !searchOpen) home()
      else if (e.key === '/' && !searchOpen && document.activeElement === document.body) { e.preventDefault(); setSearchOpen(true) }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [searchOpen, home])

  useEffect(() => { if (selected && !isVisible(selected)) setSelected(null) }, [selected, isVisible])

  // 供调试与验证的只读句柄
  useEffect(() => {
    Object.assign(window, { __graph: { graph, select: (id: number) => { const n = graph.byId.get(id); if (n) pick(n) } } })
  }, [graph, pick])

  const toggleCat = (c: Category) => setHidden((h) => {
    const next = new Set(h)
    if (next.has(c)) next.delete(c)
    else next.add(c)
    return next
  })

  return (
    <div className="relative h-full overflow-hidden bg-background">
      <Galaxy
        graph={graph}
        isVisible={isVisible}
        hovered={hovered}
        selected={selected}
        flyKey={flyKey}
        onHover={setHovered}
        onSelect={(n) => (n ? pick(n) : setSelected(null))}
      />

      <header className="pointer-events-none absolute inset-x-0 top-12 z-10 flex items-start justify-between gap-6 p-5">
        <div className="pointer-events-auto flex flex-col gap-3">
          <button
            onClick={() => setSearchOpen(true)}
            className="glass flex h-9 w-72 items-center gap-2.5 rounded-lg px-3 text-sm text-muted-foreground transition-colors hover:text-foreground"
          >
            <SearchIcon className="size-4" />
            <span className="flex-1 text-left">搜索节点</span>
            <Kbd>⌘K</Kbd>
          </button>
          <div className="flex gap-1.5">
            {CATEGORIES.map((c) => {
              const on = !hidden.has(c)
              return (
                <button
                  key={c}
                  onClick={() => toggleCat(c)}
                  aria-pressed={on}
                  className={cn(
                    'glass flex h-7 items-center gap-2 rounded-full px-3 text-xs transition-[opacity,transform] duration-150 active:scale-[0.97]',
                    !on && 'opacity-40',
                  )}
                >
                  <span className="size-1.5 rounded-full" style={{ background: CATEGORY_COLOR[c], boxShadow: on ? `0 0 8px ${CATEGORY_COLOR[c]}` : 'none' }} />
                  {c}
                  <span className="font-mono text-[10px] text-muted-foreground">{counts[c]}</span>
                </button>
              )
            })}
          </div>
        </div>

        <div className={cn('pointer-events-auto flex gap-6 transition-[margin] duration-300', selected && 'mr-[376px]')}>
          <Stat value={real.length} label="节点" />
          <button onClick={() => setPendingOnly((v) => !v)} aria-pressed={pendingOnly} className="group text-right">
            <Stat value={counts.pending} label={pendingOnly ? '只看待确认' : '待确认'} accent={pendingOnly} />
          </button>
          {counts.missing > 0 && <Stat value={counts.missing} label="路径失效" danger />}
        </div>
      </header>

      <DetailPanel node={selected} onPick={pick} onClose={home} />
      <SearchCommand graph={graph} open={searchOpen} onOpenChange={setSearchOpen} onPick={pick} />
    </div>
  )
}

function Stat({ value, label, accent, danger }: { value: number; label: string; accent?: boolean; danger?: boolean }) {
  return (
    <div className="flex flex-col items-end gap-1">
      <span className={cn('font-mono text-2xl leading-none font-semibold tabular-nums', accent && 'text-primary', danger && 'text-destructive')}>{value}</span>
      <span className={cn('eyebrow', accent && 'text-primary')}>{label}</span>
    </div>
  )
}
