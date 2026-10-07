@echo off
title audit-agent  Worker
cd /d D:\deepseek\audit-agent
set PYTHONIOENCODING=utf-8

echo ============================================================
echo    audit-agent   Audit Worker
echo ------------------------------------------------------------
echo    Claims jobs via SKIP LOCKED.
echo    Open more windows to run more workers in parallel.
echo    Close this window to stop.
echo ============================================================
echo.

python -u worker.py --interval 0.4

echo.
echo [Worker stopped]
pause
