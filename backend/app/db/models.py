"""
SQLAlchemy ORM models.
These mirror the MySQL schema in sql/init.sql.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Computed,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    func,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
)


class Base(DeclarativeBase):
    pass


# ─────────────────────────────────────────────────────────────
# 1. Shops
# ─────────────────────────────────────────────────────────────
class Shop(Base):
    __tablename__ = "shops"

    shop_code: Mapped[str] = mapped_column(String(64), primary_key=True)
    shop_name: Mapped[str | None] = mapped_column(String(512), default=None)
    shop_url: Mapped[str | None] = mapped_column(String(2048), default=None)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    items: Mapped[list[Item]] = relationship("Item", back_populates="shop")


# ─────────────────────────────────────────────────────────────
# 2. Items
# ─────────────────────────────────────────────────────────────
class Item(Base):
    __tablename__ = "items"
    __table_args__ = (
        Index("uk_shop_item", "shop_code", "item_code", unique=True),
        Index("uk_product_uid", "product_uid", unique=True),
        Index("idx_canonical_url", "canonical_url", mysql_length=255),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(BigInteger, "mysql"),
        primary_key=True,
        autoincrement=True,
    )
    product_uid: Mapped[str] = mapped_column(
        String(320),
        Computed("CONCAT(shop_code, ':', item_code)", persisted=True),
        comment="商品ユニークID ({shop_code}:{item_code})",
    )
    shop_code: Mapped[str] = mapped_column(
        String(64), ForeignKey("shops.shop_code", onupdate="CASCADE"), nullable=False
    )
    item_code: Mapped[str] = mapped_column(String(255), nullable=False)
    api_item_code: Mapped[str | None] = mapped_column(
        String(255), default=None, index=True,
        comment="API itemCode (e.g. shopCode:productId)",
    )
    canonical_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    item_name: Mapped[str | None] = mapped_column(String(1024), default=None)
    catchcopy: Mapped[str | None] = mapped_column(String(2048), default=None)
    genre_id: Mapped[str | None] = mapped_column(
        String(32), default=None, index=True,
    )

    # Source flags
    seen_in_api: Mapped[bool] = mapped_column(Boolean, default=False)
    seen_in_list: Mapped[bool] = mapped_column(Boolean, default=False)
    seen_in_detail: Mapped[bool] = mapped_column(Boolean, default=False)
    seen_in_ext: Mapped[bool] = mapped_column(Boolean, default=False)

    # Extension tracking
    ext_first_seen: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    ext_view_count: Mapped[int] = mapped_column(Integer, default=0)
    in_server_crawl: Mapped[bool] = mapped_column(Boolean, default=False)

    # Lifecycle
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    shop: Mapped[Shop] = relationship("Shop", back_populates="items")
    variants: Mapped[list[Variant]] = relationship(
        "Variant", back_populates="item", cascade="all, delete-orphan"
    )
    list_observations: Mapped[list[ListObservation]] = relationship(
        "ListObservation", back_populates="item", cascade="all, delete-orphan"
    )
    detail_observations: Mapped[list[DetailObservation]] = relationship(
        "DetailObservation", back_populates="item", cascade="all, delete-orphan"
    )


# ─────────────────────────────────────────────────────────────
# 3. Variants
# ─────────────────────────────────────────────────────────────
class Variant(Base):
    __tablename__ = "variants"
    __table_args__ = (
        Index("uk_item_variant", "item_id", "variant_code", unique=True),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(BigInteger, "mysql"),
        primary_key=True,
        autoincrement=True,
    )
    item_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("items.id", onupdate="CASCADE", ondelete="CASCADE"),
        nullable=False,
    )
    variant_code: Mapped[str] = mapped_column(String(128), default="default")
    jan_code: Mapped[str | None] = mapped_column(String(16), default=None, index=True)
    variant_name: Mapped[str | None] = mapped_column(String(512), default=None)
    selector_values: Mapped[dict | None] = mapped_column(JSON, default=None)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    item: Mapped[Item] = relationship("Item", back_populates="variants")
    snapshot: Mapped[VariantSnapshot | None] = relationship(
        "VariantSnapshot", back_populates="variant", uselist=False,
        cascade="all, delete-orphan",
    )
    daily_history: Mapped[list[VariantDailyHistory]] = relationship(
        "VariantDailyHistory", back_populates="variant",
        cascade="all, delete-orphan",
    )


# ─────────────────────────────────────────────────────────────
# 4. Variant Snapshots (current state)
# ─────────────────────────────────────────────────────────────
class VariantSnapshot(Base):
    __tablename__ = "variant_snapshots"

    variant_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("variants.id", onupdate="CASCADE", ondelete="CASCADE"),
        primary_key=True,
    )
    price: Mapped[int | None] = mapped_column(Integer, default=None)
    point_rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 1), default=None)
    point_back_percent: Mapped[Decimal | None] = mapped_column(
        Numeric(5, 1), default=None
    )
    coupon_yen: Mapped[int | None] = mapped_column(Integer, default=None)
    coupon_percent: Mapped[Decimal | None] = mapped_column(
        Numeric(5, 1), default=None
    )
    shipping_text_raw: Mapped[str | None] = mapped_column(String(512), default=None)
    shipping_days_min: Mapped[int | None] = mapped_column(SmallInteger, default=None)
    shipping_days_max: Mapped[int | None] = mapped_column(SmallInteger, default=None)
    image_url: Mapped[str | None] = mapped_column(String(2048), default=None)
    extra1_key: Mapped[str | None] = mapped_column(String(128), default=None)
    extra1_value: Mapped[str | None] = mapped_column(String(1024), default=None)
    extra2_key: Mapped[str | None] = mapped_column(String(128), default=None)
    extra2_value: Mapped[str | None] = mapped_column(String(1024), default=None)

    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    source: Mapped[str] = mapped_column(
        Enum("api", "list", "detail", "extension"), default="detail"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    variant: Mapped[Variant] = relationship("Variant", back_populates="snapshot")


# ─────────────────────────────────────────────────────────────
# 5. Variant Daily History
# ─────────────────────────────────────────────────────────────
class VariantDailyHistory(Base):
    __tablename__ = "variant_daily_history"
    __table_args__ = (
        Index("uk_variant_date", "variant_id", "record_date", unique=True),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(BigInteger, "mysql"),
        primary_key=True,
        autoincrement=True,
    )
    variant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    record_date: Mapped[date] = mapped_column(Date, nullable=False)
    price: Mapped[int | None] = mapped_column(Integer, default=None)
    point_rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 1), default=None)
    point_back_percent: Mapped[Decimal | None] = mapped_column(
        Numeric(5, 1), default=None
    )
    coupon_yen: Mapped[int | None] = mapped_column(Integer, default=None)
    coupon_percent: Mapped[Decimal | None] = mapped_column(
        Numeric(5, 1), default=None
    )
    shipping_days_min: Mapped[int | None] = mapped_column(SmallInteger, default=None)
    shipping_days_max: Mapped[int | None] = mapped_column(SmallInteger, default=None)
    image_url: Mapped[str | None] = mapped_column(String(2048), default=None)
    extra1_key: Mapped[str | None] = mapped_column(String(128), default=None)
    extra1_value: Mapped[str | None] = mapped_column(String(1024), default=None)
    extra2_key: Mapped[str | None] = mapped_column(String(128), default=None)
    extra2_value: Mapped[str | None] = mapped_column(String(1024), default=None)

    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    source: Mapped[str] = mapped_column(
        Enum("api", "list", "detail", "extension"), default="detail"
    )

    # Relationships
    variant: Mapped[Variant] = relationship("Variant", back_populates="daily_history")


# ─────────────────────────────────────────────────────────────
# 6. Crawl Targets
# ─────────────────────────────────────────────────────────────
class CrawlTarget(Base):
    __tablename__ = "crawl_targets"
    __table_args__ = (
        Index("uk_type_value", "target_type", "target_value", unique=True, mysql_length={"target_value": 255}),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(BigInteger, "mysql"),
        primary_key=True,
        autoincrement=True,
    )
    target_type: Mapped[str] = mapped_column(
        Enum("genre", "shop", "keyword"), nullable=False
    )
    target_value: Mapped[str] = mapped_column(String(512), nullable=False)
    base_url: Mapped[str | None] = mapped_column(String(2048), default=None)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(SmallInteger, default=0)
    added_by: Mapped[str] = mapped_column(
        Enum("manual", "auto_ranking", "extension"), default="manual"
    )
    last_crawled_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    price_slices: Mapped[list[PriceRangeSlice]] = relationship(
        "PriceRangeSlice", back_populates="crawl_target", cascade="all, delete-orphan"
    )


# ─────────────────────────────────────────────────────────────
# 7. Price Range Slices
# ─────────────────────────────────────────────────────────────
class PriceRangeSlice(Base):
    __tablename__ = "price_range_slices"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(BigInteger, "mysql"),
        primary_key=True,
        autoincrement=True,
    )
    crawl_target_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("crawl_targets.id", onupdate="CASCADE", ondelete="CASCADE"),
        nullable=False,
    )
    min_price: Mapped[int] = mapped_column(Integer, default=0)
    max_price: Mapped[int] = mapped_column(Integer, nullable=False)
    estimated_count: Mapped[int | None] = mapped_column(Integer, default=None)
    last_crawled_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    status: Mapped[str] = mapped_column(
        Enum("pending", "in_progress", "done", "needs_split"), default="pending"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    crawl_target: Mapped[CrawlTarget] = relationship(
        "CrawlTarget", back_populates="price_slices"
    )


# ─────────────────────────────────────────────────────────────
# 8. Crawl Jobs
# ─────────────────────────────────────────────────────────────
class CrawlJob(Base):
    __tablename__ = "crawl_jobs"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(BigInteger, "mysql"),
        primary_key=True,
        autoincrement=True,
    )
    job_type: Mapped[str] = mapped_column(
        Enum("api_discovery", "list_scrape", "detail_scrape"), nullable=False
    )
    target_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    params_json: Mapped[dict | None] = mapped_column(JSON, default=None)
    item_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    status: Mapped[str] = mapped_column(
        Enum("pending", "running", "done", "failed", "skipped"), default="pending"
    )
    attempts: Mapped[int] = mapped_column(SmallInteger, default=0)
    max_attempts: Mapped[int] = mapped_column(SmallInteger, default=3)
    locked_by: Mapped[str | None] = mapped_column(String(128), default=None)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    error_message: Mapped[str | None] = mapped_column(Text, default=None)
    result_summary: Mapped[dict | None] = mapped_column(JSON, default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)


# ─────────────────────────────────────────────────────────────
# 9. User Browse Events
# ─────────────────────────────────────────────────────────────
class UserBrowseEvent(Base):
    __tablename__ = "user_browse_events"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(BigInteger, "mysql"),
        primary_key=True,
        autoincrement=True,
    )
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    variant_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    item_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    shop_code: Mapped[str] = mapped_column(String(64), nullable=False)
    page_url: Mapped[str | None] = mapped_column(String(2048), default=None)
    browsed_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )


# ─────────────────────────────────────────────────────────────
# 10. Shop Rankings
# ─────────────────────────────────────────────────────────────
class ShopRanking(Base):
    __tablename__ = "shop_rankings"
    __table_args__ = (
        Index("uk_date_shop", "ranking_date", "shop_code", unique=True),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(BigInteger, "mysql"),
        primary_key=True,
        autoincrement=True,
    )
    ranking_date: Mapped[date] = mapped_column(Date, nullable=False)
    shop_code: Mapped[str] = mapped_column(String(64), nullable=False)
    browse_count: Mapped[int] = mapped_column(Integer, default=0)
    rank_position: Mapped[int | None] = mapped_column(Integer, default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )


# ─────────────────────────────────────────────────────────────
# 11. List Observations
# ─────────────────────────────────────────────────────────────
class ListObservation(Base):
    __tablename__ = "list_observations"
    __table_args__ = (
        Index("idx_shop_code", "shop_code"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(BigInteger, "mysql"),
        primary_key=True,
        autoincrement=True,
    )
    item_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("items.id", onupdate="CASCADE", ondelete="CASCADE"),
        nullable=False,
    )
    # ── Price ──────────────────────────────────────────────
    observed_price: Mapped[int | None] = mapped_column(Integer, default=None)
    price_range: Mapped[str | None] = mapped_column(String(128), default=None)
    # ── Points (structured from JSON) ─────────────────────
    point_count: Mapped[int | None] = mapped_column(Integer, default=None)
    point_base_multiplier: Mapped[int | None] = mapped_column(SmallInteger, default=None)
    point_shop_multiplier: Mapped[int | None] = mapped_column(SmallInteger, default=None)
    point_up_multiplier: Mapped[int | None] = mapped_column(SmallInteger, default=None)
    point_deal_multiplier: Mapped[int | None] = mapped_column(SmallInteger, default=None)
    point_item_multiplier: Mapped[int | None] = mapped_column(SmallInteger, default=None)
    point_text_raw: Mapped[str | None] = mapped_column(String(256), default=None)
    # ── Coupon (structured from JSON) ─────────────────────
    coupon_discount: Mapped[int | None] = mapped_column(Integer, default=None)
    coupon_type: Mapped[str | None] = mapped_column(String(16), default=None)
    coupon_text_raw: Mapped[str | None] = mapped_column(String(256), default=None)
    # ── Shipping & Delivery ───────────────────────────────
    shipping_cost: Mapped[int | None] = mapped_column(Integer, default=None)
    delivery_text: Mapped[str | None] = mapped_column(String(512), default=None)
    delivery_days: Mapped[int | None] = mapped_column(SmallInteger, default=None)
    is_free_shipping: Mapped[bool] = mapped_column(Boolean, default=False)
    # ── Review ────────────────────────────────────────────
    review_score: Mapped[float | None] = mapped_column(Numeric(3, 2), default=None)
    review_count: Mapped[int | None] = mapped_column(Integer, default=None)
    # ── Shop info ─────────────────────────────────────────
    shop_code: Mapped[str | None] = mapped_column(String(64), default=None)
    shop_name: Mapped[str | None] = mapped_column(String(256), default=None)
    shop_url_code: Mapped[str | None] = mapped_column(String(128), default=None)
    # ── Item metadata ─────────────────────────────────────
    item_name: Mapped[str | None] = mapped_column(String(1024), default=None)
    item_subtitle: Mapped[str | None] = mapped_column(String(1024), default=None)
    image_url: Mapped[str | None] = mapped_column(String(2048), default=None)
    genre_ids: Mapped[str | None] = mapped_column(String(256), default=None)
    variant_id: Mapped[str | None] = mapped_column(String(128), default=None)
    # ── SKU info ──────────────────────────────────────────
    has_multi_sku: Mapped[bool] = mapped_column(Boolean, default=False)
    # ── Flags ─────────────────────────────────────────────
    is_pr: Mapped[bool] = mapped_column(Boolean, default=False)
    is_super_deal: Mapped[bool] = mapped_column(Boolean, default=False)
    is_shop39: Mapped[bool] = mapped_column(Boolean, default=False)
    is_sold_out: Mapped[bool] = mapped_column(Boolean, default=False)
    # ── Source ────────────────────────────────────────────
    data_source: Mapped[str] = mapped_column(String(8), default="html")
    list_url: Mapped[str | None] = mapped_column(String(2048), default=None)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )

    # Relationships
    item: Mapped[Item] = relationship("Item", back_populates="list_observations")


# ─────────────────────────────────────────────────────────────
# 12. Detail Observations
# ─────────────────────────────────────────────────────────────
class DetailObservation(Base):
    """
    Observation data extracted from product detail pages.

    Primarily sourced from the embedded JSON
    ``<script type="application/json" id="item-page-app-data">``.
    Stores item-level aggregate info plus a JSON summary of all variants.
    """

    __tablename__ = "detail_observations"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(BigInteger, "mysql"),
        primary_key=True,
        autoincrement=True,
    )
    item_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("items.id", onupdate="CASCADE", ondelete="CASCADE"),
        nullable=False,
    )
    # ── Basic item info ─────────────────────────────────────
    item_name: Mapped[str | None] = mapped_column(String(1024), default=None)
    shop_name: Mapped[str | None] = mapped_column(String(512), default=None)
    shop_url_code: Mapped[str | None] = mapped_column(String(128), default=None)
    # ── Price range ─────────────────────────────────────────
    price_min: Mapped[int | None] = mapped_column(Integer, default=None)
    price_max: Mapped[int | None] = mapped_column(Integer, default=None)
    # ── Review ──────────────────────────────────────────────
    review_count: Mapped[int | None] = mapped_column(Integer, default=None)
    review_rating: Mapped[Decimal | None] = mapped_column(
        Numeric(4, 2), default=None
    )
    # ── Flags ───────────────────────────────────────────────
    is_39_shop: Mapped[bool] = mapped_column(Boolean, default=False)
    is_super_deal: Mapped[bool] = mapped_column(Boolean, default=False)
    # ── Inventory ───────────────────────────────────────────
    inventory_type: Mapped[str | None] = mapped_column(
        String(32), default="single", comment="single or multiple"
    )
    # ── Coupon ──────────────────────────────────────────────
    coupon_yen: Mapped[int | None] = mapped_column(Integer, default=None)
    coupon_percent: Mapped[Decimal | None] = mapped_column(
        Numeric(5, 1), default=None
    )
    # ── Variants summary ────────────────────────────────────
    variant_count: Mapped[int] = mapped_column(SmallInteger, default=0)
    variant_data_json: Mapped[dict | None] = mapped_column(
        JSON, default=None,
        comment="[{variantId, jan, price, name, soldOut, stock, deliveryMsg}]",
    )
    # ── Genre ───────────────────────────────────────────────
    ancestor_genre_id: Mapped[str | None] = mapped_column(String(32), default=None)
    r_category_id: Mapped[str | None] = mapped_column(String(32), default=None)
    # ── Source ──────────────────────────────────────────────
    data_source: Mapped[str] = mapped_column(String(8), default="json")
    detail_url: Mapped[str | None] = mapped_column(String(2048), default=None)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )

    # Relationships
    item: Mapped[Item] = relationship("Item", back_populates="detail_observations")


# ─────────────────────────────────────────────────────────────
# 13. Crawl Stats
# ─────────────────────────────────────────────────────────────
class CrawlStat(Base):
    __tablename__ = "crawl_stats"
    __table_args__ = (
        Index("uk_date_type", "stat_date", "job_type", unique=True),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(BigInteger, "mysql"),
        primary_key=True,
        autoincrement=True,
    )
    stat_date: Mapped[date] = mapped_column(Date, nullable=False)
    job_type: Mapped[str] = mapped_column(
        Enum("api_discovery", "list_scrape", "detail_scrape"), nullable=False
    )
    total_jobs: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    skip_count: Mapped[int] = mapped_column(Integer, default=0)
    avg_duration_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    items_found: Mapped[int] = mapped_column(Integer, default=0)
    items_updated: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
