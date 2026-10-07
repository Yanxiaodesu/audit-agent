@echo off
title audit-agent  API  (port 8100)
cd /d D:\deepseek\audit-agent
set PYTHONIOENCODING=utf-8

echo ============================================================
echo    audit-agent   API service
echo ------------------------------------------------------------
echo    Swagger docs : http://127.0.0.1:8100/docs
echo    Health check : http://127.0.0.1:8100/health
echo    Review queue : http://127.0.0.1:8100/review-queue
echo ------------------------------------------------------------
echo    Close this window to stop the service.
echo ============================================================
echo.

python -m uvicorn app.main:app --host 127.0.0.1 --port 8100

echo.
echo [API stopped]
pause
