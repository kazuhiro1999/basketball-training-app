@echo off
rem DelayCam + skeleton overlay in one click.
rem Same as start.bat but also launches the pose analyzer (analyzer\ must be present).
rem   start_with_pose.bat            -> preset medium
rem   start_with_pose.bat --pose light
cd /d "%~dp0"

where uv >nul 2>nul
if errorlevel 1 (
  echo uv not found. Installing uv ^(https://astral.sh/uv^)...
  powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
  set "PATH=%USERPROFILE%\.local\bin;%PATH%"
)

uv run server.py --kiosk --pose %*
pause
