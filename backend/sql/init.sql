-- ============================================================
-- Rakuten Price Tracker - Database Schema
-- MySQL 8.0+ / MariaDB 10.2+ required
-- ============================================================

SET NAMES utf8mb4;
SET CHARACTER SET utf8mb4;

-- ============================================================
-- 1. SHOPS (店舗マスタ)
-- ============================================================
CREATE TABLE IF NOT EXISTS shops (
    shop_code       VARCHAR(64)     NOT NULL COMMENT '店舗コード',
    shop_name       VARCHAR(512)    DEFAULT NULL COMMENT '店舗名',
    shop_url        VARCHAR(2048)   DEFAULT NULL COMMENT '店舗URL',
    first_seen_at   DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (shop_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='店舗マスタテーブル';

-- ============================================================
-- 2. ITEMS (商品マスタ — shopCode + itemCode 単位)
-- ============================================================
CREATE TABLE IF NOT EXISTS items (
    id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    product_uid     VARCHAR(320)    GENERATED ALWAYS AS (CONCAT(shop_code, ':', item_code)) STORED
                                    COMMENT '商品ユニークID ({shop_code}:{item_code})',
    shop_code       VARCHAR(64)     NOT NULL COMMENT '店舗コード',
    item_code       VARCHAR(255)    NOT NULL COMMENT '商品コード (URLスラッグ)',
    api_item_code   VARCHAR(255)    DEFAULT NULL COMMENT 'API itemCode (e.g. shopCode:productId)',
    canonical_url   VARCHAR(2048)   NOT NULL COMMENT '正規化済み商品URL',
    item_name       VARCHAR(1024)   DEFAULT NULL COMMENT '商品名',
    catchcopy       VARCHAR(2048)   DEFAULT NULL COMMENT '商品キャッチコピー',
    genre_id        VARCHAR(32)     DEFAULT NULL COMMENT 'ジャンルID',
    -- source tracking flags
    seen_in_api     TINYINT(1)      NOT NULL DEFAULT 0 COMMENT 'API経由で発見',
    seen_in_list    TINYINT(1)      NOT NULL DEFAULT 0 COMMENT '一覧ページ経由で発見',
    seen_in_detail  TINYINT(1)      NOT NULL DEFAULT 0 COMMENT '詳細ページ経由で発見',
    seen_in_ext     TINYINT(1)      NOT NULL DEFAULT 0 COMMENT 'Chrome拡張機能経由で発見',
    -- extension tracking (Phase 5: auto-delete logic)
    ext_first_seen  DATETIME        DEFAULT NULL COMMENT '拡張機能での初回閲覧日時',
    ext_view_count  INT UNSIGNED    NOT NULL DEFAULT 0 COMMENT '拡張機能での閲覧回数',
    -- added to server crawl target from extension?
    in_server_crawl TINYINT(1)      NOT NULL DEFAULT 0 COMMENT 'サーバークロール対象に追加済み',
    -- lifecycle
    is_active       TINYINT(1)      NOT NULL DEFAULT 1,
    created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_shop_item (shop_code, item_code),
    UNIQUE KEY uk_product_uid (product_uid),
    INDEX idx_api_item_code (api_item_code),
    INDEX idx_canonical_url (canonical_url(255)),
    INDEX idx_genre_id (genre_id),
    INDEX idx_ext_first_seen (ext_first_seen),
    INDEX idx_ext_view_count (ext_view_count),
    INDEX idx_in_server_crawl (in_server_crawl),
    INDEX idx_is_active (is_active),
    CONSTRAINT fk_items_shop FOREIGN KEY (shop_code) REFERENCES shops(shop_code)
        ON UPDATE CASCADE ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='商品マスタテーブル';

-- ============================================================
-- 3. VARIANTS (バリエーション — item + variantId 単位)
--    単一商品は variant_code = 'default'
-- ============================================================
CREATE TABLE IF NOT EXISTS variants (
    id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    item_id         BIGINT UNSIGNED NOT NULL,
    variant_code    VARCHAR(128)    NOT NULL DEFAULT 'default' COMMENT 'variantId or "default"',
    jan_code        VARCHAR(16)     DEFAULT NULL COMMENT 'JAN/GTINコード (13桁 or 12桁)',
    variant_name    VARCHAR(512)    DEFAULT NULL COMMENT 'バリエーション名 (e.g. フローラルスウィート)',
    selector_values JSON            DEFAULT NULL COMMENT 'サイズ/カラー等の選択値',
    is_active       TINYINT(1)      NOT NULL DEFAULT 1,
    created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_item_variant (item_id, variant_code),
    INDEX idx_jan_code (jan_code),
    INDEX idx_is_active (is_active),
    CONSTRAINT fk_variants_item FOREIGN KEY (item_id) REFERENCES items(id)
        ON UPDATE CASCADE ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='バリエーションテーブル';

-- ============================================================
-- 4. VARIANT_SNAPSHOTS (最新スナップショット — 現在値)
--    1 variant につき 1 行。毎回上書き。
-- ============================================================
CREATE TABLE IF NOT EXISTS variant_snapshots (
    variant_id          BIGINT UNSIGNED NOT NULL,
    price               INT UNSIGNED    DEFAULT NULL COMMENT '税込価格 [円]',
    point_rate           DECIMAL(5,1)   DEFAULT NULL COMMENT 'ショップポイント倍率 (e.g. +3倍 → 3.0)',
    point_back_percent   DECIMAL(5,1)   DEFAULT NULL COMMENT 'ポイントバック% (e.g. +30% → 30.0)',
    coupon_yen           INT UNSIGNED   DEFAULT NULL COMMENT 'クーポン額面 [円]',
    coupon_percent       DECIMAL(5,1)   DEFAULT NULL COMMENT 'クーポン割引率 [%]',
    shipping_text_raw    VARCHAR(512)   DEFAULT NULL COMMENT '配送テキスト原文',
    shipping_days_min    SMALLINT       DEFAULT NULL COMMENT '発送日数 最小',
    shipping_days_max    SMALLINT       DEFAULT NULL COMMENT '発送日数 最大',
    image_url            VARCHAR(2048)  DEFAULT NULL COMMENT '商品写真URL',
    extra1_key           VARCHAR(128)   DEFAULT NULL COMMENT '追加情報1 キー',
    extra1_value         VARCHAR(1024)  DEFAULT NULL COMMENT '追加情報1 値',
    extra2_key           VARCHAR(128)   DEFAULT NULL COMMENT '追加情報2 キー',
    extra2_value         VARCHAR(1024)  DEFAULT NULL COMMENT '追加情報2 値',
    content_hash         CHAR(64)       NOT NULL COMMENT 'SHA-256 hash for diff detection',
    fetched_at           DATETIME       NOT NULL COMMENT 'データ取得日時',
    source               ENUM('api','list','detail','extension') NOT NULL DEFAULT 'detail',
    updated_at           DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (variant_id),
    INDEX idx_price (price),
    INDEX idx_fetched_at (fetched_at),
    INDEX idx_coupon (coupon_yen, coupon_percent),
    INDEX idx_shipping_days (shipping_days_min),
    CONSTRAINT fk_snap_variant FOREIGN KEY (variant_id) REFERENCES variants(id)
        ON UPDATE CASCADE ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='バリエーション最新スナップショット';

-- ============================================================
-- 5. VARIANT_DAILY_HISTORY (日次履歴 — 最大730日)
--    同日に複数回取得 → 最新のみ上書き (UPSERT on variant_id + record_date)
--    RANGE パーティショニングで古いデータの削除・クエリを高速化
-- ============================================================
CREATE TABLE IF NOT EXISTS variant_daily_history (
    id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    variant_id      BIGINT UNSIGNED NOT NULL,
    record_date     DATE            NOT NULL COMMENT '記録日',
    price           INT UNSIGNED    DEFAULT NULL,
    point_rate      DECIMAL(5,1)    DEFAULT NULL,
    point_back_percent DECIMAL(5,1) DEFAULT NULL,
    coupon_yen      INT UNSIGNED    DEFAULT NULL,
    coupon_percent  DECIMAL(5,1)    DEFAULT NULL,
    shipping_days_min SMALLINT      DEFAULT NULL,
    shipping_days_max SMALLINT      DEFAULT NULL,
    image_url       VARCHAR(2048)   DEFAULT NULL,
    extra1_key      VARCHAR(128)    DEFAULT NULL,
    extra1_value    VARCHAR(1024)   DEFAULT NULL,
    extra2_key      VARCHAR(128)    DEFAULT NULL,
    extra2_value    VARCHAR(1024)   DEFAULT NULL,
    content_hash    CHAR(64)        NOT NULL,
    fetched_at      DATETIME        NOT NULL,
    source          ENUM('api','list','detail','extension') NOT NULL DEFAULT 'detail',
    PRIMARY KEY (id, record_date),
    UNIQUE KEY uk_variant_date (variant_id, record_date),
    INDEX idx_record_date (record_date),
    INDEX idx_variant_id (variant_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='バリエーション日次履歴テーブル (最大730日)'
  PARTITION BY RANGE (TO_DAYS(record_date)) (
    PARTITION p2025q1 VALUES LESS THAN (TO_DAYS('2025-04-01')),
    PARTITION p2025q2 VALUES LESS THAN (TO_DAYS('2025-07-01')),
    PARTITION p2025q3 VALUES LESS THAN (TO_DAYS('2025-10-01')),
    PARTITION p2025q4 VALUES LESS THAN (TO_DAYS('2026-01-01')),
    PARTITION p2026q1 VALUES LESS THAN (TO_DAYS('2026-04-01')),
    PARTITION p2026q2 VALUES LESS THAN (TO_DAYS('2026-07-01')),
    PARTITION p2026q3 VALUES LESS THAN (TO_DAYS('2026-10-01')),
    PARTITION p2026q4 VALUES LESS THAN (TO_DAYS('2027-01-01')),
    PARTITION p2027q1 VALUES LESS THAN (TO_DAYS('2027-04-01')),
    PARTITION p2027q2 VALUES LESS THAN (TO_DAYS('2027-07-01')),
    PARTITION p2027q3 VALUES LESS THAN (TO_DAYS('2027-10-01')),
    PARTITION p2027q4 VALUES LESS THAN (TO_DAYS('2028-01-01')),
    PARTITION p_future VALUES LESS THAN MAXVALUE
  );

-- ============================================================
-- 6. CRAWL_TARGETS (クロール対象 — ジャンル/店舗/キーワード)
-- ============================================================
CREATE TABLE IF NOT EXISTS crawl_targets (
    id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    target_type     ENUM('genre','shop','keyword') NOT NULL COMMENT '対象タイプ',
    target_value    VARCHAR(512)    NOT NULL COMMENT 'genre_id / shop_code / keyword',
    base_url        VARCHAR(2048)   DEFAULT NULL COMMENT '基本URL',
    is_active       TINYINT(1)      NOT NULL DEFAULT 1,
    priority        SMALLINT        NOT NULL DEFAULT 0 COMMENT '優先度 (高い=先に処理)',
    added_by        ENUM('manual','auto_ranking','extension') NOT NULL DEFAULT 'manual',
    last_crawled_at DATETIME        DEFAULT NULL,
    created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_type_value (target_type, target_value(255)),
    INDEX idx_active_priority (is_active, priority DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='クロール対象マスタ';

-- ============================================================
-- 7. PRICE_RANGE_SLICES (価格帯スライス管理)
--    API / 一覧ページの 6750件制限対策用
-- ============================================================
CREATE TABLE IF NOT EXISTS price_range_slices (
    id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    crawl_target_id BIGINT UNSIGNED NOT NULL,
    min_price       INT UNSIGNED    NOT NULL DEFAULT 0,
    max_price       INT UNSIGNED    NOT NULL,
    estimated_count INT UNSIGNED    DEFAULT NULL COMMENT '推定ヒット件数',
    last_crawled_at DATETIME        DEFAULT NULL,
    status          ENUM('pending','in_progress','done','needs_split') NOT NULL DEFAULT 'pending',
    created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    INDEX idx_target_status (crawl_target_id, status),
    CONSTRAINT fk_slice_target FOREIGN KEY (crawl_target_id) REFERENCES crawl_targets(id)
        ON UPDATE CASCADE ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='価格帯スライス管理テーブル';

-- ============================================================
-- 8. CRAWL_JOBS (ジョブキュー — API / List / Detail)
-- ============================================================
CREATE TABLE IF NOT EXISTS crawl_jobs (
    id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    job_type        ENUM('api_discovery','list_scrape','detail_scrape') NOT NULL,
    target_url      VARCHAR(2048)   NOT NULL,
    params_json     JSON            DEFAULT NULL COMMENT 'ジョブパラメータ (genreId, minPrice, maxPrice, page, etc.)',
    item_id         BIGINT UNSIGNED DEFAULT NULL COMMENT '対象商品ID (detail_scrape時)',
    status          ENUM('pending','running','done','failed','skipped') NOT NULL DEFAULT 'pending',
    attempts        TINYINT UNSIGNED NOT NULL DEFAULT 0,
    max_attempts    TINYINT UNSIGNED NOT NULL DEFAULT 3,
    locked_by       VARCHAR(128)    DEFAULT NULL COMMENT 'ワーカーID',
    locked_at       DATETIME        DEFAULT NULL,
    started_at      DATETIME        DEFAULT NULL COMMENT 'ジョブ開始日時',
    error_message   TEXT            DEFAULT NULL,
    result_summary  JSON            DEFAULT NULL COMMENT '処理結果サマリ',
    created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    scheduled_at    DATETIME        DEFAULT NULL COMMENT '実行予定日時',
    completed_at    DATETIME        DEFAULT NULL,
    PRIMARY KEY (id),
    INDEX idx_status_type (status, job_type),
    INDEX idx_target_url (target_url(255)),
    INDEX idx_locked_at (locked_at),
    INDEX idx_scheduled_at (scheduled_at),
    INDEX idx_item_id (item_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='クロールジョブキューテーブル';

-- ============================================================
-- 9. USER_BROWSE_EVENTS (拡張機能ユーザー閲覧ログ)
--    直近24時間の集計 → 店舗コード頻度ランキング
-- ============================================================
CREATE TABLE IF NOT EXISTS user_browse_events (
    id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    user_id         VARCHAR(128)    NOT NULL COMMENT 'ユーザー匿名ハッシュ (拡張機能)',
    variant_id      BIGINT UNSIGNED DEFAULT NULL,
    item_id         BIGINT UNSIGNED DEFAULT NULL,
    shop_code       VARCHAR(64)     NOT NULL,
    page_url        VARCHAR(2048)   DEFAULT NULL COMMENT '閲覧ページURL',
    browsed_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    INDEX idx_browsed_at (browsed_at),
    INDEX idx_shop_code_browsed (shop_code, browsed_at),
    INDEX idx_user_browsed (user_id, browsed_at),
    INDEX idx_item_browsed (item_id, browsed_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='ユーザー閲覧イベントログ';

-- ============================================================
-- 10. SHOP_RANKINGS (店舗コード頻度ランキング — 集計結果)
-- ============================================================
CREATE TABLE IF NOT EXISTS shop_rankings (
    id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    ranking_date    DATE            NOT NULL COMMENT 'ランキング集計日',
    shop_code       VARCHAR(64)     NOT NULL,
    browse_count    INT UNSIGNED    NOT NULL DEFAULT 0 COMMENT '閲覧回数',
    rank_position   INT UNSIGNED    DEFAULT NULL COMMENT '順位',
    created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_date_shop (ranking_date, shop_code),
    INDEX idx_date_rank (ranking_date, rank_position)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='店舗コード頻度ランキング';

-- ============================================================
-- 11. LIST_OBSERVATIONS (一覧ページ由来の補助データ)
--     API結果との差分マージ比較用
-- ============================================================
CREATE TABLE IF NOT EXISTS list_observations (
    id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    item_id         BIGINT UNSIGNED NOT NULL,
    -- Price
    observed_price  INT UNSIGNED    DEFAULT NULL COMMENT '税込価格',
    price_range     VARCHAR(128)    DEFAULT NULL COMMENT '価格帯テキスト (例: "6270")',
    -- Points (structured from JSON)
    point_count     INT UNSIGNED    DEFAULT NULL COMMENT '獲得ポイント数',
    point_base_multiplier    SMALLINT DEFAULT NULL COMMENT '基本倍率 (例: 1)',
    point_shop_multiplier    SMALLINT DEFAULT NULL COMMENT 'ショップ倍率 (例: 5)',
    point_up_multiplier      SMALLINT DEFAULT NULL COMMENT 'ポイントUP倍率 (例: 4)',
    point_deal_multiplier    SMALLINT DEFAULT NULL COMMENT 'スーパーDEAL倍率',
    point_item_multiplier    SMALLINT DEFAULT NULL COMMENT 'アイテム倍率',
    point_total_multiplier   SMALLINT DEFAULT NULL COMMENT 'ポイント合計倍率 (base+shop+up+deal+item)',
    is_point_back   TINYINT(1)      NOT NULL DEFAULT 0 COMMENT 'ポイントバックフラグ',
    point_back_rate DECIMAL(5,1)    DEFAULT NULL COMMENT 'ポイントバック率 (例: 20%→20.0)',
    point_text_raw  VARCHAR(256)    DEFAULT NULL COMMENT 'ポイント表示テキスト原文 (例: "780ポイント(1倍+9倍UP)" or "2,520ポイント(20%ポイントバック)")',
    -- Coupon (structured from JSON)
    coupon_discount INT UNSIGNED    DEFAULT NULL COMMENT 'クーポン値 (例: 200, 15)',
    coupon_type     ENUM('exact','percentage') DEFAULT NULL COMMENT 'クーポンタイプ (円/％)',
    coupon_text_raw VARCHAR(256)    DEFAULT NULL COMMENT 'クーポンテキスト原文 (例: "200円OFFクーポンあり")',
    -- Shipping & Delivery
    shipping_cost   INT UNSIGNED    DEFAULT NULL COMMENT '送料 (0=送料無料)',
    delivery_text   VARCHAR(512)    DEFAULT NULL COMMENT '配送テキスト (例: "12:00までの注文で最短2/22(翌日)お届け")',
    delivery_days   SMALLINT        DEFAULT NULL COMMENT '配送日数',
    is_free_shipping TINYINT(1)     NOT NULL DEFAULT 0 COMMENT '送料無料フラグ',
    -- Review
    review_score    DECIMAL(3,2)    DEFAULT NULL COMMENT '評価スコア (例: 4.75)',
    review_count    INT UNSIGNED    DEFAULT NULL COMMENT 'レビュー数 (例: 169)',
    -- Shop info
    shop_code       VARCHAR(64)     DEFAULT NULL COMMENT '店舗コード',
    shop_name       VARCHAR(256)    DEFAULT NULL COMMENT '店舗名',
    shop_url_code   VARCHAR(128)    DEFAULT NULL COMMENT '店舗URLコード',
    -- Item metadata
    item_name       VARCHAR(1024)   DEFAULT NULL COMMENT '商品名',
    item_subtitle   VARCHAR(1024)   DEFAULT NULL COMMENT 'サブタイトル(キャッチコピー)',
    image_url       VARCHAR(2048)   DEFAULT NULL COMMENT '商品画像URL',
    genre_ids       VARCHAR(256)    DEFAULT NULL COMMENT 'ジャンルIDリスト',
    variant_id      VARCHAR(128)    DEFAULT NULL COMMENT 'バリアントID',
    -- SKU info
    has_multi_sku   TINYINT(1)      NOT NULL DEFAULT 0 COMMENT '複数SKUフラグ',
    -- Flags
    is_pr           TINYINT(1)      NOT NULL DEFAULT 0 COMMENT 'PR/広告フラグ',
    is_super_deal   TINYINT(1)      NOT NULL DEFAULT 0 COMMENT 'スーパーDEALフラグ',
    is_shop39       TINYINT(1)      NOT NULL DEFAULT 0 COMMENT '39ショップフラグ',
    is_sold_out     TINYINT(1)      NOT NULL DEFAULT 0 COMMENT '売切れフラグ',
    -- Source
    data_source     ENUM('html','json','both') NOT NULL DEFAULT 'html' COMMENT 'データ取得元',
    list_url        VARCHAR(2048)   DEFAULT NULL COMMENT '取得元一覧ページURL',
    observed_at     DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    INDEX idx_item_observed (item_id, observed_at),
    INDEX idx_shop_code (shop_code),
    CONSTRAINT fk_listobs_item FOREIGN KEY (item_id) REFERENCES items(id)
        ON UPDATE CASCADE ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='一覧ページ由来の観測データ (HTML + 埋め込みJSON)';

-- ============================================================
-- 12. DETAIL_OBSERVATIONS (詳細ページ由来の観測データ)
--     商品詳細ページから取得した構造化データ
--     JSON埋め込みデータ (item-page-app-data) から抽出
-- ============================================================
CREATE TABLE IF NOT EXISTS detail_observations (
    id                  BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    item_id             BIGINT UNSIGNED NOT NULL,
    -- Basic item info
    item_name           VARCHAR(1024)   DEFAULT NULL COMMENT '商品名',
    shop_name           VARCHAR(512)    DEFAULT NULL COMMENT '店舗名',
    shop_url_code       VARCHAR(128)    DEFAULT NULL COMMENT '店舗URLコード',
    -- Price range
    price_min           INT UNSIGNED    DEFAULT NULL COMMENT '最低価格 (税込)',
    price_max           INT UNSIGNED    DEFAULT NULL COMMENT '最高価格 (税込)',
    -- Review
    review_count        INT UNSIGNED    DEFAULT NULL COMMENT 'レビュー数',
    review_rating       DECIMAL(4,2)    DEFAULT NULL COMMENT '平均評価 (例: 4.83)',
    -- Flags
    is_39_shop          TINYINT(1)      NOT NULL DEFAULT 0 COMMENT '39ショップフラグ',
    is_super_deal       TINYINT(1)      NOT NULL DEFAULT 0 COMMENT 'スーパーDEALフラグ',
    -- Inventory
    inventory_type      VARCHAR(32)     DEFAULT 'single' COMMENT 'single or multiple',
    -- Coupon
    coupon_yen          INT UNSIGNED    DEFAULT NULL COMMENT 'クーポン円額',
    coupon_percent      DECIMAL(5,1)    DEFAULT NULL COMMENT 'クーポン割引率%',
    -- Variants summary
    variant_count       SMALLINT UNSIGNED DEFAULT 0 COMMENT 'バリエーション数',
    variant_data_json   JSON            DEFAULT NULL COMMENT 'バリエーション概要JSON [{variantId, jan, price, name, soldOut, stock, deliveryMsg}]',
    -- Genre
    ancestor_genre_id   VARCHAR(32)     DEFAULT NULL COMMENT '祖先ジャンルID',
    r_category_id       VARCHAR(32)     DEFAULT NULL COMMENT '楽天カテゴリID',
    -- Source
    data_source         ENUM('json','html') NOT NULL DEFAULT 'json' COMMENT 'データ取得元',
    detail_url          VARCHAR(2048)   DEFAULT NULL COMMENT '取得元詳細ページURL',
    observed_at         DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    INDEX idx_item_observed (item_id, observed_at),
    INDEX idx_observed_at (observed_at),
    CONSTRAINT fk_detailobs_item FOREIGN KEY (item_id) REFERENCES items(id)
        ON UPDATE CASCADE ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='詳細ページ由来の観測データ (埋め込みJSON)';

-- ============================================================
-- 13. CRAWL_STATS (クロール統計 — 監視用)
-- ============================================================
CREATE TABLE IF NOT EXISTS crawl_stats (
    id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    stat_date       DATE            NOT NULL,
    job_type        ENUM('api_discovery','list_scrape','detail_scrape') NOT NULL,
    total_jobs      INT UNSIGNED    NOT NULL DEFAULT 0,
    success_count   INT UNSIGNED    NOT NULL DEFAULT 0,
    fail_count      INT UNSIGNED    NOT NULL DEFAULT 0,
    skip_count      INT UNSIGNED    NOT NULL DEFAULT 0,
    avg_duration_ms INT UNSIGNED    DEFAULT NULL COMMENT '平均処理時間 (ms)',
    items_found     INT UNSIGNED    NOT NULL DEFAULT 0 COMMENT '発見商品数',
    items_updated   INT UNSIGNED    NOT NULL DEFAULT 0 COMMENT '更新商品数',
    created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_date_type (stat_date, job_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='クロール統計テーブル';
