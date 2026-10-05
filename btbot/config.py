from __future__ import annotations

import os
from dataclasses import dataclass, field

import yaml

from .params import ParamError, Rule, current_value


@dataclass
class Project:
    name: str
    root: str
    build_command: str
    params: list[Rule]
    timeout_minutes: int = 240
    restore_after_build: bool = True
    artifacts: list[str] = field(default_factory=list)
    publish_dir: str | None = None  # 编译产物复制到这里（如 NFS/Samba 共享目录）
    publish_url_prefix: str | None = None  # 共享目录对应的访问地址，用于回复里给出链接
    min_free_gb: float = 0  # 编译前检查 root 所在磁盘剩余空间，0 表示不检查
    note: str = ""  # 附在结果末尾的提示


@dataclass
class Limits:
    max_queue: int = 10  # 排队任务总数上限
    max_jobs_per_user: int = 2  # 每人同时排队/编译的任务数上限
    max_message_chars: int = 8000
    max_message_age_minutes: int = 10  # 忽略太旧的消息（重启后飞书补推）
    max_retries_after_crash: int = 1  # 机器人异常重启后，被中断的任务自动重试次数
    handler_threads: int = 4


@dataclass
class Config:
    app_id: str
    app_secret: str
    allowed_users: list[str]
    admins: list[str]  # 可执行 /unblock 的 open_id，空表示 allowed_users 都可以
    alert_chat_id: str | None
    workdir: str
    projects: dict[str, Project]
    default_project: str | None
    limits: Limits

    def project(self, name: str | None) -> Project:
        name = name or self.default_project or (next(iter(self.projects)) if len(self.projects) == 1 else None)
        if name is None:
            raise KeyError(f"请指定项目，例如 `项目=xxx`。可选: {', '.join(self.projects)}")
        if name not in self.projects:
            raise KeyError(f"未知项目 `{name}`。可选: {', '.join(self.projects)}")
        return self.projects[name]

    def check(self) -> list[str]:
        """启动自检：源码目录、参数文件、定位规则是否有效。返回问题列表。"""
        problems = []
        for p in self.projects.values():
            if not os.path.isdir(p.root):
                problems.append(f"[{p.name}] 源码目录不存在: {p.root}")
                continue
            for r in p.params:
                try:
                    current_value(r, p.root)
                except (ParamError, OSError) as e:
                    problems.append(f"[{p.name}] 参数 {r.name}: {e}")
            if p.publish_dir and not os.path.isdir(p.publish_dir):
                problems.append(f"[{p.name}] publish_dir 不存在: {p.publish_dir}")
        return problems


def _build(cls, d: dict, where: str):
    unknown = set(d) - set(cls.__dataclass_fields__)
    if unknown:
        raise ValueError(f"{where} 含未知配置项: {', '.join(sorted(unknown))}")
    return cls(**d)


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
    """只读取参数规则（projects.<名>.params 与 default_project），供 Jenkins 命令行使用。
    与 config.yaml 格式兼容，可以直接共用同一个文件。"""
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    projects = {name: parse_rules(name, (p or {}).get("params")) for name, p in (raw.get("projects") or {}).items()}
    if not projects:
        raise ValueError(f"{path} 中没有配置 projects")
    return projects, raw.get("default_project")


def load_config(path: str) -> Config:
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    fs = raw.get("feishu") or {}
    projects = {}
    for name, p in (raw.get("projects") or {}).items():
        p = dict(p)
        rules = parse_rules(name, p.pop("params", None))
        for req in ("root", "build_command"):
            if not p.get(req):
                raise ValueError(f"项目 {name} 缺少 {req}")
        projects[name] = _build(Project, {"name": name, "params": rules, **p}, f"项目 {name}")
    if not projects:
        raise ValueError("config 中没有配置 projects")
    default = raw.get("default_project")
    if default and default not in projects:
        raise ValueError(f"default_project `{default}` 不在 projects 中")
    base = os.path.dirname(os.path.abspath(path))
    return Config(
        app_id=os.environ.get("FEISHU_APP_ID") or fs.get("app_id", ""),
        app_secret=os.environ.get("FEISHU_APP_SECRET") or fs.get("app_secret", ""),
        allowed_users=fs.get("allowed_users") or [],
        admins=fs.get("admins") or [],
        alert_chat_id=fs.get("alert_chat_id"),
        workdir=os.path.join(base, raw.get("workdir", "data")),
        projects=projects,
        default_project=default,
        limits=_build(Limits, raw.get("limits") or {}, "limits"),
    )
