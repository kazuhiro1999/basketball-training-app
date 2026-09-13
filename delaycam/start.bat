@echo off
rem DelayCam launcher: installs uv if missing, then starts the server with the kiosk viewer.
cd /d "%~dp0"

where uv >nul 2>nul
if errorlevel 1 (
  echo uv not found. Installing uv ^(https://astral.sh/uv^)...
  powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
  set "PATH=%USERPROFILE%\.local\bin;%PATH%"
)

uv run server.py --kiosk %*
pause
