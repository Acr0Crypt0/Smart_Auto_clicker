@echo off
REM Builds ONE standalone file: dist\SmartAutoClicker.exe
REM (no Python, libraries or extra folders needed to run it)
python -m pip install -r requirements.txt pyinstaller
python -m PyInstaller --clean --noconfirm --noconsole --onefile --name SmartAutoClicker ^
  --hidden-import pystray._win32 ^
  --hidden-import pynput.keyboard._win32 ^
  --hidden-import pynput.mouse._win32 ^
  smart_auto_clicker.py
echo.
echo Done. Your file is dist\SmartAutoClicker.exe
pause
