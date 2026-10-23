"""浏览器内嵌 mstsc 画面桥：connect / frame / input / disconnect。

继续复用宿主的 ``app.rdp.window_bridge``（模块与宿主同进程，这是契约允许的），
只把路由挂到模块前缀下。模块停用时会主动关掉自己打开过的桥，避免留下 mstsc 窗口。
"""

from __future__ import annotations

from flask import Response, request

from autoops import ModuleApiError

FRAME_WIDTH = 1280
FRAME_HEIGHT = 800

#: 本模块打开过的会话 id，停用/卸载时统一关闭。
_OPEN_SIDS: set[str] = set()


def _actor():
    try:
        from app.security import load_actor

        actor = load_actor()
    except Exception:  # noqa: BLE001
        actor = None
    if actor is None:
        raise ModuleApiError("登录状态已失效", status=401, code="UNAUTHORIZED")
    return actor


def _session_or_error(sid):
    from app.models import SessionRecord

    if not sid:
        raise ModuleApiError("缺少会话 id", status=400, code="INVALID_ARGUMENT")
    record = SessionRecord.query.filter_by(sid=str(sid)).first()
    if record is None:
        raise ModuleApiError("会话不存在或已结束", status=404, code="SESSION_NOT_FOUND")
    return record


def _rdp_endpoint_port(host) -> int:
    try:
        for endpoint in host.protocol_endpoints():
            if (endpoint.protocol or "").lower() == "rdp" and endpoint.port:
                return int(endpoint.port)
    except Exception:  # noqa: BLE001
        pass
    return 3389


def shutdown() -> None:
    """关闭本模块打开的全部桥；模块停用时调用，永不抛。"""
    from app.rdp import window_bridge

    for sid in list(_OPEN_SIDS):
        try:
            window_bridge.close_bridge(sid)
        except Exception:  # noqa: BLE001
            pass
        _OPEN_SIDS.discard(sid)


def register(ctx) -> None:
    @ctx.route("/connect", methods=("POST",), permission="rdp:use")
    def connect():
        from app.audit import log_event
        from app.crypto import decrypt
        from app.extensions import db
        from app.models import Host, HostAccount
        from app.rdp import window_bridge

        actor = _actor()
        payload = request.get_json(silent=True) or {}
        record = _session_or_error(payload.get("sid"))

        host = db.session.get(Host, record.host_id)
        if host is None:
            raise ModuleApiError("会话对应的主机不存在", status=400, code="HOST_MISSING")
        account = db.session.get(HostAccount, record.account_id)
        if account is None:
            raise ModuleApiError("会话对应的账号不存在", status=400, code="ACCOUNT_MISSING")
        try:
            password = decrypt(account.secret_enc)
        except Exception as exc:  # noqa: BLE001
            raise ModuleApiError(
                f"取用账号口令失败：{exc}", status=500, code="DECRYPT_FAILED"
            ) from exc

        port = _rdp_endpoint_port(host)
        try:
            window_bridge.open_bridge(
                record.sid, host.address, port, account.username, password
            )
        except window_bridge.BridgeError as exc:
            raise ModuleApiError(
                f"打开远程桌面失败：{exc}", status=502, code="BRIDGE_FAILED"
            ) from exc
        except Exception as exc:  # noqa: BLE001
            ctx.logger.warning("打开远程桌面桥失败", exc_info=True)
            raise ModuleApiError(
                f"打开远程桌面失败：{exc}", status=500, code="BRIDGE_ERROR"
            ) from exc

        _OPEN_SIDS.add(record.sid)
        try:
            log_event(
                "session",
                "rdp_credential_reveal",
                message=(
                    f"{actor.username} 的 RDP 网关会话 {record.sid} 取用了 "
                    f"{record.host_name} 的账号口令（{account.username}）"
                ),
                target_type="host",
                target_id=record.host_id,
                target_name=record.host_name or "",
                actor=actor,
                detail={
                    "protocol": "rdp",
                    "source": "gateway-bridge",
                    "sid": record.sid,
                },
                ip=request.remote_addr or "",
            )
        except Exception:  # noqa: BLE001
            ctx.logger.warning("写远程桌面桥审计失败", exc_info=True)

        return {
            "sid": record.sid,
            "host": f"{host.address}:{port}",
            "width": FRAME_WIDTH,
            "height": FRAME_HEIGHT,
        }

    @ctx.route("/frame", methods=("GET",), permission="rdp:use")
    def frame():
        from app.rdp import window_bridge

        sid = request.args.get("sid") or ""
        bridge = window_bridge.bridge_for(sid) if sid else None
        if bridge is None:
            return Response(b"", status=204)
        try:
            payload = bridge.frame(full=bool(request.args.get("full")))
        except Exception:  # noqa: BLE001
            ctx.logger.debug("抓取远程桌面画面失败", exc_info=True)
            return Response(b"", status=204)
        if not payload:
            return Response(b"", status=204)
        return Response(payload, status=200, mimetype="application/octet-stream")

    @ctx.route("/input", methods=("POST",), permission="rdp:use")
    def send_input():
        from app.rdp import window_bridge

        payload = request.get_json(silent=True) or {}
        sid = payload.get("sid")
        events = payload.get("events") or []
        bridge = window_bridge.bridge_for(str(sid)) if sid else None
        if bridge is None:
            raise ModuleApiError("远程桌面连接尚未建立", status=404, code="NO_BRIDGE")
        if not isinstance(events, list):
            raise ModuleApiError("events 必须是数组", status=400, code="INVALID_ARGUMENT")
        try:
            bridge.send_input(events)
        except Exception as exc:  # noqa: BLE001
            raise ModuleApiError(
                f"转发输入失败：{exc}", status=500, code="INPUT_FAILED"
            ) from exc
        return {"forwarded": len(events)}

    @ctx.route("/disconnect", methods=("POST",), permission="rdp:use")
    def disconnect():
        from app.audit import log_event
        from app.rdp import window_bridge

        payload = request.get_json(silent=True) or {}
        sid = payload.get("sid")
        if sid:
            _OPEN_SIDS.discard(str(sid))
        closed = window_bridge.close_bridge(str(sid)) if sid else False
        if closed:
            try:
                actor = _actor()
                log_event(
                    "session",
                    "rdp_gateway_close",
                    message=f"{actor.username} 关闭了 RDP 网关会话 {sid}",
                    target_type="session",
                    target_id=sid,
                    actor=actor,
                    detail={"protocol": "rdp", "source": "gateway-bridge"},
                    ip=request.remote_addr or "",
                )
            except Exception:  # noqa: BLE001
                ctx.logger.warning("写关闭会话审计失败", exc_info=True)
        return {"closed": bool(closed)}

    @ctx.route("/sessions/<sid>/close", methods=("POST",), permission="rdp:use")
    def close_session_record(sid):
        """在模块页面「断开」一条会话：关桥 + 把会话记录收口成 closed。

        ``/disconnect`` 只关画面桥（Rust 客户端回到选单时也会调它），这里额外闭合
        数据库里的 SessionRecord，模块首页的「在线会话」才会随之减少。
        """
        from app.audit import close_session, log_event
        from app.rdp import window_bridge
        from app.security import is_admin as _is_admin

        actor = _actor()
        record = _session_or_error(sid)
        if not _is_admin(actor) and int(record.user_id or 0) != int(actor.id):
            raise ModuleApiError("只能断开自己的会话", status=403, code="FORBIDDEN")

        _OPEN_SIDS.discard(record.sid)
        try:
            window_bridge.close_bridge(record.sid)
        except Exception:  # noqa: BLE001
            ctx.logger.warning("关闭远程桌面桥失败", exc_info=True)

        close_session(record, status="closed", reason="用户在模块页面断开")
        try:
            log_event(
                "session",
                "rdp_gateway_close",
                message=f"{actor.username} 断开了 RDP 网关会话 {record.sid}",
                target_type="session",
                target_id=record.sid,
                actor=actor,
                detail={"protocol": "rdp", "source": "gateway-bridge"},
                ip=request.remote_addr or "",
            )
        except Exception:  # noqa: BLE001
            ctx.logger.warning("写断开会话审计失败", exc_info=True)

        return {"closed": True, "sid": record.sid, "status": "closed"}
