"""
Celery application configuration.

On worker startup, automatically attempts Rakuten login and injects
auth cookies into scraper browser contexts so all subsequent tasks
run in authenticated mode.
"""

from __future__ import annotations

import logging

from celery import Celery
from celery.schedules import crontab
from celery.signals import worker_ready

from app.config import settings

logger = logging.getLogger(__name__)

celery_app = Celery(
    "rakuten_tracker",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
)

celery_app.conf.update(
    # Serialization
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Tokyo",
    enable_utc=True,

    # Task routing
    task_routes={
        "app.workers.api_discovery.*": {"queue": "api_discovery"},
        "app.workers.list_scraper.*": {"queue": "list_scrape"},
        "app.workers.detail_scraper.*": {"queue": "detail_scrape"},
        "app.workers.ranking_aggregator.*": {"queue": "aggregation"},
        "app.workers.cleanup.*": {"queue": "cleanup"},
    },

    # Rate limiting — no global limit; individual tasks control their own
    # For 28.7M products, throughput must be maximized
    task_default_rate_limit=None,

    # Retry policy
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=8,  # Prefetch 8 tasks per worker for high throughput

    # Result expiry (1 hour)
    result_expires=3600,

    # Task time limits (prevent hung tasks)
    task_soft_time_limit=300,   # 5 min soft limit
    task_time_limit=600,        # 10 min hard limit

    # Beat schedule (periodic tasks)
    beat_schedule={
        # ── Daily full crawl (all genres, all products) ──────
        "daily-full-crawl": {
            "task": "app.workers.api_discovery.discover_all_rakuten_genres",
            "schedule": crontab(hour=2, minute=0),  # 2:00 AM JST daily
            "options": {"queue": "api_discovery"},
        },
        # ── Configured genres discovery (every 6 hours) ──────
        "periodic-genre-discovery": {
            "task": "app.workers.api_discovery.discover_all_genres",
            "schedule": crontab(hour="*/6", minute=15),  # every 6h at :15
            "options": {"queue": "api_discovery"},
        },
        "daily-list-scrape": {
            "task": "app.workers.list_scraper.run_daily_list_scrape",
            "schedule": crontab(hour=3, minute=0),  # 3:00 AM JST
            "options": {"queue": "list_scrape"},
        },

        # ── Detail job dispatcher — HIGH FREQUENCY ───────────
        # 1000 jobs × 360 dispatches/hour = 360,000 jobs/hour
        # With 30 concurrent workers + httpx fast-path: ~3M+ detail pages/day
        "dispatch-detail-jobs": {
            "task": "app.workers.detail_scraper.process_pending_detail_jobs",
            "schedule": 10.0,  # Every 10 seconds
            "kwargs": {"batch_size": settings.DETAIL_BATCH_SIZE},
            "options": {"queue": "detail_scrape"},
        },

        # ── Shop ranking aggregation ─────────────────────────
        "hourly-ranking-aggregation": {
            "task": "app.workers.ranking_aggregator.aggregate_shop_rankings",
            "schedule": crontab(minute=0),  # Every hour
            "options": {"queue": "aggregation"},
        },

        # ── Auto-add top 3 shops to crawl targets (Phase 5) ──
        "daily-top-shops-crawl": {
            "task": "app.workers.ranking_aggregator.auto_add_top_shops",
            "schedule": crontab(hour=1, minute=30),  # 1:30 AM JST
            "options": {"queue": "aggregation"},
        },

        # ── Periodic cookie re-login (every 6 hours) ─────────
        # More frequent re-login to keep long-running crawls authenticated
        "periodic-relogin": {
            "task": "app.workers.auth_tasks.relogin_if_expired",
            "schedule": crontab(hour="*/6", minute=45),  # every 6h (was 12h)
            "options": {"queue": "list_scrape"},
        },

        # ── Data cleanup ─────────────────────────────────────
        "daily-history-cleanup": {
            "task": "app.workers.cleanup.cleanup_old_history",
            "schedule": crontab(hour=4, minute=0),  # 4:00 AM JST
            "options": {"queue": "cleanup"},
        },
        "daily-extension-cleanup": {
            "task": "app.workers.cleanup.cleanup_extension_products",
            "schedule": crontab(hour=4, minute=30),  # 4:30 AM JST
            "options": {"queue": "cleanup"},
        },
        "daily-browse-events-cleanup": {
            "task": "app.workers.cleanup.cleanup_old_browse_events",
            "schedule": crontab(hour=5, minute=0),  # 5:00 AM JST
            "options": {"queue": "cleanup"},
        },
    },
)

# Auto-discover tasks in workers modules
celery_app.autodiscover_tasks([
    "app.workers.api_discovery",
    "app.workers.list_scraper",
    "app.workers.detail_scraper",
    "app.workers.ranking_aggregator",
    "app.workers.cleanup",
    "app.workers.auth_tasks",
])


# ═══════════════════════════════════════════════════════════════════
#  Auto-login on worker startup
# ═══════════════════════════════════════════════════════════════════

@worker_ready.connect
def _on_worker_ready(sender, **kwargs):
    """
    Celery signal handler: fires once when a worker is fully ready to
    accept tasks.  Attempts to login to Rakuten automatically so that
    all subsequent scraping tasks run with authenticated cookies.

    Flow:
      1. Check if valid cookies already exist (Redis / file).
      2. If yes → skip login, just ensure browser contexts pick them up.
      3. If no  → attempt automated Playwright login with .env credentials.
      4. If auto login fails → log a warning (user can use /auth/login-manual).
      5. Dispatch refresh_auth_cookies to list_scraper & detail_scraper queues.
    """
    import redis as _redis

    logger.info(
        "╔══════════════════════════════════════════════════════╗\n"
        "║  Celery worker ready — starting auto-login flow...  ║\n"
        "╚══════════════════════════════════════════════════════╝"
    )

    # ── Acquire a distributed lock to prevent concurrent logins ──────
    LOCK_KEY = "rakuten:auth:login_lock"
    LOCK_TTL = 120  # 2 minutes max for the login process

    try:
        r = _redis.from_url(settings.REDIS_URL, decode_responses=True, socket_connect_timeout=3)
        r.ping()
    except Exception as e:
        logger.error("Cannot connect to Redis — skipping auto-login: %s", e)
        return

    lock = r.lock(LOCK_KEY, timeout=LOCK_TTL, blocking_timeout=5)
    acquired = False
    try:
        acquired = lock.acquire(blocking=True)
    except Exception:
        acquired = False

    if not acquired:
        logger.info(
            "Another worker is already performing login — skipping. "
            "Cookies will be picked up from Redis."
        )
        _dispatch_cookie_refresh()
        return

    try:
        _perform_auto_login(r)
    finally:
        try:
            lock.release()
        except Exception:
            pass

    # ── Tell running scraper contexts to reload cookies ───────────────
    _dispatch_cookie_refresh()


def _perform_auto_login(r) -> None:
    """
    Core login logic:
      1. Check for existing valid cookies
      2. If missing or expired → login with .env credentials
    """
    from app.services.rakuten_auth import (
        cookie_store,
        get_auth_cookies,
        validate_cookies,
        validate_cookies_online,
        login_auto,
        login_with_fallback,
    )

    # ── Step 1: Check if valid cookies already exist ─────────────────
    existing = get_auth_cookies()
    if existing and validate_cookies(existing):
        # Quick offline check passed — optionally do an online check
        logger.info(
            "✅ Found %d existing auth cookies (Ra/Rb present). "
            "Checking if session is still active...",
            len(existing),
        )
        try:
            if validate_cookies_online(existing):
                logger.info(
                    "✅ Existing cookies are still valid — skipping login. "
                    "Scrapers will use cached cookies."
                )
                return
            else:
                logger.warning(
                    "⚠️ Existing cookies expired (online check failed). "
                    "Will re-login..."
                )
        except Exception as e:
            logger.warning(
                "⚠️ Online cookie validation failed (%s). Will re-login...", e
            )

    # ── Step 2: Get credentials from .env ────────────────────────────
    email = settings.RAKUTEN_LOGIN_EMAIL
    password = settings.RAKUTEN_LOGIN_PASSWORD

    if not email or not password:
        logger.warning(
            "╔══════════════════════════════════════════════════════════╗\n"
            "║  ⚠️ RAKUTEN_LOGIN_EMAIL / RAKUTEN_LOGIN_PASSWORD       ║\n"
            "║     not set in .env — cannot auto-login!               ║\n"
            "║                                                         ║\n"
            "║  Scrapers will run in GUEST mode.                       ║\n"
            "║  To enable authenticated scraping:                      ║\n"
            "║    1. Set credentials in .env                           ║\n"
            "║    2. Restart workers, OR                               ║\n"
            "║    3. POST /api/v1/auth/login-manual                    ║\n"
            "╚══════════════════════════════════════════════════════════╝"
        )
        return

    # ── Step 3: Attempt automated login ──────────────────────────────
    logger.info("🔐 Attempting automated Rakuten login for %s...", email[:3] + "***")

    result = login_auto(email, password)

    if result["success"]:
        logger.info(
            "✅ Auto-login successful! %d cookies stored. "
            "All scrapers will run in AUTHENTICATED mode.",
            result["cookie_count"],
        )
        return

    # ── Step 4: Auto login failed → try fallback if display available ─
    logger.warning(
        "⚠️ Automated login failed: %s. "
        "Attempting fallback (manual browser login)...",
        result["message"],
    )

    try:
        fallback = login_with_fallback(email, password)
        if fallback["success"]:
            logger.info(
                "✅ Fallback login successful (method=%s)! %d cookies stored.",
                fallback.get("method", "unknown"),
                fallback.get("cookie_count", 0),
            )
            return
    except Exception as e:
        logger.warning("Fallback login also failed: %s", e)

    logger.error(
        "╔══════════════════════════════════════════════════════════╗\n"
        "║  ❌ ALL login attempts failed.                          ║\n"
        "║                                                         ║\n"
        "║  Scrapers will run in GUEST mode (limited data).        ║\n"
        "║                                                         ║\n"
        "║  To fix:                                                ║\n"
        "║    POST /api/v1/auth/login-manual                       ║\n"
        "║    (opens a visible browser for manual login)            ║\n"
        "╚══════════════════════════════════════════════════════════╝"
    )


def _dispatch_cookie_refresh() -> None:
    """
    Send refresh_auth_cookies tasks to list_scraper and detail_scraper
    queues so their Playwright browser contexts pick up the latest cookies.
    """
    try:
        celery_app.send_task(
            "app.workers.list_scraper.refresh_auth_cookies",
            queue="list_scrape",
        )
        logger.debug("Dispatched cookie refresh → list_scrape queue")
    except Exception as e:
        logger.warning("Failed to dispatch list_scraper cookie refresh: %s", e)

    try:
        celery_app.send_task(
            "app.workers.detail_scraper.refresh_auth_cookies",
            queue="detail_scrape",
        )
        logger.debug("Dispatched cookie refresh → detail_scrape queue")
    except Exception as e:
        logger.warning("Failed to dispatch detail_scraper cookie refresh: %s", e)
