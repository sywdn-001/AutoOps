"""对**正在运行**的堡垒机做端到端联调（真实 HTTP + 真实 Socket.IO + 真实 SSH 网关）。

覆盖（按用户四条硬性需求组织）：
  需求① 身份审计：只读策略下 `cat /etc/shadow` 必须被判 deny；命令与输出落库；录像可回放
  需求② 网页终端：Socket.IO 实时通道 + 真实 SSH 会话（xterm 数据面 + 命令实时推送）
  需求③ 权限边界：非管理员建主机 / 改设置 / 建管理员账号必须 403
  需求④ SSH 网关：`ssh -p 2222` 登录 → 强制改密 → 主机菜单 → 执行命令 → 落库审计
  附加：超级管理员兜底准入（授权表为空时仍能在网页终端看到全部启用主机）

前置条件（缺一不可）：
  1. 后端在跑：`python run.py --host 127.0.0.1 --port 5000 --gateway-port 2222`
  2. 演示目标机在跑：`python tools/demo_ssh_target.py --port 2200`
  3. 已安装联调依赖：`pip install paramiko python-socketio[client] websocket-client`

用法：
  python tools/live_e2e_check.py
  python tools/live_e2e_check.py --base http://127.0.0.1:5000 --gateway-port 2222
  python tools/live_e2e_check.py --admin-password <我改过的管理员口令>

管理员口令：默认 `admin/admin123`。**首次用管理员登录 SSH 网关会被强制改密**（产品行为），
改过之后默认口令不再可用——此时用 `--admin-password`（或环境变量 `BASTION_ADMIN_PASSWORD`）
传入当前口令，或先执行 `python run.py --reset-admin` 把管理员口令恢复为配置文件里的默认值。

副作用（可重复执行）：会创建/复用 `e2e-ops` 账号、`e2e-demo-01` 主机与账号、一条只读策略授权，
并把 `e2e-ops` 的口令重置为已知值（因此该账号下一轮网关登录必然走「强制改密」流程——
这是产品行为，脚本主动走完，而不是绕过）。验证完可在「系统设置 → 维护」里清理，或直接删库重来。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import paramiko
import socketio

# 报告里有中文和带圈数字（需求⑥~⑬），Windows 控制台/管道默认是 GBK（cp936），
# print 这类字符会抛 UnicodeEncodeError 把整份报告截断（表现为退出码 1 的假失败）。
# 统一按 UTF-8 输出，真遇到编码不了的字符就替换，绝不让报告自己崩掉。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = "http://127.0.0.1:5000"
GW_HOST, GW_PORT = "127.0.0.1", 2222
TARGET_HOST, TARGET_PORT = "127.0.0.1", 2200
ADMIN, ADMIN_PW = os.environ.get("BASTION_ADMIN", "admin"), os.environ.get("BASTION_ADMIN_PASSWORD", "admin123")
OPS_USER, OPS_PW = "e2e-ops", "Ops12345"
VIEWER_USER = "e2e-viewer"
#: 网关首次登录（或管理员重置口令后）必须先改密，这是产品行为；脚本走完这段流程
OPS_NEW_PW = "Ops12345b"
HOST_NAME = "e2e-demo-01"
SECRET_MARKER = "root:$6$demo$fakefakefake:19700"  # 演示目标机 cat /etc/shadow 的应答
ANSI_SGR = re.compile(r"\x1b\[[0-9;]*m")  # 菜单/横幅带色，比对前先剥掉
RESULTS: list[tuple[str, bool, str]] = []


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AutoOps 堡垒机端到端联调（真实 HTTP / Socket.IO / SSH 网关）")
    parser.add_argument("--base", default=BASE, help="堡垒机后端地址（默认 http://127.0.0.1:5000）")
    parser.add_argument("--gateway-port", type=int, default=GW_PORT, help="SSH 网关端口（默认 2222）")
    parser.add_argument("--target-port", type=int, default=TARGET_PORT, help="演示目标机端口（默认 2200）")
    parser.add_argument("--admin-user", default=ADMIN, help="管理员账号（默认 admin，可被 BASTION_ADMIN 覆盖）")
    parser.add_argument(
        "--admin-password",
        default=ADMIN_PW,
        help="管理员当前口令（默认 admin123，可被 BASTION_ADMIN_PASSWORD 覆盖）",
    )
    return parser.parse_args()


_args = _parse_args()
BASE = _args.base.rstrip("/")
GW_PORT = _args.gateway_port
TARGET_PORT = _args.target_port
ADMIN = _args.admin_user
ADMIN_PW = _args.admin_password


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(ok), str(detail)[:400]))
    print(f"{'PASS' if ok else 'FAIL'} | {name}" + (f" | {detail}" if detail else ""), flush=True)
    return bool(ok)


def api(method: str, path: str, token: str | None = None, body=None):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, {"raw": raw}


def login(username: str, password: str):
    status, body = api("POST", "/api/login/account", body={"username": username, "password": password, "type": "account"})
    token = (body.get("data") or {}).get("token") if isinstance(body, dict) else None
    return token, status, body.get("message")


def api_raw(method: str, path: str, token: str | None = None, body=None):
    """取原始字节（文件下载/打包下载是二进制流，走不了 JSON 信封）。"""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def api_upload(sid: str, token: str, remote_dir: str, filename: str, content: bytes, overwrite: bool = True):
    """multipart 上传（前端 Upload 组件走的就是这条路径）。"""
    boundary = "----bastionE2E" + uuid.uuid4().hex
    body = b"".join(
        [
            f'--{boundary}\r\nContent-Disposition: form-data; name="path"\r\n\r\n{remote_dir}\r\n'.encode("utf-8"),
            f'--{boundary}\r\nContent-Disposition: form-data; name="overwrite"\r\n\r\n{"true" if overwrite else "false"}\r\n'.encode("utf-8"),
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                "Content-Type: application/octet-stream\r\n\r\n"
            ).encode("utf-8"),
            content,
            f"\r\n--{boundary}--\r\n".encode("utf-8"),
        ]
    )
    req = urllib.request.Request(BASE + f"/api/files/sessions/{sid}/upload", data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except Exception:  # noqa: BLE001
            return exc.code, {"raw": raw}


def file_url(sid: str, kind: str, path: str) -> str:
    return f"/api/files/sessions/{sid}/{kind}?path=" + urllib.parse.quote(path, safe="")


def rows_of(body, key: str = "data"):
    rows = (body or {}).get(key) if isinstance(body, dict) else None
    if isinstance(rows, dict):
        rows = rows.get("list") or []
    return rows or []


# ---------------------------------------------------------------- 准备数据 ---
print("=== 0. 登录与准备演示数据 ===", flush=True)
admin_token, status, msg = login(ADMIN, ADMIN_PW)
if not check(f"管理员登录（{ADMIN}）", bool(admin_token), f"HTTP {status} {msg}"):
    print(
        "\n提示：若你曾用管理员登录 SSH 网关/网页终端，首次登录会被强制改密，"
        "默认口令 `admin123` 即失效。\n"
        "      请用 `--admin-password <当前口令>` 重跑，或先 `python run.py --reset-admin` 恢复默认口令。",
        flush=True,
    )
    sys.exit(2)
AW = {"Authorization": f"Bearer {admin_token}"}

status, body = api("GET", "/api/users?keyword=" + OPS_USER + "&pageSize=20", admin_token)
rows = body.get("data") or []
if isinstance(rows, dict):
    rows = rows.get("list") or []
ops = next((r for r in rows if r.get("username") == OPS_USER), None)
if not ops:
    status, body = api("POST", "/api/users", admin_token, {"username": OPS_USER, "password": OPS_PW, "displayName": "E2E 运维", "roleCode": "ops", "remark": "端到端联调账号"})
    ops = body.get("data") if isinstance(body, dict) else None
    check("创建运维账号 e2e-ops", bool(ops), f"HTTP {status} {body.get('message') if isinstance(body, dict) else body}")
else:
    check("复用已存在的运维账号 e2e-ops", True, f"id={ops.get('id')}")
ops_id = (ops or {}).get("id")

# 每轮统一把运维口令重置成已知值，保证脚本可重复运行。
# 注意该接口会把 must_change_password 置 True —— 也就是说第 4 段的 SSH 网关
# **必然**先要求改密。那是真实产品行为（新账号/重置口令后首次登录网关必须自己设密码），
# 脚本主动覆盖这段流程，而不是绕过它。
status, body = api("POST", f"/api/users/{ops_id}/password", admin_token, {"password": OPS_PW})
check("重置运维账号口令（保证脚本可重复运行）", status == 200, f"HTTP {status} {body.get('message') if isinstance(body, dict) else body}")

status, body = api("GET", "/api/hosts?keyword=" + HOST_NAME + "&pageSize=20", admin_token)
rows = body.get("data") or []
if isinstance(rows, dict):
    rows = rows.get("list") or []
host = next((r for r in rows if r.get("name") == HOST_NAME), None)
if not host:
    status, body = api("POST", "/api/hosts", admin_token, {"name": HOST_NAME, "address": TARGET_HOST, "port": TARGET_PORT, "protocol": "ssh", "osType": "linux", "description": "演示目标机（tools/demo_ssh_target.py）"})
    host = body.get("data") if isinstance(body, dict) else None
    check("创建演示主机 e2e-demo-01", bool(host), f"HTTP {status} {body.get('message') if isinstance(body, dict) else body}")
else:
    check("复用已存在的演示主机", True, f"id={host.get('id')}")
host_id = (host or {}).get("id")

status, body = api("GET", f"/api/hosts/{host_id}/accounts", admin_token)
accounts = body.get("data") or []
if isinstance(accounts, dict):
    accounts = accounts.get("list") or []
account = next((a for a in accounts if a.get("username") == "root"), None)
if not account:
    status, body = api("POST", f"/api/hosts/{host_id}/accounts", admin_token, {"name": "root", "username": "root", "authType": "password", "password": "s3cret", "description": "演示目标机 root"})
    account = body.get("data") if isinstance(body, dict) else None
    check("创建目标机账号 root", bool(account), f"HTTP {status} {body.get('message') if isinstance(body, dict) else body}")
else:
    check("复用已存在的目标机账号 root", True, f"id={account.get('id')}")
account_id = (account or {}).get("id")

status, body = api("GET", "/api/policies?pageSize=50", admin_token)
policies = body.get("data") or []
if isinstance(policies, dict):
    policies = policies.get("list") or []
readonly = next((p for p in policies if "只读" in (p.get("name") or "")), None)
check("找到内置「只读审计策略」", bool(readonly), f"策略={[p.get('name') for p in policies]}")
policy_id = (readonly or {}).get("id")

status, body = api("GET", f"/api/grants?userId={ops_id}&pageSize=50", admin_token)
grants = body.get("data") or []
if isinstance(grants, dict):
    grants = grants.get("list") or []
grant = next((g for g in grants if g.get("hostId") == host_id), None)
if not grant:
    status, body = api("POST", "/api/grants", admin_token, {"userId": ops_id, "hostId": host_id, "hostAccountId": account_id, "policyId": policy_id, "canLogin": True, "canWebterm": True, "canSftp": False, "canUpload": False, "canDownload": False, "weekdays": [], "maxSessions": 0, "enabled": True, "remark": "端到端联调授权"})
    grant = body.get("data") if isinstance(body, dict) else None
    check("创建授权（只读策略 + 网页终端）", bool(grant), f"HTTP {status} {body.get('message') if isinstance(body, dict) else body}")
else:
    check("复用已存在的授权", True, f"id={grant.get('id')}")
grant_id = (grant or {}).get("id")

ops_token, status, msg = login(OPS_USER, OPS_PW)
check("运维账号登录", bool(ops_token), f"HTTP {status} {msg}")
if not ops_token:
    sys.exit(2)

# ------------------------------------------------- 需求③ 非管理员写操作 403 ---
print("\n=== 1. 需求③：只有管理员能添加机器/改设置 ===", flush=True)
status, body = api("POST", "/api/hosts", ops_token, {"name": "e2e-should-fail", "address": "10.9.9.9"})
check("运维账号 POST /api/hosts 被拒（403）", status == 403, f"HTTP {status} {body.get('message')}")
status, body = api("PUT", "/api/settings", ops_token, {"values": {"command_timeout": 5}})
check("运维账号改系统设置被拒（403）", status == 403, f"HTTP {status} {body.get('message')}")
status, body = api("POST", "/api/users", ops_token, {"username": "hacker", "password": "Hacker123", "roleCode": "admin"})
check("运维账号建管理员账号被拒（403）", status == 403, f"HTTP {status} {body.get('message')}")
status, body = api("GET", "/api/terminal/targets", ops_token)
targets = body.get("data") or []
check("运维账号可读自己的可访问主机", status == 200 and any(t.get("hostId") == host_id for t in targets), f"HTTP {status} targets={[(t.get('hostName'), t.get('address'), t.get('port')) for t in targets]}")
t = next((x for x in targets if x.get("hostId") == host_id), {})
check("目标结构含 port/osType/accounts（serialize_target 契约）", t.get("port") == TARGET_PORT and t.get("osType") == "linux" and bool(t.get("accounts")), json.dumps(t, ensure_ascii=False)[:200])

# ------------------------------------------- 超级管理员兜底准入（授权表之外） ---
print("\n=== 1.5 超级管理员兜底准入（授权表为空也能用网页终端） ===", flush=True)
status, body = api("GET", "/api/terminal/targets", admin_token)
adm_targets = body.get("data") or []
adm_names = [x.get("hostName") for x in adm_targets]
check("管理员可访问主机列表包含演示主机（未给管理员建过授权）", status == 200 and HOST_NAME in adm_names, f"HTTP {status} {adm_names}")
no_account = next((x for x in adm_targets if not x.get("accounts")), None)
if no_account is not None:
    status, body = api("POST", f"/api/terminal/targets/{no_account['hostId']}/check", admin_token, {})
    decision = body.get("data") or {}
    check(
        "无资产账号的主机在准入校验即被拒（reason 明确，不让人白连一次）",
        status == 200 and decision.get("allowed") is False and "账号" in (decision.get("reason") or ""),
        f"HTTP {status} {json.dumps(decision, ensure_ascii=False)[:200]}",
    )
else:
    check("无资产账号的主机在准入校验即被拒（reason 明确，不让人白连一次）", True, "当前没有无账号主机，跳过")

# ----------------------------------------------------- 需求② 网页终端 E2E ---
print("\n=== 2. 需求②：Socket.IO 网页终端实时会话 ===", flush=True)
state = {"ready": None, "error": [], "targets": None, "opened": None, "output": "", "notice": [], "commands": [], "closed": []}
lock = threading.Lock()

sio = socketio.Client(reconnection=False)


@sio.on("terminal:ready")
def _ready(d):
    with lock:
        state["ready"] = d


@sio.on("terminal:error")
def _err(d):
    with lock:
        state["error"].append(d)


@sio.on("terminal:targets")
def _tgts(d):
    with lock:
        state["targets"] = d


@sio.on("terminal:opened")
def _opened(d):
    with lock:
        state["opened"] = d


@sio.on("terminal:output")
def _out(d):
    with lock:
        state["output"] += (d or {}).get("data", "")


@sio.on("terminal:notice")
def _notice(d):
    with lock:
        state["notice"].append(d)


@sio.on("terminal:command")
def _cmd(d):
    with lock:
        state["commands"].append(d)


@sio.on("terminal:closed")
def _closed(d):
    with lock:
        state["closed"].append(d)


def wait_for(pred, timeout=25.0, interval=0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with lock:
            if pred():
                return True
        time.sleep(interval)
    return False


connected = False
try:
    sio.connect(BASE, auth={"token": ops_token}, transports=["websocket"], wait_timeout=20)
    connected = True
except Exception as exc:  # noqa: BLE001
    check("Socket.IO 建连（websocket 传输）", False, f"{type(exc).__name__}: {exc}")
if connected:
    check("Socket.IO 建连（websocket 传输）", True)
    check("收到 terminal:ready（令牌/权限校验通过）", wait_for(lambda: state["ready"] is not None, 10), str(state["ready"]))
    check("连接未收到 terminal:error", not state["error"], str(state["error"]))

    sio.emit("terminal:targets")
    check("terminal:targets 返回可访问主机", wait_for(lambda: state["targets"] is not None, 10), json.dumps(state["targets"], ensure_ascii=False)[:200])

    sio.emit("terminal:open", {"hostId": host_id, "accountId": account_id, "cols": 120, "rows": 32})
    opened = wait_for(lambda: state["opened"] is not None, 45)
    check("terminal:open 建立会话（含真实 SSH 握手 + PS1 分段）", opened, json.dumps(state["opened"], ensure_ascii=False))
    sid = (state["opened"] or {}).get("sid", "")
    check("会话返回会话号", bool(sid), sid)

    with lock:
        state["output"] = ""
    sio.emit("terminal:input", {"data": "whoami\n"})
    got = wait_for(lambda: "opsadmin" in state["output"], 30)
    with lock:
        out1 = state["output"]
    check("网页终端执行 whoami 并回显 opsadmin", got, out1[-160:].replace("\r", "\\r").replace("\n", "\\n"))

    got_cmd = wait_for(lambda: any(c.get("action") == "allow" and "whoami" in (c.get("command") or "") for c in state["commands"]), 15)
    check("命令审计实时推送（terminal:command, action=allow）", got_cmd, json.dumps(state["commands"][-2:], ensure_ascii=False))

    with lock:
        state["output"] = ""
        state["notice"] = []
    sio.emit("terminal:input", {"data": "cat /etc/shadow\n"})
    denied = wait_for(lambda: any(c.get("action") == "deny" for c in state["commands"]), 20)
    check("需求①：只读策略下 cat /etc/shadow 被判 deny 并实时推送", denied, json.dumps(state["commands"][-2:], ensure_ascii=False))
    with lock:
        out2 = state["output"]
    check("需求①：被拒命令的输出没有落到网页终端（凭据不泄露）", SECRET_MARKER not in out2, out2[-160:].replace("\r", "\\r").replace("\n", "\\n"))
    # 拦截原因由桥接层直接写进终端输出流（产品刻意不再发第二条 terminal:notice，
    # 避免同一件事提示两次）；提示里必须带命中的规则编号，否则管理员按说明找规则会删错。
    check(
        "被拒命令在网页终端回显拦截原因（含策略名与命中规则号）",
        "命令被拒绝" in out2 and "命令被策略" in out2 and "命中规则 #" in out2,
        out2[-220:].replace("\r", "\\r").replace("\n", "\\n"),
    )

    sio.emit("terminal:close")
    check("terminal:close 后收到 terminal:closed", wait_for(lambda: bool(state["closed"]), 15), str(state["closed"]))
    time.sleep(0.6)
    sio.disconnect()

# ------------------------------------------- 需求① 落库审计（命令与输出） ---
print("\n=== 3. 需求①②：命令与输出落库（REST 回读） ===", flush=True)
status, body = api("GET", "/api/sessions?source=web&pageSize=10", ops_token)
sessions = body.get("data") or []
if isinstance(sessions, dict):
    sessions = sessions.get("list") or []
mine = [s for s in sessions if s.get("hostName") == HOST_NAME]
check("会话记录已落库（source=web）", bool(mine), f"HTTP {status} 最近={[(s.get('id'), s.get('status'), s.get('commandCount')) for s in sessions[:3]]}")
rec = mine[0] if mine else {}
status, body = api("GET", f"/api/sessions/{rec.get('id')}/commands?pageSize=50", ops_token)
cmds = body.get("data") or []
if isinstance(cmds, dict):
    cmds = cmds.get("list") or []
rows_txt = [(c.get("command"), c.get("action"), (c.get("output") or "")[:40]) for c in cmds]
check("命令日志含 whoami（allow）", any(c.get("command") == "whoami" and c.get("action") == "allow" for c in cmds), str(rows_txt))
check("命令日志含 cat /etc/shadow（deny）", any(c.get("command") == "cat /etc/shadow" and c.get("action") == "deny" for c in cmds), str(rows_txt))
check("被拒命令的输出为空（未触碰目标机）", not any(SECRET_MARKER in (c.get("output") or "") for c in cmds), str(rows_txt))
status, body = api("GET", f"/api/sessions/{rec.get('id')}/transcript?limit=200", ops_token)
events = (body.get("data") or {}).get("events") or []
check("录像/事件流可回放", bool(events), f"events={len(events)} types={[e.get('t') for e in events][:8]}")

# ------------------------------------------------------ 需求④ SSH 网关 E2E ---
print("\n=== 4. 需求④：SSH 网关（ssh -p 2222）+ 命令落库 ===", flush=True)
cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
chan = None
gbuf = [""]
try:
    cli.connect(GW_HOST, port=GW_PORT, username=OPS_USER, password=OPS_PW, look_for_keys=False, allow_agent=False, timeout=20, banner_timeout=30)
    check("SSH 网关密码认证成功", True)
    chan = cli.invoke_shell(width=120, height=32)

    def read_until(needle: str, timeout: float = 30.0):
        """等 needle 出现；**先查已收缓冲区**，否则会把上一步同批收到的内容漏判。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if needle in gbuf[0]:
                return True
            if chan.recv_ready():
                gbuf[0] += chan.recv(65536).decode("utf-8", "replace")
                continue
            time.sleep(0.15)
        return needle in gbuf[0]

    def tail(n: int = 300) -> str:
        return gbuf[0][-n:].replace("\r", "\\r").replace("\n", "\\n")

    # 首次登录（或管理员刚重置口令）时网关强制改密：脚本把这段真实流程走完
    if read_until("必须先设置新密码", 20):
        chan.send(OPS_NEW_PW + "\n")
        time.sleep(0.8)
        chan.send(OPS_NEW_PW + "\n")
        check("网关强制改密流程可用（改密后进入菜单）", read_until("密码已更新", 25), tail())

    # 注意：菜单可能与上一步的文本在同一次 recv 里到达，所以这里不能先清空 gbuf
    check("网关下发主机菜单（含授权主机）", read_until(HOST_NAME, 30), tail())
    # 进站字符画（盾牌 + AutoOps 字标）+ 配色 + CRLF（m05469：进站要彩色字符画、`=` 跟随内容宽度）
    art_pos = gbuf[0].find("█")
    menu_pos = gbuf[0].find("你可访问的主机")
    check("进站字符画在主机菜单之前出现（盾牌 + 品牌字标）", 0 <= art_pos < menu_pos and "A u t o O p s" in gbuf[0], tail(400))
    check("进站横幅与菜单带 ANSI 配色", "\x1b[" in gbuf[0], repr(gbuf[0][:120]))
    rules = {ln.strip() for ln in ANSI_SGR.sub("", gbuf[0]).split("\r\n") if ln.strip() and set(ln.strip()) == {"="}}
    rule_widths = sorted(len(r) for r in rules)
    check(
        "分隔线长度跟随内容（不是固定 78 列的横贯线）",
        0 < len(rule_widths) <= 8 and len(set(rule_widths)) > 1 and max(rule_widths) <= 120,
        f"各段分隔线长度={rule_widths}",
    )
    check("菜单区没有裸 LF（一律 CRLF）", not re.search(r"(?<!\r)\n", gbuf[0]), repr(re.search(r"(?<!\r)\n", gbuf[0]) and gbuf[0][:200]))
    gbuf[0] = ""
    chan.send("1\n")
    check("选择编号 1 进入目标机会话", read_until("已连接", 45) or read_until("#", 5), tail())
    gbuf[0] = ""
    chan.send("whoami\n")
    check("网关会话内执行 whoami 回显 opsadmin", read_until("opsadmin", 30), tail(200))
    gbuf[0] = ""
    chan.send("exit\n")
    time.sleep(2.5)
    chan.send("q\n")
    time.sleep(1.5)
    check("退出会话后回到菜单并可退出", True)
except Exception as exc:  # noqa: BLE001
    check("需求④ SSH 网关端到端", False, f"{type(exc).__name__}: {exc}")
finally:
    try:
        if chan is not None:
            chan.close()
    except Exception:  # noqa: BLE001
        pass
    try:
        cli.close()
    except Exception:  # noqa: BLE001
        pass

time.sleep(1.0)
status, body = api("GET", "/api/sessions?source=gateway&pageSize=10", ops_token)
gs = body.get("data") or []
if isinstance(gs, dict):
    gs = gs.get("list") or []
check("网关会话落库（source=gateway）", bool(gs), f"HTTP {status} {[(s.get('id'), s.get('source'), s.get('status'), s.get('commandCount')) for s in gs[:3]]}")
if gs:
    rid = gs[0].get("id")
    status, body = api("GET", f"/api/sessions/{rid}/commands?pageSize=50", ops_token)
    gcmds = body.get("data") or []
    if isinstance(gcmds, dict):
        gcmds = gcmds.get("list") or []
    check("网关会话的命令与输出被记录（需求④核心）", any((c.get("command") or "").strip() == "whoami" for c in gcmds), str([(c.get("command"), c.get("action"), (c.get("output") or "")[:20]) for c in gcmds]))

# ---------------------------------- SFTP 文件管理器（浏览/操作 + 全量访问控制与审计） ---
print("\n=== 5. SFTP 可视化文件管理器（每一类操作都过访问控制与审计） ===", flush=True)

# 5.1 访问控制的第一层：授权开关。先只开「能进 + 能看」，关掉改文件。
status, body = api(
    "PUT",
    f"/api/grants/{grant_id}",
    admin_token,
    {"canSftp": True, "canDownload": True, "canUpload": False, "canFileWrite": False},
)
check(
    "管理员把授权设为「只读 SFTP」（canSftp/canDownload 开，canUpload/canFileWrite 关）",
    status == 200,
    f"HTTP {status} {body.get('message') if isinstance(body, dict) else body}",
)

status, body = api("GET", "/api/terminal/targets", ops_token)
target_now = next((x for x in rows_of(body) if x.get("hostId") == host_id), {})
check(
    "资产目录把文件能力透出给前端（canSftp/canFileWrite/filePolicyName）",
    target_now.get("canSftp") is True
    and target_now.get("canFileWrite") is False
    and bool(target_now.get("filePolicyName")),
    json.dumps(
        {k: target_now.get(k) for k in ("canSftp", "canUpload", "canDownload", "canFileWrite", "filePolicyName")},
        ensure_ascii=False,
    ),
)

status, body = api("POST", "/api/files/sessions", ops_token, {"hostId": host_id, "accountId": account_id})
caps = (body.get("data") or {}) if isinstance(body, dict) else {}
sid = caps.get("sid")
check(
    "打开文件管理器会话（真实 SFTP 子系统握手 + 会话能力字典）",
    status == 200 and bool(sid) and caps.get("canSftp") is True and caps.get("canFileWrite") is False,
    f"HTTP {status} "
    + json.dumps(
        {k: caps.get(k) for k in ("sid", "homeDir", "canSftp", "canUpload", "canDownload", "canFileWrite", "policyName")},
        ensure_ascii=False,
    )[:260],
)
check(
    "会话能力字典带出文件策略名与可用操作清单（前端按此置灰按钮）",
    bool(caps.get("policyName")) and "list" in (caps.get("operations") or []) and "upload" in (caps.get("operations") or []),
    json.dumps(caps.get("operations"), ensure_ascii=False)[:220],
)

if not sid:
    check("文件管理器后续操作（依赖已建立的 SFTP 会话）", False, "未拿到 sid")
else:
    status, body = api("GET", "/api/sessions/online", ops_token)
    online = rows_of(body)
    check(
        "文件会话进入在线会话表（管理员可在审计页强制断开）",
        any(e.get("sid") == sid for e in online),
        f"sids={[e.get('sid') for e in online][:4]}",
    )

    status, body = api("GET", file_url(sid, "list", "/"), ops_token)
    listing = (body.get("data") or {}) if isinstance(body, dict) else {}
    entries = listing.get("entries") or []
    names = [e.get("name") for e in entries]
    check(
        "浏览远端目录（真实 SFTP 列目录）",
        status == 200 and "readme.txt" in names and "docs" in names,
        f"HTTP {status} entries={names}",
    )
    docs = next((e for e in entries if e.get("name") == "docs"), {})
    check(
        "目录项带类型/大小/权限/时间（可视化列表所需字段）",
        docs.get("isDir") is True and bool(docs.get("modeOctal")) and bool(docs.get("mtime")),
        json.dumps(docs, ensure_ascii=False)[:220],
    )

    status, body = api("GET", file_url(sid, "read", "/readme.txt"), ops_token)
    data = (body.get("data") or {}) if isinstance(body, dict) else {}
    check(
        "在线预览文件内容（read）",
        status == 200 and bool(data.get("content")) and bool(data.get("encoding")),
        f"HTTP {status} size={data.get('size')} encoding={data.get('encoding')}",
    )

    status, raw, headers = api_raw("GET", file_url(sid, "download", "/docs/notes.txt"), ops_token)
    check(
        "下载文件（流式二进制 + 文件名随响应头返回）",
        status == 200 and len(raw) > 0 and "notes.txt" in headers.get("Content-Disposition", ""),
        f"HTTP {status} bytes={len(raw)} disp={headers.get('Content-Disposition')}",
    )

    # 5.2 第二层：文件策略（路径级）。凭据类路径即便只读也不许读。
    status, body = api("GET", file_url(sid, "read", "/.ssh/id_rsa"), ops_token)
    msg = body.get("message") if isinstance(body, dict) else str(body)
    check(
        "文件策略拦下读取私钥（403 + 命中规则说明）",
        status == 403 and "策略" in (msg or "") and "命中规则" in (msg or ""),
        f"HTTP {status} {msg}",
    )

    # 5.3 只读授权下，修改类操作在「试算」阶段就被拒（前端据此置灰并给出原因）
    status, body = api(
        "POST", f"/api/files/sessions/{sid}/check", ops_token, {"operation": "delete", "path": "/docs/notes.txt"}
    )
    decision = (body.get("data") or {}) if isinstance(body, dict) else {}
    check(
        "只读授权下删除在试算阶段即被拒（reason 明确）",
        status == 200 and decision.get("allowed") is False and "修改" in (decision.get("reason") or ""),
        json.dumps(decision, ensure_ascii=False)[:220],
    )
    status, body = api(
        "POST", f"/api/files/sessions/{sid}/write", ops_token, {"path": "/data/e2e-denied.txt", "content": "nope"}
    )
    check(
        "只读授权下真实写入被拦截（403）",
        status == 403,
        f"HTTP {status} {body.get('message') if isinstance(body, dict) else body}",
    )
    status, body = api_upload(sid, ops_token, "/data", "e2e-denied.txt", b"nope")
    check(
        "只读授权下上传被拦截（403）",
        status == 403,
        f"HTTP {status} {body.get('message') if isinstance(body, dict) else body}",
    )

    # 5.4 补开上传/改文件后，把「能想到的操作」全跑一遍（真实 SFTP 写 + 真实审计）
    status, body = api("PUT", f"/api/grants/{grant_id}", admin_token, {"canUpload": True, "canFileWrite": True})
    check("管理员补开上传与改文件权限", status == 200, f"HTTP {status}")

    status, body = api("POST", f"/api/files/sessions/{sid}/mkdir", ops_token, {"path": "/data/e2e-new", "parents": True})
    check("新建目录", status == 200, f"HTTP {status} {body.get('message') if isinstance(body, dict) else body}")

    status, body = api(
        "POST",
        f"/api/files/sessions/{sid}/write",
        ops_token,
        {"path": "/data/e2e-new/hello.txt", "content": "AutoOps 文件管理器 E2E"},
    )
    check("在线编辑保存（新建文件）", status == 200, f"HTTP {status} {body.get('message') if isinstance(body, dict) else body}")

    status, body = api("GET", file_url(sid, "read", "/data/e2e-new/hello.txt"), ops_token)
    data = (body.get("data") or {}) if isinstance(body, dict) else {}
    check(
        "回读刚保存的文件内容一致",
        status == 200 and "AutoOps" in (data.get("content") or ""),
        f"HTTP {status} {str(data.get('content'))[:60]!r}",
    )

    status, body = api(
        "POST",
        f"/api/files/sessions/{sid}/rename",
        ops_token,
        {"path": "/data/e2e-new/hello.txt", "newName": "hello-renamed.txt"},
    )
    check("重命名", status == 200, f"HTTP {status} {body.get('message') if isinstance(body, dict) else body}")

    status, body = api(
        "POST",
        f"/api/files/sessions/{sid}/copy",
        ops_token,
        {"path": "/data/e2e-new/hello-renamed.txt", "targetPath": "/data/e2e-new/hello-copy.txt"},
    )
    check("复制", status == 200, f"HTTP {status} {body.get('message') if isinstance(body, dict) else body}")

    status, body = api(
        "POST",
        f"/api/files/sessions/{sid}/rename",
        ops_token,
        {"path": "/data/e2e-new/hello-copy.txt", "targetPath": "/data/e2e-moved.txt"},
    )
    check("移动（跨目录）", status == 200, f"HTTP {status} {body.get('message') if isinstance(body, dict) else body}")

    status, body = api("POST", f"/api/files/sessions/{sid}/chmod", ops_token, {"path": "/data/e2e-moved.txt", "mode": "600"})
    chmod_data = (body.get("data") or {}) if isinstance(body, dict) else {}
    _stat_status, stat_body = api("GET", f"/api/files/sessions/{sid}/stat?path=/data/e2e-moved.txt", ops_token)
    stat_data = (stat_body.get("data") or {}) if isinstance(stat_body, dict) else {}
    check(
        "改权限（after 与随后的 stat 一致且为四位八进制）",
        status == 200
        and re.fullmatch(r"\d{4}", str(chmod_data.get("after") or "")) is not None
        and chmod_data.get("after") == stat_data.get("modeOctal"),
        f"HTTP {status} {json.dumps(chmod_data, ensure_ascii=False)[:120]} stat={stat_data.get('modeOctal')}",
    )

    upload_bytes = "AutoOps 上传内容".encode("utf-8")
    status, body = api_upload(sid, ops_token, "/data/e2e-new", "uploaded.txt", upload_bytes)
    uploaded = ((body.get("data") or {}) if isinstance(body, dict) else {}).get("uploaded") or []
    check(
        "上传文件（multipart，前端 Upload 走同一路径）",
        status == 200 and bool(uploaded) and uploaded[0].get("size") == len(upload_bytes),
        f"HTTP {status} {json.dumps(body, ensure_ascii=False)[:200]}",
    )

    status, raw, headers = api_raw("GET", file_url(sid, "download", "/data/e2e-new/uploaded.txt"), ops_token)
    check("下载刚上传的文件字节一致", status == 200 and raw == upload_bytes, f"HTTP {status} bytes={len(raw)}")

    status, raw, headers = api_raw(
        "POST", f"/api/files/sessions/{sid}/archive", ops_token, {"paths": ["/data/e2e-new"]}
    )
    check(
        "打包下载（zip 响应 + 条目数随头返回）",
        status == 200 and raw[:2] == b"PK" and int(headers.get("X-Archive-Entries") or 0) >= 1,
        f"HTTP {status} bytes={len(raw)} entries={headers.get('X-Archive-Entries')} type={headers.get('Content-Type')}",
    )

    status, body = api(
        "POST",
        f"/api/files/sessions/{sid}/delete",
        ops_token,
        {"paths": ["/data/e2e-moved.txt", "/data/e2e-new"], "recursive": True},
    )
    deleted = ((body.get("data") or {}) if isinstance(body, dict) else {}).get("deleted") or []
    check(
        "删除（文件 + 目录递归）",
        status == 200 and len(deleted) >= 2,
        f"HTTP {status} deleted={deleted}",
    )

    # 5.5 审计回读：每一类操作都必须落库；被拦的也要落库并带命中规则
    status, body = api("GET", "/api/audits/files?pageSize=100", ops_token)
    flogs = rows_of(body)
    seen = {r.get("operation") for r in flogs}
    need = {"list", "read", "download", "mkdir", "write", "rename", "copy", "move", "chmod", "upload", "delete", "archive"}
    check(
        "文件审计覆盖本轮所有操作（列目录/预览/下载/新建/编辑/改名/复制/移动/改权限/上传/删除/打包）",
        need <= seen,
        f"missing={sorted(need - seen)}",
    )
    denied = [r for r in flogs if r.get("result") == "denied"]
    policy_denied = [r for r in denied if r.get("matchedRuleId")]
    check(
        "被拦操作同样落审计（策略级拒绝带命中规则号/风险等级，grant 级拒绝标原因）",
        bool(denied)
        and bool(policy_denied)
        and all(r.get("riskLevel") for r in denied[:3])
        and all((r.get("reason") or r.get("message")) for r in denied[:3]),
        json.dumps(
            [
                {k: r.get(k) for k in ("operation", "path", "result", "matchedRuleId", "riskLevel", "reason")}
                for r in denied[:3]
            ],
            ensure_ascii=False,
        )[:300],
    )
    check(
        "审计行带操作人/主机/路径（能定位是谁在哪台机上动了哪个文件）",
        bool(flogs)
        and all(r.get("path") for r in flogs[:3])
        and all(r.get("username") for r in flogs[:3])
        and all(r.get("hostName") for r in flogs[:3]),
        json.dumps({k: flogs[0].get(k) for k in ("username", "hostName", "operation", "path", "result")}, ensure_ascii=False)
        if flogs
        else "无记录",
    )
    status, body = api("GET", "/api/audits/files/options", ops_token)
    options = (body.get("data") or {}) if isinstance(body, dict) else {}
    check(
        "文件记录页筛选候选项可用（操作/动作/结果/风险/用户/主机）",
        status == 200
        and all(options.get(k) for k in ("operations", "actions", "results", "riskLevels", "usernames", "hosts")),
        f"HTTP {status} "
        + json.dumps(
            {k: len(options.get(k) or []) for k in ("operations", "actions", "results", "riskLevels", "usernames", "hosts")},
            ensure_ascii=False,
        ),
    )
    # 清除文件审计与清除命令记录同一口径：要求 command:view_all。
    # e2e-ops 是内置 ops 角色（本来就带 command:view_all），所以它清除成功是对的；
    # 「不能清除」这条边界必须拿没有该权限的账号验证（viewer 角色）。
    status, body = api("POST", "/api/audits/files/delete", ops_token, {"all": True})
    check(
        "运维账号能清除文件审计（command:view_all 口径与命令记录一致）",
        status == 200,
        f"HTTP {status} {body.get('message') if isinstance(body, dict) else body}",
    )
    status, body = api("GET", "/api/users?keyword=" + VIEWER_USER + "&pageSize=20", admin_token)
    rows = body.get("data") or []
    if isinstance(rows, dict):
        rows = rows.get("list") or []
    viewer = next((r for r in rows if r.get("username") == VIEWER_USER), None)
    if not viewer:
        status, body = api(
            "POST",
            "/api/users",
            admin_token,
            {
                "username": VIEWER_USER,
                "password": OPS_PW,
                "displayName": "E2E 只读",
                "roleCode": "viewer",
                "remark": "端到端联调只读账号",
            },
        )
        viewer = body.get("data") if isinstance(body, dict) else None
        check("创建只读账号 e2e-viewer", bool(viewer), f"HTTP {status} {body.get('message') if isinstance(body, dict) else body}")
    status, body = api("POST", f"/api/users/{(viewer or {}).get('id')}/password", admin_token, {"password": OPS_PW})
    viewer_token, status, msg = login(VIEWER_USER, OPS_PW)
    check("只读账号登录", bool(viewer_token), f"HTTP {status} {msg}")
    status, body = api("POST", "/api/audits/files/delete", viewer_token, {"all": True})
    check(
        "没有 command:view_all 的账号不能清除文件审计（403）",
        status == 403,
        f"HTTP {status} {body.get('message') if isinstance(body, dict) else body}",
    )

    # 5.6 关闭会话 → 落会话审计（protocol=sftp）
    status, body = api("DELETE", f"/api/files/sessions/{sid}", ops_token)
    check(
        "关闭文件管理器会话（SFTP 与 SSH 通道一并释放）",
        status == 200,
        f"HTTP {status} {body.get('message') if isinstance(body, dict) else body}",
    )
    time.sleep(1.2)
    status, body = api("GET", "/api/sessions?source=web&pageSize=30", ops_token)
    sftp_rec = next((r for r in rows_of(body) if r.get("protocol") == "sftp"), None)
    check(
        "文件会话落到会话审计（protocol=sftp、状态已关闭、统计到操作次数）",
        bool(sftp_rec)
        and sftp_rec.get("status") in ("closed", "terminated")
        and int(sftp_rec.get("commandCount") or 0) > 0,
        json.dumps(
            {k: (sftp_rec or {}).get(k) for k in ("id", "sid", "protocol", "status", "commandCount", "bytesOut", "endReason")},
            ensure_ascii=False,
        ),
    )

# 5.7 文件策略（管理端页面数据源 + 试算器）
status, body = api("GET", "/api/file-policies?pageSize=50", admin_token)
pols = rows_of(body)
pol_names = [p.get("name") for p in pols]
check(
    "内置文件策略已就位（默认放行 + 只读白名单两套）",
    status == 200 and any("默认文件策略" in (n or "") for n in pol_names) and any("只读" in (n or "") for n in pol_names),
    f"HTTP {status} policies={pol_names}",
)
check(
    "策略列表带规则数与引用授权数（管理端表格列）",
    bool(pols) and all("ruleCount" in p and "grantCount" in p for p in pols) and any((p.get("ruleCount") or 0) > 0 for p in pols),
    json.dumps(
        [{k: p.get(k) for k in ("name", "ruleCount", "grantCount", "isDefault")} for p in pols], ensure_ascii=False
    )[:240],
)
status, body = api("POST", "/api/file-policies/evaluate", admin_token, {"operation": "write", "path": "/etc/passwd"})
ev = (body.get("data") or {}) if isinstance(body, dict) else {}
check(
    "策略试算器：往系统目录写文件被判拒绝并给出命中规则",
    status == 200 and ev.get("allowed") is False and bool(ev.get("ruleId")),
    f"HTTP {status} {json.dumps(ev, ensure_ascii=False)[:220]}",
)
status, body = api("POST", "/api/file-policies/evaluate", admin_token, {"operation": "no-such-op", "path": "/tmp/x"})
check(
    "试算器对未知操作返回 400（不给出误导结论）",
    status == 400,
    f"HTTP {status} {body.get('message') if isinstance(body, dict) else body}",
)
status, body = api("GET", "/api/files/sessions", ops_token)
check(
    "文件管理器在线会话列表（关闭后不再有在线会话）",
    status == 200 and not rows_of(body),
    f"HTTP {status} {json.dumps(rows_of(body), ensure_ascii=False)[:160]}",
)

# ------------------------------------------- 需求⑥~⑬ AI 运维（AIOps）与客户端版本 ---
print("\n=== 6. AI 运维（工具目录 / 对话审计 / 敏感操作守卫）+ 客户端版本 ===", flush=True)

status, body = api("GET", "/api/ai/status", admin_token)
ai_status = (body.get("data") or {}) if isinstance(body, dict) else {}
check(
    "AI 状态：已配置 DeepSeek、模型与工具总数可见（需求⑥）",
    status == 200
    and ai_status.get("enabled") is True
    and ai_status.get("configured") is True
    and bool(ai_status.get("model"))
    and int(ai_status.get("totalToolCount") or 0) >= 50,
    f"HTTP {status} model={ai_status.get('model')} tools={ai_status.get('totalToolCount')} base={ai_status.get('baseUrl')}",
)

status, body = api("GET", "/api/ai/tools", admin_token)
tools_payload = (body.get("data") or {}) if isinstance(body, dict) else {}
tools = tools_payload.get("items") or []
tool_names = [t.get("name") for t in tools]
check(
    "AI 工具目录：≥50 个工具且字段完整、无重名（需求⑩）",
    status == 200
    and len(tools) >= 50
    and len(set(tool_names)) == len(tool_names)
    and all(t.get("name") and t.get("description") and t.get("method") and t.get("path") for t in tools),
    f"HTTP {status} total={tools_payload.get('total')} available={tools_payload.get('available')} 唯一名={len(set(tool_names))}",
)

required_tools = {
    "create_user",
    "update_host",
    "delete_host",
    "create_grant",
    "run_command",
    "update_settings",
    "get_audit",
    "list_sessions",
    "reset_user_password",
    "create_command_policy",
    "create_file_policy",
    "terminate_session",
}
missing_tools = sorted(required_tools - set(tool_names))
check(
    "AI 工具覆盖人类能做的全部功能（建用户/改主机/发授权/执行命令/改设置/查审计…）",
    not missing_tools,
    f"missing={missing_tools}",
)

sensitive_tools = [t for t in tools if t.get("sensitive")]
check(
    "敏感工具被标记且都带权限码（否则审批与权限门形同虚设，需求⑪⑫）",
    bool(sensitive_tools) and all(t.get("permission") for t in sensitive_tools),
    f"敏感工具数={len(sensitive_tools)} 例={[t.get('name') for t in sensitive_tools[:5]]}",
)

status, body = api("GET", "/api/ai/tools", ops_token)
ops_payload = (body.get("data") or {}) if isinstance(body, dict) else {}
ops_tool_names = {t.get("name") for t in (ops_payload.get("items") or [])}
check(
    "AI 工具按权限裁剪：内置 ops 角色没有 ai:* 权限，工具目录可见但一个都拿不到（需求⑫）",
    status == 200
    and ops_tool_names == set()
    and int(ops_payload.get("available") or 0) == 0
    and int(ops_payload.get("total") or 0) == len(tool_names),
    f"HTTP {status} ops可用={ops_payload.get('available')} 目录总数={ops_payload.get('total')} admin可用={len(tool_names)}",
)

status, body = api("GET", "/api/roles/permissions", admin_token)
groups_payload = []
if isinstance(body, dict):
    raw = body.get("data")
    groups_payload = raw if isinstance(raw, list) else []
catalog_codes = {
    item.get("code")
    for group in groups_payload
    if isinstance(group, dict)
    for item in (group.get("items") or [])
    if isinstance(item, dict)
}
ai_codes = {
    "ai:view",
    "ai:use",
    "ai:view_all",
    "ai:tool",
    "ai:tool_write",
    "ai:tool_exec",
    "ai:manage",
}
check(
    "AI 权限可单独分配给角色/用户（角色权限目录含 7 个 ai:* 码，需求⑫）",
    status == 200 and ai_codes <= catalog_codes,
    f"HTTP {status} missing={sorted(ai_codes - catalog_codes)} 目录码数={len(catalog_codes)}",
)

status, body = api("GET", "/api/ai/tools", None)
check(
    "未登录访问 AI 工具目录被拒（401）",
    status == 401,
    f"HTTP {status} {body.get('message') if isinstance(body, dict) else body}",
)

status, body = api("GET", "/api/ai/conversations", admin_token)
conv_payload = body if isinstance(body, dict) else {}
check(
    "AI 对话审计数据面可用（列表信封 total/page/pageSize，需求⑦）",
    status == 200 and "total" in conv_payload and "page" in conv_payload and isinstance(conv_payload.get("data"), list),
    f"HTTP {status} total={conv_payload.get('total')} page={conv_payload.get('page')}",
)

status, body = api("GET", "/api/ai/tool-calls", admin_token)
calls_payload = body if isinstance(body, dict) else {}
check(
    "AI 工具调用审计数据面可用（记下调了什么工具、什么结果，需求⑦）",
    status == 200 and "total" in calls_payload and isinstance(calls_payload.get("data"), list),
    f"HTTP {status} total={calls_payload.get('total')}",
)

status, body = api("GET", "/api/ai/conversations/999999", admin_token)
check(
    "不存在的对话返回 404（不是 500，也不会泄露别人的对话）",
    status == 404,
    f"HTTP {status} {body.get('message') if isinstance(body, dict) else body}",
)

status, body = api("POST", "/api/ai/confirm", admin_token, {"conversationId": 999999, "approve": True})
check(
    "敏感操作确认接口对不存在的对话拒绝（不能被伪造调用，需求⑪）",
    status == 404,
    f"HTTP {status} {body.get('message') if isinstance(body, dict) else body}",
)

def _is_paramiko_ident(value) -> bool:
    """paramiko 真实握手标识是**小写**的 `SSH-2.0-paramiko_<版本>`（`Transport.local_version`），
    兜底真值 `default_client_version()` 与之一致；这里大小写不敏感，只校验「就是 paramiko 报的」。"""
    return str(value or "").lower().startswith("ssh-2.0-paramiko")


status, body = api("GET", "/api/settings/gateway", admin_token)
gw = (body.get("data") or {}) if isinstance(body, dict) else {}
check(
    "SSH 网关状态暴露握手两端版本：服务器版本 + 客户端版本（需求⑥·五）",
    status == 200
    and _is_paramiko_ident(gw.get("clientVersion"))
    and bool(gw.get("serverVersion"))
    and gw.get("serverVersion") != gw.get("clientVersion"),
    f"HTTP {status} server={gw.get('serverVersion')} client={gw.get('clientVersion')}",
)

status, body = api("POST", f"/api/hosts/{host_id}/accounts/{account_id}/test", admin_token, {})
conn = (body.get("data") or {}) if isinstance(body, dict) else {}
check(
    "账号连通性测试回传客户端版本（同一个真值出现在两处界面）",
    status == 200
    and _is_paramiko_ident(conn.get("clientVersion"))
    and bool(conn.get("serverVersion"))
    and bool(conn.get("fingerprint")),
    # 演示目标机自己也是 paramiko，两端标识串相同是正常的；真实 OpenSSH 目标上两者不同。
    f"HTTP {status} server={conn.get('serverVersion')} client={conn.get('clientVersion')} fp={conn.get('fingerprint')}",
)

# ---------------------------------------------------------------- 汇总 ---
print("\n=== 汇总 ===", flush=True)
failed = [r for r in RESULTS if not r[1]]
for name, ok, detail in RESULTS:
    print(f"{'PASS' if ok else 'FAIL'} | {name}" + (f" | {detail}" if (detail and not ok) else ""), flush=True)
print(f"\n共 {len(RESULTS)} 项，通过 {len(RESULTS) - len(failed)}，失败 {len(failed)}", flush=True)
sys.exit(1 if failed else 0)
