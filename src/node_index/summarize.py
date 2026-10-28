import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import ops
from .db import LEAD_DOCS, CliError, fmt_id, mount_mtime_iso, parse_id
from .llm import Llm, LlmError, load_env

HIER_TARGET, CONTENT_TARGET = 32, 170  # 提示里给的目标,留出余量;硬上限仍是 40 / 200
LIST_MAX = 60
LEAD_CHARS = 3000
FILE_CHARS = 5000
SECOND_CHARS = 1500
CHILD_NAMES_MAX = 40


def _head(path, n):
    try:
        with open(path, "rb") as f:
            raw = f.read(n * 4)
        if b"\x00" in raw[:2048]:
            return None
        return raw.decode("utf-8", "replace")[:n]
    except OSError:
        return None


def gather(conn, row):
    """收集喂给模型的材料。只读文件开头,不读 .env 之类的敏感文件。"""
    i = row["id"]
    parents = [r["name"] for r in conn.execute(
        "SELECT n.name FROM edges e JOIN nodes n ON n.id = e.parent_id WHERE e.child_id = ? ORDER BY n.name", (i,))]
    kid_rows = conn.execute(
        "SELECT n.name, n.hier_summary FROM edges e JOIN nodes n ON n.id = e.child_id WHERE e.parent_id = ? ORDER BY n.name", (i,)).fetchall()
    kids = [r["name"] for r in kid_rows]
    lines = [f"节点名: {row['name']}", f"类别: {row['category']}",
             f"上级节点: {'、'.join(parents) if parents else '(无,这是根节点)'}"]
    if kids:
        more = f"(共 {len(kids)} 个,只列前 {CHILD_NAMES_MAX} 个)" if len(kids) > CHILD_NAMES_MAX else ""
        described = [f"{r['name']} — {r['hier_summary']}" if r["hier_summary"] else r["name"] for r in kid_rows[:CHILD_NAMES_MAX]]
        lines.append(f"索引里的子节点{more}:\n" + "\n".join("  · " + d for d in described))
    path = row["path"]
    if path:
        if not os.path.exists(path):
            raise LookupError("挂载路径不存在")
        lines.append(f"挂载物: {path} ({'文件夹' if row['path_kind'] == 'dir' else '文件'})")
        if row["path_kind"] == "dir":
            try:
                names = sorted(n for n in os.listdir(path) if not n.startswith("."))
            except OSError:
                names = []
            shown = [n + ("/" if os.path.isdir(os.path.join(path, n)) else "") for n in names[:LIST_MAX]]
            tail = f" …共 {len(names)} 项" if len(names) > LIST_MAX else ""
            lines.append("目录清单: " + (" | ".join(shown) if shown else "(空文件夹)") + tail)
            lead = next((n for n in LEAD_DOCS if os.path.isfile(os.path.join(path, n))), None)
            if lead:
                lines.append(f"--- {lead} 开头 ---\n{_head(os.path.join(path, lead), LEAD_CHARS) or ''}")
            else:
                mds = [n for n in names if n.lower().endswith(".md")][:2]
                for n in mds:
                    lines.append(f"--- {n} 开头 ---\n{_head(os.path.join(path, n), SECOND_CHARS) or ''}")
        else:
            lines.append("--- 文件开头 ---\n" + (_head(path, FILE_CHARS) or "(无法读取或不是文本)"))
    return "\n".join(lines)


def build_messages(material, has_path):
    content_rule = (f'"content": 内容摘要。说明挂载物具体讲什么、什么情况下该去读它。不超过 {CONTENT_TARGET} 个汉字。'
                    if has_path else '"content": 这个节点没有挂载物,填空字符串。')
    prompt = f"""你在为一个个人知识索引的节点写摘要。读者是日后要决定"要不要往下点开这个节点"的 AI agent。

{material}

要求:
1. "hier": 层级摘要。说明这一层是什么、里面主要有什么。不超过 {HIER_TARGET} 个汉字。不要重复节点名,不要以"某某:"开头,不要句号结尾。
2. {content_rule}
3. 只依据上面给出的材料,不要编造。有"索引里的子节点"及其摘要时,层级摘要要概括它们共同构成的这一层,而不是只罗列名字。
4. 不要在摘要里写出挂载路径(路径是单独字段)。不要以"材料不足"开头;确实缺信息时,先写已知内容,最后用半句话说明未展开的部分。
5. 只输出一个 JSON 对象,形如 {{"hier":"...","content":"..."}},不要代码块,不要其它文字。"""
    return [{"role": "system", "content": "你是严谨的知识库编辑,只输出 JSON。"},
            {"role": "user", "content": prompt}]


def _parse(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("回复里没有 JSON")
    d = json.loads(m.group(0))
    hier = str(d.get("hier", "")).strip().rstrip("。.")
    content = str(d.get("content", "")).strip()
    if not hier:
        raise ValueError("hier 为空")
    return hier, content


def generate(llm, material, has_path, attempts=3):
    """生成并校验长度。超长则带着反馈重问,不截断。"""
    messages = build_messages(material, has_path)
    problem = "未知"
    for _ in range(attempts):
        raw = llm.chat(messages)
        try:
            hier, content = _parse(raw)
            if len(hier) > ops.HIER_MAX:
                problem = f"hier {len(hier)} 字,超过 {ops.HIER_MAX}"
            elif has_path and not content:
                problem = "content 为空"
            elif len(content) > ops.CONTENT_MAX:
                problem = f"content {len(content)} 字,超过 {ops.CONTENT_MAX}"
            else:
                return hier, (content if has_path else None)
        except (ValueError, json.JSONDecodeError) as e:
            problem = f"解析失败: {e}"
        messages = messages + [{"role": "assistant", "content": raw},
                               {"role": "user", "content": f"上一次不合格: {problem}。请改正后只输出 JSON,hier 不超过 {HIER_TARGET} 字,content 不超过 {CONTENT_TARGET} 字。"}]
    raise LlmError(f"多次重试仍不合格: {problem}")


def _is_stale(row):
    if row["content_summary"] is None or not row["path"] or not os.path.exists(row["path"]):
        return False
    return not row["content_src_mtime"] or mount_mtime_iso(row["path"]) > row["content_src_mtime"]


def is_sop_file(row):
    """shared/reference/sops/<id>.md。Lesson 与 lessons/、sops/ 文件夹都不是。"""
    path = row["path"]
    if not path or row["path_kind"] != "file":
        return False
    p = Path(path)
    return p.suffix == ".md" and p.parent.name == "sops" and p.parent.parent.name == "reference"


def select_targets(conn, ids, force):
    rows = conn.execute("SELECT * FROM nodes ORDER BY id").fetchall()
    if ids:
        want = {parse_id(x) for x in ids}
        found = {r["id"] for r in rows}
        for w in sorted(want - found):
            raise CliError("not_found", f"节点 {fmt_id(w)} 不存在")
        rows = [r for r in rows if r["id"] in want]
    out, skipped = [], []
    for r in rows:
        hier_locked = r["hier_by"] == "human" and r["hier_summary"]
        content_locked = r["content_by"] == "human" and r["content_summary"]
        need_content = bool(r["path"]) and not content_locked and (
            r["content_summary"] is None or _is_stale(r) or force)
        # SOP 的 trait 来自这一句。内容要重写时,模型本来就会交回层级摘要,一并留下。文件夹不在此列。
        need_hier = not hier_locked and (r["hier_summary"] == "" or force or (need_content and is_sop_file(r)))
        if need_hier or need_content:
            out.append({"row": r, "need_hier": need_hier, "need_content": need_content})
        elif ids:
            skipped.append({"id": fmt_id(r["id"]), "reason": "无需生成或摘要由人写,不覆盖"})
    return out, skipped


def run(conn, ids=(), limit=0, force=False, dry_run=False, workers=8, env_path=None):
    targets, skipped = select_targets(conn, ids, force)
    if limit and limit > 0:
        targets = targets[:limit]
    if dry_run:
        sample = None
        if targets:
            r = targets[0]["row"]
            try:
                sample = build_messages(gather(conn, r), bool(r["path"]))[1]["content"]
            except LookupError as e:
                sample = f"(首个目标无法取材: {e})"
        return {"targets": len(targets), "target_ids": [fmt_id(t["row"]["id"]) for t in targets[:20]],
                "sample_prompt": sample}
    if not targets:
        return {"targets": 0, "ok": [], "failed": [], "skipped": skipped, "sop_hier": []}
    env = load_env(env_path)
    llm = Llm(env)
    ok, failed, sop_hier = [], [], []

    parents = {}
    for e in conn.execute("SELECT parent_id, child_id FROM edges"):
        parents.setdefault(e["child_id"], []).append(e["parent_id"])
    memo = {}

    def level(i):  # 距根节点的最长层数;DAG 无环,递归安全
        if i not in memo:
            memo[i] = 0 if i not in parents else 1 + max(level(p) for p in parents[i])
        return memo[i]

    def call(job):
        t, material = job
        hier, content = generate(llm, material, bool(t["row"]["path"]))
        return t, hier, content

    # 自底向上: 最深的一层先写,父节点取材时就能看到子节点刚写好的摘要。层内并发,层间串行。
    by_level = {}
    for t in targets:
        by_level.setdefault(level(t["row"]["id"]), []).append(t)
    for lv in sorted(by_level, reverse=True):
        jobs = []
        for t in by_level[lv]:
            r = conn.execute("SELECT * FROM nodes WHERE id = ?", (t["row"]["id"],)).fetchone()
            try:
                jobs.append((dict(t, row=r), gather(conn, r)))  # 取材在主线程(sqlite 连接不跨线程),只有网络请求并发
            except LookupError as e:
                failed.append({"id": fmt_id(r["id"]), "reason": str(e)})
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futs = {pool.submit(call, j): j[0] for j in jobs}
            for f in as_completed(futs):
                t = futs[f]
                i = t["row"]["id"]
                try:
                    _, hier, content = f.result()
                    ops.update_node(conn, i, "ai",
                                    hier_summary=hier if t["need_hier"] else None,
                                    content_summary=content if t["need_content"] and content else None)
                    ok.append(fmt_id(i))
                    if t["need_hier"] and is_sop_file(t["row"]):
                        sop_hier.append(fmt_id(i))
                except (LlmError, CliError) as e:
                    failed.append({"id": fmt_id(i), "reason": getattr(e, "message", str(e))})
    return {"targets": len(targets), "ok": sorted(ok, key=lambda x: int(x[1:])),
            "failed": failed, "skipped": skipped,
            "sop_hier": sorted(sop_hier, key=lambda x: int(x[1:]))}
