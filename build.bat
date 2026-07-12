@echo off
setlocal
cd /d "%~dp0"
if errorlevel 1 goto :failed

if not exist ".venv\Scripts\python.exe" (
  echo ERROR: Missing .venv. Run install.bat first.
  goto :missing_environment
)

.venv\Scripts\python.exe -c "import sys; raise SystemExit(0 if (3, 11) <= sys.version_info[:2] <= (3, 13) else 1)"
if errorlevel 1 (
  echo ERROR: The build environment must use Python 3.11, 3.12, or 3.13.
  goto :unsupported_environment
)

.venv\Scripts\python.exe -m pip install "PyInstaller==6.21.0" --progress-bar off
if errorlevel 1 goto :failed

.venv\Scripts\python.exe -m pip check
if errorlevel 1 goto :failed

set "stagedExe=%~dp0build\release\ClipSave.exe"
set "replacementExe=%~dp0ClipSave.exe.new"
if exist "%stagedExe%" del /q "%stagedExe%"
if errorlevel 1 goto :failed
if exist "%replacementExe%" del /q "%replacementExe%"
if errorlevel 1 goto :failed

.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name ClipSave ^
  --icon "%~dp0assets\clipsave.ico" ^
  --distpath build\release ^
  --workpath build\work ^
  --specpath build ^
  --collect-data lucide ^
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
if errorlevel 1 goto :failed

if not exist "%stagedExe%" (
  echo ERROR: PyInstaller completed without creating the staged executable.
  goto :missing_output
)

copy /y "%stagedExe%" "%replacementExe%" >nul
if errorlevel 1 goto :failed
move /y "%replacementExe%" "%~dp0ClipSave.exe" >nul
if errorlevel 1 goto :failed

echo.
echo Built: %~dp0ClipSave.exe
pause
exit /b 0

:missing_environment
set "exitCode=1"
goto :report_failure

:unsupported_environment
set "exitCode=1"
goto :report_failure

:missing_output
set "exitCode=1"
goto :report_failure

:failed
set "exitCode=%errorlevel%"
if "%exitCode%"=="0" set "exitCode=1"

:report_failure
if defined replacementExe if exist "%replacementExe%" del /q "%replacementExe%" >nul 2>nul
echo.
echo ClipSave build failed with exit code %exitCode%.
pause
exit /b %exitCode%
