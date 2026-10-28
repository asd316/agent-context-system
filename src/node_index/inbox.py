"""Report / Inbox / Decision log: 新建 Report、列出、查看、认领、处理,以及网页的直接批准与驳回。

文件形状与校验全在 shared.py;这里只管状态流转、并发与把 Proposal 应用到目标文件。
"""
import fcntl
import os
import subprocess
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from . import shared
from .db import CliError
from .shared import (REPORT_ID, Decision, DecisionResult, DiffChange, Outcome, ReplaceChange, Report,
                     ReportStatus, Resolution)

CLOSED = (ReportStatus.RESOLVED, ReportStatus.REJECTED)
OPEN = (ReportStatus.PENDING, ReportStatus.IN_PROGRESS)


def _now():
    return datetime.now().replace(microsecond=0)


@contextmanager
def _locked(p):
    """claim/resolve/decide 是读-改-写;对 inbox 目录加排他锁,不在 Git 管理的目录里留锁文件。"""
    p.inbox.mkdir(parents=True, exist_ok=True)
    fd = os.open(p.inbox, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _save(p, report):
    """先写临时文件再改名,并发的 list/show 不会读到半个文件。临时名不匹配 R-*.yaml。"""
    shared.validate_report(report, p.report_file(report.id))
    tmp = p.inbox / f".{report.id}.yaml.tmp"
    shared.write_yaml(tmp, report.to_dict())
    os.replace(tmp, p.report_file(report.id))


def load(p, report_id):
    if not REPORT_ID.match(report_id):
        raise CliError("bad_input", f"Report id 格式不对: {report_id!r},应为 R-YYYYMMDD-xxxx")
    path = p.report_file(report_id)
    if not path.exists():
        raise CliError("not_found", f"Report {report_id} 不存在 ({path})")
    return shared.load_report(path)


# ---- report ----

def create(p, kind, title, reason, evidence, client, position, target=None, diff=None, old=None, new=None):
    """返回新建的 Report。change 的组合是否合法交给 shared 判断。"""
    if target is None and (diff is not None or old is not None or new is not None):
        raise CliError("bad_input", "--diff / --old / --new 须与 --target 一起用")
    change = None
    if target is not None:
        if diff is not None and (old is not None or new is not None):
            raise CliError("bad_input", "--diff 与 --old/--new 只能选一种")
        if diff is None and (old is None or new is None):
            raise CliError("bad_input", "Proposal 须带 --diff,或同时带 --old 与 --new")
        change = {"target": target, "diff": diff} if diff is not None else \
            {"target": target, "replace": {"old": old, "new": new}}
    host = shared.load_host(p).host
    with _locked(p):
        taken = {x.stem for x in p.inbox.glob("R-*.yaml")}
        rid = shared.new_id("R", taken)
        data = {"id": rid, "kind": kind, "title": title, "reason": reason, "evidence": evidence,
                "from": {"client": client, "position": position, "host": host},
                "at": _now().isoformat(timespec="seconds"), "change": change,
                "status": ReportStatus.PENDING.value, "claimed_by": None, "resolution": None}
        report = shared.parse_report(data, "report")
        _save(p, report)
    return report


# ---- inbox list / show ----

def brief(r):
    return {"id": r.id, "kind": r.kind.value, "title": r.title, "status": r.status.value,
            "at": r.to_dict()["at"], "claimed_by": r.claimed_by,
            "decided_by": r.resolution.decided_by if r.resolution else None}


def list_reports(p, statuses=(), kind=None):
    """不给 statuses 时只列未关闭的;按 at 倒序,同一时刻按 id 倒序。"""
    want = set(statuses) or set(OPEN)
    rs = [r for r in shared.load_reports(p).values()
          if r.status in want and (kind is None or r.kind is kind)]
    return sorted(rs, key=lambda r: (r.at, r.id), reverse=True)


def decisions_for(p, report_id):
    return [d for d in shared.load_decisions(p) if d.target == report_id]


def _indent(text, prefix="    "):
    return "".join(prefix + line + "\n" for line in text.rstrip("\n").split("\n"))


def show_text(p, r):
    """给 agent 的全部上下文: 字段、Proposal 原文、已有决定、按当前状态该跑的命令。"""
    out = [f"{r.id} [{r.kind.value}] {r.title}",
           f"status: {r.status.value}" + (f" (claimed_by {r.claimed_by})" if r.claimed_by else ""),
           f"from: {r.from_.client} / {r.from_.position.value} @ {r.from_.host}",
           f"at: {r.to_dict()['at']}",
           "reason:", _indent(r.reason).rstrip("\n"),
           "evidence:", _indent(r.evidence).rstrip("\n")]
    c = r.change
    if c is None:
        out.append("change: 无 (不是 Proposal)")
    elif isinstance(c, DiffChange):
        out += [f"change: {c.target} (统一 diff)", _indent(c.diff).rstrip("\n")]
    else:
        out += [f"change: {c.target} (精确替换)", "  old:", _indent(c.old).rstrip("\n"),
                "  new:", _indent(c.new).rstrip("\n")]
    if r.resolution:
        z = r.resolution
        out += [f"resolution: {z.decided_by} @ {z.at.isoformat(timespec='seconds')}, "
                f"outcome {z.outcome.value if z.outcome else '无'}",
                f"  instruction: {z.instruction or '无'}",
                f"  changes: {', '.join(z.changes) or '无'}"]
    ds = decisions_for(p, r.id)
    if ds:
        out.append("decisions:")
        out += [f"  {d.to_dict()['at']} {d.action} {d.result.value} by {d.by}: {d.instruction or d.note}" for d in ds]
    if r.status in OPEN:
        cl = r.claimed_by or "<client>"
        out += ["",
                "处理步骤 (用户在复制指令里给出的意见即 instruction,原话照抄):",
                f"  1. node-index inbox claim {r.id} --client {cl}",
                "  2. 按用户意见修改" + (f" {c.target}" if c else "相关文件") +
                ";没有意见或意见是照办时按原 Proposal 改。改的是仓库里的源头文件,不是 Build 产物",
                f"  3. node-index inbox resolve {r.id} --status resolved --decided-by user "
                f"--instruction '<用户原话>' --outcome as-proposed|modified --change <提交或文件> --by {cl}",
                f"     驳回: node-index inbox resolve {r.id} --status rejected --decided-by user "
                f"--instruction '<驳回理由>' --by {cl}"]
    return "\n".join(out) + "\n"


def copy_instruction(report_id, text):
    """网页「复制指令」按钮产出的字符串;agent 收到后先 inbox show <id>。"""
    text = (text or "").strip()
    head = f"处理 Inbox {report_id}，我的意见：{text}" if text else f"处理 Inbox {report_id}"
    return f"{head}。按意见修改后应用，并记入 Decision log。"


# ---- claim / resolve ----

def claim(p, report_id, client):
    with _locked(p):
        r = load(p, report_id)
        if r.status in CLOSED:
            raise CliError("conflict", f"{report_id} 已是 {r.status.value},不能认领")
        if r.status is ReportStatus.IN_PROGRESS:
            if r.claimed_by == client:
                return r
            raise CliError("conflict", f"{report_id} 已被 {r.claimed_by} 认领")
        r = replace(r, status=ReportStatus.IN_PROGRESS, claimed_by=client)
        _save(p, r)
    return r


RESULTS = {None: DecisionResult.REJECTED, Outcome.AS_PROPOSED: DecisionResult.APPROVED,
           Outcome.MODIFIED: DecisionResult.MODIFIED, Outcome.SUPERSEDED: DecisionResult.SUPERSEDED}


def _close(p, r, status, decided_by, instruction, outcome, changes, by, action, note, session=None):
    """写 resolution 与 Decision。两者都先校验,校验过了才写盘,避免只写了一半。"""
    if r.status in CLOSED:
        raise CliError("conflict", f"{r.id} 已是 {r.status.value}")
    resolution = Resolution(decided_by=decided_by, instruction=instruction or None, outcome=outcome,
                            changes=tuple(changes), at=_now())
    closed = replace(r, status=status, claimed_by=None, resolution=resolution)
    shared.validate_report(closed, p.report_file(r.id))
    decision = Decision(at=resolution.at, target=r.id, action=action, result=RESULTS[outcome], by=by,
                        instruction=instruction or None, note=None if instruction else note, session=session)
    shared.parse_decision(decision.to_dict(), "decision")
    _save(p, closed)
    shared.append_decision(p, decision)
    return closed, decision


def resolve(p, report_id, status, decided_by, by, instruction=None, outcome=None, changes=(), session=None):
    status, outcome = ReportStatus(status), Outcome(outcome) if outcome else None
    if status not in CLOSED:
        raise CliError("bad_input", "--status 只能是 resolved 或 rejected")
    if status is ReportStatus.RESOLVED and outcome in (None, Outcome.SUPERSEDED):
        raise CliError("bad_input", "--status resolved 须带 --outcome as-proposed 或 modified")
    if status is ReportStatus.REJECTED and outcome not in (None, Outcome.SUPERSEDED):
        raise CliError("bad_input", "--status rejected 只能不带 --outcome 或带 --outcome superseded")
    if status is ReportStatus.REJECTED and not (instruction or "").strip():
        raise CliError("bad_input", "驳回须用 --instruction 写明理由 (用户原话)")
    with _locked(p):
        r = load(p, report_id)
        if r.status is ReportStatus.IN_PROGRESS and r.claimed_by != by:
            raise CliError("conflict", f"{report_id} 由 {r.claimed_by} 认领,不能由 {by} 处理")
        note = f"{outcome.value}; changes: {', '.join(changes) or '无'}" if outcome else None
        return _close(p, r, status, decided_by, instruction, outcome, changes, by, "resolve", note, session or None)


# ---- 网页直接批准 / 驳回 ----

def _target_path(p, target):
    root = p.root.resolve()
    path = (p.root / target).resolve()
    if root not in path.parents:
        raise CliError("bad_input", f"change.target 指向仓库外: {target}")
    return path


def _retarget(diff, target):
    """diff 头里的路径不可信: 只许改一个文件,把头统一改写成 target 再交给 git apply。"""
    lines = diff.split("\n")
    # 文件头是紧接着 +++ 与 @@ 的 --- 行;单看 "--- " 前缀会把删掉的 "-- x" 行误认成头
    heads = [i for i in range(len(lines) - 2) if lines[i].startswith("--- ")
             and lines[i + 1].startswith("+++ ") and lines[i + 2].startswith("@@")]
    if len(heads) != 1:
        raise CliError("bad_input", f"diff 须恰好包含一个文件的 ---/+++ 头,实际 {len(heads)} 个")
    i = heads[0]
    dev_null = lambda line: line[4:].split("\t")[0].strip() == "/dev/null"
    old = "--- /dev/null" if dev_null(lines[i]) else f"--- a/{target}"
    new = "+++ /dev/null" if dev_null(lines[i + 1]) else f"+++ b/{target}"
    body = [x for x in lines[:i] if not x.startswith(("diff --git ", "index "))]
    patch = "\n".join(body + [old, new] + lines[i + 2:])
    return patch if patch.endswith("\n") else patch + "\n"


def _git_apply(root, patch, *flags):
    return subprocess.run(["git", "apply", "-p1", *flags, "-"], cwd=root, input=patch,
                          capture_output=True, text=True)


def apply_change(p, change):
    """把 Proposal 的精确改动写进目标文件。不匹配就报 conflict,文件保持原样。返回改动的相对路径。"""
    path = _target_path(p, change.target)
    if isinstance(change, ReplaceChange):
        if not path.is_file():
            raise CliError("conflict", f"目标文件不存在: {change.target}")
        text = path.read_text(encoding="utf-8")
        n = text.count(change.old)
        if n != 1:
            raise CliError("conflict", f"{change.target} 中 old 出现 {n} 次 (须恰好 1 次),目标已变,未应用")
        path.write_text(text.replace(change.old, change.new, 1), encoding="utf-8")
        return change.target
    patch = _retarget(change.diff, change.target)
    check = _git_apply(p.root, patch, "--check")
    if check.returncode != 0:
        raise CliError("conflict", f"diff 与 {change.target} 当前内容不符,未应用: {check.stderr.strip()}")
    done = _git_apply(p.root, patch)
    if done.returncode != 0:
        raise CliError("failed", f"git apply 失败: {done.stderr.strip()}")
    return change.target


def decide(p, report_id, decision, instruction=None):
    """POST /api/inbox/{id}/decide 调用的函数。decision 为 approve 或 reject。

    批准: 若是 Proposal,先把 change 应用到工作区的目标文件,再记 resolved/as-proposed;
    目标已变时报 conflict,文件与 Report 都不动。之后的 Build/Activate 由「应用」完成。
    驳回: instruction 即理由,必填。
    返回 (report, decision)。
    """
    if decision not in ("approve", "reject"):
        raise CliError("bad_input", f"decision 只能是 approve 或 reject: {decision!r}")
    instruction = (instruction or "").strip() or None
    with _locked(p):
        r = load(p, report_id)
        if r.status in CLOSED:
            raise CliError("conflict", f"{report_id} 已是 {r.status.value}")
        if decision == "reject":
            if instruction is None:
                raise CliError("bad_input", "驳回须附理由 (instruction)")
            return _close(p, r, ReportStatus.REJECTED, "user", instruction, None, (), "user", "decide", None)
        changes = (apply_change(p, r.change),) if r.change else ()
        note = "网页直接批准" + (f",已应用到 {changes[0]}" if changes else "")
        return _close(p, r, ReportStatus.RESOLVED, "user", instruction, Outcome.AS_PROPOSED, changes,
                      "user", "decide", note)
