import { OrbitControls } from '@react-three/drei'
import { Canvas, useFrame, useThree } from '@react-three/fiber'
import { Bloom, EffectComposer, Vignette } from '@react-three/postprocessing'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import * as THREE from 'three'
import type { OrbitControls as OrbitControlsImpl } from 'three-stdlib'
import { CATEGORIES, type Category, type GNode, type Graph } from '@/lib/graph'
import { cn } from '@/lib/utils'

export interface GalaxyProps {
  graph: Graph
  isVisible: (n: GNode) => boolean
  hovered: GNode | null
  selected: GNode | null
  /** 每次变化都让相机飞向 selected */
  flyKey: number
  onHover: (n: GNode | null) => void
  onSelect: (n: GNode | null) => void
}

/** Retina 屏 2x 的像素量是 1.5x 的 1.8 倍,星点与辉光肉眼几乎看不出差别 */
const DPR = Math.min(window.devicePixelRatio, 1.5)
/** 画质档位: 2 全效果;1 关星云光斑、辉光半分辨率且少几级模糊;0 再把像素比降到 1。
 *  自转期间帧间隔中位数超过 SLOW_FRAME_MS(低于约 40fps)就降一档,只降不升 */
type Quality = 0 | 1 | 2
const SLOW_FRAME_MS = 25
/** 无操作这么久后停止自转;画面静止时不再渲染,GPU 归零 */
const DRIFT_MS = 30_000
/** 装饰粒子与真实节点的比例。装饰只负责氛围,数量压在节点的同一量级,真实的星才读得出来 */
const CLOUD_PER_NODE = 2
/** 星系尘埃的总量上限: 节点再多,氛围层的绘制量也不再跟着涨 */
const CLOUD_MAX = 8000
const FAR_DUST = 1200
const NEBULA_PUFFS = 40

const STAR_VERT = /* glsl */ `
  attribute float size;
  attribute float alpha;
  attribute vec3 color;
  uniform float uPixelRatio;
  uniform float uTime;
  #ifdef FOCUS
  // lit: 0 普通、1 焦点的直接邻居、2 焦点本身。有焦点时(uFocus = 1)普通星压暗
  attribute float lit;
  uniform float uFocus;
  #endif
  varying vec3 vColor;
  varying float vAlpha;
  void main() {
    float a = alpha;
    float sz = size;
    #ifdef FOCUS
    // size = 0 表示被筛选隐藏;点精灵最小也会画 1px,所以亮度一并归零
    float vis = step(0.0001, size);
    if (uFocus > 0.5) a = lit > 1.5 ? 1.0 : lit > 0.5 ? max(alpha, 0.7) : alpha * 0.12;
    if (lit > 1.5) sz = max(size * 1.35, 16.0) * vis;
    a *= vis;
    #endif
    vec4 mv = modelViewMatrix * vec4(position, 1.0);
    gl_PointSize = sz * uPixelRatio * (700.0 / -mv.z);
    gl_Position = projectionMatrix * mv;
    // 暗星轻微闪烁,亮星稳定
    float tw = 1.0 - (1.0 - smoothstep(0.0, 0.5, a)) * 0.35 * (0.5 + 0.5 * sin(uTime * 1.3 + position.x * 0.37 + position.z * 0.21));
    vColor = color;
    vAlpha = a * tw;
  }
`
const STAR_FRAG = /* glsl */ `
  uniform float uSoft;
  uniform float uGain;
  varying vec3 vColor;
  varying float vAlpha;
  void main() {
    float d = length(gl_PointCoord - 0.5) * 2.0;
    if (d > 1.0) discard;
    float core = exp(-d * d * 22.0) * (1.0 - uSoft);
    float halo = exp(-d * d * mix(4.5, 2.2, uSoft)) * mix(0.32, 1.0, uSoft);
    gl_FragColor = vec4(vColor * (core * 1.8 + halo) * vAlpha * uGain, 1.0);
  }
`

function starMaterial(soft = 0, focus = false) {
  return new THREE.ShaderMaterial({
    uniforms: { uPixelRatio: { value: DPR }, uTime: { value: 0 }, uSoft: { value: soft }, uGain: { value: 1 }, uFocus: { value: 0 } },
    defines: focus ? { FOCUS: '' } : {},
    vertexShader: STAR_VERT,
    fragmentShader: STAR_FRAG,
    blending: THREE.AdditiveBlending,
    depthWrite: false,
    transparent: true,
  })
}

/** 连线: color 已乘可见性(0 = 隐藏),lit = 1 是连着焦点的边。亮度系数在这里算,焦点切换只改几个 lit */
const LINE_VERT = /* glsl */ `
  attribute vec3 color;
  attribute float lit;
  uniform float uFocus;
  varying vec3 vColor;
  #include <fog_pars_vertex>
  void main() {
    vColor = color * (lit > 0.5 ? 0.55 : mix(0.08, 0.012, uFocus));
    vec4 mvPosition = modelViewMatrix * vec4(position, 1.0);
    gl_Position = projectionMatrix * mvPosition;
    #include <fog_vertex>
  }
`
const LINE_FRAG = /* glsl */ `
  varying vec3 vColor;
  #include <fog_pars_fragment>
  void main() {
    gl_FragColor = vec4(vColor, 1.0);
    #include <fog_fragment>
  }
`

function lineMaterial() {
  return new THREE.ShaderMaterial({
    uniforms: { ...THREE.UniformsUtils.clone(THREE.UniformsLib.fog), uFocus: { value: 0 } },
    vertexShader: LINE_VERT,
    fragmentShader: LINE_FRAG,
    blending: THREE.AdditiveBlending,
    depthWrite: false,
    transparent: true,
    fog: true,
  })
}

/** 只上传改过的元素;改动占比大时整块上传 */
function uploadSome(attr: THREE.BufferAttribute, idx: number[]) {
  if (!idx.length) return
  if (idx.length * 8 > attr.count) attr.clearUpdateRanges()
  else for (const i of idx) attr.addUpdateRange(i * attr.itemSize, attr.itemSize)
  attr.needsUpdate = true
}

const starSize = (n: GNode) => (n.virtual ? 56 : n.depth === 0 ? 46 : (n.desc === 0 ? 4.5 : Math.min(22, 6 + 2.6 * Math.log2(n.desc + 1))) * (1 + 0.5 * n.glow))

let seed = 11
const rand = () => ((seed = (seed * 16807) % 2147483647) - 1) / 2147483646

function Stars({ graph, isVisible, hovered, selected }: Pick<GalaxyProps, 'graph' | 'isVisible' | 'hovered' | 'selected'>) {
  const { nodes, edges, me } = graph
  const invalidate = useThree((s) => s.invalidate)
  const mat = useMemo(() => starMaterial(0, true), [])
  const lineMat = useMemo(() => lineMaterial(), [])
  const geo = useMemo(() => {
    const g = new THREE.BufferGeometry()
    g.setAttribute('position', new THREE.Float32BufferAttribute(nodes.flatMap((n) => [n.x, n.y, n.z]), 3))
    g.setAttribute('color', new THREE.Float32BufferAttribute(nodes.flatMap((n) => n.color), 3))
    g.setAttribute('size', new THREE.Float32BufferAttribute(nodes.map(starSize), 1))
    g.setAttribute('alpha', new THREE.Float32BufferAttribute(nodes.map((n) => n.rank), 1))
    g.setAttribute('lit', new THREE.Float32BufferAttribute(nodes.length, 1))
    return g
  }, [nodes])

  const lines = useMemo(() => {
    const pairs: [GNode, GNode][] = [...edges, ...me.children.map((c) => [me, c.node] as [GNode, GNode])]
    // 每个节点连着哪些线段: 焦点切换时只改这些
    const incident: number[][] = nodes.map(() => [])
    pairs.forEach(([a, b], i) => { incident[a.idx].push(i); incident[b.idx].push(i) })
    const g = new THREE.BufferGeometry()
    g.setAttribute('position', new THREE.Float32BufferAttribute(pairs.flatMap(([a, b]) => [a.x, a.y, a.z, b.x, b.y, b.z]), 3))
    g.setAttribute('color', new THREE.Float32BufferAttribute(pairs.length * 6, 3))
    g.setAttribute('lit', new THREE.Float32BufferAttribute(pairs.length * 2, 1))
    return { g, pairs, incident }
  }, [nodes, edges, me])

  // 可见性(类别开关、只看待确认): 整块重写,但只在筛选变化时发生
  useEffect(() => {
    const size = geo.getAttribute('size') as THREE.BufferAttribute
    for (const n of nodes) size.setX(n.idx, isVisible(n) ? starSize(n) : 0)
    size.needsUpdate = true
    const col = lines.g.getAttribute('color') as THREE.BufferAttribute
    lines.pairs.forEach(([a, b], i) => {
      const k = isVisible(a) && isVisible(b) ? 1 : 0
      const c = b.color
      for (const j of [0, 1]) col.setXYZ(i * 2 + j, c[0] * k, c[1] * k, c[2] * k)
    })
    col.needsUpdate = true
    invalidate()
  }, [geo, lines, nodes, isVisible, invalidate])

  // 焦点: 自身与直接邻居保持亮度,其他压暗。压暗由 shader 的 uFocus 统一完成,
  // 这里只把上一个焦点那一小撮的 lit 清零、给新焦点那一小撮置位,与全库规模无关
  const lit = useRef<{ geo: THREE.BufferGeometry | null; stars: number[]; segs: number[] }>({ geo: null, stars: [], segs: [] })
  useEffect(() => {
    const focus = hovered ?? selected
    const starLit = geo.getAttribute('lit') as THREE.BufferAttribute
    const lineLit = lines.g.getAttribute('lit') as THREE.BufferAttribute
    const prev = lit.current.geo === geo ? lit.current : { stars: [], segs: [] }
    for (const i of prev.stars) starLit.setX(i, 0)
    for (const i of prev.segs) { lineLit.setX(i * 2, 0); lineLit.setX(i * 2 + 1, 0) }
    const stars: number[] = [], segs: number[] = []
    if (focus) {
      for (const l of [...focus.parents, ...focus.children]) { starLit.setX(l.node.idx, 1); stars.push(l.node.idx) }
      starLit.setX(focus.idx, 2)
      stars.push(focus.idx)
      for (const i of lines.incident[focus.idx]) { lineLit.setX(i * 2, 1); lineLit.setX(i * 2 + 1, 1); segs.push(i) }
    }
    uploadSome(starLit, [...prev.stars, ...stars])
    uploadSome(lineLit, [...prev.segs, ...segs].flatMap((i) => [i * 2, i * 2 + 1]))
    lit.current = { geo, stars, segs }
    mat.uniforms.uFocus.value = lineMat.uniforms.uFocus.value = focus ? 1 : 0
    invalidate()
  }, [geo, lines, mat, lineMat, hovered, selected, invalidate])

  useFrame(({ clock }) => { mat.uniforms.uTime.value = clock.elapsedTime })

  return (
    <>
      <lineSegments geometry={lines.g} material={lineMat} />
      <points geometry={geo} material={mat} />
    </>
  )
}

/** 远处星尘与星系星云: 只做氛围,不参与交互 */
function Atmosphere({ graph, focused, hiddenCats, nebulaOn }: { graph: Graph; focused: boolean; hiddenCats: Set<Category>; nebulaOn: boolean }) {
  const invalidate = useThree((s) => s.invalidate)
  useEffect(() => invalidate(), [focused, nebulaOn, invalidate])
  const dust = useMemo(() => {
    const N = FAR_DUST, pos: number[] = [], col: number[] = [], size: number[] = [], alpha: number[] = []
    for (let i = 0; i < N; i++) {
      const u = rand() * 2 - 1, t = rand() * Math.PI * 2, r = 900 + rand() * 2200
      const s = Math.sqrt(1 - u * u)
      pos.push(r * s * Math.cos(t), r * u * 0.6, r * s * Math.sin(t))
      const warm = rand()
      col.push(0.85 + 0.15 * warm, 0.82 + 0.1 * warm, 0.8 + 0.2 * (1 - warm))
      size.push(2 + rand() * 4)
      alpha.push(0.08 + rand() * 0.35)
    }
    const g = new THREE.BufferGeometry()
    g.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3))
    g.setAttribute('color', new THREE.Float32BufferAttribute(col, 3))
    g.setAttribute('size', new THREE.Float32BufferAttribute(size, 1))
    g.setAttribute('alpha', new THREE.Float32BufferAttribute(alpha, 1))
    return { g, mat: starMaterial() }
  }, [])

  // 星系尘埃: 在真实节点周围撒少量细小微粒(颜色跟随所在星系),给星团一点体量;另有少量极淡的星云光斑
  const cloud = useMemo(() => {
    const real = graph.nodes.filter((n) => !n.virtual)
    const gauss = () => (rand() + rand() + rand() - 1.5) / 1.5
    const pos: number[] = [], col: number[] = [], size: number[] = [], alpha: number[] = [], cats: Category[] = []
    for (let i = 0; i < Math.min(real.length * CLOUD_PER_NODE, CLOUD_MAX); i++) {
      const n = real[Math.floor(rand() * real.length)]
      const r = 18 + 30 * Math.sqrt(n.desc + 1) * rand()
      pos.push(n.x + gauss() * r, n.y + gauss() * r * 0.5, n.z + gauss() * r)
      col.push(...n.color)
      size.push(1.4 + rand() * 1.8)
      alpha.push(0.08 + rand() * 0.18)
      cats.push(n.raw.category)
    }
    const g = new THREE.BufferGeometry()
    g.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3))
    g.setAttribute('color', new THREE.Float32BufferAttribute(col, 3))
    g.setAttribute('size', new THREE.Float32BufferAttribute(size, 1))
    g.setAttribute('alpha', new THREE.Float32BufferAttribute(alpha, 1))
    return { g, mat: starMaterial(), cats, base: alpha }
  }, [graph])
  const nebula = useMemo(() => {
    const real = graph.nodes.filter((n) => n.desc > 0 && !n.virtual)
    const pos: number[] = [], col: number[] = [], size: number[] = [], alpha: number[] = [], cats: Category[] = []
    for (let i = 0; i < NEBULA_PUFFS; i++) {
      const n = real[Math.floor(rand() * real.length)]
      pos.push(n.x + (rand() - 0.5) * 80, n.y + (rand() - 0.5) * 30, n.z + (rand() - 0.5) * 80)
      col.push(...n.color)
      size.push(160 + rand() * 260)
      alpha.push(0.0015 + rand() * 0.0025)
      cats.push(n.raw.category)
    }
    const g = new THREE.BufferGeometry()
    g.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3))
    g.setAttribute('color', new THREE.Float32BufferAttribute(col, 3))
    g.setAttribute('size', new THREE.Float32BufferAttribute(size, 1))
    g.setAttribute('alpha', new THREE.Float32BufferAttribute(alpha, 1))
    return { g, mat: starMaterial(1), cats, base: alpha }
  }, [graph])

  // 关掉某类别时,它的尘埃与星云一起隐去
  useEffect(() => {
    for (const layer of [cloud, nebula]) {
      const a = layer.g.getAttribute('alpha') as THREE.BufferAttribute
      layer.cats.forEach((c, i) => a.setX(i, hiddenCats.has(c) ? 0 : layer.base[i]))
      a.needsUpdate = true
    }
    invalidate()
  }, [cloud, nebula, hiddenCats, invalidate])

  const shell = useRef<THREE.LineSegments>(null)
  const shellGeo = useMemo(() => new THREE.EdgesGeometry(new THREE.IcosahedronGeometry(1300, 1)), [])
  useFrame(({ clock }, dt) => {
    dust.mat.uniforms.uTime.value = cloud.mat.uniforms.uTime.value = clock.elapsedTime
    // 有焦点时星系尘埃退后,让焦点子树浮出来
    const g = cloud.mat.uniforms.uGain
    const goal = focused ? 0.28 : 1
    g.value += (goal - g.value) * (1 - Math.exp(-dt * 6))
    if (Math.abs(goal - g.value) > 0.002) invalidate()
    else g.value = goal
    nebula.mat.uniforms.uGain.value = g.value
    if (shell.current) shell.current.rotation.y += dt * 0.012
  })

  return (
    <>
      <points geometry={dust.g} material={dust.mat} />
      <points geometry={cloud.g} material={cloud.mat} />
      {nebulaOn && <points geometry={nebula.g} material={nebula.mat} />}
      <lineSegments ref={shell} geometry={shellGeo}>
        <lineBasicMaterial color="#ffffff" transparent opacity={0.03} depthWrite={false} />
      </lineSegments>
    </>
  )
}

function useLabeled({ graph, isVisible, hovered, selected }: Pick<GalaxyProps, 'graph' | 'isVisible' | 'hovered' | 'selected'>) {
  const focus = hovered ?? selected
  return useMemo(() => {
    const s = new Set<GNode>([graph.me, ...graph.me.children.map((l) => l.node)])
    if (focus) {
      s.add(focus)
      const near = [...focus.parents, ...focus.children].map((l) => l.node)
      if (near.length <= 24) near.forEach((n) => s.add(n))
    }
    if (selected) s.add(selected)
    return [...s].filter(isVisible)
  }, [graph, focus, selected, isVisible])
}

/** 每帧把标签投影到屏幕上;标签本身是画布外的普通 DOM */
function LabelProjector({ labeled, refs }: { labeled: GNode[]; refs: React.RefObject<Map<number, HTMLDivElement>> }) {
  const v = useMemo(() => new THREE.Vector3(), [])
  const invalidate = useThree((s) => s.invalidate)
  useEffect(() => invalidate(), [labeled, invalidate])
  useFrame(({ camera, size }) => {
    for (const n of labeled) {
      const el = refs.current.get(n.idx)
      if (!el) continue
      v.set(n.x, n.y, n.z).project(camera)
      const behind = v.z > 1
      el.style.opacity = behind ? '0' : ''
      el.style.transform = `translate3d(${((v.x + 1) / 2) * size.width}px, ${((1 - v.y) / 2) * size.height}px, 0) translate(-50%, 12px)`
    }
  })
  return null
}

function LabelLayer({ labeled, focus, selected, refs }: { labeled: GNode[]; focus: GNode | null; selected: GNode | null; refs: React.RefObject<Map<number, HTMLDivElement>> }) {
  return (
    <div className="pointer-events-none absolute inset-0 overflow-hidden">
      {labeled.map((n) => {
        const major = n.virtual || n.depth === 0
        const active = n === focus || n === selected
        return (
          <div
            key={n.idx}
            ref={(el) => { if (el) refs.current.set(n.idx, el); else refs.current.delete(n.idx) }}
            className={cn(
              'absolute top-0 left-0 whitespace-nowrap select-none [text-shadow:0_0_6px_#000,0_0_2px_#000]',
              major ? 'font-mono text-[11px] tracking-[0.2em] uppercase text-white/85' : 'text-[11px] text-white/60',
              active && 'text-[12px] font-medium text-white',
            )}
          >
            {n.raw.name.length > 28 ? n.raw.name.slice(0, 27) + '…' : n.raw.name}
          </div>
        )
      })}
    </div>
  )
}

/** 拾取网格的格子边长(px) */
const PICK_CELL = 32
interface PickHit { n: GNode; sx: number; sy: number; r: number }
interface PickGrid { cols: number; cells: Map<number, PickHit[]> }

/** 把可见节点投影到屏幕,按命中圆覆盖的格子分桶。整体 O(n),只在视角变化后的第一次拾取时做 */
function buildPickGrid(nodes: GNode[], isVisible: (n: GNode) => boolean, camera: THREE.Camera, width: number, height: number): PickGrid {
  const v = new THREE.Vector3()
  const cols = Math.max(1, Math.ceil(width / PICK_CELL)), rows = Math.max(1, Math.ceil(height / PICK_CELL))
  const cells = new Map<number, PickHit[]>()
  for (const n of nodes) {
    if (!isVisible(n)) continue
    v.set(n.x, n.y, n.z).project(camera)
    if (v.z > 1) continue
    const sx = (v.x + 1) / 2 * width, sy = (1 - v.y) / 2 * height
    const dist = camera.position.distanceTo(v.set(n.x, n.y, n.z))
    const r = Math.max(7, starSize(n) * 0.22 * 700 / dist)
    if (sx + r < 0 || sx - r > width || sy + r < 0 || sy - r > height) continue
    const hit: PickHit = { n, sx, sy, r }
    const x0 = Math.max(0, Math.floor((sx - r) / PICK_CELL)), x1 = Math.min(cols - 1, Math.floor((sx + r) / PICK_CELL))
    const y0 = Math.max(0, Math.floor((sy - r) / PICK_CELL)), y1 = Math.min(rows - 1, Math.floor((sy + r) / PICK_CELL))
    for (let cy = y0; cy <= y1; cy++) for (let cx = x0; cx <= x1; cx++) {
      const k = cy * cols + cx
      const bucket = cells.get(k)
      if (bucket) bucket.push(hit)
      else cells.set(k, [hit])
    }
  }
  return { cols, cells }
}

/** 屏幕空间拾取: 比射线检测点精灵更稳,命中半径跟随星的屏幕尺寸。
 *  投影结果按屏幕网格缓存,相机不动时每次指针移动只查鼠标所在的一个格子;
 *  pointermove 合并到每帧最多一次 */
function Picker({ graph, isVisible, onHover, onSelect }: Pick<GalaxyProps, 'graph' | 'isVisible' | 'onHover' | 'onSelect'>) {
  const { camera, gl, size } = useThree()
  useEffect(() => {
    const el = gl.domElement
    // 网格对应的视角: 相机位姿或投影(含详情面板的 view offset)一变就作废
    const view = new THREE.Matrix4(), proj = new THREE.Matrix4()
    let grid: PickGrid | null = null
    const pick = (ev: PointerEvent) => {
      if (!grid || !view.equals(camera.matrixWorldInverse) || !proj.equals(camera.projectionMatrix)) {
        grid = buildPickGrid(graph.nodes, isVisible, camera, size.width, size.height)
        view.copy(camera.matrixWorldInverse)
        proj.copy(camera.projectionMatrix)
      }
      const rect = el.getBoundingClientRect()
      const mx = ev.clientX - rect.left, my = ev.clientY - rect.top
      if (mx < 0 || my < 0 || mx >= size.width || my >= size.height) return null
      let best: GNode | null = null, bd = Infinity
      for (const h of grid.cells.get(Math.floor(my / PICK_CELL) * grid.cols + Math.floor(mx / PICK_CELL)) ?? []) {
        const d = Math.hypot(h.sx - mx, h.sy - my)
        if (d < h.r && d < bd) { bd = d; best = h.n }
      }
      return best
    }
    let down: { x: number; y: number } | null = null
    let pending: PointerEvent | null = null, raf = 0
    const hover = () => {
      raf = 0
      const ev = pending
      pending = null
      if (!ev) return
      if (down && Math.hypot(ev.clientX - down.x, ev.clientY - down.y) > 4) return onHover(null)
      const n = pick(ev)
      onHover(n)
      el.style.cursor = n ? 'pointer' : 'grab'
    }
    const move = (ev: PointerEvent) => {
      pending = ev
      if (!raf) raf = requestAnimationFrame(hover)
    }
    const press = (ev: PointerEvent) => { down = { x: ev.clientX, y: ev.clientY } }
    const release = (ev: PointerEvent) => {
      if (down && Math.hypot(ev.clientX - down.x, ev.clientY - down.y) <= 4) onSelect(pick(ev))
      down = null
    }
    const leave = () => {
      cancelAnimationFrame(raf)
      raf = 0
      pending = null
      onHover(null)
    }
    el.addEventListener('pointermove', move)
    el.addEventListener('pointerdown', press)
    el.addEventListener('pointerup', release)
    el.addEventListener('pointerleave', leave)
    return () => {
      cancelAnimationFrame(raf)
      el.removeEventListener('pointermove', move)
      el.removeEventListener('pointerdown', press)
      el.removeEventListener('pointerup', release)
      el.removeEventListener('pointerleave', leave)
    }
  }, [camera, gl, size, graph, isVisible, onHover, onSelect])
  return null
}

/** 点击后的观察距离: 让「该节点 + 它的直接下级」占画面约 FOCUS_FILL。
 *  半径取节点到下级距离的 80 分位(个别离群的下级不撑大画面);叶子没有下级,用到父节点的距离。
 *  每次点击都按此重算,与当前相机位置无关。 */
const FOCUS_FILL = 0.6
const FOCUS_MIN = 60
const FOCUS_MAX = 700
const CAMERA_FOV = 50
/** 详情面板(右 16px + 宽 360px)再留 16px 间隙: 选中时画面的"中心"是左边剩下那块可见区域的中心 */
const PANEL_INSET = 392
function focusDistance(n: GNode, visibleAspect: number): number {
  const d = (m: GNode) => Math.hypot(m.x - n.x, m.y - n.y, m.z - n.z)
  const ds = (n.children.length ? n.children : n.parents).map((l) => d(l.node)).sort((a, b) => a - b)
  const r = ds.length ? ds[Math.min(ds.length - 1, Math.floor(ds.length * 0.8))] : FOCUS_MIN
  const tanV = Math.tan((CAMERA_FOV / 2) * Math.PI / 180)
  const dist = r / (FOCUS_FILL * Math.min(tanV, tanV * visibleAspect))
  return THREE.MathUtils.clamp(dist, FOCUS_MIN, FOCUS_MAX)
}

function CameraRig({ selected, flyKey, idle }: { selected: GNode | null; flyKey: number; idle: boolean }) {
  const controls = useThree((s) => s.controls) as OrbitControlsImpl | null
  const flight = useRef<{ target: THREE.Vector3; dist: number } | null>(null)
  const invalidate = useThree((s) => s.invalidate)
  const size = useThree((s) => s.size)
  const shift = useRef(0)
  useEffect(() => { if (controls) { controls.autoRotate = idle; invalidate() } }, [controls, idle, invalidate])
  useEffect(() => {
    if (!flyKey) return
    flight.current = selected
      ? { target: new THREE.Vector3(selected.x, selected.y, selected.z), dist: focusDistance(selected, Math.max(0.3, (size.width - PANEL_INSET) / size.height)) }
      : { target: new THREE.Vector3(0, 0, 0), dist: 690 }
    invalidate()
  }, [flyKey, selected, invalidate])
  useFrame((_, dt) => {
    if (!controls) return
    // 把投影中心移到可见区域的中心: 选中时让出右侧面板的宽度
    const want = selected ? Math.min(PANEL_INSET, size.width * 0.5) : 0
    if (Math.abs(shift.current - want) > 0.5 || (want === 0 && shift.current !== 0)) {
      shift.current = Math.abs(shift.current - want) <= 0.5 ? want : THREE.MathUtils.lerp(shift.current, want, 1 - Math.exp(-dt * 4))
      const cam = controls.object as THREE.PerspectiveCamera
      if (shift.current === 0) cam.clearViewOffset()
      else cam.setViewOffset(size.width, size.height, shift.current / 2, 0, size.width, size.height)
      invalidate()
    }
    // 按需渲染: 只有自转和飞行需要持续出帧;阻尼由 OrbitControls 的 change 事件自己续帧
    if (idle) invalidate()
    const f = flight.current
    if (!f) return
    invalidate()
    const k = 1 - Math.exp(-dt * 4)
    const cam = controls.object
    const offset = cam.position.clone().sub(controls.target)
    controls.target.lerp(f.target, k)
    offset.setLength(THREE.MathUtils.lerp(offset.length(), f.dist, k))
    cam.position.copy(controls.target).add(offset)
    if (controls.target.distanceTo(f.target) < 0.5 && Math.abs(offset.length() - f.dist) < 1) flight.current = null
  })
  useEffect(() => {
    if (!controls) return
    const stop = () => { flight.current = null }
    controls.addEventListener('start', stop)
    return () => controls.removeEventListener('start', stop)
  }, [controls])
  return null
}

/** 只在自转期间采样: 那时每帧都出,帧间隔反映的是显卡快慢;按需出帧时的间隔只反映操作节奏 */
function QualityProbe({ sampling, quality, onSlow }: { sampling: boolean; quality: Quality; onSlow: () => void }) {
  const samples = useRef<number[]>([])
  const last = useRef(0)
  useEffect(() => { samples.current = [] }, [sampling, quality])
  useFrame(() => {
    const now = performance.now(), dt = now - last.current
    last.current = now
    if (!sampling || quality === 0 || dt > 250) return
    const s = samples.current
    s.push(dt)
    if (s.length < 90) return
    const median = [...s].sort((a, b) => a - b)[s.length >> 1]
    samples.current = []
    if (median > SLOW_FRAME_MS) onSlow()
  })
  return null
}

/** 最近 DRIFT_MS 内有过操作则为 true */
function useRecentlyActive() {
  const [active, setActive] = useState(true)
  useEffect(() => {
    let timer = setTimeout(() => setActive(false), DRIFT_MS)
    const poke = () => {
      setActive(true)
      clearTimeout(timer)
      timer = setTimeout(() => setActive(false), DRIFT_MS)
    }
    const events = ['pointerdown', 'pointermove', 'wheel', 'keydown'] as const
    for (const e of events) window.addEventListener(e, poke, { passive: true })
    return () => {
      clearTimeout(timer)
      for (const e of events) window.removeEventListener(e, poke)
    }
  }, [])
  return active
}

export function Galaxy(props: GalaxyProps) {
  const { graph, isVisible, hovered, selected, flyKey, onHover, onSelect } = props
  const labeled = useLabeled(props)
  const hiddenCats = useMemo(
    () => new Set(CATEGORIES.filter((c) => !graph.nodes.some((n) => !n.virtual && n.raw.category === c && isVisible(n)))),
    [graph, isVisible],
  )
  const labelRefs = useRef(new Map<number, HTMLDivElement>())
  const active = useRecentlyActive()
  const idle = active && !hovered && !selected
  const [quality, setQuality] = useState<Quality>(2)
  const degrade = useCallback(() => setQuality((q) => (q > 0 ? (q - 1) as Quality : q)), [])
  return (
    <div className="absolute inset-0" data-quality={quality}>
      <Canvas
        camera={{ position: [0, 300, 620], fov: CAMERA_FOV, near: 1, far: 9000 }}
        dpr={quality === 0 ? 1 : DPR}
        frameloop="demand"
        // 画面先进后期合成的离屏缓冲,画布自身的抗锯齿用不上
        gl={{ antialias: false, powerPreference: 'high-performance' }}
      >
        <color attach="background" args={['#050407']} />
        <fog attach="fog" args={['#050407', 1600, 4200]} />
        <Atmosphere graph={graph} focused={!!(hovered ?? selected)} hiddenCats={hiddenCats} nebulaOn={quality === 2} />
        <Stars graph={graph} isVisible={isVisible} hovered={hovered} selected={selected} />
        <LabelProjector labeled={labeled} refs={labelRefs} />
        <Picker graph={graph} isVisible={isVisible} onHover={onHover} onSelect={onSelect} />
        <OrbitControls makeDefault enableDamping dampingFactor={0.08} autoRotateSpeed={0.25} minDistance={40} maxDistance={2600} />
        <CameraRig selected={selected} flyKey={flyKey} idle={idle} />
        <QualityProbe sampling={idle} quality={quality} onSlow={degrade} />
        <EffectComposer multisampling={0}>
          <Bloom
            key={quality === 2 ? 'full' : 'lite'}
            mipmapBlur intensity={1.1} luminanceThreshold={0.12} luminanceSmoothing={0.35} radius={0.75}
            {...(quality < 2 && { resolutionScale: 0.5, levels: 5 })}
          />
          <Vignette offset={0.25} darkness={0.75} />
        </EffectComposer>
      </Canvas>
      <LabelLayer labeled={labeled} focus={hovered ?? selected} selected={selected} refs={labelRefs} />
    </div>
  )
}
