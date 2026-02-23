"""
Authentication management endpoints.

Provides endpoints to:
  - Trigger automated Rakuten login
  - Trigger manual login (opens visible browser)
  - Check authentication status
  - Validate cookies online
  - Clear stored cookies
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.config import settings

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    """Login credentials (optional — uses .env defaults if omitted)."""
    email: str | None = None
    password: str | None = None


class LoginResponse(BaseModel):
    success: bool
    message: str
    method: str = ""
    cookie_count: int = 0


class AuthStatusResponse(BaseModel):
    has_cookies: bool
    cookies_valid: bool
    cookie_count: int
    login_method: str
    updated_at: str
    email: str


@router.post("/login", response_model=LoginResponse)
async def login_rakuten(payload: LoginRequest | None = None) -> LoginResponse:
    """
    Login to Rakuten via automated Playwright login.

    If email/password not provided in request body,
    uses RAKUTEN_LOGIN_EMAIL / RAKUTEN_LOGIN_PASSWORD from .env.

    If automated login fails, returns failure status with a message
    suggesting to use the /auth/login-manual endpoint.
    """
    from app.services.rakuten_auth import login_auto

    email = (payload.email if payload and payload.email else settings.RAKUTEN_LOGIN_EMAIL)
    password = (payload.password if payload and payload.password else settings.RAKUTEN_LOGIN_PASSWORD)

    if not email or not password:
        return LoginResponse(
            success=False,
            message=(
                "No credentials provided. Set RAKUTEN_LOGIN_EMAIL and "
                "RAKUTEN_LOGIN_PASSWORD in .env, or pass them in the request body."
            ),
            method="",
            cookie_count=0,
        )

    result = login_auto(email, password)

    # If login succeeded, dispatch Celery tasks to refresh cookies in worker
    if result["success"]:
        try:
            from app.workers.list_scraper import refresh_auth_cookies as lr
            from app.workers.detail_scraper import refresh_auth_cookies as dr
            lr.delay()
            dr.delay()
        except Exception:
            pass  # scrapers may not be imported in the API process

    return LoginResponse(
        success=result["success"],
        message=result["message"],
        method="auto",
        cookie_count=result["cookie_count"],
    )


@router.post("/login-manual", response_model=LoginResponse)
async def login_manual_browser(
    timeout_sec: int = Query(300, description="Max wait time for manual login in seconds"),
) -> LoginResponse:
    """
    Open a visible browser for manual Rakuten login.

    Use this when automated login fails (e.g., CAPTCHA, 2FA).
    A Chromium window will open — complete the login manually.
    The system waits up to timeout_sec (default 300s = 5 minutes).

    NOTE: This endpoint blocks until login completes or times out.
    Only call this from a machine with a display (not inside Docker).
    """
    from app.services.rakuten_auth import login_manual

    result = login_manual(timeout_sec=timeout_sec)

    # If login succeeded, dispatch Celery tasks to refresh cookies in worker
    if result["success"]:
        try:
            from app.workers.list_scraper import refresh_auth_cookies as lr
            from app.workers.detail_scraper import refresh_auth_cookies as dr
            lr.delay()
            dr.delay()
        except Exception:
            pass

    return LoginResponse(
        success=result["success"],
        message=result["message"],
        method="manual",
        cookie_count=result["cookie_count"],
    )


@router.post("/login-fallback", response_model=LoginResponse)
async def login_with_fallback(payload: LoginRequest | None = None) -> LoginResponse:
    """
    Try automated login first; if it fails, fall back to manual login.

    This is the recommended login endpoint.

    Flow:
      1. Attempt headless Playwright login with credentials
      2. If that fails → open visible browser for manual login
    """
    from app.services.rakuten_auth import login_with_fallback as do_login

    email = (payload.email if payload and payload.email else settings.RAKUTEN_LOGIN_EMAIL)
    password = (payload.password if payload and payload.password else settings.RAKUTEN_LOGIN_PASSWORD)

    if not email or not password:
        return LoginResponse(
            success=False,
            message=(
                "No credentials provided. Set RAKUTEN_LOGIN_EMAIL and "
                "RAKUTEN_LOGIN_PASSWORD in .env, or pass them in the request body."
            ),
        )

    result = do_login(email, password)

    # If login succeeded, dispatch Celery tasks to refresh cookies in worker
    if result["success"]:
        try:
            from app.workers.list_scraper import refresh_auth_cookies as lr
            from app.workers.detail_scraper import refresh_auth_cookies as dr
            lr.delay()
            dr.delay()
        except Exception:
            pass

    return LoginResponse(
        success=result["success"],
        message=result["message"],
        method=result.get("method", ""),
        cookie_count=result.get("cookie_count", 0),
    )


@router.get("/status", response_model=AuthStatusResponse)
async def auth_status() -> AuthStatusResponse:
    """
    Check current Rakuten authentication status.

    Returns:
      - has_cookies: Whether any cookies are stored
      - cookies_valid: Whether essential cookies (Ra, Rb) are present
      - cookie_count: Total number of stored cookies
      - login_method: "auto" or "manual"
      - updated_at: When cookies were last saved
      - email: Masked email used for login
    """
    from app.services.rakuten_auth import get_auth_status
    return AuthStatusResponse(**get_auth_status())


@router.post("/validate")
async def validate_cookies_online() -> dict:
    """
    Validate stored cookies by making a request to Rakuten.

    Checks if the session is still active by accessing my.rakuten.co.jp.
    """
    from app.services.rakuten_auth import get_auth_cookies, validate_cookies_online as validate

    cookies = get_auth_cookies()
    if not cookies:
        return {"valid": False, "message": "No cookies stored"}

    is_valid = validate(cookies)
    return {
        "valid": is_valid,
        "message": "Session is active" if is_valid else "Session expired — re-login required",
    }


@router.post("/clear")
async def clear_cookies() -> dict:
    """Clear all stored Rakuten authentication cookies."""
    from app.services.rakuten_auth import clear_auth
    return clear_auth()
