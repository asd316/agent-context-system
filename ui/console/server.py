#!/usr/bin/env python3
"""配置网页的服务: 托管前端构建产物 web/dist,提供 /api/... (契约见 docs/openapi.yaml 的 x-http 部分)。

只监听 127.0.0.1。读接口只读打开 Index;写接口调用与 CLI 相同的函数 (node_index.catalog / inbox / activate),
改动写进 shared/ 或 host/host.yml,经「应用」(apply) 生效。apply 期间拒绝其余写接口 (409)。

用法:
  python3 ui/console/server.py              # http://127.0.0.1:5180
  python3 ui/console/server.py --port 5181 --db 某.db --repo 某仓库
"""
import argparse
import contextlib
import errno
import gzip
import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))

from node_index import activate, catalog, console, db, inbox, lessons, shared  # noqa: E402
from node_index.db import CliError  # noqa: E402

DIST = HERE / "web" / "dist"
DEFAULT_DB = db.DEFAULT_DB
HTTP_STATUS = {"bad_input": 400, "not_found": 404, "conflict": 409, "forbidden": 403, "failed": 500}
def load_graph(db_path):
    # mode=ro: 页面永远不写库
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        cols = ("id", "name", "category", "hier_summary", "content_summary", "path", "path_kind",
                "status", "modified_at", "pending", "path_state")
        sel = ", ".join(f"n.{c}" for c in cols)
        try:  # 访问计数表由 CLI 创建;老库可能还没有,此时全部按 0 处理
            rows = conn.execute(f"SELECT {sel}, COALESCE(a.hits, 0) AS hits, a.last_at AS last_at "
                                "FROM nodes n LEFT JOIN node_access a ON a.node_id = n.id ORDER BY n.id").fetchall()
        except sqlite3.OperationalError:
            rows = [dict(r, hits=0, last_at=None) for r in conn.execute(f"SELECT {sel} FROM nodes n ORDER BY n.id")]
        nodes = [{k: r[k] for k in (*cols, "hits", "last_at")} for r in rows]
        edges = [{"parent": r[0], "child": r[1], "origin": r[2]}
                 for r in conn.execute("SELECT parent_id, child_id, origin FROM edges")]
    finally:
        conn.close()
    for n in nodes:
        n["pending"] = bool(n["pending"])
        n["missing"] = n.pop("path_state") == "missing"
    return {"nodes": nodes, "edges": edges, "db_mtime": int(os.stat(db_path).st_mtime)}


def db_fingerprint(db_path):
    """库文件(及 WAL/日志文件)的 mtime 与大小。任何写入都会改变它"""
    parts = []
    for suffix in ("", "-wal", "-journal"):
        try:
            st = os.stat(f"{db_path}{suffix}")
        except FileNotFoundError:
            continue
        parts.append(f"{suffix}:{st.st_mtime_ns}:{st.st_size}")
    return "|".join(parts)


class GraphCache:
    """按库指纹缓存 /api/graph 的序列化结果(原文与 gzip 各一份)。库没变时不查库、不序列化"""

    def __init__(self, db_path):
        self.db_path = db_path
        self.lock = threading.Lock()
        self.entry = None  # (fingerprint, etag, raw, gz)

    def get(self):
        fp = db_fingerprint(self.db_path)
        with self.lock:
            if self.entry and self.entry[0] == fp:
                return self.entry[1:]
            raw = json.dumps(load_graph(self.db_path), ensure_ascii=False).encode()
            # 查库期间若有写入,指纹按查询前的算: 下次请求会看到新指纹并重建,不会把新数据当旧的缓存住
            etag = '"%s"' % hashlib.sha1(raw).hexdigest()[:20]
            self.entry = (fp, etag, raw, gzip.compress(raw, compresslevel=6))
            return self.entry[1:]


class Api:
    """路由表: (方法, 路径正则) → 处理函数。处理函数收 (match, query, body),返回 (状态码, 数据)。"""

    def __init__(self, db_path, repo):
        self.db_path = db_path
        self.p = shared.paths(repo)
        self.apply_thread = None
        self.lock = threading.Lock()
        R = self.routes = []

        def route(method, pattern, write=False):
            def deco(fn):
                R.append((method, re.compile(f"^{pattern}$"), fn, write))
                return fn
            return deco

        p = self.p

        @route("GET", "/api/organize")
        def _(m, q, b):
            with self.read_conn() as conn:
                return 200, console.organize(p, conn)

        @route("POST", "/api/category", write=True)
        def _(m, q, b):
            return 201, catalog.create_category(p, b.get("id", ""), b.get("name", ""), b.get("router"))

        @route("DELETE", "/api/category/(?P<id>.+)", write=True)
        def _(m, q, b):
            return 200, catalog.delete_category(p, m["id"])

        @route("POST", "/api/membership", write=True)
        def _(m, q, b):
            op, member = b.get("op"), b.get("member", "")
            if op == "move":
                return 200, catalog.move_member(p, member, b.get("from", ""), b.get("to", ""))
            if op == "copy":
                return 200, catalog.copy_member(p, member, b.get("to", ""))
            if op == "remove":
                return 200, catalog.remove_member(p, member, b.get("from", ""))
            raise CliError("bad_input", f"op 只能是 move、copy、remove: {op!r}")

        @route("PATCH", "/api/catalog/(?P<name>[^/]+)", write=True)
        def _(m, q, b):
            extra = set(b) - {"tier", "manual_only"}
            if extra:
                raise CliError("bad_input", f"只能改 tier 与 manual_only,收到 {sorted(extra)}")
            return 200, catalog.set_entry(p, m["name"], tier=b.get("tier"), manual_only=b.get("manual_only"))

        @route("GET", "/api/profiles")
        def _(m, q, b):
            with self.read_conn() as conn:
                return 200, console.profiles(p, conn)

        @route("PATCH", "/api/clients/(?P<name>[^/]+)", write=True)
        def _(m, q, b):
            return 200, console.set_client_profile(p, m["name"], b.get("profile"))

        @route("GET", "/api/inbox")
        def _(m, q, b):
            return 200, console.inbox_list(p, q.get("status", []), (q.get("kind") or [None])[0])

        @route("GET", "/api/inbox/(?P<id>[^/]+)")
        def _(m, q, b):
            return 200, inbox.load(p, m["id"]).to_dict()

        @route("GET", "/api/inbox/(?P<id>[^/]+)/preview")
        def _(m, q, b):
            return 200, console.report_preview(p, m["id"])

        @route("POST", "/api/inbox/(?P<id>[^/]+)/instruction")
        def _(m, q, b):
            inbox.load(p, m["id"])
            return 200, {"instruction": inbox.copy_instruction(m["id"], b.get("text"))}

        @route("POST", "/api/inbox/(?P<id>[^/]+)/decide", write=True)
        def _(m, q, b):
            r, d = inbox.decide(p, m["id"], b.get("decision"), b.get("instruction"))
            return 200, {"report": r.to_dict(), "decision": d.to_dict()}

        @route("GET", "/api/lessons")
        def _(m, q, b):
            return 200, lessons.list_all(p.root)

        @route("POST", "/api/lessons", write=True)
        def _(m, q, b):
            extra = set(b) - {"topic", "client", "situation", "problem", "fix", "host_only"}
            if extra:
                raise CliError("bad_input", f"不认识的字段 {sorted(extra)}")
            r = lessons.add(b.get("topic", ""), b.get("client", ""), b.get("situation", ""), b.get("problem", ""),
                            b.get("fix", ""), bool(b.get("host_only")), p.root)
            return 201, next(x for x in lessons.list_all(p.root) if x["id"] == r["id"])

        @route("PATCH", "/api/lessons/(?P<id>[^/]+)", write=True)
        def _(m, q, b):
            return 200, lessons.update(m["id"], b, p.root)

        @route("GET", "/api/decisions")
        def _(m, q, b):
            return 200, console.decisions(p)

        @route("GET", "/api/changes")
        def _(m, q, b):
            return 200, console.changes(p)

        @route("POST", "/api/changes/discard", write=True)
        def _(m, q, b):
            return 200, console.discard(p)

        @route("POST", "/api/apply", write=True)
        def _(m, q, b):
            return 202, self.start_apply()

        @route("GET", "/api/apply/status")
        def _(m, q, b):
            return 200, activate.apply_status(p)

    def read_conn(self):
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return contextlib.closing(conn)

    def applying(self):
        return (self.apply_thread is not None and self.apply_thread.is_alive()) or \
            activate.apply_status(self.p)["state"] == "running"

    def start_apply(self):
        """apply 在后台线程里跑;进度由 activate.apply 写进 host/apply-state.json,前端轮询 /api/apply/status。"""
        def run():
            conn = db.connect(self.db_path)
            try:
                activate.apply(self.p, conn, push=True)
            except CliError:
                pass  # 失败的步骤与原因已写进状态文件
            finally:
                conn.close()

        prev = activate.apply_status(self.p).get("at")
        self.apply_thread = threading.Thread(target=run, name="apply", daemon=True)
        self.apply_thread.start()
        # 等 apply 写下这次的状态文件再返回,之后的轮询不会读到上一次的结果
        deadline = time.monotonic() + 5
        while self.apply_thread.is_alive() and time.monotonic() < deadline:
            s = activate.apply_status(self.p)
            if s["state"] == "running" and s.get("at") != prev:
                return s
            time.sleep(0.02)
        return activate.apply_status(self.p)

    def handle(self, method, url, body):
        parts = urlsplit(url)
        path = unquote(parts.path)
        for meth, rx, fn, write in self.routes:
            m = rx.match(path)
            if m and meth == method:
                try:
                    with self.lock if write else contextlib.nullcontext():
                        if write and self.applying():
                            raise CliError("conflict", "正在应用,应用期间不能修改")
                        return fn(m, parse_qs(parts.query), body)
                except CliError as e:
                    return HTTP_STATUS[e.kind], {"error": e.kind, "message": e.message}
                except Exception as e:  # 未预料的错误也回 JSON,页面才能显示原因
                    return 500, {"error": "failed", "message": f"{type(e).__name__}: {e}"}
        return None


def make_handler(db_path, repo=None):
    cache = GraphCache(db_path)
    api = Api(db_path, repo)

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(DIST), **kw)

        def send_json(self, status, data):
            body = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def dispatch(self, method):
            body = {}
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                try:
                    body = json.loads(self.rfile.read(n))
                except json.JSONDecodeError as e:
                    return self.send_json(400, {"error": "bad_input", "message": f"请求体不是 JSON: {e}"})
                if not isinstance(body, dict):
                    return self.send_json(400, {"error": "bad_input", "message": "请求体应为 JSON 对象"})
            out = api.handle(method, self.path, body)
            if out is None:
                return self.send_json(404, {"error": "not_found", "message": f"没有接口 {method} {self.path}"})
            self.send_json(*out)

        def do_POST(self):
            self.dispatch("POST")

        def do_PATCH(self):
            self.dispatch("PATCH")

        def do_DELETE(self):
            self.dispatch("DELETE")

        def do_GET(self):
            if self.path.split("?")[0] == "/api/graph":
                try:
                    etag, raw, gz = cache.get()
                except sqlite3.Error as e:
                    return self.send_json(500, {"error": "failed", "message": str(e)})
                # no-cache: 浏览器每次都来问,内容没变就回 304,直接用它自己缓存的那份
                if etag in (t.strip() for t in self.headers.get("If-None-Match", "").split(",")):
                    self.send_response(304)
                    self.send_header("ETag", etag)
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    return
                use_gz = "gzip" in self.headers.get("Accept-Encoding", "")
                body = gz if use_gz else raw
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("ETag", etag)
                self.send_header("Vary", "Accept-Encoding")
                if use_gz:
                    self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path.startswith("/api/"):
                return self.dispatch("GET")
            super().do_GET()

        def log_message(self, *a):
            pass

    return Handler


def main():
    ap = argparse.ArgumentParser(description="配置网页服务 (Explore / Organize / Profiles / Inbox)")
    ap.add_argument("--port", type=int, default=5180)
    ap.add_argument("--repo", type=Path, default=None, help="仓库根,默认本仓库 (NODE_INDEX_REPO 同效)")
    ap.add_argument("--db", type=Path, default=Path(os.environ.get("NODE_INDEX_DB") or DEFAULT_DB).expanduser())
    args = ap.parse_args()
    if not args.db.is_file():
        raise SystemExit(f"找不到数据库 {args.db}")
    if not DIST.is_dir():
        print(f"提示: 前端尚未构建 ({DIST} 不存在),只有 /api/... 可用。")
    url = f"http://127.0.0.1:{args.port}"
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(args.db, args.repo))
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            raise SystemExit(f"端口 {args.port} 已被占用: 服务可能已在运行,直接打开 {url};或用 --port 换端口。")
        raise
    print(f"配置网页: {url}")
    server.serve_forever()


if __name__ == "__main__":
    main()
