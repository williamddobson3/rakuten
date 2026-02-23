"""
Detail Scraper Worker.

Step 3 of the 3-stage pipeline:
  Scrapes individual product detail pages to extract full information
  at the variant level.

PRIMARY strategy:
  Parse the embedded JSON from <script type="application/json" id="item-page-app-data">.
  Path: api.data.itemInfoSku  (or newApi.itemInfoSku)

  This JSON contains:
    - itemInfoSku.sku[]          → per-variant data (price, JAN, images, shipping, etc.)
    - itemInfoSku.variantSelectors → variant axis definitions (color, size, etc.)
    - itemInfoSku.purchaseInfo   → stock quantities, delivery messages, sold-out status
    - itemInfoSku.itemReviewInfo → review count + average rating
    - itemInfoSku.pointCampaignInfo → point campaign details
    - itemInfoSku.genreInfo      → genre ID path
    - itemInfoSku.is39Shop       → 39ショップ flag
    - itemInfoSku.superDeal      → スーパーDEAL flag
    - itemInfoSku.payment        → tax-included flag
    - itemInfoSku.media          → main product images + per-SKU images

FALLBACK strategy:
  If embedded JSON is not found or is incomplete, fall back to HTML parsing
  using BeautifulSoup (itemprop selectors, CSS class selectors, etc.).

Extracts per variant:
  - variantId, merchantDefinedSkuId
  - JAN code (from articleNumber.value, 12-13 digits)
  - taxIncludedPrice
  - images
  - selectorValues (color/size labels)
  - shipping (postageIncluded, singleItemShipping)
  - normalDeliveryTime (delivery days)
  - stock quantity, sold-out status, delivery message
  - attributes (series, brand, model number, color, etc.)

Then upserts: shop → item → variant(s) → snapshot+history
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime
from typing import Any

import httpx
from bs4 import BeautifulSoup
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

_CONTEXT_NAME = "detail_scraper"

# Thread lock for Playwright — only ONE thread may use Playwright at a time.
# Playwright's sync API is single-threaded; concurrent access from thread pool
# causes "cannot switch to a different thread" errors.
_pw_thread_lock = threading.Lock()


# ═══════════════════════════════════════════════════════════════
#  Playwright Browser Context (shared via playwright_pool)
# ═══════════════════════════════════════════════════════════════


def _get_browser_context():
    """Get the shared Playwright browser context for detail scraping."""
    return _pool_get_context(_CONTEXT_NAME)


def force_refresh_cookies() -> bool:
    """Force immediate cookie refresh in the detail scraper context."""
    return _pool_force_refresh(_CONTEXT_NAME)


def _get_proxy() -> str | None:
    """Get proxy URL from pool if configured."""
    if not settings.PROXY_POOL_URL:
        return None
    try:
        with httpx.Client(timeout=5) as client:
            resp = client.get(settings.PROXY_POOL_URL)
            return resp.text.strip() or None
    except Exception:
        return None


def _get_auth_cookies() -> dict[str, str]:
    """
    Load Rakuten authentication cookies for authenticated scraping.
    Cookies enable viewing login-only data (coupons, user points, etc.).
    """
    try:
        from app.services.rakuten_auth import get_auth_cookie_dict
        cookies = get_auth_cookie_dict()
        if cookies:
            logger.debug("Loaded %d auth cookies for scraping", len(cookies))
        return cookies
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
_httpx_detail_client: httpx.Client | None = None


def _get_httpx_client() -> httpx.Client:
    """Get or create a shared httpx client with connection pooling."""
    global _httpx_detail_client
    if _httpx_detail_client is None or _httpx_detail_client.is_closed:
        cookies_dict = _get_auth_cookies()
        # Note: http2 requires 'h2' package; use http1.1 which works fine for HTML
        _httpx_detail_client = httpx.Client(
            headers=_BROWSER_HEADERS,
            cookies=cookies_dict,
            timeout=20,
            follow_redirects=True,
        )
    return _httpx_detail_client


def _is_waf_blocked(html: str) -> bool:
    """Check if the response is a WAF/bot challenge page."""
    # Very short pages are suspicious, but Rakuten 404/gone pages can be small too.
    # Only flag as WAF if BOTH short AND no typical Rakuten markers.
    if len(html) < 1000:
        return True
    # A valid product page has item-page-app-data or typical Rakuten HTML
    if "item-page-app-data" in html:
        return False  # Definitely a valid product page
    if "itemprop" in html and "rakuten" in html.lower():
        return False  # Likely a valid product page (HTML fallback path)
    # Check for WAF challenge markers
    waf_signals = [
        "Access Denied",
        "cf-browser-verification",
        "challenge-platform",
        "_sec_cpt",
        "Just a moment",
    ]
    html_lower = html[:5000].lower()
    return any(sig.lower() in html_lower for sig in waf_signals)


def _fetch_detail_page(url: str) -> str | None:
    """
    Fetch product detail page — fast httpx first, Playwright fallback.

    Strategy:
      1. Try httpx with browser headers + auth cookies (0.1-0.3s)
      2. If WAF blocked → fall back to Playwright (2-5s)

    This is 10-50x faster than always using Playwright.
    The embedded JSON (item-page-app-data) is in the initial HTML,
    no JavaScript rendering needed.
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
            logger.warning("Product page not found: %s", url)
            return None
        elif resp.status_code == 429:
            logger.warning("Rate limited (httpx) on %s", url)
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
                        logger.warning("Product page not found: %s", url)
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
#  Embedded JSON extraction (PRIMARY strategy)
# ═══════════════════════════════════════════════════════════════

def _extract_app_data_json(html: str) -> dict[str, Any] | None:
    """
    Extract the full JSON object from:
      <script type="application/json" id="item-page-app-data">

    Returns the parsed dict, or None if not found.
    """
    soup = BeautifulSoup(html, "lxml")
    script_tag = soup.find("script", {"id": "item-page-app-data", "type": "application/json"})
    if not script_tag or not script_tag.string:
        return None

    try:
        data = json.loads(script_tag.string)
        return data
    except json.JSONDecodeError as e:
        logger.warning("Failed to parse item-page-app-data JSON: %s", e)
        return None


def _get_item_info_sku(app_data: dict) -> dict[str, Any] | None:
    """
    Navigate to the itemInfoSku object from the app_data root.

    Tries multiple paths:
      1. api → data → itemInfoSku
      2. newApi → itemInfoSku
      3. api → itemInfoSku  (flat structure)
    """
    # Path 1: api.data.itemInfoSku
    api_data = app_data.get("api", {})
    if isinstance(api_data, dict):
        data_section = api_data.get("data", api_data)
        if isinstance(data_section, dict):
            sku_info = data_section.get("itemInfoSku")
            if sku_info and isinstance(sku_info, dict):
                return sku_info

    # Path 2: newApi.itemInfoSku
    new_api = app_data.get("newApi", {})
    if isinstance(new_api, dict):
        sku_info = new_api.get("itemInfoSku")
        if sku_info and isinstance(sku_info, dict):
            return sku_info

    return None


def _extract_item_level_from_json(
    item_info: dict,
    app_data: dict,
) -> dict[str, Any]:
    """
    Extract item-level (non-variant) data from itemInfoSku + root app_data.

    Returns a dict with: title, shop_name, shop_url_code, is39Shop,
    superDeal, genre_id, review_count, review_rating, tax_included,
    point_campaign, price_range, etc.
    """
    result: dict[str, Any] = {}

    # ── Basic item info ────────────────────────────────────────
    result["title"] = item_info.get("title")
    result["manage_number"] = item_info.get("manageNumber")
    result["shop_id_numeric"] = item_info.get("shopId")
    result["item_id_numeric"] = item_info.get("itemId")
    result["new_item_number"] = item_info.get("newItemNumber")
    result["sell_type"] = item_info.get("sellType", "NORMAL")

    # ── Flags ──────────────────────────────────────────────────
    result["is_39_shop"] = item_info.get("is39Shop", False)
    result["is_super_deal"] = item_info.get("superDeal", False)
    result["is_mno_flag"] = item_info.get("isMnoFlag", False)

    # ── Genre ──────────────────────────────────────────────────
    genre_info = item_info.get("genreInfo", {})
    result["ancestor_genre_id"] = genre_info.get("ancestorGenreId")

    # From pcFields
    pc_fields = item_info.get("pcFields", {})
    result["r_category_id"] = pc_fields.get("rCategoryId")
    result["item_number"] = pc_fields.get("itemNumber")

    # ── Payment ────────────────────────────────────────────────
    payment = item_info.get("payment", {})
    result["tax_included"] = payment.get("taxIncluded", True)

    # ── Reviews ────────────────────────────────────────────────
    review_info = item_info.get("itemReviewInfo", {})
    summary = review_info.get("summary", {})
    result["review_count"] = summary.get("itemReviewCount")
    result["review_rating"] = summary.get("itemReviewRating")

    # ── Point Campaign ─────────────────────────────────────────
    point_campaigns = item_info.get("pointCampaignInfo", [])
    if point_campaigns and isinstance(point_campaigns, list):
        first_campaign = point_campaigns[0]
        point_campaign = first_campaign.get("pointCampaign", {})
        result["point_campaign"] = point_campaign
        result["is_point_optimization"] = first_campaign.get("isPointOptimization", False)

    # ── Price range (from purchaseInfo) ────────────────────────
    purchase_info = item_info.get("purchaseInfo", {})
    pbs = purchase_info.get("purchaseBySellType", {})
    normal_purchase = pbs.get("normalPurchase", {})
    price_obj = normal_purchase.get("price", {})
    result["price_min"] = price_obj.get("minPrice")
    result["price_max"] = price_obj.get("maxPrice")
    result["pre_tax_price"] = normal_purchase.get("preTaxPrice")

    # ── Inventory type ─────────────────────────────────────────
    result["inventory_type"] = item_info.get("inventoryType", "single")
    # "multiple" = has variants, "single" = no variants

    # ── Variant selectors (axis definitions) ───────────────────
    result["variant_selectors"] = item_info.get("variantSelectors", [])

    # ── Main images ────────────────────────────────────────────
    media = item_info.get("media", {})
    main_images = media.get("images", [])
    if main_images:
        first_img = main_images[0]
        result["main_image_url"] = first_img.get("location")
    else:
        # Fallback to oldImage
        result["main_image_url"] = item_info.get("oldImage")

    # ── Shop info (from root app_data) ─────────────────────────
    shop_data = app_data.get("shop", {})
    result["shop_name"] = shop_data.get("shopName")
    result["shop_url_code"] = shop_data.get("shopUrl")
    result["tax_rate"] = shop_data.get("taxRate")

    # ── Breadcrumbs (for genre path) ──────────────────────────
    breadcrumbs = item_info.get("breadcrumbs", {})
    genre_breadcrumbs = breadcrumbs.get("genreBreadcrumbs", [])
    result["genre_breadcrumbs"] = genre_breadcrumbs

    # ── Coupon (from featureSectionInfo) ───────────────────────
    feature_info = item_info.get("featureSectionInfo", {})
    result["has_coupon_section"] = feature_info.get("coupon", False)

    # ── Identical variants flags ───────────────────────────────
    result["identical_variants"] = item_info.get("identicalVariants", {})

    # ── Hometown tax eligible ──────────────────────────────────
    result["hometown_tax_eligible"] = item_info.get("hometownTaxEligible", False)

    return result


def _extract_variants_from_item_info(item_info: dict) -> list[dict[str, Any]]:
    """
    Extract all variant (SKU) data from itemInfoSku.

    Merges data from:
      - itemInfoSku.sku[]                  → core variant data
      - itemInfoSku.purchaseInfo.sku[]     → delivery messages, sold-out status
      - itemInfoSku.purchaseInfo.variantMappedInventories[] → stock quantities

    Returns a list of variant dicts, each containing:
      variant_code, jan_code, price, variant_name, image_url,
      selector_values, attributes, shipping_info, stock_quantity,
      is_sold_out, delivery_message, etc.
    """
    sku_list = item_info.get("sku", [])
    if not sku_list:
        return []

    # Build lookup maps from purchaseInfo
    purchase_info = item_info.get("purchaseInfo", {})

    # Stock quantities: variantMappedInventories
    inventory_map: dict[str, int] = {}
    for inv in purchase_info.get("variantMappedInventories", []):
        sku_id = inv.get("sku", "")
        quantity = inv.get("quantity", 0)
        inventory_map[sku_id] = quantity

    # Purchase SKU info: delivery messages, sold-out status
    purchase_sku_map: dict[str, dict] = {}
    for ps in purchase_info.get("sku", []):
        vid = ps.get("variantId", "")
        purchase_sku_map[vid] = ps

    variants: list[dict[str, Any]] = []

    for sku in sku_list:
        variant_id = sku.get("variantId", "")
        if not variant_id:
            continue

        # Skip hidden variants
        if sku.get("hidden", False):
            continue

        # ── JAN code ──────────────────────────────────────────
        jan_code = None
        article_number = sku.get("articleNumber", {})
        if isinstance(article_number, dict):
            jan_raw = article_number.get("value")
            # Only accept valid JAN codes (12 or 13 digits)
            if jan_raw and isinstance(jan_raw, str) and re.match(r"^\d{12,13}$", jan_raw):
                jan_code = jan_raw
            # If exemptionReason is present, no JAN registered
            # exemptionReason: 1=not applicable, 2=not registered, etc.

        # ── Price ─────────────────────────────────────────────
        price_raw = sku.get("taxIncludedPrice")
        price = int(price_raw) if price_raw is not None else None

        # ── Variant name from selectorValues ──────────────────
        selector_values = sku.get("selectorValues", [])
        variant_name = " / ".join(selector_values) if selector_values else None

        # ── Merchant-defined SKU ID ───────────────────────────
        merchant_sku_id = sku.get("merchantDefinedSkuId")

        # ── Images ────────────────────────────────────────────
        images = sku.get("images", [])
        image_url = None
        image_urls: list[str] = []
        for img in images:
            if isinstance(img, dict):
                loc = img.get("location", "")
                if loc:
                    image_urls.append(loc)
                    if not image_url:
                        image_url = loc
            elif isinstance(img, str):
                image_urls.append(img)
                if not image_url:
                    image_url = img

        # ── Shipping ──────────────────────────────────────────
        shipping_obj = sku.get("shipping", {})
        postage_included = shipping_obj.get("postageIncluded", False)
        single_item_shipping = shipping_obj.get("singleItemShipping", 0)

        # ── Delivery ──────────────────────────────────────────
        normal_delivery_time = sku.get("normalDeliveryTime")  # days
        normal_delivery_date_id = sku.get("normalDeliveryDateId")

        # ── Attributes (brand, color, etc.) ───────────────────
        attributes = sku.get("attributes", [])

        # ── Purchase info (delivery message, sold-out) ────────
        p_sku = purchase_sku_map.get(variant_id, {})
        new_purchase_sku = p_sku.get("newPurchaseSku", {})
        delivery_message = new_purchase_sku.get("deliveryMessage")
        stock_condition = new_purchase_sku.get("stockCondition")  # "sold-out" or None
        stock_quantity_purchase = new_purchase_sku.get("quantity", 0)

        # Double price (reference price) info
        double_price = p_sku.get("doublePrice", {})
        reference_price_verified = double_price.get("referencePriceVerified", False)

        # ── Stock from inventory map ──────────────────────────
        stock_quantity = inventory_map.get(variant_id, stock_quantity_purchase)
        is_sold_out = stock_condition == "sold-out" or stock_quantity == 0

        # ── Noshi (gift wrapping) ─────────────────────────────
        noshi = sku.get("noshi", False)

        # ── Build variant dict ────────────────────────────────
        variants.append({
            "variant_code": variant_id,
            "merchant_sku_id": merchant_sku_id,
            "jan_code": jan_code,
            "price": price,
            "variant_name": variant_name,
            "image_url": image_url,
            "image_urls": image_urls,
            "selector_values": selector_values,
            "attributes": attributes,
            "postage_included": postage_included,
            "single_item_shipping": single_item_shipping,
            "normal_delivery_time": normal_delivery_time,
            "normal_delivery_date_id": normal_delivery_date_id,
            "delivery_message": delivery_message,
            "stock_quantity": stock_quantity,
            "is_sold_out": is_sold_out,
            "reference_price_verified": reference_price_verified,
            "noshi": noshi,
        })

    return variants


# ═══════════════════════════════════════════════════════════════
#  Coupon extraction from detail page
# ═══════════════════════════════════════════════════════════════

def _extract_coupon_from_page(html: str, soup: BeautifulSoup) -> dict[str, Any]:
    """
    Extract coupon information from the detail page.

    Checks:
    1. Embedded JSON for coupon data
    2. HTML coupon elements
    """
    coupon_yen: int | None = None
    coupon_percent: float | None = None

    # ── Method 1: HTML coupon elements ────────────────────────
    coupon_els = soup.select("[class*='coupon'], [class*='Coupon']")
    for el in coupon_els:
        text_content = el.get_text(strip=True)
        # Match "XXX円OFF" or "XXX円クーポン"
        yen_match = re.search(r"(\d[\d,]*)(?:\s*円)", text_content)
        if yen_match:
            val = int(yen_match.group(1).replace(",", ""))
            if coupon_yen is None or val > coupon_yen:
                coupon_yen = val

        # Match "XX%OFF"
        pct_match = re.search(r"(\d+(?:\.\d+)?)\s*%", text_content)
        if pct_match:
            val = float(pct_match.group(1))
            if coupon_percent is None or val > coupon_percent:
                coupon_percent = val

    # ── Method 2: Embedded JSON coupon patterns ───────────────
    coupon_json_match = re.search(
        r'"coupon"\s*:\s*\{\s*"discount"\s*:\s*(\d+)\s*,\s*"discountType"\s*:\s*"(\w+)"',
        html,
    )
    if coupon_json_match:
        discount = int(coupon_json_match.group(1))
        discount_type = coupon_json_match.group(2)
        if discount_type == "exact" and (coupon_yen is None or discount > coupon_yen):
            coupon_yen = discount
        elif discount_type == "percentage" and (coupon_percent is None or discount > coupon_percent):
            coupon_percent = float(discount)

    return {
        "coupon_yen": coupon_yen,
        "coupon_percent": coupon_percent,
    }


# ═══════════════════════════════════════════════════════════════
#  HTML fallback extraction (SECONDARY strategy)
# ═══════════════════════════════════════════════════════════════

def _extract_jan_single(soup: BeautifulSoup) -> str | None:
    """
    Extract JAN code for a single (non-variant) product from HTML.
    Tries itemprop="gtin13" then itemprop="gtin12".
    """
    gtin13 = soup.find(attrs={"itemprop": "gtin13"})
    if gtin13:
        content = gtin13.get("content", "")
        if content and re.match(r"^\d{13}$", content):
            return content

    gtin12 = soup.find(attrs={"itemprop": "gtin12"})
    if gtin12:
        content = gtin12.get("content", "")
        if content and re.match(r"^\d{12}$", content):
            return content

    return None


def _extract_item_name_html(soup: BeautifulSoup) -> str | None:
    """Extract product name from HTML (fallback)."""
    name_el = soup.find(attrs={"itemprop": "name"})
    if name_el:
        return name_el.get_text(strip=True)

    title = soup.find("title")
    if title:
        text_content = title.get_text(strip=True)
        text_content = re.sub(r"【楽天市場】", "", text_content)
        text_content = re.sub(r"\s*:\s*[^:]+$", "", text_content)
        return text_content.strip()

    return None


def _extract_price_html(soup: BeautifulSoup) -> int | None:
    """Extract main product price from HTML (fallback)."""
    price_el = soup.find(attrs={"itemprop": "price"})
    if price_el:
        content = price_el.get("content", "")
        if content and re.match(r"^\d+(\.\d+)?$", content):
            return int(float(content))
    return None


def _extract_image_html(soup: BeautifulSoup) -> str | None:
    """Extract main product image URL from HTML (fallback)."""
    img = soup.find(attrs={"itemprop": "image"})
    if img:
        return img.get("content") or img.get("src")

    og_img = soup.find("meta", attrs={"property": "og:image"})
    if og_img:
        return og_img.get("content")

    return None


def _extract_shop_name_html(soup: BeautifulSoup) -> str | None:
    """Extract shop name from HTML (fallback)."""
    shop_el = soup.select_one(".shopName, [class*='shop-name'], [class*='shopName']")
    if shop_el:
        return shop_el.get_text(strip=True)

    seller = soup.find(attrs={"itemprop": "seller"})
    if seller:
        name = seller.find(attrs={"itemprop": "name"})
        if name:
            return name.get_text(strip=True)

    return None


def _extract_point_info_html(soup: BeautifulSoup) -> dict[str, Any]:
    """
    Extract point rate and point-back percent from rendered HTML.

    Handles two distinct Rakuten point systems:
      1. Shop point (ショップポイント): "85ポイント(1倍)" or "780ポイント(1倍+9倍UP)"
         → stored as point_rate (the total multiplier, e.g. 1.0 or 10.0)
      2. Point back (ポイントバック): "2,520ポイント(20%ポイントバック)"
         → stored as point_back_percent (e.g. 20.0)

    Real HTML structure (from Playwright-rendered page):
      <div class="points--DNEud bdg-point-display" data-price="9458" ...>
        <span>85ポイント(1倍)</span>
      </div>
      <div class="points--DNEud" data-price="13860" ...>
        <span>2,520ポイント(20%ポイントバック)</span>
      </div>
    """
    point_rate = None
    point_back_percent = None

    # Search both class-based selectors and data-attribute selectors
    point_els = soup.select(
        "[class*='point'], [class*='Point'], "
        "[data-trigger='breakdown-link'], "
        ".bdg-point-display"
    )
    for el in point_els:
        text_content = el.get_text(strip=True)
        if not text_content:
            continue

        # ── Point back: "2,520ポイント(20%ポイントバック)" ─────────
        # Must check BEFORE multiplier to avoid false positives
        back_match = re.search(
            r"(\d+(?:\.\d+)?)\s*%\s*ポイントバック",
            text_content,
        )
        if back_match:
            val = float(back_match.group(1))
            if point_back_percent is None or val > point_back_percent:
                point_back_percent = val
            continue  # This element is point-back, skip multiplier check

        # ── Shop point multiplier: "85ポイント(1倍)" or "780ポイント(1倍+9倍UP)" ─
        # Total multiplier = base + UP parts
        # Pattern: "(1倍)" or "(1倍+9倍UP)"
        total_multi = 0.0

        # Base multiplier: "(3倍" or "(1倍+..."
        base_match = re.search(r"\((\d+(?:\.\d+)?)\s*倍", text_content)
        if base_match:
            total_multi = float(base_match.group(1))

        # Additional UP multiplier: "+9倍UP" or "+4倍UP"
        up_match = re.search(r"\+(\d+(?:\.\d+)?)\s*倍\s*(?:UP|up|アップ)", text_content)
        if up_match:
            total_multi += float(up_match.group(1))

        # Fallback: standalone "X倍" without parentheses
        if total_multi == 0:
            rate_match = re.search(r"\+?\s*(\d+(?:\.\d+)?)\s*倍", text_content)
            if rate_match:
                total_multi = float(rate_match.group(1))

        if total_multi > 0:
            if point_rate is None or total_multi > point_rate:
                point_rate = total_multi

    return {
        "point_rate": point_rate,
        "point_back_percent": point_back_percent,
    }


def _extract_shipping_info_html(soup: BeautifulSoup) -> dict[str, Any]:
    """Extract shipping/delivery info from HTML (fallback)."""
    shipping_text_raw = None
    shipping_days_min = None
    shipping_days_max = None

    shipping_el = soup.select_one(
        "[class*='shipping'], [class*='delivery'], [class*='postage']"
    )
    if shipping_el:
        shipping_text_raw = shipping_el.get_text(strip=True)
        if shipping_text_raw:
            if "翌日" in shipping_text_raw:
                shipping_days_min = 1
                shipping_days_max = 1
            days_match = re.search(r"(\d+)\s*日以内", shipping_text_raw)
            if days_match:
                shipping_days_max = int(days_match.group(1))
                shipping_days_min = 1
            range_match = re.search(r"(\d+)\s*[～~-]\s*(\d+)\s*日", shipping_text_raw)
            if range_match:
                shipping_days_min = int(range_match.group(1))
                shipping_days_max = int(range_match.group(2))

    return {
        "shipping_text_raw": shipping_text_raw,
        "shipping_days_min": shipping_days_min,
        "shipping_days_max": shipping_days_max,
    }


def _extract_extra_fields_html(soup: BeautifulSoup) -> dict[str, Any]:
    """Extract +2 additional fields from HTML (fallback)."""
    extra1_key = None
    extra1_value = None
    extra2_key = None
    extra2_value = None

    brand = soup.find(attrs={"itemprop": "brand"})
    if brand:
        brand_name = brand.find(attrs={"itemprop": "name"})
        if brand_name:
            extra1_key = "ブランド"
            extra1_value = brand_name.get_text(strip=True)

    sku = soup.find(attrs={"itemprop": "sku"})
    if sku:
        content = sku.get("content") or sku.get_text(strip=True)
        if content:
            extra2_key = "型番"
            extra2_value = content

    if not extra1_key or not extra2_key:
        spec_rows = soup.select("table tr, dl dt, .spec-item")
        for row in spec_rows[:20]:
            text_content = row.get_text(strip=True)
            if not extra1_key and ("ブランド" in text_content or "メーカー" in text_content):
                value_el = row.find_next("td") or row.find_next("dd")
                if value_el:
                    extra1_key = "ブランド"
                    extra1_value = value_el.get_text(strip=True)
            elif not extra2_key and ("型番" in text_content or "素材" in text_content or "材質" in text_content):
                value_el = row.find_next("td") or row.find_next("dd")
                if value_el:
                    extra2_key = text_content.split("：")[0].strip() if "：" in text_content else "型番"
                    extra2_value = value_el.get_text(strip=True)

    return {
        "extra1_key": extra1_key,
        "extra1_value": extra1_value,
        "extra2_key": extra2_key,
        "extra2_value": extra2_value,
    }


# ═══════════════════════════════════════════════════════════════
#  Helper: build snapshot data from variant + item-level info
# ═══════════════════════════════════════════════════════════════

def _build_snapshot_from_json_variant(
    variant_data: dict[str, Any],
    item_level: dict[str, Any],
    coupon_info: dict[str, Any],
) -> dict[str, Any]:
    """
    Build the snapshot_data dict that gets passed to upsert_snapshot_and_history_sync.

    Merges variant-level data with item-level data.
    """
    # Price
    price = variant_data.get("price")

    # Image: prefer variant image, fallback to item main image
    image_url = variant_data.get("image_url") or item_level.get("main_image_url")

    # Shipping text
    shipping_text_raw = None
    if variant_data.get("postage_included"):
        shipping_text_raw = "送料込み"
    elif variant_data.get("single_item_shipping"):
        shipping_text_raw = f"送料 {variant_data['single_item_shipping']}円"

    delivery_msg = variant_data.get("delivery_message")
    if delivery_msg:
        if shipping_text_raw:
            shipping_text_raw = f"{shipping_text_raw} / {delivery_msg}"
        else:
            shipping_text_raw = delivery_msg

    # Shipping days from normalDeliveryTime
    shipping_days_max = variant_data.get("normal_delivery_time")
    shipping_days_min = None
    if shipping_days_max is not None:
        shipping_days_min = 1  # Minimum is usually 1 day

    # Point rate from item-level point campaign
    point_rate = None
    point_back_percent = None
    point_campaign = item_level.get("point_campaign", {})
    if point_campaign:
        # Point campaign can have various multiplier fields
        point_rate = point_campaign.get("pointRate") or point_campaign.get("rate")
        # Point back percentage (e.g. Super DEAL +30% ポイントバック)
        point_back_percent = point_campaign.get("pointBackPercent") or point_campaign.get("pointBack")
        if point_back_percent is not None:
            point_back_percent = float(point_back_percent)

    # Also check for Super DEAL point back at item level
    if point_back_percent is None and item_level.get("is_super_deal"):
        # Super DEAL items typically have a deal multiplier
        deal_multi = point_campaign.get("dealMultiplier") if point_campaign else None
        if deal_multi:
            point_back_percent = float(deal_multi)

    # Extra fields from variant attributes
    extra1_key = None
    extra1_value = None
    extra2_key = None
    extra2_value = None
    attributes = variant_data.get("attributes", [])
    for attr in attributes:
        title = attr.get("title", "")
        value = attr.get("value", "")
        if value == "-" or not value:
            continue
        if "ブランド" in title and not extra1_key:
            extra1_key = "ブランド"
            extra1_value = value
        elif "メーカー" in title and not extra1_key:
            extra1_key = "メーカー"
            extra1_value = value
        elif ("型番" in title or "メーカー型番" in title) and not extra2_key:
            extra2_key = "型番"
            extra2_value = value
        elif "カラー" in title and not extra2_key:
            extra2_key = "カラー"
            extra2_value = value

    # If extra fields still empty, use series/brand names from attributes
    if not extra1_key:
        for attr in attributes:
            title = attr.get("title", "")
            value = attr.get("value", "")
            if value == "-" or not value:
                continue
            if "シリーズ" in title:
                extra1_key = "シリーズ"
                extra1_value = value
                break

    # Review data as extra if no brand/model found
    if not extra1_key and item_level.get("review_count"):
        extra1_key = "review_count"
        extra1_value = str(item_level["review_count"])
    if not extra2_key and item_level.get("review_rating"):
        extra2_key = "review_rating"
        extra2_value = str(item_level["review_rating"])

    return {
        "price": price,
        "point_rate": point_rate,
        "point_back_percent": point_back_percent,
        "coupon_yen": coupon_info.get("coupon_yen"),
        "coupon_percent": coupon_info.get("coupon_percent"),
        "shipping_text_raw": shipping_text_raw,
        "shipping_days_min": shipping_days_min,
        "shipping_days_max": shipping_days_max,
        "image_url": image_url,
        "extra1_key": extra1_key,
        "extra1_value": extra1_value,
        "extra2_key": extra2_key,
        "extra2_value": extra2_value,
    }


# ═══════════════════════════════════════════════════════════════
#  Save detail observation to DB
# ═══════════════════════════════════════════════════════════════

def _save_detail_observation(
    db,
    item_id: int,
    item_level: dict[str, Any],
    variants_data: list[dict[str, Any]],
    coupon_info: dict[str, Any],
    detail_url: str,
    data_source: str = "json",
) -> None:
    """
    Save a detail observation record with rich structured data.
    This captures a snapshot of the full detail page scrape.
    """
    db.execute(text("""
        INSERT INTO detail_observations
            (item_id, item_name, shop_name, shop_url_code,
             price_min, price_max, review_count, review_rating,
             is_39_shop, is_super_deal, inventory_type,
             coupon_yen, coupon_percent,
             variant_count, variant_data_json,
             ancestor_genre_id, r_category_id,
             data_source, detail_url)
        VALUES
            (:item_id, :item_name, :shop_name, :shop_url_code,
             :price_min, :price_max, :review_count, :review_rating,
             :is_39_shop, :is_super_deal, :inventory_type,
             :coupon_yen, :coupon_percent,
             :variant_count, :variant_data_json,
             :ancestor_genre_id, :r_category_id,
             :data_source, :detail_url)
    """), {
        "item_id": item_id,
        "item_name": item_level.get("title"),
        "shop_name": item_level.get("shop_name"),
        "shop_url_code": item_level.get("shop_url_code"),
        "price_min": int(item_level["price_min"]) if item_level.get("price_min") is not None else None,
        "price_max": int(item_level["price_max"]) if item_level.get("price_max") is not None else None,
        "review_count": item_level.get("review_count"),
        "review_rating": item_level.get("review_rating"),
        "is_39_shop": item_level.get("is_39_shop", False),
        "is_super_deal": item_level.get("is_super_deal", False),
        "inventory_type": item_level.get("inventory_type", "single"),
        "coupon_yen": coupon_info.get("coupon_yen"),
        "coupon_percent": coupon_info.get("coupon_percent"),
        "variant_count": len(variants_data),
        "variant_data_json": json.dumps(
            [
                {
                    "variantId": v["variant_code"],
                    "jan": v.get("jan_code"),
                    "price": v.get("price"),
                    "name": v.get("variant_name"),
                    "soldOut": v.get("is_sold_out", False),
                    "stock": v.get("stock_quantity", 0),
                    "deliveryMsg": v.get("delivery_message"),
                }
                for v in variants_data
            ],
            ensure_ascii=False,
        ) if variants_data else None,
        "ancestor_genre_id": str(item_level.get("ancestor_genre_id", "")) or None,
        "r_category_id": item_level.get("r_category_id"),
        "data_source": data_source,
        "detail_url": detail_url,
    })


# ═══════════════════════════════════════════════════════════════
#  Cookie Refresh Task (called from API after login)
# ═══════════════════════════════════════════════════════════════

@celery_app.task(name="app.workers.detail_scraper.refresh_auth_cookies")
def refresh_auth_cookies() -> dict:
    """
    Celery task to refresh auth cookies in the detail scraper's browser context.
    This runs inside the Celery WORKER process, so it can modify the
    global browser context directly.
    """
    ok = force_refresh_cookies()
    authed = _pool_is_authenticated(_CONTEXT_NAME)
    return {
        "status": "ok" if ok else "no_cookies",
        "authenticated": authed,
        "message": "Auth cookies refreshed in detail scraper" if ok else
                   "No valid auth cookies found — running in guest mode",
    }


# ═══════════════════════════════════════════════════════════════
#  Main detail scrape task
# ═══════════════════════════════════════════════════════════════

@celery_app.task(
    name="app.workers.detail_scraper.scrape_detail_page",
    rate_limit=None,  # No per-task rate limit; concurrency controlled by worker -c flag
    max_retries=3,
    default_retry_delay=60,
)
def scrape_detail_page(url: str, job_id: int | None = None) -> dict:
    """
    Scrape a single product detail page.

    Strategy:
    1. PRIMARY: Parse embedded JSON from <script id="item-page-app-data">
       - Extract item-level data (title, shop, reviews, flags)
       - Extract all variants from sku[] array
       - Merge with purchaseInfo (stock, delivery, sold-out)
       - Extract JAN codes from articleNumber.value

    2. FALLBACK: HTML parsing with BeautifulSoup
       - itemprop selectors for basic data
       - CSS class selectors for points, coupons, shipping

    Then upserts: shop → item → variant(s) → snapshot+history
    """
    db = get_sync_db()
    try:
        # Update job status
        if job_id:
            db.execute(text("""
                UPDATE crawl_jobs
                SET status = 'running', locked_at = NOW(), attempts = attempts + 1
                WHERE id = :job_id
            """), {"job_id": job_id})
            db.commit()

        # Fetch page
        html = _fetch_detail_page(url)
        if not html:
            if job_id:
                db.execute(text("""
                    UPDATE crawl_jobs
                    SET status = 'failed', error_message = 'Fetch failed'
                    WHERE id = :job_id
                """), {"job_id": job_id})
                db.commit()
            return {"status": "error", "url": url, "reason": "fetch_failed"}

        soup = BeautifulSoup(html, "lxml")

        # Canonicalize URL
        canonical_url, url_variant_id = canonicalize_item_url(url)
        try:
            shop_code, item_code = extract_shop_item_code(canonical_url)
        except ValueError:
            return {"status": "error", "url": url, "reason": "invalid_url"}

        # ══════════════════════════════════════════════════════
        #  Try PRIMARY strategy: Embedded JSON
        # ══════════════════════════════════════════════════════
        app_data = _extract_app_data_json(html)
        item_info = _get_item_info_sku(app_data) if app_data else None

        if item_info:
            return _process_with_json(
                db=db,
                html=html,
                soup=soup,
                url=url,
                canonical_url=canonical_url,
                shop_code=shop_code,
                item_code=item_code,
                url_variant_id=url_variant_id,
                app_data=app_data,
                item_info=item_info,
                job_id=job_id,
            )

        # ══════════════════════════════════════════════════════
        #  FALLBACK strategy: HTML parsing
        # ══════════════════════════════════════════════════════
        logger.info("JSON not found for %s, falling back to HTML parsing", url)
        return _process_with_html(
            db=db,
            html=html,
            soup=soup,
            url=url,
            canonical_url=canonical_url,
            shop_code=shop_code,
            item_code=item_code,
            url_variant_id=url_variant_id,
            job_id=job_id,
        )

    except Exception as e:
        logger.error("Detail scrape error for %s: %s", url, e, exc_info=True)
        if job_id:
            try:
                db.execute(text("""
                    UPDATE crawl_jobs
                    SET status = 'failed', error_message = :error
                    WHERE id = :job_id
                """), {"job_id": job_id, "error": str(e)[:2000]})
                db.commit()
            except Exception:
                pass
        raise
    finally:
        db.close()


def _process_with_json(
    db,
    html: str,
    soup: BeautifulSoup,
    url: str,
    canonical_url: str,
    shop_code: str,
    item_code: str,
    url_variant_id: str | None,
    app_data: dict,
    item_info: dict,
    job_id: int | None,
) -> dict:
    """
    Process detail page using the embedded JSON data (primary strategy).
    """
    # ── Extract item-level data ───────────────────────────────
    item_level = _extract_item_level_from_json(item_info, app_data)
    item_name = item_level.get("title")
    shop_name = item_level.get("shop_name")
    shop_url_code = item_level.get("shop_url_code")
    genre_id = str(item_level.get("ancestor_genre_id", "")) or None

    # ── Extract coupon info ───────────────────────────────────
    coupon_info = _extract_coupon_from_page(html, soup)

    # ── Extract point info from rendered HTML (always, as fallback) ──
    # The JSON pointCampaignInfo may be empty or use different field
    # names, so we always also parse the rendered HTML which is the
    # most authoritative source (it's what users actually see).
    html_point_info = _extract_point_info_html(soup)

    # ── Upsert shop ───────────────────────────────────────────
    shop_url = f"https://www.rakuten.co.jp/{shop_url_code}/" if shop_url_code else None
    upsert_shop_sync(db, shop_code=shop_code, shop_name=shop_name, shop_url=shop_url)

    # ── Upsert item ───────────────────────────────────────────
    item_id = upsert_item_sync(
        db,
        shop_code=shop_code,
        item_code=item_code,
        canonical_url=canonical_url,
        item_name=item_name,
        genre_id=genre_id,
        source="detail",
    )

    # ── Extract variants ──────────────────────────────────────
    variants_data = _extract_variants_from_item_info(item_info)
    variants_processed = 0

    if variants_data and len(variants_data) > 0:
        # ── Multi-variant product ─────────────────────────────
        for vd in variants_data:
            variant_db_id = upsert_variant_sync(
                db,
                item_id=item_id,
                variant_code=vd["variant_code"],
                jan_code=vd.get("jan_code"),
                variant_name=vd.get("variant_name"),
                selector_values={
                    "selectorValues": vd.get("selector_values", []),
                    "attributes": vd.get("attributes", []),
                },
            )

            snapshot_data = _build_snapshot_from_json_variant(
                vd, item_level, coupon_info,
            )
            # Supplement with HTML point data when JSON didn't provide values
            if snapshot_data.get("point_rate") is None and html_point_info.get("point_rate") is not None:
                snapshot_data["point_rate"] = html_point_info["point_rate"]
            if snapshot_data.get("point_back_percent") is None and html_point_info.get("point_back_percent") is not None:
                snapshot_data["point_back_percent"] = html_point_info["point_back_percent"]

            upsert_snapshot_and_history_sync(
                db, variant_db_id, snapshot_data, source="detail",
            )
            variants_processed += 1

    else:
        # ── Single product (no variants in JSON) ──────────────
        jan_code = _extract_jan_single(soup)
        variant_code = url_variant_id or "default"

        variant_db_id = upsert_variant_sync(
            db,
            item_id=item_id,
            variant_code=variant_code,
            jan_code=jan_code,
        )

        # Build snapshot from item-level data
        price = None
        if item_level.get("price_max"):
            price = int(item_level["price_max"])
        elif item_level.get("price_min"):
            price = int(item_level["price_min"])

        # Extract point rate + point_back_percent from JSON campaign
        single_point_rate = None
        single_point_back = None
        point_campaign = item_level.get("point_campaign", {})
        if point_campaign:
            single_point_rate = point_campaign.get("pointRate") or point_campaign.get("rate")
            single_point_back = point_campaign.get("pointBackPercent") or point_campaign.get("pointBack")
            if single_point_back is not None:
                single_point_back = float(single_point_back)
        if single_point_back is None and item_level.get("is_super_deal"):
            deal_multi = point_campaign.get("dealMultiplier") if point_campaign else None
            if deal_multi:
                single_point_back = float(deal_multi)

        # Supplement with HTML point data when JSON didn't provide values
        if single_point_rate is None and html_point_info.get("point_rate") is not None:
            single_point_rate = html_point_info["point_rate"]
        if single_point_back is None and html_point_info.get("point_back_percent") is not None:
            single_point_back = html_point_info["point_back_percent"]

        snapshot_data = {
            "price": price,
            "point_rate": float(single_point_rate) if single_point_rate is not None else None,
            "point_back_percent": single_point_back,
            "image_url": item_level.get("main_image_url"),
            "coupon_yen": coupon_info.get("coupon_yen"),
            "coupon_percent": coupon_info.get("coupon_percent"),
            "shipping_text_raw": "送料込み" if item_level.get("is_39_shop") else None,
            "shipping_days_min": None,
            "shipping_days_max": item_info.get("normalDeliveryTime"),
            "extra1_key": "review_count" if item_level.get("review_count") else None,
            "extra1_value": str(item_level["review_count"]) if item_level.get("review_count") else None,
            "extra2_key": "review_rating" if item_level.get("review_rating") else None,
            "extra2_value": str(item_level["review_rating"]) if item_level.get("review_rating") else None,
        }
        upsert_snapshot_and_history_sync(
            db, variant_db_id, snapshot_data, source="detail",
        )
        variants_processed = 1

    # ── Save detail observation ────────────────────────────────
    _save_detail_observation(
        db,
        item_id=item_id,
        item_level=item_level,
        variants_data=variants_data,
        coupon_info=coupon_info,
        detail_url=url,
        data_source="json",
    )

    # ── Update job status ─────────────────────────────────────
    if job_id:
        db.execute(text("""
            UPDATE crawl_jobs
            SET status = 'done',
                completed_at = NOW(),
                result_summary = :summary
            WHERE id = :job_id
        """), {
            "job_id": job_id,
            "summary": json.dumps({
                "variants": variants_processed,
                "item_name": item_name,
                "source": "json",
                "is_39_shop": item_level.get("is_39_shop"),
                "is_super_deal": item_level.get("is_super_deal"),
            }),
        })

    db.commit()

    product_uid = make_item_id_str(shop_code, item_code)
    return {
        "status": "ok",
        "url": url,
        "item_id": item_id,
        "product_uid": product_uid,
        "shop_code": shop_code,
        "item_name": item_name,
        "variants_processed": variants_processed,
        "data_source": "json",
        "is_39_shop": item_level.get("is_39_shop"),
        "is_super_deal": item_level.get("is_super_deal"),
        "review_count": item_level.get("review_count"),
    }


def _process_with_html(
    db,
    html: str,
    soup: BeautifulSoup,
    url: str,
    canonical_url: str,
    shop_code: str,
    item_code: str,
    url_variant_id: str | None,
    job_id: int | None,
) -> dict:
    """
    Process detail page using HTML parsing (fallback strategy).
    """
    # Extract page-level data
    item_name = _extract_item_name_html(soup)
    main_price = _extract_price_html(soup)
    main_image = _extract_image_html(soup)
    shop_name = _extract_shop_name_html(soup)
    coupon_info = _extract_coupon_from_page(html, soup)
    point_info = _extract_point_info_html(soup)
    shipping_info = _extract_shipping_info_html(soup)
    extra_fields = _extract_extra_fields_html(soup)

    # Upsert shop + item
    upsert_shop_sync(db, shop_code=shop_code, shop_name=shop_name)
    item_id = upsert_item_sync(
        db,
        shop_code=shop_code,
        item_code=item_code,
        canonical_url=canonical_url,
        item_name=item_name,
        source="detail",
    )

    # Single product path
    jan_code = _extract_jan_single(soup)
    variant_code = url_variant_id or "default"

    variant_db_id = upsert_variant_sync(
        db,
        item_id=item_id,
        variant_code=variant_code,
        jan_code=jan_code,
    )

    snapshot_data = {
        "price": main_price,
        "image_url": main_image,
        **point_info,
        **coupon_info,
        **shipping_info,
        **extra_fields,
    }
    upsert_snapshot_and_history_sync(
        db, variant_db_id, snapshot_data, source="detail",
    )

    # ── Save detail observation (HTML fallback) ──────────────
    html_item_level = {
        "title": item_name,
        "shop_name": shop_name,
        "shop_url_code": None,
        "price_min": main_price,
        "price_max": main_price,
        "review_count": None,
        "review_rating": None,
        "is_39_shop": False,
        "is_super_deal": False,
        "inventory_type": "single",
        "ancestor_genre_id": None,
        "r_category_id": None,
    }
    _save_detail_observation(
        db,
        item_id=item_id,
        item_level=html_item_level,
        variants_data=[{
            "variant_code": variant_code,
            "jan_code": jan_code,
            "price": main_price,
            "variant_name": item_name,
            "is_sold_out": False,
            "stock_quantity": None,
            "delivery_message": shipping_info.get("shipping_text_raw"),
        }],
        coupon_info=coupon_info,
        detail_url=url,
        data_source="html",
    )

    # Update job status
    if job_id:
        db.execute(text("""
            UPDATE crawl_jobs
            SET status = 'done',
                completed_at = NOW(),
                result_summary = :summary
            WHERE id = :job_id
        """), {
            "job_id": job_id,
            "summary": json.dumps({
                "variants": 1,
                "item_name": item_name,
                "source": "html",
            }),
        })

    db.commit()

    product_uid = make_item_id_str(shop_code, item_code)
    return {
        "status": "ok",
        "url": url,
        "item_id": item_id,
        "product_uid": product_uid,
        "shop_code": shop_code,
        "item_name": item_name,
        "variants_processed": 1,
        "data_source": "html",
    }


# ═══════════════════════════════════════════════════════════════
#  Job dispatcher
# ═══════════════════════════════════════════════════════════════

@celery_app.task(name="app.workers.detail_scraper.process_pending_detail_jobs")
def process_pending_detail_jobs(batch_size: int = 500) -> dict:
    """
    Process pending detail scrape jobs from the crawl_jobs table.
    Picks up pending jobs and triggers individual scrapes.

    For 28.7M products, this must be aggressive:
      - Default batch_size=500 (dispatches 500 jobs per run)
      - Called every 30 seconds by celery beat
      - = 60,000 jobs/hour = 1.44M jobs/day
      - Combined with multiple workers, this covers 28.7M in ~20 days
    """
    db = get_sync_db()
    try:
        # Mark jobs as 'running' to prevent double-dispatch
        result = db.execute(text("""
            SELECT id, target_url
            FROM crawl_jobs
            WHERE job_type = 'detail_scrape'
              AND status = 'pending'
              AND (scheduled_at IS NULL OR scheduled_at <= NOW())
              AND attempts < max_attempts
            ORDER BY created_at ASC
            LIMIT :batch_size
        """), {"batch_size": batch_size})
        jobs = result.mappings().all()

        if not jobs:
            return {"status": "ok", "dispatched": 0, "message": "no pending jobs"}

        # Mark all selected jobs as 'running' before dispatching
        job_ids = [job["id"] for job in jobs]
        if job_ids:
            # Batch update status — use comma-joined IDs for IN clause
            ids_str = ",".join(str(jid) for jid in job_ids)
            db.execute(text(f"""
                UPDATE crawl_jobs
                SET status = 'running', started_at = NOW(), attempts = attempts + 1
                WHERE id IN ({ids_str})
            """))
            db.commit()

        dispatched = 0
        for job in jobs:
            scrape_detail_page.delay(url=job["target_url"], job_id=job["id"])
            dispatched += 1

        logger.info(
            "Dispatched %d detail scrape jobs (batch_size=%d)",
            dispatched, batch_size,
        )
        return {"status": "ok", "dispatched": dispatched}

    except Exception as e:
        logger.error("Job dispatcher error: %s", e, exc_info=True)
        raise
    finally:
        db.close()
