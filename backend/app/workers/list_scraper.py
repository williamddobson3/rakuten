"""
List Scraper Worker.

Step 2 of the 3-stage pipeline:
  Scrapes Rakuten search result pages (一覧ページ) to discover product URLs
  and collect supplementary information.

Data extraction strategy (two layers):
  1. PRIMARY: Parse embedded JSON from window.__INITIAL_STATE__
     Path: state.*.ichibaSearch.items[]
     Contains structured: price, point (with multiplier breakdown),
     coupons (discount + type), shipping, review, shop info, SKU, tags
  2. FALLBACK: Parse HTML product cards (.dui-card.searchresultitem)
     Uses data-* attributes and visible text elements

Handles:
- Genre pages:   https://search.rakuten.co.jp/search/mall/-/{genre_id}/?p=N
- Shop pages:    https://search.rakuten.co.jp/search/mall/?sid={shop_code}&p=N
- Keyword pages: https://search.rakuten.co.jp/search/mall/{keyword}/{genre_id}/?p=N
- Price slicing: ?min=X&max=Y to stay within 150-page limit

Per-item extraction:
  - Product name, subtitle (catchcopy)
  - Price (numeric)
  - Points: count, baseMultiplier, shopMultiplier, pointUpMultiplier, etc.
  - Coupons: discount amount, type (exact yen / percentage)
  - Shipping: cost (0=free), delivery estimate text, delivery days
  - Review: score, count
  - Shop: code, name, urlCode
  - SKU/Variant info: variantId, hasMultiSku, skuAttributes
  - Flags: superDeal, shop39, soldOut, PR
  - Image URL, genre IDs
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import text

from app.celery_app import celery_app
from app.config import settings
from app.db.session import get_sync_db
from app.services.item_service import upsert_item_sync, upsert_shop_sync
from app.services.url_canonicalizer import (
    build_search_url,
    canonicalize_item_url,
    extract_shop_item_code,
    make_item_id_str,
)
from app.workers.playwright_pool import (
    get_browser_context as _pool_get_context,
    force_refresh_cookies as _pool_force_refresh,
    inject_cookies as _pool_inject_cookies,
    is_authenticated as _pool_is_authenticated,
)

logger = logging.getLogger(__name__)

MAX_PAGES = settings.RAKUTEN_LIST_MAX_PAGES
ITEMS_PER_PAGE = 45  # Rakuten list pages show 45 items

_CONTEXT_NAME = "list_scraper"

# Thread lock for Playwright — only ONE thread may use Playwright at a time.
_pw_thread_lock = threading.Lock()


# ═══════════════════════════════════════════════════════════════
#  Playwright Browser Context (shared via playwright_pool)
# ═══════════════════════════════════════════════════════════════


def _get_browser_context():
    """Get the shared Playwright browser context for list scraping."""
    return _pool_get_context(_CONTEXT_NAME)


def force_refresh_cookies() -> bool:
    """Force immediate cookie refresh in the list scraper context."""
    return _pool_force_refresh(_CONTEXT_NAME)


# ═══════════════════════════════════════════════════════════════
#  HTTP Fetch (Playwright-based — bypasses Rakuten WAF)
# ═══════════════════════════════════════════════════════════════

def _get_proxy() -> dict | None:
    """Get a proxy from the pool if configured."""
    if not settings.PROXY_POOL_URL:
        return None
    try:
        with httpx.Client(timeout=5) as client:
            resp = client.get(settings.PROXY_POOL_URL)
            proxy_url = resp.text.strip()
            if proxy_url:
                return {"http://": proxy_url, "https://": proxy_url}
    except Exception:
        pass
    return None


def _get_auth_cookies() -> dict[str, str]:
    """Load Rakuten auth cookies for authenticated scraping."""
    try:
        from app.services.rakuten_auth import get_auth_cookie_dict
        return get_auth_cookie_dict()
    except Exception as e:
        logger.warning("Failed to load auth cookies: %s", e)
        return {}


_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}

# Shared httpx client for connection pooling (reused across calls)
_httpx_client: httpx.Client | None = None


def _get_httpx_client() -> httpx.Client:
    """Get or create a shared httpx client with connection pooling."""
    global _httpx_client
    if _httpx_client is None or _httpx_client.is_closed:
        cookies_dict = _get_auth_cookies()
        # Note: http2 requires 'h2' package; use http1.1 which works fine for HTML
        _httpx_client = httpx.Client(
            headers=_BROWSER_HEADERS,
            cookies=cookies_dict,
            timeout=20,
            follow_redirects=True,
        )
    return _httpx_client


def _is_waf_blocked(html: str) -> bool:
    """Check if the response is a WAF/bot challenge page."""
    if len(html) < 1000:
        return True
    # Rakuten list pages always contain __INITIAL_STATE__ or search result markers
    if "__INITIAL_STATE__" in html:
        return False
    if "searchResultItems" in html.lower() or "item-list" in html.lower():
        return False
    waf_signals = [
        "Access Denied",
        "cf-browser-verification",
        "challenge-platform",
        "_sec_cpt",
        "Just a moment",
    ]
    html_lower = html[:5000].lower()
    return any(sig.lower() in html_lower for sig in waf_signals)


def _fetch_page(url: str) -> str | None:
    """
    Fetch a Rakuten search page — fast httpx first, Playwright fallback.

    Strategy:
      1. Try httpx with browser headers + auth cookies (0.1-0.3s)
      2. If WAF blocked or failed → fall back to Playwright (2-5s)

    This is 10-50x faster than always using Playwright.
    """
    # ── Fast path: httpx ──────────────────────────────────────
    try:
        client = _get_httpx_client()
        resp = client.get(url)

        if resp.status_code == 200:
            html = resp.text
            if not _is_waf_blocked(html):
                return html
            logger.debug("WAF detected on %s via httpx, falling back to Playwright", url)
        elif resp.status_code == 404:
            logger.warning("Page not found: %s", url)
            return None
        elif resp.status_code == 429:
            logger.warning("Rate limited (httpx) on %s", url)
            # Fall through to Playwright
        else:
            logger.debug("httpx got status %d for %s, trying Playwright", resp.status_code, url)
    except Exception as e:
        logger.debug("httpx failed for %s: %s, trying Playwright", url, e)

    # ── Slow path: Playwright (WAF bypass) ────────────────────
    # Playwright sync API is single-threaded — serialize access across all threads.
    with _pw_thread_lock:
        for attempt in range(3):
            try:
                context = _get_browser_context()
                page = context.new_page()

                try:
                    resp = page.goto(url, wait_until="domcontentloaded", timeout=30000)

                    if resp and resp.status == 429:
                        wait = 3 * (attempt + 1)
                        logger.warning("Rate limited on %s, waiting %ds", url, wait)
                        time.sleep(wait)
                        continue

                    if resp and resp.status == 404:
                        logger.warning("Page not found: %s", url)
                        return None

                    html = page.content()

                    if len(html) < 1000:
                        logger.warning(
                            "Short response (%d bytes) for %s, retrying",
                            len(html), url,
                        )
                        time.sleep(1)
                        continue

                    return html

                finally:
                    page.close()

            except Exception as e:
                logger.error("Playwright error (attempt %d) for %s: %s", attempt + 1, url, e)
                if attempt < 2:
                    time.sleep(1)

    return None


# ═══════════════════════════════════════════════════════════════
#  Embedded JSON Parser (Primary — more reliable)
# ═══════════════════════════════════════════════════════════════

def _extract_initial_state(html: str) -> dict | None:
    """
    Extract window.__INITIAL_STATE__ JSON from page HTML.

    The page embeds a massive JSON object containing all product data:
      <script>
        window.__INITIAL_STATE__ = { "state": { ... } };
      </script>

    Returns the parsed dict or None on failure.
    """
    # Match: window.__INITIAL_STATE__ = {...}
    match = re.search(
        r"window\.__INITIAL_STATE__\s*=\s*(\{.+?\})\s*;?\s*(?:</script>|window\.)",
        html,
        re.DOTALL,
    )
    if not match:
        return None

    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError as e:
        logger.warning("Failed to parse __INITIAL_STATE__ JSON: %s", e)
        return None


def _find_ichiba_items(state: dict) -> tuple[list[dict], dict | None]:
    """
    Navigate the __INITIAL_STATE__ structure to find ichibaSearch.items[].

    The path varies but is typically:
      state.state.<component_name>.ichibaSearch.items

    Returns:
        (items_list, pagination_dict)
    """
    root_state = state.get("state", state)

    # Search through all top-level keys for ichibaSearch
    for key, value in root_state.items():
        if not isinstance(value, dict):
            continue

        ichiba = value.get("ichibaSearch")
        if ichiba and isinstance(ichiba, dict):
            items = ichiba.get("items", [])
            pagination = ichiba.get("pagination")
            if isinstance(items, list) and len(items) > 0:
                return items, pagination

        # Sometimes nested one more level
        for sub_key, sub_value in value.items():
            if isinstance(sub_value, dict):
                ichiba = sub_value.get("ichibaSearch")
                if ichiba and isinstance(ichiba, dict):
                    items = ichiba.get("items", [])
                    pagination = ichiba.get("pagination")
                    if isinstance(items, list) and len(items) > 0:
                        return items, pagination

    return [], None


def _parse_json_item(item: dict) -> dict[str, Any]:
    """
    Parse a single item from the ichibaSearch.items[] JSON structure.

    Real structure example:
    {
        "name": "靴 メンズ スリッポン...",
        "price": 6270,
        "subtitle": "EDWIN エドウィン...",
        "url": "https://...",
        "originalItemUrl": "https://item.rakuten.co.jp/moriashizakka/edm544/",
        "code": "10000395",
        "images": [{"url": "https://..."}],
        "genreIdList": "/0/558885/110983/558926",
        "isSoldOut": false,
        "itemOptions": {"superDeal": false, "shop39": true, ...},
        "shop": {"name": "moriashizakka", "id": 380959, "code": "380959", "urlCode": "moriashizakka"},
        "shipping": {"price": 0, "estimateDeliveryDay": "2/24 12:00までの注文で最短2/25お届け", "deliveryDays": null},
        "point": {"count": 285, "baseMultiplier": 1, "shopMultiplier": 5, "pointUpMultiplier": 4, ...},
        "review": {"score": 4.65, "numReviews": 49},
        "coupons": [{"discount": 15, "discountType": "percentage"}],
        "variantId": "12054",
        "skuInfo": {"hasMultiSku": true, "priceRange": "6270"},
        "tags": [{"id": 1000909, "name": "エドウイン", "tag_group": {"name": "ブランド"}}],
        "genres": [{"id": 110983, "name": "メンズ靴"}],
        ...
    }
    """
    result: dict[str, Any] = {}

    # ── Basic info ─────────────────────────────────────────
    result["item_name"] = item.get("name")
    result["item_subtitle"] = item.get("subtitle")
    result["price"] = item.get("price")
    result["item_code"] = item.get("code")
    result["variant_id"] = item.get("variantId")
    result["is_sold_out"] = bool(item.get("isSoldOut", False))

    # ── Item URL ──────────────────────────────────────────
    # Prefer originalItemUrl (clean) over url (tracking-wrapped)
    result["item_url"] = item.get("originalItemUrl") or item.get("url", "")

    # ── Image ──────────────────────────────────────────────
    images = item.get("images", [])
    if images and isinstance(images, list):
        first_img = images[0]
        if isinstance(first_img, dict):
            result["image_url"] = first_img.get("url")
        elif isinstance(first_img, str):
            result["image_url"] = first_img
    else:
        result["image_url"] = None

    # ── Genre IDs ──────────────────────────────────────────
    genre_id_list = item.get("genreIdList", "")
    result["genre_ids"] = genre_id_list

    # Extract leaf genre from genres array
    genres = item.get("genres", [])
    if genres and isinstance(genres, list):
        leaf_genre = genres[-1] if genres else None
        if leaf_genre and isinstance(leaf_genre, dict):
            result["leaf_genre_id"] = leaf_genre.get("id")
            result["leaf_genre_name"] = leaf_genre.get("name")

    # ── Shop info ──────────────────────────────────────────
    shop = item.get("shop", {})
    if isinstance(shop, dict):
        result["shop_id"] = shop.get("id")
        result["shop_code"] = str(shop.get("code", ""))
        result["shop_name"] = shop.get("name")
        result["shop_url_code"] = shop.get("urlCode")
    else:
        result["shop_code"] = ""
        result["shop_name"] = None
        result["shop_url_code"] = None

    # ── Point data (structured breakdown) ───────────────────
    # Rakuten list page JSON has two point systems:
    #   1. Multiplier-based (ショップポイント): baseMultiplier, shopMultiplier, etc.
    #      e.g. "85ポイント(1倍)" or "780ポイント(1倍+9倍UP)"
    #   2. Point-back (ポイントバック): pointBackRate / pointBackPercent
    #      e.g. "2,520ポイント(20%ポイントバック)"
    point = item.get("point", {})
    if isinstance(point, dict):
        result["point_count"] = point.get("count")
        result["point_base_multiplier"] = point.get("baseMultiplier")
        result["point_shop_multiplier"] = point.get("shopMultiplier")
        result["point_up_multiplier"] = point.get("pointUpMultiplier")
        result["point_deal_multiplier"] = point.get("dealMultiplier")
        result["point_item_multiplier"] = point.get("itemMultiplier")

        # Point-back fields (ポイントバック): percentage-based system
        result["is_point_back"] = bool(point.get("isPointBack", False))
        result["point_back_rate"] = (
            point.get("pointBackRate")
            or point.get("pointBackPercent")
            or point.get("pointBack")
        )

        # Compute total multiplier for snapshot compatibility
        # Total = base + shop + up + deal + item
        base = point.get("baseMultiplier") or 0
        shop = point.get("shopMultiplier") or 0
        up = point.get("pointUpMultiplier") or 0
        deal = point.get("dealMultiplier") or 0
        item_m = point.get("itemMultiplier") or 0
        total_multiplier = base + shop + up + deal + item_m
        result["point_total_multiplier"] = total_multiplier if total_multiplier > 0 else None
    else:
        result["point_count"] = None
        result["is_point_back"] = False
        result["point_back_rate"] = None
        result["point_total_multiplier"] = None

    # ── Coupon data (first/best coupon) ────────────────────
    coupons = item.get("coupons", [])
    if coupons and isinstance(coupons, list):
        # Take the first (usually highest) coupon
        best_coupon = coupons[0]
        if isinstance(best_coupon, dict):
            result["coupon_discount"] = best_coupon.get("discount")
            result["coupon_type"] = best_coupon.get("discountType")  # "exact" or "percentage"
    else:
        result["coupon_discount"] = None
        result["coupon_type"] = None

    # ── Shipping data ──────────────────────────────────────
    shipping = item.get("shipping", {})
    if isinstance(shipping, dict):
        result["shipping_cost"] = shipping.get("price")
        result["delivery_text"] = shipping.get("estimateDeliveryDay")
        result["delivery_days"] = shipping.get("deliveryDays")
        result["is_free_shipping"] = (shipping.get("price", 1) == 0)
    else:
        result["shipping_cost"] = None
        result["delivery_text"] = None
        result["is_free_shipping"] = False

    # ── Review data ────────────────────────────────────────
    review = item.get("review", {})
    if isinstance(review, dict) and review:
        result["review_score"] = review.get("score")
        result["review_count"] = review.get("numReviews")
    else:
        result["review_score"] = None
        result["review_count"] = None

    # ── Item options / flags ───────────────────────────────
    options = item.get("itemOptions", {})
    if isinstance(options, dict):
        result["is_super_deal"] = bool(options.get("superDeal", False))
        result["is_shop39"] = bool(options.get("shop39", False))
        result["is_pr"] = bool(options.get("cpc") and options["cpc"].get("type"))
    else:
        result["is_super_deal"] = False
        result["is_shop39"] = False
        result["is_pr"] = False

    # ── SKU info ───────────────────────────────────────────
    sku_info = item.get("skuInfo", {})
    if isinstance(sku_info, dict):
        result["has_multi_sku"] = bool(sku_info.get("hasMultiSku", False))
        result["price_range"] = sku_info.get("priceRange")
    else:
        result["has_multi_sku"] = False
        result["price_range"] = None

    # ── Data source ────────────────────────────────────────
    result["data_source"] = "json"

    return result


def _parse_search_results_from_json(html: str) -> tuple[list[dict], dict | None]:
    """
    Extract product data from embedded __INITIAL_STATE__ JSON.

    Returns:
        (list of parsed item dicts, pagination info dict or None)
    """
    state = _extract_initial_state(html)
    if not state:
        return [], None

    raw_items, pagination = _find_ichiba_items(state)
    if not raw_items:
        return [], pagination

    parsed = []
    for item in raw_items:
        try:
            parsed_item = _parse_json_item(item)
            if parsed_item.get("item_url"):
                parsed.append(parsed_item)
        except Exception as e:
            logger.warning("Error parsing JSON item: %s", e)
            continue

    logger.info(
        "JSON extraction: %d/%d items parsed from __INITIAL_STATE__",
        len(parsed), len(raw_items),
    )
    return parsed, pagination


# ═══════════════════════════════════════════════════════════════
#  HTML Parser (Fallback)
# ═══════════════════════════════════════════════════════════════

def _parse_search_results_from_html(html: str) -> list[dict]:
    """
    Parse product cards from HTML as fallback.

    Uses the real Rakuten HTML structure:
      <div class="dui-card searchresultitem" data-track-price="8580" ...>
        <a href="https://item.rakuten.co.jp/...">
        <div class="price--3zUvK">8,580<span>円</span></div>
        <span class="free-shipping-label--1shop">送料無料</span>
        <div class="points--DNEud">780ポイント(1倍+9倍UP)</div>
        <div class="coupon">200円OFFクーポンあり</div>
        <div class="shipping">12:00までの注文で最短2/22(翌日)お届け</div>
        <span class="score">4.75</span><span class="legend">(169件)</span>
        <a data-rpp-url-copy="shop">スニークオンラインショップ</a>
      </div>
    """
    soup = BeautifulSoup(html, "lxml")
    results: list[dict] = []

    cards = soup.select(".dui-card.searchresultitem")
    if not cards:
        # Try broader selector
        cards = soup.select("[data-card-type='item']")

    for card in cards:
        result: dict[str, Any] = {}

        # ── Item URL from image link (clean, no tracking) ────
        img_link = card.select_one("a.image-link-wrapper--3XCNg, a[class*='image-link']")
        if img_link:
            result["item_url"] = img_link.get("href", "")
        else:
            # Fall back to any item.rakuten.co.jp link
            any_link = card.select_one("a[href*='item.rakuten.co.jp']")
            if any_link:
                result["item_url"] = any_link.get("href", "")
            else:
                continue  # Skip card without item URL

        # ── Price from data attribute (most reliable) ─────────
        price_str = card.get("data-track-price")
        if price_str and price_str.isdigit():
            result["price"] = int(price_str)
        else:
            # Parse from visible text
            price_el = card.select_one("[class*='price--']")
            if price_el:
                price_text = re.sub(r"[^\d]", "", price_el.get_text())
                result["price"] = int(price_text) if price_text else None
            else:
                result["price"] = None

        result["price_range"] = card.get("data-track-price-ranges")

        # ── Item name ──────────────────────────────────────────
        title_link = card.select_one("[class*='title-link--']")
        result["item_name"] = title_link.get("title") or title_link.get_text(strip=True) if title_link else None

        # ── Image URL ──────────────────────────────────────────
        img = card.select_one("img[class*='image--']")
        result["image_url"] = img.get("src") if img else None

        # ── Shipping ──────────────────────────────────────────
        free_ship = card.select_one("[class*='free-shipping']")
        result["is_free_shipping"] = bool(free_ship)
        result["shipping_cost"] = 0 if free_ship else None

        # ── Points ─────────────────────────────────────────────
        # Two Rakuten point formats:
        #   1. Shop point: "780ポイント(1倍+9倍UP)" or "85ポイント(1倍)"
        #   2. Point back: "2,520ポイント(20%ポイントバック)"
        points_el = card.select_one("[class*='points--']")
        if points_el:
            point_text = points_el.get_text(strip=True)
            result["point_text_raw"] = point_text

            # Parse point count — handle comma-separated numbers: "2,520ポイント"
            count_match = re.search(r"([\d,]+)\s*ポイント", point_text)
            if count_match:
                result["point_count"] = int(count_match.group(1).replace(",", ""))
            else:
                result["point_count"] = None

            # Check if this is point-back format: "20%ポイントバック"
            back_match = re.search(r"(\d+(?:\.\d+)?)\s*%\s*ポイントバック", point_text)
            if back_match:
                result["is_point_back"] = True
                result["point_back_rate"] = float(back_match.group(1))
                result["point_base_multiplier"] = None
                result["point_up_multiplier"] = None
                result["point_total_multiplier"] = None
            else:
                result["is_point_back"] = False
                result["point_back_rate"] = None

                # Parse multiplier format: "(1倍+9倍UP)" → base=1, up=9
                base_match = re.search(r"\((\d+)倍", point_text)
                result["point_base_multiplier"] = int(base_match.group(1)) if base_match else None

                up_match = re.search(r"\+(\d+)倍(?:UP|up|アップ)", point_text)
                result["point_up_multiplier"] = int(up_match.group(1)) if up_match else None

                # Compute total multiplier
                base_val = result["point_base_multiplier"] or 0
                up_val = result["point_up_multiplier"] or 0
                total = base_val + up_val
                result["point_total_multiplier"] = total if total > 0 else None
        else:
            result["point_text_raw"] = None
            result["point_count"] = None
            result["is_point_back"] = False
            result["point_back_rate"] = None
            result["point_total_multiplier"] = None

        # ── Coupon ─────────────────────────────────────────────
        coupon_el = card.select_one(".coupon, [class*='coupon']")
        if coupon_el:
            coupon_text = coupon_el.get_text(strip=True)
            result["coupon_text_raw"] = coupon_text

            # Parse "200円OFFクーポンあり" or "15%OFFクーポンあり"
            yen_match = re.search(r"([\d,]+)\s*円OFF", coupon_text)
            pct_match = re.search(r"(\d+)\s*%OFF", coupon_text)
            if yen_match:
                result["coupon_discount"] = int(yen_match.group(1).replace(",", ""))
                result["coupon_type"] = "exact"
            elif pct_match:
                result["coupon_discount"] = int(pct_match.group(1))
                result["coupon_type"] = "percentage"
            else:
                result["coupon_discount"] = None
                result["coupon_type"] = None
        else:
            result["coupon_text_raw"] = None
            result["coupon_discount"] = None
            result["coupon_type"] = None

        # ── Delivery ──────────────────────────────────────────
        delivery_el = card.select_one(".shipping, [class*='shipping-status'] .shipping")
        if delivery_el:
            result["delivery_text"] = delivery_el.get_text(strip=True)
        else:
            result["delivery_text"] = None

        # ── Review ─────────────────────────────────────────────
        score_el = card.select_one(".score")
        legend_el = card.select_one(".legend")
        if score_el:
            try:
                result["review_score"] = float(score_el.get_text(strip=True))
            except ValueError:
                result["review_score"] = None
        else:
            result["review_score"] = None

        if legend_el:
            count_match = re.search(r"(\d+)", legend_el.get_text())
            result["review_count"] = int(count_match.group(1)) if count_match else None
        else:
            result["review_count"] = None

        # ── Shop name ──────────────────────────────────────────
        shop_link = card.select_one("a[data-rpp-url-copy='shop']")
        if shop_link:
            result["shop_name"] = shop_link.get_text(strip=True)
            shop_href = shop_link.get("href", "")
            # Extract shop code from URL like "https://www.rakuten.co.jp/sneak/"
            shop_match = re.search(r"rakuten\.co\.jp/([^/]+)/", shop_href)
            result["shop_url_code"] = shop_match.group(1) if shop_match else None
        else:
            result["shop_name"] = None
            result["shop_url_code"] = None

        # ── Data attributes for tracking ──────────────────────
        result["shop_code"] = card.get("data-shop-id", "")
        result["variant_id"] = card.get("data-track-variantid")

        # ── Flags ──────────────────────────────────────────────
        result["is_pr"] = False  # Would need to check [PR] tag
        result["is_super_deal"] = False
        result["is_shop39"] = bool(card.select_one("[title*='39']"))
        result["is_sold_out"] = False
        result["has_multi_sku"] = bool(card.select_one("[class*='sku-attribute']"))

        result["data_source"] = "html"
        results.append(result)

    logger.info("HTML extraction: %d items parsed from cards", len(results))
    return results


# ═══════════════════════════════════════════════════════════════
#  Combined Parser
# ═══════════════════════════════════════════════════════════════

def _parse_search_results(html: str) -> tuple[list[dict], dict | None]:
    """
    Parse product data from a Rakuten search results page.

    Strategy:
      1. Try JSON extraction from __INITIAL_STATE__ (most reliable, richest data)
      2. Fall back to HTML parsing if JSON extraction fails

    Returns:
        (list of item dicts, pagination info or None)
    """
    # Try JSON first
    items, pagination = _parse_search_results_from_json(html)
    if items:
        return items, pagination

    # Fall back to HTML
    logger.info("JSON extraction failed, falling back to HTML parsing")
    html_items = _parse_search_results_from_html(html)
    return html_items, None


def _extract_price_from_text(text_str: str | None) -> int | None:
    """Extract numeric price from text like '¥1,234' or '1234円'."""
    if not text_str:
        return None
    digits = re.sub(r"[^\d]", "", text_str)
    if digits:
        return int(digits)
    return None


# ═══════════════════════════════════════════════════════════════
#  Coupon text builder (for point_text_raw from JSON data)
# ═══════════════════════════════════════════════════════════════

def _build_point_text(item: dict) -> str | None:
    """Build human-readable point text from structured JSON data.

    Handles two formats:
      1. Multiplier: "780ポイント(1倍+9倍UP)"
      2. Point back: "2,520ポイント(20%ポイントバック)"
    """
    count = item.get("point_count")
    if count is None:
        return None

    # Format count with commas for readability
    count_str = f"{count:,}"

    # Check if this is a point-back item
    if item.get("is_point_back") and item.get("point_back_rate"):
        rate = item["point_back_rate"]
        # Format rate: remove .0 if integer
        rate_str = f"{int(rate)}" if rate == int(rate) else f"{rate}"
        return f"{count_str}ポイント({rate_str}%ポイントバック)"

    # Multiplier format
    base = item.get("point_base_multiplier")
    parts = []
    if base is not None:
        parts.append(f"{base}倍")

    # Calculate total UP multiplier
    up_parts = []
    for key in ["point_shop_multiplier", "point_up_multiplier",
                "point_deal_multiplier", "point_item_multiplier"]:
        val = item.get(key)
        if val and val > 0:
            up_parts.append(val)

    total_up = sum(up_parts) if up_parts else 0
    if total_up > 0:
        parts.append(f"+{total_up}倍UP")

    multiplier_text = "".join(parts)
    return f"{count_str}ポイント({multiplier_text})" if multiplier_text else f"{count_str}ポイント"


def _build_coupon_text(item: dict) -> str | None:
    """Build human-readable coupon text from structured JSON data."""
    discount = item.get("coupon_discount")
    c_type = item.get("coupon_type")

    if discount is None:
        return None

    if c_type == "percentage":
        return f"{discount}%OFFクーポンあり"
    elif c_type == "exact":
        # Format with comma separator for large numbers
        formatted = f"{discount:,}"
        return f"{formatted}円OFFクーポンあり"
    else:
        return f"{discount}クーポンあり"


# ═══════════════════════════════════════════════════════════════
#  Cookie Refresh Task (called from API after login)
# ═══════════════════════════════════════════════════════════════

@celery_app.task(name="app.workers.list_scraper.refresh_auth_cookies")
def refresh_auth_cookies() -> dict:
    """
    Celery task to refresh auth cookies in the list scraper's browser context.
    This runs inside the Celery WORKER process, so it can modify the
    global browser context directly.

    Call this after login to ensure the scraper picks up fresh cookies.
    """
    ok = force_refresh_cookies()
    authed = _pool_is_authenticated(_CONTEXT_NAME)
    return {
        "status": "ok" if ok else "no_cookies",
        "authenticated": authed,
        "message": "Auth cookies refreshed in list scraper" if ok else
                   "No valid auth cookies found — running in guest mode",
    }


# ═══════════════════════════════════════════════════════════════
#  Main Scrape Task
# ═══════════════════════════════════════════════════════════════

@celery_app.task(
    name="app.workers.list_scraper.scrape_list_page",
    rate_limit=None,  # No per-task rate limit; concurrency controlled by worker -c flag
)
def scrape_list_page(
    url: str,
    crawl_target_id: int | None = None,
) -> dict:
    """
    Scrape a single search results page.

    Extracts product data from embedded JSON or HTML,
    upserts shops/items, stores list observations, and
    enqueues detail scrape jobs.
    """
    db = get_sync_db()
    try:
        html = _fetch_page(url)
        if not html:
            return {"status": "error", "url": url, "reason": "fetch_failed"}

        items, pagination = _parse_search_results(html)
        enqueued = 0

        for item_data in items:
            item_url = item_data.get("item_url", "")
            if not item_url:
                continue

            canonical_url, url_variant_id = canonicalize_item_url(item_url)

            try:
                shop_code_from_url, item_code = extract_shop_item_code(canonical_url)
            except ValueError:
                continue

            # Use shop_code from JSON if available, else from URL
            shop_code = item_data.get("shop_url_code") or shop_code_from_url
            shop_name = item_data.get("shop_name")
            shop_url = f"https://www.rakuten.co.jp/{shop_code}/" if shop_code else None

            # ── Upsert shop + item ──────────────────────────
            upsert_shop_sync(db, shop_code=shop_code, shop_name=shop_name, shop_url=shop_url)
            item_id = upsert_item_sync(
                db,
                shop_code=shop_code,
                item_code=item_code,
                canonical_url=canonical_url,
                item_name=item_data.get("item_name"),
                catchcopy=item_data.get("item_subtitle"),
                source="list",
            )

            # ── Build text representations from JSON data ────
            point_text_raw = item_data.get("point_text_raw") or _build_point_text(item_data)
            coupon_text_raw = item_data.get("coupon_text_raw") or _build_coupon_text(item_data)

            # ── Save list observation ────────────────────────
            db.execute(text("""
                INSERT INTO list_observations
                    (item_id, observed_price, price_range,
                     point_count, point_base_multiplier, point_shop_multiplier,
                     point_up_multiplier, point_deal_multiplier, point_item_multiplier,
                     point_total_multiplier, is_point_back, point_back_rate,
                     point_text_raw,
                     coupon_discount, coupon_type, coupon_text_raw,
                     shipping_cost, delivery_text, delivery_days, is_free_shipping,
                     review_score, review_count,
                     shop_code, shop_name, shop_url_code,
                     item_name, item_subtitle, image_url, genre_ids, variant_id,
                     has_multi_sku,
                     is_pr, is_super_deal, is_shop39, is_sold_out,
                     data_source, list_url)
                VALUES
                    (:item_id, :price, :price_range,
                     :point_count, :point_base_multiplier, :point_shop_multiplier,
                     :point_up_multiplier, :point_deal_multiplier, :point_item_multiplier,
                     :point_total_multiplier, :is_point_back, :point_back_rate,
                     :point_text_raw,
                     :coupon_discount, :coupon_type, :coupon_text_raw,
                     :shipping_cost, :delivery_text, :delivery_days, :is_free_shipping,
                     :review_score, :review_count,
                     :shop_code, :shop_name, :shop_url_code,
                     :item_name, :item_subtitle, :image_url, :genre_ids, :variant_id,
                     :has_multi_sku,
                     :is_pr, :is_super_deal, :is_shop39, :is_sold_out,
                     :data_source, :list_url)
            """), {
                "item_id": item_id,
                "price": item_data.get("price"),
                "price_range": item_data.get("price_range"),
                "point_count": item_data.get("point_count"),
                "point_base_multiplier": item_data.get("point_base_multiplier"),
                "point_shop_multiplier": item_data.get("point_shop_multiplier"),
                "point_up_multiplier": item_data.get("point_up_multiplier"),
                "point_deal_multiplier": item_data.get("point_deal_multiplier"),
                "point_item_multiplier": item_data.get("point_item_multiplier"),
                "point_total_multiplier": item_data.get("point_total_multiplier"),
                "is_point_back": 1 if item_data.get("is_point_back") else 0,
                "point_back_rate": item_data.get("point_back_rate"),
                "point_text_raw": point_text_raw,
                "coupon_discount": item_data.get("coupon_discount"),
                "coupon_type": item_data.get("coupon_type"),
                "coupon_text_raw": coupon_text_raw,
                "shipping_cost": item_data.get("shipping_cost"),
                "delivery_text": item_data.get("delivery_text"),
                "delivery_days": item_data.get("delivery_days"),
                "is_free_shipping": 1 if item_data.get("is_free_shipping") else 0,
                "review_score": item_data.get("review_score"),
                "review_count": item_data.get("review_count"),
                "shop_code": shop_code,
                "shop_name": shop_name,
                "shop_url_code": item_data.get("shop_url_code"),
                "item_name": item_data.get("item_name"),
                "item_subtitle": item_data.get("item_subtitle"),
                "image_url": item_data.get("image_url"),
                "genre_ids": item_data.get("genre_ids"),
                "variant_id": item_data.get("variant_id"),
                "has_multi_sku": 1 if item_data.get("has_multi_sku") else 0,
                "is_pr": 1 if item_data.get("is_pr") else 0,
                "is_super_deal": 1 if item_data.get("is_super_deal") else 0,
                "is_shop39": 1 if item_data.get("is_shop39") else 0,
                "is_sold_out": 1 if item_data.get("is_sold_out") else 0,
                "data_source": item_data.get("data_source", "html"),
                "list_url": url,
            })

            # Compute product_uid for logging / traceability
            product_uid = make_item_id_str(shop_code, item_code)

            # ── Enqueue detail scrape (deduplicated) ──────────
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

        # Extract pagination info for logging
        total_found = pagination.get("numFound") if pagination else None
        page_size = pagination.get("pageSize") if pagination else None

        return {
            "status": "ok",
            "url": url,
            "items_found": len(items),
            "enqueued": enqueued,
            "total_found": total_found,
            "page_size": page_size,
        }

    except Exception as e:
        logger.error("List scrape error for %s: %s", url, e, exc_info=True)
        raise
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════
#  Paginated Scraper
# ═══════════════════════════════════════════════════════════════

@celery_app.task(name="app.workers.list_scraper.scrape_list_paginated")
def scrape_list_paginated(
    base_url: str,
    genre_id: str | None = None,
    shop_code: str | None = None,
    keyword: str | None = None,
    min_price: int | None = None,
    max_price: int | None = None,
    sort: str | None = None,
    max_pages: int = MAX_PAGES,
) -> dict:
    """
    Scrape all pages for a given search query (paginated) — SMART version.

    Instead of blindly dispatching all 150 pages, this:
      1. Fetches page 1 to determine the ACTUAL page count
      2. Dispatches only the pages that actually have results
      3. For 28.7M products with thousands of price slices, this prevents
         millions of wasted requests to empty pages

    This is **Stage 2** of the 3-stage pipeline:
      Stage 1 (API): basic product data (name, price, image, points)
      Stage 2 (this): enrichment via list pages (coupons, point breakdown, shipping)
      Stage 3 (detail): full variant data (auto-enqueued per product)
    """
    # ── Fetch page 1 to get actual page count ─────────────────
    page1_url = build_search_url(
        genre_id=genre_id,
        keyword=keyword,
        shop_code=shop_code,
        min_price=min_price,
        max_price=max_price,
        sort=sort,
        page=1,
    )

    html = _fetch_page(page1_url)
    if not html:
        logger.warning(
            "Stage 2: failed to fetch page 1 for genre=%s price=%s-%s",
            genre_id, min_price, max_price,
        )
        return {
            "status": "error",
            "reason": "page1_fetch_failed",
            "genre_id": genre_id,
            "price_range": f"{min_price}-{max_price}",
        }

    # Parse page 1 to get items + pagination metadata
    items, pagination = _parse_search_results(html)
    total_found = pagination.get("numFound", 0) if pagination else 0
    page_size = pagination.get("pageSize", ITEMS_PER_PAGE) if pagination else ITEMS_PER_PAGE

    if total_found == 0 and not items:
        logger.info(
            "Stage 2: no results for genre=%s price=%s-%s, skipping",
            genre_id, min_price, max_price,
        )
        return {
            "status": "ok",
            "pages_dispatched": 0,
            "total_found": 0,
            "genre_id": genre_id,
            "price_range": f"{min_price}-{max_price}",
        }

    # Calculate actual page count
    if total_found > 0 and page_size > 0:
        import math
        actual_pages = min(math.ceil(total_found / page_size), max_pages)
    elif items:
        # Fallback: if we got items but no pagination, assume at least some pages
        actual_pages = max_pages
    else:
        actual_pages = 1

    # ── Dispatch page 1 for processing (we already fetched it) ──
    # Process page 1 directly via scrape_list_page task
    scrape_list_page.delay(page1_url)
    pages_dispatched = 1

    # ── Dispatch remaining pages (2..actual_pages) ──────────────
    for page in range(2, actual_pages + 1):
        url = build_search_url(
            genre_id=genre_id,
            keyword=keyword,
            shop_code=shop_code,
            min_price=min_price,
            max_price=max_price,
            sort=sort,
            page=page,
        )
        scrape_list_page.delay(url)
        pages_dispatched += 1

    logger.info(
        "Stage 2 dispatched: %d/%d actual pages for genre=%s price=%s-%s "
        "(total_found=%d, page_size=%d)",
        pages_dispatched, actual_pages, genre_id, min_price, max_price,
        total_found, page_size,
    )

    return {
        "status": "ok",
        "pages_dispatched": pages_dispatched,
        "actual_pages": actual_pages,
        "total_found": total_found,
        "base_url": base_url,
        "genre_id": genre_id,
        "price_range": f"{min_price}-{max_price}",
    }


# ═══════════════════════════════════════════════════════════════
#  Daily Scheduled Task
# ═══════════════════════════════════════════════════════════════

@celery_app.task(name="app.workers.list_scraper.run_daily_list_scrape")
def run_daily_list_scrape() -> dict:
    """
    Scheduled daily task: run list scraping for active shop crawl targets.
    Shop targets are handled here (genre/keyword targets are handled by API discovery).
    """
    db = get_sync_db()
    try:
        result = db.execute(text("""
            SELECT id, target_type, target_value, base_url
            FROM crawl_targets
            WHERE is_active = 1 AND target_type = 'shop'
            ORDER BY priority DESC
        """))
        targets = result.mappings().all()

        tasks_dispatched = 0
        for target in targets:
            shop_code = target["target_value"]
            scrape_list_paginated.delay(
                base_url=f"https://search.rakuten.co.jp/search/mall/?sid={shop_code}",
                shop_code=shop_code,
                max_pages=MAX_PAGES,
            )
            tasks_dispatched += 1

            db.execute(text("""
                UPDATE crawl_targets SET last_crawled_at = NOW()
                WHERE id = :target_id
            """), {"target_id": target["id"]})

        db.commit()

        logger.info("Daily list scrape: dispatched %d shop targets", tasks_dispatched)
        return {"status": "ok", "tasks_dispatched": tasks_dispatched}

    except Exception as e:
        logger.error("Daily list scrape error: %s", e, exc_info=True)
        raise
    finally:
        db.close()
