"""
Shop code frequency ranking endpoints.

Implements the requirement:
  拡張機能で、直近24時間にユーザーが閲覧した商品データのうち
  販売店舗の店舗コードの頻度をサーバー上でカウントし、
  店舗コードの頻度ランキングを独自作成ウェブサイト上で見れるようにする。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_async_db
from app.schemas.product import ShopRankEntry, ShopRankingResponse

router = APIRouter(prefix="/rankings", tags=["rankings"])


@router.get("/shops/live", response_model=ShopRankingResponse)
async def get_live_shop_rankings(
    limit: int = Query(20, ge=1, le=100, description="表示件数"),
    hours: int = Query(24, ge=1, le=168, description="集計期間 (時間)"),
    db: AsyncSession = Depends(get_async_db),
) -> ShopRankingResponse:
    """
    Live shop code frequency ranking from user browse events.
    Counts shop_code occurrences in the last N hours (default 24).
    """
    result = await db.execute(text("""
        SELECT
            ube.shop_code,
            s.shop_name,
            COUNT(*) AS browse_count
        FROM user_browse_events ube
        LEFT JOIN shops s ON ube.shop_code = s.shop_code
        WHERE ube.browsed_at >= NOW() - INTERVAL :hours HOUR
        GROUP BY ube.shop_code, s.shop_name
        ORDER BY browse_count DESC
        LIMIT :limit
    """), {"hours": hours, "limit": limit})

    rows = result.mappings().all()
    rankings = [
        ShopRankEntry(
            rank=i + 1,
            shop_code=r["shop_code"],
            shop_name=r["shop_name"],
            browse_count=r["browse_count"],
        )
        for i, r in enumerate(rows)
    ]

    return ShopRankingResponse(period_hours=hours, rankings=rankings)


@router.get("/shops/daily", response_model=ShopRankingResponse)
async def get_daily_shop_rankings(
    date: str | None = Query(
        None,
        description="ランキング日付 (YYYY-MM-DD)。省略時は最新",
    ),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_async_db),
) -> ShopRankingResponse:
    """
    Pre-aggregated daily shop rankings (from shop_rankings table).
    These are computed by the ranking_aggregator worker.
    """
    if date:
        result = await db.execute(text("""
            SELECT
                sr.shop_code,
                s.shop_name,
                sr.browse_count,
                sr.rank_position
            FROM shop_rankings sr
            LEFT JOIN shops s ON sr.shop_code = s.shop_code
            WHERE sr.ranking_date = :ranking_date
            ORDER BY sr.rank_position ASC
            LIMIT :limit
        """), {"ranking_date": date, "limit": limit})
    else:
        result = await db.execute(text("""
            SELECT
                sr.shop_code,
                s.shop_name,
                sr.browse_count,
                sr.rank_position
            FROM shop_rankings sr
            LEFT JOIN shops s ON sr.shop_code = s.shop_code
            WHERE sr.ranking_date = (
                SELECT MAX(ranking_date) FROM shop_rankings
            )
            ORDER BY sr.rank_position ASC
            LIMIT :limit
        """), {"limit": limit})

    rows = result.mappings().all()
    rankings = [
        ShopRankEntry(
            rank=r["rank_position"] or (i + 1),
            shop_code=r["shop_code"],
            shop_name=r["shop_name"],
            browse_count=r["browse_count"],
        )
        for i, r in enumerate(rows)
    ]

    return ShopRankingResponse(period_hours=24, rankings=rankings)
