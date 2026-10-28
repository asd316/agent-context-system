"""端到端测试: 通过真实命令行调用 bin/node-index,契约见 docs/openapi.yaml。"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BIN = REPO / "bin" / "node-index"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = str(self.root / "t.db")

    def run_cli(self, *args, cwd=REPO, expect=0, scope=None):
        cmd = [sys.executable, str(BIN), *args, "--db", self.db]
        if scope:
            cmd += ["--scope", str(scope)]
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           env={**os.environ, "NODE_INDEX_DB": ""})
        self.assertEqual(p.returncode, expect, f"{args}\nstdout={p.stdout}\nstderr={p.stderr}")
        return p

    def j(self, *args, **kw):
        return json.loads(self.run_cli(*args, "--json", **kw).stdout)

    def add(self, name, category="Skill", actor="human", parents=(), **kw):
        a = ["add", "--name", name, "--category", category, "--actor", actor]
        for p in parents:
            a += ["--parent", p]
        for k, v in kw.items():
            a += ["--" + k.replace("_", "-"), v]
        return self.j(*a)["id"]


class Hierarchy(Base):
    def test_add_get_and_ids_never_reused(self):
        a = self.add("甲", hier_summary="第一个")
        self.assertEqual(a, "n1")
        n = self.j("get", a)
        self.assertEqual((n["hier_by"], n["pending"], n["mounted"]), ("human", False, False))
        self.run_cli("rm", a, "--actor", "human")
        self.assertEqual(self.add("乙"), "n2")  # 编号不复用

    def test_ai_created_node_is_pending_and_confirm(self):
        a = self.add("新", actor="ai")
        self.assertTrue(self.j("get", a)["pending"])
        self.run_cli("confirm", a)
        self.assertFalse(self.j("get", a)["pending"])

    def test_confirm_subtree(self):
        r = self.add("根", actor="ai"); a = self.add("甲", parents=[r], actor="ai")
        b = self.add("乙", parents=[a], actor="ai"); other = self.add("别处", actor="ai")
        self.run_cli("confirm", a)
        self.assertEqual(self.j("confirm", r, "--subtree")["confirmed"], 2)  # 甲已确认,不计入
        self.assertFalse(any(self.j("get", x)["pending"] for x in (r, a, b)))
        self.assertTrue(self.j("get", other)["pending"])

    def test_children_sorted_and_ancestors_multi_parent(self):
        k = self.add("知识库", "Knowledge")
        p = self.add("项目", "Project")
        s = self.add("调研视频", parents=[k, p])
        self.add("乙", parents=[k]); self.add("甲", parents=[k])
        names = [c["name"] for c in self.j("children", k)]
        self.assertEqual(names, sorted(names))
        paths = self.j("ancestors", s)
        self.assertEqual(len(paths), 2)
        self.assertEqual({tuple(n["name"] for n in x) for x in paths},
                         {("知识库", "调研视频"), ("项目", "调研视频")})

    def test_cycle_and_self_loop_rejected(self):
        a = self.add("a"); b = self.add("b", parents=[a])
        self.run_cli("link", b, a, "--actor", "human", expect=4)
        self.run_cli("link", a, a, "--actor", "human", expect=4)

    def test_duplicate_edge_conflict(self):
        a = self.add("a"); b = self.add("b", parents=[a])
        self.run_cli("link", a, b, "--actor", "human", expect=4)

    def test_rm_with_edges_needs_detach_and_ai_forbidden(self):
        a = self.add("a"); self.add("b", parents=[a])
        self.run_cli("rm", a, "--actor", "human", expect=4)
        self.run_cli("rm", a, "--actor", "ai", expect=5)
        self.run_cli("rm", a, "--actor", "human", "--detach")
        self.run_cli("get", a, expect=3)

    def test_unlink_manual_edge_protected_from_ai(self):
        a = self.add("a"); b = self.add("b", parents=[a])
        self.run_cli("unlink", a, b, "--actor", "ai", expect=5)
        self.run_cli("unlink", a, b, "--actor", "human")
        self.run_cli("unlink", a, b, "--actor", "human", expect=3)

    def test_ai_link_forbidden_but_ai_created_edge_removable_by_ai(self):
        a = self.add("a")
        self.run_cli("link", a, a, "--actor", "ai", expect=5)
        b = self.add("b", actor="ai", parents=[a])
        self.run_cli("unlink", a, b, "--actor", "ai")

    def test_bad_input(self):
        self.run_cli("get", "12", expect=2)
        self.run_cli("get", "n999", expect=3)
        self.run_cli("add", "--name", "x", "--category", "不存在", "--actor", "human", expect=2)
        self.run_cli("add", "--name", "x", "--category", "Skill", "--actor", "human",
                     "--parent", "n77", expect=3)


class Summaries(Base):
    def test_length_limits_reject_not_truncate(self):
        self.run_cli("add", "--name", "x", "--category", "Skill", "--actor", "human",
                     "--hier-summary", "字" * 41, expect=2)
        a = self.add("a", hier_summary="字" * 40)
        self.assertEqual(len(self.j("get", a)["hier_summary"]), 40)
        self.run_cli("set", a, "--hier-summary", "字" * 41, "--actor", "human", expect=2)

    def test_content_summary_needs_mount_and_limit(self):
        f = self.root / "doc.md"; f.write_text("x")
        a = self.add("a")
        self.run_cli("set", a, "--content-summary", "无挂载", "--actor", "human", expect=2)
        b = self.add("b", path=str(f))
        self.run_cli("set", b, "--content-summary", "字" * 201, "--actor", "human", expect=2)
        n = self.j("set", b, "--content-summary", "说明", "--actor", "ai")
        self.assertEqual((n["content_by"], n["mounted"], n["path_kind"]), ("ai", True, "file"))

    def test_ai_cannot_overwrite_human_summary(self):
        a = self.add("a", hier_summary="人写的")
        self.run_cli("set", a, "--hier-summary", "AI改", "--actor", "ai", expect=4)
        self.assertEqual(self.j("get", a)["hier_summary"], "人写的")
        self.run_cli("set", a, "--hier-summary", "人改", "--actor", "human")
        b = self.add("b", actor="ai", hier_summary="AI写的")
        self.run_cli("set", b, "--hier-summary", "AI再写", "--actor", "ai")

    def test_path_unique_and_must_exist(self):
        d = self.root / "d"; d.mkdir()
        self.add("a", path=str(d))
        self.run_cli("add", "--name", "b", "--category", "Skill", "--actor", "human",
                     "--path", str(d), expect=4)
        self.run_cli("add", "--name", "c", "--category", "Skill", "--actor", "human",
                     "--path", str(self.root / "nope"), expect=2)

    def test_status_set_and_clear(self):
        a = self.add("a")
        self.assertEqual(self.j("set", a, "--status", "进行中", "--actor", "human")["status"], "进行中")
        self.assertIsNone(self.j("set", a, "--status", "", "--actor", "human")["status"])
        self.run_cli("set", a, "--actor", "human", expect=2)


class Search(Base):
    def setUp(self):
        super().setUp()
        self.k = self.add("笔记库", "Knowledge")
        self.c = self.add("调研", parents=[self.k], hier_summary="各类调研方法")
        self.v = self.add("调研视频", parents=[self.c], hier_summary="看视频做调研的流程")

    def test_three_chars_uses_fts_and_returns_root(self):
        hits = self.j("search", "调研视频")
        self.assertEqual([h["id"] for h in hits], [self.v])
        self.assertEqual([r["name"] for r in hits[0]["roots"]], ["笔记库"])

    def test_two_chars_falls_back_to_like(self):
        ids = {h["id"] for h in self.j("search", "调研")}
        self.assertEqual(ids, {self.c, self.v})

    def test_special_chars_do_not_break(self):
        for q in ['"', "a%b", "a_b", "x'y", "AND OR *"]:
            self.assertEqual(self.j("search", q), [])
        self.run_cli("search", "   ", expect=2)
        self.run_cli("search", "调研", "--limit", "0", expect=2)

    def test_search_follows_updates(self):
        self.run_cli("set", self.v, "--name", "拆解视频", "--actor", "human")
        self.assertEqual(self.j("search", "拆解视频")[0]["id"], self.v)
        self.assertEqual(self.j("search", "调研视频"), [])

    def test_root_hit_returns_itself(self):
        self.assertEqual(self.j("search", "笔记库")[0]["roots"][0]["id"], self.k)


class Tree(Base):
    def test_text_format_depth_and_dedup(self):
        k = self.add("知识库", "Knowledge", hier_summary="笔记")
        s = self.add("Skill", "Skill")
        c = self.add("调研", parents=[s])
        v = self.add("调研视频", parents=[c])
        p = self.add("项目", "Project")
        w = self.add("内容生产", "Project", parents=[p])
        self.run_cli("link", w, v, "--actor", "human")
        out = self.run_cli("tree", "--depth", "4").stdout.splitlines()
        self.assertIn(f"{k} 知识库 — 笔记", out)
        self.assertLess(out.index(f"{s} Skill"), out.index(f"{k} 知识库 — 笔记"))  # 同级按名称排序
        # 规范父节点按 (名称, id) 取最先: "内容生产" < "调研"(Unicode 码位),调研视频在内容生产下展开
        canon = min([("内容生产", w), ("调研", c)])[1]
        full = [l for l in out if l.strip().startswith(f"{v} 调研视频") and "→" not in l]
        ref = [l for l in out if l.strip().startswith(f"{v} 调研视频") and "→" in l]
        self.assertEqual(len(full), 1)
        self.assertEqual(len(ref), 1)
        self.assertTrue(ref[0].endswith(f"→ 见 {canon}"))

    def test_depth_limits(self):
        a = self.add("a"); b = self.add("b", parents=[a]); self.add("c", parents=[b])
        self.assertEqual(len(self.run_cli("tree", "--depth", "1").stdout.splitlines()), 1)
        self.assertEqual(len(self.run_cli("tree", "--depth", "3").stdout.splitlines()), 3)
        self.run_cli("tree", "--depth", "0", expect=2)
        self.run_cli("tree", "--depth", "11", expect=2)

    def test_root_option_and_json(self):
        a = self.add("a"); b = self.add("b", parents=[a]); self.add("c", parents=[b])
        t = self.j("tree", "--depth", "2", "--root", b)
        self.assertEqual(t[0]["id"], b)
        self.assertEqual(len(t[0]["children"]), 1)
        self.assertEqual(t[0]["children"][0]["children"], [])
        self.run_cli("tree", "--depth", "2", "--root", "n99", expect=3)

    def test_canonical_parent_outside_subtree_expands_here(self):
        a = self.add("a"); z = self.add("z"); shared = self.add("shared", parents=[a, z])
        t = self.j("tree", "--depth", "2", "--root", z)
        self.assertIsNone(t[0]["children"][0]["see"])  # a 不在 z 子树内,所以在 z 下展开

    def test_marks_mount_and_missing(self):
        d = self.root / "d"; d.mkdir()
        a = self.add("a", path=str(d))
        self.assertIn("📎", self.run_cli("tree", "--depth", "1").stdout)
        d.rmdir()
        self.run_cli("check")
        line = self.run_cli("tree", "--depth", "1").stdout
        self.assertIn("⚠", line)
        self.assertEqual(self.j("get", a)["path_state"], "missing")

    def test_empty_db(self):
        self.assertEqual(self.run_cli("tree", "--depth", "2").stdout, "")


class WriteGuard(Base):
    def test_write_commands_forbidden_outside_repo(self):
        other = self.root
        for args in (["add", "--name", "x", "--category", "Skill", "--actor", "human"],
                     ["check"], ["snapshot", "--out", str(self.root / "s.jsonl")],
                     ["render", "--depth", "1", "--out", str(self.root / "p.md")]):
            self.run_cli(*args, cwd=other, expect=5)

    def test_read_commands_work_anywhere_and_subdirs_of_repo_allowed(self):
        a = self.add("a")
        self.run_cli("get", a, cwd=self.root)
        self.run_cli("tree", "--depth", "1", cwd=self.root)
        self.run_cli("add", "--name", "b", "--category", "Skill", "--actor", "human", cwd=REPO / "docs")


class Maintenance(Base):
    def make_scope(self, **root_kw):
        base = self.root / "kb"
        (base / "设计").mkdir(parents=True)
        (base / "产品").mkdir()
        (base / "设计" / "子").mkdir()
        (base / "node_modules").mkdir()
        (base / "说明.md").write_text("x")
        (base / "笔记.txt").write_text("x")
        (base / "产品" / "p.md").write_text("x")
        cfg = {"path": str(base), "category": "Knowledge", "depth": 1, "include_files": False,
               "exclude": ["产品"], **root_kw}
        lines = ["global_exclude:", "  - node_modules", "roots:", "  - path: " + cfg["path"],
                 "    category: " + cfg["category"], "    depth: " + str(cfg["depth"]),
                 "    include_files: " + str(cfg["include_files"]).lower(), "    exclude:"]
        lines += ["      - " + e for e in cfg["exclude"]]
        scope = self.root / "scope.yml"
        scope.write_text("\n".join(lines), encoding="utf-8")
        return base, scope

    def test_scan_dry_run_then_apply_idempotent(self):
        base, scope = self.make_scope()
        d = self.j("scan", "--dry-run", scope=scope, cwd=self.root)
        names = sorted(x["name"] for x in d["would_create"])
        self.assertEqual(names, ["kb", "设计"])  # 产品被排除,node_modules 被全局排除,depth=1 不含"子"
        self.assertEqual(self.j("tree", "--depth", "3"), [])  # dry-run 不写库
        r = self.j("scan", scope=scope)
        self.assertEqual(len(r["created"]), 2)
        self.assertEqual(self.j("scan", scope=scope)["created"], [])
        roots = self.j("tree", "--depth", "3")
        self.assertEqual([n["name"] for n in roots], ["kb"])
        self.assertEqual([c["name"] for c in roots[0]["children"]], ["设计"])
        n = self.j("get", roots[0]["id"])
        self.assertEqual((n["pending"], n["category"], n["path_kind"]), (True, "Knowledge", "dir"))
        self.assertEqual(self.j("get", roots[0]["children"][0]["id"])["parents"][0]["origin"], "scan")

    def test_include_files_md_only_and_depth(self):
        base, scope = self.make_scope(include_files=True, depth=2, exclude=[])
        paths = {x["path"] for x in self.j("scan", "--dry-run", scope=scope)["would_create"]}
        rel = {Path(p).relative_to(base).as_posix() if p != str(base) else "." for p in paths}
        self.assertEqual(rel, {".", "设计", "设计/子", "说明.md", "产品", "产品/p.md"})

    def test_coverage_uncovered_and_acceptance(self):
        base, scope = self.make_scope()
        r = self.j("coverage", scope=scope)
        self.assertEqual(len(r["uncovered"]), 2)
        self.run_cli("scan", scope=scope)
        r = self.j("coverage", scope=scope)
        self.assertEqual((r["uncovered"], r["pending"], r["nodes"]), ([], 2, 2))

    def test_scan_keeps_manual_mount_as_covered(self):
        base, scope = self.make_scope()
        self.add("我的设计", path=str(base / "设计"))
        r = self.j("scan", scope=scope)
        self.assertEqual(sorted(n["name"] for n in r["created"]), ["kb"])

    def test_deeper_root_inside_shallow_root_has_no_duplicates(self):
        base = self.root / "kb"
        (base / "a" / "b" / "c").mkdir(parents=True)
        (base / "a" / "笔记.md").write_text("x")
        scope = self.root / "s.yml"
        scope.write_text(
            f"roots:\n  - path: {base}\n    category: Knowledge\n    depth: 1\n"
            f"  - path: {base}/a\n    category: Knowledge\n    depth: 2\n    include_files: true\n")
        d = self.j("scan", "--dry-run", scope=scope)["would_create"]
        paths = [x["path"] for x in d]
        self.assertEqual(len(paths), len(set(paths)))
        self.assertEqual(len(self.j("scan", scope=scope)["created"]), 5)  # kb a b c 笔记.md
        t = self.j("tree", "--depth", "5")
        self.assertEqual([n["name"] for n in t], ["kb"])  # 只有一个根,深根没有变成孤儿
        self.assertEqual(self.j("scan", scope=scope)["created"], [])
        self.assertEqual(self.j("coverage", scope=scope)["uncovered"], [])

    def test_missing_scope_and_bad_root(self):
        self.run_cli("scan", scope=self.root / "none.yml", expect=2)
        bad = self.root / "bad.yml"
        bad.write_text(f"roots:\n  - path: {self.root}/nope\n    category: Skill\n")
        self.run_cli("scan", scope=bad, expect=2)

    def test_check_marks_and_recovers(self):
        d = self.root / "d"; d.mkdir()
        a = self.add("a", path=str(d))
        d.rmdir()
        r = self.j("check")
        self.assertEqual((r["checked"], [m["id"] for m in r["missing"]]), (1, [a]))
        d.mkdir()
        self.assertEqual(self.j("check")["missing"], [])
        self.assertEqual(self.j("get", a)["path_state"], "ok")

    def test_set_path_clears_missing(self):
        d = self.root / "d"; d.mkdir(); e = self.root / "e"; e.mkdir()
        a = self.add("a", path=str(d)); d.rmdir(); self.run_cli("check")
        n = self.j("set", a, "--path", str(e), "--actor", "human")
        self.assertEqual((n["path_state"], n["path"]), ("ok", str(e)))

    def test_stale_content_summary_detected(self):
        f = self.root / "f.md"; f.write_text("v1")
        a = self.add("a", path=str(f))
        self.run_cli("set", a, "--content-summary", "摘要", "--actor", "ai")
        _, scope = self.make_scope()
        self.assertEqual(self.j("coverage", scope=scope)["stale_content_summaries"], 0)
        future = time.time() + 120
        os.utime(f, (future, future))
        self.assertEqual(self.j("coverage", scope=scope)["stale_content_summaries"], 1)

    def test_render_and_snapshot_do_not_rewrite_when_content_unchanged(self):
        a = self.add("a", hier_summary="根")
        for cmd, out, key in (("render", self.root / "p.md", "bytes"), ("snapshot", self.root / "s.jsonl", "nodes")):
            args = [cmd, "--depth", "2", "--out", str(out)] if cmd == "render" else [cmd, "--out", str(out)]
            first = self.j(*args)
            self.assertTrue(first["changed"])
            old = (os.stat(out).st_mtime_ns, out.read_bytes())
            time.sleep(0.05)
            second = self.j(*args)
            self.assertFalse(second["changed"])
            self.assertEqual((os.stat(out).st_mtime_ns, out.read_bytes()), old)  # 文件完全没动
            self.assertEqual(second[key], first[key])
        self.add("b", parents=[a])  # 索引变了才会重写
        self.assertTrue(self.j("render", "--depth", "2", "--out", str(self.root / "p.md"))["changed"])
        self.assertTrue(self.j("snapshot", "--out", str(self.root / "s.jsonl"))["changed"])

    def test_snapshot_and_render(self):
        a = self.add("a", hier_summary="根"); self.add("b", parents=[a])
        out = self.root / "s" / "i.jsonl"
        r = self.j("snapshot", "--out", str(out))
        lines = out.read_text(encoding="utf-8").splitlines()
        self.assertEqual((r["nodes"], len(lines)), (2, 2))
        rows = [json.loads(x) for x in lines]
        self.assertEqual(rows[1]["parents"], [{"parent": a, "origin": "manual"}])
        doc = self.root / "p.md"
        r = self.j("render", "--depth", "2", "--out", str(doc))
        text = doc.read_text(encoding="utf-8")
        self.assertIn("search <词>", text)
        self.assertIn(f"{a} a — 根", text)
        self.assertEqual(r["bytes"], len(text.encode("utf-8")))


class AccessCount(Base):
    """访问计数: get / children / ancestors 累加,search / tree / 写命令不计;计数失败不影响读命令。"""

    def hits(self, i):
        n = self.j("get", i, "--no-count")
        return n["hits"], n["last_access"]

    def test_get_children_ancestors_count_target_node(self):
        a = self.add("根", "Knowledge"); b = self.add("子", parents=[a])
        self.assertEqual(self.hits(a), (0, None))
        self.assertEqual(self.j("get", b)["hits"], 0)  # 读命令不写库,返回的是并入后的值
        self.j("get", b)
        self.j("children", a)
        self.j("ancestors", b)
        self.assertEqual(self.hits(b), (0, None))  # 未并入前计数不变
        self.assertEqual(self.j("ingest-access")["ingested"], 4)
        self.assertEqual(self.hits(b)[0], 3)
        self.assertEqual(self.hits(a)[0], 1)
        self.assertRegex(self.hits(b)[1], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$")

    def test_search_tree_and_writes_do_not_count(self):
        a = self.add("检索词丰富的节点"); self.add("乙", parents=[a])
        self.j("search", "检索词丰富"); self.j("tree", "--depth", "2"); self.j("tree", "--depth", "2", "--root", a)
        self.run_cli("set", a, "--hier-summary", "改", "--actor", "human"); self.run_cli("confirm", a)
        self.assertEqual(self.j("ingest-access")["ingested"], 1)  # 只有 search 进了明细
        self.assertEqual(self.hits(a), (0, None))

    def test_opt_out_flag_and_env(self):
        a = self.add("甲")
        self.j("get", a, "--no-count"); self.j("children", a, "--no-count"); self.j("ancestors", a, "--no-count")
        cmd = [sys.executable, str(BIN), "get", a, "--json", "--db", self.db]
        subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, env={**os.environ, "NODE_INDEX_NO_COUNT": "1"})
        self.assertEqual(self.j("ingest-access")["ingested"], 0)  # 不计数就不产生事件
        self.assertEqual(self.hits(a), (0, None))

    def test_counts_when_run_outside_repo(self):
        # 读命令在任何目录都能用,事件照常进暂存文件,不受"写命令只在维护目录"限制
        a = self.add("甲")
        self.j("get", a, cwd=self.root)
        self.assertEqual(self.j("ingest-access")["ingested"], 1)
        self.assertEqual(self.hits(a)[0], 1)

    def test_missing_node_not_found_and_leaves_no_row(self):
        self.add("甲")
        self.run_cli("get", "n9", expect=3)
        import sqlite3
        c = sqlite3.connect(self.db)
        self.assertEqual(c.execute("SELECT count(*) FROM node_access").fetchone()[0], 0)

    def test_deleting_node_drops_its_counter(self):
        a = self.add("甲"); self.j("get", a)
        self.j("ingest-access")
        self.run_cli("rm", a, "--actor", "human")
        import sqlite3
        self.assertEqual(sqlite3.connect(self.db).execute("SELECT count(*) FROM node_access").fetchone()[0], 0)

    def test_snapshot_excludes_counters(self):
        a = self.add("甲"); self.j("get", a)
        out = self.root / "s.jsonl"
        self.run_cli("snapshot", "--out", str(out))
        self.assertNotIn("hits", out.read_text(encoding="utf-8"))

    def test_old_database_without_table_is_upgraded_on_ingest(self):
        import re, sqlite3
        schema = (REPO / "src" / "schema.sql").read_text(encoding="utf-8")
        old = re.sub(r"-- 访问计数.*?\);\n", "", schema, flags=re.S)
        self.assertNotIn("node_access", old)
        c = sqlite3.connect(self.db); c.executescript(old)
        c.execute("INSERT INTO nodes(name, category) VALUES ('旧库节点', 'Skill')"); c.commit(); c.close()
        self.assertEqual(self.j("get", "n1")["hits"], 0)  # 读命令不建表
        self.assertEqual(sqlite3.connect(self.db).execute(
            "SELECT count(*) FROM sqlite_master WHERE name='node_access'").fetchone()[0], 0)
        self.assertEqual(self.j("ingest-access")["ingested"], 1)  # 并入时补建表并计数
        self.assertEqual(self.j("get", "n1", "--no-count")["hits"], 1)

    def test_read_only_database_still_reads(self):
        a = self.add("甲")
        os.chmod(self.db, 0o444)
        self.addCleanup(os.chmod, self.db, 0o644)
        n = self.j("get", a)
        self.assertEqual((n["name"], n["hits"]), ("甲", 0))

    def test_locked_database_does_not_break_or_stall_reads(self):
        import sqlite3
        a = self.add("甲")
        holder = sqlite3.connect(self.db, isolation_level=None)
        holder.execute("BEGIN IMMEDIATE")  # 占着写锁,读仍可进行
        self.addCleanup(holder.close)
        t = time.time()
        n = self.j("get", a)
        self.assertLess(time.time() - t, 5)
        self.assertEqual((n["name"], n["hits"]), ("甲", 0))
        holder.execute("ROLLBACK")
        self.assertEqual(self.hits(a)[0], 0)


class HostConfig(Base):
    """scan 合并 scope.yml 的共享根与 host/host.yml 的 scan_roots;snapshot 默认写 snapshots/<host>.jsonl。"""

    def setUp(self):
        super().setUp()
        self.repo = self.root / "repo"
        (self.repo / "shared" / "reference" / "sops").mkdir(parents=True)
        self.kb = self.root / "kb"
        (self.kb / "设计").mkdir(parents=True)
        self.scope = self.root / "scope.yml"
        self.scope.write_text("host_roots: true\nroots:\n  - path: shared/reference\n    category: Knowledge\n"
                              "    depth: 1\n", encoding="utf-8")
        self.env = {**os.environ, "NODE_INDEX_DB": "", "NODE_INDEX_REPO": str(self.repo)}

    def write_host(self, roots):
        (self.repo / "host").mkdir(exist_ok=True)
        (self.repo / "host" / "host.yml").write_text(
            "host: test-host\nclients: {}\nknowledge_root: /tmp\nscan_roots:\n" +
            "".join(f"- {{path: {r}, category: Knowledge, depth: 1}}\n" for r in roots), encoding="utf-8")

    def cli(self, *args, expect=0):
        p = subprocess.run([sys.executable, str(BIN), *args, "--db", self.db, "--json"], cwd=self.repo,
                           capture_output=True, text=True, env=self.env)
        self.assertEqual(p.returncode, expect, p.stderr)
        return json.loads(p.stdout or p.stderr)

    def test_scan_merges_shared_and_host_roots(self):
        self.write_host([self.kb])
        self.cli("scan", "--scope", str(self.scope))
        paths = {x["path"] for x in self.cli("scan", "--scope", str(self.scope), "--dry-run")["would_create"]}
        self.assertEqual(paths, set())  # 已全部建好
        import sqlite3
        paths = {r[0] for r in sqlite3.connect(self.db).execute("SELECT path FROM nodes")}
        self.assertIn(str(self.repo / "shared" / "reference" / "sops"), paths)
        self.assertIn(str(self.kb / "设计"), paths)

    def test_scan_without_host_config_or_with_missing_root_fails(self):
        self.assertIn("host", self.cli("scan", "--scope", str(self.scope), expect=2)["message"].lower())
        self.write_host([self.kb, self.root / "已删除的目录"])
        self.assertIn("已删除的目录", self.cli("scan", "--scope", str(self.scope), expect=2)["message"])

    def test_snapshot_default_path_is_named_after_host(self):
        self.write_host([self.kb])
        r = self.cli("snapshot")
        self.assertEqual(r["path"], str(self.repo / "snapshots" / "test-host.jsonl"))
        self.assertTrue(Path(r["path"]).is_file())


class CategoryMigration(Base):
    def test_old_values_become_layer_names_and_ids_continue(self):
        import sqlite3
        sys.path.insert(0, str(REPO / "src"))
        import node_index.db as db
        SCHEMA, connect = db.SCHEMA, db.connect
        raw = SCHEMA.read_text(encoding="utf-8").replace(
            "CHECK (category IN ('Reference','Knowledge','Project','Skill'))",
            "CHECK (category IN ('知识库','项目','Skill'))")
        path = self.root / "old.db"
        c = sqlite3.connect(path)
        c.executescript(raw)
        ref = str((REPO / "shared" / "reference" / "sops" / "x.md").resolve())
        kb = str(self.root / "笔记目录")
        c.execute("INSERT INTO nodes(name, category, path, path_kind) VALUES (?,?,?,?)", ("做法", "知识库", ref, "file"))
        c.execute("INSERT INTO nodes(name, category, path, path_kind) VALUES (?,?,?,?)", ("个人笔记", "知识库", kb, "dir"))
        c.execute("INSERT INTO nodes(name, category) VALUES (?,?)", ("代码目录", "项目"))
        c.execute("INSERT INTO edges VALUES (2, 3, 'manual')")
        c.commit()
        c.close()
        conn = connect(path)
        rows = {r["name"]: r["category"] for r in conn.execute("SELECT name, category FROM nodes")}
        self.assertEqual(rows, {"做法": "Reference", "个人笔记": "Knowledge", "代码目录": "Project"})
        self.assertEqual(tuple(conn.execute("SELECT parent_id, child_id, origin FROM edges").fetchone()), (2, 3, "manual"))
        hit = conn.execute("SELECT name FROM nodes_fts WHERE nodes_fts MATCH '个人笔记'").fetchone()
        self.assertEqual(hit[0], "个人笔记")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO nodes(name, category) VALUES ('坏','知识库')")
        conn.close()
        conn = connect(path)
        conn.execute("INSERT INTO nodes(name, category) VALUES ('新技能','Skill')")
        conn.commit()
        self.assertEqual(conn.execute("SELECT id FROM nodes WHERE name='新技能'").fetchone()[0], 4)
        conn.close()


if __name__ == "__main__":
    unittest.main()
