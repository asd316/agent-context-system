"""Catalog、共享 Category、Index 同步与 Router Member list。CLI 部分端到端调用 bin/node-index。"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

import yaml

from test_cli import REPO, Base

sys.path.insert(0, str(REPO / "src"))
from node_index import db, routers, shared  # noqa: E402

GUIDE = "# Selection guide\n\n做动效选 motion 下的 skill;不确定时先问用户。\n\n"


class Repo(Base):
    def setUp(self):
        super().setUp()
        self.repo = self.root / "repo"
        (self.repo / "shared" / "skills" / "routers").mkdir(parents=True)
        (self.repo / "shared" / "catalog.yaml").write_text("# 头注释\n{}\n", encoding="utf-8")
        (self.repo / "shared" / "categories.yaml").write_text("# 头注释\n{}\n", encoding="utf-8")
        patch = mock.patch.dict(os.environ, {"NODE_INDEX_REPO": str(self.repo)})
        patch.start()
        self.addCleanup(patch.stop)
        self.p = shared.paths(self.repo)

    def cats(self):
        return yaml.safe_load(self.p.categories.read_text(encoding="utf-8"))

    def write_cats(self, data):
        shared.write_yaml(self.p.categories, data)

    def write_catalog(self, data):
        shared.write_yaml(self.p.catalog, data)


class Catalog(Repo):
    def test_set_creates_then_modifies(self):
        self.run_cli("catalog", "set", "foo", "--tier", "leaf", expect=3)  # 新建缺 --entry/--source
        e = self.j("catalog", "set", "foo", "--entry", "referenced", "--source", "~/x/foo", "--tier", "leaf")
        self.assertEqual((e["name"], e["tier"], e["manual_only"], e["status"]), ("foo", "leaf", False, "active"))
        e = self.j("catalog", "set", "foo", "--tier", "core", "--manual-only", "true")
        self.assertEqual((e["tier"], e["manual_only"], e["source"]), ("core", True, "~/x/foo"))
        self.j("catalog", "set", "bar", "--entry", "vendored", "--source", "bar", "--tier", "leaf",
               "--status", "retired")
        self.assertEqual([x["name"] for x in self.j("catalog", "list", "--tier", "core")], ["foo"])
        self.assertEqual([x["name"] for x in self.j("catalog", "list", "--status", "retired")], ["bar"])
        self.assertTrue(self.p.catalog.read_text(encoding="utf-8").startswith("# 头注释\n"))
        self.run_cli("catalog", "set", "foo", expect=2)

    def test_native_entry_only_tier_and_manual_only(self):
        self.j("catalog", "set", "n", "--entry", "native", "--source", "claude-code", "--tier", "leaf")
        self.assertEqual(self.j("catalog", "set", "n", "--tier", "core")["tier"], "core")
        self.run_cli("catalog", "set", "n", "--status", "retired", expect=5)
        self.run_cli("catalog", "set", "n", "--source", "other", expect=5)

    def test_set_refused_outside_repo(self):
        self.run_cli("catalog", "set", "foo", "--tier", "core", cwd=self.root, expect=5)
        self.j("catalog", "list", cwd=self.root)  # 读命令任何目录可执行


class Membership(Repo):
    def setUp(self):
        super().setUp()
        self.j("category", "new", "design", "--name", "设计")
        self.j("category", "new", "design/motion", "--name", "动效")
        self.j("category", "new", "ui", "--name", "界面")

    def test_new_checks_parent_and_duplicate(self):
        self.run_cli("category", "new", "nope/child", "--name", "x", expect=3)
        self.run_cli("category", "new", "design", "--name", "x", expect=4)
        self.run_cli("category", "new", "Bad_Id", "--name", "x", expect=2)
        self.assertEqual(self.cats()["design/motion"], {"name": "动效", "router": None, "members": []})

    def test_copy_keeps_both_move_replaces(self):
        r = self.j("category", "copy", "anim", "--to", "design")
        self.assertEqual(r, {"member": "anim", "categories": ["design"]})
        r = self.j("category", "copy", "anim", "--to", "design/motion")
        self.assertEqual(r["categories"], ["design", "design/motion"])
        self.assertEqual(self.j("category", "copy", "anim", "--to", "design")["categories"],
                         ["design", "design/motion"])  # 幂等,不重复
        self.assertEqual(self.cats()["design"]["members"], ["anim"])

        r = self.j("category", "move", "anim", "--from", "design", "--to", "ui")
        self.assertEqual(r["categories"], ["design/motion", "ui"])
        self.assertEqual(self.cats()["design"]["members"], [])
        # 已在目标里时只从来源移除
        r = self.j("category", "move", "anim", "--from", "ui", "--to", "design/motion")
        self.assertEqual(r["categories"], ["design/motion"])
        self.assertEqual(self.cats()["design/motion"]["members"], ["anim"])
        self.run_cli("category", "move", "anim", "--from", "ui", "--to", "design", expect=3)
        self.run_cli("category", "copy", "anim", "--to", "nope", expect=3)

    def test_remove_last_membership_allowed_and_idempotent(self):
        self.j("category", "copy", "sop:demo-sop", "--to", "ui")
        r = self.j("category", "remove", "sop:demo-sop", "--from", "ui")
        self.assertEqual(r, {"member": "sop:demo-sop", "categories": []})
        self.assertEqual(self.j("category", "remove", "sop:demo-sop", "--from", "ui")["categories"], [])
        self.run_cli("category", "remove", "x", "--from", "nope", expect=3)
        self.run_cli("category", "copy", "sop:Bad Id", "--to", "ui", expect=2)
        self.assertTrue(self.p.categories.read_text(encoding="utf-8").startswith("# 头注释\n"))


def skill(d, text="---\nname: x\n---\n"):
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(text, encoding="utf-8")


class IndexSync(Repo):
    def setUp(self):
        super().setUp()
        self.ext = self.root / "ext"
        for name in ("anim", "pixel"):
            skill(self.ext / name)
        skill(self.p.skills / "demo-maintain")
        skill(self.p.routers / "demo-router-design",
              f"---\nname: demo-router-design\ndescription: 设计入口\n---\n\n{GUIDE}"
              f"{shared.ROUTER_BEGIN}\n{shared.ROUTER_END}\n\n尾注不动。\n")
        skill(self.p.routers / "demo-router-motion",
              f"---\nname: demo-router-motion\n---\n{shared.ROUTER_BEGIN}\n{shared.ROUTER_END}\n")
        self.scope = self.root / "scope.yml"
        self.scope.write_text(
            "roots:\n  - path: shared/skills\n    category: Skill\n    depth: 1\n"
            "  - path: shared/skills/routers\n    category: Skill\n    depth: 1\n"
            f"  - path: {self.ext}\n    category: Skill\n    depth: 1\n", encoding="utf-8")
        today = "2026-10-23"
        ref = lambda n: {"entry": "referenced", "source": str(self.ext / n), "version": None, "added": today,
                         "tier": "leaf", "manual_only": False, "status": "active"}
        self.write_catalog({
            "anim": ref("anim"), "pixel": ref("pixel"),
            "demo-maintain": {**ref("x"), "entry": "vendored", "source": "demo-maintain", "tier": "core"},
            "demo-router-design": {**ref("x"), "entry": "vendored", "source": "routers/demo-router-design",
                                     "tier": "router"},
            "demo-router-motion": {**ref("x"), "entry": "vendored", "source": "routers/demo-router-motion",
                                     "tier": "router"},
        })
        self.write_cats({
            "design": {"name": "设计", "router": "demo-router-design", "members": ["pixel", "demo-maintain"]},
            "design/motion": {"name": "动效", "router": "demo-router-motion", "members": ["anim", "ghost"]},
            "design/ui": {"name": "界面", "router": None, "members": ["pixel"]},
        })

    def scan(self):
        return self.j("scan", scope=self.scope)

    def node(self, path):
        return next(n for n in self.j("search", Path(path).name) if n["name"] == Path(path).name)

    def category_node(self, root_id, *names):
        cur = root_id
        for name in names:
            cur = next(c["id"] for c in self.j("children", cur, "--no-count") if c["name"] == name)
        return cur

    def kids(self, nid):
        return sorted(c["name"] for c in self.j("children", nid, "--no-count"))

    def test_scan_writes_category_tree_and_memberships(self):
        r = self.scan()["categories"]
        self.assertEqual(r["created"], ["design", "design/motion", "design/ui"])
        self.assertEqual(r["unresolved"], ["design/motion: ghost"])
        root = self.node(self.p.skills)["id"]
        self.assertIn("设计", self.kids(root))
        design = self.category_node(root, "设计")
        self.assertEqual(self.kids(design), ["demo-maintain", "pixel", "动效", "界面"])
        self.assertEqual(self.kids(self.category_node(root, "设计", "动效")), ["anim"])
        self.assertEqual(self.j("search", "动效")[0]["name"], "动效")
        again = self.scan()["categories"]
        self.assertEqual((again["created"], again["linked"], again["unlinked"]), ([], 0, 0))

        cats = self.cats()
        cats["design"]["members"] = ["demo-maintain"]
        cats["design/motion"]["members"].append("pixel")
        cats["design/motion"]["name"] = "动画"
        del cats["design/ui"]
        self.write_cats(cats)
        r = self.scan()["categories"]
        self.assertEqual((r["renamed"], r["removed"]), (["design/motion"], ["design/ui"]))
        self.assertEqual(self.kids(design), ["demo-maintain", "动画"])
        self.assertEqual(self.kids(self.category_node(root, "设计", "动画")), ["anim", "pixel"])
        self.assertFalse([n for n in self.j("search", "界面") if n["name"] == "界面"])
        # 只有一个 Skill 根: 外部扫描根挂在它下面;已归类的成员只在 Category 下
        ext = self.node(self.ext)["id"]
        self.assertIn("ext", self.kids(root))
        self.assertNotIn("demo-maintain", self.kids(root))
        self.assertEqual(self.kids(ext), [])
        cats = self.cats(); cats["design/motion"]["members"] = ["pixel"]; self.write_cats(cats)
        self.scan()
        self.assertNotIn("anim", self.kids(self.category_node(root, "设计", "动画")))
        self.assertEqual(self.kids(ext), ["anim"])  # 未归类的回到扫描目录

    def test_scan_without_shared_skills_root_leaves_categories_alone(self):
        scope = self.root / "plain.yml"
        scope.write_text(f"roots:\n  - path: {self.ext}\n    category: Skill\n    depth: 1\n", encoding="utf-8")
        self.assertNotIn("categories", self.j("scan", scope=scope))

    def routers(self):
        return routers.render_routers(db.connect(self.db), self.p)

    def members(self, name):
        return yaml.safe_load((self.p.routers / name / "members.yaml").read_text(encoding="utf-8"))

    def test_router_member_list_regenerates_and_keeps_selection_guide(self):
        self.scan()
        self.run_cli("set", self.node(self.ext / "pixel")["id"], "--hier-summary", "像素级视觉还原", "--actor", "human")
        r = self.routers()
        self.assertEqual(r, [{"name": "demo-router-design", "members": 3, "changed": True},
                             {"name": "demo-router-motion", "members": 2, "changed": True}])
        got = self.members("demo-router-design")
        self.assertEqual([m["ref"] for m in got], ["pixel", "demo-maintain", "demo-router-motion"])
        self.assertEqual(got[0], {"ref": "pixel", "kind": "skill", "tier": "leaf", "trait": "像素级视觉还原"})
        self.assertEqual(got[2]["tier"], "router")
        self.assertEqual([m["ref"] for m in self.members("demo-router-motion")], ["anim", "ghost"])
        self.assertIsNone(self.members("demo-router-motion")[1]["tier"])
        self.assertEqual(self.routers()[0]["changed"], False)

        skill_md = self.p.routers / "demo-router-design" / "SKILL.md"
        edited = skill_md.read_text(encoding="utf-8").replace("不确定时先问用户", "不确定时先问用户,别猜")
        skill_md.write_text(edited, encoding="utf-8")
        cats = self.cats()
        cats["design"]["members"].append("anim")
        cats["design/motion"]["router"] = None
        self.write_cats(cats)
        self.routers()
        text = skill_md.read_text(encoding="utf-8")
        self.assertIn("不确定时先问用户,别猜", text)
        self.assertTrue(text.endswith(f"{shared.ROUTER_END}\n\n尾注不动。\n"))
        self.assertIn("- `anim`", text)
        # 子 Category 没有 Router 时把它的成员展开进来,不重复
        self.assertEqual([m["ref"] for m in self.members("demo-router-design")],
                         ["pixel", "demo-maintain", "anim", "ghost"])

    def test_missing_markers_fail_before_writing(self):
        (self.p.routers / "demo-router-motion" / "SKILL.md").write_text("---\nname: m\n---\n", encoding="utf-8")
        with self.assertRaises(shared.ShapeError):
            self.routers()
        self.assertFalse((self.p.routers / "demo-router-design" / "members.yaml").exists())


if __name__ == "__main__":
    unittest.main()
