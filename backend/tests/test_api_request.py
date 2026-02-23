"""
Test Suite: Rakuten API Request (API Discovery Worker)

Tests the 1st stage of the pipeline:
  - API Key Rotation (round-robin, rate-limit marking, recovery)
  - Rakuten API call (_call_rakuten_api) with key injection, retry, 429 handling
  - Response processing (_process_api_items) — URL canonicalization, shop/item/variant upsert, snapshot creation, job enqueue
  - Genre discovery task (discover_by_genre) — pagination, price-range splitting
  - Keyword discovery task (discover_by_keyword)
  - Discover all genres orchestration
  - Daily scheduled task
"""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch, PropertyMock

import pytest


# ─────────────────────────────────────────────────────────────
#  Fixtures
# ─────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _patch_settings():
    """
    Patch settings so tests don't touch real .env / external services.

    We patch BOTH ``app.config.settings`` (the canonical location) AND the
    local reference that ``api_discovery`` imported with
    ``from app.config import settings``.
    """
    with patch("app.config.settings") as mock_settings:
        mock_settings.RAKUTEN_API_BASE_URL = "https://openapi.rakuten.co.jp/ichibams/api"
        mock_settings.RAKUTEN_API_KEYS = "appId1|key1,appId2|key2,appId3|key3"
        mock_settings.RAKUTEN_AFFILIATE_ID = ""
        mock_settings.RAKUTEN_GENRE_IDS = "215783,100938,551169"
        mock_settings.RAKUTEN_ITEMS_PER_PAGE = 30
        mock_settings.RAKUTEN_MAX_PAGES = 100
        mock_settings.RAKUTEN_MAX_ITEMS_PER_SLICE = 3000
        mock_settings.REQUEST_DELAY_SEC = 0  # No delay in tests
        mock_settings.REDIS_URL = "redis://localhost:6379/0"
        type(mock_settings).api_key_pairs = PropertyMock(return_value=[
            ("appId1", "key1"),
            ("appId2", "key2"),
            ("appId3", "key3"),
        ])
        type(mock_settings).genre_id_list = PropertyMock(return_value=["215783", "100938", "551169"])

        # Also patch the local reference imported by api_discovery
        with patch("app.workers.api_discovery.settings", mock_settings):
            yield mock_settings


def _make_api_item(
    item_code: str = "horiman:10009058",
    shop_code: str = "horiman",
    shop_name: str = "ほりまん茶園",
    item_name: str = "抹茶 100g",
    item_price: int = 2500,
    item_url: str = "https://item.rakuten.co.jp/horiman/10009058/?scid=af_pc",
    genre_id: int = 215783,
    point_rate: int = 1,
    postage_flag: int = 0,
    review_count: int = 42,
    review_average: float = 4.75,
) -> dict:
    """Build a realistic Rakuten API item response dict."""
    return {
        "Item": {
            "itemCode": item_code,
            "itemName": item_name,
            "catchcopy": "宇治抹茶の本格派",
            "itemPrice": item_price,
            "itemUrl": item_url,
            "shopCode": shop_code,
            "shopName": shop_name,
            "shopUrl": "https://www.rakuten.co.jp/horiman/?rafcid=test",
            "genreId": genre_id,
            "pointRate": point_rate,
            "postageFlag": postage_flag,
            "reviewCount": review_count,
            "reviewAverage": review_average,
            "availability": 1,
            "mediumImageUrls": [
                {"imageUrl": "https://thumbnail.image.rakuten.co.jp/@0_mall/horiman/cabinet/img001.jpg"}
            ],
            "tagIds": [],
        }
    }


def _make_api_response(
    items: list[dict] | None = None,
    count: int = 100,
    page: int = 1,
    page_count: int = 4,
) -> dict:
    """Build a realistic Rakuten API Search response."""
    if items is None:
        items = [_make_api_item()]
    return {
        "count": count,
        "page": page,
        "first": (page - 1) * 30 + 1,
        "last": min(page * 30, count),
        "hits": len(items),
        "pageCount": page_count,
        "Items": items,
    }


# ═══════════════════════════════════════════════════════════════
#  1. API Key Rotation Tests
# ═══════════════════════════════════════════════════════════════

class TestAPIKeyRotator:
    """Tests for the round-robin API key rotator."""

    def test_key_count(self):
        from app.workers.api_discovery import APIKeyRotator
        rotator = APIKeyRotator()
        # Rotator uses settings.api_key_pairs which is mocked to 3 keys
        assert rotator.key_count == 3

    def test_round_robin_rotation(self):
        """Keys should rotate in round-robin order."""
        from app.workers.api_discovery import APIKeyRotator
        rotator = APIKeyRotator()

        key1 = rotator.get_next_key()
        key2 = rotator.get_next_key()
        key3 = rotator.get_next_key()
        key4 = rotator.get_next_key()  # wraps around

        assert key1 == ("appId1", "key1")
        assert key2 == ("appId2", "key2")
        assert key3 == ("appId3", "key3")
        assert key4 == ("appId1", "key1")  # round-robin wrap

    def test_rate_limit_skip(self):
        """Rate-limited keys should be skipped."""
        from app.workers.api_discovery import APIKeyRotator
        rotator = APIKeyRotator()

        # Get first key (appId1) and mark it rate-limited
        key1 = rotator.get_next_key()
        assert key1[0] == "appId1"
        rotator.mark_rate_limited("appId1", cooldown_sec=60.0)

        # Next call should skip appId1 → return appId2
        key_after = rotator.get_next_key()
        assert key_after[0] == "appId2"

    def test_rate_limit_recovery(self):
        """Keys should recover after cooldown expires."""
        from app.workers.api_discovery import APIKeyRotator
        rotator = APIKeyRotator()

        # Mark key1 rate-limited with 0 second cooldown (already expired)
        rotator.mark_rate_limited("appId1", cooldown_sec=0.0)
        time.sleep(0.05)

        # After expiry, key should be available again
        keys_seen = set()
        for _ in range(3):
            k = rotator.get_next_key()
            keys_seen.add(k[0])
        assert "appId1" in keys_seen

    def test_no_keys_configured_raises(self):
        """Should raise RuntimeError when no API keys are configured."""
        from app.workers.api_discovery import APIKeyRotator
        rotator = APIKeyRotator()
        rotator._keys = []  # Force empty

        with pytest.raises(RuntimeError, match="No API keys configured"):
            rotator.get_next_key()

    def test_get_status(self):
        """Status dict should report correct counts."""
        from app.workers.api_discovery import APIKeyRotator
        rotator = APIKeyRotator()

        status = rotator.get_status()
        assert status["total_keys"] == 3
        assert status["available_keys"] == 3
        assert len(status["keys"]) == 3

        # Mark one as rate-limited
        rotator.mark_rate_limited("appId2", cooldown_sec=60.0)
        status = rotator.get_status()
        assert status["available_keys"] == 2

    def test_all_keys_rate_limited_waits(self):
        """When all keys are rate-limited, rotator waits for the earliest to expire."""
        from app.workers.api_discovery import APIKeyRotator
        rotator = APIKeyRotator()

        # Mark all keys with short cooldown
        rotator.mark_rate_limited("appId1", cooldown_sec=0.1)
        rotator.mark_rate_limited("appId2", cooldown_sec=0.1)
        rotator.mark_rate_limited("appId3", cooldown_sec=0.1)

        start = time.time()
        key = rotator.get_next_key()
        elapsed = time.time() - start

        # Should have waited at least ~0.1 seconds
        assert elapsed >= 0.05
        assert key[0] in ("appId1", "appId2", "appId3")


# ═══════════════════════════════════════════════════════════════
#  2. API Call Tests
# ═══════════════════════════════════════════════════════════════

class TestCallRakutenAPI:
    """Tests for _call_rakuten_api with key injection, retry, and 429 handling."""

    @patch("app.workers.api_discovery.key_rotator")
    @patch("httpx.Client")
    def test_successful_api_call(self, mock_client_class, mock_rotator):
        """Normal API call should inject key and return parsed JSON."""
        from app.workers.api_discovery import _call_rakuten_api

        mock_rotator.key_count = 3
        mock_rotator.get_next_key.return_value = ("appId1", "key1")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = _make_api_response()
        mock_resp.raise_for_status = MagicMock()

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = mock_resp
        mock_client_class.return_value = mock_client

        result = _call_rakuten_api({"genreId": "215783", "page": 1})

        assert result["count"] == 100
        assert result["page"] == 1
        assert len(result["Items"]) == 1

        # Verify key was injected
        call_args = mock_client.get.call_args
        params = call_args.kwargs.get("params", call_args[1].get("params", {}))
        assert params["applicationId"] == "appId1"
        assert params["accessKey"] == "key1"
        assert params["format"] == "json"

    @patch("app.workers.api_discovery.key_rotator")
    @patch("httpx.Client")
    def test_rate_limit_429_retry(self, mock_client_class, mock_rotator):
        """429 response should mark key rate-limited and retry with next key."""
        from app.workers.api_discovery import _call_rakuten_api

        mock_rotator.key_count = 3
        mock_rotator.get_next_key.side_effect = [
            ("appId1", "key1"),
            ("appId2", "key2"),
        ]

        # First call returns 429, second returns 200
        mock_resp_429 = MagicMock()
        mock_resp_429.status_code = 429

        mock_resp_200 = MagicMock()
        mock_resp_200.status_code = 200
        mock_resp_200.json.return_value = _make_api_response()
        mock_resp_200.raise_for_status = MagicMock()

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.side_effect = [mock_resp_429, mock_resp_200]
        mock_client_class.return_value = mock_client

        result = _call_rakuten_api({"genreId": "215783"})

        assert result["count"] == 100
        mock_rotator.mark_rate_limited.assert_called_once_with("appId1", cooldown_sec=10.0)

    @patch("app.workers.api_discovery.key_rotator")
    @patch("httpx.Client")
    def test_http_error_retries(self, mock_client_class, mock_rotator):
        """HTTP errors should trigger retries with exponential backoff."""
        import httpx
        from app.workers.api_discovery import _call_rakuten_api

        mock_rotator.key_count = 3
        mock_rotator.get_next_key.side_effect = [
            ("appId1", "key1"),
            ("appId2", "key2"),
            ("appId3", "key3"),
        ]

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.side_effect = httpx.ConnectError("Connection refused")
        mock_client_class.return_value = mock_client

        with pytest.raises(httpx.ConnectError):
            _call_rakuten_api({"genreId": "215783"})

        # Should have retried 3 times
        assert mock_client.get.call_count == 3


# ═══════════════════════════════════════════════════════════════
#  3. Response Processor Tests
# ═══════════════════════════════════════════════════════════════

class TestProcessAPIItems:
    """Tests for _process_api_items — the core item processing logic."""

    def _setup_mock_db(self):
        """Create a mock DB session."""
        mock_db = MagicMock()
        mock_db.execute.return_value.first.return_value = None  # No existing crawl_job
        return mock_db

    @patch("app.workers.api_discovery.upsert_snapshot_and_history_sync")
    @patch("app.workers.api_discovery.upsert_variant_sync", return_value=1001)
    @patch("app.workers.api_discovery.upsert_item_sync", return_value=501)
    @patch("app.workers.api_discovery.upsert_shop_sync")
    def test_process_single_item(self, mock_shop, mock_item, mock_variant, mock_snapshot):
        """Processing a single API item should upsert shop → item → variant → snapshot."""
        from app.workers.api_discovery import _process_api_items

        mock_db = self._setup_mock_db()
        items = [_make_api_item()]

        enqueued = _process_api_items(items, mock_db, genre_id_context="215783")

        # Shop upsert
        mock_shop.assert_called_once()
        shop_args = mock_shop.call_args
        assert shop_args.kwargs["shop_code"] == "horiman"
        assert shop_args.kwargs["shop_name"] == "ほりまん茶園"

        # Item upsert
        mock_item.assert_called_once()
        item_args = mock_item.call_args
        assert item_args.kwargs["shop_code"] == "horiman"
        assert item_args.kwargs["item_code"] == "10009058"
        assert item_args.kwargs["source"] == "api"
        assert item_args.kwargs["api_item_code"] == "horiman:10009058"

        # Variant upsert
        mock_variant.assert_called_once()

        # Snapshot upsert
        mock_snapshot.assert_called_once()
        snap_args = mock_snapshot.call_args
        assert snap_args.kwargs["source"] == "api"
        snap_data = snap_args.kwargs["snapshot_data"]
        assert snap_data["price"] == 2500
        assert snap_data["shipping_text_raw"] == "送料無料"  # postageFlag=0

        # Should enqueue a detail scrape job
        assert enqueued == 1

    @patch("app.workers.api_discovery.upsert_snapshot_and_history_sync")
    @patch("app.workers.api_discovery.upsert_variant_sync", return_value=1001)
    @patch("app.workers.api_discovery.upsert_item_sync", return_value=501)
    @patch("app.workers.api_discovery.upsert_shop_sync")
    def test_skip_unavailable_items(self, mock_shop, mock_item, mock_variant, mock_snapshot):
        """Items with availability=0 should be skipped."""
        from app.workers.api_discovery import _process_api_items

        item = _make_api_item()
        item["Item"]["availability"] = 0

        mock_db = self._setup_mock_db()
        enqueued = _process_api_items([item], mock_db)

        mock_shop.assert_not_called()
        mock_item.assert_not_called()
        assert enqueued == 0

    @patch("app.workers.api_discovery.upsert_snapshot_and_history_sync")
    @patch("app.workers.api_discovery.upsert_variant_sync", return_value=1001)
    @patch("app.workers.api_discovery.upsert_item_sync", return_value=501)
    @patch("app.workers.api_discovery.upsert_shop_sync")
    def test_skip_items_without_url(self, mock_shop, mock_item, mock_variant, mock_snapshot):
        """Items without itemUrl should be skipped."""
        from app.workers.api_discovery import _process_api_items

        item = _make_api_item()
        item["Item"]["itemUrl"] = ""

        mock_db = self._setup_mock_db()
        enqueued = _process_api_items([item], mock_db)

        mock_shop.assert_not_called()
        assert enqueued == 0

    @patch("app.workers.api_discovery.upsert_snapshot_and_history_sync")
    @patch("app.workers.api_discovery.upsert_variant_sync", return_value=1001)
    @patch("app.workers.api_discovery.upsert_item_sync", return_value=501)
    @patch("app.workers.api_discovery.upsert_shop_sync")
    def test_multiple_items_processing(self, mock_shop, mock_item, mock_variant, mock_snapshot):
        """Should process all valid items in a batch."""
        from app.workers.api_discovery import _process_api_items

        items = [
            _make_api_item(item_code="shop1:001", shop_code="shop1", item_url="https://item.rakuten.co.jp/shop1/001/"),
            _make_api_item(item_code="shop2:002", shop_code="shop2", item_url="https://item.rakuten.co.jp/shop2/002/"),
            _make_api_item(item_code="shop3:003", shop_code="shop3", item_url="https://item.rakuten.co.jp/shop3/003/"),
        ]

        mock_db = self._setup_mock_db()
        enqueued = _process_api_items(items, mock_db)

        assert mock_shop.call_count == 3
        assert mock_item.call_count == 3
        assert mock_variant.call_count == 3
        assert mock_snapshot.call_count == 3
        assert enqueued == 3

    @patch("app.workers.api_discovery.upsert_snapshot_and_history_sync")
    @patch("app.workers.api_discovery.upsert_variant_sync", return_value=1001)
    @patch("app.workers.api_discovery.upsert_item_sync", return_value=501)
    @patch("app.workers.api_discovery.upsert_shop_sync")
    def test_deduplicate_crawl_jobs(self, mock_shop, mock_item, mock_variant, mock_snapshot):
        """If a crawl_job already exists for the URL, should not enqueue another."""
        from app.workers.api_discovery import _process_api_items

        mock_db = MagicMock()
        # Simulate existing crawl_job
        mock_db.execute.return_value.first.return_value = (999,)

        items = [_make_api_item()]
        enqueued = _process_api_items(items, mock_db)

        # Job already exists → no new enqueue
        assert enqueued == 0

    @patch("app.workers.api_discovery.upsert_snapshot_and_history_sync")
    @patch("app.workers.api_discovery.upsert_variant_sync", return_value=1001)
    @patch("app.workers.api_discovery.upsert_item_sync", return_value=501)
    @patch("app.workers.api_discovery.upsert_shop_sync")
    def test_review_data_stored_in_extra_fields(self, mock_shop, mock_item, mock_variant, mock_snapshot):
        """Review count/average should be stored in extra1/extra2 fields."""
        from app.workers.api_discovery import _process_api_items

        items = [_make_api_item(review_count=150, review_average=4.92)]
        mock_db = self._setup_mock_db()
        _process_api_items(items, mock_db)

        snap_data = mock_snapshot.call_args.kwargs["snapshot_data"]
        assert snap_data["extra1_key"] == "review_count"
        assert snap_data["extra1_value"] == "150"
        assert snap_data["extra2_key"] == "review_average"
        assert snap_data["extra2_value"] == "4.92"

    @patch("app.workers.api_discovery.upsert_snapshot_and_history_sync")
    @patch("app.workers.api_discovery.upsert_variant_sync", return_value=1001)
    @patch("app.workers.api_discovery.upsert_item_sync", return_value=501)
    @patch("app.workers.api_discovery.upsert_shop_sync")
    def test_url_canonicalization(self, mock_shop, mock_item, mock_variant, mock_snapshot):
        """Tracking params (scid, rafcid) should be stripped from URLs."""
        from app.workers.api_discovery import _process_api_items

        items = [_make_api_item(
            item_url="https://item.rakuten.co.jp/horiman/10009058/?scid=af_pc&rafcid=wsc_i_is_abc"
        )]
        mock_db = self._setup_mock_db()
        _process_api_items(items, mock_db)

        item_args = mock_item.call_args.kwargs
        assert "scid" not in item_args["canonical_url"]
        assert "rafcid" not in item_args["canonical_url"]
        assert "item.rakuten.co.jp/horiman/10009058/" in item_args["canonical_url"]


# ═══════════════════════════════════════════════════════════════
#  4. Genre Discovery Task Tests
# ═══════════════════════════════════════════════════════════════

class TestDiscoverByGenre:
    """Tests for the discover_by_genre Celery task."""

    @patch("app.workers.api_discovery.time.sleep")
    @patch("app.workers.api_discovery._process_api_items", return_value=5)
    @patch("app.workers.api_discovery._call_rakuten_api")
    @patch("app.workers.api_discovery.get_sync_db")
    def test_normal_single_page(self, mock_get_db, mock_api, mock_process, mock_sleep):
        """Single-page genre discovery should process items and return ok."""
        from app.workers.api_discovery import discover_by_genre

        mock_db = MagicMock()
        mock_get_db.return_value = mock_db

        mock_api.return_value = _make_api_response(
            count=25, page=1, page_count=1,
            items=[_make_api_item()],
        )

        result = discover_by_genre("215783", min_price=0, max_price=None, page=1)

        assert result["status"] == "ok"
        assert result["genre_id"] == "215783"
        assert result["page"] == 1
        assert result["total_count"] == 25
        mock_process.assert_called_once()

    @patch("app.workers.api_discovery.time.sleep")
    @patch("app.workers.api_discovery.discover_by_genre.delay")
    @patch("app.workers.api_discovery._process_api_items", return_value=5)
    @patch("app.workers.api_discovery._call_rakuten_api")
    @patch("app.workers.api_discovery.get_sync_db")
    def test_pagination_enqueues_next_page(self, mock_get_db, mock_api, mock_process, mock_delay, mock_sleep):
        """Multi-page results should enqueue the next page."""
        from app.workers.api_discovery import discover_by_genre

        mock_db = MagicMock()
        mock_get_db.return_value = mock_db

        mock_api.return_value = _make_api_response(
            count=90, page=1, page_count=3,
            items=[_make_api_item()],
        )

        result = discover_by_genre("215783", min_price=0, max_price=1000, page=1)

        assert result["status"] == "ok"
        # Should enqueue page 2
        mock_delay.assert_called_once_with("215783", 0, 1000, 2)

    @patch("app.workers.api_discovery.time.sleep")
    @patch("app.workers.api_discovery.discover_by_genre.delay")
    @patch("app.workers.api_discovery._process_api_items", return_value=0)
    @patch("app.workers.api_discovery._call_rakuten_api")
    @patch("app.workers.api_discovery.get_sync_db")
    def test_price_range_splitting(self, mock_get_db, mock_api, mock_process, mock_delay, mock_sleep):
        """count > 3000 should trigger price range splitting."""
        from app.workers.api_discovery import discover_by_genre

        mock_db = MagicMock()
        mock_get_db.return_value = mock_db

        mock_api.return_value = _make_api_response(
            count=5000, page=1, page_count=100,
            items=[_make_api_item()],
        )

        result = discover_by_genre("215783", min_price=0, max_price=10000, page=1)

        assert result["status"] == "split"
        assert result["total_count"] == 5000

        # Should enqueue two sub-ranges
        assert mock_delay.call_count == 2
        calls = [c.args for c in mock_delay.call_args_list]
        # Lower half: (genre, 0, 5000, 1) and Upper half: (genre, 5001, 10000, 1)
        assert ("215783", 0, 5000, 1) in calls
        assert ("215783", 5001, 10000, 1) in calls


# ═══════════════════════════════════════════════════════════════
#  4b. Full 100-Page Pagination Tests (3,000 Products)
# ═══════════════════════════════════════════════════════════════

class TestFull100PagePagination:
    """
    Comprehensive tests for full API pagination through all 100 pages.

    Rakuten API returns 30 items/page, max 100 pages = 3,000 products.
    These tests verify the complete pipeline processes every product
    across all pages correctly.
    """

    @staticmethod
    def _make_page_items(page: int, items_per_page: int = 30) -> list[dict]:
        """Generate unique realistic items for a given page number."""
        items = []
        for i in range(items_per_page):
            idx = (page - 1) * items_per_page + i  # global index 0..2999
            shop_code = f"shop{idx % 50:03d}"       # 50 unique shops
            item_num = f"{10000 + idx}"
            items.append({
                "Item": {
                    "itemCode": f"{shop_code}:{item_num}",
                    "itemName": f"商品 #{idx + 1} テスト",
                    "catchcopy": f"キャッチコピー {idx + 1}",
                    "itemPrice": 100 + idx * 10,       # unique prices
                    "itemUrl": f"https://item.rakuten.co.jp/{shop_code}/{item_num}/?scid=af_pc",
                    "shopCode": shop_code,
                    "shopName": f"ショップ {shop_code}",
                    "shopUrl": f"https://www.rakuten.co.jp/{shop_code}/?rafcid=wsc",
                    "genreId": 215783,
                    "pointRate": 1,
                    "postageFlag": idx % 3,      # 0=free, 1/2=charged
                    "reviewCount": idx % 200,
                    "reviewAverage": round(3.0 + (idx % 20) * 0.1, 2),
                    "availability": 1,
                    "mediumImageUrls": [
                        {"imageUrl": f"https://thumbnail.image.rakuten.co.jp/@0_mall/{shop_code}/img{item_num}.jpg"}
                    ],
                    "tagIds": [],
                }
            })
        return items

    @staticmethod
    def _make_page_response(page: int, total_count: int = 3000, page_count: int = 100, items_per_page: int = 30) -> dict:
        """Build API response for a specific page."""
        items = TestFull100PagePagination._make_page_items(page, items_per_page)
        return {
            "count": total_count,
            "page": page,
            "first": (page - 1) * items_per_page + 1,
            "last": min(page * items_per_page, total_count),
            "hits": len(items),
            "pageCount": page_count,
            "Items": items,
        }

    # ────────────────────────────────────────────────────────────
    #  Test: Single page processes all 30 items correctly
    # ────────────────────────────────────────────────────────────
    @patch("app.workers.api_discovery.time.sleep")
    @patch("app.workers.api_discovery.discover_by_genre.delay")
    @patch("app.workers.api_discovery.upsert_snapshot_and_history_sync")
    @patch("app.workers.api_discovery.upsert_variant_sync", return_value=1001)
    @patch("app.workers.api_discovery.upsert_item_sync", return_value=501)
    @patch("app.workers.api_discovery.upsert_shop_sync")
    @patch("app.workers.api_discovery._call_rakuten_api")
    @patch("app.workers.api_discovery.get_sync_db")
    def test_single_page_processes_30_items(
        self, mock_get_db, mock_api, mock_shop, mock_item,
        mock_variant, mock_snapshot, mock_delay, mock_sleep,
    ):
        """A single page with 30 items should upsert all 30 shops, items, variants, snapshots."""
        from app.workers.api_discovery import discover_by_genre

        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        mock_db.execute.return_value.first.return_value = None

        mock_api.return_value = self._make_page_response(page=1, total_count=3000, page_count=100)

        result = discover_by_genre("215783", min_price=0, max_price=None, page=1)

        assert result["status"] == "ok"
        assert result["items_on_page"] == 30
        assert result["total_count"] == 3000
        assert result["page_count"] == 100

        # All 30 items processed
        assert mock_shop.call_count == 30
        assert mock_item.call_count == 30
        assert mock_variant.call_count == 30
        assert mock_snapshot.call_count == 30

        # Should enqueue page 2
        mock_delay.assert_called_once_with("215783", 0, None, 2)

    # ────────────────────────────────────────────────────────────
    #  Test: Full chain — all 100 pages, all 3000 products
    # ────────────────────────────────────────────────────────────
    @patch("app.workers.api_discovery.time.sleep")
    @patch("app.workers.api_discovery.upsert_snapshot_and_history_sync")
    @patch("app.workers.api_discovery.upsert_variant_sync", return_value=1001)
    @patch("app.workers.api_discovery.upsert_item_sync", return_value=501)
    @patch("app.workers.api_discovery.upsert_shop_sync")
    @patch("app.workers.api_discovery._call_rakuten_api")
    @patch("app.workers.api_discovery.get_sync_db")
    def test_full_100_page_chain_processes_3000_products(
        self, mock_get_db, mock_api, mock_shop, mock_item,
        mock_variant, mock_snapshot, mock_sleep,
    ):
        """
        Simulate the FULL 100-page pagination chain.

        Calls discover_by_genre for each page 1→100, capturing .delay()
        calls to verify the next page is enqueued. After page 100, no
        further pages should be enqueued.

        Verifies all 3,000 products (30/page × 100 pages) are processed.
        """
        from app.workers.api_discovery import discover_by_genre

        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        mock_db.execute.return_value.first.return_value = None

        total_items_processed = 0
        total_enqueued = 0
        pages_with_next = []

        for page_num in range(1, 101):
            # Configure API response for this page
            mock_api.return_value = self._make_page_response(
                page=page_num, total_count=3000, page_count=100,
            )

            # Reset per-page counters
            mock_shop.reset_mock()
            mock_item.reset_mock()
            mock_variant.reset_mock()
            mock_snapshot.reset_mock()

            # We need to capture .delay() calls per page
            with patch("app.workers.api_discovery.discover_by_genre.delay") as mock_delay:
                result = discover_by_genre(
                    "215783", min_price=0, max_price=None, page=page_num,
                )

            assert result["status"] == "ok", f"Page {page_num} failed"
            assert result["page"] == page_num
            assert result["items_on_page"] == 30
            assert result["total_count"] == 3000

            # Each page processes exactly 30 items
            assert mock_shop.call_count == 30, f"Page {page_num}: expected 30 shop upserts, got {mock_shop.call_count}"
            assert mock_item.call_count == 30, f"Page {page_num}: expected 30 item upserts, got {mock_item.call_count}"
            assert mock_variant.call_count == 30, f"Page {page_num}: expected 30 variant upserts, got {mock_variant.call_count}"
            assert mock_snapshot.call_count == 30, f"Page {page_num}: expected 30 snapshot upserts, got {mock_snapshot.call_count}"

            total_items_processed += 30
            total_enqueued += result["enqueued"]

            # Pages 1-99 should enqueue the next page; page 100 should NOT
            if mock_delay.called:
                pages_with_next.append(page_num)
                # Verify correct next page was enqueued
                next_page_args = mock_delay.call_args[0]
                assert next_page_args == ("215783", 0, None, page_num + 1), \
                    f"Page {page_num} enqueued wrong next page: {next_page_args}"

        # ── Final assertions across all 100 pages ──────────────
        assert total_items_processed == 3000, \
            f"Expected 3000 total items, got {total_items_processed}"
        assert total_enqueued == 3000, \
            f"Expected 3000 total enqueued detail jobs, got {total_enqueued}"

        # Pages 1-99 enqueue next page, page 100 does NOT
        assert pages_with_next == list(range(1, 100)), \
            f"Expected pages 1-99 to enqueue next, got: {pages_with_next}"
        assert 100 not in pages_with_next, \
            "Page 100 should NOT enqueue page 101"

    # ────────────────────────────────────────────────────────────
    #  Test: Page 100 is the last page — no page 101 enqueued
    # ────────────────────────────────────────────────────────────
    @patch("app.workers.api_discovery.time.sleep")
    @patch("app.workers.api_discovery.discover_by_genre.delay")
    @patch("app.workers.api_discovery.upsert_snapshot_and_history_sync")
    @patch("app.workers.api_discovery.upsert_variant_sync", return_value=1001)
    @patch("app.workers.api_discovery.upsert_item_sync", return_value=501)
    @patch("app.workers.api_discovery.upsert_shop_sync")
    @patch("app.workers.api_discovery._call_rakuten_api")
    @patch("app.workers.api_discovery.get_sync_db")
    def test_page_100_stops_pagination(
        self, mock_get_db, mock_api, mock_shop, mock_item,
        mock_variant, mock_snapshot, mock_delay, mock_sleep,
    ):
        """Page 100 (the maximum) should NOT enqueue page 101."""
        from app.workers.api_discovery import discover_by_genre

        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        mock_db.execute.return_value.first.return_value = None

        mock_api.return_value = self._make_page_response(page=100, total_count=3000, page_count=100)

        result = discover_by_genre("215783", min_price=0, max_price=None, page=100)

        assert result["status"] == "ok"
        assert result["page"] == 100
        assert result["items_on_page"] == 30

        # MUST NOT enqueue page 101
        mock_delay.assert_not_called()

    # ────────────────────────────────────────────────────────────
    #  Test: Middle page correctly chains to the next page
    # ────────────────────────────────────────────────────────────
    @patch("app.workers.api_discovery.time.sleep")
    @patch("app.workers.api_discovery.discover_by_genre.delay")
    @patch("app.workers.api_discovery.upsert_snapshot_and_history_sync")
    @patch("app.workers.api_discovery.upsert_variant_sync", return_value=1001)
    @patch("app.workers.api_discovery.upsert_item_sync", return_value=501)
    @patch("app.workers.api_discovery.upsert_shop_sync")
    @patch("app.workers.api_discovery._call_rakuten_api")
    @patch("app.workers.api_discovery.get_sync_db")
    def test_middle_page_enqueues_next(
        self, mock_get_db, mock_api, mock_shop, mock_item,
        mock_variant, mock_snapshot, mock_delay, mock_sleep,
    ):
        """Page 50 (middle) should enqueue page 51."""
        from app.workers.api_discovery import discover_by_genre

        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        mock_db.execute.return_value.first.return_value = None

        mock_api.return_value = self._make_page_response(page=50, total_count=3000, page_count=100)

        result = discover_by_genre("215783", min_price=0, max_price=5000, page=50)

        assert result["status"] == "ok"
        assert result["page"] == 50
        mock_delay.assert_called_once_with("215783", 0, 5000, 51)

    # ────────────────────────────────────────────────────────────
    #  Test: Each product has unique shop_code + item_code
    # ────────────────────────────────────────────────────────────
    @patch("app.workers.api_discovery.time.sleep")
    @patch("app.workers.api_discovery.discover_by_genre.delay")
    @patch("app.workers.api_discovery.upsert_snapshot_and_history_sync")
    @patch("app.workers.api_discovery.upsert_variant_sync", return_value=1001)
    @patch("app.workers.api_discovery.upsert_item_sync", return_value=501)
    @patch("app.workers.api_discovery.upsert_shop_sync")
    @patch("app.workers.api_discovery._call_rakuten_api")
    @patch("app.workers.api_discovery.get_sync_db")
    def test_all_items_have_unique_canonical_urls(
        self, mock_get_db, mock_api, mock_shop, mock_item,
        mock_variant, mock_snapshot, mock_delay, mock_sleep,
    ):
        """All 3000 products across 100 pages should have unique canonical URLs."""
        from app.workers.api_discovery import discover_by_genre

        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        mock_db.execute.return_value.first.return_value = None

        all_canonical_urls = set()

        for page_num in range(1, 101):
            mock_api.return_value = self._make_page_response(page=page_num)
            mock_item.reset_mock()

            with patch("app.workers.api_discovery.discover_by_genre.delay"):
                discover_by_genre("215783", min_price=0, max_price=None, page=page_num)

            # Collect all canonical_url values from upsert_item_sync calls
            for call in mock_item.call_args_list:
                canonical_url = call.kwargs["canonical_url"]
                all_canonical_urls.add(canonical_url)

        assert len(all_canonical_urls) == 3000, \
            f"Expected 3000 unique canonical URLs, got {len(all_canonical_urls)}"

    # ────────────────────────────────────────────────────────────
    #  Test: Prices increase across pages (sort=+itemPrice)
    # ────────────────────────────────────────────────────────────
    @patch("app.workers.api_discovery.time.sleep")
    @patch("app.workers.api_discovery.discover_by_genre.delay")
    @patch("app.workers.api_discovery.upsert_snapshot_and_history_sync")
    @patch("app.workers.api_discovery.upsert_variant_sync", return_value=1001)
    @patch("app.workers.api_discovery.upsert_item_sync", return_value=501)
    @patch("app.workers.api_discovery.upsert_shop_sync")
    @patch("app.workers.api_discovery._call_rakuten_api")
    @patch("app.workers.api_discovery.get_sync_db")
    def test_prices_increase_across_pages(
        self, mock_get_db, mock_api, mock_shop, mock_item,
        mock_variant, mock_snapshot, mock_delay, mock_sleep,
    ):
        """Items should be price-sorted ascending (API sort=+itemPrice)."""
        from app.workers.api_discovery import discover_by_genre

        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        mock_db.execute.return_value.first.return_value = None

        all_prices = []

        for page_num in [1, 50, 100]:  # Spot-check first, middle, last
            mock_api.return_value = self._make_page_response(page=page_num)
            mock_snapshot.reset_mock()

            with patch("app.workers.api_discovery.discover_by_genre.delay"):
                discover_by_genre("215783", min_price=0, max_price=None, page=page_num)

            # Collect prices from snapshots on this page
            page_prices = [
                c.kwargs["snapshot_data"]["price"]
                for c in mock_snapshot.call_args_list
            ]
            all_prices.extend(page_prices)

        # Prices from page 1 should all be < prices from page 50 < prices from page 100
        page1_max = max(all_prices[:30])
        page50_min = min(all_prices[30:60])
        page100_min = min(all_prices[60:90])

        assert page1_max < page50_min, \
            f"Page 1 max price ({page1_max}) should be < page 50 min ({page50_min})"
        assert page50_min < page100_min, \
            f"Page 50 min price ({page50_min}) should be < page 100 min ({page100_min})"

    # ────────────────────────────────────────────────────────────
    #  Test: Snapshot data correctness for every item
    # ────────────────────────────────────────────────────────────
    @patch("app.workers.api_discovery.time.sleep")
    @patch("app.workers.api_discovery.discover_by_genre.delay")
    @patch("app.workers.api_discovery.upsert_snapshot_and_history_sync")
    @patch("app.workers.api_discovery.upsert_variant_sync", return_value=1001)
    @patch("app.workers.api_discovery.upsert_item_sync", return_value=501)
    @patch("app.workers.api_discovery.upsert_shop_sync")
    @patch("app.workers.api_discovery._call_rakuten_api")
    @patch("app.workers.api_discovery.get_sync_db")
    def test_snapshot_data_complete_for_all_items(
        self, mock_get_db, mock_api, mock_shop, mock_item,
        mock_variant, mock_snapshot, mock_delay, mock_sleep,
    ):
        """Every snapshot should have price, point_rate, image_url fields."""
        from app.workers.api_discovery import discover_by_genre

        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        mock_db.execute.return_value.first.return_value = None

        # Test 5 representative pages
        for page_num in [1, 25, 50, 75, 100]:
            mock_snapshot.reset_mock()
            mock_api.return_value = self._make_page_response(page=page_num)

            with patch("app.workers.api_discovery.discover_by_genre.delay"):
                discover_by_genre("215783", min_price=0, max_price=None, page=page_num)

            for call_idx, call in enumerate(mock_snapshot.call_args_list):
                snap_data = call.kwargs["snapshot_data"]

                # Every snapshot must have price
                assert snap_data["price"] is not None, \
                    f"Page {page_num} item {call_idx}: missing price"

                # Every snapshot must have point_rate
                assert snap_data["point_rate"] is not None, \
                    f"Page {page_num} item {call_idx}: missing point_rate"

                # Every snapshot must have image_url
                assert snap_data["image_url"] is not None, \
                    f"Page {page_num} item {call_idx}: missing image_url"
                assert "thumbnail.image.rakuten.co.jp" in snap_data["image_url"]

                # Source must be "api"
                assert call.kwargs["source"] == "api"

    # ────────────────────────────────────────────────────────────
    #  Test: Free shipping items detected from postageFlag
    # ────────────────────────────────────────────────────────────
    @patch("app.workers.api_discovery.time.sleep")
    @patch("app.workers.api_discovery.discover_by_genre.delay")
    @patch("app.workers.api_discovery.upsert_snapshot_and_history_sync")
    @patch("app.workers.api_discovery.upsert_variant_sync", return_value=1001)
    @patch("app.workers.api_discovery.upsert_item_sync", return_value=501)
    @patch("app.workers.api_discovery.upsert_shop_sync")
    @patch("app.workers.api_discovery._call_rakuten_api")
    @patch("app.workers.api_discovery.get_sync_db")
    def test_free_shipping_detected_across_pages(
        self, mock_get_db, mock_api, mock_shop, mock_item,
        mock_variant, mock_snapshot, mock_delay, mock_sleep,
    ):
        """Items with postageFlag=0 should have shipping_text_raw='送料無料'."""
        from app.workers.api_discovery import discover_by_genre

        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        mock_db.execute.return_value.first.return_value = None

        mock_api.return_value = self._make_page_response(page=1)

        with patch("app.workers.api_discovery.discover_by_genre.delay"):
            discover_by_genre("215783", min_price=0, max_price=None, page=1)

        free_shipping_count = 0
        for call in mock_snapshot.call_args_list:
            snap_data = call.kwargs["snapshot_data"]
            if snap_data.get("shipping_text_raw") == "送料無料":
                free_shipping_count += 1

        # In our test data, every 3rd item has postageFlag=0
        assert free_shipping_count == 10, \
            f"Expected 10 free-shipping items (every 3rd), got {free_shipping_count}"

    # ────────────────────────────────────────────────────────────
    #  Test: Detail scrape jobs enqueued for all products
    # ────────────────────────────────────────────────────────────
    @patch("app.workers.api_discovery.time.sleep")
    @patch("app.workers.api_discovery.discover_by_genre.delay")
    @patch("app.workers.api_discovery.upsert_snapshot_and_history_sync")
    @patch("app.workers.api_discovery.upsert_variant_sync", return_value=1001)
    @patch("app.workers.api_discovery.upsert_item_sync", return_value=501)
    @patch("app.workers.api_discovery.upsert_shop_sync")
    @patch("app.workers.api_discovery._call_rakuten_api")
    @patch("app.workers.api_discovery.get_sync_db")
    def test_detail_jobs_enqueued_for_all_3000(
        self, mock_get_db, mock_api, mock_shop, mock_item,
        mock_variant, mock_snapshot, mock_delay, mock_sleep,
    ):
        """All 3000 products should each have a detail_scrape crawl_job enqueued."""
        from app.workers.api_discovery import discover_by_genre

        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        mock_db.execute.return_value.first.return_value = None  # No existing jobs

        total_enqueued = 0

        for page_num in range(1, 101):
            mock_api.return_value = self._make_page_response(page=page_num)

            with patch("app.workers.api_discovery.discover_by_genre.delay"):
                result = discover_by_genre("215783", min_price=0, max_price=None, page=page_num)

            total_enqueued += result["enqueued"]

        assert total_enqueued == 3000, \
            f"Expected 3000 detail scrape jobs enqueued, got {total_enqueued}"

    # ────────────────────────────────────────────────────────────
    #  Test: Existing crawl jobs are deduplicated across pages
    # ────────────────────────────────────────────────────────────
    @patch("app.workers.api_discovery.time.sleep")
    @patch("app.workers.api_discovery.discover_by_genre.delay")
    @patch("app.workers.api_discovery.upsert_snapshot_and_history_sync")
    @patch("app.workers.api_discovery.upsert_variant_sync", return_value=1001)
    @patch("app.workers.api_discovery.upsert_item_sync", return_value=501)
    @patch("app.workers.api_discovery.upsert_shop_sync")
    @patch("app.workers.api_discovery._call_rakuten_api")
    @patch("app.workers.api_discovery.get_sync_db")
    def test_deduplication_when_job_exists(
        self, mock_get_db, mock_api, mock_shop, mock_item,
        mock_variant, mock_snapshot, mock_delay, mock_sleep,
    ):
        """If a crawl_job already exists for a URL, should not double-enqueue."""
        from app.workers.api_discovery import discover_by_genre

        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        # Simulate ALL items already have pending crawl_jobs
        mock_db.execute.return_value.first.return_value = (999,)

        mock_api.return_value = self._make_page_response(page=1)

        with patch("app.workers.api_discovery.discover_by_genre.delay"):
            result = discover_by_genre("215783", min_price=0, max_price=None, page=1)

        # Items are still processed (upserted) but no new jobs enqueued
        assert mock_shop.call_count == 30
        assert mock_item.call_count == 30
        assert result["enqueued"] == 0

    # ────────────────────────────────────────────────────────────
    #  Test: Unavailable items are skipped on every page
    # ────────────────────────────────────────────────────────────
    @patch("app.workers.api_discovery.time.sleep")
    @patch("app.workers.api_discovery.discover_by_genre.delay")
    @patch("app.workers.api_discovery.upsert_snapshot_and_history_sync")
    @patch("app.workers.api_discovery.upsert_variant_sync", return_value=1001)
    @patch("app.workers.api_discovery.upsert_item_sync", return_value=501)
    @patch("app.workers.api_discovery.upsert_shop_sync")
    @patch("app.workers.api_discovery._call_rakuten_api")
    @patch("app.workers.api_discovery.get_sync_db")
    def test_unavailable_items_skipped_across_pages(
        self, mock_get_db, mock_api, mock_shop, mock_item,
        mock_variant, mock_snapshot, mock_delay, mock_sleep,
    ):
        """Items with availability=0 should be skipped even in full pagination."""
        from app.workers.api_discovery import discover_by_genre

        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        mock_db.execute.return_value.first.return_value = None

        # Build page where every 5th item is unavailable
        items = self._make_page_items(page=1, items_per_page=30)
        for i in range(0, 30, 5):
            items[i]["Item"]["availability"] = 0

        resp = {
            "count": 3000, "page": 1, "first": 1, "last": 30,
            "hits": 30, "pageCount": 100, "Items": items,
        }
        mock_api.return_value = resp

        with patch("app.workers.api_discovery.discover_by_genre.delay"):
            result = discover_by_genre("215783", min_price=0, max_price=None, page=1)

        assert result["status"] == "ok"
        # 6 items unavailable (indices 0,5,10,15,20,25) → 24 processed
        assert mock_shop.call_count == 24
        assert mock_item.call_count == 24
        assert result["enqueued"] == 24

    # ────────────────────────────────────────────────────────────
    #  Test: Last page with fewer than 30 items
    # ────────────────────────────────────────────────────────────
    @patch("app.workers.api_discovery.time.sleep")
    @patch("app.workers.api_discovery.discover_by_genre.delay")
    @patch("app.workers.api_discovery.upsert_snapshot_and_history_sync")
    @patch("app.workers.api_discovery.upsert_variant_sync", return_value=1001)
    @patch("app.workers.api_discovery.upsert_item_sync", return_value=501)
    @patch("app.workers.api_discovery.upsert_shop_sync")
    @patch("app.workers.api_discovery._call_rakuten_api")
    @patch("app.workers.api_discovery.get_sync_db")
    def test_partial_last_page(
        self, mock_get_db, mock_api, mock_shop, mock_item,
        mock_variant, mock_snapshot, mock_delay, mock_sleep,
    ):
        """Last page with fewer items should still process all and NOT enqueue next."""
        from app.workers.api_discovery import discover_by_genre

        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        mock_db.execute.return_value.first.return_value = None

        # Total 2875 items: 95 full pages + 1 page with 25 items = 96 pages
        last_page_items = self._make_page_items(page=96, items_per_page=25)
        mock_api.return_value = {
            "count": 2875, "page": 96, "first": 2851, "last": 2875,
            "hits": 25, "pageCount": 96, "Items": last_page_items,
        }

        result = discover_by_genre("215783", min_price=0, max_price=None, page=96)

        assert result["status"] == "ok"
        assert result["items_on_page"] == 25
        assert mock_item.call_count == 25

        # This is the last page → no more pages to enqueue
        mock_delay.assert_not_called()

    # ────────────────────────────────────────────────────────────
    #  Test: Review data stored for items with reviews
    # ────────────────────────────────────────────────────────────
    @patch("app.workers.api_discovery.time.sleep")
    @patch("app.workers.api_discovery.discover_by_genre.delay")
    @patch("app.workers.api_discovery.upsert_snapshot_and_history_sync")
    @patch("app.workers.api_discovery.upsert_variant_sync", return_value=1001)
    @patch("app.workers.api_discovery.upsert_item_sync", return_value=501)
    @patch("app.workers.api_discovery.upsert_shop_sync")
    @patch("app.workers.api_discovery._call_rakuten_api")
    @patch("app.workers.api_discovery.get_sync_db")
    def test_review_data_stored_across_pages(
        self, mock_get_db, mock_api, mock_shop, mock_item,
        mock_variant, mock_snapshot, mock_delay, mock_sleep,
    ):
        """Items with reviewCount > 0 should have review data in extra fields."""
        from app.workers.api_discovery import discover_by_genre

        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        mock_db.execute.return_value.first.return_value = None

        mock_api.return_value = self._make_page_response(page=5)

        with patch("app.workers.api_discovery.discover_by_genre.delay"):
            discover_by_genre("215783", min_price=0, max_price=None, page=5)

        items_with_reviews = 0
        for call in mock_snapshot.call_args_list:
            snap_data = call.kwargs["snapshot_data"]
            if snap_data.get("extra1_key") == "review_count":
                items_with_reviews += 1
                assert snap_data["extra2_key"] == "review_average"

        # Items with reviewCount > 0 should have review data
        # In test data: reviewCount = idx % 200, so idx=0 has 0 reviews
        # Page 5 items have global idx 120-149, idx%200 = 120-149, all > 0
        assert items_with_reviews == 30


# ═══════════════════════════════════════════════════════════════
#  5. Keyword Discovery Task Tests
# ═══════════════════════════════════════════════════════════════

class TestDiscoverByKeyword:
    """Tests for the discover_by_keyword Celery task."""

    @patch("app.workers.api_discovery.time.sleep")
    @patch("app.workers.api_discovery._process_api_items", return_value=3)
    @patch("app.workers.api_discovery._call_rakuten_api")
    @patch("app.workers.api_discovery.get_sync_db")
    def test_keyword_discovery(self, mock_get_db, mock_api, mock_process, mock_sleep):
        """Keyword discovery should pass keyword param to the API."""
        from app.workers.api_discovery import discover_by_keyword

        mock_db = MagicMock()
        mock_get_db.return_value = mock_db

        mock_api.return_value = _make_api_response(count=10, page=1, page_count=1)

        result = discover_by_keyword("抹茶", genre_id="215783", page=1)

        assert result["status"] == "ok"
        assert result["keyword"] == "抹茶"

        # Verify keyword was passed to API
        api_params = mock_api.call_args[0][0]
        assert api_params["keyword"] == "抹茶"
        assert api_params["genreId"] == "215783"


# ═══════════════════════════════════════════════════════════════
#  6. Discover All Genres Tests
# ═══════════════════════════════════════════════════════════════

class TestDiscoverAllGenres:
    """Tests for the discover_all_genres orchestration task."""

    @patch("app.workers.api_discovery.discover_by_genre.delay")
    @patch("app.workers.api_discovery.get_sync_db")
    def test_dispatches_all_genres(self, mock_get_db, mock_delay):
        """Should dispatch tasks for all configured genre IDs."""
        from app.workers.api_discovery import discover_all_genres

        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        # No existing crawl_target records → will create and dispatch
        mock_db.execute.return_value.first.return_value = None

        result = discover_all_genres()

        assert result["status"] == "ok"
        assert result["tasks_dispatched"] == 3
        assert set(result["genre_ids"]) == {"215783", "100938", "551169"}

    @patch("app.workers.api_discovery.discover_by_genre.delay")
    @patch("app.workers.api_discovery.get_sync_db")
    def test_uses_price_slices_if_available(self, mock_get_db, mock_delay):
        """If price slices exist for a genre, should use them."""
        from app.workers.api_discovery import discover_all_genres

        mock_db = MagicMock()
        mock_get_db.return_value = mock_db

        # First genre has a crawl_target with slices
        mock_db.execute.return_value.first.side_effect = [
            (1,),  # target exists for genre 215783
            None,  # no target for genre 100938
            None,  # no target for genre 551169
        ]
        mock_db.execute.return_value.mappings.return_value.all.return_value = [
            {"min_price": 0, "max_price": 5000},
            {"min_price": 5001, "max_price": 10000},
        ]

        result = discover_all_genres()
        assert result["status"] == "ok"


# ═══════════════════════════════════════════════════════════════
#  7. URL Canonicalization Tests (used by API discovery)
# ═══════════════════════════════════════════════════════════════

class TestURLCanonicalization:
    """Tests for URL canonicalization used in API item processing."""

    def test_strip_tracking_params(self):
        from app.services.url_canonicalizer import canonicalize_item_url
        url = "https://item.rakuten.co.jp/horiman/10009058/?rafcid=wsc&scid=af_pc&s-id=xxx"
        canonical, variant_id = canonicalize_item_url(url)

        assert "rafcid" not in canonical
        assert "scid" not in canonical
        assert "s-id" not in canonical
        assert canonical == "https://item.rakuten.co.jp/horiman/10009058/"
        assert variant_id is None

    def test_extract_variant_id(self):
        from app.services.url_canonicalizer import canonicalize_item_url
        url = "https://item.rakuten.co.jp/rakuten24/404953/?variantId=4901301445520"
        canonical, variant_id = canonicalize_item_url(url)

        assert variant_id == "4901301445520"
        assert "variantId" not in canonical

    def test_extract_shop_item_code(self):
        from app.services.url_canonicalizer import extract_shop_item_code
        shop, item = extract_shop_item_code("https://item.rakuten.co.jp/horiman/10009058/")
        assert shop == "horiman"
        assert item == "10009058"

    def test_make_item_id_str(self):
        from app.services.url_canonicalizer import make_item_id_str
        uid = make_item_id_str("horiman", "10009058")
        assert uid == "horiman:10009058"

    def test_parse_api_item_code(self):
        from app.services.url_canonicalizer import parse_api_item_code
        shop, product_id = parse_api_item_code("horiman:10009058")
        assert shop == "horiman"
        assert product_id == "10009058"

    def test_clean_tracking_params(self):
        from app.services.url_canonicalizer import clean_tracking_params
        url = "https://www.rakuten.co.jp/horiman/?rafcid=wsc_i_is_xxx"
        cleaned = clean_tracking_params(url)
        assert cleaned == "https://www.rakuten.co.jp/horiman/"

    def test_build_search_url_genre(self):
        from app.services.url_canonicalizer import build_search_url
        url = build_search_url(genre_id="215783", page=2, min_price=100, max_price=5000)
        assert "215783" in url
        assert "min=100" in url
        assert "max=5000" in url
        assert "p=2" in url

    def test_build_search_url_shop(self):
        from app.services.url_canonicalizer import build_search_url
        url = build_search_url(shop_code="261122", page=1)
        assert "sid=261122" in url
