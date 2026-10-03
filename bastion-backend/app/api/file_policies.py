"""文件策略接口（文件管理器能碰哪些路径、能做哪些操作）。"""

from __future__ import annotations

import re

from flask import Blueprint, request

from ..audit import log_event
from ..extensions import db
from ..file_policy import (
    FILE_OPERATIONS,
    RISK_ORDER,
    evaluate_file_policy,
    seed_file_policies,
)
from ..models import FilePolicy, FileRule, Grant
from ..security import admin_required, permission_required
from ..utils import api_error, api_list, api_ok, page_args, parse_bool, parse_int

bp = Blueprint("file_policies", __name__, url_prefix="/api/file-policies")

ALLOWED_ACTIONS = {"allow", "deny"}
ALLOWED_MATCH_TYPES = {"glob", "regex", "prefix", "contains"}
ALLOWED_RISK_LEVELS = set(RISK_ORDER.keys())
ALLOWED_OPERATIONS = set(FILE_OPERATIONS.keys()) | {"*"}


def _policy_payload(policy: FilePolicy, with_rules: bool = True) -> dict:
    data = policy.to_dict(with_rules=with_rules)
    data["ruleCount"] = FileRule.query.filter_by(policy_id=policy.id).count()
    data["grantCount"] = Grant.query.filter_by(file_policy_id=policy.id).count()
    data["operations"] = [
        {"value": key, "label": label} for key, label in FILE_OPERATIONS.items()
    ]
    return data


@bp.get("")
@permission_required("filepolicy:view", "grant:view", "policy:view")
def list_file_policies():
    page, size = page_args(default_size=50)
    query = FilePolicy.query
    keyword = (request.args.get("keyword") or "").strip()
    if keyword:
        query = query.filter(FilePolicy.name.like(f"%{keyword}%"))
    total = query.count()
    rows = query.order_by(FilePolicy.id.asc()).offset((page - 1) * size).limit(size).all()
    return api_list([_policy_payload(row, with_rules=False) for row in rows], total, page, size)


@bp.get("/options")
@permission_required("filepolicy:view", "grant:view", "policy:view")
def file_policy_options():
    rows = FilePolicy.query.order_by(FilePolicy.id.asc()).all()
    return api_ok(
        [
            {
                "label": row.name,
                "value": row.id,
                "defaultAction": row.default_action,
                "isDefault": row.is_default,
            }
            for row in rows
        ]
    )


@bp.get("/operations")
@permission_required("filepolicy:view", "policy:view", "grant:view")
def operations():
    """操作清单：策略页下拉与审计筛选共用一份定义。"""
    return api_ok([{"value": key, "label": label} for key, label in FILE_OPERATIONS.items()])


@bp.get("/<int:policy_id>")
@permission_required("filepolicy:view", "policy:view")
def get_file_policy(policy_id: int):
    policy = db.session.get(FilePolicy, policy_id)
    if policy is None:
        return api_error("文件策略不存在", 404, code="NOT_FOUND")
    return api_ok(_policy_payload(policy))


@bp.post("")
@admin_required
def create_file_policy():
    payload = request.get_json(silent=True) or {}
    name = (payload.get("name") or "").strip()
    if not name:
        return api_error("策略名称不能为空", 400, code="INVALID_ARGUMENT")
    if FilePolicy.query.filter_by(name=name).first() is not None:
        return api_error("策略名称已存在", 409, code="DUPLICATED")
    default_action = (payload.get("defaultAction") or "deny").strip().lower()
    if default_action not in ALLOWED_ACTIONS:
        return api_error("默认动作只支持 allow / deny", 400, code="INVALID_ARGUMENT")
    policy = FilePolicy(
        name=name,
        description=(payload.get("description") or "").strip(),
        default_action=default_action,
        is_default=False,
    )
    db.session.add(policy)
    db.session.flush()
    for item in payload.get("rules") or []:
        rule, error = _build_rule(policy.id, item)
        if error is not None:
            db.session.rollback()
            return error
        db.session.add(rule)
    db.session.commit()
    log_event("policy", "create_file_policy", target_type="file_policy", target_id=policy.id, target_name=policy.name)
    return api_ok(_policy_payload(policy), "文件策略已创建")


@bp.put("/<int:policy_id>")
@admin_required
def update_file_policy(policy_id: int):
    policy = db.session.get(FilePolicy, policy_id)
    if policy is None:
        return api_error("文件策略不存在", 404, code="NOT_FOUND")
    payload = request.get_json(silent=True) or {}
    if "name" in payload:
        name = (payload.get("name") or "").strip()
        if not name:
            return api_error("策略名称不能为空", 400, code="INVALID_ARGUMENT")
        duplicated = FilePolicy.query.filter(
            FilePolicy.name == name, FilePolicy.id != policy_id
        ).first()
        if duplicated is not None:
            return api_error("策略名称已存在", 409, code="DUPLICATED")
        policy.name = name
    if "description" in payload:
        policy.description = (payload.get("description") or "").strip()
    if "defaultAction" in payload:
        default_action = (payload.get("defaultAction") or "").strip().lower()
        if default_action not in ALLOWED_ACTIONS:
            return api_error("默认动作只支持 allow / deny", 400, code="INVALID_ARGUMENT")
        policy.default_action = default_action
    db.session.commit()
    log_event("policy", "update_file_policy", target_type="file_policy", target_id=policy.id, target_name=policy.name)
    return api_ok(_policy_payload(policy), "文件策略已更新")


@bp.delete("/<int:policy_id>")
@admin_required
def delete_file_policy(policy_id: int):
    policy = db.session.get(FilePolicy, policy_id)
    if policy is None:
        return api_error("文件策略不存在", 404, code="NOT_FOUND")
    if policy.is_default:
        return api_error("默认文件策略不允许删除", 400, code="INVALID_OPERATION")
    in_use = Grant.query.filter_by(file_policy_id=policy_id).count()
    if in_use:
        return api_error(f"还有 {in_use} 条授权引用该策略，无法删除", 400, code="INVALID_OPERATION")
    name = policy.name
    FileRule.query.filter_by(policy_id=policy_id).delete()
    db.session.delete(policy)
    db.session.commit()
    log_event("policy", "delete_file_policy", target_type="file_policy", target_id=policy_id, target_name=name)
    return api_ok(None, "文件策略已删除")


@bp.post("/reset-builtin")
@admin_required
def reset_builtin():
    """把内置文件策略模板重新写入（幂等，不覆盖自定义策略）。"""
    created = seed_file_policies(db.session)
    log_event("policy", "reset_builtin_file", message=f"重建内置文件策略：{', '.join(created.keys())}")
    return api_ok([{"id": p.id, "name": p.name} for p in created.values()], "内置文件策略已就绪")


def _build_rule(policy_id: int, payload: dict):
    pattern = (payload.get("pathPattern") or payload.get("pattern") or "").strip()
    if not pattern:
        return None, api_error("路径匹配内容不能为空", 400, code="INVALID_ARGUMENT")
    match_type = (payload.get("matchType") or "glob").strip().lower()
    if match_type not in ALLOWED_MATCH_TYPES:
        return None, api_error(
            "匹配方式只支持 glob / regex / prefix / contains", 400, code="INVALID_ARGUMENT"
        )
    if match_type == "regex":
        try:
            re.compile(pattern)
        except re.error as exc:
            return None, api_error(f"正则表达式非法：{exc}", 400, code="INVALID_REGEX")
    action = (payload.get("action") or "deny").strip().lower()
    if action not in ALLOWED_ACTIONS:
        return None, api_error("规则动作只支持 allow / deny", 400, code="INVALID_ARGUMENT")
    operation = (payload.get("operation") or "*").strip().lower() or "*"
    if operation not in ALLOWED_OPERATIONS:
        return None, api_error(
            "操作只支持：" + " / ".join(sorted(ALLOWED_OPERATIONS)), 400, code="INVALID_ARGUMENT"
        )
    risk_level = (payload.get("riskLevel") or "high").strip().lower()
    if risk_level not in ALLOWED_RISK_LEVELS:
        return None, api_error("风险等级只支持 low / medium / high / critical", 400, code="INVALID_ARGUMENT")
    return (
        FileRule(
            policy_id=policy_id,
            priority=parse_int(payload.get("priority"), 100) or 100,
            action=action,
            operation=operation,
            match_type=match_type,
            path_pattern=pattern,
            risk_level=risk_level,
            description=(payload.get("description") or "").strip(),
            enabled=parse_bool(payload.get("enabled"), True),
        ),
        None,
    )


# --------------------------------------------------------------------------
# 文件策略规则
# --------------------------------------------------------------------------
rule_bp = Blueprint("file_rules", __name__, url_prefix="/api/file-rules")


@rule_bp.get("")
@permission_required("filepolicy:view")
def list_file_rules():
    page, size = page_args(default_size=100)
    query = FileRule.query
    policy_id = parse_int(request.args.get("policyId"))
    if policy_id:
        query = query.filter(FileRule.policy_id == policy_id)
    keyword = (request.args.get("keyword") or "").strip()
    if keyword:
        query = query.filter(FileRule.path_pattern.like(f"%{keyword}%"))
    total = query.count()
    rows = (
        query.order_by(FileRule.priority.asc(), FileRule.id.asc())
        .offset((page - 1) * size)
        .limit(size)
        .all()
    )
    return api_list([row.to_dict() for row in rows], total, page, size)


@rule_bp.post("")
@admin_required
def create_file_rule():
    payload = request.get_json(silent=True) or {}
    policy_id = parse_int(payload.get("policyId"))
    policy = db.session.get(FilePolicy, policy_id) if policy_id else None
    if policy is None:
        return api_error("请选择有效的文件策略", 400, code="INVALID_ARGUMENT")
    rule, error = _build_rule(policy.id, payload)
    if error is not None:
        return error
    db.session.add(rule)
    db.session.commit()
    log_event(
        "policy",
        "create_file_rule",
        target_type="file_policy",
        target_id=policy.id,
        target_name=policy.name,
        message=f"新增文件规则 {rule.action} {rule.operation} {rule.match_type}:{rule.path_pattern}",
    )
    return api_ok(rule.to_dict(), "规则已添加")


@rule_bp.put("/<int:rule_id>")
@admin_required
def update_file_rule(rule_id: int):
    rule = db.session.get(FileRule, rule_id)
    if rule is None:
        return api_error("规则不存在", 404, code="NOT_FOUND")
    payload = request.get_json(silent=True) or {}
    merged_keys = ("pathPattern", "pattern", "matchType", "action", "operation", "riskLevel")
    if any(key in payload for key in merged_keys):
        merged = {
            "pathPattern": payload.get("pathPattern", payload.get("pattern", rule.path_pattern)),
            "matchType": payload.get("matchType", rule.match_type),
            "action": payload.get("action", rule.action),
            "operation": payload.get("operation", rule.operation),
            "riskLevel": payload.get("riskLevel", rule.risk_level),
        }
        rebuilt, error = _build_rule(rule.policy_id, merged)
        if error is not None:
            return error
        rule.path_pattern = rebuilt.path_pattern
        rule.match_type = rebuilt.match_type
        rule.action = rebuilt.action
        rule.operation = rebuilt.operation
        rule.risk_level = rebuilt.risk_level
    if "priority" in payload:
        rule.priority = parse_int(payload.get("priority"), rule.priority) or rule.priority
    if "description" in payload:
        rule.description = (payload.get("description") or "").strip()
    if "enabled" in payload:
        rule.enabled = parse_bool(payload.get("enabled"), True)
    db.session.commit()
    log_event("policy", "update_file_rule", target_type="file_rule", target_id=rule.id, target_name=rule.path_pattern)
    return api_ok(rule.to_dict(), "规则已更新")


@rule_bp.delete("/<int:rule_id>")
@admin_required
def delete_file_rule(rule_id: int):
    rule = db.session.get(FileRule, rule_id)
    if rule is None:
        return api_error("规则不存在", 404, code="NOT_FOUND")
    pattern = rule.path_pattern
    db.session.delete(rule)
    db.session.commit()
    log_event("policy", "delete_file_rule", target_type="file_rule", target_id=rule_id, target_name=pattern)
    return api_ok(None, "规则已删除")


@bp.post("/<int:policy_id>/test")
@permission_required("filepolicy:view", "filepolicy:manage")
def test_file_policy(policy_id: int):
    """策略试算：给一个操作 + 路径，返回允许/拒绝、命中规则与风险等级。"""
    policy = db.session.get(FilePolicy, policy_id)
    if policy is None:
        return api_error("文件策略不存在", 404, code="NOT_FOUND")
    payload = request.get_json(silent=True) or {}
    operation = (payload.get("operation") or "").strip().lower()
    if not operation:
        return api_error("请选择要试算的操作", 400, code="INVALID_ARGUMENT")
    if operation not in ALLOWED_OPERATIONS:
        return api_error(
            f"操作只支持：" + " / ".join(sorted(ALLOWED_OPERATIONS)), 400, code="INVALID_ARGUMENT"
        )
    decision = evaluate_file_policy(
        policy,
        operation,
        payload.get("path") or "/",
        target_path=payload.get("targetPath") or "",
    )
    data = decision.to_dict()
    data["policyId"] = policy.id
    data["policyName"] = policy.name
    return api_ok(data)


@bp.post("/evaluate")
@permission_required("filepolicy:view", "grant:view", "policy:view")
def evaluate_preview():
    """全局试算：不指定策略时用默认文件策略。"""
    payload = request.get_json(silent=True) or {}
    operation = (payload.get("operation") or "").strip().lower()
    if operation and operation not in ALLOWED_OPERATIONS:
        # 未知操作不能悄悄按「默认动作」给出结论，否则前端拿到的是误导性的放行/拦截
        return api_error(
            f"操作只支持：" + " / ".join(sorted(ALLOWED_OPERATIONS)), 400, code="INVALID_ARGUMENT"
        )
    policy_id = parse_int(payload.get("policyId"))
    policy = db.session.get(FilePolicy, policy_id) if policy_id else None
    if policy is None:
        policy = FilePolicy.query.filter_by(is_default=True).first()
    decision = evaluate_file_policy(
        policy,
        operation or "list",
        payload.get("path") or "/",
        target_path=payload.get("targetPath") or "",
    )
    data = decision.to_dict()
    data["policyId"] = policy.id if policy else None
    data["policyName"] = policy.name if policy else ""
    return api_ok(data)
