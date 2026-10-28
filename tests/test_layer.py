"""layer 命令测试: 按方案文件建分类节点并挂载,契约见 docs/openapi.yaml。"""
import json
import unittest

from test_cli import Base
from test_contract import check, response_schema


def plan_dict(skills, root="skills"):
    return {
        "root": root,
        "groups": [
            {"id": "A", "name": "调研", "subgroups": [{"id": "A1", "name": "网页", "desc": ""},
                                                     {"id": "A2", "name": "论文", "desc": ""}]},
            {"id": "B", "name": "浏览器", "subgroups": [{"id": "B1", "name": "浏览器", "desc": ""}]},
            {"id": "Z", "name": "待定", "subgroups": [{"id": "ZZ", "name": "待定", "desc": ""}]},
        ],
        "skills": skills,
    }


class Layer(Base):
    def setUp(self):
        super().setUp()
        self.root_id = self.add("skills", "Skill")
        self.ids = {n: self.add(n, "Skill", actor="ai", parents=[self.root_id]) for n in
                    ("firecrawl", "arxiv", "paper-reader", "agent-browser", "mystery")}
        self.plan_path = self.root / "plan.json"

    def write_plan(self, skills=None, **over):
        skills = skills or {
            "firecrawl": {"primary": "A1", "secondary": None},
            "arxiv": {"primary": "A2", "secondary": "A1"},
            "paper-reader": {"primary": "A2", "secondary": None},
            "agent-browser": {"primary": "B1", "secondary": None},
            "mystery": {"primary": "ZZ", "secondary": None},
        }
        p = plan_dict(skills)
        p.update(over)
        self.plan_path.write_text(json.dumps(p, ensure_ascii=False), encoding="utf-8")

    def layer(self, *a, expect=0):
        return self.run_cli("layer", "--plan", str(self.plan_path), *a, "--json", expect=expect)

    def lj(self, *a):
        return json.loads(self.layer(*a).stdout)

    def parents_of(self, name):
        n = self.j("get", self.ids[name])
        return sorted(self.j("get", p["parent"])["name"] for p in n["parents"])

    def test_builds_levels_collapses_single_subgroup_and_leaves_unplaced(self):
        self.write_plan()
        r = self.lj()
        created = {(x["name"], x["parent"]) for x in r["created_nodes"]}
        self.assertEqual(created, {("调研", "skills"), ("网页", "调研"), ("论文", "调研"), ("浏览器", "skills")})
        # 大类 B 只有一个子类: 不建同名子类,skill 直接挂大类
        self.assertEqual(self.parents_of("agent-browser"), ["skills", "浏览器"])
        self.assertEqual(self.parents_of("arxiv"), sorted(["skills", "网页", "论文"]))  # 主、次归属 + 尚未移除的根边
        self.assertEqual(self.parents_of("mystery"), ["skills"])  # 待定: 不动,也没建"待定"节点
        names = {c["name"] for c in self.j("children", self.root_id)}
        self.assertNotIn("待定", names)
        check(r, response_schema("applyLayerPlan"))

    def test_category_nodes_are_pure_directories_owned_by_human(self):
        self.write_plan()
        self.layer()
        n = next(c for c in self.j("children", self.root_id) if c["name"] == "调研")
        full = self.j("get", n["id"])
        self.assertEqual((full["path"], full["category"], full["pending"], full["hier_summary"]),
                         (None, "Skill", False, ""))
        edge = full["parents"][0]
        self.assertEqual((edge["parent"], edge["origin"]), (self.root_id, "manual"))

    def test_detach_root_moves_only_placed_nodes(self):
        self.write_plan()
        r = self.lj("--detach-root")
        self.assertEqual(r["detached"], 4)
        self.assertEqual(self.parents_of("firecrawl"), ["网页"])
        self.assertEqual(self.parents_of("arxiv"), sorted(["网页", "论文"]))
        self.assertEqual(self.parents_of("mystery"), ["skills"])  # 未归类的留在根下作收件箱
        self.assertEqual(self.parents_of("agent-browser"), ["浏览器"])

    def test_idempotent(self):
        self.write_plan()
        self.layer("--detach-root")
        before = self.run_cli("snapshot", "--out", str(self.root / "a.jsonl"))
        r = self.lj("--detach-root")
        self.assertEqual((r["created_nodes"], r["linked"], r["detached"], r["skipped"]), ([], 0, 0, []))
        self.run_cli("snapshot", "--out", str(self.root / "b.jsonl"))
        def strip(p):
            with open(p, encoding="utf-8") as f:
                return [{k: v for k, v in json.loads(l).items() if k != "modified_at"} for l in f]
        self.assertEqual(strip(self.root / "a.jsonl"), strip(self.root / "b.jsonl"))

    def test_second_run_after_first_without_detach_can_then_detach(self):
        self.write_plan()
        self.layer()
        self.assertEqual(self.parents_of("firecrawl"), ["skills", "网页"])
        r = self.lj("--detach-root")
        self.assertEqual((r["created_nodes"], r["linked"], r["detached"]), ([], 0, 4))
        self.assertEqual(self.parents_of("firecrawl"), ["网页"])

    def test_dry_run_writes_nothing_and_needs_no_maintenance_dir(self):
        self.write_plan()
        before = self.j("tree", "--depth", "5")
        p = self.run_cli("layer", "--plan", str(self.plan_path), "--dry-run", "--detach-root", "--json", cwd=self.root)
        r = json.loads(p.stdout)
        self.assertTrue(r["dry_run"])
        self.assertEqual(len(r["created_nodes"]), 4)
        self.assertEqual(r["detached"], 4)
        self.assertEqual(self.j("tree", "--depth", "5"), before)
        # 预演数字与真实应用一致
        real = self.lj("--detach-root")
        self.assertEqual((len(real["created_nodes"]), real["linked"], real["detached"]),
                         (len(r["created_nodes"]), r["linked"], r["detached"]))
        check(r, response_schema("applyLayerPlan"))

    def test_real_run_outside_maintenance_dir_forbidden(self):
        self.write_plan()
        self.run_cli("layer", "--plan", str(self.plan_path), cwd=self.root, expect=5)

    def test_invalid_plans_rejected_before_any_write(self):
        before = self.j("tree", "--depth", "5")
        self.plan_path.write_text("{坏", encoding="utf-8")
        self.layer(expect=2)
        self.plan_path.write_text(json.dumps({"root": "skills"}), encoding="utf-8")
        self.layer(expect=2)
        self.write_plan(skills={"firecrawl": {"primary": "A1", "secondary": "Q9"}})
        self.layer(expect=2)
        self.write_plan(skills={"firecrawl": {"primary": "A1", "secondary": None},
                                "arxiv": {"primary": "NOPE", "secondary": None}})
        self.layer(expect=2)
        self.assertEqual(self.j("tree", "--depth", "5"), before)  # 没有留下半成品
        self.run_cli("layer", "--plan", str(self.root / "不存在.json"), expect=2)

    def test_missing_or_ambiguous_root(self):
        self.write_plan(root="不存在的根")
        self.layer(expect=2)
        self.add("skills", "Skill")  # 第二个同名根
        self.write_plan()
        self.layer(expect=2)

    def test_unknown_skill_is_skipped_not_fatal(self):
        self.write_plan(skills={"firecrawl": {"primary": "A1", "secondary": None},
                                "不在库里的": {"primary": "A1", "secondary": None}})
        r = self.lj()
        self.assertEqual(r["skipped"], [{"name": "不在库里的", "reason": "库里没有这个节点"}])
        self.assertEqual(self.parents_of("firecrawl"), ["skills", "网页"])

    def test_duplicate_skill_name_aborts_before_writing(self):
        # 根下有两个同名节点: 无法唯一确定
        self.add("firecrawl", "Skill", actor="ai", parents=[self.root_id])
        self.write_plan()
        before = self.j("tree", "--depth", "5")
        self.layer(expect=2)
        self.assertEqual(self.j("tree", "--depth", "5"), before)

    def test_reuses_existing_category_node_with_same_name(self):
        existing = self.add("调研", "Skill", parents=[self.root_id])
        self.write_plan()
        r = self.lj()
        self.assertNotIn(("调研", "skills"), {(x["name"], x["parent"]) for x in r["created_nodes"]})
        subs = {c["name"] for c in self.j("children", existing)}
        self.assertEqual(subs, {"网页", "论文"})

    def test_export_tree_shows_secondary_as_reference(self):
        self.write_plan()
        self.layer("--detach-root")
        out = self.run_cli("tree", "--depth", "4", "--root", self.root_id).stdout.splitlines()
        arxiv = [l for l in out if "arxiv" in l]
        self.assertEqual(len(arxiv), 2)  # 两个父节点下各出现一次
        self.assertEqual(sum("→ 见" in l for l in arxiv), 1)  # 只有一处完整展开,另一处是引用

    def test_summarize_can_fill_category_nodes_bottom_up(self):
        # 分类节点没有挂载物,只生成层级摘要,且子节点摘要进入提示词
        from test_summarize import Gateway
        gw = Gateway(); self.addCleanup(gw.close)
        env = self.root / "t.env"
        env.write_text(f"OPENAI_BASE_URL={gw.url}\nOPENAI_API_KEY=k\nOPENAI_MODEL=m\nSUMMARY_RETRY_SLEEP=0\n", encoding="utf-8")
        self.write_plan()
        self.layer("--detach-root")
        self.run_cli("set", self.ids["arxiv"], "--hier-summary", "论文检索", "--actor", "human")
        r = json.loads(self.run_cli("summarize", "--env", str(env), "--json", "--ids",
                                    ",".join(c["id"] for c in self.j("children", self.root_id))).stdout)
        self.assertEqual(r["failed"], [])
        cat = next(c for c in self.j("children", self.root_id) if c["name"] == "调研")
        full = self.j("get", cat["id"])
        self.assertEqual((full["hier_summary"], full["hier_by"], full["content_summary"]), ("调研的层级摘要", "ai", None))


class LayerExtended(Base):
    """root 可新建并收纳现有挂载节点、root 按路径定位、detach_from。"""

    def setUp(self):
        super().setUp()
        self.disks = {}
        for d in ("disk-a", "disk-b"):
            path = self.root / d
            path.mkdir()
            self.disks[d] = self.add(d, "Project", path=str(path))
        self.projs = {}
        for name, disk in (("web", "disk-a"), ("cli", "disk-a"), ("paper", "disk-b"), ("empty", "disk-b")):
            self.projs[name] = self.add(name, "Project", actor="ai", parents=[self.disks[disk]])
        self.plan_path = self.root / "plan.json"

    def write_plan(self, root=None, skills=None, **over):
        plan = {
            "root": root or {"name": "项目", "create": {"category": "Project", "adopt_roots": ["disk-a", "disk-b"]}},
            "detach_from": ["disk-a", "disk-b"],
            "groups": [{"id": "A", "name": "应用", "subgroups": [{"id": "A.1", "name": "应用", "desc": ""}]},
                       {"id": "B", "name": "研究", "subgroups": [{"id": "B.1", "name": "研究", "desc": ""}]}],
            "skills": skills or {"web": {"primary": "A.1", "secondary": None},
                                 "cli": {"primary": "A.1", "secondary": "B.1"},
                                 "paper": {"primary": "B.1", "secondary": None},
                                 "empty": {"primary": "ZZ", "secondary": None}},
        }
        plan.update(over)
        self.plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")

    def layer(self, *a, expect=0):
        return self.run_cli("layer", "--plan", str(self.plan_path), *a, "--json", expect=expect)

    def lj(self, *a):
        return json.loads(self.layer(*a).stdout)

    def parents_of(self, i):
        return sorted(self.j("get", p["parent"])["name"] for p in self.j("get", i)["parents"])

    def roots(self):
        return sorted(n["name"] for n in self.j("tree", "--depth", "1"))

    def test_creates_root_adopts_existing_mounts_and_detaches_from_them(self):
        self.write_plan()
        r = self.lj("--detach")
        self.assertEqual({(x["name"], x["parent"]) for x in r["created_nodes"]},
                         {("项目", ""), ("应用", "项目"), ("研究", "项目")})
        # 边: 根→2 类别、根→2 收纳节点、主归属 3 条、次要 1 条
        self.assertEqual((r["linked"], r["detached"], r["skipped"]), (2 + 2 + 3 + 1, 3, []))
        self.assertEqual(self.roots(), ["项目"])  # disk-a / disk-b 不再是根,挂到了 项目 下
        top = self.j("get", next(n["id"] for n in self.j("tree", "--depth", "1")))
        self.assertEqual((top["path"], top["category"], top["pending"], top["parents"]), (None, "Project", False, []))
        self.assertEqual(self.parents_of(self.disks["disk-a"]), ["项目"])
        self.assertEqual(self.parents_of(self.projs["web"]), ["应用"])
        self.assertEqual(self.parents_of(self.projs["cli"]), ["应用", "研究"])
        self.assertEqual(self.parents_of(self.projs["paper"]), ["研究"])
        self.assertEqual(self.parents_of(self.projs["empty"]), ["disk-b"])  # 待定的留在原收件箱
        check(r, response_schema("applyLayerPlan"))

    def test_dry_run_matches_real_run_and_writes_nothing(self):
        self.write_plan()
        before = self.j("tree", "--depth", "5")
        d = json.loads(self.run_cli("layer", "--plan", str(self.plan_path), "--dry-run", "--detach", "--json",
                                    cwd=self.root).stdout)
        self.assertEqual(self.j("tree", "--depth", "5"), before)
        real = self.lj("--detach")
        self.assertEqual((len(d["created_nodes"]), d["linked"], d["detached"]),
                         (len(real["created_nodes"]), real["linked"], real["detached"]))

    def test_idempotent(self):
        self.write_plan()
        self.layer("--detach")
        r = self.lj("--detach")
        self.assertEqual((r["created_nodes"], r["linked"], r["detached"], r["skipped"]), ([], 0, 0, []))
        self.assertEqual(self.roots(), ["项目"])

    def test_without_detach_flag_old_edges_stay(self):
        self.write_plan()
        self.layer()
        self.assertEqual(self.parents_of(self.projs["web"]), ["disk-a", "应用"])
        r = self.lj("--detach")
        self.assertEqual((r["created_nodes"], r["linked"], r["detached"]), ([], 0, 3))
        self.assertEqual(self.parents_of(self.projs["web"]), ["应用"])

    def test_existing_root_is_reused_and_adopt_edges_not_duplicated(self):
        top = self.add("项目", "Project")
        self.run_cli("link", top, self.disks["disk-a"], "--actor", "human")
        self.write_plan()
        r = self.lj("--detach")
        self.assertNotIn(("项目", ""), {(x["name"], x["parent"]) for x in r["created_nodes"]})
        self.assertEqual(len([n for n in self.j("search", "项目")[:50] if n["name"] == "项目"]), 1)
        self.assertEqual(self.parents_of(self.disks["disk-a"]), ["项目"])
        self.assertEqual(self.parents_of(self.disks["disk-b"]), ["项目"])

    def test_duplicate_name_across_adopted_roots_aborts_before_writing(self):
        self.add("web", "Project", actor="ai", parents=[self.disks["disk-b"]])  # disk-a 与 disk-b 下各有一个 web
        self.write_plan()
        before = self.j("tree", "--depth", "5")
        self.layer("--detach", expect=2)
        self.assertEqual(self.j("tree", "--depth", "5"), before)
        self.assertEqual(self.roots(), sorted(["disk-a", "disk-b"]))

    def test_adopt_only_matches_nodes_of_the_same_category(self):
        # 知识库里恰好也有一个叫 disk-a 的挂载文件夹: 不应与项目类别的 disk-a 混淆
        other = self.root / "elsewhere"; other.mkdir()
        self.add("disk-a", "Knowledge", path=str(other))
        self.write_plan()
        r = self.lj("--detach")
        self.assertEqual(self.parents_of(self.disks["disk-a"]), ["项目"])
        self.assertEqual(r["skipped"], [])

    def test_adopt_root_missing_or_ambiguous_rejected(self):
        before = self.j("tree", "--depth", "5")
        self.write_plan(root={"name": "项目", "create": {"category": "Project", "adopt_roots": ["不存在"]}})
        self.layer(expect=2)
        self.assertEqual(self.j("tree", "--depth", "5"), before)

    def test_invalid_root_specs(self):
        self.write_plan(root={"create": {"category": "Project"}})
        self.layer(expect=2)
        self.write_plan(root={"name": "项目", "create": {"adopt_roots": []}})  # 缺 category
        self.layer(expect=2)
        self.write_plan(root={"name": "不存在的根"})  # 不带 create 时找不到就报错
        self.layer(expect=2)
        self.write_plan(detach_from="disk-a")
        self.layer(expect=2)

    def test_string_root_still_supported(self):
        self.write_plan(root="disk-a", detach_from=None,
                        skills={"web": {"primary": "A.1", "secondary": None}, "cli": {"primary": "A.1", "secondary": None}})
        r = self.lj("--detach")
        self.assertEqual(r["created_nodes"], [{"name": "应用", "parent": "disk-a"}])
        self.assertEqual(self.parents_of(self.projs["web"]), ["应用"])  # 默认从根 disk-a 移除

    def test_root_located_by_mount_path_even_when_it_has_a_parent(self):
        top = self.add("总目录", "Project")
        self.run_cli("link", top, self.disks["disk-a"], "--actor", "human")
        self.write_plan(root={"path": str(self.root / "disk-a")}, detach_from=["disk-a"],
                        skills={"web": {"primary": "A.1", "secondary": None},
                                "cli": {"primary": "B.1", "secondary": None}})
        r = self.lj("--detach")
        self.assertEqual({(x["name"], x["parent"]) for x in r["created_nodes"]}, {("应用", "disk-a"), ("研究", "disk-a")})
        self.assertEqual(self.parents_of(self.disks["disk-a"]), ["总目录"])  # 根自己的位置不变
        self.assertEqual(self.parents_of(self.projs["web"]), ["应用"])
        self.assertEqual(self.parents_of(self.projs["paper"]), ["disk-b"])  # 方案之外的不动

    def test_root_path_not_mounted_rejected(self):
        self.write_plan(root={"path": str(self.root / "没有节点挂载的路径")})
        self.layer(expect=2)

    def test_unknown_project_skipped(self):
        self.write_plan(skills={"web": {"primary": "A.1", "secondary": None},
                                "不存在": {"primary": "A.1", "secondary": None}})
        r = self.lj("--detach")
        self.assertEqual(r["skipped"], [{"name": "不存在", "reason": "库里没有这个节点"}])
        self.assertEqual(self.parents_of(self.projs["web"]), ["应用"])

    def test_summarize_fills_new_root_and_categories_bottom_up(self):
        from test_summarize import Gateway
        gw = Gateway(); self.addCleanup(gw.close)
        env = self.root / "t.env"
        env.write_text(f"OPENAI_BASE_URL={gw.url}\nOPENAI_API_KEY=k\nOPENAI_MODEL=m\nSUMMARY_RETRY_SLEEP=0\n", encoding="utf-8")
        self.write_plan()
        self.layer("--detach")
        top = next(n["id"] for n in self.j("tree", "--depth", "1"))
        ids = [top] + [c["id"] for c in self.j("children", top) if c["name"] in ("应用", "研究")]
        r = json.loads(self.run_cli("summarize", "--env", str(env), "--json", "--ids", ",".join(ids)).stdout)
        self.assertEqual(r["failed"], [])
        names = [json.loads(json.dumps(q))["body"]["messages"][1]["content"].split("节点名: ")[1].splitlines()[0] for q in gw.requests]
        self.assertEqual(names[-1], "项目")  # 根最后写,能看到类别节点刚写好的摘要


if __name__ == "__main__":
    unittest.main()
