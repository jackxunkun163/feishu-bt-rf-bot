#!/usr/bin/env python3
"""通过 Jenkins API 提交 MTK 蓝牙射频参数编译并等待结果（仅依赖 Python 标准库）。

环境变量：
  JENKINS_URL     Jenkins 地址，如 https://jenkins.example.com
  JENKINS_USER    Jenkins 用户名
  JENKINS_TOKEN   该用户的 API Token（用户设置 → API Token）
  JENKINS_JOB     任务路径，如 bt-rf-build 或 文件夹/bt-rf-build
  JENKINS_INSECURE=1  可选，跳过 HTTPS 证书校验（自签名证书时）

用法：
  jenkins_rf.py run --action list   [--project P]
  jenkins_rf.py run --action check  [--project P] --params-file f.txt
  jenkins_rf.py run --action build  [--project P] --params-file f.txt --requester 张三 --request-id <飞书消息ID>
  jenkins_rf.py wait   --build-url URL
  jenkins_rf.py status --build-url URL
  jenkins_rf.py find   --request-id ID
  jenkins_rf.py cancel --build-url URL

run 默认一直等到构建结束，最后在 stdout 输出结果报告（--json 输出 JSON）；进度写到 stderr。
退出码：0 成功；1 构建失败/被取消；2 用法或配置错误；3 等待超时（构建仍在进行，可用 wait 继续等）。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

MAX_PARAMS_CHARS = 8000
RETRYABLE_HTTP = {500, 502, 503, 504}


class JenkinsError(Exception):
    pass


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


class Jenkins:
    def __init__(self, url: str, user: str, token: str, job: str, insecure: bool = False, timeout: float = 30):
        self.base = url.rstrip("/")
        self.job_url = self.base + "".join("/job/" + urllib.parse.quote(p) for p in job.strip("/").split("/")) + "/"
        self.auth = "Basic " + base64.b64encode(f"{user}:{token}".encode()).decode()
        self.ctx = ssl._create_unverified_context() if insecure else None
        self.timeout = timeout
        self.crumb: tuple[str, str] | None = None

    @classmethod
    def from_env(cls) -> "Jenkins":
        missing = [k for k in ("JENKINS_URL", "JENKINS_USER", "JENKINS_TOKEN", "JENKINS_JOB") if not os.environ.get(k)]
        if missing:
            raise JenkinsError(f"缺少环境变量: {', '.join(missing)}")
        return cls(os.environ["JENKINS_URL"], os.environ["JENKINS_USER"], os.environ["JENKINS_TOKEN"],
                   os.environ["JENKINS_JOB"], os.environ.get("JENKINS_INSECURE") == "1")

    def abs(self, url: str) -> str:
        return url if url.startswith("http") else self.base + "/" + url.lstrip("/")

    def request(self, method: str, url: str, data: dict | None = None, retries: int = 4,
                headers: dict | None = None):
        """返回 (status, headers, body)。网络错误和 5xx 自动重试；POST 只在确定未送达时重试由调用方决定。"""
        url = self.abs(url)
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        delay, attempt, crumb_retried = 2.0, 0, False
        while True:
            attempt += 1
            req = urllib.request.Request(url, data=body, method=method)
            req.add_header("Authorization", self.auth)
            for k, v in (headers or {}).items():
                req.add_header(k, v)
            if method == "POST" and self.crumb:
                req.add_header(*self.crumb)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx) as resp:
                    return resp.status, resp.headers, resp.read()
            except urllib.error.HTTPError as e:
                if e.code == 403 and method == "POST" and not crumb_retried and self._fetch_crumb():
                    crumb_retried = True  # 需要 CSRF crumb：取到后立即重发，不计入重试次数
                    attempt -= 1
                    continue
                if e.code in (401, 403):
                    raise JenkinsError(f"Jenkins 认证失败（HTTP {e.code}），请检查 JENKINS_USER / JENKINS_TOKEN 及任务权限")
                if e.code not in RETRYABLE_HTTP or attempt == retries:
                    return e.code, e.headers, e.read()
                log(f"HTTP {e.code}，{delay:.0f}s 后重试: {url}")
            except (urllib.error.URLError, OSError) as e:
                if attempt == retries:
                    raise JenkinsError(f"无法连接 Jenkins: {e}") from e
                log(f"连接失败（{e}），{delay:.0f}s 后重试")
            time.sleep(delay)
            delay = min(delay * 2, 60)

    def _fetch_crumb(self) -> bool:
        try:
            st, _, body = self.request("GET", "/crumbIssuer/api/json", retries=2)
            if st != 200:
                return False
            d = json.loads(body)
            self.crumb = (d["crumbRequestField"], d["crumb"])
            return True
        except (JenkinsError, ValueError, KeyError):
            return False

    def get_json(self, url: str, tree: str | None = None, retries: int = 4):
        url = self.abs(url).rstrip("/") + "/api/json" + (f"?tree={urllib.parse.quote(tree, safe='[],{}')}" if tree else "")
        st, _, body = self.request("GET", url, retries=retries)
        if st == 404:
            return None
        if st != 200:
            raise JenkinsError(f"GET {url} 返回 HTTP {st}")
        return json.loads(body)

    # ------------------------------------------------------------ 业务操作
    def trigger(self, params: dict) -> str:
        """触发构建，返回队列项 URL。"""
        st, headers, body = self.request("POST", self.job_url + "buildWithParameters", data=params, retries=1)
        if st not in (200, 201):
            text = body.decode("utf-8", "replace")[:300]
            if st == 400 and "parameters" in text.lower():
                raise JenkinsError("Jenkins 任务还没有参数定义：请先在 Jenkins 上手动运行一次该任务")
            raise JenkinsError(f"触发构建失败 HTTP {st}: {text}")
        loc = headers.get("Location")
        if not loc:
            raise JenkinsError("Jenkins 未返回队列地址")
        return loc

    def find(self, request_id: str) -> dict | None:
        """按 REQUEST_ID 查找已存在的构建或排队项。"""
        tree = "builds[number,url,building,result,actions[parameters[name,value]]]{0,100}"
        job = self.get_json(self.job_url, tree) or {}
        for b in job.get("builds") or []:
            if _param(b, "REQUEST_ID") == request_id:
                return {"type": "build", "url": b["url"]}
        q = self.get_json("/queue", "items[id,url,task[url],actions[parameters[name,value]]]") or {}
        for it in q.get("items") or []:
            if _param(it, "REQUEST_ID") == request_id and (it.get("task") or {}).get("url", "").rstrip("/") \
                    == self.job_url.rstrip("/"):
                return {"type": "queue", "url": self.abs(it.get("url") or f"queue/item/{it['id']}/")}
        return None

    def wait_queue(self, queue_url: str, request_id: str, deadline: float, poll: float) -> str:
        """等待队列项开始执行，返回构建 URL。"""
        last_why = None
        while time.time() < deadline:
            try:
                item = self.get_json(queue_url)
            except JenkinsError as e:
                log(f"查询队列失败: {e}")
                item = {}
            if item is None:  # 队列项已过期，按 REQUEST_ID 查找
                found = request_id and self.find(request_id)
                if found and found["type"] == "build":
                    return found["url"]
                raise JenkinsError("队列项已消失且找不到对应构建（可能被取消）")
            if item.get("cancelled"):
                raise JenkinsError("构建在队列中被取消")
            ex = item.get("executable")
            if ex and ex.get("url"):
                return ex["url"]
            why = item.get("why")
            if why and why != last_why:
                log(f"排队中：{why}")
                last_why = why
            time.sleep(poll)
        raise TimeoutError("等待开始执行超时")

    def wait_build(self, build_url: str, deadline: float, poll: float) -> dict:
        start, last_note = time.time(), 0.0
        tree = "number,url,building,result,duration,timestamp,description"
        while True:
            try:
                info = self.get_json(build_url, tree)
                if info is None:
                    raise JenkinsError(f"构建不存在: {build_url}")
                if not info.get("building") and info.get("result"):
                    return info
            except JenkinsError as e:
                if "不存在" in str(e):
                    raise
                log(f"查询构建状态失败（{e}），稍后重试")  # Jenkins 重启等情况，继续等
            if time.time() >= deadline:
                raise TimeoutError("等待构建结束超时")
            if time.time() - last_note > 600:
                log(f"仍在执行，已等待 {int(time.time() - start) // 60} 分钟：{build_url}")
                last_note = time.time()
            time.sleep(poll)

    def summary(self, build_url: str) -> dict | None:
        st, _, body = self.request("GET", build_url.rstrip("/") + "/artifact/rf-out/summary.json")
        if st != 200:
            return None
        try:
            return json.loads(body)
        except ValueError:
            return None

    def console_tail(self, build_url: str, max_bytes: int = 6000) -> str:
        url = build_url.rstrip("/") + "/logText/progressiveText"
        st, headers, _ = self.request("GET", f"{url}?start=999999999999")
        size = int(headers.get("X-Text-Size") or 0) if st == 200 else 0
        st, _, body = self.request("GET", f"{url}?start={max(0, size - max_bytes)}")
        return body.decode("utf-8", "replace") if st == 200 else ""

    def cancel(self, url: str) -> str:
        m = re.search(r"/queue/item/(\d+)", url)
        if m:
            self.request("POST", f"/queue/cancelItem?id={m.group(1)}", data={}, retries=2)
            return "已取消排队中的构建"
        st, _, _ = self.request("POST", url.rstrip("/") + "/stop", data={}, retries=2)
        return "已请求停止构建（源码会在 Jenkins 的收尾步骤中自动还原）" if st in (200, 302) else f"停止失败 HTTP {st}"


def _param(obj: dict, name: str):
    for a in obj.get("actions") or []:
        for p in (a or {}).get("parameters") or []:
            if p.get("name") == name:
                return p.get("value")
    return None


# ---------------------------------------------------------------- 结果报告

def build_report(j: Jenkins, info: dict, action: str) -> dict:
    url = info["url"]
    result = info.get("result")
    s = j.summary(url) or {}
    rep = {"ok": result == "SUCCESS", "result": result, "action": action, "build_url": url,
           "number": info.get("number"), "duration_s": round((info.get("duration") or 0) / 1000), "summary": s}
    if result != "SUCCESS" and not s.get("errors") and not s.get("error_summary"):
        rep["console_tail"] = j.console_tail(url)
    return rep


def fmt_report(rep: dict) -> str:
    s = rep.get("summary") or {}
    action = s.get("action") or rep.get("action")
    res = rep.get("result")
    title = {
        ("SUCCESS", "list"): "📋 参数列表",
        ("SUCCESS", "check"): "🔍 参数校验通过（未编译）",
        ("SUCCESS", "build"): "✅ 编译成功",
        ("SUCCESS", "apply"): "✅ 编译成功",
    }.get((res, action)) or {"ABORTED": "⏹ 构建已取消"}.get(res) or f"❌ 失败（{res}）"
    dur = rep.get("duration_s") or 0
    lines = [title, f"构建：{rep['build_url']}（耗时 {dur // 60} 分 {dur % 60} 秒）"]
    if s.get("project"):
        lines.append(f"项目：{s['project']}")
    if s.get("params"):
        lines.append("支持的参数（名称: 当前值）：")
        lines += [f"- {p['name']}: {p['current']}  {p.get('desc') or ''}".rstrip() for p in s["params"]]
    if s.get("changes"):
        lines.append("修改的参数：")
        lines += [f"- {c['label']}: {c['old']} → {c['new']}" for c in s["changes"]]
    if s.get("errors"):
        lines.append("参数错误：")
        lines += [f"- {e}" for e in s["errors"]]
    if s.get("artifacts"):
        lines.append("产物：" + "、".join(s["artifacts"]))
    if s.get("restore_problems"):
        lines.append("⚠️ 源码还原异常，请通知管理员：")
        lines += [f"- {p}" for p in s["restore_problems"]]
    if s.get("error_summary"):
        lines.append("错误摘要：\n" + _clip(s["error_summary"], 3000))
    elif rep.get("console_tail"):
        lines.append("日志末尾：\n" + _clip(rep["console_tail"], 3000))
    if s.get("diff") and action in ("check", "apply", "build"):
        lines.append("Diff：\n" + _clip(s["diff"], 3000))
    return "\n".join(lines)


def _clip(text: str, n: int) -> str:
    text = text.strip()
    return text if len(text) <= n else "...\n" + text[-n:]


# ---------------------------------------------------------------- 命令

def read_params(args) -> str:
    if args.params_file:
        with open(args.params_file, encoding="utf-8-sig") as f:
            return f.read()
    return args.params or ""


def finish(j: Jenkins, build_url: str, action: str, args) -> int:
    deadline = time.time() + args.timeout_hours * 3600
    info = j.wait_build(build_url, deadline, args.poll)
    rep = build_report(j, info, action)
    print(json.dumps(rep, ensure_ascii=False, indent=1) if args.json else fmt_report(rep))
    return 0 if rep["ok"] else 1


def cmd_run(j: Jenkins, args) -> int:
    text = read_params(args).strip()
    if args.action in ("check", "build") and not text:
        raise JenkinsError("check/build 需要参数（--params 或 --params-file）")
    if len(text) > MAX_PARAMS_CHARS:
        raise JenkinsError(f"参数过长（{len(text)} 字符，上限 {MAX_PARAMS_CHARS}）")
    rid = re.sub(r"[^A-Za-z0-9_.\-]", "_", args.request_id or uuid.uuid4().hex)[:100]

    # 同一个请求 ID 已提交过（例如重试、重复消息）：直接跟踪原构建，不重复编译
    existing = args.request_id and j.find(rid)
    if existing:
        log(f"请求 {rid} 已提交过，继续跟踪: {existing['url']}")
        queue_url = existing["url"] if existing["type"] == "queue" else None
        build_url = existing["url"] if existing["type"] == "build" else None
    else:
        params = {"ACTION": args.action, "PROJECT": args.project or "", "BT_PARAMS": text,
                  "REQUESTER": args.requester or "", "REQUEST_ID": rid}
        try:
            queue_url, build_url = j.trigger(params), None
        except JenkinsError as e:
            # 触发请求可能已送达但响应丢失：按请求 ID 确认，避免重复编译
            time.sleep(3)
            found = j.find(rid)
            if not found:
                raise
            log(f"触发响应异常（{e}），但已确认构建已提交")
            queue_url = found["url"] if found["type"] == "queue" else None
            build_url = found["url"] if found["type"] == "build" else None
        log(f"已提交，请求 ID {rid}")

    deadline = time.time() + args.timeout_hours * 3600
    if build_url is None:
        build_url = j.wait_queue(queue_url, rid, deadline, args.poll)
    log(f"构建已开始：{build_url}")
    if args.no_wait:
        print(json.dumps({"build_url": build_url, "request_id": rid}, ensure_ascii=False))
        return 0
    return finish(j, build_url, args.action, args)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def waiting(p):
        p.add_argument("--timeout-hours", type=float, default=8, help="最长等待时间（含排队），默认 8 小时")
        p.add_argument("--poll", type=float, default=20, help="轮询间隔秒数")
        p.add_argument("--json", action="store_true", help="输出 JSON 而不是文字报告")

    p = sub.add_parser("run", help="提交并等待结果")
    p.add_argument("--action", choices=["list", "check", "build"], required=True)
    p.add_argument("--project")
    p.add_argument("--params", help="参数文本，每行一个 名称=值")
    p.add_argument("--params-file", help="参数文件（推荐）")
    p.add_argument("--requester", help="提交人姓名")
    p.add_argument("--request-id", help="请求 ID（建议用飞书消息 ID），相同 ID 不会重复编译")
    p.add_argument("--no-wait", action="store_true", help="构建开始后立即返回构建地址")
    waiting(p)
    for name in ("wait", "status"):
        p = sub.add_parser(name)
        p.add_argument("--build-url", required=True)
        waiting(p)
    p = sub.add_parser("find")
    p.add_argument("--request-id", required=True)
    p = sub.add_parser("cancel")
    p.add_argument("--build-url", required=True, help="构建地址或队列项地址")
    args = ap.parse_args(argv)

    try:
        j = Jenkins.from_env()
        if args.cmd == "run":
            return cmd_run(j, args)
        if args.cmd == "wait":
            return finish(j, args.build_url, "build", args)
        if args.cmd == "status":
            info = j.get_json(args.build_url, "number,url,building,result,duration,description")
            if info is None:
                raise JenkinsError("构建不存在")
            if info.get("building") or not info.get("result"):
                print(f"⏳ 构建进行中：{info['url']}（{info.get('description') or ''}）")
                return 0
            rep = build_report(j, info, "build")
            print(json.dumps(rep, ensure_ascii=False, indent=1) if args.json else fmt_report(rep))
            return 0 if rep["ok"] else 1
        if args.cmd == "find":
            found = j.find(re.sub(r"[^A-Za-z0-9_.\-]", "_", args.request_id))
            print(json.dumps(found, ensure_ascii=False) if found else "未找到")
            return 0 if found else 1
        if args.cmd == "cancel":
            print(j.cancel(args.build_url))
            return 0
    except TimeoutError as e:
        print(f"⏳ {e}，构建可能仍在进行，可稍后用 wait/status 查询", file=sys.stderr)
        return 3
    except JenkinsError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
