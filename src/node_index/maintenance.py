import fnmatch
import json
import os
from pathlib import Path

from .db import (CATEGORIES, CliError, fmt_id, mount_mtime_iso,
                 normalize_path, now_iso)
from .ops import brief


def load_scope(path):
    try:
        import yaml
    except ImportError:
        raise CliError("bad_input", "需要 PyYAML: pip install pyyaml")
    p = Path(path).expanduser()
    if not p.exists():
        raise CliError("bad_input", f"收录范围清单不存在: {p}(可复制 config/scope.example.yml)")
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    roots = list(data.get("roots") or [])
    if data.get("host_roots"):  # 共享规则之后接本机扫描根 (host/host.yml 的 scan_roots)
        from . import shared
        roots += shared.load_host(shared.paths()).scan_roots
    if not roots:
        raise CliError("bad_input", "scope.yml 里没有 roots")
    glob = list(data.get("global_exclude") or [])
    out = []
    for r in roots:
        cat = r.get("category")
        if cat not in CATEGORIES:
            raise CliError("bad_input", f"根 {r.get('path')} 的 category 应为 {'/'.join(CATEGORIES)}")
        depth = r.get("depth", 1)
        if not isinstance(depth, int) or depth < 0:
            raise CliError("bad_input", f"根 {r.get('path')} 的 depth 应为非负整数")
        rp = str(r["path"])
        if not os.path.isabs(os.path.expanduser(rp)):  # 相对路径指本仓库内的共享目录,如 shared/reference
            from .shared import repo_root
            rp = str(repo_root() / rp)
        rp = normalize_path(rp)
        if not os.path.isdir(rp):
            raise CliError("bad_input", f"根路径不是文件夹或不存在: {rp}")
        out.append({"path": rp, "category": cat, "depth": depth,
                    "include_files": bool(r.get("include_files", False)),
                    "exclude": glob + list(r.get("exclude") or [])})
    return out


def _excluded(rel, name, is_dir, patterns):
    for pat in patterns:
        dir_only = pat.endswith("/")
        pat = pat.rstrip("/")
        if dir_only and not is_dir:
            continue
        target = rel if "/" in pat else name
        if fnmatch.fnmatchcase(target, pat):
            return True
    return False


def walk_scope(roots):
    """按父先于子的顺序返回范围内应有节点的路径。"""
    entries = []
    for r in roots:
        entries.append({"path": r["path"], "kind": "dir", "parent_path": None,
                        "category": r["category"]})

        def descend(d, rel_base, level):
            if level > r["depth"]:
                return
            try:
                items = sorted(os.scandir(d), key=lambda e: e.name)
            except OSError:
                return
            for it in items:
                rel = f"{rel_base}/{it.name}" if rel_base else it.name
                is_dir = it.is_dir()
                if _excluded(rel, it.name, is_dir, r["exclude"]):
                    continue
                if is_dir:
                    entries.append({"path": it.path, "kind": "dir", "parent_path": d,
                                    "category": r["category"]})
                    if not it.is_symlink():
                        descend(it.path, rel, level + 1)
                elif r["include_files"] and it.name.lower().endswith(".md"):
                    entries.append({"path": it.path, "kind": "file", "parent_path": d,
                                    "category": r["category"]})

        descend(r["path"], "", 1)
    return entries


def scan(conn, scope_path, dry_run=False):
    entries = walk_scope(load_scope(scope_path))
    known = {r["path"]: r["id"] for r in conn.execute("SELECT id, path FROM nodes WHERE path IS NOT NULL")}
    plan, seen = [], set(known)
    for e in entries:  # 同一路径可同时出现在浅根的子目录和深根的根上,只保留首次出现
        if e["path"] not in seen:
            seen.add(e["path"])
            plan.append(e)
    if dry_run:
        return {"would_create": [
            {"name": os.path.basename(e["path"]) or e["path"], "category": e["category"],
             "path": e["path"], "path_kind": e["kind"], "parent_path": e["parent_path"]}
            for e in plan]}
    created = []
    with conn:
        for e in plan:
            cur = conn.execute(
                """INSERT INTO nodes(name, category, path, path_kind, pending, path_checked_at)
                   VALUES (?,?,?,?,1,?)""",
                (os.path.basename(e["path"]) or e["path"], e["category"], e["path"], e["kind"], now_iso()))
            nid = cur.lastrowid
            known[e["path"]] = nid
            parent = known.get(e["parent_path"]) if e["parent_path"] else None
            if parent is not None:
                conn.execute("INSERT INTO edges VALUES (?,?,'scan')", (parent, nid))
            created.append(nid)
    rows = [conn.execute("SELECT * FROM nodes WHERE id = ?", (i,)).fetchone() for i in created]
    return {"created": [brief(r) for r in rows]}


def check_paths(conn):
    rows = conn.execute("SELECT * FROM nodes WHERE path IS NOT NULL").fetchall()
    missing = []
    with conn:
        for r in rows:
            ok = os.path.exists(r["path"])
            conn.execute("UPDATE nodes SET path_state = ?, path_checked_at = ? WHERE id = ?",
                         ("ok" if ok else "missing", now_iso(), r["id"]))
            if not ok:
                missing.append(conn.execute("SELECT * FROM nodes WHERE id = ?", (r["id"],)).fetchone())
    return {"checked": len(rows), "missing": [brief(r) for r in missing]}


def report(conn, scope_path):
    entries = walk_scope(load_scope(scope_path))
    mounted = {r["path"] for r in conn.execute("SELECT path FROM nodes WHERE path IS NOT NULL")}
    stale = 0
    for r in conn.execute("SELECT path, content_src_mtime FROM nodes WHERE content_summary IS NOT NULL"):
        if r["path"] and os.path.exists(r["path"]):
            if not r["content_src_mtime"] or mount_mtime_iso(r["path"]) > r["content_src_mtime"]:
                stale += 1
    return {
        "nodes": conn.execute("SELECT count(*) FROM nodes").fetchone()[0],
        "pending": conn.execute("SELECT count(*) FROM nodes WHERE pending = 1").fetchone()[0],
        "missing": conn.execute("SELECT count(*) FROM nodes WHERE path_state = 'missing'").fetchone()[0],
        "uncovered": [e["path"] for e in entries if e["path"] not in mounted],
        "stale_content_summaries": stale,
    }


def snapshot(conn, out=None):
    """默认写 snapshots/<host>.jsonl,host 取 host/host.yml。"""
    if out:
        target = Path(out).expanduser()
    else:
        from . import shared
        p = shared.paths()
        target = p.snapshot(shared.load_host(p).host)
    target.parent.mkdir(parents=True, exist_ok=True)
    edges = {}
    for e in conn.execute("SELECT parent_id, child_id, origin FROM edges ORDER BY parent_id"):
        edges.setdefault(e["child_id"], []).append({"parent": fmt_id(e["parent_id"]), "origin": e["origin"]})
    lines = []
    for r in conn.execute("SELECT * FROM nodes ORDER BY id"):
        d = {k: r[k] for k in r.keys()}
        d["id"] = fmt_id(d["id"])
        d["parents"] = edges.get(r["id"], [])
        lines.append(json.dumps(d, ensure_ascii=False) + "\n")
    data = "".join(lines)
    changed = not (target.exists() and target.read_text(encoding="utf-8") == data)
    if changed:
        target.write_text(data, encoding="utf-8")
    return {"path": str(target), "nodes": len(lines), "changed": changed}
