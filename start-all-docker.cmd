@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title External Proxies (Docker)
chcp 65001 >nul

echo ================================
echo  OpenRouter + ModelScope (Docker)
echo  External: 9999 / 10001
echo  Does NOT change ArcReel strip
echo ================================
echo.

if not exist "%~dp0.env" (
  echo [ERROR] Missing .env
  pause
  exit /b 1
)
if not exist "%~dp0.env.modelscope" (
  echo [ERROR] Missing .env.modelscope
  pause
  exit /b 1
)

where docker >nul 2>&1
if errorlevel 1 (
  echo [ERROR] docker.exe not found.
  pause
  exit /b 1
)
docker info >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Docker Desktop is not running.
  pause
  exit /b 1
)

set "OPENROUTER_PORT=9999"
set "MODELSCOPE_PORT=10001"

echo Freeing leftover python on 9999 / 10001 ...
powershell -NoProfile -Command "foreach ($p in 9999,10001) { $conns = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue; foreach ($c in $conns) { $proc = Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue; if ($proc -and $proc.ProcessName -match 'python') { Write-Host ('[!] Freeing port ' + $p + ' pid=' + $c.OwningProcess); Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue } } }; Start-Sleep -Seconds 1"

echo Starting containers...
docker compose up -d openrouter-proxy modelscope-proxy
if errorlevel 1 (
  echo [ERROR] docker compose up failed.
  echo Try: docker compose build
  pause
  exit /b 1
)

echo Waiting for dashboards ...
set /a _tries=0
:wait
set /a _tries+=1
powershell -NoProfile -Command "try { $a = Invoke-WebRequest -Uri 'http://127.0.0.1:9999/dashboard' -UseBasicParsing -TimeoutSec 2; $b = Invoke-WebRequest -Uri 'http://127.0.0.1:10001/dashboard' -UseBasicParsing -TimeoutSec 2; if ($a.StatusCode -eq 200 -and $b.StatusCode -eq 200) { exit 0 } else { exit 1 } } catch { exit 1 }" >nul 2>&1
if %errorlevel%==0 goto ready
if %_tries% geq 40 (
  echo [ERROR] Containers started but dashboards did not respond in time.
  docker compose logs --tail 40 openrouter-proxy
  docker compose logs --tail 40 modelscope-proxy
  pause
  exit /b 1
)
timeout /t 1 /nobreak >nul
goto wait

:ready
echo [OK] OpenRouter:  http://127.0.0.1:9999/dashboard
echo [OK] ModelScope:  http://127.0.0.1:10001/dashboard
echo.
echo Cursor / other apps:
echo   OpenRouter  base URL  http://127.0.0.1:9999/v1
echo   ModelScope  base URL  http://127.0.0.1:10001/v1
echo.
echo Docker Desktop: start/stop these two (not ArcReel 7a73 / 7ef).
echo Stop: docker compose stop openrouter-proxy modelscope-proxy
start "" "http://127.0.0.1:9999/dashboard"
start "" "http://127.0.0.1:10001/dashboard"
pause
exit /b 0
