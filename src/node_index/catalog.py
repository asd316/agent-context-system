"""Catalog entry 与共享 Category 的读写,以及把 categories.yaml 写进本机 Index。

CLI (`catalog …`、`category …`) 与网页后端共用这里的函数。文件形状只经 node_index.shared 解析。
"""
import os
import sqlite3
from dataclasses import replace
from datetime import date
from pathlib import Path

from . import shared
from .db import CliError, normalize_path, now_iso
from .shared import (Category, CatalogEntry, EntryKind, EntryStatus, MemberKind, MemberRef, Tier,
                     catalog_to_dict, categories_to_dict, load_catalog, load_categories)


def _save(path, data):
    """写回时保留文件开头的注释行 (write_yaml 本身不保留注释)。"""
    path = Path(path)
    head = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.startswith("#"):
                break
            head.append(line)
    shared.write_yaml(path, data)
    if head:
        path.write_text("\n".join(head) + "\n" + path.read_text(encoding="utf-8"), encoding="utf-8")


def entry_dict(e):
    d = e.to_dict()
    d["added"] = e.added.isoformat()
    return {"name": e.name, **d}


# ---- Catalog ----

def list_entries(p, tier=None, status=None):
    cat = load_catalog(p)
    return [entry_dict(cat[n]) for n in sorted(cat)
            if (tier is None or cat[n].tier is Tier(tier)) and (status is None or cat[n].status is EntryStatus(status))]


def set_entry(p, name, tier=None, manual_only=None, status=None, entry=None, source=None, version=None, today=None):
    """修改或新建一条 Catalog entry。None 表示不改该字段。"""
    changes = {k: v for k, v in (("tier", tier), ("manual_only", manual_only), ("status", status),
                                 ("entry", entry), ("source", source), ("version", version)) if v is not None}
    if not changes:
        raise CliError("bad_input", "至少改一个字段")
    cat = load_catalog(p)
    old = cat.get(name)
    if old is None:
        if entry is None or source is None or tier is None:
            raise CliError("not_found", f"Catalog 中没有 {name};新建须同时给 --entry、--source、--tier")
        new = CatalogEntry(name=name, entry=EntryKind(entry), source=source, version=version or None,
                           added=today or date.today(), tier=Tier(tier), manual_only=bool(manual_only),
                           status=EntryStatus(status or "active"))
    else:
        if old.entry is EntryKind.NATIVE and set(changes) - {"tier", "manual_only"}:
            raise CliError("forbidden", f"{name} 是 native entry,只能改 tier 与 manual_only")
        new = replace(old, **{k: (EntryKind(v) if k == "entry" else Tier(v) if k == "tier"
                                  else EntryStatus(v) if k == "status" else (v or None) if k == "version" else v)
                              for k, v in changes.items()})
    cat[name] = new
    data = catalog_to_dict(cat)
    shared.parse_catalog(data, p.catalog)
    _save(p.catalog, data)
    return entry_dict(new)


# ---- Category ----

def category_dict(c):
    return {"id": c.id, **c.to_dict()}


def _write_categories(p, cats):
    data = categories_to_dict(cats)
    shared.parse_categories(data, p.categories)
    _save(p.categories, data)


def _need(cats, cid):
    if cid not in cats:
        raise CliError("not_found", f"Category {cid} 不存在")
    return cats[cid]


def membership(cats, ref):
    return {"member": str(ref), "categories": sorted(c.id for c in cats.values() if ref in c.members)}


def create_category(p, cid, name, router=None):
    if not shared.CATEGORY_ID.match(cid):
        raise CliError("bad_input", f"Category id 格式不对: {cid!r}")
    cats = load_categories(p)
    if cid in cats:
        raise CliError("conflict", f"Category {cid} 已存在")
    c = Category(id=cid, name=name, router=router or None)
    if c.parent and c.parent not in cats:
        raise CliError("not_found", f"父 Category {c.parent} 不存在")
    cats[cid] = c
    _write_categories(p, cats)
    return category_dict(c)


def delete_category(p, cid):
    """只删空的 Category (没有成员、没有子 Category);否则 conflict,先把成员移走。"""
    cats = load_categories(p)
    _need(cats, cid)
    if cats[cid].members or any(c.parent == cid for c in cats.values()):
        raise CliError("conflict", f"Category {cid} 不是空的: 先移走成员与子 Category")
    del cats[cid]
    _write_categories(p, cats)
    return {"deleted": cid}


def copy_member(p, member, to):
    ref = MemberRef.parse(member)
    cats = load_categories(p)
    dst = _need(cats, to)
    if ref not in dst.members:
        cats[to] = replace(dst, members=dst.members + (ref,))
        _write_categories(p, cats)
    return membership(cats, ref)


def remove_member(p, member, frm):
    ref = MemberRef.parse(member)
    cats = load_categories(p)
    src = _need(cats, frm)
    if ref in src.members:
        cats[frm] = replace(src, members=tuple(m for m in src.members if m != ref))
        _write_categories(p, cats)
    return membership(cats, ref)


def move_member(p, member, frm, to):
    ref = MemberRef.parse(member)
    cats = load_categories(p)
    src, dst = _need(cats, frm), _need(cats, to)
    if ref not in src.members:
        raise CliError("not_found", f"{ref} 不在 {frm} 中")
    cats[frm] = replace(src, members=tuple(m for m in src.members if m != ref))
    if ref not in dst.members:
        cats[to] = replace(dst, members=dst.members + (ref,))
    _write_categories(p, cats)
    return membership(cats, ref)


# ---- 写进本机 Index ----

# Category id → 节点编号。节点编号只在本机有效,所以映射只存在本机库,不进快照。
MAP_DDL = """CREATE TABLE IF NOT EXISTS shared_categories (
  category_id TEXT PRIMARY KEY,
  node_id     INTEGER NOT NULL UNIQUE REFERENCES nodes(id) ON DELETE CASCADE)"""


REFERENCED_CACHE = Path("~/.local/share/node-index/skills").expanduser()


def entry_path(p, e):
    """Catalog entry 在本 Host 上的目录;native 为 None。不检查是否存在 (Host 缺少时由调用方静默跳过)。
    referenced 的 source 是本机路径 (`/`、`~` 开头) 时就是它,否则 (远程地址) 由安装器
    装到 REFERENCED_CACHE/<name>。"""
    if e.entry is EntryKind.VENDORED:
        return p.skills / e.source
    if e.entry is EntryKind.REFERENCED:
        return Path(e.source).expanduser() if e.source.startswith(("/", "~")) else REFERENCED_CACHE / e.name
    return None


def member_paths(p, catalog, ref):
    """成员在磁盘上可能的位置,按优先级。"""
    if ref.kind is MemberKind.SOP:
        return [p.sops / f"{ref.id}.md"]
    if ref.kind is MemberKind.LESSON:
        return sorted(p.lessons.glob(f"{ref.id}-*.md"))
    e = catalog.get(ref.id)
    own = entry_path(p, e) if e is not None else None
    return ([own] if own else []) + [p.skills / ref.id, p.routers / ref.id]


def find_member_node(conn, p, catalog, ref):
    """成员对应的本机节点 (sqlite Row: id、hier_summary),找不到为 None。
    先按路径找;skill 再按同名的已挂载 Skill 节点兜底 (须唯一),因为同一 skill 在不同 Host 上路径不同。"""
    for path in member_paths(p, catalog, ref):
        r = conn.execute("SELECT id, hier_summary FROM nodes WHERE path = ?", (normalize_path(str(path)),)).fetchone()
        if r:
            return r
    if ref.kind is MemberKind.SKILL:
        rows = conn.execute("SELECT id, hier_summary FROM nodes WHERE category = 'Skill' AND path IS NOT NULL "
                            "AND name = ?", (ref.id,)).fetchall()
        if len(rows) == 1:
            return rows[0]
    return None


def skills_root_in_scope(roots, p):
    target = normalize_path(str(p.skills))
    return any(r["path"] == target for r in roots)


def sync_index(conn, p):
    """把 categories.yaml 写进本机库: 每个 Category 一个无挂载物的 Skill 节点,挂在 shared/skills 的根节点下;
    成员节点挂在 Category 节点下。其他 Skill 扫描根挂到这个根下,已归类的成员从扫描目录下摘掉。Category 节点的出边、以及来自根或其他 Category 节点的入边完全由文件决定,
    多余的删、缺的补 (origin=scan)。重复执行结果相同。"""
    cats, catalog = load_categories(p), load_catalog(p)
    conn.execute(MAP_DDL)
    root_path = normalize_path(str(p.skills))
    out = {"created": [], "renamed": [], "removed": [], "linked": 0, "unlinked": 0, "unresolved": []}
    try:
        with conn:
            row = conn.execute("SELECT id FROM nodes WHERE path = ?", (root_path,)).fetchone()
            root = row["id"] if row else conn.execute(
                "INSERT INTO nodes(name, category, path, path_kind, path_checked_at) VALUES (?,?,?,?,?)",
                (os.path.basename(root_path), "Skill", root_path, "dir", now_iso())).lastrowid
            mapping = {r["category_id"]: r["node_id"] for r in conn.execute("SELECT * FROM shared_categories")}
            for cid in sorted(set(mapping) - set(cats)):
                nid = mapping.pop(cid)
                n = conn.execute("DELETE FROM edges WHERE parent_id = ? OR child_id = ?", (nid, nid)).rowcount
                out["unlinked"] += n
                conn.execute("DELETE FROM nodes WHERE id = ?", (nid,))
                out["removed"].append(cid)
            for cid in sorted(cats):
                c = cats[cid]
                if cid in mapping:
                    cur = conn.execute("SELECT name FROM nodes WHERE id = ?", (mapping[cid],)).fetchone()["name"]
                    if cur != c.name:
                        conn.execute("UPDATE nodes SET name = ?, modified_at = ? WHERE id = ?",
                                     (c.name, now_iso(), mapping[cid]))
                        out["renamed"].append(cid)
                else:
                    mapping[cid] = conn.execute("INSERT INTO nodes(name, category) VALUES (?, 'Skill')",
                                                (c.name,)).lastrowid
                    conn.execute("INSERT INTO shared_categories VALUES (?, ?)", (cid, mapping[cid]))
                    out["created"].append(cid)

            want = []  # 父在前
            for cid in sorted(cats):
                c = cats[cid]
                want.append((mapping[c.parent] if c.parent else root, mapping[cid]))
                for m in c.members:
                    r = find_member_node(conn, p, catalog, m)
                    if r is None:
                        out["unresolved"].append(f"{cid}: {m}")
                    else:
                        want.append((mapping[cid], r["id"]))
            owned = set(mapping.values())
            q = ",".join("?" * len(owned)) or "NULL"
            have = {(e[0], e[1]) for e in conn.execute(
                f"SELECT parent_id, child_id FROM edges WHERE parent_id IN ({q}) "
                f"OR (child_id IN ({q}) AND (parent_id = ? OR parent_id IN ({q})))",
                (*owned, *owned, root, *owned))}
            for e in have - set(want):
                conn.execute("DELETE FROM edges WHERE parent_id = ? AND child_id = ?", e)
                out["unlinked"] += 1
            seen = set()
            for e in want:
                if e not in have and e not in seen:
                    conn.execute("INSERT INTO edges VALUES (?, ?, 'scan')", e)
                    out["linked"] += 1
                seen.add(e)

            # 没有父节点的 Skill 节点: 所在目录有节点的 (移出全部 Category 的成员) 挂回该目录,
            # 否则是另一个 Skill 扫描根,挂到 Skill 根下。只有一个 Skill 根。
            for nid, path in conn.execute(
                    "SELECT id, path FROM nodes n WHERE category = 'Skill' AND path IS NOT NULL AND id != ? "
                    "AND NOT EXISTS (SELECT 1 FROM edges e WHERE e.child_id = n.id)", (root,)).fetchall():
                par = conn.execute("SELECT id FROM nodes WHERE path = ?", (os.path.dirname(path),)).fetchone()
                conn.execute("INSERT INTO edges VALUES (?, ?, 'scan')", (par[0] if par else root, nid))
                out["linked"] += 1
            # 已归类的成员只经 Category 找到,不再挂在扫描目录下;未归类的留在扫描目录里当收件箱
            members = {c for par, c in want if par in owned and c not in owned}
            folders = {root} | {r[0] for r in conn.execute(
                "SELECT e.child_id FROM edges e JOIN nodes n ON n.id = e.child_id WHERE e.parent_id = ? "
                "AND n.path_kind = 'dir' AND EXISTS (SELECT 1 FROM edges k WHERE k.parent_id = n.id)", (root,))} - owned
            for par, c in conn.execute("SELECT parent_id, child_id FROM edges").fetchall():
                if par in folders and c in members:
                    conn.execute("DELETE FROM edges WHERE parent_id = ? AND child_id = ?", (par, c))
                    out["unlinked"] += 1
    except sqlite3.IntegrityError as e:
        raise CliError("conflict", f"categories.yaml 写进 Index 失败: {e}") from None
    return out
