#!/usr/bin/env python3
"""把 Release 说明模板 + 本次真实附件，合成 GitHub Release 的正文（中英双语）。

做三件事：
  1. 扫 `--assets-dir`，按文件名认出每个附件是什么（两个 onedir 压缩包、
     Python 的 x64/x32 安装包、Node 的 x64 安装包、SHA256SUMS.txt），
     生成一张中英对照的表（平台 / 大小 / 说明）；
  2. 从 `SHA256SUMS.txt` 里读回校验和（没有也照样能生成，只是正文少一行总数）；
  3. 把 `--template`（`.github/RELEASE_README.md`）里的 `{{占位符}}` 全部替换掉，
     写 `--out`。**有占位符没被替换掉就直接报错退出**，避免发出去一篇带 {{...}} 的说明。

用法（在工作流里就是这样调的）::

    python3 .github/scripts/make_release_notes.py \\
        --template .github/RELEASE_README.md --assets-dir release \\
        --out release-notes.md --version v1.0.0 --commit "$GITHUB_SHA" \\
        --repo https://github.com/sywdn-001/AutoOps \\
        --workflow https://github.com/sywdn-001/AutoOps/actions/runs/123
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path

# 文件名特征 → (排序号, 平台(中), 平台(英), 说明(中), 说明(英))
CATALOG: list[tuple[str, int, str, str, str, str]] = [
    (
        "windows-server-x64",
        10,
        "Windows / Windows Server x64（64 位）",
        "Windows / Windows Server x64",
        "免安装目录包：内含 Python 运行时、后端依赖与前端页面，解压双击 `start-autoops.bat` 即用",
        "Portable onedir bundle: Python runtime, backend deps and the built web UI included; unzip and run `start-autoops.bat`",
    ),
    (
        "windows-server-x32",
        20,
        "Windows / Windows Server x32（32 位）",
        "Windows / Windows Server x32",
        "同上，给 32 位系统用（老 Server 2003 / XP 时代机器）",
        "Same as above, for 32-bit systems",
    ),
    (
        "installer-python",
        30,
        "Windows 安装包（Python）",
        "Windows installer (Python)",
        "官方 Python 安装包（改用源码方式跑时才需要；zip 包不需要）",
        "Official Python installer — only needed if you run from source; the zip does not need it",
    ),
    (
        "installer-nodejs",
        40,
        "Windows 安装包（Node.js）",
        "Windows installer (Node.js)",
        "官方 Node.js 安装包（只提供 x64，上游没有 32 位 msi）",
        "Official Node.js installer (x64 only — upstream publishes no 32-bit msi)",
    ),
    (
        "SHA256SUMS",
        90,
        "校验和",
        "Checksums",
        "本次全部附件的 SHA256 清单",
        "SHA256 list of every asset in this release",
    ),
]

PLACEHOLDER_RE = re.compile(r"\{\{([A-Z_]+)\}\}")


def human_size(num_bytes: int) -> str:
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def read_checksums(assets_dir: Path) -> dict[str, str]:
    """读 SHA256SUMS.txt：一行 `hash  filename`。"""
    table: dict[str, str] = {}
    for candidate in ("SHA256SUMS.txt", "SHA256SUMS"):
        path = assets_dir / candidate
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.split()
            if len(parts) >= 2 and len(parts[0]) == 64:
                table[parts[-1].lstrip("*")] = parts[0]
    return table


def describe(name: str) -> tuple[int, str, str, str, str]:
    for token, order, zh_platform, en_platform, zh_note, en_note in CATALOG:
        if token in name:
            if token == "installer-python":
                zh_platform += "（x64 64 位）" if "x64" in name else "（x32 32 位）"
                en_platform += " (x64 64-bit)" if "x64" in name else " (x32 32-bit)"
            return order, zh_platform, en_platform, zh_note, en_note
    return 99, "其它 / Other", "Other", "—", "—"


def build_table(assets: list[Path], checksums: dict[str, str]) -> tuple[str, int, int]:
    rows = []
    for path in assets:
        order, zh_platform, en_platform, zh_note, en_note = describe(path.name)
        rows.append((order, path, zh_platform, en_platform, zh_note, en_note))
    rows.sort(key=lambda item: (item[0], item[1].name))

    lines = [
        "| 文件 / File | 平台 / Platform | 大小 / Size | 说明 / Notes |",
        "| --- | --- | --- | --- |",
    ]
    total = 0
    for _order, path, zh_platform, en_platform, zh_note, en_note in rows:
        size = path.stat().st_size
        total += size
        lines.append(
            f"| `{path.name}` | {zh_platform}<br />{en_platform} | {human_size(size)} "
            f"| {zh_note}<br />{en_note} |"
        )
    if checksums:
        lines.append("")
        lines.append(
            f"> 每个附件的 SHA256 见 `SHA256SUMS.txt`（共 {len(checksums)} 条）；"
            "校验方法见下面的「校验下载」。"
        )
        lines.append(
            f"> Every asset's SHA256 is in `SHA256SUMS.txt` ({len(checksums)} entries); see *Verify the download*."
        )
    return "\n".join(lines), len(rows), total


def main() -> int:
    parser = argparse.ArgumentParser(description="生成中英双语的 Release 说明")
    parser.add_argument("--template", required=True)
    parser.add_argument("--assets-dir", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--commit", default="")
    parser.add_argument("--repo", default="https://github.com/sywdn-001/AutoOps")
    parser.add_argument("--workflow", default="")
    parser.add_argument("--python-installer-version", default="3.15.0")
    parser.add_argument("--node-installer-version", default="26.11.1")
    parser.add_argument("--date", default="", help="发布日期，默认取当前 UTC 日期")
    args = parser.parse_args()

    template_path = Path(args.template)
    assets_dir = Path(args.assets_dir)
    if not template_path.exists():
        print(f"找不到模板：{template_path}", file=sys.stderr)
        return 2
    if not assets_dir.is_dir():
        print(f"找不到附件目录：{assets_dir}", file=sys.stderr)
        return 2

    assets = sorted(p for p in assets_dir.iterdir() if p.is_file() and p.suffix in (".zip", ".exe", ".msi", ".txt"))
    if not assets:
        print(f"{assets_dir} 里没有任何附件，拒绝生成空说明", file=sys.stderr)
        return 2

    checksums = read_checksums(assets_dir)
    table, count, total_bytes = build_table(assets, checksums)

    build_date = args.date or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    values = {
        "VERSION": args.version,
        "BUILD_DATE": build_date,
        "COMMIT": args.commit,
        "COMMIT_SHORT": args.commit[:7] if args.commit else "-",
        "ASSET_TABLE": table,
        "ASSET_COUNT": str(count),
        "TOTAL_SIZE": human_size(total_bytes),
        "REPO_URL": args.repo.rstrip("/"),
        "WORKFLOW_URL": args.workflow or f"{args.repo.rstrip('/')}/actions",
        "PYTHON_INSTALLER_VERSION": args.python_installer_version,
        "NODE_INSTALLER_VERSION": args.node_installer_version,
    }

    text = template_path.read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    # [中文](#autoops-堡垒机-version) 这类自锚点跟着版本号变，这里一起改掉
    text = text.replace("[中文](#autoops-堡垒机-version)", f"[中文](#autoops-堡垒机-{args.version.lower().replace('.', '')})")

    leftover = sorted(set(PLACEHOLDER_RE.findall(text)))
    if leftover:
        print("模板里还有没替换的占位符：" + ", ".join(leftover), file=sys.stderr)
        return 3

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8", newline="\n")
    print(f"[notes] {out_path}  {len(text.splitlines())} 行 | 附件 {count} 个 / {human_size(total_bytes)} | 校验和 {len(checksums)} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
