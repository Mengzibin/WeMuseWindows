@echo off
setlocal
cd /d "%~dp0"

:: Find conda env "cpu" python.exe
set CONDA_PY=
for %%P in (
    "%USERPROFILE%\anaconda3\envs\cpu\python.exe"
    "%USERPROFILE%\miniconda3\envs\cpu\python.exe"
    "C:\ProgramData\anaconda3\envs\cpu\python.exe"
    "C:\ProgramData\miniconda3\envs\cpu\python.exe"
) do (
    if exist %%P ( set CONDA_PY=%%P & goto :found )
)
echo ERROR: Could not find conda env "cpu". Run: conda activate cpu
pause & exit /b 1

:found
echo Using Python: %CONDA_PY%

:: Verify key deps
%CONDA_PY% -c "import pyperclip" >nul 2>&1
if errorlevel 1 (
    echo pyperclip missing - install it first:
    echo   conda activate cpu
    echo   pip install pyperclip uiautomation pywin32 pystray keyboard mss Pillow pynput
    pause & exit /b 1
)

set LOG=%TEMP%\wemuse_panel.log
echo Starting WeMuse panel...
echo Log: %LOG%
echo Tip: Open WeChat first, the panel will attach to its right side.
echo.

%CONDA_PY% -m src.main_panel > "%LOG%" 2>&1

if errorlevel 1 (
    echo ERROR - see log: %LOG%
    echo ---
    type "%LOG%"
    pause
)
