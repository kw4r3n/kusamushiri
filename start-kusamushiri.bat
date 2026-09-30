@echo off
setlocal
cd /d "%~dp0"

where uv >nul 2>nul
if errorlevel 1 (
    echo uv was not found in PATH.
    echo Install uv first, then double-click this file again.
    echo https://docs.astral.sh/uv/getting-started/installation/
    pause
    exit /b 1
)

rem uv creates the environment on first run; Chromium downloads on the first browser start.
echo Starting Kusamushiri...
uv run --locked kusamushiri
if errorlevel 1 (
    echo.
    echo Kusamushiri could not start. See the messages above.
    pause
    exit /b 1
)
