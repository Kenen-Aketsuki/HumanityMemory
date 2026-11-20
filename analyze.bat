@echo off
chcp 65001 >nul
setlocal

rem ---------------------------------------------------------------------------
rem  Memory Observatory: local HTTP backend + browser frontend (read-only).
rem  Drag a folder/file onto this bat, or pass it as an argument. All forms are
rem  read-only and need no registry:
rem    - project root folder            (token.json lives here)
rem    - library folder under libraries^\^<label^>
rem    - .ai_memory\token.json file     (read-only: library resolved from it)
rem    - or the workspace.db file itself
rem
rem  NOTE: webtool is new code, absent from old published exe.
rem  This script prefers the source .venv; only when HM_ANALYZE_USE_PUBLISHED=1
rem  will it use the published exe (after a fresh build_release --publish).
rem ---------------------------------------------------------------------------

rem Project root: this bat sits directly in <project>\ ; %~dp0 ends with a slash.
set "PROJ=%~dp0"

if defined HM_ANALYZE_USE_PUBLISHED (
  rem Release root config: env var wins, then local.env in the project root.
  if not defined HM_RELEASE_ROOT if exist "%PROJ%local.env" (
    for /f "usebackq tokens=1,2 delims==" %%A in ("%PROJ%local.env") do (
      if /i "%%A"=="HM_RELEASE_ROOT" set "HM_RELEASE_ROOT=%%B"
    )
  )
  if defined HM_RELEASE_ROOT (
    set "HM_CLI=%HM_RELEASE_ROOT%\current\humanity-memory.exe"
    set "HM_SRC=published (%HM_RELEASE_ROOT%\current)"
  ) else (
    echo [ERROR] HM_ANALYZE_USE_PUBLISHED=1 but HM_RELEASE_ROOT is not configured.
    pause
    exit /b 1
  )
) else (
  set "HM_CLI=%PROJ%.venv\Scripts\humanity-memory.exe"
  set "HM_SRC=source (%PROJ%.venv)"
)

if not exist "%HM_CLI%" (
  echo [ERROR] CLI not found: %HM_CLI%
  echo         Install the dev env into .venv, e.g.:
  echo           python -m venv .venv ^&^& .venv\Scripts\pip install -e .
  echo         Or set HM_ANALYZE_USE_PUBLISHED=1 with a published build that has webtool.
  pause
  exit /b 1
)

if "%~1"=="" (
  echo.
  echo  Drag a folder/file onto this bat to open the memory observatory:
  echo    - project root folder            ^(token.json lives here^)
  echo    - library folder under libraries^\^<label^>
  echo    - .ai_memory\token.json file     ^(read-only: library resolved from it^)
  echo    - or the workspace.db file itself
  echo.
  echo  Read-only. No database is created or modified.
  echo.
  pause
  exit /b 1
)

if not defined HM_ANALYZE_PORT set "HM_ANALYZE_PORT=8765"

echo Using: %HM_SRC%
echo Opening observatory for: %~1
echo URL: http://127.0.0.1:%HM_ANALYZE_PORT%
echo   (press Ctrl+C in this window to stop the server)
echo.

"%HM_CLI%" analyze "%~1" --port %HM_ANALYZE_PORT%
if errorlevel 1 (
  echo.
  echo [ERROR] Observatory exited with an error, see the message above.
  pause
  exit /b 1
)
endlocal
