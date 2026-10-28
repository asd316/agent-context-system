import argparse
import json
import os
import sys
from pathlib import Path

from . import access, maintenance, ops, tree
from .db import DEFAULT_SCOPE, REPO, CliError, connect, parse_id


def _require_maintenance_dir():
    from .shared import repo_root
    cwd = Path.cwd().resolve()
    # NODE_INDEX_REPO 指向另一份仓库 (测试、Sync) 时,在那份仓库里执行也算
    if not any(cwd == r or r in cwd.parents for r in {REPO.resolve(), repo_root().resolve()}):
        raise CliError("forbidden", f"写命令只能在 {REPO} 内执行,当前目录是 {cwd}")


def _common():
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--json", action="store_true", help="输出 JSON")
    p.add_argument("--db")
    p.add_argument("--scope", default=str(DEFAULT_SCOPE))
    p.add_argument("--no-count", action="store_true",
                   help="get/children/ancestors 不累加访问计数 (环境变量 NODE_INDEX_NO_COUNT=1 同效)")
    return p


def _build_parser():
    common = _common()
    ap = argparse.ArgumentParser(prog="node-index", description="全局节点索引")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, write=False, **kw):
        sp = sub.add_parser(name, parents=[common], **kw)
        sp.set_defaults(write=write)
        return sp

    def actor(sp):
        sp.add_argument("--actor", required=True, choices=["human", "ai"])

    s = add("search"); s.add_argument("query"); s.add_argument("--limit", type=int, default=20)
    s = add("get"); s.add_argument("id")
    s = add("children"); s.add_argument("id")
    s = add("ancestors"); s.add_argument("id")
    s = add("tree"); s.add_argument("--depth", type=int, required=True); s.add_argument("--root")
    add("coverage")

    s = add("add", write=True)
    s.add_argument("--name", required=True)
    s.add_argument("--category", required=True)
    s.add_argument("--hier-summary", default="")
    s.add_argument("--path"); s.add_argument("--status")
    s.add_argument("--parent", action="append", default=[])
    actor(s)

    s = add("set", write=True); s.add_argument("id")
    s.add_argument("--name"); s.add_argument("--category")
    s.add_argument("--hier-summary"); s.add_argument("--content-summary")
    s.add_argument("--status", help="传空字符串清除状态"); s.add_argument("--path")
    actor(s)

    s = add("rm", write=True); s.add_argument("id"); s.add_argument("--detach", action="store_true"); actor(s)
    s = add("confirm", write=True); s.add_argument("id")
    s.add_argument("--subtree", action="store_true", help="连同全部后代一起确认")
    s = add("link", write=True); s.add_argument("parent"); s.add_argument("child"); actor(s)
    s = add("unlink", write=True); s.add_argument("parent"); s.add_argument("child"); actor(s)

    add("check", write=True)
    add("ingest-access", write=True)
    s = add("scan", write=True); s.add_argument("--dry-run", action="store_true")
    s = add("render", write=True); s.add_argument("--depth", type=int, required=True); s.add_argument("--out")
    s = add("snapshot", write=True); s.add_argument("--out")
    s = add("layer", write=True)
    s.add_argument("--plan", required=True, help="分层方案文件(plan.json)")
    s.add_argument("--dry-run", action="store_true", help="只计算将发生的变化,不写库")
    s.add_argument("--detach", "--detach-root", dest="detach", action="store_true",
                   help="节点归类后,移除它与 detach_from 所列父节点(未写则为根节点)之间原有的边")
    s = add("summarize", write=True)
    s.add_argument("--ids", help="只处理这些节点,逗号分隔,如 n1,n2")
    s.add_argument("--limit", type=int, default=None, help="最多处理多少个;0 或不填表示不限")
    s.add_argument("--force", action="store_true", help="连已有的 ai 摘要也重写(人写的永远不覆盖)")
    s.add_argument("--dry-run", action="store_true", help="只列出目标并给出一条示例提示词,不调用模型")
    s.add_argument("--workers", type=int, default=None)
    s.add_argument("--env", help=".env 路径,默认仓库根的 .env")

    # ---- P3 S2: Lesson (agent-write,任何目录可执行) ----
    lesson = sub.add_parser("lesson", help="Lesson (经验) 相关命令")
    lsub = lesson.add_subparsers(dest="lesson_cmd", required=True)
    s = lsub.add_parser("add", parents=[common], help="Capture 一条 Lesson")
    s.set_defaults(write=False)
    s.add_argument("--topic", required=True, help="主题,进入文件名,不得含 /")
    s.add_argument("--client", required=True, help="来源 Client,如 claude-code / dsh / pi")
    s.add_argument("--situation", required=True, help="情境")
    s.add_argument("--problem", required=True, help="问题")
    s.add_argument("--fix", required=True, help="正确做法")
    s.add_argument("--host-only", action="store_true", help="只在本 Host 成立,host 取 host/host.yml")

    _inbox_parsers(sub, common)

    # ---- P3: Catalog 与共享 Category (catalog.py) ----
    def group(name, **kw):
        g = sub.add_parser(name, **kw).add_subparsers(dest="sub", required=True)

        def leaf(sname, write):
            sp = g.add_parser(sname, parents=[common])
            sp.set_defaults(write=write)
            return sp
        return leaf

    tiers, statuses = ["core", "router", "leaf"], ["active", "retired"]
    leaf = group("catalog", help="Catalog entry")
    s = leaf("list", False); s.add_argument("--tier", choices=tiers); s.add_argument("--status", choices=statuses)
    s = leaf("set", True); s.add_argument("name")
    s.add_argument("--tier", choices=tiers); s.add_argument("--status", choices=statuses)
    s.add_argument("--manual-only", choices=["true", "false"])
    s.add_argument("--entry", choices=["vendored", "referenced", "native"]); s.add_argument("--source")
    s.add_argument("--version", help="传空字符串清除")
    leaf = group("category", help="共享 Category (shared/categories.yaml)")
    s = leaf("new", True); s.add_argument("id"); s.add_argument("--name", required=True); s.add_argument("--router")
    s = leaf("move", True); s.add_argument("member"); s.add_argument("--from", dest="frm", required=True)
    s.add_argument("--to", required=True)
    s = leaf("copy", True); s.add_argument("member"); s.add_argument("--to", required=True)
    s = leaf("remove", True); s.add_argument("member"); s.add_argument("--from", dest="frm", required=True)

    # ---- P3 S1: Build / Activate / Apply ----
    s = add("build", write=True); s.add_argument("--profile", choices=["orchestrator", "executor"])
    s = add("activate", write=True); s.add_argument("--client")
    s = add("apply", write=True); s.add_argument("--no-push", action="store_true")

    # ---- P3 glue: Digest ----
    s = add("digest", write=True)
    s.add_argument("--dry-run", action="store_true", help="只列出检查单位并给出一条示例提示词,不调用模型")
    s.add_argument("--if-due", action="store_true", help="距上次成功运行不满 7 天就跳过 (每日流水线用)")
    s.add_argument("--workers", type=int, default=8)
    s.add_argument("--env", help=".env 路径,默认仓库根的 .env")
    return ap


def _catalog_cmd(args):
    from . import catalog
    from .shared import paths
    p, c = paths(), args.sub
    if args.cmd == "catalog" and c == "list":
        r = catalog.list_entries(p, args.tier, args.status)
        _emit(args, r, _lines(r, lambda e: f"{e['name']} [{e['tier']}] {e['entry']} {e['status']}"
                              + (" manual-only" if e["manual_only"] else "")))
    elif args.cmd == "catalog":
        mo = None if args.manual_only is None else args.manual_only == "true"
        e = catalog.set_entry(p, args.name, args.tier, mo, args.status, args.entry, args.source, args.version)
        _emit(args, e, "\n".join(f"{k}: {v}" for k, v in e.items()))
    elif c == "new":
        r = catalog.create_category(p, args.id, args.name, args.router)
        _emit(args, r, f"已新建 {r['id']} {r['name']}")
    else:
        r = (catalog.move_member(p, args.member, args.frm, args.to) if c == "move"
             else catalog.copy_member(p, args.member, args.to) if c == "copy"
             else catalog.remove_member(p, args.member, args.frm))
        _emit(args, r, f"{r['member']} 现属: {', '.join(r['categories']) or '无'}")


def _emit(args, data, text):
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    elif text:
        print(text, end="" if text.endswith("\n") else "\n")


def _counting(args):
    return not args.no_count and not os.environ.get("NODE_INDEX_NO_COUNT")


def _brief_line(n):
    marks = (" 📎" if n["mounted"] else "") + (" ⚠" if n["path_state"] == "missing" else "")
    s = f"{n['id']} {n['name']} [{n['category']}]{marks}"
    return s + (f" — {n['hier_summary']}" if n["hier_summary"] else "")


def _lines(items, fn=_brief_line):
    return "\n".join(fn(i) for i in items) + ("\n" if items else "")


def _node_text(n):
    keys = ["id", "name", "category", "hier_summary", "content_summary", "path", "path_kind",
            "path_state", "status", "pending", "hier_by", "content_by", "created_at",
            "modified_at", "path_checked_at", "hits", "last_access"]
    out = [f"{k}: {n[k]}" for k in keys]
    out.append("parents: " + (", ".join(f"{p['parent']}({p['origin']})" for p in n["parents"]) or "无"))
    return "\n".join(out) + "\n"


def run(args):
    if args.cmd in ("report", "inbox"):
        return _run_inbox(args)
    if args.write and not (args.cmd in ("scan", "summarize", "layer", "digest") and args.dry_run):
        _require_maintenance_dir()
    c = args.cmd
    # ---- P3 S2: Lesson,不碰 Index 数据库 ----
    if c == "lesson":
        from . import lessons
        r = lessons.add(args.topic, args.client, args.situation, args.problem, args.fix, args.host_only)
        _emit(args, r, f"{r['id']} {r['path']}\n")
        return
    conn = connect(args.db)
    if c == "search":
        r = ops.search(conn, args.query, args.limit)
        if _counting(args):
            access.log_event(conn, "search", query=args.query)
        _emit(args, r, _lines(r, lambda n: _brief_line(n) + "  ← 根: " +
                              ", ".join(f"{x['name']}({x['id']})" for x in n["roots"])))
    elif c == "get":
        i = parse_id(args.id)
        if _counting(args):
            access.log_event(conn, "get", i)
        n = ops.node_full(conn, i); _emit(args, n, _node_text(n))
    elif c == "children":
        i = parse_id(args.id)
        r = ops.children(conn, i)
        if _counting(args):
            access.log_event(conn, "children", i)
        _emit(args, r, _lines(r))
    elif c == "ancestors":
        i = parse_id(args.id)
        r = ops.ancestor_paths(conn, i)
        if _counting(args):
            access.log_event(conn, "ancestors", i)
        _emit(args, r, "\n".join(" > ".join(f"{n['name']}({n['id']})" for n in p) for p in r) + "\n")
    elif c == "tree":
        root = parse_id(args.root) if args.root else None
        t = tree.build_tree(conn, args.depth, root)
        _emit(args, t, tree.tree_text(t))
    elif c == "coverage":
        r = maintenance.report(conn, args.scope)
        text = (f"节点 {r['nodes']},待确认 {r['pending']},路径失效 {r['missing']},"
                f"内容摘要过期 {r['stale_content_summaries']},范围内未覆盖 {len(r['uncovered'])}\n" +
                "".join(f"  未覆盖: {p}\n" for p in r["uncovered"]))
        _emit(args, r, text)
    elif c == "add":
        n = ops.create_node(conn, args.name, args.category, args.actor, args.hier_summary,
                            args.path, args.status, args.parent)
        _emit(args, n, _node_text(n))
    elif c == "set":
        n = ops.update_node(conn, parse_id(args.id), args.actor, args.name, args.category,
                            args.hier_summary, args.content_summary,
                            None if args.status == "" else args.status, args.path,
                            clear_status=(args.status == ""))
        _emit(args, n, _node_text(n))
    elif c == "rm":
        ops.delete_node(conn, parse_id(args.id), args.actor, args.detach)
        _emit(args, {"deleted": args.id}, f"已删除 {args.id}\n")
    elif c == "confirm" and args.subtree:
        r = ops.confirm_subtree(conn, parse_id(args.id))
        _emit(args, r, f"已确认 {r['confirmed']} 个节点 ({r['root']} 及其后代)\n")
    elif c == "confirm":
        n = ops.confirm_node(conn, parse_id(args.id)); _emit(args, n, _node_text(n))
    elif c == "link":
        e = ops.link(conn, parse_id(args.parent), parse_id(args.child), args.actor)
        _emit(args, e, f"{e['parent']} → {e['child']} (manual)\n")
    elif c == "unlink":
        ops.unlink(conn, parse_id(args.parent), parse_id(args.child), args.actor)
        _emit(args, {"unlinked": [args.parent, args.child]}, "已移除\n")
    elif c == "check":
        r = maintenance.check_paths(conn)
        _emit(args, r, f"检查 {r['checked']} 个,失效 {len(r['missing'])} 个\n" + _lines(r["missing"]))
    elif c == "ingest-access":
        n = access.ingest(conn)
        _emit(args, {"ingested": n}, f"并入 {n} 条访问记录\n")
    elif c in ("catalog", "category"):
        _catalog_cmd(args)
    elif c == "scan":
        r = maintenance.scan(conn, args.scope, args.dry_run)
        from . import catalog, shared
        if not args.dry_run and catalog.skills_root_in_scope(maintenance.load_scope(args.scope), shared.paths()):
            r["categories"] = catalog.sync_index(conn, shared.paths())
        if "would_create" in r:
            _emit(args, r, "".join(f"将新建 {x['path']} [{x['category']}]\n" for x in r["would_create"]))
        else:
            text = f"新建 {len(r['created'])} 个待确认节点\n" + _lines(r["created"])
            if "categories" in r:
                k = r["categories"]
                text += (f"Category: 新建 {len(k['created'])},改名 {len(k['renamed'])},删除 {len(k['removed'])},"
                         f"新建边 {k['linked']},移除边 {k['unlinked']},未找到成员 {len(k['unresolved'])}\n")
                text += "".join(f"  未找到: {u}\n" for u in k["unresolved"])
            _emit(args, r, text)
    elif c == "render":
        r = tree.render_doc(conn, args.depth, args.out)
        _emit(args, r, (f"已写入 {r['path']} ({r['bytes']} 字节)\n" if r["changed"]
                        else f"无变化,未重写 {r['path']} ({r['bytes']} 字节)\n"))
    elif c == "snapshot":
        r = maintenance.snapshot(conn, args.out)
        _emit(args, r, (f"已写入 {r['path']} ({r['nodes']} 个节点)\n" if r["changed"]
                        else f"无变化,未重写 {r['path']} ({r['nodes']} 个节点)\n"))
    elif c == "layer":
        from . import layering
        r = layering.apply(conn, args.plan, args.dry_run, args.detach)
        head = "(预演,未写库)" if r["dry_run"] else ""
        text = (f"{head}新建分类节点 {len(r['created_nodes'])} 个,新建边 {r['linked']} 条,移除旧边 {r['detached']} 条,"
                f"跳过 {len(r['skipped'])} 个\n")
        text += "".join(f"  新建 {x['name']} ← {x['parent']}\n" for x in r["created_nodes"])
        text += "".join(f"  跳过 {x['name']}: {x['reason']}\n" for x in r["skipped"])
        _emit(args, r, text)
    elif c in ("build", "activate", "apply"):
        _rules_cmd(args, conn)
    elif c == "digest":
        _digest_cmd(args, conn)
    elif c == "summarize":
        r = _summarize(args, conn)
        if "sample_prompt" in r:
            _emit(args, r, f"将处理 {r['targets']} 个节点,前几个: {', '.join(r['target_ids'][:10])}\n"
                           f"--- 示例提示词(首个目标)---\n{r['sample_prompt']}\n")
        else:
            text = f"目标 {r['targets']},成功 {len(r['ok'])},失败 {len(r['failed'])},跳过 {len(r['skipped'])}\n"
            text += "".join(f"  失败 {f['id']}: {f['reason']}\n" for f in r["failed"])
            _emit(args, r, text)


# ---- P3 S1: Build / Activate / Apply ----

def _rules_cmd(args, conn):
    from . import activate, rules, shared
    p = shared.paths()
    if args.cmd == "build":
        r = rules.build(p, conn, [shared.Profile(args.profile)] if args.profile else None)
        text = "".join(f"{x['profile']}: {x['rules_path']} ({x['bytes']} 字节, {len(x['skills'])} 个 skill)"
                       f"{'' if x['changed'] else ' 无变化'}\n" for x in r["profiles"])
    elif args.cmd == "activate":
        r = activate.activate(p, args.client)
        text = "".join(f"{x['client']} ({x['profile']}): 规则{'已更新' if x['rules_changed'] else '无变化'}"
                       f", 装入 {x['linked']}, 移除 {x['unlinked']}, 跳过 {x['skipped']}, 冲突 {x['conflicts']}\n"
                       for x in r["clients"])
        text += "".join(f"{x['client']}: 跳过,{x['reason']}\n" for x in r["skipped_clients"])
    else:
        r = activate.apply(p, conn, push=not args.no_push)
        text = "".join(f"{x['step']}: {'ok' if x['ok'] else '失败'} {x['detail']}\n" for x in r["steps"])
    _emit(args, r, text)


# ---- P3 glue: Digest ----

def _digest_cmd(args, conn):
    from . import digest, shared
    r = digest.run(conn, shared.paths(), dry_run=args.dry_run, if_due=args.if_due, workers=args.workers,
                   env_path=args.env)
    if r.get("skipped"):
        text = f"未到期,跳过: 上次 {r['last_run']},下次 {r['next_due']}\n"
    elif "sample_prompt" in r:
        text = f"将检查 {r['targets']} 个单位: {', '.join(r['target_keys'])}\n--- 示例提示词 ---\n{r['sample_prompt']}\n"
    else:
        text = (f"检查 {r['targets']} 个单位,新建 Report {len(r['reports'])},已有未关闭的 {len(r['duplicates'])},"
                f"失败 {len(r['failed'])}\n")
        text += "".join(f"  {x['id']} {x['title']}\n" for x in r["reports"])
        text += "".join(f"  失败 {x['key']}: {x['reason']}\n" for x in r["failed"])
    _emit(args, r, text)


def _summarize(args, conn):
    import os
    from . import summarize
    from .llm import load_env
    env_path = args.env
    limit, workers = args.limit, args.workers
    if limit is None or workers is None:  # 命令行优先,其次 .env,最后默认
        try:
            env = load_env(env_path)
        except CliError:
            env = {}
        if limit is None:
            limit = int(env.get("SUMMARY_MAX_PER_RUN") or 0)
        if workers is None:
            workers = int(env.get("SUMMARY_CONCURRENCY") or 8)
    ids = [x.strip() for x in args.ids.split(",") if x.strip()] if args.ids else []
    return summarize.run(conn, ids=ids, limit=limit, force=args.force, dry_run=args.dry_run,
                         workers=workers, env_path=env_path)


# ---- P3 S4: report / inbox。不连数据库,只读写 shared/inbox 与 shared/decisions.jsonl ----

def _inbox_parsers(sub, common):
    from .shared import Position, ReportKind, ReportStatus
    s = sub.add_parser("report", parents=[common], help="提交一条 Report 到 Inbox")
    s.set_defaults(write=False)
    s.add_argument("kind", choices=[k.value for k in ReportKind])
    for name in ("--title", "--reason", "--evidence", "--client"):
        s.add_argument(name, required=True)
    s.add_argument("--position", required=True, choices=[x.value for x in Position])
    s.add_argument("--target", help="Proposal 的目标文件 (仓库内相对路径)")
    s.add_argument("--diff", help="统一 diff 文件,- 为标准输入")
    s.add_argument("--old"); s.add_argument("--new")

    ib = sub.add_parser("inbox", help="列出、查看、认领、处理 Report")
    ib.set_defaults(write=False)
    isub = ib.add_subparsers(dest="inbox_cmd", required=True)
    s = isub.add_parser("list", parents=[common])
    s.add_argument("--status", action="append", default=[], choices=[x.value for x in ReportStatus])
    s.add_argument("--kind", choices=[k.value for k in ReportKind])
    s = isub.add_parser("show", parents=[common]); s.add_argument("id")
    s = isub.add_parser("claim", parents=[common]); s.add_argument("id"); s.add_argument("--client", required=True)
    s = isub.add_parser("resolve", parents=[common]); s.add_argument("id")
    s.add_argument("--status", required=True, choices=["resolved", "rejected"])
    s.add_argument("--decided-by", required=True)
    s.add_argument("--instruction", help="用户原话;驳回时为理由,必填")
    s.add_argument("--outcome", choices=["as-proposed", "modified", "superseded"],
                   help="superseded 只配 rejected: 被另一个决定整体取代")
    s.add_argument("--change", action="append", default=[], help="提交或文件,可重复")
    s.add_argument("--by", required=True, help="执行处理的 Client")
    s.add_argument("--session", default=os.environ.get("CLAUDE_CODE_SESSION_ID"),
                   help="执行者的会话 id,默认取 CLAUDE_CODE_SESSION_ID")


def _run_inbox(args):
    from . import inbox, shared
    p = shared.paths()
    if args.cmd == "report":
        diff = args.diff
        if diff is not None:
            diff = sys.stdin.read() if diff == "-" else _read_text(diff)
        r = inbox.create(p, args.kind, args.title, args.reason, args.evidence, args.client, args.position,
                         args.target, diff, args.old, args.new)
        return _emit(args, r.to_dict(), f"已提交 {r.id} ({r.status.value}): {p.report_file(r.id)}\n")
    c = args.inbox_cmd
    if c == "list":
        kind = shared.ReportKind(args.kind) if args.kind else None
        rs = inbox.list_reports(p, [shared.ReportStatus(x) for x in args.status], kind)
        text = "".join(f"{r.id} [{r.status.value}] [{r.kind.value}] {r.title}" +
                       (f" (claimed_by {r.claimed_by})" if r.claimed_by else "") + "\n" for r in rs)
        return _emit(args, [inbox.brief(r) for r in rs], text or "(无)\n")
    if c == "show":
        r = inbox.load(p, args.id)
        return _emit(args, r.to_dict(), inbox.show_text(p, r))
    if c == "claim":
        r = inbox.claim(p, args.id, args.client)
        return _emit(args, r.to_dict(), f"{r.id} 已由 {r.claimed_by} 认领 (in-progress)\n")
    r, d = inbox.resolve(p, args.id, args.status, args.decided_by, args.by, args.instruction, args.outcome,
                         args.change, args.session)
    _emit(args, {"report": r.to_dict(), "decision": d.to_dict()},
          f"{r.id} 已 {r.status.value},Decision log 记为 {d.result.value}\n")


def _read_text(path):
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as e:
        raise CliError("bad_input", f"读不了 diff 文件 {path}: {e}") from None


def main(argv=None):
    args = _build_parser().parse_args(argv)
    try:
        run(args)
    except CliError as e:
        if args.json:
            print(json.dumps({"error": e.kind, "message": e.message}, ensure_ascii=False), file=sys.stderr)
        else:
            print(f"错误({e.kind}): {e.message}", file=sys.stderr)
        return e.code
    return 0
