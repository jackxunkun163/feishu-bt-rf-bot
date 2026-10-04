"""持久化状态与单实例锁。"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading

log = logging.getLogger(__name__)


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
        log.exception("状态文件 %s 损坏，已忽略并备份", path)
        try:
            os.replace(path, path + ".corrupt")
        except OSError:
            pass
        return default


class SeenMessages:
    """持久化的 message_id 去重集合，防止重启后飞书重推导致重复编译。"""

    def __init__(self, path: str, keep: int = 2000):
        self.path, self.keep = path, keep
        self.ids: list[str] = load_json(path, [])
        self.set = set(self.ids)
        self.lock = threading.Lock()

    def add(self, mid: str) -> bool:
        """新消息返回 True，重复返回 False。"""
        with self.lock:
            if mid in self.set:
                return False
            self.ids.append(mid)
            self.set.add(mid)
            if len(self.ids) > self.keep:
                for old in self.ids[: len(self.ids) - self.keep]:
                    self.set.discard(old)
                self.ids = self.ids[-self.keep:]
            try:
                save_json(self.path, self.ids)
            except OSError:
                log.exception("保存去重记录失败")
            return True


class InstanceLock:
    """防止两个机器人进程同时操作同一份源码。进程退出（包括被 kill）后锁自动释放。"""

    def __init__(self, path: str):
        self.path = path
        self.fh = None

    def acquire(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self.fh = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.fh.close()
            raise SystemExit(f"已有另一个机器人实例在运行（锁文件 {self.path}）")
        self.fh.seek(0)
        self.fh.truncate()
        self.fh.write(str(os.getpid()))
        self.fh.flush()
