"""把扩展模块源码目录打成 ``.mod.ops_ext``。

用法::

    python tools/pack_module.py <源码目录> [-o <输出目录>] [--check]

``--check`` 与安装期走完全相同的 ``spec.validate``，但只写临时 zip、不产出文件；
打包成功后会再对生成的 zip 跑一次同样的校验，避免「目录里能过、压完不能装」。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.modules import spec  # noqa: E402


def _app_version() -> str:
    return os.environ.get("BASTION_APP_VERSION", "1.0.0")


def _force_utf8_stdout() -> None:
    """把标准输出/错误切到 UTF-8。

    Windows 的英文控制台默认是 cp1252，打一行「模块：hello-bastion 1.1.1」就会抛
    ``UnicodeEncodeError`` 直接退出（CI 的 windows-latest 就是这样，中文 Windows 反而看不出来）。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):  # 控制台被重定向到奇怪的对象时不要因此失败
            pass


def _default_output_dir() -> Path:
    # 仓库根目录的 modules-dist/，与 docs/modules/CONTRACT.md §8 一致。
    return BACKEND_DIR.parent / "modules-dist"


def _build_zip(source: Path, destination: Path) -> tuple[int, int]:
    count = 0
    total = 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for relative, full in spec.iter_source_files(source):
            archive.write(full, arcname=relative)
            count += 1
            total += full.stat().st_size
    return count, total


def _validate_zip(path: Path) -> dict:
    return spec.validate(path, app_version=_app_version())


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="打包 AutoOps 扩展模块（.mod.ops_ext）"
    )
    parser.add_argument("source", help="模块源码目录（必须含 description.json）")
    parser.add_argument(
        "-o",
        "--output",
        default=str(_default_output_dir()),
        help="输出目录（默认仓库根 modules-dist/）",
    )
    parser.add_argument(
        "--check", action="store_true", help="只校验，不生成 .mod.ops_ext"
    )
    args = parser.parse_args(argv)

    source = Path(args.source).resolve()
    if not source.is_dir():
        print(f"错误：源码目录不存在：{source}", file=sys.stderr)
        return 2
    description_path = source / spec.DESCRIPTION_NAME
    if not description_path.is_file():
        print(f"错误：缺少 {description_path}", file=sys.stderr)
        return 2

    workdir = Path(tempfile.mkdtemp(prefix="bastion-pack-"))
    temp_zip = workdir / "package.mod.ops_ext"
    output_path: Path | None = None
    try:
        try:
            raw, _ = spec.read_description_file(description_path)
            description = spec.normalize_description(
                raw, app_version=_app_version()
            )
        except spec.ModulePackageError as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return 1

        module_id = description["id"]
        version = description["version"]
        filename = f"{module_id}-{version}.mod.ops_ext"

        try:
            entry_count, source_bytes = _build_zip(source, temp_zip)
            # 对最终 zip 再校验一次；目录扫描阶段的校验不能代替它。
            _validate_zip(temp_zip)
        except spec.ModulePackageError as exc:
            print(f"校验失败：{exc}", file=sys.stderr)
            return 1
        except OSError as exc:
            print(f"打包失败：{exc}", file=sys.stderr)
            return 1

        if args.check:
            print(f"模块：{module_id} {version}")
            print(f"条目：{entry_count}")
            print("校验：通过（--check 未生成文件）")
            return 0

        output_dir = Path(args.output).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / filename
        # 先写同目录临时文件再 os.replace，避免覆盖到一半留下坏包。
        partial = output_dir / f".{filename}.part"
        shutil.copyfile(temp_zip, partial)
        os.replace(partial, output_path)

        # 打包后对“最终文件”再跑一次 validate（而不是刚写的临时 zip）。
        _validate_zip(output_path)
        size = output_path.stat().st_size
        print(f"模块：{module_id} {version}")
        print(f"大小：{size} 字节")
        print(f"条目：{entry_count}")
        print(f"输出：{output_path}")
        print("校验：通过")
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        if output_path is not None:
            partial = output_path.parent / f".{output_path.name}.part"
            if partial.exists():
                partial.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
