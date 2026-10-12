# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（onedir 目录包）。

产物结构（发布 zip 的根目录）：:

    AutoOps-Bastion/
    ├── AutoOps-Bastion.exe     # 入口，等价于 python run.py
    ├── _internal/              # Python 运行时与所有依赖（PyInstaller 6.x 布局）
    ├── frontend/               # 前端 dist（流水线里从 ant-design-pro/dist 拷进来）
    ├── instance/               # SQLite + 三把密钥 + 录像，第一次启动自动生成
    ├── start-autoops.bat       # 双击启动（纯 ASCII；显式设好下面三个环境变量）
    ├── README-FIRST.txt        # 中英说明（带 UTF-8 BOM，记事本直接打开不乱码）
    └── BUILD-INFO.txt          # 版本、系统、位数、构建时间、commit

为什么启动脚本必须显式设环境变量：打包后 `app/config.py` 的
`BASE_DIR = Path(__file__).resolve().parent.parent` 会落在 `_internal/`，
于是默认的 `FRONTEND_DIST`（`BASE_DIR.parent/"ant-design-pro"/"dist"`）和
`INSTANCE_DIR`（`BASE_DIR/"instance"`）都不是我们想要的目录，必须由
`BASTION_FRONTEND_DIST` / `BASTION_INSTANCE_DIR` 指回来。

本地也能直接跑（在 `bastion-backend/` 目录下）::

    pip install -r requirements.txt pyinstaller
    python -m PyInstaller packaging/autoops.spec --noconfirm --clean
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

SPEC_DIR = Path(SPECPATH).resolve()
BACKEND_DIR = SPEC_DIR.parent
ENTRY = BACKEND_DIR / "run.py"
NAME = "AutoOps-Bastion"

datas = []
binaries = []
hiddenimports = [
    # Flask-SocketIO 在运行时按名字挑异步驱动，静态分析看不到
    "engineio.async_drivers.threading",
    "engineio.async_drivers._websocket_wsgi",
    "simple_websocket",
    # pywinrm[ntlm]：NTLM 走 requests-ntlm + pyspnego，也是运行时才导入
    "winrm",
    "spnego",
    "requests_ntlm",
]

# 把整包（含子模块与包内数据）收进来，避免冻结后缺模块只在真机上才炸
for package in (
    "flask",
    "flask_cors",
    "flask_jwt_extended",
    "flask_socketio",
    "flask_sqlalchemy",
    "engineio",
    "socketio",
    "simple_websocket",
    "jwt",
    "paramiko",
    "winrm",
    "spnego",
    "requests",
):
    try:
        package_datas, package_binaries, package_hidden = collect_all(package)
    except Exception as exc:  # noqa: BLE001 - 某个包没装不该让整包构建失败
        print(f"[spec] collect_all({package}) 跳过：{exc}")
        continue
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden

hiddenimports += collect_submodules("sqlalchemy.dialects.sqlite")
hiddenimports += ["app"]

# 明确排除本项目从不导入的重量级/桌面栈。
# 理由有两个：① 开发机上「什么库都装过」时，PyInstaller 会为每个能扫到的包找 hook 并分析依赖，
# 本地打包能慢到十几分钟（在干净 venv 里就快得多，见 packaging/build-local.ps1）；
# ② 冻结包里塞进这些库只会让体积翻倍。这里只排「我们确定不用」的，宁少勿多。
EXCLUDES = [
    "tkinter",
    "numpy",
    "pandas",
    "matplotlib",
    "scipy",
    "PIL",
    "IPython",
    "jupyter",
    "notebook",
    "PyQt5",
    "PyQt6",
    "PySide2",
    "PySide6",
    "wx",
    "pip",
    "setuptools",
    "pkg_resources",
    "pytest",
    "_pytest",
    "docx",
    "pptx",
    "openpyxl",
]

analysis = Analysis(
    [str(ENTRY)],
    pathex=[str(BACKEND_DIR)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
)

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name=NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

collect = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=NAME,
)
