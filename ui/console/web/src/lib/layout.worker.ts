import { buildGraph, type RawGraph } from '@/lib/graph'
import { computeLayout, type LayoutRecord } from '@/lib/layout'

self.onmessage = (e: MessageEvent<{ data: RawGraph; prev: LayoutRecord | null }>) => {
  const res = computeLayout(buildGraph(e.data.data), e.data.prev)
  self.postMessage(res, { transfer: [res.pos.buffer] })
}
