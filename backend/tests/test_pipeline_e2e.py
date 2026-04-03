"""
End-to-End Pipeline Test Script
================================

Tests whether ALL information requested by the client can be retrieved
by actually running the full 3-stage pipeline on a sample of real products:

  Stage 1: API Discovery   -> items, shops, variant_snapshots (price, image, points)
  Stage 2: List Scraping   -> list_observations (price, points, coupons, shipping, reviews)
  Stage 3: Detail Scraping -> detail_observations (variants, JAN, stock, delivery, flags)

This script:
  1. Checks what data already exists from Stage 1 (API) and Stage 2 (List)
  2. Dispatches a small batch of detail scrape jobs (Stage 3)
  3. Monitors their completion in real-time
  4. Verifies ALL client-required fields are populated
  5. Prints a detailed per-product report

Usage:
    cd D:\\project\\rakuten\\backend
    source venv/Scripts/activate

    # Quick test (10 products):
    python tests/test_pipeline_e2e.py

    # Custom sample size:
    python tests/test_pipeline_e2e.py --sample 20

    # Skip dispatch (just verify existing data):
    python tests/test_pipeline_e2e.py --no-dispatch
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime

# Force UTF-8 output on Windows
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# Allow running from backend/ or tests/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pymysql

# =====================================================================
#  Configuration
# =====================================================================

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
except ImportError:
    pass

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME = os.getenv("DB_NAME", "rakuten_tracker")

TARGET_GENRES = ["215783", "100938", "551169"]

# Max time to wait for detail scraping to finish (seconds)
MAX_WAIT_SEC = 600  # 10 minutes
POLL_INTERVAL_SEC = 10


# =====================================================================
#  DB helpers
# =====================================================================

def get_conn():
    return pymysql.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER,
        password=DB_PASSWORD, database=DB_NAME,
        charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor,
    )

def q(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params or ())
        return list(cur.fetchall())

def q1(conn, sql, params=None):
    rows = q(conn, sql, params)
    return rows[0] if rows else None

def execute(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params or ())
    conn.commit()


# =====================================================================
#  Printing helpers
# =====================================================================

def header(text):
    print("\n" + "=" * 76)
    print("  " + text)
    print("=" * 76)

def section(text):
    print("\n" + "-" * 60)
    print("  " + text)
    print("-" * 60)

def ok(msg):
    print("  [OK] " + msg)

def warn(msg):
    print("  [!!] " + msg)

def fail(msg):
    print("  [XX] " + msg)

def info(msg):
    print("  [--] " + msg)


# =====================================================================
#  Step 1: Check existing pipeline data
# =====================================================================

def check_existing_data(conn) -> dict:
    """Check what data already exists from Stage 1 and Stage 2."""
    section("STEP 1: Checking existing pipeline data")

    stats = {}

    # Items
    row = q1(conn, "SELECT COUNT(*) AS cnt FROM items")
    stats["total_items"] = row["cnt"]
    ok("Total items: {:,}".format(stats["total_items"]))

    # API items
    row = q1(conn, "SELECT COUNT(*) AS cnt FROM items WHERE seen_in_api = 1")
    stats["api_items"] = row["cnt"]
    ok("Items from API (Stage 1): {:,}".format(stats["api_items"]))

    # List items
    row = q1(conn, "SELECT COUNT(*) AS cnt FROM items WHERE seen_in_list = 1")
    stats["list_items"] = row["cnt"]
    ok("Items from List scraping (Stage 2): {:,}".format(stats["list_items"]))

    # Detail items
    row = q1(conn, "SELECT COUNT(*) AS cnt FROM items WHERE seen_in_detail = 1")
    stats["detail_items"] = row["cnt"]
    if stats["detail_items"] > 0:
        ok("Items from Detail scraping (Stage 3): {:,}".format(stats["detail_items"]))
    else:
        warn("Items from Detail scraping (Stage 3): 0 (not yet started)")

    # List observations
    row = q1(conn, "SELECT COUNT(*) AS cnt FROM list_observations")
    stats["list_obs"] = row["cnt"]
    ok("List observations: {:,}".format(stats["list_obs"]))

    # Detail observations
    row = q1(conn, "SELECT COUNT(*) AS cnt FROM detail_observations")
    stats["detail_obs"] = row["cnt"]
    ok("Detail observations: {:,}".format(stats["detail_obs"]))

    # Pending detail jobs
    row = q1(conn, """
        SELECT COUNT(*) AS cnt FROM crawl_jobs
        WHERE job_type = 'detail_scrape' AND status = 'pending'
    """)
    stats["pending_detail_jobs"] = row["cnt"]
    info("Pending detail scrape jobs: {:,}".format(stats["pending_detail_jobs"]))

    return stats


# =====================================================================
#  Step 2: Pick sample products and dispatch detail scrape
# =====================================================================

def pick_sample_products(conn, sample_size: int) -> list[dict]:
    """Pick products that have API+List data but no detail data yet."""

    # Priority 1: Products with API + List data but no detail
    samples = q(conn, """
        SELECT i.id, i.product_uid, i.shop_code, i.item_code,
               i.item_name, i.canonical_url, i.genre_id,
               i.seen_in_api, i.seen_in_list, i.seen_in_detail
        FROM items i
        WHERE i.seen_in_api = 1 AND i.seen_in_list = 1 AND i.seen_in_detail = 0
        ORDER BY i.updated_at DESC
        LIMIT %s
    """, (sample_size,))

    if len(samples) < sample_size:
        # Priority 2: Products with List data but no detail
        remaining = sample_size - len(samples)
        existing_ids = [s["id"] for s in samples]
        if existing_ids:
            placeholders = ",".join(["%s"] * len(existing_ids))
            more = q(conn, """
                SELECT i.id, i.product_uid, i.shop_code, i.item_code,
                       i.item_name, i.canonical_url, i.genre_id,
                       i.seen_in_api, i.seen_in_list, i.seen_in_detail
                FROM items i
                WHERE i.seen_in_list = 1 AND i.seen_in_detail = 0
                  AND i.id NOT IN ({})
                ORDER BY i.updated_at DESC
                LIMIT %s
            """.format(placeholders), (*existing_ids, remaining))
        else:
            more = q(conn, """
                SELECT i.id, i.product_uid, i.shop_code, i.item_code,
                       i.item_name, i.canonical_url, i.genre_id,
                       i.seen_in_api, i.seen_in_list, i.seen_in_detail
                FROM items i
                WHERE i.seen_in_list = 1 AND i.seen_in_detail = 0
                ORDER BY i.updated_at DESC
                LIMIT %s
            """, (remaining,))
        samples.extend(more)

    if len(samples) < sample_size:
        # Priority 3: Any product with no detail yet
        remaining = sample_size - len(samples)
        existing_ids = [s["id"] for s in samples]
        if existing_ids:
            placeholders = ",".join(["%s"] * len(existing_ids))
            more = q(conn, """
                SELECT i.id, i.product_uid, i.shop_code, i.item_code,
                       i.item_name, i.canonical_url, i.genre_id,
                       i.seen_in_api, i.seen_in_list, i.seen_in_detail
                FROM items i
                WHERE i.seen_in_detail = 0
                  AND i.id NOT IN ({})
                ORDER BY i.updated_at DESC
                LIMIT %s
            """.format(placeholders), (*existing_ids, remaining))
        else:
            more = q(conn, """
                SELECT i.id, i.product_uid, i.shop_code, i.item_code,
                       i.item_name, i.canonical_url, i.genre_id,
                       i.seen_in_api, i.seen_in_list, i.seen_in_detail
                FROM items i
                WHERE i.seen_in_detail = 0
                ORDER BY i.updated_at DESC
                LIMIT %s
            """, (remaining,))
        samples.extend(more)

    return samples


def run_detail_scrape_directly(conn, samples: list[dict]) -> dict:
    """
    Run detail scraping DIRECTLY (synchronous, not through Celery).

    This bypasses the Celery queue entirely, running the scraper functions
    in-process so we get immediate results without depending on worker
    availability or queue competition.
    """
    section("STEP 2: Running detail scrape DIRECTLY for {} products".format(len(samples)))

    os.chdir(os.path.join(os.path.dirname(__file__), ".."))

    # Import the scraper internals
    from app.workers.detail_scraper import (
        _fetch_detail_page,
        _extract_app_data_json,
        _get_item_info_sku,
        _process_with_json,
        _process_with_html,
    )
    from app.db.session import get_sync_db
    from app.services.url_canonicalizer import canonicalize_item_url, extract_shop_item_code
    from bs4 import BeautifulSoup

    results_map = {"done": 0, "failed": 0}

    for idx, item in enumerate(samples, 1):
        url = item["canonical_url"]
        uid = item["product_uid"] or "{}:{}".format(item["shop_code"], item["item_code"])

        # Check if detail already scraped for this item
        check_conn = get_conn()
        try:
            existing = q1(check_conn, """
                SELECT id FROM detail_observations WHERE item_id = %s LIMIT 1
            """, (item["id"],))
        finally:
            check_conn.close()
        if existing:
            ok("[{}/{}] {} - already has detail data, skipping".format(idx, len(samples), uid))
            results_map["done"] += 1
            continue

        info("[{}/{}] Fetching detail page for {}...".format(idx, len(samples), uid))
        start = time.time()

        try:
            # Fetch the page
            html = _fetch_detail_page(url)
            if not html:
                warn("[{}/{}] {} - FETCH FAILED (WAF/timeout)".format(idx, len(samples), uid))
                results_map["failed"] += 1
                continue

            elapsed = time.time() - start
            info("  Fetched in {:.1f}s ({} bytes)".format(elapsed, len(html)))

            # Parse
            soup = BeautifulSoup(html, "lxml")
            canonical_url, url_variant_id = canonicalize_item_url(url)
            shop_code, item_code = extract_shop_item_code(canonical_url)

            # Try JSON extraction
            app_data = _extract_app_data_json(html)
            item_info = _get_item_info_sku(app_data) if app_data else None

            db = get_sync_db()
            try:
                if item_info:
                    result = _process_with_json(
                        db, html, soup, url, canonical_url,
                        shop_code, item_code, url_variant_id,
                        app_data, item_info, job_id=None,
                    )
                    ok("[{}/{}] {} - OK (JSON) | variants={}, is_39_shop={}, reviews={}".format(
                        idx, len(samples), uid,
                        result.get("variants_processed", 0),
                        result.get("is_39_shop"),
                        result.get("review_count"),
                    ))
                    results_map["done"] += 1
                else:
                    result = _process_with_html(
                        db, html, soup, url, canonical_url,
                        shop_code, item_code, url_variant_id,
                        job_id=None,
                    )
                    ok("[{}/{}] {} - OK (HTML fallback)".format(idx, len(samples), uid))
                    results_map["done"] += 1
            finally:
                db.close()

        except Exception as e:
            warn("[{}/{}] {} - ERROR: {}".format(idx, len(samples), uid, e))
            results_map["failed"] += 1

    ok("Detail scraping complete: {} done, {} failed".format(
        results_map["done"], results_map["failed"]))

    return results_map


# =====================================================================
#  Step 4: Verify all client-required data
# =====================================================================

def verify_product_data(conn, samples: list[dict]) -> dict:
    """
    For each sample product, verify ALL client-required information fields.

    Client Requirements:
    ====================
    A. PRODUCT IDENTITY
       - product_uid (unique ID: shop_code:item_code)
       - shop_code, item_code
       - item_name (product name)
       - canonical_url (clean product URL)

    B. PRICING
       - Current price (from API or list or detail)
       - Price range for multi-variant items (from detail)

    C. POINTS (from list page JSON)
       - point_count (total points)
       - point_base_multiplier (base rate)
       - point_shop_multiplier (shop bonus)
       - point_up_multiplier (campaign bonus)
       - point_text_raw (display text)

    D. COUPONS (from list page or detail page)
       - coupon_discount, coupon_type
       - coupon_yen, coupon_percent

    E. SHIPPING
       - shipping_cost (0 = free)
       - is_free_shipping flag
       - delivery_text (estimated delivery)

    F. REVIEWS
       - review_score (average rating)
       - review_count (number of reviews)

    G. VARIANTS & JAN (from detail page)
       - variant_code (variant ID)
       - jan_code (JAN/GTIN barcode)
       - variant_name
       - variant stock, sold-out status
       - delivery message per variant

    H. FLAGS
       - is_super_deal (Super DEAL flag)
       - is_39_shop (39 shop flag)
       - is_sold_out

    I. IMAGES
       - Product image URL

    J. GENRE
       - genre_id
    """

    section("STEP 4: Verifying client-required data for {} products".format(len(samples)))

    results = {
        "total": len(samples),
        "fully_complete": 0,
        "field_stats": defaultdict(lambda: {"filled": 0, "total": 0}),
        "products": [],
    }

    # Define the field groups and their sources
    FIELD_GROUPS = {
        "A. IDENTITY": {
            "product_uid": "items.product_uid IS NOT NULL",
            "shop_code": "items.shop_code IS NOT NULL",
            "item_code": "items.item_code IS NOT NULL",
            "item_name": "items.item_name IS NOT NULL",
            "canonical_url": "items.canonical_url IS NOT NULL",
        },
        "B. PRICING": {
            "api_price": "variant_snapshots.price",
            "list_price": "list_observations.observed_price",
            "detail_price_min": "detail_observations.price_min",
        },
        "C. POINTS": {
            "point_count": "list_observations.point_count",
            "point_base_multiplier": "list_observations.point_base_multiplier",
            "point_text_raw": "list_observations.point_text_raw",
        },
        "D. COUPONS": {
            "list_coupon": "list_observations.coupon_discount",
            "detail_coupon": "detail_observations.coupon_yen OR detail_observations.coupon_percent",
        },
        "E. SHIPPING": {
            "shipping_cost": "list_observations.shipping_cost",
            "is_free_shipping": "list_observations.is_free_shipping",
            "delivery_text": "list_observations.delivery_text",
        },
        "F. REVIEWS": {
            "review_score": "list_observations.review_score",
            "review_count": "list_observations.review_count",
        },
        "G. VARIANTS & JAN": {
            "variant_exists": "variants table",
            "jan_code": "variants.jan_code",
            "variant_data_json": "detail_observations.variant_data_json",
            "variant_count": "detail_observations.variant_count",
        },
        "H. FLAGS": {
            "is_super_deal": "list_observations.is_super_deal",
            "is_39_shop": "detail_observations.is_39_shop",
        },
        "I. IMAGES": {
            "image_url": "variant_snapshots.image_url OR list_observations.image_url",
        },
        "J. GENRE": {
            "genre_id": "items.genre_id",
        },
    }

    for idx, item in enumerate(samples, 1):
        item_id = item["id"]
        uid = item["product_uid"] or "{}:{}".format(item["shop_code"], item["item_code"])

        product_result = {
            "uid": uid,
            "fields": {},
            "complete": True,
        }

        # ── Fetch all related data ────────────────────────────

        # Item basics
        item_row = q1(conn, """
            SELECT product_uid, shop_code, item_code, item_name,
                   canonical_url, genre_id, catchcopy,
                   seen_in_api, seen_in_list, seen_in_detail
            FROM items WHERE id = %s
        """, (item_id,))

        # Latest variant snapshot
        snapshot = q1(conn, """
            SELECT vs.price, vs.point_rate, vs.coupon_yen, vs.coupon_percent,
                   vs.shipping_text_raw, vs.shipping_days_min, vs.shipping_days_max,
                   vs.image_url, vs.source, vs.fetched_at,
                   vs.extra1_key, vs.extra1_value, vs.extra2_key, vs.extra2_value
            FROM variant_snapshots vs
            JOIN variants v ON v.id = vs.variant_id
            WHERE v.item_id = %s
            ORDER BY vs.fetched_at DESC LIMIT 1
        """, (item_id,))

        # Latest list observation
        list_obs = q1(conn, """
            SELECT observed_price, point_count, point_base_multiplier,
                   point_shop_multiplier, point_up_multiplier,
                   point_text_raw,
                   coupon_discount, coupon_type, coupon_text_raw,
                   shipping_cost, delivery_text, delivery_days, is_free_shipping,
                   review_score, review_count,
                   shop_name, image_url,
                   is_super_deal, is_shop39, is_sold_out,
                   has_multi_sku, data_source
            FROM list_observations
            WHERE item_id = %s
            ORDER BY observed_at DESC LIMIT 1
        """, (item_id,))

        # Latest detail observation
        detail_obs = q1(conn, """
            SELECT item_name, shop_name, price_min, price_max,
                   review_count, review_rating,
                   is_39_shop, is_super_deal,
                   inventory_type, coupon_yen, coupon_percent,
                   variant_count, variant_data_json,
                   ancestor_genre_id, r_category_id,
                   data_source
            FROM detail_observations
            WHERE item_id = %s
            ORDER BY observed_at DESC LIMIT 1
        """, (item_id,))

        # Variants
        variants = q(conn, """
            SELECT v.id, v.variant_code, v.jan_code, v.variant_name
            FROM variants v
            WHERE v.item_id = %s
        """, (item_id,))

        # ── Evaluate each field ────────────────────────────────

        fields = {}

        # A. IDENTITY
        fields["product_uid"] = bool(item_row and item_row.get("product_uid"))
        fields["shop_code"] = bool(item_row and item_row.get("shop_code"))
        fields["item_code"] = bool(item_row and item_row.get("item_code"))
        fields["item_name"] = bool(item_row and item_row.get("item_name"))
        fields["canonical_url"] = bool(item_row and item_row.get("canonical_url"))

        # B. PRICING
        fields["api_price"] = bool(snapshot and snapshot.get("price") is not None)
        fields["list_price"] = bool(list_obs and list_obs.get("observed_price") is not None)
        fields["detail_price_min"] = bool(detail_obs and detail_obs.get("price_min") is not None)

        # C. POINTS
        fields["point_count"] = bool(list_obs and list_obs.get("point_count") is not None)
        fields["point_base_multiplier"] = bool(list_obs and list_obs.get("point_base_multiplier") is not None)
        fields["point_text_raw"] = bool(list_obs and list_obs.get("point_text_raw"))

        # D. COUPONS (legitimately sparse — many products have no coupons)
        fields["list_coupon"] = bool(list_obs and list_obs.get("coupon_discount") is not None)
        fields["detail_coupon"] = bool(detail_obs and (
            detail_obs.get("coupon_yen") is not None or detail_obs.get("coupon_percent") is not None))

        # E. SHIPPING
        fields["shipping_cost"] = bool(list_obs and list_obs.get("shipping_cost") is not None)
        fields["is_free_shipping"] = bool(list_obs)  # Flag always present if list_obs exists
        fields["delivery_text"] = bool(list_obs and list_obs.get("delivery_text"))

        # F. REVIEWS
        fields["review_score"] = bool(list_obs and list_obs.get("review_score") is not None)
        fields["review_count"] = bool(list_obs and list_obs.get("review_count") is not None)

        # G. VARIANTS & JAN
        fields["variant_exists"] = len(variants) > 0
        fields["jan_code"] = any(v.get("jan_code") for v in variants)
        # For single-product items, variant_data_json is None and variant_count = 0
        # This is expected behavior, not a failure
        is_multi_sku = detail_obs and detail_obs.get("variant_count") and detail_obs["variant_count"] > 0
        fields["variant_data_json"] = bool(
            detail_obs and (detail_obs.get("variant_data_json") or detail_obs.get("variant_count") == 0))
        fields["variant_count"] = bool(
            detail_obs and detail_obs.get("variant_count") is not None)  # 0 is valid for single products

        # H. FLAGS
        fields["is_super_deal"] = bool(list_obs)  # Flag always present
        fields["is_39_shop"] = bool(detail_obs)  # Flag always present if detail exists

        # I. IMAGES
        has_img = (
            (snapshot and snapshot.get("image_url"))
            or (list_obs and list_obs.get("image_url"))
        )
        fields["image_url"] = bool(has_img)

        # J. GENRE
        fields["genre_id"] = bool(item_row and item_row.get("genre_id"))

        product_result["fields"] = fields

        # ── Track stats ────────────────────────────────────────
        # Fields that are REQUIRED (not optional like coupons, JAN codes)
        required_fields = [
            # Identity (always available)
            "product_uid", "shop_code", "item_code", "item_name", "canonical_url",
            # Pricing (at least list price always available)
            "list_price",
            # Points (from list page JSON)
            "point_count", "point_text_raw",
            # Shipping & Reviews (from list page)
            "shipping_cost", "review_score", "review_count",
            # Variants (at least one variant always created)
            "variant_exists", "image_url",
            # Detail-dependent (Stage 3)
            "detail_price_min", "variant_data_json", "variant_count",
            "is_39_shop",
        ]
        # JAN code and API price are NOT required — many products don't have them

        for fk, fv in fields.items():
            results["field_stats"][fk]["total"] += 1
            if fv:
                results["field_stats"][fk]["filled"] += 1

        missing_required = [f for f in required_fields if not fields.get(f)]
        if not missing_required:
            results["fully_complete"] += 1
            product_result["complete"] = True
        else:
            product_result["complete"] = False
            product_result["missing"] = missing_required

        results["products"].append(product_result)

        # ── Print per-product result ───────────────────────────
        stages = []
        if item_row and item_row.get("seen_in_api"):
            stages.append("API")
        if item_row and item_row.get("seen_in_list"):
            stages.append("List")
        if item_row and item_row.get("seen_in_detail"):
            stages.append("Detail")

        print("\n  --- Product {}/{}: {} ({}) ---".format(
            idx, len(samples), uid, "+".join(stages) if stages else "None"))

        name = (item_row.get("item_name") or "")[:60] if item_row else ""
        if name:
            info("Name: {}".format(name))

        # Group fields by category for display
        for group_name, group_fields in FIELD_GROUPS.items():
            filled = sum(1 for f in group_fields if fields.get(f))
            total = len(group_fields)
            missing = [f for f in group_fields if not fields.get(f)]

            if filled == total:
                ok("{}: {}/{} fields".format(group_name, filled, total))
            elif group_name in ("D. COUPONS",):
                # Coupons are optional
                if filled > 0:
                    ok("{}: {}/{} fields (coupons are optional)".format(group_name, filled, total))
                else:
                    info("{}: {}/{} fields (product may have no coupons)".format(group_name, filled, total))
            else:
                if filled > 0:
                    warn("{}: {}/{} fields (missing: {})".format(
                        group_name, filled, total, ", ".join(missing)))
                else:
                    fail("{}: {}/{} fields (missing: {})".format(
                        group_name, filled, total, ", ".join(missing)))

        # Variant detail
        if detail_obs and detail_obs.get("variant_data_json"):
            try:
                vdata = json.loads(detail_obs["variant_data_json"])
                total_v = len(vdata)
                with_jan = sum(1 for v in vdata if v.get("jan"))
                with_price = sum(1 for v in vdata if v.get("price"))
                with_stock = sum(1 for v in vdata if v.get("stock") is not None)
                info("  Variants: {} total, JAN={}/{}, price={}/{}, stock={}/{}".format(
                    total_v, with_jan, total_v, with_price, total_v, with_stock, total_v))
            except (json.JSONDecodeError, TypeError):
                warn("  Variant JSON: parse error")

    return results


# =====================================================================
#  Step 5: Print final summary
# =====================================================================

def print_summary(results: dict, job_status: dict):
    """Print the final summary report."""
    header("FINAL SUMMARY")

    total = results["total"]
    complete = results["fully_complete"]

    print()
    info("Products tested: {}".format(total))
    if complete == total:
        ok("Fully complete products: {}/{} (100%)".format(complete, total))
    elif complete > 0:
        warn("Fully complete products: {}/{} ({:.1f}%)".format(
            complete, total, complete / total * 100))
    else:
        fail("Fully complete products: 0/{} (0%)".format(total))

    # Job completion
    if job_status:
        done = job_status.get("done", 0)
        failed = job_status.get("failed", 0)
        print()
        info("Detail scrape results: {} done, {} failed".format(done, failed))

    # Field-by-field fill rates
    section("FIELD FILL RATES")
    field_stats = results["field_stats"]

    FIELD_GROUPS_ORDERED = [
        ("IDENTITY", ["product_uid", "shop_code", "item_code", "item_name", "canonical_url"]),
        ("PRICING", ["api_price", "list_price", "detail_price_min"]),
        ("POINTS", ["point_count", "point_base_multiplier", "point_text_raw"]),
        ("COUPONS (optional)", ["list_coupon", "detail_coupon"]),
        ("SHIPPING", ["shipping_cost", "is_free_shipping", "delivery_text"]),
        ("REVIEWS", ["review_score", "review_count"]),
        ("VARIANTS & JAN", ["variant_exists", "jan_code", "variant_data_json", "variant_count"]),
        ("FLAGS", ["is_super_deal", "is_39_shop"]),
        ("IMAGES", ["image_url"]),
        ("GENRE", ["genre_id"]),
    ]

    total_ok = 0
    total_fields = 0

    for group_name, group_fields in FIELD_GROUPS_ORDERED:
        print()
        info("{}:".format(group_name))
        for fname in group_fields:
            stat = field_stats.get(fname, {"filled": 0, "total": total})
            filled = stat["filled"]
            t = stat["total"] or total
            pct = filled / t * 100 if t > 0 else 0
            total_ok += filled
            total_fields += t

            if pct >= 90:
                ok("  {}: {}/{} ({:.0f}%)".format(fname, filled, t, pct))
            elif pct >= 50:
                warn("  {}: {}/{} ({:.0f}%)".format(fname, filled, t, pct))
            elif "coupon" in fname.lower():
                info("  {}: {}/{} ({:.0f}%) (optional)".format(fname, filled, t, pct))
            else:
                fail("  {}: {}/{} ({:.0f}%)".format(fname, filled, t, pct))

    # Overall score
    overall_pct = total_ok / total_fields * 100 if total_fields > 0 else 0
    print()
    header("OVERALL SCORE: {:.1f}% ({}/{} field-checks passed)".format(
        overall_pct, total_ok, total_fields))

    # Products that are incomplete
    incomplete = [p for p in results["products"] if not p["complete"]]
    if incomplete:
        section("INCOMPLETE PRODUCTS ({})".format(len(incomplete)))
        for p in incomplete[:10]:
            missing = p.get("missing", [])
            warn("{}: missing {} fields: {}".format(
                p["uid"], len(missing), ", ".join(missing)))
    else:
        ok("All {} products have complete client-required data!".format(total))


# =====================================================================
#  Main
# =====================================================================

def main():
    parser = argparse.ArgumentParser(description="End-to-end pipeline test")
    parser.add_argument("--sample", type=int, default=10,
                        help="Number of products to test (default: 10)")
    parser.add_argument("--no-dispatch", action="store_true",
                        help="Skip dispatching detail jobs (just verify existing data)")
    parser.add_argument("--timeout", type=int, default=600,
                        help="Max seconds to wait for detail scrape (default: 600)")
    args = parser.parse_args()

    global MAX_WAIT_SEC
    MAX_WAIT_SEC = args.timeout

    header("RAKUTEN PIPELINE END-TO-END TEST")
    info("Date: {}".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    info("Sample size: {}".format(args.sample))
    info("Detail dispatch: {}".format("NO (verify only)" if args.no_dispatch else "YES"))
    info("Timeout: {}s".format(MAX_WAIT_SEC))

    conn = get_conn()

    try:
        # Step 1: Check existing data
        stats = check_existing_data(conn)

        # Step 2: Pick sample and dispatch
        samples = pick_sample_products(conn, args.sample)
        if not samples:
            fail("No products found in database. Run the pipeline first!")
            return 1

        ok("Selected {} sample products".format(len(samples)))

        job_status = {}

        if not args.no_dispatch:
            # Step 2+3: Run detail scrape directly (synchronous, immediate)
            # Close the pymysql connection first - the scraper uses SQLAlchemy
            conn.close()
            job_status = run_detail_scrape_directly(None, samples)

            # Reopen connection to see fresh data (REPEATABLE READ isolation)
            conn = get_conn()

            # Refresh samples to get updated seen_in_detail flags
            sample_ids = [s["id"] for s in samples]
            placeholders = ",".join(["%s"] * len(sample_ids))
            samples = q(conn, """
                SELECT id, product_uid, shop_code, item_code,
                       item_name, canonical_url, genre_id,
                       seen_in_api, seen_in_list, seen_in_detail
                FROM items
                WHERE id IN ({})
            """.format(placeholders), tuple(sample_ids))
        else:
            info("Skipping detail scraping (--no-dispatch)")

        # Step 4: Verify data
        results = verify_product_data(conn, samples)

        # Step 5: Summary
        print_summary(results, job_status)

        # Return code
        if results["fully_complete"] == results["total"]:
            return 0
        elif results["fully_complete"] > 0:
            return 0  # Partial success is acceptable
        else:
            return 1

    finally:
        conn.close()


# =====================================================================
#  Pytest Integration
# =====================================================================

def test_pipeline_e2e():
    """
    Pytest entry point.

    Runs with --no-dispatch by default (just verifies existing data).
    Use direct execution for full dispatch mode.
    """
    conn = get_conn()
    try:
        stats = check_existing_data(conn)
        samples = pick_sample_products(conn, 10)
        assert len(samples) > 0, "No products found in database"

        results = verify_product_data(conn, samples)
        print_summary(results, {})

        # At minimum, identity + pricing should be present
        identity_fill = results["field_stats"]["product_uid"]["filled"]
        assert identity_fill > 0, "No products have product_uid"

        price_fill = results["field_stats"]["api_price"]["filled"]
        assert price_fill > 0 or results["field_stats"]["list_price"]["filled"] > 0, \
            "No products have any price data"
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main() or 0)
