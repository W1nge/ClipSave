@echo off
setlocal
set "releaseDir=%~dp0build\release"
cd /d "%~dp0"
if errorlevel 1 goto :failed
if exist "%releaseDir%" rmdir /s /q "%releaseDir%"
if exist "%releaseDir%" (
  echo ERROR: Could not clean the previous release directory.
  goto :failed
)
if exist "%~dp0build\work" rmdir /s /q "%~dp0build\work"
if exist "%~dp0build\ClipSave.spec" del /q "%~dp0build\ClipSave.spec"
if exist "%~dp0build\version_info.txt" del /q "%~dp0build\version_info.txt"

if not exist ".venv\Scripts\python.exe" (
  echo ERROR: Missing .venv. Run install.bat first.
  goto :missing_environment
)

.venv\Scripts\python.exe -c "import sys; raise SystemExit(0 if (3, 11) <= sys.version_info[:2] <= (3, 13) else 1)"
if errorlevel 1 (
  echo ERROR: The build environment must use Python 3.11, 3.12, or 3.13.
  goto :unsupported_environment
)

.venv\Scripts\python.exe -c "import platform,struct; raise SystemExit(0 if platform.machine().upper() in ('AMD64','X86_64') and struct.calcsize('P') == 8 else 1)"
if errorlevel 1 (
  echo ERROR: Release builds require 64-bit x86 Python.
  goto :unsupported_environment
)

if "%CLIPSAVE_OFFICIAL_BUILD%"=="1" (
  .venv\Scripts\python.exe -c "import pathlib,platform,sys; official=platform.python_implementation() == 'CPython' and sys.version_info[:3] == (3,13,5) and 'Anaconda' not in sys.version and not (pathlib.Path(sys.base_prefix) / 'conda-meta').exists(); raise SystemExit(0 if official else 1)"
  if errorlevel 1 (
    echo ERROR: Official releases require official CPython 3.13.5.
    goto :unsupported_environment
  )
  git rev-parse --verify HEAD >nul 2>nul
  if errorlevel 1 (
    echo ERROR: Official releases require Git commit provenance.
    goto :failed
  )
  set "officialDirty="
  for /f "delims=" %%S in ('git status --porcelain --untracked-files^=normal 2^>nul') do set "officialDirty=1"
  if defined officialDirty (
    echo ERROR: Official releases require a clean Git working tree.
    goto :failed
  )
  set "officialHead="
  for /f "delims=" %%C in ('git rev-parse HEAD 2^>nul') do set "officialHead=%%C"
  if not defined officialHead (
    echo ERROR: Official releases require Git commit provenance.
    goto :failed
  )
)

set "lockedInstallOptions="
if "%CLIPSAVE_OFFICIAL_BUILD%"=="1" set "lockedInstallOptions=--force-reinstall"

.venv\Scripts\python.exe -m pip install %lockedInstallOptions% --require-hashes -r requirements-windows.lock --progress-bar off
if errorlevel 1 goto :failed

.venv\Scripts\python.exe -m pip install %lockedInstallOptions% --require-hashes -r build-requirements-windows.lock --progress-bar off
if errorlevel 1 goto :failed

.venv\Scripts\python.exe -m pip check
if errorlevel 1 goto :failed

if "%CLIPSAVE_OFFICIAL_BUILD%"=="1" (
  .venv\Scripts\python.exe -c "from importlib.metadata import distributions; from packaging.requirements import Requirement; from pathlib import Path; canon=lambda s: s.lower().replace('_','-').replace('.','-'); locks=('requirements-windows.lock','build-requirements-windows.lock'); allowed={'pip'}; [allowed.add(canon(Requirement(line.split('--hash=',1)[0].strip()).name)) for lock in locks for line in Path(lock).read_text(encoding='utf-8').replace('\\\n',' ').splitlines() if line.strip() and not line.lstrip().startswith('#')]; installed={canon(d.metadata['Name']) for d in distributions() if d.metadata.get('Name')}; extra=sorted(installed-allowed); missing=sorted(allowed-installed); print('ERROR: Official build environment has undeclared distributions: ' + ', '.join(extra)) if extra else None; print('ERROR: Official build environment is missing distributions: ' + ', '.join(missing)) if missing else None; raise SystemExit(1 if extra or missing else 0)"
  if errorlevel 1 goto :failed
)

set "appDir=%~dp0build\release\ClipSave"
set "stagedExe=%~dp0build\release\ClipSave\ClipSave.exe"
set "versionInfo=%~dp0build\version_info.txt"
mkdir "%releaseDir%"
if errorlevel 1 goto :failed
.venv\Scripts\python.exe build_version_info.py "%versionInfo%"
if errorlevel 1 goto :failed

.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --onedir --windowed ^
  --name ClipSave ^
  --contents-directory _internal ^
  --icon "%~dp0assets\clipsave.ico" ^
  --version-file "%versionInfo%" ^
  --noupx ^
  --distpath build\release ^
  --workpath build\work ^
  --specpath build ^
  --collect-data lucide ^
  --hidden-import winrt.windows.storage ^
  --hidden-import winrt.windows.storage.streams ^
  --hidden-import winrt.windows.graphics.imaging ^
  --hidden-import winrt.windows.graphics.directx ^
  --hidden-import winrt.windows.graphics.directx.direct3d11 ^
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

.venv\Scripts\python.exe -c "from pathlib import Path; import shutil; name='\u53cc\u51fb\u542f\u52a8.vbs'; shutil.copy2(Path(r'%~dp0.') / name, Path(r'%releaseDir%') / name)"
if errorlevel 1 goto :failed
copy /y "%~dp0LICENSE" "%releaseDir%\LICENSE" >nul
if errorlevel 1 goto :failed
copy /y "%~dp0THIRD_PARTY_NOTICES.md" "%releaseDir%\THIRD_PARTY_NOTICES.md" >nul
if errorlevel 1 goto :failed
.venv\Scripts\python.exe collect_third_party_licenses.py "%releaseDir%\THIRD_PARTY_LICENSES"
if errorlevel 1 goto :failed
copy /y "%~dp0README_RELEASE.md" "%releaseDir%\README.md" >nul
if errorlevel 1 goto :failed

for /f "usebackq delims=" %%V in (`.venv\Scripts\python.exe -c "from clipsave_app.constants import APP_VERSION; print(APP_VERSION)"`) do set "appVersion=%%V"
if not defined appVersion goto :failed
for /f "usebackq delims=" %%C in (`git rev-parse HEAD 2^>nul`) do set "commitHash=%%C"
if not defined commitHash set "commitHash=unknown"
if "%CLIPSAVE_OFFICIAL_BUILD%"=="1" if "%commitHash%"=="unknown" (
  echo ERROR: Official releases require Git commit provenance.
  goto :failed
)
for /f "delims=" %%S in ('git status --porcelain --untracked-files^=normal 2^>nul') do set "workingTreeDirty=1"
if defined workingTreeDirty set "commitHash=%commitHash%-dirty"
set "buildLabel=UNOFFICIAL - local or unverified build"
set "archiveLabel=-UNOFFICIAL"
if "%CLIPSAVE_OFFICIAL_BUILD%"=="1" call :verify_official_source
if "%CLIPSAVE_OFFICIAL_BUILD%"=="1" if errorlevel 1 goto :failed
if "%CLIPSAVE_OFFICIAL_BUILD%"=="1" set "commitHash=%officialHead%"
if "%CLIPSAVE_OFFICIAL_BUILD%"=="1" set "buildLabel=OFFICIAL"
if "%CLIPSAVE_OFFICIAL_BUILD%"=="1" set "archiveLabel="
>"%releaseDir%\BUILD_INFO.txt" echo ClipSave %appVersion%
>>"%releaseDir%\BUILD_INFO.txt" echo Build channel %buildLabel%
>>"%releaseDir%\BUILD_INFO.txt" echo Commit %commitHash%
>>"%releaseDir%\BUILD_INFO.txt" .venv\Scripts\python.exe -c "import platform; print('Python ' + platform.python_version())"
>>"%releaseDir%\BUILD_INFO.txt" .venv\Scripts\python.exe -c "import platform,sys; print('Python implementation ' + platform.python_implementation()); print('Python build ' + sys.version.replace(chr(10), ' '))"
>>"%releaseDir%\BUILD_INFO.txt" .venv\Scripts\python.exe -c "import glob,hashlib,os; [print('Bundled runtime ' + os.path.basename(p) + ' SHA256 ' + hashlib.sha256(open(p,'rb').read()).hexdigest().upper()) for p in glob.glob(r'%appDir%\_internal\python*.dll')]"
if defined ImageOS >>"%releaseDir%\BUILD_INFO.txt" echo GitHub runner image OS %ImageOS%
if defined ImageVersion >>"%releaseDir%\BUILD_INFO.txt" echo GitHub runner image version %ImageVersion%
if defined ImageRelease >>"%releaseDir%\BUILD_INFO.txt" echo GitHub runner image release %ImageRelease%
.venv\Scripts\python.exe build_manifest.py "%releaseDir%" "%releaseDir%\SHA256SUMS.txt"
if errorlevel 1 goto :failed
set "releaseArchive=%releaseDir%\ClipSave-%appVersion%%archiveLabel%-windows-x64.zip"
powershell -NoProfile -Command "Compress-Archive -Path '%appDir%','%releaseDir%\*.vbs','%releaseDir%\LICENSE','%releaseDir%\THIRD_PARTY_NOTICES.md','%releaseDir%\THIRD_PARTY_LICENSES','%releaseDir%\README.md','%releaseDir%\BUILD_INFO.txt','%releaseDir%\SHA256SUMS.txt' -DestinationPath '%releaseArchive%' -Force"
if errorlevel 1 goto :failed
powershell -NoProfile -Command "$hash=(Get-FileHash -LiteralPath '%releaseArchive%' -Algorithm SHA256).Hash; Set-Content -LiteralPath ('%releaseArchive%' + '.sha256') -Value ($hash + '  ' + [IO.Path]::GetFileName('%releaseArchive%')) -Encoding ASCII"
if errorlevel 1 goto :failed

echo.
echo Built: %stagedExe%
echo Release: %releaseArchive%
pause
exit /b 0

:verify_official_source
set "currentOfficialHead="
for /f "delims=" %%C in ('git rev-parse --verify HEAD 2^>nul') do set "currentOfficialHead=%%C"
if not defined currentOfficialHead (
  echo ERROR: Official release source changed during the build.
  exit /b 1
)
if not "%currentOfficialHead%"=="%officialHead%" (
  echo ERROR: Official release source changed during the build.
  exit /b 1
)
set "officialDirty="
for /f "delims=" %%S in ('git status --porcelain --untracked-files^=normal 2^>nul') do set "officialDirty=1"
if defined officialDirty (
  echo ERROR: Official release source became dirty during the build.
  exit /b 1
)
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
if defined releaseDir if exist "%releaseDir%" rmdir /s /q "%releaseDir%" >nul 2>nul
if defined releaseDir if exist "%releaseDir%" echo WARNING: Incomplete release directory could not be removed: %releaseDir%
echo.
echo ClipSave build failed with exit code %exitCode%.
pause
exit /b %exitCode%
