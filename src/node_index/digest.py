"""Digest 最小版 (Operations E3): 定期检查 Category 归类与 Router skill,每条发现写成一条 kind=digest 的 Report。

两类检查,每次模型调用对应一个检查单位:
- category-fit: 一个 Category 一次调用,判断其中每个 skill 的名字与内容是否符合该 Category 的主题。
  发现按 skill 拆开,条目键 `category-fit:<category>:<skill>`。
- router-guide: 一个 Router 一次调用,判断 Selection guide 与当前 Member list 是否一致。
  条目键 `router-guide:<router>`;模型给出修订稿时附 Proposal (replace 整段 Selection guide)。

条目键写在 Report evidence 的首行 (`Digest 条目: <键>`)。同一条目已有未关闭的 digest Report 时不再新建。
上次成功运行记在 host/digest-state.json;`if_due` 时不满 INTERVAL_DAYS 天就跳过。
"""
import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import inbox, routers, shared
from .catalog import entry_path, find_member_node
from .db import CliError
from .llm import Llm, LlmError, load_env
from .rules import atomic_write
from .shared import ROUTER_BEGIN, EntryStatus, MemberKind, ReportKind

INTERVAL_DAYS = 7
STATE_FILE = "digest-state.json"
CLIENT = "node-index-digest"
POSITION = "orchestrator"
KEY_PREFIX = "Digest 条目: "
DESC_CHARS = 400
BODY_CHARS = 600
MAX_TOKENS = 4000


@dataclass(frozen=True)
class Unit:
    """一次模型调用。kind 为 category-fit 或 router-guide。"""
    kind: str
    category: str
    messages: tuple
    context: dict


# ---- 状态 ----

def _state_path(p):
    return p.host.parent / STATE_FILE


def last_run(p):
    try:
        return datetime.fromisoformat(json.loads(_state_path(p).read_text(encoding="utf-8"))["last_run"])
    except FileNotFoundError:
        return None
    except (ValueError, KeyError) as e:
        raise CliError("bad_input", f"{_state_path(p)}: 无法解析 ({e});删掉它会让下次 digest 立即运行") from None


def _record(p, at, report_ids):
    atomic_write(_state_path(p), json.dumps({"last_run": at.isoformat(timespec="seconds"), "reports": report_ids},
                                            ensure_ascii=False, indent=2) + "\n")


# ---- 取材 ----

def skill_material(conn, p, catalog, name):
    """(描述, 正文开头)。先读本机 SKILL.md;没有时退回 Index 摘要。"""
    e = catalog.get(name)
    d = entry_path(p, e) if e is not None else None
    skill = d / "SKILL.md" if d else None
    if skill is not None and skill.is_file():
        text = skill.read_text(encoding="utf-8", errors="replace")
        try:
            meta, body = shared.split_frontmatter(text, skill)
            desc = str(meta.get("description") or "")
        except shared.ShapeError:
            desc, body = "", text
        return desc[:DESC_CHARS], body.strip()[:BODY_CHARS]
    if conn is not None:
        r = find_member_node(conn, p, catalog, shared.MemberRef(MemberKind.SKILL, name))
        if r is not None:
            row = conn.execute("SELECT hier_summary, content_summary FROM nodes WHERE id = ?", (r["id"],)).fetchone()
            return (row["content_summary"] or row["hier_summary"] or "")[:DESC_CHARS], ""
    return "", ""


def _cat_label(cats, cid):
    chain, c = [], cid
    while c:
        chain.append(cats[c].name)
        c = cats[c].parent
    return " / ".join(reversed(chain))


def _category_list(cats):
    return "\n".join(f"- `{cid}`: {_cat_label(cats, cid)}" for cid in sorted(cats))


SYSTEM = "你是严谨的 skill 库编辑,检查分类与路由文档的质量。只输出 JSON。"


def fit_unit(conn, p, catalog, cats, cid):
    c = cats[cid]
    skills = [m.id for m in c.members if m.kind is MemberKind.SKILL
              and not (m.id in catalog and catalog[m.id].status is EntryStatus.RETIRED)]
    if not skills:
        return None
    blocks, materials = [], {}
    for name in skills:
        desc, body = skill_material(conn, p, catalog, name)
        materials[name] = desc
        blocks.append(f"### {name}\n描述: {desc or '(无)'}\n正文开头:\n{body or '(无)'}")
    prompt = f"""下面是一个个人 skill 库里的一个分类 (Category) 及其中的 skill。请判断每个 skill 的名字与内容是否符合这个分类的主题。

分类: `{cid}` ({_cat_label(cats, cid)})

全部分类 (供你建议移到哪里):
{_category_list(cats)}

该分类下的 skill:

{chr(10).join(blocks)}

要求:
1. 只报明显不吻合的 skill: 它的主要用途属于另一个分类,或与本分类主题无关。沾边、或同时属于多类但本分类说得通的,不报。
2. 每条给出具体建议: 移到哪个分类 (`move_to` 填上面列表里的分类 id),或也放进另一个分类 (`copy_to`),或说明分类本身该改名或拆分 (两者都填 null)。
3. `problem` 说明为什么不吻合,依据上面的描述或正文,不要编造;`suggestion` 写一句可以照做的话。
4. 只输出一个 JSON 对象: {{"findings": [{{"skill": "...", "problem": "...", "suggestion": "...", "move_to": "分类id或null", "copy_to": "分类id或null"}}]}}。全部吻合时 findings 为空列表。不要代码块,不要其它文字。"""
    return Unit("category-fit", cid, ({"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}),
                {"skills": skills, "materials": materials})


def selection_guide(text):
    """SKILL.md 中 frontmatter 之后、Member list 标记之前的部分,原样返回 (作为 Proposal 的 old)。"""
    try:
        _, body = shared.split_frontmatter(text, "SKILL.md")
    except shared.ShapeError:
        body = text
    return body.split(ROUTER_BEGIN, 1)[0].rstrip("\n")


def guide_unit(conn, p, catalog, cats, cid):
    c = cats[cid]
    skill = p.routers / c.router / "SKILL.md"
    if not skill.is_file():
        return None
    guide = selection_guide(skill.read_text(encoding="utf-8"))
    grouped = routers.router_members(conn, p, catalog, cats, cid)
    lines = []
    for gid, m in grouped:
        desc = m["trait"]
        if m["kind"] == "skill":
            desc = skill_material(conn, p, catalog, m["ref"])[0] or desc
        tier = f" [{m['tier']}]" if m["tier"] in ("router", "core") else ""
        lines.append(f"- `{m['ref']}`{tier} (分组 `{gid}`): {desc or '(无描述)'}")
    prompt = f"""下面是一个 Router skill: 它是某类 skill 的入口,agent 先读它的 Selection guide,再从 Member list 里选 skill。
Member list 由程序按分类生成,是事实;Selection guide 由人或 agent 手写,可能过时。请判断 Selection guide 与 Member list 是否一致。

Router: `{c.router}`,对应分类 `{cid}` ({_cat_label(cats, cid)})

--- Selection guide 原文 ---
{guide}
--- 原文结束 ---

当前 Member list (含各成员描述):
{chr(10).join(lines) or '(空)'}

要检查的问题:
1. guide 提到的分组、skill 或做法,在 Member list 里不存在或已改名。
2. Member list 里有成员或分组,guide 完全没有覆盖,agent 读完 guide 不知道何时该选它们。
3. guide 对某分组或 skill 的描述与成员实际描述明显不符,会把 agent 引向错误的 skill。
只报会影响 agent 选择的问题;措辞、格式问题不报。

只输出一个 JSON 对象: {{"consistent": true或false, "issues": ["每条一句,点名具体分组或 skill"], "suggestion": "一句话说明怎么改", "revised_guide": "完整的修订版 Selection guide (Markdown,替换上面的原文;保留原文的结构与有效内容,只改有问题的地方),consistent 为 true 时为空字符串"}}。不要代码块,不要其它文字。"""
    return Unit("router-guide", cid, ({"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}),
                {"router": c.router, "guide": guide, "skill": skill, "members": len(grouped)})


def plan(conn, p):
    catalog, cats = shared.load_catalog(p), shared.load_categories(p)
    units = []
    for cid in sorted(cats):
        u = fit_unit(conn, p, catalog, cats, cid)
        if u:
            units.append(u)
    for cid in sorted(cats):
        if cats[cid].router:
            u = guide_unit(conn, p, catalog, cats, cid)
            if u:
                units.append(u)
    return units, cats


# ---- 模型回复 → 发现 ----

def _json(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("回复里没有 JSON")
    return json.loads(m.group(0))


def _text(v):
    return v.strip() if isinstance(v, str) else ""


def parse_fit(unit, raw, cats):
    """返回 [finding]。不合格抛 ValueError,带上原因供重问。"""
    d = _json(raw)
    items = d.get("findings")
    if not isinstance(items, list):
        raise ValueError("findings 应为列表")
    out = []
    for x in items:
        if not isinstance(x, dict):
            raise ValueError("findings 的每项应为对象")
        name, problem, suggestion = _text(x.get("skill")), _text(x.get("problem")), _text(x.get("suggestion"))
        if name not in unit.context["skills"]:
            raise ValueError(f"skill {name!r} 不在该分类的 skill 列表里")
        if not problem or not suggestion:
            raise ValueError(f"{name}: problem 与 suggestion 都必须非空")
        targets = {}
        for k in ("move_to", "copy_to"):
            v = x.get(k)
            v = _text(v) if v not in (None, "null") else ""
            if v and (v not in cats or v == unit.category):
                raise ValueError(f"{name}: {k} {v!r} 不是另一个已有分类的 id")
            targets[k] = v or None
        out.append({"skill": name, "problem": problem, "suggestion": suggestion, **targets})
    return out


def parse_guide(unit, raw, cats):
    d = _json(raw)
    if not isinstance(d.get("consistent"), bool):
        raise ValueError("consistent 应为 true 或 false")
    if d["consistent"]:
        return []
    issues = [_text(i) for i in d.get("issues") or [] if _text(i)]
    suggestion = _text(d.get("suggestion"))
    if not issues or not suggestion:
        raise ValueError("consistent 为 false 时 issues 与 suggestion 都必须非空")
    revised = _text(d.get("revised_guide"))
    return [{"issues": issues, "suggestion": suggestion, "revised": revised or None}]


PARSERS = {"category-fit": parse_fit, "router-guide": parse_guide}


def ask(llm, unit, cats, attempts=3):
    messages = list(unit.messages)
    problem = "未知"
    for _ in range(attempts):
        raw = llm.chat(messages, max_tokens=MAX_TOKENS)
        try:
            return PARSERS[unit.kind](unit, raw, cats)
        except (ValueError, json.JSONDecodeError) as e:
            problem = str(e)
        messages += [{"role": "assistant", "content": raw},
                     {"role": "user", "content": f"上一次不合格: {problem}。请改正后只输出 JSON。"}]
    raise LlmError(f"多次重试仍不合格: {problem}")


# ---- 发现 → Report ----

def key_of(report):
    first = report.evidence.split("\n", 1)[0]
    return first[len(KEY_PREFIX):].strip() if first.startswith(KEY_PREFIX) else None


def open_keys(p):
    return {key_of(r) for r in inbox.list_reports(p, kind=ReportKind.DIGEST)} - {None}


def fit_report(unit, f, cats):
    cid, name = unit.category, f["skill"]
    cmds = []
    if f["move_to"]:
        cmds.append(f"node-index category move {name} --from {cid} --to {f['move_to']}")
    if f["copy_to"]:
        cmds.append(f"node-index category copy {name} --to {f['copy_to']}")
    reason = f"{f['problem']}\n\n建议: {f['suggestion']}" + "".join(f"\n  {c}" for c in cmds)
    evidence = (f"{KEY_PREFIX}category-fit:{cid}:{name}\n分类: `{cid}` ({_cat_label(cats, cid)})\n"
                f"{name} 的描述: {unit.context['materials'].get(name) or '(无)'}")
    return {"key": f"category-fit:{cid}:{name}", "title": f"归类: {name} 与「{cats[cid].name}」不吻合",
            "reason": reason, "evidence": evidence, "change": None}


def guide_report(unit, f, p):
    router = unit.context["router"]
    reason = "Selection guide 与 Member list 不一致:\n" + "".join(f"- {i}\n" for i in f["issues"]) + \
             f"\n建议: {f['suggestion']}"
    target = str(unit.context["skill"].relative_to(p.root))
    change = None
    if f["revised"] and f["revised"] != unit.context["guide"].strip():
        change = {"target": target, "old": unit.context["guide"], "new": f["revised"]}
        reason += "\n\n附 Proposal: 用修订稿替换整段 Selection guide (Member list 区段不变)。"
    evidence = (f"{KEY_PREFIX}router-guide:{router}\n文件: {target}\n"
                f"Member list 共 {unit.context['members']} 项,按分类 `{unit.category}` 当前的 categories.yaml 计算")
    return {"key": f"router-guide:{router}", "title": f"Router: {router} 的 Selection guide 与成员不一致",
            "reason": reason, "evidence": evidence, "change": change}


# ---- 入口 ----

def run(conn, p, dry_run=False, if_due=False, workers=8, env_path=None, now=None):
    now = now or datetime.now().replace(microsecond=0)
    last = last_run(p)
    if if_due and last is not None and now - last < timedelta(days=INTERVAL_DAYS):
        return {"skipped": True, "last_run": last.isoformat(timespec="seconds"),
                "next_due": (last + timedelta(days=INTERVAL_DAYS)).isoformat(timespec="seconds")}
    units, cats = plan(conn, p)
    keys = [f"{u.kind}:{u.category}" for u in units]
    if dry_run:
        return {"targets": len(units), "target_keys": keys,
                "sample_prompt": units[0].messages[1]["content"] if units else None}
    shared.load_host(p)  # 先确认能写 Report,别等模型都调用完才失败
    llm = Llm(load_env(env_path))
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futs = [(u, pool.submit(ask, llm, u, cats)) for u in units]
    failed, drafts = [], []
    for u, fut in futs:
        try:
            found = fut.result()
        except LlmError as e:
            failed.append({"key": f"{u.kind}:{u.category}", "reason": str(e)})
            continue
        drafts += [fit_report(u, f, cats) if u.kind == "category-fit" else guide_report(u, f, p) for f in found]

    taken = open_keys(p)
    reports, duplicates = [], []
    for d in drafts:
        if d["key"] in taken:
            duplicates.append(d["key"])
            continue
        c = d["change"] or {}
        r = inbox.create(p, ReportKind.DIGEST.value, d["title"], d["reason"], d["evidence"], CLIENT, POSITION,
                         target=c.get("target"), old=c.get("old"), new=c.get("new"))
        taken.add(d["key"])
        reports.append({"id": r.id, "key": d["key"], "title": d["title"], "proposal": bool(c)})
    if not failed:  # 有失败时不记录,下次 (每日流水线) 再跑;已建的 Report 靠去重不会重复
        _record(p, now, [r["id"] for r in reports])
        last = now
    return {"targets": len(units), "reports": reports, "duplicates": duplicates, "failed": failed,
            "last_run": last.isoformat(timespec="seconds") if last else None}
