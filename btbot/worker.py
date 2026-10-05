"""编译任务队列：同一时间只编译一个任务，其余排队。

健壮性设计：
- 队列持久化到 queue.json，机器人重启后继续执行；
- 改源码前先备份原文件并写 journal.json，进程崩溃/断电后重启时据此还原源码、清理残留编译进程；
- 还原前校验文件内容，若编译期间被别人改动则不覆盖，避免误伤；
- 还原失败时暂停接单并告警，绝不在脏源码上继续编译。
"""
from __future__ import annotations

import gzip
import json
import logging
import os
import shutil
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Protocol

from . import builder as B
from .config import Config
from .feishu import FILE_LIMIT, code_block
from .journal import backup_files, restore_files
from .params import ParamError, make_plan, parse_message, write_text
from .store import load_json, save_json

log = logging.getLogger(__name__)


class Notifier(Protocol):
    def text(self, message_id: str, text: str) -> None: ...
    def card(self, message_id: str, title: str, markdown: str, color: str = "blue") -> None: ...
    def file(self, message_id: str, path: str, name: str | None = None) -> None: ...
    def alert(self, text: str) -> None: ...


class Rejected(Exception):
    """任务不能入队（队列满、超过个人上限、机器人暂停等）。"""


@dataclass
class Job:
    project: str
    text: str  # 原始消息，执行时按最新配置重新解析
    requester: str  # open_id
    message_id: str
    id: str = field(default_factory=lambda: datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3])
    created: float = field(default_factory=time.time)
    attempts: int = 0  # 因机器人崩溃被中断的次数
    started: float | None = None

    def describe(self) -> str:
        return f"`{self.id}` 项目 {self.project}，<at id={self.requester}></at>"


class Worker(threading.Thread):
    def __init__(self, cfg: Config, notifier: Notifier):
        super().__init__(daemon=True, name="build-worker")
        self.cfg = cfg
        self.n = notifier
        self.cv = threading.Condition()
        self.pending: list[Job] = []
        self.current: Job | None = None
        self.builder = B.Builder()
        self.stopping = False
        self.blocked: str | None = None  # 非空时暂停接单（源码还原失败等）
        self.queue_path = os.path.join(cfg.workdir, "queue.json")
        self.journal_path = os.path.join(cfg.workdir, "journal.json")
        os.makedirs(os.path.join(cfg.workdir, "jobs"), exist_ok=True)
        self._recover()

    # ------------------------------------------------------------ 对外接口
    def submit(self, job: Job) -> int:
        """入队，返回前面还有几个任务；不能入队时抛 Rejected。"""
        lim = self.cfg.limits
        with self.cv:
            if self.blocked:
                raise Rejected(f"机器人已暂停接单：{self.blocked}")
            if self.stopping:
                raise Rejected("机器人正在重启，请稍后再试")
            if len(self.pending) >= lim.max_queue:
                raise Rejected(f"排队任务已达上限 {lim.max_queue}，请稍后再试")
            mine = [j for j in self._all() if j.requester == job.requester]
            if len(mine) >= lim.max_jobs_per_user:
                raise Rejected(f"你已有 {len(mine)} 个任务在排队/编译，请等完成后再提交（或 /cancel）")
            ahead = len(self._all())
            self.pending.append(job)
            self._persist()
            self.cv.notify()
        return ahead

    def status(self) -> str:
        with self.cv:
            lines = []
            if self.blocked:
                lines.append(f"⛔ 已暂停接单：{self.blocked}")
            if self.current:
                el = B.human_time(time.time() - (self.current.started or time.time()))
                lines.append(f"🔨 正在编译：{self.current.describe()}（已用时 {el}）")
            for i, j in enumerate(self.pending, 1):
                lines.append(f"⏳ 排队 {i}：{j.describe()}")
        return "\n".join(lines) or "空闲，没有任务。"

    def cancel(self, requester: str) -> str:
        with self.cv:
            mine = [j for j in self.pending if j.requester == requester]
            if mine:
                self.pending.remove(mine[-1])
                self._persist()
                return f"已取消排队中的任务 `{mine[-1].id}`"
            if self.current and self.current.requester == requester:
                self.builder.cancel()
                return f"正在终止编译任务 `{self.current.id}`，源码会自动还原..."
        return "你没有可取消的任务。"

    def unblock(self) -> str:
        with self.cv:
            self.blocked = None
            self.cv.notify()
        return "已恢复接单。"

    def shutdown(self, timeout: float = 120) -> None:
        """优雅退出：终止当前编译、还原源码，当前任务保留在队列中重启后继续。"""
        with self.cv:
            self.stopping = True
            self.cv.notify_all()
        self.builder.close()
        if self.is_alive():
            self.join(timeout)

    # ------------------------------------------------------------ 持久化 / 崩溃恢复
    def _all(self) -> list[Job]:
        return ([self.current] if self.current else []) + self.pending

    def _persist(self) -> None:
        data = [dict(asdict(j), running=(j is self.current)) for j in self._all()]
        try:
            save_json(self.queue_path, data)
        except OSError:
            log.exception("保存队列失败")

    def _recover(self) -> None:
        journal = load_json(self.journal_path, None)
        if journal:
            self.n.alert(f"检测到上次异常退出（任务 {journal.get('job_id')}），正在清理")
            if journal.get("pid") and B.kill_stale(journal["pid"], journal.get("pid_token")):
                self.n.alert(f"已终止残留编译进程 {journal['pid']}")
            problems = self._restore(journal)
            if problems:
                self.blocked = "上次异常退出后源码还原失败，请人工检查后发送 /unblock"
                self.n.alert(self.blocked + "\n" + "\n".join(problems))
            else:
                self._clear_journal()

        lim = self.cfg.limits
        for d in load_json(self.queue_path, []):
            try:
                running = d.pop("running", False)
                d.pop("started", None)
                job = Job(**d)
            except TypeError:
                log.error("丢弃无法识别的排队任务: %s", d)
                continue
            if running:
                job.attempts += 1
                if job.attempts > lim.max_retries_after_crash:
                    self.n.text(job.message_id, f"任务 {job.id} 因机器人异常重启被中断，已多次重试失败，请重新提交。")
                    continue
                self.n.text(job.message_id, f"机器人异常重启，任务 {job.id} 被中断，源码已还原，现重新排队执行。")
            self.pending.append(job)
        self._persist()

    def _write_journal(self, job: Job, plan, job_dir: str) -> dict:
        files = backup_files(plan, os.path.join(job_dir, "backup"))
        journal = {"job_id": job.id, "files": files, "pid": None, "pid_token": None}
        save_json(self.journal_path, journal)
        return journal

    def _clear_journal(self) -> None:
        try:
            os.remove(self.journal_path)
        except FileNotFoundError:
            pass

    def _restore(self, journal: dict) -> list[str]:
        return restore_files(journal["files"])

    # ------------------------------------------------------------ 执行
    def run(self):
        while True:
            with self.cv:
                while not self.stopping and (not self.pending or self.blocked):
                    self.cv.wait()
                if self.stopping:
                    return
                job = self.pending.pop(0)
                self.current = job
                job.started = time.time()
                self._persist()
            try:
                self._process(job)
            except Exception as e:  # noqa: BLE001
                log.exception("任务 %s 异常", job.id)
                self.n.card(job.message_id, "❌ 机器人内部错误", code_block(repr(e)), "red")
                self.n.alert(f"任务 {job.id} 内部错误: {e!r}")
            finally:
                with self.cv:
                    self.current = None
                    self._persist()

    def _process(self, job: Job):
        try:
            proj = self.cfg.project(job.project)
        except KeyError as e:
            self.n.text(job.message_id, str(e.args[0]))
            return
        job_dir = os.path.join(self.cfg.workdir, "jobs", job.id)
        os.makedirs(job_dir, exist_ok=True)

        # 排队期间源码/配置可能变化，执行前重新解析、校验
        _, assigns, errors = parse_message(job.text, proj.params)
        try:
            if errors:
                raise ParamError("\n".join(errors))
            plan = make_plan(assigns, proj.root)
        except ParamError as e:
            self.n.card(job.message_id, "❌ 参数校验失败", str(e), "red")
            return
        if proj.min_free_gb:
            free = B.free_gb(proj.root)
            if free < proj.min_free_gb:
                self.n.card(job.message_id, "❌ 磁盘空间不足",
                            f"剩余 {free:.1f}GB，低于要求的 {proj.min_free_gb}GB，已取消编译。", "red")
                self.n.alert(f"[{proj.name}] 磁盘剩余 {free:.1f}GB，不足 {proj.min_free_gb}GB")
                return
        diff, summary = plan.diff(), plan.summary()
        with open(os.path.join(job_dir, "changes.diff"), "w", encoding="utf-8") as f:
            f.write(diff)

        journal = self._write_journal(job, plan, job_dir)
        restore_problems: list[str] = []
        try:
            for path, text in plan.new_texts.items():
                write_text(path, text)
            self.n.card(job.message_id, f"🔨 开始编译 {proj.name}",
                        f"任务 `{job.id}`\n**本次修改：**\n{code_block(summary)}", "blue")

            def on_start(pid):
                journal.update(pid=pid, pid_token=B.proc_token(pid))
                save_json(self.journal_path, journal)
            res = self.builder.run(proj.build_command, proj.root, os.path.join(job_dir, "build.log"),
                                   proj.timeout_minutes * 60, on_start)
        finally:
            if proj.restore_after_build or self.stopping or "res" not in locals():
                restore_problems = self._restore(journal)
            if restore_problems:
                with self.cv:
                    self.blocked = f"任务 {job.id} 编译后源码还原异常，请人工检查后发送 /unblock"
                self.n.alert(self.blocked + "\n" + "\n".join(restore_problems))
            else:
                self._clear_journal()

        if self.stopping and res.cancelled:
            # 机器人正在重启：任务放回队首，重启后继续
            with self.cv:
                job.started = None
                self.pending.insert(0, job)
            self.n.text(job.message_id, f"机器人正在重启，任务 {job.id} 已中止并还原源码，重启后会自动重新编译。")
            return
        self._record(job, summary, res)
        self._report(job, proj, summary, diff, res, job_dir, restore_problems)

    def _report(self, job, proj, summary, diff, res: B.BuildResult, job_dir, restore_problems):
        md = [f"任务 `{job.id}`　耗时 **{B.human_time(res.seconds)}**",
              f"**修改的参数：**\n{code_block(summary)}",
              f"**Diff：**\n{code_block(diff, 'diff')}"]
        if restore_problems:
            restore_msg = "⚠️ 源码还原异常，已通知管理员：\n" + "\n".join(f"- {p}" for p in restore_problems)
        elif proj.restore_after_build:
            restore_msg = "源码已还原"
        else:
            restore_msg = "⚠️ 修改已保留在源码中（restore_after_build=false）"

        if res.ok:
            arts = B.collect_artifacts(proj.root, proj.artifacts)
            if arts and proj.publish_dir:
                dest = os.path.join(proj.publish_dir, proj.name, job.id)
                try:
                    B.publish(arts, dest)
                    with open(os.path.join(dest, "changes.diff"), "w", encoding="utf-8") as f:
                        f.write(diff)
                    prefix = proj.publish_url_prefix
                    md.append(f"**产物已发布：** {prefix.rstrip('/') + '/' + proj.name + '/' + job.id + '/' if prefix else dest}")
                except OSError as e:
                    md.append(f"⚠️ 产物发布失败：{e}")
                    self.n.alert(f"[{proj.name}] 任务 {job.id} 产物发布失败: {e}")
            if arts:
                md.append("**产物：**\n" + "\n".join(
                    f"- {os.path.relpath(a, proj.root)}（{B.human_size(os.path.getsize(a))}）" for a in arts))
            elif proj.artifacts:
                md.append("⚠️ 未找到配置的编译产物，请检查 artifacts 配置")
            md.append(restore_msg)
            if proj.note:
                md.append(f"💡 {proj.note}")
            self.n.card(job.message_id, f"✅ 编译成功 {proj.name}", "\n\n".join(md), "green")
            return

        reason = "已取消" if res.cancelled else ("超时" if res.timed_out else f"退出码 {res.returncode}")
        md.insert(1, f"**失败原因：** {reason}")
        md.append(f"**错误摘要：**\n{code_block(res.error_summary)}")
        md.append(restore_msg)
        self.n.card(job.message_id, f"❌ 编译失败 {proj.name}", "\n\n".join(md), "red")
        if not res.cancelled and os.path.exists(res.log_path):
            self.n.file(job.message_id, self._pack_log(res.log_path, job))

    @staticmethod
    def _pack_log(log_path: str, job: Job) -> str:
        """压缩日志；压缩后仍超过飞书上限则只保留日志末尾。"""
        d = os.path.dirname(log_path)
        src, keep = log_path, None
        for _ in range(4):
            gz = os.path.join(d, f"build-{job.id}{'-tail' if keep else ''}.log.gz")
            with open(src, "rb") as fi, gzip.open(gz, "wb") as fo:
                shutil.copyfileobj(fi, fo)
            if os.path.getsize(gz) <= FILE_LIMIT:
                return gz
            keep = (keep or os.path.getsize(log_path)) // 4
            src = B.tail_file(log_path, os.path.join(d, "build-tail.log"), keep)
        return gz

    def _record(self, job: Job, summary: str, res: B.BuildResult):
        rec = {"id": job.id, "project": job.project, "requester": job.requester,
               "changes": summary, "ok": res.ok, "returncode": res.returncode,
               "timed_out": res.timed_out, "cancelled": res.cancelled, "seconds": round(res.seconds)}
        try:
            with open(os.path.join(self.cfg.workdir, "history.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            log.exception("写历史记录失败")
