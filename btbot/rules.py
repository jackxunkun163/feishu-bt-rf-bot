"""读取参数规则文件（格式见 rules.example.yaml）。"""
from __future__ import annotations

import yaml

from .params import Rule


def parse_rules(project: str, raw) -> list[Rule]:
    flat = []  # 支持在 params 中引用 YAML 锚点列表
    for r in raw or []:
        flat.extend(r if isinstance(r, list) else [r])
    if not flat:
        raise ValueError(f"项目 {project} 没有配置 params")
    rules = [Rule.from_dict(r) for r in flat]
    names = [n.lower() for r in rules for n in [r.name, *r.aliases]]
    if len(names) != len(set(names)):
        raise ValueError(f"项目 {project} 的参数名/别名有重复")
    return rules


def load_rules(path: str) -> tuple[dict[str, list[Rule]], str | None]:
    """返回 ({项目名: 规则列表}, default_project)。"""
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    projects = {name: parse_rules(name, (p or {}).get("params")) for name, p in (raw.get("projects") or {}).items()}
    if not projects:
        raise ValueError(f"{path} 中没有配置 projects")
    default = raw.get("default_project")
    if default and default not in projects:
        raise ValueError(f"default_project `{default}` 不在 projects 中")
    return projects, default
