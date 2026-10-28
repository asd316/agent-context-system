"""db.migrate_categories: 旧类别取值的库迁移后,其他表的外键仍指向 nodes。"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from node_index import db  # noqa: E402


class MigrateCategories(unittest.TestCase):
    def test_foreign_keys_still_point_to_nodes(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "index.db"
            old = db.SCHEMA.read_text(encoding="utf-8").replace(
                "'Reference','Knowledge','Project','Skill'", "'知识库','项目','Skill'")
            conn = sqlite3.connect(path)
            conn.executescript(old)
            conn.close()

            conn = db.connect(path)
            refs = conn.execute("SELECT name FROM sqlite_master WHERE sql LIKE '%nodes_old%'").fetchall()
            self.assertEqual([], [r[0] for r in refs])
            self.assertEqual([], conn.execute("PRAGMA foreign_key_check").fetchall())


if __name__ == "__main__":
    unittest.main()
