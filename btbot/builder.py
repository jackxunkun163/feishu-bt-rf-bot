from __future__ import annotations

import glob
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass

_ERR_RE = re.compile(r"(error:|error \d+|FAILED:|ninja: build stopped|make: \*\*\*|fatal:|Traceback)", re.I)


@dataclass
class BuildResult:
    ok: bool
    returncode: int | None
    timed_out: bool
    cancelled: bool
    seconds: float
    log_path: str
    error_summary: str = ""


class Builder:
    def __init__(self):
        self._proc: subprocess.Popen | None = None
        self._cancelled = False
        self._closed = False  # close() 之后不再启动新编译
        self._lock = threading.Lock()

    def run(self, command: str, cwd: str, log_path: str, timeout_s: int, on_start=None) -> BuildResult:
        """on_start(pid)：进程启动后回调，用于记录 PID，机器人异常重启后可清理残留编译进程。"""
        start = time.time()
        timed_out = False
        with open(log_path, "wb") as log:
            log.write(f"$ cd {cwd}\n$ {command}\n\n".encode())
            log.flush()
            with self._lock:
                self._cancelled = False
                if self._closed:
                    return BuildResult(False, None, False, True, 0, log_path, "机器人正在退出")
                try:
                    proc = self._proc = self._popen(command, cwd, log)
                except OSError as e:
                    return BuildResult(False, None, False, False, time.time() - start, log_path,
                                       f"无法启动编译: {e}")
            try:
                if on_start:
                    on_start(proc.pid)
                rc = proc.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                timed_out = True
                self.kill()
                rc = proc.wait()
            except BaseException:
                self.kill()  # on_start 失败等意外情况，不留孤儿进程
                raise
            finally:
                with self._lock:
                    self._proc = None
        cancelled = self._cancelled
        ok = rc == 0 and not timed_out and not cancelled
        return BuildResult(ok, rc, timed_out, cancelled, time.time() - start, log_path,
                           "" if ok else error_summary(log_path))

    @staticmethod
    def _popen(command: str, cwd: str, log) -> subprocess.Popen:
        if os.name == "nt":
            return subprocess.Popen(command, cwd=cwd, shell=True, stdout=log,
                                    stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        # MTK 编译需要 bash（source build/envsetup.sh），独立进程组便于超时/取消时整组杀掉
        return subprocess.Popen(["/bin/bash", "-c", command], cwd=cwd, stdout=log, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True)

    def cancel(self) -> bool:
        with self._lock:
            if self._proc is None:
                return False
            self._cancelled = True
        self.kill()
        return True

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self.cancel()

    def kill(self):
        p = self._proc
        if p is None or p.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
            else:
                os.killpg(p.pid, signal.SIGTERM)
                try:
                    p.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass


def proc_token(pid: int) -> str | None:
    """进程启动时间（Linux /proc），用来确认 PID 没有被系统复用给别的进程。"""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def kill_stale(pid: int, token: str | None) -> bool:
    """杀掉上次异常退出时遗留的编译进程组。只在能确认是同一个进程时才动手。"""
    if os.name == "nt" or token is None or proc_token(pid) != token:
        return False
    try:
        os.killpg(pid, signal.SIGTERM)
        for _ in range(20):
            time.sleep(1)
            os.killpg(pid, 0)  # 进程组已退出时抛 ProcessLookupError
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        pass
    return True


def free_gb(path: str) -> float:
    return shutil.disk_usage(path).free / 1024 ** 3


def tail_file(src: str, dst: str, max_bytes: int) -> str:
    with open(src, "rb") as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - max_bytes))
        data = f.read()
    with open(dst, "wb") as f:
        f.write(data)
    return dst


def error_summary(log_path: str, max_err_lines: int = 25, tail_lines: int = 15) -> str:
    """从日志末尾提取错误行 + 最后几行。"""
    try:
        with open(log_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 4 * 1024 * 1024))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError as e:
        return f"读取日志失败: {e}"
    lines = [re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", l) for l in lines]
    errs = [l for l in lines if _ERR_RE.search(l)][-max_err_lines:]
    tail = lines[-tail_lines:]
    parts = []
    if errs:
        parts.append("\n".join(errs))
    parts.append("--- 日志末尾 ---\n" + "\n".join(tail))
    return "\n".join(parts)


def collect_artifacts(root: str, patterns: list[str]) -> list[str]:
    files = []
    for pat in patterns:
        files.extend(sorted(glob.glob(os.path.join(root, pat))))
    return [f for f in files if os.path.isfile(f)]


def publish(files: list[str], dest_dir: str) -> list[str]:
    os.makedirs(dest_dir, exist_ok=True)
    out = []
    for f in files:
        d = os.path.join(dest_dir, os.path.basename(f))
        shutil.copy2(f, d)
        out.append(d)
    return out


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def human_time(s: float) -> str:
    s = int(s)
    return f"{s // 3600}h{s % 3600 // 60:02d}m{s % 60:02d}s" if s >= 3600 else f"{s // 60}m{s % 60:02d}s"
