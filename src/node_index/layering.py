import json
from pathlib import Path

from . import ops
from .db import CliError, fmt_id

UNPLACED = "ZZ"


def load_plan(path):
    p = Path(path).expanduser()
    if not p.exists():
        raise CliError("bad_input", f"方案文件不存在: {p}")
    try:
        plan = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise CliError("bad_input", f"方案不是合法 JSON: {e}")
    for key in ("root", "groups", "skills"):
        if key not in plan:
            raise CliError("bad_input", f"方案缺少字段 {key}")
    root = plan["root"]
    if isinstance(root, str):
        plan["root"] = root = {"name": root}
    if not isinstance(root, dict) or not (root.get("name") or root.get("path")):
        raise CliError("bad_input", "root 应是名称字符串,或带 name / path 的对象")
    create = root.get("create")
    if create is not None:
        if not root.get("name") or not create.get("category"):
            raise CliError("bad_input", "root.create 需要 root.name 与 create.category")
        if not isinstance(create.get("adopt_roots", []), list):
            raise CliError("bad_input", "create.adopt_roots 应是名称列表")
    if plan.get("detach_from") is not None and not isinstance(plan["detach_from"], list):
        raise CliError("bad_input", "detach_from 应是名称列表")
    sub_ids = {s["id"] for g in plan["groups"] for s in g.get("subgroups", [])}
    for name, v in plan["skills"].items():
        if not v.get("primary"):
            raise CliError("bad_input", f"{name} 缺少 primary")
        for slot in ("primary", "secondary"):
            sid = v.get(slot)
            if sid and sid != UNPLACED and sid not in sub_ids:  # ZZ 是保留值,表示留在原处,不需要定义
                raise CliError("bad_input", f"{name} 的 {slot} 引用了不存在的子类 {sid}")
    return plan


def _one(rows, what):
    if len(rows) != 1:
        raise CliError("bad_input", f"{what} 应恰有一个,实际 {len(rows)} 个")
    return rows[0]


def _find_root(conn, spec):
    """返回 (根节点行 或 None, 是否需要新建)。"""
    if spec.get("path"):
        from .db import normalize_path
        rows = conn.execute("SELECT * FROM nodes WHERE path = ?", (normalize_path(spec["path"]),)).fetchall()
        return _one(rows, f"挂载 {spec['path']} 的节点"), False
    rows = conn.execute("SELECT * FROM nodes WHERE name = ? AND id NOT IN (SELECT child_id FROM edges)",
                        (spec["name"],)).fetchall()
    if not rows and spec.get("create"):
        return None, True
    return _one(rows, f"名为 {spec['name']!r} 的无父节点的根"), False


def _children_named(conn, parent_id, name):
    return conn.execute(
        """SELECT n.* FROM edges e JOIN nodes n ON n.id = e.child_id
           WHERE e.parent_id = ? AND n.name = ?""", (parent_id, name)).fetchall()


def _descendants_named(conn, parent_id, name, category):
    return conn.execute(
        """WITH RECURSIVE d(id) AS (SELECT child_id FROM edges WHERE parent_id = ?
                                    UNION SELECT e.child_id FROM edges e JOIN d ON e.parent_id = d.id)
           SELECT n.* FROM d JOIN nodes n ON n.id = d.id WHERE n.name = ? AND n.category = ?""",
        (parent_id, name, category)).fetchall()


def _pick(rows, name):
    """多个候选时优先带挂载物的;仍有多个则无法唯一确定。"""
    uniq = {r["id"]: r for r in rows}.values()
    mounted = [r for r in uniq if r["path"] is not None]
    pool = mounted or list(uniq)
    if len(pool) > 1:
        raise CliError("bad_input", f"节点名 {name!r} 在方案范围内重复 {len(pool)} 个,无法唯一确定")
    return pool[0] if pool else None


def apply(conn, plan_path, dry_run=False, detach=False):
    plan = load_plan(plan_path)
    spec = plan["root"]
    root, must_create = _find_root(conn, spec)
    category = root["category"] if root else spec["create"]["category"]
    root_name = root["name"] if root else spec["name"]

    # 1) 写入之前做完所有会失败的检查
    adopt = []  # 要挂到根下的现有节点 (按名称找带挂载物的节点)
    for name in (spec.get("create") or {}).get("adopt_roots", []):
        rows = conn.execute("SELECT * FROM nodes WHERE name = ? AND category = ? AND path IS NOT NULL",
                            (name, category)).fetchall()  # 限定同类别: 库里可能有别的类别下的同名文件夹
        adopt.append(_one(rows, f"类别为 {category} 的待收纳挂载节点 {name!r}"))

    search_parents = ([root["id"]] if root else []) + [a["id"] for a in adopt]
    resolved, skipped = {}, []
    for name, v in sorted(plan["skills"].items()):
        if v["primary"] == UNPLACED:
            continue
        hits = [r for pid in search_parents for r in _children_named(conn, pid, name)]
        row = _pick(hits, name)
        if row is None:  # 已被移走 (如上次 detach 之后重跑): 到根与收纳节点的全部后代里找
            hits = [r for pid in search_parents for r in _descendants_named(conn, pid, name, category)]
            row = _pick(hits, name)
        if row is None:
            skipped.append({"name": name, "reason": "库里没有这个节点"})
        else:
            resolved[name] = row

    # 2) 需要建立的大类/子类: 只含有成员的,且不含"待定"
    used = set()
    for name in resolved:
        for slot in ("primary", "secondary"):
            sid = plan["skills"][name].get(slot)
            if sid and sid != UNPLACED:
                used.add(sid)
    groups = [(g, [s for s in g["subgroups"] if s["id"] in used]) for g in plan["groups"]]
    groups = [(g, subs) for g, subs in groups if subs]

    created, linked, detached = [], 0, 0
    virtual = {}  # dry-run 下模拟新建的节点,用负数 id,避免与真实 id 冲突

    def new_virtual():
        return -(len(virtual) + 1)

    def has_edge(p, c):
        return p > 0 and c > 0 and conn.execute(
            "SELECT 1 FROM edges WHERE parent_id = ? AND child_id = ?", (p, c)).fetchone() is not None

    def add_edge(p, c):
        nonlocal linked
        if has_edge(p, c):
            return
        if not dry_run:
            ops.link(conn, p, c, "human")
        linked += 1

    def ensure_dir(parent_id, parent_label, name):
        nonlocal linked
        if parent_id > 0:
            hits = _children_named(conn, parent_id, name)
            if len(hits) > 1:
                raise CliError("bad_input", f"{fmt_id(parent_id)} 下有 {len(hits)} 个重名的 {name!r}")
            if hits:
                return hits[0]["id"]
        key = (parent_id, name)
        if dry_run:
            if key not in virtual:
                virtual[key] = new_virtual()
                created.append({"name": name, "parent": parent_label})
                linked += 1
            return virtual[key]
        node = ops.create_node(conn, name, category, "human", parents=[fmt_id(parent_id)])
        created.append({"name": name, "parent": parent_label})
        linked += 1
        return int(node["id"][1:])

    # 3) 根: 必要时新建,并收纳现有的挂载节点
    if must_create:
        if dry_run:
            root_id = new_virtual()
            virtual[("root", root_name)] = root_id
            created.append({"name": root_name, "parent": ""})
        else:
            node = ops.create_node(conn, root_name, category, "human")
            root_id = int(node["id"][1:])
            created.append({"name": root_name, "parent": ""})
    else:
        root_id = root["id"]
    for a in adopt:
        add_edge(root_id, a["id"])

    # 4) 大类与子类
    sub_node = {}
    for g, subs in groups:
        gid = ensure_dir(root_id, root_name, g["name"])
        for s in subs:
            # 方案里一个大类只定义了一个子类时,不再多建一层同名子类 (按方案而非按实际用到的数量,结构才稳定)
            sub_node[s["id"]] = gid if len(g["subgroups"]) == 1 else ensure_dir(gid, g["name"], s["name"])

    # 5) 挂载节点,并按需移除旧边
    detach_names = set(plan.get("detach_from") or [root_name])
    for name, row in resolved.items():
        v = plan["skills"][name]
        placed = set()
        for slot in ("primary", "secondary"):
            sid = v.get(slot)
            if not sid or sid == UNPLACED:
                continue
            placed.add(sub_node[sid])
            add_edge(sub_node[sid], row["id"])
        if detach:
            for e in conn.execute(
                    """SELECT e.parent_id, p.name FROM edges e JOIN nodes p ON p.id = e.parent_id
                       WHERE e.child_id = ?""", (row["id"],)).fetchall():
                if e["name"] in detach_names and e["parent_id"] not in placed:
                    if not dry_run:
                        ops.unlink(conn, e["parent_id"], row["id"], "human")
                    detached += 1
    return {"dry_run": dry_run, "created_nodes": created, "linked": linked,
            "detached": detached, "skipped": skipped}
