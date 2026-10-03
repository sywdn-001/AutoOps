"""轻量级库结构同步：给已经存在的表补上模型里新增的列。

项目没有引入 Alembic（单文件 SQLite、开发期快速演进），但 ``db.create_all()``
**只会建新表、不会改老表** —— 模型一加列，老库跑新代码就会：

    sqlalchemy.exc.OperationalError: no such column: grants.file_policy_id

``ensure_schema()`` 因此做**只增不减**的同构迁移：缺失的列用
``ALTER TABLE ... ADD COLUMN`` 补上；带标量默认值的列顺手把已有行的 NULL
回填成默认值（否则老行的布尔列是 NULL，``to_dict()`` 会吐出 ``None``）。

边界（刻意不做的事）：不删列、不改类型、不改默认值、不动已有数据，也不处理
NOT NULL 约束（SQLite 加 NOT NULL 列必须带默认值，写进迁移脚本反而更容易把人
坑住）。表结构有破坏性变更时，仍然只能人工处理或删库重建。
"""

from __future__ import annotations

import logging

from sqlalchemy import inspect, text

logger = logging.getLogger(__name__)


def _literal(value):
    """把模型默认值转成能塞进 :value 绑定参数的 Python 值。"""
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float, str)):
        return value
    return None


def _scalar_default(column):
    """取列定义里的标量默认值（``default=False`` / ``default=0`` / ``default="x"``）。

    只认 ``ColumnDefault`` 的标量参数：``default=utcnow`` 这类可调用对象会被跳过，
    因为它在 DDL 里写不出稳定的字面量。
    """
    default = getattr(column, "default", None)
    if default is None or not getattr(default, "is_scalar", False):
        return None
    return _literal(default.arg)


def _column_ddl(column, dialect) -> str:
    type_sql = column.type.compile(dialect=dialect)
    ddl = f'"{column.name}" {type_sql}'
    literal = _scalar_default(column)
    if isinstance(literal, str):
        escaped = literal.replace("'", "''")
        ddl += f" DEFAULT '{escaped}'"
    elif literal is not None:
        ddl += f" DEFAULT {literal}"
    return ddl


def ensure_schema(engine=None) -> list[str]:
    """把模型里新增的列补进已存在的表，返回实际执行的语句（空列表 = 结构已是最新）。

    ``engine`` 省略时用当前 app 的 ``db.engine``（必须在 app context 内调用）。
    """
    from .extensions import db

    engine = engine if engine is not None else db.engine
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    statements: list[str] = []

    for table in db.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue  # 新表交给 db.create_all()
        present = {column["name"] for column in inspector.get_columns(table.name)}
        with engine.begin() as conn:
            for column in table.columns:
                if column.name in present:
                    continue
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN {_column_ddl(column, engine.dialect)}'
                conn.execute(text(ddl))
                statements.append(ddl)
                literal = _scalar_default(column)
                if literal is not None:
                    backfill = (
                        f'UPDATE "{table.name}" SET "{column.name}" = :value '
                        f'WHERE "{column.name}" IS NULL'
                    )
                    conn.execute(text(backfill), {"value": literal})
                    statements.append(backfill)
                logger.warning(
                    "已为表 %s 补充列 %s（轻量迁移；如需审计请记录本次启动）",
                    table.name,
                    column.name,
                )
    return statements
