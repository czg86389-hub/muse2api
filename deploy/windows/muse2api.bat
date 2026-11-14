@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
title muse2api 控制台
rem ============================================================
rem  muse2api Windows 一键控制脚本（启动 / 停止 / 重启 / 状态）
rem  位置: deploy/windows/muse2api.bat，自动定位仓库根目录
rem  依赖: 仓库根目录下已创建 venv（见 README「Windows 裸机部署」）
rem ============================================================
cd /d "%~dp0..\.."
set "M2A_REPO_ROOT=%CD%"

set "SYS=%SystemRoot%\System32"
set "PS=%SYS%\WindowsPowerShell\v1.0\powershell.exe"
set "PYW=venv\Scripts\pythonw.exe"
set "PY=venv\Scripts\python.exe"
set "PORT=18610"
set "BASE=http://127.0.0.1:%PORT%"
set "KEY="
if exist ".env" (
  for /f "usebackq tokens=1,* delims==" %%a in (".env") do (
    if /i "%%a"=="MUSE2API_KEY" set "KEY=%%b"
  )
)

if /i "%~1"=="start" goto :do_start
if /i "%~1"=="stop" goto :do_stop
if /i "%~1"=="restart" goto :do_restart
if /i "%~1"=="status" goto :do_status
if /i "%~1"=="open" goto :do_open
goto :menu

rem ==================== 启动 ====================
:do_start
if not exist "%PY%" goto :no_venv
call :check
if "%RUNNING%"=="1" goto :already
echo 正在启动 muse2api ，请稍候……
rem pythonw.exe 无控制台黑框；stdout/stderr 重定向到 server.log
start "muse2api 服务" /min "%SYS%\cmd.exe" /c ""%PYW%" -m uvicorn app:app --host 127.0.0.1 --port %PORT% >> server.log 2>&1"
set /a tries=0
:wait_loop
set /a tries+=1
"%SYS%\ping.exe" -n 2 127.0.0.1 >nul 2>&1
call :check
if "%RUNNING%"=="1" goto :started
if %tries% GEQ 20 goto :start_fail
goto :wait_loop
:started
echo.
echo   [OK] 启动成功
echo   [OK] 管理面板   %BASE%/admin
echo.
call :do_open
exit /b 0

:already
echo.
echo   [i] 服务已在运行，无需重复启动
echo   [i] 管理面板   %BASE%/admin
echo.
call :do_open
exit /b 0

:start_fail
echo.
echo   [X] 启动失败或超时，请查看根目录 server.log
echo.
exit /b 1

:no_venv
echo.
echo   [X] 找不到虚拟环境 venv，请先在仓库根目录执行：
echo         python -m venv venv
echo         venv\Scripts\python.exe -m pip install -r requirements.txt
echo.
exit /b 1

rem ==================== 停止 ====================
:do_stop
call :check
if "%RUNNING%"=="0" goto :not_running
echo 正在停止 muse2api ……
"%PS%" -NoProfile -ExecutionPolicy Bypass -Command "$c=Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue|Select-Object -First 1; if($c){Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue}" >nul 2>&1
call :cleanup
"%SYS%\ping.exe" -n 3 127.0.0.1 >nul 2>&1
call :check
if "%RUNNING%"=="1" goto :stop_fail
echo   [OK] 已停止
exit /b 0

:not_running
echo   [i] 服务当前未运行
call :cleanup
exit /b 0

:stop_fail
echo   [X] 停止失败，可能需要以管理员身份运行本文件
exit /b 1

rem ==================== 状态 ====================
:do_status
call :check
if "%RUNNING%"=="0" goto :status_off
echo   服务状态: 运行中   %BASE%/admin
"%PS%" -NoProfile -Command "$c=Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue|Select-Object -First 1; if($c){'   进程 PID: '+$c.OwningProcess}; try{$r=Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 '%BASE%/healthz';'   健康检查: HTTP '+$r.StatusCode}catch{'   健康检查: 失败'}"
exit /b 0
:status_off
echo   服务状态: 未运行
exit /b 0

rem ==================== 打开管理面板 ====================
:do_open
if defined KEY goto :open_with_key
start "" "%BASE%/admin"
exit /b 0
:open_with_key
start "" "%BASE%/admin?key=%KEY%"
exit /b 0

rem ==================== 重启 ====================
:do_restart
call :do_stop
call :do_start
exit /b 0

rem ==================== 工具 ====================
:check
set "RUNNING=0"
"%SYS%\netstat.exe" -ano -p tcp | "%SYS%\findstr.exe" /c:":%PORT%" | "%SYS%\findstr.exe" /c:"LISTENING" >nul 2>&1
if not errorlevel 1 set "RUNNING=1"
exit /b 0

:cleanup
rem 清理残留：本仓库浏览器 profile 的浏览器进程 + 本仓库 venv 拉起的 uvicorn 孤儿进程
"%PS%" -NoProfile -ExecutionPolicy Bypass -Command "$root=$env:M2A_REPO_ROOT; Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { (($_.Name -in @('msedge.exe','chrome.exe','chromium.exe')) -and ($_.CommandLine -like '*muse2api-profiles*')) -or (($_.ExecutablePath -like ('*'+$root+'\venv\Scripts\python*')) -and ($_.CommandLine -like '*uvicorn*')) } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1
exit /b 0

rem ==================== 菜单 ====================
:menu
cls
echo ==================================================
echo                 muse2api 控制台
echo ==================================================
echo   目录: %CD%
call :check
if "%RUNNING%"=="1" echo   状态: 运行中   %BASE%/admin
if "%RUNNING%"=="0" echo   状态: 未运行
echo --------------------------------------------------
echo    1. 启动服务
echo    2. 停止服务
echo    3. 重启服务
echo    4. 打开管理面板
echo    5. 查看运行日志
echo    6. 查看详细状态
echo    0. 退出
echo --------------------------------------------------
set "ch="
set /p "ch=请输入序号并回车: "
if errorlevel 1 exit /b 0
if not defined ch goto :menu
if "%ch%"=="1" goto :m_start
if "%ch%"=="2" goto :m_stop
if "%ch%"=="3" goto :m_restart
if "%ch%"=="4" goto :m_open
if "%ch%"=="5" goto :m_log
if "%ch%"=="6" goto :m_status
if "%ch%"=="0" exit /b 0
goto :menu

:m_start
call :do_start
pause
goto :menu

:m_stop
call :do_stop
pause
goto :menu

:m_restart
call :do_restart
pause
goto :menu

:m_open
call :do_open
goto :menu

:m_log
start "" "%SYS%\notepad.exe" "server.log"
goto :menu

:m_status
call :do_status
pause
goto :menu
