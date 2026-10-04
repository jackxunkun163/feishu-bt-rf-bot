"""本地调试：不连飞书，在终端模拟收到一条消息。

    python -m btbot.cli -c config.yaml "/check Radio[0]=0x07"
    python -m btbot.cli -c config.yaml "Radio[0]=0x07"      # 真实修改并编译
"""
from __future__ import annotations

import argparse
import logging
import time

from .bot import Bot
from .config import load_config


class ConsoleNotifier:
    def text(self, message_id, text):
        print(f"\n[回复] {text}")

    def card(self, message_id, title, markdown, color="blue"):
        print(f"\n[卡片:{color}] {title}\n{markdown}")

    def file(self, message_id, path, name=None):
        print(f"\n[文件] {path}")

    def alert(self, text):
        print(f"\n[告警] {text}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("--user", default="local")
    ap.add_argument("text")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO)
    cfg = load_config(args.config)
    cfg.allowed_users = []
    bot = Bot(cfg, ConsoleNotifier())
    bot.handle(args.text.replace("\\n", "\n"), args.user, "local-msg")
    try:
        while bot.worker.current or bot.worker.pending:
            time.sleep(1)
    except KeyboardInterrupt:
        bot.worker.shutdown()


if __name__ == "__main__":
    main()
