"""Build: 用 Shared config 生成各 Profile 的规则文件与 skills.txt,产物在 build/<profile>/。

规则文件 = 文件头 + 适用于该 Profile 的 Rule block (kind 固定顺序、kind 内按 order)。
文件头含生成时间,所以「内容未变」按文件头之后的正文比较: 正文相同就不重写,时间也不变。
"""
import os
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from . import routers, shared, tree
from .db import CliError
from .shared import EntryStatus, Profile, RuleKind, Tier

RULES_FILE = "rules.md"
SKILLS_FILE = "skills.txt"
TREE_DEPTH = 2
MANUAL_MARK = "manual-only"
HEADER_PREFIX = "<!-- node-index build"


def atomic_write(path, data):
    """同目录临时文件 + rename: 读者要么看到旧内容,要么看到新内容。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def profile_dir(p, profile):
    return p.build / profile.value


# ---- skills.txt ----

def profile_skills(catalog, categories, profile):
    """[(name, manual_only)],按名排序。Core 进所有 Profile;顶层 Category 的 Router 只进 orchestrator。"""
    active = {n: e for n, e in catalog.items() if e.status is EntryStatus.ACTIVE}
    names = {n for n, e in active.items() if e.tier is Tier.CORE}
    if profile is Profile.ORCHESTRATOR:
        names |= {c.router for c in categories.values()
                  if c.parent is None and c.router in active and active[c.router].tier is Tier.ROUTER}
    return [(n, active[n].manual_only) for n in sorted(names)]


def render_skills(profile, skills):
    lines = [f"# node-index build: profile={profile.value}。每行一个 skill;标 {MANUAL_MARK} 的只供用户手动调用。"]
    lines += [f"{n} {MANUAL_MARK}" if manual else n for n, manual in skills]
    return "\n".join(lines) + "\n"


def load_skills_txt(path):
    """[(name, manual_only)]。build 产物,不是 Shared config,格式只由本模块读写。"""
    out = []
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        parts = line.split()
        if not parts or line.startswith("#"):
            continue
        if len(parts) > 2 or (len(parts) == 2 and parts[1] != MANUAL_MARK):
            raise CliError("bad_input", f"{path}:{n}: 无法解析 {line!r},重新运行 node-index build")
        out.append((parts[0], len(parts) == 2))
    return out


# ---- 规则文件 ----

def required_read_path(p, rr, rule):
    sop = p.sops / f"{rr.sop}.md"
    if sop.exists():
        return str(sop)
    if rr.fallback:
        return rr.fallback
    raise CliError("bad_input", f"shared/rules/{rule.id}.md: SOP {rr.sop} 不存在 ({sop}),也没有 fallback")


def tree_renderer(conn):
    """返回 tree_for(exclude_categories, collapse_categories, categories_only) -> 树文本;同一组参数只导出一次。"""
    trees = {}

    def tree_for(exclude, collapse=(), only=()):
        key = (frozenset(exclude), frozenset(collapse), frozenset(only))
        if key not in trees:
            trees[key] = tree.tree_text(tree.build_tree(conn, TREE_DEPTH, exclude_categories=key[0],
                                                        collapse_categories=key[1], categories_only=key[2]))
        return trees[key]
    return tree_for


def render_body(p, rules, profile, tree_for):
    """tree_for(exclude_categories) 返回导出的树文本。"""
    parts, reads = [], []
    for r in rules:
        if not r.applies(profile):
            continue
        if r.kind is RuleKind.REQUIRED_READ:
            rr = shared.parse_required_read(r)
            if not reads:
                parts.append(None)  # Required read 整组渲染成一节,位置在第一块处
            reads.append(f"- {rr.task}: `{required_read_path(p, rr, r)}`")
            continue
        body = r.body.strip().replace("{{sops}}", str(p.sops))
        if r.kind is RuleKind.INDEX:
            body = body.replace("{{tree_depth}}", str(TREE_DEPTH)).replace("{{tree}}", tree_for(r.tree_exclude_categories, r.tree_collapse_categories, r.tree_categories_only).rstrip("\n"))
        parts.append(body)
    section = "## Required read\n\n动手前读对应文件:\n\n" + "\n".join(reads)
    return "\n\n".join(section if x is None else x for x in parts) + "\n"


def source_commit(root):
    def git(*a):
        return subprocess.run(["git", "-C", str(root), *a], capture_output=True, text=True)
    head = git("rev-parse", "--short", "HEAD")
    if head.returncode != 0:
        return "unknown"
    dirty = git("status", "--porcelain", "--", "shared").stdout.strip()
    return head.stdout.strip() + ("+uncommitted" if dirty else "")


def header(profile, commit, now):
    return (f"{HEADER_PREFIX}: profile={profile.value} built={now.isoformat(timespec='seconds')} source={commit} -->\n"
            "<!-- 生成文件,不要手改。改 shared/rules/ 后运行 node-index apply。 -->\n\n")


def split_header(text):
    """返回文件头之后的正文;不是本模块生成的文件返回 None。"""
    if not text.startswith(HEADER_PREFIX):
        return None
    head, sep, body = text.partition("\n\n")
    return body if sep else None


# ---- Build ----

def build(p, conn, profiles=None):
    """先全部校验与渲染,再写盘: Shared config 有错时一个文件也不写。"""
    profiles = profiles or list(Profile)
    rules = shared.load_rules(p)
    catalog = shared.load_catalog(p)
    categories = shared.load_categories(p)
    tree_for = tree_renderer(conn)
    commit, now = source_commit(p.root), datetime.now()

    planned = [(prof, render_body(p, rules, prof, tree_for), profile_skills(catalog, categories, prof))
               for prof in profiles]
    # render_routers 先校验全部 Router 再写;放在规则文件之前,任何一处不合格时两边都不写
    router_out = routers.render_routers(conn, p, dry_run=False)
    out = []
    for prof, body, skills in planned:
        d = profile_dir(p, prof)
        rules_path, skills_path = d / RULES_FILE, d / SKILLS_FILE
        old = rules_path.read_text(encoding="utf-8") if rules_path.exists() else None
        changed = old is None or split_header(old) != body
        if changed:
            atomic_write(rules_path, header(prof, commit, now) + body)
        skills_text = render_skills(prof, skills)
        if not (skills_path.exists() and skills_path.read_text(encoding="utf-8") == skills_text):
            atomic_write(skills_path, skills_text)
            changed = True
        out.append({"profile": prof.value, "rules_path": str(rules_path),
                    "bytes": rules_path.stat().st_size,
                    "skills": [{"name": n, "manual_only": m} for n, m in skills], "changed": changed})
    return {"profiles": out, "routers": router_out}

