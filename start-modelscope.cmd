@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title ModelScope Proxy
chcp 65001 >nul

echo ================================
echo  ModelScope Upstream Proxy
echo ================================
echo.

if not exist "%~dp0.env.modelscope" (
  echo [ERROR] Missing .env.modelscope
  echo Copy .env.modelscope.sample to .env.modelscope and fill in keys.
  pause
  exit /b 1
)

where python >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Python not found in PATH.
  pause
  exit /b 1
)

echo Loading .env.modelscope ...
for /f "usebackq eol=# tokens=1,* delims==" %%A in ("%~dp0.env.modelscope") do (
  if not "%%A"=="" set "%%A=%%B"
)

if "%PROXY_API_KEY%"=="" (
  echo [ERROR] PROXY_API_KEY is empty in .env.modelscope
  pause
  exit /b 1
)
if "%UPSTREAM_KEYS%"=="" (
  echo [ERROR] UPSTREAM_KEYS is empty in .env.modelscope
  pause
  exit /b 1
)

if "%APP_PORT%"=="" set "APP_PORT=10001"

powershell -NoProfile -Command "try { $r = Invoke-WebRequest -Uri 'http://127.0.0.1:%APP_PORT%/docs' -UseBasicParsing -TimeoutSec 2; if ($r.StatusCode -eq 200) { exit 0 } else { exit 1 } } catch { exit 1 }" >nul 2>&1
if %errorlevel%==0 (
  echo [OK] Already running: http://127.0.0.1:%APP_PORT%
  if /i not "%SKIP_DOCS%"=="1" start "" "http://127.0.0.1:%APP_PORT%/docs"
  pause
  exit /b 0
)

echo URL:     http://127.0.0.1:%APP_PORT%
echo Docs:    http://127.0.0.1:%APP_PORT%/docs
echo Upstream: %UPSTREAM_BASE_URL%
echo API Key: %PROXY_API_KEY%
echo.
echo Close this window to stop the proxy.
echo ================================
echo.

REM Avoid opening a second docs tab when launched from 一键启动.bat
if /i not "%SKIP_DOCS%"=="1" (
  start "" cmd /c "timeout /t 3 /nobreak >nul & start http://127.0.0.1:%APP_PORT%/docs"
)

python -m uvicorn main:app --host 0.0.0.0 --port %APP_PORT%
set ERR=%errorlevel%

echo.
if not %ERR%==0 (
  echo [ERROR] Service exited with code %ERR%
) else (
  echo Service stopped.
)
pause
exit /b %ERR%
