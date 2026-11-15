@echo off
setlocal
cd /d "%~dp0"
py -m pip install --upgrade pyinstaller
py -m PyInstaller --clean --onefile --console --name BlazingBotLauncher launcher.py
echo.
echo Launcher build finished. Find it in dist\BlazingBotLauncher.exe
pause
