@echo off
title audit-agent  Launcher
cd /d D:\deepseek\audit-agent

echo ============================================================
echo    audit-agent   one-click start
echo ============================================================
echo.

echo [1/2] starting API window ...
start "audit-agent API" cmd /k "D:\deepseek\audit-agent\start-api.cmd"
timeout /t 3 /nobreak >nul

echo [2/2] starting Worker window ...
start "audit-agent Worker" cmd /k "D:\deepseek\audit-agent\start-worker.cmd"
timeout /t 2 /nobreak >nul

echo.
echo ============================================================
echo    Two windows opened:
echo      audit-agent API      http://127.0.0.1:8100/docs
echo      audit-agent Worker   consumes the audit queue
echo.
echo    Close those two windows to stop the services.
echo ============================================================
echo.
timeout /t 6 >nul
