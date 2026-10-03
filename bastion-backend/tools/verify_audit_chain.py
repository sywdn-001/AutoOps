"""CLI：审计链式哈希校验工具。

用法：
    python tools/verify_audit_chain.py            # 默认校验三张表，exit 0/1
    python tools/verify_audit_chain.py -v         # 详细模式：打印每表错误前 10 条
    python tools/verify_audit_chain.py --table audit_logs
    python tools/verify_audit_chain.py --print-head          # 抄链尾做库外锚点（每天一条到远端/文件）
    python tools/verify_audit_chain.py --expect-head audit_logs=686f5202…

为什么需要 --print-head / --expect-head：链只能证明「手上这串是连续的」，证明不了「没被砍掉尾巴」。
删掉尾行的攻击在库内自洽，只有拿**库外**抄走的链尾来比对才露馅。

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
    parser.add_argument(
        "--print-head",
        action="store_true",
        help="只打印三张表的链尾哈希（库外锚点），exit 0",
    )
    parser.add_argument(
        "--expect-head",
        action="append",
        default=[],
        metavar="TABLE=HASH",
        help="传入库外锚点比对链尾，可重复；对不上则判「被截断」并 exit 1",
    )
    args = parser.parse_args()

    expected_heads: dict[str, str] = {}
    for item in args.expect_head:
        if "=" not in item:
            parser.error(f"--expect-head 需要 TABLE=HASH 形式：{item}")
        name, _, value = item.partition("=")
        expected_heads[name.strip()] = value.strip()

    from app import create_app

    overrides = {}
    if args.db_uri:
        overrides["SQLALCHEMY_DATABASE_URI"] = args.db_uri
    if args.secret_key:
        overrides["SECRET_KEY"] = args.secret_key
    app = create_app(overrides or None)

    with app.app_context():
        from app.audit import CHAIN_TABLES, verify_table_chain, verify_all_chains

        if args.print_head:
            payload = {}
            for name, model in CHAIN_TABLES:
                one = verify_table_chain(model)
                payload[name] = one["head"]
            if args.as_json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print("AutoOps 堡垒机 · 审计链尾锚点（抄到库外/远端留存）")
                for name, value in payload.items():
                    print(f"{name}={value or '(空)'}")
            return 0

        if args.table:
            name_to_model = dict(CHAIN_TABLES)
            result = {"ok": True, "tables": {}}
            one = verify_table_chain(
                name_to_model[args.table],
                expected_head=expected_heads.get(args.table),
            )
            result["tables"][args.table] = one
            result["ok"] = one["ok"]
        else:
            result = verify_all_chains(expected_heads=expected_heads or None)

    summary_lines = []
    for name, info in result["tables"].items():
        status = "OK" if info["ok"] else "FAIL"
        line = (
            f"[{status}] {name}: 总行 {info['total']}, 已哈希 {info['verified']}, "
            f"遗留前缀 {info.get('prefix', 0)}, 链内空洞 {info.get('pending_inside', 0)}, "
            f"坏首行 {info['first_bad']}"
        )
        anchor_err = next(
            (e for e in info["errors"] if "锚点" in str(e.get("reason", ""))), None
        )
        if anchor_err:
            line += (
                f"\n        库外锚点不符：期望 {str(anchor_err['expected'])[:12]}… "
                f"实际 {str(anchor_err['actual'])[:12]}…（链被截断或表被替换）"
            )
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
                        f"      expected: {str(err.get('expected', ''))[:32]}…\n"
                        f"      actual  : {str(err.get('actual', ''))[:32]}…"
                    )
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
