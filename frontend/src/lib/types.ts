/* ── API Response Types ─────────────────────────────── */

export interface VariantSnapshot {
  price: number | null;
  point_rate: number | null;
  point_back_percent: number | null;
  coupon_yen: number | null;
  coupon_percent: number | null;
  shipping_text_raw: string | null;
  shipping_days_min: number | null;
  shipping_days_max: number | null;
  image_url: string | null;
  extra1_key: string | null;
  extra1_value: string | null;
  extra2_key: string | null;
  extra2_value: string | null;
  fetched_at: string | null;
  source: string | null;
}

export interface Variant {
  variant_id: number;
  variant_code: string;
  jan_code: string | null;
  variant_name: string | null;
  snapshot: VariantSnapshot | null;
}

export interface Product {
  item_id: number;
  product_uid: string;
  shop_code: string;
  shop_name: string | null;
  shop_url: string | null;
  item_code: string;
  item_name: string | null;
  canonical_url: string;
  variants: Variant[];
}

export interface ProductSearchResult {
  variant_id: number;
  product_uid: string;
  shop_code: string;
  shop_name: string | null;
  shop_url: string | null;
  item_code: string;
  item_name: string | null;
  canonical_url: string;
  variant_code: string;
  jan_code: string | null;
  variant_name: string | null;
  price: number | null;
  point_rate: number | null;
  point_back_percent: number | null;
  coupon_yen: number | null;
  coupon_percent: number | null;
  shipping_text_raw: string | null;
  shipping_days_min: number | null;
  shipping_days_max: number | null;
  image_url: string | null;
  extra1_key: string | null;
  extra1_value: string | null;
  extra2_key: string | null;
  extra2_value: string | null;
  fetched_at: string | null;
}

export interface ProductSearchResponse {
  page: number;
  per_page: number;
  total: number;
  results: ProductSearchResult[];
}

export interface HistoryRecord {
  record_date: string;
  price: number | null;
  point_rate: number | null;
  point_back_percent: number | null;
  coupon_yen: number | null;
  coupon_percent: number | null;
  shipping_days_min: number | null;
  shipping_days_max: number | null;
}

export interface VariantHistoryResponse {
  variant_id: number;
  days: number;
  history: HistoryRecord[];
}

export interface PointCalcOutput {
  effective_coupon: number;
  total_points: number;
  price_minus_points: number;
  price_minus_points_coupon: number;
}

export interface PointCalcInput {
  point_multiplier: number;
  rakuten_price: number;
  coupon_yen: number;
  coupon_percent: number;
  plus_x_up: number;
  point_back_flag: number;
}

export interface PointCalcResponse {
  variant_id: number;
  input: PointCalcInput;
  output: PointCalcOutput;
}

export interface ShopRankEntry {
  rank: number;
  shop_code: string;
  shop_name: string | null;
  browse_count: number;
}

export interface ShopRankingResponse {
  period_hours: number;
  rankings: ShopRankEntry[];
}

export interface JanSearchResult {
  jan_code: string;
  variants: ProductSearchResult[];
}

/* ── Frontend-only types ────────────────────────────── */

export interface WatchlistItem {
  product_uid: string;
  item_name: string;
  image_url: string | null;
  added_at: string;
}
