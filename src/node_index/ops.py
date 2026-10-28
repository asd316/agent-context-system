import os
import sqlite3

from .db import (ACTORS, CATEGORIES, CliError, fmt_id, mount_mtime_iso, normalize_path,
                 now_iso, parse_id)

HIER_MAX = 40
CONTENT_MAX = 200
MAX_PATHS = 200


def brief(row):
    return {
        "id": fmt_id(row["id"]),
        "name": row["name"],
        "category": row["category"],
        "hier_summary": row["hier_summary"],
        "mounted": row["path"] is not None,
        "path_state": row["path_state"],
    }


def _row(conn, i):
    row = conn.execute("SELECT * FROM nodes WHERE id = ?", (i,)).fetchone()
    if row is None:
        raise CliError("not_found", f"节点 {fmt_id(i)} 不存在")
    return row


def access_of(conn, i):
    try:
        r = conn.execute("SELECT hits, last_at FROM node_access WHERE node_id = ?", (i,)).fetchone()
    except sqlite3.Error:
        r = None
    return (r["hits"], r["last_at"]) if r else (0, None)


def node_full(conn, i):
    row = _row(conn, i)
    d = brief(row)
    hits, last_at = access_of(conn, i)
    d.update({
        "content_summary": row["content_summary"],
        "path": row["path"],
        "path_kind": row["path_kind"],
        "status": row["status"],
        "created_at": row["created_at"],
        "modified_at": row["modified_at"],
        "pending": bool(row["pending"]),
        "path_checked_at": row["path_checked_at"],
        "hier_by": row["hier_by"],
        "content_by": row["content_by"],
        "hits": hits,
        "last_access": last_at,
        "parents": [
            {"parent": fmt_id(e["parent_id"]), "child": fmt_id(i), "origin": e["origin"]}
            for e in conn.execute(
                "SELECT parent_id, origin FROM edges WHERE child_id = ? ORDER BY parent_id", (i,))
        ],
    })
    return d


# ---- 读 ----

def roots_of(conn, i):
    rows = conn.execute(
        """WITH RECURSIVE a(id) AS (
               SELECT ? UNION SELECT e.parent_id FROM edges e JOIN a ON e.child_id = a.id)
           SELECT n.id, n.name FROM a JOIN nodes n ON n.id = a.id
           WHERE NOT EXISTS (SELECT 1 FROM edges WHERE child_id = a.id)
           ORDER BY n.name, n.id""", (i,)).fetchall()
    return [{"id": fmt_id(r["id"]), "name": r["name"]} for r in rows]


def search(conn, query, limit=20):
    q = query.strip()
    if not q:
        raise CliError("bad_input", "搜索词不能为空")
    if not 1 <= limit <= 100:
        raise CliError("bad_input", "limit 应在 1–100")
    if len(q) >= 3:
        phrase = '"' + q.replace('"', '""') + '"'
        rows = conn.execute(
            """SELECT n.* FROM nodes_fts JOIN nodes n ON n.id = nodes_fts.rowid
               WHERE nodes_fts MATCH ? ORDER BY rank LIMIT ?""", (phrase, limit)).fetchall()
    else:
        pat = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        rows = conn.execute(
            """SELECT * FROM nodes WHERE name LIKE ?1 ESCAPE '\\'
                  OR hier_summary LIKE ?1 ESCAPE '\\' OR content_summary LIKE ?1 ESCAPE '\\'
               ORDER BY id LIMIT ?2""", (pat, limit)).fetchall()
    out = []
    for r in rows:
        d = brief(r)
        d["roots"] = roots_of(conn, r["id"])
        out.append(d)
    return out


def children(conn, i):
    _row(conn, i)
    rows = conn.execute(
        """SELECT n.* FROM edges e JOIN nodes n ON n.id = e.child_id
           WHERE e.parent_id = ? ORDER BY n.name, n.id""", (i,)).fetchall()
    return [brief(r) for r in rows]


def ancestor_paths(conn, i):
    _row(conn, i)
    paths = []

    def up(cur, stack):
        if len(paths) >= MAX_PATHS:
            return
        stack = stack + [cur]
        parents = [r["parent_id"] for r in conn.execute(
            "SELECT parent_id FROM edges WHERE child_id = ? ORDER BY parent_id", (cur,))]
        if not parents:
            paths.append(list(reversed(stack)))
            return
        for p in parents:
            up(p, stack)

    up(i, [])
    return [[brief(_row(conn, x)) for x in p] for p in paths]


# ---- 写 ----

def _check_actor(actor):
    if actor not in ACTORS:
        raise CliError("bad_input", "actor 应为 human 或 ai")


def _check_lengths(hier=None, content=None):
    if hier is not None and len(hier) > HIER_MAX:
        raise CliError("bad_input", f"层级摘要 {len(hier)} 字,超过上限 {HIER_MAX}")
    if content is not None and len(content) > CONTENT_MAX:
        raise CliError("bad_input", f"内容摘要 {len(content)} 字,超过上限 {CONTENT_MAX}")


def _existing_path(path):
    p = normalize_path(path)
    if not os.path.exists(p):
        raise CliError("bad_input", f"路径不存在: {p}")
    return p, ("dir" if os.path.isdir(p) else "file")


def _map_integrity(e):
    msg = str(e)
    if "cycle" in msg:
        return CliError("conflict", "会形成环")
    if "UNIQUE" in msg and "nodes.path" in msg:
        return CliError("conflict", "该路径已被其他节点挂载")
    if "UNIQUE" in msg and "edges" in msg:
        return CliError("conflict", "这条边已存在")
    if "FOREIGN KEY" in msg:
        return CliError("conflict", "节点仍被边引用")
    return CliError("bad_input", f"数据不合法: {msg}")


def create_node(conn, name, category, actor, hier_summary="", path=None, status=None, parents=()):
    _check_actor(actor)
    if not name or not name.strip():
        raise CliError("bad_input", "名称不能为空")
    if category not in CATEGORIES:
        raise CliError("bad_input", f"类别应为 {'/'.join(CATEGORIES)}")
    _check_lengths(hier=hier_summary)
    p = kind = None
    if path:
        p, kind = _existing_path(path)
    parent_ids = [parse_id(x) for x in parents]
    for pid in parent_ids:
        _row(conn, pid)
    origin = "manual" if actor == "human" else "scan"
    try:
        with conn:
            cur = conn.execute(
                """INSERT INTO nodes(name, category, hier_summary, path, path_kind, status,
                                     pending, hier_by, path_checked_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (name, category, hier_summary or "", p, kind, status,
                 1 if actor == "ai" else 0,
                 actor if hier_summary else None, now_iso() if p else None))
            nid = cur.lastrowid
            for pid in parent_ids:
                conn.execute("INSERT INTO edges VALUES (?,?,?)", (pid, nid, origin))
    except sqlite3.IntegrityError as e:
        raise _map_integrity(e)
    return node_full(conn, nid)


def update_node(conn, i, actor, name=None, category=None, hier_summary=None,
                content_summary=None, status=None, path=None, clear_status=False):
    _check_actor(actor)
    row = _row(conn, i)
    sets, args = [], []
    if name is not None:
        if not name.strip():
            raise CliError("bad_input", "名称不能为空")
        sets.append("name = ?"); args.append(name)
    if category is not None:
        if category not in CATEGORIES:
            raise CliError("bad_input", f"类别应为 {'/'.join(CATEGORIES)}")
        sets.append("category = ?"); args.append(category)
    _check_lengths(hier=hier_summary, content=content_summary)
    if hier_summary is not None:
        if actor == "ai" and row["hier_by"] == "human" and row["hier_summary"]:
            raise CliError("conflict", "ai 不得覆盖人写的层级摘要")
        sets += ["hier_summary = ?", "hier_by = ?"]; args += [hier_summary, actor]
    if content_summary is not None:
        if actor == "ai" and row["content_by"] == "human" and row["content_summary"]:
            raise CliError("conflict", "ai 不得覆盖人写的内容摘要")
        target = path if path is not None else row["path"]
        if target is None:
            raise CliError("bad_input", "没有挂载物的节点不能写内容摘要")
        src = normalize_path(target)
        if not os.path.exists(src):
            raise CliError("bad_input", f"挂载物不存在,无法记录摘要依据: {src}")
        sets += ["content_summary = ?", "content_by = ?", "content_src_mtime = ?"]
        args += [content_summary, actor, mount_mtime_iso(src)]
    if clear_status:
        sets.append("status = NULL")
    elif status is not None:
        sets.append("status = ?"); args.append(status)
    if path is not None:
        p, kind = _existing_path(path)
        sets += ["path = ?", "path_kind = ?", "path_state = 'ok'", "path_checked_at = ?"]
        args += [p, kind, now_iso()]
    if not sets:
        raise CliError("bad_input", "没有要修改的字段")
    sets.append("modified_at = ?"); args.append(now_iso())
    try:
        with conn:
            conn.execute(f"UPDATE nodes SET {', '.join(sets)} WHERE id = ?", (*args, i))
    except sqlite3.IntegrityError as e:
        raise _map_integrity(e)
    return node_full(conn, i)


def delete_node(conn, i, actor, detach=False):
    _check_actor(actor)
    if actor == "ai":
        raise CliError("forbidden", "ai 不得删除节点")
    _row(conn, i)
    n = conn.execute("SELECT count(*) FROM edges WHERE parent_id = ?1 OR child_id = ?1", (i,)).fetchone()[0]
    if n and not detach:
        raise CliError("conflict", f"节点仍有 {n} 条边,需要 --detach")
    try:
        with conn:
            conn.execute("DELETE FROM edges WHERE parent_id = ?1 OR child_id = ?1", (i,))
            conn.execute("DELETE FROM nodes WHERE id = ?", (i,))
    except sqlite3.IntegrityError as e:
        raise _map_integrity(e)


def confirm_node(conn, i):
    _row(conn, i)
    with conn:
        conn.execute("UPDATE nodes SET pending = 0, modified_at = ? WHERE id = ?", (now_iso(), i))
    return node_full(conn, i)


def confirm_subtree(conn, i):
    _row(conn, i)
    with conn:
        n = conn.execute(
            "UPDATE nodes SET pending = 0, modified_at = ? WHERE pending = 1 AND id IN ("
            "WITH RECURSIVE d(id) AS (SELECT ? UNION SELECT e.child_id FROM edges e JOIN d ON e.parent_id = d.id) "
            "SELECT id FROM d)", (now_iso(), i)).rowcount
    return {"root": fmt_id(i), "confirmed": n}


def link(conn, parent, child, actor):
    _check_actor(actor)
    if actor == "ai":
        raise CliError("forbidden", "ai 不得通过 link 建边,请用 scan")
    _row(conn, parent)
    _row(conn, child)
    try:
        with conn:
            conn.execute("INSERT INTO edges VALUES (?,?,'manual')", (parent, child))
    except sqlite3.IntegrityError as e:
        raise _map_integrity(e)
    return {"parent": fmt_id(parent), "child": fmt_id(child), "origin": "manual"}


def unlink(conn, parent, child, actor):
    _check_actor(actor)
    e = conn.execute("SELECT origin FROM edges WHERE parent_id = ? AND child_id = ?",
                     (parent, child)).fetchone()
    if e is None:
        raise CliError("not_found", f"边 {fmt_id(parent)} → {fmt_id(child)} 不存在")
    if actor == "ai" and e["origin"] == "manual":
        raise CliError("forbidden", "ai 不得移除手工建立的边")
    with conn:
        conn.execute("DELETE FROM edges WHERE parent_id = ? AND child_id = ?", (parent, child))
