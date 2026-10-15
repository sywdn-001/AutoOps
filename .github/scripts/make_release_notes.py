#!/usr/bin/env python3
"""把 Release 说明模板 + 本次真实附件，合成 GitHub Release 的正文（中英双语）。

做四件事：
  1. 扫 `--assets-dir`，按文件名认出每个附件是什么（两个 onedir 压缩包、
     Python 的 x64/x32 安装包、Node 的 x64 安装包、`*-Source-Code.tar.gz` /
     `*-Source-Code.zip` 源码附件、SHA256SUMS.txt），生成一张中英对照的表（平台 / 大小 / 说明）；
  2. 从 `SHA256SUMS.txt` 里读回校验和（没有也照样能生成，只是正文少一行总数）；
  3. 按 `--channel`（stable / rc）与 `--dev-build` 裁模板里的条件块：
     `<!-- if:stable -->…<!-- endif -->` 只在正式版留下，`<!-- if:rc -->` 只在候选版/夜间构建留下，
     `<!-- if:dev -->` 只在 Dev 分支夜间构建留下；同时填 `{{CHANNEL_BANNER_ZH}}` /
     `{{CHANNEL_BANNER_EN}}` 两条醒目提示（正式版填空，候选版/夜间构建写明
     「仅供体验新功能，请勿用于生产环境」）；
  4. 把 `--template`（`.github/RELEASE_README.md`）里的 `{{占位符}}` 全部替换掉，写 `--out`。
     **有占位符没被替换掉就直接报错退出**，避免发出去一篇带 {{...}} 的说明。

用法（在工作流里就是这样调的）::

    python3 .github/scripts/make_release_notes.py \\
        --template .github/RELEASE_README.md --assets-dir release \\
        --out release-notes.md --version v1.1.1 --commit "$GITHUB_SHA" \\
        --repo https://github.com/sywdn-001/AutoOps \\
        --workflow https://github.com/sywdn-001/AutoOps/actions/runs/123

    # Dev 分支的夜间构建（候选版 + 预发布）：
    python3 .github/scripts/make_release_notes.py \\
        --template .github/RELEASE_README.md --assets-dir release --out release-notes.md \\
        --version v1.1.1-RC-nightly.20261016.42 --channel rc --dev-build \\
        --repo https://github.com/sywdn-001/AutoOps
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
        "-Source-Code.tar.gz",
        50,
        "源代码（tar.gz）",
        "Source code (.tar.gz)",
        "本次构建对应的完整源码（`git archive` 自构建提交导出，含全部被跟踪的文件）",
        "The exact source of this build (`git archive` of the built commit, every tracked file included)",
    ),
    (
        "-Source-Code.zip",
        51,
        "源代码（zip）",
        "Source code (.zip)",
        "同上，只是换了个压缩格式（Windows 上双击就能解）",
        "Same as above, zipped (handy on Windows)",
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

# 模板里的条件块：`<!-- if:stable -->…<!-- endif -->`，按本次通道只留命中的那一段。
# 行首那段空白一起吃掉，免得裁完留下一堆空行。
CONDITIONAL_RE = re.compile(
    r"(?ms)^[ \t]*<!--\s*if:([a-z_]+)\s*-->[ \t]*\n(.*?)^[ \t]*<!--\s*endif\s*-->[ \t]*\n?"
)


def strip_conditionals(text: str, conditions: set[str]) -> str:
    """只保留条件命中的 `<!-- if:xxx -->…<!-- endif -->` 块内容（其余整块删掉）。"""

    def _pick(match: re.Match[str]) -> str:
        return match.group(2) if match.group(1) in conditions else ""

    return CONDITIONAL_RE.sub(_pick, text)


def channel_banner(channel: str, dev_build: bool, ctx: dict[str, str]) -> tuple[str, str]:
    """说明最上面那条「这不是正式版」的提示（中 / 英）；正式版返回两个空串。"""
    if channel != "rc":
        return "", ""
    if dev_build:
        zh = (
            "> ⚠️ **这是 Dev 分支的夜间构建产物（Nightly Build），版本 `{version}` —— "
            "也就是下一版的候选版（RC），仅供体验新功能，请勿用于生产环境。**\n"
            ">\n"
            "> 构建于 {date}（UTC），对应提交 [`{commit_short}`]({repo_url}/commit/{commit})；"
            "要上生产环境，请到 [Releases]({repo_url}/releases/latest) 取不带 RC 的正式版。"
        )
        en = (
            "> ⚠️ **This is a Dev-branch nightly build: `{version}`, a release candidate (RC) for the "
            "next version. It is here so you can preview new features, not for production use.**\n"
            ">\n"
            "> Built on {date} (UTC) from commit [`{commit_short}`]({repo_url}/commit/{commit}). "
            "For production, take the non-RC stable release from "
            "[Releases]({repo_url}/releases/latest)."
        )
    else:
        zh = (
            "> ⚠️ **这是候选版（Release Candidate）`{version}`，仅供体验新功能，请勿用于生产环境。**\n"
            ">\n"
            "> 构建于 {date}（UTC），对应提交 [`{commit_short}`]({repo_url}/commit/{commit})；"
            "正式版见 [Releases]({repo_url}/releases/latest)。"
        )
        en = (
            "> ⚠️ **This is a release candidate (RC): `{version}` — preview new features with it, "
            "do not put it into production.**\n"
            ">\n"
            "> Built on {date} (UTC) from commit [`{commit_short}`]({repo_url}/commit/{commit}). "
            "See [Releases]({repo_url}/releases/latest) for the stable version."
        )
    return zh.format(**ctx), en.format(**ctx)


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
    parser.add_argument(
        "--channel",
        choices=("stable", "rc"),
        default="stable",
        help="发布通道：stable 正式版（默认）；rc 候选版/夜间构建（预发布）",
    )
    parser.add_argument(
        "--dev-build",
        action="store_true",
        help="Dev 分支的夜间构建：说明顶部写明「开发分支夜间构建，仅供体验新功能」",
    )
    args = parser.parse_args()

    template_path = Path(args.template)
    assets_dir = Path(args.assets_dir)
    if not template_path.exists():
        print(f"找不到模板：{template_path}", file=sys.stderr)
        return 2
    if not assets_dir.is_dir():
        print(f"找不到附件目录：{assets_dir}", file=sys.stderr)
        return 2

    suffixes = (".zip", ".exe", ".msi", ".txt", ".gz")
    assets = sorted(p for p in assets_dir.iterdir() if p.is_file() and p.suffix in suffixes)
    if not assets:
        print(f"{assets_dir} 里没有任何附件，拒绝生成空说明", file=sys.stderr)
        return 2

    checksums = read_checksums(assets_dir)
    table, count, total_bytes = build_table(assets, checksums)

    build_date = args.date or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    # 先按通道裁模板里的条件块，再替换占位符（裁掉的那段里就算有占位符也不会被算作残留）。
    conditions = {"stable"} if args.channel == "stable" else {"rc"}
    if args.dev_build:
        conditions.add("dev")
    template_text = strip_conditionals(template_path.read_text(encoding="utf-8"), conditions)

    repo_url = args.repo.rstrip("/")
    commit_short = args.commit[:7] if args.commit else "-"
    banner_zh, banner_en = channel_banner(
        args.channel,
        args.dev_build,
        {
            "version": args.version,
            "date": build_date,
            "commit": args.commit,
            "commit_short": commit_short,
            "repo_url": repo_url,
        },
    )
    values = {
        "VERSION": args.version,
        "BUILD_DATE": build_date,
        "COMMIT": args.commit,
        "COMMIT_SHORT": commit_short,
        "ASSET_TABLE": table,
        "ASSET_COUNT": str(count),
        "TOTAL_SIZE": human_size(total_bytes),
        "REPO_URL": repo_url,
        "WORKFLOW_URL": args.workflow or f"{repo_url}/actions",
        "PYTHON_INSTALLER_VERSION": args.python_installer_version,
        "NODE_INSTALLER_VERSION": args.node_installer_version,
        # 放最后：这两条是整段文本（里面已经填好真实值），排在最后最保险。
        "CHANNEL_BANNER_ZH": banner_zh,
        "CHANNEL_BANNER_EN": banner_en,
    }

    text = template_text
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
    print(
        f"[notes] {out_path}  {len(text.splitlines())} 行 | 附件 {count} 个 / {human_size(total_bytes)} "
        f"| 校验和 {len(checksums)} 条 | 通道 {args.channel}{'（Dev 夜间构建）' if args.dev_build else ''}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
