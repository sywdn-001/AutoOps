════════════════════════════════════════════════════════════════════
 AutoOps 堡垒机 · Windows 免安装包（PyInstaller onedir）
 AutoOps Bastion · portable Windows build (PyInstaller onedir)
════════════════════════════════════════════════════════════════════

【这是什么 / What is this】
  解压即用的 Windows 版本：目录里已经带好 Python 运行时、后端依赖与前端页面，
  目标机器上**不需要**先装 Python 或 Node。双击 `start-autoops.bat` 就能跑。

  A self-contained Windows build: the Python runtime, backend dependencies and
  the built web UI are all inside this folder. No Python or Node needed on the
  target machine. Just double-click `start-autoops.bat`.

【怎么用 / How to run】
  1. 把整个目录解压到任意位置（路径尽量不要有空格；不要只解压 exe 一个文件）。
  2. 双击 `start-autoops.bat`。
  3. 浏览器会自动打开 http://127.0.0.1:5000 ；没自动开就手动访问。
  4. 用 `admin / admin123` 登录，**登录后第一件事是改口令**。
  5. 使用期间不要关那个黑窗口（关了服务就停了）。按 Ctrl+C 停止。

  Default account: `admin / admin123` — change the password right after the
  first login. Keep the console window open while using the service.

【端口 / Ports】
  Web 控制台 5000，SSH 网关 2222（`ssh -p 2222 <堡垒机账号>@<这台机器的IP>`）。
  端口被占用时，可以在命令行里这样换端口：

      start-autoops.bat --port 8080 --gateway-port 2222

  Web console: 5000. SSH gateway: 2222. Pass `--port` / `--gateway-port` to
  change them; run `AutoOps-Bastion.exe --help` for every option.

【数据放在哪 / Where the data lives】
  `instance\` 目录：`bastion.db`（SQLite 全部数据）+ `secret.key`、
  `fernet.key`（加密资产口令）、`gateway_host_rsa.key`（SSH 网关主机密钥）。
  **备份就是把这个目录整个拷走**；这三把密钥丢了，库里存的资产口令就解不开了。

  Everything is in `instance\`. Backing up means copying that folder. If the
  key files are lost, the stored asset passwords can no longer be decrypted.

【想让它开机自启 / Run as a service】
  用「任务计划程序」，触发器选「计算机启动时」，操作指到
  `start-autoops.bat`，并勾选「不管用户是否登录都要运行」。
  生产环境更推荐在 Linux 上按仓库 README 的部署章节跑。

  On Windows, use Task Scheduler with a "At startup" trigger pointing at
  `start-autoops.bat`. For production, the Linux deployment in the repository
  README is the recommended path.

【附件里的 Python / Node 安装包 / Bundled runtime installers】
  Release 页面上还挂着 Python 与 Node 的官方安装包，给需要在这台机器上
  做二次开发、或想用源码方式跑的人用，**跑本目录不需要它们**。
  - Python 3.15.0：x64（`-amd64.exe`）与 x32（不带后缀的 `.exe`）
  - Node.js v26.11.1：只有 x64（官方没有发布 32 位 msi）

  The Python and Node installers attached to the release are for people who
  want to hack on the source on the same machine — this folder does not need
  them. Node has no 32-bit installer because upstream does not publish one.

【许可 / License】
  MIT，见同目录 `LICENSE`；版权归 sywdn-001。
  项目主页 / Docs / Issues：https://github.com/sywdn-001/AutoOps
