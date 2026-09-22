@echo off
rem Live skeleton analyzer for delaycam. Start delaycam first (delaycam\start.bat), then this.
rem   start_live.bat                 -> preset medium (rtmpose-s), infer every 2nd frame, auto-adjust
rem   start_live.bat --preset light  -> lighter model
rem   start_live.bat --preset heavy --stride 1
cd /d "%~dp0"

where uv >nul 2>nul
if errorlevel 1 (
  echo uv not found. Installing uv ^(https://astral.sh/uv^)...
  powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
  set "PATH=%USERPROFILE%\.local\bin;%PATH%"
)

uv run live --stride 2 --auto-stride %*
pause
