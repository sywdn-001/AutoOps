"""把 .github/WikiRELEASE.md 模板渲染成 Wiki 手册包的 Release 说明。

和 .github/scripts/make_release_notes.py 是一对：那个渲染主程序 Release 的说明
（附件表来自真实产物），这个渲染 Wiki 手册 Release 的说明（页面表来自真实的 .md 文件、
压缩包名/大小/SHA256 来自真实产物）。模板里只要还剩 `{{...}}` 就退出码 3 —— 宁可失败，
也不发一份带占位符的说明。

用法::

    python3 .github/scripts/make_wiki_release_notes.py \\
        --template .github/WikiRELEASE.md --out wiki-notes.md \\
        --version v1.0.0 --stamp 20261005-1530 \\
        --zip "v1.0.0+20261005-1530+AutoOpsWiki.zip" --pages-dir out/wiki \\
        --commit <sha> --repo <repo url> --wiki <wiki url> --workflow <run url>
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

PLACEHOLDER_RE = re.compile(r"\{\{[A-Z_]+\}\}")


def human_size(num: float) -> str:
    value = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def page_table(pages_dir: Path) -> tuple[str, int, int]:
    """页面清单：`| 文件 | 大小 |`，Home / _Sidebar / _Footer 排在最前。"""
    files = sorted(
        (p for p in pages_dir.rglob("*.md")),
        key=lambda p: (not p.name.startswith(("Home", "_Sidebar", "_Footer")), p.name.lower()),
    )
    rows = ["| 页面 / Page | 大小 / Size |", "| --- | --- |"]
    for path in files:
        rows.append(f"| [`{path.name}`]({path.name}) | {human_size(path.stat().st_size)} |")
    return "\n".join(rows), len(files), sum(p.stat().st_size for p in files)


def main() -> int:
    parser = argparse.ArgumentParser(description="渲染 Wiki 手册 Release 说明")
    parser.add_argument("--template", required=True, help="说明模板（.github/WikiRELEASE.md）")
    parser.add_argument("--out", required=True, help="输出文件")
    parser.add_argument("--version", required=True, help="手册版本号，如 v1.0.0")
    parser.add_argument("--stamp", required=True, help="打包时间戳，如 20261005-1530（UTC）")
    parser.add_argument("--zip", required=True, help="压缩包文件名")
    parser.add_argument("--pages-dir", required=True, help="页面目录（用于生成清单）")
    parser.add_argument("--commit", default="", help="对应代码提交")
    parser.add_argument("--repo", default="", help="仓库地址")
    parser.add_argument("--wiki", default="", help="Wiki 地址")
    parser.add_argument("--workflow", default="", help="本次 Actions 运行地址")
    args = parser.parse_args()

    template_path = Path(args.template)
    if not template_path.is_file():
        print(f"模板不存在：{template_path}", file=sys.stderr)
        return 2

    zip_path = Path(args.zip)
    if not zip_path.is_file():
        print(f"压缩包不存在：{zip_path}", file=sys.stderr)
        return 2

    table, page_count, total_size = page_table(Path(args.pages_dir))
    commit = args.commit
    mapping = {
        "VERSION": args.version,
        "STAMP": args.stamp,
        "DATE": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "ZIP_NAME": zip_path.name,
        "ZIP_SIZE": human_size(zip_path.stat().st_size),
        "SHA256": sha256_of(zip_path),
        "PAGE_COUNT": str(page_count),
        "PAGE_TABLE": table,
        "TOTAL_SIZE": human_size(total_size),
        "COMMIT": commit,
        "COMMIT_SHORT": commit[:7],
        "REPO_URL": args.repo,
        "WIKI_URL": args.wiki,
        "WORKFLOW_URL": args.workflow,
    }

    text = template_path.read_text(encoding="utf-8")
    for key, value in mapping.items():
        text = text.replace(f"{{{{{key}}}}}", value)

    leftover = sorted(set(PLACEHOLDER_RE.findall(text)))
    if leftover:
        print(f"模板里还有没替换的占位符：{', '.join(leftover)}", file=sys.stderr)
        return 3

    Path(args.out).write_text(text, encoding="utf-8", newline="\n")
    print(f"[notes] {args.out}  {len(text.splitlines())} 行 · {page_count} 页 · {mapping['ZIP_SIZE']} · {mapping['SHA256'][:16]}…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
