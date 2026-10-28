import { copyText } from '@/lib/format'
import { cn } from '@/lib/utils'

/** ID 一律等宽显示,点击即复制 */
export function IdChip({ id, className }: { id: string; className?: string }) {
  return (
    <button
      onClick={(e) => { e.stopPropagation(); copyText(id, `已复制 ${id}`) }}
      title="点击复制"
      className={cn('rounded bg-secondary px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground hover:text-foreground', className)}
    >
      {id}
    </button>
  )
}
