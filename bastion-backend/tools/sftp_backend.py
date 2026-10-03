"""服务端 SFTP 子系统（开发 / 演示 / 测试用）：把一个本地目录当成远端根目录。

为什么要有这个文件：文件管理器（SFTP）的验收标准是「真机上浏览、上传、下载、
改名、删除、改权限都成立，而且每一步都进审计」——只有**真正的 SFTP 服务端**
（paramiko ``SFTPServer`` + 文件后端）才能证明这一点，mock 只能证明代码自洽。

演示目标机（``tools/demo_ssh_target.py``）与集成测试（``tests/``）共用这一份实现，
避免「测试里能过、演示机却连不上」这种两套实现的老问题。

路径语义：客户端看到的是以 ``/`` 为根的 POSIX 路径（``canonicalize`` 会归一化），
``..`` 允许但解析后必须仍落在根目录内，否则 ``EACCES``——真机的 chroot 也是这个行为。
"""

from __future__ import annotations

import errno
import os
import shutil

import paramiko


class FileBackedSFTPHandle(paramiko.SFTPHandle):
    """单个已打开文件的句柄：stat / chattr 直接落到真实文件描述符上。"""

    def stat(self):
        try:
            return paramiko.SFTPAttributes.from_stat(os.fstat(self.readfile.fileno()))
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def chattr(self, attr):
        try:
            paramiko.SFTPServer.set_file_attr(self.filename, attr)
            return paramiko.SFTP_OK
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)


class FileBackedSFTPServer(paramiko.SFTPServerInterface):
    """把 ``root`` 当远端 ``/`` 的 SFTP 服务端接口实现。"""

    def __init__(self, server, root=".", on_end=None, **_kwargs):  # noqa: ANN001
        super().__init__(server)
        self.root = os.path.abspath(str(root))
        self.on_end = on_end
        os.makedirs(self.root, exist_ok=True)

    # -- 路径 ---------------------------------------------------------------
    def _real(self, path: str) -> str:
        clean = str(path or "/").replace("\\", "/")
        if not clean.startswith("/"):
            clean = "/" + clean
        parts = []
        for piece in clean.split("/"):
            if piece in ("", "."):
                continue
            if piece == "..":
                if parts:
                    parts.pop()
                continue
            parts.append(piece)
        real = os.path.abspath(os.path.join(self.root, *parts))
        if real != self.root and not real.startswith(self.root + os.sep):
            raise OSError(errno.EACCES, "path escapes sftp root")
        return real

    def canonicalize(self, path):
        clean = str(path or "/").replace("\\", "/")
        if not clean.startswith("/"):
            clean = "/" + clean
        parts = []
        for piece in clean.split("/"):
            if piece in ("", "."):
                continue
            if piece == "..":
                if parts:
                    parts.pop()
                continue
            parts.append(piece)
        return "/" + "/".join(parts)

    # -- 只读 ---------------------------------------------------------------
    def list_folder(self, path):
        real = self._real(path)
        try:
            names = sorted(os.listdir(real))
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)
        entries = []
        for name in names:
            try:
                st = os.lstat(os.path.join(real, name))
            except OSError:
                continue
            attr = paramiko.SFTPAttributes.from_stat(st)
            attr.filename = name
            entries.append(attr)
        return entries

    def stat(self, path):
        try:
            return paramiko.SFTPAttributes.from_stat(os.stat(self._real(path)))
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def lstat(self, path):
        try:
            return paramiko.SFTPAttributes.from_stat(os.lstat(self._real(path)))
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def readlink(self, path):
        try:
            return os.readlink(self._real(path))
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    # -- 读写 ---------------------------------------------------------------
    def open(self, path, flags, attr):
        real = self._real(path)
        try:
            binary_flag = getattr(os, "O_BINARY", 0)
            mode = getattr(attr, "st_mode", None) or 0o666
            fd = os.open(real, flags | binary_flag, mode)
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)
        fstr = "wb" if flags & os.O_WRONLY else "rb"
        try:
            stream = os.fdopen(fd, fstr)
        except OSError as exc:  # pragma: no cover - fdopen 极少失败
            os.close(fd)
            return paramiko.SFTPServer.convert_errno(exc.errno)
        handle = FileBackedSFTPHandle(flags)
        handle.filename = real
        handle.readfile = stream
        handle.writefile = stream
        return handle

    def remove(self, path):
        try:
            os.remove(self._real(path))
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)
        return paramiko.SFTP_OK

    def rename(self, oldpath, newpath):
        try:
            os.rename(self._real(oldpath), self._real(newpath))
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)
        return paramiko.SFTP_OK

    def mkdir(self, path, attr):
        try:
            mode = (attr.st_mode & 0o777) if attr is not None and attr.st_mode else 0o777
            os.mkdir(self._real(path), mode)
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)
        return paramiko.SFTP_OK

    def rmdir(self, path):
        try:
            os.rmdir(self._real(path))
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)
        return paramiko.SFTP_OK

    def chattr(self, path, attr):
        try:
            paramiko.SFTPServer.set_file_attr(self._real(path), attr)
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)
        return paramiko.SFTP_OK

    def symlink(self, target_path, path):
        try:
            os.symlink(target_path, self._real(path))
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)
        return paramiko.SFTP_OK

    def session_ended(self):
        if self.on_end is not None:
            self.on_end()


def install_sftp_subsystem(transport: paramiko.Transport, root, *, on_end=None) -> None:
    """给 transport 挂上 ``sftp`` 子系统（配合 ServerInterface 的
    ``check_channel_subsystem_request`` 返回 True）。"""
    transport.set_subsystem_handler(
        "sftp",
        paramiko.SFTPServer,
        sftp_si=FileBackedSFTPServer,
        root=os.path.abspath(str(root)),
        on_end=on_end,
    )


DEMO_FILES: dict[str, str] = {
    "/readme.txt": (
        "AutoOps 演示目标机文件区\n"
        "========================\n"
        "这个目录由 SFTP 子系统的文件后端提供，用于验证文件管理器的\n"
        "浏览、上传、下载、重命名、移动、复制、删除、改权限与打包下载。\n"
    ),
    "/docs/notes.txt": "运维笔记\n- 每日 06:00 巡检\n- 备份目录 /data/backup\n",
    "/docs/deploy.md": "# 发布流程\n\n1. 灰度\n2. 全量\n3. 回滚演练\n",
    "/logs/app.log": "2026-10-01 09:00:01 INFO  服务启动\n2026-10-01 09:00:02 WARN  磁盘使用率 78%\n",
    "/data/report.csv": "host,cpu,mem\nweb-01,12%,38%\ndb-01,45%,71%\n",
    "/data/backup/README": "备份文件目录（演示用）\n",
    "/.ssh/id_rsa": "-----BEGIN OPENSSH PRIVATE KEY-----\n演示用私钥占位，文件策略应拦截读取与下载\n-----END OPENSSH PRIVATE KEY-----\n",
    "/.ssh/authorized_keys": "ssh-rsa AAAAB3NzaC1yc2E demo\n",
}


def seed_demo_root(root) -> None:
    """演示根目录不存在内容时铺一批样例文件（已存在则不覆盖）。"""
    root = os.path.abspath(str(root))
    os.makedirs(root, exist_ok=True)
    for path, content in DEMO_FILES.items():
        target = os.path.join(root, path.lstrip("/"))
        if os.path.exists(target):
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(content)
    os.makedirs(os.path.join(root, "data", "backup"), exist_ok=True)
    # 让演示目录里有一份「大一点的文件」，方便验证下载与打包的体积统计
    big = os.path.join(root, "data", "big.bin")
    if not os.path.exists(big):
        with open(big, "wb") as handle:
            handle.write(b"\x00\x01\x02\x03" * 262144)  # 1 MiB
    if not os.path.exists(os.path.join(root, "tmp")):
        os.makedirs(os.path.join(root, "tmp"), exist_ok=True)


def reset_demo_root(root) -> None:
    """清空并重建演示根目录（测试用）。"""
    root = os.path.abspath(str(root))
    if os.path.isdir(root):
        shutil.rmtree(root)
    seed_demo_root(root)
