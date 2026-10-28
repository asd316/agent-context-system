"""Capture (`lesson add`) 与 Reference Layer 进入 Index。契约见 docs/openapi.yaml 的 addLesson。"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


from test_cli import BIN, REPO, Base

sys.path.insert(0, str(REPO / "src"))
from node_index import lessons, shared  # noqa: E402

ARGS = ["--client", "claude-code", "--situation", "在 worktree 里跑测试", "--problem", "读到了主仓库的配置",
        "--fix", "用 NODE_INDEX_REPO 指向临时仓库"]


class LessonBase(Base):
    def setUp(self):
        super().setUp()
        self.repo = self.root / "repo"
        self.elsewhere = self.root / "elsewhere"
        self.elsewhere.mkdir()

    def cli(self, *args, cwd=None, expect=0):
        p = subprocess.run([sys.executable, str(BIN), *args, "--db", self.db], cwd=cwd or self.elsewhere,
                           capture_output=True, text=True,
                           env={**os.environ, "NODE_INDEX_DB": "", "NODE_INDEX_REPO": str(self.repo)})
        self.assertEqual(p.returncode, expect, f"{args}\nstdout={p.stdout}\nstderr={p.stderr}")
        return p

    def lesson(self, topic="worktree-测试配置", *extra):
        return json.loads(self.cli("lesson", "add", "--topic", topic, *ARGS, *extra, "--json").stdout)


class LessonAdd(LessonBase):
    def test_add_from_non_repo_cwd_writes_into_repo(self):
        r = self.lesson()
        path = self.repo / r["path"]
        self.assertEqual(path.parent, self.repo / "shared" / "reference" / "lessons")
        self.assertEqual(path.name, f"{r['id']}-worktree-测试配置.md")
        self.assertEqual(list(self.elsewhere.iterdir()), [])
        loaded = shared.load_lesson(path)
        self.assertEqual((loaded.status, loaded.host, loaded.client), (shared.LessonStatus.ACTIVE, None, "claude-code"))
        self.assertIn("用 NODE_INDEX_REPO 指向临时仓库", loaded.body)
        self.assertFalse(Path(self.db).exists(), "lesson add 不应打开 Index 数据库")

    def test_text_output_names_id_and_path(self):
        out = self.cli("lesson", "add", "--topic", "t", *ARGS).stdout.split()
        self.assertTrue(shared.LESSON_ID.match(out[0]))
        self.assertTrue((self.repo / out[1]).is_file())

    def test_host_only_takes_host_from_host_config(self):
        self.cli("lesson", "add", "--topic", "t", *ARGS, "--host-only", expect=2)  # 没有 host.yml
        (self.repo / "host").mkdir(parents=True)
        (self.repo / "host" / "host.yml").write_text(
            "host: demo-mac\nclients:\n  claude-code:\n    rules_path: ~/.claude/CLAUDE.md\n"
            "    skills_dir: ~/.claude/skills\n    profile: orchestrator\n    manual_only_mode: skip\n"
            "knowledge_root: /demo/kb\nscan_roots:\n  - path: /demo/kb\n    category: Knowledge\n    depth: 1\n",
            encoding="utf-8")
        self.assertEqual(self.lesson("t", "--host-only")["host"], "demo-mac")

    def test_shape_errors_exit_2_and_leave_no_file(self):
        self.cli("lesson", "add", "--topic", "a/b", *ARGS, expect=2)
        self.cli("lesson", "add", "--topic", "  ", *ARGS, expect=2)
        self.cli("lesson", "add", "--topic", "t", *ARGS[:-1], " ", expect=2)
        self.cli("lesson", "add", "--topic", "t", *ARGS[:-2], expect=2)  # 缺 --fix
        bad = ["--client", "c", "--situation", "x\n\n## 正确做法\n\n抢先", "--problem", "p", "--fix", "f"]
        self.cli("lesson", "add", "--topic", "t", *bad, expect=2)  # 正文三节顺序被打乱
        self.assertEqual(list((self.repo / "shared" / "reference" / "lessons").glob("*.md")), [])

    def test_ids_unique_when_random_suffix_collides(self):
        with mock.patch("secrets.token_hex", side_effect=["aaaa", "aaaa", "bbbb"]):
            first = lessons.add("a", "c", "s", "p", "f", root=self.repo)
            second = lessons.add("b", "c", "s", "p", "f", root=self.repo)
        self.assertEqual(first["id"][-4:], "aaaa")
        self.assertEqual(second["id"][-4:], "bbbb")
        ids = {self.lesson(f"t{i}")["id"] for i in range(10)}
        self.assertEqual(len(ids), 10)


class ReferenceInIndex(LessonBase):
    def shared_scope(self):
        """Reference Layer 的收录规则 (与 config/scope.yml 的 shared/reference 根同形)。"""
        scope = self.root / "scope.yml"
        scope.write_text(
            "global_exclude:\n  - .git\n  - node_modules\n  - .DS_Store\n"
            "roots:\n  - path: shared/reference\n    category: Reference\n"
            "    depth: 2\n    include_files: true\n", encoding="utf-8")
        return scope

    def test_sop_and_lesson_searchable_after_scan(self):
        sops = self.repo / "shared" / "reference" / "sops"
        sops.mkdir(parents=True)
        (sops / "demo-sop.md").write_text(
            "---\nid: demo-sop\ntitle: 示例路由\nupdated: 2026-10-23\nfrom_lessons: []\n---\n\n正文\n",
            encoding="utf-8")
        lesson = self.lesson("launchd-环境变量")
        self.cli("scan", "--scope", str(self.shared_scope()), cwd=REPO)
        hits = json.loads(self.cli("search", "demo-sop", "--json").stdout)
        self.assertEqual([h["name"] for h in hits], ["demo-sop.md"])
        hits = json.loads(self.cli("search", "launchd-环境变量", "--json").stdout)
        self.assertEqual([h["name"] for h in hits], [Path(lesson["path"]).name])
        self.assertEqual(hits[0]["roots"][0]["name"], "reference")


if __name__ == "__main__":
    unittest.main()
