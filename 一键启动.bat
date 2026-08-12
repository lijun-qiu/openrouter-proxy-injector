@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title OpenRouter + ModelScope Launcher
chcp 65001 >nul

echo ================================
echo  One-click: OpenRouter + ModelScope
echo ================================
echo.

where python >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Python not found in PATH.
  pause
  exit /b 1
)

if not exist "%~dp0.env" (
  echo [ERROR] Missing .env
  echo Copy .env.sample to .env and fill in OpenRouter keys.
  pause
  exit /b 1
)

if not exist "%~dp0.env.modelscope" (
  echo [ERROR] Missing .env.modelscope
  echo Copy .env.modelscope.sample to .env.modelscope and fill in ModelScope keys.
  pause
  exit /b 1
)

REM Read ModelScope port from .env.modelscope (default 10001)
set "MS_PORT=10001"
for /f "usebackq eol=# tokens=1,* delims==" %%A in ("%~dp0.env.modelscope") do (
  if /i "%%A"=="APP_PORT" if not "%%B"=="" set "MS_PORT=%%B"
)

set "OR_RUNNING=0"
set "MS_RUNNING=0"

powershell -NoProfile -Command "try { $r = Invoke-WebRequest -Uri 'http://127.0.0.1:9999/docs' -UseBasicParsing -TimeoutSec 2; if ($r.StatusCode -eq 200) { exit 0 } else { exit 1 } } catch { exit 1 }" >nul 2>&1
if %errorlevel%==0 set "OR_RUNNING=1"

powershell -NoProfile -Command "try { $r = Invoke-WebRequest -Uri 'http://127.0.0.1:%MS_PORT%/docs' -UseBasicParsing -TimeoutSec 2; if ($r.StatusCode -eq 200) { exit 0 } else { exit 1 } } catch { exit 1 }" >nul 2>&1
if %errorlevel%==0 set "MS_RUNNING=1"

if "%OR_RUNNING%"=="1" (
  echo [OK] OpenRouter already running: http://127.0.0.1:9999
) else (
  echo [..] Starting OpenRouter on :9999 ...
  start "OpenRouter Proxy" /D "%~dp0" cmd /k "set SKIP_DOCS=1& call start-proxy.cmd"
)

if "%MS_RUNNING%"=="1" (
  echo [OK] ModelScope already running: http://127.0.0.1:%MS_PORT%
) else (
  echo [..] Starting ModelScope on :%MS_PORT% ...
  start "ModelScope Proxy" /D "%~dp0" cmd /k "set SKIP_DOCS=1& call start-modelscope.cmd"
)

echo.
echo -------------------------------
echo  OpenRouter : http://127.0.0.1:9999
echo  ModelScope : http://127.0.0.1:%MS_PORT%
echo -------------------------------
echo.
echo Two service windows keep the proxies alive.
echo Close each service window to stop that proxy.
echo.

timeout /t 3 /nobreak >nul
start "" "http://127.0.0.1:9999/docs"
start "" "http://127.0.0.1:%MS_PORT%/docs"

pause
exit /b 0
