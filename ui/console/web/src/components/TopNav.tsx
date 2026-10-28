import { HexagonIcon } from 'lucide-react'
import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import type { ApplyStatus } from '@/lib/api'
import { formatTime } from '@/lib/format'
import { useStatus } from '@/lib/status'
import { cn } from '@/lib/utils'

export const PAGES = ['explore', 'organize', 'profiles', 'lessons', 'inbox'] as const
export type Page = (typeof PAGES)[number]
const LABEL: Record<Page, string> = { explore: 'Explore', organize: 'Organize', profiles: 'Profiles', lessons: 'Lessons', inbox: 'Inbox' }

export function TopNav({ page }: { page: Page }) {
  const { pendingInbox } = useStatus()
  return (
    <nav
      className={cn(
        'z-30 flex h-12 shrink-0 items-center gap-6 border-b border-border px-4',
        page === 'explore' ? 'absolute inset-x-0 top-0 bg-background/55 backdrop-blur-md' : 'bg-card',
      )}
    >
      <div className="flex items-center gap-2">
        <HexagonIcon className="size-4 text-primary" strokeWidth={1.75} />
        <span className="font-mono text-[13px] font-semibold tracking-[0.18em]">AGENT CONTEXT</span>
      </div>
      <div className="flex h-full items-stretch gap-1">
        {PAGES.map((p) => (
          <a
            key={p}
            href={`#/${p}`}
            aria-current={p === page ? 'page' : undefined}
            className={cn(
              'flex items-center gap-1.5 border-b-2 px-2.5 text-sm transition-colors',
              p === page ? 'border-primary text-foreground' : 'border-transparent text-muted-foreground hover:text-foreground',
            )}
          >
            {LABEL[p]}
            {p === 'inbox' && pendingInbox > 0 && (
              <span className="rounded-full bg-primary px-1.5 font-mono text-[10px] leading-4 font-semibold text-primary-foreground">{pendingInbox}</span>
            )}
          </a>
        ))}
      </div>
      <div className="ml-auto">
        <ApplyBar />
      </div>
    </nav>
  )
}

const STEPS: { key: ApplyStatus['steps'][number]['step']; label: string }[] = [
  { key: 'build', label: 'Build' }, { key: 'activate', label: 'Activate' }, { key: 'commit', label: 'commit' }, { key: 'push', label: 'push' },
]

function StepChips({ apply }: { apply: ApplyStatus }) {
  return (
    <div className="flex items-center gap-1 font-mono text-[11px]" aria-label="应用进度">
      {STEPS.map(({ key, label }, i) => {
        const done = apply.steps.find((s) => s.step === key)
        const current = apply.state === 'running' && !done && i === apply.steps.length
        return (
          <span
            key={key}
            className={cn(
              'rounded px-1.5 py-0.5',
              done?.ok && 'bg-primary/15 text-primary',
              done && !done.ok && 'bg-destructive/15 text-destructive',
              current && 'animate-pulse bg-secondary text-foreground',
              !done && !current && 'text-muted-foreground/50',
            )}
          >
            {label}
          </span>
        )
      })}
    </div>
  )
}

function ApplyBar() {
  const { apply, unapplied, changes, startApply, discard } = useStatus()
  const [confirm, setConfirm] = useState(false)
  const [detail, setDetail] = useState(false)
  if (!apply) return null
  const running = apply.state === 'running'
  const failed = apply.state === 'failed'
  const unpushed = apply.state === 'done' && !apply.pushed && !!apply.unpushed_reason
  const failedStep = apply.steps.find((s) => !s.ok)

  return (
    <div className="flex items-center gap-3 text-sm">
      {running ? (
        <StepChips apply={apply} />
      ) : (
        <>
          {failed && (
            <button onClick={() => setDetail(true)} className="font-medium text-destructive hover:underline">
              应用失败 · {failedStep ? STEPS.find((s) => s.key === failedStep.step)?.label : '已中断'} · 详情
            </button>
          )}
          {unpushed && !failed && (
            <button onClick={() => setDetail(true)} className="text-destructive hover:underline">未推送 · 详情</button>
          )}
          {unapplied > 0 ? (
            <span><span className="font-mono font-semibold text-primary">{unapplied}</span> 处未应用</span>
          ) : (
            !failed && (
              <span className="text-muted-foreground" title={apply.at ? '已开着的 agent 会话仍用旧规则,新开的会话才读到' : undefined}>
                {apply.at ? `已应用 · ${formatTime(apply.at)} · 新会话生效` : '尚未应用'}
              </span>
            )
          )}
          {unapplied > 0 && (
            <Button variant="ghost" size="sm" onClick={() => setConfirm(true)}>撤销</Button>
          )}
        </>
      )}
      <Button size="sm" variant={unapplied > 0 || failed ? 'default' : 'outline'} disabled={running} onClick={startApply}>
        {running ? '应用中…' : '应用'}
      </Button>

      <Dialog open={confirm} onOpenChange={setConfirm}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>撤销 {unapplied} 处未应用改动?</DialogTitle>
            <DialogDescription>源文件恢复到上次应用时的状态,不能再找回。</DialogDescription>
          </DialogHeader>
          <ul className="max-h-48 overflow-auto font-mono text-[12px] text-muted-foreground">
            {changes?.files.map((f) => <li key={f.path}>{f.status} {f.path}</li>)}
            {changes?.clients.map((c) => <li key={c.client}>Profile {c.client}: {c.active_profile} → {c.profile}</li>)}
          </ul>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setConfirm(false)}>取消</Button>
            <Button variant="destructive" onClick={() => { setConfirm(false); discard() }}>撤销</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={detail} onOpenChange={setDetail}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>{failed ? '应用停在一半' : 'push 失败'}</DialogTitle>
            <DialogDescription>
              {failed
                ? `${failedStep?.step ?? '应用'} 失败 · 之前的步骤已生效,没有回滚 · 修好原因后再点应用,或让 agent 处理`
                : 'push 失败 · 本机已生效,远端未更新 · 让 agent 处理'}
            </DialogDescription>
          </DialogHeader>
          <ol className="space-y-1.5 font-mono text-[12px]">
            {apply.steps.map((s) => (
              <li key={s.step} className={cn('flex gap-2', !s.ok && 'text-destructive')}>
                <span className="w-16 shrink-0">{s.step}</span>
                <span className="break-all">{s.detail}</span>
              </li>
            ))}
            {failed && !failedStep && <li className="text-destructive">上次应用被中断 (进程已不在)</li>}
          </ol>
          {apply.at && <p className="text-xs text-muted-foreground">开始于 {formatTime(apply.at)}</p>}
        </DialogContent>
      </Dialog>
    </div>
  )
}
