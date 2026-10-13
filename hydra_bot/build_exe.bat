@echo off
REM Builds dist\HydraGUI\HydraGUI.exe
REM   build_exe.bat        -> lite build (no YOLO; small, fast to start)
REM   build_exe.bat full   -> includes PyTorch/Ultralytics for the YOLO track style
cd /d "%~dp0"

python -m pip install -r requirements.txt pyinstaller || goto :fail

set EXCLUDES=--exclude-module torch --exclude-module ultralytics --exclude-module torchvision
if /i "%1"=="full" set EXCLUDES=

python -m PyInstaller --noconfirm --clean --windowed --onedir --name HydraGUI ^
  --add-data "hydra_bot\default_config.json;hydra_bot" ^
  --hidden-import win32api --hidden-import win32con --hidden-import win32gui ^
  %EXCLUDES% run_gui.py || goto :fail

REM Ship the user's config + model/template folders next to the exe.
if exist config.json copy /y config.json dist\HydraGUI\ >nul
if exist "..\YOLO rods" xcopy /e /i /y "..\YOLO rods" "dist\HydraGUI\YOLO rods" >nul
if exist "..\Detection" xcopy /e /i /y "..\Detection" "dist\HydraGUI\Detection" >nul

echo.
echo Done: dist\HydraGUI\HydraGUI.exe
pause
exit /b 0

:fail
echo Build failed.
pause
exit /b 1
