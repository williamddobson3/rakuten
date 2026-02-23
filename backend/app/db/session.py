"""
Database session management.
Provides both async (FastAPI) and sync (Celery / Alembic) session factories.
"""

from __future__ import annotations

from typing import AsyncGenerator

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings

# ── Async Engine (for FastAPI) ────────────────────────────────
async_engine = create_async_engine(
    settings.DATABASE_URL_ASYNC,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_recycle=settings.DB_POOL_RECYCLE,
    pool_pre_ping=True,
    echo=False,
)

AsyncSessionLocal = async_sessionmaker(
    bind=async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_async_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency for async DB sessions."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


# ── Sync Engine (for Celery workers / Alembic) ────────────────
sync_engine = create_engine(
    settings.DATABASE_URL_SYNC,
    pool_size=10,
    max_overflow=20,
    pool_recycle=settings.DB_POOL_RECYCLE,
    pool_pre_ping=True,
    echo=False,
)

SyncSessionLocal = sessionmaker(
    bind=sync_engine,
    class_=Session,
    expire_on_commit=False,
)


def get_sync_db() -> Session:
    """Get a sync DB session (for Celery workers)."""
    return SyncSessionLocal()
