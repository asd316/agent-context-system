import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'
import { toast } from 'sonner'
import { api, type ApplyStatus, type Changes } from '@/lib/api'

interface Status {
  changes: Changes | null
  apply: ApplyStatus | null
  pendingInbox: number
  /** 未应用改动的数量: 文件 + Profile 已改的 Client */
  unapplied: number
  /** 应用期间禁止编辑 */
  locked: boolean
  refresh: () => Promise<void>
  startApply: () => Promise<void>
  discard: () => Promise<void>
}

const Ctx = createContext<Status | null>(null)

export function StatusProvider({ children }: { children: React.ReactNode }) {
  const [changes, setChanges] = useState<Changes | null>(null)
  const [apply, setApply] = useState<ApplyStatus | null>(null)
  const [pendingInbox, setPendingInbox] = useState(0)
  const timer = useRef<number | null>(null)

  const refresh = useCallback(async () => {
    const [c, a, inbox] = await Promise.all([
      api.changes().catch(() => null), api.applyStatus().catch(() => null), api.inbox(['pending']).catch(() => []),
    ])
    setChanges(c)
    setApply(a)
    setPendingInbox(inbox.length)
  }, [])

  const poll = useCallback(() => {
    if (timer.current) window.clearTimeout(timer.current)
    const tick = async () => {
      const a = await api.applyStatus().catch(() => null)
      setApply(a)
      if (a?.state === 'running') timer.current = window.setTimeout(tick, 500)
      else {
        await refresh()
        window.dispatchEvent(new Event('console:applied'))
      }
    }
    timer.current = window.setTimeout(tick, 300)
  }, [refresh])

  useEffect(() => {
    refresh().then(() => api.applyStatus().then((a) => a.state === 'running' && poll(), () => {}))
    const onFocus = () => refresh()
    window.addEventListener('focus', onFocus)
    return () => window.removeEventListener('focus', onFocus)
  }, [refresh, poll])

  const startApply = useCallback(async () => {
    try {
      setApply(await api.apply())
      poll()
    } catch (e) {
      toast.error(`应用未开始 · ${e instanceof Error ? e.message : e}`)
      refresh()
    }
  }, [poll, refresh])

  const discard = useCallback(async () => {
    try {
      const r = await api.discard()
      toast.success(`已撤销 ${r.discarded.length} 处未应用改动`)
    } catch (e) {
      toast.error(`撤销失败 · 改动仍在 · ${e instanceof Error ? e.message : e}`)
    }
    await refresh()
    window.dispatchEvent(new Event('console:applied'))
  }, [refresh])

  const value = useMemo<Status>(() => ({
    changes, apply, pendingInbox,
    unapplied: (changes?.files.length ?? 0) + (changes?.clients.length ?? 0),
    locked: apply?.state === 'running',
    refresh, startApply, discard,
  }), [changes, apply, pendingInbox, refresh, startApply, discard])
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

export function useStatus() {
  const s = useContext(Ctx)
  if (!s) throw new Error('useStatus 须在 StatusProvider 内')
  return s
}

/** 页面数据: 首次加载、每次写入后、应用或撤销结束后重新取 */
export function useLoad<T>(load: () => Promise<T>) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const reload = useCallback(() => load().then((d) => { setData(d); setError(null) }, (e) => setError(String(e.message ?? e))), [load])
  useEffect(() => {
    reload()
    window.addEventListener('console:applied', reload)
    return () => window.removeEventListener('console:applied', reload)
  }, [reload])
  return { data, error, reload }
}
