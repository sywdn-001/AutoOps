"""RDP 网关进程生命周期：启动、停止、状态、诊断、设置与日志。

这里从旧 `app/api/rdp_gateway.py` 的进程管理部分移植而来，差异只有三类：

1. 路径与设置全部走模块自己的命名空间（``mod.rdp-gateway.*``、数据目录
   ``ctx.data_dir``），不再读写宿主的 ``rdp_gateway_*`` 设置；
2. 所有文件（pid、运行时状态、日志）都落在 ``ctx.data_dir``，模块卸载时随
   模块目录一起清理；
3. 进程启停做成**幂等**：重复 start 不会产生第二个子进程；模块停用/宿主退出
   时通过 ``ctx.on("disable")``、顶层 ``on_disable``/``teardown`` 与 ``atexit``
   三层兜底收掉子进程。
"""

from __future__ import annotations

import atexit
import json
import os
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from autoops import ModuleApiError

#: 与 description.json 的 settings[].key 一致（ctx 会自动加 mod.<id>. 前缀）。
SETTING_ENABLED = "enabled"
SETTING_PORT = "port"
SETTING_AUTO_START = "autoStart"

DEFAULT_PORT = 4489
MIN_PORT = 1
MAX_PORT = 65535

BIN_NAME = "bastion-rdp-gateway.exe" if os.name == "nt" else "bastion-rdp-gateway"

_LOCK = threading.RLock()
_CTX = None
_TAIL_THREAD: threading.Thread | None = None
_TAIL_STOP: threading.Event | None = None
_ATEXIT_REGISTERED = False


# ---------------------------------------------------------------------------
# 设置读取：ctx.get_setting 返回的一律是字符串，模块自己转类型
# ---------------------------------------------------------------------------
_TRUTHY = frozenset({"1", "true", "yes", "on", "y", "t"})


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in _TRUTHY


def _as_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def settings_snapshot(ctx) -> dict:
    """读取三个设置项，任何一项异常都退回声明默认值。"""
    try:
        enabled = _as_bool(ctx.get_setting(SETTING_ENABLED, "1"), True)
    except Exception:  # noqa: BLE001
        enabled = True
    try:
        port = _as_int(ctx.get_setting(SETTING_PORT, str(DEFAULT_PORT)), DEFAULT_PORT, MIN_PORT, MAX_PORT)
    except Exception:  # noqa: BLE001
        port = DEFAULT_PORT
    try:
        auto_start = _as_bool(ctx.get_setting(SETTING_AUTO_START, "0"), False)
    except Exception:  # noqa: BLE001
        auto_start = False
    return {"enabled": enabled, "port": port, "autoStart": auto_start}


# ---------------------------------------------------------------------------
# 路径与二进制解析
# ---------------------------------------------------------------------------
def data_dir(ctx) -> Path:
    return Path(ctx.data_dir)


def log_path(ctx) -> Path:
    return data_dir(ctx) / "gateway.log"


def pid_path(ctx) -> Path:
    return data_dir(ctx) / "gateway.pid"


def runtime_path(ctx) -> Path:
    return data_dir(ctx) / "runtime.json"


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        try:
            key = os.path.normcase(str(path))
        except (TypeError, ValueError):
            continue
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def binary_candidates(ctx) -> list[Path]:
    """按优先级列出网关可执行文件可能的位置。

    优先级：``BASTION_RDP_GATEWAY_BIN`` 环境变量 → 模块包 ``bin/`` → 仓库里
    ``bastion-rdp-gateway/target/release/``（方便源码开发时直接跑）。
    """
    candidates: list[Path] = []
    env_path = os.environ.get("BASTION_RDP_GATEWAY_BIN")
    if env_path:
        candidates.append(Path(env_path))

    candidates.append(Path(ctx.package_dir) / "bin" / BIN_NAME)

    bases: list[Path] = []
    try:
        bases.append(Path(__file__).resolve())
    except OSError:
        pass
    try:
        bases.append(Path.cwd().resolve())
    except OSError:
        pass
    try:
        bases.append(Path(ctx.package_dir).resolve())
    except OSError:
        pass
    for base in bases:
        for parent in (base, *base.parents):
            candidates.append(parent / "bastion-rdp-gateway" / "target" / "release" / BIN_NAME)
            # 到仓库根就停，避免把整个盘符的每一层都扫一遍
            if parent.name.lower() == "autoops":
                break
    return _dedupe_paths(candidates)


def resolve_binary(ctx) -> Path:
    """返回第一个真实存在的候选；都不存在时返回模块包内路径（便于报错）。"""
    candidates = binary_candidates(ctx)
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return candidates[1] if len(candidates) > 1 else Path(ctx.package_dir) / "bin" / BIN_NAME


# ---------------------------------------------------------------------------
# PID / 运行时状态
# ---------------------------------------------------------------------------
def _utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def read_runtime(ctx) -> dict:
    runtime: dict = {}
    try:
        raw = json.loads(runtime_path(ctx).read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            runtime = raw
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError):
        runtime = {}
    if not runtime.get("pid"):
        pid = _read_pid_file(ctx)
        if pid:
            runtime["pid"] = pid
    return runtime


def _read_pid_file(ctx) -> int | None:
    try:
        text = pid_path(ctx).read_text(encoding="utf-8").strip()
        value = int(text)
        return value if value > 0 else None
    except (FileNotFoundError, OSError, ValueError):
        return None


def write_runtime(ctx, *, pid: int, port: int, started_at: str, binary: Path) -> None:
    directory = data_dir(ctx)
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": int(pid),
        "port": int(port),
        "startedAt": started_at,
        "binary": str(binary),
        "updatedAt": _utc_now_iso(),
    }
    try:
        runtime_path(ctx).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        pid_path(ctx).write_text(str(int(pid)), encoding="utf-8")
    except OSError:
        # 写不进 pid 文件只影响重启后的进程发现，不应该让启动失败
        if _CTX is not None:
            _CTX.logger.warning("写入网关运行时状态失败：%s", directory)


def clear_runtime(ctx) -> None:
    for path in (runtime_path(ctx), pid_path(ctx)):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def pid_alive(pid: int | None) -> bool:
    """判断 pid 是否仍然存活且确实是本网关进程。"""
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        try:
            completed = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                encoding="mbcs",
                errors="replace",
                timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        output = (completed.stdout or "").lower()
        return "bastion-rdp-gateway" in output
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def _read_pid(ctx) -> int | None:
    runtime = read_runtime(ctx)
    try:
        pid = int(runtime.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    return pid if pid > 0 else None


def _listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.5):
            return True
    except (OSError, ValueError):
        return False


def _adopt_pid(ctx, port: int) -> int | None:
    """pid 文件丢失但端口在监听时，用 netstat 反查监听进程。"""
    if os.name != "nt":
        return None
    try:
        completed = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True,
            text=True,
            encoding="mbcs",
            errors="replace",
            timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    suffix = f":{int(port)}"
    for line in (completed.stdout or "").splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[3].upper() != "LISTENING":
            continue
        if not parts[1].endswith(suffix):
            continue
        try:
            pid = int(parts[4])
        except ValueError:
            continue
        if pid_alive(pid):
            write_runtime(
                ctx,
                pid=pid,
                port=int(port),
                started_at=_utc_now_iso(),
                binary=resolve_binary(ctx),
            )
            return pid
    return None


def running_info(ctx, *, fallback_port: int | None = None) -> dict:
    runtime = read_runtime(ctx)
    try:
        runtime_port = int(runtime.get("port") or 0)
    except (TypeError, ValueError):
        runtime_port = 0
    pid = _read_pid(ctx)
    if not pid_alive(pid):
        pid = None
        port_for_adopt = runtime_port or fallback_port
        if port_for_adopt and _listening(port_for_adopt):
            pid = _adopt_pid(ctx, port_for_adopt)
            if pid:
                runtime_port = port_for_adopt
        if not pid:
            return {
                "pid": None,
                "runningPort": None,
                "listening": False,
                "startedAt": None,
                "binary": None,
            }
    listening = _listening(runtime_port) if runtime_port else False
    return {
        "pid": pid,
        "runningPort": runtime_port or None,
        "listening": listening,
        "startedAt": runtime.get("startedAt"),
        "binary": runtime.get("binary"),
    }


# ---------------------------------------------------------------------------
# 本机地址 / 管理员判定 / 审计
# ---------------------------------------------------------------------------
def local_ip() -> str:
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.2)
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        pass
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return "127.0.0.1"


def local_ips() -> list[str]:
    values: list[str] = []
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        infos = []
    for info in infos:
        try:
            address = info[4][0]
        except (IndexError, TypeError):
            continue
        if not address or address.startswith("127."):
            continue
        if address not in values:
            values.append(address)
    if not values:
        values.append(local_ip())
    return values


def is_admin() -> bool:
    try:
        from app.security import is_admin as _is_admin
        from app.security import load_actor

        return bool(_is_admin(load_actor()))
    except Exception:  # noqa: BLE001 - 无请求上下文/未登录都按非管理员处理
        return False


def _current_actor():
    """当前请求的登录主体；无请求上下文（后台线程/模块钩子）时返回 None。"""
    try:
        from flask import has_request_context

        if not has_request_context():
            return None
        from app.security import load_actor

        return load_actor()
    except Exception:  # noqa: BLE001 - 只用于展示，不参与鉴权
        return None


def _active_gateway_sessions(actor) -> int:
    """当前账号能看到的、仍处于 active 的 RDP 网关会话数。

    管理员看全部；普通账号只看自己的。查询失败不影响状态接口。
    """
    if actor is None:
        return 0
    try:
        from app.models import SessionRecord

        query = SessionRecord.query.filter_by(
            protocol="rdp", source="gateway", status="active"
        )
        if not is_admin():
            query = query.filter_by(user_id=actor.id)
        return int(query.count())
    except Exception:  # noqa: BLE001
        return 0


def require_admin():
    try:
        from app.security import is_admin as _is_admin
        from app.security import load_actor

        actor = load_actor()
    except Exception:  # noqa: BLE001
        raise ModuleApiError("登录状态已失效", status=401, code="UNAUTHORIZED") from None
    if actor is None:
        raise ModuleApiError("登录状态已失效", status=401, code="UNAUTHORIZED")
    if not _is_admin(actor):
        raise ModuleApiError("仅管理员可执行该操作", status=403, code="ADMIN_ONLY")
    return actor


def _audit(ctx, action: str, message: str, detail: dict | None = None) -> None:
    """网关启停审计；autoStart（后台线程、无请求上下文）时静默跳过。"""
    try:
        from flask import has_request_context, request

        if not has_request_context():
            return
        from app.audit import log_event
        from app.security import load_actor

        actor = load_actor()
        forwarded = request.headers.get("X-Forwarded-For", "")
        ip = forwarded.split(",")[0].strip() if forwarded else (request.remote_addr or "")
        log_event(
            "session",
            action,
            message=message,
            target_type="module",
            target_id=ctx.id,
            target_name="RDP 网关",
            actor=actor,
            detail={"moduleId": ctx.id, **(detail or {})},
            ip=ip,
        )
    except Exception:  # noqa: BLE001 - 审计失败不能影响启停本身
        ctx.logger.warning("写 RDP 网关审计失败：%s", action, exc_info=True)


# ---------------------------------------------------------------------------
# 进程启停
# ---------------------------------------------------------------------------
def _creation_flags() -> int:
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def _backend_api_base() -> str:
    """告诉网关二进制「宿主 Flask 在哪」。

    网关和宿主的后端调用走的是裸 HTTP，所以必须给 host:port：
    - 有请求上下文时用 ``request.host_url``（用户实际访问的地址，反代场景也对）；
    - 模块自动启动（无请求）时退回 ``BASTION_API_BASE`` / ``BASTION_API_PORT``，
      都没有就按 Flask 开发服务器默认的 5000 端口。
    """
    try:
        from flask import has_request_context, request

        if has_request_context():
            base = (request.host_url or "").strip().rstrip("/")
            if base:
                return base
    except Exception:  # noqa: BLE001
        pass
    base = (os.environ.get("BASTION_API_BASE") or "").strip().rstrip("/")
    if base:
        return base
    port = (os.environ.get("BASTION_API_PORT") or "5000").strip() or "5000"
    return f"http://127.0.0.1:{port}"


def _tail_lines(path: Path, limit: int) -> list[str]:
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 256 * 1024))
            data = handle.read()
    except OSError:
        return []
    text = data.decode("utf-8", errors="replace")
    return text.splitlines()[-limit:]


def _start_log_tail(ctx) -> None:
    """把网关进程新写出的日志行也送进模块 logger。

    宿主 ``GET /api/modules/<id>/logs`` 读的是 logger 的环形缓冲；只把 exe 的
    stdout 写文件的话那个接口永远只有启动/停止两条。这里做一个轻量 tail，
    保证页面能同时看到「进程日志」和「网关输出」。
    """
    global _TAIL_THREAD, _TAIL_STOP
    if _TAIL_THREAD is not None and _TAIL_THREAD.is_alive():
        return
    stop_event = threading.Event()
    _TAIL_STOP = stop_event
    path = log_path(ctx)

    def worker() -> None:
        try:
            handle = open(path, "r", encoding="utf-8", errors="replace")
        except OSError:
            return
        try:
            handle.seek(0, os.SEEK_END)
            while not stop_event.is_set():
                line = handle.readline()
                if line:
                    text = line.strip()
                    if text:
                        ctx.logger.info("gateway | %s", text[:2000])
                    continue
                stop_event.wait(0.5)
            # 退出前把最后几行读完，避免丢掉崩溃现场
            for line in handle.read().splitlines():
                text = line.strip()
                if text:
                    ctx.logger.info("gateway | %s", text[:2000])
        finally:
            try:
                handle.close()
            except OSError:
                pass

    thread = threading.Thread(
        target=worker, name="rdp-gateway-log-tail", daemon=True
    )
    _TAIL_THREAD = thread
    thread.start()


def _stop_log_tail() -> None:
    global _TAIL_THREAD, _TAIL_STOP
    if _TAIL_STOP is not None:
        _TAIL_STOP.set()
    thread = _TAIL_THREAD
    if thread is not None and thread.is_alive():
        thread.join(timeout=1.5)
    _TAIL_THREAD = None
    _TAIL_STOP = None


def _terminate_pid(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/F", "/T"],
            capture_output=True,
            text=True,
            encoding="mbcs",
            errors="replace",
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return
    import signal

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    for _ in range(50):
        if not pid_alive(pid):
            return
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _wait_pid_gone(pid: int, timeout: float = 6.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.1)
    return not pid_alive(pid)


def _wait_listening(port: int, proc: subprocess.Popen, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        if _listening(port):
            return True
        time.sleep(0.15)
    return _listening(port)


def _stop_locked(ctx, *, audit: bool = True, reason: str = "api") -> dict:
    pid = _read_pid(ctx)
    runtime = read_runtime(ctx)
    if not pid_alive(pid):
        # 尝试用运行时端口反查一次（pid 文件丢失但进程还活着的情况）
        try:
            running_port = int(runtime.get("port") or 0)
        except (TypeError, ValueError):
            running_port = 0
        if running_port and _listening(running_port):
            pid = _adopt_pid(ctx, running_port)
    if not pid_alive(pid):
        clear_runtime(ctx)
        _stop_log_tail()
        return {"stopped": False, "pid": None, "message": "网关没有在运行"}
    assert pid is not None
    try:
        _terminate_pid(pid)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ModuleApiError(f"停止失败：{exc}", status=500, code="STOP_FAILED") from exc
    if not _wait_pid_gone(pid):
        raise ModuleApiError(
            f"停止失败：进程 {pid} 仍在运行（可能权限不足）",
            status=500,
            code="STOP_FAILED",
        )
    clear_runtime(ctx)
    _stop_log_tail()
    if audit:
        _audit(
            ctx,
            "rdp_gateway_stop",
            f"{_actor_username()} 停止了 RDP 网关（进程 {pid}）",
            {"pid": pid, "reason": reason},
        )
    return {"stopped": True, "pid": pid, "message": "网关已停止"}


def _actor_username() -> str:
    try:
        from app.security import load_actor

        actor = load_actor()
        return getattr(actor, "username", "") or "admin"
    except Exception:  # noqa: BLE001
        return "admin"


def stop_gateway(ctx, *, audit: bool = True, reason: str = "api") -> dict:
    with _LOCK:
        return _stop_locked(ctx, audit=audit, reason=reason)


def shutdown(ctx, *, reason: str = "shutdown") -> None:
    """停用/卸载/进程退出时的兜底停止：永不抛异常。"""
    try:
        with _LOCK:
            _stop_locked(ctx, audit=False, reason=reason)
    except Exception:  # noqa: BLE001
        try:
            ctx.logger.warning("停止 RDP 网关进程失败（%s）", reason, exc_info=True)
        except Exception:  # noqa: BLE001
            pass


def start_gateway(ctx, *, trigger: str = "api") -> dict:
    config = settings_snapshot(ctx)
    if not config["enabled"]:
        raise ModuleApiError(
            "RDP 网关已在模块设置中停用，请先启用后再启动",
            status=400,
            code="MODULE_DISABLED",
        )
    port = int(config["port"])

    with _LOCK:
        info = running_info(ctx, fallback_port=port)
        if _listening(port):
            pid = info["pid"] or _adopt_pid(ctx, port)
            if pid:
                return {
                    "port": port,
                    "pid": pid,
                    "started": False,
                    "listening": True,
                    "message": "网关已经在运行",
                }
            raise ModuleApiError(
                f"端口 {port} 已被其他程序占用，请换一个端口或先结束占用进程",
                status=409,
                code="PORT_IN_USE",
            )
        if info["pid"]:
            # 进程活着但不在当前配置端口上（例如端口改过）：先收掉旧进程
            _stop_locked(ctx, audit=False, reason="restart")

        binary = resolve_binary(ctx)
        if not binary.is_file():
            raise ModuleApiError(
                f"找不到网关程序：{binary}。模块包应自带 bin/{BIN_NAME}；"
                "源码开发时可先在 bastion-rdp-gateway 目录执行 cargo build --release",
                status=400,
                code="BINARY_MISSING",
            )

        log_file_path = log_path(ctx)
        log_file_path.parent.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env["RDP_GATEWAY_PORT"] = str(port)
        env["BASTION_INSTANCE_DIR"] = str(data_dir(ctx))
        env["BASTION_API_BASE"] = _backend_api_base()
        env["BASTION_API_PREFIX"] = str(ctx.api_prefix)
        env.setdefault("RUST_LOG", "info")
        started_at = _utc_now_iso()
        try:
            with open(log_file_path, "ab") as log_handle:
                proc = subprocess.Popen(
                    [str(binary)],
                    cwd=str(binary.parent),
                    env=env,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    creationflags=_creation_flags(),
                )
        except OSError as exc:
            raise ModuleApiError(
                f"启动网关失败：{exc}", status=500, code="START_FAILED"
            ) from exc

        write_runtime(ctx, pid=proc.pid, port=port, started_at=started_at, binary=binary)
        _start_log_tail(ctx)
        listening = _wait_listening(port, proc)
        exit_code = proc.poll()
        if exit_code is not None:
            tail = "\n".join(_tail_lines(log_file_path, 8))
            clear_runtime(ctx)
            _stop_log_tail()
            raise ModuleApiError(
                "网关进程启动后立即退出（exit=%s）。%s"
                % (exit_code, tail[-1200:] or "日志为空，请检查 bin 目录与系统依赖"),
                status=500,
                code="START_FAILED",
            )
        message = "网关已启动" if listening else "进程已启动，端口尚未就绪，请稍后刷新"
        _audit(
            ctx,
            "rdp_gateway_start",
            f"{_actor_username()} 启动了 RDP 网关（端口 {port}）",
            {"pid": proc.pid, "port": port, "trigger": trigger},
        )
        ctx.logger.info("RDP 网关已启动：pid=%s port=%s binary=%s", proc.pid, port, binary)
        return {
            "port": port,
            "pid": proc.pid,
            "started": True,
            "listening": listening,
            "startedAt": started_at,
            "message": message,
        }


def start_in_background(ctx, *, trigger: str = "auto-start") -> None:
    """后台启动，异常只进日志（用于 autoStart / 宿主启动阶段）。"""

    def worker() -> None:
        try:
            with ctx.app.app_context():
                start_gateway(ctx, trigger=trigger)
        except Exception as exc:  # noqa: BLE001
            try:
                ctx.logger.warning("后台启动 RDP 网关失败：%s", exc)
            except Exception:  # noqa: BLE001
                pass

    threading.Thread(target=worker, name="rdp-gateway-autostart", daemon=True).start()


def _atexit_stop() -> None:
    ctx = _CTX
    if ctx is None:
        return
    try:
        shutdown(ctx, reason="atexit")
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# 响应体
# ---------------------------------------------------------------------------
def status_payload(ctx) -> dict:
    config = settings_snapshot(ctx)
    info = running_info(ctx, fallback_port=config["port"])
    pid = info["pid"]
    running_port = info["runningPort"] or config["port"]
    listening = _listening(running_port) if pid else _listening(config["port"])
    binary = resolve_binary(ctx)
    connect_port = running_port if pid else config["port"]
    host = local_ip()
    actor = _current_actor()
    return {
        "apiVersion": 3,
        "moduleId": ctx.id,
        "moduleVersion": ctx.version,
        "enabled": config["enabled"],
        "port": config["port"],
        "autoStart": config["autoStart"],
        "runningPort": running_port if pid else None,
        "connectPort": connect_port,
        "listening": bool(listening),
        "running": bool(listening),
        "pid": pid,
        "startedAt": info["startedAt"],
        "binary": str(binary),
        "binaryExists": binary.is_file(),
        "logFile": str(log_path(ctx)),
        "dataDir": str(data_dir(ctx)),
        "pidFile": str(pid_path(ctx)),
        "localIp": host,
        "localIps": local_ips(),
        "connectCommand": f"mstsc /v:{host}:{connect_port}",
        "isAdmin": is_admin(),
        "currentUser": getattr(actor, "username", None) if actor else None,
        "activeSessions": _active_gateway_sessions(actor),
        "platform": sys.platform,
    }


def diagnose_payload(ctx) -> dict:
    require_admin()
    config = settings_snapshot(ctx)
    info = running_info(ctx, fallback_port=config["port"])
    binary = resolve_binary(ctx)
    candidates = []
    for candidate in binary_candidates(ctx):
        try:
            exists = candidate.is_file()
        except OSError:
            exists = False
        candidates.append({"path": str(candidate), "exists": exists})
    return {
        "apiVersion": 3,
        "moduleId": ctx.id,
        "moduleVersion": ctx.version,
        "moduleFile": str(Path(__file__).resolve()),
        "packageDir": str(ctx.package_dir),
        "dataDir": str(data_dir(ctx)),
        "cwd": os.getcwd(),
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "binary": str(binary),
        "binaryExists": binary.is_file(),
        "candidates": candidates,
        "pidFile": str(pid_path(ctx)),
        "logFile": str(log_path(ctx)),
        "port": config["port"],
        "listening": info["listening"] if info["pid"] else _listening(config["port"]),
        "pid": info["pid"],
        "runningPort": info["runningPort"],
        "enabledSetting": ctx.get_setting(SETTING_ENABLED, "1"),
        "portSetting": ctx.get_setting(SETTING_PORT, str(DEFAULT_PORT)),
        "autoStartSetting": ctx.get_setting(SETTING_AUTO_START, "0"),
        "logTail": _tail_lines(log_path(ctx), 30),
    }


# ---------------------------------------------------------------------------
# 设置写接口
# ---------------------------------------------------------------------------
def _coerce_bool(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUTHY:
        return True
    if text in ("0", "false", "no", "off", "n", "f", ""):
        return False
    raise ModuleApiError(f"{field} 必须是布尔值", status=400, code="INVALID_ARGUMENT")


def update_settings(ctx, payload: dict) -> dict:
    require_admin()
    if not isinstance(payload, dict):
        raise ModuleApiError("请求体必须是 JSON 对象", status=400, code="INVALID_ARGUMENT")
    unknown = set(payload) - {"enabled", "port", "autoStart", "auto_start"}
    if unknown:
        raise ModuleApiError(
            "未知的设置项：%s（仅支持 enabled/port/autoStart）" % ", ".join(sorted(unknown)),
            status=400,
            code="INVALID_ARGUMENT",
        )

    before = settings_snapshot(ctx)
    changes: dict[str, str] = {}
    if "enabled" in payload:
        changes[SETTING_ENABLED] = "1" if _coerce_bool(payload["enabled"], "enabled") else "0"
    if "autoStart" in payload or "auto_start" in payload:
        raw = payload.get("autoStart", payload.get("auto_start"))
        changes[SETTING_AUTO_START] = "1" if _coerce_bool(raw, "autoStart") else "0"
    if "port" in payload:
        raw_port = payload["port"]
        if isinstance(raw_port, bool):
            raise ModuleApiError("端口必须是整数", status=400, code="INVALID_ARGUMENT")
        try:
            port = int(str(raw_port).strip())
        except (TypeError, ValueError):
            raise ModuleApiError(
                "端口必须是 1-65535 的整数", status=400, code="INVALID_ARGUMENT"
            ) from None
        if port < MIN_PORT or port > MAX_PORT:
            raise ModuleApiError(
                "端口必须是 1-65535 的整数", status=400, code="INVALID_ARGUMENT"
            )
        changes[SETTING_PORT] = str(port)

    try:
        for key, value in changes.items():
            ctx.set_setting(key, value)
    except Exception as exc:  # noqa: BLE001
        raise ModuleApiError(
            f"保存模块设置失败：{exc}", status=500, code="SETTINGS_SAVE_FAILED"
        ) from exc

    after = settings_snapshot(ctx)
    notices: list[str] = []
    stopped = False
    if changes:
        info = running_info(ctx, fallback_port=after["port"])
        if info["pid"]:
            if not after["enabled"]:
                stop_gateway(ctx, audit=True, reason="module-disabled")
                stopped = True
                notices.append("网关设置已停用，进程已停止")
            elif after["port"] != before["port"]:
                stop_gateway(ctx, audit=True, reason="port-changed")
                stopped = True
                notices.append(f"端口已改为 {after['port']}，网关已停止，请重新启动")
        if not after["enabled"]:
            notices.append("模块设置中的「启用 RDP 网关」已关闭")
        if after["autoStart"] and not before["autoStart"]:
            notices.append("已开启自动启动：下次宿主启动/模块启用时生效")

    return {
        "settings": after,
        "stopped": stopped,
        "notice": "；".join(notices),
    }


# ---------------------------------------------------------------------------
# 路由注册
# ---------------------------------------------------------------------------
def register(ctx) -> None:
    global _CTX, _ATEXIT_REGISTERED
    _CTX = ctx
    if not _ATEXIT_REGISTERED:
        atexit.register(_atexit_stop)
        _ATEXIT_REGISTERED = True

    @ctx.route("/status", methods=("GET",), permission="rdp:use")
    def module_status():
        return status_payload(ctx)

    @ctx.route("/settings", methods=("GET",), permission="rdp:use")
    def module_get_settings():
        config = settings_snapshot(ctx)
        return {
            "settings": config,
            "defaults": {
                "enabled": True,
                "port": DEFAULT_PORT,
                "autoStart": False,
            },
            "minPort": MIN_PORT,
            "maxPort": MAX_PORT,
            "isAdmin": is_admin(),
        }

    @ctx.route("/settings", methods=("POST",), permission="rdp:use")
    def module_update_settings():
        from flask import request

        return update_settings(ctx, request.get_json(silent=True) or {})

    @ctx.route("/start", methods=("POST",), permission="rdp:use")
    def module_start():
        require_admin()
        return start_gateway(ctx, trigger="api")

    @ctx.route("/stop", methods=("POST",), permission="rdp:use")
    def module_stop():
        require_admin()
        return stop_gateway(ctx, audit=True, reason="api")

    @ctx.route("/diagnose", methods=("GET",), permission="rdp:use")
    def module_diagnose():
        return diagnose_payload(ctx)

    @ctx.route("/logs", methods=("GET",), permission="rdp:use")
    def module_logs():
        from flask import request

        raw = request.args.get("lines") or "200"
        try:
            lines = max(1, min(1000, int(raw)))
        except (TypeError, ValueError):
            raise ModuleApiError("lines 必须是 1-1000 的整数", status=400, code="INVALID_ARGUMENT") from None
        path = log_path(ctx)
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        return {
            "path": str(path),
            "lines": _tail_lines(path, lines),
            "bytes": size,
            "updatedAt": _utc_now_iso(),
        }
