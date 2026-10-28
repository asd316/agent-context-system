import hashlib
import json
import os
import sqlite3
import stat
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_cli import Base  # noqa: E402


class AccessSpool(Base):
    """读命令只追加暂存文件、不写库; 事件在 ingest-access (每日流水线) 时并入数据库。"""

    def spool(self):
        return Path(self.db + ".spool")

    def rows(self, sql):
        c = sqlite3.connect(self.db)
        try:
            return c.execute(sql).fetchall()
        finally:
            c.close()

    def digest(self):
        return hashlib.sha256(Path(self.db).read_bytes()).hexdigest()

    def test_reads_never_write_the_database_file(self):
        a = self.add("甲")
        before = self.digest()
        self.j("get", a); self.j("search", "甲"); self.j("children", a); self.j("ancestors", a)
        self.assertEqual(self.digest(), before)  # 库文件字节不变,读命令不抢写锁
        self.assertEqual(len(self.spool().read_text(encoding="utf-8").splitlines()), 4)

    def test_counts_appear_only_after_ingest(self):
        a = self.add("甲")
        self.j("get", a)
        self.assertEqual(self.rows("SELECT count(*) FROM node_access")[0][0], 0)
        self.assertEqual(self.j("ingest-access")["ingested"], 1)
        self.assertFalse(self.spool().exists())
        self.assertEqual(self.rows("SELECT hits FROM node_access")[0][0], 1)

    def test_readonly_db_keeps_events_until_writable_run(self):
        a = self.add("甲")
        os.chmod(self.db, stat.S_IRUSR)
        try:
            self.run_cli("get", a)
            self.run_cli("search", "甲")
            self.assertEqual(self.j("ingest-access")["ingested"], 0)  # 库只读: 并入失败,事件留着
        finally:
            os.chmod(self.db, stat.S_IRUSR | stat.S_IWUSR)
        self.assertEqual(len(self.spool().read_text(encoding="utf-8").splitlines()), 2)
        self.assertEqual(self.j("ingest-access")["ingested"], 2)  # 能写库了: 并入
        self.assertFalse(self.spool().exists())
        self.assertEqual(self.rows("SELECT hits FROM node_access")[0][0], 1)  # get 计数;search 不计数
        self.assertEqual([r[0] for r in self.rows("SELECT cmd FROM access_log ORDER BY id")],
                         ["get", "search"])

    def test_search_query_and_cwd_logged_but_not_counted(self):
        a = self.add("检索词丰富的节点")
        self.j("search", "检索词丰富")
        self.j("ingest-access")
        row = self.rows("SELECT cmd, node_id, query, cwd FROM access_log")[0]
        self.assertEqual(row[:3], ("search", None, "检索词丰富"))
        self.assertTrue(row[3])
        self.assertEqual(self.rows("SELECT count(*) FROM node_access")[0][0], 0)
        self.assertEqual(self.j("get", a, "--no-count")["hits"], 0)

    def test_deleted_node_keeps_log_but_not_counter(self):
        a = self.add("甲"); self.j("get", a)
        nid = int(a[1:])
        self.run_cli("rm", a, "--actor", "human")
        self.assertEqual(self.j("ingest-access")["ingested"], 1)  # 节点已删,并入时只留明细
        self.assertEqual(self.rows("SELECT count(*) FROM node_access")[0][0], 0)
        self.assertEqual(self.rows(f"SELECT count(*) FROM access_log WHERE node_id={nid}")[0][0], 1)

    def test_corrupt_spool_line_skipped(self):
        a = self.add("甲")
        self.spool().write_text("{坏行\n" + json.dumps({"ts": "2026-01-01T00:00:00", "cmd": "get", "node_id": int(a[1:])}) + "\n",
                                encoding="utf-8")
        self.assertEqual(self.j("ingest-access")["ingested"], 1)  # 坏行跳过
        self.assertEqual(self.j("get", a, "--no-count")["hits"], 1)


if __name__ == "__main__":
    unittest.main()
