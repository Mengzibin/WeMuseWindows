@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo  WeMuse - Windows Setup
echo ============================================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found in PATH.
    echo Install Python 3.11+ from https://www.python.org/downloads/
    pause & exit /b 1
)

echo Python: && python --version
echo.
echo Installing dependencies...
python -m pip install -r requirements.txt --no-warn-script-location
if errorlevel 1 ( echo WARNING: Some packages failed. Check errors above. )

echo.
echo Checking for Claude CLI...
where claude >nul 2>&1
if errorlevel 1 (
    echo NOTE: claude not in PATH.
    echo Install the Claude Code VSCode extension, or run:
    echo   npm install -g @anthropic-ai/claude-code
) else ( echo Found: && where claude )

echo.
echo ============================================================
echo  Done. Run run_panel.bat to start WeMuse.
echo ============================================================
pause
