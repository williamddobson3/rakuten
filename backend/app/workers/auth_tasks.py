"""
Authentication Tasks.

Periodic Celery tasks for cookie management:
  - relogin_if_expired: checks if auth cookies are still valid and re-logins
    if they have expired.  Runs every 12 hours via beat schedule.
"""

from __future__ import annotations

import logging

from app.celery_app import celery_app
from app.config import settings

logger = logging.getLogger(__name__)


@celery_app.task(
    name="app.workers.auth_tasks.relogin_if_expired",
    rate_limit="1/h",
)
def relogin_if_expired() -> dict:
    """
    Check if Rakuten auth cookies are still valid.
    If expired or missing, attempt automated re-login.

    This task is scheduled every 12 hours by celery beat.
    It ensures scrapers always have fresh authenticated cookies
    even across long-running worker sessions.

    Returns:
        Status dict with login result.
    """
    from app.services.rakuten_auth import (
        get_auth_cookies,
        validate_cookies,
        validate_cookies_online,
        login_auto,
    )

    # ── Step 1: Check existing cookies ────────────────────────────
    existing = get_auth_cookies()
    if existing and validate_cookies(existing):
        try:
            if validate_cookies_online(existing):
                logger.info(
                    "✅ [relogin_if_expired] Cookies are still valid — no action needed."
                )
                return {
                    "status": "ok",
                    "action": "none",
                    "message": "Cookies still valid",
                }
        except Exception as e:
            logger.warning(
                "Online validation failed (%s) — will attempt re-login.", e
            )

    # ── Step 2: Cookies missing or expired → re-login ─────────────
    email = settings.RAKUTEN_LOGIN_EMAIL
    password = settings.RAKUTEN_LOGIN_PASSWORD

    if not email or not password:
        logger.warning(
            "[relogin_if_expired] No credentials in .env — cannot re-login. "
            "Scrapers will continue in guest mode."
        )
        return {
            "status": "skipped",
            "action": "none",
            "message": "No credentials configured",
        }

    logger.info("🔐 [relogin_if_expired] Cookies expired — attempting re-login...")

    result = login_auto(email, password)

    if result["success"]:
        logger.info(
            "✅ [relogin_if_expired] Re-login successful! %d cookies stored.",
            result["cookie_count"],
        )
        # Refresh cookies in running browser contexts
        _dispatch_cookie_refresh()
        return {
            "status": "ok",
            "action": "relogin",
            "message": f"Re-login successful ({result['cookie_count']} cookies)",
        }

    logger.error(
        "❌ [relogin_if_expired] Re-login failed: %s. "
        "Use POST /api/v1/auth/login-manual to login manually.",
        result["message"],
    )
    return {
        "status": "failed",
        "action": "relogin_attempted",
        "message": result["message"],
    }


def _dispatch_cookie_refresh() -> None:
    """Dispatch cookie refresh tasks to scraper workers."""
    try:
        celery_app.send_task(
            "app.workers.list_scraper.refresh_auth_cookies",
            queue="list_scrape",
        )
    except Exception:
        pass

    try:
        celery_app.send_task(
            "app.workers.detail_scraper.refresh_auth_cookies",
            queue="detail_scrape",
        )
    except Exception:
        pass
