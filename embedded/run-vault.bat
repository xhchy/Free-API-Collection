@echo off
REM SPDX-License-Identifier: AGPL-3.0-or-later  /  Copyright (C) 2026 XHTV
cd /d "%~dp0..\vault"
call "%~dp0env.bat"
set VAULT_PORT=3997
%PY_BASE% vault.py
