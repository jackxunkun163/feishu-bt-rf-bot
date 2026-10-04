"""飞书机器人入口：使用长连接（WebSocket）接收消息，无需公网 IP。

    python -m btbot.main -c config.yaml           # 启动
    python -m btbot.main -c config.yaml --check   # 只做配置自检
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import re
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .bot import Bot
from .config import load_config
from .store import InstanceLock, SeenMessages

log = logging.getLogger("btbot")


def extract_text(message) -> str:
    try:
        content = json.loads(message.content or "{}")
    except ValueError:
        return ""
    if message.message_type == "text":
        text = content.get("text", "")
    elif message.message_type == "post":  # 富文本：拼接所有 text 片段
        post = content.get("content") or next((v.get("content") for v in content.values() if isinstance(v, dict)), [])
        text = "\n".join("".join(seg.get("text", "") for seg in para if isinstance(seg, dict)
                                 and seg.get("tag") in ("text", "a")) for para in post or [])
    else:
        return ""
    for m in message.mentions or []:
        text = text.replace(m.key, "")
    return re.sub(r"@_user_\d+", "", text).strip()


def setup_logging(workdir: str, verbose: bool) -> None:
    os.makedirs(workdir, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(name)s: %(message)s")
    fh = logging.handlers.RotatingFileHandler(os.path.join(workdir, "bot.log"), maxBytes=20 * 1024 * 1024,
                                              backupCount=5, encoding="utf-8")
    sh = logging.StreamHandler()
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for h in (fh, sh):
        h.setFormatter(fmt)
        root.addHandler(h)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--check", action="store_true", help="只检查配置，不启动")
    args = ap.parse_args()

    try:
        cfg = load_config(args.config)
    except (OSError, ValueError, TypeError) as e:
        sys.exit(f"配置文件错误: {e}")
    problems = cfg.check()
    if args.check:
        print("\n".join(problems) or "配置检查通过")
        sys.exit(1 if problems else 0)
    if not cfg.app_id or not cfg.app_secret:
        sys.exit("缺少 feishu.app_id / app_secret")

    setup_logging(cfg.workdir, args.verbose)
    instance_lock = InstanceLock(os.path.join(cfg.workdir, "bot.lock"))  # 必须保持引用，对象被回收锁就释放了
    instance_lock.acquire()

    import lark_oapi as lark
    from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

    from .feishu import Messenger

    client = lark.Client.builder().app_id(cfg.app_id).app_secret(cfg.app_secret).build()
    messenger = Messenger(client, cfg.alert_chat_id)
    bot = Bot(cfg, messenger)
    seen = SeenMessages(os.path.join(cfg.workdir, "seen.json"))
    pool = ThreadPoolExecutor(cfg.limits.handler_threads, thread_name_prefix="handler")
    max_age_ms = cfg.limits.max_message_age_minutes * 60 * 1000

    if problems:
        messenger.alert("启动自检发现问题：\n" + "\n".join(problems))
    messenger.alert(f"机器人已启动，项目: {', '.join(cfg.projects)}\n{bot.worker.status()}")

    def on_message(data: P2ImMessageReceiveV1) -> None:
        # 事件回调必须快速返回且不能抛异常，否则飞书会重推
        try:
            msg = data.event.message
            if not seen.add(msg.message_id):
                return
            age = time.time() * 1000 - int(msg.create_time or 0)
            if age > max_age_ms:
                log.warning("忽略过期消息 %s（%.0f 秒前）", msg.message_id, age / 1000)
                return
            sender = data.event.sender.sender_id.open_id
            text = extract_text(msg)
            log.info("收到消息 from=%s chat=%s type=%s: %r", sender, msg.chat_type, msg.message_type, text[:200])
            if not text:
                pool.submit(messenger.text, msg.message_id, "请发送文本格式的参数，发送 /help 查看帮助。")
                return
            pool.submit(bot.handle, text, sender, msg.message_id)
        except Exception:  # noqa: BLE001
            log.exception("事件处理异常")

    stopping = threading.Event()

    def shutdown(signum=None, frame=None):
        if stopping.is_set():
            return
        stopping.set()
        log.info("收到退出信号，正在终止编译并还原源码...")
        bot.worker.shutdown()
        pool.shutdown(wait=False, cancel_futures=True)
        messenger.alert("机器人已停止，未完成的任务会在下次启动后继续")
        logging.shutdown()
        os._exit(0)

    signal.signal(signal.SIGINT, shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, shutdown)

    handler = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(on_message).build()
    delay = 5
    while not stopping.is_set():
        try:
            log.info("连接飞书长连接...")
            ws = lark.ws.Client(cfg.app_id, cfg.app_secret, event_handler=handler, auto_reconnect=True,
                                log_level=lark.LogLevel.DEBUG if args.verbose else lark.LogLevel.INFO)
            ws.start()  # 正常情况下不会返回，SDK 内部自动断线重连
            log.warning("长连接退出，%d 秒后重连", delay)
        except KeyboardInterrupt:
            shutdown()
        except Exception:  # noqa: BLE001
            log.exception("长连接异常，%d 秒后重连", delay)
        time.sleep(delay)
        delay = min(delay * 2, 300)


if __name__ == "__main__":
    main()
