@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\pyinstaller.exe" .venv\Scripts\python.exe -m pip install pyinstaller --progress-bar off
.venv\Scripts\pyinstaller.exe --noconfirm --clean --onefile --windowed ^
  --name ClipSave ^
  --icon "%~dp0assets\clipsave.ico" ^
  --distpath . ^
  --workpath build\work ^
  --specpath build ^
  --collect-all lucide ^
  --hidden-import winrt.windows.storage ^
  --hidden-import winrt.windows.storage.streams ^
  --hidden-import winrt.windows.graphics.imaging ^
  --hidden-import winrt.windows.graphics.directx ^
  --hidden-import winrt.windows.globalization ^
  --hidden-import winrt.windows.media.ocr ^
  --hidden-import winrt.windows.system ^
  --hidden-import winrt.windows.storage.search ^
  --hidden-import winrt.windows.storage.provider ^
  --hidden-import winrt.windows.storage.fileproperties ^
  clipsave.py
echo.
echo Built: %~dp0ClipSave.exe
pause
