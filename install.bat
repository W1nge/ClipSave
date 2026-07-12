@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
if errorlevel 1 goto :failed

if not exist ".venv\Scripts\python.exe" (
  set "PYTHON_CMD="
  where py >nul 2>nul
  if not errorlevel 1 (
    for %%V in (3.13 3.12 3.11) do (
      if not defined PYTHON_CMD (
        py -%%V -c "import sys; raise SystemExit(0 if sys.version_info[:2] == tuple(map(int, '%%V'.split('.'))) else 1)" >nul 2>nul
        if not errorlevel 1 set "PYTHON_CMD=py -%%V"
      )
    )
  )
  if not defined PYTHON_CMD (
    where python >nul 2>nul
    if not errorlevel 1 set "PYTHON_CMD=python"
  )
  if not defined PYTHON_CMD (
    echo ERROR: Python 3.11-3.13 was not found on PATH.
    goto :missing_python
  )
  !PYTHON_CMD! -c "import sys; raise SystemExit(0 if (3, 11) <= sys.version_info[:2] <= (3, 13) else 1)"
  if errorlevel 1 (
    echo ERROR: ClipSave supports Python 3.11, 3.12, and 3.13.
    goto :unsupported_python
  )
  !PYTHON_CMD! -m venv .venv
  if errorlevel 1 goto :failed
)

if not exist ".venv\Scripts\python.exe" (
  echo ERROR: The virtual environment was not created correctly.
  goto :missing_python
)

.venv\Scripts\python.exe -c "import sys; raise SystemExit(0 if (3, 11) <= sys.version_info[:2] <= (3, 13) else 1)"
if errorlevel 1 (
  echo ERROR: The virtual environment must use Python 3.11, 3.12, or 3.13.
  goto :unsupported_python
)

.venv\Scripts\python.exe -m pip install -r requirements.txt --progress-bar off
if errorlevel 1 goto :failed

.venv\Scripts\python.exe -m pip uninstall --yes pyperclip
if errorlevel 1 goto :failed

.venv\Scripts\python.exe -m pip check
if errorlevel 1 goto :failed

echo.
echo ClipSave installation completed.
pause
exit /b 0

:missing_python
set "exitCode=1"
goto :report_failure

:unsupported_python
set "exitCode=1"
goto :report_failure

:failed
set "exitCode=%errorlevel%"
if "%exitCode%"=="0" set "exitCode=1"

:report_failure
echo.
echo ClipSave installation failed with exit code %exitCode%.
pause
exit /b %exitCode%
