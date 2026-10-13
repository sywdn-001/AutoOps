"""ComputerUse 真机联调（选跑，不在默认门禁里）。

需求②③④的真机取证：单元测试能证明权限、校验、SSE 分帧与 MCP 报文解析，但证明不了
「目标机上真的装上了 windows-mcp、自启 VBS 真的起了进程、portproxy 真的把 8000 端口
映射到了堡垒机连得上的地址、堡垒机隔着 portproxy 调 MCP 真的能截到图」——那几件事只有
对着一台真 Windows 主机跑一遍才知道（本项目里它们各自暴露过一个缺陷：WinRS 命令结束
会杀掉子进程树、mcp 包只认回环 Host、端口接管有几秒窄缝）。

这个脚本就是那条通路的真机取证，断言分两段：

* **验证段（默认，只读）**：登录 → 列安装包版本 → 列可部署主机 → 查该主机部署状态 →
  探活 MCP → 列工具 → 调 `Screenshot` 存图 → 调 `PowerShell` 跑一条只读命令。
* **部署段（`--deploy`，会真的在目标机上装东西）**：建部署 → 订阅 SSE 日志到结束 →
  断言七个阶段都出现、终态是 success；随后接上验证段。

用法：
    # 只验「已经部署好」的侧边栏通路（最快，推荐日常复跑）
    python tools/computeruse_check.py --host 5 --admin-password <管理员口令>

    # 完整跑一遍七阶段部署，再验一遍
    python tools/computeruse_check.py --host 5 --account 5 --deploy --admin-password <口令>

    # 指定 Python 版本 / pip 源 / 访问密钥（前端向导的完整选项面）
    python tools/computeruse_check.py --host 5 --account 5 --deploy --version 3.13.16 \
        --pip-index https://pypi.tuna.tsinghua.edu.cn/simple --auth-key 3f9a1c7d2b8e4506af13c9d7e2b84510

    # 先把某个版本的安装包缓存到堡垒机，再验
    python tools/computeruse_check.py --host 5 --cache 3.13.16 --admin-password <口令>

    # 只看华为镜像上现在有哪些版本
    python tools/computeruse_check.py --list-versions

需要后端在跑（`python run.py`）、目标机开着 WinRM 且**已经部署过 ComputerUse**（默认只验
不装）；`--deploy` 会真的下载安装包、装 Python、装 windows-mcp、写自启项并改防火墙。
所有 MCP 调用都会进审计（`category=computeruse`），日志里能看出这次 AI 是「只读观察」
还是「会操作桌面」。截图默认落 `computeruse-evidence/`。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path

import requests

# 报告里有中文，Windows 控制台/管道默认 GBK（cp936）会让 print 抛 UnicodeEncodeError 截断报告。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ADMIN_USER = os.environ.get("BASTION_ADMIN", "admin")
ADMIN_PW = os.environ.get("BASTION_ADMIN_PASSWORD", "admin123")
BASE = os.environ.get("CU_BASE", "http://127.0.0.1:5000")
STAGES = ("prepare", "upload", "install", "pip", "vbs", "expose", "verify")
PIP_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"

PASS: list[str] = []
FAIL: list[str] = []
SKIP: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    (PASS if ok else FAIL).append(name)
    print(f"{'PASS' if ok else 'FAIL'} | {name}" + (f" | {detail}" if detail else ""), flush=True)
    return ok


def skip(name: str, why: str = "") -> None:
    """前置条件不成立时的跳过（不算失败，但也不假装通过）。"""
    SKIP.append(name)
    print(f"SKIP | {name}" + (f" | {why}" if why else ""), flush=True)


def summarize() -> str:
    text = f"共 {len(PASS) + len(FAIL)} 项，通过 {len(PASS)}，失败 {len(FAIL)}"
    if SKIP:
        text += f"，跳过 {len(SKIP)}（{'；'.join(SKIP)}）"
    return text


def brief(body: object, limit: int = 400) -> str:
    return json.dumps(body, ensure_ascii=False)[:limit]


class Bastion:
    """最小 API 客户端：登录一次，之后都带 Bearer。"""

    def __init__(self, base: str, user: str, password: str) -> None:
        self.base = base.rstrip("/")
        self.session = requests.Session()
        self._login(user, password)

    def _login(self, user: str, password: str) -> None:
        resp = self.session.post(
            f"{self.base}/api/auth/login",
            json={"username": user, "password": password},
            timeout=30,
        )
        body = resp.json() if resp.content else {}
        data = body.get("data") or {}
        token = data.get("token") or data.get("accessToken") or data.get("access_token")
        if not token:
            raise SystemExit(f"登录失败：HTTP {resp.status_code} {brief(body, 300)}")
        self.session.headers["Authorization"] = f"Bearer {token}"
        self.user = (data.get("user") or {}).get("username") or user

    def get(self, path: str, **kwargs):
        return self.session.get(f"{self.base}{path}", **kwargs)

    def post(self, path: str, payload: dict | None = None, **kwargs):
        return self.session.post(f"{self.base}{path}", json=payload or {}, **kwargs)

    def data(self, resp: requests.Response) -> dict:
        """取信封里的 data；data 为 null 或没带 data 时退回信封本身（去掉 data 键）。"""
        try:
            body = resp.json()
        except ValueError:
            return {"raw": resp.text[:600]}
        if not isinstance(body, dict):
            return {"raw": body}
        payload = body.get("data")
        if isinstance(payload, dict):
            return payload
        if payload is None:
            return {key: value for key, value in body.items() if key != "data"}
        return {"raw": payload}


def list_versions(cli: Bastion, args) -> dict:
    resp = cli.get("/api/computer-use/versions", timeout=180)
    data = cli.data(resp)
    items = data.get("items") or []
    print(f"[versions] HTTP {resp.status_code} 共 {len(items)} 个版本，partial={data.get('partial')}")
    for item in items[:args.show]:
        print(
            "  {version:<10} {name:<30} {size:>7} MCP兼容={mcp} 已缓存={cached}".format(
                version=item.get("version", ""),
                name=item.get("filename", ""),
                size=f"{item.get('size', 0) / 1048576:.1f}MB",
                mcp=item.get("mcpCompatible"),
                cached=bool(item.get("cached")),
            )
        )
    check("列安装包版本拿到清单", bool(items), f"{len(items)} 个")
    if items:
        first = items[0]
        check(
            "版本条目字段齐全",
            bool(first.get("version") and first.get("filename") and first.get("size")),
            brief(first, 200),
        )
    return {"items": items, "minPython": data.get("minPython")}


def list_hosts(cli: Bastion, args) -> dict:
    resp = cli.get("/api/computer-use/hosts", timeout=120)
    data = cli.data(resp)
    items = data.get("items") or []
    print(f"[hosts] HTTP {resp.status_code} 共 {len(items)} 台主机")
    for host in items:
        print(
            "  host#{id} {name} {address} winrm={port}/{transport} 已部署={ready} 账号={accounts}".format(
                id=host.get("id"),
                name=host.get("name"),
                address=host.get("address"),
                port=host.get("winrmPort"),
                transport=host.get("winrmTransport") or "-",
                ready=host.get("ready"),
                accounts=[(a.get("id"), a.get("username")) for a in (host.get("accounts") or [])],
            )
        )
    target = next((h for h in items if h.get("id") == args.host), None)
    check("可部署主机清单里有目标主机", target is not None, f"hostId={args.host}")
    if target is not None:
        check(
            "目标主机是 Windows 且有 winrm 端点",
            bool(target.get("hasWinrm") and target.get("winrmPort")),
            brief({"osType": target.get("osType"), "hasWinrm": target.get("hasWinrm"),
                   "winrmPort": target.get("winrmPort"), "winrmTransport": target.get("winrmTransport")}, 240),
        )
        check(
            "目标主机有可用账号",
            bool(target.get("accounts")),
            brief(target.get("accounts"), 200),
        )
    return {"items": items, "target": target or {}}


def cache_installer(cli: Bastion, args, version: str, items: list[dict]) -> None:
    match = next((i for i in items if i.get("version") == version), None)
    if match is None:
        check(f"镜像里有 {version} 的安装包", False, "版本清单里没这个版本")
        return
    print(f"[cache] {version} -> {match.get('filename')}（{match.get('size')} 字节）")
    resp = cli.post(
        "/api/computer-use/installers",
        {"version": version, "filename": match.get("filename")},
        timeout=1800,
    )
    data = cli.data(resp)
    print(f"[cache] HTTP {resp.status_code} {brief(data, 300)}")
    check(
        f"安装包已缓存到堡垒机（{version}）",
        resp.status_code == 200 and bool(data.get("sha256")),
        brief(data, 240),
    )


def stream_logs(cli: Bastion, deployment_id: int, timeout: float) -> tuple[dict, list[dict]]:
    """订阅 SSE 到 end，返回（终态快照, 每个阶段最后一次日志）。"""
    url = f"{self_base(cli)}/api/computer-use/deployments/{deployment_id}/logs"
    headers = {"Authorization": cli.session.headers["Authorization"], "Accept": "text/event-stream"}
    stages: dict[str, dict] = {}
    final: dict = {}
    event = ""
    with requests.get(url, headers=headers, stream=True, timeout=(20, timeout)) as resp:
        print(f"[sse] HTTP {resp.status_code} 开始收日志")
        for raw in resp.iter_lines(decode_unicode=True):
            line = (raw or "").strip()
            if not line:
                continue
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
                continue
            if not line.startswith("data:"):
                continue
            try:
                item = json.loads(line[5:].strip())
            except ValueError:
                continue
            if event == "log":
                stages[item.get("stage", "")] = item
                print(
                    "  [{stage}] {level:<7} {message}".format(
                        stage=item.get("stage", ""),
                        level=item.get("level", ""),
                        message=str(item.get("message", ""))[:300],
                    )
                )
            elif event == "status":
                print(
                    "  == 状态 {status} · 阶段 {stage} · {progress}%".format(
                        status=item.get("status"),
                        stage=item.get("stageLabel") or item.get("stage"),
                        progress=item.get("progress"),
                    )
                )
                final = item
            elif event == "end":
                final = item
                print(f"  == 流结束 {brief(item, 300)}")
                break
    return final, list(stages.values())


def self_base(cli: Bastion) -> str:
    return cli.base


def deploy(cli: Bastion, args, items: list[dict]) -> int | None:
    match = next((i for i in items if i.get("version") == args.version), None)
    if match is None:
        check(f"镜像里有 {args.version}", False, "版本清单里没这个版本")
        return None
    payload = {
        "hostId": args.host,
        "accountId": args.account,
        "pythonVersion": args.version,
        "installerName": args.installer or match.get("filename"),
        "options": {
            "mcpPort": args.mcp_port,
            "pipIndex": args.pip_index,
            "bindAll": False,
        },
    }
    if args.auth_key:
        payload["options"]["authKey"] = args.auth_key
    print(f"[deploy] POST /deployments {brief(payload, 300)}")
    resp = cli.post("/api/computer-use/deployments", payload, timeout=120)
    data = cli.data(resp)
    deployment_id = data.get("id")
    check("创建部署", bool(deployment_id) and resp.status_code in (200, 201), brief(data, 300))
    if not deployment_id:
        return None
    print(f"[deploy] 部署 id={deployment_id} 初始状态={data.get('status')}")
    final, logs = stream_logs(cli, int(deployment_id), args.sse_timeout)
    seen = {row.get("stage") for row in logs}
    for stage in STAGES:
        check(f"七阶段日志出现 {stage}", stage in seen)
    check(
        "部署终态 success",
        str(final.get("status")) == "success",
        brief({k: final.get(k) for k in ("status", "stage", "progress", "error")}, 300),
    )
    return int(deployment_id)


def verify_sidebar(cli: Bastion, args, out_dir: Path) -> None:
    resp = cli.get("/api/computer-use/status", params={"hostId": args.host}, timeout=120)
    status = cli.data(resp)
    print(f"[status] HTTP {resp.status_code} {brief(status, 500)}")
    check("部署状态 ready", status.get("ready") is True, brief(status.get("error") or status, 200))
    check("拿到 MCP 地址", bool(status.get("mcpUrl")), str(status.get("mcpUrl")))
    if args.auth_key:
        check("访问密钥已生效", status.get("hasAuthKey") is True, f"hasAuthKey={status.get('hasAuthKey')}")

    probe_resp = cli.post("/api/computer-use/mcp/probe", {"hostId": args.host}, timeout=180)
    probe = cli.data(probe_resp)
    print(f"[probe] HTTP {probe_resp.status_code} {brief(probe, 500)}")
    alive = check("MCP 探活成功", probe.get("ok") is True, brief(probe.get("error") or probe, 200))
    if not alive:
        print(
            "  提示：探活失败最常见的原因是目标机上现在没人登录桌面 —— Windows-MCP 需要一个登录会话，"
            "没人登录时开机自启脚本不会跑、挂在后台会话（会话 0）里的 MCP 也截不了图。"
            "用 RDP 登录目标机之后再跑一次这条命令。",
            flush=True,
        )
        skip("工具清单 / 截图 / PowerShell 检查", "MCP 探活失败，目标机上大概没人登录桌面")
        return
    server = probe.get("server") or {}
    check("探活报出工具数", int(probe.get("toolCount") or 0) > 0, f"toolCount={probe.get('toolCount')}")
    check("服务端自报是 windows-mcp", "windows-mcp" in str(server.get("name", "")), brief(server, 200))

    tools_resp = cli.get(
        "/api/computer-use/mcp/tools",
        params={"hostId": args.host, "live": 1},
        timeout=180,
    )
    tools = cli.data(tools_resp)
    listed = tools.get("items") or tools.get("tools") or []
    names = [t.get("name") for t in listed]
    print(f"[tools] HTTP {tools_resp.status_code} {len(names)} 个：{names[:24]}")
    check("工具清单非空", bool(names), f"{len(names)} 个")
    check("工具清单里有 Screenshot", "Screenshot" in names)
    check(
        "工具清单带只读/会改机器状态的标记",
        bool(listed) and all(("observeOnly" in (t or {}) or "mutating" in (t or {})) for t in listed),
        f"{len(listed)} 个",
    )

    started = time.time()
    shot = cli.post(
        "/api/computer-use/mcp/call",
        {"hostId": args.host, "tool": "Screenshot", "arguments": {}},
        timeout=args.call_timeout,
    )
    data = cli.data(shot)
    images = data.get("images") or []
    print(
        f"[screenshot] HTTP {shot.status_code} 图 {len(images)} 张 耗时 {data.get('durationMs')}ms "
        f"文本 {str(data.get('text'))[:160]!r}"
    )
    check("Screenshot 返回图片", bool(images), f"{len(images)} 张")
    saved = 0
    for index, image in enumerate(images):
        payload = str(image.get("dataUrl") or "").split(",", 1)[-1]
        if not payload:
            continue
        blob = base64.b64decode(payload)
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / f"mcp-screenshot-{index}-{len(blob)}.png"
        target.write_bytes(blob)
        saved += 1
        print(f"  已存图 {target}（{len(blob)} 字节，{image.get('mime')}）")
    check("截图落盘且不是空图", saved > 0 and images and len(base64.b64decode(
        str(images[0].get("dataUrl") or "").split(",", 1)[-1]
    )) > 1024, f"{saved} 张")
    check("截图调用被标成只读观察", data.get("observeOnly") is True, f"observeOnly={data.get('observeOnly')}")
    print(f"[screenshot] 端到端 {time.time() - started:.1f}s")

    if args.no_powershell:
        return
    command = "$env:COMPUTERNAME; (Get-Process | Measure-Object).Count"
    ps = cli.post(
        "/api/computer-use/mcp/call",
        {"hostId": args.host, "tool": "PowerShell", "arguments": {"command": command}},
        timeout=args.call_timeout,
    )
    psdata = cli.data(ps)
    text = str(psdata.get("text") or "")
    print(f"[powershell] HTTP {ps.status_code} mutating={psdata.get('mutating')} 文本 {text[:240]!r}")
    check("PowerShell 工具在目标机上跑出输出", bool(text.strip()), text[:160])
    check("PowerShell 被标成会改机器状态", psdata.get("mutating") is True, f"mutating={psdata.get('mutating')}")
    if args.expect_computer:
        check(
            f"输出里出现目标机名 {args.expect_computer}",
            args.expect_computer.lower() in text.lower(),
            text[:160],
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ComputerUse 真机联调（部署 + 侧边栏 MCP 调用）")
    parser.add_argument("--base", default=BASE, help=f"后端地址（默认 {BASE}，可用 CU_BASE 覆盖）")
    parser.add_argument("--admin-user", default=ADMIN_USER, help="管理员账号（默认 admin）")
    parser.add_argument("--admin-password", default=ADMIN_PW, help="管理员口令（默认取 BASTION_ADMIN_PASSWORD）")
    parser.add_argument("--host", type=int, default=0, help="目标主机 id（在 /hosts 里找）")
    parser.add_argument("--account", type=int, default=0, help="目标机上的账号 id（--deploy 必填）")
    parser.add_argument("--deploy", action="store_true", help="真的跑一遍七阶段部署（会装东西、改防火墙）")
    parser.add_argument("--cache", default="", metavar="VERSION", help="先把该版本的安装包缓存到堡垒机")
    parser.add_argument("--list-versions", action="store_true", help="只列出镜像上的可用版本")
    parser.add_argument("--version", default="3.13.16", help="要部署的 Python 版本（默认 3.13.16）")
    parser.add_argument("--installer", default="", help="安装包文件名（默认用版本清单里的）")
    parser.add_argument("--pip-index", default=PIP_INDEX, help=f"pip 源（默认清华源 {PIP_INDEX}）")
    parser.add_argument("--auth-key", default="", help="给这台机器的 MCP 设访问密钥（前端向导默认会生成一个）")
    parser.add_argument("--mcp-port", type=int, default=8000, help="目标机上 MCP 监听端口（默认 8000）")
    parser.add_argument("--expect-computer", default="", help="断言 PowerShell 输出里包含这个目标机名")
    parser.add_argument("--no-powershell", action="store_true", help="跳过最后那条会改机器状态的 PowerShell 调用")
    parser.add_argument("--out-dir", default="computeruse-evidence", help="截图落盘目录")
    parser.add_argument("--show", type=int, default=8, help="版本清单里打印几行")
    parser.add_argument("--call-timeout", type=float, default=180.0, help="单次 MCP 调用的 HTTP 超时（秒）")
    parser.add_argument("--sse-timeout", type=float, default=900.0, help="SSE 读完一次部署的超时（秒）")
    args = parser.parse_args(argv)
    out_dir = Path(args.out_dir)

    try:
        cli = Bastion(args.base, args.admin_user, args.admin_password)
    except Exception as exc:  # noqa: BLE001
        check("登录堡垒机", False, f"{type(exc).__name__}: {exc}")
        print("\n" + summarize(), flush=True)
        return 1
    check("登录堡垒机", True, f"用户 {cli.user} @ {cli.base}")

    try:
        versions = list_versions(cli, args)
        if args.list_versions:
            print("\n" + summarize(), flush=True)
            return 1 if FAIL else 0
        if not args.host:
            check("指定目标主机", False, "日常验证请带 --host <id>")
        else:
            if args.cache:
                cache_installer(cli, args, args.cache, versions["items"])
            list_hosts(cli, args)
            if args.deploy:
                check("--deploy 需要 --account", bool(args.account), f"account={args.account}")
                if args.account:
                    deploy(cli, args, versions["items"])
            verify_sidebar(cli, args, out_dir)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        check("ComputerUse 真机联调", False, f"{type(exc).__name__}: {exc}")

    print("\n" + summarize(), flush=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
