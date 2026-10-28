import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCHEMA = REPO / "src" / "schema.sql"
DEFAULT_DB = Path("~/.local/share/node-index/index.db").expanduser()
DEFAULT_SCOPE = REPO / "config" / "scope.yml"
DEFAULT_PROMPT_CONFIG = REPO / "config" / "prompt.yml"
DEFAULT_PROMPT_DOC = Path("~/.local/share/node-index/prompt.md").expanduser()

CATEGORIES = ("Reference", "Knowledge", "Project", "Skill")
ACTORS = ("human", "ai")


class CliError(Exception):
    EXIT = {"bad_input": 2, "not_found": 3, "conflict": 4, "forbidden": 5, "failed": 6}

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind
        self.message = message

    @property
    def code(self):
        return self.EXIT[self.kind]


def connect(db_path=None):
    path = Path(db_path or os.environ.get("NODE_INDEX_DB") or DEFAULT_DB).expanduser()
    fresh = not path.exists()
    if fresh:
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if fresh:
        conn.executescript(SCHEMA.read_text(encoding="utf-8"))  # 读命令也可能首次建库,这是唯一的读路径写入
    else:
        migrate_categories(conn)
    return conn


def _map_category(category, path, ref_root):
    if category == "项目":
        return "Project"
    if category == "知识库":
        if path == ref_root or (path or "").startswith(ref_root + os.sep):
            return "Reference"
        return "Knowledge"
    return category


def migrate_categories(conn):
    """旧库的类别取值是知识库/项目/Skill。改成 Layer 的英文名,并重建 CHECK。已迁移的库直接返回。"""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='nodes'").fetchone()
    if row is None or "知识库" not in row[0]:
        return False
    ref_root = str((REPO / "shared" / "reference").resolve())
    ddl = SCHEMA.read_text(encoding="utf-8")
    nodes_ddl = ddl[ddl.index("CREATE TABLE nodes ("):ddl.index("CREATE TABLE edges")]
    fts_ddl = ddl[ddl.index("CREATE VIRTUAL TABLE nodes_fts"):ddl.index("-- 访问计数")]
    cols = [r[1] for r in conn.execute("PRAGMA table_info(nodes)")]
    conn.execute("PRAGMA foreign_keys=OFF")
    # 否则 RENAME 会把 edges 等表的外键改指 nodes_old, DROP 之后外键悬空
    conn.execute("PRAGMA legacy_alter_table=ON")
    try:
        conn.execute("BEGIN")
        for name in ("nodes_ai", "nodes_ad", "nodes_au"):
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        conn.execute("DROP TABLE IF EXISTS nodes_fts")
        conn.execute("ALTER TABLE nodes RENAME TO nodes_old")
        conn.execute(nodes_ddl)
        placeholders = ",".join("?" for _ in cols)
        insert = f"INSERT INTO nodes ({','.join(cols)}) VALUES ({placeholders})"
        cat_at = cols.index("category")
        path_at = cols.index("path")
        rows = []
        for old in conn.execute("SELECT * FROM nodes_old"):
            rec = [old[c] for c in cols]
            rec[cat_at] = _map_category(rec[cat_at], rec[path_at], ref_root)
            rows.append(rec)
        conn.executemany(insert, rows)
        conn.execute("DROP TABLE nodes_old")
        conn.execute("DELETE FROM sqlite_sequence WHERE name='nodes_old'")
        buf = ""
        for line in fts_ddl.splitlines(keepends=True):
            buf += line
            if sqlite3.complete_statement(buf):
                conn.execute(buf)
                buf = ""
        if buf.strip():
            raise sqlite3.OperationalError("FTS 语句未结束")
        conn.execute("INSERT INTO nodes_fts(nodes_fts) VALUES('rebuild')")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA legacy_alter_table=OFF")
        conn.execute("PRAGMA foreign_keys=ON")
    return True


ACCESS_DDL = (
    """CREATE TABLE IF NOT EXISTS node_access (
  node_id INTEGER PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
  hits INTEGER NOT NULL DEFAULT 0 CHECK (hits >= 0),
  last_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS access_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  cmd TEXT NOT NULL,
  node_id INTEGER,
  query TEXT,
  cwd TEXT)""",
)


def ensure_access_table(conn):
    """老库补建访问计数表 (仅由并入流程调用)。库只读或被锁时静默跳过。"""
    try:
        have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"node_access", "access_log"} <= have:
            for ddl in ACCESS_DDL:
                conn.execute(ddl)
            conn.commit()
    except sqlite3.Error:
        pass


def now_iso():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


LEAD_DOCS = ("SKILL.md", "README.md", "README", "AGENTS.md", "CLAUDE.md")  # 文件夹挂载物的说明文档,按此顺序取材


def mount_mtime_iso(path):
    """挂载物的"有效修改时间"。文件夹取自身与其说明文档中最晚的一个:
    改动文件夹里的文件不会改变文件夹自身的修改时间,只看后者会漏掉内容更新(如 skill 被 git pull 更新)。"""
    ts = os.stat(path).st_mtime
    if os.path.isdir(path):
        for name in LEAD_DOCS:
            try:
                ts = max(ts, os.stat(os.path.join(path, name)).st_mtime)
            except OSError:
                pass
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%S")


def mtime_iso(path):
    return datetime.fromtimestamp(os.stat(path).st_mtime).strftime("%Y-%m-%dT%H:%M:%S")


def fmt_id(i):
    return f"n{i}"


def parse_id(text):
    m = re.fullmatch(r"n(\d+)", str(text))
    if not m:
        raise CliError("bad_input", f"节点编号应形如 n12,收到 {text!r}")
    return int(m.group(1))


def normalize_path(p):
    return os.path.abspath(os.path.expanduser(p))
