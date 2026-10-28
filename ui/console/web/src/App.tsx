import { lazy, Suspense, useEffect, useState } from 'react'
import { PAGES, TopNav, type Page } from '@/components/TopNav'
import { Toaster } from '@/components/ui/sonner'
import Inbox from '@/pages/Inbox'
import Lessons from '@/pages/Lessons'
import Organize from '@/pages/Organize'
import Profiles from '@/pages/Profiles'
import { StatusProvider } from '@/lib/status'

// three.js 很重,只有 Explore 用到,单独分包
const Explore = lazy(() => import('@/pages/Explore'))

export interface Route { page: Page; query: URLSearchParams }

function parse(): Route {
  const [path, qs] = location.hash.replace(/^#\/?/, '').split('?')
  const page = (PAGES as readonly string[]).includes(path) ? (path as Page) : 'explore'
  return { page, query: new URLSearchParams(qs ?? '') }
}

export default function App() {
  const [route, setRoute] = useState(parse)
  useEffect(() => {
    const on = () => setRoute(parse())
    window.addEventListener('hashchange', on)
    return () => window.removeEventListener('hashchange', on)
  }, [])

  return (
    <StatusProvider>
      <div className="relative flex h-full flex-col bg-background">
        <TopNav page={route.page} />
        <main className="relative min-h-0 flex-1">
          {route.page === 'explore' && (
            <Suspense fallback={<div className="grid h-full place-items-center font-mono text-xs text-muted-foreground">…</div>}>
              <Explore />
            </Suspense>
          )}
          {route.page === 'organize' && <Organize find={route.query.get('find')} />}
          {route.page === 'profiles' && <Profiles />}
          {route.page === 'lessons' && <Lessons />}
          {route.page === 'inbox' && <Inbox />}
        </main>
      </div>
      <Toaster position="bottom-center" />
    </StatusProvider>
  )
}
