"""
Item / Variant upsert service.

Handles the 3-stage pipeline convergence:
1. Upsert shop (if not exists)
2. Upsert item (shopCode + itemCode unique)
3. Upsert variant(s) (itemId + variantCode unique)

All three data sources (API, list scrape, detail scrape, extension)
converge here using ON DUPLICATE KEY UPDATE.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
#  SQL templates
# ═══════════════════════════════════════════════════════════════

_UPSERT_SHOP_SQL = text("""
    INSERT INTO shops (shop_code, shop_name, shop_url)
    VALUES (:shop_code, :shop_name, :shop_url)
    ON DUPLICATE KEY UPDATE
        shop_name = COALESCE(VALUES(shop_name), shop_name),
        shop_url  = COALESCE(VALUES(shop_url), shop_url)
""")

_UPSERT_ITEM_SQL = text("""
    INSERT INTO items
        (shop_code, item_code, api_item_code, canonical_url,
         item_name, catchcopy, genre_id,
         seen_in_api, seen_in_list, seen_in_detail, seen_in_ext,
         ext_first_seen, ext_view_count, in_server_crawl)
    VALUES
        (:shop_code, :item_code, :api_item_code, :canonical_url,
         :item_name, :catchcopy, :genre_id,
         :seen_in_api, :seen_in_list, :seen_in_detail, :seen_in_ext,
         :ext_first_seen, :ext_view_count, :in_server_crawl)
    ON DUPLICATE KEY UPDATE
        api_item_code = COALESCE(VALUES(api_item_code), api_item_code),
        canonical_url = COALESCE(VALUES(canonical_url), canonical_url),
        item_name     = COALESCE(VALUES(item_name), item_name),
        catchcopy     = COALESCE(VALUES(catchcopy), catchcopy),
        genre_id      = COALESCE(VALUES(genre_id), genre_id),
        seen_in_api   = seen_in_api   | VALUES(seen_in_api),
        seen_in_list  = seen_in_list  | VALUES(seen_in_list),
        seen_in_detail= seen_in_detail| VALUES(seen_in_detail),
        seen_in_ext   = seen_in_ext   | VALUES(seen_in_ext),
        ext_first_seen= COALESCE(ext_first_seen, VALUES(ext_first_seen)),
        ext_view_count= ext_view_count + VALUES(ext_view_count),
        in_server_crawl = in_server_crawl | VALUES(in_server_crawl)
""")

_GET_ITEM_ID_SQL = text("""
    SELECT id, product_uid FROM items
    WHERE shop_code = :shop_code AND item_code = :item_code
""")

_GET_ITEM_BY_UID_SQL = text("""
    SELECT id, shop_code, item_code, product_uid FROM items
    WHERE product_uid = :product_uid
""")

_UPSERT_VARIANT_SQL = text("""
    INSERT INTO variants
        (item_id, variant_code, jan_code, variant_name, selector_values)
    VALUES
        (:item_id, :variant_code, :jan_code, :variant_name, :selector_values)
    ON DUPLICATE KEY UPDATE
        jan_code        = COALESCE(VALUES(jan_code), jan_code),
        variant_name    = COALESCE(VALUES(variant_name), variant_name),
        selector_values = COALESCE(VALUES(selector_values), selector_values)
""")

_GET_VARIANT_ID_SQL = text("""
    SELECT id FROM variants
    WHERE item_id = :item_id AND variant_code = :variant_code
""")

_MARK_SERVER_CRAWL_SQL = text("""
    UPDATE items SET in_server_crawl = 1
    WHERE id = :item_id
""")


# ═══════════════════════════════════════════════════════════════
#  Source flags builder
# ═══════════════════════════════════════════════════════════════

def _source_flags(source: str) -> dict[str, Any]:
    """Build source flag dict based on data source."""
    return {
        "seen_in_api": 1 if source == "api" else 0,
        "seen_in_list": 1 if source == "list" else 0,
        "seen_in_detail": 1 if source == "detail" else 0,
        "seen_in_ext": 1 if source == "extension" else 0,
        "ext_first_seen": datetime.utcnow() if source == "extension" else None,
        "ext_view_count": 1 if source == "extension" else 0,
        "in_server_crawl": 1 if source in ("api", "list", "detail") else 0,
    }


# ═══════════════════════════════════════════════════════════════
#  Async functions (FastAPI)
# ═══════════════════════════════════════════════════════════════

async def upsert_shop_async(
    db: AsyncSession,
    shop_code: str,
    shop_name: str | None = None,
    shop_url: str | None = None,
) -> None:
    """Upsert a shop record."""
    await db.execute(_UPSERT_SHOP_SQL, {
        "shop_code": shop_code,
        "shop_name": shop_name,
        "shop_url": shop_url,
    })


async def upsert_item_async(
    db: AsyncSession,
    shop_code: str,
    item_code: str,
    canonical_url: str,
    item_name: str | None = None,
    api_item_code: str | None = None,
    catchcopy: str | None = None,
    genre_id: str | None = None,
    source: str = "detail",
) -> int:
    """
    Upsert an item record. Returns the item's DB id.

    Source flags are set based on the data source.
    The product_uid column is auto-computed by MySQL as
    ``CONCAT(shop_code, ':', item_code)``.
    """
    flags = _source_flags(source)
    await db.execute(_UPSERT_ITEM_SQL, {
        "shop_code": shop_code,
        "item_code": item_code,
        "api_item_code": api_item_code,
        "canonical_url": canonical_url,
        "item_name": item_name,
        "catchcopy": catchcopy,
        "genre_id": genre_id,
        **flags,
    })

    result = await db.execute(_GET_ITEM_ID_SQL, {
        "shop_code": shop_code,
        "item_code": item_code,
    })
    row = result.first()
    if not row:
        raise RuntimeError(
            f"Failed to get item_id after upsert: {shop_code}/{item_code}"
        )
    return row[0]


async def get_item_by_uid_async(
    db: AsyncSession, product_uid: str
) -> dict | None:
    """Look up an item by its product_uid. Returns dict or None."""
    result = await db.execute(_GET_ITEM_BY_UID_SQL, {"product_uid": product_uid})
    row = result.mappings().first()
    return dict(row) if row else None


async def upsert_variant_async(
    db: AsyncSession,
    item_id: int,
    variant_code: str = "default",
    jan_code: str | None = None,
    variant_name: str | None = None,
    selector_values: dict | None = None,
) -> int:
    """
    Upsert a variant record. Returns the variant's DB id.
    """
    import json as _json

    await db.execute(_UPSERT_VARIANT_SQL, {
        "item_id": item_id,
        "variant_code": variant_code,
        "jan_code": jan_code,
        "variant_name": variant_name,
        "selector_values": _json.dumps(selector_values) if selector_values else None,
    })

    result = await db.execute(_GET_VARIANT_ID_SQL, {
        "item_id": item_id,
        "variant_code": variant_code,
    })
    row = result.first()
    if not row:
        raise RuntimeError(
            f"Failed to get variant_id after upsert: item_id={item_id}, "
            f"variant_code={variant_code}"
        )
    return row[0]


async def mark_for_server_crawl_async(db: AsyncSession, item_id: int) -> None:
    """Mark an item to be included in server-side daily crawl."""
    await db.execute(_MARK_SERVER_CRAWL_SQL, {"item_id": item_id})


# ═══════════════════════════════════════════════════════════════
#  Sync functions (Celery workers)
# ═══════════════════════════════════════════════════════════════

def upsert_shop_sync(
    db: Session,
    shop_code: str,
    shop_name: str | None = None,
    shop_url: str | None = None,
) -> None:
    """Upsert a shop record (sync)."""
    db.execute(_UPSERT_SHOP_SQL, {
        "shop_code": shop_code,
        "shop_name": shop_name,
        "shop_url": shop_url,
    })


def upsert_item_sync(
    db: Session,
    shop_code: str,
    item_code: str,
    canonical_url: str,
    item_name: str | None = None,
    api_item_code: str | None = None,
    catchcopy: str | None = None,
    genre_id: str | None = None,
    source: str = "detail",
) -> int:
    """
    Upsert an item record (sync). Returns item DB id.

    The product_uid column is auto-computed by MySQL as
    ``CONCAT(shop_code, ':', item_code)``.
    """
    flags = _source_flags(source)
    db.execute(_UPSERT_ITEM_SQL, {
        "shop_code": shop_code,
        "item_code": item_code,
        "api_item_code": api_item_code,
        "canonical_url": canonical_url,
        "item_name": item_name,
        "catchcopy": catchcopy,
        "genre_id": genre_id,
        **flags,
    })

    result = db.execute(_GET_ITEM_ID_SQL, {
        "shop_code": shop_code,
        "item_code": item_code,
    })
    row = result.first()
    if not row:
        raise RuntimeError(
            f"Failed to get item_id after upsert: {shop_code}/{item_code}"
        )
    return row[0]


def get_item_by_uid_sync(db: Session, product_uid: str) -> dict | None:
    """Look up an item by its product_uid (sync). Returns dict or None."""
    result = db.execute(_GET_ITEM_BY_UID_SQL, {"product_uid": product_uid})
    row = result.mappings().first()
    return dict(row) if row else None


def upsert_variant_sync(
    db: Session,
    item_id: int,
    variant_code: str = "default",
    jan_code: str | None = None,
    variant_name: str | None = None,
    selector_values: dict | None = None,
) -> int:
    """Upsert a variant record (sync). Returns variant DB id."""
    import json as _json

    db.execute(_UPSERT_VARIANT_SQL, {
        "item_id": item_id,
        "variant_code": variant_code,
        "jan_code": jan_code,
        "variant_name": variant_name,
        "selector_values": _json.dumps(selector_values) if selector_values else None,
    })

    result = db.execute(_GET_VARIANT_ID_SQL, {
        "item_id": item_id,
        "variant_code": variant_code,
    })
    row = result.first()
    if not row:
        raise RuntimeError(
            f"Failed to get variant_id after upsert: item_id={item_id}, "
            f"variant_code={variant_code}"
        )
    return row[0]


def mark_for_server_crawl_sync(db: Session, item_id: int) -> None:
    """Mark an item to be included in server-side daily crawl (sync)."""
    db.execute(_MARK_SERVER_CRAWL_SQL, {"item_id": item_id})
