"""摘要流水线测试: 用本地假的 OpenAI 兼容网关,不碰真实 API 与真实密钥。"""
import json
import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from test_cli import Base, REPO
from test_contract import check, response_schema

FAKE_KEY = "sk-fake-should-never-leak-0123456789"


class Gateway:
    """按提示词里的节点名决定行为,便于覆盖各种坏回复。"""

    def __init__(self):
        self.requests = []
        self.calls = {}
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
                prompt = body["messages"][1]["content"]
                name = next((l.split(": ", 1)[1] for l in prompt.splitlines() if l.startswith("节点名: ")), "")
                n = outer.calls[name] = outer.calls.get(name, 0) + 1
                feedback = len(body["messages"]) > 2

                def reply(content, code=200):
                    data = json.dumps({"choices": [{"message": {"content": content}}]}) if code == 200 else content
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(data.encode())

                ok = json.dumps({"hier": f"{name}的层级摘要。", "content": f"{name}的内容摘要"}, ensure_ascii=False)
                if name == "超长":
                    reply(ok if feedback else json.dumps({"hier": "长" * 50, "content": "c"}, ensure_ascii=False))
                elif name == "永远超长":
                    reply(json.dumps({"hier": "长" * 50, "content": "c"}, ensure_ascii=False))
                elif name == "空回复":
                    reply("")
                elif name == "代码块":
                    reply("```json\n" + ok + "\n```")
                elif name == "坏JSON":
                    reply(ok if feedback else "我觉得这个目录很好")
                elif name == "抖动500":
                    reply("boom " + FAKE_KEY, 500) if n == 1 else reply(ok)
                elif name == "鉴权失败":
                    reply("denied " + FAKE_KEY, 401)
                else:
                    reply(ok)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class SummarizeBase(Base):
    def setUp(self):
        super().setUp()
        self.gw = Gateway()
        self.addCleanup(self.gw.close)
        self.env = self.root / "test.env"
        self.write_env()

    def write_env(self, **over):
        v = {"OPENAI_BASE_URL": self.gw.url, "OPENAI_API_KEY": FAKE_KEY, "OPENAI_MODEL": "fake-model",
             "SUMMARY_RETRY_SLEEP": "0", "SUMMARY_TIMEOUT_SECONDS": "5", **over}
        self.env.write_text("\n".join(f"{k}={x}" for k, x in v.items()) + "\n", encoding="utf-8")

    def folder(self, name, files=()):
        d = self.root / name
        d.mkdir()
        for f, text in files:
            (d / f).write_text(text, encoding="utf-8")
        return d

    def node(self, name, **kw):
        return self.add(name, "Knowledge", **kw)

    def summarize(self, *args, expect=0, **kw):
        return self.run_cli("summarize", "--env", str(self.env), *args, "--json", expect=expect, **kw)

    def sj(self, *args, **kw):
        return json.loads(self.summarize(*args, **kw).stdout)


class Pipeline(SummarizeBase):
    def test_fills_both_summaries_and_records_ai_source(self):
        d = self.folder("档案", [("README.md", "这是一个档案目录")])
        a = self.node("档案", path=str(d))
        r = self.sj()
        self.assertEqual((r["targets"], r["ok"], r["failed"]), (1, [a], []))
        n = self.j("get", a)
        self.assertEqual((n["hier_summary"], n["content_summary"]), ("档案的层级摘要", "档案的内容摘要"))
        self.assertEqual((n["hier_by"], n["content_by"], n["pending"]), ("ai", "ai", False))
        check(r, response_schema("summarize"))

    def test_request_shape_auth_and_material(self):
        d = self.folder("资料", [("SKILL.md", "技能说明正文"), (".env", "SECRET=1"), ("a.md", "x")])
        self.node("资料", path=str(d))
        self.summarize()
        req = self.gw.requests[0]
        self.assertTrue(req["path"].endswith("/chat/completions"))
        self.assertEqual(req["auth"], "Bearer " + FAKE_KEY)
        self.assertEqual(req["body"]["model"], "fake-model")
        self.assertEqual(req["body"]["thinking"], {"type": "disabled"})
        prompt = req["body"]["messages"][1]["content"]
        self.assertIn("技能说明正文", prompt)
        self.assertIn("a.md", prompt)
        self.assertNotIn(".env", prompt)  # 隐藏文件不进材料
        self.assertNotIn("SECRET", prompt)

    def test_thinking_can_be_left_on_by_env(self):
        self.write_env(SUMMARY_DISABLE_THINKING="0")
        d = self.folder("d")
        self.node("d", path=str(d))
        self.summarize()
        self.assertNotIn("thinking", self.gw.requests[0]["body"])

    def test_node_without_mount_gets_only_hier(self):
        a = self.node("纯目录")
        self.sj()
        n = self.j("get", a)
        self.assertEqual((n["hier_summary"], n["content_summary"]), ("纯目录的层级摘要", None))

    def test_idempotent_and_resumable(self):
        d1, d2 = self.folder("甲"), self.folder("乙")
        self.node("甲", path=str(d1)); self.node("乙", path=str(d2))
        self.assertEqual(self.sj()["targets"], 2)
        n_calls = len(self.gw.requests)
        self.assertEqual(self.sj()["targets"], 0)
        self.assertEqual(len(self.gw.requests), n_calls)  # 第二次不再调用

    def test_human_summaries_never_overwritten_even_with_force(self):
        d = self.folder("人写")
        a = self.node("人写", path=str(d), hier_summary="人写的层级")
        self.run_cli("set", a, "--content-summary", "人写的内容", "--actor", "human")
        self.assertEqual(self.sj("--force")["targets"], 0)
        n = self.j("get", a)
        self.assertEqual((n["hier_summary"], n["content_summary"]), ("人写的层级", "人写的内容"))
        self.assertEqual(self.gw.requests, [])

    def test_partial_human_only_fills_missing_side(self):
        d = self.folder("半")
        a = self.node("半", path=str(d), hier_summary="人写的层级")
        self.sj()
        n = self.j("get", a)
        self.assertEqual((n["hier_summary"], n["hier_by"]), ("人写的层级", "human"))
        self.assertEqual((n["content_summary"], n["content_by"]), ("半的内容摘要", "ai"))

    def test_force_rewrites_ai_summaries(self):
        d = self.folder("重写")
        a = self.node("重写", path=str(d), actor="ai", hier_summary="旧摘要")
        self.assertEqual(self.sj()["targets"], 1)  # 只缺内容摘要
        self.assertEqual(self.j("get", a)["hier_summary"], "旧摘要")
        self.assertEqual(self.sj("--force")["targets"], 1)
        self.assertEqual(self.j("get", a)["hier_summary"], "重写的层级摘要")

    def test_stale_content_summary_regenerated(self):
        f = self.root / "n.md"; f.write_text("v1")
        a = self.node("文", path=str(f))
        self.sj()
        self.assertEqual(self.sj()["targets"], 0)
        t = time.time() + 300
        os.utime(f, (t, t))
        self.assertEqual(self.sj()["ok"], [a])

    def test_editing_skill_md_inside_folder_marks_summary_stale(self):
        # skill 被 git pull 更新时,文件夹自身的修改时间不变,只有里面的 SKILL.md 变了
        d = self.folder("技能", [("SKILL.md", "v1"), ("data.txt", "x")])
        a = self.node("技能", path=str(d))
        self.sj()
        self.assertEqual(self.sj()["targets"], 0)
        dir_mtime = os.stat(d).st_mtime
        future = time.time() + 300
        os.utime(d / "SKILL.md", (future, future))
        os.utime(d, (dir_mtime, dir_mtime))  # 明确保持文件夹自身时间不变
        self.assertEqual(os.stat(d).st_mtime, dir_mtime)
        _, scope = None, self.root / "scope.yml"
        scope.write_text(f"roots:\n  - path: {self.root}\n    category: Knowledge\n    depth: 0\n")
        self.assertEqual(self.j("coverage", scope=scope)["stale_content_summaries"], 1)
        self.assertEqual(self.sj()["ok"], [a])
        self.assertEqual(self.sj()["targets"], 0)  # 重新生成后记录了新的时间

    def test_editing_unrelated_file_inside_folder_does_not_trigger(self):
        d = self.folder("技能2", [("SKILL.md", "v1"), ("data.txt", "x")])
        self.node("技能2", path=str(d))
        self.sj()
        future = time.time() + 300
        os.utime(d / "data.txt", (future, future))
        self.assertEqual(self.sj()["targets"], 0)

    def test_ids_limit_and_workers(self):
        ds = [self.folder(f"目录{i}") for i in range(5)]
        ids = [self.node(f"目录{i}", path=str(d)) for i, d in enumerate(ds)]
        self.assertEqual(len(self.sj("--limit", "2")["ok"]), 2)
        self.assertEqual(self.sj("--ids", ids[4], "--workers", "1")["ok"], [ids[4]])
        self.assertEqual(self.sj("--limit", "0")["targets"], 2)  # 前两个与 ids[4] 已完成,余下 2 个
        self.run_cli("summarize", "--env", str(self.env), "--ids", "n99", expect=3)

    def test_limit_default_read_from_env_and_zero_means_unlimited(self):
        ids = [self.node(f"目录{i}", path=str(self.folder(f"目录{i}"))) for i in range(4)]
        self.write_env(SUMMARY_MAX_PER_RUN="3")
        self.assertEqual(len(self.sj()["ok"]), 3)
        self.write_env(SUMMARY_MAX_PER_RUN="0")
        self.assertEqual(len(self.sj()["ok"]), 1)


class BottomUp(SummarizeBase):
    def test_parent_prompt_contains_children_fresh_summaries(self):
        root = self.folder("根")
        kid = self.folder("子甲")
        r = self.node("根", path=str(root))
        self.node("子甲", path=str(kid), parents=[r])
        self.sj()
        names = [self.req_name(q) for q in self.gw.requests]
        self.assertEqual(names, ["子甲", "根"])  # 子节点先于父节点
        parent_prompt = self.gw.requests[1]["body"]["messages"][1]["content"]
        self.assertIn("子甲 — 子甲的层级摘要", parent_prompt)  # 看到的是刚写好的摘要

    def test_multi_parent_child_goes_before_both_parents(self):
        a, b = self.folder("甲"), self.folder("乙")
        c = self.folder("共享")
        na = self.node("甲", path=str(a)); nb = self.node("乙", path=str(b))
        self.node("共享", path=str(c), parents=[na, nb])
        self.sj("--workers", "1")
        names = [self.req_name(q) for q in self.gw.requests]
        self.assertEqual(names[0], "共享")
        self.assertEqual(set(names[1:]), {"甲", "乙"})

    def test_prompt_rules_and_no_path_repetition_instruction(self):
        d = self.folder("规则")
        self.node("规则", path=str(d))
        self.summarize()
        p = self.gw.requests[0]["body"]["messages"][1]["content"]
        self.assertIn("不要在摘要里写出挂载路径", p)
        self.assertIn("不要以\"材料不足\"开头", p)

    @staticmethod
    def req_name(req):
        prompt = req["body"]["messages"][1]["content"]
        return next(l.split(": ", 1)[1] for l in prompt.splitlines() if l.startswith("节点名: "))


class BadReplies(SummarizeBase):
    def failed(self, name):
        d = self.folder(name)
        a = self.node(name, path=str(d))
        r = self.sj()
        return a, r

    def test_overlong_is_retried_with_feedback_not_truncated(self):
        a, r = self.failed("超长")
        self.assertEqual(r["ok"], [a])
        self.assertEqual(self.gw.calls["超长"], 2)
        self.assertIn("不合格", self.gw.requests[1]["body"]["messages"][-1]["content"])
        self.assertEqual(self.j("get", a)["hier_summary"], "超长的层级摘要")

    def test_always_overlong_fails_and_writes_nothing(self):
        a, r = self.failed("永远超长")
        self.assertEqual((r["ok"], r["failed"][0]["id"]), ([], a))
        self.assertEqual(self.j("get", a)["hier_summary"], "")
        self.assertEqual(self.gw.calls["永远超长"], 3)

    def test_empty_reply_fails_not_written_as_empty_summary(self):
        a, r = self.failed("空回复")
        self.assertEqual(r["failed"][0]["id"], a)
        self.assertEqual(self.j("get", a)["hier_summary"], "")

    def test_code_fence_json_is_accepted(self):
        a, r = self.failed("代码块")
        self.assertEqual(r["ok"], [a])

    def test_non_json_is_retried(self):
        a, r = self.failed("坏JSON")
        self.assertEqual(r["ok"], [a])

    def test_transient_500_retried(self):
        a, r = self.failed("抖动500")
        self.assertEqual(r["ok"], [a])

    def test_one_failure_does_not_block_others_and_key_never_leaks(self):
        d1, d2 = self.folder("鉴权失败"), self.folder("好的")
        self.node("鉴权失败", path=str(d1)); b = self.node("好的", path=str(d2))
        p = self.summarize()
        r = json.loads(p.stdout)
        self.assertEqual(r["ok"], [b])
        self.assertEqual(len(r["failed"]), 1)
        self.assertIn("401", r["failed"][0]["reason"])
        self.assertNotIn(FAKE_KEY, p.stdout + p.stderr)
        self.assertEqual(self.gw.calls["鉴权失败"], 1)  # 4xx 不重试

    def test_missing_mount_is_reported_not_crashing(self):
        d = self.folder("会消失")
        a = self.node("会消失", path=str(d))
        d.rmdir()
        r = self.sj()
        self.assertEqual(r["failed"], [{"id": a, "reason": "挂载路径不存在"}])

    def test_unreachable_gateway_fails_per_node(self):
        d = self.folder("断网")
        a = self.node("断网", path=str(d))
        self.write_env(OPENAI_BASE_URL="http://127.0.0.1:9/v1")
        r = self.sj()
        self.assertEqual(r["failed"][0]["id"], a)
        self.assertNotIn(FAKE_KEY, json.dumps(r))


class Config(SummarizeBase):
    def test_missing_env_file_and_empty_fields(self):
        d = self.folder("d"); self.node("d", path=str(d))
        p = self.run_cli("summarize", "--env", str(self.root / "nope.env"), "--json", expect=2)
        self.assertIn("缺少", p.stderr)
        self.write_env(OPENAI_API_KEY="")
        p = self.run_cli("summarize", "--env", str(self.env), "--json", expect=2)
        self.assertIn("OPENAI_API_KEY", p.stderr)
        self.assertNotIn(FAKE_KEY, p.stderr)

    def test_dry_run_needs_no_env_no_network_and_allowed_outside_repo(self):
        d = self.folder("干跑", [("README.md", "说明")])
        self.node("干跑", path=str(d))
        p = self.run_cli("summarize", "--dry-run", "--env", str(self.root / "nope.env"), "--json", cwd=self.root)
        r = json.loads(p.stdout)
        self.assertEqual(r["targets"], 1)
        self.assertIn("说明", r["sample_prompt"])
        self.assertEqual(self.gw.requests, [])
        check(r, response_schema("summarize"))

    def test_real_run_outside_repo_forbidden(self):
        d = self.folder("d"); self.node("d", path=str(d))
        self.run_cli("summarize", "--env", str(self.env), cwd=self.root, expect=5)

    def test_no_targets_does_not_require_env(self):
        self.run_cli("summarize", "--env", str(self.root / "nope.env"), "--json")

    def test_sop_rewrites_hier_with_content_folder_does_not(self):
        import os
        sop_dir = self.root / "reference" / "sops"
        sop_dir.mkdir(parents=True)
        sop_file = sop_dir / "routing.md"
        sop_file.write_text("第一版做法")
        lessons = self.root / "reference" / "lessons"
        lessons.mkdir()
        (lessons / "README.md").write_text("经验夹")
        sop = self.add("routing", "Reference", path=str(sop_file))
        folder = self.add("lessons", "Reference", path=str(lessons))
        self.sj()
        future = time.time() + 10
        os.utime(sop_file, (future, future))
        os.utime(lessons, (future, future))
        # 人写的层级摘要先盖上,确认内容过期时文件夹那句不动,SOP 那句会换掉
        self.run_cli("set", folder, "--hier-summary", "不要改文件夹", "--actor", "ai")
        self.run_cli("set", sop, "--hier-summary", "旧SOP摘要", "--actor", "ai")
        r = self.sj()
        self.assertEqual(r["sop_hier"], [sop])
        self.assertEqual(self.j("get", sop)["hier_summary"], "routing的层级摘要")
        self.assertEqual(self.j("get", folder)["hier_summary"], "不要改文件夹")
        self.assertNotIn(folder, r["sop_hier"])


if __name__ == "__main__":
    unittest.main()
