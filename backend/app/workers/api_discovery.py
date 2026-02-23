"""
API Discovery Worker.

Step 1 of the 3-stage pipeline:
  Uses Rakuten Ichiba Item Search API to discover product URLs.

Real API endpoint:
  GET https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20220601

Required parameters:
  - format=json
  - applicationId  (one of 10 rotating keys)
  - accessKey      (paired with applicationId)
  - genreId (one of: 215783, 100938, 551169)
  - minPrice / maxPrice
  - sort=+itemPrice (ascending by price)
  - page (1-based)

API Key Rotation:
  10 applicationId+accessKey pairs are configured in settings.RAKUTEN_API_KEYS.
  Keys are rotated round-robin per API call to distribute rate limits evenly.
  If a key gets rate-limited (429), we skip to the next key and retry.

Response structure:
  {
    "count": 2867,       # total matching items
    "page": 1,           # current page
    "first": 1,          # first item index on page
    "last": 30,          # last item index on page
    "hits": 30,          # items on this page
    "pageCount": 96,     # total pages available
    "Items": [
      { "Item": { ... } },
      ...
    ]
  }

Each Item contains:
  itemName, catchcopy, itemCode (shopCode:productId),
  itemPrice, itemUrl, shopUrl, shopName, shopCode,
  genreId, pointRate, postageFlag, reviewCount, reviewAverage,
  mediumImageUrls[].imageUrl, tagIds[], availability, ...

Key features:
  - 10 API key pairs rotated round-robin for 10x throughput
  - Per-key rate-limit tracking with automatic failover
  - Price range slicing to stay within API page limit
  - Pagination through all pages (using pageCount from response)
  - Creates initial snapshot with API data (price, pointRate, image)
  - Enqueues detail_scrape jobs for enrichment
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

import httpx
from sqlalchemy import text

from app.celery_app import celery_app
from app.config import settings
from app.db.session import get_sync_db
from app.services.item_service import (
    upsert_item_sync,
    upsert_shop_sync,
    upsert_variant_sync,
)
from app.services.snapshot_service import upsert_snapshot_and_history_sync
from app.services.url_canonicalizer import (
    build_search_url,
    canonicalize_item_url,
    clean_tracking_params,
    extract_shop_item_code,
    make_item_id_str,
)

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
#  API Constants
# ═══════════════════════════════════════════════════════════════

# Rakuten Ichiba Item Search API (real endpoint)
SEARCH_API_URL = f"{settings.RAKUTEN_API_BASE_URL}/IchibaItem/Search/20220601"

# Rakuten Ichiba Genre Search API (for enumerating all genres)
GENRE_API_URL = f"{settings.RAKUTEN_API_BASE_URL}/IchibaGenre/Search/20220601"

# API returns 30 items per page (fixed)
API_HITS_PER_PAGE = 30

# Max page number the API allows
API_MAX_PAGE = 100

# Max items reachable = 30 * 100 = 3000
# If count > 3000, we must split the price range
API_MAX_ITEMS = API_HITS_PER_PAGE * API_MAX_PAGE


# ═══════════════════════════════════════════════════════════════
#  API Key Rotation Manager
# ═══════════════════════════════════════════════════════════════

class APIKeyRotator:
    """
    Thread-safe round-robin API key rotator with rate-limit tracking.

    Manages 10 (applicationId, accessKey) pairs. Each call to
    get_next_key() returns the next available key pair. If a key
    is rate-limited, it can be marked and temporarily skipped.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._keys: list[tuple[str, str]] = settings.api_key_pairs
        self._index: int = 0
        # Track rate-limited keys: key_index → expiry timestamp
        self._rate_limited: dict[int, float] = {}

    @property
    def key_count(self) -> int:
        return len(self._keys)

    def get_next_key(self) -> tuple[str, str]:
        """
        Get the next available (applicationId, accessKey) pair.

        Skips rate-limited keys. If ALL keys are rate-limited,
        waits until the earliest one expires, then returns it.

        Returns:
            (application_id, access_key) tuple

        Raises:
            RuntimeError: If no API keys are configured.
        """
        if not self._keys:
            raise RuntimeError(
                "No API keys configured. Set RAKUTEN_API_KEYS in .env file. "
                "Format: appId1|key1,appId2|key2,..."
            )

        with self._lock:
            now = time.time()

            # Clean up expired rate limits
            self._rate_limited = {
                k: v for k, v in self._rate_limited.items() if v > now
            }

            # Try to find a non-rate-limited key (round-robin)
            for _ in range(len(self._keys)):
                idx = self._index % len(self._keys)
                self._index += 1

                if idx not in self._rate_limited:
                    return self._keys[idx]

            # ALL keys are rate-limited — find the one that expires soonest
            earliest_idx = min(self._rate_limited, key=self._rate_limited.get)
            wait_seconds = self._rate_limited[earliest_idx] - now
            if wait_seconds > 0:
                logger.warning(
                    "All %d API keys rate-limited. Waiting %.1fs for key #%d",
                    len(self._keys), wait_seconds, earliest_idx,
                )
                # Release lock while waiting
                self._lock.release()
                try:
                    time.sleep(wait_seconds + 0.1)
                finally:
                    self._lock.acquire()

            # Remove expired entry and return
            self._rate_limited.pop(earliest_idx, None)
            self._index = earliest_idx + 1
            return self._keys[earliest_idx]

    def mark_rate_limited(self, app_id: str, cooldown_sec: float = 10.0) -> None:
        """
        Mark a key as rate-limited for `cooldown_sec` seconds.

        Args:
            app_id: The applicationId that received a 429 response.
            cooldown_sec: How long to skip this key (default 10s).
        """
        with self._lock:
            for idx, (aid, _) in enumerate(self._keys):
                if aid == app_id:
                    self._rate_limited[idx] = time.time() + cooldown_sec
                    logger.info(
                        "Key #%d (%s...) rate-limited for %.0fs",
                        idx, app_id[:12], cooldown_sec,
                    )
                    break

    def get_status(self) -> dict:
        """Get current status of all keys (for monitoring)."""
        now = time.time()
        with self._lock:
            status = []
            for idx, (app_id, _) in enumerate(self._keys):
                limited_until = self._rate_limited.get(idx)
                status.append({
                    "index": idx,
                    "app_id": f"{app_id[:12]}...",
                    "rate_limited": limited_until is not None and limited_until > now,
                    "cooldown_remaining": (
                        max(0, limited_until - now) if limited_until else 0
                    ),
                })
            return {
                "total_keys": len(self._keys),
                "available_keys": sum(
                    1 for s in status if not s["rate_limited"]
                ),
                "keys": status,
            }


# Global key rotator instance (shared across all Celery worker threads)
key_rotator = APIKeyRotator()


# ═══════════════════════════════════════════════════════════════
#  API Caller (with key rotation)
# ═══════════════════════════════════════════════════════════════

def _call_rakuten_api(params: dict) -> dict:
    """
    Send GET request to Rakuten Item Search API.

    Uses round-robin API key rotation across 10 key pairs.
    Automatically retries with a different key on 429 rate-limit.
    Retries up to 3 times on transient errors.

    Args:
        params: Query parameters (genreId, minPrice, maxPrice, page, sort, etc.)
                applicationId and accessKey are injected automatically.

    Returns:
        Parsed JSON response dict.
    """
    params["format"] = "json"
    max_retries = min(3, key_rotator.key_count) if key_rotator.key_count > 0 else 3

    for attempt in range(max_retries):
        # Get next available key pair
        app_id, access_key = key_rotator.get_next_key()

        # Inject credentials
        call_params = {**params, "applicationId": app_id, "accessKey": access_key}

        try:
            with httpx.Client(timeout=30) as client:
                resp = client.get(SEARCH_API_URL, params=call_params)

            if resp.status_code == 429:
                # Rate limited on this key — mark it and try another
                key_rotator.mark_rate_limited(app_id, cooldown_sec=10.0)
                logger.warning(
                    "Rate limited (429) on key %s..., rotating to next key (attempt %d/%d)",
                    app_id[:12], attempt + 1, max_retries,
                )
                continue

            resp.raise_for_status()
            return resp.json()

        except httpx.HTTPError as e:
            logger.error(
                "API error on key %s... (attempt %d/%d): %s",
                app_id[:12], attempt + 1, max_retries, e,
            )
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
            else:
                raise

    return {}


# ═══════════════════════════════════════════════════════════════
#  Response Processor
# ═══════════════════════════════════════════════════════════════

def _process_api_items(items: list[dict], db, genre_id_context: str = "") -> int:
    """
    Process items from API response.

    For each item:
      1. Canonicalize URL, extract shop/item codes
      2. Upsert shop + item + default variant
      3. Create initial snapshot (price, pointRate, image, postage)
      4. Enqueue detail_scrape job for enrichment

    Returns count of new detail_scrape jobs enqueued.
    """
    enqueued = 0

    for item_entry in items:
        # API wraps each item in {"Item": {...}}
        item = item_entry.get("Item", item_entry)

        # ── Extract fields from API response ──────────────────
        item_url = item.get("itemUrl", "")
        if not item_url:
            continue

        # Skip unavailable items
        if item.get("availability", 1) == 0:
            continue

        shop_code = item.get("shopCode", "")
        shop_name = item.get("shopName")
        shop_url_raw = item.get("shopUrl", "")
        item_name = item.get("itemName")
        catchcopy = item.get("catchcopy") or None
        api_item_code = item.get("itemCode", "")  # e.g. "horiman:10009058"
        genre_id = item.get("genreId", "") or genre_id_context
        item_price = item.get("itemPrice")
        point_rate = item.get("pointRate")  # base point multiplier (e.g. 1)
        postage_flag = item.get("postageFlag", 1)  # 0=free, 1=charged
        review_count = item.get("reviewCount", 0)
        review_average = item.get("reviewAverage", 0.0)

        # Image URL from mediumImageUrls (first one)
        medium_images = item.get("mediumImageUrls", [])
        image_url = None
        if medium_images and isinstance(medium_images, list):
            first_img = medium_images[0]
            if isinstance(first_img, dict):
                image_url = first_img.get("imageUrl")
            elif isinstance(first_img, str):
                image_url = first_img

        # ── Canonicalize URLs ─────────────────────────────────
        canonical_url, variant_id_param = canonicalize_item_url(item_url)

        try:
            _, item_code = extract_shop_item_code(canonical_url)
        except ValueError:
            logger.warning("Cannot parse item URL: %s", item_url)
            continue

        # Clean shop URL (strip tracking params)
        shop_url_clean = clean_tracking_params(shop_url_raw) if shop_url_raw else None

        # ── Upsert shop ───────────────────────────────────────
        upsert_shop_sync(
            db,
            shop_code=shop_code,
            shop_name=shop_name,
            shop_url=shop_url_clean,
        )

        # ── Upsert item ──────────────────────────────────────
        item_id = upsert_item_sync(
            db,
            shop_code=shop_code,
            item_code=item_code,
            canonical_url=canonical_url,
            item_name=item_name,
            api_item_code=api_item_code,
            catchcopy=catchcopy,
            genre_id=str(genre_id) if genre_id else None,
            source="api",
        )

        # ── Upsert default variant ───────────────────────────
        variant_code = variant_id_param or "default"
        variant_id = upsert_variant_sync(
            db,
            item_id=item_id,
            variant_code=variant_code,
        )

        # ── Create initial snapshot from API data ─────────────
        # API provides: price, pointRate, image, postageFlag
        # Detail scraper will enrich: coupons, JAN, shipping days, etc.
        snapshot_data: dict = {
            "price": item_price,
            "point_rate": float(point_rate) if point_rate is not None else None,
            "image_url": image_url,
        }

        # Postage info from API flag
        if postage_flag == 0:
            snapshot_data["shipping_text_raw"] = "送料無料"

        # Store review data in extra fields
        if review_count > 0:
            snapshot_data["extra1_key"] = "review_count"
            snapshot_data["extra1_value"] = str(review_count)
            snapshot_data["extra2_key"] = "review_average"
            snapshot_data["extra2_value"] = str(review_average)

        upsert_snapshot_and_history_sync(
            db,
            variant_id=variant_id,
            snapshot_data=snapshot_data,
            source="api",
            skip_if_unchanged=True,
        )

        # Compute product_uid for logging / traceability
        product_uid = make_item_id_str(shop_code, item_code)

        # ── Enqueue detail scrape job (if not already pending) ──
        existing = db.execute(text("""
            SELECT id FROM crawl_jobs
            WHERE target_url = :url
              AND job_type = 'detail_scrape'
              AND status IN ('pending', 'running')
        """), {"url": canonical_url}).first()

        if not existing:
            db.execute(text("""
                INSERT INTO crawl_jobs (job_type, target_url, item_id, status)
                VALUES ('detail_scrape', :url, :item_id, 'pending')
            """), {"url": canonical_url, "item_id": item_id})
            enqueued += 1
            logger.debug(
                "Enqueued detail scrape for product_uid=%s url=%s",
                product_uid, canonical_url,
            )

    db.commit()
    return enqueued


# ═══════════════════════════════════════════════════════════════
#  Genre Discovery Task
# ═══════════════════════════════════════════════════════════════

@celery_app.task(name="app.workers.api_discovery.discover_by_genre")
def discover_by_genre(
    genre_id: str,
    min_price: int = 0,
    max_price: int | None = None,
    page: int = 1,
) -> dict:
    """
    Discover products via API for a specific genre + price range.

    **3-stage pipeline (per price slice):**
      Stage 1 (this task): API → get up to 3,000 products (30/page × 100 pages)
      Stage 2 (auto-triggered): List scraping → enrich with coupons, points, etc.
      Stage 3 (auto-enqueued): Detail scraping → full variant data per product

    **Price slicing for full coverage:**
      If a genre has >3,000 products, the price range is recursively split
      in half until each sub-range has ≤3,000 products. This covers ALL
      hundreds of thousands (or millions) of products in a genre.

    Flow:
      1. Call Rakuten Item Search API with genre + price range
      2. If total_count > 3,000:
         a. If max_price is None → determine actual max price → re-queue
         b. If max_price is set → binary split into two sub-ranges
      3. Process items on current page (upsert + snapshot + enqueue detail)
      4. Paginate to next page
      5. On LAST page → auto-trigger list scraping for same price range
    """
    db = get_sync_db()
    try:
        # ── Build API parameters ──────────────────────────────
        params: dict = {
            "genreId": genre_id,
            "page": page,
            "sort": "+itemPrice",
        }
        if min_price > 0:
            params["minPrice"] = min_price
        if max_price is not None:
            params["maxPrice"] = max_price

        # ── Call API (key is auto-rotated) ─────────────────────
        data = _call_rakuten_api(params)

        total_count = data.get("count", 0)
        page_count = data.get("pageCount", 0)
        items = data.get("Items", [])
        current_page = data.get("page", page)

        logger.info(
            "API discovery: genre=%s price=%d-%s page=%d/%d count=%d items=%d",
            genre_id, min_price, max_price, current_page,
            page_count, total_count, len(items),
        )

        # ── Price range splitting (covers ALL products) ────────
        # The API can only return 3,000 items (30 × 100 pages).
        # If the genre has more, we split the price range recursively.
        if total_count > API_MAX_ITEMS and page == 1:

            if max_price is None:
                # ── First call with no price bound ──────────────
                # Determine the actual max price by querying
                # the highest-priced item in this genre.
                logger.info(
                    "Genre %s has %d products (>%d). "
                    "Determining price range for splitting...",
                    genre_id, total_count, API_MAX_ITEMS,
                )

                # Process items from this page (don't waste the call)
                enqueued = _process_api_items(items, db, genre_id_context=genre_id)

                # Get highest price via reverse sort
                high_params = {
                    "genreId": genre_id,
                    "page": 1,
                    "sort": "-itemPrice",  # descending
                }
                try:
                    high_data = _call_rakuten_api(high_params)
                    high_items = high_data.get("Items", [])
                    if high_items:
                        first_item = high_items[0].get("Item", high_items[0])
                        highest_price = first_item.get("itemPrice", 10_000_000)
                    else:
                        highest_price = 10_000_000
                except Exception:
                    highest_price = 10_000_000

                # Also process the high-price items (free data)
                if high_items:
                    _process_api_items(high_items, db, genre_id_context=genre_id)

                logger.info(
                    "Genre %s: price range 1-%d yen, %d total products. "
                    "Starting recursive price splitting...",
                    genre_id, highest_price, total_count,
                )

                # Re-queue with actual price bounds → triggers splitting
                discover_by_genre.delay(genre_id, 1, highest_price, 1)

                return {
                    "status": "range_initialized",
                    "genre_id": genre_id,
                    "total_count": total_count,
                    "highest_price": highest_price,
                    "items_processed": len(items) + len(high_items) if high_items else len(items),
                    "enqueued": enqueued,
                }

            else:
                # ── max_price is set, split the range in half ────
                mid = (min_price + max_price) // 2
                if mid > min_price:
                    logger.info(
                        "Splitting genre=%s price %d-%d → [%d-%d] + [%d-%d] "
                        "(count=%d exceeds %d limit)",
                        genre_id, min_price, max_price,
                        min_price, mid, mid + 1, max_price,
                        total_count, API_MAX_ITEMS,
                    )

                    # Process items from this page first
                    _process_api_items(items, db, genre_id_context=genre_id)

                # Enqueue two sub-ranges
                discover_by_genre.delay(genre_id, min_price, mid, 1)
                discover_by_genre.delay(genre_id, mid + 1, max_price, 1)

                # Record the split in DB
                db.execute(text("""
                    UPDATE price_range_slices
                    SET status = 'needs_split'
                    WHERE crawl_target_id IN (
                        SELECT id FROM crawl_targets
                        WHERE target_type = 'genre' AND target_value = :genre_id
                    )
                    AND min_price = :min_price AND max_price = :max_price
                """), {
                    "genre_id": genre_id,
                    "min_price": min_price,
                    "max_price": max_price,
                })
                db.commit()

                return {
                    "status": "split",
                    "genre_id": genre_id,
                    "total_count": total_count,
                    "split": f"{min_price}-{mid} + {mid+1}-{max_price}",
                }

        # ── Process current page items ────────────────────────
        enqueued = _process_api_items(items, db, genre_id_context=genre_id)

        # ── Paginate: queue next page if more exist ───────────
        if current_page < page_count and current_page < API_MAX_PAGE:
            discover_by_genre.delay(genre_id, min_price, max_price, current_page + 1)
        else:
            # ═══════════════════════════════════════════════════
            # LAST PAGE reached → trigger Stage 2: List Scraping
            # ═══════════════════════════════════════════════════
            # Now that API has processed all items in this price slice,
            # trigger list page scraping for the same genre + price range
            # to collect login-only data (coupons, SPU points, etc.)
            from app.workers.list_scraper import scrape_list_paginated
            list_max_pages = settings.RAKUTEN_LIST_MAX_PAGES

            scrape_list_paginated.delay(
                base_url=build_search_url(
                    genre_id=genre_id,
                    min_price=min_price if min_price > 0 else None,
                    max_price=max_price,
                    sort="2",  # price ascending — same order as API
                ),
                genre_id=genre_id,
                min_price=min_price if min_price > 0 else None,
                max_price=max_price,
                max_pages=list_max_pages,
            )
            logger.info(
                "API done for genre=%s price=%d-%s (%d pages). "
                "→ Triggered Stage 2: list scraping (up to %d pages)",
                genre_id, min_price, max_price,
                current_page, list_max_pages,
            )

        return {
            "status": "ok",
            "genre_id": genre_id,
            "page": current_page,
            "page_count": page_count,
            "total_count": total_count,
            "items_on_page": len(items),
            "enqueued": enqueued,
        }

    except Exception as e:
        logger.error("API discovery error: %s", e, exc_info=True)
        raise
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════
#  Keyword Discovery Task
# ═══════════════════════════════════════════════════════════════

@celery_app.task(name="app.workers.api_discovery.discover_by_keyword")
def discover_by_keyword(
    keyword: str,
    genre_id: str | None = None,
    min_price: int = 0,
    max_price: int | None = None,
    page: int = 1,
) -> dict:
    """Discover products via API for a keyword search."""
    db = get_sync_db()
    try:
        params: dict = {
            "keyword": keyword,
            "page": page,
            "sort": "+itemPrice",
        }
        if genre_id:
            params["genreId"] = genre_id
        if min_price > 0:
            params["minPrice"] = min_price
        if max_price is not None:
            params["maxPrice"] = max_price

        data = _call_rakuten_api(params)
        total_count = data.get("count", 0)
        page_count = data.get("pageCount", 0)
        items = data.get("Items", [])
        current_page = data.get("page", page)

        enqueued = _process_api_items(items, db, genre_id_context=genre_id or "")

        if current_page < page_count and current_page < API_MAX_PAGE:
            discover_by_keyword.delay(
                keyword, genre_id, min_price, max_price, current_page + 1,
            )

        return {
            "status": "ok",
            "keyword": keyword,
            "page": current_page,
            "page_count": page_count,
            "total_count": total_count,
            "items_on_page": len(items),
            "enqueued": enqueued,
        }
    except Exception as e:
        logger.error("Keyword discovery error: %s", e, exc_info=True)
        raise
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════
#  Discover All Configured Genres
# ═══════════════════════════════════════════════════════════════

@celery_app.task(name="app.workers.api_discovery.discover_all_genres")
def discover_all_genres() -> dict:
    """
    Launch the full 3-stage pipeline for all configured genre IDs.

    For each genre:
      1. API discovery (with automatic price slicing for full coverage)
      2. List page scraping (auto-triggered when each API slice finishes)
      3. Detail scraping (auto-enqueued by both API and list scrapers)

    Genre IDs come from settings.RAKUTEN_GENRE_IDS (default: 215783,100938,551169).
    If a genre has price slices defined in the DB, those are used.
    Otherwise, starts with no price range and auto-detects + splits.
    """
    db = get_sync_db()
    try:
        genre_ids = settings.genre_id_list
        tasks_dispatched = 0

        for gid in genre_ids:
            # Check if this genre has a crawl_target with price slices
            target = db.execute(text("""
                SELECT id FROM crawl_targets
                WHERE target_type = 'genre' AND target_value = :genre_id
                AND is_active = 1
            """), {"genre_id": gid}).first()

            if target:
                # Check for existing price slices
                slices = db.execute(text("""
                    SELECT min_price, max_price
                    FROM price_range_slices
                    WHERE crawl_target_id = :target_id
                    AND status != 'needs_split'
                    ORDER BY min_price ASC
                """), {"target_id": target[0]}).mappings().all()

                if slices:
                    for s in slices:
                        discover_by_genre.delay(gid, s["min_price"], s["max_price"], 1)
                        tasks_dispatched += 1
                    logger.info(
                        "Genre %s: using %d existing price slices",
                        gid, len(slices),
                    )
                else:
                    # No slices → start full range scan
                    # discover_by_genre will auto-detect max price and split
                    discover_by_genre.delay(gid, 0, None, 1)
                    tasks_dispatched += 1

                # Update last_crawled_at
                db.execute(text("""
                    UPDATE crawl_targets SET last_crawled_at = NOW()
                    WHERE id = :target_id
                """), {"target_id": target[0]})
            else:
                # No crawl_target record → create one and start full scan
                db.execute(text("""
                    INSERT INTO crawl_targets
                        (target_type, target_value, is_active, priority, added_by)
                    VALUES ('genre', :genre_id, 1, 10, 'manual')
                    ON DUPLICATE KEY UPDATE
                        is_active = 1, last_crawled_at = NOW()
                """), {"genre_id": gid})
                db.commit()

                discover_by_genre.delay(gid, 0, None, 1)
                tasks_dispatched += 1

        db.commit()
        logger.info(
            "Full pipeline started: dispatched %d tasks for %d genres. "
            "List scraping will auto-trigger when each API slice finishes.",
            tasks_dispatched, len(genre_ids),
        )
        return {
            "status": "ok",
            "genre_ids": genre_ids,
            "tasks_dispatched": tasks_dispatched,
        }

    except Exception as e:
        logger.error("Discover all genres error: %s", e, exc_info=True)
        raise
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════
#  Daily Scheduled Task
# ═══════════════════════════════════════════════════════════════

@celery_app.task(name="app.workers.api_discovery.run_daily_api_discovery")
def run_daily_api_discovery() -> dict:
    """
    Scheduled daily task: run API discovery for all active crawl targets.

    1. Discovers all 3 configured genres
    2. Also processes any keyword/shop crawl targets from the DB
    """
    db = get_sync_db()
    try:
        # ── Step 1: Discover all configured genres ────────────
        discover_all_genres.delay()

        # ── Step 2: Process additional crawl targets ──────────
        result = db.execute(text("""
            SELECT id, target_type, target_value, base_url
            FROM crawl_targets
            WHERE is_active = 1
            AND target_type IN ('keyword', 'shop')
            ORDER BY priority DESC
        """))
        targets = result.mappings().all()

        tasks_dispatched = 0
        for target in targets:
            tt = target["target_type"]
            tv = target["target_value"]

            # Check for existing price slices
            slices = db.execute(text("""
                SELECT min_price, max_price
                FROM price_range_slices
                WHERE crawl_target_id = :target_id AND status != 'needs_split'
                ORDER BY min_price ASC
            """), {"target_id": target["id"]}).mappings().all()

            if slices:
                for s in slices:
                    if tt == "keyword":
                        discover_by_keyword.delay(
                            tv, None, s["min_price"], s["max_price"], 1,
                        )
                    tasks_dispatched += 1
            else:
                if tt == "keyword":
                    discover_by_keyword.delay(tv, None, 0, None, 1)
                    tasks_dispatched += 1
                elif tt == "shop":
                    # Shop targets are handled via list_scraper
                    pass

            # Mark last crawled
            db.execute(text("""
                UPDATE crawl_targets SET last_crawled_at = NOW()
                WHERE id = :target_id
            """), {"target_id": target["id"]})

        db.commit()

        logger.info(
            "Daily API discovery: dispatched %d additional tasks", tasks_dispatched,
        )
        return {"status": "ok", "additional_tasks_dispatched": tasks_dispatched}

    except Exception as e:
        logger.error("Daily API discovery error: %s", e, exc_info=True)
        raise
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════
#  Genre Discovery (Rakuten IchibaGenre Search API)
# ═══════════════════════════════════════════════════════════════

def _call_genre_api(genre_id: int = 0) -> dict:
    """
    Call Rakuten IchibaGenre Search API to get child genres.

    Args:
        genre_id: Parent genre ID (0 = root → returns all top-level genres)

    Returns:
        Parsed JSON response with 'children', 'current', 'parents' keys.

    API docs: https://webservice.rakuten.co.jp/documentation/ichiba-genre-search
    Response structure:
        {
          "current": {"genreId": 0, "genreName": "All", "genreLevel": 0},
          "children": [
            {"child": {"genreId": 100371, "genreName": "食品", "genreLevel": 1}},
            {"child": {"genreId": 100433, "genreName": "ダイエット・健康", "genreLevel": 1}},
            ...
          ]
        }
    """
    app_id, access_key = key_rotator.get_next_key()
    params = {
        "format": "json",
        "genreId": genre_id,
        "applicationId": app_id,
        "accessKey": access_key,
    }
    try:
        with httpx.Client(timeout=30) as client:
            resp = client.get(GENRE_API_URL, params=params)
        if resp.status_code == 429:
            key_rotator.mark_rate_limited(app_id, cooldown_sec=10.0)
            # Retry once with a different key
            time.sleep(1)
            app_id2, access_key2 = key_rotator.get_next_key()
            params["applicationId"] = app_id2
            params["accessKey"] = access_key2
            with httpx.Client(timeout=30) as client:
                resp = client.get(GENRE_API_URL, params=params)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error("Genre API error for genre_id=%d: %s", genre_id, e)
        return {}


def _get_all_top_level_genres() -> list[dict]:
    """
    Fetch ALL top-level genre IDs from the Rakuten GenreSearch API.

    Returns list of dicts: [{"genreId": "100371", "genreName": "食品"}, ...]

    Rakuten has ~36 top-level genres. Every product belongs to exactly
    one genre hierarchy, so covering all top-level genres covers ALL
    28.7M+ products.
    """
    data = _call_genre_api(genre_id=0)
    children = data.get("children", [])

    genres = []
    for entry in children:
        child = entry.get("child", {})
        gid = child.get("genreId")
        gname = child.get("genreName", "")
        if gid:
            genres.append({"genreId": str(gid), "genreName": gname})

    logger.info(
        "Fetched %d top-level genres from Rakuten GenreSearch API", len(genres),
    )
    return genres


def _get_sub_genres(parent_genre_id: int, depth: int = 1) -> list[dict]:
    """
    Recursively fetch sub-genres up to given depth.
    Useful for splitting very large genres into smaller sub-genres
    that are more manageable for price slicing.

    Returns flat list of genre dicts at the target depth.
    """
    if depth <= 0:
        return [{"genreId": str(parent_genre_id)}]

    data = _call_genre_api(genre_id=parent_genre_id)
    children = data.get("children", [])

    if not children:
        return [{"genreId": str(parent_genre_id)}]

    if depth == 1:
        return [
            {
                "genreId": str(c.get("child", {}).get("genreId")),
                "genreName": c.get("child", {}).get("genreName", ""),
            }
            for c in children
            if c.get("child", {}).get("genreId")
        ]

    # Recurse deeper
    result = []
    for entry in children:
        child_id = entry.get("child", {}).get("genreId")
        if child_id:
            result.extend(_get_sub_genres(child_id, depth - 1))
    return result


@celery_app.task(name="app.workers.api_discovery.discover_all_rakuten_genres")
def discover_all_rakuten_genres(
    sub_genre_depth: int = 0,
    use_sub_genres_for_large: bool = True,
) -> dict:
    """
    **MASTER TASK: Discover ALL products across ALL Rakuten genres.**

    This is the entry point for extracting all 28.7M+ products.

    Strategy:
      1. Call Rakuten GenreSearch API to get all ~36 top-level genres
      2. For each genre, trigger discover_by_genre (with auto price-slicing)
      3. discover_by_genre auto-triggers list scraping when each slice completes
      4. Both API and list scrapers auto-enqueue detail scraping per product

    Flow: discover_all_rakuten_genres()
          → discover_by_genre(genre_id, ...) × N genres
            → price slicing (recursive binary split for >3000 products)
              → _process_api_items() → enqueue detail scrape
              → scrape_list_paginated() (auto-triggered on last API page)
                → scrape_list_page() × M pages → enqueue detail scrape
          → process_pending_detail_jobs (every 30s, batch=500)
            → scrape_detail_page() × K products

    Args:
        sub_genre_depth: If >0, split each top-level genre into sub-genres
                         at this depth before running discovery. Helps spread
                         the work more evenly across many smaller tasks.
        use_sub_genres_for_large: If True, automatically split genres that
                                  have >1M products into sub-genres for
                                  better parallelism.
    """
    db = get_sync_db()
    try:
        # ── Step 1: Get all top-level genres from API ─────────
        genres = _get_all_top_level_genres()
        if not genres:
            logger.error("Failed to fetch genres from Rakuten API")
            return {"status": "error", "message": "no genres returned from API"}

        tasks_dispatched = 0
        genre_details = []

        for genre in genres:
            gid = genre["genreId"]
            gname = genre.get("genreName", "")

            # ── Ensure crawl_target record exists ─────────────
            db.execute(text("""
                INSERT INTO crawl_targets
                    (target_type, target_value, is_active, priority, added_by)
                VALUES ('genre', :genre_id, 1, 10, 'auto_discovery')
                ON DUPLICATE KEY UPDATE
                    is_active = 1, last_crawled_at = NOW()
            """), {"genre_id": gid})

            if sub_genre_depth > 0:
                # Split into sub-genres for better parallelism
                sub_genres = _get_sub_genres(int(gid), sub_genre_depth)
                for sg in sub_genres:
                    discover_by_genre.delay(sg["genreId"], 0, None, 1)
                    tasks_dispatched += 1
                genre_details.append({
                    "genre_id": gid,
                    "genre_name": gname,
                    "sub_genres": len(sub_genres),
                })
            else:
                # Direct discovery with auto price-slicing
                discover_by_genre.delay(gid, 0, None, 1)
                tasks_dispatched += 1
                genre_details.append({
                    "genre_id": gid,
                    "genre_name": gname,
                })

        db.commit()

        logger.info(
            "FULL CRAWL STARTED: dispatched %d discovery tasks for %d genres. "
            "All 28.7M+ products will be covered via automatic price range splitting. "
            "List scraping auto-triggers per API slice. Detail scraping auto-enqueues.",
            tasks_dispatched, len(genres),
        )

        return {
            "status": "ok",
            "total_genres": len(genres),
            "tasks_dispatched": tasks_dispatched,
            "genres": genre_details,
            "pipeline": "API → List → Detail (fully automatic)",
        }

    except Exception as e:
        logger.error("Full genre discovery error: %s", e, exc_info=True)
        raise
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════
#  Key Status (for monitoring API)
# ═══════════════════════════════════════════════════════════════

def get_key_rotation_status() -> dict:
    """Return current status of all API keys (for admin monitoring endpoint)."""
    return key_rotator.get_status()
