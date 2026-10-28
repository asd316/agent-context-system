from .db import DEFAULT_PROMPT_CONFIG, DEFAULT_PROMPT_DOC, REPO, CliError, fmt_id
from .ops import brief


def build_tree(conn, depth, root=None, exclude_categories=(), collapse_categories=(), categories_only=()):
    """exclude_categories: 略去这些 Category 的最上层节点及其子树;collapse_categories: 这些 Category 的最上层节点
    只列一行、不展开;categories_only: 这些 Category 的最上层节点下只列没有挂载物的节点 (共享 Category),
    磁盘目录与未归类的 skill 不进树。都只在不指定 root 时生效。"""
    if not 1 <= depth <= 10:
        raise CliError("bad_input", "depth 应在 1–10")
    rows = {r["id"]: r for r in conn.execute("SELECT * FROM nodes")}
    kids, parents = {}, {}
    for e in conn.execute("SELECT parent_id, child_id FROM edges"):
        kids.setdefault(e["parent_id"], []).append(e["child_id"])
        parents.setdefault(e["child_id"], []).append(e["parent_id"])

    def key(i):
        return (rows[i]["name"], i)

    if root is not None:
        if root not in rows:
            raise CliError("not_found", f"节点 {fmt_id(root)} 不存在")
        starts = [root]
    else:
        starts = sorted((i for i in rows if i not in parents and rows[i]["category"] not in exclude_categories),
                        key=key)

    reachable, stack = set(), list(starts)
    while stack:
        i = stack.pop()
        if i not in reachable:
            reachable.add(i)
            stack.extend(kids.get(i, []))

    def canonical(i):
        return min(parents[i], key=key)

    def build(i, parent, level):
        node = brief(rows[i])
        node["see"] = None
        node["children"] = []
        if parent is not None and len(parents.get(i, [])) > 1:
            canon = canonical(i)
            if canon != parent and canon in reachable:
                node["see"] = fmt_id(canon)
                return node
        if level < depth and not (parent is None and root is None and rows[i]["category"] in collapse_categories):
            top_only = parent is None and root is None and rows[i]["category"] in categories_only
            for c in sorted(kids.get(i, []), key=key):
                if top_only and rows[c]["path"] is not None:
                    continue
                node["children"].append(build(c, i, level + 1))
        return node

    return [build(i, None, 1) for i in starts]


def _line(n, indent):
    marks = ""
    if n["mounted"]:
        marks += " 📎"
    if n["path_state"] == "missing":
        marks += " ⚠"
    line = f"{'  ' * indent}{n['id']} {n['name']}{marks}"
    if n["see"]:
        return f"{line} → 见 {n['see']}"
    if n["hier_summary"]:
        line += f" — {n['hier_summary']}"
    return line


def tree_text(nodes):
    lines = []

    def walk(n, indent):
        lines.append(_line(n, indent))
        for c in n["children"]:
            walk(c, indent + 1)

    for n in nodes:
        walk(n, 0)
    return "\n".join(lines) + ("\n" if lines else "")


def load_prompt_config(path=DEFAULT_PROMPT_CONFIG):
    try:
        import yaml
    except ImportError:
        raise CliError("bad_input", "需要 PyYAML: pip install pyyaml")
    if not path.exists():
        raise CliError("bad_input", f"提示词文字配置不存在: {path}")
    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    for k in ("title", "intro", "commands_heading", "commands_footer", "notes_heading", "tree_heading"):
        if not isinstance(cfg.get(k), str):
            raise CliError("bad_input", f"{path.name}: {k} 必须是字符串")
    for k in ("commands", "notes"):
        if not isinstance(cfg.get(k), list):
            raise CliError("bad_input", f"{path.name}: {k} 必须是列表")
    for c in cfg["commands"]:
        if not (isinstance(c, dict) and isinstance(c.get("cmd"), str) and isinstance(c.get("desc"), str)):
            raise CliError("bad_input", f"{path.name}: commands 每项需要 cmd 和 desc 字符串")
    if not all(isinstance(n, str) for n in cfg["notes"]):
        raise CliError("bad_input", f"{path.name}: notes 每项必须是字符串")
    return cfg


def prompt_doc(nodes, depth, cfg=None):
    cfg = cfg or load_prompt_config()
    exe = str(REPO / "bin" / "node-index")

    def fill(t):
        return t.replace("{exe}", exe).replace("{depth}", str(depth))

    cmds = "\n".join(f"- `{fill(c['cmd'])}`: {fill(c['desc'])}" for c in cfg["commands"])
    notes = "\n".join(f"- {fill(n)}" for n in cfg["notes"])
    return (f"# {fill(cfg['title'])}\n\n{fill(cfg['intro'])}\n\n"
            f"## {fill(cfg['commands_heading'])}\n{cmds}\n- {fill(cfg['commands_footer'])}\n\n"
            f"## {fill(cfg['notes_heading'])}\n{notes}\n\n"
            f"## {fill(cfg['tree_heading'])}\n{tree_text(nodes)}")


def render_doc(conn, depth, out=None):
    nodes = build_tree(conn, depth)
    from pathlib import Path
    target = Path(out).expanduser() if out else DEFAULT_PROMPT_DOC
    target.parent.mkdir(parents=True, exist_ok=True)
    data = prompt_doc(nodes, depth)
    changed = not (target.exists() and target.read_text(encoding="utf-8") == data)
    if changed:  # 内容相同就不重写,避免无谓改动修改时间而让读取它的 agent 被重新注入
        target.write_text(data, encoding="utf-8")
    return {"path": str(target), "bytes": len(data.encode("utf-8")), "changed": changed}
