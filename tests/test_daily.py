"""每日流水线脚本测试: 全部用临时目录、临时数据库和本地假网关,不碰真实数据。"""
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

from test_cli import Base, REPO
from test_summarize import FAKE_KEY, Gateway

DAILY = REPO / "bin" / "node-index-daily"


class Daily(Base):
    def setUp(self):
        super().setUp()
        self.gw = Gateway()
        self.addCleanup(self.gw.close)
        self.state = self.root / "state"
        self.env = self.root / "t.env"
        self.write_env()
        kb = self.root / "kb"
        (kb / "设计").mkdir(parents=True)
        (kb / "产品").mkdir()
        self.kb = kb
        self.scope = self.root / "scope.yml"
        self.scope.write_text(f"roots:\n  - path: {kb}\n    category: Knowledge\n    depth: 1\n", encoding="utf-8")
        self.prompt = self.root / "prompt.md"
        self.snap = self.root / "snap.jsonl"
        # digest 写 shared/inbox 与 host/: 指向临时仓库,不碰本仓库
        self.repo = self.root / "repo"
        (self.repo / "shared").mkdir(parents=True)
        (self.repo / "host").mkdir()
        for name in ("catalog.yaml", "categories.yaml"):
            (self.repo / "shared" / name).write_text("{}\n", encoding="utf-8")
        (self.repo / "host" / "host.yml").write_text("host: t\nclients: {}\nknowledge_root: /tmp\n", encoding="utf-8")
        self.run_env = {**os.environ, "NODE_INDEX_REPO": str(self.repo)}

    def write_env(self, **over):
        v = {"OPENAI_BASE_URL": self.gw.url, "OPENAI_API_KEY": FAKE_KEY, "OPENAI_MODEL": "m",
             "SUMMARY_RETRY_SLEEP": "0", "SUMMARY_TIMEOUT_SECONDS": "5", **over}
        self.env.write_text("\n".join(f"{k}={x}" for k, x in v.items()) + "\n", encoding="utf-8")

    def daily(self, *extra, cwd=None, expect=0):
        cmd = [sys.executable, str(DAILY), "--db", self.db, "--scope", str(self.scope), "--env", str(self.env),
               "--prompt-out", str(self.prompt), "--snapshot-out", str(self.snap), "--state-dir", str(self.state), *extra]
        p = subprocess.run(cmd, cwd=cwd or self.root, capture_output=True, text=True, env=self.run_env)
        self.assertEqual(p.returncode, expect, f"stdout={p.stdout}\nstderr={p.stderr}")
        return p

    def status(self):
        return json.loads((self.state / "last-run.json").read_text(encoding="utf-8"))

    def test_sop_hier_runs_build_and_not_activate(self):
        sop = self.root / "reference" / "sops"
        sop.mkdir(parents=True)
        (sop / "routing.md").write_text("怎么做")
        self.scope.write_text(
            f"roots:\n  - path: {self.kb}\n    category: Knowledge\n    depth: 1\n"
            f"  - path: {sop}\n    category: Reference\n    depth: 1\n    include_files: true\n",
            encoding="utf-8")
        p = self.daily()
        self.assertEqual([s["step"] for s in self.status()["steps"]],
                         ["ingest", "scan", "check", "summarize", "build", "digest", "render", "snapshot"])
        self.assertIn("刷新 Member list", p.stdout)
        self.assertNotIn("activate", p.stdout)

    def test_runs_all_steps_in_order_and_produces_every_artifact(self):
        p = self.daily()
        st = self.status()
        self.assertEqual([s["step"] for s in st["steps"]], ["ingest", "scan", "check", "summarize", "digest", "render", "snapshot"])
        self.assertTrue(st["ok"])
        tree = self.j("tree", "--depth", "3")
        self.assertEqual([n["name"] for n in tree], ["kb"])
        kb = self.j("get", tree[0]["id"])
        self.assertEqual((kb["hier_by"], kb["content_by"]), ("ai", "ai"))  # 摘要真的生成了
        self.assertIn("节点索引", self.prompt.read_text(encoding="utf-8"))
        self.assertIn("kb", self.prompt.read_text(encoding="utf-8"))
        self.assertEqual(len(self.snap.read_text(encoding="utf-8").splitlines()), 3)
        self.assertNotIn(FAKE_KEY, p.stdout + p.stderr)  # 密钥不进日志

    def test_second_run_is_idempotent_and_makes_no_model_calls(self):
        self.daily()
        n = len(self.gw.requests)
        self.daily()
        self.assertEqual(len(self.gw.requests), n)
        self.assertTrue(self.status()["ok"])

    def test_picks_up_new_folder_and_refreshes_only_what_changed(self):
        self.daily()
        n = len(self.gw.requests)
        time.sleep(1.1)  # 修改时间精度是秒;不等的话父目录的新旧 mtime 可能落在同一秒,结果随机
        (self.kb / "新来的").mkdir()
        self.daily()
        names = {x["name"] for x in self.j("children", self.j("tree", "--depth", "1")[0]["id"])}
        self.assertIn("新来的", names)
        # 新节点 1 次;加子文件夹改变了父目录自己的 mtime,它的内容摘要按设计过期,再 1 次。"设计"和"产品"没动,不重写
        self.assertEqual(len(self.gw.requests), n + 2)
        prompts = [r["body"]["messages"][1]["content"] for r in self.gw.requests[n:]]
        self.assertTrue(any("节点名: 新来的" in p for p in prompts))
        self.assertFalse(any("节点名: 设计" in p or "节点名: 产品" in p for p in prompts))

    def test_detects_missing_path(self):
        self.daily()
        (self.kb / "产品").rmdir()
        self.daily()
        kb_id = self.j("tree", "--depth", "1")[0]["id"]
        missing = [c for c in self.j("children", kb_id) if c["path_state"] == "missing"]
        self.assertEqual([c["name"] for c in missing], ["产品"])
        self.assertIn("⚠", self.prompt.read_text(encoding="utf-8"))

    def test_failed_step_does_not_stop_later_steps_and_exit_is_nonzero(self):
        self.write_env(OPENAI_BASE_URL="http://127.0.0.1:9/v1")  # 模型网关不可达
        p = self.daily(expect=1)
        st = self.status()
        by = {s["step"]: s for s in st["steps"]}
        self.assertFalse(st["ok"])
        # summarize 命令自身对单个节点失败只记入 failed、退出码 0,每日脚本读结果后判为失败
        self.assertEqual(by["summarize"]["exit"], 1)
        for s in ("scan", "check", "digest", "render", "snapshot"):
            self.assertEqual(by[s]["exit"], 0, s)
        self.assertTrue(self.prompt.exists() and self.snap.exists())  # 失败不影响后面的步骤
        self.assertNotIn(FAKE_KEY, p.stdout + p.stderr)

    def test_vanished_path_is_not_a_summarize_failure(self):
        # 制造"节点还没有摘要、路径又没了": 先让网关不可达(摘要留空),再删目录,再恢复网关
        self.write_env(OPENAI_BASE_URL="http://127.0.0.1:9/v1")
        self.daily(expect=1)
        (self.kb / "产品").rmdir()
        self.write_env()
        p = self.daily()
        st = self.status()
        by = {s["step"]: s for s in st["steps"]}
        self.assertTrue(st["ok"], p.stdout)
        self.assertEqual(by["summarize"]["failed_nodes"], 0)
        self.assertIn("路径失效跳过", by["summarize"]["summary"])  # 确实走到了被忽略的分支
        kb_id = self.j("tree", "--depth", "1")[0]["id"]
        gone = next(c for c in self.j("children", kb_id) if c["name"] == "产品")
        self.assertEqual((gone["path_state"], gone["hier_summary"]), ("missing", ""))
        self.daily()  # 之后每天重跑仍然是绿的,不会因为这个失效节点永久变红
        self.assertTrue(self.status()["ok"])

    def test_summarize_failures_make_the_day_red_with_counts(self):
        self.write_env(OPENAI_BASE_URL="http://127.0.0.1:9/v1")
        p = self.daily(expect=1)
        by = {s["step"]: s for s in self.status()["steps"]}
        self.assertEqual(by["summarize"]["failed_nodes"], 3)
        self.assertEqual(by["summarize"]["exit"], 1)
        self.assertIn("失败 3", p.stdout)
        self.assertIsNotNone(by["summarize"]["error"])

    def test_bad_scope_fails_scan_but_still_renders_and_snapshots(self):
        self.scope.write_text("roots: []\n", encoding="utf-8")
        self.daily(expect=1)
        by = {s["step"]: s for s in self.status()["steps"]}
        self.assertNotEqual(by["scan"]["exit"], 0)
        self.assertIsNotNone(by["scan"]["error"])
        self.assertEqual((by["render"]["exit"], by["snapshot"]["exit"]), (0, 0))

    def test_second_instance_is_skipped_while_first_runs(self):
        import fcntl
        self.state.mkdir(parents=True, exist_ok=True)
        held = open(self.state / "daily.lock", "w")
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            p = self.daily()
            self.assertIn("已有另一个实例在运行", p.stdout)
            self.assertFalse((self.state / "last-run.json").exists())
            self.assertEqual(self.gw.requests, [])
        finally:
            held.close()
        self.daily()  # 锁释放后可正常运行
        self.assertTrue(self.status()["ok"])

    def test_digest_runs_once_then_waits_seven_days(self):
        self.daily()
        st = json.loads((self.repo / "host" / "digest-state.json").read_text(encoding="utf-8"))
        p = self.daily()
        self.assertIn("digest: ok", p.stdout)
        self.assertIn("未到期", p.stdout)
        self.assertEqual(json.loads((self.repo / "host" / "digest-state.json").read_text(encoding="utf-8")), st)
        (self.repo / "host" / "digest-state.json").write_text(json.dumps({"last_run": "2026-01-01T00:00:00"}))
        self.assertNotIn("未到期", self.daily().stdout)  # 超过 7 天: 再跑一次

    def test_works_from_any_cwd_and_with_minimal_path(self):
        env = {"HOME": str(Path.home()), "PATH": "/usr/bin:/bin", "NODE_INDEX_REPO": str(self.repo)}  # 模拟 launchd 的精简环境
        cmd = [sys.executable, str(DAILY), "--db", self.db, "--scope", str(self.scope), "--env", str(self.env),
               "--prompt-out", str(self.prompt), "--snapshot-out", str(self.snap), "--state-dir", str(self.state)]
        p = subprocess.run(cmd, cwd="/", capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(self.status()["ok"])

    def test_log_lines_are_one_per_step_and_timestamped(self):
        p = self.daily()
        lines = [l for l in p.stdout.splitlines() if l.startswith("[")]
        steps = [l for l in lines if any(f"] {s}:" in l for s in ("ingest", "scan", "check", "summarize", "digest", "render", "snapshot"))]
        self.assertEqual(len(steps), 7)
        self.assertTrue(all(l[1:5].isdigit() for l in lines))


class Gate(Daily):
    """--gate-dir: 事件触发时的指纹闸门。监视目录里无关的写入不能让流水线跑起来。"""

    def setUp(self):
        super().setUp()
        self.gate = self.root / "central"
        for n in ("alpha", "beta"):
            (self.gate / n).mkdir(parents=True)
            (self.gate / n / "SKILL.md").write_text(f"{n} v1", encoding="utf-8")

    def gated(self, expect=0):
        return self.daily("--gate-dir", str(self.gate), expect=expect)

    def runs(self):
        return len([l for l in self.log_lines if "开始每日维护" in l])

    @property
    def log_lines(self):
        return getattr(self, "_log", [])

    def run_gated(self, expect=0):
        p = self.gated(expect=expect)
        ran = "开始每日维护" in p.stdout
        self._log = self.log_lines + (p.stdout.splitlines() if ran else [])
        return p, ran

    def test_first_run_runs_then_unchanged_is_silent_and_touches_nothing(self):
        p, ran = self.run_gated()
        self.assertTrue(ran)
        self.assertIn("触发原因", p.stdout)
        st = self.state / "last-run.json"
        before = (st.stat().st_mtime_ns, st.read_bytes(), len(self.gw.requests))
        p, ran = self.run_gated()
        self.assertFalse(ran)
        self.assertEqual((p.stdout, p.stderr), ("", ""))
        self.assertEqual((st.stat().st_mtime_ns, st.read_bytes(), len(self.gw.requests)), before)

    def test_new_removed_and_edited_skill_each_trigger_a_run(self):
        self.run_gated()
        (self.gate / "gamma").mkdir(); (self.gate / "gamma" / "SKILL.md").write_text("g", encoding="utf-8")
        self.assertTrue(self.run_gated()[1])  # 新增
        self.assertFalse(self.run_gated()[1])
        (self.gate / "alpha" / "SKILL.md").write_text("alpha v2 更长了", encoding="utf-8")
        self.assertTrue(self.run_gated()[1])  # 说明文档被更新
        self.assertFalse(self.run_gated()[1])
        import shutil
        shutil.rmtree(self.gate / "beta")
        self.assertTrue(self.run_gated()[1])  # 删除
        self.assertFalse(self.run_gated()[1])

    def test_unrelated_writes_do_not_trigger(self):
        # 这正是 skill-manager 自己的行为: 往 .git、.skills-manager 里不停写元数据,递归的 WatchPaths 会被反复叫醒
        self.run_gated()
        for rel in (".git/index", ".skills-manager/scenarios/a.json", ".skills-manager/schema.json", ".DS_Store", "顶层文件.txt"):
            f = self.gate / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("noise", encoding="utf-8")
        (self.gate / "alpha" / "scripts").mkdir()
        (self.gate / "alpha" / "scripts" / "run.sh").write_text("echo", encoding="utf-8")  # skill 里的非说明文件
        (self.gate / "alpha" / "data.json").write_text("{}", encoding="utf-8")
        self.assertFalse(self.run_gated()[1])

    def test_failed_run_does_not_record_fingerprint_so_next_trigger_retries(self):
        self.scope.rename(self.root / "scope.bak")  # 让 scan 失败
        p, ran = self.run_gated(expect=1)
        self.assertTrue(ran)
        (self.root / "scope.bak").rename(self.scope)
        p, ran = self.run_gated()  # 目录没变,但上次失败了,所以仍要跑
        self.assertTrue(ran)
        self.assertFalse(self.run_gated()[1])  # 成功之后才静默

    def test_unreadable_gate_dir_fails_loudly_not_silently(self):
        p = self.daily("--gate-dir", str(self.root / "不存在的目录"), expect=1)
        self.assertIn("闸门路径无法读取", p.stdout)

    def test_file_gate_and_multiple_gates(self):
        cats = self.root / "categories.yaml"
        cats.write_text("a: {name: A}\n", encoding="utf-8")
        run = lambda: "开始每日维护" in self.daily("--gate", str(self.gate), "--gate", str(cats)).stdout
        self.assertTrue(run())
        self.assertFalse(run())
        cats.write_text("a: {name: AB}\n", encoding="utf-8")
        self.assertTrue(run())  # 文件闸门: 内容变了
        self.assertFalse(run())
        (self.gate / "alpha" / "SKILL.md").write_text("alpha v3 改了", encoding="utf-8")
        self.assertTrue(run())  # 另一个闸门仍生效

    def test_without_gate_flag_behaviour_is_unchanged(self):
        self.daily()
        p = self.daily()
        self.assertIn("开始每日维护", p.stdout)  # 没有闸门时每次都完整运行
        self.assertNotIn("触发原因", p.stdout)


if __name__ == "__main__":
    unittest.main()
