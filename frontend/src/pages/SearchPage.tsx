import { useState, useEffect, useCallback } from 'react';
import { useSearchParams } from 'react-router-dom';
import {
  Search,
  RefreshCw,
  SlidersHorizontal,
  ChevronDown,
  ChevronUp,
  Loader2,
  AlertCircle,
  Package,
  ArrowUpDown,
} from 'lucide-react';
import { searchProducts } from '../api/client';
import ProductCard from '../components/ProductCard';
import Pagination from '../components/Pagination';
import type { ProductSearchResult, WatchlistItem } from '../lib/types';
import { cn } from '../lib/utils';

interface Props {
  multiplier: number;
  watchlist: WatchlistItem[];
  isWatching: (uid: string) => boolean;
  addWatch: (item: WatchlistItem) => void;
  removeWatch: (uid: string) => void;
}

const SORT_OPTIONS = [
  { value: 'price_asc', label: '有効価格 (安い順)' },
  { value: 'price_desc', label: '有効価格 (高い順)' },
  { value: 'updated', label: '最終更新 (新しい順)' },
  { value: 'name', label: '商品名 (あいうえお順)' },
];

const PER_PAGE = 20;

export default function SearchPage({
  multiplier,
  isWatching,
  addWatch,
  removeWatch,
}: Props) {
  const [searchParams, setSearchParams] = useSearchParams();
  const [query, setQuery] = useState(searchParams.get('q') || '');
  const [sortBy, setSortBy] = useState(searchParams.get('sort') || 'price_asc');
  const [page, setPage] = useState(Number(searchParams.get('page')) || 1);

  const [hasJan, setHasJan] = useState(searchParams.get('has_jan') === 'true');
  const [hasCoupon, setHasCoupon] = useState(searchParams.get('has_coupon') === 'true');
  const [hasVariant, setHasVariant] = useState(searchParams.get('has_variant') === 'true');
  const [updated24h, setUpdated24h] = useState(searchParams.get('updated_24h') === 'true');
  const [priceMin, setPriceMin] = useState(searchParams.get('price_min') || '');
  const [priceMax, setPriceMax] = useState(searchParams.get('price_max') || '');
  const [shippingMaxDays, setShippingMaxDays] = useState<number | null>(
    searchParams.get('shipping_max_days') ? Number(searchParams.get('shipping_max_days')) : null
  );
  const [results, setResults] = useState<ProductSearchResult[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdate, setLastUpdate] = useState<Date | null>(null);
  const [filtersOpen, setFiltersOpen] = useState(true);

  const totalPages = Math.ceil(total / PER_PAGE);

  interface FilterOverrides {
    has_jan?: boolean;
    has_coupon?: boolean;
    has_variant?: boolean;
    updated_24h?: boolean;
    price_min?: string;
    price_max?: string;
    shipping_max_days?: number | null;
  }

  const doSearch = useCallback(
    async (q: string, sort: string, p: number, filters?: FilterOverrides) => {
      setLoading(true);
      setError(null);
      try {
        const f = filters ?? {};
        const janVal = f.has_jan ?? hasJan;
        const couponVal = f.has_coupon ?? hasCoupon;
        const variantVal = f.has_variant ?? hasVariant;
        const updated24hVal = f.updated_24h ?? updated24h;
        const pMinStr = f.price_min ?? priceMin;
        const pMaxStr = f.price_max ?? priceMax;
        const shipVal = f.shipping_max_days !== undefined ? f.shipping_max_days : shippingMaxDays;

        const pMin = pMinStr ? parseInt(pMinStr, 10) : undefined;
        const pMax = pMaxStr ? parseInt(pMaxStr, 10) : undefined;

        const res = await searchProducts({
          q: q || undefined,
          has_jan: janVal || undefined,
          has_coupon: couponVal || undefined,
          has_variant: variantVal || undefined,
          updated_24h: updated24hVal || undefined,
          price_min: !isNaN(pMin!) ? pMin : undefined,
          price_max: !isNaN(pMax!) ? pMax : undefined,
          shipping_max_days: shipVal ?? undefined,
          sort_by: sort,
          page: p,
          per_page: PER_PAGE,
        });
        setResults(res.results);
        setTotal(res.total);
        setLastUpdate(new Date());
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Search failed');
      } finally {
        setLoading(false);
      }
    },
    [hasJan, hasCoupon, hasVariant, updated24h, priceMin, priceMax, shippingMaxDays]
  );

  useEffect(() => {
    doSearch(query, sortBy, page, { has_jan: hasJan });
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const buildParams = (overrides?: FilterOverrides & { page?: string; sort?: string }) => {
    const p: Record<string, string> = {
      q: query,
      sort: overrides?.sort ?? sortBy,
      page: overrides?.page ?? '1',
    };
    if ((overrides?.has_jan ?? hasJan)) p.has_jan = 'true';
    if ((overrides?.has_coupon ?? hasCoupon)) p.has_coupon = 'true';
    if ((overrides?.has_variant ?? hasVariant)) p.has_variant = 'true';
    if ((overrides?.updated_24h ?? updated24h)) p.updated_24h = 'true';
    const pMin = overrides?.price_min ?? priceMin;
    const pMax = overrides?.price_max ?? priceMax;
    if (pMin) p.price_min = pMin;
    if (pMax) p.price_max = pMax;
    const ship = overrides?.shipping_max_days !== undefined ? overrides.shipping_max_days : shippingMaxDays;
    if (ship != null) p.shipping_max_days = String(ship);
    return p;
  };

  const handleSearch = () => {
    setPage(1);
    setSearchParams(buildParams());
    doSearch(query, sortBy, 1);
  };

  const handleSortChange = (newSort: string) => {
    setSortBy(newSort);
    setPage(1);
    setSearchParams(buildParams({ sort: newSort }));
    doSearch(query, newSort, 1);
  };

  const handlePageChange = (newPage: number) => {
    setPage(newPage);
    setSearchParams(buildParams({ page: String(newPage) }));
    doSearch(query, sortBy, newPage);
    window.scrollTo({ top: 0, behavior: 'smooth' });
  };

  const handleFilterToggle = (
    key: 'has_jan' | 'has_coupon' | 'has_variant' | 'updated_24h',
    checked: boolean
  ) => {
    const setters: Record<string, (v: boolean) => void> = {
      has_jan: setHasJan,
      has_coupon: setHasCoupon,
      has_variant: setHasVariant,
      updated_24h: setUpdated24h,
    };
    setters[key](checked);
    setPage(1);
    setSearchParams(buildParams({ [key]: checked }));
    doSearch(query, sortBy, 1, { [key]: checked });
  };

  const handlePriceApply = () => {
    setPage(1);
    setSearchParams(buildParams({ price_min: priceMin, price_max: priceMax }));
    doSearch(query, sortBy, 1, { price_min: priceMin, price_max: priceMax });
  };

  const handleShippingChange = (maxDays: number | null) => {
    setShippingMaxDays(maxDays);
    setPage(1);
    setSearchParams(buildParams({ shipping_max_days: maxDays }));
    doSearch(query, sortBy, 1, { shipping_max_days: maxDays });
  };

  const handleClearFilters = () => {
    setHasJan(false);
    setHasCoupon(false);
    setHasVariant(false);
    setUpdated24h(false);
    setPriceMin('');
    setPriceMax('');
    setShippingMaxDays(null);
    setPage(1);
    const p: Record<string, string> = { q: query, sort: sortBy, page: '1' };
    setSearchParams(p);
    doSearch(query, sortBy, 1, {
      has_jan: false, has_coupon: false, has_variant: false, updated_24h: false,
      price_min: '', price_max: '', shipping_max_days: null,
    });
  };

  const handleRefresh = () => doSearch(query, sortBy, page);

  const toggleWatch = (item: ProductSearchResult) => {
    if (isWatching(item.product_uid)) {
      removeWatch(item.product_uid);
    } else {
      addWatch({
        product_uid: item.product_uid,
        item_name: item.item_name ?? '',
        image_url: item.image_url,
        added_at: new Date().toISOString(),
      });
    }
  };

  return (
    <div className="max-w-[1480px] mx-auto px-5 py-5">
      <div className="flex gap-5">
        {/* ══════ Filters Sidebar ══════════════════════ */}
        <aside
          className={cn(
            'shrink-0 transition-all duration-300',
            filtersOpen ? 'w-[240px]' : 'w-0 overflow-hidden'
          )}
        >
          <div className="bg-white rounded-xl border border-slate-200 shadow-sm sticky top-[70px]">
            {/* Sidebar header */}
            <div className="flex items-center justify-between p-4 border-b border-slate-100">
              <div className="flex items-center gap-2">
                <SlidersHorizontal className="w-4 h-4 text-slate-500" />
                <span className="text-sm font-bold text-slate-700">
                  絞り込み条件
                </span>
              </div>
              <button
                onClick={handleClearFilters}
                className="text-xs text-blue-500 hover:text-blue-600 font-medium"
              >
                クリア
              </button>
            </div>

            <div className="p-4 space-y-1">
              {/* Marketplace */}
              <FilterSection title="マーケットプレイス">
                <label className="flex items-center gap-2.5 text-[13px] text-slate-600 cursor-pointer">
                  <input
                    type="checkbox"
                    checked
                    readOnly
                    className="w-4 h-4 rounded border-slate-300 text-blue-600 focus:ring-blue-500"
                  />
                  Rakuten
                </label>
              </FilterSection>

              {/* Price range */}
              <FilterSection title="価格帯 (¥)">
                <div className="space-y-3">
                  <div className="flex items-center gap-2">
                    <input
                      type="text"
                      value={priceMin}
                      onChange={(e) => setPriceMin(e.target.value.replace(/[^0-9]/g, ''))}
                      onKeyDown={(e) => e.key === 'Enter' && handlePriceApply()}
                      placeholder="¥0"
                      className="w-full px-2.5 py-1.5 text-xs text-center border border-slate-200 rounded-lg bg-white focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                    />
                    <span className="text-slate-400 text-xs shrink-0">〜</span>
                    <input
                      type="text"
                      value={priceMax}
                      onChange={(e) => setPriceMax(e.target.value.replace(/[^0-9]/g, ''))}
                      onKeyDown={(e) => e.key === 'Enter' && handlePriceApply()}
                      placeholder="¥100,000"
                      className="w-full px-2.5 py-1.5 text-xs text-center border border-slate-200 rounded-lg bg-white focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent"
                    />
                  </div>
                  <button
                    onClick={handlePriceApply}
                    className="w-full py-1.5 text-xs font-medium text-white bg-blue-600 hover:bg-blue-700 rounded-lg transition-colors"
                  >
                    価格で絞り込み
                  </button>
                </div>
              </FilterSection>

              {/* Shipping */}
              <FilterSection title="発送日数">
                {[
                  { label: 'すべて', value: null },
                  { label: '1-2日以内 (即納)', value: 2 },
                  { label: '3-5日以内', value: 5 },
                  { label: '7日以内', value: 7 },
                ].map((opt) => (
                  <label
                    key={opt.label}
                    className="flex items-center gap-2.5 text-[13px] text-slate-600 cursor-pointer"
                  >
                    <input
                      type="radio"
                      name="shipping_filter"
                      checked={shippingMaxDays === opt.value}
                      onChange={() => handleShippingChange(opt.value)}
                      className="w-4 h-4 border-slate-300 text-blue-600 focus:ring-blue-500"
                    />
                    {opt.label}
                  </label>
                ))}
              </FilterSection>

              {/* Options */}
              <FilterSection title="オプション">
                <label className="flex items-center gap-2.5 text-[13px] text-slate-600 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={hasJan}
                    onChange={(e) => handleFilterToggle('has_jan', e.target.checked)}
                    className="w-4 h-4 rounded border-slate-300 text-blue-600 focus:ring-blue-500"
                  />
                  <span className="flex items-center gap-1">
                    JANコードありのみ
                    <span className="inline-flex items-center justify-center px-1.5 py-0.5 bg-emerald-100 text-emerald-700 text-[10px] font-bold rounded">
                      JAN
                    </span>
                  </span>
                </label>
                <label className="flex items-center gap-2.5 text-[13px] text-slate-600 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={hasCoupon}
                    onChange={(e) => handleFilterToggle('has_coupon', e.target.checked)}
                    className="w-4 h-4 rounded border-slate-300 text-blue-600 focus:ring-blue-500"
                  />
                  クーポンありのみ表示
                </label>
                <label className="flex items-center gap-2.5 text-[13px] text-slate-600 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={hasVariant}
                    onChange={(e) => handleFilterToggle('has_variant', e.target.checked)}
                    className="w-4 h-4 rounded border-slate-300 text-blue-600 focus:ring-blue-500"
                  />
                  バリアントのみ表示
                </label>
                <label className="flex items-center gap-2.5 text-[13px] text-slate-600 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={updated24h}
                    onChange={(e) => handleFilterToggle('updated_24h', e.target.checked)}
                    className="w-4 h-4 rounded border-slate-300 text-blue-600 focus:ring-blue-500"
                  />
                  24時間以内の更新
                </label>
              </FilterSection>
            </div>

            {/* Multiplier info */}
            <div className="mx-4 mb-4 p-3 bg-blue-50 rounded-lg border border-blue-100">
              <div className="flex items-center gap-1.5 text-xs font-semibold text-blue-700 mb-1">
                <AlertCircle className="w-3.5 h-3.5" />
                ポイント計算中
              </div>
              <p className="text-[11px] text-blue-600 leading-relaxed">
                現在、設定された「{multiplier}倍」を元に実質価格を算出しています。
                キャンペーン等で変動する場合は上部で調整ください。
              </p>
            </div>
          </div>
        </aside>

        {/* ══════ Main Content ═══════════════════════ */}
        <div className="flex-1 min-w-0">
          {/* Search Bar */}
          <div className="flex gap-2.5 mb-5">
            <button
              onClick={() => setFiltersOpen(!filtersOpen)}
              className="p-2.5 bg-white border border-slate-200 rounded-xl hover:bg-slate-50 transition-colors lg:hidden shrink-0"
            >
              <SlidersHorizontal className="w-5 h-5 text-slate-500" />
            </button>
            <div className="flex-1 relative">
              <Search className="absolute left-4 top-1/2 -translate-y-1/2 w-5 h-5 text-slate-400" />
              <input
                type="text"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && handleSearch()}
                placeholder="JANコード、キーワード、商品URLを入力して検索..."
                className="w-full pl-12 pr-4 py-3 bg-white border border-slate-200 rounded-xl text-[14px] focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent placeholder:text-slate-400 shadow-sm"
              />
            </div>
            <button
              onClick={handleSearch}
              className="px-7 py-3 bg-blue-600 text-white text-[14px] font-bold rounded-xl hover:bg-blue-700 transition-colors flex items-center gap-2 whitespace-nowrap shadow-sm shadow-blue-200"
            >
              <Search className="w-4 h-4" />
              検索実行
            </button>
            <button
              onClick={handleRefresh}
              className="px-4 py-3 bg-white border border-slate-200 rounded-xl text-[13px] text-slate-600 hover:bg-slate-50 transition-colors flex items-center gap-1.5 whitespace-nowrap"
            >
              <RefreshCw
                className={cn('w-4 h-4', loading && 'animate-spin')}
              />
              データ更新
            </button>
          </div>

          {/* Results header bar */}
          <div className="flex items-center justify-between mb-3">
            <div className="flex items-center gap-3">
              {total > 0 && (
                <span className="inline-flex items-center gap-1.5 px-3.5 py-1 bg-blue-600 text-white text-[12px] font-bold rounded-full shadow-sm">
                  ヒット数 {total.toLocaleString()}件
                </span>
              )}
              {hasJan && (
                <ActiveBadge label="JANコードあり" onRemove={() => handleFilterToggle('has_jan', false)} />
              )}
              {hasCoupon && (
                <ActiveBadge label="クーポンあり" onRemove={() => handleFilterToggle('has_coupon', false)} />
              )}
              {hasVariant && (
                <ActiveBadge label="バリアントあり" onRemove={() => handleFilterToggle('has_variant', false)} />
              )}
              {updated24h && (
                <ActiveBadge label="24h以内更新" onRemove={() => handleFilterToggle('updated_24h', false)} />
              )}
              {(priceMin || priceMax) && (
                <ActiveBadge
                  label={`¥${priceMin || '0'}〜¥${priceMax || '∞'}`}
                  onRemove={() => {
                    setPriceMin('');
                    setPriceMax('');
                    setPage(1);
                    setSearchParams(buildParams({ price_min: '', price_max: '' }));
                    doSearch(query, sortBy, 1, { price_min: '', price_max: '' });
                  }}
                />
              )}
              {shippingMaxDays != null && (
                <ActiveBadge
                  label={`発送${shippingMaxDays}日以内`}
                  onRemove={() => handleShippingChange(null)}
                />
              )}
              {lastUpdate && (
                <span className="text-[12px] text-slate-400 flex items-center gap-1.5">
                  <ClockIcon className="w-3.5 h-3.5" />
                  最終同期: {lastUpdate.toLocaleTimeString('ja-JP')}
                </span>
              )}
            </div>
            <div className="flex items-center gap-2">
              <span className="text-[12px] text-slate-500">並び替え:</span>
              <select
                value={sortBy}
                onChange={(e) => handleSortChange(e.target.value)}
                className="text-[13px] border border-slate-200 rounded-lg px-3 py-1.5 focus:outline-none focus:ring-2 focus:ring-blue-500 bg-white shadow-sm cursor-pointer"
              >
                {SORT_OPTIONS.map((opt) => (
                  <option key={opt.value} value={opt.value}>
                    {opt.label}
                  </option>
                ))}
              </select>
            </div>
          </div>

          {/* Loading */}
          {loading && (
            <div className="flex items-center justify-center py-24">
              <div className="text-center">
                <Loader2 className="w-8 h-8 text-blue-500 animate-spin mx-auto mb-3" />
                <p className="text-sm text-slate-500">検索中...</p>
              </div>
            </div>
          )}

          {/* Error */}
          {error && (
            <div className="bg-red-50 border border-red-200 rounded-xl p-4 flex items-center gap-3">
              <AlertCircle className="w-5 h-5 text-red-500 shrink-0" />
              <p className="text-sm text-red-700">{error}</p>
            </div>
          )}

          {/* Empty */}
          {!loading && !error && results.length === 0 && (
            <div className="text-center py-24">
              <Package className="w-14 h-14 text-slate-300 mx-auto mb-4" />
              <p className="text-slate-500 text-[15px] font-medium mb-1">
                検索結果がありません
              </p>
              <p className="text-slate-400 text-sm">
                別のキーワードで検索してください
              </p>
            </div>
          )}

          {/* ── Results Table ───────────────────── */}
          {!loading && !error && results.length > 0 && (
            <>
              <div className="bg-white rounded-xl border border-slate-200 shadow-sm overflow-hidden">
                <div className="overflow-x-auto">
                  <table className="product-table">
                    <thead>
                      <tr>
                        <th className="w-[80px] text-center">画像</th>
                        <th>商品情報</th>
                        <th className="w-[130px]">JAN</th>
                        <th className="w-[190px]">
                          <span className="inline-flex items-center gap-1">
                            <ArrowUpDown className="w-3 h-3" />
                            価格・ポイント内訳
                          </span>
                        </th>
                        <th className="w-[120px]">ポイント/クーポン</th>
                        <th className="w-[110px]">発送</th>
                        <th className="w-[100px]">最終更新</th>
                        <th className="w-[100px]">操作</th>
                      </tr>
                    </thead>
                    <tbody>
                      {results.map((item) => (
                        <ProductCard
                          key={`${item.product_uid}-${item.variant_code}`}
                          item={item}
                          multiplier={multiplier}
                          isWatching={isWatching(item.product_uid)}
                          onToggleWatch={() => toggleWatch(item)}
                        />
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>

              <Pagination
                page={page}
                totalPages={totalPages}
                onPageChange={handlePageChange}
              />

              <p className="text-center text-[12px] text-slate-400 mt-3 mb-2">
                表示中: {(page - 1) * PER_PAGE + 1}-
                {Math.min(page * PER_PAGE, total)}件 / 全
                {total.toLocaleString()}件の検索結果
              </p>
            </>
          )}
        </div>

      </div>
    </div>
  );
}

/* ── Sub-components ──────────────────────────────── */

function FilterSection({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(true);
  return (
    <div className="py-3 border-b border-slate-100 last:border-0">
      <button
        onClick={() => setOpen(!open)}
        className="flex items-center justify-between w-full text-[12px] font-bold text-slate-500 uppercase tracking-wider mb-2"
      >
        {title}
        {open ? (
          <ChevronUp className="w-3.5 h-3.5" />
        ) : (
          <ChevronDown className="w-3.5 h-3.5" />
        )}
      </button>
      {open && <div className="space-y-2">{children}</div>}
    </div>
  );
}

function ActiveBadge({ label, onRemove }: { label: string; onRemove: () => void }) {
  return (
    <span className="inline-flex items-center gap-1.5 px-3 py-1 bg-emerald-50 border border-emerald-200 text-emerald-700 text-[12px] font-bold rounded-full">
      <span className="w-1.5 h-1.5 rounded-full bg-emerald-500" />
      {label}
      <button
        onClick={onRemove}
        className="ml-0.5 text-emerald-400 hover:text-emerald-600"
      >
        ✕
      </button>
    </span>
  );
}

function ClockIcon({ className }: { className?: string }) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
    >
      <circle cx="12" cy="12" r="10" />
      <polyline points="12 6 12 12 16 14" />
    </svg>
  );
}

