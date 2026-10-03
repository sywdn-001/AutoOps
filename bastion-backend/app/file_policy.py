"""SFTP 文件操作策略引擎。

与命令策略（``app/policy.py``）同构，只把「命令段」换成「操作 + 路径」：

* 一条规则 = ``operation``（``*`` 或具体操作）+ 路径模式（glob / regex / prefix / contains）；
* 规则按 ``priority`` 从小到大逐条匹配，**首个命中即生效**；
* 都没命中时按策略的 ``default_action`` 处理。

访问控制是两层：``Grant`` 上的开关（能不能用文件管理器、能不能上传/下载/修改）
负责「这个人能不能做这类事」，本策略负责「这类事能不能发生在这个路径上」。
两层是**与**关系，任何一层不过就拦，并落一条 ``file_logs`` 审计。
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from typing import Sequence

RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}
RISK_BY_RANK = {rank: name for name, rank in RISK_ORDER.items()}

#: 文件管理器的全部操作 —— 前端按钮、策略页下拉、审计筛选都从这里取，避免三处各写一遍
FILE_OPERATIONS: dict[str, str] = {
    "list": "列目录 / 查看属性",
    "read": "读取 / 预览文件内容",
    "download": "下载文件",
    "archive": "打包下载（多选压缩）",
    "upload": "上传文件",
    "write": "编辑并保存文件内容",
    "mkdir": "新建目录",
    "rename": "重命名",
    "move": "移动到其它目录",
    "copy": "复制（文件 / 目录）",
    "delete": "删除（文件 / 目录）",
    "chmod": "修改文件权限",
}

#: 修改类操作 —— 需要授权里的 `can_file_write` 打开
WRITE_OPERATIONS = frozenset(
    {"upload", "write", "mkdir", "rename", "move", "copy", "delete", "chmod"}
)

#: 需要授权里的 `can_download` 打开（list / read 只要有 `can_sftp` 即可）
DOWNLOAD_OPERATIONS = frozenset({"download", "archive"})

#: 需要授权里的 `can_upload` 打开
UPLOAD_OPERATIONS = frozenset({"upload"})


def operation_label(operation: str) -> str:
    if operation == "*":
        return "全部操作"
    return FILE_OPERATIONS.get(operation, operation or "")


def normalize_path(path: str) -> str:
    """把用户给的路径收敛成远端绝对路径。

    SFTP 里路径由目标机解析，这里只做展示与策略匹配用的归一化：去空白、反斜杠转正斜杠、
    折叠重复斜杠与 ``.`` / ``..``，保留结尾斜杠之外的原始语义。
    """
    raw = (path or "").strip().replace("\\", "/")
    if not raw:
        return "/"
    if not raw.startswith("/"):
        raw = "/" + raw
    parts: list[str] = []
    for chunk in raw.split("/"):
        if chunk in ("", "."):
            continue
        if chunk == "..":
            if parts:
                parts.pop()
            continue
        parts.append(chunk)
    return "/" + "/".join(parts)


def parent_path(path: str) -> str:
    current = normalize_path(path)
    if current == "/":
        return "/"
    head, _, _ = current.rpartition("/")
    return head or "/"


def join_path(directory: str, name: str) -> str:
    base = normalize_path(directory)
    if base == "/":
        return f"/{name}"
    return f"{base}/{name}"


def _glob_matches(pattern: str, path: str) -> bool:
    """glob 语义（``*`` 也跨 ``/``，和运维直觉一致；``**`` 只是可读性写法）。

    另外把 `/etc` 这种「目录写法」当成 `/etc` 及其子树，省得管理员必须写 `/etc/**`。
    """
    if not pattern:
        return False
    if fnmatch.fnmatchcase(path, pattern):
        return True
    trimmed = pattern.rstrip("/")
    return bool(trimmed) and (path == trimmed or path.startswith(f"{trimmed}/"))


def _regex_matches(pattern: str, path: str) -> bool:
    try:
        return re.search(pattern, path, re.IGNORECASE) is not None
    except re.error:
        # 管理员写坏的正则不能把整个判定搞崩：退化成字面量比较
        return pattern in path


def path_matches(rule, path: str) -> bool:
    """规则是否命中该路径（match_type 决定比较方式）。"""
    pattern = (rule.path_pattern or "").strip()
    if not pattern:
        return False
    match_type = (rule.match_type or "glob").lower()
    if match_type == "regex":
        return _regex_matches(pattern, path)
    if match_type == "prefix":
        return path.startswith(pattern)
    if match_type == "contains":
        return pattern in path
    return _glob_matches(pattern, path)


def operation_matches(rule, operation: str) -> bool:
    target = (rule.operation or "*").strip().lower()
    if target in ("", "*", "all"):
        return True
    return target == (operation or "").strip().lower()


def _sorted_rules(rules: Sequence) -> list:
    enabled = [rule for rule in (rules or []) if bool(rule.enabled)]
    return sorted(
        enabled,
        key=lambda rule: (
            rule.priority if rule.priority is not None else 100,
            rule.id or 0,
        ),
    )


@dataclass
class FileDecision:
    """一次文件操作的判定结果。"""

    allowed: bool
    action: str
    risk_level: str = "low"
    rule_id: int | None = None
    rule_pattern: str = ""
    reason: str = ""
    policy_id: int | None = None
    policy_name: str = ""
    operation: str = ""
    path: str = ""

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "action": self.action,
            "riskLevel": self.risk_level,
            "ruleId": self.rule_id,
            "rulePattern": self.rule_pattern,
            "reason": self.reason,
            "policyId": self.policy_id,
            "policyName": self.policy_name,
            "operation": self.operation,
            "path": self.path,
        }


def evaluate_file_policy(
    policy,
    operation: str,
    path: str,
    *,
    target_path: str = "",
) -> FileDecision:
    """判定一次文件操作。``policy`` 为 None 时按「放行但记录」处理。"""
    operation = (operation or "").strip().lower()
    path = normalize_path(path)
    target = normalize_path(target_path) if target_path else ""

    if policy is None:
        return FileDecision(
            allowed=True,
            action="allow",
            risk_level="low",
            reason="未绑定文件策略，按审计放行处理",
            operation=operation,
            path=path,
        )

    policy_id = getattr(policy, "id", None)
    policy_name = getattr(policy, "name", "") or ""
    default_action = (getattr(policy, "default_action", "allow") or "allow").lower()

    # 双路径操作（rename / move / copy）两个路径都要过策略：把「源可以、目标不行」的
    # 绕过路径堵掉（例如把文件从 /tmp 复制进 /etc）。任一路径命中 deny 立即拦截；
    # 两个路径都没被拦时，只要有一个命中 allow 规则就带上规则信息放行。
    checks: list[tuple[str, str]] = [(path, "源路径")]
    if target and target != path:
        checks.append((target, "目标路径"))

    allow_hit: tuple | None = None
    rules = _sorted_rules(getattr(policy, "rules", None))
    for candidate, label in checks:
        for rule in rules:
            if not operation_matches(rule, operation):
                continue
            if not path_matches(rule, candidate):
                continue
            action = (rule.action or "deny").lower()
            risk = (rule.risk_level or "low").lower()
            description = rule.description or f"命中规则 {rule.path_pattern}"
            if action == "deny":
                hit = f"命中规则 #{rule.id}：" if rule.id is not None else ""
                return FileDecision(
                    allowed=False,
                    action="deny",
                    risk_level=risk,
                    rule_id=rule.id,
                    rule_pattern=rule.path_pattern or "",
                    reason=(
                        f"文件操作被策略「{policy_name}」拦截：操作 [{operation}] "
                        f"{label} [{candidate}] {hit}{description}"
                    ),
                    policy_id=policy_id,
                    policy_name=policy_name,
                    operation=operation,
                    path=path,
                )
            if allow_hit is None:
                allow_hit = (rule, candidate, label, risk, description)
            break

    if allow_hit is not None:
        rule, candidate, label, risk, description = allow_hit
        return FileDecision(
            allowed=True,
            action="allow",
            risk_level=risk,
            rule_id=rule.id,
            rule_pattern=rule.path_pattern or "",
            reason=f"操作 [{operation}] {label} [{candidate}] 命中放行规则：{description}",
            policy_id=policy_id,
            policy_name=policy_name,
            operation=operation,
            path=path,
        )

    return FileDecision(
        allowed=default_action == "allow",
        action=default_action,
        risk_level="low" if default_action == "allow" else "medium",
        reason=(
            f"文件操作 [{operation}] [{path}] 未命中任何规则，"
            f"按策略「{policy_name}」默认动作{'放行' if default_action == 'allow' else '拦截'}"
        ),
        policy_id=policy_id,
        policy_name=policy_name,
        operation=operation,
        path=path,
    )


@dataclass
class FrozenFileRule:
    """脱离 ORM session 的规则快照。"""

    id: int | None
    priority: int
    action: str
    operation: str
    match_type: str
    path_pattern: str
    risk_level: str
    enabled: bool = True
    description: str = ""


@dataclass
class FrozenFilePolicy:
    id: int | None
    name: str
    default_action: str
    rules: list = field(default_factory=list)


def freeze_file_policy(policy) -> FrozenFilePolicy | None:
    """把 ORM 文件策略转成纯数据快照（后台线程安全）。"""
    if policy is None:
        return None
    rules = [
        FrozenFileRule(
            id=rule.id,
            priority=rule.priority if rule.priority is not None else 100,
            action=rule.action or "deny",
            operation=rule.operation or "*",
            match_type=rule.match_type or "glob",
            path_pattern=rule.path_pattern or "",
            risk_level=rule.risk_level or "low",
            enabled=bool(rule.enabled),
            description=rule.description or "",
        )
        for rule in (policy.rules or [])
    ]
    return FrozenFilePolicy(
        id=policy.id,
        name=policy.name or "",
        default_action=policy.default_action or "allow",
        rules=rules,
    )


#: 凭据 / 私钥类路径：默认策略直接拦读取与下载
CREDENTIAL_PATH_PATTERN = (
    r"^(.*/)?(shadow|gshadow|shadow-[a-z0-9._-]*|id_(rsa|dsa|ecdsa|ed25519)"
    r"|\.netrc|\.pgpass|\.my\.cnf|\.git-credentials|credentials"
    r"|.*\.(pem|key|pfx|p12|jks|keystore))$"
)

#: 系统关键路径：默认策略拦一切修改类操作
SYSTEM_PATH_PATTERN = (
    r"^(/etc|/boot|/usr|/bin|/sbin|/lib|/lib64|/sys|/proc|/dev|/var/lib|/var/spool)(/.*)?$"
)

#: 系统关键目录本身：默认策略拦删除（删根目录是最贵的一类误操作）
SYSTEM_ROOT_PATTERN = (
    r"^(/|/(etc|usr|var|home|root|opt|srv|boot|bin|sbin|lib|lib64|data))$"
)

#: 只读策略放行的路径（只看这几个目录，其余一律拒）
READONLY_ALLOW_PATTERN = (
    r"^(/(etc|usr|opt|srv|home|tmp|var|data|mnt|media|run/log|var/log))(/.*)?$"
)

#: 只读策略下的目录浏览范围：列目录只返回文件名与属性，不返回文件内容。
#: 若不放开浏览，用户的家目录是 ``/`` 时进站第一步就被拒，白名单目录根本走不到。
#: ``.ssh`` 与凭据文件仍被更高优先级（priority 5/6）的规则挡住，进不去也看不到内容。
READONLY_NAV_PATTERN = r"^/.*$"

#: 内置「默认文件策略」：默认放行，但敏感路径与破坏性操作一律拦。
DEFAULT_FILE_POLICY = {
    "name": "默认文件策略·敏感路径拦截",
    "description": "默认放行常规文件操作；拦截凭据/私钥读取下载、系统目录修改与关键目录删除。",
    "default_action": "allow",
    "is_default": True,
    "rules": [
        {
            "priority": 10,
            "action": "deny",
            "operation": "*",
            "match_type": "regex",
            "path_pattern": r"^.*/\.ssh(/.*)?$",
            "risk_level": "critical",
            "description": "禁止通过文件管理器访问 .ssh 目录（私钥与授权文件必须走运维流程）",
        },
        {
            "priority": 11,
            "action": "deny",
            "operation": "read",
            "match_type": "regex",
            "path_pattern": CREDENTIAL_PATH_PATTERN,
            "risk_level": "critical",
            "description": "禁止读取口令/私钥等凭据文件",
        },
        {
            "priority": 12,
            "action": "deny",
            "operation": "download",
            "match_type": "regex",
            "path_pattern": CREDENTIAL_PATH_PATTERN,
            "risk_level": "critical",
            "description": "禁止下载口令/私钥等凭据文件",
        },
        {
            "priority": 13,
            "action": "deny",
            "operation": "archive",
            "match_type": "regex",
            "path_pattern": r"^.*/\.ssh(/.*)?$|^(.*/)?(shadow|gshadow)$",
            "risk_level": "critical",
            "description": "禁止把凭据类文件打进压缩包带走",
        },
        {
            "priority": 20,
            "action": "deny",
            "operation": "upload",
            "match_type": "regex",
            "path_pattern": SYSTEM_PATH_PATTERN,
            "risk_level": "critical",
            "description": "禁止写入系统目录（/etc、/usr、/boot 等）",
        },
        {
            "priority": 21,
            "action": "deny",
            "operation": "write",
            "match_type": "regex",
            "path_pattern": SYSTEM_PATH_PATTERN,
            "risk_level": "critical",
            "description": "禁止编辑系统目录下的文件",
        },
        {
            "priority": 22,
            "action": "deny",
            "operation": "mkdir",
            "match_type": "regex",
            "path_pattern": SYSTEM_PATH_PATTERN,
            "risk_level": "high",
            "description": "禁止在系统目录下新建目录",
        },
        {
            "priority": 23,
            "action": "deny",
            "operation": "chmod",
            "match_type": "regex",
            "path_pattern": SYSTEM_PATH_PATTERN,
            "risk_level": "critical",
            "description": "禁止修改系统目录内文件的权限",
        },
        {
            "priority": 24,
            "action": "deny",
            "operation": "rename",
            "match_type": "regex",
            "path_pattern": SYSTEM_PATH_PATTERN,
            "risk_level": "high",
            "description": "禁止重命名系统目录内的文件",
        },
        {
            "priority": 25,
            "action": "deny",
            "operation": "delete",
            "match_type": "regex",
            "path_pattern": SYSTEM_PATH_PATTERN,
            "risk_level": "critical",
            "description": "禁止删除系统目录内的文件",
        },
        {
            "priority": 26,
            "action": "deny",
            "operation": "delete",
            "match_type": "regex",
            "path_pattern": SYSTEM_ROOT_PATTERN,
            "risk_level": "critical",
            "description": "禁止删除系统关键目录本身（/、/etc、/usr、/home 等）",
        },
        {
            "priority": 27,
            "action": "deny",
            "operation": "move",
            "match_type": "regex",
            "path_pattern": SYSTEM_PATH_PATTERN,
            "risk_level": "high",
            "description": "禁止把文件移入或移出系统目录",
        },
        {
            "priority": 28,
            "action": "deny",
            "operation": "copy",
            "match_type": "regex",
            "path_pattern": SYSTEM_PATH_PATTERN,
            "risk_level": "critical",
            "description": "禁止把文件复制进系统目录（复制落地同样能提权/做持久化）",
        },
        {
            "priority": 40,
            "action": "deny",
            "operation": "*",
            "match_type": "glob",
            "path_pattern": "/proc/*",
            "risk_level": "medium",
            "description": "禁止通过文件管理器操作 /proc（请用命令查看）",
        },
    ],
}

#: 内置「只读文件策略」：default deny，只放行浏览/读取/下载。
READONLY_FILE_POLICY = {
    "name": "只读文件策略",
    "description": "白名单模式，仅放行浏览、查看与下载；任何上传、编辑、删除、改权限都被拒绝。",
    "default_action": "deny",
    "is_default": False,
    "rules": [
        {
            "priority": 5,
            "action": "deny",
            "operation": "*",
            "match_type": "regex",
            "path_pattern": r"^.*/\.ssh(/.*)?$",
            "risk_level": "critical",
            "description": "禁止访问 .ssh 目录",
        },
        {
            "priority": 6,
            "action": "deny",
            "operation": "*",
            "match_type": "regex",
            "path_pattern": CREDENTIAL_PATH_PATTERN,
            "risk_level": "critical",
            "description": "禁止读取/下载凭据与私钥文件",
        },
        {
            "priority": 49,
            "action": "allow",
            "operation": "list",
            "match_type": "regex",
            "path_pattern": READONLY_NAV_PATTERN,
            "risk_level": "low",
            "description": "放行目录浏览（只列文件名与属性，文件内容仍需白名单）",
        },
        {
            "priority": 50,
            "action": "allow",
            "operation": "list",
            "match_type": "regex",
            "path_pattern": READONLY_ALLOW_PATTERN,
            "risk_level": "low",
            "description": "放行备份/日志/应用目录浏览",
        },
        {
            "priority": 51,
            "action": "allow",
            "operation": "read",
            "match_type": "regex",
            "path_pattern": READONLY_ALLOW_PATTERN,
            "risk_level": "low",
            "description": "放行备份/日志/应用目录内文件查看",
        },
        {
            "priority": 52,
            "action": "allow",
            "operation": "download",
            "match_type": "regex",
            "path_pattern": READONLY_ALLOW_PATTERN,
            "risk_level": "low",
            "description": "放行备份/日志/应用目录内文件下载",
        },
        {
            "priority": 53,
            "action": "allow",
            "operation": "archive",
            "match_type": "regex",
            "path_pattern": READONLY_ALLOW_PATTERN,
            "risk_level": "low",
            "description": "放行备份/日志/应用目录内打包下载",
        },
    ],
}

BUILTIN_FILE_POLICIES = [DEFAULT_FILE_POLICY, READONLY_FILE_POLICY]

#: 内置模板版本：落后就整组刷新内置规则，让安全修复能落到已有安装
BUILTIN_FILE_POLICY_REV = 2


def resolve_file_policy(grant):
    """取授权绑定的文件策略；未绑定则回落到系统默认文件策略。"""
    from .extensions import db
    from .models import FilePolicy

    policy = None
    if grant is not None and getattr(grant, "file_policy_id", None):
        policy = db.session.get(FilePolicy, grant.file_policy_id)
    if policy is None:
        policy = FilePolicy.query.filter_by(is_default=True).first()
    if policy is None:
        policy = FilePolicy.query.order_by(FilePolicy.id.asc()).first()
    return policy


def seed_file_policies(session) -> dict:
    """幂等写入内置文件策略，返回 name -> FilePolicy。"""
    from .models import FilePolicy, FileRule, SystemSetting

    created: dict[str, FilePolicy] = {}
    rev_row = session.get(SystemSetting, "builtin_file_policy_rev")
    stored_rev = "" if rev_row is None else str(rev_row.value or "")
    refresh = stored_rev != str(BUILTIN_FILE_POLICY_REV)

    for template in BUILTIN_FILE_POLICIES:
        policy = session.query(FilePolicy).filter_by(name=template["name"]).one_or_none()
        if policy is None:
            policy = FilePolicy(
                name=template["name"],
                description=template["description"],
                default_action=template["default_action"],
                is_default=template["is_default"],
            )
            session.add(policy)
            session.flush()
            for rule in template["rules"]:
                session.add(FileRule(policy_id=policy.id, **rule))
        elif refresh:
            session.query(FileRule).filter_by(policy_id=policy.id).delete()
            for rule in template["rules"]:
                session.add(FileRule(policy_id=policy.id, **rule))
            policy.description = template["description"]
            policy.default_action = template["default_action"]
            policy.is_default = template["is_default"]
        created[policy.name] = policy

    if refresh:
        if rev_row is None:
            session.add(
                SystemSetting(
                    key="builtin_file_policy_rev",
                    value=str(BUILTIN_FILE_POLICY_REV),
                    description="内置文件策略模板版本（用于升级后刷新内置规则）",
                )
            )
        else:
            rev_row.value = str(BUILTIN_FILE_POLICY_REV)
    session.flush()
    return created
