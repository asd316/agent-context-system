import { CopyIcon, XIcon } from 'lucide-react'
import { toast } from 'sonner'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { ScrollArea } from '@/components/ui/scroll-area'
import { CATEGORY_COLOR, type GNode, type Link } from '@/lib/graph'
import { cn } from '@/lib/utils'

export const dotColor = (n: GNode) => (n.virtual ? '#f3eee4' : CATEGORY_COLOR[n.raw.category])

function Section({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <section className="space-y-2 border-t border-border pt-4">
      <div className="eyebrow">{label}</div>
      {children}
    </section>
  )
}

function Links({ items, onPick }: { items: Link[]; onPick: (n: GNode) => void }) {
  return (
    <div className="-mx-2 flex flex-col">
      {items.map(({ node, origin }) => (
        <button
          key={node.idx}
          onClick={() => onPick(node)}
          className="group flex items-center gap-2.5 rounded-md px-2 py-1.5 text-left text-[13px] transition-colors hover:bg-accent"
        >
          <span className="size-1.5 shrink-0 rounded-full" style={{ background: dotColor(node), boxShadow: `0 0 6px ${dotColor(node)}` }} />
          <span className="min-w-0 flex-1 truncate text-foreground/85 group-hover:text-foreground">{node.raw.name}</span>
          {node.desc > 0 && <span className="font-mono text-[10px] text-muted-foreground">{node.desc}</span>}
          {origin === 'manual' && <span className="font-mono text-[10px] text-primary">手工</span>}
        </button>
      ))}
    </div>
  )
}

// 浏览器拒绝 Clipboard API 时(如嵌入式预览)退回旧接口
function legacyCopy(text: string) {
  const ta = document.createElement('textarea')
  ta.value = text
  ta.style.cssText = 'position:fixed;opacity:0'
  document.body.append(ta)
  ta.select()
  let ok = false
  try { ok = document.execCommand('copy') } catch { /* 忽略 */ }
  ta.remove()
  return ok
}

export function DetailPanel({ node, onPick, onClose }: { node: GNode | null; onPick: (n: GNode) => void; onClose: () => void }) {
  const d = node?.raw
  const copy = async (text: string) => {
    const ok = await navigator.clipboard?.writeText(text).then(() => true, () => false) || legacyCopy(text)
    if (ok) toast.success('已复制路径')
    else toast.error('复制失败')
  }
  return (
    <aside
      className={cn(
        'glass absolute top-16 right-4 bottom-4 z-20 flex w-[360px] flex-col rounded-xl shadow-2xl shadow-black/60 transition-[translate,opacity] duration-300 ease-[cubic-bezier(0.32,0.72,0,1)]',
        node ? 'translate-x-0 opacity-100' : 'pointer-events-none translate-x-6 opacity-0',
      )}
    >
      {node && d && (
        <>
          <div className="flex items-start gap-3 px-5 pt-5 pb-4">
            <div className="min-w-0 flex-1 space-y-2">
              <div className="flex items-center gap-2">
                <span className="size-2 rounded-full" style={{ background: dotColor(node), boxShadow: `0 0 8px ${dotColor(node)}` }} />
                <span className="eyebrow">{node.virtual ? '中心' : node.depth === 0 ? `${d.category} · 根` : d.category}</span>
              </div>
              <h2 className="text-lg leading-snug font-semibold break-all">{d.name}</h2>
              {(d.pending || d.missing) && (
                <div className="flex gap-1.5">
                  {d.pending && <Badge variant="outline" className="border-primary/40 text-primary">待确认</Badge>}
                  {d.missing && <Badge variant="destructive">路径失效</Badge>}
                </div>
              )}
            </div>
            <Button variant="ghost" size="icon-sm" onClick={onClose} aria-label="关闭">
              <XIcon />
            </Button>
          </div>
          <ScrollArea className="min-h-0 flex-1">
            <div className="space-y-4 px-5 pb-5">
              {!node.virtual && (
                <p className={cn('text-sm leading-relaxed', d.hier_summary ? 'text-foreground/85' : 'text-muted-foreground')}>
                  {d.hier_summary || '暂无摘要'}
                </p>
              )}
              {d.content_summary && (
                <Section label="内容">
                  <p className="text-[13px] leading-relaxed text-muted-foreground">{d.content_summary}</p>
                </Section>
              )}
              {d.path && (
                <Section label={d.path_kind === 'file' ? '文件' : '目录'}>
                  <div className="flex items-start gap-2">
                    <code className="min-w-0 flex-1 font-mono text-[12px] leading-relaxed break-all text-foreground/75">{d.path}</code>
                    <Button variant="ghost" size="icon-xs" onClick={() => copy(d.path!)} aria-label="复制路径">
                      <CopyIcon />
                    </Button>
                  </div>
                  {(d.status || d.modified_at) && (
                    <div className="flex gap-4 font-mono text-[11px] text-muted-foreground">
                      {d.status && <span>{d.status}</span>}
                      {d.modified_at && <span>{d.modified_at.replace('T', ' ').slice(0, 16)}</span>}
                    </div>
                  )}
                </Section>
              )}
              {!node.virtual && d.category === 'Skill' && (
                <a href={`#/organize?find=${encodeURIComponent(d.name)}`} className="inline-flex text-[12px] text-primary hover:underline">
                  在 Organize 中查看
                </a>
              )}
              {!node.virtual && (
                <div className="font-mono text-[11px] text-muted-foreground">
                  {d.hits > 0 ? `被 agent 读取 ${d.hits} 次 · 最近 ${d.last_at?.replace('T', ' ').slice(0, 16)}` : '尚未被 agent 读取'}
                </div>
              )}
              {node.parents.length > 0 && (
                <Section label={`上级 ${node.parents.length}`}>
                  <Links items={node.parents} onPick={onPick} />
                </Section>
              )}
              {node.children.length > 0 && (
                <Section label={`下级 ${node.children.length}`}>
                  <Links items={node.children} onPick={onPick} />
                </Section>
              )}
            </div>
          </ScrollArea>
        </>
      )}
    </aside>
  )
}
