@echo off
setlocal
cd /d "%~dp0"

if not exist templates (
    echo ERROR: Put the complete templates folder beside this BAT file.
    pause
    exit /b 1
)

py -m pip install --upgrade pyinstaller opencv-python numpy adbutils Pillow pynput
py -m PyInstaller --clean --noconfirm --onedir --console --name NarutoBlazingBot ^
  --collect-all cv2 --collect-all numpy --collect-all adbutils ^
  --collect-all PIL --collect-all pynput blazing_bot_v2.4_launcher_ready.py

if errorlevel 1 (
    echo Bot build failed.
    pause
    exit /b 1
)

xcopy templates "dist\NarutoBlazingBot\templates\" /E /I /Y
echo 2.4.0> "dist\NarutoBlazingBot\version.txt"
echo.
echo Bot build finished in dist\NarutoBlazingBot
pause
