"""
Snapshot upsert service.

Handles the core data-flow rules:
1. variant_snapshots:  always overwrite with latest data
2. variant_daily_history:  (variant_id, record_date) unique;
   same day → overwrite with latest (keep newest fetched_at)
3. Differential update: compute content_hash; skip write if unchanged
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Fields used for the content hash (order matters for determinism)
_HASH_FIELDS = (
    "price",
    "point_rate",
    "point_back_percent",
    "coupon_yen",
    "coupon_percent",
    "shipping_text_raw",
    "shipping_days_min",
    "shipping_days_max",
    "image_url",
    "extra1_key",
    "extra1_value",
    "extra2_key",
    "extra2_value",
)


def compute_content_hash(data: dict[str, Any]) -> str:
    """SHA-256 hash of snapshot fields for differential-update detection."""
    subset = {k: data.get(k) for k in _HASH_FIELDS}
    raw = json.dumps(subset, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _build_snapshot_params(
    variant_id: int,
    data: dict[str, Any],
    content_hash: str,
    fetched_at: datetime,
    source: str,
) -> dict[str, Any]:
    """Build the parameter dict used in both snapshot and history upserts."""
    return {
        "variant_id": variant_id,
        "price": data.get("price"),
        "point_rate": data.get("point_rate"),
        "point_back_percent": data.get("point_back_percent"),
        "coupon_yen": data.get("coupon_yen"),
        "coupon_percent": data.get("coupon_percent"),
        "shipping_text_raw": data.get("shipping_text_raw"),
        "shipping_days_min": data.get("shipping_days_min"),
        "shipping_days_max": data.get("shipping_days_max"),
        "image_url": data.get("image_url"),
        "extra1_key": data.get("extra1_key"),
        "extra1_value": data.get("extra1_value"),
        "extra2_key": data.get("extra2_key"),
        "extra2_value": data.get("extra2_value"),
        "content_hash": content_hash,
        "fetched_at": fetched_at,
        "source": source,
    }


# ═══════════════════════════════════════════════════════════════
#  SQL templates (MySQL ON DUPLICATE KEY UPDATE)
# ═══════════════════════════════════════════════════════════════

_UPSERT_SNAPSHOT_SQL = text("""
    INSERT INTO variant_snapshots
        (variant_id, price, point_rate, point_back_percent,
         coupon_yen, coupon_percent, shipping_text_raw,
         shipping_days_min, shipping_days_max, image_url,
         extra1_key, extra1_value, extra2_key, extra2_value,
         content_hash, fetched_at, source)
    VALUES
        (:variant_id, :price, :point_rate, :point_back_percent,
         :coupon_yen, :coupon_percent, :shipping_text_raw,
         :shipping_days_min, :shipping_days_max, :image_url,
         :extra1_key, :extra1_value, :extra2_key, :extra2_value,
         :content_hash, :fetched_at, :source)
    ON DUPLICATE KEY UPDATE
        price              = VALUES(price),
        point_rate         = VALUES(point_rate),
        point_back_percent = VALUES(point_back_percent),
        coupon_yen         = VALUES(coupon_yen),
        coupon_percent     = VALUES(coupon_percent),
        shipping_text_raw  = VALUES(shipping_text_raw),
        shipping_days_min  = VALUES(shipping_days_min),
        shipping_days_max  = VALUES(shipping_days_max),
        image_url          = VALUES(image_url),
        extra1_key         = VALUES(extra1_key),
        extra1_value       = VALUES(extra1_value),
        extra2_key         = VALUES(extra2_key),
        extra2_value       = VALUES(extra2_value),
        content_hash       = VALUES(content_hash),
        fetched_at         = VALUES(fetched_at),
        source             = VALUES(source)
""")

_UPSERT_HISTORY_SQL = text("""
    INSERT INTO variant_daily_history
        (variant_id, record_date, price, point_rate, point_back_percent,
         coupon_yen, coupon_percent, shipping_days_min, shipping_days_max,
         image_url, extra1_key, extra1_value, extra2_key, extra2_value,
         content_hash, fetched_at, source)
    VALUES
        (:variant_id, :record_date, :price, :point_rate, :point_back_percent,
         :coupon_yen, :coupon_percent, :shipping_days_min, :shipping_days_max,
         :image_url, :extra1_key, :extra1_value, :extra2_key, :extra2_value,
         :content_hash, :fetched_at, :source)
    ON DUPLICATE KEY UPDATE
        price              = VALUES(price),
        point_rate         = VALUES(point_rate),
        point_back_percent = VALUES(point_back_percent),
        coupon_yen         = VALUES(coupon_yen),
        coupon_percent     = VALUES(coupon_percent),
        shipping_days_min  = VALUES(shipping_days_min),
        shipping_days_max  = VALUES(shipping_days_max),
        image_url          = VALUES(image_url),
        extra1_key         = VALUES(extra1_key),
        extra1_value       = VALUES(extra1_value),
        extra2_key         = VALUES(extra2_key),
        extra2_value       = VALUES(extra2_value),
        content_hash       = VALUES(content_hash),
        fetched_at         = VALUES(fetched_at),
        source             = VALUES(source)
""")

_CHECK_HASH_SQL = text("""
    SELECT content_hash FROM variant_snapshots
    WHERE variant_id = :variant_id
""")


# ═══════════════════════════════════════════════════════════════
#  Async version (for FastAPI / extension endpoint)
# ═══════════════════════════════════════════════════════════════

async def upsert_snapshot_and_history_async(
    db: AsyncSession,
    variant_id: int,
    snapshot_data: dict[str, Any],
    source: str = "detail",
    skip_if_unchanged: bool = True,
) -> bool:
    """
    Upsert current snapshot + daily history (async).

    Args:
        db: async SQLAlchemy session
        variant_id: DB primary key of the variant
        snapshot_data: dict with price, point_rate, coupon_yen, etc.
        source: one of "api", "list", "detail", "extension"
        skip_if_unchanged: if True, skip write when content_hash matches

    Returns:
        True if data was written (new or changed), False if skipped
    """
    now = datetime.utcnow()
    today = date.today()
    content_hash = compute_content_hash(snapshot_data)

    # Differential check
    if skip_if_unchanged:
        result = await db.execute(_CHECK_HASH_SQL, {"variant_id": variant_id})
        row = result.first()
        if row and row[0] == content_hash:
            logger.debug(
                "Skipping upsert for variant_id=%s (hash unchanged)", variant_id
            )
            return False

    params = _build_snapshot_params(variant_id, snapshot_data, content_hash, now, source)

    # Upsert current snapshot
    await db.execute(_UPSERT_SNAPSHOT_SQL, params)

    # Upsert daily history
    history_params = {**params, "record_date": today}
    await db.execute(_UPSERT_HISTORY_SQL, history_params)

    await db.commit()
    logger.debug("Upserted snapshot+history for variant_id=%s", variant_id)
    return True


# ═══════════════════════════════════════════════════════════════
#  Sync version (for Celery workers)
# ═══════════════════════════════════════════════════════════════

def upsert_snapshot_and_history_sync(
    db: Session,
    variant_id: int,
    snapshot_data: dict[str, Any],
    source: str = "detail",
    skip_if_unchanged: bool = True,
) -> bool:
    """
    Upsert current snapshot + daily history (sync version for Celery).

    Same logic as async version.
    """
    now = datetime.utcnow()
    today = date.today()
    content_hash = compute_content_hash(snapshot_data)

    # Differential check
    if skip_if_unchanged:
        result = db.execute(_CHECK_HASH_SQL, {"variant_id": variant_id})
        row = result.first()
        if row and row[0] == content_hash:
            logger.debug(
                "Skipping upsert for variant_id=%s (hash unchanged)", variant_id
            )
            return False

    params = _build_snapshot_params(variant_id, snapshot_data, content_hash, now, source)

    # Upsert current snapshot
    db.execute(_UPSERT_SNAPSHOT_SQL, params)

    # Upsert daily history
    history_params = {**params, "record_date": today}
    db.execute(_UPSERT_HISTORY_SQL, history_params)

    db.commit()
    logger.debug("Upserted snapshot+history for variant_id=%s", variant_id)
    return True


# ═══════════════════════════════════════════════════════════════
#  Batch upsert (for Celery workers processing many variants)
# ═══════════════════════════════════════════════════════════════

def batch_upsert_snapshots_sync(
    db: Session,
    records: list[tuple[int, dict[str, Any], str]],
) -> int:
    """
    Batch upsert multiple variant snapshots + histories.

    Args:
        db: sync SQLAlchemy session
        records: list of (variant_id, snapshot_data, source)

    Returns:
        Number of records actually written (non-skipped)
    """
    now = datetime.utcnow()
    today = date.today()
    written = 0

    for variant_id, snapshot_data, source in records:
        content_hash = compute_content_hash(snapshot_data)
        params = _build_snapshot_params(
            variant_id, snapshot_data, content_hash, now, source
        )

        db.execute(_UPSERT_SNAPSHOT_SQL, params)

        history_params = {**params, "record_date": today}
        db.execute(_UPSERT_HISTORY_SQL, history_params)
        written += 1

    db.commit()
    logger.info("Batch upserted %d snapshot+history records", written)
    return written
