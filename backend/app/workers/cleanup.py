"""
Data cleanup / pruning workers.

Handles:
1. History cleanup: remove records older than 730 days
2. Extension product cleanup: when > 5M extension products,
   delete old low-view-count items
3. Browse event cleanup: remove old events (keep 7 days for live ranking)
4. Stale job cleanup: reset stuck/orphaned crawl jobs
"""

from __future__ import annotations

import logging

from sqlalchemy import text

from app.celery_app import celery_app
from app.config import settings
from app.db.session import get_sync_db

logger = logging.getLogger(__name__)


@celery_app.task(name="app.workers.cleanup.cleanup_old_history")
def cleanup_old_history() -> dict:
    """
    Delete variant_daily_history records older than MAX_HISTORY_DAYS (730).

    Spec requirement:
      最大730日分のデータをサーバーに蓄積
    """
    db = get_sync_db()
    try:
        max_days = settings.MAX_HISTORY_DAYS

        result = db.execute(text("""
            DELETE FROM variant_daily_history
            WHERE record_date < DATE_SUB(CURDATE(), INTERVAL :max_days DAY)
        """), {"max_days": max_days})

        deleted = result.rowcount
        db.commit()

        logger.info("Cleaned up %d history records older than %d days", deleted, max_days)
        return {"status": "ok", "deleted": deleted, "max_days": max_days}

    except Exception as e:
        logger.error("History cleanup error: %s", e, exc_info=True)
        db.rollback()
        raise
    finally:
        db.close()


@celery_app.task(name="app.workers.cleanup.cleanup_extension_products")
def cleanup_extension_products() -> dict:
    """
    When extension products exceed MAX_EXTENSION_PRODUCTS (5M),
    delete the oldest, least-viewed extension-only items.

    Spec requirement:
      拡張機能による閲覧数が500万商品を超えたら、
      拡張機能による初回閲覧日時が2ヵ月以上前で、かつ、
      拡張機能による閲覧回数が少ない商品データを自動削除して、
      拡張機能による新規閲覧商品データをサーバーに自動追加する。
    """
    db = get_sync_db()
    try:
        max_ext = settings.MAX_EXTENSION_PRODUCTS

        # Count current extension products
        count_result = db.execute(text("""
            SELECT COUNT(*) AS cnt FROM items
            WHERE seen_in_ext = 1 AND is_active = 1
        """))
        current_count = count_result.scalar() or 0

        if current_count <= max_ext:
            logger.info(
                "Extension products (%d) within limit (%d), no cleanup needed",
                current_count, max_ext,
            )
            return {
                "status": "ok",
                "current_count": current_count,
                "max": max_ext,
                "deleted": 0,
            }

        # Calculate how many to delete
        excess = current_count - max_ext
        # Delete in batches to avoid long locks
        batch_size = min(excess + 1000, 50000)  # Some buffer

        # Delete: ext_first_seen > 2 months ago, lowest view count first
        # Only delete items that are NOT also in server crawl
        result = db.execute(text("""
            UPDATE items
            SET is_active = 0
            WHERE id IN (
                SELECT id FROM (
                    SELECT id
                    FROM items
                    WHERE seen_in_ext = 1
                      AND in_server_crawl = 0
                      AND is_active = 1
                      AND ext_first_seen < DATE_SUB(NOW(), INTERVAL 2 MONTH)
                    ORDER BY ext_view_count ASC, ext_first_seen ASC
                    LIMIT :batch_size
                ) AS to_delete
            )
        """), {"batch_size": batch_size})

        deactivated = result.rowcount
        db.commit()

        logger.info(
            "Extension cleanup: deactivated %d items (was %d, limit %d)",
            deactivated, current_count, max_ext,
        )
        return {
            "status": "ok",
            "current_count": current_count,
            "max": max_ext,
            "deactivated": deactivated,
        }

    except Exception as e:
        logger.error("Extension cleanup error: %s", e, exc_info=True)
        db.rollback()
        raise
    finally:
        db.close()


@celery_app.task(name="app.workers.cleanup.cleanup_old_browse_events")
def cleanup_old_browse_events(keep_days: int = 7) -> dict:
    """
    Delete browse events older than keep_days.
    We keep 7 days for historical ranking comparison,
    but the live ranking only needs 24 hours.
    """
    db = get_sync_db()
    try:
        result = db.execute(text("""
            DELETE FROM user_browse_events
            WHERE browsed_at < DATE_SUB(NOW(), INTERVAL :keep_days DAY)
        """), {"keep_days": keep_days})

        deleted = result.rowcount
        db.commit()

        logger.info("Cleaned up %d browse events older than %d days", deleted, keep_days)
        return {"status": "ok", "deleted": deleted, "keep_days": keep_days}

    except Exception as e:
        logger.error("Browse events cleanup error: %s", e, exc_info=True)
        db.rollback()
        raise
    finally:
        db.close()


@celery_app.task(name="app.workers.cleanup.cleanup_stale_jobs")
def cleanup_stale_jobs(stale_minutes: int = 60) -> dict:
    """
    Reset jobs that have been 'running' for too long (stuck/orphaned).
    """
    db = get_sync_db()
    try:
        result = db.execute(text("""
            UPDATE crawl_jobs
            SET status = 'pending',
                locked_by = NULL,
                locked_at = NULL,
                error_message = CONCAT(
                    COALESCE(error_message, ''),
                    ' | Reset from stale running state'
                )
            WHERE status = 'running'
              AND locked_at < DATE_SUB(NOW(), INTERVAL :stale_minutes MINUTE)
              AND attempts < max_attempts
        """), {"stale_minutes": stale_minutes})

        reset = result.rowcount
        db.commit()

        # Also mark jobs that exceeded max_attempts as failed
        result2 = db.execute(text("""
            UPDATE crawl_jobs
            SET status = 'failed',
                error_message = CONCAT(
                    COALESCE(error_message, ''),
                    ' | Max attempts exceeded'
                )
            WHERE status IN ('pending', 'running')
              AND attempts >= max_attempts
        """))
        failed = result2.rowcount
        db.commit()

        logger.info("Stale job cleanup: reset %d, failed %d", reset, failed)
        return {"status": "ok", "reset": reset, "failed": failed}

    except Exception as e:
        logger.error("Stale job cleanup error: %s", e, exc_info=True)
        db.rollback()
        raise
    finally:
        db.close()
