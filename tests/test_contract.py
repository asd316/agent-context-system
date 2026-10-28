"""契约测试: CLI 的 --json 输出必须符合 docs/openapi.yaml 里对应操作的响应 schema。"""
import json
import os
import unittest
from unittest import mock

import jsonschema
import yaml

from test_cli import REPO, Base

SPEC = yaml.safe_load((REPO / "docs" / "openapi.yaml").read_text(encoding="utf-8"))


def response_schema(op_id, status=None):
    for item in SPEC["paths"].values():
        for method, op in item.items():
            if isinstance(op, dict) and op.get("operationId") == op_id:
                codes = [status] if status else [c for c in op["responses"] if c.startswith("2")]
                content = op["responses"][codes[0]].get("content", {})
                return content["application/json"]["schema"]
    raise KeyError(op_id)


def implemented_cli_ops():
    """有 CLI 命令且已实现的操作。x-unimplemented 与 x-http 的约定见 openapi.yaml 的 info.description。"""
    for item in SPEC["paths"].values():
        for op in item.values():
            if isinstance(op, dict) and "operationId" in op and not op.get("x-unimplemented") and not op.get("x-http"):
                yield op


def cli_words(op):
    """命令名 (含子命令),如 "search"、"catalog set"。"""
    words = []
    for w in op["x-cli"]["command"].split()[1:]:
        if w[0] in "<[-":
            break
        words.append(w)
    return words


def check(data, schema):
    jsonschema.validate(data, {**schema, "components": SPEC["components"]})


class SpecParity(unittest.TestCase):
    def test_every_operation_has_a_cli_command(self):
        import subprocess, sys
        for op in implemented_cli_ops():
            cmd = cli_words(op)
            p = subprocess.run([sys.executable, str(REPO / "bin" / "node-index"), *cmd, "--help"],
                               capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, f"{op['operationId']} → {cmd}")

    def test_write_tag_matches_cli_guard(self):
        # 声明为 write/maintenance 的操作,在非维护目录必须被拒绝(退出码 5)
        import subprocess, sys, tempfile
        needs_args = {"add": ["--name", "x", "--category", "Skill", "--actor", "human"],
                      "set": ["n1", "--name", "x", "--actor", "human"], "rm": ["n1", "--actor", "human"],
                      "confirm": ["n1"], "link": ["n1", "n2", "--actor", "human"],
                      "unlink": ["n1", "n2", "--actor", "human"], "check": [], "ingest-access": [], "scan": [],
                      "render": ["--depth", "1"], "snapshot": [], "summarize": [], "layer": ["--plan", "x.json"],
                      "catalog set": ["x", "--tier", "core"], "category new": ["x", "--name", "X"],
                      "category move": ["x", "--from", "a", "--to", "b"], "category copy": ["x", "--to", "b"],
                      "category remove": ["x", "--from", "a"],
                      "build": [], "activate": [], "apply": ["--no-push"], "digest": []}
        with tempfile.TemporaryDirectory() as d:
            for op in implemented_cli_ops():
                if set(op.get("tags", [])) & {"write", "maintenance"}:
                    cmd = " ".join(cli_words(op))
                    p = subprocess.run([sys.executable, str(REPO / "bin" / "node-index"), *cmd.split(),
                                        *needs_args[cmd], "--db", f"{d}/x.db"],
                                       cwd=d, capture_output=True, text=True)
                    self.assertEqual(p.returncode, 5, f"{cmd}: {p.stderr}")


class Contract(Base):
    def test_outputs_conform(self):
        f = self.root / "m.md"; f.write_text("x")
        d = self.root / "kb"; (d / "子").mkdir(parents=True)
        scope = self.root / "scope.yml"
        scope.write_text(f"roots:\n  - path: {d}\n    category: Knowledge\n    depth: 1\n")

        a = self.j("add", "--name", "根", "--category", "Knowledge", "--hier-summary", "根节点", "--actor", "human")
        check(a, response_schema("createNode"))
        b = self.j("add", "--name", "调研视频", "--category", "Skill", "--path", str(f),
                   "--parent", a["id"], "--actor", "ai")
        check(b, response_schema("createNode"))
        c = self.j("add", "--name", "项目", "--category", "Project", "--actor", "human")
        check(self.j("link", c["id"], b["id"], "--actor", "human"), response_schema("linkNodes"))
        check(self.j("set", b["id"], "--content-summary", "摘要", "--actor", "ai"), response_schema("updateNode"))
        check(self.j("confirm", b["id"]), response_schema("confirmNode"))
        check(self.j("confirm", a["id"], "--subtree"), response_schema("confirmSubtree"))
        check(self.j("get", b["id"]), response_schema("getNode"))
        check(self.j("search", "调研视频"), response_schema("search"))
        check(self.j("search", "根"), response_schema("search"))
        check(self.j("children", a["id"]), response_schema("listChildren"))
        check(self.j("ancestors", b["id"]), response_schema("listAncestorPaths"))
        check(self.j("tree", "--depth", "3"), response_schema("exportTree"))
        check(self.j("check"), response_schema("checkPaths"))
        check(self.j("scan", "--dry-run", scope=scope), response_schema("scan"))
        check(self.j("scan", scope=scope), response_schema("scan"))
        check(self.j("coverage", scope=scope), response_schema("coverage"))
        check(self.j("render", "--depth", "2", "--out", str(self.root / "p.md")), response_schema("renderPromptDoc"))
        check(self.j("snapshot", "--out", str(self.root / "s.jsonl")), response_schema("writeSnapshot"))
        with mock.patch.dict(os.environ, NODE_INDEX_REPO=str(self.root / "repo")):
            check(self.j("lesson", "add", "--topic", "t", "--client", "c", "--situation", "s", "--problem", "p",
                         "--fix", "f", cwd=self.root), response_schema("addLesson"))

        # P3 S4: report / inbox 写到临时仓库,不碰本仓库的 shared/
        repo = self.root / "repo"; (repo / "host").mkdir(parents=True)
        (repo / "host" / "host.yml").write_text("host: h\nclients: {}\nknowledge_root: /tmp\n")
        with mock.patch.dict(os.environ, {"NODE_INDEX_REPO": str(repo)}):
            r = self.j("report", "other", "--title", "t", "--reason", "r", "--evidence", "e",
                       "--client", "dsh", "--position", "executor", "--target", "a.md", "--old", "x", "--new", "y")
            check(r, response_schema("createReport"))
            check(self.j("inbox", "list"), response_schema("listInbox"))
            check(self.j("inbox", "show", r["id"]), response_schema("showReport"))
            check(self.j("inbox", "claim", r["id"], "--client", "dsh"), response_schema("claimReport"))
            check(self.j("inbox", "resolve", r["id"], "--status", "rejected", "--decided-by", "user",
                         "--instruction", "不要", "--by", "dsh"), response_schema("resolveReport"))

    def test_catalog_outputs_conform(self):
        import os
        from unittest import mock
        repo = self.root / "repo"
        (repo / "shared").mkdir(parents=True)
        for name in ("catalog.yaml", "categories.yaml"):
            (repo / "shared" / name).write_text("{}\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"NODE_INDEX_REPO": str(repo)}):
            check(self.j("catalog", "set", "foo", "--entry", "referenced", "--source", "~/foo", "--tier", "leaf"),
                  response_schema("setCatalogEntry"))
            check(self.j("catalog", "list"), response_schema("listCatalog"))
            check(self.j("category", "new", "design", "--name", "设计"), response_schema("createCategory"))
            check(self.j("category", "new", "design/motion", "--name", "动效"), response_schema("createCategory"))
            check(self.j("category", "copy", "foo", "--to", "design"), response_schema("copyMember"))
            check(self.j("category", "move", "foo", "--from", "design", "--to", "design/motion"),
                  response_schema("moveMember"))
            check(self.j("category", "remove", "foo", "--from", "design/motion"), response_schema("removeMember"))
            (repo / "shared" / "skills").mkdir()
            scope = self.root / "scope.yml"
            scope.write_text("roots:\n  - path: shared/skills\n    category: Skill\n    depth: 1\n")
            r = self.j("scan", scope=scope)
            self.assertIn("categories", r)
            check(r, response_schema("scan"))

        from test_rules_build import Fixture
        fx = Fixture(self.root / "p3")
        check(fx.cli("build"), response_schema("build"))
        check(fx.cli("activate"), response_schema("activate"))
        check(fx.cli("apply"), response_schema("apply"))
        status = json.loads((fx.repo / "host" / "apply-state.json").read_text(encoding="utf-8"))
        check(status, response_schema("httpApplyStatus"))

    def test_errors_conform_and_exit_codes(self):
        err_schema = SPEC["components"]["schemas"]["Error"]
        for args, code, kind in ((["get", "n9"], 3, "not_found"), (["get", "x"], 2, "bad_input"),
                                 (["rm", "n9", "--actor", "ai"], 5, "forbidden")):
            p = self.run_cli(*args, "--json", expect=code)
            e = json.loads(p.stderr)
            check(e, err_schema)
            self.assertEqual(e["error"], kind)
        a = self.add("a"); b = self.add("b", parents=[a])
        p = self.run_cli("link", b, a, "--actor", "human", "--json", expect=4)
        self.assertEqual(json.loads(p.stderr)["error"], "conflict")


if __name__ == "__main__":
    unittest.main()
