@echo off
setlocal
chcp 65001 >nul
title AutoOps Bastion

rem ============================================================
rem  AutoOps Bastion launcher (Windows / Windows Server)
rem  This file is intentionally ASCII-only: cmd.exe renders text in the
rem  console codepage (936/GBK on Chinese Windows), so UTF-8 Chinese here
rem  would show up as mojibake. Chinese notes live in README-FIRST.txt.
rem ============================================================

rem --- Why these three lines matter -------------------------------------------
rem After packaging, app\config.py computes BASE_DIR = parent of the module file,
rem which lives inside _internal\. So the default frontend path and the default
rem data path would both be wrong; point them back to the unpacked folder.
set "BASTION_INSTANCE_DIR=%~dp0instance"
set "BASTION_FRONTEND_DIST=%~dp0frontend"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

if not exist "%BASTION_INSTANCE_DIR%" mkdir "%BASTION_INSTANCE_DIR%"

echo.
echo   AutoOps Bastion (Chinese notes: README-FIRST.txt)
echo   ------------------------------------------------------------
echo   web console : http://127.0.0.1:5000
echo   ssh gateway : port 2222
echo   data dir    : %BASTION_INSTANCE_DIR%
echo   frontend    : %BASTION_FRONTEND_DIST%
echo   default user: admin / admin123   (change it right after login)
echo   ------------------------------------------------------------
echo   Keep this window open while you use it; Ctrl+C stops the service.
echo.

rem Open the browser a few seconds later; a failure here must not stop the server.
start "" /b cmd /c "timeout /t 3 /nobreak >nul & start http://127.0.0.1:5000"

"%~dp0AutoOps-Bastion.exe" %*
set "EXITCODE=%ERRORLEVEL%"

echo.
echo   Service stopped (exit code %EXITCODE%).
pause
endlocal
