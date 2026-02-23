"""
API router aggregation.
Mounts all sub-routers under /api/v1.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.auth import router as auth_router
from app.api.crawl_targets import router as crawl_targets_router
from app.api.extension import router as extension_router
from app.api.history import router as history_router
from app.api.products import router as products_router
from app.api.rankings import router as rankings_router

api_router = APIRouter()

api_router.include_router(auth_router)
api_router.include_router(products_router)
api_router.include_router(history_router)
api_router.include_router(rankings_router)
api_router.include_router(extension_router)
api_router.include_router(crawl_targets_router)
