import { useCallback, useState } from 'react'
import { toast } from 'sonner'
import { IdChip } from '@/components/IdChip'
import { api, type ClientRow, type Profile, type ProfilePreview } from '@/lib/api'
import { formatBytes, writeFailed } from '@/lib/format'
import { useLoad, useStatus } from '@/lib/status'
import { cn } from '@/lib/utils'

const PROFILE_LABEL: Record<Profile, string> = { orchestrator: 'Orchestrator', executor: 'Executor' }
const KIND_ORDER = ['general', 'index', 'required-read', 'dispatch', 'maintenance']

export default function Profiles() {
  const load = useCallback(() => api.profiles(), [])
  const { data, error, reload } = useLoad(load)
  const status = useStatus()
  const [chosen, setChosen] = useState<Profile>('orchestrator')

  if (error) return <div className="grid h-full place-items-center font-mono text-xs text-muted-foreground">读取失败 · {error}</div>
  if (!data) return <div className="grid h-full place-items-center font-mono text-xs text-muted-foreground">…</div>
  const preview = data.profiles.find((p) => p.profile === chosen)!
  const pending = new Set(status.changes?.clients.map((c) => c.client))

  const pick = async (client: string, profile: Profile) => {
    try {
      await api.setProfile(client, profile)
      toast.success(`${client} 改用 ${PROFILE_LABEL[profile]} · 应用后,下次启动该 Client 时生效`)
    } catch (e) { writeFailed('切换', e) }
    await reload()
    status.refresh()
  }

  return (
    <div className="flex h-full flex-col gap-4 overflow-auto p-5">
      {status.locked && <div className="rounded-md bg-secondary px-3 py-2 text-xs text-muted-foreground">正在应用 · 暂时不能切换 · 应用结束后自动恢复</div>}
      <section className="rounded-xl border border-border bg-card">
        <div className="eyebrow px-4 pt-3 pb-2">激活表 · 每个 Client 选一个 Profile</div>
        <table className="w-full text-[13px]">
          <thead>
            <tr className="border-y border-border text-left text-xs text-muted-foreground">
              <th className="py-2 pl-4 font-normal">Client</th>
              {data.profiles.map((p) => (
                <th key={p.profile} className="w-44 px-3 font-normal">
                  <button
                    onClick={() => setChosen(p.profile)}
                    aria-pressed={chosen === p.profile}
                    className={cn('rounded px-2 py-1', chosen === p.profile ? 'bg-primary/15 text-primary' : 'hover:text-foreground')}
                  >
                    {PROFILE_LABEL[p.profile]}
                  </button>
                </th>
              ))}
              <th className="px-3 font-normal">规则文件</th>
              <th className="px-4 font-normal">常驻大小</th>
            </tr>
          </thead>
          <tbody>
            {data.clients.map((c) => (
              <tr key={c.client} className={cn('border-b border-border/60', !c.installed && 'text-muted-foreground/45')}>
                <td className="py-2.5 pl-4">
                  <span className="font-medium">{c.client}</span>
                  {!c.installed && <span className="ml-2 text-xs">本机未安装</span>}
                  {pending.has(c.client) && <span className="ml-2 text-xs text-primary">未应用 · 现在用 {c.active_profile}</span>}
                </td>
                {data.profiles.map((p) => (
                  <td key={p.profile} className="px-3">
                    <label className={cn('inline-flex items-center gap-2 px-2', c.installed && !status.locked ? 'cursor-pointer' : 'cursor-not-allowed')}>
                      <input
                        type="radio"
                        name={`profile-${c.client}`}
                        aria-label={`${c.client} 用 ${PROFILE_LABEL[p.profile]}`}
                        checked={c.profile === p.profile}
                        disabled={!c.installed || status.locked}
                        onChange={() => pick(c.client, p.profile)}
                        className="size-4 accent-[var(--primary)]"
                      />
                    </label>
                  </td>
                ))}
                <td className="px-3 font-mono text-[11px] text-muted-foreground">{c.rules_path}</td>
                <td className="px-4 font-mono text-[12px]">{c.installed ? formatBytes(c.current_bytes) : '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
      <ProfileView preview={preview} current={data.clients.filter((c) => c.installed && c.profile === chosen)} />
    </div>
  )
}

function ProfileView({ preview, current }: { preview: ProfilePreview; current: ClientRow[] }) {
  const groups = KIND_ORDER.map((k) => ({ kind: k, rules: preview.rules.filter((r) => r.kind === k).sort((a, b) => a.order - b.order) }))
    .filter((g) => g.rules.length)
  const manual = preview.skills.filter((s) => s.manual_only)
  const auto = preview.skills.filter((s) => !s.manual_only)
  return (
    <section className="grid min-h-0 flex-1 grid-cols-[1fr_1fr_240px] gap-4">
      <Panel title={`Rule block · ${PROFILE_LABEL[preview.profile]} · ${preview.rules.length}`}>
        {groups.map((g) => (
          <div key={g.kind} className="mb-3">
            <div className="mb-1 font-mono text-[11px] text-muted-foreground">{g.kind}</div>
            {g.rules.map((r) => (
              <div key={r.id} className="flex items-center gap-2 py-1 text-[13px]">
                <IdChip id={r.id} />
                <span className="min-w-0 flex-1 truncate">{r.title}</span>
                <span className="font-mono text-[11px] text-muted-foreground">{formatBytes(r.bytes)}</span>
              </div>
            ))}
          </div>
        ))}
        <p className="text-xs text-muted-foreground">只读。改 Rule block 请在 Inbox 提 Proposal 或让 agent 修改 shared/rules/。</p>
      </Panel>
      <Panel title={`要装的 skill · ${preview.skills.length}`}>
        {auto.map((s) => <SkillRow key={s.name} name={s.name} tier={s.tier} />)}
        {manual.length > 0 && (
          <>
            <div className="mt-3 mb-1 font-mono text-[11px] text-muted-foreground">Manual-only · 只供用户手动调用</div>
            {manual.map((s) => <SkillRow key={s.name} name={s.name} tier={s.tier} manual />)}
          </>
        )}
        {preview.skills.length === 0 && <p className="text-xs text-muted-foreground">没有 Core 或顶层 Router skill。到 Organize 把 Tier 改为 Core。</p>}
      </Panel>
      <Panel title="常驻上下文">
        {preview.error ? (
          <p className="text-sm text-destructive">无法估算 · {preview.error}</p>
        ) : (
          <>
            <div className="font-mono text-3xl font-semibold text-primary">{formatBytes(preview.rules_bytes)}</div>
            <p className="mt-1 text-xs text-muted-foreground">按当前 Shared config 估算的规则文件大小</p>
            <div className="mt-4 space-y-1">
              <div className="font-mono text-[11px] text-muted-foreground">现状</div>
              {current.map((c) => (
                <div key={c.client} className="flex justify-between text-[13px]">
                  <span>{c.client}</span>
                  <span className="font-mono">{formatBytes(c.current_bytes)}</span>
                </div>
              ))}
              {current.length === 0 && <p className="text-xs text-muted-foreground">没有 Client 选用这个 Profile</p>}
            </div>
          </>
        )}
      </Panel>
    </section>
  )
}

function SkillRow({ name, tier, manual }: { name: string; tier?: string; manual?: boolean }) {
  return (
    <div className="flex items-center gap-2 py-1 text-[13px]">
      <span className="min-w-0 flex-1 truncate">{name}</span>
      {manual && <span className="rounded border border-border px-1.5 text-[10px] text-muted-foreground">Manual-only</span>}
      {tier && <span className="font-mono text-[11px] text-muted-foreground">{tier === 'core' ? 'Core' : tier === 'router' ? 'Router' : 'Leaf'}</span>}
    </div>
  )
}

function Panel({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="flex min-h-56 flex-col rounded-xl border border-border bg-card">
      <div className="eyebrow px-4 pt-3 pb-2">{title}</div>
      <div className="min-h-0 flex-1 overflow-auto px-4 pb-4">{children}</div>
    </div>
  )
}
