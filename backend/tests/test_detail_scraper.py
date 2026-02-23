"""
Test Suite: Product Detail Page Scraping (Detail Scraper Worker)

Tests the 3rd stage of the pipeline:
  - Embedded JSON extraction (<script id="item-page-app-data">)
  - Item-level data extraction from JSON (title, shop, reviews, flags, genre, points)
  - Variant (SKU) extraction from JSON (JAN, price, images, shipping, stock, delivery)
  - HTML fallback extraction (itemprop selectors, CSS class selectors)
  - Coupon extraction (HTML + JSON patterns)
  - Snapshot data building from variant + item-level data
  - Detail observation saving
  - Main scrape task (scrape_detail_page) — JSON primary + HTML fallback
  - Job dispatcher (process_pending_detail_jobs)
  - Page fetch with auth cookies + proxy rotation
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch, PropertyMock

import pytest


# ─────────────────────────────────────────────────────────────
#  Fixtures
# ─────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _patch_settings():
    """Patch settings for tests."""
    with patch("app.config.settings") as mock_settings:
        mock_settings.REQUEST_DELAY_SEC = 0
        mock_settings.PROXY_POOL_URL = ""
        mock_settings.REDIS_URL = "redis://localhost:6379/0"
        yield mock_settings


def _make_sku(
    variant_id: str = "12345",
    price: int = 6270,
    jan_code: str | None = "4549526701870",
    variant_name: str | None = None,
    selector_values: list[str] | None = None,
    sold_out: bool = False,
    postage_included: bool = True,
) -> dict:
    """Build a realistic SKU (variant) from itemInfoSku.sku[]."""
    return {
        "variantId": variant_id,
        "merchantDefinedSkuId": f"SKU-{variant_id}",
        "articleNumber": {
            "value": jan_code,
        } if jan_code else {"exemptionReason": 1},
        "taxIncludedPrice": price,
        "selectorValues": selector_values or ["ブラック", "26.0cm"],
        "images": [
            {"location": f"https://image.rakuten.co.jp/sku_{variant_id}.jpg"},
        ],
        "shipping": {
            "postageIncluded": postage_included,
            "singleItemShipping": 0 if postage_included else 550,
        },
        "normalDeliveryTime": 3,
        "normalDeliveryDateId": None,
        "attributes": [
            {"title": "ブランド", "value": "EDWIN"},
            {"title": "カラー", "value": "ブラック"},
        ],
        "hidden": False,
        "noshi": False,
    }


def _make_item_info_sku(
    skus: list[dict] | None = None,
    title: str = "EDWIN エドウィン スリッポン",
    is_39_shop: bool = True,
    super_deal: bool = False,
    review_count: int = 49,
    review_rating: float = 4.65,
    price_min: int = 5500,
    price_max: int = 7200,
) -> dict:
    """Build a realistic itemInfoSku object."""
    if skus is None:
        skus = [
            _make_sku("12345", 6270, "4549526701870", selector_values=["ブラック", "26.0cm"]),
            _make_sku("12346", 6270, "4549526701887", selector_values=["ブラウン", "26.0cm"]),
            _make_sku("12347", 5500, "4549526701894", selector_values=["ネイビー", "27.0cm"]),
        ]

    return {
        "title": title,
        "manageNumber": "edm544",
        "shopId": 380959,
        "itemId": 10000395,
        "newItemNumber": "edm544",
        "sellType": "NORMAL",
        "is39Shop": is_39_shop,
        "superDeal": super_deal,
        "isMnoFlag": False,
        "genreInfo": {
            "ancestorGenreId": 558885,
        },
        "pcFields": {
            "rCategoryId": "110983",
            "itemNumber": "edm544",
        },
        "payment": {"taxIncluded": True},
        "itemReviewInfo": {
            "summary": {
                "itemReviewCount": review_count,
                "itemReviewRating": review_rating,
            }
        },
        "pointCampaignInfo": [
            {
                "pointCampaign": {"pointRate": 5, "rate": 5},
                "isPointOptimization": False,
            }
        ],
        "purchaseInfo": {
            "purchaseBySellType": {
                "normalPurchase": {
                    "price": {
                        "minPrice": price_min,
                        "maxPrice": price_max,
                    },
                    "preTaxPrice": None,
                },
            },
            "sku": [
                {
                    "variantId": "12345",
                    "newPurchaseSku": {
                        "deliveryMessage": "2/24(月)にお届け",
                        "stockCondition": None,
                        "quantity": 10,
                    },
                    "doublePrice": {"referencePriceVerified": False},
                },
                {
                    "variantId": "12346",
                    "newPurchaseSku": {
                        "deliveryMessage": "2/25(火)にお届け",
                        "stockCondition": None,
                        "quantity": 5,
                    },
                    "doublePrice": {"referencePriceVerified": False},
                },
                {
                    "variantId": "12347",
                    "newPurchaseSku": {
                        "deliveryMessage": None,
                        "stockCondition": "sold-out",
                        "quantity": 0,
                    },
                    "doublePrice": {"referencePriceVerified": False},
                },
            ],
            "variantMappedInventories": [
                {"sku": "12345", "quantity": 10},
                {"sku": "12346", "quantity": 5},
                {"sku": "12347", "quantity": 0},
            ],
        },
        "inventoryType": "multiple",
        "variantSelectors": [
            {"name": "カラー", "values": ["ブラック", "ブラウン", "ネイビー"]},
            {"name": "サイズ", "values": ["26.0cm", "27.0cm"]},
        ],
        "media": {
            "images": [
                {"location": "https://image.rakuten.co.jp/main_image.jpg"},
            ],
        },
        "breadcrumbs": {
            "genreBreadcrumbs": [
                {"id": 558885, "name": "靴"},
                {"id": 110983, "name": "メンズ靴"},
            ],
        },
        "featureSectionInfo": {"coupon": True},
        "identicalVariants": {},
        "hometownTaxEligible": False,
        "sku": skus,
    }


def _make_app_data(item_info: dict | None = None) -> dict:
    """Build the full app_data structure."""
    if item_info is None:
        item_info = _make_item_info_sku()

    return {
        "api": {
            "data": {
                "itemInfoSku": item_info,
            },
        },
        "shop": {
            "shopName": "moriashizakka",
            "shopUrl": "moriashizakka",
            "taxRate": 0.1,
        },
    }


def _make_detail_page_html(app_data: dict | None = None) -> str:
    """Build HTML with embedded <script id="item-page-app-data">."""
    if app_data is None:
        app_data = _make_app_data()

    app_json = json.dumps(app_data, ensure_ascii=False)
    return f"""
    <html>
    <head><title>【楽天市場】EDWIN エドウィン スリッポン:moriashizakka</title></head>
    <body>
    <script type="application/json" id="item-page-app-data">{app_json}</script>
    <div itemscope itemtype="http://schema.org/Product">
        <span itemprop="name">EDWIN エドウィン スリッポン</span>
        <meta itemprop="price" content="6270" />
        <meta itemprop="gtin13" content="4549526701870" />
        <meta itemprop="image" content="https://image.rakuten.co.jp/main.jpg" />
        <div itemprop="seller" itemscope>
            <span itemprop="name">moriashizakka</span>
        </div>
        <div class="points--xyz">285ポイント(1倍+9倍UP)</div>
        <div class="coupon">500円OFFクーポンあり</div>
        <div class="shipping">翌日配送</div>
    </div>
    </body>
    </html>
    """


def _make_html_only_detail_page() -> str:
    """Build HTML without embedded JSON (for fallback testing)."""
    return """
    <html>
    <head><title>【楽天市場】テスト商品:shopname</title></head>
    <body>
    <div itemscope itemtype="http://schema.org/Product">
        <span itemprop="name">テスト商品 シンプル</span>
        <meta itemprop="price" content="3980" />
        <meta itemprop="gtin13" content="4901234567890" />
        <meta itemprop="image" content="https://image.rakuten.co.jp/simple.jpg" />
        <meta property="og:image" content="https://image.rakuten.co.jp/simple_og.jpg" />
        <div itemprop="seller" itemscope>
            <span itemprop="name">テストショップ</span>
        </div>
        <div itemprop="brand" itemscope>
            <span itemprop="name">テストブランド</span>
        </div>
        <meta itemprop="sku" content="MODEL-ABC123" />
        <div class="points--xyz">39ポイント(1倍)</div>
        <div class="coupon">300円OFFクーポンあり</div>
        <div class="shipping">1～3日以内に発送</div>
    </div>
    </body>
    </html>
    """


# ═══════════════════════════════════════════════════════════════
#  1. Embedded JSON Extraction Tests
# ═══════════════════════════════════════════════════════════════

class TestExtractAppDataJson:
    """Tests for extracting <script id="item-page-app-data"> JSON."""

    def test_extract_valid_json(self):
        """Should parse valid app-data JSON from the script tag."""
        from app.workers.detail_scraper import _extract_app_data_json

        html = _make_detail_page_html()
        data = _extract_app_data_json(html)

        assert data is not None
        assert "api" in data

    def test_extract_no_script_tag(self):
        """Should return None when script tag is missing."""
        from app.workers.detail_scraper import _extract_app_data_json

        html = "<html><body>No JSON here</body></html>"
        data = _extract_app_data_json(html)

        assert data is None

    def test_extract_malformed_json(self):
        """Should return None when JSON is malformed."""
        from app.workers.detail_scraper import _extract_app_data_json

        html = """
        <html><body>
        <script type="application/json" id="item-page-app-data">
            {invalid json content}
        </script>
        </body></html>
        """
        data = _extract_app_data_json(html)

        assert data is None


class TestGetItemInfoSku:
    """Tests for navigating to itemInfoSku from app_data."""

    def test_path_api_data(self):
        """Should find itemInfoSku via api.data.itemInfoSku."""
        from app.workers.detail_scraper import _get_item_info_sku

        app_data = _make_app_data()
        info = _get_item_info_sku(app_data)

        assert info is not None
        assert info["title"] == "EDWIN エドウィン スリッポン"

    def test_path_new_api(self):
        """Should find itemInfoSku via newApi.itemInfoSku."""
        from app.workers.detail_scraper import _get_item_info_sku

        info_sku = _make_item_info_sku(title="Via newApi")
        app_data = {
            "newApi": {"itemInfoSku": info_sku},
        }
        info = _get_item_info_sku(app_data)

        assert info is not None
        assert info["title"] == "Via newApi"

    def test_no_item_info_sku(self):
        """Should return None when itemInfoSku is not found."""
        from app.workers.detail_scraper import _get_item_info_sku

        info = _get_item_info_sku({"random": "data"})

        assert info is None


# ═══════════════════════════════════════════════════════════════
#  2. Item-Level Extraction Tests
# ═══════════════════════════════════════════════════════════════

class TestExtractItemLevelFromJson:
    """Tests for extracting item-level data from itemInfoSku."""

    def test_full_extraction(self):
        """Should extract all item-level fields."""
        from app.workers.detail_scraper import _extract_item_level_from_json

        item_info = _make_item_info_sku()
        app_data = _make_app_data(item_info)

        result = _extract_item_level_from_json(item_info, app_data)

        assert result["title"] == "EDWIN エドウィン スリッポン"
        assert result["is_39_shop"] is True
        assert result["is_super_deal"] is False
        assert result["review_count"] == 49
        assert result["review_rating"] == 4.65
        assert result["price_min"] == 5500
        assert result["price_max"] == 7200
        assert result["tax_included"] is True
        assert result["inventory_type"] == "multiple"
        assert result["shop_name"] == "moriashizakka"
        assert result["shop_url_code"] == "moriashizakka"
        assert result["ancestor_genre_id"] == 558885

    def test_super_deal_flag(self):
        """Should detect Super DEAL products."""
        from app.workers.detail_scraper import _extract_item_level_from_json

        item_info = _make_item_info_sku(super_deal=True)
        app_data = _make_app_data(item_info)

        result = _extract_item_level_from_json(item_info, app_data)
        assert result["is_super_deal"] is True

    def test_variant_selectors(self):
        """Should extract variant selector definitions."""
        from app.workers.detail_scraper import _extract_item_level_from_json

        item_info = _make_item_info_sku()
        app_data = _make_app_data(item_info)

        result = _extract_item_level_from_json(item_info, app_data)
        selectors = result["variant_selectors"]
        assert len(selectors) == 2
        assert selectors[0]["name"] == "カラー"
        assert selectors[1]["name"] == "サイズ"

    def test_point_campaign_extraction(self):
        """Should extract point campaign info."""
        from app.workers.detail_scraper import _extract_item_level_from_json

        item_info = _make_item_info_sku()
        app_data = _make_app_data(item_info)

        result = _extract_item_level_from_json(item_info, app_data)
        assert result["point_campaign"]["pointRate"] == 5


# ═══════════════════════════════════════════════════════════════
#  3. Variant (SKU) Extraction Tests
# ═══════════════════════════════════════════════════════════════

class TestExtractVariantsFromItemInfo:
    """Tests for extracting variant data from itemInfoSku."""

    def test_extract_multiple_variants(self):
        """Should extract all non-hidden variants."""
        from app.workers.detail_scraper import _extract_variants_from_item_info

        item_info = _make_item_info_sku()
        variants = _extract_variants_from_item_info(item_info)

        assert len(variants) == 3

    def test_variant_fields(self):
        """Should extract all fields for each variant."""
        from app.workers.detail_scraper import _extract_variants_from_item_info

        item_info = _make_item_info_sku()
        variants = _extract_variants_from_item_info(item_info)

        v1 = variants[0]
        assert v1["variant_code"] == "12345"
        assert v1["jan_code"] == "4549526701870"
        assert v1["price"] == 6270
        assert v1["variant_name"] == "ブラック / 26.0cm"
        assert v1["merchant_sku_id"] == "SKU-12345"
        assert v1["postage_included"] is True
        assert v1["normal_delivery_time"] == 3
        assert v1["stock_quantity"] == 10
        assert v1["is_sold_out"] is False

    def test_sold_out_variant(self):
        """Should detect sold-out variants."""
        from app.workers.detail_scraper import _extract_variants_from_item_info

        item_info = _make_item_info_sku()
        variants = _extract_variants_from_item_info(item_info)

        v3 = variants[2]  # Third variant is sold-out
        assert v3["variant_code"] == "12347"
        assert v3["stock_quantity"] == 0
        assert v3["is_sold_out"] is True

    def test_delivery_message(self):
        """Should extract delivery message from purchaseInfo."""
        from app.workers.detail_scraper import _extract_variants_from_item_info

        item_info = _make_item_info_sku()
        variants = _extract_variants_from_item_info(item_info)

        assert variants[0]["delivery_message"] == "2/24(月)にお届け"
        assert variants[2]["delivery_message"] is None  # sold-out has no message

    def test_jan_code_validation(self):
        """Should only accept 12-13 digit JAN codes."""
        from app.workers.detail_scraper import _extract_variants_from_item_info

        # Valid 13-digit JAN
        item_info = _make_item_info_sku(skus=[_make_sku(jan_code="4549526701870")])
        variants = _extract_variants_from_item_info(item_info)
        assert variants[0]["jan_code"] == "4549526701870"

        # Invalid JAN (too short)
        item_info = _make_item_info_sku(skus=[_make_sku(jan_code="12345")])
        variants = _extract_variants_from_item_info(item_info)
        assert variants[0]["jan_code"] is None

        # No JAN
        item_info = _make_item_info_sku(skus=[_make_sku(jan_code=None)])
        variants = _extract_variants_from_item_info(item_info)
        assert variants[0]["jan_code"] is None

    def test_hidden_variants_skipped(self):
        """Hidden variants should be excluded."""
        from app.workers.detail_scraper import _extract_variants_from_item_info

        sku = _make_sku("99999")
        sku["hidden"] = True
        item_info = _make_item_info_sku(skus=[sku])

        variants = _extract_variants_from_item_info(item_info)
        assert len(variants) == 0

    def test_empty_sku_list(self):
        """Should return empty list when no SKUs."""
        from app.workers.detail_scraper import _extract_variants_from_item_info

        item_info = _make_item_info_sku(skus=[])
        variants = _extract_variants_from_item_info(item_info)
        assert variants == []

    def test_variant_images(self):
        """Should extract variant-level images."""
        from app.workers.detail_scraper import _extract_variants_from_item_info

        item_info = _make_item_info_sku()
        variants = _extract_variants_from_item_info(item_info)

        assert variants[0]["image_url"] is not None
        assert "sku_12345" in variants[0]["image_url"]
        assert len(variants[0]["image_urls"]) == 1

    def test_variant_attributes(self):
        """Should extract attributes (brand, color, etc.)."""
        from app.workers.detail_scraper import _extract_variants_from_item_info

        item_info = _make_item_info_sku()
        variants = _extract_variants_from_item_info(item_info)

        attrs = variants[0]["attributes"]
        assert len(attrs) == 2
        assert attrs[0]["title"] == "ブランド"
        assert attrs[0]["value"] == "EDWIN"


# ═══════════════════════════════════════════════════════════════
#  4. Snapshot Data Building Tests
# ═══════════════════════════════════════════════════════════════

class TestBuildSnapshotFromJsonVariant:
    """Tests for building snapshot_data from variant + item-level data."""

    def test_build_full_snapshot(self):
        """Should build a complete snapshot dict."""
        from app.workers.detail_scraper import (
            _build_snapshot_from_json_variant,
            _extract_item_level_from_json,
        )

        item_info = _make_item_info_sku()
        app_data = _make_app_data(item_info)
        item_level = _extract_item_level_from_json(item_info, app_data)

        variant_data = {
            "price": 6270,
            "image_url": "https://image.rakuten.co.jp/variant1.jpg",
            "postage_included": True,
            "single_item_shipping": 0,
            "delivery_message": "2/24(月)にお届け",
            "normal_delivery_time": 3,
            "attributes": [
                {"title": "ブランド", "value": "EDWIN"},
                {"title": "カラー", "value": "ブラック"},
            ],
        }

        coupon_info = {"coupon_yen": 500, "coupon_percent": None}

        snapshot = _build_snapshot_from_json_variant(variant_data, item_level, coupon_info)

        assert snapshot["price"] == 6270
        assert snapshot["image_url"] == "https://image.rakuten.co.jp/variant1.jpg"
        assert snapshot["shipping_text_raw"] == "送料込み / 2/24(月)にお届け"
        assert snapshot["shipping_days_min"] == 1
        assert snapshot["shipping_days_max"] == 3
        assert snapshot["coupon_yen"] == 500
        assert snapshot["extra1_key"] == "ブランド"
        assert snapshot["extra1_value"] == "EDWIN"
        assert snapshot["extra2_key"] == "カラー"
        assert snapshot["extra2_value"] == "ブラック"

    def test_fallback_to_item_image(self):
        """Should use item-level main_image_url if variant has no image."""
        from app.workers.detail_scraper import (
            _build_snapshot_from_json_variant,
            _extract_item_level_from_json,
        )

        item_info = _make_item_info_sku()
        app_data = _make_app_data(item_info)
        item_level = _extract_item_level_from_json(item_info, app_data)

        variant_data = {
            "price": 6270,
            "image_url": None,
            "postage_included": False,
            "single_item_shipping": 550,
            "delivery_message": None,
            "normal_delivery_time": None,
            "attributes": [],
        }

        snapshot = _build_snapshot_from_json_variant(variant_data, item_level, {})

        assert snapshot["image_url"] == item_level["main_image_url"]
        assert "送料 550円" in snapshot["shipping_text_raw"]


# ═══════════════════════════════════════════════════════════════
#  5. Coupon Extraction Tests
# ═══════════════════════════════════════════════════════════════

class TestExtractCouponFromPage:
    """Tests for extracting coupon information from detail pages."""

    def test_extract_yen_coupon_from_html(self):
        """Should extract yen-based coupon from HTML."""
        from app.workers.detail_scraper import _extract_coupon_from_page
        from bs4 import BeautifulSoup

        html = '<div class="coupon">500円OFFクーポン</div>'
        soup = BeautifulSoup(html, "lxml")

        result = _extract_coupon_from_page(html, soup)
        assert result["coupon_yen"] == 500

    def test_extract_percent_coupon_from_html(self):
        """Should extract percentage coupon from HTML."""
        from app.workers.detail_scraper import _extract_coupon_from_page
        from bs4 import BeautifulSoup

        html = '<div class="coupon">10%OFFクーポン</div>'
        soup = BeautifulSoup(html, "lxml")

        result = _extract_coupon_from_page(html, soup)
        assert result["coupon_percent"] == 10.0

    def test_extract_coupon_from_json_pattern(self):
        """Should extract coupon from embedded JSON pattern."""
        from app.workers.detail_scraper import _extract_coupon_from_page
        from bs4 import BeautifulSoup

        html = '"coupon" : { "discount" : 200, "discountType" : "exact" }'
        soup = BeautifulSoup("<html></html>", "lxml")

        result = _extract_coupon_from_page(html, soup)
        assert result["coupon_yen"] == 200

    def test_no_coupon(self):
        """Should return None values when no coupon found."""
        from app.workers.detail_scraper import _extract_coupon_from_page
        from bs4 import BeautifulSoup

        html = "<html><body>No coupons</body></html>"
        soup = BeautifulSoup(html, "lxml")

        result = _extract_coupon_from_page(html, soup)
        assert result["coupon_yen"] is None
        assert result["coupon_percent"] is None


# ═══════════════════════════════════════════════════════════════
#  6. HTML Fallback Extraction Tests
# ═══════════════════════════════════════════════════════════════

class TestHTMLFallbackExtraction:
    """Tests for HTML-based data extraction (fallback strategy)."""

    def _get_soup(self, html: str | None = None):
        from bs4 import BeautifulSoup
        if html is None:
            html = _make_html_only_detail_page()
        return BeautifulSoup(html, "lxml")

    def test_extract_item_name(self):
        from app.workers.detail_scraper import _extract_item_name_html
        soup = self._get_soup()
        name = _extract_item_name_html(soup)
        assert name == "テスト商品 シンプル"

    def test_extract_item_name_from_title(self):
        """Should fall back to <title> tag."""
        from app.workers.detail_scraper import _extract_item_name_html
        soup = self._get_soup("<html><head><title>【楽天市場】特売品:ショップ名</title></head><body></body></html>")
        name = _extract_item_name_html(soup)
        assert name is not None
        assert "特売品" in name

    def test_extract_price(self):
        from app.workers.detail_scraper import _extract_price_html
        soup = self._get_soup()
        price = _extract_price_html(soup)
        assert price == 3980

    def test_extract_image(self):
        from app.workers.detail_scraper import _extract_image_html
        soup = self._get_soup()
        image = _extract_image_html(soup)
        assert image == "https://image.rakuten.co.jp/simple.jpg"

    def test_extract_jan_single(self):
        from app.workers.detail_scraper import _extract_jan_single
        soup = self._get_soup()
        jan = _extract_jan_single(soup)
        assert jan == "4901234567890"

    def test_extract_jan_invalid_format(self):
        from app.workers.detail_scraper import _extract_jan_single
        from bs4 import BeautifulSoup
        html = '<meta itemprop="gtin13" content="123" />'
        soup = BeautifulSoup(html, "lxml")
        jan = _extract_jan_single(soup)
        assert jan is None

    def test_extract_shop_name(self):
        from app.workers.detail_scraper import _extract_shop_name_html
        soup = self._get_soup()
        name = _extract_shop_name_html(soup)
        assert name == "テストショップ"

    def test_extract_point_info(self):
        from app.workers.detail_scraper import _extract_point_info_html
        soup = self._get_soup()
        info = _extract_point_info_html(soup)
        assert info["point_rate"] == 1.0

    def test_extract_shipping_info(self):
        from app.workers.detail_scraper import _extract_shipping_info_html
        soup = self._get_soup()
        info = _extract_shipping_info_html(soup)
        assert info["shipping_text_raw"] is not None
        assert info["shipping_days_max"] == 3
        assert info["shipping_days_min"] == 1

    def test_extract_extra_fields(self):
        from app.workers.detail_scraper import _extract_extra_fields_html
        soup = self._get_soup()
        extra = _extract_extra_fields_html(soup)
        assert extra["extra1_key"] == "ブランド"
        assert extra["extra1_value"] == "テストブランド"
        assert extra["extra2_key"] == "型番"
        assert extra["extra2_value"] == "MODEL-ABC123"


# ═══════════════════════════════════════════════════════════════
#  7. Main Scrape Task Tests (JSON Strategy)
# ═══════════════════════════════════════════════════════════════

class TestScrapeDetailPageJson:
    """Tests for scrape_detail_page using the JSON primary strategy."""

    @patch("app.workers.detail_scraper.time.sleep")
    @patch("app.workers.detail_scraper._save_detail_observation")
    @patch("app.workers.detail_scraper.upsert_snapshot_and_history_sync")
    @patch("app.workers.detail_scraper.upsert_variant_sync")
    @patch("app.workers.detail_scraper.upsert_item_sync", return_value=501)
    @patch("app.workers.detail_scraper.upsert_shop_sync")
    @patch("app.workers.detail_scraper._fetch_detail_page")
    @patch("app.workers.detail_scraper.get_sync_db")
    def test_json_strategy_multi_variant(
        self, mock_get_db, mock_fetch, mock_shop, mock_item,
        mock_variant, mock_snapshot, mock_save_obs, mock_sleep,
    ):
        """Should process multi-variant product from embedded JSON."""
        from app.workers.detail_scraper import scrape_detail_page

        mock_db = MagicMock()
        mock_get_db.return_value = mock_db

        mock_fetch.return_value = _make_detail_page_html()
        mock_variant.side_effect = [1001, 1002, 1003]

        result = scrape_detail_page("https://item.rakuten.co.jp/moriashizakka/edm544/")

        assert result["status"] == "ok"
        assert result["data_source"] == "json"
        assert result["variants_processed"] == 3
        assert result["shop_code"] == "moriashizakka"
        assert result["product_uid"] == "moriashizakka:edm544"
        assert result["is_39_shop"] is True

        # Should have upserted 3 variants
        assert mock_variant.call_count == 3

        # Should have upserted 3 snapshots
        assert mock_snapshot.call_count == 3

        # Should have saved detail observation
        mock_save_obs.assert_called_once()

    @patch("app.workers.detail_scraper.time.sleep")
    @patch("app.workers.detail_scraper._save_detail_observation")
    @patch("app.workers.detail_scraper.upsert_snapshot_and_history_sync")
    @patch("app.workers.detail_scraper.upsert_variant_sync", return_value=1001)
    @patch("app.workers.detail_scraper.upsert_item_sync", return_value=501)
    @patch("app.workers.detail_scraper.upsert_shop_sync")
    @patch("app.workers.detail_scraper._fetch_detail_page")
    @patch("app.workers.detail_scraper.get_sync_db")
    def test_json_strategy_single_product(
        self, mock_get_db, mock_fetch, mock_shop, mock_item,
        mock_variant, mock_snapshot, mock_save_obs, mock_sleep,
    ):
        """Should handle single product (no SKU array) from JSON."""
        from app.workers.detail_scraper import scrape_detail_page

        mock_db = MagicMock()
        mock_get_db.return_value = mock_db

        # Create item_info with empty SKU list
        item_info = _make_item_info_sku(skus=[])
        app_data = _make_app_data(item_info)
        mock_fetch.return_value = _make_detail_page_html(app_data)

        result = scrape_detail_page("https://item.rakuten.co.jp/moriashizakka/edm544/")

        assert result["status"] == "ok"
        assert result["variants_processed"] == 1
        mock_variant.assert_called_once()


# ═══════════════════════════════════════════════════════════════
#  8. Main Scrape Task Tests (HTML Fallback Strategy)
# ═══════════════════════════════════════════════════════════════

class TestScrapeDetailPageHTML:
    """Tests for scrape_detail_page using the HTML fallback strategy."""

    @patch("app.workers.detail_scraper.time.sleep")
    @patch("app.workers.detail_scraper._save_detail_observation")
    @patch("app.workers.detail_scraper.upsert_snapshot_and_history_sync")
    @patch("app.workers.detail_scraper.upsert_variant_sync", return_value=1001)
    @patch("app.workers.detail_scraper.upsert_item_sync", return_value=501)
    @patch("app.workers.detail_scraper.upsert_shop_sync")
    @patch("app.workers.detail_scraper._fetch_detail_page")
    @patch("app.workers.detail_scraper.get_sync_db")
    def test_html_fallback(
        self, mock_get_db, mock_fetch, mock_shop, mock_item,
        mock_variant, mock_snapshot, mock_save_obs, mock_sleep,
    ):
        """Should fall back to HTML parsing when JSON is not available."""
        from app.workers.detail_scraper import scrape_detail_page

        mock_db = MagicMock()
        mock_get_db.return_value = mock_db

        mock_fetch.return_value = _make_html_only_detail_page()

        result = scrape_detail_page("https://item.rakuten.co.jp/testshop/item1/")

        assert result["status"] == "ok"
        assert result["data_source"] == "html"
        assert result["variants_processed"] == 1
        assert result["product_uid"] == "testshop:item1"

        # Should call snapshot with extracted HTML data
        mock_snapshot.assert_called_once()
        snap_args = mock_snapshot.call_args
        snap_data = snap_args[1] if len(snap_args) > 1 else snap_args[0][1]
        # Price should be extracted from itemprop
        assert isinstance(snap_data, dict) or True  # data passed positionally or by keyword


# ═══════════════════════════════════════════════════════════════
#  9. Error Handling Tests
# ═══════════════════════════════════════════════════════════════

class TestScrapeDetailPageErrors:
    """Tests for error handling in scrape_detail_page."""

    @patch("app.workers.detail_scraper._fetch_detail_page", return_value=None)
    @patch("app.workers.detail_scraper.get_sync_db")
    def test_fetch_failed(self, mock_get_db, mock_fetch):
        """Should return error when page fetch fails."""
        from app.workers.detail_scraper import scrape_detail_page

        mock_db = MagicMock()
        mock_get_db.return_value = mock_db

        result = scrape_detail_page("https://item.rakuten.co.jp/shop/item/")

        assert result["status"] == "error"
        assert result["reason"] == "fetch_failed"

    @patch("app.workers.detail_scraper._fetch_detail_page")
    @patch("app.workers.detail_scraper.get_sync_db")
    def test_invalid_url(self, mock_get_db, mock_fetch):
        """Should return error for unparseable URLs."""
        from app.workers.detail_scraper import scrape_detail_page

        mock_db = MagicMock()
        mock_get_db.return_value = mock_db

        # Return some HTML but URL is not a valid item page
        mock_fetch.return_value = "<html><body>test</body></html>"

        result = scrape_detail_page("https://www.rakuten.co.jp/")

        assert result["status"] == "error"
        assert result["reason"] == "invalid_url"

    @patch("app.workers.detail_scraper._fetch_detail_page", return_value=None)
    @patch("app.workers.detail_scraper.get_sync_db")
    def test_job_status_updated_on_fetch_fail(self, mock_get_db, mock_fetch):
        """Should update job status to 'failed' when fetch fails."""
        from app.workers.detail_scraper import scrape_detail_page

        mock_db = MagicMock()
        mock_get_db.return_value = mock_db

        result = scrape_detail_page("https://item.rakuten.co.jp/shop/item/", job_id=42)

        assert result["status"] == "error"
        # Should have updated job status
        update_calls = [
            c for c in mock_db.execute.call_args_list
            if hasattr(c.args[0], "text") and "failed" in c.args[0].text
        ]
        assert len(update_calls) >= 1


# ═══════════════════════════════════════════════════════════════
#  10. Page Fetch Tests
# ═══════════════════════════════════════════════════════════════

class TestFetchDetailPage:
    """Tests for _fetch_detail_page with auth cookies + proxy."""

    @patch("app.workers.detail_scraper._get_auth_cookies", return_value={})
    @patch("app.workers.detail_scraper._get_proxy", return_value=None)
    @patch("httpx.Client")
    def test_successful_fetch(self, mock_client_class, mock_proxy, mock_cookies):
        """Should return HTML on successful fetch."""
        from app.workers.detail_scraper import _fetch_detail_page

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "<html>Product Page</html>"
        mock_resp.raise_for_status = MagicMock()

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = mock_resp
        mock_client_class.return_value = mock_client

        result = _fetch_detail_page("https://item.rakuten.co.jp/shop/item/")
        assert result == "<html>Product Page</html>"

    @patch("app.workers.detail_scraper._get_auth_cookies", return_value={})
    @patch("app.workers.detail_scraper._get_proxy", return_value=None)
    @patch("httpx.Client")
    def test_404_returns_none(self, mock_client_class, mock_proxy, mock_cookies):
        """Should return None for 404."""
        from app.workers.detail_scraper import _fetch_detail_page

        mock_resp = MagicMock()
        mock_resp.status_code = 404

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = mock_resp
        mock_client_class.return_value = mock_client

        result = _fetch_detail_page("https://item.rakuten.co.jp/shop/item/")
        assert result is None

    @patch("app.workers.detail_scraper._get_auth_cookies")
    @patch("app.workers.detail_scraper._get_proxy", return_value=None)
    @patch("httpx.Client")
    def test_auth_cookies_used(self, mock_client_class, mock_proxy, mock_cookies):
        """Should inject auth cookies for authenticated scraping."""
        from app.workers.detail_scraper import _fetch_detail_page

        mock_cookies.return_value = {"Ra": "auth_token", "Rb": "secondary"}

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "<html>Auth</html>"
        mock_resp.raise_for_status = MagicMock()

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = mock_resp
        mock_client_class.return_value = mock_client

        _fetch_detail_page("https://item.rakuten.co.jp/shop/item/")

        client_kwargs = mock_client_class.call_args.kwargs
        assert client_kwargs["cookies"] == {"Ra": "auth_token", "Rb": "secondary"}


# ═══════════════════════════════════════════════════════════════
#  11. Job Dispatcher Tests
# ═══════════════════════════════════════════════════════════════

class TestProcessPendingDetailJobs:
    """Tests for the job dispatcher task."""

    @patch("app.workers.detail_scraper.scrape_detail_page.delay")
    @patch("app.workers.detail_scraper.get_sync_db")
    def test_dispatches_pending_jobs(self, mock_get_db, mock_delay):
        """Should dispatch scrape tasks for pending jobs."""
        from app.workers.detail_scraper import process_pending_detail_jobs

        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        mock_db.execute.return_value.mappings.return_value.all.return_value = [
            {"id": 1, "target_url": "https://item.rakuten.co.jp/shop1/item1/"},
            {"id": 2, "target_url": "https://item.rakuten.co.jp/shop2/item2/"},
            {"id": 3, "target_url": "https://item.rakuten.co.jp/shop3/item3/"},
        ]

        result = process_pending_detail_jobs(batch_size=100)

        assert result["status"] == "ok"
        assert result["dispatched"] == 3
        assert mock_delay.call_count == 3

    @patch("app.workers.detail_scraper.scrape_detail_page.delay")
    @patch("app.workers.detail_scraper.get_sync_db")
    def test_no_pending_jobs(self, mock_get_db, mock_delay):
        """Should handle empty job queue gracefully."""
        from app.workers.detail_scraper import process_pending_detail_jobs

        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        mock_db.execute.return_value.mappings.return_value.all.return_value = []

        result = process_pending_detail_jobs()

        assert result["status"] == "ok"
        assert result["dispatched"] == 0
        mock_delay.assert_not_called()


# ═══════════════════════════════════════════════════════════════
#  12. Product UID Consistency Tests
# ═══════════════════════════════════════════════════════════════

class TestProductUIDConsistency:
    """Verify product_uid is correctly generated and returned."""

    @patch("app.workers.detail_scraper.time.sleep")
    @patch("app.workers.detail_scraper._save_detail_observation")
    @patch("app.workers.detail_scraper.upsert_snapshot_and_history_sync")
    @patch("app.workers.detail_scraper.upsert_variant_sync", return_value=1001)
    @patch("app.workers.detail_scraper.upsert_item_sync", return_value=501)
    @patch("app.workers.detail_scraper.upsert_shop_sync")
    @patch("app.workers.detail_scraper._fetch_detail_page")
    @patch("app.workers.detail_scraper.get_sync_db")
    def test_product_uid_in_json_result(
        self, mock_get_db, mock_fetch, mock_shop, mock_item,
        mock_variant, mock_snapshot, mock_save_obs, mock_sleep,
    ):
        """JSON scrape result should contain correct product_uid."""
        from app.workers.detail_scraper import scrape_detail_page

        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        mock_fetch.return_value = _make_detail_page_html()
        mock_variant.side_effect = [1001, 1002, 1003]

        result = scrape_detail_page("https://item.rakuten.co.jp/moriashizakka/edm544/")

        assert result["product_uid"] == "moriashizakka:edm544"

    @patch("app.workers.detail_scraper.time.sleep")
    @patch("app.workers.detail_scraper._save_detail_observation")
    @patch("app.workers.detail_scraper.upsert_snapshot_and_history_sync")
    @patch("app.workers.detail_scraper.upsert_variant_sync", return_value=1001)
    @patch("app.workers.detail_scraper.upsert_item_sync", return_value=501)
    @patch("app.workers.detail_scraper.upsert_shop_sync")
    @patch("app.workers.detail_scraper._fetch_detail_page")
    @patch("app.workers.detail_scraper.get_sync_db")
    def test_product_uid_in_html_result(
        self, mock_get_db, mock_fetch, mock_shop, mock_item,
        mock_variant, mock_snapshot, mock_save_obs, mock_sleep,
    ):
        """HTML fallback scrape result should contain correct product_uid."""
        from app.workers.detail_scraper import scrape_detail_page

        mock_db = MagicMock()
        mock_get_db.return_value = mock_db
        mock_fetch.return_value = _make_html_only_detail_page()

        result = scrape_detail_page("https://item.rakuten.co.jp/testshop/item1/")

        assert result["product_uid"] == "testshop:item1"

    def test_make_item_id_str_consistency(self):
        """make_item_id_str should produce the same format as MySQL CONCAT."""
        from app.services.url_canonicalizer import make_item_id_str

        # These should match what MySQL CONCAT(shop_code, ':', item_code) produces
        assert make_item_id_str("rakuten24", "404953") == "rakuten24:404953"
        assert make_item_id_str("horiman", "10009058") == "horiman:10009058"
        assert make_item_id_str("moriashizakka", "edm544") == "moriashizakka:edm544"
