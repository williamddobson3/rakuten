"""
FastAPI application entry point.

Rakuten Price Tracker API
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, ORJSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.router import api_router
from app.config import settings
from app.db.session import async_engine

# ── Logging ───────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ── Lifespan ──────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup/shutdown lifecycle."""
    logger.info("Starting Rakuten Price Tracker API...")
    yield
    logger.info("Shutting down Rakuten Price Tracker API...")
    await async_engine.dispose()


# ── Application ───────────────────────────────────────────────
app = FastAPI(
    title="Rakuten Price Tracker API",
    description=(
        "楽天市場商品価格トラッキングシステム — "
        "商品情報の収集・蓄積・検索・価格推移表示・ポイント計算"
    ),
    version="1.0.0",
    lifespan=lifespan,
    default_response_class=ORJSONResponse,
)

# ── CORS ──────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Restrict in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routes ────────────────────────────────────────────────────
app.include_router(api_router, prefix="/api/v1")


@app.get("/health", tags=["system"])
async def health_check():
    """Health check endpoint."""
    return {"status": "ok", "service": "rakuten-price-tracker"}


@app.get("/api/info", tags=["system"])
async def api_info():
    """API info endpoint."""
    return {
        "service": "Rakuten Price Tracker",
        "version": "1.0.0",
        "docs": "/docs",
        "health": "/health",
        "api": "/api/v1",
    }


# ── Serve Frontend (SPA) ────────────────────────────────
FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"

if FRONTEND_DIR.is_dir():
    # Serve static assets (js, css, images)
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIR / "assets"), name="assets")

    # Catch-all: serve index.html for SPA routing (must come LAST)
    @app.get("/{full_path:path}", tags=["frontend"], include_in_schema=False)
    async def serve_spa(request: Request, full_path: str):
        """Serve the React SPA for all non-API routes."""
        # Check if the requested file exists in dist
        file_path = FRONTEND_DIR / full_path
        if file_path.is_file():
            return FileResponse(file_path)
        # Fallback to index.html for SPA routing
        return FileResponse(FRONTEND_DIR / "index.html")
else:
    @app.get("/", tags=["system"])
    async def root():
        """Root endpoint (no frontend build found)."""
        return {
            "service": "Rakuten Price Tracker",
            "version": "1.0.0",
            "docs": "/docs",
            "health": "/health",
            "api": "/api/v1",
            "note": "Frontend not built. Run 'npm run build' in frontend/",
        }
