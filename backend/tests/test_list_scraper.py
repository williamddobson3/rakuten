"""
Test Suite: Product List Page Scraping (List Scraper Worker)

Tests the 2nd stage of the pipeline:
  - Embedded JSON extraction (window.__INITIAL_STATE__)
  - JSON item parsing (_parse_json_item)
  - HTML fallback parsing (_parse_search_results_from_html)
  - Combined parser (_parse_search_results)
  - Page fetch with auth cookies + retry
  - Main scrape task (scrape_list_page)
  - Paginated scraper orchestration
  - Helper functions (point text, coupon text, price extraction)
"""

from __future__ import annotations

import json
import re
from unittest.mock import MagicMock, patch, PropertyMock

import pytest


# ─────────────────────────────────────────────────────────────
#  Fixtures
# ─────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _patch_settings():
    """Patch settings for tests."""
    with patch("app.config.settings") as mock_settings:
        mock_settings.REQUEST_DELAY_SEC = 0
        mock_settings.PROXY_POOL_URL = ""
        mock_settings.RAKUTEN_MAX_PAGES = 100
        mock_settings.REDIS_URL = "redis://localhost:6379/0"
        yield mock_settings


def _make_json_item(
    name: str = "靴 メンズ スリッポン",
    price: int = 6270,
    shop_code: str = "380959",
    shop_url_code: str = "moriashizakka",
    shop_name: str = "moriashizakka",
    url: str = "https://item.rakuten.co.jp/moriashizakka/edm544/",
) -> dict:
    """Build a realistic __INITIAL_STATE__ item dict."""
    return {
        "name": name,
        "price": price,
        "subtitle": "EDWIN エドウィン スリッポン",
        "url": f"https://search.rakuten.co.jp/redirect?url={url}",
        "originalItemUrl": url,
        "code": "10000395",
        "images": [{"url": "https://thumbnail.image.rakuten.co.jp/@0_mall/moriashizakka/cabinet/img001.jpg"}],
        "genreIdList": "/0/558885/110983/558926",
        "isSoldOut": False,
        "itemOptions": {"superDeal": False, "shop39": True, "cpc": {}},
        "shop": {
            "name": shop_name,
            "id": int(shop_code),
            "code": shop_code,
            "urlCode": shop_url_code,
        },
        "shipping": {
            "price": 0,
            "estimateDeliveryDay": "2/24 12:00までの注文で最短2/25お届け",
            "deliveryDays": None,
        },
        "point": {
            "count": 285,
            "baseMultiplier": 1,
            "shopMultiplier": 5,
            "pointUpMultiplier": 4,
            "dealMultiplier": 0,
            "itemMultiplier": 0,
        },
        "review": {"score": 4.65, "numReviews": 49},
        "coupons": [{"discount": 15, "discountType": "percentage"}],
        "variantId": "12054",
        "skuInfo": {"hasMultiSku": True, "priceRange": "6270"},
        "tags": [{"id": 1000909, "name": "エドウイン", "tag_group": {"name": "ブランド"}}],
        "genres": [{"id": 110983, "name": "メンズ靴"}],
    }


def _make_initial_state_html(items: list[dict] | None = None) -> str:
    """Build HTML containing window.__INITIAL_STATE__ with items."""
    if items is None:
        items = [_make_json_item()]

    state = {
        "state": {
            "searchPage": {
                "ichibaSearch": {
                    "items": items,
                    "pagination": {
                        "numFound": 1200,
                        "pageSize": 45,
                        "currentPage": 1,
                    },
                }
            }
        }
    }

    state_json = json.dumps(state, ensure_ascii=False)
    return f"""
    <html>
    <head><title>Rakuten Search</title></head>
    <body>
    <script>
        window.__INITIAL_STATE__ = {state_json};
    </script>
    <div id="root"></div>
    </body>
    </html>
    """


def _make_html_card_page() -> str:
    """Build HTML with product cards for HTML fallback parsing."""
    return """
    <html>
    <head><title>Rakuten Search</title></head>
    <body>
    <div class="dui-card searchresultitem"
         data-track-price="8580"
         data-track-price-ranges="8580"
         data-shop-id="261122"
         data-track-variantid="54321">
        <a class="image-link-wrapper--3XCNg" href="https://item.rakuten.co.jp/sneak/abc123/">
            <img class="image--abc" src="https://img.example.com/product1.jpg" />
        </a>
        <a class="title-link--xyz" title="ナイキ エアマックス メンズ" href="#">ナイキ エアマックス メンズ</a>
        <div class="price--3zUvK">8,580<span>円</span></div>
        <span class="free-shipping-label--1shop">送料無料</span>
        <div class="points--DNEud">780ポイント(1倍+9倍UP)</div>
        <div class="coupon">200円OFFクーポンあり</div>
        <div class="shipping">12:00までの注文で最短2/22(翌日)お届け</div>
        <span class="score">4.75</span><span class="legend">(169件)</span>
        <a data-rpp-url-copy="shop" href="https://www.rakuten.co.jp/sneak/">スニークオンラインショップ</a>
    </div>

    <div class="dui-card searchresultitem"
         data-track-price="3200"
         data-shop-id="123456">
        <a class="image-link-wrapper--3XCNg" href="https://item.rakuten.co.jp/shopb/item999/">
            <img class="image--def" src="https://img.example.com/product2.jpg" />
        </a>
        <a class="title-link--xyz" title="スニーカー レディース" href="#">スニーカー レディース</a>
        <div class="price--3zUvK">3,200<span>円</span></div>
        <div class="points--DNEud">32ポイント(1倍)</div>
        <span class="score">3.50</span><span class="legend">(12件)</span>
        <a data-rpp-url-copy="shop" href="https://www.rakuten.co.jp/shopb/">ショップB</a>
    </div>
    </body>
    </html>
    """


# ═══════════════════════════════════════════════════════════════
#  1. Embedded JSON Extraction Tests
# ═══════════════════════════════════════════════════════════════

class TestExtractInitialState:
    """Tests for extracting window.__INITIAL_STATE__ from HTML."""

    def test_extract_valid_json(self):
        """Should parse valid __INITIAL_STATE__ JSON."""
        from app.workers.list_scraper import _extract_initial_state

        html = _make_initial_state_html()
        state = _extract_initial_state(html)

        assert state is not None
        assert "state" in state

    def test_extract_no_initial_state(self):
        """Should return None when __INITIAL_STATE__ is absent."""
        from app.workers.list_scraper import _extract_initial_state

        html = "<html><body>No state here</body></html>"
        state = _extract_initial_state(html)

        assert state is None

    def test_extract_malformed_json(self):
        """Should return None on malformed JSON."""
        from app.workers.list_scraper import _extract_initial_state

        html = """<script>window.__INITIAL_STATE__ = {invalid json};</script>"""
        state = _extract_initial_state(html)

        assert state is None


class TestFindIchibaItems:
    """Tests for navigating the state tree to find ichibaSearch.items[]."""

    def test_find_items_in_state(self):
        """Should find items in the nested state structure."""
        from app.workers.list_scraper import _find_ichiba_items

        state = {
            "state": {
                "searchPage": {
                    "ichibaSearch": {
                        "items": [_make_json_item(), _make_json_item()],
                        "pagination": {"numFound": 100},
                    }
                }
            }
        }

        items, pagination = _find_ichiba_items(state)
        assert len(items) == 2
        assert pagination is not None
        assert pagination["numFound"] == 100

    def test_empty_state(self):
        """Should return empty list for empty state."""
        from app.workers.list_scraper import _find_ichiba_items

        items, pagination = _find_ichiba_items({})
        assert items == []
        assert pagination is None


# ═══════════════════════════════════════════════════════════════
#  2. JSON Item Parsing Tests
# ═══════════════════════════════════════════════════════════════

class TestParseJsonItem:
    """Tests for parsing individual items from __INITIAL_STATE__."""

    def test_parse_full_item(self):
        """Should extract all fields from a complete JSON item."""
        from app.workers.list_scraper import _parse_json_item

        item = _make_json_item()
        parsed = _parse_json_item(item)

        # Basic info
        assert parsed["item_name"] == "靴 メンズ スリッポン"
        assert parsed["price"] == 6270
        assert parsed["item_code"] == "10000395"
        assert parsed["variant_id"] == "12054"
        assert parsed["is_sold_out"] is False

        # Item URL: should prefer originalItemUrl
        assert parsed["item_url"] == "https://item.rakuten.co.jp/moriashizakka/edm544/"

        # Image
        assert "thumbnail.image.rakuten.co.jp" in parsed["image_url"]

        # Shop
        assert parsed["shop_code"] == "380959"
        assert parsed["shop_name"] == "moriashizakka"
        assert parsed["shop_url_code"] == "moriashizakka"

        # Points
        assert parsed["point_count"] == 285
        assert parsed["point_base_multiplier"] == 1
        assert parsed["point_shop_multiplier"] == 5
        assert parsed["point_up_multiplier"] == 4

        # Coupon
        assert parsed["coupon_discount"] == 15
        assert parsed["coupon_type"] == "percentage"

        # Shipping
        assert parsed["shipping_cost"] == 0
        assert parsed["is_free_shipping"] is True
        assert "最短2/25お届け" in parsed["delivery_text"]

        # Review
        assert parsed["review_score"] == 4.65
        assert parsed["review_count"] == 49

        # Flags
        assert parsed["is_super_deal"] is False
        assert parsed["is_shop39"] is True

        # SKU
        assert parsed["has_multi_sku"] is True

        # Data source
        assert parsed["data_source"] == "json"

    def test_parse_minimal_item(self):
        """Should handle items with minimal/missing fields."""
        from app.workers.list_scraper import _parse_json_item

        item = {
            "name": "Simple Item",
            "price": 100,
            "url": "https://item.rakuten.co.jp/shop/item1/",
        }
        parsed = _parse_json_item(item)

        assert parsed["item_name"] == "Simple Item"
        assert parsed["price"] == 100
        assert parsed["item_url"] == "https://item.rakuten.co.jp/shop/item1/"
        assert parsed["point_count"] is None
        assert parsed["coupon_discount"] is None
        assert parsed["is_free_shipping"] is False

    def test_parse_sold_out_item(self):
        """Sold-out items should be flagged correctly."""
        from app.workers.list_scraper import _parse_json_item

        item = _make_json_item()
        item["isSoldOut"] = True
        parsed = _parse_json_item(item)

        assert parsed["is_sold_out"] is True

    def test_parse_super_deal_item(self):
        """Super DEAL items should be flagged."""
        from app.workers.list_scraper import _parse_json_item

        item = _make_json_item()
        item["itemOptions"]["superDeal"] = True
        parsed = _parse_json_item(item)

        assert parsed["is_super_deal"] is True


# ═══════════════════════════════════════════════════════════════
#  3. JSON Full Parser Tests
# ═══════════════════════════════════════════════════════════════

class TestParseSearchResultsFromJson:
    """Tests for the full JSON extraction pipeline."""

    def test_parse_valid_page(self):
        """Should parse items and pagination from valid __INITIAL_STATE__."""
        from app.workers.list_scraper import _parse_search_results_from_json

        html = _make_initial_state_html([_make_json_item(), _make_json_item()])
        items, pagination = _parse_search_results_from_json(html)

        assert len(items) == 2
        assert pagination is not None

    def test_parse_empty_page(self):
        """Should return empty list for page without __INITIAL_STATE__."""
        from app.workers.list_scraper import _parse_search_results_from_json

        html = "<html><body>Nothing here</body></html>"
        items, pagination = _parse_search_results_from_json(html)

        assert items == []
        assert pagination is None


# ═══════════════════════════════════════════════════════════════
#  4. HTML Fallback Parser Tests
# ═══════════════════════════════════════════════════════════════

class TestParseSearchResultsFromHTML:
    """Tests for the HTML fallback parser."""

    def test_parse_card_elements(self):
        """Should extract product data from dui-card elements."""
        from app.workers.list_scraper import _parse_search_results_from_html

        html = _make_html_card_page()
        results = _parse_search_results_from_html(html)

        assert len(results) == 2

        # First card
        card1 = results[0]
        assert card1["price"] == 8580
        assert card1["item_name"] == "ナイキ エアマックス メンズ"
        assert card1["is_free_shipping"] is True
        assert card1["shipping_cost"] == 0
        assert "item.rakuten.co.jp/sneak/abc123/" in card1["item_url"]
        assert card1["shop_code"] == "261122"
        assert card1["variant_id"] == "54321"
        assert card1["data_source"] == "html"

        # Points parsing
        assert card1["point_count"] == 780
        assert card1["point_base_multiplier"] == 1
        assert card1["point_up_multiplier"] == 9

        # Coupon parsing
        assert card1["coupon_discount"] == 200
        assert card1["coupon_type"] == "exact"

        # Review
        assert card1["review_score"] == 4.75
        assert card1["review_count"] == 169

        # Shop name
        assert card1["shop_name"] == "スニークオンラインショップ"
        assert card1["shop_url_code"] == "sneak"

    def test_parse_card_no_free_shipping(self):
        """Cards without free-shipping label should have is_free_shipping=False."""
        from app.workers.list_scraper import _parse_search_results_from_html

        html = _make_html_card_page()
        results = _parse_search_results_from_html(html)

        card2 = results[1]
        assert card2["is_free_shipping"] is False
        assert card2["shipping_cost"] is None

    def test_parse_empty_html(self):
        """Should return empty list for pages without product cards."""
        from app.workers.list_scraper import _parse_search_results_from_html

        html = "<html><body><p>No products</p></body></html>"
        results = _parse_search_results_from_html(html)

        assert results == []

    def test_parse_percentage_coupon(self):
        """Should parse percentage-based coupons from HTML."""
        from app.workers.list_scraper import _parse_search_results_from_html

        html = """
        <html><body>
        <div class="dui-card searchresultitem" data-track-price="5000">
            <a class="image-link-wrapper--3XCNg" href="https://item.rakuten.co.jp/shop/item/">
                <img class="image--abc" />
            </a>
            <div class="coupon">10%OFFクーポンあり</div>
        </div>
        </body></html>
        """
        results = _parse_search_results_from_html(html)
        assert len(results) == 1
        assert results[0]["coupon_discount"] == 10
        assert results[0]["coupon_type"] == "percentage"


# ═══════════════════════════════════════════════════════════════
#  5. Combined Parser Tests
# ═══════════════════════════════════════════════════════════════

class TestParseSearchResults:
    """Tests for the combined parser (JSON preferred, HTML fallback)."""

    def test_prefers_json(self):
        """Should prefer JSON extraction over HTML."""
        from app.workers.list_scraper import _parse_search_results

        html = _make_initial_state_html([_make_json_item()])
        items, pagination = _parse_search_results(html)

        assert len(items) == 1
        assert items[0]["data_source"] == "json"

    def test_falls_back_to_html(self):
        """Should fall back to HTML when JSON is not available."""
        from app.workers.list_scraper import _parse_search_results

        html = _make_html_card_page()  # No __INITIAL_STATE__
        items, pagination = _parse_search_results(html)

        assert len(items) == 2
        assert items[0]["data_source"] == "html"
        assert pagination is None  # HTML doesn't provide pagination


# ═══════════════════════════════════════════════════════════════
#  6. Helper Function Tests
# ═══════════════════════════════════════════════════════════════

class TestHelperFunctions:
    """Tests for point text, coupon text, and price extraction helpers."""

    def test_build_point_text(self):
        """Should build human-readable point text from structured data."""
        from app.workers.list_scraper import _build_point_text

        item = {
            "point_count": 285,
            "point_base_multiplier": 1,
            "point_shop_multiplier": 5,
            "point_up_multiplier": 4,
            "point_deal_multiplier": 0,
            "point_item_multiplier": 0,
        }
        text = _build_point_text(item)
        assert text == "285ポイント(1倍+9倍UP)"

    def test_build_point_text_no_up(self):
        """Should handle case with no UP multiplier."""
        from app.workers.list_scraper import _build_point_text

        item = {
            "point_count": 50,
            "point_base_multiplier": 1,
        }
        text = _build_point_text(item)
        assert text == "50ポイント(1倍)"

    def test_build_point_text_none(self):
        """Should return None when no point count."""
        from app.workers.list_scraper import _build_point_text

        text = _build_point_text({})
        assert text is None

    def test_build_coupon_text_percentage(self):
        """Should format percentage coupon text."""
        from app.workers.list_scraper import _build_coupon_text

        text = _build_coupon_text({"coupon_discount": 15, "coupon_type": "percentage"})
        assert text == "15%OFFクーポンあり"

    def test_build_coupon_text_exact(self):
        """Should format yen-amount coupon text."""
        from app.workers.list_scraper import _build_coupon_text

        text = _build_coupon_text({"coupon_discount": 2000, "coupon_type": "exact"})
        assert text == "2,000円OFFクーポンあり"

    def test_build_coupon_text_none(self):
        """Should return None when no coupon."""
        from app.workers.list_scraper import _build_coupon_text

        text = _build_coupon_text({})
        assert text is None

    def test_extract_price_from_text(self):
        """Should extract numeric price from text."""
        from app.workers.list_scraper import _extract_price_from_text

        assert _extract_price_from_text("¥1,234") == 1234
        assert _extract_price_from_text("8580円") == 8580
        assert _extract_price_from_text("") is None
        assert _extract_price_from_text(None) is None


# ═══════════════════════════════════════════════════════════════
#  7. Page Fetch Tests
# ═══════════════════════════════════════════════════════════════

class TestFetchPage:
    """Tests for _fetch_page with auth cookies, proxy, and retry."""

    @patch("app.workers.list_scraper._get_auth_cookies", return_value={})
    @patch("app.workers.list_scraper._get_proxy", return_value=None)
    @patch("httpx.Client")
    def test_successful_fetch(self, mock_client_class, mock_proxy, mock_cookies):
        """Should return HTML on successful fetch."""
        from app.workers.list_scraper import _fetch_page

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "<html><body>Test</body></html>"
        mock_resp.raise_for_status = MagicMock()

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = mock_resp
        mock_client_class.return_value = mock_client

        result = _fetch_page("https://search.rakuten.co.jp/search/mall/-/215783/")
        assert result == "<html><body>Test</body></html>"

    @patch("app.workers.list_scraper._get_auth_cookies", return_value={})
    @patch("app.workers.list_scraper._get_proxy", return_value=None)
    @patch("httpx.Client")
    def test_404_returns_none(self, mock_client_class, mock_proxy, mock_cookies):
        """Should return None for 404 responses."""
        from app.workers.list_scraper import _fetch_page

        mock_resp = MagicMock()
        mock_resp.status_code = 404

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = mock_resp
        mock_client_class.return_value = mock_client

        result = _fetch_page("https://search.rakuten.co.jp/search/mall/-/999999/")
        assert result is None

    @patch("app.workers.list_scraper.time.sleep")
    @patch("app.workers.list_scraper._get_auth_cookies", return_value={})
    @patch("app.workers.list_scraper._get_proxy", return_value=None)
    @patch("httpx.Client")
    def test_429_retries_with_backoff(self, mock_client_class, mock_proxy, mock_cookies, mock_sleep):
        """Should retry with backoff on 429 rate limit."""
        from app.workers.list_scraper import _fetch_page

        mock_resp_429 = MagicMock()
        mock_resp_429.status_code = 429

        mock_resp_200 = MagicMock()
        mock_resp_200.status_code = 200
        mock_resp_200.text = "<html>OK</html>"
        mock_resp_200.raise_for_status = MagicMock()

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.side_effect = [mock_resp_429, mock_resp_200]
        mock_client_class.return_value = mock_client

        result = _fetch_page("https://search.rakuten.co.jp/test")
        assert result == "<html>OK</html>"
        mock_sleep.assert_called()

    @patch("app.workers.list_scraper._get_auth_cookies")
    @patch("app.workers.list_scraper._get_proxy", return_value=None)
    @patch("httpx.Client")
    def test_auth_cookies_injected(self, mock_client_class, mock_proxy, mock_cookies):
        """Should pass auth cookies to the HTTP client."""
        from app.workers.list_scraper import _fetch_page

        mock_cookies.return_value = {"Ra": "token123", "Rb": "token456"}

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "<html>Auth</html>"
        mock_resp.raise_for_status = MagicMock()

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = mock_resp
        mock_client_class.return_value = mock_client

        _fetch_page("https://search.rakuten.co.jp/test")

        # Verify cookies were passed
        client_kwargs = mock_client_class.call_args.kwargs
        assert client_kwargs["cookies"] == {"Ra": "token123", "Rb": "token456"}


# ═══════════════════════════════════════════════════════════════
#  8. Main Scrape Task Tests
# ═══════════════════════════════════════════════════════════════

class TestScrapeListPage:
    """Tests for the main scrape_list_page Celery task."""

    @patch("app.workers.list_scraper.time.sleep")
    @patch("app.workers.list_scraper.upsert_item_sync", return_value=501)
    @patch("app.workers.list_scraper.upsert_shop_sync")
    @patch("app.workers.list_scraper._fetch_page")
    @patch("app.workers.list_scraper.get_sync_db")
    def test_scrape_json_items(self, mock_get_db, mock_fetch, mock_shop, mock_item, mock_sleep):
        """Should process items from JSON-embedded data."""
        from app.workers.list_scraper import scrape_list_page

        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        mock_db.execute.return_value.first.return_value = None  # No existing job

        mock_fetch.return_value = _make_initial_state_html([_make_json_item()])

        result = scrape_list_page("https://search.rakuten.co.jp/search/mall/-/215783/")

        assert result["status"] == "ok"
        assert result["items_found"] == 1
        mock_shop.assert_called_once()
        mock_item.assert_called_once()

    @patch("app.workers.list_scraper.time.sleep")
    @patch("app.workers.list_scraper.upsert_item_sync", return_value=501)
    @patch("app.workers.list_scraper.upsert_shop_sync")
    @patch("app.workers.list_scraper._fetch_page")
    @patch("app.workers.list_scraper.get_sync_db")
    def test_scrape_html_fallback(self, mock_get_db, mock_fetch, mock_shop, mock_item, mock_sleep):
        """Should fall back to HTML parsing when JSON is unavailable."""
        from app.workers.list_scraper import scrape_list_page

        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        mock_db.execute.return_value.first.return_value = None

        mock_fetch.return_value = _make_html_card_page()

        result = scrape_list_page("https://search.rakuten.co.jp/search/mall/-/215783/")

        assert result["status"] == "ok"
        assert result["items_found"] == 2

    @patch("app.workers.list_scraper._fetch_page", return_value=None)
    @patch("app.workers.list_scraper.get_sync_db")
    def test_scrape_fetch_failed(self, mock_get_db, mock_fetch):
        """Should return error when page fetch fails."""
        from app.workers.list_scraper import scrape_list_page

        mock_db = MagicMock()
        mock_get_db.return_value = mock_db

        result = scrape_list_page("https://search.rakuten.co.jp/search/mall/-/215783/")

        assert result["status"] == "error"
        assert result["reason"] == "fetch_failed"

    @patch("app.workers.list_scraper.time.sleep")
    @patch("app.workers.list_scraper.upsert_item_sync", return_value=501)
    @patch("app.workers.list_scraper.upsert_shop_sync")
    @patch("app.workers.list_scraper._fetch_page")
    @patch("app.workers.list_scraper.get_sync_db")
    def test_list_observation_saved(self, mock_get_db, mock_fetch, mock_shop, mock_item, mock_sleep):
        """Should insert a list_observations record for each item."""
        from app.workers.list_scraper import scrape_list_page

        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        mock_db.execute.return_value.first.return_value = None

        mock_fetch.return_value = _make_initial_state_html([_make_json_item()])

        scrape_list_page("https://search.rakuten.co.jp/test")

        # Check that db.execute was called with INSERT INTO list_observations
        insert_calls = [
            c for c in mock_db.execute.call_args_list
            if hasattr(c.args[0], "text") and "list_observations" in c.args[0].text
        ]
        assert len(insert_calls) >= 1

    @patch("app.workers.list_scraper.time.sleep")
    @patch("app.workers.list_scraper.upsert_item_sync", return_value=501)
    @patch("app.workers.list_scraper.upsert_shop_sync")
    @patch("app.workers.list_scraper._fetch_page")
    @patch("app.workers.list_scraper.get_sync_db")
    def test_detail_scrape_job_enqueued(self, mock_get_db, mock_fetch, mock_shop, mock_item, mock_sleep):
        """Should enqueue a detail_scrape crawl_job for each new item."""
        from app.workers.list_scraper import scrape_list_page

        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        mock_db.execute.return_value.first.return_value = None  # No existing job

        mock_fetch.return_value = _make_initial_state_html([_make_json_item()])

        result = scrape_list_page("https://search.rakuten.co.jp/test")

        assert result["enqueued"] >= 1


# ═══════════════════════════════════════════════════════════════
#  8b. Full Pagination Tests — All Pages, All Products
# ═══════════════════════════════════════════════════════════════

class TestFullListPagination:
    """
    Comprehensive tests for full list page pagination.

    Real Rakuten behaviour:
      - Page 1: https://www.rakuten.co.jp/category/100938/  (category landing)
                also: https://search.rakuten.co.jp/search/mall/-/100938/
      - Page 2+: https://search.rakuten.co.jp/search/mall/-/100938/?p=2
      - 45 items per page, up to 150 pages max (subset=6750)
      - pagination.numFound = total matching products (e.g. 1,362,286)
      - pagination.subset = max reachable items (e.g. 6,750)
      - pagination.pageSize = 45
    """

    @staticmethod
    def _make_page_items(page: int, items_per_page: int = 45) -> list[dict]:
        """Generate unique realistic items for a given page number."""
        items = []
        for i in range(items_per_page):
            idx = (page - 1) * items_per_page + i  # global index 0..N
            shop_url_code = f"shop{idx % 80:03d}"   # 80 unique shops
            item_slug = f"item{10000 + idx}"
            items.append({
                "name": f"ダイエット商品 #{idx + 1}",
                "price": 500 + idx * 5,
                "subtitle": f"キャッチコピー {idx + 1}",
                "url": f"https://search.rakuten.co.jp/redirect?url=https://item.rakuten.co.jp/{shop_url_code}/{item_slug}/",
                "originalItemUrl": f"https://item.rakuten.co.jp/{shop_url_code}/{item_slug}/",
                "code": str(10000 + idx),
                "images": [{
                    "url": f"https://thumbnail.image.rakuten.co.jp/@0_mall/{shop_url_code}/cabinet/{item_slug}.jpg"
                }],
                "genreIdList": "/0/100938",
                "isSoldOut": idx % 20 == 0,  # every 20th is sold out
                "itemOptions": {
                    "superDeal": idx % 15 == 0,
                    "shop39": idx % 3 == 0,
                    "cpc": {},
                },
                "shop": {
                    "name": f"ショップ {shop_url_code}",
                    "id": 100000 + (idx % 80),
                    "code": str(100000 + (idx % 80)),
                    "urlCode": shop_url_code,
                },
                "shipping": {
                    "price": 0 if idx % 3 == 0 else 550,
                    "estimateDeliveryDay": "2/24 12:00までの注文で最短2/25お届け",
                    "deliveryDays": None,
                },
                "point": {
                    "count": max(1, (500 + idx * 5) // 100),
                    "baseMultiplier": 1,
                    "shopMultiplier": (idx % 5) + 1,
                    "pointUpMultiplier": idx % 10,
                    "dealMultiplier": None,
                    "itemMultiplier": None,
                },
                "review": {
                    "score": round(3.0 + (idx % 20) * 0.1, 2),
                    "numReviews": idx % 500,
                },
                "coupons": (
                    [{"discount": 10 + (idx % 30), "discountType": "percentage"}]
                    if idx % 4 == 0
                    else []
                ),
                "variantId": str(50000 + idx),
                "skuInfo": {
                    "hasMultiSku": idx % 5 == 0,
                    "priceRange": str(500 + idx * 5),
                },
                "genres": [{"id": 100938, "name": "ダイエット・健康"}],
            })
        return items

    @staticmethod
    def _make_page_html(
        page: int,
        items_per_page: int = 45,
        num_found: int = 1_362_286,
        subset: int = 6_750,
    ) -> str:
        """Build HTML containing __INITIAL_STATE__ for a given page."""
        items = TestFullListPagination._make_page_items(page, items_per_page)
        state = {
            "state": {
                "data": {
                    "ichibaSearch": {
                        "pagination": {
                            "numFound": num_found,
                            "start": (page - 1) * items_per_page,
                            "pageSize": items_per_page,
                            "subset": subset,
                        },
                        "items": items,
                    }
                }
            }
        }
        state_json = json.dumps(state, ensure_ascii=False)
        return f"""
        <html>
        <head><title>【楽天市場】ダイエット・健康 - page {page}</title></head>
        <body>
        <script>
            window.__INITIAL_STATE__ = {state_json};
        </script>
        </body>
        </html>
        """

    # ────────────────────────────────────────────────────────────
    #  Test: Page 1 (category URL) processes all 45 items
    # ────────────────────────────────────────────────────────────
    @patch("app.workers.list_scraper.time.sleep")
    @patch("app.workers.list_scraper.upsert_item_sync", return_value=501)
    @patch("app.workers.list_scraper.upsert_shop_sync")
    @patch("app.workers.list_scraper._fetch_page")
    @patch("app.workers.list_scraper.get_sync_db")
    def test_page1_category_url_45_items(
        self, mock_get_db, mock_fetch, mock_shop, mock_item, mock_sleep,
    ):
        """
        Page 1 is the category landing page at:
          https://www.rakuten.co.jp/category/100938/
        Should process all 45 items.
        """
        from app.workers.list_scraper import scrape_list_page

        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        mock_db.execute.return_value.first.return_value = None

        mock_fetch.return_value = self._make_page_html(page=1)

        # Page 1 uses the category URL
        result = scrape_list_page("https://www.rakuten.co.jp/category/100938/?l-id=top_normal_gmenu_health")

        assert result["status"] == "ok"
        assert result["items_found"] == 45
        assert mock_shop.call_count == 45
        assert mock_item.call_count == 45
        assert result["total_found"] == 1_362_286
        assert result["page_size"] == 45

    # ────────────────────────────────────────────────────────────
    #  Test: Page 2 (search URL) processes all 45 items
    # ────────────────────────────────────────────────────────────
    @patch("app.workers.list_scraper.time.sleep")
    @patch("app.workers.list_scraper.upsert_item_sync", return_value=501)
    @patch("app.workers.list_scraper.upsert_shop_sync")
    @patch("app.workers.list_scraper._fetch_page")
    @patch("app.workers.list_scraper.get_sync_db")
    def test_page2_search_url_45_items(
        self, mock_get_db, mock_fetch, mock_shop, mock_item, mock_sleep,
    ):
        """
        Page 2+ uses the search URL at:
          https://search.rakuten.co.jp/search/mall/-/100938/?p=2
        """
        from app.workers.list_scraper import scrape_list_page

        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        mock_db.execute.return_value.first.return_value = None

        mock_fetch.return_value = self._make_page_html(page=2)

        result = scrape_list_page("https://search.rakuten.co.jp/search/mall/-/100938/?p=2")

        assert result["status"] == "ok"
        assert result["items_found"] == 45
        assert mock_shop.call_count == 45
        assert mock_item.call_count == 45

    # ────────────────────────────────────────────────────────────
    #  Test: Full chain — all 150 pages (6,750 products)
    # ────────────────────────────────────────────────────────────
    @patch("app.workers.list_scraper.time.sleep")
    @patch("app.workers.list_scraper.upsert_item_sync", return_value=501)
    @patch("app.workers.list_scraper.upsert_shop_sync")
    @patch("app.workers.list_scraper._fetch_page")
    @patch("app.workers.list_scraper.get_sync_db")
    def test_full_150_page_chain_processes_6750_products(
        self, mock_get_db, mock_fetch, mock_shop, mock_item, mock_sleep,
    ):
        """
        Simulate scraping ALL 150 pages (45 items/page × 150 = 6,750 products).

        Rakuten list pages show subset=6750 as the maximum reachable items.
        Page 1 URL: https://www.rakuten.co.jp/category/100938/
        Page 2-150 URLs: https://search.rakuten.co.jp/search/mall/-/100938/?p=N
        """
        from app.workers.list_scraper import scrape_list_page

        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        mock_db.execute.return_value.first.return_value = None

        total_items = 0
        total_enqueued = 0

        for page_num in range(1, 151):
            mock_shop.reset_mock()
            mock_item.reset_mock()
            mock_fetch.return_value = self._make_page_html(page=page_num)

            # Page 1 = category URL, page 2+ = search URL
            if page_num == 1:
                url = "https://www.rakuten.co.jp/category/100938/"
            else:
                url = f"https://search.rakuten.co.jp/search/mall/-/100938/?p={page_num}"

            result = scrape_list_page(url)

            assert result["status"] == "ok", f"Page {page_num} failed"
            assert result["items_found"] == 45, \
                f"Page {page_num}: expected 45 items, got {result['items_found']}"
            assert mock_shop.call_count == 45, \
                f"Page {page_num}: expected 45 shop upserts"
            assert mock_item.call_count == 45, \
                f"Page {page_num}: expected 45 item upserts"

            total_items += result["items_found"]
            total_enqueued += result["enqueued"]

        assert total_items == 6750, \
            f"Expected 6750 total items, got {total_items}"
        assert total_enqueued == 6750, \
            f"Expected 6750 enqueued detail jobs, got {total_enqueued}"

    # ────────────────────────────────────────────────────────────
    #  Test: All items have unique canonical URLs across all pages
    # ────────────────────────────────────────────────────────────
    @patch("app.workers.list_scraper.time.sleep")
    @patch("app.workers.list_scraper.upsert_item_sync", return_value=501)
    @patch("app.workers.list_scraper.upsert_shop_sync")
    @patch("app.workers.list_scraper._fetch_page")
    @patch("app.workers.list_scraper.get_sync_db")
    def test_all_items_unique_across_pages(
        self, mock_get_db, mock_fetch, mock_shop, mock_item, mock_sleep,
    ):
        """All products across 150 pages should have unique canonical URLs."""
        from app.workers.list_scraper import scrape_list_page

        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        mock_db.execute.return_value.first.return_value = None

        all_canonical_urls = set()

        for page_num in range(1, 151):
            mock_item.reset_mock()
            mock_fetch.return_value = self._make_page_html(page=page_num)

            url = f"https://search.rakuten.co.jp/search/mall/-/100938/?p={page_num}"
            scrape_list_page(url)

            for call in mock_item.call_args_list:
                canonical_url = call.kwargs["canonical_url"]
                all_canonical_urls.add(canonical_url)

        assert len(all_canonical_urls) == 6750, \
            f"Expected 6750 unique URLs, got {len(all_canonical_urls)}"

    # ────────────────────────────────────────────────────────────
    #  Test: List observations saved for every item on every page
    # ────────────────────────────────────────────────────────────
    @patch("app.workers.list_scraper.time.sleep")
    @patch("app.workers.list_scraper.upsert_item_sync", return_value=501)
    @patch("app.workers.list_scraper.upsert_shop_sync")
    @patch("app.workers.list_scraper._fetch_page")
    @patch("app.workers.list_scraper.get_sync_db")
    def test_list_observations_saved_for_all_items(
        self, mock_get_db, mock_fetch, mock_shop, mock_item, mock_sleep,
    ):
        """Every item should have a list_observations INSERT across pages."""
        from app.workers.list_scraper import scrape_list_page

        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        mock_db.execute.return_value.first.return_value = None

        # Test 5 representative pages
        for page_num in [1, 25, 75, 100, 150]:
            mock_db.reset_mock()
            mock_db.execute.return_value.first.return_value = None
            mock_fetch.return_value = self._make_page_html(page=page_num)

            url = f"https://search.rakuten.co.jp/search/mall/-/100938/?p={page_num}"
            scrape_list_page(url)

            insert_calls = [
                c for c in mock_db.execute.call_args_list
                if hasattr(c.args[0], "text") and "list_observations" in c.args[0].text
            ]
            assert len(insert_calls) == 45, \
                f"Page {page_num}: expected 45 list_observation INSERTs, got {len(insert_calls)}"

    # ────────────────────────────────────────────────────────────
    #  Test: Point data correctly extracted for all items
    # ────────────────────────────────────────────────────────────
    @patch("app.workers.list_scraper.time.sleep")
    @patch("app.workers.list_scraper.upsert_item_sync", return_value=501)
    @patch("app.workers.list_scraper.upsert_shop_sync")
    @patch("app.workers.list_scraper._fetch_page")
    @patch("app.workers.list_scraper.get_sync_db")
    def test_point_data_stored_for_items(
        self, mock_get_db, mock_fetch, mock_shop, mock_item, mock_sleep,
    ):
        """Point breakdown (base, shop, UP multipliers) should be stored in observations."""
        from app.workers.list_scraper import scrape_list_page

        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        mock_db.execute.return_value.first.return_value = None

        mock_fetch.return_value = self._make_page_html(page=1)
        scrape_list_page("https://www.rakuten.co.jp/category/100938/")

        # Find list_observation INSERT calls and check point parameters
        obs_calls = [
            c for c in mock_db.execute.call_args_list
            if hasattr(c.args[0], "text") and "list_observations" in c.args[0].text
        ]
        assert len(obs_calls) == 45

        # Verify first item has point data
        first_params = obs_calls[0].args[1] if len(obs_calls[0].args) > 1 else obs_calls[0].kwargs
        assert first_params["point_count"] is not None
        assert first_params["point_base_multiplier"] == 1

    # ────────────────────────────────────────────────────────────
    #  Test: Coupon data correctly extracted
    # ────────────────────────────────────────────────────────────
    @patch("app.workers.list_scraper.time.sleep")
    @patch("app.workers.list_scraper.upsert_item_sync", return_value=501)
    @patch("app.workers.list_scraper.upsert_shop_sync")
    @patch("app.workers.list_scraper._fetch_page")
    @patch("app.workers.list_scraper.get_sync_db")
    def test_coupon_data_stored_for_items_with_coupons(
        self, mock_get_db, mock_fetch, mock_shop, mock_item, mock_sleep,
    ):
        """Items with coupons should have coupon_discount and coupon_type stored."""
        from app.workers.list_scraper import scrape_list_page

        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        mock_db.execute.return_value.first.return_value = None

        mock_fetch.return_value = self._make_page_html(page=1)
        scrape_list_page("https://search.rakuten.co.jp/search/mall/-/100938/")

        obs_calls = [
            c for c in mock_db.execute.call_args_list
            if hasattr(c.args[0], "text") and "list_observations" in c.args[0].text
        ]

        # In test data, every 4th item (idx % 4 == 0) has a coupon
        items_with_coupons = 0
        for call in obs_calls:
            params = call.args[1] if len(call.args) > 1 else call.kwargs
            if params.get("coupon_discount") is not None:
                items_with_coupons += 1
                assert params["coupon_type"] == "percentage"

        # 45 items, every 4th (idx 0,4,8,12,16,20,24,28,32,36,40,44) = 12 items
        assert items_with_coupons == 12, \
            f"Expected 12 items with coupons, got {items_with_coupons}"

    # ────────────────────────────────────────────────────────────
    #  Test: Free shipping detected correctly
    # ────────────────────────────────────────────────────────────
    @patch("app.workers.list_scraper.time.sleep")
    @patch("app.workers.list_scraper.upsert_item_sync", return_value=501)
    @patch("app.workers.list_scraper.upsert_shop_sync")
    @patch("app.workers.list_scraper._fetch_page")
    @patch("app.workers.list_scraper.get_sync_db")
    def test_free_shipping_detected_across_pages(
        self, mock_get_db, mock_fetch, mock_shop, mock_item, mock_sleep,
    ):
        """Items with shipping.price=0 should be marked is_free_shipping=1."""
        from app.workers.list_scraper import scrape_list_page

        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        mock_db.execute.return_value.first.return_value = None

        mock_fetch.return_value = self._make_page_html(page=1)
        scrape_list_page("https://search.rakuten.co.jp/search/mall/-/100938/")

        obs_calls = [
            c for c in mock_db.execute.call_args_list
            if hasattr(c.args[0], "text") and "list_observations" in c.args[0].text
        ]

        free_shipping_count = sum(
            1 for c in obs_calls
            if (c.args[1] if len(c.args) > 1 else c.kwargs).get("is_free_shipping") == 1
        )

        # Every 3rd item (idx % 3 == 0) has free shipping → 15 out of 45
        assert free_shipping_count == 15, \
            f"Expected 15 free-shipping items, got {free_shipping_count}"

    # ────────────────────────────────────────────────────────────
    #  Test: Sold-out items are still recorded
    # ────────────────────────────────────────────────────────────
    @patch("app.workers.list_scraper.time.sleep")
    @patch("app.workers.list_scraper.upsert_item_sync", return_value=501)
    @patch("app.workers.list_scraper.upsert_shop_sync")
    @patch("app.workers.list_scraper._fetch_page")
    @patch("app.workers.list_scraper.get_sync_db")
    def test_sold_out_items_tracked(
        self, mock_get_db, mock_fetch, mock_shop, mock_item, mock_sleep,
    ):
        """Sold-out items should still be upserted and observed (with is_sold_out=1)."""
        from app.workers.list_scraper import scrape_list_page

        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        mock_db.execute.return_value.first.return_value = None

        mock_fetch.return_value = self._make_page_html(page=1)
        scrape_list_page("https://search.rakuten.co.jp/search/mall/-/100938/")

        obs_calls = [
            c for c in mock_db.execute.call_args_list
            if hasattr(c.args[0], "text") and "list_observations" in c.args[0].text
        ]

        sold_out_count = sum(
            1 for c in obs_calls
            if (c.args[1] if len(c.args) > 1 else c.kwargs).get("is_sold_out") == 1
        )

        # Every 20th item (idx % 20 == 0) is sold out → 3 out of 45 (idx 0, 20, 40)
        assert sold_out_count == 3, \
            f"Expected 3 sold-out items, got {sold_out_count}"

    # ────────────────────────────────────────────────────────────
    #  Test: scrape_list_paginated dispatches all pages with correct URLs
    # ────────────────────────────────────────────────────────────
    @patch("app.workers.list_scraper.scrape_list_page.delay")
    def test_paginated_dispatches_correct_urls(self, mock_delay):
        """
        scrape_list_paginated should dispatch page tasks with correct URLs:
          - Page 1: no ?p= param
          - Page 2+: ?p=N
        """
        from app.workers.list_scraper import scrape_list_paginated

        result = scrape_list_paginated(
            base_url="https://search.rakuten.co.jp/search/mall/-/100938/",
            genre_id="100938",
            max_pages=150,
        )

        assert result["status"] == "ok"
        assert result["pages_dispatched"] == 150
        assert mock_delay.call_count == 150

        # Page 1 URL should NOT have ?p= param
        page1_url = mock_delay.call_args_list[0][0][0]
        assert "search.rakuten.co.jp/search/mall/-/100938/" in page1_url
        assert "p=" not in page1_url

        # Page 2 URL should have ?p=2
        page2_url = mock_delay.call_args_list[1][0][0]
        assert "p=2" in page2_url

        # Page 150 URL should have ?p=150
        page150_url = mock_delay.call_args_list[149][0][0]
        assert "p=150" in page150_url

    # ────────────────────────────────────────────────────────────
    #  Test: Detail scrape jobs deduplicated across pages
    # ────────────────────────────────────────────────────────────
    @patch("app.workers.list_scraper.time.sleep")
    @patch("app.workers.list_scraper.upsert_item_sync", return_value=501)
    @patch("app.workers.list_scraper.upsert_shop_sync")
    @patch("app.workers.list_scraper._fetch_page")
    @patch("app.workers.list_scraper.get_sync_db")
    def test_existing_jobs_not_duplicated(
        self, mock_get_db, mock_fetch, mock_shop, mock_item, mock_sleep,
    ):
        """If a crawl_job already exists, should not enqueue another."""
        from app.workers.list_scraper import scrape_list_page

        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        # All items already have pending crawl_jobs
        mock_db.execute.return_value.first.return_value = (999,)

        mock_fetch.return_value = self._make_page_html(page=1)

        result = scrape_list_page("https://search.rakuten.co.jp/search/mall/-/100938/")

        assert result["status"] == "ok"
        assert result["items_found"] == 45
        # Items still upserted, but no new jobs enqueued
        assert mock_shop.call_count == 45
        assert mock_item.call_count == 45
        assert result["enqueued"] == 0

    # ────────────────────────────────────────────────────────────
    #  Test: Last page with fewer than 45 items
    # ────────────────────────────────────────────────────────────
    @patch("app.workers.list_scraper.time.sleep")
    @patch("app.workers.list_scraper.upsert_item_sync", return_value=501)
    @patch("app.workers.list_scraper.upsert_shop_sync")
    @patch("app.workers.list_scraper._fetch_page")
    @patch("app.workers.list_scraper.get_sync_db")
    def test_partial_last_page(
        self, mock_get_db, mock_fetch, mock_shop, mock_item, mock_sleep,
    ):
        """Last page may have fewer than 45 items."""
        from app.workers.list_scraper import scrape_list_page

        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        mock_db.execute.return_value.first.return_value = None

        # Page 150 with only 30 items
        mock_fetch.return_value = self._make_page_html(page=150, items_per_page=30)

        result = scrape_list_page("https://search.rakuten.co.jp/search/mall/-/100938/?p=150")

        assert result["status"] == "ok"
        assert result["items_found"] == 30
        assert mock_item.call_count == 30

    # ────────────────────────────────────────────────────────────
    #  Test: Review data stored for items with reviews
    # ────────────────────────────────────────────────────────────
    @patch("app.workers.list_scraper.time.sleep")
    @patch("app.workers.list_scraper.upsert_item_sync", return_value=501)
    @patch("app.workers.list_scraper.upsert_shop_sync")
    @patch("app.workers.list_scraper._fetch_page")
    @patch("app.workers.list_scraper.get_sync_db")
    def test_review_data_stored(
        self, mock_get_db, mock_fetch, mock_shop, mock_item, mock_sleep,
    ):
        """Items with reviews should have review_score and review_count stored."""
        from app.workers.list_scraper import scrape_list_page

        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        mock_db.execute.return_value.first.return_value = None

        mock_fetch.return_value = self._make_page_html(page=5)
        scrape_list_page("https://search.rakuten.co.jp/search/mall/-/100938/?p=5")

        obs_calls = [
            c for c in mock_db.execute.call_args_list
            if hasattr(c.args[0], "text") and "list_observations" in c.args[0].text
        ]

        items_with_reviews = 0
        for call in obs_calls:
            params = call.args[1] if len(call.args) > 1 else call.kwargs
            if params.get("review_score") is not None:
                items_with_reviews += 1
                assert 3.0 <= params["review_score"] <= 5.0

        # All items except idx % 500 == 0 have reviews; page 5 idx 180-224
        # idx 180 has numReviews = 180, etc. None are 0 on page 5
        assert items_with_reviews >= 44, \
            f"Expected at least 44 items with reviews, got {items_with_reviews}"

    # ────────────────────────────────────────────────────────────
    #  Test: Super DEAL items flagged correctly
    # ────────────────────────────────────────────────────────────
    @patch("app.workers.list_scraper.time.sleep")
    @patch("app.workers.list_scraper.upsert_item_sync", return_value=501)
    @patch("app.workers.list_scraper.upsert_shop_sync")
    @patch("app.workers.list_scraper._fetch_page")
    @patch("app.workers.list_scraper.get_sync_db")
    def test_super_deal_items_flagged(
        self, mock_get_db, mock_fetch, mock_shop, mock_item, mock_sleep,
    ):
        """Super DEAL items should be flagged in observations."""
        from app.workers.list_scraper import scrape_list_page

        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        mock_db.execute.return_value.first.return_value = None

        mock_fetch.return_value = self._make_page_html(page=1)
        scrape_list_page("https://search.rakuten.co.jp/search/mall/-/100938/")

        obs_calls = [
            c for c in mock_db.execute.call_args_list
            if hasattr(c.args[0], "text") and "list_observations" in c.args[0].text
        ]

        super_deal_count = sum(
            1 for c in obs_calls
            if (c.args[1] if len(c.args) > 1 else c.kwargs).get("is_super_deal") == 1
        )

        # Every 15th item (idx % 15 == 0) is Super DEAL → 3 out of 45 (idx 0, 15, 30)
        assert super_deal_count == 3, \
            f"Expected 3 Super DEAL items, got {super_deal_count}"


# ═══════════════════════════════════════════════════════════════
#  9. Paginated Scraper Tests
# ═══════════════════════════════════════════════════════════════

class TestScrapeListPaginated:
    """Tests for the paginated list scraping orchestration."""

    @patch("app.workers.list_scraper.scrape_list_page.delay")
    def test_dispatches_pages(self, mock_delay):
        """Should dispatch page scrape tasks for all pages."""
        from app.workers.list_scraper import scrape_list_paginated

        result = scrape_list_paginated(
            base_url="https://search.rakuten.co.jp/search/mall/-/215783/",
            genre_id="215783",
            max_pages=5,
        )

        assert result["status"] == "ok"
        assert result["pages_dispatched"] == 5
        assert mock_delay.call_count == 5


# ═══════════════════════════════════════════════════════════════
#  10. Daily Scheduled Task Tests
# ═══════════════════════════════════════════════════════════════

class TestRunDailyListScrape:
    """Tests for the daily scheduled list scrape task."""

    @patch("app.workers.list_scraper.scrape_list_paginated.delay")
    @patch("app.workers.list_scraper.get_sync_db")
    def test_dispatches_shop_targets(self, mock_get_db, mock_paginate):
        """Should dispatch paginated scrape for each active shop target."""
        from app.workers.list_scraper import run_daily_list_scrape

        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        mock_db.execute.return_value.mappings.return_value.all.return_value = [
            {"id": 1, "target_type": "shop", "target_value": "sneak", "base_url": None},
            {"id": 2, "target_type": "shop", "target_value": "horiman", "base_url": None},
        ]

        result = run_daily_list_scrape()

        assert result["status"] == "ok"
        assert result["tasks_dispatched"] == 2
        assert mock_paginate.call_count == 2


# ═══════════════════════════════════════════════════════════════
#  11. URL Building Tests (used by list scraper)
# ═══════════════════════════════════════════════════════════════

class TestBuildSearchURL:
    """Tests for the search URL builder used in list scraping."""

    def test_genre_url(self):
        from app.services.url_canonicalizer import build_search_url

        url = build_search_url(genre_id="215783", page=1)
        assert "search.rakuten.co.jp/search/mall/-/215783/" in url

    def test_genre_with_price(self):
        from app.services.url_canonicalizer import build_search_url

        url = build_search_url(genre_id="215783", min_price=0, max_price=5000, page=1)
        assert "min=0" in url
        assert "max=5000" in url

    def test_keyword_url(self):
        from app.services.url_canonicalizer import build_search_url

        url = build_search_url(keyword="ミルク", genre_id="100939", page=2)
        assert "ミルク" in url
        assert "100939" in url
        assert "p=2" in url

    def test_shop_url(self):
        from app.services.url_canonicalizer import build_search_url

        url = build_search_url(shop_code="261122", page=1)
        assert "sid=261122" in url
