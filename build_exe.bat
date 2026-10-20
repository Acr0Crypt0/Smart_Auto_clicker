@echo off
REM Builds a Windows executable into dist\SmartAutoClicker\
pip install -r requirements.txt pyinstaller
pyinstaller --noconsole --onedir --name SmartAutoClicker ^
  --hidden-import pystray._win32 ^
  --hidden-import pynput.keyboard._win32 ^
  --hidden-import pynput.mouse._win32 ^
  smart_auto_clicker.py
echo.
echo Done. Run dist\SmartAutoClicker\SmartAutoClicker.exe
pause
