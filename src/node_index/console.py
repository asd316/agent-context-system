"""配置网页 (ui/console/server.py) 的读视图与几项只有网页才用的写操作。

读视图把 Shared config、Host config 与 Index 合并成页面要的形状;写操作 (改归类、改 Catalog、
新建 Category、批准驳回) 由服务直接调用 catalog / inbox 里与 CLI 相同的函数,不经这里。
这里只放 CLI 没有对应命令的部分: 切换 Client 的 Profile、未应用改动的计算与撤销、新归类成员。
"""
import re
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

import yaml

from . import activate, inbox, rules, shared
from .catalog import entry_dict, find_member_node
from .db import CliError
from .shared import MemberKind, Paths, Profile

RECENT_DAYS = 7
APPLY_SUBJECT = "apply:"  # activate.apply 的提交信息前缀: 用户在网页上的改动经它提交,不算 agent 归类


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)


# ---- Organize ----

def _categories_at(root, rev, rel):
    r = _git(root, "show", f"{rev}:{rel}")
    if r.returncode != 0:
        return {}
    return shared.parse_categories(yaml.safe_load(r.stdout), f"{rev}:{rel}")


def recent_classified(p, days=RECENT_DAYS):
    """最近 days 天内由提交 (不含 apply 提交) 加进 Category 的成员,且现在仍在其中。
    [{member, category, commit, at}],新的在前;同一 (member, category) 只留最近一次。"""
    rel = str(p.categories.relative_to(p.root))
    log = _git(p.root, "log", f"--since={days}.days", "--format=%h%x09%cI%x09%s", "--", rel)
    if log.returncode != 0:
        return []
    current = shared.load_categories(p)
    out, seen = [], set()
    for line in log.stdout.splitlines():
        sha, at, subject = line.split("\t", 2)
        if subject.startswith(APPLY_SUBJECT):
            continue
        after, before = _categories_at(p.root, sha, rel), _categories_at(p.root, f"{sha}^", rel)
        for cid, c in sorted(after.items()):
            old = set(before[cid].members) if cid in before else set()
            for m in c.members:
                key = (str(m), cid)
                if m in old or key in seen or cid not in current or m not in current[cid].members:
                    continue
                seen.add(key)
                out.append({"member": str(m), "category": cid, "commit": sha, "at": at[:19]})
    return out


def organize(p, conn):
    """GET /api/organize。member_details 只列 Category 自己的成员 (不展开子 Category)。"""
    cats, catalog = shared.load_categories(p), shared.load_catalog(p)

    def detail(ref):
        e = catalog.get(ref.id) if ref.kind is MemberKind.SKILL else None
        node = find_member_node(conn, p, catalog, ref) if conn is not None else None
        return {"ref": str(ref), "kind": ref.kind.value, "tier": e.tier.value if e else None,
                "trait": ((node["hier_summary"] or "") if node else "")[:40]}

    placed = {m.id for c in cats.values() for m in c.members if m.kind is MemberKind.SKILL}
    routers = {c.router for c in cats.values() if c.router}
    return {
        "categories": [{"id": c.id, **c.to_dict(), "member_details": [detail(m) for m in c.members]}
                       for c in (cats[k] for k in sorted(cats))],
        "uncategorized": sorted(n for n, e in catalog.items()
                                if e.status.value == "active" and n not in placed and n not in routers),
        "entries": {n: entry_dict(catalog[n]) for n in sorted(catalog)},
        "recent": recent_classified(p),
        "problems": shared.cross_check(catalog, cats, {x.stem for x in p.sops.glob("*.md")},
                                       {x.name[:15] for x in p.lessons.glob("L-*.md")}),
    }


# ---- Profiles ----

def installed(client):
    """与 activate 的判断一致: rules_path 所在目录不存在,即本机未安装该 Client。"""
    return client.rules_path.parent.is_dir()


def active_profile(client):
    """Client 现在实际在用的 Profile: 读 rules_path 里 build 写下的 profile=;读不出为 None。
    .mdc 的 alwaysApply frontmatter 在文件头注释之前,所以不限定第一行。"""
    try:
        with open(client.rules_path, encoding="utf-8") as f:
            head = f.read(800)
    except OSError:
        return None
    m = re.search(re.escape(rules.HEADER_PREFIX) + r": profile=(\S+)", head)
    return m.group(1) if m else None


def _size(path):
    try:
        return Path(path).stat().st_size
    except OSError:
        return None


def profiles(p, conn):
    """GET /api/profiles。rules_bytes 用与 build 相同的渲染函数估算,不写任何文件。"""
    host = shared.load_host(p)
    all_rules = shared.load_rules(p)
    catalog, cats = shared.load_catalog(p), shared.load_categories(p)
    tree_for = rules.tree_renderer(conn)
    commit, now = rules.source_commit(p.root), datetime.now()
    clients = []
    for name in sorted(host.clients):
        c = host.clients[name]
        clients.append({"client": name, "profile": c.profile.value, "rules_path": str(c.rules_path),
                        "skills_dir": str(c.skills_dir), "installed": installed(c),
                        "active_profile": active_profile(c), "current_bytes": _size(c.rules_path)})
    out = []
    for prof in Profile:
        error = None
        try:
            text = rules.header(prof, commit, now) + rules.render_body(p, all_rules, prof, tree_for)
            size = len(text.encode("utf-8"))
        except CliError as e:
            size, error = 0, e.message
        out.append({
            "profile": prof.value, "rules_bytes": size, "error": error,
            "rules": [{"id": r.id, "title": r.title, "kind": r.kind.value, "order": r.order,
                       "bytes": len(r.body.encode("utf-8"))} for r in all_rules if r.applies(prof)],
            "skills": [{"name": n, "manual_only": m, "tier": catalog[n].tier.value}
                       for n, m in rules.profile_skills(catalog, cats, prof)],
        })
    return {"clients": clients, "profiles": out}


def set_client_profile(p, name, profile):
    """PATCH /api/clients/{name}: 只改 host/host.yml 里该 Client 的 profile 一行,保留注释与其余格式。
    两种写法都认: 块状 (`  name:` 下一行行 `profile: x`) 与行内映射 (`name: {..., profile: x, ...}`)。"""
    prof = Profile(profile).value if profile in {x.value for x in Profile} else None
    if prof is None:
        raise CliError("bad_input", f"profile 只能是 {[x.value for x in Profile]}: {profile!r}")
    host = shared.load_host(p)
    if name not in host.clients:
        raise CliError("not_found", f"Host config 里没有 Client {name!r}")
    lines = p.host.read_text(encoding="utf-8").split("\n")
    start = next(i for i, x in enumerate(lines) if re.match(rf"\s+{re.escape(name)}\s*:", x))
    inline = re.compile(r"(profile:\s*)[a-z]+")
    if "{" in lines[start]:
        lines[start] = inline.sub(rf"\g<1>{prof}", lines[start], count=1)
    else:
        indent = len(lines[start]) - len(lines[start].lstrip())
        for i in range(start + 1, len(lines)):
            x = lines[i]
            if x.strip() and len(x) - len(x.lstrip()) <= indent:
                break
            if x.strip().startswith("profile:"):
                lines[i] = inline.sub(rf"\g<1>{prof}", x, count=1)
                break
    text = "\n".join(lines)
    shared.parse_host(yaml.safe_load(text), p.host)
    rules.atomic_write(p.host, text)
    return {"client": name, "profile": prof}


# ---- 未应用改动 ----

def _base(p):
    return activate.apply_status(p).get("head")


def changes(p):
    """GET /api/changes: shared/ 相对上次 apply 结束时 HEAD 的差异 (含未跟踪文件);从未 apply 时对比当前 HEAD。
    另列出 Profile 已改、尚未 activate 的 Client (host/host.yml 不进 Git,所以单独算);从未 activate 过的不算。"""
    base = _base(p)
    status = {"A": "added", "M": "modified", "D": "deleted", "T": "modified"}
    files = {}
    diff = _git(p.root, "diff", "--name-status", "--no-renames", base or "HEAD", "--", "shared")
    for line in diff.stdout.splitlines():
        code, path = line.split("\t", 1)
        files[path] = status.get(code[0], "modified")
    for path in _git(p.root, "ls-files", "--others", "--exclude-standard", "--", "shared").stdout.splitlines():
        files[path] = "added"
    clients = []
    try:
        host = shared.load_host(p)
    except CliError:
        host = None
    for name in sorted(host.clients if host else ()):
        c = host.clients[name]
        cur = active_profile(c)
        if installed(c) and cur is not None and cur != c.profile.value:
            clients.append({"client": name, "profile": c.profile.value, "active_profile": cur})
    return {"base_commit": base, "files": [{"path": k, "status": v} for k, v in sorted(files.items())],
            "clients": clients}


def discard(p):
    """POST /api/changes/discard: shared/ 恢复到上次 apply 的提交;Profile 改回各 Client 正在用的那个。"""
    ch = changes(p)
    base = ch["base_commit"] or "HEAD"
    paths = [f["path"] for f in ch["files"]]
    untracked = set(_git(p.root, "ls-files", "--others", "--exclude-standard", "--", "shared").stdout.splitlines())
    tracked = [x for x in paths if x not in untracked]
    if tracked:
        r = _git(p.root, "restore", f"--source={base}", "--staged", "--worktree", "--", *tracked)
        if r.returncode != 0:
            raise CliError("failed", f"git restore: {r.stderr.strip()}")
    for x in untracked:  # git restore 不管未跟踪的新文件
        (p.root / x).unlink()
    out = list(paths)
    for c in ch["clients"]:
        if c["active_profile"]:
            set_client_profile(p, c["client"], c["active_profile"])
            out.append(f"host/host.yml#{c['client']}")
    return {"discarded": out}


# ---- Inbox ----

def inbox_list(p, statuses=(), kind=None):
    """与 listInbox 相同,另带 proposal (是否带 change),供页面把 Proposal 排在前面。"""
    rs = inbox.list_reports(p, [shared.ReportStatus(s) for s in statuses],
                            shared.ReportKind(kind) if kind else None)
    return [{**inbox.brief(r), "proposal": r.change is not None} for r in rs]


def report_preview(p, report_id):
    """Proposal 应用后的目标文件: 在临时目录里对目标文件的副本跑 inbox.apply_change (与批准同一函数)。
    目标是 categories.yaml 时再给出前后两棵 Category 树。"""
    r = inbox.load(p, report_id)
    c = r.change
    if c is None:
        return {"target": None, "before": None, "after": None, "conflict": None, "trees": None}
    src = inbox._target_path(p, c.target)
    before = src.read_text(encoding="utf-8") if src.is_file() else None
    after, conflict = None, None
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d) / c.target
        if before is not None:
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(before, encoding="utf-8")
        try:
            inbox.apply_change(Paths(Path(d)), c)
            after = tmp.read_text(encoding="utf-8") if tmp.exists() else ""
        except CliError as e:
            conflict = e.message
    trees = None
    if Path(c.target) == p.categories.relative_to(p.root) and after is not None:
        def tree_of(text):
            cats = shared.parse_categories(yaml.safe_load(text) if text else None, c.target)
            return [{"id": k, **cats[k].to_dict()} for k in sorted(cats)]
        try:
            trees = {"before": tree_of(before or ""), "after": tree_of(after)}
        except CliError as e:
            conflict = conflict or e.message
    return {"target": c.target, "before": before, "after": after, "conflict": conflict, "trees": trees}


def decisions(p):
    return [d.to_dict() for d in reversed(shared.load_decisions(p))]
