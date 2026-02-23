"""
Rakuten Authentication Service.

Manages login to Rakuten via Playwright and stores session cookies.

Login flow:
  1. Navigate to Rakuten SSO login page
  2. Enter email in #user_id input
  3. Click "Next" button #cta001
  4. Enter password in #password_current input
  5. Click "Next" button #cta011
  6. Wait for redirect to rakuten.co.jp
  7. Extract all cookies for .rakuten.co.jp domain

Cookie management:
  - Cookies are stored in both a local JSON file and Redis
  - Cookies are validated before use (check if session is still active)
  - If automated login fails, opens a visible browser for manual login

Key cookies for authenticated sessions:
  - Ra  (session cookie, .rakuten.co.jp)  → main auth token
  - Rb  (session cookie, .rakuten.co.jp)  → secondary auth
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import redis

from app.config import settings

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
#  Constants
# ═══════════════════════════════════════════════════════════════

RAKUTEN_LOGIN_URL = (
    "https://login.account.rakuten.com/sso/authorize"
    "?client_id=rakuten_ichiba_top_web"
    "&service_id=s245"
    "&response_type=code"
    "&scope=openid"
    "&redirect_uri=https%3A%2F%2Fwww.rakuten.co.jp%2F"
    "#/sign_in"
)

# After successful login, should redirect to this domain
RAKUTEN_HOME_URL = "https://www.rakuten.co.jp/"

# CSS selectors for login form elements
SELECTOR_EMAIL_INPUT = "#user_id"
SELECTOR_NEXT_BUTTON_1 = "#cta001"       # "Next" after email
SELECTOR_PASSWORD_INPUT = "#password_current"
SELECTOR_NEXT_BUTTON_2 = "#cta011"       # "Next" after password

# Cookie file path (relative to backend root)
COOKIE_FILE_PATH = Path("data/rakuten_cookies.json")

# Redis key for cookies
REDIS_COOKIE_KEY = "rakuten:auth:cookies"
REDIS_COOKIE_META_KEY = "rakuten:auth:meta"

# Essential auth cookies that must be present
ESSENTIAL_COOKIES = ["Ra", "Rb"]

# Domains to capture cookies from
COOKIE_DOMAINS = [".rakuten.co.jp"]


# ═══════════════════════════════════════════════════════════════
#  Cookie Storage
# ═══════════════════════════════════════════════════════════════

class CookieStore:
    """
    Persists cookies to both a local JSON file and Redis.

    Storage format (JSON):
    {
        "cookies": [ {name, value, domain, path, expires, ...}, ... ],
        "updated_at": "2026-02-21T...",
        "login_method": "auto" | "manual",
        "email": "masked_email"
    }
    """

    def __init__(self) -> None:
        self._redis: redis.Redis | None = None
        try:
            self._redis = redis.from_url(
                settings.REDIS_URL,
                decode_responses=True,
                socket_connect_timeout=3,
            )
            self._redis.ping()
        except Exception as e:
            logger.warning("Redis not available for cookie storage: %s", e)
            self._redis = None

    def save(
        self,
        cookies: list[dict],
        login_method: str = "auto",
        email: str = "",
    ) -> None:
        """Save cookies to file and Redis."""
        masked_email = self._mask_email(email)
        payload = {
            "cookies": cookies,
            "updated_at": datetime.utcnow().isoformat(),
            "login_method": login_method,
            "email": masked_email,
        }

        # Save to file
        try:
            COOKIE_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
            COOKIE_FILE_PATH.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            logger.info("Cookies saved to file: %s", COOKIE_FILE_PATH)
        except Exception as e:
            logger.error("Failed to save cookies to file: %s", e)

        # Save to Redis
        if self._redis:
            try:
                self._redis.set(
                    REDIS_COOKIE_KEY,
                    json.dumps(cookies, ensure_ascii=False),
                )
                meta = {
                    "updated_at": payload["updated_at"],
                    "login_method": login_method,
                    "email": masked_email,
                    "cookie_count": str(len(cookies)),
                }
                for field, value in meta.items():
                    self._redis.hset(REDIS_COOKIE_META_KEY, field, value)
                # Set TTL of 24 hours (cookies may expire, force re-login daily)
                self._redis.expire(REDIS_COOKIE_KEY, 86400)
                self._redis.expire(REDIS_COOKIE_META_KEY, 86400)
                logger.info("Cookies saved to Redis (%d cookies)", len(cookies))
            except Exception as e:
                logger.error("Failed to save cookies to Redis: %s", e)

    def load(self) -> list[dict] | None:
        """
        Load cookies from Redis first, fall back to file.

        Returns list of cookie dicts, or None if no cookies stored.
        """
        # Try Redis first
        if self._redis:
            try:
                data = self._redis.get(REDIS_COOKIE_KEY)
                if data:
                    cookies = json.loads(data)
                    logger.debug("Loaded %d cookies from Redis", len(cookies))
                    return cookies
            except Exception as e:
                logger.warning("Failed to load cookies from Redis: %s", e)

        # Fall back to file
        try:
            if COOKIE_FILE_PATH.exists():
                payload = json.loads(COOKIE_FILE_PATH.read_text(encoding="utf-8"))
                cookies = payload.get("cookies", [])
                logger.debug("Loaded %d cookies from file", len(cookies))
                return cookies
        except Exception as e:
            logger.warning("Failed to load cookies from file: %s", e)

        return None

    def get_meta(self) -> dict[str, str]:
        """Get metadata about stored cookies."""
        # Try Redis
        if self._redis:
            try:
                meta = self._redis.hgetall(REDIS_COOKIE_META_KEY)
                if meta:
                    return meta
            except Exception:
                pass

        # Fall back to file
        try:
            if COOKIE_FILE_PATH.exists():
                payload = json.loads(COOKIE_FILE_PATH.read_text(encoding="utf-8"))
                return {
                    "updated_at": payload.get("updated_at", ""),
                    "login_method": payload.get("login_method", ""),
                    "email": payload.get("email", ""),
                    "cookie_count": str(len(payload.get("cookies", []))),
                }
        except Exception:
            pass

        return {}

    def clear(self) -> None:
        """Clear all stored cookies."""
        if self._redis:
            try:
                self._redis.delete(REDIS_COOKIE_KEY, REDIS_COOKIE_META_KEY)
            except Exception:
                pass
        try:
            if COOKIE_FILE_PATH.exists():
                COOKIE_FILE_PATH.unlink()
        except Exception:
            pass

    @staticmethod
    def _mask_email(email: str) -> str:
        """Mask email for safe storage: test@example.com → t***@example.com"""
        if not email or "@" not in email:
            return "***"
        local, domain = email.split("@", 1)
        if len(local) <= 1:
            return f"{local}***@{domain}"
        return f"{local[0]}***@{domain}"


# Global cookie store
cookie_store = CookieStore()


# ═══════════════════════════════════════════════════════════════
#  Playwright Login
# ═══════════════════════════════════════════════════════════════

def _login_with_playwright(
    email: str,
    password: str,
    headless: bool = True,
    timeout_ms: int = 30000,
) -> list[dict]:
    """
    Login to Rakuten using Playwright.

    Args:
        email: Rakuten account email.
        password: Rakuten account password.
        headless: If True, run browser in headless mode (automated).
                  If False, open visible browser (for manual login fallback).
        timeout_ms: Max wait time for each step in milliseconds.

    Returns:
        List of cookie dicts from the authenticated session.

    Raises:
        RuntimeError: If login fails.
    """
    from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

    cookies: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
            ],
        )

        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
        )

        # Disable webdriver detection
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        """)

        page = context.new_page()

        try:
            # ── Step 1: Navigate to login page ─────────────────
            logger.info("Navigating to Rakuten login page...")
            page.goto(RAKUTEN_LOGIN_URL, wait_until="networkidle", timeout=timeout_ms)

            # Wait for email input to be ready
            page.wait_for_selector(
                SELECTOR_EMAIL_INPUT,
                state="visible",
                timeout=timeout_ms,
            )

            # ── Step 2: Enter email ────────────────────────────
            logger.info("Entering email...")
            email_input = page.locator(SELECTOR_EMAIL_INPUT)
            email_input.click()
            # Type with small delay to mimic human input
            email_input.fill(email)
            time.sleep(0.5)

            # ── Step 3: Click "Next" button after email ────────
            logger.info("Clicking Next (email)...")
            next_btn_1 = page.locator(SELECTOR_NEXT_BUTTON_1)
            next_btn_1.click()

            # ── Step 4: Wait for password field ────────────────
            page.wait_for_selector(
                SELECTOR_PASSWORD_INPUT,
                state="visible",
                timeout=timeout_ms,
            )
            time.sleep(0.5)

            # ── Step 5: Enter password ─────────────────────────
            logger.info("Entering password...")
            pw_input = page.locator(SELECTOR_PASSWORD_INPUT)
            pw_input.click()
            pw_input.fill(password)
            time.sleep(0.5)

            # ── Step 6: Click "Next" button after password ─────
            logger.info("Clicking Next (password)...")
            next_btn_2 = page.locator(SELECTOR_NEXT_BUTTON_2)
            next_btn_2.click()

            # ── Step 7: Wait for redirect to rakuten.co.jp ─────
            logger.info("Waiting for login redirect...")
            page.wait_for_url(
                "**/www.rakuten.co.jp/**",
                timeout=timeout_ms,
            )
            # Extra wait for all cookies to be set
            time.sleep(3)

            logger.info("Login successful! Extracting cookies...")

            # ── Step 8: Extract all cookies ─────────────────────
            cookies = context.cookies()

            # Verify essential cookies are present
            cookie_names = {c["name"] for c in cookies}
            missing = [name for name in ESSENTIAL_COOKIES if name not in cookie_names]
            if missing:
                logger.warning(
                    "Login succeeded but missing essential cookies: %s", missing
                )

            logger.info(
                "Extracted %d cookies (%d from .rakuten.co.jp)",
                len(cookies),
                sum(1 for c in cookies if ".rakuten.co.jp" in c.get("domain", "")),
            )

        except PwTimeout as e:
            logger.error("Login timeout: %s", e)
            raise RuntimeError(f"Rakuten login timed out: {e}") from e
        except Exception as e:
            logger.error("Login error: %s", e, exc_info=True)
            raise RuntimeError(f"Rakuten login failed: {e}") from e
        finally:
            browser.close()

    return cookies


def _manual_login_with_playwright(timeout_sec: int = 300) -> list[dict]:
    """
    Open a visible browser for manual login.

    The user must complete the login process manually.
    The browser stays open until either:
      - The user successfully logs in (detected by URL change)
      - The timeout expires (default 5 minutes)

    Returns:
        List of cookie dicts from the authenticated session.

    Raises:
        RuntimeError: If manual login times out.
    """
    from playwright.sync_api import sync_playwright

    cookies: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,  # MUST be visible for manual login
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--start-maximized",
            ],
        )

        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
        )

        page = context.new_page()

        try:
            logger.info(
                "Opening browser for MANUAL login. "
                "Please complete login within %d seconds...",
                timeout_sec,
            )
            page.goto(RAKUTEN_LOGIN_URL, wait_until="networkidle", timeout=60000)

            # Poll until we detect successful login (redirect to rakuten.co.jp)
            start = time.time()
            logged_in = False

            while time.time() - start < timeout_sec:
                current_url = page.url
                if "www.rakuten.co.jp" in current_url and "login" not in current_url:
                    # Wait a bit more for cookies to settle
                    time.sleep(3)
                    logged_in = True
                    break
                time.sleep(2)

            if not logged_in:
                raise RuntimeError(
                    f"Manual login timed out after {timeout_sec} seconds. "
                    "Please try again."
                )

            logger.info("Manual login detected! Extracting cookies...")
            cookies = context.cookies()

            logger.info(
                "Extracted %d cookies from manual login",
                len(cookies),
            )

        except Exception as e:
            if "timed out" not in str(e).lower():
                logger.error("Manual login error: %s", e, exc_info=True)
            raise
        finally:
            browser.close()

    return cookies


# ═══════════════════════════════════════════════════════════════
#  Cookie Utilities
# ═══════════════════════════════════════════════════════════════

def filter_rakuten_cookies(cookies: list[dict]) -> list[dict]:
    """
    Filter cookies to only include .rakuten.co.jp domain cookies.

    These are the cookies needed for authenticated scraping.
    """
    return [
        c for c in cookies
        if any(domain in c.get("domain", "") for domain in COOKIE_DOMAINS)
    ]


def cookies_to_httpx_dict(cookies: list[dict]) -> dict[str, str]:
    """
    Convert Playwright cookie list to a simple {name: value} dict
    for use with httpx requests.

    Only includes .rakuten.co.jp domain cookies.
    """
    result: dict[str, str] = {}
    for c in cookies:
        domain = c.get("domain", "")
        if any(d in domain for d in COOKIE_DOMAINS):
            result[c["name"]] = c["value"]
    return result


def cookies_to_header_string(cookies: list[dict]) -> str:
    """
    Convert cookies to a single Cookie header string.

    Format: "name1=value1; name2=value2; ..."
    """
    httpx_dict = cookies_to_httpx_dict(cookies)
    return "; ".join(f"{k}={v}" for k, v in httpx_dict.items())


def validate_cookies(cookies: list[dict]) -> bool:
    """
    Check if cookies appear to be valid (have essential auth cookies).

    This does NOT make an HTTP request — just checks presence.
    For full validation, use validate_cookies_online().
    """
    if not cookies:
        return False

    cookie_names = {c["name"] for c in cookies}
    return all(name in cookie_names for name in ESSENTIAL_COOKIES)


def validate_cookies_online(cookies: list[dict]) -> bool:
    """
    Validate cookies by making a request to Rakuten and checking
    if we're still logged in.

    Returns True if the session is still valid.
    """
    import httpx

    cookie_dict = cookies_to_httpx_dict(cookies)
    if not cookie_dict:
        return False

    try:
        with httpx.Client(
            timeout=15,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            },
            cookies=cookie_dict,
        ) as client:
            # Check the my page — if logged in, won't redirect to login
            resp = client.get("https://my.rakuten.co.jp/")

            # If we're redirected to login page, cookies are expired
            if "login.account.rakuten.com" in str(resp.url):
                logger.info("Cookies are expired (redirected to login)")
                return False

            # Check for logged-in indicators in the response
            if resp.status_code == 200:
                logger.info("Cookies are valid (session active)")
                return True

            return False

    except Exception as e:
        logger.error("Cookie validation error: %s", e)
        return False


# ═══════════════════════════════════════════════════════════════
#  High-Level Auth Functions
# ═══════════════════════════════════════════════════════════════

def login_auto(email: str, password: str) -> dict:
    """
    Attempt automated login to Rakuten.

    Steps:
      1. Try headless Playwright login
      2. Store cookies on success
      3. Return status

    Returns:
        {"success": True/False, "message": str, "cookie_count": int}
    """
    try:
        logger.info("Attempting automated Rakuten login...")
        raw_cookies = _login_with_playwright(
            email=email,
            password=password,
            headless=True,
        )

        # Filter to Rakuten domain cookies
        rakuten_cookies = filter_rakuten_cookies(raw_cookies)

        if not validate_cookies(rakuten_cookies):
            return {
                "success": False,
                "message": (
                    "Login completed but essential cookies (Ra, Rb) not found. "
                    "Try manual login."
                ),
                "cookie_count": len(rakuten_cookies),
            }

        # Save cookies
        cookie_store.save(
            cookies=raw_cookies,  # Save ALL cookies (not just rakuten domain)
            login_method="auto",
            email=email,
        )

        return {
            "success": True,
            "message": "Automated login successful",
            "cookie_count": len(rakuten_cookies),
        }

    except RuntimeError as e:
        logger.warning("Automated login failed: %s", e)
        return {
            "success": False,
            "message": str(e),
            "cookie_count": 0,
        }


def login_manual(timeout_sec: int = 300) -> dict:
    """
    Open a visible browser for manual login.

    The user must complete the login in the browser window.

    Returns:
        {"success": True/False, "message": str, "cookie_count": int}
    """
    try:
        logger.info("Opening browser for manual Rakuten login...")
        raw_cookies = _manual_login_with_playwright(timeout_sec=timeout_sec)

        rakuten_cookies = filter_rakuten_cookies(raw_cookies)

        if not validate_cookies(rakuten_cookies):
            return {
                "success": False,
                "message": "Manual login completed but essential cookies not found.",
                "cookie_count": len(rakuten_cookies),
            }

        cookie_store.save(
            cookies=raw_cookies,
            login_method="manual",
            email="(manual)",
        )

        return {
            "success": True,
            "message": "Manual login successful",
            "cookie_count": len(rakuten_cookies),
        }

    except RuntimeError as e:
        logger.warning("Manual login failed: %s", e)
        return {
            "success": False,
            "message": str(e),
            "cookie_count": 0,
        }


def login_with_fallback(email: str, password: str) -> dict:
    """
    Try automated login first; if it fails, fall back to manual login.

    This is the primary login function to use.

    Returns:
        {"success": True/False, "message": str, "method": "auto"|"manual"}
    """
    # Step 1: Try auto login
    result = login_auto(email, password)
    if result["success"]:
        return {**result, "method": "auto"}

    logger.warning(
        "Automated login failed (%s). Falling back to manual login...",
        result["message"],
    )

    # Step 2: Fall back to manual login
    result = login_manual()
    return {**result, "method": "manual"}


def get_auth_cookies() -> list[dict] | None:
    """
    Get valid authentication cookies.

    Loads from storage, validates, and returns.
    Returns None if no valid cookies are available.
    """
    cookies = cookie_store.load()
    if not cookies:
        logger.warning("No stored cookies found")
        return None

    if not validate_cookies(cookies):
        logger.warning("Stored cookies are missing essential auth tokens")
        return None

    return cookies


def get_auth_cookie_dict() -> dict[str, str]:
    """
    Get authentication cookies as a {name: value} dict for httpx.

    Returns empty dict if no valid cookies available.
    """
    cookies = get_auth_cookies()
    if not cookies:
        return {}
    return cookies_to_httpx_dict(cookies)


def get_auth_status() -> dict:
    """
    Get current authentication status.

    Returns:
        {
            "has_cookies": bool,
            "cookies_valid": bool,
            "cookie_count": int,
            "login_method": str,
            "updated_at": str,
            "email": str,
        }
    """
    meta = cookie_store.get_meta()
    cookies = cookie_store.load()

    has_cookies = cookies is not None and len(cookies) > 0
    cookies_valid = validate_cookies(cookies) if has_cookies else False

    return {
        "has_cookies": has_cookies,
        "cookies_valid": cookies_valid,
        "cookie_count": len(cookies) if cookies else 0,
        "login_method": meta.get("login_method", ""),
        "updated_at": meta.get("updated_at", ""),
        "email": meta.get("email", ""),
    }


def clear_auth() -> dict:
    """Clear all stored authentication cookies."""
    cookie_store.clear()
    return {"status": "cleared", "message": "All authentication cookies have been cleared"}
