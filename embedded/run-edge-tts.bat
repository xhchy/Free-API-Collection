@echo off
REM SPDX-License-Identifier: AGPL-3.0-or-later  /  Copyright (C) 2026 XHTV
REM ============================================================
REM  edge-tts-shim - OpenAI-compatible layer over MS Edge TTS
REM  port 3993
REM  free and unlimited, no key required, 14 Chinese voices
REM  requires Python 3.10+ plus:  pip install edge-tts
REM ============================================================
cd /d "%~dp0edge-tts"
call "%~dp0env.bat"
set EDGE_TTS_PORT=3993
%PY_BASE% edge-tts-shim.py
