@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Anthropic Official Key Rotator
chcp 65001 >nul

echo ================================
echo  Anthropic Official Rotator :10002
echo  upstream: api.anthropic.com
echo ================================
echo.

if not exist "%~dp0.env.anthropic" (
  echo [ERROR] Missing .env.anthropic
  pause
  exit /b 1
)

findstr /C:"REPLACE_WITH_SK_ANT_KEYS" "%~dp0.env.anthropic" >nul 2>&1
if %errorlevel%==0 (
  echo [ERROR] .env.anthropic still has placeholder UPSTREAM_KEYS.
  echo Edit the file and put your sk-ant-... keys, comma-separated.
  pause
  exit /b 1
)

where python >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Python not found in PATH
  pause
  exit /b 1
)

powershell -NoProfile -Command "try { $r = Invoke-WebRequest -Uri 'http://127.0.0.1:10002/health' -UseBasicParsing -TimeoutSec 2; if ($r.StatusCode -eq 200) { exit 0 } else { exit 1 } } catch { exit 1 }" >nul 2>&1
if %errorlevel%==0 (
  echo [WARN] Port 10002 already healthy. Stop the old Agnes proxy window first, then re-run.
  pause
  exit /b 0
)

echo Loading .env.anthropic and starting...
echo URL: http://127.0.0.1:10002
echo Close this window to stop the proxy.
echo ================================
echo.

python -c "import os; from pathlib import Path; [os.environ.__setitem__(k.strip(), v.strip()) for line in Path('.env.anthropic').read_text(encoding='utf-8').splitlines() if line.strip() and not line.strip().startswith('#') and '=' in line for k,v in [line.split('=',1)]]; print('upstream', os.environ.get('UPSTREAM_BASE_URL')); print('keys', len([x for x in os.environ.get('UPSTREAM_KEYS','').split(',') if x.strip()])); import uvicorn; uvicorn.run('main:app', host='0.0.0.0', port=10002)"
set ERR=%errorlevel%
echo.
if not %ERR%==0 (echo [ERROR] exited %ERR%) else (echo stopped.)
pause
exit /b %ERR%
