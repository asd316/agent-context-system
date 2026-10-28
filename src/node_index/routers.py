"""Router skill 的 Member list: 生成 members.yaml,并渲染进 SKILL.md 的标记区段。

`build` 调用 render_routers;Selection guide (标记以外的部分) 由 agent 维护,这里不碰。
"""
from pathlib import Path

import yaml

from .catalog import find_member_node
from .shared import (ROUTER_BEGIN, ROUTER_END, EntryStatus, MemberKind, MemberRef, ShapeError,
                     load_catalog, load_categories)

TRAIT_MAX = 40
MEMBERS_HEADER = "# 由 node-index build 生成,不要手改。改成员请改 shared/categories.yaml。\n"


def _children(cats, cid):
    return sorted((c for c in cats.values() if c.parent == cid), key=lambda c: c.id)


def router_members(conn, p, catalog, cats, cid):
    """返回 [(分组 Category id, 成员)]。成员先列本 Category 的,再按 id 列子 Category:
    有 Router 的子 Category 只列它的 Router (Pointer chain),没有的把成员展开进来。retired 的 skill 不列。"""
    out, seen = [], set()

    def member(ref, trait_fallback=""):
        e = catalog.get(ref.id) if ref.kind is MemberKind.SKILL else None
        node = find_member_node(conn, p, catalog, ref) if conn is not None else None
        trait = (node["hier_summary"] if node and node["hier_summary"] else trait_fallback)[:TRAIT_MAX]
        return {"ref": str(ref), "kind": ref.kind.value, "tier": e.tier.value if e else None, "trait": trait}

    def walk(c):
        for ref in c.members:
            e = catalog.get(ref.id) if ref.kind is MemberKind.SKILL else None
            if ref in seen or (e is not None and e.status is EntryStatus.RETIRED):
                continue
            seen.add(ref)
            out.append((c.id, member(ref)))
        for d in _children(cats, c.id):
            if d.router:
                ref = MemberRef(MemberKind.SKILL, d.router)
                if ref not in seen:
                    seen.add(ref)
                    out.append((d.id, member(ref, d.name)))
            else:
                walk(d)

    walk(cats[cid])
    return out


def render_member_list(grouped, cats):
    lines = ["## Member list", ""]
    last = None
    for gid, m in grouped:
        if gid != last:
            if last is not None:
                lines.append("")
            lines += [f"### {cats[gid].name} (`{gid}`)", ""]
            last = gid
        tag = f" [{m['tier']}]" if m["tier"] in ("router", "core") else ""
        lines.append(f"- `{m['ref']}`{tag}" + (f": {m['trait']}" if m["trait"] else ""))
    return "\n".join(lines) + "\n"


def splice(text, section, source):
    """只替换两行标记之间的内容,其余原样保留。"""
    b, e = text.find(ROUTER_BEGIN), text.find(ROUTER_END)
    if b < 0 or e < 0 or e < b:
        raise ShapeError(source, "SKILL.md 缺少 Member list 标记")
    start = b + len(ROUTER_BEGIN)
    return text[:start] + "\n" + section + text[e:]


def plan_routers(conn, p):
    """计算每个 Router 的 members.yaml 与 SKILL.md 新内容,不写盘。任何一个 SKILL.md 不合格就整体报错。"""
    catalog, cats = load_catalog(p), load_categories(p)
    plans = []
    for cid in sorted(cats):
        c = cats[cid]
        if not c.router:
            continue
        d = p.routers / c.router
        skill = d / "SKILL.md"
        if not skill.exists():
            raise ShapeError(skill, f"Category {cid} 的 Router skill 不存在")
        grouped = router_members(conn, p, catalog, cats, cid)
        members = [m for _, m in grouped]
        new_skill = splice(skill.read_text(encoding="utf-8"), render_member_list(grouped, cats), skill)
        new_members = MEMBERS_HEADER + yaml.safe_dump(members, allow_unicode=True, sort_keys=False)
        plans.append((c.router, members, [(skill, new_skill), (d / "members.yaml", new_members)]))
    return plans


def render_routers(conn, p, dry_run=False):
    """build 的 Router 部分。conn 为本机 Index (取 trait),可为 None (trait 留空)。
    返回 [{name, members, changed}],与 openapi BuildResult.routers 一致。内容未变的文件不重写。"""
    out = []
    for name, members, files in plan_routers(conn, p):
        changed = False
        for path, text in files:
            if not (Path(path).exists() and Path(path).read_text(encoding="utf-8") == text):
                changed = True
                if not dry_run:
                    Path(path).write_text(text, encoding="utf-8")
        out.append({"name": name, "members": len(members), "changed": changed})
    return out
