import { CommandDialog, Command, CommandEmpty, CommandInput, CommandItem, CommandList } from '@/components/ui/command'
import type { GNode, Graph } from '@/lib/graph'
import { dotColor } from './DetailPanel'

// 子串匹配,名称命中排在摘要命中之前
const filter = (_value: string, search: string, keywords?: string[]) => {
  const q = search.trim().toLowerCase()
  if (!q || !keywords) return 1
  const [name, rest] = keywords
  return name.includes(q) ? 1 : rest.includes(q) ? 0.5 : 0
}

export function SearchCommand({ graph, open, onOpenChange, onPick }: {
  graph: Graph
  open: boolean
  onOpenChange: (v: boolean) => void
  onPick: (n: GNode) => void
}) {
  return (
    <CommandDialog open={open} onOpenChange={onOpenChange} title="搜索节点" description="按名称或摘要搜索" className="sm:max-w-xl">
      <Command filter={filter}>
        <CommandInput placeholder="名称或摘要…" />
        <CommandList className="max-h-[420px]">
          <CommandEmpty>没有匹配的节点</CommandEmpty>
          {graph.nodes.filter((n) => !n.virtual).map((n) => (
            <CommandItem
              key={n.idx}
              value={String(n.raw.id)}
              keywords={[n.raw.name.toLowerCase(), `${n.raw.hier_summary}\n${n.raw.content_summary ?? ''}`.toLowerCase()]}
              onSelect={() => { onOpenChange(false); onPick(n) }}
              className="gap-3 py-2"
            >
              <span className="size-1.5 shrink-0 rounded-full" style={{ background: dotColor(n), boxShadow: `0 0 6px ${dotColor(n)}` }} />
              <div className="min-w-0 flex-1">
                <div className="truncate">{n.raw.name}</div>
                {n.raw.hier_summary && <div className="truncate text-xs text-muted-foreground">{n.raw.hier_summary}</div>}
              </div>
              {n.desc > 0 && <span className="font-mono text-[10px] text-muted-foreground">{n.desc}</span>}
            </CommandItem>
          ))}
        </CommandList>
      </Command>
    </CommandDialog>
  )
}
