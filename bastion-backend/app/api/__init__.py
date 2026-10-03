"""REST API 蓝图注册。"""

from __future__ import annotations


def register_blueprints(app) -> None:
    """注册全部蓝图。

    注意：hosts.py 与 policies.py 各自定义了**两个**蓝图
    （主机 + 主机分组、命令策略 + 策略规则）。漏注册第二个蓝图不会报错，
    只会让整组接口静默 404 —— 前端页面直接变成死链。
    新增蓝图时必须同步加进下面这个元组。
    """
    from .ai import bp as ai_bp
    from .audits import bp as audits_bp
    from .auth import bp as auth_bp
    from .file_policies import bp as file_policies_bp
    from .file_policies import rule_bp as file_rules_bp
    from .files import bp as files_bp
    from .grants import bp as grants_bp
    from .hosts import bp as hosts_bp
    from .hosts import group_bp as host_groups_bp
    from .policies import bp as policies_bp
    from .policies import rule_bp as policy_rules_bp
    from .rdp import bp as rdp_bp
    from .roles import bp as roles_bp
    from .sessions import bp as sessions_bp
    from .settings import bp as settings_bp
    from .users import bp as users_bp

    for blueprint in (
        auth_bp,
        users_bp,
        roles_bp,
        hosts_bp,
        host_groups_bp,
        grants_bp,
        policies_bp,
        policy_rules_bp,
        file_policies_bp,
        file_rules_bp,
        files_bp,
        rdp_bp,
        sessions_bp,
        audits_bp,
        settings_bp,
        ai_bp,
    ):
        app.register_blueprint(blueprint)
