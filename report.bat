@echo off
chcp 65001 >nul
setlocal

rem ---------------------------------------------------------------------------
rem  Double-click to build and open the retrieval analysis page.
rem  Drag a project folder onto it to analyse that workspace instead of the cwd.
rem
rem  Release root lives OUTSIDE the project on purpose: the host sandbox blocks
rem  processes started inside the workspace from writing outside it.
rem ---------------------------------------------------------------------------

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

if not exist "%HM_CLI%" (
  echo [ERROR] CLI not found: %HM_CLI%
  echo         Run tools\build_release.py --publish, or install the dev env into .venv.
  pause
  exit /b 1
)

set "WS="
if not "%~1"=="" set "WS=--workspace "%~1""

if not defined HM_REPORT_OUT set "HM_REPORT_OUT=%TEMP%\humanity_memory_retrieval.html"
set "OUT=%HM_REPORT_OUT%"

echo Using: %HM_SRC%
if defined WS echo Analyzing: %~1
echo.

"%HM_CLI%" %WS% retrieval-report --out "%OUT%"
if errorlevel 1 (
  echo.
  echo [ERROR] Report generation failed, see the message above.
  pause
  exit /b 1
)

echo.
echo Output: %OUT%
if not defined HM_REPORT_NO_OPEN start "" "%OUT%"
endlocal
