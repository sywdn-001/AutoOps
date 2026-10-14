"""给真机巡检脚本签发一个本地管理员 JWT（管理员口令被改过时用这个）。

用法（在 bastion-backend 目录下）：

    python tools/mint_token.py            # 打印 token
    python tools/mint_token.py --write    # 顺便写进 %TEMP%\\cu_token.txt

为什么需要它：`tools/computeruse_check.py` / `live_e2e_check.py` / `ui_check.py` 都接受
`--token`，而管理员口令属于用户资产、不应该出现在命令行历史里。这个脚本直接用后端自己的
`_token_for()` 签一个 12 小时有效的令牌，读的是同一个 `instance/secret.key`。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# 直接 `python tools/mint_token.py` 时 sys.path[0] 是 tools/，会撞上 site-packages 里同名的
# `app` 包（ImportError: cannot import name 'create_app'），所以先把项目根塞进 sys.path。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import create_app  # noqa: E402
from app.api.auth import _token_for  # noqa: E402
from app.models import User  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="签发一个管理员 JWT")
    parser.add_argument("--user", default="admin", help="用户名（默认 admin）")
    parser.add_argument("--write", action="store_true", help="写进 %%TEMP%%\\cu_token.txt")
    args = parser.parse_args()

    app = create_app()
    with app.app_context():
        user = User.query.filter_by(username=args.user).first()
        if user is None:
            print(f"没有用户 {args.user}")
            return 1
        token, ttl = _token_for(user)
    print(token)
    if args.write:
        target = Path(os.environ.get("TEMP", ".")) / "cu_token.txt"
        target.write_text(token, encoding="ascii")
        print(f"已写入 {target}（{ttl:.0f} 秒有效）", file=__import__("sys").stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
