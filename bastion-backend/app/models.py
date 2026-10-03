"""数据库模型。

五个域：
    身份域 —— Role / User
    资产域 —— HostGroup / Host / HostAccount
    授权域 —— Grant
    策略域 —— CommandPolicy / CommandRule
    审计域 —— SessionRecord / CommandLog / AuditLog
    系统域 —— SystemSetting
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import datetime, timezone

from .extensions import db


def utcnow() -> datetime:
    """统一的 UTC now（**naive**，与 ``db.DateTime`` 列的存储形态一致）。

    为什么一定要 naive：SQLAlchemy 的 ``DateTime`` 未开 ``timezone=True``，
    写进去的 tzinfo 会被 SQLite 丢掉，读出来是 naive 的；
    如果这里返回 aware，那么任何「读出来的时间 - utcnow()」都会抛
    ``TypeError: can't subtract offset-naive and offset-aware datetimes``。
    别改回 aware —— 需要 aware 时用 ``to_aware_utc()``。
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_naive_utc(value: datetime | None) -> datetime | None:
    """把任意 datetime 归一为 naive UTC（可以安全地和 :func:`utcnow` 相减）。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def to_aware_utc(value: datetime | None) -> datetime | None:
    """把任意 datetime 归一为带 UTC tzinfo 的形态（序列化给外部用）。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def gen_sid() -> str:
    return uuid.uuid4().hex


# --------------------------------------------------------------------------
# 审计链式哈希
# --------------------------------------------------------------------------

CHAIN_SEP = "\x01"
CHAIN_KV = "\x02"


def chain_key(secret_key: str) -> bytes:
    """派生链式哈希专属 HMAC 密钥（基于应用 SECRET_KEY，HKDF-lite）。"""
    base = (secret_key or "").encode("utf-8") or b"bastion-audit-chain"
    return hashlib.sha256(b"bastion-audit-chain-v1" + base).digest()


def _norm(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value.isoformat(timespec="microseconds")
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.hex()
    if isinstance(value, (dict, list, tuple)):
        try:
            return json.dumps(
                value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        except Exception:
            return str(value)
    return str(value)


def _serialize_fields(fields: list[tuple[str, object]]) -> str:
    return CHAIN_SEP.join(name + CHAIN_KV + _norm(value) for name, value in fields)


def _hmac_hex(key: bytes, message: str) -> str:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).hexdigest()


def compute_entry_hash(
    *,
    table: str,
    fields: list[tuple[str, object]],
    prev_hash: str,
    secret_key: str,
) -> str:
    """计算单条记录的 entry_hash。"""
    msg = "table" + CHAIN_KV + table + CHAIN_SEP + "prev" + CHAIN_KV + (prev_hash or "") + CHAIN_SEP + _serialize_fields(fields)
    return _hmac_hex(chain_key(secret_key), msg)


class TimestampMixin:
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)


# --------------------------------------------------------------------------
# 身份域
# --------------------------------------------------------------------------

class Role(TimestampMixin, db.Model):
    """控制台角色。``permissions`` 中的 ``*`` 表示全部权限。"""

    __tablename__ = "roles"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(32), unique=True, nullable=False, index=True)
    name = db.Column(db.String(64), nullable=False)
    description = db.Column(db.String(255), default="")
    permissions = db.Column(db.JSON, default=list, nullable=False)
    is_builtin = db.Column(db.Boolean, default=False, nullable=False)

    users = db.relationship("User", back_populates="role")

    def permission_set(self) -> set[str]:
        return set(self.permissions or [])

    def to_dict(self, with_users: bool = False) -> dict:
        data = {
            "id": self.id,
            "code": self.code,
            "name": self.name,
            "description": self.description,
            "permissions": list(self.permissions or []),
            "isBuiltin": self.is_builtin,
            "createdAt": _iso(self.created_at),
            "updatedAt": _iso(self.updated_at),
        }
        if with_users:
            data["userCount"] = len(self.users)
        return data


class User(TimestampMixin, db.Model):
    """堡垒机账号（控制台 + SSH 网关共用同一套凭据）。"""

    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    display_name = db.Column(db.String(64), default="")
    email = db.Column(db.String(128), default="")
    phone = db.Column(db.String(32), default="")
    remark = db.Column(db.String(255), default="")

    role_id = db.Column(db.Integer, db.ForeignKey("roles.id"), nullable=False)
    #: active / disabled
    status = db.Column(db.String(16), default="active", nullable=False)
    is_superuser = db.Column(db.Boolean, default=False, nullable=False)
    #: 是否允许登录 SSH 审计网关
    gateway_enabled = db.Column(db.Boolean, default=True, nullable=False)
    #: 是否允许使用 Web 终端
    webterm_enabled = db.Column(db.Boolean, default=True, nullable=False)
    must_change_password = db.Column(db.Boolean, default=False, nullable=False)

    last_login_at = db.Column(db.DateTime)
    last_login_ip = db.Column(db.String(64), default="")
    failed_attempts = db.Column(db.Integer, default=0, nullable=False)
    locked_until = db.Column(db.DateTime)

    public_key = db.Column(db.Text, default="")  # 网关公钥登录（可选）

    role = db.relationship("Role", back_populates="users")
    grants = db.relationship(
        "Grant", back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )

    # -- 便捷方法 ---------------------------------------------------------
    @property
    def role_code(self) -> str:
        return self.role.code if self.role else ""

    @property
    def role_name(self) -> str:
        return self.role.name if self.role else ""

    def permission_set(self) -> set[str]:
        perms = self.role.permission_set() if self.role else set()
        if self.is_superuser:
            perms.add("*")
        return perms

    def is_active(self) -> bool:
        return self.status == "active"

    def is_locked(self) -> bool:
        locked_until = to_naive_utc(self.locked_until)
        if locked_until is None:
            return False
        return locked_until > utcnow()

    def to_dict(self, with_grants: bool = False) -> dict:
        data = {
            "id": self.id,
            "username": self.username,
            "displayName": self.display_name or self.username,
            "email": self.email,
            "phone": self.phone,
            "remark": self.remark,
            "status": self.status,
            "roleId": self.role_id,
            "roleCode": self.role_code,
            "roleName": self.role_name,
            "isSuperuser": self.is_superuser,
            "gatewayEnabled": self.gateway_enabled,
            "webtermEnabled": self.webterm_enabled,
            "mustChangePassword": self.must_change_password,
            "lastLoginAt": _iso(self.last_login_at),
            "lastLoginIp": self.last_login_ip,
            "lockedUntil": _iso(self.locked_until),
            "permissions": sorted(self.permission_set()),
            "createdAt": _iso(self.created_at),
            "updatedAt": _iso(self.updated_at),
        }
        if with_grants:
            data["grantCount"] = len(self.grants)
        return data


# --------------------------------------------------------------------------
# 资产域
# --------------------------------------------------------------------------

class HostGroup(TimestampMixin, db.Model):
    __tablename__ = "host_groups"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), unique=True, nullable=False)
    description = db.Column(db.String(255), default="")

    hosts = db.relationship("Host", back_populates="group")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "hostCount": len(self.hosts),
            "createdAt": _iso(self.created_at),
        }


class Host(TimestampMixin, db.Model):
    """被管 Linux 主机。``name`` 即网关里的可选别名。"""

    __tablename__ = "hosts"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), unique=True, nullable=False, index=True)
    address = db.Column(db.String(128), nullable=False)
    port = db.Column(db.Integer, default=22, nullable=False)
    #: ssh / telnet 预留
    protocol = db.Column(db.String(16), default="ssh", nullable=False)
    os_type = db.Column(db.String(32), default="linux", nullable=False)
    group_id = db.Column(db.Integer, db.ForeignKey("host_groups.id"))
    tags = db.Column(db.JSON, default=list)
    description = db.Column(db.String(255), default="")
    #: active / disabled
    status = db.Column(db.String(16), default="active", nullable=False)
    created_by = db.Column(db.String(64), default="")

    group = db.relationship("HostGroup", back_populates="hosts")
    accounts = db.relationship(
        "HostAccount", back_populates="host", cascade="all, delete-orphan", lazy="selectin"
    )
    grants = db.relationship("Grant", back_populates="host", cascade="all, delete-orphan")

    def to_dict(self, with_accounts: bool = False) -> dict:
        data = {
            "id": self.id,
            "name": self.name,
            "address": self.address,
            "port": self.port,
            "protocol": self.protocol,
            "osType": self.os_type,
            "groupId": self.group_id,
            "groupName": self.group.name if self.group else "",
            "tags": list(self.tags or []),
            "description": self.description,
            "status": self.status,
            "createdBy": self.created_by,
            "accountCount": len(self.accounts),
            "grantCount": len(self.grants),
            "createdAt": _iso(self.created_at),
            "updatedAt": _iso(self.updated_at),
        }
        if with_accounts:
            data["accounts"] = [a.to_dict() for a in self.accounts]
        return data


class HostAccount(TimestampMixin, db.Model):
    """资产账号：堡垒机登录目标主机所用的凭据（口令密文存储）。"""

    __tablename__ = "host_accounts"
    __table_args__ = (
        db.UniqueConstraint("host_id", "name", name="uq_host_account_name"),
    )

    id = db.Column(db.Integer, primary_key=True)
    host_id = db.Column(db.Integer, db.ForeignKey("hosts.id"), nullable=False, index=True)
    name = db.Column(db.String(64), nullable=False)
    username = db.Column(db.String(64), nullable=False)
    #: password / key
    auth_type = db.Column(db.String(16), default="password", nullable=False)
    secret_enc = db.Column(db.Text, default="")
    private_key_enc = db.Column(db.Text, default="")
    passphrase_enc = db.Column(db.Text, default="")
    #: 登录后自动执行的提权命令，如 ``sudo -i``
    sudo_command = db.Column(db.String(128), default="")
    description = db.Column(db.String(255), default="")

    host = db.relationship("Host", back_populates="accounts")

    def to_dict(self, with_secret: bool = False) -> dict:
        data = {
            "id": self.id,
            "hostId": self.host_id,
            "hostName": self.host.name if self.host else "",
            "name": self.name,
            "username": self.username,
            "authType": self.auth_type,
            "hasSecret": bool(self.secret_enc),
            "hasPrivateKey": bool(self.private_key_enc),
            "hasPassphrase": bool(self.passphrase_enc),
            "sudoCommand": self.sudo_command,
            "description": self.description,
            "createdAt": _iso(self.created_at),
            "updatedAt": _iso(self.updated_at),
        }
        if with_secret:
            from .crypto import decrypt

            data["secret"] = decrypt(self.secret_enc) if self.secret_enc else ""
            data["privateKey"] = decrypt(self.private_key_enc) if self.private_key_enc else ""
            data["passphrase"] = decrypt(self.passphrase_enc) if self.passphrase_enc else ""
        return data


# --------------------------------------------------------------------------
# 授权域
# --------------------------------------------------------------------------

class Grant(TimestampMixin, db.Model):
    """用户 ->（主机, 资产账号）的访问授权，并绑定命令策略。"""

    __tablename__ = "grants"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    host_id = db.Column(db.Integer, db.ForeignKey("hosts.id"), nullable=False, index=True)
    host_account_id = db.Column(db.Integer, db.ForeignKey("host_accounts.id"))
    policy_id = db.Column(db.Integer, db.ForeignKey("command_policies.id"))

    file_policy_id = db.Column(db.Integer, db.ForeignKey("file_policies.id"))

    can_login = db.Column(db.Boolean, default=True, nullable=False)
    can_sftp = db.Column(db.Boolean, default=False, nullable=False)
    can_upload = db.Column(db.Boolean, default=False, nullable=False)
    can_download = db.Column(db.Boolean, default=False, nullable=False)
    #: 文件管理器的「修改类」操作：新建 / 重命名 / 移动 / 复制 / 删除 / 改权限 / 编辑保存
    can_file_write = db.Column(db.Boolean, default=False, nullable=False)
    can_port_forward = db.Column(db.Boolean, default=False, nullable=False)
    can_webterm = db.Column(db.Boolean, default=True, nullable=False)

    time_start = db.Column(db.Time)
    time_end = db.Column(db.Time)
    #: 允许的星期，0=周一 … 6=周日；空表示每天
    weekdays = db.Column(db.JSON, default=list)
    expire_at = db.Column(db.DateTime)
    max_sessions = db.Column(db.Integer, default=3, nullable=False)
    enabled = db.Column(db.Boolean, default=True, nullable=False)
    remark = db.Column(db.String(255), default="")
    created_by = db.Column(db.String(64), default="")

    user = db.relationship("User", back_populates="grants")
    host = db.relationship("Host", back_populates="grants")
    account = db.relationship("HostAccount")
    policy = db.relationship("CommandPolicy")
    file_policy = db.relationship("FilePolicy")

    @property
    def host_account_username(self) -> str:
        return self.account.username if self.account else "*"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "userId": self.user_id,
            "username": self.user.username if self.user else "",
            "displayName": (self.user.display_name or self.user.username) if self.user else "",
            "hostId": self.host_id,
            "hostName": self.host.name if self.host else "",
            "hostAddress": self.host.address if self.host else "",
            "hostAccountId": self.host_account_id,
            "accountName": self.account.name if self.account else "*",
            "accountUsername": self.host_account_username,
            "policyId": self.policy_id,
            "policyName": self.policy.name if self.policy else "",
            "filePolicyId": self.file_policy_id,
            "filePolicyName": self.file_policy.name if self.file_policy else "",
            "canLogin": self.can_login,
            "canSftp": self.can_sftp,
            "canUpload": self.can_upload,
            "canDownload": self.can_download,
            "canFileWrite": self.can_file_write,
            "canPortForward": self.can_port_forward,
            "canWebterm": self.can_webterm,
            "timeStart": self.time_start.strftime("%H:%M") if self.time_start else None,
            "timeEnd": self.time_end.strftime("%H:%M") if self.time_end else None,
            "weekdays": list(self.weekdays or []),
            "expireAt": _iso(self.expire_at),
            "maxSessions": self.max_sessions,
            "enabled": self.enabled,
            "remark": self.remark,
            "createdBy": self.created_by,
            "createdAt": _iso(self.created_at),
            "updatedAt": _iso(self.updated_at),
        }


# --------------------------------------------------------------------------
# 策略域
# --------------------------------------------------------------------------

class CommandPolicy(TimestampMixin, db.Model):
    """命令策略集。default_action 决定未命中任何规则时的行为。"""

    __tablename__ = "command_policies"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), unique=True, nullable=False)
    description = db.Column(db.String(255), default="")
    #: deny / allow
    default_action = db.Column(db.String(8), default="allow", nullable=False)
    is_default = db.Column(db.Boolean, default=False, nullable=False)

    rules = db.relationship(
        "CommandRule",
        back_populates="policy",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="CommandRule.priority",
    )

    def to_dict(self, with_rules: bool = True) -> dict:
        data = {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "defaultAction": self.default_action,
            "isDefault": self.is_default,
            "ruleCount": len(self.rules),
            "createdAt": _iso(self.created_at),
            "updatedAt": _iso(self.updated_at),
        }
        if with_rules:
            data["rules"] = [r.to_dict() for r in self.rules]
        return data


class CommandRule(db.Model):
    """单条命令规则。priority 越小越先匹配，首个命中即生效。"""

    __tablename__ = "command_rules"

    id = db.Column(db.Integer, primary_key=True)
    policy_id = db.Column(
        db.Integer, db.ForeignKey("command_policies.id"), nullable=False, index=True
    )
    priority = db.Column(db.Integer, default=100, nullable=False)
    #: allow / deny
    action = db.Column(db.String(8), default="deny", nullable=False)
    #: regex / prefix / exact / contains
    match_type = db.Column(db.String(16), default="regex", nullable=False)
    pattern = db.Column(db.Text, nullable=False)
    #: low / medium / high / critical
    risk_level = db.Column(db.String(16), default="medium", nullable=False)
    description = db.Column(db.String(255), default="")
    enabled = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    policy = db.relationship("CommandPolicy", back_populates="rules")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "policyId": self.policy_id,
            "priority": self.priority,
            "action": self.action,
            "matchType": self.match_type,
            "pattern": self.pattern,
            "riskLevel": self.risk_level,
            "description": self.description,
            "enabled": self.enabled,
            "createdAt": _iso(self.created_at),
        }


class FilePolicy(TimestampMixin, db.Model):
    """文件操作策略集（SFTP 文件管理器的细粒度访问控制）。

    与命令策略同构：`default_action` 决定未命中任何规则时的行为，规则按 priority
    从小到大逐条匹配，首个命中即生效。
    """

    __tablename__ = "file_policies"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), unique=True, nullable=False)
    description = db.Column(db.String(255), default="")
    #: deny / allow
    default_action = db.Column(db.String(8), default="allow", nullable=False)
    is_default = db.Column(db.Boolean, default=False, nullable=False)

    rules = db.relationship(
        "FileRule",
        back_populates="policy",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="FileRule.priority",
    )

    def to_dict(self, with_rules: bool = True) -> dict:
        data = {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "defaultAction": self.default_action,
            "isDefault": self.is_default,
            "ruleCount": len(self.rules),
            "createdAt": _iso(self.created_at),
            "updatedAt": _iso(self.updated_at),
        }
        if with_rules:
            data["rules"] = [r.to_dict() for r in self.rules]
        return data


class FileRule(db.Model):
    """单条文件操作规则：operation + 路径模式 → allow / deny。"""

    __tablename__ = "file_rules"

    id = db.Column(db.Integer, primary_key=True)
    policy_id = db.Column(
        db.Integer, db.ForeignKey("file_policies.id"), nullable=False, index=True
    )
    priority = db.Column(db.Integer, default=100, nullable=False)
    #: allow / deny
    action = db.Column(db.String(8), default="deny", nullable=False)
    #: * 或 list/read/download/upload/write/mkdir/rename/move/copy/delete/chmod/archive
    operation = db.Column(db.String(16), default="*", nullable=False)
    #: glob / regex / prefix / contains
    match_type = db.Column(db.String(16), default="glob", nullable=False)
    path_pattern = db.Column(db.Text, nullable=False)
    #: low / medium / high / critical
    risk_level = db.Column(db.String(16), default="medium", nullable=False)
    description = db.Column(db.String(255), default="")
    enabled = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    policy = db.relationship("FilePolicy", back_populates="rules")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "policyId": self.policy_id,
            "priority": self.priority,
            "action": self.action,
            "operation": self.operation,
            "matchType": self.match_type,
            "pathPattern": self.path_pattern,
            "riskLevel": self.risk_level,
            "description": self.description,
            "enabled": self.enabled,
            "createdAt": _iso(self.created_at),
        }


# --------------------------------------------------------------------------
# 审计域
# --------------------------------------------------------------------------

class SessionRecord(db.Model):
    """一次 SSH / Web 终端会话。"""

    __tablename__ = "sessions"
    __table_args__ = (db.Index("ix_sessions_started_at", "started_at"),)

    id = db.Column(db.Integer, primary_key=True)
    sid = db.Column(db.String(64), unique=True, nullable=False, default=gen_sid, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    username = db.Column(db.String(64), index=True)
    role_code = db.Column(db.String(32), default="")

    host_id = db.Column(db.Integer, db.ForeignKey("hosts.id"))
    host_name = db.Column(db.String(64))
    host_address = db.Column(db.String(128))
    account_id = db.Column(db.Integer, db.ForeignKey("host_accounts.id"))
    account_username = db.Column(db.String(64))
    grant_id = db.Column(db.Integer, db.ForeignKey("grants.id"))

    #: gateway / web
    source = db.Column(db.String(16), default="gateway", nullable=False)
    protocol = db.Column(db.String(16), default="ssh", nullable=False)
    client_ip = db.Column(db.String(64), default="")
    client_port = db.Column(db.Integer, default=0)

    #: active / closed / failed / terminated / denied
    status = db.Column(db.String(16), default="active", nullable=False, index=True)
    end_reason = db.Column(db.String(128), default="")

    started_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    ended_at = db.Column(db.DateTime)
    duration_seconds = db.Column(db.Integer, default=0)
    idle_seconds = db.Column(db.Integer, default=0)

    command_count = db.Column(db.Integer, default=0, nullable=False)
    denied_count = db.Column(db.Integer, default=0, nullable=False)
    bytes_in = db.Column(db.BigInteger, default=0, nullable=False)
    bytes_out = db.Column(db.BigInteger, default=0, nullable=False)
    max_risk_level = db.Column(db.String(16), default="low", nullable=False)
    transcript_path = db.Column(db.String(255), default="")

    user = db.relationship("User")
    host = db.relationship("Host")

    def to_dict(self, detail: bool = False) -> dict:
        data = {
            "id": self.id,
            "sid": self.sid,
            "userId": self.user_id,
            "username": self.username,
            "roleCode": self.role_code,
            "hostId": self.host_id,
            "hostName": self.host_name,
            "hostAddress": self.host_address,
            "accountId": self.account_id,
            "accountUsername": self.account_username,
            "grantId": self.grant_id,
            "source": self.source,
            "protocol": self.protocol,
            "clientIp": self.client_ip,
            "clientPort": self.client_port,
            "status": self.status,
            "endReason": self.end_reason,
            "startedAt": _iso(self.started_at),
            "endedAt": _iso(self.ended_at),
            "durationSeconds": self.duration_seconds or 0,
            "commandCount": self.command_count or 0,
            "deniedCount": self.denied_count or 0,
            "bytesIn": self.bytes_in or 0,
            "bytesOut": self.bytes_out or 0,
            "riskLevel": self.max_risk_level or "low",
        }
        if detail:
            data["hasTranscript"] = bool(self.transcript_path)
        return data


class CommandLog(db.Model):
    """命令级审计：谁、在哪台机器、执行了什么、结果是什么。"""

    __tablename__ = "command_logs"
    __table_args__ = (
        db.Index("ix_command_logs_started_at", "started_at"),
        db.Index("ix_command_logs_user", "user_id"),
        db.Index("ix_command_logs_session", "session_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey("sessions.id"))
    sid = db.Column(db.String(64), index=True)
    seq = db.Column(db.Integer, default=0, nullable=False)

    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    username = db.Column(db.String(64))
    host_id = db.Column(db.Integer, db.ForeignKey("hosts.id"))
    host_name = db.Column(db.String(64))

    command = db.Column(db.Text, default="")
    output = db.Column(db.Text, default="")
    #: allow / deny
    action = db.Column(db.String(8), default="allow", nullable=False, index=True)
    risk_level = db.Column(db.String(16), default="low", nullable=False)
    matched_rule_id = db.Column(db.Integer, db.ForeignKey("command_rules.id"))
    matched_rule_pattern = db.Column(db.Text, default="")
    reason = db.Column(db.String(255), default="")

    started_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    ended_at = db.Column(db.DateTime)
    duration_ms = db.Column(db.Integer, default=0)
    exit_status = db.Column(db.Integer)
    truncated = db.Column(db.Boolean, default=False, nullable=False)

    prev_hash = db.Column(db.String(64), default="")
    entry_hash = db.Column(db.String(64), default="", index=True)

    def chain_fields(self) -> list[tuple[str, object]]:
        return [
            ("id", self.id),
            ("session_id", self.session_id),
            ("sid", self.sid),
            ("seq", self.seq),
            ("user_id", self.user_id),
            ("username", self.username),
            ("host_id", self.host_id),
            ("host_name", self.host_name),
            ("command", self.command),
            ("output_hash", hashlib.sha256((self.output or "").encode("utf-8")).hexdigest()),
            ("output_len", len(self.output or "")),
            ("action", self.action),
            ("risk_level", self.risk_level),
            ("matched_rule_id", self.matched_rule_id),
            ("matched_rule_pattern", self.matched_rule_pattern),
            ("reason", self.reason),
            ("started_at", self.started_at),
            ("ended_at", self.ended_at),
            ("duration_ms", self.duration_ms),
            ("exit_status", self.exit_status),
            ("truncated", self.truncated),
        ]

    def to_dict(self, with_output: bool = True, output_limit: int = 4000) -> dict:
        output = self.output or ""
        truncated = self.truncated
        if with_output and output_limit and len(output) > output_limit:
            output = output[:output_limit] + "\n... (输出已截断，完整内容见会话回放)"
            truncated = True
        data = {
            "id": self.id,
            "sessionId": self.session_id,
            "sid": self.sid,
            "seq": self.seq,
            "userId": self.user_id,
            "username": self.username,
            "hostId": self.host_id,
            "hostName": self.host_name,
            "command": self.command,
            "action": self.action,
            "riskLevel": self.risk_level,
            "matchedRuleId": self.matched_rule_id,
            "matchedRulePattern": self.matched_rule_pattern,
            "reason": self.reason,
            "startedAt": _iso(self.started_at),
            "endedAt": _iso(self.ended_at),
            "durationMs": self.duration_ms or 0,
            "exitStatus": self.exit_status,
            "truncated": truncated,
            "prevHash": self.prev_hash or "",
            "entryHash": self.entry_hash or "",
        }
        if with_output:
            data["output"] = output
        return data


class FileLog(db.Model):
    """文件操作级审计：谁、在哪台机器、对哪个路径做了什么、结果是什么。

    每一次文件管理器操作都会落一条记录（放行的与拦截的都落），deny 的还会在
    `AuditLog` 里再留一条 `file` 类别的安全事件。
    """

    __tablename__ = "file_logs"
    __table_args__ = (
        db.Index("ix_file_logs_started_at", "started_at"),
        db.Index("ix_file_logs_user", "user_id"),
        db.Index("ix_file_logs_session", "session_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey("sessions.id"))
    sid = db.Column(db.String(64), index=True)
    seq = db.Column(db.Integer, default=0, nullable=False)

    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    username = db.Column(db.String(64))
    host_id = db.Column(db.Integer, db.ForeignKey("hosts.id"))
    host_name = db.Column(db.String(64))

    #: list/read/download/upload/write/mkdir/rename/move/copy/delete/chmod/archive
    operation = db.Column(db.String(16), default="", nullable=False, index=True)
    path = db.Column(db.Text, default="")
    target_path = db.Column(db.Text, default="")
    #: allow / deny
    action = db.Column(db.String(8), default="allow", nullable=False, index=True)
    risk_level = db.Column(db.String(16), default="low", nullable=False)
    matched_rule_id = db.Column(db.Integer, db.ForeignKey("file_rules.id"))
    matched_rule_pattern = db.Column(db.Text, default="")
    reason = db.Column(db.String(255), default="")
    #: success / failure（放行但执行失败也要落，别把失败伪装成成功）
    result = db.Column(db.String(16), default="success", nullable=False)
    message = db.Column(db.String(512), default="")
    size = db.Column(db.BigInteger, default=0)
    file_count = db.Column(db.Integer, default=1)

    started_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    ended_at = db.Column(db.DateTime)
    duration_ms = db.Column(db.Integer, default=0)

    prev_hash = db.Column(db.String(64), default="")
    entry_hash = db.Column(db.String(64), default="", index=True)

    def chain_fields(self) -> list[tuple[str, object]]:
        return [
            ("id", self.id),
            ("session_id", self.session_id),
            ("sid", self.sid),
            ("seq", self.seq),
            ("user_id", self.user_id),
            ("username", self.username),
            ("host_id", self.host_id),
            ("host_name", self.host_name),
            ("operation", self.operation),
            ("path", self.path),
            ("target_path", self.target_path),
            ("action", self.action),
            ("risk_level", self.risk_level),
            ("matched_rule_id", self.matched_rule_id),
            ("matched_rule_pattern", self.matched_rule_pattern),
            ("reason", self.reason),
            ("result", self.result),
            ("message", self.message),
            ("size", self.size),
            ("file_count", self.file_count),
            ("started_at", self.started_at),
            ("ended_at", self.ended_at),
            ("duration_ms", self.duration_ms),
        ]

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "sessionId": self.session_id,
            "sid": self.sid,
            "seq": self.seq,
            "userId": self.user_id,
            "username": self.username,
            "hostId": self.host_id,
            "hostName": self.host_name,
            "operation": self.operation,
            "path": self.path,
            "targetPath": self.target_path,
            "action": self.action,
            "riskLevel": self.risk_level,
            "matchedRuleId": self.matched_rule_id,
            "matchedRulePattern": self.matched_rule_pattern,
            "reason": self.reason,
            "result": self.result,
            "message": self.message,
            "size": self.size or 0,
            "fileCount": self.file_count or 1,
            "startedAt": _iso(self.started_at),
            "endedAt": _iso(self.ended_at),
            "durationMs": self.duration_ms or 0,
            "prevHash": self.prev_hash or "",
            "entryHash": self.entry_hash or "",
        }


class AuditLog(db.Model):
    """控制台 / 身份审计流水。"""

    __tablename__ = "audit_logs"
    __table_args__ = (
        db.Index("ix_audit_logs_ts", "ts"),
        db.Index("ix_audit_logs_actor", "actor_username"),
        db.Index("ix_audit_logs_category", "category"),
    )

    id = db.Column(db.Integer, primary_key=True)
    ts = db.Column(db.DateTime, default=utcnow, nullable=False)
    #: auth / console / asset / policy / grant / session / system
    category = db.Column(db.String(16), default="console", nullable=False)
    action = db.Column(db.String(64), nullable=False)
    actor_id = db.Column(db.Integer)
    actor_username = db.Column(db.String(64), default="")
    actor_role = db.Column(db.String(32), default="")
    target_type = db.Column(db.String(32), default="")
    target_id = db.Column(db.String(64), default="")
    target_name = db.Column(db.String(128), default="")
    #: success / failure
    result = db.Column(db.String(16), default="success", nullable=False)
    message = db.Column(db.String(512), default="")
    detail = db.Column(db.JSON, default=dict)
    ip = db.Column(db.String(64), default="")
    user_agent = db.Column(db.String(255), default="")

    prev_hash = db.Column(db.String(64), default="")
    entry_hash = db.Column(db.String(64), default="", index=True)

    def chain_fields(self) -> list[tuple[str, object]]:
        return [
            ("id", self.id),
            ("ts", self.ts),
            ("category", self.category),
            ("action", self.action),
            ("actor_id", self.actor_id),
            ("actor_username", self.actor_username),
            ("actor_role", self.actor_role),
            ("target_type", self.target_type),
            ("target_id", self.target_id),
            ("target_name", self.target_name),
            ("result", self.result),
            ("message", self.message),
            ("detail", self.detail),
            ("ip", self.ip),
            ("user_agent", self.user_agent),
        ]

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "ts": _iso(self.ts),
            "category": self.category,
            "action": self.action,
            "actorId": self.actor_id,
            "actorUsername": self.actor_username,
            "actorRole": self.actor_role,
            "targetType": self.target_type,
            "targetId": self.target_id,
            "targetName": self.target_name,
            "result": self.result,
            "message": self.message,
            "detail": self.detail or {},
            "ip": self.ip,
            "userAgent": self.user_agent,
            "prevHash": self.prev_hash or "",
            "entryHash": self.entry_hash or "",
        }


class SystemSetting(db.Model):
    """键值型系统设置。"""

    __tablename__ = "system_settings"

    key = db.Column(db.String(64), primary_key=True)
    value = db.Column(db.JSON)
    description = db.Column(db.String(255), default="")
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "value": self.value,
            "description": self.description,
            "updatedAt": _iso(self.updated_at),
        }


class AiConversation(db.Model):
    """一次 AI 运维对话（一个用户 + 一个上下文，可绑定某台机器/终端会话）。"""

    __tablename__ = "ai_conversations"
    __table_args__ = (
        db.Index("ix_ai_conv_user", "user_id"),
        db.Index("ix_ai_conv_updated", "updated_at"),
    )

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(128), default="")
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    username = db.Column(db.String(64), default="")
    #: web（AI 运维页）/ ssh（网关里 /ask-ai）/ api
    source = db.Column(db.String(16), default="web", nullable=False)
    model = db.Column(db.String(64), default="")
    #: 可选上下文：绑定主机 / 堡垒机会话
    host_id = db.Column(db.Integer)
    host_name = db.Column(db.String(64), default="")
    host_address = db.Column(db.String(128), default="")
    sid = db.Column(db.String(64), default="")
    message_count = db.Column(db.Integer, default=0)
    tool_count = db.Column(db.Integer, default=0)
    token_count = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    messages = db.relationship(
        "AiMessage",
        backref="conversation",
        cascade="all, delete-orphan",
        order_by="AiMessage.id",
        lazy="select",
    )

    def to_dict(self, *, with_messages: bool = False) -> dict:
        data = {
            "id": self.id,
            "title": self.title,
            "userId": self.user_id,
            "username": self.username,
            "source": self.source,
            "model": self.model,
            "hostId": self.host_id,
            "hostName": self.host_name,
            "hostAddress": self.host_address,
            "sid": self.sid,
            "messageCount": self.message_count or 0,
            "toolCount": self.tool_count or 0,
            "tokenCount": self.token_count or 0,
            "createdAt": _iso(self.created_at),
            "updatedAt": _iso(self.updated_at),
        }
        if with_messages:
            data["messages"] = [m.to_dict() for m in self.messages]
        return data


class AiMessage(db.Model):
    """对话里的一条消息（用户提问 / AI 回复 / 工具结果），逐条入审计。"""

    __tablename__ = "ai_messages"
    __table_args__ = (
        db.Index("ix_ai_msg_conv", "conversation_id"),
        db.Index("ix_ai_msg_ts", "created_at"),
    )

    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(
        db.Integer, db.ForeignKey("ai_conversations.id", ondelete="CASCADE"), nullable=False
    )
    #: user / assistant / tool / system
    role = db.Column(db.String(16), nullable=False)
    content = db.Column(db.Text, default="")
    #: 推理模型的思考内容（reasoning_content），可与正文分开查看
    reasoning = db.Column(db.Text, default="")
    #: assistant 消息请求的工具调用（原始数组），供审计「AI 调用了什么」
    tool_calls = db.Column(db.JSON, default=list)
    #: role=tool 时，对应的上游 tool_call id（回放给模型要带上）
    tool_call_id = db.Column(db.String(64), default="")
    #: AI 下发的卡片（解析自 ```ai-card 代码块）
    cards = db.Column(db.JSON, default=list)
    #: 该条 assistant 消息是否因敏感操作而挂起等待管理员确认
    status = db.Column(db.String(24), default="done")
    model = db.Column(db.String(64), default="")
    prompt_tokens = db.Column(db.Integer, default=0)
    completion_tokens = db.Column(db.Integer, default=0)
    reasoning_tokens = db.Column(db.Integer, default=0)
    finish_reason = db.Column(db.String(32), default="")
    elapsed_ms = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    tool_calls_rel = db.relationship(
        "AiToolCall",
        backref="message",
        cascade="all, delete-orphan",
        order_by="AiToolCall.id",
        lazy="select",
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "conversationId": self.conversation_id,
            "role": self.role,
            "content": self.content or "",
            "reasoning": self.reasoning or "",
            "toolCalls": self.tool_calls or [],
            "toolCallId": self.tool_call_id or "",
            "cards": self.cards or [],
            "status": self.status,
            "model": self.model,
            "promptTokens": self.prompt_tokens or 0,
            "completionTokens": self.completion_tokens or 0,
            "reasoningTokens": self.reasoning_tokens or 0,
            "finishReason": self.finish_reason or "",
            "elapsedMs": self.elapsed_ms or 0,
            "createdAt": _iso(self.created_at),
        }


class AiToolCall(db.Model):
    """AI 的一次工具调用（审计「调用了什么」+ 敏感操作提权留痕）。"""

    __tablename__ = "ai_tool_calls"
    __table_args__ = (
        db.Index("ix_ai_tool_conv", "conversation_id"),
        db.Index("ix_ai_tool_name", "tool_name"),
        db.Index("ix_ai_tool_ts", "created_at"),
    )

    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(
        db.Integer, db.ForeignKey("ai_conversations.id", ondelete="CASCADE"), nullable=False
    )
    message_id = db.Column(db.Integer, db.ForeignKey("ai_messages.id", ondelete="CASCADE"))
    #: 上游流式返回的 tool_call id，用于和模型回合对齐
    call_id = db.Column(db.String(64), default="")
    user_id = db.Column(db.Integer)
    username = db.Column(db.String(64), default="")
    tool_name = db.Column(db.String(64), nullable=False)
    arguments = db.Column(db.JSON, default=dict)
    #: 该工具要求的权限码（ai:tool / ai:tool_write / ai:tool_exec）
    required_permission = db.Column(db.String(32), default="")
    #: 敏感操作（写/执行）需要管理员密码确认
    sensitive = db.Column(db.Boolean, default=False)
    #: pending / approved / rejected / running / success / failure / denied
    status = db.Column(db.String(24), default="pending")
    #: 提权确认人（管理员账号名），用于审计「谁批准的」
    confirmed_by = db.Column(db.String(64), default="")
    confirmed_at = db.Column(db.DateTime)
    result_summary = db.Column(db.String(512), default="")
    result_preview = db.Column(db.Text, default="")
    error = db.Column(db.String(512), default="")
    duration_ms = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "conversationId": self.conversation_id,
            "messageId": self.message_id,
            "callId": self.call_id,
            "userId": self.user_id,
            "username": self.username,
            "toolName": self.tool_name,
            "arguments": self.arguments or {},
            "requiredPermission": self.required_permission,
            "sensitive": bool(self.sensitive),
            "status": self.status,
            "confirmedBy": self.confirmed_by,
            "confirmedAt": _iso(self.confirmed_at),
            "resultSummary": self.result_summary or "",
            "resultPreview": self.result_preview or "",
            "error": self.error or "",
            "durationMs": self.duration_ms or 0,
            "createdAt": _iso(self.created_at),
        }


def _iso(value: datetime | None) -> str | None:
    """统一输出为带 Z 的 ISO8601，前端 dayjs 可直接解析。"""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
