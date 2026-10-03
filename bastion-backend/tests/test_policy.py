"""命令策略引擎单测：这是审计的核心，必须逐条钉死。"""

from __future__ import annotations

import pytest

from app.policy import (
    BUILTIN_POLICIES,
    FrozenPolicy,
    FrozenRule,
    evaluate_policy,
    is_control_input,
    split_commands,
)


def frozen(template_name: str) -> FrozenPolicy:
    for template in BUILTIN_POLICIES:
        if template["name"] == template_name:
            return FrozenPolicy(
                id=1,
                name=template["name"],
                default_action=template["default_action"],
                rules=[
                    FrozenRule(
                        id=index,
                        priority=rule["priority"],
                        action=rule["action"],
                        match_type=rule["match_type"],
                        pattern=rule["pattern"],
                        risk_level=rule["risk_level"],
                        enabled=True,
                        description=rule.get("description", ""),
                    )
                    for index, rule in enumerate(template["rules"], start=1)
                ],
            )
    raise AssertionError(f"内置策略不存在：{template_name}")


@pytest.fixture()
def guarded():
    return frozen("默认策略·高危命令拦截")


@pytest.fixture()
def readonly():
    return frozen("只读审计策略")


# --------------------------------------------------------------------------- 拆分
def test_split_commands_breaks_on_shell_operators():
    assert split_commands("ls -l && rm -rf /") == ["ls -l", "rm -rf /"]
    assert split_commands("a; b | c || d") == ["a", "b", "c", "d"]


def test_split_commands_keeps_quoted_operators():
    segments = split_commands("echo 'a && b'")
    assert len(segments) == 1
    assert "a && b" in segments[0]


def test_split_commands_handles_empty():
    assert split_commands("") == []
    assert split_commands("   ") == []


# --------------------------------------------------------------------------- 高危拦截
@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /",
        "rm -fr /",
        "mkfs.ext4 /dev/sda1",
        "dd if=/dev/zero of=/dev/sda",
        "shutdown -h now",
        "reboot",
        "useradd hacker",
        "visudo",
        "iptables -F",
        "history -c",
        "pkill -9 nginx",
        "vim /etc/passwd",
        "top",
        "bash",
        "python3",
    ],
)
def test_guarded_policy_denies_dangerous_commands(guarded, command):
    decision = evaluate_policy(guarded, command)
    assert decision.allowed is False, f"{command} 应被拦截，实际：{decision}"
    assert decision.action == "deny"
    assert decision.rule_id is not None


@pytest.mark.parametrize(
    "command",
    ["ls -l /var/log", "cat /etc/os-release", "df -h", "systemctl status nginx", "echo hi"],
)
def test_guarded_policy_allows_normal_commands(guarded, command):
    decision = evaluate_policy(guarded, command)
    assert decision.allowed is True, f"{command} 应放行，实际：{decision}"
    assert decision.action == "allow"


def test_any_denied_segment_denies_whole_line(guarded):
    """关键语义：`ls && rm -rf /` 不能因为第一段放行就整体放行。"""
    decision = evaluate_policy(guarded, "ls -l && rm -rf /")
    assert decision.allowed is False
    assert "rm" in decision.reason or "策略" in decision.reason


def test_subcommand_substitution_is_inspected(guarded):
    decision = evaluate_policy(guarded, "echo $(rm -rf /)")
    assert decision.allowed is False, "命令替换里的高危命令必须被拆出来判定"


def test_backtick_substitution_is_inspected(guarded):
    decision = evaluate_policy(guarded, "echo `mkfs.ext4 /dev/sda`")
    assert decision.allowed is False


def test_case_insensitive_matching(guarded):
    assert evaluate_policy(guarded, "RM -RF /").allowed is False
    assert evaluate_policy(guarded, "Shutdown -h now").allowed is False


def test_whitespace_normalised_matching(guarded):
    assert evaluate_policy(guarded, "rm    -rf    /").allowed is False


# --------------------------------------------------------------------------- 白名单
def test_readonly_policy_denies_by_default(readonly):
    assert evaluate_policy(readonly, "ls -l").allowed is True
    assert evaluate_policy(readonly, "cat /etc/hosts").allowed is True
    assert evaluate_policy(readonly, "whoami").allowed is True
    decision = evaluate_policy(readonly, "touch /tmp/x")
    assert decision.allowed is False
    assert decision.rule_pattern == ".*"


def test_readonly_policy_denies_write_side_effects(readonly):
    for command in ["rm -rf /tmp/x", "chmod 777 /etc/shadow", "touch /tmp/x"]:
        assert evaluate_policy(readonly, command).allowed is False, command


def test_readonly_policy_denies_output_redirection(readonly):
    """白名单里的 `echo` 不能成为写文件的跳板。"""
    for command in [
        "echo hi > /etc/passwd",
        "echo hi >> /root/.ssh/authorized_keys",
        "> /etc/crontab",
        "cat /etc/hosts > /tmp/x",
    ]:
        decision = evaluate_policy(readonly, command)
        assert decision.allowed is False, f"{command} 必须被拒绝，实际：{decision}"


def test_readonly_policy_denies_write_helpers(readonly):
    for command in ["tee /etc/passwd", "dd if=/dev/zero of=/dev/sda", "mkfifo /tmp/p"]:
        assert evaluate_policy(readonly, command).allowed is False, command


def test_readonly_policy_allows_stderr_discard(readonly):
    """`2>/dev/null` 是排查常用写法，不能因为只读策略被误拦。"""
    assert evaluate_policy(readonly, "ls -la 2>/dev/null").allowed is True


def test_guarded_policy_denies_writes_to_system_paths(guarded):
    for command in [
        "echo 'x' > /etc/passwd",
        "echo 'x' >> /etc/sudoers",
        "tee /etc/sudoers",
        "chattr +i /etc/passwd",
        "systemctl stop auditd",
    ]:
        assert evaluate_policy(guarded, command).allowed is False, command


CREDENTIAL_READS = [
    "cat /etc/shadow",
    "head -1 /etc/shadow",
    "grep -v x /etc/shadow",
    "cat /root/.ssh/id_rsa",
    "cat /etc/ssh/ssh_host_rsa_key",
    "cat /etc/sudoers",
    "ls /etc/ssl/private",
    "cat ~/.pgpass",
]


@pytest.mark.parametrize("command", CREDENTIAL_READS)
def test_guarded_policy_denies_credential_files(guarded, command):
    """默认策略也必须拦凭据读取：放行 cat 不等于允许读 /etc/shadow。"""
    decision = evaluate_policy(guarded, command)
    assert decision.allowed is False, f"{command} 必须被拒绝，实际：{decision}"
    assert decision.rule_id is not None


@pytest.mark.parametrize("command", CREDENTIAL_READS)
def test_readonly_policy_denies_credential_files(readonly, command):
    """只读白名单是按命令字白名单的，必须按路径再拦一层。"""
    decision = evaluate_policy(readonly, command)
    assert decision.allowed is False, f"{command} 必须被拒绝，实际：{decision}"
    assert decision.rule_id is not None


def test_credential_guard_does_not_break_normal_reads(guarded, readonly):
    """别把普通排查命令误伤成「一律拒绝」。"""
    for command in ["cat /etc/hosts", "cat /etc/passwd", "head -20 /var/log/syslog", "grep root /etc/passwd"]:
        assert evaluate_policy(guarded, command).allowed is True, command
        assert evaluate_policy(readonly, command).allowed is True, command


# --------------------------------------------------------------------------- 边界
def test_none_policy_means_audit_only():
    decision = evaluate_policy(None, "rm -rf /")
    assert decision.allowed is True
    assert "策略" in decision.reason


def test_empty_command_is_allowed_but_marked(guarded):
    decision = evaluate_policy(guarded, "")
    assert decision.allowed is True
    assert decision.action in ("allow", "empty")


def test_priority_order_decides_first_match():
    policy = FrozenPolicy(
        id=9,
        name="优先级测试",
        default_action="allow",
        rules=[
            FrozenRule(id=1, priority=50, action="deny", match_type="contains", pattern="nginx", risk_level="high"),
            FrozenRule(id=2, priority=10, action="allow", match_type="contains", pattern="nginx", risk_level="low"),
        ],
    )
    decision = evaluate_policy(policy, "systemctl restart nginx")
    assert decision.allowed is True, "低 priority 数值应优先，此处 allow(10) 先命中"
    assert decision.rule_id == 2


def test_error_reason_contains_rule_description(guarded):
    decision = evaluate_policy(guarded, "mkfs.ext4 /dev/sda1")
    assert decision.reason
    assert decision.rule_pattern


def test_segments_are_reported(guarded):
    decision = evaluate_policy(guarded, "ls && cat /etc/hosts")
    assert len(decision.segments) == 2
    assert all(segment.allowed for segment in decision.segments)


def test_control_input_recognition():
    assert is_control_input("exit") is True
    assert is_control_input("  EXIT ") is True
    assert is_control_input("hosts") is True
    assert is_control_input("rm -rf /") is False
