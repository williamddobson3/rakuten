"""
Ranking Aggregator Worker.

Handles:
1. Aggregating shop code frequency rankings from user_browse_events
2. Storing pre-computed rankings in shop_rankings table
3. Auto-adding Top 3 shops as crawl targets (Phase 5 requirement)

Phase 5 spec:
  拡張機能で、直近24時間にユーザーが閲覧した商品データのうち
  販売店舗の店舗コードの頻度をサーバー上でカウントし、
  店舗コードの頻度ランキングがTop3から、
  店舗商品一覧サイトをサーバー上で毎日3店舗分だけ自動指定して、
  ページ数を自動で変化させながら全体クロールできるようにしておく。
"""

from __future__ import annotations

import logging
from datetime import date, datetime

from sqlalchemy import text

from app.celery_app import celery_app
from app.db.session import get_sync_db

logger = logging.getLogger(__name__)


@celery_app.task(name="app.workers.ranking_aggregator.aggregate_shop_rankings")
def aggregate_shop_rankings() -> dict:
    """
    Aggregate shop code frequency from browse events (last 24 hours).
    Stores results in shop_rankings table with rank positions.
    """
    db = get_sync_db()
    try:
        today = date.today()

        # Aggregate counts from last 24 hours
        result = db.execute(text("""
            SELECT
                shop_code,
                COUNT(*) AS browse_count
            FROM user_browse_events
            WHERE browsed_at >= NOW() - INTERVAL 24 HOUR
            GROUP BY shop_code
            ORDER BY browse_count DESC
        """))
        rows = result.mappings().all()

        if not rows:
            logger.info("No browse events in last 24 hours")
            return {"status": "ok", "rankings_count": 0}

        # Delete existing rankings for today (replace)
        db.execute(text("""
            DELETE FROM shop_rankings WHERE ranking_date = :today
        """), {"today": today})

        # Insert new rankings with positions
        for rank, row in enumerate(rows, 1):
            db.execute(text("""
                INSERT INTO shop_rankings
                    (ranking_date, shop_code, browse_count, rank_position)
                VALUES
                    (:ranking_date, :shop_code, :browse_count, :rank_position)
            """), {
                "ranking_date": today,
                "shop_code": row["shop_code"],
                "browse_count": row["browse_count"],
                "rank_position": rank,
            })

        db.commit()

        logger.info("Aggregated %d shop rankings for %s", len(rows), today)
        return {"status": "ok", "rankings_count": len(rows)}

    except Exception as e:
        logger.error("Ranking aggregation error: %s", e, exc_info=True)
        db.rollback()
        raise
    finally:
        db.close()


@celery_app.task(name="app.workers.ranking_aggregator.auto_add_top_shops")
def auto_add_top_shops(top_n: int = 3) -> dict:
    """
    Auto-add Top N shops from the latest ranking as crawl targets.

    Phase 5 requirement:
    Take the top 3 shops by browse frequency and automatically
    add them as daily crawl targets for full store scraping.
    """
    db = get_sync_db()
    try:
        # Get latest top N shops
        result = db.execute(text("""
            SELECT shop_code, browse_count
            FROM shop_rankings
            WHERE ranking_date = (SELECT MAX(ranking_date) FROM shop_rankings)
            ORDER BY rank_position ASC
            LIMIT :top_n
        """), {"top_n": top_n})
        top_shops = result.mappings().all()

        if not top_shops:
            logger.info("No shop rankings available for auto-add")
            return {"status": "ok", "shops_added": 0}

        shops_added = 0
        for shop in top_shops:
            shop_code = shop["shop_code"]

            # Upsert as crawl target (with high priority)
            db.execute(text("""
                INSERT INTO crawl_targets
                    (target_type, target_value, base_url, is_active,
                     priority, added_by)
                VALUES
                    ('shop', :shop_code,
                     CONCAT('https://search.rakuten.co.jp/search/mall/?sid=', :shop_code),
                     1, 100, 'auto_ranking')
                ON DUPLICATE KEY UPDATE
                    is_active = 1,
                    priority = GREATEST(priority, 100),
                    updated_at = NOW()
            """), {"shop_code": shop_code})
            shops_added += 1

        db.commit()

        logger.info(
            "Auto-added %d top shops as crawl targets: %s",
            shops_added,
            [s["shop_code"] for s in top_shops],
        )
        return {
            "status": "ok",
            "shops_added": shops_added,
            "shop_codes": [s["shop_code"] for s in top_shops],
        }

    except Exception as e:
        logger.error("Auto-add top shops error: %s", e, exc_info=True)
        db.rollback()
        raise
    finally:
        db.close()
