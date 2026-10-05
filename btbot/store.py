"""JSON 文件读写（原子写）。"""
from __future__ import annotations

import json
import os
import sys
import tempfile


def save_json(path: str, data) -> None:
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (OSError, ValueError):
        print(f"警告: {path} 损坏，已忽略并备份为 .corrupt", file=sys.stderr)
        try:
            os.replace(path, path + ".corrupt")
        except OSError:
            pass
        return default
