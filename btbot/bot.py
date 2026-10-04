"""消息处理逻辑（与飞书 SDK 解耦，便于本地测试）。"""
from __future__ import annotations

import logging

from .config import Config
from .feishu import code_block
from .params import ParamError, current_value, make_plan, parse_message
from .worker import Job, Notifier, Rejected, Worker

log = logging.getLogger(__name__)

HELP = """**蓝牙射频参数编译机器人**
直接发送参数即可，每行一个，例如：
```
项目=k6789
Radio[0]=0x07
TxPWOffset=0x80,0x82,0x80
```
- 数组参数可写下标 `名称[i]=值`，或一次给出全部元素 `名称=v1,v2,...`
- 只有一个项目时可以省略 `项目=`

**命令**
- `/params [项目]` 查看支持的参数及当前值
- `/check` + 参数：只校验并预览修改，不编译
- `/status` 查看编译队列
- `/cancel` 取消自己的任务
- `/help` 帮助"""


class Bot:
    def __init__(self, cfg: Config, notifier: Notifier, start_worker: bool = True):
        self.cfg = cfg
        self.n = notifier
        self.worker = Worker(cfg, notifier)
        if start_worker:
            self.worker.start()

    def handle(self, text: str, sender: str, message_id: str) -> None:
        """处理一条消息。任何异常都转成回复，不会向外抛出。"""
        try:
            self._handle(text, sender, message_id)
        except Exception as e:  # noqa: BLE001
            log.exception("处理消息异常: %r", text[:200])
            self.n.text(message_id, f"机器人处理出错：{e!r}，请稍后重试或联系管理员。")
            self.n.alert(f"处理消息异常: {e!r}")

    def _handle(self, text: str, sender: str, message_id: str) -> None:
        if self.cfg.allowed_users and sender not in self.cfg.allowed_users:
            self.n.text(message_id, "你没有使用此机器人的权限，请联系管理员加入白名单。")
            return
        text = text.strip()
        if len(text) > self.cfg.limits.max_message_chars:
            self.n.text(message_id, f"消息过长（{len(text)} 字符，上限 {self.cfg.limits.max_message_chars}）。")
            return
        cmd, rest = "", text
        first, _, body = text.partition("\n")
        if first.startswith("/"):
            cmd, _, arg = first.partition(" ")
            cmd, rest = cmd.lower(), (arg + "\n" + body).strip()

        if cmd in ("/help", "/帮助"):
            self.n.card(message_id, "使用帮助", HELP)
        elif cmd in ("/status", "/状态"):
            self.n.text(message_id, self.worker.status())
        elif cmd in ("/cancel", "/取消"):
            self.n.text(message_id, self.worker.cancel(sender))
        elif cmd in ("/params", "/参数"):
            self._list_params(rest.strip() or None, message_id)
        elif cmd in ("/check", "/预览"):
            self._submit(rest, sender, message_id, dry_run=True)
        elif cmd == "/unblock":
            if self.cfg.admins and sender not in self.cfg.admins:
                self.n.text(message_id, "只有管理员可以执行 /unblock。")
            else:
                self.n.text(message_id, self.worker.unblock())
        elif cmd:
            self.n.text(message_id, f"未知命令 {cmd}，发送 /help 查看帮助")
        else:
            self._submit(text, sender, message_id, dry_run=False)

    def _list_params(self, project: str | None, message_id: str) -> None:
        try:
            proj = self.cfg.project(project)
        except KeyError as e:
            self.n.text(message_id, str(e.args[0]))
            return
        lines = []
        for r in proj.params:
            try:
                cur = current_value(r, proj.root)
            except (ParamError, OSError) as e:
                cur = f"读取失败: {e}"
            alias = f"（别名: {', '.join(r.aliases)}）" if r.aliases else ""
            lines.append(f"- **{r.name}**{alias} {r.desc}\n  当前值: `{cur}`")
        self.n.card(message_id, f"项目 {proj.name} 支持的参数", "\n".join(lines))

    def _submit(self, text: str, sender: str, message_id: str, dry_run: bool) -> None:
        project, _, _ = parse_message(text, [])
        try:
            proj = self.cfg.project(project)
        except KeyError as e:
            self.n.text(message_id, str(e.args[0]))
            return
        _, assigns, errors = parse_message(text, proj.params)
        if not assigns and not errors:
            self.n.text(message_id, "没有识别到参数。发送 /help 查看格式。")
            return
        if errors:
            self.n.card(message_id, "❌ 参数有误", "\n".join(f"- {e}" for e in errors), "red")
            return
        try:
            plan = make_plan(assigns, proj.root)
        except ParamError as e:
            self.n.card(message_id, "❌ 参数校验失败", str(e), "red")
            return
        if dry_run:
            self.n.card(message_id, f"🔍 预览 {proj.name}（未编译）",
                        f"{code_block(plan.summary())}\n{code_block(plan.diff(), 'diff')}", "grey")
            return
        job = Job(proj.name, text, sender, message_id)
        try:
            ahead = self.worker.submit(job)
        except Rejected as e:
            self.n.text(message_id, str(e))
            return
        queue_info = f"前面还有 {ahead} 个任务，请稍候" if ahead else "马上开始编译"
        self.n.text(message_id, f"已收到 {len(plan.changes)} 项参数修改，任务 {job.id}，{queue_info}。")
