"""
Test Suite: Rakuten Login Feature

Tests the authentication system:
  - CookieStore (save/load/clear, file + Redis persistence)
  - Cookie utilities (filter, convert to httpx dict, header string)
  - Cookie validation (offline essential-cookie check, online session check)
  - Automated login (_login_with_playwright)
  - Manual login (_manual_login_with_playwright)
  - High-level login functions (login_auto, login_manual, login_with_fallback)
  - Auth status / clear
  - Auth API endpoints (FastAPI router)
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest


# ─────────────────────────────────────────────────────────────
#  Fixtures
# ─────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _patch_settings():
    """Patch settings for tests."""
    with patch("app.config.settings") as mock_settings:
        mock_settings.REDIS_URL = "redis://localhost:6379/0"
        mock_settings.RAKUTEN_LOGIN_EMAIL = "test@example.com"
        mock_settings.RAKUTEN_LOGIN_PASSWORD = "testpassword123"
        yield mock_settings


@pytest.fixture
def sample_cookies() -> list[dict]:
    """Realistic Rakuten cookie set."""
    return [
        {
            "name": "Ra",
            "value": "abc123token_long_string",
            "domain": ".rakuten.co.jp",
            "path": "/",
            "expires": 1740000000,
            "httpOnly": True,
            "secure": True,
        },
        {
            "name": "Rb",
            "value": "def456secondary_token",
            "domain": ".rakuten.co.jp",
            "path": "/",
            "expires": 1740000000,
            "httpOnly": True,
            "secure": True,
        },
        {
            "name": "Rz",
            "value": "session_tracking_value",
            "domain": ".rakuten.co.jp",
            "path": "/",
            "expires": 1740000000,
            "httpOnly": False,
            "secure": True,
        },
        {
            "name": "_ga",
            "value": "GA1.2.123456789",
            "domain": ".google.com",
            "path": "/",
            "expires": 1740000000,
            "httpOnly": False,
            "secure": False,
        },
    ]


@pytest.fixture
def valid_cookies(sample_cookies) -> list[dict]:
    """Cookies that pass validation (contain Ra and Rb)."""
    return sample_cookies


@pytest.fixture
def invalid_cookies() -> list[dict]:
    """Cookies missing essential auth tokens."""
    return [
        {
            "name": "_ga",
            "value": "GA1.2.123456789",
            "domain": ".google.com",
            "path": "/",
        },
        {
            "name": "tracking",
            "value": "somevalue",
            "domain": ".rakuten.co.jp",
            "path": "/",
        },
    ]


@pytest.fixture
def temp_cookie_file(tmp_path):
    """Temporary cookie file path for testing."""
    return tmp_path / "test_cookies.json"


# ═══════════════════════════════════════════════════════════════
#  1. Cookie Utility Tests
# ═══════════════════════════════════════════════════════════════

class TestCookieUtilities:
    """Tests for cookie filtering, conversion, and header building."""

    def test_filter_rakuten_cookies(self, sample_cookies):
        """Should only keep .rakuten.co.jp domain cookies."""
        from app.services.rakuten_auth import filter_rakuten_cookies

        filtered = filter_rakuten_cookies(sample_cookies)
        assert len(filtered) == 3  # Ra, Rb, Rz (not _ga from .google.com)
        domains = {c["domain"] for c in filtered}
        assert domains == {".rakuten.co.jp"}

    def test_filter_empty_list(self):
        from app.services.rakuten_auth import filter_rakuten_cookies
        assert filter_rakuten_cookies([]) == []

    def test_cookies_to_httpx_dict(self, sample_cookies):
        """Should convert Playwright cookies to {name: value} dict for httpx."""
        from app.services.rakuten_auth import cookies_to_httpx_dict

        result = cookies_to_httpx_dict(sample_cookies)
        assert result["Ra"] == "abc123token_long_string"
        assert result["Rb"] == "def456secondary_token"
        assert result["Rz"] == "session_tracking_value"
        # Non-rakuten cookies should be excluded
        assert "_ga" not in result

    def test_cookies_to_httpx_dict_empty(self):
        from app.services.rakuten_auth import cookies_to_httpx_dict
        assert cookies_to_httpx_dict([]) == {}

    def test_cookies_to_header_string(self, sample_cookies):
        """Should produce a proper Cookie header string."""
        from app.services.rakuten_auth import cookies_to_header_string

        header = cookies_to_header_string(sample_cookies)
        assert "Ra=abc123token_long_string" in header
        assert "Rb=def456secondary_token" in header
        assert "; " in header  # Proper separator

    def test_cookies_to_header_string_empty(self):
        from app.services.rakuten_auth import cookies_to_header_string
        assert cookies_to_header_string([]) == ""


# ═══════════════════════════════════════════════════════════════
#  2. Cookie Validation Tests
# ═══════════════════════════════════════════════════════════════

class TestCookieValidation:
    """Tests for offline and online cookie validation."""

    def test_validate_cookies_valid(self, valid_cookies):
        """Should return True when Ra and Rb are present."""
        from app.services.rakuten_auth import validate_cookies
        assert validate_cookies(valid_cookies) is True

    def test_validate_cookies_missing_ra(self, valid_cookies):
        """Should return False when Ra is missing."""
        from app.services.rakuten_auth import validate_cookies
        cookies = [c for c in valid_cookies if c["name"] != "Ra"]
        assert validate_cookies(cookies) is False

    def test_validate_cookies_missing_rb(self, valid_cookies):
        """Should return False when Rb is missing."""
        from app.services.rakuten_auth import validate_cookies
        cookies = [c for c in valid_cookies if c["name"] != "Rb"]
        assert validate_cookies(cookies) is False

    def test_validate_cookies_empty(self):
        from app.services.rakuten_auth import validate_cookies
        assert validate_cookies([]) is False
        assert validate_cookies(None) is False

    @patch("httpx.Client")
    def test_validate_cookies_online_valid(self, mock_client_class, valid_cookies):
        """Online validation: 200 from my.rakuten.co.jp = valid."""
        from app.services.rakuten_auth import validate_cookies_online

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.url = "https://my.rakuten.co.jp/"

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = mock_resp
        mock_client_class.return_value = mock_client

        assert validate_cookies_online(valid_cookies) is True

    @patch("httpx.Client")
    def test_validate_cookies_online_expired(self, mock_client_class, valid_cookies):
        """Online validation: redirect to login page = expired."""
        from app.services.rakuten_auth import validate_cookies_online

        mock_resp = MagicMock()
        mock_resp.status_code = 302
        mock_resp.url = "https://login.account.rakuten.com/sso/authorize?..."

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = mock_resp
        mock_client_class.return_value = mock_client

        assert validate_cookies_online(valid_cookies) is False

    @patch("httpx.Client")
    def test_validate_cookies_online_error(self, mock_client_class, valid_cookies):
        """Online validation: network error = False."""
        from app.services.rakuten_auth import validate_cookies_online

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.side_effect = Exception("Connection refused")
        mock_client_class.return_value = mock_client

        assert validate_cookies_online(valid_cookies) is False

    def test_validate_cookies_online_empty(self):
        from app.services.rakuten_auth import validate_cookies_online
        assert validate_cookies_online([]) is False


# ═══════════════════════════════════════════════════════════════
#  3. CookieStore Tests
# ═══════════════════════════════════════════════════════════════

class TestCookieStore:
    """Tests for the CookieStore class (file + Redis persistence)."""

    @patch("app.services.rakuten_auth.COOKIE_FILE_PATH")
    @patch("app.services.rakuten_auth.redis.from_url")
    def test_save_and_load_from_file(self, mock_redis_from_url, mock_cookie_path, valid_cookies, tmp_path):
        """Should save cookies to file and load them back."""
        from app.services.rakuten_auth import CookieStore

        # Redis not available
        mock_redis_from_url.side_effect = Exception("Redis unavailable")

        cookie_file = tmp_path / "cookies.json"
        mock_cookie_path.__str__ = MagicMock(return_value=str(cookie_file))
        mock_cookie_path.parent = tmp_path
        mock_cookie_path.exists.side_effect = lambda: cookie_file.exists()
        mock_cookie_path.write_text = cookie_file.write_text
        mock_cookie_path.read_text = cookie_file.read_text

        store = CookieStore()

        # Save
        store.save(valid_cookies, login_method="auto", email="test@example.com")
        assert cookie_file.exists()

        # Verify file content
        saved = json.loads(cookie_file.read_text(encoding="utf-8"))
        assert len(saved["cookies"]) == len(valid_cookies)
        assert saved["login_method"] == "auto"
        assert saved["email"] == "t***@example.com"  # masked

        # Load
        loaded = store.load()
        assert loaded is not None
        assert len(loaded) == len(valid_cookies)

    @patch("app.services.rakuten_auth.redis.from_url")
    def test_save_to_redis(self, mock_redis_from_url, valid_cookies):
        """Should save cookies to Redis when available."""
        from app.services.rakuten_auth import CookieStore

        mock_redis = MagicMock()
        mock_redis.ping.return_value = True
        mock_redis_from_url.return_value = mock_redis

        store = CookieStore()
        store.save(valid_cookies, login_method="auto", email="test@example.com")

        # Verify Redis calls
        mock_redis.set.assert_called_once()
        mock_redis.hset.assert_called_once()
        mock_redis.expire.assert_called()  # TTL set

    @patch("app.services.rakuten_auth.redis.from_url")
    def test_load_from_redis(self, mock_redis_from_url, valid_cookies):
        """Should load cookies from Redis first."""
        from app.services.rakuten_auth import CookieStore

        mock_redis = MagicMock()
        mock_redis.ping.return_value = True
        mock_redis.get.return_value = json.dumps(valid_cookies)
        mock_redis_from_url.return_value = mock_redis

        store = CookieStore()
        loaded = store.load()

        assert loaded is not None
        assert len(loaded) == len(valid_cookies)
        mock_redis.get.assert_called_once()

    @patch("app.services.rakuten_auth.redis.from_url")
    def test_clear_cookies(self, mock_redis_from_url):
        """Should clear cookies from both Redis and file."""
        from app.services.rakuten_auth import CookieStore

        mock_redis = MagicMock()
        mock_redis.ping.return_value = True
        mock_redis_from_url.return_value = mock_redis

        store = CookieStore()
        store.clear()

        mock_redis.delete.assert_called_once()

    @patch("app.services.rakuten_auth.redis.from_url")
    def test_get_meta_from_redis(self, mock_redis_from_url):
        """Should return metadata about stored cookies."""
        from app.services.rakuten_auth import CookieStore

        mock_redis = MagicMock()
        mock_redis.ping.return_value = True
        mock_redis.hgetall.return_value = {
            "updated_at": "2026-02-22T12:00:00",
            "login_method": "auto",
            "email": "t***@example.com",
            "cookie_count": "42",
        }
        mock_redis_from_url.return_value = mock_redis

        store = CookieStore()
        meta = store.get_meta()

        assert meta["login_method"] == "auto"
        assert meta["cookie_count"] == "42"

    def test_mask_email(self):
        from app.services.rakuten_auth import CookieStore
        assert CookieStore._mask_email("test@example.com") == "t***@example.com"
        assert CookieStore._mask_email("a@b.com") == "a***@b.com"
        assert CookieStore._mask_email("") == "***"
        assert CookieStore._mask_email("no-at-sign") == "***"


# ═══════════════════════════════════════════════════════════════
#  4. Automated Login Tests
# ═══════════════════════════════════════════════════════════════

class TestAutomatedLogin:
    """Tests for the Playwright-based automated login."""

    @patch("app.services.rakuten_auth.cookie_store")
    @patch("app.services.rakuten_auth._login_with_playwright")
    def test_login_auto_success(self, mock_pw_login, mock_store, valid_cookies):
        """Successful auto login should save cookies and return success."""
        from app.services.rakuten_auth import login_auto

        mock_pw_login.return_value = valid_cookies

        result = login_auto("test@example.com", "password123")

        assert result["success"] is True
        assert result["message"] == "Automated login successful"
        assert result["cookie_count"] == 3  # Only rakuten domain cookies
        mock_store.save.assert_called_once()

    @patch("app.services.rakuten_auth.cookie_store")
    @patch("app.services.rakuten_auth._login_with_playwright")
    def test_login_auto_missing_essential_cookies(self, mock_pw_login, mock_store):
        """Login that produces cookies without Ra/Rb should fail."""
        from app.services.rakuten_auth import login_auto

        mock_pw_login.return_value = [
            {"name": "tracking", "value": "abc", "domain": ".rakuten.co.jp"},
        ]

        result = login_auto("test@example.com", "password123")

        assert result["success"] is False
        assert "essential cookies" in result["message"]
        mock_store.save.assert_not_called()

    @patch("app.services.rakuten_auth._login_with_playwright")
    def test_login_auto_playwright_error(self, mock_pw_login):
        """Playwright errors should be caught and returned as failure."""
        from app.services.rakuten_auth import login_auto

        mock_pw_login.side_effect = RuntimeError("Login timed out")

        result = login_auto("test@example.com", "password123")

        assert result["success"] is False
        assert "timed out" in result["message"]
        assert result["cookie_count"] == 0


# ═══════════════════════════════════════════════════════════════
#  5. Manual Login Tests
# ═══════════════════════════════════════════════════════════════

class TestManualLogin:
    """Tests for the manual (visible browser) login."""

    @patch("app.services.rakuten_auth.cookie_store")
    @patch("app.services.rakuten_auth._manual_login_with_playwright")
    def test_login_manual_success(self, mock_manual, mock_store, valid_cookies):
        """Successful manual login should save cookies."""
        from app.services.rakuten_auth import login_manual

        mock_manual.return_value = valid_cookies

        result = login_manual(timeout_sec=30)

        assert result["success"] is True
        assert result["message"] == "Manual login successful"
        mock_store.save.assert_called_once()

    @patch("app.services.rakuten_auth._manual_login_with_playwright")
    def test_login_manual_timeout(self, mock_manual):
        """Manual login timeout should return failure."""
        from app.services.rakuten_auth import login_manual

        mock_manual.side_effect = RuntimeError("Manual login timed out after 300 seconds")

        result = login_manual()

        assert result["success"] is False
        assert "timed out" in result["message"]


# ═══════════════════════════════════════════════════════════════
#  6. Login with Fallback Tests
# ═══════════════════════════════════════════════════════════════

class TestLoginWithFallback:
    """Tests for login_with_fallback (auto → manual)."""

    @patch("app.services.rakuten_auth.login_manual")
    @patch("app.services.rakuten_auth.login_auto")
    def test_auto_success_skips_manual(self, mock_auto, mock_manual):
        """If auto login succeeds, manual login should not be tried."""
        from app.services.rakuten_auth import login_with_fallback

        mock_auto.return_value = {
            "success": True,
            "message": "Automated login successful",
            "cookie_count": 42,
        }

        result = login_with_fallback("test@example.com", "password")

        assert result["success"] is True
        assert result["method"] == "auto"
        mock_manual.assert_not_called()

    @patch("app.services.rakuten_auth.login_manual")
    @patch("app.services.rakuten_auth.login_auto")
    def test_auto_fail_falls_back_to_manual(self, mock_auto, mock_manual):
        """If auto login fails, should fall back to manual."""
        from app.services.rakuten_auth import login_with_fallback

        mock_auto.return_value = {
            "success": False,
            "message": "CAPTCHA detected",
            "cookie_count": 0,
        }
        mock_manual.return_value = {
            "success": True,
            "message": "Manual login successful",
            "cookie_count": 30,
        }

        result = login_with_fallback("test@example.com", "password")

        assert result["success"] is True
        assert result["method"] == "manual"


# ═══════════════════════════════════════════════════════════════
#  7. Auth Status / Get Cookies Tests
# ═══════════════════════════════════════════════════════════════

class TestAuthStatus:
    """Tests for get_auth_status, get_auth_cookies, etc."""

    @patch("app.services.rakuten_auth.cookie_store")
    def test_get_auth_status_with_valid_cookies(self, mock_store, valid_cookies):
        """Should report valid status when cookies exist and are valid."""
        from app.services.rakuten_auth import get_auth_status

        mock_store.load.return_value = valid_cookies
        mock_store.get_meta.return_value = {
            "updated_at": "2026-02-22T12:00:00",
            "login_method": "auto",
            "email": "t***@example.com",
        }

        status = get_auth_status()

        assert status["has_cookies"] is True
        assert status["cookies_valid"] is True
        assert status["cookie_count"] == 4
        assert status["login_method"] == "auto"

    @patch("app.services.rakuten_auth.cookie_store")
    def test_get_auth_status_no_cookies(self, mock_store):
        """Should report no cookies when nothing is stored."""
        from app.services.rakuten_auth import get_auth_status

        mock_store.load.return_value = None
        mock_store.get_meta.return_value = {}

        status = get_auth_status()

        assert status["has_cookies"] is False
        assert status["cookies_valid"] is False
        assert status["cookie_count"] == 0

    @patch("app.services.rakuten_auth.cookie_store")
    def test_get_auth_cookies_valid(self, mock_store, valid_cookies):
        """Should return cookies when valid."""
        from app.services.rakuten_auth import get_auth_cookies

        mock_store.load.return_value = valid_cookies
        cookies = get_auth_cookies()

        assert cookies is not None
        assert len(cookies) == 4

    @patch("app.services.rakuten_auth.cookie_store")
    def test_get_auth_cookies_none_when_invalid(self, mock_store, invalid_cookies):
        """Should return None when essential cookies are missing."""
        from app.services.rakuten_auth import get_auth_cookies

        mock_store.load.return_value = invalid_cookies
        cookies = get_auth_cookies()

        assert cookies is None

    @patch("app.services.rakuten_auth.cookie_store")
    def test_get_auth_cookie_dict(self, mock_store, valid_cookies):
        """Should return httpx-compatible dict."""
        from app.services.rakuten_auth import get_auth_cookie_dict

        mock_store.load.return_value = valid_cookies
        cookie_dict = get_auth_cookie_dict()

        assert "Ra" in cookie_dict
        assert "Rb" in cookie_dict

    @patch("app.services.rakuten_auth.cookie_store")
    def test_get_auth_cookie_dict_empty_when_no_cookies(self, mock_store):
        """Should return empty dict when no cookies stored."""
        from app.services.rakuten_auth import get_auth_cookie_dict

        mock_store.load.return_value = None
        cookie_dict = get_auth_cookie_dict()

        assert cookie_dict == {}

    @patch("app.services.rakuten_auth.cookie_store")
    def test_clear_auth(self, mock_store):
        """Should clear all cookies and return status."""
        from app.services.rakuten_auth import clear_auth

        result = clear_auth()

        assert result["status"] == "cleared"
        mock_store.clear.assert_called_once()


# ═══════════════════════════════════════════════════════════════
#  8. Auth API Endpoint Tests (FastAPI)
# ═══════════════════════════════════════════════════════════════

class TestAuthAPIEndpoints:
    """Tests for the FastAPI auth router endpoints."""

    @pytest.fixture
    def client(self):
        """Create a test client for the FastAPI app."""
        from fastapi.testclient import TestClient
        from app.main import app
        return TestClient(app)

    @patch("app.services.rakuten_auth.login_auto")
    def test_login_endpoint_success(self, mock_login, client):
        """POST /api/v1/auth/login should call login_auto."""
        mock_login.return_value = {
            "success": True,
            "message": "Automated login successful",
            "cookie_count": 42,
        }

        resp = client.post("/api/v1/auth/login", json={
            "email": "test@example.com",
            "password": "password123",
        })

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["method"] == "auto"

    def test_login_endpoint_no_credentials(self, client):
        """Should return error when no credentials provided."""
        with patch("app.api.auth.settings") as mock_s:
            mock_s.RAKUTEN_LOGIN_EMAIL = ""
            mock_s.RAKUTEN_LOGIN_PASSWORD = ""
            resp = client.post("/api/v1/auth/login", json={})

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert "No credentials" in data["message"]

    @patch("app.services.rakuten_auth.get_auth_status")
    def test_status_endpoint(self, mock_status, client):
        """GET /api/v1/auth/status should return auth status."""
        mock_status.return_value = {
            "has_cookies": True,
            "cookies_valid": True,
            "cookie_count": 42,
            "login_method": "auto",
            "updated_at": "2026-02-22T12:00:00",
            "email": "t***@example.com",
        }

        resp = client.get("/api/v1/auth/status")

        assert resp.status_code == 200
        data = resp.json()
        assert data["has_cookies"] is True
        assert data["cookies_valid"] is True

    @patch("app.services.rakuten_auth.clear_auth")
    def test_clear_endpoint(self, mock_clear, client):
        """POST /api/v1/auth/clear should clear cookies."""
        mock_clear.return_value = {
            "status": "cleared",
            "message": "All authentication cookies have been cleared",
        }

        resp = client.post("/api/v1/auth/clear")

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "cleared"

    def test_validate_endpoint_no_cookies(self, client):
        """POST /api/v1/auth/validate with no cookies should return invalid."""
        with patch("app.services.rakuten_auth.get_auth_cookies", return_value=None):
            resp = client.post("/api/v1/auth/validate")

        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is False


# ═══════════════════════════════════════════════════════════════
#  9. Playwright Login Flow Tests (mocked)
# ═══════════════════════════════════════════════════════════════

class TestPlaywrightLoginFlow:
    """Tests for the Playwright login mechanics (fully mocked)."""

    @patch("app.services.rakuten_auth.time.sleep")
    def test_login_with_playwright_flow(self, mock_sleep):
        """Full automated login flow should navigate, fill, click, and extract cookies."""
        from unittest.mock import call

        # Build elaborate mock chain for Playwright
        mock_page = MagicMock()
        mock_page.url = "https://www.rakuten.co.jp/"

        mock_context = MagicMock()
        mock_context.new_page.return_value = mock_page
        mock_context.cookies.return_value = [
            {"name": "Ra", "value": "token1", "domain": ".rakuten.co.jp"},
            {"name": "Rb", "value": "token2", "domain": ".rakuten.co.jp"},
        ]

        mock_browser = MagicMock()
        mock_browser.new_context.return_value = mock_context

        mock_pw = MagicMock()
        mock_pw.chromium.launch.return_value = mock_browser

        with patch("playwright.sync_api.sync_playwright") as mock_sync_pw:
            mock_sync_pw.return_value.__enter__ = MagicMock(return_value=mock_pw)
            mock_sync_pw.return_value.__exit__ = MagicMock(return_value=False)

            from app.services.rakuten_auth import _login_with_playwright
            cookies = _login_with_playwright("test@example.com", "pass123", headless=True)

        assert len(cookies) == 2
        assert cookies[0]["name"] == "Ra"

        # Verify the login flow steps
        mock_page.goto.assert_called_once()
        mock_page.wait_for_selector.assert_called()  # Email input wait
        mock_page.wait_for_url.assert_called()  # Redirect wait

    @patch("app.services.rakuten_auth.time.sleep")
    def test_login_with_playwright_timeout(self, mock_sleep):
        """Playwright timeout should raise RuntimeError."""
        mock_page = MagicMock()

        # Import the correct timeout error
        mock_page.goto.side_effect = Exception("Timeout 30000ms exceeded")

        mock_context = MagicMock()
        mock_context.new_page.return_value = mock_page

        mock_browser = MagicMock()
        mock_browser.new_context.return_value = mock_context

        mock_pw = MagicMock()
        mock_pw.chromium.launch.return_value = mock_browser

        with patch("playwright.sync_api.sync_playwright") as mock_sync_pw:
            mock_sync_pw.return_value.__enter__ = MagicMock(return_value=mock_pw)
            mock_sync_pw.return_value.__exit__ = MagicMock(return_value=False)

            from app.services.rakuten_auth import _login_with_playwright

            with pytest.raises(RuntimeError, match="login failed"):
                _login_with_playwright("test@example.com", "pass", headless=True)
