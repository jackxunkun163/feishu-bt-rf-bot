"""openclaw/mtk-bt-rf/scripts/jenkins_rf.py：用模拟的 Jenkins 服务器测试。"""
import json
import os
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "openclaw", "mtk-bt-rf", "scripts"))
import jenkins_rf  # noqa: E402


class FakeJenkins:
    def __init__(self, result="SUCCESS", summary=None, console="", require_crumb=False, token="tok"):
        self.result, self.summary, self.console = result, summary, console
        self.require_crumb, self.token = require_crumb, token
        self.triggers = []  # 每次触发的参数
        self.queue_polls = {}
        self.build_polls = {}
        self.stopped = []

    def url(self, path=""):
        return f"http://127.0.0.1:{self.port}/{path.lstrip('/')}"

    def start(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def send(self, code, body=b"", headers=None):
                if isinstance(body, (dict, list)):
                    body = json.dumps(body, ensure_ascii=False).encode()
                elif isinstance(body, str):
                    body = body.encode()
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def authed(self):
                import base64
                ok = self.headers.get("Authorization") == "Basic " + base64.b64encode(
                    f"u:{fake.token}".encode()).decode()
                if not ok:
                    self.send(401)
                return ok

            def do_POST(self):
                if not self.authed():
                    return
                n = int(self.headers.get("Content-Length") or 0)
                form = dict(urllib.parse.parse_qsl(self.rfile.read(n).decode()))
                p = urllib.parse.urlparse(self.path).path
                if p == "/job/bt/buildWithParameters":
                    if fake.require_crumb and self.headers.get("Jenkins-Crumb") != "c1":
                        return self.send(403)
                    fake.triggers.append(form)
                    qid = len(fake.triggers)
                    return self.send(201, headers={"Location": fake.url(f"queue/item/{qid}/")})
                if p.endswith("/stop"):
                    fake.stopped.append(p)
                    return self.send(200)
                self.send(404)

            def do_GET(self):
                if not self.authed():
                    return
                u = urllib.parse.urlparse(self.path)
                p, q = u.path, dict(urllib.parse.parse_qsl(u.query))
                if p == "/crumbIssuer/api/json":
                    return self.send(200, {"crumbRequestField": "Jenkins-Crumb", "crumb": "c1"})
                if p.startswith("/queue/item/"):
                    qid = int(p.split("/")[3])
                    fake.queue_polls[qid] = fake.queue_polls.get(qid, 0) + 1
                    if fake.queue_polls[qid] < 2:
                        return self.send(200, {"why": "等待上一个构建完成"})
                    return self.send(200, {"executable": {"url": fake.url(f"job/bt/{qid}/")}})
                if p == "/queue/api/json":
                    return self.send(200, {"items": []})
                if p == "/job/bt/api/json":
                    builds = [{"number": i, "url": fake.url(f"job/bt/{i}/"),
                               "actions": [{"parameters": [{"name": k, "value": v} for k, v in t.items()]}]}
                              for i, t in enumerate(fake.triggers, 1)]
                    return self.send(200, {"builds": builds[::-1]})
                parts = p.strip("/").split("/")
                if len(parts) >= 3 and parts[:2] == ["job", "bt"] and parts[2].isdigit():
                    num, rest = int(parts[2]), "/".join(parts[3:])
                    if rest == "api/json":
                        fake.build_polls[num] = fake.build_polls.get(num, 0) + 1
                        building = fake.build_polls[num] < 2
                        return self.send(200, {"number": num, "url": fake.url(f"job/bt/{num}/"),
                                               "building": building, "duration": 125000,
                                               "result": None if building else fake.result})
                    if rest == "artifact/rf-out/summary.json":
                        return self.send(200, fake.summary) if fake.summary is not None else self.send(404)
                    if rest == "logText/progressiveText":
                        data = fake.console.encode()
                        start = min(int(q.get("start", 0)), len(data))
                        return self.send(200, data[start:], {"X-Text-Size": str(len(data))})
                self.send(404)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self


@pytest.fixture
def jenkins(monkeypatch):
    servers = []

    def make(**kw):
        f = FakeJenkins(**kw).start()
        servers.append(f)
        monkeypatch.setenv("JENKINS_URL", f.url())
        monkeypatch.setenv("JENKINS_USER", "u")
        monkeypatch.setenv("JENKINS_TOKEN", "tok")
        monkeypatch.setenv("JENKINS_JOB", "bt")
        return f
    yield make
    for s in servers:
        s.server.shutdown()


def run(*argv):
    return jenkins_rf.main(list(argv) + ["--poll", "0.01"])


SUMMARY_OK = {"ok": True, "action": "apply", "project": "demo", "build_result": "SUCCESS",
              "changes": [{"label": "Radio[0]", "old": "0x06", "new": "0x07"}],
              "diff": "-{0x06}\n+{0x07}", "artifacts": ["smb://share/demo/1/"], "restored": True}


def test_build_success(jenkins, capsys, tmp_path):
    f = jenkins(summary=SUMMARY_OK)
    pf = tmp_path / "p.txt"
    pf.write_text("Radio[0]=0x07\n", encoding="utf-8")
    assert run("run", "--action", "build", "--params-file", str(pf), "--requester", "张三",
               "--request-id", "om_123") == 0
    out = capsys.readouterr().out
    assert "✅ 编译成功" in out and "Radio[0]: 0x06 → 0x07" in out and "smb://share/demo/1/" in out
    assert "2 分 5 秒" in out
    t = f.triggers[0]
    assert t["ACTION"] == "build" and t["BT_PARAMS"] == "Radio[0]=0x07" and t["REQUESTER"] == "张三"
    assert t["REQUEST_ID"] == "om_123"


def test_same_request_id_not_rebuilt(jenkins, capsys):
    f = jenkins(summary=SUMMARY_OK)
    assert run("run", "--action", "build", "--params", "Radio[0]=7", "--request-id", "om_1") == 0
    assert run("run", "--action", "build", "--params", "Radio[0]=7", "--request-id", "om_1") == 0
    assert len(f.triggers) == 1
    assert "已提交过" in capsys.readouterr().err


def test_param_error_reported(jenkins, capsys):
    jenkins(result="FAILURE", summary={"ok": False, "action": "check", "errors": ["未知参数 `Foo`"]})
    assert run("run", "--action", "check", "--params", "Foo=1") == 1
    out = capsys.readouterr().out
    assert "❌ 失败（FAILURE）" in out and "未知参数 `Foo`" in out


def test_build_failure_with_error_summary(jenkins, capsys):
    s = dict(SUMMARY_OK, build_result="FAILURE", error_summary="error: undefined reference", artifacts=None)
    jenkins(result="FAILURE", summary=s)
    assert run("run", "--action", "build", "--params", "Radio[0]=7") == 1
    out = capsys.readouterr().out
    assert "undefined reference" in out and "Radio[0]: 0x06 → 0x07" in out


def test_failure_without_summary_uses_console_tail(jenkins, capsys):
    jenkins(result="FAILURE", summary=None, console="x" * 10000 + "\nERROR: agent offline\n")
    assert run("run", "--action", "list") == 1
    out = capsys.readouterr().out
    assert "agent offline" in out and len(out) < 5000


def test_crumb_and_list(jenkins, capsys):
    f = jenkins(require_crumb=True, summary={"ok": True, "action": "list", "project": "demo",
                                              "params": [{"name": "Radio", "current": "{0x06}", "desc": "射频"}]})
    assert run("run", "--action", "list") == 0
    assert len(f.triggers) == 1 and "Radio: {0x06}" in capsys.readouterr().out


def test_auth_and_config_errors(jenkins, monkeypatch, capsys):
    jenkins(token="other")
    assert run("run", "--action", "list") == 2
    assert "认证失败" in capsys.readouterr().err
    monkeypatch.delenv("JENKINS_JOB")
    assert run("run", "--action", "list") == 2
    assert "JENKINS_JOB" in capsys.readouterr().err


def test_requires_params_for_build(jenkins, capsys):
    jenkins()
    assert run("run", "--action", "build", "--params", "  ") == 2


def test_status_find_cancel(jenkins, capsys):
    f = jenkins(summary=SUMMARY_OK)
    assert run("run", "--action", "build", "--params", "Radio[0]=7", "--request-id", "om_9", "--no-wait") == 0
    url = json.loads(capsys.readouterr().out)["build_url"]
    assert jenkins_rf.main(["find", "--request-id", "om_9"]) == 0
    assert url in capsys.readouterr().out
    assert run("status", "--build-url", url) == 0
    assert run("wait", "--build-url", url) == 0
    assert "✅" in capsys.readouterr().out
    assert jenkins_rf.main(["cancel", "--build-url", url]) == 0
    assert f.stopped == ["/job/bt/1/stop"]
