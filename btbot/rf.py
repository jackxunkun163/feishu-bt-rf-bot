"""BT 射频参数命令行工具，给 Jenkins 等编译流水线调用（只依赖 PyYAML）。

    python -m btbot.rf list    --rules rules.yaml --project k6789 --root $SRC
    python -m btbot.rf check   --rules rules.yaml --project k6789 --root $SRC --params-file params.txt --out rf-out
    python -m btbot.rf apply   --rules rules.yaml --project k6789 --root $SRC --params-file params.txt --out rf-out
    python -m btbot.rf restore --out rf-out
    python -m btbot.rf finish  --out rf-out --result FAILURE --log rf-out/build.log

apply 会把原文件备份到 <out>/backup，编译结束后用 restore 还原（建议放在流水线的 post/always 里）。
结果写入 <out>/summary.json，供调用方（如小龙虾技能）读取：
    {"ok": bool, "action": ..., "project": ..., "changes": [{"label","old","new"}], "diff": ..., "errors": [...]}

restore / finish 会把还原结果、编译结果和错误摘要合并进 summary.json。
退出码：0 成功；2 参数错误；3 还原异常；1 其它错误。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .builder import error_summary
from .config import load_rules
from .journal import backup_files, restore_files
from .params import ParamError, current_value, make_plan, parse_message, write_text
from .store import load_json, save_json

EXIT_PARAMS, EXIT_RESTORE, EXIT_OTHER = 2, 3, 1


class CliError(Exception):
    def __init__(self, msg: str, code: int = EXIT_PARAMS, errors: list[str] | None = None,
                 keep_summary: bool = False):
        super().__init__(msg)
        self.code = code
        self.errors = errors or [msg]
        self.keep_summary = keep_summary  # True：不覆盖已有的 summary.json


def _summary(out: str | None, data: dict) -> None:
    if out:
        save_json(os.path.join(out, "summary.json"), data)


def _merge_summary(out: str, **fields) -> None:
    path = os.path.join(out, "summary.json")
    data = load_json(path, {})
    data.update(fields)
    save_json(path, data)


def _read_params(args) -> str:
    if args.params is not None:
        return args.params
    if args.params_file:
        with open(args.params_file, encoding="utf-8-sig") as f:
            return f.read()
    raise CliError("需要 --params 或 --params-file")


def _select(args):
    projects, default = load_rules(args.rules)
    name = args.project or default or (next(iter(projects)) if len(projects) == 1 else None)
    if not name:
        raise CliError(f"请用 --project 指定项目，可选: {', '.join(projects)}")
    if name not in projects:
        raise CliError(f"未知项目 {name}，可选: {', '.join(projects)}")
    if not os.path.isdir(args.root):
        raise CliError(f"源码目录不存在: {args.root}", EXIT_OTHER)
    return name, projects[name]


def _plan(args):
    name, rules = _select(args)
    text = _read_params(args)
    project_in_text, assigns, errors = parse_message(text, rules)
    if project_in_text and project_in_text != name:
        errors.insert(0, f"参数里写的项目 {project_in_text} 与编译任务的项目 {name} 不一致")
    if not assigns and not errors:
        errors.append("没有识别到参数，格式为每行一个 名称=值 或 名称[下标]=值")
    if errors:
        raise CliError("参数有误", errors=errors)
    try:
        plan = make_plan(assigns, args.root)
    except ParamError as e:
        raise CliError("参数校验失败", errors=str(e).splitlines())
    return name, plan


def _changes(plan) -> list[dict]:
    return [{"label": c.label, "old": c.old, "new": c.new} for c in plan.changes]


def cmd_list(args) -> int:
    name, rules = _select(args)
    rows = []
    for r in rules:
        try:
            cur = current_value(r, args.root)
        except (ParamError, OSError) as e:
            cur = f"<读取失败: {e}>"
        rows.append({"name": r.name, "aliases": r.aliases, "desc": r.desc, "type": r.type,
                     "min": r.min, "max": r.max, "current": cur})
        alias = f" ({', '.join(r.aliases)})" if r.aliases else ""
        print(f"{r.name}{alias}: {cur}  {r.desc}")
    _summary(args.out, {"ok": True, "action": "list", "project": name, "params": rows})
    return 0


def cmd_check(args) -> int:
    name, plan = _plan(args)
    print(f"[项目 {name}] 参数校验通过，将修改：\n{plan.summary()}\n\n{plan.diff()}")
    _summary(args.out, {"ok": True, "action": "check", "project": name,
                        "changes": _changes(plan), "diff": plan.diff()})
    return 0


def cmd_apply(args) -> int:
    if not args.out:
        raise CliError("apply 需要 --out（保存备份和结果）", EXIT_OTHER)
    state_path = os.path.join(args.out, "rf-state.json")
    if load_json(state_path, None):
        raise CliError(f"{state_path} 已存在：上一次修改尚未还原，请先执行 restore", EXIT_OTHER, keep_summary=True)
    name, plan = _plan(args)
    files = backup_files(plan, os.path.join(args.out, "backup"))
    save_json(state_path, {"project": name, "root": os.path.abspath(args.root), "files": files})
    try:
        for path, text in plan.new_texts.items():
            write_text(path, text)
    except OSError as e:
        problems = restore_files(files)
        raise CliError(f"写文件失败: {e}", EXIT_OTHER, [f"写文件失败: {e}", *problems])
    with open(os.path.join(args.out, "changes.diff"), "w", encoding="utf-8") as f:
        f.write(plan.diff())
    print(f"[项目 {name}] 已修改：\n{plan.summary()}\n\n{plan.diff()}")
    _summary(args.out, {"ok": True, "action": "apply", "project": name,
                        "changes": _changes(plan), "diff": plan.diff()})
    return 0


def cmd_restore(args) -> int:
    state_path = os.path.join(args.out, "rf-state.json")
    state = load_json(state_path, None)
    if not state:
        print("没有需要还原的修改")
        return 0
    problems = restore_files(state["files"])
    _merge_summary(args.out, restore_problems=problems, restored=not problems)
    if problems:
        print("还原异常：\n" + "\n".join(problems), file=sys.stderr)
        return EXIT_RESTORE
    os.replace(state_path, state_path + ".done")
    print(f"已还原 {len(state['files'])} 个文件")
    return 0


def cmd_finish(args) -> int:
    """记录编译结果；失败时从编译日志中提取错误摘要。"""
    fields = {"build_result": args.result}
    if args.result != "SUCCESS" and args.log and os.path.exists(args.log):
        fields["error_summary"] = error_summary(args.log)
    if args.artifacts:
        fields["artifacts"] = args.artifacts
    _merge_summary(args.out, **fields)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m btbot.rf", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("list", "check", "apply"):
        p = sub.add_parser(name)
        p.add_argument("--rules", required=True, help="参数规则文件（与 config.yaml 格式相同）")
        p.add_argument("--project")
        p.add_argument("--root", required=True, help="Android 源码根目录")
        p.add_argument("--out", help="结果目录（summary.json、changes.diff、备份）")
        if name != "list":
            p.add_argument("--params", help="参数文本，每行一个 名称=值")
            p.add_argument("--params-file", help="从文件读取参数文本（推荐，避免 shell 转义问题）")
    p = sub.add_parser("restore")
    p.add_argument("--out", required=True)
    p = sub.add_parser("finish")
    p.add_argument("--out", required=True)
    p.add_argument("--result", required=True, help="SUCCESS / FAILURE / ABORTED / UNSTABLE")
    p.add_argument("--log", help="编译日志，失败时提取错误摘要")
    p.add_argument("--artifacts", nargs="*", help="产物链接或路径")
    args = ap.parse_args(argv)
    if getattr(args, "out", None):
        os.makedirs(args.out, exist_ok=True)

    try:
        return {"list": cmd_list, "check": cmd_check, "apply": cmd_apply, "restore": cmd_restore,
                "finish": cmd_finish}[args.cmd](args)
    except (CliError, ValueError, OSError) as e:
        code = e.code if isinstance(e, CliError) else EXIT_OTHER
        errors = e.errors if isinstance(e, CliError) else [str(e)]
        print("错误: " + str(e) + "".join(f"\n  - {x}" for x in errors if x != str(e)), file=sys.stderr)
        if args.cmd in ("list", "check", "apply") and not getattr(e, "keep_summary", False):
            _summary(getattr(args, "out", None), {"ok": False, "action": args.cmd, "project": args.project,
                                                  "errors": errors})
        return code


if __name__ == "__main__":
    sys.exit(main())
