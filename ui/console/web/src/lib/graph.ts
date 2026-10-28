export type Category = 'Reference' | 'Knowledge' | 'Project' | 'Skill'

export interface RawNode {
  id: number
  name: string
  category: Category
  hier_summary: string
  content_summary: string | null
  path: string | null
  path_kind: 'file' | 'dir' | null
  status: string | null
  modified_at: string
  pending: boolean
  missing: boolean
  /** 被 agent 读取(get/children/ancestors)的累计次数 */
  hits: number
  last_at: string | null
}
interface RawEdge { parent: number; child: number; origin: 'scan' | 'manual' }
export interface RawGraph { nodes: RawNode[]; edges: RawEdge[]; db_mtime: number }

export interface Link { node: GNode; origin: RawEdge['origin'] }

export interface GNode {
  raw: RawNode
  idx: number
  parents: Link[]
  children: Link[]
  depth: number
  /** 子孙数量,决定星的大小 */
  desc: number
  /** 0..1,决定亮度:根与枢纽亮,叶子是暗尘 */
  rank: number
  /** 0..1,访问热度:log(1+hits) 相对全库最大值。0 = 从未被读取 */
  glow: number
  color: [number, number, number]
  x: number; y: number; z: number
  virtual?: boolean
}

export interface Graph {
  nodes: GNode[]
  edges: [GNode, GNode][]
  me: GNode
  byId: Map<number, GNode>
}

// 三个星系的主色:低饱和、同一明度带,在辉光下互不打架
export const CATEGORY_COLOR: Record<Category, string> = {
  Skill: '#ff7a59',
  Knowledge: '#4fd1b8',
  Project: '#a08cff',
  Reference: '#7eb6ff',
}
export const CATEGORIES = Object.keys(CATEGORY_COLOR) as Category[]
const MOON = '#f3eee4'

export const hash = (s: string) => {
  let h = 2166136261
  for (const c of s) h = Math.imul(h ^ c.charCodeAt(0), 16777619)
  return ((h >>> 0) % 10000) / 10000
}

function rgb(hex: string): [number, number, number] {
  const n = parseInt(hex.slice(1), 16)
  return [(n >> 16) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255]
}

/** 节点颜色: 星系主色,按子树轻微偏色;越深越向主色饱和、越浅越接近白(恒星核) */
function starColor(n: GNode): [number, number, number] {
  const base = rgb(n.virtual ? MOON : CATEGORY_COLOR[n.raw.category] ?? '#9a948d')
  const branch = n.parents[0]?.node
  const jitter = branch ? (hash(String(branch.raw.id)) - 0.5) * 0.18 : 0
  const white = n.virtual ? 1 : n.depth === 0 ? 0.55 : n.desc > 0 ? 0.22 : 0
  return base.map((c, i) => {
    const shifted = Math.min(1, Math.max(0, c + jitter * (i === 1 ? -1 : 1)))
    return shifted + (1 - shifted) * white
  }) as [number, number, number]
}

export async function fetchGraph(): Promise<RawGraph> {
  const res = await fetch('/api/graph')
  if (!res.ok) throw new Error(`/api/graph ${res.status}`)
  return res.json()
}

/** 结构与配色;坐标由 lib/layout.ts 另算(在 Worker 里,结果按结构缓存) */
export function buildGraph(data: RawGraph): Graph {
  const byId = new Map<number, GNode>()
  const nodes: GNode[] = data.nodes.map((raw, idx) => {
    const n: GNode = { raw, idx, parents: [], children: [], depth: 0, desc: 0, rank: 0, glow: 0, color: [1, 1, 1], x: 0, y: 0, z: 0 }
    byId.set(raw.id, n)
    return n
  })
  const edges: [GNode, GNode][] = []
  for (const e of data.edges) {
    const p = byId.get(e.parent), c = byId.get(e.child)
    if (!p || !c) continue
    p.children.push({ node: c, origin: e.origin })
    c.parents.push({ node: p, origin: e.origin })
    edges.push([p, c])
  }

  const roots = nodes.filter((n) => n.parents.length === 0)
  // 深度: 到任一根的最短距离
  const seen = new Set<GNode>(roots)
  let frontier = roots
  for (let d = 1; frontier.length; d++) {
    const next: GNode[] = []
    for (const n of frontier) for (const { node: c } of n.children) {
      if (seen.has(c)) continue
      seen.add(c); c.depth = d; next.push(c)
    }
    frontier = next
  }
  // 子孙数量: DAG 里同一子孙可经多条路径到达,只计一次。
  // 逐节点迭代 DFS,访问标记共用一块 Int32Array(记"被谁访问过"),内存 O(n)、不递归;
  // 不再为每个节点保存完整的子孙集合(那在深层结构里接近平方级内存)
  const mark = new Int32Array(nodes.length).fill(-1)
  const stack: GNode[] = []
  for (const n of nodes) {
    let count = 0
    stack.push(n)
    while (stack.length) {
      for (const { node: c } of stack.pop()!.children) {
        if (mark[c.idx] === n.idx) continue
        mark[c.idx] = n.idx
        count++
        stack.push(c)
      }
    }
    n.desc = count
  }

  const me: GNode = {
    raw: { id: 0, name: '我', category: 'Skill', hier_summary: '', content_summary: null, path: null, path_kind: null, status: null, modified_at: '', pending: false, missing: false, hits: 0, last_at: null },
    idx: nodes.length, parents: [], children: roots.map((node) => ({ node, origin: 'scan' })),
    depth: -1, desc: nodes.length, rank: 1, glow: 0, color: [1, 1, 1], x: 0, y: 0, z: 0, virtual: true,
  }
  nodes.push(me)
  byId.set(0, me)

  // 访问热度: 对数缩放,个别高频节点不会把其他节点压成一片暗
  // 不用 Math.max(...数组): 参数个数有上限,几十万节点时会爆栈
  let maxHits = 0
  for (const n of nodes) if (n.raw.hits > maxHits) maxHits = n.raw.hits
  const maxLog = Math.log1p(maxHits)
  for (const n of nodes) {
    n.glow = maxLog > 0 ? Math.log1p(n.raw.hits) / maxLog : 0
    n.rank = n.virtual ? 1 : n.depth === 0 ? 1 : n.desc === 0 ? 0.18 : Math.min(0.85, 0.35 + 0.12 * Math.log2(n.desc + 1))
    // 结构亮度之上叠加访问热度: 从未被读取的星与此前完全一致
    if (!n.virtual) n.rank = Math.min(1, n.rank + 0.55 * n.glow)
    n.color = starColor(n)
  }
  return { nodes, edges, me, byId }
}
