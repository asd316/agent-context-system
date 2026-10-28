import { forceLink, forceManyBody, forceSimulation } from 'd3-force-3d'
import { buildGraph, hash, type GNode, type Graph, type RawGraph } from '@/lib/graph'

/** 改了布局的任何参数都要加一,让浏览器里缓存的旧坐标失效(并强制一次全量布局) */
const LAYOUT_VERSION = 2
/** 力导向在 3D 里算,然后把 y 压扁成略带厚度的星盘 */
const FLATTEN = 0.42
/** 斥力的作用距离 */
const CHARGE_RANGE = 260
/** 增量布局时,离要动的节点这么近的旧节点才放进模拟(固定不动,只提供斥力)。
 *  斥力随距离衰减,更远的旧节点对新节点落位影响很小,却会让模拟规模接近全库 */
const INCR_RADIUS = 90
const FULL_TICKS = 400
const INCR_TICKS = 120
/** 需要移动的节点超过这个比例就不做增量,直接全量重排 */
const INCR_MAX_SHARE = 0.2

/** 上一次布局的结果,存在 IndexedDB 里(二进制,不占主线程序列化,也没有 localStorage 的 5MB 配额) */
export interface LayoutRecord {
  version: number
  /** 结构指纹,与当前完全一致时直接复用坐标 */
  key: string
  /** 与 pos 一一对应的节点 id(含「我」= 0) */
  ids: Int32Array
  /** 边,扁平的 [parent, child, parent, child, ...];用来找出新增的边 */
  edges: Int32Array
  /** 压扁后的 xyz */
  pos: Float32Array
}
export interface LayoutResult { pos: Float32Array; mode: 'full' | 'incremental' }

/** 只取决定布局的输入: 节点 id 与名称(叶子边长用到名称)、边。摘要、状态、访问次数变化不影响坐标 */
function layoutKey(data: RawGraph) {
  const parts = [String(LAYOUT_VERSION)]
  for (const n of data.nodes) parts.push(`${n.id}:${n.name}`)
  for (const e of data.edges) parts.push(`${e.parent}>${e.child}`)
  let h = 2166136261
  for (const c of parts.join('|')) h = Math.imul(h ^ c.charCodeAt(0), 16777619)
  return `${parts.length}:${(h >>> 0).toString(36)}`
}

type SimNode = { x?: number; y?: number; z?: number; fx?: number; fy?: number; fz?: number; desc: number }
type SimLink = { source: SimNode; target: SimNode; len: number; k: number }

/** 父子边的弹簧;keep 为假的边不进模拟(增量布局时两端都固定的边没有作用) */
function makeLinks(nodes: GNode[], sim: SimNode[], keep: (p: GNode, c: GNode) => boolean = () => true): SimLink[] {
  const links: SimLink[] = []
  for (const n of nodes) for (const { node: c } of n.children) {
    if (!keep(n, c)) continue
    const fromMe = !!n.virtual
    links.push({
      source: sim[n.idx], target: sim[c.idx],
      len: fromMe ? 150 : 10 + 7 * Math.sqrt(c.desc + 1) + (c.desc === 0 ? 6 * hash(c.raw.name) : 0),
      k: fromMe ? 0.6 : 1 / Math.min(n.children.length, Math.max(1, c.parents.length * 2)),
    })
  }
  return links
}

/** reheat 给定时: 从这个 alpha 起步,在 ticks 步内衰减到 0.001(增量用);不给则用 d3 默认节奏(全量,与旧版一致) */
function simulate(sim: SimNode[], links: SimLink[], ticks: number, reheat?: number) {
  let seed = 7
  const rand = () => ((seed = (seed * 16807) % 2147483647) - 1) / 2147483646
  const s = forceSimulation(sim, 3).randomSource(rand)
  if (reheat) s.alpha(reheat).alphaDecay(1 - Math.pow(0.001 / reheat, 1 / ticks))
  s
    .force('link', forceLink(links).distance((l: SimLink) => l.len).strength((l: SimLink) => l.k).iterations(2))
    .force('charge', forceManyBody().strength((s: SimNode) => -(6 + 5 * Math.sqrt(s.desc))).distanceMax(CHARGE_RANGE))
    .stop()
    .tick(ticks)
}

function output(sim: SimNode[]): Float32Array {
  const out = new Float32Array(sim.length * 3)
  sim.forEach((s, i) => out.set([s.x ?? 0, (s.y ?? 0) * FLATTEN, s.z ?? 0], i * 3))
  return out
}

/** 全量: 3D 力导向,然后压扁成星盘 */
function fullLayout({ nodes, me }: Graph): Float32Array {
  const sim: SimNode[] = nodes.map((n) => ({ desc: n.desc }))
  sim[me.idx].fx = sim[me.idx].fy = sim[me.idx].fz = 0
  // 根节点按等角起步,避免三个星系挤成一团
  const roots = me.children.map((l) => l.node)
  roots.forEach((r, i) => {
    const a = (i / roots.length) * Math.PI * 2
    Object.assign(sim[r.idx], { x: Math.cos(a) * 150, y: 0, z: Math.sin(a) * 150 })
  })
  simulate(sim, makeLinks(nodes, sim), FULL_TICKS)
  return output(sim)
}

/** 增量: 旧节点钉在原位,只让新节点(和挂上新边的节点)在附近落位。
 *  模拟里只放它们、它们的邻居、以及斥力范围内的旧节点,代价与改动量相关,而不是与全库规模相关。
 *  返回 null 表示改动太大,应改做全量 */
function incrementalLayout(graph: Graph, prev: LayoutRecord): Float32Array | null {
  const { nodes, me } = graph
  const at = new Map<number, number>()
  prev.ids.forEach((id, i) => at.set(id, i))
  const oldEdges = new Set<string>()
  for (let i = 0; i < prev.edges.length; i += 2) oldEdges.add(`${prev.edges[i]}>${prev.edges[i + 1]}`)

  const sim: SimNode[] = nodes.map((n) => ({ desc: n.desc }))
  const fresh: GNode[] = []
  for (const n of nodes) {
    const s = sim[n.idx]
    if (n.virtual) { s.x = s.y = s.z = s.fx = s.fy = s.fz = 0; continue }
    const i = at.get(n.raw.id)
    if (i === undefined) { fresh.push(n); continue }
    s.x = s.fx = prev.pos[i * 3]
    s.y = s.fy = prev.pos[i * 3 + 1] / FLATTEN
    s.z = s.fz = prev.pos[i * 3 + 2]
  }
  // 挂上新边的旧节点: 从原位出发,但解除固定,让它向新的父节点靠
  const moving = new Set<GNode>(fresh)
  for (const n of nodes) for (const { node: c } of n.children) {
    if (!n.virtual && !moving.has(c) && !oldEdges.has(`${n.raw.id}>${c.raw.id}`)) moving.add(c)
  }
  if (moving.size === 0) return output(sim)
  if (moving.size > (nodes.length - 1) * INCR_MAX_SHARE) return null
  for (const n of moving) { const s = sim[n.idx]; s.fx = s.fy = s.fz = undefined }

  // 新节点由浅到深落位: 放在已就位的父/子节点的平均位置附近;都没有就放在外圈
  let seed = 13
  const rand = () => ((seed = (seed * 16807) % 2147483647) - 1) / 2147483646
  const jitter = () => (rand() - 0.5) * 30
  for (const n of [...fresh].sort((a, b) => a.depth - b.depth)) {
    const anchors = [...n.parents, ...n.children].map((l) => sim[l.node.idx]).filter((s) => s.x !== undefined)
    const s = sim[n.idx]
    if (anchors.length) {
      const avg = (k: 'x' | 'y' | 'z') => anchors.reduce((t, a) => t + a[k]!, 0) / anchors.length
      s.x = avg('x') + jitter(); s.y = avg('y') + jitter(); s.z = avg('z') + jitter()
    } else {
      const a = rand() * Math.PI * 2
      s.x = Math.cos(a) * 150; s.y = jitter(); s.z = Math.sin(a) * 150
    }
  }

  // 参与模拟的节点: 要动的、它们的邻居、INCR_RADIUS 内的旧节点(3D 网格分桶后逐个量距离,O(n))
  const active = new Set<GNode>(moving)
  for (const n of moving) {
    for (const l of [...n.parents, ...n.children]) active.add(l.node)
    if (n.depth === 0) active.add(me)
  }
  const cell = (v: number) => Math.floor(v / INCR_RADIUS)
  const grid = new Map<string, SimNode[]>()
  for (const n of moving) {
    const s = sim[n.idx], k = `${cell(s.x!)},${cell(s.y!)},${cell(s.z!)}`
    grid.set(k, [...(grid.get(k) ?? []), s])
  }
  for (const n of nodes) {
    if (active.has(n)) continue
    const s = sim[n.idx], cx = cell(s.x!), cy = cell(s.y!), cz = cell(s.z!)
    near: for (let dx = -1; dx <= 1; dx++) for (let dy = -1; dy <= 1; dy++) for (let dz = -1; dz <= 1; dz++) {
      for (const m of grid.get(`${cx + dx},${cy + dy},${cz + dz}`) ?? []) {
        if (Math.hypot(s.x! - m.x!, s.y! - m.y!, s.z! - m.z!) <= INCR_RADIUS) { active.add(n); break near }
      }
    }
  }
  const links = makeLinks(nodes, sim, (p, c) => moving.has(p) || moving.has(c))
  simulate([...active].map((n) => sim[n.idx]), links, INCR_TICKS, 0.5)
  return output(sim)
}

/** 在 Worker 里调用。prev 是上一次的布局(版本一致时),有它就先试增量 */
export function computeLayout(graph: Graph, prev: LayoutRecord | null): LayoutResult {
  const pos = prev && incrementalLayout(graph, prev)
  return pos ? { pos, mode: 'incremental' } : { pos: fullLayout(graph), mode: 'full' }
}

function applyLayout(graph: Graph, pos: ArrayLike<number>) {
  for (const n of graph.nodes) [n.x, n.y, n.z] = [pos[n.idx * 3], pos[n.idx * 3 + 1], pos[n.idx * 3 + 2]]
}

// —— 缓存: IndexedDB 里只存一条「最近一次」,读写失败(隐私模式等)时退回每次计算 ——
const DB_NAME = 'star-layout'
const STORE = 'layout'
const RECORD = 'latest'

function openDb(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 1)
    req.onupgradeneeded = () => req.result.createObjectStore(STORE)
    req.onsuccess = () => resolve(req.result)
    req.onerror = () => reject(req.error)
  })
}

async function readRecord(): Promise<LayoutRecord | null> {
  try {
    const db = await openDb()
    try {
      return await new Promise((resolve, reject) => {
        const req = db.transaction(STORE).objectStore(STORE).get(RECORD)
        req.onsuccess = () => resolve(req.result ?? null)
        req.onerror = () => reject(req.error)
      })
    } finally { db.close() }
  } catch { return null }
}

async function writeRecord(rec: LayoutRecord) {
  try {
    const db = await openDb()
    try {
      await new Promise<void>((resolve, reject) => {
        const tx = db.transaction(STORE, 'readwrite')
        tx.objectStore(STORE).put(rec, RECORD)
        tx.oncomplete = () => resolve()
        tx.onerror = () => reject(tx.error)
      })
    } finally { db.close() }
  } catch { /* 存不下就下次重算 */ }
}

/** 旧版本把坐标存在 localStorage 的 star-layout:* 里,清掉 */
function dropLegacyCache() {
  try {
    for (let i = localStorage.length - 1; i >= 0; i--) {
      const k = localStorage.key(i)
      if (k?.startsWith('star-layout:')) localStorage.removeItem(k)
    }
  } catch { /* 无所谓 */ }
}

/** 建图并布局: 结构没变时直接用缓存坐标;变了就在 Worker 里增量(或全量)计算,主线程不卡 */
export async function layoutGraph(data: RawGraph): Promise<Graph> {
  dropLegacyCache()
  const graph = buildGraph(data)
  const key = layoutKey(data)
  const cached = await readRecord()
  const prev = cached?.version === LAYOUT_VERSION ? cached : null
  if (prev?.key === key && prev.pos.length === graph.nodes.length * 3) {
    applyLayout(graph, prev.pos)
    return graph
  }
  const worker = new Worker(new URL('./layout.worker.ts', import.meta.url), { type: 'module' })
  const { pos } = await new Promise<LayoutResult>((resolve, reject) => {
    worker.onmessage = (e: MessageEvent<LayoutResult>) => resolve(e.data)
    worker.onerror = (e) => reject(new Error(`布局失败: ${e.message}`))
    worker.postMessage({ data, prev })
  }).finally(() => worker.terminate())
  applyLayout(graph, pos)
  const edges = new Int32Array(data.edges.length * 2)
  data.edges.forEach((e, i) => { edges[i * 2] = e.parent; edges[i * 2 + 1] = e.child })
  void writeRecord({ version: LAYOUT_VERSION, key, ids: Int32Array.from(graph.nodes, (n) => n.raw.id), edges, pos })
  return graph
}
