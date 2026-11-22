@echo off
REM SPDX-License-Identifier: AGPL-3.0-or-later  /  Copyright (C) 2026 XHTV
REM ============================================================
REM  start-all.bat - one-click launcher for the Free API Stack
REM
REM  IMPORTANT - keep this file ASCII-only, CRLF line endings, no BOM,
REM  and do NOT add "chcp 65001". On Windows 10 <= 1909 the UTF-8 code
REM  page makes cmd.exe misparse batch files and lose the ability to
REM  find ANY external command (timeout, netstat, findstr...).
REM  Every external tool below is called through an absolute System32
REM  path so a broken PATH cannot take the launcher down again.
REM
REM  Before the first run:
REM    1) copy vault\keys.example.json vault\keys.json , then fill in keys
REM    2) download new-api-v1.0.0-rc.51.exe from the upstream NewAPI
REM       Releases and put it in this folder (not redistributed here)
REM    3) Python 3.10+ and Node 18+ on PATH (or edit embedded\env.bat)
REM    4) create venv-foa / venv-g4f if you want isolated python envs
REM    5) cd embedded\aiclient  and run: npm install
REM
REM  Each component opens its own minimized window; closing this window
REM  does not stop them. Re-running is safe and idempotent: any component
REM  whose port is already listening is skipped instead of double-started.
REM ============================================================

setlocal
set "SYS=%SystemRoot%\System32"

pushd "%~dp0"
if errorlevel 1 goto :nodir

if not exist "embedded\env.bat" goto :noenv
if not exist "vault\keys.json" goto :nokeys
if not exist "new-api-v1.0.0-rc.51.exe" goto :noexe

if not defined SESSION_SECRET (
  echo [!] SESSION_SECRET is not set - using a temporary dev value.
  echo     For real use run first:  set SESSION_SECRET=some-random-string
  set "SESSION_SECRET=dev-only-please-change"
)

echo [1/9] edge-tts        3993
call :spawn "free-edge-tts" 3993 "%~dp0embedded\run-edge-tts.bat" 2

echo [2/9] foa-bridge      3994
call :spawn "free-foa-bridge" 3994 "%~dp0embedded\run-foa-bridge.bat" 1

echo [3/9] gpt4free        3996
call :spawn "free-g4f" 3996 "%~dp0embedded\run-g4f.bat" 2

echo [4/9] free-one-api    3995
call :spawn "free-one-api" 3995 "%~dp0embedded\run-foa.bat" 2

echo [5/9] cf-ai-shim      3998
call :spawn "free-cf-shim" 3998 "%~dp0embedded\run-shim.bat" 1

echo [6/9] AIClient2API    3999
call :spawn "free-aiclient" 3999 "%~dp0embedded\run-aiclient.bat" 2

echo [7/9] keyvault        3997
call :spawn "free-vault" 3997 "%~dp0embedded\run-vault.bat" 2

echo [8/9] NewAPI          9978
set "SQLITE_PATH=%~dp0one-api.db"
set "PORT=9978"
set "TZ=Asia/Shanghai"
call :spawn "NewAPI-9978" 9978 "%~dp0new-api-v1.0.0-rc.51.exe" 0

echo.
echo ------------------------------------------------------------
echo   NewAPI      http://127.0.0.1:9978/
echo   sub 3993 edge-tts / 3994 bridge / 3995 foa / 3996 g4f
echo       3997 vault    / 3998 shim   / 3999 aiclient
echo   creds  vault\keys.json (never commit) template keys.example.json
echo ------------------------------------------------------------
call :nap 6

echo [9/9] port self-check
for %%P in (9978 3993 3994 3995 3996 3997 3998 3999) do call :chkport %%P
echo.
pause
popd
exit /b 0

REM ============================================================
:nodir
echo [X] cannot enter the script directory
pause
exit /b 1

:noenv
echo [X] missing embedded\env.bat - the package looks incomplete
pause
exit /b 1

:nokeys
echo [X] missing vault\keys.json
echo     run:  copy vault\keys.example.json vault\keys.json
echo     then fill in your real keys
pause
exit /b 1

:noexe
echo [X] missing new-api-v1.0.0-rc.51.exe
echo     this project does not ship that binary. Download it from the
echo     upstream NewAPI Releases and drop it in this folder
pause
exit /b 1

REM ============================================================
REM  :spawn  TITLE PORT COMMAND WAIT_SEC
REM  idempotent: skip when PORT is already listening
REM ============================================================
:spawn
"%SYS%\netstat.exe" -ano | "%SYS%\findstr.exe" /c:":%~2 " >nul 2>&1
if errorlevel 1 goto :spawn_go
echo        port %~2 already listening - skipped
call :nap 1
goto :eof

:spawn_go
start "%~1" /min "%~3"
call :nap %~4
goto :eof

REM ============================================================
:nap
REM  wait ~N seconds on the loopback ping (no console dependency)
set /a NAPN=%~1+1
"%SYS%\ping.exe" -n %NAPN% 127.0.0.1 >nul 2>&1
goto :eof

REM ============================================================
:chkport
"%SYS%\netstat.exe" -ano | "%SYS%\findstr.exe" /r /c:":%~1 .*LISTENING" >nul 2>&1
if errorlevel 1 (
  echo    %~1  DOWN  ^(see that component's window^)
) else (
  echo    %~1  UP
)
goto :eof
