"""shared.py: Shared config 与 Host config 的加载、校验、序列化与 ID。"""
import json
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from node_index import shared as S  # noqa: E402
from node_index.db import CliError  # noqa: E402


class Tmp(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.p = S.paths(t.name)

    def write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def assertShape(self, fn, needle):
        with self.assertRaises(S.ShapeError) as cm:
            fn()
        self.assertEqual(cm.exception.code, 2)
        self.assertIn(needle, cm.exception.message)


class RepoSeed(unittest.TestCase):
    # 种子数据 (shared/ 目录与 host/ 示例) 不随精简版仓库分发,缺文件时跳过
    @unittest.skipUnless((REPO / "shared" / "catalog.yaml").exists(), "仓库未提交种子数据")
    def test_committed_seed_files_load(self):
        p = S.paths(REPO)
        catalog, categories = S.load_catalog(p), S.load_categories(p)  # P3 S3 的临时种子
        self.assertTrue(catalog and categories)
        retired = [name for name, e in catalog.items() if e.status is S.EntryStatus.RETIRED]
        self.assertTrue(retired, "种子 catalog 应存在已退役条目")
        self.assertEqual(S.cross_check(catalog, categories, set(S.load_sops(p)), set(S.load_lessons(p))), [])
        rules = S.load_rules(p)  # Import 进来的 Rule block 全部合法,required-read 正文可解析
        self.assertTrue(rules)
        for r in rules:
            if r.kind is S.RuleKind.REQUIRED_READ:
                S.parse_required_read(r)
        S.load_decisions(p)  # 已提交的真实数据全部可加载
        S.load_reports(p)
        S.load_lessons(p)

    @unittest.skipUnless((REPO / "host" / "host/host.yml").exists(), "仓库未提交种子文件")
    def test_host_example_loads_with_seven_clients(self):
        p = S.paths(REPO)
        h = S.load_host(p, REPO / "host" / "host/host.yml")
        self.assertEqual(set(h.clients), {"claude-code", "dsh", "pi", "codex", "gemini", "trae-cn", "cursor"})
        self.assertEqual(h.clients["cursor"].manual_only_mode, S.ManualOnlyMode.FRONTMATTER)
        self.assertEqual(h.clients["cursor"].rules_path.name, "node-index.mdc")
        self.assertEqual(h.clients["claude-code"].manual_only_mode, S.ManualOnlyMode.FRONTMATTER)
        self.assertTrue(h.clients["dsh"].rules_path.is_absolute())
        self.assertTrue(h.scan_roots)
        roots = [r["path"] for r in h.scan_roots]
        self.assertNotIn("~/.skills-manager/skills", roots)
        self.assertIn("~/.local/share/node-index/skills", roots)


class Ids(unittest.TestCase):
    def test_format_and_collision_retry(self):
        rid = S.new_id("R", today=date(2020, 1, 1))
        self.assertRegex(rid, r"^R-20200101-[0-9a-f]{4}$")
        self.assertRegex(S.new_id("L"), S.LESSON_ID.pattern)
        taken = {f"L-20200101-{i:04x}" for i in range(65535)}
        self.assertEqual(S.new_id("L", taken, date(2020, 1, 1)), "L-20200101-ffff")

    def test_unknown_prefix_rejected(self):
        with self.assertRaises(ValueError):
            S.new_id("X")


class Catalog(Tmp):
    def test_round_trip_sorted(self):
        e = S.CatalogEntry("zeta", S.EntryKind.VENDORED, "zeta-skill", None, date(2026, 10, 23), S.Tier.LEAF)
        a = S.CatalogEntry("alpha", S.EntryKind.NATIVE, "pstack", "0.9", date(2026, 10, 1), S.Tier.CORE, True)
        S.write_yaml(self.p.catalog, S.catalog_to_dict({"zeta": e, "alpha": a}))
        self.assertEqual(list(S.load_catalog(self.p)), ["alpha", "zeta"])
        self.assertEqual(S.load_catalog(self.p)["alpha"], a)

    def test_illegal_tier_and_unknown_field(self):
        base = "x:\n  entry: native\n  source: s\n  added: 2026-10-23\n  status: active\n"
        self.write(self.p.catalog, base + "  tier: main\n")
        self.assertShape(lambda: S.load_catalog(self.p), "tier 不合法")
        self.write(self.p.catalog, base + "  tier: core\n  categories: [a]\n")
        self.assertShape(lambda: S.load_catalog(self.p), "未知字段")
        self.write(self.p.catalog, base.replace("  added: 2026-10-23\n", "") + "  tier: core\n")
        self.assertShape(lambda: S.load_catalog(self.p), "缺少必填字段 added")


class Categories(Tmp):
    def test_members_parse_and_parent_required(self):
        self.write(self.p.categories,
                   "design:\n  name: 设计\n  members: [demo-a, 'sop:demo-sop', 'lesson:L-20200101-0a1b']\n"
                   "design/motion:\n  name: 动效\n  router: demo-router-motion\n")
        c = S.load_categories(self.p)
        self.assertEqual([m.kind for m in c["design"].members],
                         [S.MemberKind.SKILL, S.MemberKind.SOP, S.MemberKind.LESSON])
        self.assertEqual(c["design/motion"].parent, "design")
        self.assertEqual(S.categories_to_dict(c)["design"]["members"][1], "sop:demo-sop")
        self.write(self.p.categories, "design/motion:\n  name: 动效\n")
        self.assertShape(lambda: S.load_categories(self.p), "父 Category design 不存在")

    def test_bad_refs(self):
        for bad in ("'lesson:L-1'", "'kb:x'", "[a, a]"):
            members = bad if bad.startswith("[") else f"[{bad}]"
            self.write(self.p.categories, f"design:\n  name: 设计\n  members: {members}\n")
            with self.assertRaises(S.ShapeError, msg=bad):
                S.load_categories(self.p)
        self.write(self.p.categories, "Design:\n  name: 设计\n")
        self.assertShape(lambda: S.load_categories(self.p), "Category id 格式不对")

    def test_cross_check(self):
        cat = {"r": S.CatalogEntry("r", S.EntryKind.VENDORED, "r", None, date.today(), S.Tier.LEAF)}
        cats = S.parse_categories({"d": {"name": "D", "router": "r", "members": ["r", "ghost", "sop:x"]}}, "t")
        problems = S.cross_check(cat, cats, sop_ids=set(), lesson_ids=set())
        self.assertEqual(len(problems), 3, problems)
        self.assertEqual(S.cross_check(cat, {}, set(), set()), [])


RULE = """---
id: {id}
title: T
kind: {kind}
applies_to: [{applies}]
order: {order}
added: 2026-10-23
---
正文
"""


class Rules(Tmp):
    def rule(self, id, kind="general", applies="", order=0):
        self.write(self.p.rules / f"{id}.md", RULE.format(id=id, kind=kind, applies=applies, order=order))

    def test_order_by_kind_then_order(self):
        self.rule("m", "maintenance")
        self.rule("g2", order=2)
        self.rule("g1", order=1)
        self.rule("ix", "index", applies="executor")
        rules = S.load_rules(self.p)
        self.assertEqual([r.id for r in rules], ["g1", "g2", "ix", "m"])
        self.assertTrue(rules[0].applies(S.Profile.EXECUTOR))
        self.assertFalse(rules[2].applies(S.Profile.ORCHESTRATOR))

    def test_rejects(self):
        self.write(self.p.rules / "a.md", RULE.format(id="b", kind="general", applies="", order=0))
        self.assertShape(lambda: S.load_rules(self.p), "与文件名不一致")
        self.rule("a", kind="misc")
        self.assertShape(lambda: S.load_rules(self.p), "kind 不合法")
        self.rule("a", applies="reviewer")
        self.assertShape(lambda: S.load_rules(self.p), "applies_to 不合法")
        self.write(self.p.rules / "a.md", "no frontmatter\n")
        self.assertShape(lambda: S.load_rules(self.p), "缺少 frontmatter")
        self.write(self.p.rules / "a.md", RULE.format(id="a", kind="general", applies="", order=0).replace("正文\n", ""))
        self.assertShape(lambda: S.load_rules(self.p), "正文为空")


class Lessons(Tmp):
    def test_render_then_load(self):
        name, text = S.render_lesson("L-20200101-0b7c", "git-代理", "claude-code", "在公司网络", "push 超时", "设置代理")
        lesson = S.load_lesson(self.write(self.p.lessons / name, text))
        self.assertEqual((lesson.topic, lesson.status, lesson.merged_into, lesson.host),
                         ("git-代理", S.LessonStatus.ACTIVE, None, None))
        self.assertEqual(set(S.load_lessons(self.p)), {"L-20200101-0b7c"})

    def test_rejects(self):
        _, text = S.render_lesson("L-20200101-0b7c", "t", "pi", "a", "b", "c")
        f = self.p.lessons / "L-20200101-0b7c-t.md"
        self.write(f, text.replace("status: active", "status: merged"))
        self.assertShape(lambda: S.load_lesson(f), "merged_into")
        self.write(f, text.replace("## 问题", "## 难点"))
        self.assertShape(lambda: S.load_lesson(f), "二级标题")
        self.write(self.p.lessons / "L-20200101-0b7c.md", text)
        self.assertShape(lambda: S.load_lesson(self.p.lessons / "L-20200101-0b7c.md"), "文件名应为")
        with self.assertRaises(S.ShapeError):
            S.render_lesson("L-1", "t", "pi", "a", "b", "c")


class Sops(Tmp):
    def test_load_and_reject_bad_lesson_ref(self):
        f = self.write(self.p.sops / "demo-sop.md",
                       "---\nid: demo-sop\ntitle: 示例路由\nupdated: 2026-10-23\nfrom_lessons: [L-20200101-0a1b]\n---\n正文\n")
        self.assertEqual(S.load_sops(self.p)["demo-sop"].from_lessons, ("L-20200101-0a1b",))
        self.write(f, f.read_text().replace("L-20200101-0a1b", "lesson-1"))
        self.assertShape(lambda: S.load_sop(f), "from_lessons")


def report(**kw):
    d = {"id": "R-20200101-a3f9", "kind": "skill-issue", "title": "t", "reason": "r", "evidence": "e",
         "from": {"client": "dsh", "position": "executor", "host": "test-host"},
         "at": "2026-10-23T21:00:00", "change": None, "status": "pending", "claimed_by": None, "resolution": None}
    d.update(kw)
    return d


class Reports(Tmp):
    def test_round_trip_through_file(self):
        r = S.parse_report(report(kind="rule-change", change={"target": "shared/rules/a.md", "replace": {"old": "x", "new": "y"}},
                                  status="resolved", resolution={"decided_by": "user", "instruction": "照做",
                                                                 "outcome": "as-proposed", "changes": ["abc123"],
                                                                 "at": "2026-10-23T22:00:00"}), "t")
        f = self.p.report_file(r.id)
        S.write_yaml(f, r.to_dict())
        self.assertEqual(S.load_report(f), r)
        self.assertEqual(S.load_reports(self.p)[r.id].change, S.ReplaceChange("shared/rules/a.md", "x", "y"))
        self.assertIsInstance(S.load_report(f).at, datetime)

    def test_digest_is_a_kind(self):
        self.assertIs(S.parse_report(report(kind="digest"), "t").kind, S.ReportKind.DIGEST)

    def test_state_invariants(self):
        cases = [
            (report(kind="rule-change"), "rule-change 必须带 change"),
            (report(kind="bug"), "kind 不合法"),
            (report(status="in-progress"), "claimed_by"),
            (report(claimed_by="pi"), "claimed_by"),
            (report(status="resolved"), "resolution"),
            (report(status="rejected", resolution={"decided_by": "u", "outcome": "modified", "at": "2026-10-23T22:00:00"}),
             "不填 outcome"),
            (report(status="resolved", resolution={"decided_by": "u", "at": "2026-10-23T22:00:00"}), "须有 outcome"),
            (report(change={"target": "a", "diff": "d", "replace": {"old": "x", "new": "y"}}), "恰好有一个"),
            (report(change={"target": "/etc/hosts", "diff": "d"}), "相对路径"),
            (report(**{"from": {"client": "dsh", "position": "reviewer", "host": "h"}}), "position 不合法"),
            (report(id="R-1"), "id 格式不对"),
            (report(title=""), "缺少必填字段 title"),
        ]
        for data, needle in cases:
            with self.subTest(needle=needle):
                self.assertShape(lambda: S.parse_report(data, "t"), needle)

    def test_filename_must_match_id(self):
        f = self.p.inbox / "R-20200101-0000.yaml"
        S.write_yaml(f, report())
        self.assertShape(lambda: S.load_report(f), "与文件名不一致")


class Decisions(Tmp):
    def test_append_only(self):
        d1 = S.Decision(datetime(2026, 10, 23, 22), "R-20200101-a3f9", "resolve", S.DecisionResult.APPROVED, "dsh",
                        instruction="照做")
        S.append_decision(self.p, d1)
        first = self.p.decisions.read_bytes()
        d2 = S.Decision(datetime(2026, 10, 23, 23), "digest:2026-10-23#1", "decide", S.DecisionResult.DISMISSED,
                        "user", note="不需要")
        S.append_decision(self.p, d2)
        self.assertTrue(self.p.decisions.read_bytes().startswith(first))
        self.assertEqual(S.load_decisions(self.p), [d1, d2])
        self.assertNotIn("\\u", self.p.decisions.read_text())

    def test_rejects(self):
        with self.assertRaises(S.ShapeError):
            S.append_decision(self.p, S.Decision(datetime.now(), "t", "a", S.DecisionResult.ADOPTED, "u"))
        self.assertFalse(self.p.decisions.exists())
        self.write(self.p.decisions, json.dumps({"at": "2026-10-23T22:00:00", "target": "t", "action": "a",
                                                 "result": "批准", "by": "u", "note": "n"}) + "\n")
        self.assertShape(lambda: S.load_decisions(self.p), "result 不合法")


class Host(Tmp):
    HOST = ("host: mac\nclients:\n  pi:\n    rules_path: ~/.pi/agent/AGENTS.md\n    skills_dir: ~/.pi/agent/skills\n"
            "    profile: executor\n    manual_only_mode: skip\nknowledge_root: ~/kb\n"
            "scan_roots:\n  - path: ~/code\n    category: Project\n    depth: 1\n")

    def test_load_expands_home(self):
        self.write(self.p.host, self.HOST)
        h = S.load_host(self.p)
        self.assertEqual(h.clients["pi"].profile, S.Profile.EXECUTOR)
        self.assertNotIn("~", str(h.knowledge_root))
        self.assertEqual(self.p.snapshot(h.host).name, "mac.jsonl")

    def test_rejects(self):
        self.assertShape(lambda: S.load_host(self.p), "host/host.yml")
        for old, new, needle in (("profile: executor", "profile: reviewer", "profile 不合法"),
                                 ("manual_only_mode: skip", "manual_only_mode: hide", "manual_only_mode 不合法"),
                                 ("host: mac", "host: My Mac", "host 格式不对"),
                                 ("    depth: 1\n", "    depth: 1\nextra: 1\n", "未知字段")):
            with self.subTest(needle=needle):
                self.write(self.p.host, self.HOST.replace(old, new))
                self.assertShape(lambda: S.load_host(self.p), needle)


class RepoRoot(unittest.TestCase):
    def test_env_override(self):
        import os
        old = os.environ.get("NODE_INDEX_REPO")
        os.environ["NODE_INDEX_REPO"] = "/tmp/x"
        try:
            self.assertEqual(S.paths().root, Path("/tmp/x"))
        finally:
            if old is None:
                del os.environ["NODE_INDEX_REPO"]
            else:
                os.environ["NODE_INDEX_REPO"] = old
        self.assertTrue(issubclass(S.ShapeError, CliError))


if __name__ == "__main__":
    unittest.main()
