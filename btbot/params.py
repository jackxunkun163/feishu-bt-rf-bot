"""蓝牙射频参数：解析用户消息、校验、生成文件修改。

支持三种修改方式（在 config.yaml 中按参数配置 kind）：
  c_array : C 源码中的数组，如 CFG_BT_Default.h 里 /* Radio */ 后面的 {0x06, 0x80, ...}
  kv      : key=value 形式的配置文件，如 bt.cfg / WMT_SOC.cfg
  regex   : 任意文本，用带一个捕获组的正则定位要替换的值
"""
from __future__ import annotations

import difflib
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field

PROJECT_KEYS = {"project", "项目", "工程"}

_LINE_RE = re.compile(
    r"^\s*([A-Za-z_一-龥][\w一-龥.\-]*)\s*(?:\[\s*(\d+)\s*\])?\s*[=:：]\s*(.+?)\s*$"
)
_NUM_RE = re.compile(r"[-+]?0[xX][0-9a-fA-F]+|[-+]?\d+(?:\.\d+)?")


class ParamError(Exception):
    pass


@dataclass
class Rule:
    name: str
    file: str
    kind: str  # c_array | kv | regex
    type: str = "byte"  # byte | int | float | string
    desc: str = ""
    aliases: list[str] = field(default_factory=list)
    anchor: str | None = None  # c_array: 定位数组的正则，数组 {...} 紧随其后
    index: int | None = None  # c_array: 只修改某个元素
    key: str | None = None  # kv: 键名
    pattern: str | None = None  # regex: 含一个捕获组
    min: float | None = None
    max: float | None = None
    allowed: str | None = None  # string 类型允许的正则
    fmt: str | None = None  # hex | dec，默认 byte 用 hex，其它用 dec

    @classmethod
    def from_dict(cls, d: dict) -> "Rule":
        known = set(cls.__dataclass_fields__)
        unknown = set(d) - known
        if unknown:
            raise ValueError(f"参数 {d.get('name')} 含未知配置项: {', '.join(sorted(unknown))}")
        r = cls(**d)
        if r.kind not in ("c_array", "kv", "regex"):
            raise ValueError(f"参数 {r.name}: kind 必须是 c_array/kv/regex")
        if r.kind == "c_array" and not r.anchor:
            raise ValueError(f"参数 {r.name}: c_array 需要 anchor")
        if r.kind == "kv" and not r.key:
            r.key = r.name
        if r.kind == "regex" and (not r.pattern or re.compile(r.pattern).groups != 1):
            raise ValueError(f"参数 {r.name}: regex 需要恰好一个捕获组的 pattern")
        return r

    def matches(self, name: str) -> bool:
        n = name.lower()
        return n == self.name.lower() or any(n == a.lower() for a in self.aliases)


@dataclass
class Assignment:
    """用户的一条输入，例如 Radio[0]=0x07 或 TxPWOffset=0x80,0x80,0x80"""
    rule: Rule
    index: int | None
    values: list[str]
    line: str


@dataclass
class Change:
    rule: Rule
    label: str
    old: str
    new: str


@dataclass
class Plan:
    changes: list[Change]
    new_texts: dict[str, str]  # 绝对路径 -> 修改后的内容
    old_texts: dict[str, str]
    root: str

    def diff(self) -> str:
        out = []
        for path, new in self.new_texts.items():
            rel = os.path.relpath(path, self.root).replace("\\", "/")
            out.extend(difflib.unified_diff(
                self.old_texts[path].splitlines(keepends=True), new.splitlines(keepends=True),
                f"a/{rel}", f"b/{rel}", n=1))
        return "".join(out)

    def summary(self) -> str:
        return "\n".join(f"{c.label}: {c.old} → {c.new}" for c in self.changes)


# ---------------------------------------------------------------- 消息解析

def parse_message(text: str, rules: list[Rule]) -> tuple[str | None, list[Assignment], list[str]]:
    """返回 (项目名, 赋值列表, 错误列表)。无法识别的行忽略，未知参数名报错。"""
    project = None
    assigns: list[Assignment] = []
    errors: list[str] = []
    for raw in re.split(r"[\r\n;；]+", text):
        line = raw.strip()
        m = _LINE_RE.match(line)
        if not m:
            continue
        name, idx, value = m.group(1), m.group(2), m.group(3)
        if name.lower() in PROJECT_KEYS:
            project = value.strip()
            continue
        rule = next((r for r in rules if r.matches(name)), None)
        if rule is None:
            errors.append(f"未知参数 `{name}`（发送 /params 查看支持的参数）")
            continue
        values = [v for v in re.split(r"[,，\s]+", value.strip().strip("{}[]()").strip()) if v]
        if rule.kind != "c_array" or rule.index is not None or idx is not None:
            if len(values) != 1:
                errors.append(f"`{line}`: 只能填一个值")
                continue
        if idx is not None and (rule.kind != "c_array" or rule.index is not None):
            errors.append(f"`{line}`: 参数 {rule.name} 不支持下标")
            continue
        assigns.append(Assignment(rule, int(idx) if idx is not None else None, values, line))
    return project, assigns, errors


# ---------------------------------------------------------------- 值校验与格式化

def _parse_number(s: str) -> float:
    s = s.strip()
    if not re.fullmatch(r"[-+]?(0[xX][0-9a-fA-F]+|\d+(\.\d+)?)", s):  # 拒绝 nan/inf/1e9 等
        raise ParamError(f"`{s}` 不是合法数字")
    try:
        if re.fullmatch(r"[-+]?0[xX][0-9a-fA-F]+", s):
            return int(s, 16)
        if re.fullmatch(r"[-+]?\d+", s):
            return int(s)
        return float(s)
    except ValueError:
        raise ParamError(f"`{s}` 不是合法数字")


def format_value(rule: Rule, raw: str) -> str:
    """校验并格式化成写入文件的文本。"""
    if rule.type == "string":
        if rule.allowed and not re.fullmatch(rule.allowed, raw):
            raise ParamError(f"{rule.name}: `{raw}` 不符合格式 {rule.allowed}")
        if not re.fullmatch(r"[^\r\n\"'`$\\]*", raw):
            raise ParamError(f"{rule.name}: `{raw}` 含非法字符")
        return raw
    v = _parse_number(raw)
    if rule.type in ("byte", "int"):
        if v != int(v):
            raise ParamError(f"{rule.name}: `{raw}` 必须是整数")
        v = int(v)
    lo = rule.min if rule.min is not None else (0 if rule.type == "byte" else None)
    hi = rule.max if rule.max is not None else (255 if rule.type == "byte" else None)
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        raise ParamError(f"{rule.name}: `{raw}` 超出范围 [{_fmt_bound(lo)}, {_fmt_bound(hi)}]")
    fmt = rule.fmt or ("hex" if rule.type == "byte" else "dec")
    if fmt == "hex" and rule.type != "float":
        return f"0x{v:02X}" if v >= 0 else f"-0x{-v:02X}"
    return str(v)


def _fmt_bound(b):
    return "-∞/∞" if b is None else (f"{b:g}" if isinstance(b, float) else str(b))


# ---------------------------------------------------------------- 文件定位

def read_text(path: str) -> str:
    # surrogateescape + newline="" 保证 GBK 注释、CRLF 等原样写回
    with open(path, encoding="utf-8", errors="surrogateescape", newline="") as f:
        return f.read()


def write_text(path: str, text: str) -> None:
    """原子写：先写同目录临时文件再替换，进程中途被杀也不会留下半个文件。"""
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".btbot-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if os.path.exists(path):
            shutil.copymode(path, tmp)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _unique_match(rule: Rule, pattern: str, text: str, flags=0, what="anchor") -> re.Match:
    ms = list(re.finditer(pattern, text, flags))
    if not ms:
        raise ParamError(f"{rule.name}: 在 {rule.file} 中找不到{what} `{pattern}`")
    if len(ms) > 1:
        lines = ", ".join(str(text.count("\n", 0, m.start()) + 1) for m in ms[:5])
        raise ParamError(f"{rule.name}: {what}在 {rule.file} 中匹配到 {len(ms)} 处（行 {lines}），请把规则写得更精确")
    return ms[0]


def _array_spans(rule: Rule, text: str) -> list[tuple[int, int]]:
    """返回 c_array 每个元素数值字面量在 text 中的 (start, end)。"""
    m = _unique_match(rule, rule.anchor, text)
    # anchor 与 { 之间只允许空白、注释、= 号，防止 anchor 写错时改到别处
    gap = re.match(r"(?:\s|=|/\*.*?\*/|//[^\n]*)*\{", text[m.end():], re.S)
    if not gap:
        raise ParamError(f"{rule.name}: anchor 后面紧跟的不是 {{...}} 数组")
    lb = m.end() + gap.end() - 1
    rb = text.find("}", lb)
    if rb < 0 or "{" in text[lb + 1:rb]:
        raise ParamError(f"{rule.name}: anchor 后的数组不完整或含嵌套 {{}}")
    spans, pos = [], lb + 1
    for piece in text[lb + 1:rb].split(","):
        clean = re.sub(r"/\*.*?\*/|//[^\n]*", lambda x: " " * len(x.group()), piece, flags=re.S)
        n = _NUM_RE.search(clean)
        if n:
            spans.append((pos + n.start(), pos + n.end()))
        pos += len(piece) + 1
    return spans


def _scalar_span(rule: Rule, text: str) -> tuple[int, int]:
    if rule.kind == "kv":
        pat = rf"^[ \t]*{re.escape(rule.key)}[ \t]*=[ \t]*([^\r\n#;]*?)[ \t]*(?:[#;].*)?$"
    else:
        pat = rule.pattern
    return _unique_match(rule, pat, text, re.M, "参数").span(1)


def _targets(rule: Rule, index: int | None, text: str) -> tuple[list[tuple[int, int]], list[str]]:
    """返回要替换的 span 列表及对应的显示标签。"""
    if rule.kind != "c_array":
        return [_scalar_span(rule, text)], [rule.name]
    spans = _array_spans(rule, text)
    i = rule.index if rule.index is not None else index
    if i is None:
        return spans, [f"{rule.name}[{k}]" for k in range(len(spans))]
    if i >= len(spans):
        raise ParamError(f"{rule.name}: 下标 {i} 越界（数组长度 {len(spans)}）")
    return [spans[i]], [rule.name if rule.index is not None else f"{rule.name}[{i}]"]


def current_value(rule: Rule, root: str) -> str:
    text = read_text(os.path.join(root, rule.file))
    spans, _ = _targets(rule, None, text)
    vals = [text[a:b] for a, b in spans]
    return vals[0] if rule.kind != "c_array" or rule.index is not None else "{" + ", ".join(vals) + "}"


# ---------------------------------------------------------------- 生成修改计划

def make_plan(assigns: list[Assignment], root: str) -> Plan:
    """校验全部输入并在内存中生成修改后的文件内容，不写盘。任何一条出错都抛 ParamError。"""
    errors: list[str] = []
    old_texts: dict[str, str] = {}
    edits: dict[str, dict[tuple[int, int], tuple[str, Change]]] = {}

    for a in assigns:
        path = os.path.normpath(os.path.join(root, a.rule.file))
        try:
            if path not in old_texts:
                if not os.path.isfile(path):
                    raise ParamError(f"{a.rule.name}: 文件不存在 {a.rule.file}")
                old_texts[path] = read_text(path)
            text = old_texts[path]
            spans, labels = _targets(a.rule, a.index, text)
            if len(a.values) != len(spans):
                raise ParamError(f"`{a.line}`: 需要 {len(spans)} 个值，实际给了 {len(a.values)} 个")
            for span, label, raw in zip(spans, labels, a.values):
                new = format_value(a.rule, raw)
                prev = edits.setdefault(path, {}).get(span)
                if prev and prev[0] != new:
                    raise ParamError(f"{label} 被设置了两个不同的值: {prev[0]} 和 {new}")
                edits[path][span] = (new, Change(a.rule, label, text[span[0]:span[1]], new))
        except ParamError as e:
            errors.append(str(e))
    if errors:
        raise ParamError("\n".join(errors))
    if not edits:
        raise ParamError("没有识别到要修改的参数")

    new_texts, changes = {}, []
    for path, by_span in edits.items():
        text = old_texts[path]
        for (a, b), (new, _) in sorted(by_span.items(), reverse=True):
            text = text[:a] + new + text[b:]
        new_texts[path] = text
        changes.extend(c for _, (_, c) in sorted(by_span.items()))
    return Plan(changes, new_texts, {p: old_texts[p] for p in new_texts}, root)
