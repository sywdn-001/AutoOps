"""命令策略引擎。

设计要点
--------
1. **拆分再判定**：``ls; rm -rf /`` 这类复合命令按 ``; && || |`` 和换行拆段，
   每段单独判定；``$(...)`` / 反引号里的子命令同样拆出来判定。任一段命中拒绝，
   整条命令拒绝。
2. **首个命中即生效**：规则按 ``priority`` 升序（相同则按 id）匹配，命中第一条就定论；
   一条都没命中时由策略的 ``default_action`` 兜底（默认允许，可切为「白名单模式」）。
3. **默认大小写不敏感 + 空白归一**：命令是 shell 语义，``RM  -RF`` 与 ``rm -rf``
   必须同罪。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Sequence

RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}
RISK_BY_RANK = {v: k for k, v in RISK_ORDER.items()}

_SEPARATORS = ("&&", "||", ";", "|", "\n")
_SUBST_RE = re.compile(r"\$\((.*?)\)|`(.*?)`", re.S)

#: 会话控制类输入，不是 shell 命令，不参与策略判定
CONTROL_INPUTS = {
    "exit",
    "quit",
    "logout",
    "bye",
    "\x04",
    "help",
    "?",
    "hosts",
    "list",
    "clear",
    "cls",
    "whoami",
}


@dataclass
class SegmentDecision:
    segment: str
    action: str  # allow / deny
    risk_level: str = "low"
    rule_id: int | None = None
    rule_pattern: str = ""
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.action != "deny"


@dataclass
class Decision:
    allowed: bool
    action: str
    risk_level: str = "low"
    rule_id: int | None = None
    rule_pattern: str = ""
    reason: str = ""
    segments: list[SegmentDecision] = field(default_factory=list)

    @property
    def denied_segments(self) -> list[SegmentDecision]:
        return [s for s in self.segments if s.action == "deny"]


def normalize(text: str) -> str:
    """压缩连续空白，便于匹配。"""
    return re.sub(r"\s+", " ", text or "").strip()


def split_commands(text: str) -> list[str]:
    """按 shell 分隔符拆段，正确跳过引号内内容。"""
    parts: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    length = len(text or "")
    while i < length:
        ch = text[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ('"', "'"):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < length:
            buf.append(text[i : i + 2])
            i += 2
            continue
        matched = next((s for s in _SEPARATORS if text.startswith(s, i)), None)
        if matched:
            parts.append("".join(buf))
            buf = []
            i += len(matched)
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return [p.strip() for p in parts if p and p.strip()]


def all_segments(text: str) -> list[str]:
    """各层命令段 + 命令替换内的子命令段。"""
    segments = split_commands(text)
    extra: list[str] = []
    for segment in list(segments):
        for match in _SUBST_RE.finditer(segment):
            inner = match.group(1) or match.group(2) or ""
            extra.extend(split_commands(inner))
    return segments + extra


def _rule_matches(rule, command: str) -> bool:
    pattern = (rule.pattern or "").strip()
    if not pattern:
        return False
    match_type = (rule.match_type or "regex").lower()
    if match_type == "regex":
        try:
            if re.search(pattern, command, re.IGNORECASE):
                return True
            return re.search(pattern, normalize(command), re.IGNORECASE) is not None
        except re.error:
            return False
    if match_type == "prefix":
        return normalize(command).lower().startswith(normalize(pattern).lower())
    if match_type == "exact":
        return normalize(command).lower() == normalize(pattern).lower()
    if match_type == "contains":
        return normalize(pattern).lower() in normalize(command).lower()
    return False


def _sorted_rules(rules: Iterable) -> list:
    return sorted(
        [r for r in rules if getattr(r, "enabled", True)],
        key=lambda r: (r.priority if r.priority is not None else 100, r.id or 0),
    )


def _evaluate_segment(rules: Sequence, segment: str, default_action: str) -> SegmentDecision:
    for rule in rules:
        if _rule_matches(rule, segment):
            return SegmentDecision(
                segment=segment,
                action=(rule.action or "deny").lower(),
                risk_level=(rule.risk_level or "low").lower(),
                rule_id=rule.id,
                rule_pattern=rule.pattern or "",
                reason=rule.description or f"命中规则 {rule.pattern}",
            )
    return SegmentDecision(
        segment=segment,
        action=default_action,
        risk_level="low" if default_action == "allow" else "medium",
        reason="未命中任何规则，按策略默认动作处理",
    )


def evaluate_policy(policy, command: str) -> Decision:
    """对一条用户输入做策略判定。``policy`` 为 None 时按「允许但记录」处理。"""
    command = (command or "").strip()
    if not command:
        return Decision(allowed=True, action="allow", reason="空命令")

    if policy is None:
        return Decision(
            allowed=True,
            action="allow",
            risk_level="low",
            reason="未绑定命令策略，按审计放行处理",
            segments=[SegmentDecision(segment=command, action="allow", reason="未绑定策略")],
        )

    default_action = (policy.default_action or "allow").lower()
    rules = _sorted_rules(policy.rules or [])

    segments = all_segments(command)
    decisions = [_evaluate_segment(rules, seg, default_action) for seg in segments]

    denied = [d for d in decisions if d.action == "deny"]
    risk_rank = max((RISK_ORDER.get(d.risk_level, 0) for d in decisions), default=0)
    risk_level = RISK_BY_RANK.get(risk_rank, "low")

    if denied:
        first = denied[0]
        # 提示里必须带命中的规则编号：管理员按「说明」去找规则时，一条条比对极易删错
        # （实测缺陷：真正拦 `sudo -i` 的是「禁止在会话内二次提权」，管理员却删掉了
        # 「禁止修改权限与 sudo 配置」，然后反馈「规则删了还是被拦」）。
        hit = f"命中规则 #{first.rule_id}：" if first.rule_id is not None else ""
        reason = f"命令被策略「{policy.name}」拦截：段 [{first.segment}] {hit}{first.reason}"
        return Decision(
            allowed=False,
            action="deny",
            risk_level=risk_level,
            rule_id=first.rule_id,
            rule_pattern=first.rule_pattern,
            reason=reason,
            segments=decisions,
        )

    matched = next((d for d in decisions if d.rule_id is not None), None)
    return Decision(
        allowed=True,
        action="allow",
        risk_level=risk_level,
        rule_id=matched.rule_id if matched else None,
        rule_pattern=matched.rule_pattern if matched else "",
        reason=matched.reason if matched else "未命中任何规则，按策略默认动作放行",
        segments=decisions,
    )


@dataclass
class FrozenRule:
    """脱离 ORM session 的规则快照，供后台线程安全判定。"""

    id: int | None
    priority: int
    action: str
    match_type: str
    pattern: str
    risk_level: str
    enabled: bool = True
    description: str = ""


@dataclass
class FrozenPolicy:
    id: int | None
    name: str
    default_action: str
    rules: list = field(default_factory=list)


def freeze_policy(policy) -> FrozenPolicy | None:
    """把 ORM 策略对象转成纯数据快照（线程/进程外使用，避免 DetachedInstanceError）。"""
    if policy is None:
        return None
    rules = [
        FrozenRule(
            id=rule.id,
            priority=rule.priority if rule.priority is not None else 100,
            action=rule.action or "deny",
            match_type=rule.match_type or "regex",
            pattern=rule.pattern or "",
            risk_level=rule.risk_level or "low",
            enabled=bool(rule.enabled),
            description=rule.description or "",
        )
        for rule in (policy.rules or [])
    ]
    return FrozenPolicy(
        id=policy.id,
        name=policy.name or "",
        default_action=policy.default_action or "allow",
        rules=rules,
    )


def is_control_input(command: str) -> bool:
    return (command or "").strip().lower() in CONTROL_INPUTS


#: 会话级「退出」输入：**任何命令策略都不应拦截**。
#: 历史缺陷：只读白名单策略把 `exit` 判成「白名单外命令」而拒绝，用户被困在目标机会话里
#: 出不去（网关横幅还明确承诺「输入 exit 返回主机菜单」），审计里也会留下一条 exit=deny 的
#: 噪声。退出/登出是会话控制，不是要在目标机上执行的命令，所以必须先于策略判定放行；
#: 放行后仍走正常的命令通道 —— 命令与输出照常落库审计（需求④：每条命令与输出都被记录）。
SESSION_EXIT_INPUTS = {"exit", "quit", "logout", "bye"}


def is_session_exit(command: str) -> bool:
    return (command or "").strip().lower() in SESSION_EXIT_INPUTS


# --------------------------------------------------------------------------
# 内置策略模板
# --------------------------------------------------------------------------

#: 凭据类文件：任何内置策略都不允许读取（只读白名单是按命令字白名单的，
#: 光放行 cat 就等于放行 `cat /etc/shadow`，所以必须按路径再拦一层）。
SENSITIVE_PATH_PATTERN = (
    r"(/etc/(shadow|gshadow|sudoers)|/etc/ssh/ssh_host_[a-z0-9_]*_key"
    r"|/root/\.ssh|/root/\.bash_history|/etc/ssl/private|/proc/\d+/environ"
    r"|\.pgpass|\.my\.cnf|/etc/mysql/debian\.cnf"
    r"|\bid_(rsa|dsa|ecdsa|ed25519)\b|\.pem\b)"
)

#: 内置策略模板的版本号。**每次改动 BUILTIN_POLICIES 的规则都要 +1**，
#: seed 时按版本号把内置策略的规则整组刷新，安全规则修复才能落到已有安装上。
BUILTIN_POLICY_REV = 2

#: 「高危拦截」：默认放行，只拦破坏性/提权/逃逸类命令。适合大多数运维授权。
GUARDED_POLICY = {
    "name": "默认策略·高危命令拦截",
    "description": "默认放行并全量记录，拦截破坏性、提权与审计逃逸类命令。",
    "default_action": "allow",
    "is_default": True,
    "rules": [
        {
            "priority": 10,
            "action": "deny",
            "match_type": "regex",
            "pattern": r"\brm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s+)+/(\s|$)",
            "risk_level": "critical",
            "description": "禁止递归删除根目录",
        },
        {
            "priority": 11,
            "action": "deny",
            "match_type": "regex",
            "pattern": r"\b(mkfs(\.\w+)?|mkswap)\b",
            "risk_level": "critical",
            "description": "禁止格式化文件系统",
        },
        {
            "priority": 12,
            "action": "deny",
            "match_type": "regex",
            "pattern": r"\bdd\s+.*of=/dev/(sd|nvme|hd|vd)",
            "risk_level": "critical",
            "description": "禁止向块设备直接写数据",
        },
        {
            "priority": 13,
            "action": "deny",
            "match_type": "regex",
            "pattern": r">\s*/dev/(sd|nvme|hd|vd)",
            "risk_level": "critical",
            "description": "禁止重定向覆盖块设备",
        },
        {
            "priority": 14,
            "action": "deny",
            "match_type": "regex",
            "pattern": r":\(\)\s*\{\s*:\|:&\s*\}\s*;?\s*:",
            "risk_level": "critical",
            "description": "禁止 fork 炸弹",
        },
        {
            "priority": 8,
            "action": "deny",
            "match_type": "regex",
            "pattern": SENSITIVE_PATH_PATTERN,
            "risk_level": "critical",
            "description": "禁止读取口令/密钥等凭据文件",
        },
        {
            "priority": 15,
            "action": "deny",
            "match_type": "regex",
            "pattern": r"(>>?\s*|\btee\s+)(/etc/|/boot/|/root/\.ssh|/var/spool/cron|/usr/lib/systemd)",
            "risk_level": "critical",
            "description": "禁止写入系统关键配置路径",
        },
        {
            "priority": 16,
            "action": "deny",
            "match_type": "regex",
            "pattern": r"\b(chattr\s+[+-]i|setenforce\s+0|systemctl\s+(stop|disable)\s+(auditd|rsyslog|journald))\b",
            "risk_level": "critical",
            "description": "禁止关闭审计/日志相关服务或权限",
        },
        {
            "priority": 15,
            "action": "deny",
            "match_type": "regex",
            "pattern": r"\b(shutdown|reboot|halt|poweroff|init\s+0|init\s+6)\b",
            "risk_level": "critical",
            "description": "禁止关机 / 重启",
        },
        {
            "priority": 16,
            "action": "deny",
            "match_type": "regex",
            # 必须锚定在命令位置：`\bpasswd\b` 会把 `cat /etc/passwd` 也拦掉，
            # 而读 /etc/passwd 是常规排查动作。分隔符已由 split_commands 拆段。
            "pattern": (
                r"(^|[;&|]\s*|\b(?:sudo|su|env|command|nohup|nice|timeout|xargs)\s+)"
                r"(passwd|chpasswd|usermod|useradd|userdel|groupadd|groupdel)\b"
            ),
            "risk_level": "critical",
            "description": "禁止变更系统账号",
        },
        {
            "priority": 17,
            "action": "deny",
            "match_type": "regex",
            "pattern": r"\b(visudo|sudoers|chattr|setfacl)\b",
            "risk_level": "critical",
            "description": "禁止修改权限与 sudo 配置",
        },
        {
            "priority": 18,
            "action": "deny",
            "match_type": "regex",
            "pattern": r"\b(iptables|nft|firewall-cmd|ufw)\s+(-F|flush|--flush)",
            "risk_level": "critical",
            "description": "禁止清空防火墙规则",
        },
        {
            "priority": 19,
            "action": "deny",
            "match_type": "regex",
            "pattern": r"\b(su|sudo\s+su|sudo\s+-i|sudo\s+-s|sudo\s+bash|sudo\s+sh)\b",
            "risk_level": "critical",
            "description": "禁止在会话内二次提权",
        },
        {
            "priority": 20,
            "action": "deny",
            "match_type": "regex",
            "pattern": r"\b(ssh|scp|sftp|telnet|nc|ncat|netcat|socat)\b",
            "risk_level": "high",
            "description": "禁止从会话内跳转其他主机（防审计逃逸）",
        },
        {
            "priority": 21,
            "action": "deny",
            "match_type": "regex",
            "pattern": r"\b(history\s+-c|>\s*~?/?\.?\w*history|unset\s+HISTFILE|export\s+HISTFILE=/dev/null|HISTSIZE=0)",
            "risk_level": "high",
            "description": "禁止清空或关闭历史记录",
        },
        {
            "priority": 22,
            "action": "deny",
            "match_type": "regex",
            "pattern": r"\b(auditctl|systemctl\s+(stop|disable|mask)\s+auditd|service\s+auditd\s+stop)\b",
            "risk_level": "critical",
            "description": "禁止干扰审计服务",
        },
        {
            "priority": 23,
            "action": "deny",
            "match_type": "regex",
            "pattern": r"\b(pkill|killall|kill\s+-9\s+1|kill\s+-9\s+-1)\b",
            "risk_level": "high",
            "description": "禁止批量杀进程",
        },
        {
            "priority": 30,
            "action": "deny",
            "match_type": "regex",
            "pattern": r"^\s*(vi|vim|nvim|nano|emacs|pico|ed)\b",
            "risk_level": "medium",
            "description": "禁止全屏编辑器（无法审计交互输入）",
        },
        {
            "priority": 31,
            "action": "deny",
            "match_type": "regex",
            "pattern": r"^\s*(top|htop|atop|glances|iotop)\b",
            "risk_level": "medium",
            "description": "禁止全屏监控（请用 ps / free / uptime）",
        },
        {
            "priority": 32,
            "action": "deny",
            "match_type": "regex",
            "pattern": r"^\s*(less|more|man)\b",
            "risk_level": "low",
            "description": "禁止分页器（请用 head / tail / cat）",
        },
        {
            "priority": 33,
            "action": "deny",
            "match_type": "regex",
            "pattern": r"\b(tail|head)\s+.*\s-f\b|\bwatch\b",
            "risk_level": "medium",
            "description": "禁止持续输出命令",
        },
        {
            "priority": 34,
            "action": "deny",
            "match_type": "regex",
            "pattern": r"^\s*(bash|sh|zsh|ksh|csh|fish|dash|python|python3|perl|ruby|node|php)\s*(-i)?\s*$",
            "risk_level": "critical",
            "description": "禁止进入交互式子 shell / 解释器",
        },
    ],
}

#: 「只读白名单」：default_action=deny，只有下面这些只读命令放行。
READONLY_POLICY = {
    "name": "只读审计策略",
    "description": "白名单模式，仅放行只读排查命令，适合巡检/外包人员。",
    "default_action": "deny",
    "is_default": False,
    "rules": [
        {
            "priority": 20,
            "action": "deny",
            "match_type": "regex",
            "pattern": SENSITIVE_PATH_PATTERN,
            "risk_level": "critical",
            "description": "只读策略下禁止读取口令/密钥等凭据文件（只读 ≠ 可读凭据）",
        },
        {
            "priority": 50,
            "action": "deny",
            "match_type": "regex",
            "pattern": r"(^|[^0-9&<>-])>>?(?![&>])",
            "risk_level": "high",
            "description": "只读策略下禁止输出重定向（防止借重定向写文件）",
        },
        {
            "priority": 51,
            "action": "deny",
            "match_type": "regex",
            "pattern": r"\b(tee|dd|truncate|mkfifo|mknod)\b",
            "risk_level": "high",
            "description": "只读策略下禁止写入类命令与命名管道",
        },
        {"priority": 100, "action": "allow", "match_type": "regex", "pattern": r"^\s*(ls|ll|dir|pwd|cd)\b", "risk_level": "low", "description": "目录浏览"},
        {"priority": 101, "action": "allow", "match_type": "regex", "pattern": r"^\s*(cat|head|tail|tac|wc|file|stat|du|df)\b", "risk_level": "low", "description": "文件只读查看"},
        {"priority": 102, "action": "allow", "match_type": "regex", "pattern": r"^\s*(grep|egrep|fgrep|awk|sed\s+-n|sort|uniq|cut|tr|diff|cmp)\b", "risk_level": "low", "description": "文本检索"},
        {"priority": 103, "action": "allow", "match_type": "regex", "pattern": r"^\s*(ps|top\s+-b|free|uptime|vmstat|mpstat|iostat|pidstat|sar)\b", "risk_level": "low", "description": "进程与负载"},
        {"priority": 104, "action": "allow", "match_type": "regex", "pattern": r"^\s*(netstat|ss|ip\s+(a|addr|r|route)|ifconfig|route|arp)\b", "risk_level": "low", "description": "网络状态"},
        {"priority": 105, "action": "allow", "match_type": "regex", "pattern": r"^\s*(systemctl\s+status|journalctl|dmesg|last|lastlog|who|w|whoami|id|hostname|uname|date|env|printenv)\b", "risk_level": "low", "description": "系统信息"},
        {"priority": 106, "action": "allow", "match_type": "regex", "pattern": r"^\s*(docker\s+(ps|images|logs|stats|inspect)|kubectl\s+get)\b", "risk_level": "low", "description": "容器只读"},
        {"priority": 107, "action": "allow", "match_type": "regex", "pattern": r"^\s*ping\b", "risk_level": "low", "description": "连通性探测"},
        {"priority": 108, "action": "allow", "match_type": "regex", "pattern": r"^\s*(echo|which|type|help|man)\b", "risk_level": "low", "description": "辅助命令"},
        {"priority": 200, "action": "deny", "match_type": "regex", "pattern": r".*", "risk_level": "medium", "description": "白名单外一律拒绝"},
    ],
}

BUILTIN_POLICIES = [GUARDED_POLICY, READONLY_POLICY]


def seed_policies(session) -> dict:
    """幂等写入内置策略，返回 name -> CommandPolicy。

    内置策略的规则由代码托管：`builtin_policy_rev` 落后于 BUILTIN_POLICY_REV 时整组刷新，
    保证安全规则修复能落到已有安装上。管理员自建的策略完全不受影响。
    """
    from .models import CommandPolicy, CommandRule, SystemSetting

    created: dict[str, CommandPolicy] = {}
    rev_row = session.get(SystemSetting, "builtin_policy_rev")
    stored_rev = "" if rev_row is None else str(rev_row.value or "")
    refresh = stored_rev != str(BUILTIN_POLICY_REV)

    for template in BUILTIN_POLICIES:
        policy = session.query(CommandPolicy).filter_by(name=template["name"]).one_or_none()
        if policy is None:
            policy = CommandPolicy(
                name=template["name"],
                description=template["description"],
                default_action=template["default_action"],
                is_default=template["is_default"],
            )
            session.add(policy)
            session.flush()
            for rule in template["rules"]:
                session.add(CommandRule(policy_id=policy.id, **rule))
        elif refresh:
            session.query(CommandRule).filter_by(policy_id=policy.id).delete()
            for rule in template["rules"]:
                session.add(CommandRule(policy_id=policy.id, **rule))
            policy.description = template["description"]
            policy.default_action = template["default_action"]
            policy.is_default = template["is_default"]
        created[policy.name] = policy

    if refresh:
        if rev_row is None:
            session.add(
                SystemSetting(
                    key="builtin_policy_rev",
                    value=str(BUILTIN_POLICY_REV),
                    description="内置命令策略模板版本（用于升级后刷新内置规则）",
                )
            )
        else:
            rev_row.value = str(BUILTIN_POLICY_REV)
    session.flush()
    return created
