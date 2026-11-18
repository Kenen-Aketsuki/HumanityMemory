@echo off
setlocal

rem ---------------------------------------------------------------------------
rem  Dev entry (source state). Production entry is the frozen exe in
rem  release\current -- the two never affect each other.
rem
rem  The only job of this script: pin the workspace to the isolated dev dir so
rem  the source build can never touch .ai_memory\workspace.db. A destructive
rem  schema bump would make the production exe refuse that database.
rem
rem  Usage:
rem    dev.bat doctor
rem    dev.bat memory-stats
rem    dev.bat serve          (start an MCP server on the isolated db)
rem ---------------------------------------------------------------------------

rem Path config: env var wins, then local.env in this folder (not tracked by git).
if not defined HM_DEV_ROOT if exist "%~dp0local.env" for /f "usebackq tokens=1,2 delims==" %%A in ("%~dp0local.env") do if /i "%%A"=="HM_DEV_ROOT" set "HM_DEV_ROOT=%%B"
if defined HM_DEV_ROOT (
  set "DEV_WS=%HM_DEV_ROOT%"
) else (
  echo [ERROR] HM_DEV_ROOT is not configured.
  echo         Set the env var, or copy local.env.example to local.env and fill it in.
  pause
  exit /b 1
)

set "HM_WORKSPACE=%DEV_WS%"
set "CLI=%~dp0.venv\Scripts\humanity-memory.exe"
rem NOTE: keep this file ASCII-only. cmd.exe reads .bat as the OEM codepage
rem (GBK on zh-CN), so UTF-8 Chinese text here breaks parsing.
rem For MCP startup the token's workspace outranks --workspace and
rem HM_WORKSPACE, so the production active.token.json would drag a dev server
rem back onto the production database. Hence a separate dev token.
set "DEV_TOKEN=%USERPROFILE%\.humanity-memory\dev.token.json"

if not exist "%CLI%" (
  echo [ERROR] source CLI not found: %CLI%
  echo         install the dev env first: pip install -e .
  pause
  exit /b 1
)

if "%~1"=="" goto usage
if /i "%~1"=="serve" goto serve

echo [dev] workspace = %DEV_WS%
"%CLI%" %*
set "RC=%ERRORLEVEL%"
endlocal & exit /b %RC%

:serve
if not exist "%DEV_TOKEN%" (
  echo [ERROR] dev token missing: %DEV_TOKEN%
  echo         copy active.token.json there and set "workspace" to %DEV_WS%
  pause
  exit /b 1
)
echo [dev] workspace = %DEV_WS%  (token-pinned)
"%CLI%" --token "%DEV_TOKEN%" --workspace "%DEV_WS%" serve
set "RC=%ERRORLEVEL%"
endlocal & exit /b %RC%

:usage
echo.
echo   dev entry - isolated workspace, production untouched
echo     workspace : %DEV_WS%
echo     cli       : %CLI%
echo.
echo   example: dev.bat doctor
echo            dev.bat memory-stats
echo.
pause
exit /b 1
