"""
Crawl target management endpoints.

Allows adding/managing genres, shops, and keywords to crawl.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.session import get_async_db
from app.schemas.product import CrawlTargetCreate, CrawlTargetOut

router = APIRouter(prefix="/crawl-targets", tags=["crawl-targets"])


@router.get("/", response_model=list[CrawlTargetOut])
async def list_crawl_targets(
    target_type: str | None = Query(None, pattern="^(genre|shop|keyword)$"),
    active_only: bool = Query(True),
    db: AsyncSession = Depends(get_async_db),
) -> list[CrawlTargetOut]:
    """List all crawl targets, optionally filtered by type."""
    conditions = []
    params: dict = {}

    if target_type:
        conditions.append("target_type = :target_type")
        params["target_type"] = target_type
    if active_only:
        conditions.append("is_active = 1")

    where_clause = " AND ".join(conditions) if conditions else "1=1"

    result = await db.execute(text(f"""
        SELECT id, target_type, target_value, base_url,
               is_active, priority, added_by, last_crawled_at, created_at
        FROM crawl_targets
        WHERE {where_clause}
        ORDER BY priority DESC, created_at ASC
    """), params)

    rows = result.mappings().all()
    return [CrawlTargetOut(**dict(r)) for r in rows]


@router.post("/", response_model=CrawlTargetOut)
async def create_crawl_target(
    payload: CrawlTargetCreate,
    db: AsyncSession = Depends(get_async_db),
) -> CrawlTargetOut:
    """Add a new crawl target (genre, shop, or keyword)."""
    await db.execute(text("""
        INSERT INTO crawl_targets (target_type, target_value, base_url, priority)
        VALUES (:target_type, :target_value, :base_url, :priority)
        ON DUPLICATE KEY UPDATE
            base_url = COALESCE(VALUES(base_url), base_url),
            priority = VALUES(priority),
            is_active = 1
    """), {
        "target_type": payload.target_type,
        "target_value": payload.target_value,
        "base_url": payload.base_url,
        "priority": payload.priority,
    })
    await db.commit()

    result = await db.execute(text("""
        SELECT id, target_type, target_value, base_url,
               is_active, priority, added_by, last_crawled_at, created_at
        FROM crawl_targets
        WHERE target_type = :target_type AND target_value = :target_value
    """), {
        "target_type": payload.target_type,
        "target_value": payload.target_value,
    })
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=500, detail="Failed to create crawl target")

    return CrawlTargetOut(**dict(row))


@router.delete("/{target_id}")
async def deactivate_crawl_target(
    target_id: int,
    db: AsyncSession = Depends(get_async_db),
) -> dict:
    """Deactivate a crawl target (soft delete)."""
    result = await db.execute(text("""
        UPDATE crawl_targets SET is_active = 0 WHERE id = :target_id
    """), {"target_id": target_id})
    await db.commit()

    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Crawl target not found")

    return {"status": "deactivated", "id": target_id}


@router.post("/trigger-genre-discovery")
async def trigger_genre_discovery(
    genre_id: str | None = Query(None, description="Specific genre ID, or omit for all"),
) -> dict:
    """
    Manually trigger API discovery for genre(s).

    - If genre_id is provided, discovers that specific genre.
    - If omitted, discovers all 3 configured genres (215783, 100938, 551169).
    """
    from app.workers.api_discovery import discover_all_genres, discover_by_genre

    if genre_id:
        discover_by_genre.delay(genre_id, 0, None, 1)
        return {
            "status": "triggered",
            "genre_id": genre_id,
            "message": f"Discovery started for genre {genre_id}",
        }
    else:
        discover_all_genres.delay()
        return {
            "status": "triggered",
            "genre_ids": settings.genre_id_list,
            "message": f"Discovery started for all {len(settings.genre_id_list)} genres",
        }


@router.post("/trigger-list-scrape")
async def trigger_list_scrape(
    genre_id: str | None = Query(None, description="Specific genre ID, or omit for all 3"),
) -> dict:
    """
    Manually trigger list page scraping for genre(s).

    Scrapes product listing pages (45 items/page, up to 150 pages = 6,750 items per genre).
    - If genre_id is provided, scrapes that specific genre.
    - If omitted, scrapes all 3 configured genres (215783, 100938, 551169).
    """
    from app.workers.list_scraper import scrape_list_paginated

    genre_ids = [genre_id] if genre_id else settings.genre_id_list
    tasks_dispatched = 0

    for gid in genre_ids:
        base_url = f"https://search.rakuten.co.jp/search/mall/-/{gid}/"
        scrape_list_paginated.delay(
            base_url=base_url,
            genre_id=gid,
        )
        tasks_dispatched += 1

    return {
        "status": "triggered",
        "genre_ids": genre_ids,
        "tasks_dispatched": tasks_dispatched,
        "message": f"List scraping started for {len(genre_ids)} genre(s). "
                   f"Each genre will scrape up to 150 pages (6,750 products).",
    }


@router.post("/trigger-all")
async def trigger_all_scraping(
    genre_id: str | None = Query(None, description="Specific genre ID, or omit for all configured"),
) -> dict:
    """
    **Full 3-stage pipeline** for configured genre(s).

    Pipeline per price slice:
      1. **API Discovery** → 3,000 products per slice (name, price, image, points)
      2. **List Page Scraping** → auto-triggered when each API slice finishes
         (enriches with coupons, point breakdown, shipping, sold-out status)
      3. **Detail Page Scraping** → auto-enqueued per product by both stages 1 & 2
         (full variant data: JAN codes, all SKU variants, stock, delivery days)

    **Price slicing for full coverage:**
      Genres with >3,000 products are automatically split into sub-price-ranges.
      Each sub-range processes up to 3,000 products, covering ALL products.

    Example: Genre 100938 (1.35M products) → ~450 price slices → 1.35M products covered.
    """
    from app.workers.api_discovery import discover_all_genres, discover_by_genre

    genre_ids = [genre_id] if genre_id else settings.genre_id_list

    # Trigger API discovery (list scraping auto-follows each completed slice)
    if genre_id:
        discover_by_genre.delay(genre_id, 0, None, 1)
    else:
        discover_all_genres.delay()

    return {
        "status": "triggered",
        "genre_ids": genre_ids,
        "pipeline": "API → List → Detail (automatic)",
        "message": (
            f"Full 3-stage pipeline started for {len(genre_ids)} genre(s). "
            f"Stage 1: API discovery with auto price-slicing for full coverage. "
            f"Stage 2: List scraping auto-triggered per completed slice. "
            f"Stage 3: Detail scraping auto-enqueued per product."
        ),
    }


@router.post("/trigger-full-crawl")
async def trigger_full_crawl(
    sub_genre_depth: int = Query(
        0,
        description="Depth of sub-genre splitting (0=top-level only, 1=one level deeper)",
    ),
) -> dict:
    """
    **🚀 FULL CRAWL: Extract ALL 28.7M+ products from Rakuten.**

    This endpoint auto-discovers ALL Rakuten genres via the GenreSearch API,
    then triggers the full 3-stage pipeline for each genre.

    Pipeline:
      1. **Genre Discovery** → IchibaGenre/Search API → ~36 top-level genres
      2. **Per-genre API Discovery** → auto price-slicing → covers ALL products
      3. **List Page Scraping** → auto-triggered per completed API slice
      4. **Detail Page Scraping** → auto-enqueued per product

    The entire pipeline is fully automatic after this single API call.

    Args:
        sub_genre_depth: Split each top-level genre into sub-genres at this depth.
                         0 = use top-level genres directly (simpler, recommended).
                         1 = use 2nd-level genres (more parallelism, ~500 tasks).
    """
    from app.workers.api_discovery import discover_all_rakuten_genres

    discover_all_rakuten_genres.delay(sub_genre_depth=sub_genre_depth)

    return {
        "status": "triggered",
        "pipeline": "Genre Discovery → API → List → Detail (fully automatic)",
        "sub_genre_depth": sub_genre_depth,
        "message": (
            "🚀 FULL CRAWL initiated. "
            "Step 1: Discovering all Rakuten genres via GenreSearch API. "
            "Step 2: For each genre, API discovery with auto price-slicing. "
            "Step 3: List page scraping auto-triggered per completed slice. "
            "Step 4: Detail scraping auto-enqueued per product. "
            "All 28.7M+ products will be covered automatically."
        ),
    }


@router.post("/refresh-cookies")
async def refresh_scraper_cookies() -> dict:
    """
    Force all running scrapers to reload auth cookies from storage.

    Call this after logging in (POST /auth/login or /auth/login-manual)
    to inject fresh cookies into the running Playwright browser contexts
    WITHOUT restarting the Celery worker.

    Sends Celery tasks to the worker process so the cookie refresh
    happens in the same process as the running browser contexts.

    The scrapers also auto-refresh cookies every 5 minutes, but this
    endpoint triggers an immediate refresh.
    """
    tasks = {}

    try:
        from app.workers.list_scraper import refresh_auth_cookies as list_refresh
        task = list_refresh.delay()
        tasks["list_scraper"] = {"task_id": task.id, "status": "dispatched"}
    except Exception as e:
        tasks["list_scraper"] = f"error: {e}"

    try:
        from app.workers.detail_scraper import refresh_auth_cookies as detail_refresh
        task = detail_refresh.delay()
        tasks["detail_scraper"] = {"task_id": task.id, "status": "dispatched"}
    except Exception as e:
        tasks["detail_scraper"] = f"error: {e}"

    return {
        "status": "ok",
        "message": "Cookie refresh tasks dispatched to Celery worker",
        "tasks": tasks,
    }


@router.post("/login-and-scrape")
async def login_and_scrape(
    genre_id: str | None = Query(None, description="Specific genre ID, or omit for all"),
    login_method: str = Query("auto", description="Login method: 'auto', 'manual', or 'fallback'"),
    full_crawl: bool = Query(False, description="If true, discover ALL Rakuten genres (28.7M+ products)"),
) -> dict:
    """
    **Recommended production workflow**: Login first, then trigger scraping.

    This endpoint performs:
      1. Login to Rakuten (auto/manual/fallback)
      2. Store auth cookies in Redis + file
      3. Refresh cookies in running scraper browser contexts
      4. Trigger API discovery + list page scraping for all genres

    This ensures scrapers run in AUTHENTICATED mode, capturing:
      - User-specific coupons
      - Point multiplier breakdowns
      - Login-only price information
      - SPU (Super Point Up) details

    Login methods:
      - "auto": Headless Playwright login (fastest, may fail with CAPTCHA)
      - "manual": Opens visible browser window for manual login (5 min timeout)
      - "fallback": Try auto first, then fall back to manual
    """
    from app.services.rakuten_auth import (
        login_auto,
        login_manual,
        login_with_fallback,
    )

    # ── Step 1: Login ─────────────────────────────────────────
    email = settings.RAKUTEN_LOGIN_EMAIL
    password = settings.RAKUTEN_LOGIN_PASSWORD

    if login_method == "manual":
        login_result = login_manual(timeout_sec=300)
    elif login_method == "fallback":
        if not email or not password:
            login_result = login_manual(timeout_sec=300)
        else:
            login_result = login_with_fallback(email, password)
    else:  # auto
        if not email or not password:
            return {
                "status": "error",
                "message": (
                    "No credentials configured. Set RAKUTEN_LOGIN_EMAIL and "
                    "RAKUTEN_LOGIN_PASSWORD in .env, or use login_method='manual'."
                ),
            }
        login_result = login_auto(email, password)

    if not login_result.get("success"):
        return {
            "status": "login_failed",
            "message": login_result.get("message", "Login failed"),
            "suggestion": "Try login_method='manual' to login in a visible browser",
        }

    # ── Step 2: Refresh cookies in running scrapers (via Celery tasks) ─
    try:
        from app.workers.list_scraper import refresh_auth_cookies as lr
        lr.delay()
    except Exception:
        pass
    try:
        from app.workers.detail_scraper import refresh_auth_cookies as dr
        dr.delay()
    except Exception:
        pass

    # ── Step 3: Trigger full 3-stage pipeline ─────────────────
    # API discovery starts → auto price-slicing → list scraping auto-follows
    # → detail scraping auto-enqueued per product
    from app.workers.api_discovery import (
        discover_all_genres,
        discover_all_rakuten_genres,
        discover_by_genre,
    )

    if full_crawl:
        # Discover ALL genres from Rakuten API → covers all 28.7M+ products
        discover_all_rakuten_genres.delay()
        pipeline_msg = (
            "🚀 FULL CRAWL: Login successful → cookies injected → "
            "discovering ALL Rakuten genres → full 3-stage pipeline "
            "in authenticated mode. All 28.7M+ products will be covered."
        )
        genre_ids = ["ALL (auto-discovered)"]
    else:
        genre_ids = [genre_id] if genre_id else settings.genre_id_list
        if genre_id:
            discover_by_genre.delay(genre_id, 0, None, 1)
        else:
            discover_all_genres.delay()
        pipeline_msg = (
            f"✅ Login successful → cookies injected → full 3-stage pipeline "
            f"started for {len(genre_ids)} genre(s) in authenticated mode. "
            f"All products will be covered via automatic price range splitting."
        )

    return {
        "status": "ok",
        "login": {
            "success": True,
            "method": login_result.get("method", login_method),
            "cookie_count": login_result.get("cookie_count", 0),
        },
        "cookies_refreshed": "dispatched to worker",
        "pipeline": {
            "genre_ids": genre_ids,
            "full_crawl": full_crawl,
            "stage_1": "API discovery (with auto price-slicing for full coverage)",
            "stage_2": "List scraping (auto-triggered per completed API slice)",
            "stage_3": "Detail scraping (auto-enqueued per product)",
        },
        "message": pipeline_msg,
    }


@router.get("/api-key-status")
async def api_key_status() -> dict:
    """
    Check the status of all 10 API key pairs.

    Returns:
      - total_keys: number of configured keys
      - available_keys: keys not currently rate-limited
      - keys[]: per-key status (index, masked app_id, rate_limited, cooldown)
    """
    from app.workers.api_discovery import get_key_rotation_status
    return get_key_rotation_status()
