"""配置网页的服务 (ui/console/server.py): /api/graph 的缓存,以及 Organize / Profiles / Inbox / 应用 的 HTTP 接口。
写接口全部落在临时仓库 (test_rules_build.Fixture) 与临时 Client 目录里。"""
import gzip
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from test_cli import REPO, Base  # noqa: E402
from test_contract import SPEC, check, response_schema  # noqa: E402
from test_rules_build import Fixture, git  # noqa: E402

from node_index import db, inbox, shared  # noqa: E402

spec = importlib.util.spec_from_file_location("console_server", REPO / "ui" / "console" / "server.py")
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


def serve(test, db_path, repo=None):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(db_path, repo))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    test.addCleanup(httpd.server_close)
    test.addCleanup(httpd.shutdown)
    return f"http://127.0.0.1:{httpd.server_port}"


def request(url, method="GET", body=None, **headers):
    data = json.dumps(body).encode() if body is not None else None
    if data:
        headers["Content-Type"] = "application/json"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data, method=method, headers=headers)) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


class GraphEndpoint(Base):
    """/api/graph: 库没变时用缓存并回 304,支持 gzip,任何写入后都返回新数据。"""

    def setUp(self):
        super().setUp()
        self.add("甲")
        self.url = serve(self, self.db) + "/api/graph"

    def get(self, **headers):
        return request(self.url, **headers)

    def names(self, body):
        return [n["name"] for n in json.loads(body)["nodes"]]

    def test_etag_revalidates_with_304(self):
        status, h, body = self.get()
        self.assertEqual(status, 200)
        self.assertEqual(h["Cache-Control"], "no-cache")
        self.assertEqual(self.names(body), ["甲"])
        status, _, body = self.get(**{"If-None-Match": h["ETag"]})
        self.assertEqual((status, body), (304, b""))

    def test_gzip_when_accepted(self):
        status, h, body = self.get(**{"Accept-Encoding": "gzip, deflate"})
        self.assertEqual((status, h["Content-Encoding"]), (200, "gzip"))
        _, _, plain = self.get()
        self.assertEqual(gzip.decompress(body), plain)

    def test_write_invalidates_cache(self):
        _, h, _ = self.get()
        self.add("乙")
        status, h2, body = self.get(**{"If-None-Match": h["ETag"]})
        self.assertEqual(status, 200)
        self.assertNotEqual(h2["ETag"], h["ETag"])
        self.assertEqual(self.names(body), ["甲", "乙"])


class Console(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.f = Fixture(Path(tmp.name).resolve())
        db.connect(self.f.db).close()
        self.p = shared.paths(self.f.repo)
        self.base = serve(self, self.f.db, self.f.repo)

    def call(self, method, path, body=None, expect=200, op=None):
        status, _, raw = request(self.base + path, method, body)
        data = json.loads(raw)
        self.assertEqual(status, expect, data)
        if op:
            check(data, response_schema(op) if expect < 300 else SPEC["components"]["schemas"]["Error"])
        return data

    def categories_text(self):
        return self.p.categories.read_text(encoding="utf-8")

    def add_remote(self):
        remote = self.f.root / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
        git(self.f.repo, "remote", "add", "origin", str(remote))
        git(self.f.repo, "push", "-q", "-u", "origin", "main")
        return remote

    def apply_and_wait(self):
        self.call("POST", "/api/apply", expect=202, op="httpApply")
        for _ in range(200):
            s = self.call("GET", "/api/apply/status", op="httpApplyStatus")
            if s["state"] != "running":
                return s
            time.sleep(0.05)
        self.fail("apply 没有结束")

    def test_organize_view(self):
        o = self.call("GET", "/api/organize", op="httpOrganize")
        self.assertEqual([c["id"] for c in o["categories"]], ["top", "top/sub"])
        self.assertEqual(o["categories"][0]["member_details"],
                         [{"ref": "core-a", "kind": "skill", "tier": "core", "trait": ""}])
        # Router 与已归类的不算未分类;retired 的不列
        self.assertEqual(o["uncategorized"], ["ext-g", "manual-b", "missing-e", "native-c"])
        self.assertTrue(o["entries"]["manual-b"]["manual_only"])
        self.assertEqual([(r["member"], r["category"]) for r in o["recent"]], [("core-a", "top"), ("leaf-f", "top/sub"), ("retired-d", "top/sub")])

    def test_lessons_create_list_update(self):
        body = {"topic": "旧主题", "client": "codex", "situation": "情境文字", "problem": "问题文字", "fix": "做法文字"}
        a = self.call("POST", "/api/lessons", body, expect=201, op="httpLessonCreate")
        self.assertEqual((a["situation"], a["fix"], a["status"]), ("情境文字", "做法文字", "active"))
        lst = self.call("GET", "/api/lessons", op="httpLessons")
        self.assertEqual([x["id"] for x in lst], [a["id"]])
        u = self.call("PATCH", f"/api/lessons/{a['id']}", {"topic": "新主题", "fix": "新做法"}, op="httpLessonUpdate")
        self.assertEqual((u["topic"], u["fix"], u["problem"], u["date"]), ("新主题", "新做法", "问题文字", a["date"]))
        self.assertEqual([f.name for f in self.p.lessons.glob("L-*.md")], [f"{a['id']}-新主题.md"])
        self.assertIn("lesson", self.call("GET", "/api/changes")["files"][0]["path"])
        # 不合法的改动被拒,文件保持原样
        before = (self.p.lessons / f"{a['id']}-新主题.md").read_text(encoding="utf-8")
        self.call("PATCH", f"/api/lessons/{a['id']}", {"status": "merged"}, expect=400, op="httpLessonUpdate")
        self.call("PATCH", f"/api/lessons/{a['id']}", {"fix": "  "}, expect=400)
        self.call("PATCH", f"/api/lessons/{a['id']}", {"id": "x"}, expect=400)
        self.assertEqual((self.p.lessons / f"{a['id']}-新主题.md").read_text(encoding="utf-8"), before)
        o = self.call("PATCH", f"/api/lessons/{a['id']}", {"status": "obsolete"})
        self.assertEqual(o["status"], "obsolete")
        self.call("PATCH", "/api/lessons/L-20991231-ffff", {"fix": "x"}, expect=404)

    def test_recent_ignores_apply_commits(self):
        git(self.f.repo, "commit", "-q", "--amend", "-m", "apply: test-host 2026-10-23T10:00:00")
        self.assertEqual(self.call("GET", "/api/organize")["recent"], [])

    def test_write_changes_shared_and_discard_restores(self):
        before = self.categories_text()
        self.assertEqual(self.call("GET", "/api/changes", op="httpChanges"),
                         {"base_commit": None, "files": [], "clients": []})
        m = self.call("POST", "/api/membership", {"op": "copy", "member": "leaf-f", "to": "top/sub"},
                      op="httpMembership")
        self.assertEqual(m, {"member": "leaf-f", "categories": ["top/sub"]})
        m = self.call("POST", "/api/membership", {"op": "move", "member": "leaf-f", "from": "top/sub", "to": "top"},
                      op="httpMembership")
        self.assertEqual(m["categories"], ["top"])
        self.call("POST", "/api/category", {"id": "top/new", "name": "新类"}, expect=201, op="httpCreateCategory")
        self.call("POST", "/api/category", {"id": "top/new", "name": "重名"}, expect=409, op="httpCreateCategory")
        e = self.call("PATCH", "/api/catalog/leaf-f", {"tier": "core", "manual_only": True}, op="httpPatchCatalog")
        self.assertEqual((e["tier"], e["manual_only"]), ("core", True))
        inbox.create(self.p, "other", "t", "r", "e", "dsh", "executor")
        ch = self.call("GET", "/api/changes", op="httpChanges")
        files = {f["path"]: f["status"] for f in ch["files"]}
        self.assertEqual(files.pop("shared/categories.yaml"), "modified")
        self.assertEqual(files.pop("shared/catalog.yaml"), "modified")
        (report, status), = files.items()
        self.assertTrue(report.startswith("shared/inbox/R-") and status == "added")

        d = self.call("POST", "/api/changes/discard", op="httpDiscardChanges")
        self.assertEqual(len(d["discarded"]), 3)
        self.assertEqual(self.categories_text(), before)
        self.assertFalse((self.f.repo / report).exists())
        self.assertEqual(self.call("GET", "/api/changes")["files"], [])
        self.assertEqual(git(self.f.repo, "status", "--porcelain", "--", "shared"), "")

    def test_delete_category_only_when_empty(self):
        self.call("DELETE", "/api/category/top", expect=409, op="httpDeleteCategory")
        self.call("DELETE", "/api/category/top/sub", expect=409, op="httpDeleteCategory")
        self.call("POST", "/api/category", {"id": "top/empty", "name": "空"}, expect=201)
        self.call("DELETE", "/api/category/top/empty", op="httpDeleteCategory")
        self.assertNotIn("top/empty", self.categories_text())

    def test_bad_input_maps_to_error_shape(self):
        self.call("POST", "/api/membership", {"op": "move", "member": "leaf-f", "from": "top", "to": "nope"},
                  expect=404, op="httpMembership")
        self.call("PATCH", "/api/catalog/leaf-f", {"status": "retired"}, expect=400, op="httpPatchCatalog")
        self.call("GET", "/api/nope", expect=404)

    def test_profiles_view(self):
        r = self.call("GET", "/api/profiles", op="httpProfiles")
        clients = {c["client"]: c for c in r["clients"]}
        self.assertEqual({n: c["installed"] for n, c in clients.items()},
                         {"claude-code": True, "dsh": True, "pi": False})
        orch, exe = r["profiles"]
        self.assertEqual(orch["profile"], "orchestrator")
        self.assertIn("disp", [x["id"] for x in orch["rules"]])
        self.assertNotIn("disp", [x["id"] for x in exe["rules"]])
        self.assertEqual([s["name"] for s in orch["skills"]],
                         ["core-a", "ext-g", "manual-b", "missing-e", "native-c", "router-top"])
        self.assertTrue(orch["rules_bytes"] > exe["rules_bytes"] > 0)
        # 估算与 build 写出的文件大小一致
        built = {x["profile"]: x["bytes"] for x in self.f.cli("build")["profiles"]}
        self.assertEqual(built, {"orchestrator": orch["rules_bytes"], "executor": exe["rules_bytes"]})

    def test_apply_in_background_then_profile_switch_and_discard(self):
        remote = self.add_remote()
        self.call("POST", "/api/membership", {"op": "copy", "member": "leaf-f", "to": "top"})
        s = self.apply_and_wait()
        self.assertEqual((s["state"], s["pushed"]), ("done", True), s)
        self.assertEqual([x["step"] for x in s["steps"]], ["build", "activate", "commit", "push"])
        self.assertEqual(git(remote, "rev-parse", "--short", "main"), s["commit"])
        self.assertEqual(self.call("GET", "/api/changes"), {"base_commit": s["head"], "files": [], "clients": []})

        host_before = self.p.host.read_text(encoding="utf-8")
        self.call("PATCH", "/api/clients/claude-code", {"profile": "executor"}, op="httpPatchClient")
        self.assertEqual(shared.load_host(self.p).clients["claude-code"].profile.value, "executor")
        ch = self.call("GET", "/api/changes")
        self.assertEqual(ch["clients"], [{"client": "claude-code", "profile": "executor",
                                          "active_profile": "orchestrator"}])
        self.call("POST", "/api/changes/discard")
        self.assertEqual(self.p.host.read_text(encoding="utf-8"), host_before)

        self.call("PATCH", "/api/clients/claude-code", {"profile": "executor"})
        s = self.apply_and_wait()
        self.assertEqual(s["state"], "done")
        rules = (self.f.clients["claude-code"] / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertIn("profile=executor", rules.splitlines()[0])
        self.assertEqual(self.call("GET", "/api/changes")["clients"], [])

    def test_failed_push_is_reported_not_failed(self):
        s = self.apply_and_wait()  # 没有 remote
        self.assertEqual((s["state"], s["pushed"]), ("done", False))
        self.assertTrue(s["unpushed_reason"])

    def test_writes_rejected_while_applying(self):
        state = {"state": "running", "steps": [], "commit": None, "pushed": False, "at": None,
                 "unpushed_reason": None, "head": None, "pid": os.getpid()}
        (self.f.repo / "host" / "apply-state.json").write_text(json.dumps(state), encoding="utf-8")
        before = self.categories_text()
        self.call("POST", "/api/membership", {"op": "copy", "member": "leaf-f", "to": "top"}, expect=409)
        self.call("POST", "/api/apply", expect=409, op="httpApply")
        self.assertEqual(self.categories_text(), before)
        self.call("GET", "/api/organize")  # 读接口不受影响

    def test_inbox_preview_instruction_and_decide(self):
        old = "top/sub: {name: 子类, router: router-sub, members: [leaf-f, retired-d]}"
        new = "top/sub: {name: 子类, router: router-sub, members: [leaf-f, retired-d, ext-g]}"
        r = inbox.create(self.p, "index-issue", "归类 ext-g", "理由", "证据", "dsh", "executor",
                         target="shared/categories.yaml", old=old, new=new)
        plain = inbox.create(self.p, "other", "只是报告", "r", "e", "dsh", "executor")
        lst = self.call("GET", "/api/inbox", op="httpListInbox")
        self.assertEqual({x["id"]: x["proposal"] for x in lst}, {r.id: True, plain.id: False})
        self.assertEqual(self.call("GET", f"/api/inbox/{r.id}", op="httpShowReport")["title"], "归类 ext-g")

        pv = self.call("GET", f"/api/inbox/{r.id}/preview", op="httpPreviewReport")
        self.assertIsNone(pv["conflict"])
        sub = lambda t: next(c for c in t if c["id"] == "top/sub")["members"]  # noqa: E731
        self.assertEqual(sub(pv["trees"]["after"]), sub(pv["trees"]["before"]) + ["ext-g"])
        self.assertIn(old, self.categories_text())  # 预览不改源文件

        ins = self.call("POST", f"/api/inbox/{r.id}/instruction", {"text": "照做"}, op="httpCopyInstruction")
        self.assertEqual(ins["instruction"], inbox.copy_instruction(r.id, "照做"))

        self.call("POST", f"/api/inbox/{plain.id}/decide", {"decision": "reject"}, expect=400, op="httpDecideReport")
        d = self.call("POST", f"/api/inbox/{plain.id}/decide", {"decision": "reject", "instruction": "不需要"},
                      op="httpDecideReport")
        self.assertEqual(d["decision"]["result"], "rejected")
        d = self.call("POST", f"/api/inbox/{r.id}/decide", {"decision": "approve"}, op="httpDecideReport")
        self.assertEqual(d["report"]["resolution"]["changes"], ["shared/categories.yaml"])
        self.assertIn(new, self.categories_text())
        self.assertEqual([x["target"] for x in self.call("GET", "/api/decisions", op="httpDecisions")],
                         [r.id, plain.id])
        self.call("POST", f"/api/inbox/{r.id}/decide", {"decision": "approve"}, expect=409, op="httpDecideReport")


class HttpParity(unittest.TestCase):
    def test_every_implemented_http_operation_is_routed(self):
        api = server.Api(":memory:", None)
        routes = [(m, rx) for m, rx, _, _ in api.routes]
        for path, item in SPEC["paths"].items():
            for method, op in item.items():
                if isinstance(op, dict) and op.get("x-http") and path != "/api/graph":
                    sample = path.replace("{reportId}", "R-20200101-abcd").replace("{name}", "x").replace("{id}", "a/b").replace("{lessonId}", "L-20200101-abcd")
                    routed = any(m == method.upper() and rx.match(sample) for m, rx in routes)
                    self.assertEqual(routed, not op.get("x-unimplemented"), f"{method} {path}")


if __name__ == "__main__":
    unittest.main()
