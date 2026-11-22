@echo off
REM SPDX-License-Identifier: AGPL-3.0-or-later  /  Copyright (C) 2026 XHTV
REM ============================================================
REM  pollinations bridge (upstream front for foa)
REM  adds the missing GET /health for the free-one-api ds-webagent adapter
REM  port 3994 (independent from free-api-stack 3061)
REM ============================================================
cd /d "%~dp0foa-bridge"
call "%~dp0env.bat"
set BRIDGE_PORT=3994
set POLLINATIONS_UPSTREAM=https://text.pollinations.ai
set HTTP_PROXY=
set HTTPS_PROXY=
set http_proxy=
set https_proxy=
%PY_FOA% pollinations-bridge.py
