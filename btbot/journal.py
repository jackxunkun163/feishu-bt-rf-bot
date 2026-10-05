"""改源码前备份、改完后还原。飞书机器人（worker）和 Jenkins 命令行（rf）共用。"""
from __future__ import annotations

import hashlib
import os

from .params import Plan, read_text, write_text


def sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", "surrogateescape")).hexdigest()


def backup_files(plan: Plan, backup_dir: str) -> list[dict]:
    """备份 plan 涉及的原文件，返回还原所需的记录（可 JSON 序列化）。"""
    os.makedirs(backup_dir, exist_ok=True)
    files = []
    for i, (path, old) in enumerate(plan.old_texts.items()):
        bk = os.path.join(backup_dir, f"{i}_{os.path.basename(path)}")
        write_text(bk, old)
        files.append({"path": path, "backup": bk, "orig_sha": sha(old), "new_sha": sha(plan.new_texts[path])})
    return files


def restore_files(files: list[dict]) -> list[str]:
    """按备份还原，返回问题列表（空表示全部成功）。

    只还原内容仍是"本次修改后"的文件；若文件在此期间被别人改过则不覆盖，留给人工处理。
    """
    problems = []
    for f in files:
        path = f["path"]
        try:
            cur = sha(read_text(path)) if os.path.exists(path) else None
            if cur == f["orig_sha"]:
                continue  # 尚未改动或已还原
            if cur != f["new_sha"]:
                problems.append(f"{path} 在编译期间被其它人修改，未自动还原，原始内容备份在 {f['backup']}")
                continue
            write_text(path, read_text(f["backup"]))
        except OSError as e:
            problems.append(f"还原 {path} 失败: {e}（备份在 {f['backup']}）")
    return problems
