#!/usr/bin/env python3
"""批量安装远程 Referenced entry 到 ~/.local/share/node-index/skills/<name> (安装器的批量版)。

每个仓库只克隆一次 (blob:none),按 entry 的 version 检出,只取含 SKILL.md 的子目录。
frontmatter 的 name 与 entry 名不一致的不装,列出来交给用户。version 为空的写回实际提交。
用法: tools/install_referenced.py [--dry-run] [--only name ...]
"""
import argparse
import io
import re
import subprocess
import sys
import tarfile
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from node_index import catalog as C, shared  # noqa: E402
from node_index.shared import EntryKind, EntryStatus  # noqa: E402

GH = re.compile(r"https://github\.com/([^/]+/[^/]+?)(?:\.git)?(?:/tree/([^/]+)(?:/(.*))?)?$")


def git(repo, *args, data=False, tries=4):
    """blob:none 克隆按需取 blob,网络抖动 (SSL_ERROR_SYSCALL) 时重试。"""
    for i in range(tries):
        r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
        if r.returncode == 0:
            return r.stdout if data else r.stdout.decode().strip()
        if b"promisor remote" not in r.stderr or i == tries - 1:
            raise subprocess.CalledProcessError(r.returncode, args, r.stdout, r.stderr)
        time.sleep(2 * (i + 1))


def frontmatter_name(text):
    m = re.match(r"---\n(.*?)\n---", text, re.S)
    if not m:
        return None
    n = re.search(r"^name:\s*['\"]?(.+?)['\"]?\s*$", m.group(1), re.M)
    return n.group(1) if n else None


def install_repo(repo, entries, tmp, dry_run):
    out = []
    clone = Path(tmp) / repo.replace("/", "__")
    try:
        subprocess.run(["git", "clone", "-q", "--filter=blob:none", "--no-checkout",
                        f"https://github.com/{repo}.git", str(clone)], capture_output=True, check=True)
    except subprocess.CalledProcessError as e:
        return [(e_.name, "clone-failed", e.stderr.decode().strip()[-200:]) for e_, _, _ in entries]
    for e, branch, sub in entries:
        try:
            ref = e.version or git(clone, "rev-parse", "origin/HEAD" if branch in (None, "HEAD") else f"origin/{branch}")
            commit = git(clone, "rev-parse", f"{ref}^{{commit}}")
            if sub is None:
                files = git(clone, "ls-tree", "-r", "--name-only", commit).splitlines()
                if "SKILL.md" in files:
                    sub = ""
                else:
                    hits = [f[:-len("/SKILL.md")] for f in files
                            if f.endswith(f"/{e.name}/SKILL.md") or f == f"{e.name}/SKILL.md"]
                    if len(hits) != 1:
                        out.append((e.name, "no-skill-dir", f"{len(hits)} 个候选"))
                        continue
                    sub = hits[0]
            text = git(clone, "show", f"{commit}:{sub + '/' if sub else ''}SKILL.md", data=True).decode()
            name = frontmatter_name(text)
            if name != e.name:
                out.append((e.name, "name-mismatch", f"frontmatter name={name!r} ({repo}/{sub})"))
                continue
            dst = C.REFERENCED_CACHE / e.name
            if dry_run:
                out.append((e.name, "would-install", f"{repo}@{commit[:12]}/{sub}"))
                continue
            tar = git(clone, "archive", "--format=tar", f"{commit}:{sub}" if sub else commit, data=True)
            dst.mkdir(parents=True)
            with tarfile.open(fileobj=io.BytesIO(tar)) as t:
                t.extractall(dst, filter="data")
            out.append((e.name, "installed", commit))
        except subprocess.CalledProcessError as err:
            out.append((e.name, "git-failed", err.stderr.decode().strip()[-200:]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", nargs="*")
    args = ap.parse_args()
    p = shared.paths(ROOT)
    cat = shared.load_catalog(p)
    by_repo, results = {}, []
    for e in sorted(cat.values(), key=lambda e: e.name):
        if e.entry is not EntryKind.REFERENCED or e.status is not EntryStatus.ACTIVE or e.source.startswith(("/", "~")):
            continue
        if args.only and e.name not in args.only:
            continue
        if (C.REFERENCED_CACHE / e.name / "SKILL.md").exists():
            results.append((e.name, "present", ""))
            continue
        m = GH.match(e.source)
        if not m:
            results.append((e.name, "unsupported-source", e.source))
            continue
        by_repo.setdefault(m.group(1), []).append((e, m.group(2), m.group(3)))
    with tempfile.TemporaryDirectory() as tmp, ThreadPoolExecutor(8) as pool:
        for r in pool.map(lambda kv: install_repo(kv[0], kv[1], tmp, args.dry_run), by_repo.items()):
            results.extend(r)
    for name, status, detail in sorted(results, key=lambda r: (r[1], r[0])):
        print(f"{status}\t{name}\t{detail}")
        if status == "installed" and not cat[name].version:
            C.set_entry(p, name, version=detail)
    counts = {}
    for _, s, _ in results:
        counts[s] = counts.get(s, 0) + 1
    print(counts, file=sys.stderr)


if __name__ == "__main__":
    main()
