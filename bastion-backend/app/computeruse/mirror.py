"""华为云 Python 镜像：列出「可下载的 Windows amd64 安装包」，并把选中的安装包缓存到堡垒机。

镜像结构（实测 2026-10）：

- ``https://mirrors.huaweicloud.com/python/`` 是 260 个版本目录（``3.13.16/``、``3.15.0/`` …）；
- ``https://mirrors.huaweicloud.com/python/<版本>/`` 里除 ``amd64/``、``arm64/`` 子目录外，
  正式版还有 ``python-<版本>-amd64.exe``（附 ``.asc`` / ``.crt`` / ``.sig`` 签名文件）；
  预发布版（例如 ``3.15.0``）只有 ``amd64a1/`` 之类的 alpha 目录，**没有**顶层安装包。
  所以「列出版本」之后必须再看一眼该版本目录里到底有没有 ``-amd64.exe``，否则用户会选中
  一个下不了东西的版本。

缓存策略：安装包落在 ``<BASTION_INSTANCE_DIR>/computeruse/installers/``，与它同名的
``<文件>.sha256`` 记录校验值（目标机取走前后都能对一遍）。版本清单缓存 1 小时，
避免每次打开部署页都把镜像站扫一遍。
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Callable

import requests

from ..config import INSTANCE_DIR

MIRROR_ROOT = "https://mirrors.huaweicloud.com/python/"
MCP_MIN_VERSION = (3, 13)  # Windows-MCP 要求 Python >= 3.13
LIST_TTL = 3600.0
PARTIAL_TTL = 120.0  # 上一轮没探完的清单，只认这么久的缓存
PROBE_VERSIONS = 16  # 列清单时最多探这么多个最新版本目录
PROBE_DEADLINE = 25.0  # 探测阶段的总预算（秒），超了就先把已有结果给出去
CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 45.0  # 镜像站新连接首包实测要 20~30 秒，读超时必须给够
USER_AGENT = "AutoOps-Bastion/1.0 (+https://github.com/sywdn-001/AutoOps)"

# 复用一条 keep-alive 连接：镜像站新连接要 20~30 秒，而复用连接只要 0.1 秒。
# 探测阶段也因此改成串行（并发会让每个线程各开一条慢连接，反而更慢）。
_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": USER_AGENT, "Connection": "keep-alive"})

COMPUTERUSE_DIR = INSTANCE_DIR / "computeruse"
INSTALLER_DIR = COMPUTERUSE_DIR / "installers"
CACHE_DIR = COMPUTERUSE_DIR / "cache"

_VERSION_RE = re.compile(r'href="(\d+\.\d+\.\d+)/"')
_INSTALLER_RE = re.compile(
    r'<a href="(python-(\d+\.\d+\.\d+)-amd64\.exe)"[^>]*>[^<]*</a>\s*'
    r"(\d{2}-\w{3}-\d{4} \d{2}:\d{2})\s+([\d.]+ (?:B|KB|MB|GB))",
    re.DOTALL,
)

_lock = threading.Lock()
_list_cache: dict | None = None
_list_cached_at = 0.0


class MirrorError(RuntimeError):
    """镜像站访问失败（网络不通、目录不存在、解析不到安装包）。"""


def _fetch(url: str, timeout: float | tuple[float, float] | None = None) -> str:
    try:
        resp = _SESSION.get(
            url,
            timeout=timeout or (CONNECT_TIMEOUT, READ_TIMEOUT),
        )
    except requests.RequestException as exc:  # 网络层错误统一成人话
        raise MirrorError(f"访问镜像站失败：{exc}") from exc
    if resp.status_code != 200:
        raise MirrorError(f"镜像站返回 HTTP {resp.status_code}：{url}")
    return resp.text


def ensure_dirs() -> None:
    INSTALLER_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split(".") if part.isdigit())


def parse_size(text: str) -> int:
    """把 ``28.55 MB`` 这类大小换算成字节。"""
    number, _, unit = text.partition(" ")
    factor = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3}.get(unit.strip().upper(), 1)
    try:
        return int(float(number) * factor)
    except ValueError:
        return 0


def parse_versions(html: str) -> list[str]:
    return sorted({m.group(1) for m in _VERSION_RE.finditer(html)}, key=version_tuple, reverse=True)


def parse_installers(html: str, version: str) -> list[dict]:
    """从某个版本目录页里挑出 ``python-<版本>-amd64.exe``。"""
    items = []
    for match in _INSTALLER_RE.finditer(html):
        name, ver, released, size = match.groups()
        if ver != version:
            continue
        items.append(
            {
                "filename": name,
                "version": ver,
                "url": f"{MIRROR_ROOT}{version}/{name}",
                "size": parse_size(size),
                "released": released,
            }
        )
    return items


def _cache_file(version: str) -> Path:
    return CACHE_DIR / f"installer-{version}.json"


def installer_meta(version: str, *, refresh: bool = False) -> dict | None:
    """查某个版本的 amd64 安装包（结果落盘缓存 1 小时）。"""
    ensure_dirs()
    path = _cache_file(version)
    if not refresh and path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if time.time() - float(payload.get("checkedAt", 0)) < LIST_TTL:
                return payload.get("installer")
        except (OSError, ValueError):
            pass
    url = f"{MIRROR_ROOT}{version}/"
    try:
        html = _fetch(url)
        found = parse_installers(html, version)
    except MirrorError:
        found = []
    installer = found[0] if found else None
    try:
        path.write_text(
            json.dumps({"checkedAt": time.time(), "installer": installer}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass
    return installer


def mcp_compatible(version: str) -> bool:
    return version_tuple(version) >= MCP_MIN_VERSION


def local_installer_path(filename: str) -> Path:
    return INSTALLER_DIR / Path(filename).name


def read_sha256(filename: str) -> str:
    side = local_installer_path(filename).with_suffix(local_installer_path(filename).suffix + ".sha256")
    if side.exists():
        try:
            return side.read_text(encoding="utf-8").strip().split()[0]
        except (OSError, ValueError):
            return ""
    return ""


def cached_state(filename: str) -> dict | None:
    """本地已经缓存好这份安装包了吗？返回大小与校验值。"""
    path = local_installer_path(filename)
    if not path.exists() or path.stat().st_size <= 0:
        return None
    meta_file = local_installer_path(f"{filename}.meta.json")
    meta: dict = {}
    if meta_file.exists():
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
    return {
        "filename": filename,
        "size": path.stat().st_size,
        "sha256": read_sha256(filename),
        "cachedAt": meta.get("cachedAt"),
        "version": meta.get("version", ""),
    }


def _decorate(installer: dict) -> dict:
    cached = cached_state(installer["filename"])
    return {
        **installer,
        "mcpCompatible": mcp_compatible(installer["version"]),
        "cached": bool(cached),
        "cachedAt": (cached or {}).get("cachedAt"),
        "sha256": (cached or {}).get("sha256", ""),
        "localSize": (cached or {}).get("size", 0),
    }


def list_versions(*, refresh: bool = False, probe: int = PROBE_VERSIONS) -> dict:
    """列出版本（附 amd64 安装包信息）。只返回**真的有顶层 amd64 安装包**的版本。

    镜像站新连接首包要 20~30 秒，所以这里全程复用一条 keep-alive 连接、串行探目录；
    探测阶段有总预算 ``PROBE_DEADLINE``，超预算就先交已经探到的结果（``partial=True``），
    这种不完整的清单只缓存 ``PARTIAL_TTL`` 秒，避免把半份结果锁死一小时。
    """
    global _list_cache, _list_cached_at
    ensure_dirs()
    with _lock:
        if not refresh and _list_cache:
            age = time.time() - _list_cached_at
            ttl = PARTIAL_TTL if _list_cache.get("partial") else LIST_TTL
            if age < ttl:
                return _list_cache

    html = _fetch(MIRROR_ROOT)
    versions = parse_versions(html)
    # 先挑出最新的一批候选（Windows-MCP 兼容的排前面），再逐个探目录里有没有安装包
    candidates = [v for v in versions if mcp_compatible(v)][:probe]
    candidates += [v for v in versions if not mcp_compatible(v)][: max(0, probe - len(candidates))]

    items: list[dict] = []
    deadline = time.monotonic() + PROBE_DEADLINE
    partial = False
    for version in candidates:
        if time.monotonic() > deadline:
            partial = True
            break
        installer = installer_meta(version, refresh=refresh)
        if installer:
            items.append(_decorate(installer))
    items.sort(key=lambda item: version_tuple(item["version"]), reverse=True)

    payload = {
        "mirror": MIRROR_ROOT,
        "minPython": ".".join(str(part) for part in MCP_MIN_VERSION),
        "fetchedAt": time.time(),
        "partial": partial,
        "items": items,
    }
    with _lock:
        _list_cache = payload
        _list_cached_at = time.time()
    return payload


def prewarm() -> None:
    """后台预热：把版本清单先拉进内存缓存，用户打开向导时就不用等首包那 30 秒。"""
    try:
        list_versions()
    except Exception:  # 预热失败不影响任何业务，等用户真的点开时再报错
        pass


def cache_installer(
    version: str,
    filename: str,
    *,
    on_progress: Callable[[int, int], None] | None = None,
    cancel: Callable[[], bool] | None = None,
) -> dict:
    """把安装包从镜像站拉到堡垒机本地（流式写入，边下边报进度）。

    返回 ``{path, filename, size, sha256, version, skipped}``；已经缓存过的直接返回
    （``skipped=True``），不重复下载。
    """
    ensure_dirs()
    safe_name = Path(filename).name
    if not safe_name.startswith("python-") or not safe_name.endswith("-amd64.exe"):
        raise MirrorError(f"安装包名字不像 Python amd64 安装包：{filename}")
    existing = cached_state(safe_name)
    if existing and existing["size"] > 0:
        return {**existing, "path": str(local_installer_path(safe_name)), "skipped": True}

    url = f"{MIRROR_ROOT}{version}/{safe_name}"
    target = local_installer_path(safe_name)
    tmp = target.with_suffix(target.suffix + ".part")
    import hashlib

    digest = hashlib.sha256()
    written = 0
    try:
        with _SESSION.get(
            url, stream=True, timeout=(CONNECT_TIMEOUT, 120.0)
        ) as resp:
            if resp.status_code != 200:
                raise MirrorError(f"下载安装包失败：HTTP {resp.status_code}（{url}）")
            total = int(resp.headers.get("Content-Length") or 0)
            with tmp.open("wb") as handle:
                for chunk in resp.iter_content(chunk_size=1024 * 256):
                    if cancel and cancel():
                        raise MirrorError("下载已取消")
                    if not chunk:
                        continue
                    handle.write(chunk)
                    digest.update(chunk)
                    written += len(chunk)
                    if on_progress:
                        on_progress(written, total)
    except requests.RequestException as exc:
        tmp.unlink(missing_ok=True)
        raise MirrorError(f"下载安装包失败：{exc}") from exc

    tmp.replace(target)
    sha = digest.hexdigest()
    local_installer_path(f"{safe_name}.sha256").write_text(f"{sha}  {safe_name}\n", encoding="utf-8")
    local_installer_path(f"{safe_name}.meta.json").write_text(
        json.dumps(
            {"version": version, "cachedAt": time.time(), "size": written, "sha256": sha},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return {
        "path": str(target),
        "filename": safe_name,
        "size": written,
        "sha256": sha,
        "version": version,
        "cachedAt": time.time(),
        "skipped": False,
    }
