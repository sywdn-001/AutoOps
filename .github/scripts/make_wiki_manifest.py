"""给 Wiki 手册压缩包里的每个文件写一份 MANIFEST.txt（字节数 + SHA256）。

用法::

    python3 .github/scripts/make_wiki_manifest.py \
        --dir out/wiki --out out/wiki/MANIFEST.txt --version v1.0.0

只做一件事：让「解压出来的文件」可自证 —— 谁都能对着表核一遍，
不用信发布者的描述。
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from datetime import datetime, timezone
from pathlib import Path


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def human_size(num: int) -> str:
    value = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 Wiki 手册包的 MANIFEST.txt")
    parser.add_argument("--dir", required=True, help="要清点的目录（一般是解压后的 out/wiki）")
    parser.add_argument("--out", required=True, help="MANIFEST.txt 的输出路径")
    parser.add_argument("--version", default="", help="手册版本号，写进表头")
    args = parser.parse_args()

    root = Path(args.dir)
    if not root.is_dir():
        print(f"目录不存在：{root}", file=sys.stderr)
        return 2

    files = sorted(p for p in root.rglob("*") if p.is_file() and p.name != "MANIFEST.txt")
    if not files:
        print(f"目录里没有文件：{root}", file=sys.stderr)
        return 2

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    total = sum(p.stat().st_size for p in files)
    lines = [
        "AutoOps 堡垒机 · Wiki 使用手册包清单 / MANIFEST",
        "=" * 64,
        "",
        f"版本 / Version : {args.version or '(未标注)'}",
        f"生成时间 / Built: {stamp}",
        f"文件数 / Files  : {len(files)}",
        f"合计 / Total    : {total} bytes ({human_size(total)})",
        "",
        "每行格式 / Format: <sha256>  <bytes>  <path>",
        "核对 / Verify   : sha256sum -c MANIFEST.txt   （Windows：certutil -hashfile <file> SHA256）",
        "",
    ]
    for path in files:
        rel = path.relative_to(root).as_posix()
        lines.append(f"{sha256_of(path)}  {path.stat().st_size:>8}  {rel}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(f"[manifest] {out}  {len(files)} 个文件 / {human_size(total)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
