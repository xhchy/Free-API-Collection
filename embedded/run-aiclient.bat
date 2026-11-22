@echo off
REM SPDX-License-Identifier: AGPL-3.0-or-later  /  Copyright (C) 2026 XHTV
REM ============================================================
REM  AIClient2API bundled - Grok Web / fallback OpenAI-compatible layer
REM  port 3999 (independent from free-api-stack 3010)
REM  SERVER_PORT is pinned to 3999 in configs\config.json
REM ============================================================
cd /d "%~dp0aiclient"
call "%~dp0env.bat"
set NODE_ENV=production
set PORT=3999
%NODE% src\core\master.js
