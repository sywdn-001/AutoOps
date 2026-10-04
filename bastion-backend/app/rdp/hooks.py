"""把 WebRDP 网关的动作接到堡垒机的会话记录与审计上。

网关（`app/rdp/proxy.py`）自己不碰数据库：它只认 `RdpConnectionHooks` 这三个回调，
由这里注入「在 app context 里写库」的实现。这样网关的协议逻辑可以脱离 Flask 单测，
而「谁在什么时候用哪个账号连了哪台 Windows 机器」照旧一条不落地进会话表与审计表。
"""

from __future__ import annotations

import logging

from ..audit import close_session, log_event, new_session
from ..extensions import db
from ..models import SessionRecord
from .proxy import RdpConnectionHooks, RdpTicket

logger = logging.getLogger(__name__)


def build_hooks(app) -> RdpConnectionHooks:
    """构造一套绑定到 `app` 的 hooks（每次 WebSocket 连接现取一份）。"""

    def _run(func, *args, **kwargs):
        with app.app_context():
            try:
                return func(*args, **kwargs)
            finally:
                db.session.remove()

    def _open_session(ticket: RdpTicket, meta: dict):
        """开一条 RDP 会话记录，只把 **会话行 id** 交给网关。

        不能把 ORM 实例交出去：`_run()` 结束时会 `db.session.remove()`，实例随即 detach，
        收口时再碰它会抛 `DetachedInstanceError`（线上就是这么丢过一次 `rdp_session_close`）。
        """

        def _open():
            record = new_session(
                user_id=ticket.user_id,
                username=ticket.username,
                role_code=ticket.role_code,
                host_id=ticket.host_id,
                host_name=ticket.host_name,
                host_address=f"{ticket.host_address}:{ticket.port}",
                account_id=ticket.account_id,
                account_username=ticket.account_username,
                grant_id=ticket.grant_id,
                source="web",
                protocol="rdp",
                client_ip=ticket.client_ip,
                status="active",
            )
            log_event(
                "session",
                "rdp_session_open",
                message=(
                    f"{ticket.username} 打开 {ticket.host_name} 的远程桌面"
                    f"（账号 {ticket.account_username or '-'}）"
                ),
                target_type="host",
                target_id=ticket.host_id,
                target_name=ticket.host_name,
                actor_id=ticket.user_id,
                actor_username=ticket.username,
                actor_role=ticket.role_code,
                ip=ticket.client_ip,
                detail={
                    "protocol": "rdp",
                    "account": ticket.account_username,
                    "destination": f"{ticket.host_address}:{ticket.port}",
                    "negotiation": meta.get("negotiation"),
                    "serverCertificate": meta.get("certificate") or {},
                },
            )
            # 交出去的是纯数据字典（不是 ORM 实例）：_run 结束时 db.session.remove()
            # 会让实例 detach，线上就是这么丢过一次 rdp_session_close。带上 sid 是为了
            # 让中继把这条会话登记进在线会话表（管理员才能在「会话记录」里中断它）。
            return {"id": record.id, "sid": record.sid}

        return _run(_open)

    def _close_session(session, counters: dict) -> None:
        """收口一条远程桌面会话：写字节数、结束原因与 `rdp_session_close` 审计。

        `session` 是 `_open_session()` 交出来的引用（`{"id", "sid"}`），兼容历史上
        只回行 id 的写法。被管理员中断或空闲清理时（`counters["forced"]`）状态写成
        `terminated`，跟 SSH 会话被踢时的口径一致。
        """

        def _close():
            session_id = session.get("id") if isinstance(session, dict) else session
            record = db.session.get(SessionRecord, session_id) if session_id else None
            reason = (counters.get("reason") or "").strip() or "远程桌面会话结束"
            status = "terminated" if counters.get("forced") else "closed"
            if record is not None:
                close_session(
                    record,
                    status=status,
                    reason=reason,
                    bytes_in=int(counters.get("fromClient") or 0),
                    bytes_out=int(counters.get("fromServer") or 0),
                )
            log_event(
                "session",
                "rdp_session_close",
                message=(
                    f"{getattr(record, 'username', '') or ''} 的远程桌面会话结束：{reason}"
                ),
                target_type="host",
                target_id=getattr(record, "host_id", None),
                target_name=getattr(record, "host_name", "") or "",
                actor_id=getattr(record, "user_id", None),
                actor_username=getattr(record, "username", "") or "",
                actor_role=getattr(record, "role_code", "") or "",
                ip=getattr(record, "client_ip", "") or "",
                detail={
                    "sid": getattr(record, "sid", ""),
                    "bytesFromClient": counters.get("fromClient", 0),
                    "bytesFromServer": counters.get("fromServer", 0),
                },
            )

        _run(_close)

    def _audit(ticket: RdpTicket, action: str, message: str, *, result: str = "denied") -> None:
        def _write():
            log_event(
                "session",
                action,
                result=result,
                message=f"{ticket.username} 的远程桌面连接被拒绝：{message}"
                if result == "denied"
                else f"{ticket.username} 的远程桌面连接失败：{message}",
                target_type="host",
                target_id=ticket.host_id,
                target_name=ticket.host_name,
                actor_id=ticket.user_id,
                actor_username=ticket.username,
                actor_role=ticket.role_code,
                ip=ticket.client_ip,
                detail={"protocol": "rdp", "destination": f"{ticket.host_address}:{ticket.port}"},
            )

        _run(_write)

    return RdpConnectionHooks(
        open_session=_open_session,
        close_session=_close_session,
        audit=_audit,
    )
