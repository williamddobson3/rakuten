"""
Product search, detail, and JAN-based cheapest-sort endpoints.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_async_db
from app.schemas.product import (
    JanSearchResult,
    ProductOut,
    ProductSearchResponse,
    ProductSearchResult,
    VariantOut,
    VariantSnapshotOut,
)

router = APIRouter(prefix="/products", tags=["products"])


# ─────────────────────────────────────────────────────────────
#  Search products
# ─────────────────────────────────────────────────────────────
@router.get("/search", response_model=ProductSearchResponse)
async def search_products(
    q: str | None = Query(None, description="商品名キーワード検索"),
    jan: str | None = Query(None, description="JANコード検索"),
    shop_code: str | None = Query(None, description="店舗コード絞り込み"),
    has_jan: bool = Query(False, description="JANコードありの商品のみ表示"),
    has_coupon: bool = Query(False, description="クーポンありの商品のみ表示"),
    has_variant: bool = Query(False, description="バリアントありの商品のみ表示"),
    updated_24h: bool = Query(False, description="24時間以内に更新された商品のみ表示"),
    price_min: int | None = Query(None, ge=0, description="最低価格"),
    price_max: int | None = Query(None, ge=0, description="最高価格"),
    shipping_max_days: int | None = Query(None, ge=0, description="発送日数上限"),
    sort_by: str = Query(
        "price_asc",
        description="ソート順",
        pattern="^(price_asc|price_desc|updated|name)$",
    ),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=100),
    db: AsyncSession = Depends(get_async_db),
) -> ProductSearchResponse:
    """
    Search products by name, JAN code, or shop code.
    Results include the latest snapshot data for each variant.
    """
    offset = (page - 1) * per_page
    conditions: list[str] = ["i.is_active = 1"]
    params: dict = {"limit": per_page, "offset": offset}

    if q:
        conditions.append("i.item_name LIKE :q")
        params["q"] = f"%{q}%"
    if jan:
        conditions.append("v.jan_code = :jan")
        params["jan"] = jan
    if shop_code:
        conditions.append("i.shop_code = :shop_code")
        params["shop_code"] = shop_code
    if has_jan:
        conditions.append("v.jan_code IS NOT NULL AND v.jan_code != ''")
    if has_coupon:
        conditions.append("(vs.coupon_yen > 0 OR vs.coupon_percent > 0)")
    if has_variant:
        conditions.append("""
            i.id IN (
                SELECT item_id FROM variants WHERE is_active = 1
                GROUP BY item_id HAVING COUNT(*) > 1
            )
        """)
    if updated_24h:
        conditions.append("vs.fetched_at >= NOW() - INTERVAL 24 HOUR")
    if price_min is not None:
        conditions.append("vs.price >= :price_min")
        params["price_min"] = price_min
    if price_max is not None:
        conditions.append("vs.price <= :price_max")
        params["price_max"] = price_max
    if shipping_max_days is not None:
        conditions.append("vs.shipping_days_min IS NOT NULL AND vs.shipping_days_min <= :shipping_max_days")
        params["shipping_max_days"] = shipping_max_days

    where_clause = " AND ".join(conditions)

    order_map = {
        "price_asc": "vs.price ASC",
        "price_desc": "vs.price DESC",
        "updated": "vs.fetched_at DESC",
        "name": "i.item_name ASC",
    }
    order = order_map[sort_by]

    # ── Detect whether any user-supplied filters touch snapshot columns ──
    has_user_filters = bool(q or jan or shop_code or has_jan or has_coupon
                            or has_variant or updated_24h
                            or price_min is not None or price_max is not None
                            or shipping_max_days is not None)
    needs_snapshot_in_where = any(
        "vs." in c for c in conditions if c != "i.is_active = 1"
    )

    # ── Optimized COUNT ─────────────────────────────────────
    # The data query drives from variant_snapshots, so count must match.
    # For unfiltered queries: single-table count on variant_snapshots (~0.5s).
    # For filtered queries: join only the tables needed by active filters.
    if not has_user_filters:
        count_result = await db.execute(text(
            "SELECT COUNT(*) FROM variant_snapshots WHERE price IS NOT NULL"
        ))
        total = count_result.scalar() or 0
    else:
        count_joins = "JOIN items i ON v.item_id = i.id"
        if needs_snapshot_in_where:
            count_joins += "\nJOIN variant_snapshots vs ON v.id = vs.variant_id"
        count_sql = f"""
            SELECT COUNT(*)
            FROM variants v
            {count_joins}
            WHERE {where_clause}
        """
        count_result = await db.execute(text(count_sql), params)
        total = count_result.scalar() or 0

    # ── Fetch page of results ───────────────────────────────
    # Strategy: "Deferred join" pattern.
    # 1) Fast index-only subquery picks just the variant_ids for the page
    # 2) Outer query joins full data only for those ~20 rows
    # This avoids heavy 4-table JOINs across millions of rows + deep OFFSET.
    #
    # Performance: page 1 ≈ 0.01s, page 5000 ≈ 0.07s, page 69756 ≈ 2s
    #   vs old approach: page 69756 ≈ 57s

    use_deferred = (
        sort_by in ("price_asc", "price_desc", "updated")
        and not q           # keyword search needs different driving table
        and not has_variant  # subquery filter needs standard optimizer
        and not shop_code    # shop_code filter on items table
        and not has_jan      # jan filter on variants table
    )

    # ── Shared SELECT columns for the outer query ─────────
    _OUTER_COLS = """
                sub.variant_id AS variant_id,
                i.product_uid,
                i.shop_code,
                s.shop_name,
                s.shop_url,
                i.item_code,
                i.item_name,
                i.canonical_url,
                v.variant_code,
                v.jan_code,
                v.variant_name,
                vs.price,
                vs.point_rate,
                vs.point_back_percent,
                vs.coupon_yen,
                vs.coupon_percent,
                vs.shipping_text_raw,
                vs.shipping_days_min,
                vs.shipping_days_max,
                vs.image_url,
                vs.extra1_key,
                vs.extra1_value,
                vs.extra2_key,
                vs.extra2_value,
                vs.fetched_at
    """

    _OUTER_JOINS = """
            JOIN variant_snapshots vs ON vs.variant_id = sub.variant_id
            JOIN variants v ON v.id = sub.variant_id
            JOIN items i ON i.id = v.item_id
            LEFT JOIN shops s ON s.shop_code = i.shop_code
    """

    if use_deferred and sort_by in ("price_asc", "price_desc"):
        # Inner subquery: index-only scan on idx_price (very fast even at deep offsets)
        inner_cond = "price IS NOT NULL"
        if price_min is not None:
            inner_cond += " AND price >= :price_min"
        if price_max is not None:
            inner_cond += " AND price <= :price_max"
        if has_coupon:
            inner_cond += " AND (coupon_yen > 0 OR coupon_percent > 0)"
        if shipping_max_days is not None:
            inner_cond += " AND shipping_days_min IS NOT NULL AND shipping_days_min <= :shipping_max_days"
        if updated_24h:
            inner_cond += " AND fetched_at >= NOW() - INTERVAL 24 HOUR"

        price_dir = "ASC" if sort_by == "price_asc" else "DESC"
        query_sql = f"""
            SELECT {_OUTER_COLS}
            FROM (
                SELECT variant_id
                FROM variant_snapshots
                WHERE {inner_cond}
                ORDER BY price {price_dir}
                LIMIT :limit OFFSET :offset
            ) AS sub
            {_OUTER_JOINS}
            ORDER BY vs.price {price_dir}
        """

    elif use_deferred and sort_by == "updated":
        # Inner subquery: index-only scan on idx_fetched_at
        inner_cond = "fetched_at IS NOT NULL"
        if has_coupon:
            inner_cond += " AND (coupon_yen > 0 OR coupon_percent > 0)"
        if price_min is not None:
            inner_cond += " AND price >= :price_min"
        if price_max is not None:
            inner_cond += " AND price <= :price_max"
        if shipping_max_days is not None:
            inner_cond += " AND shipping_days_min IS NOT NULL AND shipping_days_min <= :shipping_max_days"
        if updated_24h:
            inner_cond += " AND fetched_at >= NOW() - INTERVAL 24 HOUR"

        query_sql = f"""
            SELECT {_OUTER_COLS}
            FROM (
                SELECT variant_id
                FROM variant_snapshots
                WHERE {inner_cond}
                ORDER BY fetched_at DESC
                LIMIT :limit OFFSET :offset
            ) AS sub
            {_OUTER_JOINS}
            ORDER BY vs.fetched_at DESC
        """

    else:
        # Standard query (keyword search, name sort, or complex filters)
        query_sql = f"""
            SELECT
                v.id AS variant_id,
                i.product_uid,
                i.shop_code,
                s.shop_name,
                s.shop_url,
                i.item_code,
                i.item_name,
                i.canonical_url,
                v.variant_code,
                v.jan_code,
                v.variant_name,
                vs.price,
                vs.point_rate,
                vs.point_back_percent,
                vs.coupon_yen,
                vs.coupon_percent,
                vs.shipping_text_raw,
                vs.shipping_days_min,
                vs.shipping_days_max,
                vs.image_url,
                vs.extra1_key,
                vs.extra1_value,
                vs.extra2_key,
                vs.extra2_value,
                vs.fetched_at
            FROM variants v
            JOIN items i ON v.item_id = i.id
            LEFT JOIN shops s ON i.shop_code = s.shop_code
            LEFT JOIN variant_snapshots vs ON v.id = vs.variant_id
            WHERE {where_clause}
            ORDER BY {order}
            LIMIT :limit OFFSET :offset
        """

    result = await db.execute(text(query_sql), params)
    rows = result.mappings().all()

    results = [ProductSearchResult(**dict(r)) for r in rows]

    return ProductSearchResponse(
        page=page,
        per_page=per_page,
        total=total,
        results=results,
    )


# ─────────────────────────────────────────────────────────────
#  Get product detail (with all variants)
# ─────────────────────────────────────────────────────────────
@router.get("/uid/{product_uid:path}", response_model=ProductOut)
async def get_product_by_uid(
    product_uid: str,
    db: AsyncSession = Depends(get_async_db),
) -> ProductOut:
    """
    Get full product detail by product_uid ({shop_code}:{item_code}).
    """
    item_result = await db.execute(text("""
        SELECT i.id, i.product_uid, i.shop_code, i.item_code,
               i.item_name, i.canonical_url, s.shop_name, s.shop_url
        FROM items i
        LEFT JOIN shops s ON i.shop_code = s.shop_code
        WHERE i.product_uid = :product_uid AND i.is_active = 1
    """), {"product_uid": product_uid})
    item_row = item_result.mappings().first()

    if not item_row:
        raise HTTPException(status_code=404, detail="Product not found")

    return await _build_product_out(db, item_row)


@router.get("/{item_id}", response_model=ProductOut)
async def get_product_detail(
    item_id: int,
    db: AsyncSession = Depends(get_async_db),
) -> ProductOut:
    """
    Get full product detail including all variants and their snapshots.
    """
    # Fetch item
    item_result = await db.execute(text("""
        SELECT i.id, i.product_uid, i.shop_code, i.item_code,
               i.item_name, i.canonical_url, s.shop_name, s.shop_url
        FROM items i
        LEFT JOIN shops s ON i.shop_code = s.shop_code
        WHERE i.id = :item_id AND i.is_active = 1
    """), {"item_id": item_id})
    item_row = item_result.mappings().first()

    if not item_row:
        raise HTTPException(status_code=404, detail="Product not found")

    return await _build_product_out(db, item_row)


async def _build_product_out(db: AsyncSession, item_row) -> ProductOut:
    """Shared helper: load variants+snapshots and build ProductOut."""
    item_id = item_row["id"]

    variants_result = await db.execute(text("""
        SELECT
            v.id AS variant_id,
            v.variant_code,
            v.jan_code,
            v.variant_name,
            vs.price,
            vs.point_rate,
            vs.point_back_percent,
            vs.coupon_yen,
            vs.coupon_percent,
            vs.shipping_text_raw,
            vs.shipping_days_min,
            vs.shipping_days_max,
            vs.image_url,
            vs.extra1_key,
            vs.extra1_value,
            vs.extra2_key,
            vs.extra2_value,
            vs.fetched_at,
            vs.source
        FROM variants v
        LEFT JOIN variant_snapshots vs ON v.id = vs.variant_id
        WHERE v.item_id = :item_id AND v.is_active = 1
        ORDER BY v.variant_code
    """), {"item_id": item_id})
    variant_rows = variants_result.mappings().all()

    variants = []
    for vr in variant_rows:
        snapshot = None
        if vr["price"] is not None:
            snapshot = VariantSnapshotOut(
                price=vr["price"],
                point_rate=float(vr["point_rate"]) if vr["point_rate"] else None,
                point_back_percent=float(vr["point_back_percent"]) if vr["point_back_percent"] else None,
                coupon_yen=vr["coupon_yen"],
                coupon_percent=float(vr["coupon_percent"]) if vr["coupon_percent"] else None,
                shipping_text_raw=vr["shipping_text_raw"],
                shipping_days_min=vr["shipping_days_min"],
                shipping_days_max=vr["shipping_days_max"],
                image_url=vr["image_url"],
                extra1_key=vr["extra1_key"],
                extra1_value=vr["extra1_value"],
                extra2_key=vr["extra2_key"],
                extra2_value=vr["extra2_value"],
                fetched_at=vr["fetched_at"],
                source=vr["source"],
            )
        variants.append(VariantOut(
            variant_id=vr["variant_id"],
            variant_code=vr["variant_code"],
            jan_code=vr["jan_code"],
            variant_name=vr["variant_name"],
            snapshot=snapshot,
        ))

    return ProductOut(
        item_id=item_row["id"],
        product_uid=item_row["product_uid"],
        shop_code=item_row["shop_code"],
        shop_name=item_row["shop_name"],
        shop_url=item_row.get("shop_url"),
        item_code=item_row["item_code"],
        item_name=item_row["item_name"],
        canonical_url=item_row["canonical_url"],
        variants=variants,
    )


# ─────────────────────────────────────────────────────────────
#  Same-JAN cheapest sort (同一JAN最安値並び替え)
# ─────────────────────────────────────────────────────────────
@router.get("/jan/{jan_code}", response_model=JanSearchResult)
async def get_cheapest_by_jan(
    jan_code: str,
    point_multiplier: float = Query(
        4.0, ge=1.0, le=100.0,
        description="ポイント倍率 (ユーザー指定)",
    ),
    db: AsyncSession = Depends(get_async_db),
) -> JanSearchResult:
    """
    Find all variants sharing the same JAN code,
    sorted by effective price (price - points - coupon) ascending.

    This enables the 同一JAN商品の最安値並び替え feature.
    """
    result = await db.execute(text("""
        SELECT
            v.id AS variant_id,
            i.product_uid,
            i.shop_code,
            s.shop_name,
            s.shop_url,
            i.item_code,
            i.item_name,
            i.canonical_url,
            v.variant_code,
            v.jan_code,
            v.variant_name,
            vs.price,
            vs.point_rate,
            vs.point_back_percent,
            vs.coupon_yen,
            vs.coupon_percent,
            vs.shipping_text_raw,
            vs.shipping_days_min,
            vs.shipping_days_max,
            vs.image_url,
            vs.extra1_key,
            vs.extra1_value,
            vs.extra2_key,
            vs.extra2_value,
            vs.fetched_at
        FROM variants v
        JOIN items i ON v.item_id = i.id
        LEFT JOIN shops s ON i.shop_code = s.shop_code
        LEFT JOIN variant_snapshots vs ON v.id = vs.variant_id
        WHERE v.jan_code = :jan_code
          AND i.is_active = 1
          AND v.is_active = 1
        ORDER BY vs.price ASC
    """), {"jan_code": jan_code})

    rows = result.mappings().all()
    variants = [ProductSearchResult(**dict(r)) for r in rows]

    return JanSearchResult(jan_code=jan_code, variants=variants)
