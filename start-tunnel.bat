@echo off
setlocal
set CLOUDFLARED="C:\Program Files (x86)\cloudflared\cloudflared.exe"
set PROXY_PORT=9999

echo Starting OpenRouter proxy on port %PROXY_PORT%...
start "openrouter-proxy" cmd /c "cd /d C:\project\openrouter-proxy-injector && powershell -NoProfile -Command \"Get-Content .env | ForEach-Object { if ($_ -match '^\s*([^#][^=]+)=(.*)$') { [System.Environment]::SetEnvironmentVariable($matches[1].Trim(), $matches[2].Trim(), 'Process') } }; python -m uvicorn main:app --host 0.0.0.0 --port %PROXY_PORT%\""

timeout /t 3 /nobreak >nul
echo Starting Cloudflare Tunnel...
%CLOUDFLARED% tunnel --url http://127.0.0.1:%PROXY_PORT%
