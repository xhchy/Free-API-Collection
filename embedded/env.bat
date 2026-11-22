@echo off
REM SPDX-License-Identifier: AGPL-3.0-or-later  /  Copyright (C) 2026 XHTV
REM ============================================================
REM  env.bat - resolve python / node interpreters
REM
REM  Why this file: each run-*.bat used to hardcode one person's
REM  machine path (C:\Users\<name>\AppData\Local\Programs\...),
REM  which breaks on any other machine and leaks the local
REM  username into a public repository.
REM
REM  Resolution order:
REM    PY_BASE  env var -> "python" on PATH -> "py -3"
REM    PY_FOA   venv-foa\Scripts\python.exe (if present) -> %PY_BASE%
REM    PY_G4F   venv-g4f\Scripts\python.exe (if present) -> %PY_BASE%
REM    NODE     env var -> "node" on PATH
REM
REM  Override before running, e.g.:
REM    set PY_BASE=D:\Python312\python.exe
REM    set NODE=C:\Program Files\nodejs\node.exe
REM ============================================================

if not defined PY_BASE (
  set "PY_BASE=python"
  where python >nul 2>&1 || set "PY_BASE=py -3"
)

if not defined PY_FOA if exist "%~dp0venv-foa\Scripts\python.exe" set "PY_FOA=%~dp0venv-foa\Scripts\python.exe"
if not defined PY_FOA set "PY_FOA=%PY_BASE%"

if not defined PY_G4F if exist "%~dp0venv-g4f\Scripts\python.exe" set "PY_G4F=%~dp0venv-g4f\Scripts\python.exe"
if not defined PY_G4F set "PY_G4F=%PY_BASE%"

if not defined NODE set "NODE=node"
where node >nul 2>&1 || (
  echo [env] WARNING: node not found on PATH - component 3999 AIClient2API will not start
  echo [env]          install Node 18+, or: set NODE=full\path\to\node.exe
)

exit /b 0
