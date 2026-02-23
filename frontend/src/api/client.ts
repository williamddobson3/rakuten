const BASE = '/api/v1';

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${url}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`API ${res.status}: ${body}`);
  }
  return res.json();
}

import type {
  ProductSearchResponse,
  Product,
  VariantHistoryResponse,
  PointCalcResponse,
  ShopRankingResponse,
  JanSearchResult,
} from '../lib/types';

/* ── Products ──────────────────────────────────── */

export function searchProducts(params: {
  q?: string;
  jan?: string;
  shop_code?: string;
  has_jan?: boolean;
  has_coupon?: boolean;
  has_variant?: boolean;
  updated_24h?: boolean;
  price_min?: number;
  price_max?: number;
  shipping_max_days?: number;
  sort_by?: string;
  page?: number;
  per_page?: number;
}): Promise<ProductSearchResponse> {
  const sp = new URLSearchParams();
  if (params.q) sp.set('q', params.q);
  if (params.jan) sp.set('jan', params.jan);
  if (params.shop_code) sp.set('shop_code', params.shop_code);
  if (params.has_jan) sp.set('has_jan', 'true');
  if (params.has_coupon) sp.set('has_coupon', 'true');
  if (params.has_variant) sp.set('has_variant', 'true');
  if (params.updated_24h) sp.set('updated_24h', 'true');
  if (params.price_min != null) sp.set('price_min', String(params.price_min));
  if (params.price_max != null) sp.set('price_max', String(params.price_max));
  if (params.shipping_max_days != null) sp.set('shipping_max_days', String(params.shipping_max_days));
  if (params.sort_by) sp.set('sort_by', params.sort_by);
  if (params.page) sp.set('page', String(params.page));
  if (params.per_page) sp.set('per_page', String(params.per_page));
  return request<ProductSearchResponse>(`/products/search?${sp}`);
}

export function getProduct(itemId: number): Promise<Product> {
  return request<Product>(`/products/${itemId}`);
}

export function getProductByUid(uid: string): Promise<Product> {
  return request<Product>(`/products/uid/${uid}`);
}

export function getJanProducts(janCode: string, pointMultiplier?: number): Promise<JanSearchResult> {
  const sp = new URLSearchParams();
  if (pointMultiplier) sp.set('point_multiplier', String(pointMultiplier));
  return request<JanSearchResult>(`/products/jan/${janCode}?${sp}`);
}

/* ── History ───────────────────────────────────── */

export function getVariantHistory(variantId: number, days: number): Promise<VariantHistoryResponse> {
  return request<VariantHistoryResponse>(`/history/${variantId}?days=${days}`);
}

export function calculatePoints(variantId: number, pointMultiplier: number): Promise<PointCalcResponse> {
  return request<PointCalcResponse>(`/history/${variantId}/calculate?point_multiplier=${pointMultiplier}`);
}

/* ── Rankings ──────────────────────────────────── */

export function getLiveRankings(limit?: number, hours?: number): Promise<ShopRankingResponse> {
  const sp = new URLSearchParams();
  if (limit) sp.set('limit', String(limit));
  if (hours) sp.set('hours', String(hours));
  return request<ShopRankingResponse>(`/rankings/shops/live?${sp}`);
}

export function getDailyRankings(date?: string, limit?: number): Promise<ShopRankingResponse> {
  const sp = new URLSearchParams();
  if (date) sp.set('date', date);
  if (limit) sp.set('limit', String(limit));
  return request<ShopRankingResponse>(`/rankings/shops/daily?${sp}`);
}

/* ── System ────────────────────────────────────── */

export function healthCheck(): Promise<{ status: string }> {
  return request<{ status: string }>('/../health');
}
