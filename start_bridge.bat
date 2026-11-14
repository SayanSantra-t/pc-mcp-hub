@echo off
title Local MCP Bridge - Permanent Cloudflare Tunnel
cd /d "%~dp0"

echo ========================================================================
echo   Starting Local MCP Bridge...
echo ========================================================================

where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [X] Python was not found in PATH!
    echo Please install Python 3 or add it to PATH.
    pause
    exit /b 1
)

python bridge_v2.py %*

if %errorlevel% neq 0 (
    echo.
    echo Bridge exited with code %errorlevel%.
    pause
)
