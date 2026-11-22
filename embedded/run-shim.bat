@echo off
REM SPDX-License-Identifier: AGPL-3.0-or-later  /  Copyright (C) 2026 XHTV
REM ============================================================
REM  cf-ai-shim bundled - Cloudflare Workers AI vision/text layer
REM  port 3998 (independent from free-api-stack 3025)
REM ============================================================
cd /d "%~dp0shim"
call "%~dp0env.bat"
set CF_SHIM_PORT=3998
REM Credentials are NOT hardcoded. They are read at runtime from the
REM "cloudflare" section of vault\keys.json, or overridden by the env
REM vars CF_ACCOUNT_ID / CF_API_TOKEN (template: vault\keys.example.json).
REM Optional: uncomment and adjust if Cloudflare needs a local proxy.
REM set CF_SHIM_PROXY=http://127.0.0.1:7890
%PY_BASE% cf-ai-shim.py
