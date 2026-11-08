@echo off
chcp 65001 >nul
setlocal
set "HM_CLI=%~dp0.venv\Scripts\humanity-memory.exe"
set "OUT=%TEMP%\humanity_memory_starmap.html"

if "%~1"=="" (
  echo.
  echo  Drag a folder onto this bat file to draw its memory starmap:
  echo    - the .ai_memory folder with workspace.db
  echo    - or the project root folder
  echo    - or the workspace.db file itself
  echo.
  echo  Read-only. No database is created or modified.
  echo.
  pause
  exit /b 1
)

if not exist "%HM_CLI%" (
  echo [ERROR] CLI not found: %HM_CLI%
  pause
  exit /b 1
)

echo Analyzing: %~1
echo.
"%HM_CLI%" starmap "%~1" --out "%OUT%"
if errorlevel 1 (
  echo.
  echo [ERROR] Starmap failed, see the message above.
  pause
  exit /b 1
)

echo.
echo Output: %OUT%
if not defined HM_STARMAP_NO_OPEN start "" "%OUT%"
endlocal
