"""
Price / point / coupon history endpoints.
Serves data for the chart display on the frontend (サイト表示項目.xlsx).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_async_db
from app.schemas.product import (
    HistoryRecord,
    PointCalcResponse,
    PointCalcInput,
    PointCalcOutput,
    VariantHistoryResponse,
)
from app.services.point_calculator import calculate_for_variant_snapshot

router = APIRouter(prefix="/history", tags=["history"])


# ─────────────────────────────────────────────────────────────
#  Get variant price/point history (for charts)
# ─────────────────────────────────────────────────────────────
@router.get("/{variant_id}", response_model=VariantHistoryResponse)
async def get_variant_history(
    variant_id: int,
    days: int = Query(
        30,
        ge=1,
        le=730,
        description="表示期間 (日数)。直近1週間=7, 直近1ヶ月=30, 直近3か月=90, 直近1年=365, 全期間=730",
    ),
    db: AsyncSession = Depends(get_async_db),
) -> VariantHistoryResponse:
    """
    Get daily history for a variant.

    Supports the chart display requirements:
    - 製品価格推移 (price)
    - 獲得ポイント推移 (point_rate)
    - クーポン価値推移 (coupon_yen, coupon_percent)
    - 実質購入価格推移 (computed on frontend using user's point_multiplier)

    Preset buttons:
    - 直近1週間: days=7
    - 直近1ヶ月: days=30 (default)
    - 直近3か月: days=90
    - 直近1年間: days=365
    - 全期間: days=730
    """
    # Verify variant exists
    check = await db.execute(
        text("SELECT id FROM variants WHERE id = :vid"),
        {"vid": variant_id},
    )
    if not check.first():
        raise HTTPException(status_code=404, detail="Variant not found")

    result = await db.execute(text("""
        SELECT
            record_date,
            price,
            point_rate,
            point_back_percent,
            coupon_yen,
            coupon_percent,
            shipping_days_min,
            shipping_days_max
        FROM variant_daily_history
        WHERE variant_id = :variant_id
          AND record_date >= DATE_SUB(CURDATE(), INTERVAL :days DAY)
        ORDER BY record_date ASC
    """), {"variant_id": variant_id, "days": days})

    rows = result.mappings().all()
    history = [HistoryRecord(**dict(r)) for r in rows]

    return VariantHistoryResponse(
        variant_id=variant_id,
        days=days,
        history=history,
    )


# ─────────────────────────────────────────────────────────────
#  Calculate points for a variant (user-specific multiplier)
# ─────────────────────────────────────────────────────────────
@router.get("/{variant_id}/calculate", response_model=PointCalcResponse)
async def calculate_points(
    variant_id: int,
    point_multiplier: float = Query(
        4.0,
        ge=1.0,
        le=100.0,
        description="ポイント倍率 (ユーザー指定, デフォルト4)",
    ),
    db: AsyncSession = Depends(get_async_db),
) -> PointCalcResponse:
    """
    Calculate effective points / prices for a variant.

    Uses the logic from 楽天ポイント計算.xlsx:
    - クーポン採用値 [円]
    - 合計ポイント [pt]
    - 価格-ポイント
    - 価格-ポイント-クーポン

    The point_multiplier is specified by each user (default 4).
    """
    result = await db.execute(text("""
        SELECT
            vs.price,
            vs.point_rate,
            vs.point_back_percent,
            vs.coupon_yen,
            vs.coupon_percent
        FROM variant_snapshots vs
        WHERE vs.variant_id = :variant_id
    """), {"variant_id": variant_id})

    row = result.mappings().first()
    if not row:
        raise HTTPException(
            status_code=404,
            detail="Variant snapshot not found",
        )

    price = row["price"] or 0
    point_rate = float(row["point_rate"]) if row["point_rate"] else 0.0
    point_back_pct = float(row["point_back_percent"]) if row["point_back_percent"] else 0.0
    coupon_yen = row["coupon_yen"] or 0
    coupon_pct = float(row["coupon_percent"]) if row["coupon_percent"] else 0.0

    calc_output = calculate_for_variant_snapshot(
        price=price,
        point_rate=point_rate,
        point_back_percent=point_back_pct,
        coupon_yen=coupon_yen,
        coupon_percent=coupon_pct,
        user_point_multiplier=point_multiplier,
    )

    return PointCalcResponse(
        variant_id=variant_id,
        input=PointCalcInput(
            point_multiplier=point_multiplier,
            rakuten_price=price,
            coupon_yen=coupon_yen,
            coupon_percent=coupon_pct,
            plus_x_up=point_rate,
            point_back_flag=1 if point_back_pct == 0 else int(point_back_pct),
        ),
        output=PointCalcOutput(
            effective_coupon=calc_output.effective_coupon,
            total_points=calc_output.total_points,
            price_minus_points=calc_output.price_minus_points,
            price_minus_points_coupon=calc_output.price_minus_points_coupon,
        ),
    )


# ─────────────────────────────────────────────────────────────
#  Batch history for chart overlay (multiple variants)
# ─────────────────────────────────────────────────────────────
@router.get("/batch", response_model=list[VariantHistoryResponse])
async def get_batch_history(
    variant_ids: str = Query(
        ...,
        description="Comma-separated variant IDs (e.g. '1,2,3')",
    ),
    days: int = Query(30, ge=1, le=730),
    db: AsyncSession = Depends(get_async_db),
) -> list[VariantHistoryResponse]:
    """
    Get history for multiple variants at once.
    Useful for comparing prices across shops for the same JAN.
    """
    try:
        ids = [int(x.strip()) for x in variant_ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid variant_ids format")

    if not ids or len(ids) > 20:
        raise HTTPException(
            status_code=400,
            detail="Provide 1-20 variant IDs",
        )

    # Build IN clause safely
    placeholders = ", ".join([f":vid_{i}" for i in range(len(ids))])
    params = {f"vid_{i}": vid for i, vid in enumerate(ids)}
    params["days"] = days

    result = await db.execute(text(f"""
        SELECT
            variant_id,
            record_date,
            price,
            point_rate,
            point_back_percent,
            coupon_yen,
            coupon_percent,
            shipping_days_min,
            shipping_days_max
        FROM variant_daily_history
        WHERE variant_id IN ({placeholders})
          AND record_date >= DATE_SUB(CURDATE(), INTERVAL :days DAY)
        ORDER BY variant_id, record_date ASC
    """), params)

    rows = result.mappings().all()

    # Group by variant_id
    grouped: dict[int, list[HistoryRecord]] = {}
    for r in rows:
        vid = r["variant_id"]
        if vid not in grouped:
            grouped[vid] = []
        grouped[vid].append(HistoryRecord(**{
            k: v for k, v in dict(r).items() if k != "variant_id"
        }))

    return [
        VariantHistoryResponse(variant_id=vid, days=days, history=grouped.get(vid, []))
        for vid in ids
    ]
