import json
import os
import shutil
import sys
import threading
import time

import pytest
import yaml

from btbot.bot import Bot
from btbot.config import load_config
from btbot.params import ParamError, Rule, current_value, make_plan, parse_message, read_text, write_text
from btbot.store import SeenMessages, save_json
from btbot.worker import Job, Worker

HERE = os.path.dirname(__file__)
H = "custom/CFG_BT_Default.h"

# 模拟编译脚本：根据头文件里的值决定行为
BUILD_PY = r'''
import sys, time
t = open("custom/CFG_BT_Default.h").read()
print("building")
if "0x09" in t: print("error: fake failure"); sys.exit(2)
if "0x0A" in t: open("custom/CFG_BT_Default.h", "a").write("// edited by someone\n")
if "0x0B" in t: time.sleep(30)
'''


def make_cfg(tmp_path, **project_overrides):
    root = tmp_path / "src"
    if not root.exists():
        shutil.copytree(os.path.join(HERE, "fixture_project"), root)
        (root / "build.py").write_text(BUILD_PY)
    params = [
        {"name": "Radio", "aliases": ["射频"], "file": H, "kind": "c_array", "anchor": r"/\*\s*Radio\s*\*/"},
        {"name": "TxPWOffset", "file": H, "kind": "c_array", "anchor": r"/\*\s*TxPWOffset\s*\*/"},
        {"name": "Radio0", "file": H, "kind": "c_array", "anchor": r"/\*\s*Radio\s*\*/", "index": 0},
        {"name": "BtTxPower", "file": "custom/bt.cfg", "kind": "kv", "type": "int", "min": 0, "max": 15},
        {"name": "LeTxPower", "file": "custom/bt.cfg", "kind": "regex", "pattern": r"^LeTxPower=(\S+)",
         "type": "int", "min": -20, "max": 10},
    ]
    project = {"root": str(root), "build_command": f'"{sys.executable}" build.py', "timeout_minutes": 1,
               "artifacts": ["custom/*.cfg"], "params": [params], **project_overrides}
    data = {"feishu": {"app_id": "x", "app_secret": "y"}, "workdir": str(tmp_path / "data"),
            "limits": {"max_jobs_per_user": 2, "max_queue": 3}, "projects": {"demo": project}}
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return load_config(str(p))


@pytest.fixture
def cfg(tmp_path):
    return make_cfg(tmp_path)


def hpath(cfg):
    return os.path.join(cfg.project(None).root, H)


# ------------------------------------------------------------------ 参数解析/校验

def test_parse_and_plan(cfg):
    proj = cfg.project(None)
    text = "项目：demo\n射频[0] = 0x07\nTxPWOffset={0x80，0x82, 0x81}\nBtTxPower=9；LeTxPower: -3\n随便聊聊"
    project, assigns, errors = parse_message(text, proj.params)
    assert project == "demo" and not errors and len(assigns) == 4
    plan = make_plan(assigns, proj.root)
    s = plan.summary()
    assert "Radio[0]: 0x06 → 0x07" in s and "TxPWOffset[1]: 0x80 → 0x82" in s
    assert "BtTxPower: 7 → 9" in s and "LeTxPower: 5 → -3" in s
    new_h = plan.new_texts[os.path.normpath(hpath(cfg))]
    assert "{0x07, 0x80, 0x00, 0x06, 0x03, 0x06}" in new_h
    assert "{0x80, /* 1M */ 0x82, 0x81}" in new_h
    new_cfg = plan.new_texts[os.path.normpath(os.path.join(proj.root, "custom/bt.cfg"))]
    assert new_cfg == "SupportBT5=1\r\nBtTxPower = 9   # dBm\r\nLeTxPower=-3\r\n"  # CRLF 与注释保留
    assert "0x07" not in read_text(hpath(cfg))  # 未写盘


@pytest.mark.parametrize("text,err", [
    ("Radio[0]=0x100", "超出范围"),
    ("Radio[9]=1", "越界"),
    ("Radio=1,2", "需要 6 个值"),
    ("BtTxPower=16", "超出范围"),
    ("BtTxPower=1.5", "整数"),
    ("Radio0=abc", "不是合法数字"),
    ("BtTxPower=nan", "不是合法数字"),
    ("BtTxPower=inf", "不是合法数字"),
    ("Radio[0]=1\nRadio0=2", "两个不同的值"),
])
def test_validation(cfg, text, err):
    proj = cfg.project(None)
    _, assigns, errors = parse_message(text, proj.params)
    assert not errors
    with pytest.raises(ParamError, match=err):
        make_plan(assigns, proj.root)


def test_same_value_twice_ok(cfg):
    proj = cfg.project(None)
    _, assigns, _ = parse_message("Radio[0]=7\nRadio0=0x07", proj.params)
    assert len(make_plan(assigns, proj.root).changes) == 1


def test_ambiguous_and_bad_anchor(cfg):
    root = cfg.project(None).root
    dup = Rule.from_dict({"name": "X", "file": H, "kind": "c_array", "anchor": r"/\*"})
    with pytest.raises(ParamError, match="匹配到"):
        current_value(dup, root)
    nested = Rule.from_dict({"name": "Y", "file": H, "kind": "c_array", "anchor": "stBtDefault"})
    with pytest.raises(ParamError, match="嵌套"):
        current_value(nested, root)
    far = Rule.from_dict({"name": "W", "file": H, "kind": "c_array", "anchor": "#ifndef _CFG_BT_D_H"})
    with pytest.raises(ParamError, match="不是"):
        current_value(far, root)


def test_parse_errors(cfg):
    proj = cfg.project(None)
    _, _, errors = parse_message("Foo=1\nBtTxPower[1]=2\nRadio0=1,2", proj.params)
    assert len(errors) == 3


def test_current_value(cfg):
    proj = cfg.project(None)
    assert current_value(proj.params[0], proj.root) == "{0x06, 0x80, 0x00, 0x06, 0x03, 0x06}"
    assert current_value(proj.params[2], proj.root) == "0x06"
    assert current_value(proj.params[3], proj.root) == "7"


def test_config_check(tmp_path):
    cfg = make_cfg(tmp_path)
    assert cfg.check() == []
    cfg.projects["demo"].params.append(Rule.from_dict({"name": "Z", "file": "nope.h", "kind": "kv"}))
    assert any("Z" in p for p in cfg.check())


def test_write_text_atomic(tmp_path):
    p = tmp_path / "a.h"
    p.write_bytes(b"\xd6\xd0\xce\xc4 GBK\r\n")
    write_text(str(p), read_text(str(p)) + "x")
    assert p.read_bytes() == b"\xd6\xd0\xce\xc4 GBK\r\nx"
    assert [f.name for f in tmp_path.iterdir()] == ["a.h"]


def test_seen_messages_persist(tmp_path):
    s = SeenMessages(str(tmp_path / "seen.json"), keep=3)
    assert s.add("a") and not s.add("a")
    for m in "bcd":
        s.add(m)
    s2 = SeenMessages(str(tmp_path / "seen.json"), keep=3)
    assert not s2.add("d") and s2.add("a")  # a 已被淘汰


# ------------------------------------------------------------------ 端到端

class Rec:
    def __init__(self):
        self.msgs = []

    def text(self, mid, text):
        self.msgs.append(("text", text))

    def card(self, mid, title, md, color="blue"):
        self.msgs.append((color, title + "\n" + md))

    def file(self, mid, path, name=None):
        self.msgs.append(("file", path))

    def alert(self, text):
        self.msgs.append(("alert", text))


def wait_idle(bot, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        if not (bot.worker.current or bot.worker.pending):
            return
        time.sleep(0.1)
    raise AssertionError("worker 未空闲")


def run(cfg, text, user="ou_1"):
    rec = Rec()
    bot = Bot(cfg, rec)
    bot.handle(text, user, "m1")
    wait_idle(bot)
    return rec.msgs


def test_build_success_and_restore(cfg):
    before = read_text(hpath(cfg))
    msgs = run(cfg, "Radio[0]=0x07")
    assert msgs[-1][0] == "green" and "编译成功" in msgs[-1][1] and "bt.cfg" in msgs[-1][1]
    assert read_text(hpath(cfg)) == before
    assert not os.path.exists(os.path.join(cfg.workdir, "journal.json"))
    hist = [json.loads(l) for l in open(os.path.join(cfg.workdir, "history.jsonl"), encoding="utf-8")]
    assert hist[-1]["ok"] is True


def test_build_failure_uploads_log(cfg):
    msgs = run(cfg, "Radio[1]=0x09")
    colors = [m[0] for m in msgs]
    assert "red" in colors and colors[-1] == "file" and msgs[-1][1].endswith(".log.gz")
    red = next(m[1] for m in msgs if m[0] == "red")
    assert "退出码 2" in red and "fake failure" in red


def test_build_cannot_start(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.projects["demo"].root  # 存在
    cfg.projects["demo"].build_command = "definitely-not-a-command-xyz"
    msgs = run(cfg, "Radio[0]=7")
    assert any(m[0] == "red" for m in msgs)
    assert "0x07" not in read_text(hpath(cfg))


def test_conflict_not_overwritten_and_blocks(cfg):
    msgs = run(cfg, "Radio[0]=0x0A")  # 编译脚本会在编译期间改动头文件
    assert any(m[0] == "alert" and "被其它人修改" in m[1] for m in msgs)
    assert "edited by someone" in read_text(hpath(cfg))  # 没有被覆盖
    bot = Bot(cfg, Rec())
    bot.worker.blocked = "x"
    bot.handle("Radio[0]=7", "ou_1", "m2")
    assert "暂停" in bot.n.msgs[-1][1]
    bot.handle("/unblock", "ou_1", "m3")
    assert bot.worker.blocked is None


def test_disk_space_check(tmp_path):
    cfg = make_cfg(tmp_path, min_free_gb=10 ** 9)
    msgs = run(cfg, "Radio[0]=7")
    assert any("磁盘空间不足" in m[1] for m in msgs)


def test_per_user_and_queue_limits(cfg):
    rec = Rec()
    bot = Bot(cfg, rec, start_worker=False)  # 不消费，便于观察排队
    for i in range(3):
        bot.handle(f"Radio[0]={i}", "ou_1", f"m{i}")
    assert "已有 2 个任务" in rec.msgs[-1][1]
    bot.handle("Radio[0]=1", "ou_2", "x")
    bot.handle("Radio[0]=1", "ou_3", "y")
    assert "上限" in rec.msgs[-1][1]
    assert len(json.load(open(os.path.join(cfg.workdir, "queue.json")))) == 3


def test_cancel_running(cfg):
    rec = Rec()
    bot = Bot(cfg, rec)
    bot.handle("Radio[0]=0x0B", "ou_1", "m1")  # 长时间编译
    while not (bot.worker.current and bot.worker.builder._proc):
        time.sleep(0.05)
    bot.handle("/cancel", "ou_1", "m2")
    wait_idle(bot)
    assert any("已取消" in m[1] for m in rec.msgs if m[0] == "red")
    assert "0x0B" not in read_text(hpath(cfg))


def test_graceful_shutdown_requeues(cfg):
    rec = Rec()
    bot = Bot(cfg, rec)
    bot.handle("Radio[0]=0x0B", "ou_1", "m1")
    while not (bot.worker.current and bot.worker.builder._proc):
        time.sleep(0.05)
    bot.worker.shutdown(timeout=30)
    assert "0x0B" not in read_text(hpath(cfg))
    q = json.load(open(os.path.join(cfg.workdir, "queue.json")))
    assert len(q) == 1 and q[0]["attempts"] == 0
    assert any("重启后会自动重新编译" in m[1] for m in rec.msgs)


def test_crash_recovery(cfg):
    """模拟编译中途进程被 kill：源码已改、journal 存在、队列里 running 的任务。"""
    proj = cfg.project(None)
    _, assigns, _ = parse_message("Radio[0]=0x07", proj.params)
    plan = make_plan(assigns, proj.root)
    w = Worker(cfg, Rec())
    job = Job("demo", "Radio[0]=0x07", "ou_1", "m1")
    w._write_journal(job, plan, os.path.join(cfg.workdir, "jobs", job.id))
    for p, t in plan.new_texts.items():
        write_text(p, t)
    w.current = job
    w._persist()
    assert "0x07" in read_text(hpath(cfg))

    rec = Rec()
    w2 = Worker(cfg, rec)  # 重启
    assert "0x07" not in read_text(hpath(cfg))
    assert not os.path.exists(os.path.join(cfg.workdir, "journal.json"))
    assert len(w2.pending) == 1 and w2.pending[0].attempts == 1
    assert any("重新排队" in m[1] for m in rec.msgs)

    # 再崩一次：超过重试次数后放弃
    w2.current = w2.pending.pop(0)
    w2._persist()
    rec3 = Rec()
    w3 = Worker(cfg, rec3)
    assert not w3.pending and any("请重新提交" in m[1] for m in rec3.msgs)


def test_check_and_commands(cfg):
    assert run(cfg, "/check\nRadio[0]=7")[-1][0] == "grey"
    assert "当前值" in run(cfg, "/params")[-1][1]
    assert "空闲" in run(cfg, "/status")[-1][1]
    assert "未知参数" in run(cfg, "Foo=1")[-1][1]
    assert "过长" in run(cfg, "Radio[0]=1\n" * 2000)[-1][1]


def test_handler_never_raises(cfg, monkeypatch):
    rec = Rec()
    bot = Bot(cfg, rec, start_worker=False)
    monkeypatch.setattr(bot, "_handle", lambda *a: 1 / 0)
    bot.handle("x", "u", "m")
    assert "出错" in rec.msgs[0][1] and rec.msgs[1][0] == "alert"


# ------------------------------------------------------------------ 平台相关（CI 在 Linux/Windows 上都会跑）

REPO_ROOT = os.path.dirname(HERE)
linux_only = pytest.mark.skipif(os.name == "nt", reason="仅 Linux")


def test_instance_lock(tmp_path):
    import subprocess
    from btbot.store import InstanceLock
    lock = str(tmp_path / "bot.lock")
    held = InstanceLock(lock)
    held.acquire()
    r = subprocess.run([sys.executable, "-c", f"from btbot.store import InstanceLock; InstanceLock({lock!r}).acquire()"],
                       cwd=REPO_ROOT, capture_output=True, encoding="utf-8", errors="replace",
                       env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    assert r.returncode != 0 and "已有另一个机器人实例" in r.stderr


@linux_only
def test_kill_stale_process_group():
    import subprocess
    from btbot import builder as B
    p = subprocess.Popen(["/bin/bash", "-c", "sleep 60 & sleep 60"], start_new_session=True)
    threading.Thread(target=p.wait, daemon=True).start()  # 及时回收，模拟真实场景中由 init 回收
    token = B.proc_token(p.pid)
    assert token
    assert not B.kill_stale(p.pid, "wrong-token")  # PID 被复用时不能误杀
    assert p.poll() is None
    start = time.time()
    assert B.kill_stale(p.pid, token)
    assert time.time() - start < 15
    assert p.wait(timeout=5) is not None
