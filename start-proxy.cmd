@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title OpenRouter Proxy
chcp 65001 >nul

echo ================================
echo  OpenRouter Proxy
echo ================================
echo.

if not exist "%~dp0.env" (
  echo [ERROR] Missing .env file.
  echo Copy .env.sample to .env and fill in keys.
  pause
  exit /b 1
)

where python >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Python not found in PATH.
  pause
  exit /b 1
)

powershell -NoProfile -Command "try { $r = Invoke-WebRequest -Uri 'http://127.0.0.1:9999/docs' -UseBasicParsing -TimeoutSec 2; if ($r.StatusCode -eq 200) { exit 0 } else { exit 1 } } catch { exit 1 }" >nul 2>&1
if %errorlevel%==0 (
  echo [OK] Already running: http://127.0.0.1:9999
  start "" "http://127.0.0.1:9999/docs"
  pause
  exit /b 0
)

echo Loading .env ...
for /f "usebackq eol=# tokens=1,* delims==" %%A in ("%~dp0.env") do (
  if not "%%A"=="" set "%%A=%%B"
)

if "%PROXY_API_KEY%"=="" (
  echo [ERROR] PROXY_API_KEY is empty in .env
  pause
  exit /b 1
)
if "%OPENROUTER_KEYS%"=="" (
  echo [ERROR] OPENROUTER_KEYS is empty in .env
  pause
  exit /b 1
)

echo URL:     http://127.0.0.1:9999
echo Docs:    http://127.0.0.1:9999/docs
echo API Key: %PROXY_API_KEY%
echo.
echo Close this window to stop the proxy.
echo ================================
echo.

start "" cmd /c "timeout /t 3 /nobreak >nul & start http://127.0.0.1:9999/docs"

python -m uvicorn main:app --host 0.0.0.0 --port 9999
set ERR=%errorlevel%

echo.
if not %ERR%==0 (
  echo [ERROR] Service exited with code %ERR%
) else (
  echo Service stopped.
)
pause
exit /b %ERR%
