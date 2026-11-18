@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
rem Same entry resolution as starmap.bat: published build first, dev venv as fallback.
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

if "%~1"=="" (
  echo.
  echo  Drag a memory database onto this bat file to upgrade it in place:
  echo    - the .ai_memory folder with workspace.db
  echo    - or the project root folder
  echo    - or the workspace.db file itself
  echo.
  echo  Order of work: convert a copy, verify it, then swap it in.
  echo  The original is kept next to it as workspace.db.v3-YYYYMMDD-HHMMSS.
  echo  If anything fails, the live database is left untouched.
  echo.
  echo  Close the client that uses this database first, or the swap will
  echo  be refused as "database is in use".
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
set "FAILED=0"
for %%I in (%*) do (
  echo.
  echo === %%~fI
  "%HM_CLI%" upgrade "%%~fI" --in-place
  if errorlevel 1 set "FAILED=1"
)

echo.
if "%FAILED%"=="1" (
  echo [ERROR] At least one database was not upgraded, see the messages above.
) else (
  echo Done. The upgraded databases can be opened by the client again.
)
pause
endlocal
