"""
Shared Playwright Browser Pool.

Provides a single Playwright browser instance shared across ALL Celery
worker tasks (list_scraper, detail_scraper, etc.) in the same process.

Problem solved:
  When using `--pool=solo`, all tasks run in one process. If each scraper
  calls `sync_playwright().start()` independently, the second one fails:
    "It looks like you are using Playwright Sync API inside the asyncio loop."
  By sharing a single Playwright instance, only one event loop is created.

Usage:
  from app.workers.playwright_pool import get_browser_context, inject_cookies

  context = get_browser_context("list_scraper")   # named context
  context = get_browser_context("detail_scraper")  # separate context, same browser

Architecture:
  1 Playwright instance  →  1 Browser (headless Chromium)
                          →  N BrowserContexts (one per scraper name)
  Each context is isolated (separate cookies, sessions) but shares the
  same browser process for efficiency.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from playwright.sync_api import sync_playwright, Playwright, Browser, BrowserContext

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
#  Global singleton state — protected by a lock for thread safety
# ═══════════════════════════════════════════════════════════════

_lock = threading.Lock()
_pw_instance: Playwright | None = None
_pw_browser: Browser | None = None

# Named contexts: {"list_scraper": BrowserContext, "detail_scraper": BrowserContext}
_contexts: dict[str, BrowserContext] = {}

# Per-context cookie tracking
_last_cookie_refresh: dict[str, float] = {}
_is_authenticated: dict[str, bool] = {}

COOKIE_REFRESH_INTERVAL = 300  # seconds


def _ensure_browser() -> Browser:
    """Create Playwright + Browser if not yet started."""
    global _pw_instance, _pw_browser

    if _pw_browser is not None:
        try:
            # Quick liveness check
            _pw_browser.contexts  # noqa: B018
            return _pw_browser
        except Exception:
            logger.warning("Playwright browser dead — recreating...")
            _pw_browser = None
            _pw_instance = None

    logger.info("🚀 Starting shared Playwright browser...")
    _pw_instance = sync_playwright().start()
    _pw_browser = _pw_instance.chromium.launch(
        headless=True,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-setuid-sandbox",
        ],
    )
    logger.info("✅ Shared Playwright browser started (PID %s)", _pw_browser)
    return _pw_browser


def get_browser_context(name: str) -> BrowserContext:
    """
    Get or create a named BrowserContext.

    Each scraper module should use a distinct name (e.g. "list_scraper",
    "detail_scraper") so they get isolated cookie jars but share the
    same underlying Chromium process.

    Periodically refreshes auth cookies in the context.
    """
    with _lock:
        ctx = _contexts.get(name)
        if ctx is not None:
            try:
                ctx.pages  # liveness check  # noqa: B018

                # Periodic cookie refresh
                last = _last_cookie_refresh.get(name, 0.0)
                if time.time() - last > COOKIE_REFRESH_INTERVAL:
                    inject_cookies(name, ctx)

                return ctx
            except Exception:
                logger.warning("Context '%s' is dead — recreating...", name)
                _contexts.pop(name, None)

        browser = _ensure_browser()
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            locale="ja-JP",
            viewport={"width": 1920, "height": 1080},
        )

        # Disable webdriver detection
        ctx.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        """)

        _contexts[name] = ctx

        # Inject auth cookies into the new context
        inject_cookies(name, ctx)

        logger.info("Created browser context '%s'", name)
        return ctx


def inject_cookies(name: str, context: BrowserContext) -> bool:
    """
    Load auth cookies from storage (Redis / file) and inject them
    into the given browser context.

    Returns True if essential auth cookies (Ra, Rb) were found and injected.
    """
    try:
        from app.services.rakuten_auth import get_auth_cookies, validate_cookies
        cookies = get_auth_cookies()
        if cookies and validate_cookies(cookies):
            pw_cookies = []
            for c in cookies:
                domain = c.get("domain", "")
                if ".rakuten.co.jp" in domain:
                    pw_cookies.append({
                        "name": c["name"],
                        "value": c["value"],
                        "domain": domain,
                        "path": c.get("path", "/"),
                    })
            if pw_cookies:
                context.add_cookies(pw_cookies)
                _last_cookie_refresh[name] = time.time()
                _is_authenticated[name] = True
                logger.info(
                    "✅ Injected %d auth cookies into '%s' context (authenticated mode)",
                    len(pw_cookies), name,
                )
                return True

        # Fallback: simple dict format
        from app.services.rakuten_auth import get_auth_cookie_dict
        cookie_dict = get_auth_cookie_dict()
        if cookie_dict:
            pw_cookies = [
                {"name": k, "value": v, "domain": ".rakuten.co.jp", "path": "/"}
                for k, v in cookie_dict.items()
            ]
            context.add_cookies(pw_cookies)
            _last_cookie_refresh[name] = time.time()
            _is_authenticated[name] = True
            logger.info(
                "✅ Injected %d auth cookies (dict) into '%s' context",
                len(pw_cookies), name,
            )
            return True

    except Exception as e:
        logger.warning("Failed to inject auth cookies into '%s': %s", name, e)

    _last_cookie_refresh[name] = time.time()
    _is_authenticated[name] = False
    logger.warning(
        "⚠️ No valid auth cookies found — '%s' running in GUEST mode.",
        name,
    )
    return False


def force_refresh_cookies(name: str) -> bool:
    """
    Force immediate cookie refresh for a named context.
    Called after login to update a running scraper.
    """
    with _lock:
        _last_cookie_refresh[name] = 0
        ctx = _contexts.get(name)
        if ctx is not None:
            return inject_cookies(name, ctx)
    return False


def is_authenticated(name: str) -> bool:
    """Check if a named context has authenticated cookies."""
    return _is_authenticated.get(name, False)


def close_context(name: str) -> None:
    """Close a specific named context."""
    with _lock:
        ctx = _contexts.pop(name, None)
        if ctx:
            try:
                ctx.close()
            except Exception:
                pass
        _last_cookie_refresh.pop(name, None)
        _is_authenticated.pop(name, None)


def close_all() -> None:
    """Close all contexts, the browser, and the Playwright instance."""
    global _pw_instance, _pw_browser
    with _lock:
        for name in list(_contexts.keys()):
            try:
                _contexts[name].close()
            except Exception:
                pass
        _contexts.clear()
        _last_cookie_refresh.clear()
        _is_authenticated.clear()

        if _pw_browser:
            try:
                _pw_browser.close()
            except Exception:
                pass
            _pw_browser = None

        if _pw_instance:
            try:
                _pw_instance.stop()
            except Exception:
                pass
            _pw_instance = None

    logger.info("Shared Playwright pool closed.")
