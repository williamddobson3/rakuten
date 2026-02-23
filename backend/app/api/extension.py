"""
Chrome extension API endpoints.

Handles:
1. Receiving browse events from the extension
2. Upserting shop/item/variant data
3. Recording browse events for shop ranking
4. Triggering snapshot+history upsert

Phase 5 requirement:
  一度ユーザーが閲覧した商品は、その後ずっと、
  1日に1回のサーバークロール対象として追加する。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_async_db
from app.schemas.product import (
    ExtensionBrowsePayload,
    ExtensionBrowseResponse,
)
from app.services.item_service import (
    mark_for_server_crawl_async,
    upsert_item_async,
    upsert_shop_async,
    upsert_variant_async,
)
from app.services.snapshot_service import upsert_snapshot_and_history_async
from app.services.url_canonicalizer import (
    canonicalize_item_url,
    extract_shop_item_code,
    make_item_id_str,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/extension", tags=["extension"])


@router.post("/browse", response_model=ExtensionBrowseResponse)
async def receive_browse_event(
    payload: ExtensionBrowsePayload,
    db: AsyncSession = Depends(get_async_db),
) -> ExtensionBrowseResponse:
    """
    Receive product browse data from Chrome extension.

    Flow:
    1. Canonicalize URL → extract shop_code + item_code
    2. Upsert shop → item → variant(s)
    3. Record browse event (for shop ranking)
    4. Upsert snapshot + daily history
    5. Mark item for server crawl (Phase 5)
    """
    # Step 1: Parse URL
    canonical_url, url_variant_id = canonicalize_item_url(payload.page_url)
    try:
        shop_code, item_code = extract_shop_item_code(canonical_url)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid Rakuten item URL")

    # Step 2: Upsert shop
    await upsert_shop_async(db, shop_code=shop_code)

    # Step 3: Upsert item
    item_id = await upsert_item_async(
        db,
        shop_code=shop_code,
        item_code=item_code,
        canonical_url=canonical_url,
        item_name=payload.item_name,
        source="extension",
    )

    # Step 4: Process variants
    variants_processed = 0
    primary_variant_db_id: int | None = None

    if payload.variants and len(payload.variants) > 0:
        # Multi-variant product
        for var_data in payload.variants:
            variant_db_id = await upsert_variant_async(
                db,
                item_id=item_id,
                variant_code=var_data.variant_code,
                jan_code=var_data.jan_code,
                variant_name=var_data.variant_name,
            )
            if primary_variant_db_id is None:
                primary_variant_db_id = variant_db_id

            # Upsert snapshot for this variant
            snapshot_data = {
                "price": var_data.price,
                "point_rate": payload.point_rate,
                "point_back_percent": payload.point_back_percent,
                "coupon_yen": payload.coupon_yen,
                "coupon_percent": payload.coupon_percent,
                "shipping_text_raw": payload.shipping_text,
                "shipping_days_min": payload.shipping_days_min,
                "shipping_days_max": payload.shipping_days_max,
                "image_url": var_data.image_url or payload.image_url,
                "extra1_key": payload.extra1_key,
                "extra1_value": payload.extra1_value,
                "extra2_key": payload.extra2_key,
                "extra2_value": payload.extra2_value,
            }
            await upsert_snapshot_and_history_async(
                db, variant_db_id, snapshot_data, source="extension"
            )
            variants_processed += 1
    else:
        # Single product (or variant not detected)
        variant_code = url_variant_id or payload.variant_id_param or "default"
        variant_db_id = await upsert_variant_async(
            db,
            item_id=item_id,
            variant_code=variant_code,
            jan_code=payload.jan_code,
        )
        primary_variant_db_id = variant_db_id

        snapshot_data = {
            "price": payload.price,
            "point_rate": payload.point_rate,
            "point_back_percent": payload.point_back_percent,
            "coupon_yen": payload.coupon_yen,
            "coupon_percent": payload.coupon_percent,
            "shipping_text_raw": payload.shipping_text,
            "shipping_days_min": payload.shipping_days_min,
            "shipping_days_max": payload.shipping_days_max,
            "image_url": payload.image_url,
            "extra1_key": payload.extra1_key,
            "extra1_value": payload.extra1_value,
            "extra2_key": payload.extra2_key,
            "extra2_value": payload.extra2_value,
        }
        await upsert_snapshot_and_history_async(
            db, variant_db_id, snapshot_data, source="extension"
        )
        variants_processed = 1

    # Step 5: Record browse event
    from sqlalchemy import text as sa_text
    await db.execute(sa_text("""
        INSERT INTO user_browse_events
            (user_id, variant_id, item_id, shop_code, page_url)
        VALUES
            (:user_id, :variant_id, :item_id, :shop_code, :page_url)
    """), {
        "user_id": payload.user_id,
        "variant_id": primary_variant_db_id,
        "item_id": item_id,
        "shop_code": shop_code,
        "page_url": payload.page_url,
    })

    # Step 6: Mark for server crawl (Phase 5 requirement)
    await mark_for_server_crawl_async(db, item_id)

    await db.commit()

    logger.info(
        "Extension browse: user=%s shop=%s item=%s variants=%d",
        payload.user_id, shop_code, item_code, variants_processed,
    )

    return ExtensionBrowseResponse(
        status="ok",
        item_id=item_id,
        product_uid=make_item_id_str(shop_code, item_code),
        variant_id=primary_variant_db_id,
        variants_processed=variants_processed,
    )
