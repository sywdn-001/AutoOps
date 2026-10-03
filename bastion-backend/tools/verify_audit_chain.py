"""CLI：审计链式哈希校验工具。

用法：
    python tools/verify_audit_chain.py            # 默认校验三张表，exit 0/1
    python tools/verify_audit_chain.py -v         # 详细模式：打印每表错误前 10 条
    python tools/verify_audit_chain.py --table audit_logs

在测试库上跑：
    python tools/verify_audit_chain.py --db-uri sqlite:///path/to/bastion.db
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = HERE.parent
sys.path.insert(0, str(BASE))


def main() -> int:
    parser = argparse.ArgumentParser(description="校验 AutoOps 堡垒机审计链式哈希")
    parser.add_argument(
        "--table",
        choices=["audit_logs", "command_logs", "file_logs"],
        default=None,
        help="只校验单张表",
    )
    parser.add_argument(
        "--db-uri",
        default=None,
        help="指定 SQLAlchemy DB URI；默认走应用配置（bastion-backend/instance/bastion.db）",
    )
    parser.add_argument(
        "--secret-key",
        default=None,
        help="指定 SECRET_KEY；默认读应用配置 instance/secret.key",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="打印详细错误列表",
    )
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="以 JSON 输出结果",
    )
    args = parser.parse_args()

    from app import create_app

    overrides = {}
    if args.db_uri:
        overrides["SQLALCHEMY_DATABASE_URI"] = args.db_uri
    if args.secret_key:
        overrides["SECRET_KEY"] = args.secret_key
    app = create_app(overrides or None)

    with app.app_context():
        from app.audit import CHAIN_TABLES, verify_table_chain, verify_all_chains

        if args.table:
            name_to_model = dict(CHAIN_TABLES)
            result = {"ok": True, "tables": {}}
            one = verify_table_chain(name_to_model[args.table])
            result["tables"][args.table] = one
            result["ok"] = one["ok"]
        else:
            result = verify_all_chains()

    summary_lines = []
    for name, info in result["tables"].items():
        status = "OK" if info["ok"] else "FAIL"
        line = f"[{status}] {name}: 总行 {info['total']}, 已哈希 {info['verified']}, 坏首行 {info['first_bad']}"
        summary_lines.append(line)

    if args.as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("AutoOps 堡垒机 · 审计链哈希校验")
        for line in summary_lines:
            print(line)
        if args.verbose:
            for name, info in result["tables"].items():
                if not info["errors"]:
                    continue
                print(f"\n--- {name} errors ---")
                for err in info["errors"][:10]:
                    print(
                        f"  id={err['id']}  {err['reason']}\n"
                        f"      expected: {err['expected'][:32]}…\n"
                        f"      actual  : {err['actual'][:32]}…"
                    )
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
