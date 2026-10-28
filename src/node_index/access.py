"""访问记录: 读命令只把事件追加到一个“暂存文件”,不写库;由 ingest (每日流水线) 并入数据库。

为什么不写库: 读命令不应为计数去抢写锁;沙箱里写库还会静默失败。
暂存文件不需要任何锁,放在沙箱也能写的 /tmp 里。并入时才写 node_access (计数) 与 access_log (明细)。
"""
import json
import os
import sqlite3
from pathlib import Path

from .db import DEFAULT_DB, ensure_access_table, now_iso

COUNTED = ("get", "children", "ancestors")
DEFAULT_SPOOL = Path("/tmp/node-index-access.jsonl")


def db_file(conn):
    row = conn.execute("PRAGMA database_list").fetchone()
    return Path(row[2]) if row and row[2] else None


def spool_path(db):
    env = os.environ.get("NODE_INDEX_SPOOL")
    if env:
        return Path(env)
    # 默认库用固定的 /tmp 路径;指定了其他库 (测试、副本) 则跟着库走,互不串
    return DEFAULT_SPOOL if db is None or db.resolve() == DEFAULT_DB.resolve() else Path(str(db) + ".spool")


def log_event(conn, cmd, node_id=None, query=None):
    """追加一条事件。尽力而为,失败不影响读命令。不碰数据库。"""
    ev = {"ts": now_iso(), "cmd": cmd, "node_id": node_id, "query": query, "cwd": os.getcwd()}
    try:
        with open(spool_path(db_file(conn)), "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")  # 单次小写入,O_APPEND 下多进程不会交错
    except OSError:
        return False
    return True


def ingest(conn):
    """把暂存文件并入数据库。拿不到写锁 (库只读、被占用) 时原样留着,返回 0。"""
    ensure_access_table(conn)  # 老库补建表;这是写路径,只在并入时做
    sp = spool_path(db_file(conn))
    if not sp.exists():
        return 0
    tmp = sp.with_name(sp.name + f".{os.getpid()}.ing")
    try:
        conn.execute("PRAGMA busy_timeout = 200")
        conn.execute("BEGIN IMMEDIATE")  # 先拿写锁: 只读库 (沙箱) 在这里就失败,不动暂存文件
    except sqlite3.Error:
        return 0
    try:
        os.rename(sp, tmp)
    except OSError:
        conn.rollback()
        return 0
    n = 0
    try:
        for line in tmp.read_text(encoding="utf-8").splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue  # 半行或损坏行跳过
            conn.execute("INSERT INTO access_log(ts, cmd, node_id, query, cwd) VALUES (?,?,?,?,?)",
                         (ev.get("ts"), ev.get("cmd"), ev.get("node_id"), ev.get("query"), ev.get("cwd")))
            i = ev.get("node_id")
            if ev.get("cmd") in COUNTED and i is not None and conn.execute(
                    "SELECT 1 FROM nodes WHERE id = ?", (i,)).fetchone():
                conn.execute(
                    "INSERT INTO node_access(node_id, hits, last_at) VALUES (?, 1, ?) "
                    "ON CONFLICT(node_id) DO UPDATE SET hits = hits + 1, last_at = excluded.last_at",
                    (i, ev["ts"]))
            n += 1
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        with open(sp, "a", encoding="utf-8") as f:  # 失败: 把事件放回去,下次再并
            f.write(tmp.read_text(encoding="utf-8"))
        tmp.unlink()
        return 0
    tmp.unlink()
    return n
