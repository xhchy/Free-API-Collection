@echo off
REM SPDX-License-Identifier: AGPL-3.0-or-later  /  Copyright (C) 2026 XHTV
REM ============================================================
REM  gpt4free bundled - free provider aggregator
REM  port 3996 (independent from free-api-stack 3020)
REM  API key must match the g4f route in vault\keys.json
REM ============================================================
cd /d "%~dp0g4f"
call "%~dp0env.bat"
set G4F_API_KEY=g4f-local-2026
set G4F_PORT=3996
%PY_G4F% -m g4f.api.run
