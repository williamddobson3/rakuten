"""
Application configuration.
Loads settings from environment variables / .env file.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── MySQL ──────────────────────────────────────────────────
    DB_HOST: str = "localhost"
    DB_PORT: int = 3306
    DB_USER: str = "root"
    DB_PASSWORD: str = ""
    DB_NAME: str = "rakuten_tracker"

    # Pool tuning
    DB_POOL_SIZE: int = 20
    DB_MAX_OVERFLOW: int = 40
    DB_POOL_RECYCLE: int = 3600

    @property
    def DATABASE_URL_ASYNC(self) -> str:
        """Async connection string for FastAPI (aiomysql)."""
        return (
            f"mysql+aiomysql://{self.DB_USER}:{self.DB_PASSWORD}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
            "?charset=utf8mb4"
        )

    @property
    def DATABASE_URL_SYNC(self) -> str:
        """Sync connection string for Celery workers / Alembic (pymysql)."""
        return (
            f"mysql+pymysql://{self.DB_USER}:{self.DB_PASSWORD}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
            "?charset=utf8mb4"
        )

    # ── Redis ──────────────────────────────────────────────────
    REDIS_URL: str = "redis://localhost:6379/0"

    # ── Rakuten API ────────────────────────────────────────────
    # Real endpoint: https://openapi.rakuten.co.jp/ichibams/api
    RAKUTEN_API_BASE_URL: str = "https://openapi.rakuten.co.jp/ichibams/api"
    RAKUTEN_AFFILIATE_ID: str = ""

    # Multiple API key pairs for rotation (10 pairs supported)
    # Format: each is "applicationId|accessKey", stored as comma-separated string
    # Example: "appId1|key1,appId2|key2,appId3|key3,..."
    RAKUTEN_API_KEYS: str = ""

    @property
    def api_key_pairs(self) -> list[tuple[str, str]]:
        """
        Parse RAKUTEN_API_KEYS into list of (applicationId, accessKey) tuples.

        Format: "appId1|key1,appId2|key2,..."
        """
        pairs: list[tuple[str, str]] = []
        if not self.RAKUTEN_API_KEYS:
            return pairs
        for entry in self.RAKUTEN_API_KEYS.split(","):
            entry = entry.strip()
            if "|" in entry:
                app_id, access_key = entry.split("|", 1)
                app_id = app_id.strip()
                access_key = access_key.strip()
                if app_id and access_key:
                    pairs.append((app_id, access_key))
        return pairs

    # Target genre IDs (comma-separated string → parsed as list)
    RAKUTEN_GENRE_IDS: str = "215783,100938,551169"

    @property
    def genre_id_list(self) -> list[str]:
        """Parse comma-separated genre IDs into a list."""
        return [g.strip() for g in self.RAKUTEN_GENRE_IDS.split(",") if g.strip()]

    # ── Rakuten Login (for authenticated scraping) ──────────────
    RAKUTEN_LOGIN_EMAIL: str = ""
    RAKUTEN_LOGIN_PASSWORD: str = ""

    # ── Crawl Settings ─────────────────────────────────────────
    DETAIL_CONCURRENCY: int = 30
    LIST_CONCURRENCY: int = 10
    API_CONCURRENCY: int = 10
    REQUEST_DELAY_SEC: float = 0.0  # No delay — concurrency controlled by worker -c flag
    MAX_HISTORY_DAYS: int = 730

    # Max products (raised for full Rakuten coverage)
    MAX_SERVER_CRAWL_PRODUCTS: int = 30_000_000  # 28.7M+ products
    MAX_EXTENSION_PRODUCTS: int = 5_000_000

    # Rakuten API pagination
    # The API returns 30 items per page (fixed)
    RAKUTEN_ITEMS_PER_PAGE: int = 30
    # API max pages (Rakuten limits to ~100 pages per search)
    RAKUTEN_MAX_PAGES: int = 100
    # Max items viewable per price slice = 30 * 100 = 3000
    RAKUTEN_MAX_ITEMS_PER_SLICE: int = 3000

    # Rakuten list page pagination
    # List pages show 45 items per page, max 150 pages = 6,750 items
    RAKUTEN_LIST_MAX_PAGES: int = 150

    # Detail scrape dispatcher settings
    DETAIL_BATCH_SIZE: int = 1000   # Jobs per dispatch cycle
    DETAIL_DISPATCH_INTERVAL: int = 10  # Seconds between dispatches

    # ── API Server ─────────────────────────────────────────────
    API_SECRET_KEY: str = "change-this-to-a-random-secret-key"
    API_PORT: int = 8000

    # ── Proxy ──────────────────────────────────────────────────
    PROXY_POOL_URL: str = ""


settings = Settings()
