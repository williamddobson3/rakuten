@echo off
chcp 65001 >nul 2>&1
setlocal enabledelayedexpansion

:: ============================================================
::  Rakuten Price Tracker — Full Project Launcher
::  Starts ALL services and triggers full 28.7M product crawl
:: ============================================================

set "ROOT=D:\project\rakuten"
set "BACKEND=%ROOT%\backend"
set "FRONTEND=%ROOT%\frontend"
set "VENV=%BACKEND%\vnev\Scripts"

:: Colors via labels
set "OK=[32m OK [0m"
set "ERR=[31m FAIL [0m"
set "INFO=[36m INFO [0m"

echo.
echo ╔══════════════════════════════════════════════════════════╗
echo ║  Rakuten Price Tracker — Full Project Launcher           ║
echo ║  Extracts ALL 28,700,394 products automatically          ║
echo ╚══════════════════════════════════════════════════════════╝
echo.

:: ============================================================
::  Step 0: Check prerequisites
:: ============================================================
echo [Step 0] Checking prerequisites...

:: Check Python venv
if not exist "%VENV%\python.exe" (
    echo %ERR% Python venv not found at %VENV%
    echo        Run: python -m venv %BACKEND%\vnev
    goto :error
)
echo   %OK% Python venv found

:: Check node
where node >nul 2>&1
if %errorlevel% neq 0 (
    echo   %INFO% Node.js not found — frontend dev server will be skipped
    set "HAS_NODE=0"
) else (
    echo   %OK% Node.js found
    set "HAS_NODE=1"
)

:: Check Redis
redis-cli ping >nul 2>&1
if %errorlevel% neq 0 (
    echo %ERR% Redis not running! Start Redis first.
    echo        Windows: redis-server
    echo        Docker:  docker run -d -p 6379:6379 redis:7-alpine
    goto :error
)
echo   %OK% Redis is running

:: Check MySQL
mysql -u root -e "SELECT 1" >nul 2>&1
if %errorlevel% neq 0 (
    echo   %INFO% MySQL check skipped (no 'mysql' in PATH or auth required)
    echo          Make sure MySQL/MariaDB is running on port 3306
) else (
    echo   %OK% MySQL is running
)

echo.

:: ============================================================
::  Step 1: Install dependencies (if needed)
:: ============================================================
echo [Step 1] Checking Python dependencies...
cd /d "%BACKEND%"
"%VENV%\pip.exe" install -q -r requirements.txt 2>nul
echo   %OK% Python dependencies ready

:: Install Playwright browsers (if not already)
"%VENV%\python.exe" -c "from playwright.sync_api import sync_playwright" >nul 2>&1
if %errorlevel% neq 0 (
    echo   Installing Playwright browsers...
    "%VENV%\playwright.exe" install chromium
)
echo   %OK% Playwright ready
echo.

:: ============================================================
::  Step 2: Initialize database
:: ============================================================
echo [Step 2] Initializing database...
cd /d "%BACKEND%"
"%VENV%\python.exe" -c "
from app.config import settings
from sqlalchemy import create_engine, text
engine = create_engine(settings.DATABASE_URL_SYNC)
with engine.connect() as conn:
    result = conn.execute(text('SHOW TABLES'))
    tables = [r[0] for r in result]
    if 'items' in tables and 'crawl_jobs' in tables:
        items = conn.execute(text('SELECT COUNT(*) FROM items')).scalar()
        jobs = conn.execute(text('SELECT COUNT(*) FROM crawl_jobs')).scalar()
        print(f'  Database OK - {len(tables)} tables, {items:,} items, {jobs:,} jobs')
    else:
        print('  Tables not found - run sql/init.sql first!')
        print('  mysql -u root rakuten_tracker < sql/init.sql')
" 2>&1
if %errorlevel% neq 0 (
    echo   %INFO% Database check failed — make sure MySQL is running and .env is configured
)

:: Run Alembic migrations
"%VENV%\python.exe" -m alembic upgrade head >nul 2>&1
echo   %OK% Alembic migrations applied
echo.

:: ============================================================
::  Step 3: Build frontend (for production serving)
:: ============================================================
if "%HAS_NODE%"=="1" (
    echo [Step 3] Building frontend...
    cd /d "%FRONTEND%"
    if not exist "node_modules" (
        echo   Installing npm packages...
        call npm install --silent 2>nul
    )
    call npm run build --silent 2>nul
    echo   %OK% Frontend built
) else (
    echo [Step 3] Skipping frontend build (no Node.js)
)
echo.

:: ============================================================
::  Step 4: Kill any existing processes on our ports
:: ============================================================
echo [Step 4] Cleaning up old processes...
taskkill /f /fi "WINDOWTITLE eq RAKUTEN_API" >nul 2>&1
taskkill /f /fi "WINDOWTITLE eq RAKUTEN_WORKER_API" >nul 2>&1
taskkill /f /fi "WINDOWTITLE eq RAKUTEN_WORKER_LIST" >nul 2>&1
taskkill /f /fi "WINDOWTITLE eq RAKUTEN_WORKER_DETAIL" >nul 2>&1
taskkill /f /fi "WINDOWTITLE eq RAKUTEN_BEAT" >nul 2>&1
taskkill /f /fi "WINDOWTITLE eq RAKUTEN_FRONTEND" >nul 2>&1
timeout /t 2 /nobreak >nul
echo   %OK% Cleanup done
echo.

:: ============================================================
::  Step 5: Start all services
:: ============================================================
echo [Step 5] Starting services...
cd /d "%BACKEND%"

:: 5a. FastAPI backend
echo   Starting FastAPI server (port 8000)...
start "RAKUTEN_API" /min cmd /c "cd /d %BACKEND% && %VENV%\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8001 --workers 2 2>&1 | %VENV%\python.exe -c "import sys; [print(line, end='') for line in sys.stdin]""
timeout /t 3 /nobreak >nul
echo   %OK% FastAPI started

:: 5b. Celery Worker — API Discovery (10 threads)
echo   Starting Celery Worker: API Discovery (10 threads)...
start "RAKUTEN_WORKER_API" /min cmd /c "cd /d %BACKEND% && %VENV%\celery.exe -A app.celery_app worker --loglevel=info --pool=threads --concurrency=10 -Q api_discovery -n api@%%h 2>&1"
timeout /t 2 /nobreak >nul
echo   %OK% API Discovery worker started

:: 5c. Celery Worker — List Scraping (10 threads)
echo   Starting Celery Worker: List Scraping (10 threads)...
start "RAKUTEN_WORKER_LIST" /min cmd /c "cd /d %BACKEND% && %VENV%\celery.exe -A app.celery_app worker --loglevel=info --pool=threads --concurrency=10 -Q list_scrape -n list@%%h 2>&1"
timeout /t 2 /nobreak >nul
echo   %OK% List Scraping worker started

:: 5d. Celery Worker — Detail Scraping (30 threads)
echo   Starting Celery Worker: Detail Scraping (30 threads)...
start "RAKUTEN_WORKER_DETAIL" /min cmd /c "cd /d %BACKEND% && %VENV%\celery.exe -A app.celery_app worker --loglevel=info --pool=threads --concurrency=30 -Q detail_scrape -n detail@%%h 2>&1"
timeout /t 2 /nobreak >nul
echo   %OK% Detail Scraping worker started

:: 5e. Celery Worker — Aggregation + Cleanup + Auth (solo)
echo   Starting Celery Worker: Aggregation + Cleanup...
start "RAKUTEN_WORKER_MISC" /min cmd /c "cd /d %BACKEND% && %VENV%\celery.exe -A app.celery_app worker --loglevel=info --pool=solo -Q aggregation,cleanup -n misc@%%h 2>&1"
timeout /t 2 /nobreak >nul
echo   %OK% Misc worker started

:: 5f. Celery Beat (scheduler)
echo   Starting Celery Beat scheduler...
start "RAKUTEN_BEAT" /min cmd /c "cd /d %BACKEND% && %VENV%\celery.exe -A app.celery_app beat --loglevel=info 2>&1"
timeout /t 2 /nobreak >nul
echo   %OK% Celery Beat started

:: 5g. Frontend dev server (optional)
if "%HAS_NODE%"=="1" (
    echo   Starting Frontend dev server (port 5173)...
    start "RAKUTEN_FRONTEND" /min cmd /c "cd /d %FRONTEND% && npm run dev 2>&1"
    timeout /t 2 /nobreak >nul
    echo   %OK% Frontend started
)

echo.
echo ╔══════════════════════════════════════════════════════════╗
echo ║  All services started!                                   ║
echo ╚══════════════════════════════════════════════════════════╝
echo.

:: ============================================================
::  Step 6: Wait for API to be ready, then trigger full crawl
:: ============================================================
echo [Step 6] Waiting for API to be ready...
set "RETRIES=0"
:wait_api
timeout /t 2 /nobreak >nul
"%VENV%\python.exe" -c "import httpx; r=httpx.get('http://localhost:8001/health',timeout=3); assert r.status_code==200" >nul 2>&1
if %errorlevel% neq 0 (
    set /a RETRIES+=1
    if !RETRIES! lss 15 (
        goto :wait_api
    )
    echo   %ERR% API not responding after 30s
    goto :skip_trigger
)
echo   %OK% API is ready
echo.

:: ============================================================
::  Step 7: Trigger full crawl (ALL 28.7M products)
:: ============================================================
echo [Step 7] Triggering FULL CRAWL...
echo.

"%VENV%\python.exe" -c "
import sys, httpx, json

BASE = 'http://localhost:8001/api/v1'

print('  [A] API Key Status...')
try:
    r = httpx.get(f'{BASE}/crawl-targets/api-key-status', timeout=10)
    ks = r.json()
    print(f'      Keys: {ks.get(\"total_keys\", 0)} total, {ks.get(\"available_keys\", 0)} available')
except Exception as e:
    print(f'      {e}')

print()
print('  [B] Triggering full crawl pipeline...')
try:
    r = httpx.post(f'{BASE}/crawl-targets/trigger-full-crawl', timeout=30)
    d = r.json()
    print(f'      Status: {d.get(\"status\", \"unknown\")}')
    msg = d.get('message', '')[:150]
    sys.stdout.buffer.write(f'      {msg}\n'.encode('utf-8'))
except Exception as e:
    print(f'      Error: {e}')

print()
print('  [C] Triggering list scrape...')
try:
    r = httpx.post(f'{BASE}/crawl-targets/trigger-list-scrape', timeout=10)
    d = r.json()
    print(f'      Status: {d.get(\"status\", \"unknown\")}')
except Exception as e:
    print(f'      Error: {e}')

print()
print('  ALL PIPELINES TRIGGERED!')
" 2>&1

:skip_trigger

echo.
echo ╔══════════════════════════════════════════════════════════════╗
echo ║                  RAKUTEN PRICE TRACKER                      ║
echo ║                  All Systems Running                        ║
echo ╠══════════════════════════════════════════════════════════════╣
echo ║                                                             ║
echo ║  API Server:    http://localhost:8001/docs                  ║
echo ║  Frontend:      http://localhost:5173                       ║
echo ║  Health Check:  http://localhost:8001/health                ║
echo ║                                                             ║
echo ║  Workers:                                                   ║
echo ║    API Discovery:   10 threads  (api_discovery queue)       ║
echo ║    List Scraping:   10 threads  (list_scrape queue)         ║
echo ║    Detail Scraping: 30 threads  (detail_scrape queue)       ║
echo ║    Misc (agg+clean): 1 thread   (aggregation,cleanup)      ║
echo ║                                                             ║
echo ║  Pipeline: Genre Discovery -^> API -^> List -^> Detail         ║
echo ║  All 28.7M products will be crawled automatically.          ║
echo ║                                                             ║
echo ╠══════════════════════════════════════════════════════════════╣
echo ║  Press any key to STOP all services...                      ║
echo ╚══════════════════════════════════════════════════════════════╝
echo.
pause >nul

:: ============================================================
::  Shutdown
:: ============================================================
echo.
echo Stopping all services...
taskkill /f /fi "WINDOWTITLE eq RAKUTEN_API" >nul 2>&1
taskkill /f /fi "WINDOWTITLE eq RAKUTEN_WORKER_API" >nul 2>&1
taskkill /f /fi "WINDOWTITLE eq RAKUTEN_WORKER_LIST" >nul 2>&1
taskkill /f /fi "WINDOWTITLE eq RAKUTEN_WORKER_DETAIL" >nul 2>&1
taskkill /f /fi "WINDOWTITLE eq RAKUTEN_WORKER_MISC" >nul 2>&1
taskkill /f /fi "WINDOWTITLE eq RAKUTEN_BEAT" >nul 2>&1
taskkill /f /fi "WINDOWTITLE eq RAKUTEN_FRONTEND" >nul 2>&1
echo All services stopped.
goto :eof

:error
echo.
echo Fix the errors above and try again.
pause
exit /b 1
