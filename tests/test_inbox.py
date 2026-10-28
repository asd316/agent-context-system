"""Report / Inbox: 端到端走 CLI (从仓库外的目录执行),网页的 decide 直接调函数。"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_cli import BIN

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from node_index import inbox, shared  # noqa: E402
from node_index.db import CliError  # noqa: E402

RULE = "---\nid: x\n---\nhello\nworld\n"
DIFF = "--- a/whatever.md\n+++ b/whatever.md\n@@ -4,2 +4,2 @@\n hello\n-world\n+WORLD\n"


class InboxBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.repo, self.cwd = base / "repo", base / "elsewhere"
        (self.repo / "host").mkdir(parents=True)
        (self.repo / "shared" / "rules").mkdir(parents=True)
        self.cwd.mkdir()
        (self.repo / "host" / "host.yml").write_text("host: test-host\nclients: {}\nknowledge_root: /tmp\n")
        self.rule = self.repo / "shared" / "rules" / "x.md"
        self.rule.write_text(RULE)
        self.p = shared.paths(self.repo)

    def cli(self, *args, expect=0, stdin=None, env=None):
        p = subprocess.run([sys.executable, str(BIN), *args], cwd=self.cwd, capture_output=True, text=True,
                           input=stdin, env={**os.environ, "NODE_INDEX_REPO": str(self.repo), **(env or {})})
        self.assertEqual(p.returncode, expect, f"{args}\nstdout={p.stdout}\nstderr={p.stderr}")
        return p

    def j(self, *args, **kw):
        return json.loads(self.cli(*args, "--json", **kw).stdout)

    def report(self, kind="other", *extra, **kw):
        return self.j("report", kind, "--title", "标题", "--reason", "理由", "--evidence", "证据",
                      "--client", "claude-code", "--position", "executor", *extra, **kw)

    def proposal(self, old="world", new="WORLD"):
        return self.report("rule-change", "--target", "shared/rules/x.md", "--old", old, "--new", new)

    def decisions(self):
        f = self.repo / "shared" / "decisions.jsonl"
        return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []


class Report(InboxBase):
    def test_report_from_non_repo_cwd_writes_repo_inbox(self):
        r = self.report()
        self.assertEqual((r["status"], r["from"]["host"]), ("pending", "test-host"))
        self.assertEqual(shared.load_report(self.p.report_file(r["id"])).to_dict(), r)
        self.assertEqual(list(self.cwd.iterdir()), [])

    def test_diff_from_stdin_and_shape_errors(self):
        r = self.report("rule-change", "--target", "shared/rules/x.md", "--diff", "-", stdin=DIFF)
        self.assertEqual(r["change"], {"target": "shared/rules/x.md", "diff": DIFF})
        self.cli("report", "rule-change", "--title", "t", "--reason", "r", "--evidence", "e",
                 "--client", "c", "--position", "executor", expect=2)  # rule-change 必须带 change
        self.cli("report", "other", "--title", "t", "--reason", "r", "--evidence", "e", "--client", "c",
                 "--position", "executor", "--target", "../x", "--old", "a", "--new", "b", expect=2)
        (self.repo / "host" / "host.yml").unlink()
        self.cli("report", "other", "--title", "t", "--reason", "r", "--evidence", "e", "--client", "c",
                 "--position", "executor", expect=2)
        self.assertEqual(len(list(self.p.inbox.glob("R-*.yaml"))), 1)

    def test_list_filters_and_show(self):
        a = self.report()
        b = self.proposal()
        self.cli("inbox", "claim", b["id"], "--client", "dsh")
        self.assertEqual({x["id"] for x in self.j("inbox", "list")}, {a["id"], b["id"]})
        self.assertEqual([x["id"] for x in self.j("inbox", "list", "--kind", "rule-change")], [b["id"]])
        self.assertEqual([x["id"] for x in self.j("inbox", "list", "--status", "pending")], [a["id"]])
        self.cli("inbox", "resolve", a["id"], "--status", "rejected", "--decided-by", "user",
                 "--instruction", "不需要", "--by", "pi")
        self.assertEqual([x["id"] for x in self.j("inbox", "list")], [b["id"]])
        self.assertEqual([x["id"] for x in self.j("inbox", "list", "--status", "rejected")], [a["id"]])
        text = self.cli("inbox", "show", b["id"]).stdout
        for needle in ("shared/rules/x.md", "world", "WORLD", f"inbox resolve {b['id']}", "--by dsh"):
            self.assertIn(needle, text)
        self.assertIn("不需要", self.cli("inbox", "show", a["id"]).stdout)  # 驳回理由对提交者可见
        self.cli("inbox", "show", "R-20990101-ffff", expect=3)
        self.cli("inbox", "show", "nope", expect=2)


class ClaimResolve(InboxBase):
    def test_claim_conflicts(self):
        r = self.report()
        self.assertEqual(self.j("inbox", "claim", r["id"], "--client", "dsh")["claimed_by"], "dsh")
        self.assertEqual(self.j("inbox", "claim", r["id"], "--client", "dsh")["status"], "in-progress")
        self.cli("inbox", "claim", r["id"], "--client", "pi", expect=4)
        self.cli("inbox", "resolve", r["id"], "--status", "resolved", "--decided-by", "user",
                 "--outcome", "as-proposed", "--by", "pi", expect=4)
        self.assertEqual(shared.load_report(self.p.report_file(r["id"])).claimed_by, "dsh")
        self.assertEqual(self.decisions(), [])
        self.cli("inbox", "resolve", r["id"], "--status", "resolved", "--decided-by", "user",
                 "--outcome", "as-proposed", "--by", "dsh")
        self.cli("inbox", "claim", r["id"], "--client", "dsh", expect=4)

    def test_resolve_writes_report_and_decision(self):
        r = self.proposal()
        self.cli("inbox", "claim", r["id"], "--client", "dsh")
        out = self.j("inbox", "resolve", r["id"], "--status", "resolved", "--decided-by", "user",
                     "--instruction", "改成大写,但只改第二行", "--outcome", "modified",
                     "--change", "abc123", "--by", "dsh")
        saved = shared.load_report(self.p.report_file(r["id"]))
        self.assertEqual(saved.to_dict(), out["report"])
        self.assertEqual((saved.status.value, saved.claimed_by, saved.resolution.instruction,
                          saved.resolution.changes), ("resolved", None, "改成大写,但只改第二行", ("abc123",)))
        self.assertEqual(self.decisions(), [out["decision"]])
        self.assertEqual({k: out["decision"][k] for k in ("target", "action", "result", "instruction", "by")},
                         {"target": r["id"], "action": "resolve", "result": "modified",
                          "instruction": "改成大写,但只改第二行", "by": "dsh"})
        self.cli("inbox", "resolve", r["id"], "--status", "rejected", "--decided-by", "user",
                 "--instruction", "x", "--by", "dsh", expect=4)
        self.assertEqual(len(self.decisions()), 1)

    def test_resolve_argument_rules(self):
        r = self.report()
        base = ["inbox", "resolve", r["id"], "--decided-by", "user", "--by", "dsh"]
        self.cli(*base, "--status", "resolved", expect=2)
        self.cli(*base, "--status", "rejected", "--outcome", "modified", "--instruction", "x", expect=2)
        self.cli(*base, "--status", "rejected", expect=2)
        self.assertEqual(shared.load_report(self.p.report_file(r["id"])).status.value, "pending")
        self.assertEqual(self.decisions(), [])
        d = self.j(*base, "--status", "resolved", "--outcome", "as-proposed")["decision"]
        self.assertEqual((d["result"], d["note"]), ("approved", "as-proposed; changes: 无"))

    def test_resolve_superseded(self):
        r = self.report()
        base = ["inbox", "resolve", r["id"], "--decided-by", "user", "--by", "dsh"]
        self.cli(*base, "--status", "resolved", "--outcome", "superseded", expect=2)
        self.cli(*base, "--status", "rejected", "--outcome", "superseded", expect=2)
        out = self.j(*base, "--status", "rejected", "--outcome", "superseded", "--instruction", "被 P4 取代")
        self.assertEqual((out["report"]["status"], out["report"]["resolution"]["outcome"], out["decision"]["result"]),
                         ("rejected", "superseded", "superseded"))

    def test_resolve_session(self):
        r, r2 = self.report(), self.report()
        base = ["--status", "rejected", "--decided-by", "user", "--instruction", "x", "--by", "dsh"]
        d = self.j("inbox", "resolve", r["id"], *base, "--session", "s-123")["decision"]
        self.assertEqual(d["session"], "s-123")
        d = self.j("inbox", "resolve", r2["id"], *base, env={"CLAUDE_CODE_SESSION_ID": ""})["decision"]
        self.assertNotIn("session", d)


class Decide(InboxBase):
    def state(self, rid):
        return self.rule.read_bytes(), self.p.report_file(rid).read_bytes(), self.decisions()

    def test_approve_replace_applies_change(self):
        r = self.proposal()
        report, d = inbox.decide(self.p, r["id"], "approve")
        self.assertEqual(self.rule.read_text(), RULE.replace("world", "WORLD"))
        self.assertEqual((report.status.value, report.resolution.outcome.value, report.resolution.changes),
                         ("resolved", "as-proposed", ("shared/rules/x.md",)))
        self.assertEqual((d.action, d.result.value, d.by), ("decide", "approved", "user"))
        self.assertEqual(self.decisions(), [d.to_dict()])

    def test_approve_with_conflicting_target_refuses_and_changes_nothing(self):
        r = self.proposal()
        self.rule.write_text(RULE.replace("world", "someone edited this"))
        before = self.state(r["id"])
        with self.assertRaises(CliError) as e:
            inbox.decide(self.p, r["id"], "approve")
        self.assertEqual(e.exception.kind, "conflict")
        self.assertEqual(self.state(r["id"]), before)

    def test_approve_replace_ambiguous_target_refuses(self):
        r = self.proposal(old="l")  # "hello world" 里 l 出现多次
        before = self.state(r["id"])
        with self.assertRaises(CliError) as e:
            inbox.decide(self.p, r["id"], "approve")
        self.assertEqual(e.exception.kind, "conflict")
        self.assertEqual(self.state(r["id"]), before)

    def test_approve_diff_applies_and_conflict_refuses(self):
        r = self.report("rule-change", "--target", "shared/rules/x.md", "--diff", "-", stdin=DIFF)
        inbox.decide(self.p, r["id"], "approve")
        self.assertEqual(self.rule.read_text(), RULE.replace("world", "WORLD"))

        stale = self.report("rule-change", "--target", "shared/rules/x.md", "--diff", "-", stdin=DIFF)
        before = self.state(stale["id"])
        with self.assertRaises(CliError) as e:
            inbox.decide(self.p, stale["id"], "approve")
        self.assertEqual(e.exception.kind, "conflict")
        self.assertEqual(self.state(stale["id"]), before)

    def test_reject_records_reason(self):
        r = self.proposal()
        with self.assertRaises(CliError) as e:
            inbox.decide(self.p, r["id"], "reject")
        self.assertEqual(e.exception.kind, "bad_input")
        report, d = inbox.decide(self.p, r["id"], "reject", "这条规则已经有了")
        self.assertEqual(self.rule.read_text(), RULE)
        self.assertEqual((report.status.value, report.resolution.instruction, report.resolution.outcome),
                         ("rejected", "这条规则已经有了", None))
        self.assertEqual((d.result.value, d.instruction), ("rejected", "这条规则已经有了"))
        with self.assertRaises(CliError) as e:
            inbox.decide(self.p, r["id"], "approve")
        self.assertEqual(e.exception.kind, "conflict")

    def test_copy_instruction(self):
        self.assertEqual(inbox.copy_instruction("R-20200101-a3f9", " 照办,但措辞再短一点 "),
                         "处理 Inbox R-20200101-a3f9，我的意见：照办,但措辞再短一点。按意见修改后应用，并记入 Decision log。")
        self.assertEqual(inbox.copy_instruction("R-20200101-a3f9", ""), "处理 Inbox R-20200101-a3f9。按意见修改后应用，并记入 Decision log。")


if __name__ == "__main__":
    unittest.main()
