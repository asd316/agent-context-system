#!/usr/bin/env python3
"""把本机 Index 导出成对外展示用的脱敏星图数据 (个人网站 node-index 项目页的只读星图)。

白名单制: 默认不公开任何名称。只导出类别、父子边、访问热度三档;路径、内容摘要、时间、状态全部丢弃,id 重新编号。
节点数与结构保持真实。名称只有命中白名单 (或是 Catalog 里来自 GitHub 的 Referenced skill) 才公开,其余显示为类别标签。
输出前对所有公开文字做关键词扫描,命中就失败退出,不写文件。

用法: tools/export_demo_graph.py --whitelist <whitelist.yaml> --out <graph.json> [--report <names.tsv>] [--db <index.db>] [--catalog <catalog.yaml>]
"""
import argparse
import csv
import json
import sqlite3
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from node_index import db  # noqa: E402


def load(db_path):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        try:
            nodes = conn.execute("SELECT n.id, n.name, n.category, COALESCE(a.hits, 0) FROM nodes n "
                                 "LEFT JOIN node_access a ON a.node_id = n.id ORDER BY n.id").fetchall()
        except sqlite3.OperationalError:  # 老库还没有访问计数表
            nodes = [(*r, 0) for r in conn.execute("SELECT id, name, category FROM nodes ORDER BY id")]
        edges = conn.execute("SELECT parent_id, child_id FROM edges ORDER BY parent_id, child_id").fetchall()
    finally:
        conn.close()
    return nodes, edges


def github_skills(catalog_path):
    cat = yaml.safe_load(catalog_path.read_text()) or {}
    return {name for name, e in cat.items()
            if e.get("entry") == "referenced" and str(e.get("source", "")).startswith("https://github.com/")}


def hits_band(hits, cuts):
    # 三档: 0 = 没被读过, 1 = 低, 10 = 高。前端按 log(1+hits) 取亮度,三档够用且不露真实次数
    return 0 if hits <= 0 else (10 if hits >= cuts["high"] else 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--whitelist", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--report", type=Path, help="写出公开名称清单 (tsv),供人审")
    ap.add_argument("--db", default=db.DEFAULT_DB)
    ap.add_argument("--catalog", type=Path, default=ROOT / "shared" / "catalog.yaml")
    args = ap.parse_args()

    wl = yaml.safe_load(args.whitelist.read_text())
    public = {cat: set(names or []) for cat, names in wl["public_names"].items()}
    rename, summaries, anon = wl.get("rename", {}), wl.get("summaries", {}), wl["anon_label"]
    blocklist = [w.lower() for w in wl["blocklist"]]
    referenced = github_skills(args.catalog) if wl.get("public_github_skills") else set()
    hide = set(wl.get("hide", []))  # 白名单或 GitHub 规则放行、但仍要隐藏的名称

    nodes, edges = load(args.db)
    new_id = {old: i for i, (old, *_rest) in enumerate(nodes, start=1)}  # 0 留给前端的虚拟中心「我」
    out_nodes, report = [], []
    for old, name, category, hits in nodes:
        is_public = name not in hide and (name in public.get(category, ()) or (category == "Skill" and name in referenced))
        if is_public:
            shown = rename.get(name, name.removesuffix(".md"))
            reason = "github-skill" if name in referenced and name not in public.get(category, ()) else "whitelist"
            report.append((category, name, shown, reason, summaries.get(name, "")))
        out_nodes.append({
            "id": new_id[old], "name": shown if is_public else anon[category], "anon": not is_public,
            "category": category, "hier_summary": summaries.get(name, "") if is_public else "",
            "content_summary": None, "path": None, "path_kind": None, "status": None, "modified_at": "",
            "pending": False, "missing": False, "hits": hits_band(hits, wl["hits_cuts"]), "last_at": None,
        })
    out_edges = [{"parent": new_id[p], "child": new_id[c], "origin": "scan"}
                 for p, c in edges if p in new_id and c in new_id]

    leaks = [(n["name"], n["hier_summary"], w) for n in out_nodes for w in blocklist
             if w in (n["name"] + " " + n["hier_summary"]).lower()]
    if leaks:
        for name, summary, w in leaks:
            print(f"BLOCKED: 「{w}」出现在 {name!r} {summary!r}", file=sys.stderr)
        sys.exit(1)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"nodes": out_nodes, "edges": out_edges, "db_mtime": 0},
                                   ensure_ascii=False, separators=(",", ":")))
    if args.report:
        with args.report.open("w", newline="") as f:
            w = csv.writer(f, delimiter="\t")
            w.writerow(["category", "原名", "显示名", "来源", "摘要"])
            w.writerows(sorted(report))
    n_pub = sum(1 for n in out_nodes if not n["anon"])
    print(f"nodes={len(out_nodes)} edges={len(out_edges)} public={n_pub} anon={len(out_nodes) - n_pub} -> {args.out}")


if __name__ == "__main__":
    main()
