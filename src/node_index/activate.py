"""Activate 与 Apply。

Activate: 按 host/host.yml 把各 Client 当前 Profile 的 Build 产物装进 Client。
- 规则文件复制写入 rules_path (原子替换)。后缀为 .mdc 时在正文前加 alwaysApply: true 的 frontmatter。
  rules_path 是软链时拒绝: 写进去会改掉链接目标。
- skills.txt 里的 skill 装进 skills_dir: 普通 skill 是软链;manual_only_mode=frontmatter 的 Manual-only skill
  是一个目录,里面逐项软链到来源,只有 SKILL.md 是加了 disable-model-invocation 的副本 (来源不能改)。
- skill 的来源目录由 catalog.entry_path 决定 (Vendored 在 Skill Library 内,Referenced 可在库外);Native 不装。
- 只管自己装的东西: 记录在 host/activate-state.json。指向 Skill Library 的软链、以及正好指向本次来源的软链
  也认作自己的,这样中途崩溃留下的未记录软链下次能收回。其余同名文件一律不动,列入 conflicts。

Apply: build → activate → commit → push,每步结果写进 host/apply-state.json (供 GET /api/apply/status)。
"""
import json
import os
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from . import rules, shared
from .catalog import entry_path
from .db import CliError
from .shared import EntryStatus, ManualOnlyMode

ACTIVATE_STATE = "activate-state.json"
APPLY_STATE = "apply-state.json"
FARM_MARKER = ".node-index-managed"
LINK, FARM = "link", "farm"


@dataclass(frozen=True)
class Install:
    source: str
    mode: str  # LINK 或 FARM


def _state_path(p, name):
    return p.host.parent / name


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as e:
        raise CliError("bad_input", f"{path}: 不是 JSON ({e});确认无人在写后可删除它") from None


def _write_json(path, data):
    rules.atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


# ---- 单个 skill 的安装形态 ----

def flag_manual_only(text):
    """在 SKILL.md 的 frontmatter 里设 disable-model-invocation: true,其余原样保留。"""
    lines = text.split("\n")
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                meta = [x for x in lines[1:i] if not x.startswith("disable-model-invocation:")]
                return "\n".join(["---", *meta, "disable-model-invocation: true", *lines[i:]])
    return "---\ndisable-model-invocation: true\n---\n" + text


def _farm_matches(target, src):
    want = set(os.listdir(src)) | {FARM_MARKER}
    if set(os.listdir(target)) != want:
        return False
    for name in want - {FARM_MARKER}:
        t = target / name
        if name == "SKILL.md":
            if t.is_symlink() or t.read_text(encoding="utf-8") != flag_manual_only((src / name).read_text(encoding="utf-8")):
                return False
        elif not (t.is_symlink() and os.readlink(t) == str(src / name)):
            return False
    return True


def _matches(target, inst):
    if inst.mode == LINK:
        return target.is_symlink() and os.readlink(target) == inst.source
    return target.is_dir() and not target.is_symlink() and _farm_matches(target, Path(inst.source))


def _is_ours(target, rec, inst, library):
    if rec is None:  # 未记录: 只认指向 Skill Library 的软链,或正好指向本次要装的来源 (Referenced entry 在库外)
        if not target.is_symlink():
            return False
        link = os.readlink(target)
        return Path(link).is_relative_to(library) or (inst is not None and inst.mode == LINK and link == inst.source)
    if rec.mode == LINK:
        return target.is_symlink() and os.readlink(target) == rec.source
    return target.is_dir() and not target.is_symlink() and (target / FARM_MARKER).is_file()


def _install(target, inst):
    src = Path(inst.source)
    if inst.mode == LINK:
        os.symlink(src, target)
        return
    target.mkdir()
    for name in sorted(os.listdir(src)):
        if name == "SKILL.md":
            (target / name).write_text(flag_manual_only((src / name).read_text(encoding="utf-8")), encoding="utf-8")
        else:
            os.symlink(src / name, target / name)
    (target / FARM_MARKER).write_text("由 node-index activate 生成,会被覆盖。\n", encoding="utf-8")


def _remove(target):
    """删自己装的。目录里出现了别人放的文件就留下目录,返回 False。"""
    if target.is_symlink():
        target.unlink()
        return True
    for x in target.iterdir():
        if x.is_symlink() or x.name in ("SKILL.md", FARM_MARKER):
            x.unlink()
    try:
        target.rmdir()
    except OSError:
        return False
    return True


# ---- Activate ----

def _rules_text(rules_path, text):
    """Cursor 的 .mdc 没有 alwaysApply 时只在被 @ 到才生效。profile= 注释留在正文里。"""
    if rules_path.suffix == ".mdc":
        return "---\nalwaysApply: true\n---\n" + text
    return text


def _desired(p, catalog, skills, mode):
    desired, skipped = {}, []
    for name, manual in skills:
        e = catalog.get(name)
        if e is None or e.status is EntryStatus.RETIRED:
            skipped.append(name)
            continue
        src = entry_path(p, e)  # Native 为 None: 由 Client 自己的机制安装
        if src is None or (manual and mode is ManualOnlyMode.SKIP):
            continue
        farm = manual and mode is ManualOnlyMode.FRONTMATTER
        if not src.is_dir() or (farm and not (src / "SKILL.md").is_file()):
            skipped.append(name)
            continue
        desired[name] = Install(str(src.resolve()), FARM if farm else LINK)
    return desired, skipped


def _sync_skills(p, client, desired, recorded):
    library = p.skills.resolve()
    linked, unlinked, conflicts, now = [], [], [], {}
    for name in sorted(set(desired) | set(recorded)):
        target, inst, rec = client.skills_dir / name, desired.get(name), recorded.get(name)
        exists = os.path.lexists(target)
        ours = exists and _is_ours(target, rec, inst, library)
        if inst is None:
            if ours:
                (unlinked if _remove(target) else conflicts).append(name)
            continue
        if ours and _matches(target, inst):
            now[name] = inst
            continue
        if ours and not _remove(target):
            conflicts.append(name)
            continue
        if not ours and exists:
            conflicts.append(name)
            continue
        client.skills_dir.mkdir(parents=True, exist_ok=True)
        _install(target, inst)
        now[name] = inst
        linked.append(name)
    return now, linked, unlinked, conflicts


def activate(p, client=None):
    host = shared.load_host(p)
    if client is not None and client not in host.clients:
        raise CliError("not_found", f"Host config 里没有 Client {client!r},可选 {sorted(host.clients)}")
    chosen = [host.clients[client]] if client else [host.clients[n] for n in sorted(host.clients)]
    catalog = shared.load_catalog(p)

    # 先全部检查再动手: 任一 Client 不满足条件就一个也不改
    work, skipped_clients = [], []
    for c in chosen:
        if not c.rules_path.parent.is_dir():
            skipped_clients.append({"client": c.name, "reason": f"目录不存在: {c.rules_path.parent}"})
            continue
        if c.rules_path.is_symlink():
            raise CliError("conflict", f"{c.name}: rules_path {c.rules_path} 是软链 (→ {os.readlink(c.rules_path)})。"
                                       "写进去会改掉链接目标,所以拒绝;先把它换成普通文件再运行 activate")
        d = rules.profile_dir(p, c.profile)
        if not (d / rules.RULES_FILE).is_file() or not (d / rules.SKILLS_FILE).is_file():
            raise CliError("bad_input", f"{c.name}: 没有 Profile {c.profile.value} 的 Build 产物 ({d}),先运行 node-index build")
        work.append((c, d))

    state_path = _state_path(p, ACTIVATE_STATE)
    state = _read_json(state_path) or {"clients": {}}
    out = []
    for c, d in work:
        text = _rules_text(c.rules_path, (d / rules.RULES_FILE).read_text(encoding="utf-8"))
        old = c.rules_path.read_text(encoding="utf-8") if c.rules_path.exists() else None
        if old != text:
            rules.atomic_write(c.rules_path, text)
        desired, skipped = _desired(p, catalog, rules.load_skills_txt(d / rules.SKILLS_FILE), c.manual_only_mode)
        recorded = {n: Install(**r) for n, r in state["clients"].get(c.name, {}).get("installed", {}).items()}
        now, linked, unlinked, conflicts = _sync_skills(p, c, desired, recorded)
        state["clients"][c.name] = {"skills_dir": str(c.skills_dir),
                                    "installed": {n: asdict(i) for n, i in sorted(now.items())}}
        _write_json(state_path, state)  # 每个 Client 做完就落盘,中途失败也不丢记录
        out.append({"client": c.name, "profile": c.profile.value, "rules_path": str(c.rules_path),
                    "rules_changed": old != text, "linked": linked, "unlinked": unlinked,
                    "skipped": skipped, "conflicts": conflicts})
    return {"clients": out, "skipped_clients": skipped_clients}


# ---- Apply ----

def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)


def _last_line(r):
    msg = " ".join(l.strip() for l in (r.stderr or r.stdout).strip().splitlines() if l.strip())
    return msg[:500] or f"退出码 {r.returncode}"


def _alive(pid):
    try:
        os.kill(pid, 0)
    except (OSError, TypeError):
        return False
    return True


def _commit(p, host_name, at):
    root = p.root
    r = _git(root, "add", "-A", "--", "shared")
    if r.returncode:
        raise CliError("failed", f"git add: {_last_line(r)}")
    if _git(root, "diff", "--cached", "--quiet", "--", "shared").returncode == 0:
        return None, "shared/ 没有改动,未提交"
    r = _git(root, "commit", "-q", "-m", f"apply: {host_name} {at}", "--", "shared")
    if r.returncode:
        raise CliError("failed", f"git commit: {_last_line(r)}")
    sha = _git(root, "rev-parse", "--short", "HEAD").stdout.strip()
    return sha, f"已提交 {sha}"


def apply_status(p):
    """GET /api/apply/status 的数据。从未 apply 时为 idle。"""
    s = _read_json(_state_path(p, APPLY_STATE))
    if s is None:
        return {"state": "idle", "steps": [], "commit": None, "pushed": False, "at": None,
                "unpushed_reason": None, "head": None}
    if s["state"] == "running" and not _alive(s.get("pid")):
        s["state"] = "failed"  # 进程已不在: 上次 apply 被中断
    s.pop("pid", None)
    return s


def apply(p, conn, push=True):
    state_path = _state_path(p, APPLY_STATE)
    prev = _read_json(state_path)
    if prev and prev.get("state") == "running" and _alive(prev.get("pid")):
        raise CliError("conflict", f"已有一次 apply 在运行 (pid {prev['pid']})")
    try:
        host_name = shared.load_host(p).host
    except CliError:
        host_name = "unknown-host"  # activate 一步会报出具体原因
    at = datetime.now().isoformat(timespec="seconds")
    s = {"state": "running", "steps": [], "commit": None, "pushed": False, "at": at,
         "unpushed_reason": None, "head": None, "pid": os.getpid()}
    _write_json(state_path, s)

    def record(step, ok, detail):
        s["steps"].append({"step": step, "ok": ok, "detail": detail})
        _write_json(state_path, s)

    def finish(state):
        s["state"] = state
        s["head"] = _git(p.root, "rev-parse", "--short", "HEAD").stdout.strip() or None
        s.pop("pid", None)
        _write_json(state_path, s)

    def run(step, fn):
        try:
            detail = fn()
        except Exception as e:  # 外部步骤的任何失败都要进状态文件,再以 failed 退出
            msg = e.message if isinstance(e, CliError) else f"{type(e).__name__}: {e}"
            record(step, False, msg)
            finish("failed")
            raise CliError("failed", f"apply 停在 {step}: {msg}") from None
        record(step, True, detail)

    def do_build():
        r = rules.build(p, conn)
        changed = [x["profile"] for x in r["profiles"] if x["changed"]]
        return f"有变化: {', '.join(changed)}" if changed else "无变化"

    def do_activate():
        r = activate(p)
        parts = [f"{c['client']}{'(规则已更新)' if c['rules_changed'] else ''}" for c in r["clients"]]
        parts += [f"{x['client']}(跳过)" for x in r["skipped_clients"]]
        return ", ".join(parts) or "没有 Client"

    def do_commit():
        sha, detail = _commit(p, host_name, at)
        s["commit"] = sha
        return detail

    run("build", do_build)
    run("activate", do_activate)
    run("commit", do_commit)
    if push:
        r = _git(p.root, "push", "--quiet")
        if r.returncode == 0:
            s["pushed"] = True
            record("push", True, "已推送")
        else:
            s["unpushed_reason"] = _last_line(r)
            record("push", False, f"未推送: {s['unpushed_reason']}")
    else:
        s["unpushed_reason"] = "--no-push"
    finish("done")
    return {"steps": s["steps"], "commit": s["commit"], "pushed": s["pushed"], "unpushed_reason": s["unpushed_reason"]}
