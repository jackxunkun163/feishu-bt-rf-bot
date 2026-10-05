"""btbot.rf：给 Jenkins 调用的命令行。"""
import json
import os
import shutil

import pytest
import yaml

from btbot import rf
from btbot.params import read_text, write_text

HERE = os.path.dirname(__file__)
H = "custom/CFG_BT_Default.h"
RULES = {
    "default_project": "demo",
    "projects": {"demo": {"params": [
        {"name": "Radio", "aliases": ["射频"], "file": H, "kind": "c_array", "anchor": r"/\*\s*Radio\s*\*/"},
        {"name": "BtTxPower", "file": "custom/bt.cfg", "kind": "kv", "type": "int", "min": 0, "max": 15},
    ]}},
}


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "src"
    shutil.copytree(os.path.join(HERE, "fixture_project"), root)
    rules = tmp_path / "rules.yaml"
    rules.write_text(yaml.safe_dump(RULES, allow_unicode=True), encoding="utf-8")
    out = tmp_path / "rf-out"
    params = tmp_path / "params.txt"

    def run(cmd, text=None, *extra):
        if text is not None:
            params.write_text(text, encoding="utf-8")
        argv = [cmd, "--out", str(out)]
        if cmd in ("list", "check", "apply"):
            argv += ["--rules", str(rules), "--root", str(root)]
        if text is not None:
            argv += ["--params-file", str(params)]
        return rf.main(argv + list(extra))

    def summary():
        return json.load(open(out / "summary.json", encoding="utf-8"))

    return run, summary, root, out


def test_list(env):
    run, summary, _, _ = env
    assert run("list") == 0
    s = summary()
    assert s["ok"] and s["project"] == "demo"
    assert {p["name"]: p["current"] for p in s["params"]} == {
        "Radio": "{0x06, 0x80, 0x00, 0x06, 0x03, 0x06}", "BtTxPower": "7"}


def test_check_does_not_modify(env):
    run, summary, root, _ = env
    before = read_text(str(root / H))
    assert run("check", "射频[0]=7\nBtTxPower=9") == 0
    s = summary()
    assert s["ok"] and [c["label"] for c in s["changes"]] == ["Radio[0]", "BtTxPower"]
    assert read_text(str(root / H)) == before


def test_param_errors_written_to_summary(env):
    run, summary, _, _ = env
    assert run("check", "Radio[0]=0x100\nFoo=1") == rf.EXIT_PARAMS
    s = summary()
    assert not s["ok"] and any("Foo" in e for e in s["errors"])
    assert run("check", "项目=other\nRadio[0]=1") == rf.EXIT_PARAMS
    assert "不一致" in summary()["errors"][0]
    assert run("check", "随便说点什么") == rf.EXIT_PARAMS


def test_apply_restore_finish(env, tmp_path):
    run, summary, root, out = env
    before = read_text(str(root / H))
    assert run("apply", "Radio[0]=0x07") == 0
    assert "0x07" in read_text(str(root / H))
    assert (out / "changes.diff").read_text(encoding="utf-8").count("+") >= 1
    # 未还原前不允许再次 apply
    assert run("apply", "Radio[0]=0x08") == rf.EXIT_OTHER

    log = tmp_path / "build.log"
    log.write_text("make: *** [vendor] Error 2\nerror: something broke\n", encoding="utf-8")
    assert run("restore") == 0
    assert read_text(str(root / H)) == before
    assert run("finish", None, "--result", "FAILURE", "--log", str(log)) == 0
    s = summary()
    assert s["restored"] and s["build_result"] == "FAILURE" and "something broke" in s["error_summary"]
    assert s["changes"][0]["new"] == "0x07"  # apply 的内容被保留
    assert run("restore") == 0  # 重复还原无副作用


def test_restore_conflict(env):
    run, summary, root, _ = env
    assert run("apply", "Radio[0]=0x07") == 0
    write_text(str(root / H), read_text(str(root / H)) + "// someone\n")
    assert run("restore") == rf.EXIT_RESTORE
    assert "被其它人修改" in summary()["restore_problems"][0]
    assert "// someone" in read_text(str(root / H))


def test_missing_root(env, tmp_path):
    _, summary, _, out = env
    code = rf.main(["list", "--rules", str(tmp_path / "rules.yaml"), "--root", str(tmp_path / "nope"),
                    "--out", str(out)])
    assert code == rf.EXIT_OTHER and not summary()["ok"]
