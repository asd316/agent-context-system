// 形状以 docs/openapi.yaml 的 x-http 部分为准
export type Tier = 'core' | 'router' | 'leaf'
export type Profile = 'orchestrator' | 'executor'
export type MemberKind = 'skill' | 'sop' | 'lesson'

export interface CatalogEntry {
  name: string
  entry: 'vendored' | 'referenced' | 'native'
  source: string
  version: string | null
  added: string
  tier: Tier
  manual_only: boolean
  status: 'active' | 'retired'
}
export interface MemberDetail { ref: string; kind: MemberKind; tier: Tier | null; trait: string }
export interface SharedCategory { id: string; name: string; router: string | null; members: string[] }
export interface OrganizeCategory extends SharedCategory { member_details: MemberDetail[] }
export interface Recent { member: string; category: string; commit: string; at: string }
export interface Organize {
  categories: OrganizeCategory[]
  uncategorized: string[]
  entries: Record<string, CatalogEntry>
  recent: Recent[]
  problems: string[]
}
export interface Membership { member: string; categories: string[] }

export interface ClientRow {
  client: string
  profile: Profile
  rules_path: string
  skills_dir: string
  installed: boolean
  active_profile: string | null
  current_bytes: number | null
}
export interface RuleRow { id: string; title: string; kind: string; order: number; bytes: number }
export interface ProfilePreview {
  profile: Profile
  rules_bytes: number
  error: string | null
  rules: RuleRow[]
  skills: { name: string; manual_only: boolean; tier?: Tier }[]
}
export interface Profiles { clients: ClientRow[]; profiles: ProfilePreview[] }

export type ReportStatus = 'pending' | 'in-progress' | 'resolved' | 'rejected'
export interface ReportBrief {
  id: string; kind: string; title: string; status: ReportStatus; at: string; claimed_by: string | null; decided_by: string | null; proposal: boolean
}
export interface Report {
  id: string; kind: string; title: string; reason: string; evidence: string
  from: { client: string; position: string; host: string }
  at: string
  change: null | { target: string; diff?: string; replace?: { old: string; new: string } }
  status: ReportStatus
  claimed_by: string | null
  resolution: null | { decided_by: string; instruction: string | null; outcome: string | null; changes: string[]; at: string }
}
export interface Preview {
  target: string | null; before: string | null; after: string | null; conflict: string | null
  trees: null | { before: SharedCategory[]; after: SharedCategory[] }
}
export type LessonStatus = 'active' | 'merged' | 'obsolete'
export interface LessonFields { topic: string; client: string; situation: string; problem: string; fix: string }
export interface Lesson extends LessonFields {
  id: string; status: LessonStatus; merged_into: string | null; host: string | null; date: string; path: string
}
export interface Decision { at: string; target: string; action: string; result: string; instruction?: string; note?: string; by: string; session?: string }

export interface ApplyStep { step: 'build' | 'activate' | 'commit' | 'push'; ok: boolean; detail: string }
export interface ApplyStatus {
  state: 'idle' | 'running' | 'done' | 'failed'
  steps: ApplyStep[]
  commit: string | null
  pushed: boolean
  at: string | null
  unpushed_reason: string | null
  head: string | null
}
export interface Changes {
  base_commit: string | null
  files: { path: string; status: 'added' | 'modified' | 'deleted' }[]
  clients: { client: string; profile: Profile; active_profile: string | null }[]
}

export class ApiError extends Error {
  kind: string
  status: number
  constructor(kind: string, message: string, status: number) {
    super(message)
    this.kind = kind
    this.status = status
  }
}

async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
  const r = await fetch(path, {
    method,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const data = await r.json().catch(() => ({ error: 'failed', message: `${r.status} ${r.statusText}` }))
  if (!r.ok) throw new ApiError(data.error ?? 'failed', data.message ?? String(r.status), r.status)
  return data as T
}

export const api = {
  organize: () => call<Organize>('GET', '/api/organize'),
  createCategory: (id: string, name: string) => call<SharedCategory>('POST', '/api/category', { id, name }),
  deleteCategory: (id: string) => call<{ deleted: string }>('DELETE', `/api/category/${id}`),
  membership: (op: 'move' | 'copy' | 'remove', member: string, from?: string, to?: string) =>
    call<Membership>('POST', '/api/membership', { op, member, from, to }),
  patchCatalog: (name: string, patch: { tier?: Tier; manual_only?: boolean }) =>
    call<CatalogEntry>('PATCH', `/api/catalog/${encodeURIComponent(name)}`, patch),
  profiles: () => call<Profiles>('GET', '/api/profiles'),
  setProfile: (client: string, profile: Profile) =>
    call<{ client: string; profile: Profile }>('PATCH', `/api/clients/${encodeURIComponent(client)}`, { profile }),
  inbox: (statuses: ReportStatus[] = [], kind?: string) => {
    const q = new URLSearchParams()
    statuses.forEach((s) => q.append('status', s))
    if (kind) q.set('kind', kind)
    return call<ReportBrief[]>('GET', `/api/inbox?${q}`)
  },
  report: (id: string) => call<Report>('GET', `/api/inbox/${id}`),
  preview: (id: string) => call<Preview>('GET', `/api/inbox/${id}/preview`),
  instruction: (id: string, text: string) => call<{ instruction: string }>('POST', `/api/inbox/${id}/instruction`, { text }),
  decide: (id: string, decision: 'approve' | 'reject', instruction?: string) =>
    call<{ report: Report; decision: Decision }>('POST', `/api/inbox/${id}/decide`, { decision, instruction }),
  lessons: () => call<Lesson[]>('GET', '/api/lessons'),
  createLesson: (fields: LessonFields & { host_only?: boolean }) => call<Lesson>('POST', '/api/lessons', fields),
  updateLesson: (id: string, patch: Partial<LessonFields> & { status?: LessonStatus; merged_into?: string | null }) =>
    call<Lesson>('PATCH', `/api/lessons/${id}`, patch),
  decisions: () => call<Decision[]>('GET', '/api/decisions'),
  changes: () => call<Changes>('GET', '/api/changes'),
  discard: () => call<{ discarded: string[] }>('POST', '/api/changes/discard'),
  apply: () => call<ApplyStatus>('POST', '/api/apply'),
  applyStatus: () => call<ApplyStatus>('GET', '/api/apply/status'),
}
