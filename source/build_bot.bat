@echo off
setlocal
cd /d "%~dp0"

if not exist templates (
    echo ERROR: Put the complete templates folder beside this BAT file.
    pause
    exit /b 1
)

for /f "delims=" %%I in ('python -c "import sys; print(sys.executable)"') do set "PYTHON_EXE=%%I"
if not defined PYTHON_EXE (
    echo ERROR: Python was not found on PATH.
    pause
    exit /b 1
)

"%PYTHON_EXE%" -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: PyInstaller is not installed for %PYTHON_EXE%.
    pause
    exit /b 1
)

echo Using Python: %PYTHON_EXE%
"%PYTHON_EXE%" -m PyInstaller --clean --noconfirm --onedir --console --name NarutoBlazingBot ^
  --collect-all cv2 --collect-all numpy --collect-all adbutils ^
  --collect-all PIL --collect-all pynput blazing_bot_v2.4_launcher_ready.py

if errorlevel 1 (
    echo Bot build failed.
    pause
    exit /b 1
)

xcopy templates "dist\NarutoBlazingBot\templates\" /E /I /Y
echo Version will be assigned by release_tools\make_release.py.
echo.
echo Bot build finished in dist\NarutoBlazingBot
pause
