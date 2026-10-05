"""参数解析、校验、定位与文件修改。"""
import os
import shutil

import pytest

from btbot.params import ParamError, Rule, current_value, make_plan, parse_message, read_text, write_text
from btbot.rules import load_rules

HERE = os.path.dirname(__file__)
REPO = os.path.dirname(HERE)
H = "custom/CFG_BT_Default.h"

RULES = [
    Rule.from_dict({"name": "Radio", "aliases": ["射频"], "file": H, "kind": "c_array",
                    "anchor": r"/\*\s*Radio\s*\*/"}),
    Rule.from_dict({"name": "TxPWOffset", "file": H, "kind": "c_array", "anchor": r"/\*\s*TxPWOffset\s*\*/"}),
    Rule.from_dict({"name": "Radio0", "file": H, "kind": "c_array", "anchor": r"/\*\s*Radio\s*\*/", "index": 0}),
    Rule.from_dict({"name": "BtTxPower", "file": "custom/bt.cfg", "kind": "kv", "type": "int", "min": 0, "max": 15}),
    Rule.from_dict({"name": "LeTxPower", "file": "custom/bt.cfg", "kind": "regex", "pattern": r"^LeTxPower=(\S+)",
                    "type": "int", "min": -20, "max": 10}),
]


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "src"
    shutil.copytree(os.path.join(HERE, "fixture_project"), r)
    return str(r)


def test_parse_and_plan(root):
    text = "项目：demo\n射频[0] = 0x07\nTxPWOffset={0x80，0x82, 0x81}\nBtTxPower=9；LeTxPower: -3\n随便聊聊"
    project, assigns, errors = parse_message(text, RULES)
    assert project == "demo" and not errors and len(assigns) == 4
    plan = make_plan(assigns, root)
    s = plan.summary()
    assert "Radio[0]: 0x06 → 0x07" in s and "TxPWOffset[1]: 0x80 → 0x82" in s
    assert "BtTxPower: 7 → 9" in s and "LeTxPower: 5 → -3" in s
    new_h = plan.new_texts[os.path.normpath(os.path.join(root, H))]
    assert "{0x07, 0x80, 0x00, 0x06, 0x03, 0x06}" in new_h
    assert "{0x80, /* 1M */ 0x82, 0x81}" in new_h  # 注释保留
    new_cfg = plan.new_texts[os.path.normpath(os.path.join(root, "custom/bt.cfg"))]
    assert new_cfg == "SupportBT5=1\r\nBtTxPower = 9   # dBm\r\nLeTxPower=-3\r\n"  # CRLF 与注释保留
    assert "0x07" not in read_text(os.path.join(root, H))  # make_plan 不写盘


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
def test_validation(root, text, err):
    _, assigns, errors = parse_message(text, RULES)
    assert not errors
    with pytest.raises(ParamError, match=err):
        make_plan(assigns, root)


def test_same_value_twice_ok(root):
    _, assigns, _ = parse_message("Radio[0]=7\nRadio0=0x07", RULES)
    assert len(make_plan(assigns, root).changes) == 1


def test_parse_errors():
    _, _, errors = parse_message("Foo=1\nBtTxPower[1]=2\nRadio0=1,2", RULES)
    assert len(errors) == 3


def test_ambiguous_and_bad_anchor(root):
    dup = Rule.from_dict({"name": "X", "file": H, "kind": "c_array", "anchor": r"/\*"})
    with pytest.raises(ParamError, match="匹配到"):
        current_value(dup, root)
    nested = Rule.from_dict({"name": "Y", "file": H, "kind": "c_array", "anchor": "stBtDefault"})
    with pytest.raises(ParamError, match="嵌套"):
        current_value(nested, root)
    far = Rule.from_dict({"name": "W", "file": H, "kind": "c_array", "anchor": "#ifndef _CFG_BT_D_H"})
    with pytest.raises(ParamError, match="不是"):
        current_value(far, root)


def test_current_value(root):
    assert current_value(RULES[0], root) == "{0x06, 0x80, 0x00, 0x06, 0x03, 0x06}"
    assert current_value(RULES[2], root) == "0x06"
    assert current_value(RULES[3], root) == "7"


def test_write_text_atomic(tmp_path):
    p = tmp_path / "a.h"
    p.write_bytes(b"\xd6\xd0\xce\xc4 GBK\r\n")
    write_text(str(p), read_text(str(p)) + "x")
    assert p.read_bytes() == b"\xd6\xd0\xce\xc4 GBK\r\nx"  # GBK 内容与 CRLF 原样保留
    assert [f.name for f in tmp_path.iterdir()] == ["a.h"]  # 没有残留临时文件


def test_example_rules_load():
    projects, default = load_rules(os.path.join(REPO, "rules.example.yaml"))
    assert default in projects and [r.name for r in projects[default]][:2] == ["Radio", "TxPWOffset"]


def test_rules_errors(tmp_path):
    p = tmp_path / "r.yaml"
    p.write_text("projects:\n  a:\n    params:\n      - {name: X, file: f, kind: kv}\n      - {name: x, file: g, kind: kv}\n",
                 encoding="utf-8")
    with pytest.raises(ValueError, match="重复"):
        load_rules(str(p))
    p.write_text("default_project: b\nprojects:\n  a:\n    params:\n      - {name: X, file: f, kind: kv}\n",
                 encoding="utf-8")
    with pytest.raises(ValueError, match="default_project"):
        load_rules(str(p))
