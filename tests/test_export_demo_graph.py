"""端到端测试: 通过命令行调用 tools/export_demo_graph.py,用临时库、白名单与 Catalog,不碰真实 Index。"""
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
TOOL = REPO / "tools" / "export_demo_graph.py"

WHITELIST = {
    "public_github_skills": True,
    "public_names": {"Knowledge": ["笔记库", "3-方法研究"], "Project": ["项目"], "Skill": ["skills", "被隐藏"],
                     "Reference": []},
    "hide": ["被隐藏", "gh-hidden"],
    "rename": {"3-方法研究": "方法研究"},
    "summaries": {"笔记库": "笔记的结构。", "私密笔记": "不应出现"},
    "anon_label": {"Knowledge": "私人笔记", "Project": "项目文件", "Skill": "个人 Skill", "Reference": "踩坑记录"},
    "hits_cuts": {"high": 2},
    "blocklist": ["秘密词"],
}

CATALOG = {
    "gh-skill": {"entry": "referenced", "source": "https://github.com/o/r/tree/main/gh-skill"},
    "gh-hidden": {"entry": "referenced", "source": "https://github.com/o/r"},
    "local-ref": {"entry": "referenced", "source": "local-ref"},
    "vendored-gh": {"entry": "vendored", "source": "https://github.com/o/r/tree/main/vendored-gh"},
}

# (名称, 类别, 访问次数);path 与内容摘要都填上,检查导出时被丢弃
NODES = [
    ("笔记库", "Knowledge", 5),    # 1
    ("3-方法研究", "Knowledge", 1),  # 2
    ("私密笔记", "Knowledge", 0),      # 3
    ("项目", "Project", 0),            # 4
    ("skills", "Skill", 3),            # 5
    ("gh-skill", "Skill", 0),          # 6
    ("gh-hidden", "Skill", 0),         # 7
    ("local-ref", "Skill", 0),         # 8
    ("vendored-gh", "Skill", 0),       # 9
    ("被隐藏", "Skill", 0),            # 10
    ("项目", "Knowledge", 0),          # 11 同名但类别不在白名单
]
EDGES = [(1, 2), (1, 3), (5, 6), (5, 7), (5, 8), (5, 9), (5, 10)]


class ExportDemoGraph(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.db = self.root / "index.db"
        conn = sqlite3.connect(self.db)
        conn.executescript((REPO / "src" / "schema.sql").read_text())
        for i, (name, cat, hits) in enumerate(NODES, start=1):
            conn.execute("INSERT INTO nodes (id, name, category, hier_summary, content_summary, path, path_kind) "
                         "VALUES (?, ?, ?, '内部摘要', '内部内容', ?, 'file')", (i, name, cat, f"/secret/{i}"))
            if hits:
                conn.execute("INSERT INTO node_access (node_id, hits) VALUES (?, ?)", (i, hits))
        conn.executemany("INSERT INTO edges (parent_id, child_id, origin) VALUES (?, ?, 'scan')", EDGES)
        conn.commit(); conn.close()
        self.catalog = self.write("catalog.yaml", CATALOG)
        self.out = self.root / "out" / "graph.json"

    def write(self, name, data):
        p = self.root / name
        p.write_text(yaml.safe_dump(data, allow_unicode=True))
        return p

    def export(self, whitelist=WHITELIST, expect=0, *extra):
        wl = self.write("whitelist.yaml", whitelist)
        p = subprocess.run([sys.executable, str(TOOL), "--whitelist", str(wl), "--out", str(self.out),
                            "--db", str(self.db), "--catalog", str(self.catalog), *extra],
                           capture_output=True, text=True)
        if expect == 0:
            self.assertEqual(p.returncode, 0, f"stdout={p.stdout}\nstderr={p.stderr}")
        else:
            self.assertNotEqual(p.returncode, 0, f"stdout={p.stdout}\nstderr={p.stderr}")
        return p

    def graph(self):
        g = json.loads(self.out.read_text())
        return g, {n["id"]: n for n in g["nodes"]}

    def test_whitelisted_names_public_with_rename_and_summary(self):
        self.export()
        _, by = self.graph()
        self.assertEqual((by[1]["name"], by[1]["anon"], by[1]["hier_summary"]), ("笔记库", False, "笔记的结构。"))
        self.assertEqual((by[2]["name"], by[2]["anon"]), ("方法研究", False))
        self.assertEqual((by[4]["name"], by[5]["name"]), ("项目", "skills"))

    def test_other_nodes_anonymous_and_stripped(self):
        self.export()
        g, by = self.graph()
        self.assertEqual(len(g["nodes"]), len(NODES))
        self.assertEqual((by[3]["name"], by[3]["anon"], by[3]["hier_summary"]), ("私人笔记", True, ""))
        self.assertEqual((by[11]["name"], by[11]["anon"]), ("私人笔记", True))  # 白名单按类别生效
        self.assertEqual((by[8]["name"], by[8]["anon"]), ("个人 Skill", True))
        for n in g["nodes"]:
            self.assertIsNone(n["path"]); self.assertIsNone(n["content_summary"])
            self.assertNotIn("内部", json.dumps(n, ensure_ascii=False))
        self.assertNotIn("/secret/", self.out.read_text())

    def test_github_referenced_skills_public_and_hide_wins(self):
        self.export()
        _, by = self.graph()
        self.assertEqual((by[6]["name"], by[6]["anon"]), ("gh-skill", False))
        self.assertTrue(by[9]["anon"])   # vendored 即使 source 是 GitHub 也不公开
        self.assertTrue(by[8]["anon"])   # referenced 但 source 不是 GitHub
        self.assertEqual((by[7]["name"], by[7]["anon"]), ("个人 Skill", True))   # GitHub 规则放行、被 hide
        self.assertEqual((by[10]["name"], by[10]["anon"]), ("个人 Skill", True))  # 白名单放行、被 hide

    def test_github_rule_off(self):
        self.export({**WHITELIST, "public_github_skills": False})
        _, by = self.graph()
        self.assertTrue(by[6]["anon"])

    def test_edges_and_hits_bands(self):
        self.export()
        g, by = self.graph()
        self.assertEqual({(e["parent"], e["child"]) for e in g["edges"]}, set(EDGES))
        self.assertEqual((by[1]["hits"], by[2]["hits"], by[3]["hits"], by[5]["hits"]), (10, 1, 0, 10))

    def test_report_lists_public_names(self):
        report = self.root / "names.tsv"
        self.export(WHITELIST, 0, "--report", str(report))
        rows = [r.split("\t") for r in report.read_text().splitlines()[1:]]
        self.assertEqual({(r[1], r[3]) for r in rows},
                         {("笔记库", "whitelist"), ("3-方法研究", "whitelist"), ("项目", "whitelist"),
                          ("skills", "whitelist"), ("gh-skill", "github-skill")})

    def test_blocklist_hit_fails_without_writing(self):
        cases = {
            "公开名称": {**WHITELIST, "rename": {"笔记库": "含秘密词的库"}},
            "公开摘要": {**WHITELIST, "summaries": {"项目": "这里有秘密词"}},
            "匿名标签": {**WHITELIST, "anon_label": {**WHITELIST["anon_label"], "Knowledge": "秘密词笔记"}},
            "大小写": {**WHITELIST, "blocklist": ["SKILLS"]},
        }
        for label, wl in cases.items():
            with self.subTest(label):
                self.out.unlink(missing_ok=True)
                p = self.export(wl, 1)
                self.assertIn("BLOCKED", p.stderr)
                self.assertFalse(self.out.exists())

    def test_hidden_name_not_scanned(self):
        # 隐藏的名称不公开,命中 blocklist 也不该让导出失败
        self.export({**WHITELIST, "blocklist": ["gh-hidden"]})
        self.assertNotIn("gh-hidden", self.out.read_text())


if __name__ == "__main__":
    unittest.main()
