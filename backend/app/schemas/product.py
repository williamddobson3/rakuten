"""
Pydantic schemas for API request/response models.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field


# ═══════════════════════════════════════════════════════════════
#  Product / Variant schemas
# ═══════════════════════════════════════════════════════════════

class VariantSnapshotOut(BaseModel):
    """Current snapshot data for a variant."""
    price: int | None = None
    point_rate: float | None = None
    point_back_percent: float | None = None
    coupon_yen: int | None = None
    coupon_percent: float | None = None
    shipping_text_raw: str | None = None
    shipping_days_min: int | None = None
    shipping_days_max: int | None = None
    image_url: str | None = None
    extra1_key: str | None = None
    extra1_value: str | None = None
    extra2_key: str | None = None
    extra2_value: str | None = None
    fetched_at: datetime | None = None
    source: str | None = None


class VariantOut(BaseModel):
    """Variant with its current snapshot."""
    variant_id: int
    variant_code: str
    jan_code: str | None = None
    variant_name: str | None = None
    snapshot: VariantSnapshotOut | None = None


class ProductOut(BaseModel):
    """Full product response with shop info and variants."""
    item_id: int
    product_uid: str = Field(..., description="商品ユニークID ({shop_code}:{item_code})")
    shop_code: str
    shop_name: str | None = None
    shop_url: str | None = None
    item_code: str
    item_name: str | None = None
    canonical_url: str
    variants: list[VariantOut] = []


class ProductSearchResult(BaseModel):
    """Single result in product search."""
    variant_id: int
    product_uid: str = Field(..., description="商品ユニークID ({shop_code}:{item_code})")
    shop_code: str
    shop_name: str | None = None
    shop_url: str | None = None
    item_code: str
    item_name: str | None = None
    canonical_url: str
    variant_code: str
    jan_code: str | None = None
    variant_name: str | None = None
    price: int | None = None
    point_rate: float | None = None
    point_back_percent: float | None = None
    coupon_yen: int | None = None
    coupon_percent: float | None = None
    shipping_text_raw: str | None = None
    shipping_days_min: int | None = None
    shipping_days_max: int | None = None
    image_url: str | None = None
    extra1_key: str | None = None
    extra1_value: str | None = None
    extra2_key: str | None = None
    extra2_value: str | None = None
    fetched_at: datetime | None = None


class ProductSearchResponse(BaseModel):
    """Paginated product search response."""
    page: int
    per_page: int
    total: int = 0
    results: list[ProductSearchResult] = []


# ═══════════════════════════════════════════════════════════════
#  History schemas
# ═══════════════════════════════════════════════════════════════

class HistoryRecord(BaseModel):
    """Single day's data in the history."""
    record_date: date
    price: int | None = None
    point_rate: float | None = None
    point_back_percent: float | None = None
    coupon_yen: int | None = None
    coupon_percent: float | None = None
    shipping_days_min: int | None = None
    shipping_days_max: int | None = None


class VariantHistoryResponse(BaseModel):
    """History response for chart rendering."""
    variant_id: int
    days: int
    history: list[HistoryRecord] = []


# ═══════════════════════════════════════════════════════════════
#  Point Calculation schemas
# ═══════════════════════════════════════════════════════════════

class PointCalcRequest(BaseModel):
    """Request body for point calculation."""
    point_multiplier: float = Field(
        default=4.0,
        ge=1.0,
        le=100.0,
        description="ポイント倍率 (ユーザー指定, デフォルト4)",
    )


class PointCalcInput(BaseModel):
    """Input values used in calculation."""
    point_multiplier: float
    rakuten_price: int
    coupon_yen: int
    coupon_percent: float
    plus_x_up: float
    point_back_flag: int


class PointCalcOutput(BaseModel):
    """Calculated output values."""
    effective_coupon: int
    total_points: int
    price_minus_points: int
    price_minus_points_coupon: int


class PointCalcResponse(BaseModel):
    """Full point calculation response."""
    variant_id: int
    input: PointCalcInput
    output: PointCalcOutput


# ═══════════════════════════════════════════════════════════════
#  Ranking schemas
# ═══════════════════════════════════════════════════════════════

class ShopRankEntry(BaseModel):
    """Single shop in the frequency ranking."""
    rank: int
    shop_code: str
    shop_name: str | None = None
    browse_count: int


class ShopRankingResponse(BaseModel):
    """Shop frequency ranking response."""
    period_hours: int = 24
    rankings: list[ShopRankEntry] = []


# ═══════════════════════════════════════════════════════════════
#  Chrome Extension schemas
# ═══════════════════════════════════════════════════════════════

class ExtensionVariantData(BaseModel):
    """Variant data extracted by Chrome extension."""
    variant_code: str
    jan_code: str | None = None
    variant_name: str | None = None
    price: int | None = None
    image_url: str | None = None


class ExtensionBrowsePayload(BaseModel):
    """Data sent from Chrome extension when user views a product page."""
    user_id: str = Field(..., min_length=1, description="Anonymous user hash")
    page_url: str = Field(..., min_length=1, description="Product page URL")
    item_name: str | None = None
    price: int | None = None
    jan_code: str | None = None
    variant_id_param: str | None = None
    point_rate: float | None = None
    point_back_percent: float | None = None
    coupon_yen: int | None = None
    coupon_percent: float | None = None
    shipping_text: str | None = None
    shipping_days_min: int | None = None
    shipping_days_max: int | None = None
    image_url: str | None = None
    extra1_key: str | None = None
    extra1_value: str | None = None
    extra2_key: str | None = None
    extra2_value: str | None = None
    # For variant products - list of all variants on the page
    variants: list[ExtensionVariantData] | None = None


class ExtensionBrowseResponse(BaseModel):
    """Response to Chrome extension browse event."""
    status: str = "ok"
    item_id: int | None = None
    product_uid: str | None = Field(None, description="商品ユニークID ({shop_code}:{item_code})")
    variant_id: int | None = None
    variants_processed: int = 0


# ═══════════════════════════════════════════════════════════════
#  Crawl Target schemas
# ═══════════════════════════════════════════════════════════════

class CrawlTargetCreate(BaseModel):
    """Request to add a crawl target."""
    target_type: str = Field(..., pattern="^(genre|shop|keyword)$")
    target_value: str = Field(..., min_length=1)
    base_url: str | None = None
    priority: int = 0


class CrawlTargetOut(BaseModel):
    """Crawl target response."""
    id: int
    target_type: str
    target_value: str
    base_url: str | None = None
    is_active: bool
    priority: int
    added_by: str
    last_crawled_at: datetime | None = None
    created_at: datetime


# ═══════════════════════════════════════════════════════════════
#  JAN search / cheapest by JAN
# ═══════════════════════════════════════════════════════════════

class JanSearchResult(BaseModel):
    """Products sharing the same JAN code, sorted by effective price."""
    jan_code: str
    variants: list[ProductSearchResult] = []
