"""
Data Completeness Verification Script
======================================

Connects to the LIVE database and checks whether all the information
requested by the client is being correctly collected across the 3-stage pipeline:

  Stage 1 (API Discovery):
    - items table: shop_code, item_code, product_uid, item_name, catchcopy,
      genre_id, canonical_url, api_item_code, seen_in_api
    - variant_snapshots: price, point_rate, image_url, shipping_text_raw
    - extra fields: review_count, review_average

  Stage 2 (List Page Scraping):
    - list_observations: observed_price, point breakdown (base/shop/up/deal/item),
      coupon (discount, type), shipping (cost, delivery_text, delivery_days,
      is_free_shipping), review (score, count), shop info, flags
      (is_super_deal, is_shop39, is_sold_out), image_url
    - items table: seen_in_list

  Stage 3 (Detail Page Scraping):
    - detail_observations: item_name, shop_name, price_min, price_max,
      review_count, review_rating, is_39_shop, is_super_deal,
      inventory_type, coupon_yen, coupon_percent, variant_count,
      variant_data_json (JAN, price, name, soldOut, stock, deliveryMsg),
      ancestor_genre_id, r_category_id
    - variants: variant_code, jan_code, variant_name, selector_values
    - variant_snapshots (enriched): shipping_days_min, shipping_days_max,
      coupon_yen, coupon_percent
    - items table: seen_in_detail

Usage:
    cd D:\\project\\rakuten\\backend
    source vnev/Scripts/activate
    python tests/test_data_completeness.py

    Or with pytest:
    python -m pytest tests/test_data_completeness.py -v -s
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta

# Allow running directly (not just via pytest)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pymysql

# Force UTF-8 output on Windows
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ═══════════════════════════════════════════════════════════════
#  Configuration
# ═══════════════════════════════════════════════════════════════

# Try to load from .env via dotenv, fall back to defaults
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

# The 3 target genres
TARGET_GENRES = ["215783", "100938", "551169"]

# Thresholds for pass/fail
MIN_ITEMS_PER_GENRE = 100  # At minimum, expect 100+ items after a run
FIELD_FILL_RATE_WARN = 0.5  # Warn if <50% of items have a field
FIELD_FILL_RATE_FAIL = 0.1  # Fail if <10% of items have a field


# ═══════════════════════════════════════════════════════════════
#  Database Connection
# ═══════════════════════════════════════════════════════════════

def get_connection():
    """Get a pymysql connection to the live database."""
    return pymysql.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )


def query(conn, sql: str, params: dict | None = None) -> list[dict]:
    """Execute a SQL query and return results as list of dicts."""
    with conn.cursor() as cur:
        cur.execute(sql, params or {})
        return cur.fetchall()


def query_one(conn, sql: str, params: dict | None = None) -> dict | None:
    """Execute a SQL query and return the first result."""
    rows = query(conn, sql, params)
    return rows[0] if rows else None


# ═══════════════════════════════════════════════════════════════
#  Report Helpers
# ═══════════════════════════════════════════════════════════════

class CompletionReport:
    """Accumulates test results and produces a final report."""

    def __init__(self):
        self.sections: list[dict] = []
        self.passes = 0
        self.warnings = 0
        self.failures = 0

    def add(self, category: str, check: str, status: str, detail: str = ""):
        icon = {"PASS": "[OK]", "WARN": "[!!]", "FAIL": "[XX]"}.get(status, "[??]")
        self.sections.append({
            "category": category,
            "check": check,
            "status": status,
            "detail": detail,
            "icon": icon,
        })
        if status == "PASS":
            self.passes += 1
        elif status == "WARN":
            self.warnings += 1
        else:
            self.failures += 1

    def print_report(self):
        print("\n" + "=" * 80)
        print("  RAKUTEN DATA COMPLETENESS VERIFICATION REPORT")
        print("  Generated: {}".format(datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        print("=" * 80)

        current_cat = None
        for s in self.sections:
            if s["category"] != current_cat:
                current_cat = s["category"]
                print("\n" + "-" * 60)
                print("  " + current_cat)
                print("-" * 60)
            detail_str = "  ({})".format(s["detail"]) if s["detail"] else ""
            print("  {} {}{}".format(s["icon"], s["check"], detail_str))

        print("\n" + "=" * 80)
        print("  SUMMARY: {} passed, {} warnings, {} failed".format(
            self.passes, self.warnings, self.failures))
        total = self.passes + self.warnings + self.failures
        if total > 0:
            score = (self.passes / total) * 100
            print("  SCORE: {:.1f}%".format(score))
        if self.failures == 0:
            print("  RESULT: [OK] ALL CHECKS PASSED")
        else:
            print("  RESULT: [XX] SOME CHECKS FAILED")
        print("=" * 80 + "\n")


# ═══════════════════════════════════════════════════════════════
#  Check Functions
# ═══════════════════════════════════════════════════════════════

def check_overall_counts(conn, report: CompletionReport):
    """Check total item/shop/variant counts."""
    cat = "1. OVERALL DATABASE COUNTS"

    # Items
    row = query_one(conn, "SELECT COUNT(*) AS cnt FROM items")
    total_items = row["cnt"]
    report.add(cat, f"Total items in database: {total_items:,}",
               "PASS" if total_items > 0 else "FAIL")

    # Shops
    row = query_one(conn, "SELECT COUNT(*) AS cnt FROM shops")
    total_shops = row["cnt"]
    report.add(cat, f"Total shops in database: {total_shops:,}",
               "PASS" if total_shops > 0 else "FAIL")

    # Variants
    row = query_one(conn, "SELECT COUNT(*) AS cnt FROM variants")
    total_variants = row["cnt"]
    report.add(cat, f"Total variants in database: {total_variants:,}",
               "PASS" if total_variants > 0 else "FAIL")

    # Snapshots
    row = query_one(conn, "SELECT COUNT(*) AS cnt FROM variant_snapshots")
    total_snapshots = row["cnt"]
    report.add(cat, f"Total variant snapshots: {total_snapshots:,}",
               "PASS" if total_snapshots > 0 else "FAIL")

    # Daily history
    row = query_one(conn, "SELECT COUNT(*) AS cnt FROM variant_daily_history")
    total_history = row["cnt"]
    report.add(cat, f"Total daily history records: {total_history:,}",
               "PASS" if total_history > 0 else "FAIL")

    return total_items


def check_stage1_api_coverage(conn, report: CompletionReport):
    """Check Stage 1: API Discovery coverage."""
    cat = "2. STAGE 1 — API DISCOVERY"

    # Items seen by API
    row = query_one(conn, "SELECT COUNT(*) AS cnt FROM items WHERE seen_in_api = 1")
    api_items = row["cnt"]
    report.add(cat, f"Items discovered via API: {api_items:,}",
               "PASS" if api_items > 0 else "FAIL")

    # Per-genre breakdown
    for gid in TARGET_GENRES:
        row = query_one(conn,
            "SELECT COUNT(*) AS cnt FROM items WHERE seen_in_api = 1 AND genre_id = %s",
            (gid,))
        cnt = row["cnt"]
        status = "PASS" if cnt >= MIN_ITEMS_PER_GENRE else "WARN" if cnt > 0 else "FAIL"
        report.add(cat, f"  Genre {gid}: {cnt:,} items from API", status)

    # Check API-sourced snapshots have price
    row = query_one(conn, """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN vs.price IS NOT NULL THEN 1 ELSE 0 END) AS with_price,
               SUM(CASE WHEN vs.image_url IS NOT NULL THEN 1 ELSE 0 END) AS with_image,
               SUM(CASE WHEN vs.point_rate IS NOT NULL THEN 1 ELSE 0 END) AS with_points
        FROM variant_snapshots vs
        WHERE vs.source = 'api'
    """)
    if row and row["total"] > 0:
        t = row["total"]
        report.add(cat, f"API snapshots with price: {row['with_price']:,}/{t:,} "
                   f"({row['with_price']/t*100:.1f}%)",
                   "PASS" if row["with_price"] / t > 0.9 else "WARN")
        report.add(cat, f"API snapshots with image: {row['with_image']:,}/{t:,} "
                   f"({row['with_image']/t*100:.1f}%)",
                   "PASS" if row["with_image"] / t > 0.8 else "WARN")
        report.add(cat, f"API snapshots with point_rate: {row['with_points']:,}/{t:,} "
                   f"({row['with_points']/t*100:.1f}%)",
                   "PASS" if row["with_points"] / t > 0.5 else "WARN")
    else:
        report.add(cat, "No API-sourced snapshots found", "FAIL")

    # Items with product_uid
    row = query_one(conn,
        "SELECT COUNT(*) AS cnt FROM items WHERE product_uid IS NOT NULL AND product_uid != ''")
    report.add(cat, f"Items with product_uid: {row['cnt']:,}",
               "PASS" if row["cnt"] > 0 else "FAIL")

    # Items with item_name from API
    row = query_one(conn,
        "SELECT COUNT(*) AS cnt FROM items WHERE seen_in_api = 1 AND item_name IS NOT NULL")
    report.add(cat, f"API items with item_name: {row['cnt']:,}",
               "PASS" if row["cnt"] > 0 else "FAIL")


def check_stage2_list_coverage(conn, report: CompletionReport):
    """Check Stage 2: List Page Scraping coverage."""
    cat = "3. STAGE 2 — LIST PAGE SCRAPING"

    # Items seen in list
    row = query_one(conn, "SELECT COUNT(*) AS cnt FROM items WHERE seen_in_list = 1")
    list_items = row["cnt"]
    report.add(cat, f"Items discovered via list pages: {list_items:,}",
               "PASS" if list_items > 0 else "FAIL")

    # Total list observations
    row = query_one(conn, "SELECT COUNT(*) AS cnt FROM list_observations")
    total_obs = row["cnt"]
    report.add(cat, f"Total list observations: {total_obs:,}",
               "PASS" if total_obs > 0 else "FAIL")

    if total_obs == 0:
        report.add(cat, "Skipping field checks (no list observations)", "WARN")
        return

    # Field fill rates for list_observations
    field_checks = [
        ("observed_price", "Price"),
        ("point_count", "Point count"),
        ("point_base_multiplier", "Point base multiplier"),
        ("point_shop_multiplier", "Point shop multiplier"),
        ("point_up_multiplier", "Point UP multiplier"),
        ("point_text_raw", "Point text (raw)"),
        ("coupon_discount", "Coupon discount"),
        ("coupon_type", "Coupon type"),
        ("shipping_cost", "Shipping cost"),
        ("delivery_text", "Delivery text"),
        ("is_free_shipping", "Free shipping flag (non-zero)"),
        ("review_score", "Review score"),
        ("review_count", "Review count"),
        ("shop_code", "Shop code"),
        ("shop_name", "Shop name"),
        ("item_name", "Item name"),
        ("image_url", "Image URL"),
        ("is_super_deal", "Super DEAL flag (non-zero)"),
        ("is_sold_out", "Sold out flag (non-zero)"),
        ("has_multi_sku", "Multi-SKU flag (non-zero)"),
    ]

    for col, label in field_checks:
        if col in ("is_free_shipping", "is_super_deal", "is_sold_out", "has_multi_sku"):
            # For boolean flags, check non-zero (these are expected to be sparse)
            row = query_one(conn, f"""
                SELECT SUM(CASE WHEN {col} != 0 THEN 1 ELSE 0 END) AS filled
                FROM list_observations
            """)
            filled = row["filled"] or 0
            # These flags are OK to be sparse — just report presence
            report.add(cat, f"  {label}: {filled:,}/{total_obs:,} "
                       f"({filled/total_obs*100:.1f}%)",
                       "PASS")
        else:
            row = query_one(conn, f"""
                SELECT SUM(CASE WHEN {col} IS NOT NULL THEN 1 ELSE 0 END) AS filled
                FROM list_observations
            """)
            filled = row["filled"] or 0
            rate = filled / total_obs
            if rate >= FIELD_FILL_RATE_WARN:
                status = "PASS"
            elif rate >= FIELD_FILL_RATE_FAIL:
                status = "WARN"
            else:
                # Coupons/delivery are legitimately sparse
                if col in ("coupon_discount", "coupon_type", "delivery_text",
                           "delivery_days", "shipping_cost"):
                    status = "PASS"  # These are optional for many products
                else:
                    status = "FAIL"
            report.add(cat, f"  {label}: {filled:,}/{total_obs:,} "
                       f"({rate*100:.1f}%)", status)

    # JSON vs HTML data source breakdown
    rows = query(conn, """
        SELECT data_source, COUNT(*) AS cnt
        FROM list_observations
        GROUP BY data_source
        ORDER BY cnt DESC
    """)
    for r in rows:
        report.add(cat, f"  Data source '{r['data_source']}': {r['cnt']:,} observations",
                   "PASS")


def check_stage3_detail_coverage(conn, report: CompletionReport):
    """Check Stage 3: Detail Page Scraping coverage."""
    cat = "4. STAGE 3 — DETAIL PAGE SCRAPING"

    # Items seen in detail
    row = query_one(conn, "SELECT COUNT(*) AS cnt FROM items WHERE seen_in_detail = 1")
    detail_items = row["cnt"]
    report.add(cat, f"Items scraped via detail pages: {detail_items:,}",
               "PASS" if detail_items > 0 else "FAIL")

    # Total detail observations
    row = query_one(conn, "SELECT COUNT(*) AS cnt FROM detail_observations")
    total_obs = row["cnt"]
    report.add(cat, f"Total detail observations: {total_obs:,}",
               "PASS" if total_obs > 0 else "FAIL")

    if total_obs == 0:
        report.add(cat, "Skipping field checks (no detail observations)", "WARN")
        return

    # Field fill rates
    field_checks = [
        ("item_name", "Item name"),
        ("shop_name", "Shop name"),
        ("price_min", "Price (min)"),
        ("price_max", "Price (max)"),
        ("review_count", "Review count"),
        ("review_rating", "Review rating"),
        ("variant_count", "Variant count (non-zero)"),
        ("variant_data_json", "Variant data JSON"),
        ("ancestor_genre_id", "Ancestor genre ID"),
        ("r_category_id", "R-Category ID"),
        ("coupon_yen", "Coupon (yen)"),
        ("coupon_percent", "Coupon (percent)"),
    ]

    for col, label in field_checks:
        if col == "variant_count":
            row = query_one(conn, f"""
                SELECT SUM(CASE WHEN {col} > 0 THEN 1 ELSE 0 END) AS filled
                FROM detail_observations
            """)
        else:
            row = query_one(conn, f"""
                SELECT SUM(CASE WHEN {col} IS NOT NULL THEN 1 ELSE 0 END) AS filled
                FROM detail_observations
            """)
        filled = row["filled"] or 0
        rate = filled / total_obs
        if rate >= FIELD_FILL_RATE_WARN:
            status = "PASS"
        elif rate >= FIELD_FILL_RATE_FAIL:
            status = "WARN"
        else:
            if col in ("coupon_yen", "coupon_percent", "r_category_id"):
                status = "PASS"  # Legitimately sparse
            else:
                status = "FAIL"
        report.add(cat, f"  {label}: {filled:,}/{total_obs:,} ({rate*100:.1f}%)",
                   status)

    # Variants with JAN codes
    row = query_one(conn, """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN jan_code IS NOT NULL AND jan_code != '' THEN 1 ELSE 0 END) AS with_jan
        FROM variants
    """)
    if row and row["total"] > 0:
        t = row["total"]
        j = row["with_jan"]
        report.add(cat, f"Variants with JAN codes: {j:,}/{t:,} ({j/t*100:.1f}%)",
                   "PASS" if j > 0 else "WARN")
    else:
        report.add(cat, "No variants found", "FAIL")

    # Flags breakdown
    for flag_col, flag_name in [("is_39_shop", "39ショップ"), ("is_super_deal", "スーパーDEAL")]:
        row = query_one(conn, f"""
            SELECT SUM(CASE WHEN {flag_col} = 1 THEN 1 ELSE 0 END) AS flagged
            FROM detail_observations
        """)
        flagged = row["flagged"] or 0
        report.add(cat, f"  {flag_name} flagged: {flagged:,}/{total_obs:,}",
                   "PASS")

    # Data source breakdown
    rows = query(conn, """
        SELECT data_source, COUNT(*) AS cnt
        FROM detail_observations
        GROUP BY data_source
    """)
    for r in rows:
        report.add(cat, f"  Data source '{r['data_source']}': {r['cnt']:,}",
                   "PASS")


def check_pipeline_convergence(conn, report: CompletionReport):
    """Check that data from all 3 stages converges on the same products."""
    cat = "5. PIPELINE CONVERGENCE (3-stage data merge)"

    # Items seen by ALL 3 stages
    row = query_one(conn, """
        SELECT COUNT(*) AS cnt FROM items
        WHERE seen_in_api = 1 AND seen_in_list = 1 AND seen_in_detail = 1
    """)
    all_three = row["cnt"]
    report.add(cat, f"Items covered by ALL 3 stages: {all_three:,}",
               "PASS" if all_three > 0 else "WARN",
               "API + List + Detail")

    # Items by stage combination
    combos = query(conn, """
        SELECT
            seen_in_api, seen_in_list, seen_in_detail,
            COUNT(*) AS cnt
        FROM items
        GROUP BY seen_in_api, seen_in_list, seen_in_detail
        ORDER BY cnt DESC
    """)
    for c in combos:
        sources = []
        if c["seen_in_api"]:
            sources.append("API")
        if c["seen_in_list"]:
            sources.append("List")
        if c["seen_in_detail"]:
            sources.append("Detail")
        label = " + ".join(sources) if sources else "None"
        report.add(cat, f"  [{label}]: {c['cnt']:,} items", "PASS")

    # Items that have list observation AND detail observation
    row = query_one(conn, """
        SELECT COUNT(DISTINCT i.id) AS cnt
        FROM items i
        JOIN list_observations lo ON lo.item_id = i.id
        JOIN detail_observations do2 ON do2.item_id = i.id
    """)
    report.add(cat, f"Items with BOTH list + detail observations: {row['cnt']:,}",
               "PASS" if row["cnt"] > 0 else "WARN")


def check_variant_snapshots(conn, report: CompletionReport):
    """Check variant snapshot data quality."""
    cat = "6. VARIANT SNAPSHOTS & HISTORY"

    row = query_one(conn, """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN price IS NOT NULL THEN 1 ELSE 0 END) AS with_price,
            SUM(CASE WHEN point_rate IS NOT NULL THEN 1 ELSE 0 END) AS with_point_rate,
            SUM(CASE WHEN coupon_yen IS NOT NULL OR coupon_percent IS NOT NULL THEN 1 ELSE 0 END) AS with_coupon,
            SUM(CASE WHEN shipping_text_raw IS NOT NULL THEN 1 ELSE 0 END) AS with_shipping,
            SUM(CASE WHEN shipping_days_min IS NOT NULL THEN 1 ELSE 0 END) AS with_ship_days,
            SUM(CASE WHEN image_url IS NOT NULL THEN 1 ELSE 0 END) AS with_image,
            SUM(CASE WHEN extra1_key IS NOT NULL THEN 1 ELSE 0 END) AS with_extra1,
            SUM(CASE WHEN extra2_key IS NOT NULL THEN 1 ELSE 0 END) AS with_extra2
        FROM variant_snapshots
    """)

    if not row or row["total"] == 0:
        report.add(cat, "No variant snapshots found", "FAIL")
        return

    t = row["total"]
    checks = [
        ("with_price", "Price", 0.9),
        ("with_point_rate", "Point rate", 0.3),
        ("with_coupon", "Coupon (yen or %)", 0.01),
        ("with_shipping", "Shipping text", 0.3),
        ("with_ship_days", "Shipping days", 0.01),
        ("with_image", "Image URL", 0.8),
        ("with_extra1", "Extra field 1 (reviews)", 0.1),
    ]

    for key, label, threshold in checks:
        val = row[key] or 0
        rate = val / t
        status = "PASS" if rate >= threshold else "WARN" if rate > 0 else "FAIL"
        report.add(cat, f"  {label}: {val:,}/{t:,} ({rate*100:.1f}%)", status)

    # Source breakdown
    rows = query(conn, """
        SELECT source, COUNT(*) AS cnt
        FROM variant_snapshots
        GROUP BY source
        ORDER BY cnt DESC
    """)
    for r in rows:
        report.add(cat, f"  Source '{r['source']}': {r['cnt']:,} snapshots", "PASS")

    # Daily history coverage
    row = query_one(conn, """
        SELECT COUNT(DISTINCT record_date) AS dates,
               MIN(record_date) AS first_date,
               MAX(record_date) AS last_date,
               COUNT(*) AS total_records
        FROM variant_daily_history
    """)
    if row and row["total_records"] > 0:
        report.add(cat, f"History: {row['total_records']:,} records across "
                   f"{row['dates']} date(s) ({row['first_date']} → {row['last_date']})",
                   "PASS")
    else:
        report.add(cat, "No daily history records found", "WARN")


def check_crawl_jobs(conn, report: CompletionReport):
    """Check crawl job completion rates."""
    cat = "7. CRAWL JOB STATUS"

    rows = query(conn, """
        SELECT job_type, status, COUNT(*) AS cnt
        FROM crawl_jobs
        GROUP BY job_type, status
        ORDER BY job_type, status
    """)

    if not rows:
        report.add(cat, "No crawl jobs found", "WARN")
        return

    job_stats = defaultdict(dict)
    for r in rows:
        job_stats[r["job_type"]][r["status"]] = r["cnt"]

    for jt in ["api_discovery", "list_scrape", "detail_scrape"]:
        stats = job_stats.get(jt, {})
        total = sum(stats.values())
        done = stats.get("done", 0)
        failed = stats.get("failed", 0)
        pending = stats.get("pending", 0)
        running = stats.get("running", 0)

        if total == 0:
            report.add(cat, f"{jt}: no jobs", "WARN")
            continue

        done_rate = done / total * 100 if total > 0 else 0
        report.add(cat,
            f"{jt}: {done:,} done, {failed:,} failed, "
            f"{running:,} running, {pending:,} pending "
            f"(total: {total:,}, success: {done_rate:.1f}%)",
            "PASS" if done_rate > 80 else "WARN" if done_rate > 50 else "FAIL")


def check_sample_products(conn, report: CompletionReport, sample_size: int = 5):
    """Deep-inspect a sample of products to verify all fields are populated."""
    cat = "8. SAMPLE PRODUCT DEEP INSPECTION"

    # Pick products that should be fully covered (all 3 stages)
    samples = query(conn, """
        SELECT i.id, i.product_uid, i.shop_code, i.item_code, i.item_name,
               i.genre_id, i.seen_in_api, i.seen_in_list, i.seen_in_detail,
               i.canonical_url
        FROM items i
        WHERE i.seen_in_api = 1
          AND i.seen_in_list = 1
          AND i.seen_in_detail = 1
        ORDER BY i.updated_at DESC
        LIMIT %s
    """, (sample_size,))

    if not samples:
        # Fall back to items with at least 2 stages
        samples = query(conn, """
            SELECT i.id, i.product_uid, i.shop_code, i.item_code, i.item_name,
                   i.genre_id, i.seen_in_api, i.seen_in_list, i.seen_in_detail,
                   i.canonical_url
            FROM items i
            WHERE (i.seen_in_api + i.seen_in_list + i.seen_in_detail) >= 2
            ORDER BY i.updated_at DESC
            LIMIT %s
        """, (sample_size,))

    if not samples:
        # Fall back to any items
        samples = query(conn, """
            SELECT i.id, i.product_uid, i.shop_code, i.item_code, i.item_name,
                   i.genre_id, i.seen_in_api, i.seen_in_list, i.seen_in_detail,
                   i.canonical_url
            FROM items i
            ORDER BY i.updated_at DESC
            LIMIT %s
        """, (sample_size,))

    if not samples:
        report.add(cat, "No items found for sample inspection", "FAIL")
        return

    for idx, item in enumerate(samples, 1):
        item_id = item["id"]
        uid = item["product_uid"] or f"{item['shop_code']}:{item['item_code']}"
        stages = []
        if item["seen_in_api"]:
            stages.append("API")
        if item["seen_in_list"]:
            stages.append("List")
        if item["seen_in_detail"]:
            stages.append("Detail")

        report.add(cat, "--- Sample {}: {} ({}) ---".format(idx, uid, "+".join(stages)), "PASS")

        # Check: item basics
        basics_ok = all([
            item.get("item_name"),
            item.get("shop_code"),
            item.get("item_code"),
            item.get("canonical_url"),
            item.get("product_uid"),
        ])
        report.add(cat, f"  Item basics (name, codes, URL, uid): {'Complete' if basics_ok else 'INCOMPLETE'}",
                   "PASS" if basics_ok else "WARN")

        # Check: variants
        variants = query(conn, """
            SELECT v.id, v.variant_code, v.jan_code, v.variant_name,
                   vs.price, vs.point_rate, vs.coupon_yen, vs.coupon_percent,
                   vs.shipping_text_raw, vs.shipping_days_min, vs.shipping_days_max,
                   vs.image_url, vs.source, vs.fetched_at,
                   vs.extra1_key, vs.extra1_value, vs.extra2_key, vs.extra2_value
            FROM variants v
            LEFT JOIN variant_snapshots vs ON vs.variant_id = v.id
            WHERE v.item_id = %s
        """, (item_id,))

        report.add(cat, f"  Variants: {len(variants)}", "PASS" if variants else "FAIL")

        for v in variants[:3]:  # Show up to 3 variants per item
            jan_str = v.get("jan_code") or "none"
            price_str = f"¥{v['price']:,}" if v.get("price") else "none"
            source_str = v.get("source") or "none"
            report.add(cat,
                f"    variant={v['variant_code']}, JAN={jan_str}, "
                f"price={price_str}, source={source_str}",
                "PASS" if v.get("price") else "WARN")

        # Check: list observation
        list_obs = query_one(conn, """
            SELECT observed_price, point_count, point_text_raw,
                   coupon_discount, coupon_type, coupon_text_raw,
                   shipping_cost, delivery_text, is_free_shipping,
                   review_score, review_count,
                   is_super_deal, is_sold_out, data_source
            FROM list_observations
            WHERE item_id = %s
            ORDER BY observed_at DESC
            LIMIT 1
        """, (item_id,))

        if list_obs:
            list_fields = sum(1 for v in list_obs.values() if v is not None)
            report.add(cat,
                f"  List observation: {list_fields}/14 fields filled, "
                f"price=¥{list_obs.get('observed_price') or '?':}, "
                f"points={list_obs.get('point_text_raw') or 'none'}, "
                f"source={list_obs.get('data_source')}",
                "PASS" if list_fields >= 5 else "WARN")
        else:
            report.add(cat, "  List observation: NOT FOUND",
                       "WARN" if "List" not in stages else "FAIL")

        # Check: detail observation
        detail_obs = query_one(conn, """
            SELECT item_name, shop_name, price_min, price_max,
                   review_count, review_rating,
                   is_39_shop, is_super_deal,
                   variant_count, variant_data_json,
                   coupon_yen, coupon_percent,
                   ancestor_genre_id, data_source
            FROM detail_observations
            WHERE item_id = %s
            ORDER BY observed_at DESC
            LIMIT 1
        """, (item_id,))

        if detail_obs:
            detail_fields = sum(1 for v in detail_obs.values() if v is not None)
            vcount = detail_obs.get("variant_count") or 0
            has_json = detail_obs.get("variant_data_json") is not None

            report.add(cat,
                f"  Detail observation: {detail_fields}/14 fields filled, "
                f"variants={vcount}, JSON={'yes' if has_json else 'no'}, "
                f"source={detail_obs.get('data_source')}",
                "PASS" if detail_fields >= 5 else "WARN")

            # Parse variant_data_json for JAN coverage
            if has_json:
                try:
                    vdata = json.loads(detail_obs["variant_data_json"])
                    total_v = len(vdata)
                    with_jan = sum(1 for v in vdata if v.get("jan"))
                    with_price = sum(1 for v in vdata if v.get("price"))
                    with_stock = sum(1 for v in vdata if v.get("stock") is not None)
                    report.add(cat,
                        f"    Variant JSON: {total_v} variants, "
                        f"JAN={with_jan}/{total_v}, "
                        f"price={with_price}/{total_v}, "
                        f"stock={with_stock}/{total_v}",
                        "PASS" if with_jan > 0 else "WARN")
                except (json.JSONDecodeError, TypeError):
                    report.add(cat, "    Variant JSON: parse error", "WARN")
        else:
            report.add(cat, "  Detail observation: NOT FOUND",
                       "WARN" if "Detail" not in stages else "FAIL")


def check_price_range_slicing(conn, report: CompletionReport):
    """Check price range slicing progress."""
    cat = "9. PRICE RANGE SLICING"

    rows = query(conn, """
        SELECT
            prs.status, COUNT(*) AS cnt,
            ct.target_value AS genre_id
        FROM price_range_slices prs
        JOIN crawl_targets ct ON ct.id = prs.crawl_target_id
        GROUP BY ct.target_value, prs.status
        ORDER BY ct.target_value, prs.status
    """)

    if not rows:
        report.add(cat, "No price range slices found (pipeline may not have started)", "WARN")
        return

    genre_stats = defaultdict(lambda: defaultdict(int))
    for r in rows:
        genre_stats[r["genre_id"]][r["status"]] = r["cnt"]

    for gid in TARGET_GENRES:
        stats = genre_stats.get(gid, {})
        if not stats:
            report.add(cat, f"  Genre {gid}: no slices", "WARN")
            continue
        total = sum(stats.values())
        done = stats.get("done", 0)
        pending = stats.get("pending", 0)
        in_progress = stats.get("in_progress", 0)
        needs_split = stats.get("needs_split", 0)
        report.add(cat,
            f"  Genre {gid}: {total:,} slices "
            f"(done={done}, pending={pending}, "
            f"in_progress={in_progress}, needs_split={needs_split})",
            "PASS" if done > 0 else "WARN")


def check_client_required_fields(conn, report: CompletionReport):
    """
    Ultimate check: for products that have passed through all stages,
    verify that ALL client-required information fields are populated.

    Client-required fields:
    1. Product identity: product_uid, shop_code, item_code, item_name, canonical_url
    2. Pricing: price (current)
    3. Points: point breakdown (base, shop, UP multipliers), point_count
    4. Coupons: coupon amount/percent
    5. Shipping: shipping cost, free shipping flag, delivery estimate
    6. Reviews: score, count
    7. Variants: JAN code, variant name, price per variant, stock status
    8. Flags: super deal, 39 shop, sold out
    9. Images: product image URL
    10. Genre: genre_id
    """
    cat = "10. CLIENT-REQUIRED COMPLETE PRODUCT INFO"

    # Count fully complete products (have data from all key sources)
    row = query_one(conn, """
        SELECT COUNT(DISTINCT i.id) AS cnt
        FROM items i
        JOIN variant_snapshots vs ON vs.variant_id = (
            SELECT v.id FROM variants v WHERE v.item_id = i.id LIMIT 1
        )
        JOIN list_observations lo ON lo.item_id = i.id
        WHERE i.item_name IS NOT NULL
          AND vs.price IS NOT NULL
          AND vs.image_url IS NOT NULL
          AND lo.observed_price IS NOT NULL
    """)
    complete = row["cnt"] if row else 0
    report.add(cat, f"Products with name + price + image + list data: {complete:,}",
               "PASS" if complete > 0 else "WARN")

    # Count products with full point breakdown
    row = query_one(conn, """
        SELECT COUNT(DISTINCT lo.item_id) AS cnt
        FROM list_observations lo
        WHERE lo.point_count IS NOT NULL
          AND lo.point_base_multiplier IS NOT NULL
    """)
    with_points = row["cnt"] if row else 0
    report.add(cat, f"Products with point breakdown: {with_points:,}",
               "PASS" if with_points > 0 else "WARN")

    # Count products with coupon info
    row = query_one(conn, """
        SELECT COUNT(DISTINCT item_id) AS cnt
        FROM list_observations
        WHERE coupon_discount IS NOT NULL
    """)
    with_coupon = row["cnt"] if row else 0
    report.add(cat, f"Products with coupon data: {with_coupon:,}",
               "PASS" if with_coupon > 0 else "WARN",
               "Not all products have coupons — this is normal")

    # Count products with shipping info
    row = query_one(conn, """
        SELECT COUNT(DISTINCT item_id) AS cnt
        FROM list_observations
        WHERE shipping_cost IS NOT NULL OR is_free_shipping = 1
    """)
    with_shipping = row["cnt"] if row else 0
    report.add(cat, f"Products with shipping info: {with_shipping:,}",
               "PASS" if with_shipping > 0 else "WARN")

    # Count products with review data
    row = query_one(conn, """
        SELECT COUNT(DISTINCT item_id) AS cnt
        FROM list_observations
        WHERE review_score IS NOT NULL AND review_count IS NOT NULL
    """)
    with_reviews = row["cnt"] if row else 0
    report.add(cat, f"Products with review data: {with_reviews:,}",
               "PASS" if with_reviews > 0 else "WARN")

    # Count products with JAN codes (from detail scrape)
    row = query_one(conn, """
        SELECT COUNT(DISTINCT v.item_id) AS cnt
        FROM variants v
        WHERE v.jan_code IS NOT NULL AND v.jan_code != ''
    """)
    with_jan = row["cnt"] if row else 0
    report.add(cat, f"Products with JAN codes: {with_jan:,}",
               "PASS" if with_jan > 0 else "WARN")

    # Count products with variant detail (stock, delivery)
    row = query_one(conn, """
        SELECT COUNT(DISTINCT item_id) AS cnt
        FROM detail_observations
        WHERE variant_data_json IS NOT NULL
          AND variant_count > 0
    """)
    with_variant_detail = row["cnt"] if row else 0
    report.add(cat, f"Products with full variant details (JSON): {with_variant_detail:,}",
               "PASS" if with_variant_detail > 0 else "WARN")


# ═══════════════════════════════════════════════════════════════
#  Main Execution
# ═══════════════════════════════════════════════════════════════

def run_all_checks():
    """Run all completeness checks and print the report."""
    report = CompletionReport()

    print("\nConnecting to database...")
    conn = get_connection()
    print(f"Connected to {DB_HOST}:{DB_PORT}/{DB_NAME}")

    try:
        check_overall_counts(conn, report)
        check_stage1_api_coverage(conn, report)
        check_stage2_list_coverage(conn, report)
        check_stage3_detail_coverage(conn, report)
        check_pipeline_convergence(conn, report)
        check_variant_snapshots(conn, report)
        check_crawl_jobs(conn, report)
        check_sample_products(conn, report)
        check_price_range_slicing(conn, report)
        check_client_required_fields(conn, report)
    finally:
        conn.close()

    report.print_report()
    return report


# ═══════════════════════════════════════════════════════════════
#  Pytest Integration
# ═══════════════════════════════════════════════════════════════

def test_data_completeness():
    """
    Pytest entry point.

    Run with:
        python -m pytest tests/test_data_completeness.py -v -s

    This test passes if there are no FAIL results (warnings are acceptable).
    """
    report = run_all_checks()
    assert report.failures == 0, (
        f"Data completeness check failed with {report.failures} failure(s). "
        f"See report above for details."
    )


# ═══════════════════════════════════════════════════════════════
#  Direct execution
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    report = run_all_checks()
    sys.exit(1 if report.failures > 0 else 0)
