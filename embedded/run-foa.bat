@echo off
REM SPDX-License-Identifier: AGPL-3.0-or-later  /  Copyright (C) 2026 XHTV
REM ============================================================
REM  free-one-api bundled - pollinations free tier aggregator
REM  port 3995 (independent from free-api-stack 3060)
REM  upstream = ..\foa-bridge (3994)
REM ============================================================
cd /d "%~dp0free-one-api"
call "%~dp0env.bat"
%PY_FOA% main.py
