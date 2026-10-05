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

echo Loading .env ...
for /f "usebackq eol=# tokens=1,* delims==" %%A in ("%~dp0.env") do (
  if not "%%A"=="" set "%%A=%%B"
)

if "%PROXY_API_KEY%"=="" (
  echo [ERROR] PROXY_API_KEY is empty in .env
  pause
  exit /b 1
)
if "%UPSTREAM_KEYS%"=="" if "%OPENROUTER_KEYS%"=="" (
  echo [ERROR] UPSTREAM_KEYS or OPENROUTER_KEYS is empty in .env
  pause
  exit /b 1
)

rem Host mapping is OPENROUTER_PORT, not APP_PORT (.env may still say 8000).
set "OPENROUTER_PORT=9999"
set "APP_PORT=9999"

where docker >nul 2>&1
if errorlevel 1 goto no_docker
docker info >nul 2>&1
if errorlevel 1 goto no_docker_engine
goto docker_start

:no_docker
echo [ERROR] docker.exe not found in PATH.
echo Install Docker Desktop, then retry.
pause
exit /b 1

:no_docker_engine
echo [ERROR] Docker Desktop is not running.
echo Start Docker Desktop, wait until it is ready, then retry.
pause
exit /b 1

:docker_start
docker compose ps --status running openrouter-proxy 2>nul | findstr /I openrouter-proxy >nul
if %errorlevel%==0 (
  echo [OK] Docker already running: http://127.0.0.1:%OPENROUTER_PORT%/dashboard
  start "" "http://127.0.0.1:%OPENROUTER_PORT%/dashboard"
  pause
  exit /b 0
)

echo Using Docker. Closing this window will NOT stop the proxy.
echo Host port: %OPENROUTER_PORT%
echo Dashboard: http://127.0.0.1:%OPENROUTER_PORT%/dashboard
echo Stop later: docker compose stop openrouter-proxy
echo ArcReel strip containers are not touched.
echo ================================
echo.

echo Freeing port %OPENROUTER_PORT% if a leftover python process is listening...
powershell -NoProfile -Command "$conns = Get-NetTCPConnection -LocalPort %OPENROUTER_PORT% -State Listen -ErrorAction SilentlyContinue; foreach ($c in $conns) { Write-Host ('[!] Freeing port %OPENROUTER_PORT% pid=' + $c.OwningProcess); Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue }; Start-Sleep -Seconds 1" 2>nul

docker compose up -d openrouter-proxy
if errorlevel 1 (
  echo [ERROR] docker compose up failed.
  echo First-time build: docker compose build openrouter-proxy
  pause
  exit /b 1
)

echo Waiting for dashboard ...
set /a _tries=0
:wait_docker
set /a _tries+=1
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -Uri 'http://127.0.0.1:%OPENROUTER_PORT%/dashboard' -UseBasicParsing -TimeoutSec 2; if ($r.StatusCode -eq 200) { exit 0 } else { exit 1 } } catch { exit 1 }" >nul 2>&1
if %errorlevel%==0 goto docker_ready
if %_tries% geq 30 (
  echo [ERROR] Container started but dashboard did not respond in time.
  echo Logs:
  docker compose logs --tail 80 openrouter-proxy
  pause
  exit /b 1
)
timeout /t 1 /nobreak >nul
goto wait_docker

:docker_ready
echo [OK] Running in Docker: http://127.0.0.1:%OPENROUTER_PORT%/dashboard
start "" "http://127.0.0.1:%OPENROUTER_PORT%/dashboard"
echo.
echo You can close this window. To stop:
echo   docker compose stop openrouter-proxy
pause
exit /b 0
