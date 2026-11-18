@echo off
chcp 65001 >nul
setlocal
rem Release root lives OUTSIDE the project on purpose: the host sandbox blocks
rem processes started inside the workspace from writing outside it.
rem Path config: env var wins, then local.env in this folder (not tracked by git).
if not defined HM_RELEASE_ROOT if exist "%~dp0local.env" (
  for /f "usebackq tokens=1,2 delims==" %%A in ("%~dp0local.env") do (
    if /i "%%A"=="HM_RELEASE_ROOT" set "HM_RELEASE_ROOT=%%B"
  )
)
if defined HM_RELEASE_ROOT (
  set "HM_ROOT=%HM_RELEASE_ROOT%"
) else (
  echo [ERROR] HM_RELEASE_ROOT is not configured.
  echo         Set the env var, or copy local.env.example to local.env and fill it in.
  pause
  exit /b 1
)
set "HM_CLI=%HM_ROOT%\current\humanity-memory.exe"
set "HM_SRC=published (%HM_ROOT%\current)"

if not exist "%HM_CLI%" (
  set "HM_CLI=%~dp0.venv\Scripts\humanity-memory.exe"
  set "HM_SRC=source (.venv dev env; published build not found)"
)

if not defined HM_STARMAP_OUT set "HM_STARMAP_OUT=%TEMP%\humanity_memory_starmap.html"
set "OUT=%HM_STARMAP_OUT%"

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
  echo         Run tools\build_release.py --publish to produce the published build,
  echo         or install the dev environment into .venv.
  pause
  exit /b 1
)

echo Using: %HM_SRC%
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
