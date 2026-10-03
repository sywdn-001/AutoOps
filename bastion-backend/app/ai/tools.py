"""AI 运维工具注册表。

设计要点：**工具不重写业务逻辑**，而是把堡垒机自己的 REST API 声明式暴露给模型，
执行时用 Flask 的 ``test_client`` 带上调用者的 JWT 打自己的接口，于是

* 权限校验（``@permission_required``）自动复用人类路径；
* 参数校验 / 资产口令解密 / 命令策略拦截 全部走原来的代码；
* 审计（``audit_logs`` / ``command_logs`` / ``file_logs``）自动落库，AI 与人类同源。

工具分三档权限（见 ``app/security.py``）::

    ai:tool        只读查询（看一眼，不改任何东西）
    ai:tool_write  增删改（默认标记为"敏感"，需要管理员密码二次确认）
    ai:tool_exec   连目标机 / 跑命令 / 传文件（默认敏感）

参数声明用紧凑语法：``"host_id:int!:主机 ID"``，``;`` 分隔，``!`` 表示必填，
type 为 ``dict`` 的参数会被就地展开进请求体（``/api/settings`` 的 ``values`` 就用它）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import quote, urlencode

#: 三档工具权限
READ = "ai:tool"
WRITE = "ai:tool_write"
EXEC = "ai:tool_exec"

_TYPES = {"str", "int", "bool", "number", "list", "dict"}


@dataclass(frozen=True)
class ToolArg:
    """工具入参声明。"""

    name: str
    type: str = "str"
    desc: str = ""
    required: bool = False
    #: dict 类型：展开进请求体
    spread: bool = False

    def to_schema(self) -> dict:
        type_map = {
            "str": "string",
            "int": "integer",
            "bool": "boolean",
            "number": "number",
            "list": "array",
            "dict": "object",
        }
        schema: dict = {"type": type_map.get(self.type, "string")}
        if self.type == "list":
            schema["items"] = {"type": "string"}
        if self.desc:
            schema["description"] = self.desc
        return schema

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "type": self.type,
            "description": self.desc,
            "required": self.required,
        }


@dataclass(frozen=True)
class Tool:
    """一个可被 AI 调用的工具。"""

    name: str
    description: str
    method: str
    path: str
    args: tuple[ToolArg, ...] = ()
    permission: str = READ
    category: str = "其他"
    sensitive: bool = False
    query_args: tuple[str, ...] = ()
    #: 非空时：工具结果会自动生成一张卡片（前端渲染成表格/键值卡）
    card: str = ""

    def schema(self) -> dict:
        properties = {arg.name: arg.to_schema() for arg in self.args}
        required = [arg.name for arg in self.args if arg.required]
        parameters: dict = {"type": "object", "properties": properties, "additionalProperties": False}
        if required:
            parameters["required"] = required
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": parameters,
            },
        }

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "permission": self.permission,
            "sensitive": self.sensitive,
            "method": self.method,
            "path": self.path,
            "args": [arg.to_dict() for arg in self.args],
        }


def _parse_args(spec: str) -> tuple[ToolArg, ...]:
    """``"host_id:int!:主机 ID;keyword:str:模糊匹配"`` → ToolArg 元组。"""
    out: list[ToolArg] = []
    for chunk in (spec or "").split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(":", 2)
        name = parts[0].strip()
        raw_type = parts[1].strip() if len(parts) > 1 else "str"
        desc = parts[2].strip() if len(parts) > 2 else ""
        required = raw_type.endswith("!")
        raw_type = raw_type.rstrip("!")
        if raw_type not in _TYPES:
            raw_type = "str"
        out.append(
            ToolArg(
                name=name,
                type=raw_type,
                desc=desc,
                required=required,
                spread=raw_type == "dict",
            )
        )
    return tuple(out)


def T(  # noqa: N802 - 紧凑注册用的工厂函数
    name: str,
    description: str,
    method: str,
    path: str,
    args: str = "",
    *,
    permission: str = READ,
    category: str = "其他",
    sensitive: bool = False,
    query: Iterable[str] = (),
    card: str = "",
) -> Tool:
    return Tool(
        name=name,
        description=description,
        method=method,
        path=path,
        args=_parse_args(args),
        permission=permission,
        category=category,
        sensitive=sensitive,
        query_args=tuple(query),
        card=card,
    )


# --------------------------------------------------------------------------
# 工具清单（按人力能做的操作一一对应）
# --------------------------------------------------------------------------
_TOOLS: list[Tool] = [
    # ---------------- 总览 / 健康 ----------------
    T("get_overview", "运维总览：在线会话、24h 会话/命令/拦截数、主机与授权数量、14 天趋势、最近命令与审计事件", "GET", "/api/dashboard/overview", category="总览"),
    T("get_my_overview", "当前登录用户自己的概览数据（我今天的会话、命令、被拦截次数）", "GET", "/api/dashboard/mine", category="总览"),
    T("get_health", "后端健康检查：服务状态、网关是否启用、监听端口", "GET", "/api/health", category="总览"),
    T("get_gateway_status", "SSH 网关状态：是否运行、监听地址、在线会话数、接入命令示例", "GET", "/api/settings/gateway", category="总览"),
    # ---------------- 资产：主机 ----------------
    T("list_hosts", "查询被纳管的服务器列表", "GET", "/api/hosts", "keyword:str:主机名/地址/描述模糊匹配;groupId:int:按分组过滤;status:str:active|disabled;page:int;pageSize:int", category="资产", card="主机列表"),
    T("get_host", "查看某台主机的详情", "GET", "/api/hosts/{host_id}", "host_id:int!:主机 ID", category="资产"),
    T("list_host_options", "主机下拉候选项（id + 名称 + 地址）", "GET", "/api/hosts/options", category="资产"),
    T("create_host", "新增一台被纳管的服务器（需要管理员密码确认）", "POST", "/api/hosts", "name:str!:主机别名;address:str!:IP 或域名;port:int:SSH 端口，默认 22;protocol:str:ssh;osType:str:linux|windows;groupId:int:所属分组 ID;status:str:active;tags:list:标签;description:str:备注", permission=WRITE, category="资产", sensitive=True),
    T("update_host", "修改主机信息（名称、地址、端口、分组、状态、备注等，需管理员密码确认）", "PUT", "/api/hosts/{host_id}", "host_id:int!:主机 ID;name:str;address:str;port:int;protocol:str;osType:str;groupId:int;status:str;tags:list;description:str", permission=WRITE, category="资产", sensitive=True),
    T("delete_host", "删除一台主机（连同它的账号与授权，需管理员密码确认）", "DELETE", "/api/hosts/{host_id}", "host_id:int!:主机 ID", permission=WRITE, category="资产", sensitive=True),
    # ---------------- 资产：分组 ----------------
    T("list_host_groups", "查询主机分组", "GET", "/api/host-groups", "keyword:str:分组名模糊匹配", category="资产", card="主机分组"),
    T("list_host_group_options", "主机分组下拉候选项", "GET", "/api/host-groups/options", category="资产"),
    T("create_host_group", "新建主机分组（需管理员密码确认）", "POST", "/api/host-groups", "name:str!:分组名;description:str:描述", permission=WRITE, category="资产", sensitive=True),
    T("update_host_group", "修改主机分组（需管理员密码确认）", "PUT", "/api/host-groups/{group_id}", "group_id:int!:分组 ID;name:str;description:str", permission=WRITE, category="资产", sensitive=True),
    T("delete_host_group", "删除主机分组（需管理员密码确认）", "DELETE", "/api/host-groups/{group_id}", "group_id:int!:分组 ID", permission=WRITE, category="资产", sensitive=True),
    # ---------------- 资产：登录账号 ----------------
    T("list_host_accounts", "查看某台主机上配置的登录账号（名称、登录用户、认证方式、是否有口令）", "GET", "/api/hosts/{host_id}/accounts", "host_id:int!:主机 ID", category="资产", card="主机账号"),
    T("create_host_account", "给主机新增一个登录账号（密码或密钥，需管理员密码确认）", "POST", "/api/hosts/{host_id}/accounts", "host_id:int!:主机 ID;name:str!:账号别名;username:str!:登录用户名;authType:str:password|key;password:str:登录密码;privateKey:str:私钥内容;passphrase:str:私钥口令;sudoCommand:str:sudo 前缀;description:str:备注", permission=WRITE, category="资产", sensitive=True),
    T("update_host_account", "修改主机账号（可换密码/密钥，需管理员密码确认）", "PUT", "/api/hosts/{host_id}/accounts/{account_id}", "host_id:int!:主机 ID;account_id:int!:账号 ID;name:str;username:str;authType:str;password:str;privateKey:str;passphrase:str;sudoCommand:str;description:str", permission=WRITE, category="资产", sensitive=True),
    T("delete_host_account", "删除主机账号（需管理员密码确认）", "DELETE", "/api/hosts/{host_id}/accounts/{account_id}", "host_id:int!:主机 ID;account_id:int!:账号 ID", permission=WRITE, category="资产", sensitive=True),
    T("test_host_account", "测试主机账号能否登录（真的去连一次目标机）", "POST", "/api/hosts/{host_id}/accounts/{account_id}/test", "host_id:int!:主机 ID;account_id:int!:账号 ID", permission=EXEC, category="资产", sensitive=True),
    # ---------------- 身份：用户 ----------------
    T("list_users", "查询堡垒机账号列表（登录名、姓名、角色、状态、最近登录）", "GET", "/api/users", "keyword:str:登录名/姓名/邮箱模糊匹配;roleId:int:按角色过滤;status:str:active|disabled;page:int;pageSize:int", category="身份", card="用户列表"),
    T("get_user", "查看某个堡垒机账号的详情与权限", "GET", "/api/users/{user_id}", "user_id:int!:用户 ID", category="身份"),
    T("list_user_options", "用户下拉候选项", "GET", "/api/users/options", category="身份"),
    T("create_user", "新建堡垒机账号（需管理员密码确认）", "POST", "/api/users", "username:str!:登录名;password:str!:初始密码;displayName:str:姓名;roleId:int:角色 ID;email:str;phone:str;status:str:active|disabled;remark:str:备注;mustChangePassword:bool:首次登录是否强制改密;isSuperuser:bool:是否超级管理员;gatewayEnabled:bool:允许 SSH 网关登录;webtermEnabled:bool:允许网页终端", permission=WRITE, category="身份", sensitive=True),
    T("update_user", "修改堡垒机账号（角色、状态、姓名、联系方式等，需管理员密码确认）", "PUT", "/api/users/{user_id}", "user_id:int!:用户 ID;displayName:str;roleId:int;email:str;phone:str;status:str;remark:str;mustChangePassword:bool;isSuperuser:bool;gatewayEnabled:bool;webtermEnabled:bool", permission=WRITE, category="身份", sensitive=True),
    T("delete_user", "删除堡垒机账号（需管理员密码确认）", "DELETE", "/api/users/{user_id}", "user_id:int!:用户 ID", permission=WRITE, category="身份", sensitive=True),
    T("reset_user_password", "重置某个账号的登录密码（需管理员密码确认）", "POST", "/api/users/{user_id}/password", "user_id:int!:用户 ID;password:str!:新密码", permission=WRITE, category="身份", sensitive=True),
    T("unlock_user", "解锁因连续输错密码被锁定的账号（需管理员密码确认）", "POST", "/api/users/{user_id}/unlock", "user_id:int!:用户 ID", permission=WRITE, category="身份", sensitive=True),
    T("get_my_profile", "查看我自己的账号信息与权限清单", "GET", "/api/auth/me", category="身份"),
    T("get_current_user", "Ant Design Pro 口径的当前用户信息", "GET", "/api/currentUser", category="身份"),
    # ---------------- 身份：角色与权限 ----------------
    T("list_roles", "查询角色列表（角色码、名称、权限数量、用户数）", "GET", "/api/roles", "keyword:str:角色名/角色码模糊匹配", category="身份", card="角色列表"),
    T("get_role", "查看某个角色的详情与它拥有的权限码", "GET", "/api/roles/{role_id}", "role_id:int!:角色 ID", category="身份"),
    T("list_role_options", "角色下拉候选项", "GET", "/api/roles/options", category="身份"),
    T("list_permissions", "查看系统全部权限码目录（按分组，含中文说明）——分配角色权限前先看这个", "GET", "/api/roles/permissions", category="身份", card="权限目录"),
    T("create_role", "新建角色并分配权限（需管理员密码确认）", "POST", "/api/roles", "code:str!:角色码;name:str!:角色名;permissions:list:权限码列表;description:str:描述", permission=WRITE, category="身份", sensitive=True),
    T("update_role", "修改角色名称、描述或权限集合（需管理员密码确认）", "PUT", "/api/roles/{role_id}", "role_id:int!:角色 ID;name:str;permissions:list:权限码列表;description:str", permission=WRITE, category="身份", sensitive=True),
    T("delete_role", "删除角色（需管理员密码确认）", "DELETE", "/api/roles/{role_id}", "role_id:int!:角色 ID", permission=WRITE, category="身份", sensitive=True),
    # ---------------- 访问授权 ----------------
    T("list_grants", "查询访问授权列表（谁可以用哪个账号登哪台机器、有哪些能力开关）", "GET", "/api/grants", "keyword:str:关键字;userId:int:按用户过滤;hostId:int:按主机过滤;enabled:str:true|false;page:int;pageSize:int", category="授权", card="访问授权"),
    T("get_grant", "查看一条访问授权的完整开关（登录/SFTP/上传/下载/改文件/端口转发/网页终端、时间窗、并发上限）", "GET", "/api/grants/{grant_id}", "grant_id:int!:授权 ID", category="授权"),
    T("create_grant", "新增访问授权：把某台主机的某个账号授权给某个用户（需管理员密码确认）", "POST", "/api/grants", "userId:int!:被授权的用户 ID;hostId:int!:主机 ID;hostAccountId:int:限定账号 ID（留空=该主机全部账号）;policyId:int:命令策略 ID;filePolicyId:int:文件策略 ID;canLogin:bool:允许登录;canSftp:bool:允许 SFTP;canUpload:bool:允许上传;canDownload:bool:允许下载;canFileWrite:bool:允许改文件;canPortForward:bool:允许端口转发;canWebterm:bool:允许网页终端;timeStart:str:每日起始时间 HH:MM;timeEnd:str:每日结束时间 HH:MM;weekdays:list:允许的星期 0-6;expireAt:str:到期时间 ISO8601;maxSessions:int:并发上限;enabled:bool:启用;remark:str:备注", permission=WRITE, category="授权", sensitive=True),
    T("update_grant", "修改一条访问授权（开关、时间窗、策略，需管理员密码确认）", "PUT", "/api/grants/{grant_id}", "grant_id:int!:授权 ID;hostAccountId:int;policyId:int;filePolicyId:int;canLogin:bool;canSftp:bool;canUpload:bool;canDownload:bool;canFileWrite:bool;canPortForward:bool;canWebterm:bool;timeStart:str;timeEnd:str;weekdays:list;expireAt:str;maxSessions:int;enabled:bool;remark:str", permission=WRITE, category="授权", sensitive=True),
    T("delete_grant", "删除一条访问授权（需管理员密码确认）", "DELETE", "/api/grants/{grant_id}", "grant_id:int!:授权 ID", permission=WRITE, category="授权", sensitive=True),
    T("batch_grant", "批量授权：把一个用户的访问权限一次性授给多台主机（需管理员密码确认）", "POST", "/api/grants/batch", "userId:int!:用户 ID;hostIds:list!:主机 ID 列表;policyId:int:命令策略 ID;filePolicyId:int:文件策略 ID;canLogin:bool;canSftp:bool;canUpload:bool;canDownload:bool;canFileWrite:bool;canPortForward:bool;canWebterm:bool;enabled:bool;remark:str", permission=WRITE, category="授权", sensitive=True),
    T("get_grant_matrix", "用户授权矩阵：某个用户可以访问哪些主机、有哪些能力", "GET", "/api/grants/matrix", "userId:int:用户 ID（留空=全部）", category="授权", card="授权矩阵"),
    T("preview_grant", "模拟某个用户在某台主机上执行一条命令：能不能登、命令会不会被拦", "POST", "/api/grants/preview", "userId:int!:用户 ID;hostId:int!:主机 ID;command:str:要试算的命令", category="授权"),
    # ---------------- 命令策略 ----------------
    T("list_command_policies", "查询命令策略列表（默认动作、规则数、被引用的授权数）", "GET", "/api/policies", category="命令策略", card="命令策略"),
    T("get_command_policy", "查看某个命令策略的详情", "GET", "/api/policies/{policy_id}", "policy_id:int!:策略 ID", category="命令策略"),
    T("list_command_policy_options", "命令策略下拉候选项", "GET", "/api/policies/options", category="命令策略"),
    T("create_command_policy", "新建命令策略（需管理员密码确认）", "POST", "/api/policies", "name:str!:策略名;defaultAction:str:allow|deny 默认动作;description:str:描述;enabled:bool:启用", permission=WRITE, category="命令策略", sensitive=True),
    T("update_command_policy", "修改命令策略（名称、默认动作、启停，需管理员密码确认）", "PUT", "/api/policies/{policy_id}", "policy_id:int!:策略 ID;name:str;defaultAction:str;description:str;enabled:bool", permission=WRITE, category="命令策略", sensitive=True),
    T("delete_command_policy", "删除命令策略（需管理员密码确认）", "DELETE", "/api/policies/{policy_id}", "policy_id:int!:策略 ID", permission=WRITE, category="命令策略", sensitive=True),
    T("list_command_rules", "查询命令策略下的规则（优先级、动作、匹配方式、正则），查高危命令怎么写就靠它", "GET", "/api/policy-rules", "policyId:int:策略 ID;keyword:str:关键字", category="命令策略", card="命令规则"),
    T("create_command_rule", "给命令策略新增一条规则（拦截/放行某类命令，需管理员密码确认）", "POST", "/api/policy-rules", "policyId:int!:策略 ID;priority:int:优先级，越小越先命中;action:str!:deny|allow|warn;pattern:str!:匹配表达式;matchType:str:regex|glob|prefix|contains;riskLevel:str:low|medium|high|critical;description:str:说明;enabled:bool:启用", permission=WRITE, category="命令策略", sensitive=True),
    T("update_command_rule", "修改命令规则（优先级、动作、表达式、启停，需管理员密码确认）", "PUT", "/api/policy-rules/{rule_id}", "rule_id:int!:规则 ID;priority:int;action:str;pattern:str;matchType:str;riskLevel:str;description:str;enabled:bool", permission=WRITE, category="命令策略", sensitive=True),
    T("delete_command_rule", "删除命令规则（需管理员密码确认）", "DELETE", "/api/policy-rules/{rule_id}", "rule_id:int!:规则 ID", permission=WRITE, category="命令策略", sensitive=True),
    T("evaluate_command", "命令试算：判断一条命令会被放行还是拦截、命中哪条规则、风险等级", "POST", "/api/policies/evaluate", "command:str!:要试算的命令;policyId:int:策略 ID（留空=默认策略）", category="命令策略"),
    T("test_command_policy", "拿一条命令测试某个命令策略的判定结果", "POST", "/api/policies/{policy_id}/test", "policy_id:int!:策略 ID;command:str!:要测试的命令", category="命令策略"),
    T("reset_command_policies", "恢复内置命令策略（会覆盖现有内置策略的规则，需管理员密码确认）", "POST", "/api/policies/reset-builtin", permission=WRITE, category="命令策略", sensitive=True),
    # ---------------- 文件策略 ----------------
    T("list_file_policies", "查询文件策略列表（默认动作、规则数、引用数）", "GET", "/api/file-policies", "keyword:str:策略名模糊匹配", category="文件策略", card="文件策略"),
    T("get_file_policy", "查看某个文件策略的详情", "GET", "/api/file-policies/{policy_id}", "policy_id:int!:策略 ID", category="文件策略"),
    T("list_file_policy_options", "文件策略下拉候选项", "GET", "/api/file-policies/options", category="文件策略"),
    T("list_file_operations", "文件操作全集（list/read/download/archive/upload/write/mkdir/rename/move/copy/delete/chmod）", "GET", "/api/file-policies/operations", category="文件策略"),
    T("create_file_policy", "新建文件策略（需管理员密码确认）", "POST", "/api/file-policies", "name:str!:策略名;defaultAction:str:allow|deny;description:str:描述;enabled:bool:启用", permission=WRITE, category="文件策略", sensitive=True),
    T("update_file_policy", "修改文件策略（需管理员密码确认）", "PUT", "/api/file-policies/{policy_id}", "policy_id:int!:策略 ID;name:str;defaultAction:str;description:str;enabled:bool", permission=WRITE, category="文件策略", sensitive=True),
    T("delete_file_policy", "删除文件策略（需管理员密码确认）", "DELETE", "/api/file-policies/{policy_id}", "policy_id:int!:策略 ID", permission=WRITE, category="文件策略", sensitive=True),
    T("list_file_rules", "查询文件策略下的路径规则（优先级、操作、匹配方式、路径模式）", "GET", "/api/file-rules", "policyId:int:策略 ID;keyword:str:关键字", category="文件策略", card="文件规则"),
    T("create_file_rule", "给文件策略新增一条路径规则（例如禁止访问 .ssh、禁止改系统目录，需管理员密码确认）", "POST", "/api/file-rules", "policyId:int!:策略 ID;priority:int:优先级;action:str!:deny|allow|warn;operation:str!:操作名或 *;pathPattern:str!:路径模式;matchType:str:glob|regex|prefix|contains;riskLevel:str:low|medium|high|critical;description:str:说明;enabled:bool:启用", permission=WRITE, category="文件策略", sensitive=True),
    T("update_file_rule", "修改文件路径规则（需管理员密码确认）", "PUT", "/api/file-rules/{rule_id}", "rule_id:int!:规则 ID;priority:int;action:str;operation:str;pathPattern:str;matchType:str;riskLevel:str;description:str;enabled:bool", permission=WRITE, category="文件策略", sensitive=True),
    T("delete_file_rule", "删除文件路径规则（需管理员密码确认）", "DELETE", "/api/file-rules/{rule_id}", "rule_id:int!:规则 ID", permission=WRITE, category="文件策略", sensitive=True),
    T("evaluate_file_operation", "文件操作试算：判断某个文件操作会不会被策略拦截、命中哪条规则", "POST", "/api/file-policies/evaluate", "operation:str!:操作名;path:str!:路径;targetPath:str:目标路径（rename/move/copy 需要）;policyId:int:策略 ID", category="文件策略"),
    T("test_file_policy", "拿一个文件操作测试某个文件策略的判定结果", "POST", "/api/file-policies/{policy_id}/test", "policy_id:int!:策略 ID;operation:str!:操作名;path:str!:路径;targetPath:str:目标路径", category="文件策略"),
    T("reset_file_policies", "恢复内置文件策略（需管理员密码确认）", "POST", "/api/file-policies/reset-builtin", permission=WRITE, category="文件策略", sensitive=True),
    # ---------------- 会话与审计 ----------------
    T("list_sessions", "查询会话记录（谁在什么时候登了哪台机器、协议、状态、命令数）", "GET", "/api/sessions", "keyword:str:会话号/用户/主机模糊匹配;userId:int;hostId:int;protocol:str:ssh|sftp;status:str:online|closed|terminated;page:int;pageSize:int", category="审计", card="会话记录"),
    T("get_session", "查看某次会话的详情", "GET", "/api/sessions/{session_id}", "session_id:int!:会话 ID", category="审计"),
    T("list_session_commands", "查看某次会话里执行过的所有命令（含动作、风险等级、耗时）", "GET", "/api/sessions/{session_id}/commands", "session_id:int!:会话 ID", category="审计", card="会话命令"),
    T("get_session_transcript", "取某次会话的终端回放记录", "GET", "/api/sessions/{session_id}/transcript", "session_id:int!:会话 ID", category="审计"),
    T("list_online_sessions", "当前在线会话（谁的终端/SFTP 正连着哪台机器）", "GET", "/api/sessions/online", category="审计", card="在线会话"),
    T("terminate_session", "强制中断一个在线会话（踢人下线，需管理员密码确认）", "POST", "/api/sessions/{session_id}/terminate", "session_id:int!:会话 ID", permission=WRITE, category="审计", sensitive=True),
    T("purge_sessions", "清除会话记录（空筛选全部清除，需管理员密码确认）", "POST", "/api/sessions/delete", "all:bool:true=清除全部;ids:list:要清除的会话 ID 列表", permission=WRITE, category="审计", sensitive=True),
    T("list_commands", "查询命令审计记录（命令内容、动作、风险等级、命中规则、执行时间）", "GET", "/api/commands", "keyword:str:命令内容/命中规则模糊匹配;userId:int;hostId:int;action:str:allow|deny|warn;riskLevel:str;page:int;pageSize:int", category="审计", card="命令记录"),
    T("get_command_output", "查看某条命令的完整命令与输出回显（回答『刚才那条命令结果是什么』就靠它）", "GET", "/api/commands/{command_id}", "command_id:int!:命令记录 ID", category="审计"),
    T("purge_commands", "清除命令审计记录（需管理员密码确认）", "POST", "/api/commands/delete", "all:bool:true=清除全部;ids:list:命令记录 ID 列表", permission=WRITE, category="审计", sensitive=True),
    T("list_audits", "查询身份审计日志（登录、增删改、策略变更等所有控制台动作）", "GET", "/api/audits", "keyword:str:关键字;category:str:auth|console|asset|policy|grant|session|system|ai;action:str;result:str:success|failure;actor:str:操作人;page:int;pageSize:int", category="审计", card="审计日志"),
    T("get_audit", "查看一条审计日志的详情（含变更明细）", "GET", "/api/audits/{audit_id}", "audit_id:int!:审计 ID", category="审计"),
    T("list_audit_options", "审计日志的筛选候选项", "GET", "/api/audits/options", category="审计"),
    T("purge_audits", "清除审计日志（需管理员密码确认）", "POST", "/api/audits/delete", "all:bool:true=清除全部;ids:list:审计 ID 列表", permission=WRITE, category="审计", sensitive=True),
    T("list_file_logs", "查询文件操作审计（SFTP 里的浏览/上传/下载/改名/删除等每一步）", "GET", "/api/audits/files", "keyword:str;operation:str;action:str:allow|deny;result:str:success|denied|failure;riskLevel:str;username:str;hostName:str;sid:str:会话号;page:int;pageSize:int", category="审计", card="文件操作记录"),
    T("list_file_log_options", "文件操作审计的筛选候选项", "GET", "/api/audits/files/options", category="审计"),
    T("purge_file_logs", "清除文件操作审计（需管理员密码确认）", "POST", "/api/audits/files/delete", "all:bool:true=清除全部;ids:list:记录 ID 列表", permission=WRITE, category="审计", sensitive=True),
    # ---------------- 终端与执行 ----------------
    T("list_terminal_targets", "查看我当前有权登录的主机清单（含授权能力开关、命令策略名）——要执行命令前先看这个拿 hostId/accountId", "GET", "/api/terminal/targets", category="执行", card="可访问主机"),
    T("check_terminal_target", "连通性体检：能不能登上某台主机（真的连一次）", "POST", "/api/terminal/targets/{host_id}/check", "host_id:int!:主机 ID", permission=EXEC, category="执行", sensitive=True),
    T("run_command", "在某台授权的机器上执行一条 shell 命令并取回输出（受命令策略管控、全程审计；需管理员密码确认）", "POST", "/api/terminal/exec", "hostId:int!:主机 ID;command:str!:要执行的命令;accountId:int:用哪个账号登录（留空=自动选）;timeout:int:超时秒数，默认 60;reason:str:为什么执行这条命令（会写进审计）", permission=EXEC, category="执行", sensitive=True),
    # ---------------- 文件管理器（SFTP）----------------
    T("list_file_sessions", "当前打开的文件管理器（SFTP）会话", "GET", "/api/files/sessions", category="文件", card="文件会话"),
    T("open_file_session", "打开一个 SFTP 文件会话，拿到 sid 后才能浏览/上传/下载文件", "POST", "/api/files/sessions", "hostId:int!:主机 ID;accountId:int:账号 ID", permission=EXEC, category="文件"),
    T("close_file_session", "关闭一个 SFTP 文件会话", "DELETE", "/api/files/sessions/{sid}", "sid:str!:文件会话 ID", permission=EXEC, category="文件"),
    T("list_remote_dir", "列出目标机某个目录下的文件（名称、大小、权限、属主、修改时间）", "GET", "/api/files/sessions/{sid}/list", "sid:str!:文件会话 ID;path:str:目录路径，默认 /", query=("path",), permission=EXEC, category="文件", card="远端目录"),
    T("read_remote_file", "读取目标机上某个文本文件的内容", "GET", "/api/files/sessions/{sid}/read", "sid:str!:文件会话 ID;path:str!:文件路径", query=("path",), permission=EXEC, category="文件"),
    T("stat_remote_path", "查看目标机上某个文件/目录的属性", "GET", "/api/files/sessions/{sid}/stat", "sid:str!:文件会话 ID;path:str!:路径", query=("path",), permission=EXEC, category="文件"),
    T("check_file_operation", "判断某个文件操作在当前授权/文件策略下是否允许（动手前先问一句）", "POST", "/api/files/sessions/{sid}/check", "sid:str!:文件会话 ID;operation:str!:操作名;path:str:路径;targetPath:str:目标路径", permission=EXEC, category="文件"),
    T("mkdir_remote", "在目标机上新建目录（需管理员密码确认）", "POST", "/api/files/sessions/{sid}/mkdir", "sid:str!:文件会话 ID;path:str!:目录路径;mode:str:权限，如 755;parents:bool:是否递归创建", permission=EXEC, category="文件", sensitive=True),
    T("rename_remote", "重命名目标机上的文件（需管理员密码确认）", "POST", "/api/files/sessions/{sid}/rename", "sid:str!:文件会话 ID;path:str!:原路径;newName:str!:新名称;targetPath:str:新路径（可选，跨目录）", permission=EXEC, category="文件", sensitive=True),
    T("copy_remote", "在目标机上复制文件或目录（需管理员密码确认）", "POST", "/api/files/sessions/{sid}/copy", "sid:str!:文件会话 ID;path:str!:源路径;targetPath:str!:目标路径;recursive:bool:目录是否递归", permission=EXEC, category="文件", sensitive=True),
    T("delete_remote", "删除目标机上的文件或目录（需管理员密码确认）", "POST", "/api/files/sessions/{sid}/delete", "sid:str!:文件会话 ID;paths:list!:要删除的路径列表;recursive:bool:目录是否递归", permission=EXEC, category="文件", sensitive=True),
    T("chmod_remote", "修改目标机上文件/目录的权限位（需管理员密码确认）", "POST", "/api/files/sessions/{sid}/chmod", "sid:str!:文件会话 ID;path:str!:路径;mode:str!:权限，如 600", permission=EXEC, category="文件", sensitive=True),
    # ---------------- 系统设置 ----------------
    T("list_settings", "查看系统设置（站点名、网关端口、超时、登录锁定策略、默认策略等）", "GET", "/api/settings", category="系统"),
    T("update_settings", "修改系统设置（values 里放要改的键值对，例如 {\"session_idle_timeout\": 600}，需管理员密码确认）", "PUT", "/api/settings", "values:dict!:要修改的键值对", permission=WRITE, category="系统", sensitive=True),
    T("reset_settings", "把系统设置恢复默认（需管理员密码确认）", "POST", "/api/settings/reset", "keys:list:只重置这些键（留空=全部）", permission=WRITE, category="系统", sensitive=True),
    T("reconcile_sessions", "清理残留的在线会话记录（需管理员密码确认）", "POST", "/api/settings/maintenance/reconcile", permission=WRITE, category="系统", sensitive=True),
    T("reseed_builtin_data", "重建内置数据（角色、内置策略等，需管理员密码确认）", "POST", "/api/settings/maintenance/seed", permission=WRITE, category="系统", sensitive=True),
    # ---------------- AI 自身 ----------------
    T("list_ai_tools", "查看我自己能调用的全部工具清单（含权限档位）", "GET", "/api/ai/tools", category="AI"),
    T("list_ai_conversations", "查看历史 AI 运维对话", "GET", "/api/ai/conversations", "keyword:str:标题关键字;mine:str:true=只看自己的;page:int;pageSize:int", category="AI", card="AI 对话"),
    T("get_ai_conversation", "读取某次 AI 对话的完整消息（我说了什么、AI 回了什么、调用了什么工具）", "GET", "/api/ai/conversations/{conversation_id}", "conversation_id:int!:对话 ID", category="AI"),
    T("list_ai_tool_calls", "查询 AI 工具调用审计（工具名、参数、结果、是否敏感、谁批准的）", "GET", "/api/ai/tool-calls", "keyword:str;toolName:str;status:str;username:str;page:int;pageSize:int", category="AI", card="AI 工具调用"),
]

TOOLS: tuple[Tool, ...] = tuple(_TOOLS)
TOOL_INDEX: dict[str, Tool] = {tool.name: tool for tool in TOOLS}


def tool_names() -> list[str]:
    return [tool.name for tool in TOOLS]


def tools_for_permission(permissions: Iterable[str], *, is_admin: bool = False) -> list[Tool]:
    """按调用者权限过滤可用工具（管理员拿全部）。"""
    granted = set(permissions or ())
    if is_admin:
        return list(TOOLS)
    return [tool for tool in TOOLS if tool.permission in granted]


def openai_schemas(tools: Iterable[Tool]) -> list[dict]:
    return [tool.schema() for tool in tools]


def grouped_catalog(tools: Iterable[Tool]) -> list[dict]:
    """给前端/模型看的分类目录。"""
    order: list[str] = []
    buckets: dict[str, list[dict]] = {}
    for tool in tools:
        if tool.category not in buckets:
            buckets[tool.category] = []
            order.append(tool.category)
        buckets[tool.category].append(tool.to_dict())
    return [
        {"category": name, "count": len(buckets[name]), "tools": buckets[name]} for name in order
    ]


# --------------------------------------------------------------------------
# 执行：把工具调用翻译成对自己 REST API 的一次请求
# --------------------------------------------------------------------------
def build_request(tool: Tool, args: dict[str, Any]) -> tuple[str, dict, dict]:
    """返回 ``(path, query, body)``；路径参数就地替换，``dict`` 参数展开进 body。"""
    path = tool.path
    query: dict[str, Any] = {}
    body: dict[str, Any] = {}
    for arg in tool.args:
        if arg.name not in args:
            continue
        value = args[arg.name]
        if value is None:
            continue
        placeholder = "{" + arg.name + "}"
        if placeholder in path:
            path = path.replace(placeholder, quote(str(value), safe=""))
        elif arg.spread and isinstance(value, dict):
            body.update(value)
        elif tool.method == "GET" or arg.name in tool.query_args:
            query[arg.name] = value
        else:
            body[arg.name] = value
    return path, query, body


def invoke(app, tool: Tool, args: dict[str, Any], token: str, *, timeout: int = 120) -> dict:
    """用调用者的 JWT 打自己的接口，返回统一结构。

    ``{"ok": bool, "status": int, "data": Any, "message": str, "path": str, "method": str}``
    """
    path, query, body = build_request(tool, args or {})
    url = path + ("?" + urlencode(query, doseq=True) if query else "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    client = app.test_client()
    try:
        response = client.open(
            url,
            method=tool.method,
            json=body if body else None,
            headers=headers,
            buffered=True,
        )
    except Exception as exc:  # noqa: BLE001 - 工具层不应把异常抛给模型
        return {
            "ok": False,
            "status": 0,
            "data": None,
            "message": f"调用失败：{exc}",
            "path": url,
            "method": tool.method,
        }
    payload: Any = None
    raw = response.get_data(as_text=True)
    try:
        payload = json.loads(raw) if raw else None
    except ValueError:
        payload = raw[:2000]
    ok = 200 <= response.status_code < 300
    if isinstance(payload, dict) and payload.get("success") is False:
        ok = False
    message = ""
    if isinstance(payload, dict):
        message = str(payload.get("message") or payload.get("code") or "")
    if not message:
        # 人看的文案一律中文直白；HTTP 码属于系统字段，保留英文数字即可。
        message = "成功" if ok else f"服务端返回 HTTP {response.status_code}"
    return {
        "ok": ok,
        "status": response.status_code,
        "data": payload,
        "message": message,
        "path": url,
        "method": tool.method,
    }


def _human_message(message: str, ok: bool, status: int | None = None) -> str:
    """把机器词（``ok``/``OK``/``success``/``true``/空）翻成人话。

    **给人看的字段只用中文直白描写，给系统看的字段继续用英文** —— 权限码、``code``、
    ``status``、HTTP 码、工具名都保持原样，两者不混进同一句人看的文案里。这里的摘要会
    同时出现在：AI 对话审计的「AI 调用工具 …」事件、`ai_tool_calls.result_summary`、
    AI 页/SSH 网关的工具结果行。漏在这里，审计事件里就会冒出「AI 调用工具 X：ok」。
    """
    text = (message or "").strip()
    if text and text.lower() not in {"ok", "success", "true"}:
        return text
    if ok:
        return "成功"
    return f"服务端返回 HTTP {status}" if status else "调用未成功"


def summarize(tool: Tool, result: dict, *, limit: int = 8000) -> tuple[str, str, dict | None]:
    """把工具结果压成 ``(一句话摘要, 给模型看的文本, 可选的卡片)``。"""
    data = result.get("data")
    payload = data.get("data") if isinstance(data, dict) and "data" in data else data
    summary = _human_message(
        str(result.get("message") or ""), bool(result.get("ok")), result.get("status")
    )
    card = None

    if isinstance(data, dict):
        total = data.get("total")
        if total is not None:
            summary = f"{summary}（共 {total} 条）"

    text = json.dumps(payload, ensure_ascii=False, default=str, indent=2) if payload is not None else ""
    if not result.get("ok"):
        summary = f"失败：{summary}"
        # 失败时把原因并进给模型看的文本里：文本是模型唯一的"眼睛"，
        # 只放在摘要里的话，模型会拿到一段空内容然后开始编。
        text = f"{summary}\n{text}".strip()
    if len(text) > limit:
        text = text[:limit] + f"\n…（结果过长，已截断，共 {len(text)} 字符）"

    if tool.card and isinstance(payload, list) and payload:
        card = _table_card(tool.card, payload)
    return summary, text, card


def _table_card(title: str, rows: list[Any], *, max_rows: int = 20) -> dict | None:
    """从列表结果里自动生成一张表格卡片（前端渲染成 antd Table，终端渲染成文本表）。"""
    dict_rows = [row for row in rows if isinstance(row, dict)]
    if not dict_rows:
        return None
    columns = []
    for key in dict_rows[0].keys():
        columns.append({"key": key, "title": key})
    cleaned = []
    for row in dict_rows[:max_rows]:
        cleaned.append({k: _cell(v) for k, v in row.items()})
    return {
        "type": "table",
        "title": title,
        "columns": columns,
        "rows": cleaned,
        "truncated": len(dict_rows) > max_rows,
        "total": len(dict_rows),
    }


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (int, float, str)):
        text = str(value)
    elif isinstance(value, list):
        text = ", ".join(_cell(item) for item in value[:5])
    elif isinstance(value, dict):
        text = json.dumps(value, ensure_ascii=False, default=str)[:120]
    else:
        text = str(value)
    return text[:200]
