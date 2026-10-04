"""飞书消息发送封装：失败自动重试，任何情况下都不向调用方抛异常。"""
from __future__ import annotations

import json
import logging
import os
import time

import lark_oapi as lark
from lark_oapi.api.im.v1 import (CreateFileRequest, CreateFileRequestBody, CreateMessageRequest,
                                 CreateMessageRequestBody, ReplyMessageRequest, ReplyMessageRequestBody)

log = logging.getLogger(__name__)
CARD_TEXT_LIMIT = 12000
FILE_LIMIT = 30 * 1024 * 1024
# 限流 / 服务端内部错误，可重试
RETRY_CODES = {99991400, 99991401, 1000004, 1000005, 230020, 11232, 11233}


def _call(desc: str, fn, attempts: int = 4):
    """调用飞书 API，网络异常或可重试错误码时指数退避重试。成功返回 response，失败返回 None。"""
    delay = 1.0
    for i in range(1, attempts + 1):
        try:
            resp = fn()
            if resp.success():
                return resp
            log.warning("%s 失败 code=%s msg=%s (第 %d 次)", desc, resp.code, resp.msg, i)
            if resp.code not in RETRY_CODES:
                return None
        except Exception:  # noqa: BLE001  网络错误、SDK 内部错误等
            log.warning("%s 异常 (第 %d 次)", desc, i, exc_info=True)
        if i < attempts:
            time.sleep(delay)
            delay *= 2
    log.error("%s 最终失败", desc)
    return None


class Messenger:
    def __init__(self, client: lark.Client, alert_chat_id: str | None = None):
        self.client = client
        self.alert_chat_id = alert_chat_id

    def reply(self, message_id: str, msg_type: str, content: dict) -> bool:
        body = json.dumps(content, ensure_ascii=False)
        req = ReplyMessageRequest.builder().message_id(message_id).request_body(
            ReplyMessageRequestBody.builder().msg_type(msg_type).content(body).build()).build()
        return _call(f"回复消息[{msg_type}]", lambda: self.client.im.v1.message.reply(req)) is not None

    def text(self, message_id: str, text: str) -> None:
        self.reply(message_id, "text", {"text": text[:CARD_TEXT_LIMIT]})

    def card(self, message_id: str, title: str, markdown: str, color: str = "blue") -> None:
        """color: blue / green / red / orange / grey。卡片发送失败时降级为纯文本。"""
        if len(markdown) > CARD_TEXT_LIMIT:
            markdown = markdown[:CARD_TEXT_LIMIT] + "\n```\n...(内容过长已截断)"
        ok = self.reply(message_id, "interactive", {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": title}, "template": color},
            "elements": [{"tag": "markdown", "content": markdown}],
        })
        if not ok:
            self.text(message_id, f"{title}\n{markdown}")

    def file(self, message_id: str, path: str, name: str | None = None) -> None:
        try:
            if os.path.getsize(path) > FILE_LIMIT:
                self.text(message_id, f"文件超过 30MB，未上传，请到服务器查看：{path}")
                return

            def upload():
                with open(path, "rb") as f:
                    req = CreateFileRequest.builder().request_body(
                        CreateFileRequestBody.builder().file_type("stream")
                        .file_name(name or os.path.basename(path)).file(f).build()).build()
                    return self.client.im.v1.file.create(req)
            resp = _call("上传文件", upload)
        except OSError:
            log.exception("读取文件失败 %s", path)
            resp = None
        if resp is None:
            self.text(message_id, f"日志上传失败，请到服务器查看：{path}")
            return
        self.reply(message_id, "file", {"file_key": resp.data.file_key})

    def alert(self, text: str) -> None:
        """发给管理员群（未配置 alert_chat_id 时只记日志）。"""
        log.warning("ALERT: %s", text)
        if not self.alert_chat_id:
            return
        req = CreateMessageRequest.builder().receive_id_type("chat_id").request_body(
            CreateMessageRequestBody.builder().receive_id(self.alert_chat_id).msg_type("text")
            .content(json.dumps({"text": f"[BT RF 机器人] {text}"[:CARD_TEXT_LIMIT]}, ensure_ascii=False)).build()
        ).build()
        _call("发送告警", lambda: self.client.im.v1.message.create(req))


def code_block(s: str, lang: str = "") -> str:
    return f"```{lang}\n{s.rstrip().replace('```', '` ` `')}\n```"
