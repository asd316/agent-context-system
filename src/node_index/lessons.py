"""Capture: `node-index lesson add`。写入 CLI 所在仓库的 shared/reference/lessons/,与当前目录无关。
另有配置网页用的列表与编辑 (CLI 暂无对应命令)。"""
import re

import yaml

from . import shared
from .db import CliError


def _taken_ids(p):
    # 只看文件名前缀: 别的 Lesson 格式坏了不应挡住新 Capture,那是 Fix 的事。
    n = len("L-YYYYMMDD-xxxx")
    return {f.name[:n] for f in p.lessons.glob("L-*.md")}


def add(topic, client, situation, problem, fix, host_only=False, root=None):
    p = shared.paths(root)
    fields = {"topic": topic, "client": client, "situation": situation, "problem": problem, "fix": fix}
    empty = [k for k, v in fields.items() if not v.strip()]
    if empty:
        raise CliError("bad_input", f"以下参数不能为空: {', '.join(empty)}")
    topic = topic.strip()
    if "/" in topic:
        raise CliError("bad_input", f"主题会进入文件名,不得含 /: {topic!r}")
    host = shared.load_host(p).host if host_only else None
    lesson_id = shared.new_id("L", _taken_ids(p))
    name, text = shared.render_lesson(lesson_id, topic, client.strip(), situation, problem, fix, host=host)
    p.lessons.mkdir(parents=True, exist_ok=True)
    path = p.lessons / name
    with open(path, "x", encoding="utf-8") as f:  # "x": 万一并发撞号也不覆盖已有文件
        f.write(text)
    try:
        lesson = shared.load_lesson(path)
    except shared.ShapeError:
        path.unlink()
        raise
    return {"id": lesson.id, "path": str(path.relative_to(p.root)), "status": lesson.status.value,
            "merged_into": lesson.merged_into, "host": lesson.host, "client": lesson.client,
            "date": lesson.date.isoformat()}


def sections(lesson):
    """正文三节的文字: {情境, 问题, 正确做法}。"""
    names = shared.LESSON_SECTIONS
    out = {}
    for i, name in enumerate(names):
        nxt = rf"^## {names[i + 1]}\s*$" if i + 1 < len(names) else r"\Z"
        m = re.search(rf"^## {name}\s*$(.*?)(?={nxt})", lesson.body, re.M | re.S)
        out[name] = m.group(1).strip() if m else ""
    return out


def to_dict(lesson, p):
    sec = sections(lesson)
    return {"id": lesson.id, "topic": lesson.topic, "status": lesson.status.value,
            "merged_into": lesson.merged_into, "host": lesson.host, "client": lesson.client,
            "date": lesson.date.isoformat(), "path": str((p.lessons / lesson.filename).relative_to(p.root)),
            "situation": sec["情境"], "problem": sec["问题"], "fix": sec["正确做法"]}


def list_all(root=None):
    p = shared.paths(root)
    return [to_dict(x, p) for x in sorted(shared.load_lessons(p).values(), key=lambda x: x.id, reverse=True)]


EDITABLE = ("topic", "client", "situation", "problem", "fix", "status", "merged_into")


def update(lesson_id, changes, root=None):
    """改一条 Lesson。id、host、date 不变;主题变了就改文件名。写后重新校验,不合法则还原。"""
    extra = set(changes) - set(EDITABLE)
    if extra:
        raise CliError("bad_input", f"只能改 {', '.join(EDITABLE)},收到 {sorted(extra)}")
    p = shared.paths(root)
    lessons = shared.load_lessons(p)
    if lesson_id not in lessons:
        raise CliError("not_found", f"Lesson 不存在: {lesson_id}")
    old = lessons[lesson_id]
    sec = sections(old)
    cur = {"topic": old.topic, "client": old.client, "situation": sec["情境"], "problem": sec["问题"],
           "fix": sec["正确做法"], "status": old.status.value, "merged_into": old.merged_into}
    new = {**cur, **{k: (v.strip() if isinstance(v, str) else v) for k, v in changes.items()}}
    if new["status"] != shared.LessonStatus.MERGED.value:
        new["merged_into"] = None
    empty = [k for k in ("topic", "client", "situation", "problem", "fix") if not new[k]]
    if empty:
        raise CliError("bad_input", f"以下字段不能为空: {', '.join(empty)}")
    if "/" in new["topic"]:
        raise CliError("bad_input", f"主题会进入文件名,不得含 /: {new['topic']!r}")
    _, text = shared.render_lesson(lesson_id, new["topic"], new["client"], new["situation"], new["problem"],
                                   new["fix"], host=old.host, today=old.date)
    head, body = text.split("\n---\n", 1)
    meta = yaml.safe_load(head.removeprefix("---\n"))
    meta.update(status=new["status"], merged_into=new["merged_into"])
    text = "---\n" + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False) + "---\n" + body
    src, dst = p.lessons / old.filename, p.lessons / f"{lesson_id}-{new['topic']}.md"
    if dst != src and dst.exists():
        raise CliError("conflict", f"目标文件已存在: {dst.name}")
    original = src.read_text(encoding="utf-8")
    dst.write_text(text, encoding="utf-8")
    if dst != src:
        src.unlink()
    try:
        lesson = shared.load_lesson(dst)
    except shared.ShapeError as e:
        if dst != src:
            dst.unlink()
        src.write_text(original, encoding="utf-8")
        raise CliError("bad_input", str(e))
    return to_dict(lesson, p)
