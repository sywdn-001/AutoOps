#Requires -Version 5.1
<#
.SYNOPSIS
  在本地打一个 Windows 免安装包，步骤与 CI 的 .github/workflows/release.yml 完全一致。

.DESCRIPTION
  为什么要有这个脚本：PyInstaller 会给「当前环境里能导入的每个包」找 hook 并分析依赖。
  在本机全局 Python（装过一大堆库）里直接打包，会慢到十几分钟甚至更久；所以这里
  **先建一个干净的 venv**（bastion-backend/.venv-pack），只装 requirements.txt + PyInstaller，
  分析范围立刻收敛到项目真实依赖，并且产物更小。

  跑完得到的 bastion-backend/build/dist/AutoOps-Bastion 就是可以直接双击
  start-autoops.bat 运行的目录包；加 -Zip 会顺手压成和 CI 同名的 zip。

.EXAMPLE
  pwsh -File bastion-backend/packaging/build-local.ps1
.EXAMPLE
  # 前端 dist 已经是最新的，跳过 npm 构建
  pwsh -File bastion-backend/packaging/build-local.ps1 -SkipFrontend
.EXAMPLE
  # 打完包就地起一次服务打 /api/health（用 5080 端口、不开网关，绝不碰你在跑的服务）
  pwsh -File bastion-backend/packaging/build-local.ps1 -SkipFrontend -SmokeTest
#>
[CmdletBinding()]
param(
    # 用来创建 venv 的 Python（3.11 及以上；开发用 3.13）
    [string]$Python = "python",
    # 前端 dist 已是最新时跳过 npm ci && npm run build
    [switch]$SkipFrontend,
    # 删掉旧的 .venv-pack 重建（依赖升过级就用这个）
    [switch]$RecreateVenv,
    # 打完压成 zip（与 CI 同名规则）
    [switch]$Zip,
    # 打包后起一次服务打 /api/health，验证产物真的能跑
    [switch]$SmokeTest
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

# ── 路径 ────────────────────────────────────────────────────────────────────
$packaging = Split-Path -Parent $MyInvocation.MyCommand.Path   # bastion-backend/packaging
$backend = Split-Path -Parent $packaging                       # bastion-backend
$repo = Split-Path -Parent $backend                            # 仓库根
$venv = Join-Path $backend ".venv-pack"
$venvPy = Join-Path $venv "Scripts\python.exe"
$distDir = Join-Path $backend "build\dist"
$workDir = Join-Path $backend "build\work"
$bundleName = "AutoOps-Bastion"
$bundle = Join-Path $distDir $bundleName

function Step($index, $total, $text) {
    Write-Host ""
    Write-Host "== $index/$total $text" -ForegroundColor Cyan
}

Write-Host "AutoOps Bastion · 本地打包" -ForegroundColor Green
Write-Host "  仓库      : $repo"
Write-Host "  venv      : $venv"
Write-Host "  输出      : $bundle"

# ── 1. 干净 venv ────────────────────────────────────────────────────────────
Step 1 5 "准备干净 venv（本机库多的时候，这一步决定整体快慢）"
if ($RecreateVenv -and (Test-Path $venv)) {
    Write-Host "  删除旧的 .venv-pack ..."
    Remove-Item $venv -Recurse -Force
}
if (-not (Test-Path $venvPy)) {
    & $Python -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "建 venv 失败：$Python -m venv $venv" }
}
& $venvPy -m pip install --upgrade pip --quiet --disable-pip-version-check
& $venvPy -m pip install --quiet --disable-pip-version-check -r (Join-Path $backend "requirements.txt") "pyinstaller>=6.6"
if ($LASTEXITCODE -ne 0) { throw "在 venv 里装依赖失败" }
& $venvPy -c "import platform, struct, PyInstaller; print('  venv Python %s (%d bit) | PyInstaller %s' % (platform.python_version(), struct.calcsize('P')*8, PyInstaller.__version__))"

# ── 2. 前端 dist ────────────────────────────────────────────────────────────
Step 2 5 "构建前端 dist/"
$frontDist = Join-Path $repo "ant-design-pro\dist"
if ($SkipFrontend) {
    Write-Host "  已跳过（-SkipFrontend）"
} else {
    Push-Location (Join-Path $repo "ant-design-pro")
    try {
        if (-not (Test-Path "node_modules")) { npm ci }
        npm run build
        if ($LASTEXITCODE -ne 0) { throw "前端构建失败（npm run build）" }
    } finally {
        Pop-Location
    }
}
if (-not (Test-Path (Join-Path $frontDist "index.html"))) {
    throw "前端产物不存在：$frontDist\index.html（先跑一次 npm run build，或去掉 -SkipFrontend）"
}

# ── 3. PyInstaller onedir ───────────────────────────────────────────────────
Step 3 5 "PyInstaller 打成 onedir 目录包"
Push-Location $backend
try {
    & $venvPy -m PyInstaller (Join-Path $packaging "autoops.spec") --noconfirm --clean --distpath $distDir --workpath $workDir
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败" }
} finally {
    Pop-Location
}
if (-not (Test-Path (Join-Path $bundle "$bundleName.exe"))) { throw "产物不存在：$bundle\$bundleName.exe" }

# ── 4. 组装发布目录（与 CI 同一步骤）────────────────────────────────────────
Step 4 5 "组装发布目录（前端 + 启动脚本 + 说明）"
New-Item -ItemType Directory -Force -Path (Join-Path $bundle "frontend") | Out-Null
Copy-Item -Recurse -Force (Join-Path $frontDist "*") (Join-Path $bundle "frontend")
Copy-Item -Force (Join-Path $packaging "windows\start-autoops.bat") $bundle
# 说明书带 UTF-8 BOM 重新写一遍：老版本记事本没有 BOM 会把中文当 ANSI 显示成乱码
$first = Get-Content -Raw -Encoding utf8 (Join-Path $packaging "windows\README-FIRST.txt")
[System.IO.File]::WriteAllText((Join-Path $bundle "README-FIRST.txt"), $first, [System.Text.UTF8Encoding]::new($true))
Copy-Item -Force (Join-Path $repo "LICENSE") $bundle
Copy-Item -Force (Join-Path $repo "README.md") $bundle
New-Item -ItemType Directory -Force -Path (Join-Path $bundle "instance") | Out-Null

$commit = (& git -C $repo rev-parse --short HEAD) -join ""
$pyver = (& $venvPy -c "import platform;print(platform.python_version())") -join ""
$pybits = (& $venvPy -c "import struct;print(struct.calcsize('P')*8)") -join ""
$built = (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd HH:mm:ss")
$osKey = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion"
$osName = "Windows"
$osBuild = ""
try {
    $osName = (Get-ItemProperty $osKey).ProductName
    $osBuild = (Get-ItemProperty $osKey).CurrentBuild
} catch { }
$info = @"
项目 / Project   : AutoOps 堡垒机 (AutoOps Bastion)
版本 / Version   : 本地构建（未打 tag）/ local build
系统 / OS        : $osName (build $osBuild)  $([System.Environment]::OSVersion.VersionString)
打包方式 / Build : PyInstaller onedir（前端 dist 已内置 / frontend dist bundled）
构建 Python      : $pyver ($pybits bit)
代码版本 / Commit: $commit
构建时间 / Built : $built UTC
构建脚本 / Script: bastion-backend/packaging/build-local.ps1
"@
Set-Content -Path (Join-Path $bundle "BUILD-INFO.txt") -Value $info -Encoding utf8

# ── 5. 可选：打包 + 烟测 ────────────────────────────────────────────────────
Step 5 5 "收尾"
if ($Zip) {
    $tag = ((& git -C $repo describe --tags --always) -join "").Trim()
    $slug = ($osName.ToLower() -replace "[^a-z0-9]+", "-").Trim("-")
    $zip = Join-Path $distDir "$bundleName-$tag-${slug}-x64-py$pyver-onedir.zip"
    if (Test-Path $zip) { Remove-Item $zip -Force }
    Compress-Archive -Path $bundle -DestinationPath $zip -CompressionLevel Optimal
    Write-Host "  zip : $zip  ($([math]::Round((Get-Item $zip).Length / 1MB, 1)) MB)"
} else {
    Write-Host "  跳过压缩（需要就加 -Zip）"
}

if ($SmokeTest) {
    # 用 5080 + 不开网关：绝对不碰你正在跑的 5000/2222
    $env:BASTION_INSTANCE_DIR = Join-Path $bundle "instance"
    $env:BASTION_FRONTEND_DIST = Join-Path $bundle "frontend"
    $env:PYTHONUTF8 = "1"
    $env:PYTHONIOENCODING = "utf-8"
    $log = Join-Path $distDir "smoke-test.log"
    Write-Host "  起一次服务：$bundleName.exe --port 5080 --no-gateway（日志 $log）"
    $proc = Start-Process -FilePath (Join-Path $bundle "$bundleName.exe") `
        -ArgumentList "--port", "5080", "--no-gateway" `
        -WorkingDirectory $bundle -RedirectStandardOutput $log -RedirectStandardError "$log.err" -PassThru
    $ok = $false
    try {
        foreach ($i in 1..40) {
            Start-Sleep -Milliseconds 750
            try {
                $res = Invoke-RestMethod "http://127.0.0.1:5080/api/health" -TimeoutSec 3
                if ($res.success) {
                    Write-Host "  /api/health → $($res.message) | gateway_enabled=$($res.data.gateway_enabled)" -ForegroundColor Green
                    $ok = $true
                    break
                }
            } catch { }
        }
        if (-not $ok) {
            Write-Host "  40 次探测都没起来，看日志：$log" -ForegroundColor Yellow
            if (Test-Path $log) { Get-Content $log -Tail 20 }
        }
    } finally {
        if (-not $proc.HasExited) { Stop-Process -Id $proc.Id -Force }
    }
    if (-not $ok) { throw "烟测失败：产物打不开或 /api/health 不通" }
}

Write-Host ""
Write-Host "完成 → $bundle" -ForegroundColor Green
Write-Host "  双击 start-autoops.bat 即可运行（默认 http://127.0.0.1:5000，账号 admin/admin123）"
