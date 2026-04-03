#!/bin/bash
# ============================================================
#  Rakuten Price Tracker — Full Project Launcher
#  Starts ALL services and triggers full 28.7M product crawl
#
#  Usage:  bash run.sh          (start everything + trigger crawl)
#          bash run.sh stop     (stop all services)
#          bash run.sh status   (check service status)
# ============================================================

set -euo pipefail

ROOT="D:/project/rakuten"
BACKEND="$ROOT/backend"
FRONTEND="$ROOT/frontend"
VENV="$BACKEND/venv/Scripts"
PYTHON="$VENV/python.exe"
PIP="$VENV/pip.exe"
CELERY="$VENV/celery.exe"
LOG_DIR="$BACKEND/logs"
PID_DIR="$BACKEND/.pids"

# ── Colors ────────────────────────────────────────────────
GREEN='\033[0;32m'
RED='\033[0;31m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
NC='\033[0m'

ok()   { echo -e "  ${GREEN}✓${NC} $1"; }
fail() { echo -e "  ${RED}✗${NC} $1"; }
info() { echo -e "  ${CYAN}ℹ${NC} $1"; }
warn() { echo -e "  ${YELLOW}⚠${NC} $1"; }

# ── PID management ────────────────────────────────────────
mkdir -p "$PID_DIR" "$LOG_DIR"

save_pid() { echo "$2" > "$PID_DIR/$1.pid"; }
read_pid() { cat "$PID_DIR/$1.pid" 2>/dev/null || echo ""; }
is_running() {
    local pid=$(read_pid "$1")
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

# ============================================================
#  STOP command
# ============================================================
stop_all() {
    echo ""
    echo "Stopping all Rakuten services..."
    for name in api worker_api worker_list worker_detail worker_misc beat frontend; do
        local pid=$(read_pid "$name")
        if [ -n "$pid" ]; then
            kill "$pid" 2>/dev/null && ok "Stopped $name (PID $pid)" || info "$name already stopped"
            rm -f "$PID_DIR/$name.pid"
        fi
    done
    # Kill any leftover processes on our ports
    for port in 8001; do
        for pid in $(netstat -ano 2>/dev/null | grep ":${port}.*LISTENING" | awk '{print $5}' | sort -u); do
            MSYS_NO_PATHCONV=1 /c/Windows/System32/taskkill.exe /PID "$pid" /F > /dev/null 2>&1 || true
        done
    done
    # Kill celery processes
    MSYS_NO_PATHCONV=1 /c/Windows/System32/taskkill.exe /IM "celery.exe" /F > /dev/null 2>&1 || true
    echo "All services stopped."
}

# ============================================================
#  STATUS command
# ============================================================
show_status() {
    echo ""
    echo "╔══════════════════════════════════════════════════════╗"
    echo "║  Service Status                                      ║"
    echo "╚══════════════════════════════════════════════════════╝"
    for name in api worker_api worker_list worker_detail worker_misc beat frontend; do
        if is_running "$name"; then
            ok "$name is RUNNING (PID $(read_pid $name))"
        else
            fail "$name is STOPPED"
        fi
    done

    echo ""
    # Check product counts
    cd "$BACKEND"
    "$PYTHON" -c "
from app.config import settings
from sqlalchemy import create_engine, text
engine = create_engine(settings.DATABASE_URL_SYNC)
with engine.connect() as conn:
    items = conn.execute(text('SELECT COUNT(*) FROM items')).scalar()
    variants = conn.execute(text('SELECT COUNT(*) FROM variants')).scalar()
    snapshots = conn.execute(text('SELECT COUNT(*) FROM variant_snapshots')).scalar()
    shops = conn.execute(text('SELECT COUNT(*) FROM shops')).scalar()
    pending = conn.execute(text(\"SELECT COUNT(*) FROM crawl_jobs WHERE status='pending'\")).scalar()
    running = conn.execute(text(\"SELECT COUNT(*) FROM crawl_jobs WHERE status='running'\")).scalar()
    done = conn.execute(text(\"SELECT COUNT(*) FROM crawl_jobs WHERE status='done'\")).scalar()
    failed = conn.execute(text(\"SELECT COUNT(*) FROM crawl_jobs WHERE status='failed'\")).scalar()
    detail_pending = conn.execute(text(\"SELECT COUNT(*) FROM crawl_jobs WHERE job_type='detail_scrape' AND status='pending'\")).scalar()
    list_obs = conn.execute(text('SELECT COUNT(*) FROM list_observations')).scalar()
    print(f'  Database:')
    print(f'    Shops:     {shops:>12,}')
    print(f'    Items:     {items:>12,}')
    print(f'    Variants:  {variants:>12,}')
    print(f'    Snapshots: {snapshots:>12,}')
    print(f'    List obs:  {list_obs:>12,}')
    print()
    print(f'  Crawl Jobs:')
    print(f'    Pending:   {pending:>12,}')
    print(f'    Running:   {running:>12,}')
    print(f'    Done:      {done:>12,}')
    print(f'    Failed:    {failed:>12,}')
    print(f'    Detail pending: {detail_pending:>7,}')
" 2>&1 || warn "Could not connect to database"
}

# ============================================================
#  Handle subcommands
# ============================================================
if [ "${1:-}" = "stop" ]; then
    stop_all
    exit 0
fi
if [ "${1:-}" = "status" ]; then
    show_status
    exit 0
fi

# ============================================================
#  MAIN: Start everything
# ============================================================

echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║  Rakuten Price Tracker — Full Project Launcher           ║"
echo "║  Extracts ALL 28,700,394 products automatically          ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""

# ── Step 0: Prerequisites ────────────────────────────────────
echo "[Step 0] Checking prerequisites..."

if [ ! -f "$PYTHON" ]; then
    fail "Python venv not found at $VENV"
    fail "Run: python -m venv $BACKEND/venv"
    exit 1
fi
ok "Python venv"

# Find redis-cli (may not be in PATH on Windows)
REDIS_CLI="redis-cli"
if ! command -v redis-cli &> /dev/null; then
    if [ -f "/c/Program Files/Redis/redis-cli.exe" ]; then
        REDIS_CLI="/c/Program Files/Redis/redis-cli.exe"
    elif [ -f "/c/tools/Redis/redis-cli.exe" ]; then
        REDIS_CLI="/c/tools/Redis/redis-cli.exe"
    fi
fi
"$REDIS_CLI" ping > /dev/null 2>&1 && ok "Redis" || { fail "Redis not running!"; exit 1; }

cd "$BACKEND"
"$PYTHON" -c "
from app.config import settings
from sqlalchemy import create_engine, text
engine = create_engine(settings.DATABASE_URL_SYNC)
with engine.connect() as conn:
    conn.execute(text('SELECT 1'))
    result = conn.execute(text('SHOW TABLES'))
    tables = [r[0] for r in result]
    print(f'{len(tables)} tables')
" 2>/dev/null && ok "MySQL connected" || warn "MySQL check failed — make sure it's running"

echo ""

# ── Step 1: Dependencies ─────────────────────────────────────
echo "[Step 1] Installing dependencies..."
cd "$BACKEND"
"$PIP" install -q -r requirements.txt 2>/dev/null
ok "Python packages"
"$PYTHON" -m playwright install chromium > /dev/null 2>&1 || true
ok "Playwright browsers"
echo ""

# ── Step 2: Database ─────────────────────────────────────────
echo "[Step 2] Database setup..."
cd "$BACKEND"
"$PYTHON" -m alembic upgrade head > /dev/null 2>&1 && ok "Alembic migrations" || warn "Alembic migration failed (tables may already be current)"
echo ""

# ── Step 3: Build frontend ───────────────────────────────────
echo "[Step 3] Building frontend..."
if command -v node &> /dev/null; then
    cd "$FRONTEND"
    [ -d "node_modules" ] || npm install --silent 2>/dev/null
    npm run build --silent 2>/dev/null && ok "Frontend built" || warn "Frontend build failed"
else
    info "Node.js not found — skipping frontend"
fi
echo ""

# ── Step 4: Stop old processes ────────────────────────────────
echo "[Step 4] Stopping old processes..."
stop_all 2>/dev/null || true
sleep 2
ok "Cleanup done"
echo ""

# ── Step 5: Start all services ────────────────────────────────
echo "[Step 5] Starting services..."
cd "$BACKEND"

# 5a. FastAPI
echo "  Starting FastAPI (port 8001)..."
"$PYTHON" -m uvicorn app.main:app --host 0.0.0.0 --port 8001 --workers 2 \
    > "$LOG_DIR/api.log" 2>&1 &
save_pid "api" $!
sleep 3
ok "FastAPI started (PID $!)"

# 5b. Celery Worker — API Discovery
echo "  Starting Worker: API Discovery (10 threads)..."
"$CELERY" -A app.celery_app worker \
    --loglevel=info --pool=threads --concurrency=10 \
    -Q api_discovery -n "api@%h" \
    > "$LOG_DIR/worker_api.log" 2>&1 &
save_pid "worker_api" $!
sleep 2
ok "API Discovery worker (PID $!)"

# 5c. Celery Worker — List Scraping
echo "  Starting Worker: List Scraping (10 threads)..."
"$CELERY" -A app.celery_app worker \
    --loglevel=info --pool=threads --concurrency=10 \
    -Q list_scrape -n "list@%h" \
    > "$LOG_DIR/worker_list.log" 2>&1 &
save_pid "worker_list" $!
sleep 2
ok "List Scraping worker (PID $!)"

# 5d. Celery Worker — Detail Scraping
echo "  Starting Worker: Detail Scraping (30 threads)..."
"$CELERY" -A app.celery_app worker \
    --loglevel=info --pool=threads --concurrency=30 \
    -Q detail_scrape -n "detail@%h" \
    > "$LOG_DIR/worker_detail.log" 2>&1 &
save_pid "worker_detail" $!
sleep 2
ok "Detail Scraping worker (PID $!)"

# 5e. Celery Worker — Aggregation + Cleanup
echo "  Starting Worker: Aggregation + Cleanup..."
"$CELERY" -A app.celery_app worker \
    --loglevel=info --pool=solo \
    -Q aggregation,cleanup -n "misc@%h" \
    > "$LOG_DIR/worker_misc.log" 2>&1 &
save_pid "worker_misc" $!
sleep 2
ok "Misc worker (PID $!)"

# 5f. Celery Beat
echo "  Starting Celery Beat..."
"$CELERY" -A app.celery_app beat --loglevel=info \
    > "$LOG_DIR/beat.log" 2>&1 &
save_pid "beat" $!
sleep 2
ok "Celery Beat (PID $!)"

# 5g. Frontend dev server
if command -v node &> /dev/null; then
    echo "  Starting Frontend dev server (port 5173)..."
    cd "$FRONTEND"
    npm run dev > "$LOG_DIR/frontend.log" 2>&1 &
    save_pid "frontend" $!
    cd "$BACKEND"
    ok "Frontend (PID $!)"
fi

echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║  All services started!                                   ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""

# ── Step 6: Wait for API ─────────────────────────────────────
echo "[Step 6] Waiting for API..."
for i in $(seq 1 15); do
    "$PYTHON" -c "import httpx; r=httpx.get('http://localhost:8001/health',timeout=3); assert r.status_code==200" 2>/dev/null && break
    sleep 2
done
ok "API is ready"
echo ""

# ── Step 7: Trigger full crawl ───────────────────────────────
echo "[Step 7] Triggering FULL CRAWL (28.7M products)..."
echo ""

"$PYTHON" -c "
import httpx, json, sys

BASE = 'http://localhost:8001/api/v1'

# Step A: Check API keys
print('  [A] API Key Status...')
try:
    r = httpx.get(f'{BASE}/crawl-targets/api-key-status', timeout=10)
    ks = r.json()
    total = ks.get('total_keys', 0)
    avail = ks.get('available_keys', 0)
    print(f'      Keys: {total} total, {avail} available')
    if total == 0:
        print('      WARNING: No API keys configured! Set RAKUTEN_API_KEYS in .env')
except Exception as e:
    print(f'      Error: {e}')

# Step B: Trigger full crawl (no login needed for API discovery)
print()
print('  [B] Triggering full crawl pipeline...')
try:
    r = httpx.post(f'{BASE}/crawl-targets/trigger-full-crawl', timeout=30)
    d = r.json()
    print(f'      Status: {d.get(\"status\", \"unknown\")}')
    msg = d.get('message', '')
    # Print message safely (handle emoji)
    try:
        print(f'      {msg[:150]}')
    except UnicodeEncodeError:
        sys.stdout.buffer.write(f'      {msg[:150]}\n'.encode('utf-8'))
except Exception as e:
    print(f'      Error: {e}')

# Step C: Also trigger list scraping for configured genres
print()
print('  [C] Triggering list scrape for configured genres...')
try:
    r = httpx.post(f'{BASE}/crawl-targets/trigger-list-scrape', timeout=10)
    d = r.json()
    print(f'      Status: {d.get(\"status\", \"unknown\")}')
    print(f'      Genres: {d.get(\"genre_ids\", [])}')
except Exception as e:
    print(f'      Error: {e}')

# Step D: Try login (optional, enhances data quality)
print()
print('  [D] Attempting Rakuten login (optional)...')
try:
    r = httpx.get(f'{BASE}/auth/status', timeout=10)
    auth = r.json()
    if auth.get('authenticated'):
        print(f'      Already authenticated (cookies valid)')
    else:
        print(f'      Not authenticated — login with: POST {BASE}/auth/login')
        print(f'      Scraping will continue without login (reduced data quality)')
except Exception as e:
    print(f'      {e}')

print()
print('  ALL PIPELINES TRIGGERED!')
" 2>&1

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║                  RAKUTEN PRICE TRACKER                      ║"
echo "║                  All Systems Running                        ║"
echo "╠══════════════════════════════════════════════════════════════╣"
echo "║                                                             ║"
echo "║  API Server:    http://localhost:8001/docs                  ║"
echo "║  Frontend:      http://localhost:5173                       ║"
echo "║  Health Check:  http://localhost:8001/health                ║"
echo "║                                                             ║"
echo "║  Workers:                                                   ║"
echo "║    API Discovery:   10 threads  (api_discovery queue)       ║"
echo "║    List Scraping:   10 threads  (list_scrape queue)         ║"
echo "║    Detail Scraping: 30 threads  (detail_scrape queue)       ║"
echo "║    Misc (agg+clean): 1 thread   (aggregation,cleanup)      ║"
echo "║                                                             ║"
echo "║  Pipeline: Genre Discovery → API → List → Detail            ║"
echo "║  All 28.7M products will be crawled automatically.          ║"
echo "║                                                             ║"
echo "║  Logs:  $LOG_DIR/                           ║"
echo "║                                                             ║"
echo "║  Commands:                                                  ║"
echo "║    bash run.sh status  — check progress                     ║"
echo "║    bash run.sh stop    — stop everything                    ║"
echo "║                                                             ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "Crawl is running in background. Check logs or run 'bash run.sh status'."
