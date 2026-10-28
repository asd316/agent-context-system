import { toast } from 'sonner'

const pad = (n: number) => String(n).padStart(2, '0')

/** Design-Principles 的时间格式: 今天 HH:MM / 昨天 HH:MM / MM-DD HH:MM */
export function formatTime(iso: string | null | undefined, now = new Date()): string {
  if (!iso) return ''
  const d = new Date(iso.length === 19 ? iso : iso.slice(0, 19))
  if (Number.isNaN(d.getTime())) return iso
  const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`
  const day = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime()
  const diff = Math.round((day(now) - day(d)) / 86400000)
  if (diff === 0) return `今天 ${hm}`
  if (diff === 1) return `昨天 ${hm}`
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${hm}`
}

export function formatBytes(n: number | null | undefined): string {
  if (n == null) return '—'
  return n < 1024 ? `${n} B` : `${(n / 1024).toFixed(1)} KB`
}

function legacyCopy(text: string) {
  const ta = document.createElement('textarea')
  ta.value = text
  ta.style.position = 'fixed'
  ta.style.opacity = '0'
  document.body.appendChild(ta)
  ta.select()
  const ok = document.execCommand('copy')
  ta.remove()
  return ok
}

export async function copyText(text: string, label = '已复制') {
  const ok = (await navigator.clipboard?.writeText(text).then(() => true, () => false)) || legacyCopy(text)
  if (ok) toast.success(label)
  else toast.error('复制失败 · 剪贴板不可用 · 手动选中文字复制')
  return ok
}

/** 写接口失败时的三段式提示: 发生了什么 · 现在是什么状态 · 下一步 */
export function writeFailed(what: string, e: unknown) {
  const msg = e instanceof Error ? e.message : String(e)
  toast.error(`${what}失败 · 改动未写入 · ${msg}`)
}
