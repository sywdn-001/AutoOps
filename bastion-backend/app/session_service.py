"""会话服务：把「授权解析 -> 连目标机 -> 建会话记录 -> 起终端桥 -> 落审计」串成一个闭环。

SSH 网关与网页终端都调用这里，保证两条入口的审计口径完全一致。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from flask import current_app

from . import session_registry
from .access import check_session_quota, find_access, in_time_window
from .audit import close_session, log_command, log_event, new_session
from .config import Config
from .extensions import db
from .models import CommandPolicy, Host, HostAccount, SessionRecord, User, gen_sid
from .policy import evaluate_policy, freeze_policy
from .settings_store import get_int
from .ssh_client import SSHError, build_target, close as close_conn, connect, open_shell
from .terminal import BridgeConfig, ShellBridge, TranscriptRecorder
from .winrm import (
    WinrmBridge,
    WinrmError,
    build_target as build_winrm_target,
    close as close_winrm,
    connect as connect_winrm,
)

log = logging.getLogger(__name__)


def _close_target_connection(protocol: str, connection) -> None:
    """按协议关闭目标机连接：Linux 走 paramiko（SSH），Windows 走 WinRM。"""
    if connection is None:
        return
    try:
        if protocol == "winrm":
            close_winrm(connection)
        else:
            close_conn(connection)
    except Exception:  # noqa: BLE001
        log.debug("关闭目标机连接失败（忽略）", exc_info=True)

#: 单条命令写入数据库的输出上限（完整内容仍在会话录制文件里）
DB_OUTPUT_LIMIT = 100_000


class SessionError(RuntimeError):
    """带用户可读中文原因的会话建立失败。"""


@dataclass
class OpenedSession:
    sid: str
    record_id: int
    bridge: ShellBridge | WinrmBridge
    connection: object
    segmented: bool
    host_name: str = ""
    account_username: str = ""
    policy_name: str = ""
    meta: dict = field(default_factory=dict)
    #: Flask app：teardown / 落库回调要用它建应用上下文，不能依赖环境里的 current_app
    app: object | None = None


def resolve_policy_for(grant):
    """取该授权绑定的策略；未绑定则回落到系统默认策略。"""
    policy = None
    if grant is not None and grant.policy_id:
        policy = db.session.get(CommandPolicy, grant.policy_id)
    if policy is None:
        policy = CommandPolicy.query.filter_by(is_default=True).first()
    return policy


def _started_at_from(duration_ms: int) -> datetime:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return now - timedelta(milliseconds=max(int(duration_ms or 0), 0))


def build_command_callback(app, sid: str):
    """构造命令落库回调（运行在桥接线程里，需要自建应用上下文）。

    这里**只**落库，不发用户提示：命令被拒绝时桥接层已经把拒绝原因直接写进终端输出流
    （`bridge._pending_notice`），若再经 ``notify`` 推一次，用户会在终端里看到两行
    内容相同、只是前缀不同的提示（实测缺陷：`[堡垒机] 命令被拒绝 —— …` 与 `[策略通知] …`）。
    """

    def on_command(event: dict) -> None:
        session_registry.touch(sid)
        try:
            with app.app_context():
                record = SessionRecord.query.filter_by(sid=sid).first()
                output = event.get("output") or ""
                if len(output) > DB_OUTPUT_LIMIT:
                    output = output[:DB_OUTPUT_LIMIT] + "\n...[输出过长，已截断，完整内容见会话录制]"
                log_command(
                    session=record,
                    command=event.get("command", ""),
                    output=output,
                    action=event.get("action", "allow"),
                    risk_level=event.get("risk_level", "low"),
                    matched_rule_id=event.get("matched_rule_id"),
                    matched_rule_pattern=event.get("matched_rule_pattern", ""),
                    reason=event.get("reason", ""),
                    started_at=_started_at_from(event.get("duration_ms", 0)),
                    duration_ms=event.get("duration_ms", 0),
                    exit_status=event.get("exit_status"),
                    truncated=bool(event.get("truncated")),
                    seq=event.get("seq", 0),
                )
        except Exception:  # noqa: BLE001 - 审计失败不能弄断终端
            log.exception("命令审计写入失败 sid=%s", sid)

    return on_command


def open_session(
    app,
    *,
    user_id: int,
    host_id: int,
    account_id: int | None = None,
    protocol: str | None = None,
    source: str = "web",
    client_ip: str = "",
    client_port: int = 0,
    cols: int = 120,
    rows: int = 32,
    send_output=None,
    notify=None,
    on_closed=None,
    on_command=None,
    on_ready=None,
) -> OpenedSession:
    """建立一条受审计的 SSH 会话。失败抛 :class:`SessionError`。

    :param send_output: 目标机输出回调（网页终端用它推送到浏览器）。
    :param notify: 桥接层主动发起的系统提示回调（例如提示符改写失败、降级为原始录制模式）。
        命令被拒绝的提示不走这里，由桥接层直接写进终端输出流，避免同一件事提示两次。
    :param on_closed: 会话结束回调。
    :param on_command: **额外的**命令事件回调，用于向网页终端实时推送
        `terminal:command`；命令落库仍由内部回调完成，两者互不影响。
    :param on_ready: 会话号已生成、但**目标机输出还没开始回流**时的回调，
        入参是 ``(sid, info)``，``info`` 含 hostName/address/accountUsername/
        policyName。调用方必须在这里打印/推送自己的「已连接」横幅，
        否则目标机的登录欢迎语会先于堡垒机横幅出现在用户终端里（实测缺陷：
        MOTD 抢在「[堡垒机] 已连接 ...」之前）。
    """
    with app.app_context():
        user: User | None = db.session.get(User, user_id)
        host: Host | None = db.session.get(Host, host_id)
        if user is None or not user.is_active():
            raise SessionError("账号不存在或已被停用")
        if host is None or host.status != "active":
            raise SessionError("目标主机不存在或已停用")
        if source == "gateway" and not user.gateway_enabled:
            raise SessionError("你的账号已被禁止使用 SSH 网关")
        if source == "web" and not user.webterm_enabled:
            raise SessionError("你的账号已被禁止使用网页终端")

        if account_id is not None and db.session.get(HostAccount, account_id) is None:
            raise SessionError("指定的资产账号不存在")

        resolved = find_access(user, host_id=host_id, account_id=account_id)
        if resolved is None:
            raise SessionError("你没有访问该主机的权限，或当前不在授权时间段内")
        grant, host, account = resolved.grant, resolved.host, resolved.account
        if account is None:
            raise SessionError("该主机下没有可用账号，请联系管理员配置资产账号")
        if source == "web" and not grant.can_webterm:
            raise SessionError("该授权不允许使用网页终端")

        ok, reason = check_session_quota(user, grant)
        if not ok:
            raise SessionError(reason)

        max_sessions = get_int("max_sessions_per_user", Config.GATEWAY_MAX_SESSIONS_PER_USER)
        if max_sessions > 0 and session_registry.count_for_user(user.id) >= max_sessions:
            raise SessionError(f"你的并发会话数已达系统上限（{max_sessions}）")

        policy = resolve_policy_for(grant)
        frozen = freeze_policy(policy)
        policy_name = policy.name if policy is not None else "未绑定策略"

        user_snapshot = {
            "id": user.id,
            "username": user.username,
            "role_code": user.role_code,
            "display_name": user.display_name or user.username,
        }
        account_snapshot = {
            "id": account.id,
            "name": account.name,
            "username": account.username,
        }
        grant_id = grant.id

        # 协议分流：Linux 走 SSH，Windows 走 WinRM。两者的桥公开面一致，网页终端
        # 前端、命令策略、CommandLog、转录、AI 助手与会话登记全部复用；rdp 有自己的
        # /api/rdp/* 入口，不会走到这里。
        # 一台主机可以有多个端点（Windows 常见 RDP + WinRM），所以调用方可以显式指定
        # protocol；没指定时按主机镜像协议挑，挑到 rdp 就退回第一个字符端点。
        requested = (protocol or "").strip().lower() or None
        endpoint = host.endpoint_for(requested or host.protocol)
        if endpoint is None or endpoint.protocol not in ("ssh", "winrm"):
            if requested:
                raise SessionError(
                    f"该主机没有「{requested}」这个端点"
                    f"（可用：{'、'.join(host.protocol_names()) or '无'}）"
                )
            endpoint = next(
                (item for item in host.protocol_endpoints() if item.protocol in ("ssh", "winrm")),
                endpoint,
            )
        if endpoint is None or endpoint.protocol not in ("ssh", "winrm"):
            fallback_name = (endpoint.protocol if endpoint else host.protocol) or "未知"
            raise SessionError(
                f"该主机的协议「{fallback_name}」不支持交互式 shell（只有 ssh / winrm 能进网页终端）"
            )
        protocol = endpoint.protocol
        endpoint_port = endpoint.port
        endpoint_transport = endpoint.winrm_transport

        host_snapshot = {
            "id": host.id,
            "name": host.name,
            "address": host.address,
            "port": endpoint_port,
        }

        target = None
        winrm_target = None
        if protocol == "winrm":
            winrm_target = build_winrm_target(
                host,
                account,
                port=endpoint_port,
                winrm_transport=endpoint_transport,
                connect_timeout=Config.SSH_CONNECT_TIMEOUT,
                command_timeout=Config.COMMAND_TIMEOUT,
            )
        else:
            target = build_target(
                host,
                account,
                port=endpoint_port,
                connect_timeout=Config.SSH_CONNECT_TIMEOUT,
                banner_timeout=Config.SSH_BANNER_TIMEOUT,
                keepalive=Config.SSH_KEEPALIVE,
            )

    # ---- 连接目标机（网络动作放在应用上下文之外，避免长时间占用连接）
    try:
        connection = connect_winrm(winrm_target) if protocol == "winrm" else connect(target)
    except (SSHError, WinrmError) as exc:
        with app.app_context():
            log_event(
                "session",
                "connect_failed",
                result="failure",
                message=str(exc),
                target_type="host",
                target_id=host_snapshot["id"],
                target_name=host_snapshot["name"],
                actor_id=user_snapshot["id"],
                actor_username=user_snapshot["username"],
                actor_role=user_snapshot["role_code"],
                ip=client_ip,
            )
        raise SessionError(str(exc)) from exc

    sid = gen_sid()
    transcript_path = os.path.join(os.fspath(Config.TRANSCRIPT_DIR), f"{sid}.log")

    with app.app_context():
        record = new_session(
            sid=sid,
            user_id=user_snapshot["id"],
            username=user_snapshot["username"],
            role_code=user_snapshot["role_code"],
            host_id=host_snapshot["id"],
            host_name=host_snapshot["name"],
            host_address=host_snapshot["address"],
            account_id=account_snapshot["id"],
            account_username=account_snapshot["username"],
            grant_id=grant_id,
            source=source,
            protocol=protocol,
            client_ip=client_ip,
            client_port=client_port,
            status="active",
            transcript_path=transcript_path,
        )
        record_id = record.id
        log_event(
            "session",
            "session_open",
            result="success",
            message=f"{user_snapshot['username']} 通过{'网关' if source == 'gateway' else '网页终端'}登录 {host_snapshot['name']}",
            target_type="host",
            target_id=host_snapshot["id"],
            target_name=host_snapshot["name"],
            actor_id=user_snapshot["id"],
            actor_username=user_snapshot["username"],
            actor_role=user_snapshot["role_code"],
            ip=client_ip,
            detail={"sid": sid, "source": source, "account": account_snapshot["username"], "policy": policy_name},
        )

    recorder = TranscriptRecorder(sid, path=transcript_path)

    channel = None
    try:
        if protocol == "ssh":
            channel = open_shell(connection, width=cols, height=rows)
    except SSHError as exc:
        close_conn(connection)
        with app.app_context():
            stale = SessionRecord.query.filter_by(sid=sid).first()
            if stale is not None:
                close_session(stale, status="failed", reason=str(exc)[:120])
        recorder.close("打开远端 shell 失败")
        raise SessionError(str(exc)) from exc

    def _handle_closed(reason: str) -> None:
        session_registry.unregister(sid)
        try:
            with app.app_context():
                stale = SessionRecord.query.filter_by(sid=sid).first()
                if stale is not None and stale.status == "active":
                    close_session(
                        stale,
                        status="closed",
                        reason=reason[:120],
                        bytes_in=bridge_stats.get("bytes_in"),
                        bytes_out=bridge_stats.get("bytes_out"),
                    )
        except Exception:  # noqa: BLE001
            log.exception("关闭会话记录失败 sid=%s", sid)
        if on_closed is not None:
            try:
                on_closed(reason)
            except Exception:  # noqa: BLE001
                pass

    bridge_stats: dict = {}

    def _on_dead(reason: str) -> None:
        bridge_stats.update(bridge.stats())
        _handle_closed(reason)

    # 运行参数存在 system_settings 表里（settings_store.get_int 会查库），
    # 而上面的 app_context 在会话记录落库后就退出了 —— 这里必须再进一次，
    # 否则每个会话都会撞 "Working outside of application context"。
    store_command = build_command_callback(app, sid)

    def _on_command(event: dict) -> None:
        """先落库（内部回调），再把命令事件转发给外部订阅者（网页终端实时审计表）。"""
        store_command(event)
        if on_command is not None:
            try:
                on_command(event)
            except Exception:  # noqa: BLE001 - 推送失败不能影响审计落库与终端
                log.exception("外部命令回调异常 sid=%s", sid)

    with app.app_context():
        config = BridgeConfig(
            sid=sid,
            policy=frozen,
            recorder=recorder,
            host_label=f"{host_snapshot['name']}",
            channel_kind=source,
            command_timeout=get_int("command_timeout", Config.COMMAND_TIMEOUT) or 0,
            max_output_bytes=Config.MAX_OUTPUT_BYTES,
            on_output=send_output,
            on_command=_on_command,
            on_notice=notify,
            on_close=_on_dead,
        )
    if protocol == "winrm":
        # WinRM 没有 PTY 通道：连接本身就是桥的输入（每条命令单独起一个短命进程）。
        bridge = WinrmBridge(connection, config, session=None)
    else:
        bridge = ShellBridge(channel, config, session=None)

    session_registry.register(
        sid,
        kind=source,
        user_id=user_snapshot["id"],
        username=user_snapshot["username"],
        host_id=host_snapshot["id"],
        host_name=host_snapshot["name"],
        account_username=account_snapshot["username"],
        grant_id=grant_id,
        client_ip=client_ip,
        bridge=bridge,
        policy_name=policy_name,
    )

    # 必须在 bridge.start() 之前回调：start() 里会注入 PS1 并启动读数线程，
    # 目标机输出从那一刻起就会回流给用户。晚一步，MOTD/提示符就会抢在
    # 堡垒机的「已连接」横幅前面出现（实测缺陷）。
    if on_ready is not None:
        ready_info = {
            "sid": sid,
            "hostName": host_snapshot["name"],
            "address": f"{host_snapshot['address']}:{host_snapshot['port']}",
            "accountUsername": account_snapshot["username"],
            "policyName": policy_name,
        }
        try:
            on_ready(sid, ready_info)
        except Exception:  # noqa: BLE001 - 回调异常不能拖垮会话建立
            log.exception("on_ready 回调异常 sid=%s", sid)

    try:
        segmented = bridge.start()
    except Exception as exc:  # noqa: BLE001
        session_registry.unregister(sid)
        _close_target_connection(protocol, connection)
        recorder.close("初始化终端失败")
        with app.app_context():
            stale = SessionRecord.query.filter_by(sid=sid).first()
            if stale is not None:
                close_session(stale, status="failed", reason=str(exc)[:120])
        raise SessionError(f"初始化终端失败：{exc}") from exc

    return OpenedSession(
        sid=sid,
        record_id=record_id,
        bridge=bridge,
        connection=connection,
        segmented=segmented,
        host_name=host_snapshot["name"],
        account_username=account_snapshot["username"],
        policy_name=policy_name,
        meta={
            "source": source,
            "protocol": protocol,
            "client_ip": client_ip,
            "user": user_snapshot,
            "host": host_snapshot,
            "account": account_snapshot,
        },
        app=app,
    )


def teardown_session(opened: OpenedSession | None, reason: str = "用户断开连接") -> None:
    if opened is None:
        return
    try:
        opened.bridge.stop(reason)
    except Exception:  # noqa: BLE001
        pass
    session_registry.unregister(opened.sid)
    _close_target_connection(str(opened.meta.get("protocol") or "ssh"), opened.connection)
    _close_record(opened, reason)


def _close_record(opened: OpenedSession, reason: str) -> None:
    """主动断开时同步把会话记录收口。

    桥接线程的 on_close 是异步的（远端 EOF/异常才触发），主动 teardown 不能等它 ——
    否则进程退出后记录会永远停在 active，审计上等于「这条会话没有结束」。
    重复收口是安全的：状态已经不是 active 就直接返回。
    """
    try:
        app = opened.app
        if app is None:
            app = current_app._get_current_object()
    except RuntimeError:
        log.debug("teardown 时没有应用上下文，跳过会话记录收口 sid=%s", opened.sid)
        return
    try:
        stats = opened.bridge.stats() or {}
    except Exception:  # noqa: BLE001
        stats = {}
    try:
        with app.app_context():
            record = SessionRecord.query.filter_by(sid=opened.sid).first()
            if record is None or record.status != "active":
                return
            close_session(
                record,
                status="closed",
                reason=(reason or "用户断开连接")[:120],
                bytes_in=stats.get("bytes_in"),
                bytes_out=stats.get("bytes_out"),
            )
    except Exception:  # noqa: BLE001
        log.exception("收口会话记录失败 sid=%s", opened.sid)


def reconcile_stale_sessions(app) -> int:
    """服务重启后，把残留的 active 会话标记为已中断。"""
    with app.app_context():
        rows = SessionRecord.query.filter_by(status="active").all()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        for row in rows:
            row.status = "closed"
            row.end_reason = "堡垒机服务重启，会话已中断"
            row.ended_at = now
            if row.started_at:
                started = row.started_at
                if started.tzinfo is not None:
                    started = started.replace(tzinfo=None)
                row.duration_seconds = int((now - started).total_seconds())
            session_registry.unregister(row.sid)
        if rows:
            db.session.commit()
        return len(rows)


def check_command(policy_id: int | None, command: str):
    """策略试算（后台「命令测试」用）。"""
    policy = db.session.get(CommandPolicy, policy_id) if policy_id else None
    if policy is None:
        policy = CommandPolicy.query.filter_by(is_default=True).first()
    return evaluate_policy(freeze_policy(policy), command)


__all__ = [
    "OpenedSession",
    "SessionError",
    "check_command",
    "in_time_window",
    "open_session",
    "reconcile_stale_sessions",
    "resolve_policy_for",
    "teardown_session",
]
