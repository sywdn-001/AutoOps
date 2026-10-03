"""命令策略接口（能执行什么命令、不能执行什么命令）。"""

from __future__ import annotations

from flask import Blueprint, request

from ..audit import log_event
from ..extensions import db
from ..models import CommandPolicy, CommandRule, Grant
from ..policy import RISK_ORDER, evaluate_policy, seed_policies
from ..security import admin_required, permission_required
from ..utils import api_error, api_list, api_ok, page_args, parse_bool, parse_int

bp = Blueprint("policies", __name__, url_prefix="/api/policies")

ALLOWED_ACTIONS = {"allow", "deny", "confirm"}
ALLOWED_MATCH_TYPES = {"regex", "prefix", "exact", "contains"}
ALLOWED_RISK_LEVELS = set(RISK_ORDER.keys())


def _policy_payload(policy: CommandPolicy, with_rules: bool = True) -> dict:
    data = policy.to_dict(with_rules=with_rules)
    data["ruleCount"] = CommandRule.query.filter_by(policy_id=policy.id).count()
    data["grantCount"] = Grant.query.filter_by(policy_id=policy.id).count()
    return data


@bp.get("")
@permission_required("policy:view", "grant:view")
def list_policies():
    page, size = page_args(default_size=50)
    query = CommandPolicy.query
    keyword = (request.args.get("keyword") or "").strip()
    if keyword:
        query = query.filter(CommandPolicy.name.like(f"%{keyword}%"))
    total = query.count()
    rows = query.order_by(CommandPolicy.id.asc()).offset((page - 1) * size).limit(size).all()
    return api_list([_policy_payload(row, with_rules=False) for row in rows], total, page, size)


@bp.get("/options")
@permission_required("policy:view", "grant:view")
def policy_options():
    rows = CommandPolicy.query.order_by(CommandPolicy.id.asc()).all()
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


@bp.get("/<int:policy_id>")
@permission_required("policy:view")
def get_policy(policy_id: int):
    policy = db.session.get(CommandPolicy, policy_id)
    if policy is None:
        return api_error("策略不存在", 404, code="NOT_FOUND")
    return api_ok(_policy_payload(policy))


@bp.post("")
@admin_required
def create_policy():
    payload = request.get_json(silent=True) or {}
    name = (payload.get("name") or "").strip()
    if not name:
        return api_error("策略名称不能为空", 400, code="INVALID_ARGUMENT")
    if CommandPolicy.query.filter_by(name=name).first() is not None:
        return api_error("策略名称已存在", 409, code="DUPLICATED")
    default_action = (payload.get("defaultAction") or "deny").strip().lower()
    if default_action not in ALLOWED_ACTIONS:
        return api_error("默认动作只支持 allow / deny / confirm", 400, code="INVALID_ARGUMENT")
    policy = CommandPolicy(
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
    log_event("policy", "create_policy", target_type="policy", target_id=policy.id, target_name=policy.name)
    return api_ok(_policy_payload(policy), "策略已创建")


@bp.put("/<int:policy_id>")
@admin_required
def update_policy(policy_id: int):
    policy = db.session.get(CommandPolicy, policy_id)
    if policy is None:
        return api_error("策略不存在", 404, code="NOT_FOUND")
    payload = request.get_json(silent=True) or {}
    if "name" in payload:
        name = (payload.get("name") or "").strip()
        if not name:
            return api_error("策略名称不能为空", 400, code="INVALID_ARGUMENT")
        duplicated = CommandPolicy.query.filter(
            CommandPolicy.name == name, CommandPolicy.id != policy_id
        ).first()
        if duplicated is not None:
            return api_error("策略名称已存在", 409, code="DUPLICATED")
        policy.name = name
    if "description" in payload:
        policy.description = (payload.get("description") or "").strip()
    if "defaultAction" in payload:
        default_action = (payload.get("defaultAction") or "").strip().lower()
        if default_action not in ALLOWED_ACTIONS:
            return api_error("默认动作只支持 allow / deny / confirm", 400, code="INVALID_ARGUMENT")
        policy.default_action = default_action
    db.session.commit()
    log_event("policy", "update_policy", target_type="policy", target_id=policy.id, target_name=policy.name)
    return api_ok(_policy_payload(policy), "策略已更新")


@bp.delete("/<int:policy_id>")
@admin_required
def delete_policy(policy_id: int):
    policy = db.session.get(CommandPolicy, policy_id)
    if policy is None:
        return api_error("策略不存在", 404, code="NOT_FOUND")
    if policy.is_default:
        return api_error("默认策略不允许删除", 400, code="INVALID_OPERATION")
    in_use = Grant.query.filter_by(policy_id=policy_id).count()
    if in_use:
        return api_error(f"还有 {in_use} 条授权引用该策略，无法删除", 400, code="INVALID_OPERATION")
    name = policy.name
    CommandRule.query.filter_by(policy_id=policy_id).delete()
    db.session.delete(policy)
    db.session.commit()
    log_event("policy", "delete_policy", target_type="policy", target_id=policy_id, target_name=name)
    return api_ok(None, "策略已删除")


@bp.post("/reset-builtin")
@admin_required
def reset_builtin():
    """把内置策略模板重新写入（幂等，不覆盖自定义策略）。"""
    created = seed_policies(db.session)
    log_event("policy", "reset_builtin", message=f"重建内置策略：{', '.join(created.keys())}")
    return api_ok([{"id": p.id, "name": p.name} for p in created.values()], "内置策略已就绪")


def _build_rule(policy_id: int, payload: dict):
    pattern = (payload.get("pattern") or "").strip()
    if not pattern:
        return None, api_error("规则匹配内容不能为空", 400, code="INVALID_ARGUMENT")
    match_type = (payload.get("matchType") or "regex").strip().lower()
    if match_type not in ALLOWED_MATCH_TYPES:
        return None, api_error(
            "匹配方式只支持 regex / prefix / exact / contains", 400, code="INVALID_ARGUMENT"
        )
    action = (payload.get("action") or "deny").strip().lower()
    if action not in ALLOWED_ACTIONS:
        return None, api_error("规则动作只支持 allow / deny / confirm", 400, code="INVALID_ARGUMENT")
    risk_level = (payload.get("riskLevel") or "high").strip().lower()
    if risk_level not in ALLOWED_RISK_LEVELS:
        return None, api_error("风险等级只支持 low / medium / high / critical", 400, code="INVALID_ARGUMENT")
    if match_type == "regex":
        import re

        try:
            re.compile(pattern)
        except re.error as exc:
            return None, api_error(f"正则表达式非法：{exc}", 400, code="INVALID_REGEX")
    return (
        CommandRule(
            policy_id=policy_id,
            priority=parse_int(payload.get("priority"), 100) or 100,
            action=action,
            match_type=match_type,
            pattern=pattern,
            risk_level=risk_level,
            description=(payload.get("description") or "").strip(),
            enabled=parse_bool(payload.get("enabled"), True),
        ),
        None,
    )


# --------------------------------------------------------------------------
# 策略规则
# --------------------------------------------------------------------------
rule_bp = Blueprint("policy_rules", __name__, url_prefix="/api/policy-rules")


@rule_bp.get("")
@permission_required("policy:view")
def list_rules():
    page, size = page_args(default_size=100)
    query = CommandRule.query
    policy_id = parse_int(request.args.get("policyId"))
    if policy_id:
        query = query.filter(CommandRule.policy_id == policy_id)
    keyword = (request.args.get("keyword") or "").strip()
    if keyword:
        query = query.filter(CommandRule.pattern.like(f"%{keyword}%"))
    total = query.count()
    rows = (
        query.order_by(CommandRule.priority.asc(), CommandRule.id.asc())
        .offset((page - 1) * size)
        .limit(size)
        .all()
    )
    return api_list([row.to_dict() for row in rows], total, page, size)


@rule_bp.post("")
@admin_required
def create_rule():
    payload = request.get_json(silent=True) or {}
    policy_id = parse_int(payload.get("policyId"))
    policy = db.session.get(CommandPolicy, policy_id) if policy_id else None
    if policy is None:
        return api_error("请选择有效的策略", 400, code="INVALID_ARGUMENT")
    rule, error = _build_rule(policy.id, payload)
    if error is not None:
        return error
    db.session.add(rule)
    db.session.commit()
    log_event(
        "policy",
        "create_rule",
        target_type="policy",
        target_id=policy.id,
        target_name=policy.name,
        message=f"新增规则 {rule.action} {rule.match_type}:{rule.pattern}",
    )
    return api_ok(rule.to_dict(), "规则已添加")


@rule_bp.put("/<int:rule_id>")
@admin_required
def update_rule(rule_id: int):
    rule = db.session.get(CommandRule, rule_id)
    if rule is None:
        return api_error("规则不存在", 404, code="NOT_FOUND")
    payload = request.get_json(silent=True) or {}
    if "pattern" in payload or "matchType" in payload or "action" in payload or "riskLevel" in payload:
        merged = {
            "pattern": payload.get("pattern", rule.pattern),
            "matchType": payload.get("matchType", rule.match_type),
            "action": payload.get("action", rule.action),
            "riskLevel": payload.get("riskLevel", rule.risk_level),
        }
        rebuilt, error = _build_rule(rule.policy_id, merged)
        if error is not None:
            return error
        rule.pattern = rebuilt.pattern
        rule.match_type = rebuilt.match_type
        rule.action = rebuilt.action
        rule.risk_level = rebuilt.risk_level
    if "priority" in payload:
        rule.priority = parse_int(payload.get("priority"), rule.priority) or rule.priority
    if "description" in payload:
        rule.description = (payload.get("description") or "").strip()
    if "enabled" in payload:
        rule.enabled = parse_bool(payload.get("enabled"), True)
    db.session.commit()
    log_event("policy", "update_rule", target_type="command_rule", target_id=rule.id, target_name=rule.pattern)
    return api_ok(rule.to_dict(), "规则已更新")


@rule_bp.delete("/<int:rule_id>")
@admin_required
def delete_rule(rule_id: int):
    rule = db.session.get(CommandRule, rule_id)
    if rule is None:
        return api_error("规则不存在", 404, code="NOT_FOUND")
    pattern = rule.pattern
    db.session.delete(rule)
    db.session.commit()
    log_event("policy", "delete_rule", target_type="command_rule", target_id=rule_id, target_name=pattern)
    return api_ok(None, "规则已删除")


@bp.post("/<int:policy_id>/test")
@permission_required("policy:view", "policy:manage")
def test_policy(policy_id: int):
    """策略试算：给一条命令，返回允许/拒绝、命中规则与风险等级。"""
    policy = db.session.get(CommandPolicy, policy_id)
    if policy is None:
        return api_error("策略不存在", 404, code="NOT_FOUND")
    payload = request.get_json(silent=True) or {}
    command = payload.get("command") or ""
    if not command.strip():
        return api_error("请输入要试算的命令", 400, code="INVALID_ARGUMENT")
    decision = evaluate_policy(policy, command)
    return api_ok(
        {
            "allowed": decision.allowed,
            "action": decision.action,
            "riskLevel": decision.risk_level,
            "reason": decision.reason,
            "ruleId": decision.rule_id,
            "rulePattern": decision.rule_pattern,
            "segments": [
                {
                    "segment": seg.segment,
                    "allowed": seg.allowed,
                    "action": seg.action,
                    "riskLevel": seg.risk_level,
                    "ruleId": seg.rule_id,
                    "rulePattern": seg.rule_pattern,
                    "reason": seg.reason,
                }
                for seg in (decision.segments or [])
            ],
        }
    )


@bp.post("/evaluate")
@permission_required("policy:view", "grant:view", "terminal:use")
def evaluate_preview():
    """全局试算：不指定策略时用默认策略。"""
    payload = request.get_json(silent=True) or {}
    command = payload.get("command") or ""
    policy_id = parse_int(payload.get("policyId"))
    policy = db.session.get(CommandPolicy, policy_id) if policy_id else None
    if policy is None:
        policy = CommandPolicy.query.filter_by(is_default=True).first()
    decision = evaluate_policy(policy, command)
    return api_ok(
        {
            "policyId": policy.id if policy else None,
            "policyName": policy.name if policy else "",
            "allowed": decision.allowed,
            "action": decision.action,
            "riskLevel": decision.risk_level,
            "reason": decision.reason,
            "ruleId": decision.rule_id,
            "rulePattern": decision.rule_pattern,
        }
    )
