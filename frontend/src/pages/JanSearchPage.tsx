import { useState, useEffect, useMemo } from 'react';
import { useParams, Link } from 'react-router-dom';
import {
  Barcode,
  ChevronRight,
  Loader2,
  AlertCircle,
  Store,
  ExternalLink,
  ArrowDown,
  Truck,
  Tag,
  ShoppingBag,
} from 'lucide-react';
import { getJanProducts } from '../api/client';
import type { ProductSearchResult } from '../lib/types';
import { formatYen, truncate, calcEffectivePrice, relativeTime, cn } from '../lib/utils';

interface Props {
  multiplier: number;
}

export default function JanSearchPage({ multiplier }: Props) {
  const { janCode } = useParams<{ janCode: string }>();
  const [variants, setVariants] = useState<ProductSearchResult[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!janCode) return;
    setLoading(true);
    setError(null);
    getJanProducts(janCode, multiplier)
      .then((res) => setVariants(res.variants))
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }, [janCode, multiplier]);

  // Sort by effective price
  const sorted = useMemo(() => {
    return [...variants].sort((a, b) => {
      const aEff = calcEffectivePrice(
        a.price ?? 0,
        multiplier,
        a.point_rate,
        a.coupon_yen,
        a.coupon_percent
      ).effectivePrice;
      const bEff = calcEffectivePrice(
        b.price ?? 0,
        multiplier,
        b.point_rate,
        b.coupon_yen,
        b.coupon_percent
      ).effectivePrice;
      return aEff - bEff;
    });
  }, [variants, multiplier]);

  const cheapest = sorted.length > 0 ? sorted[0] : null;

  if (loading) {
    return (
      <div className="flex items-center justify-center py-32">
        <Loader2 className="w-10 h-10 text-blue-500 animate-spin" />
      </div>
    );
  }

  return (
    <div className="max-w-[1000px] mx-auto px-4 py-6">
      {/* Breadcrumb */}
      <nav className="flex items-center gap-1.5 text-xs text-slate-400 mb-4">
        <Link to="/" className="hover:text-blue-600">ホーム</Link>
        <ChevronRight className="w-3 h-3" />
        <span className="text-slate-600">同一JAN最安値比較</span>
      </nav>

      {/* Header */}
      <div className="bg-white rounded-xl border border-slate-200 p-6 mb-6">
        <div className="flex items-center gap-3 mb-2">
          <div className="w-10 h-10 rounded-xl bg-blue-100 flex items-center justify-center">
            <Barcode className="w-5 h-5 text-blue-600" />
          </div>
          <div>
            <h1 className="text-lg font-bold text-slate-800">
              JAN: {janCode}
            </h1>
            <p className="text-sm text-slate-500">
              同一JANコードの商品を有効価格の安い順に表示
            </p>
          </div>
        </div>
        {cheapest && (
          <div className="mt-3 p-3 bg-emerald-50 border border-emerald-200 rounded-lg flex items-center gap-2">
            <ArrowDown className="w-4 h-4 text-emerald-600" />
            <span className="text-sm text-emerald-700">
              最安値:{' '}
              <span className="font-bold">
                {formatYen(
                  calcEffectivePrice(
                    cheapest.price ?? 0,
                    multiplier,
                    cheapest.point_rate,
                    cheapest.coupon_yen,
                    cheapest.coupon_percent
                  ).effectivePrice
                )}
              </span>
              {' '}({cheapest.shop_code})
            </span>
          </div>
        )}
      </div>

      {error && (
        <div className="bg-red-50 border border-red-200 rounded-xl p-4 flex items-center gap-3 mb-6">
          <AlertCircle className="w-5 h-5 text-red-500" />
          <p className="text-sm text-red-700">{error}</p>
        </div>
      )}

      {sorted.length === 0 && !error && (
        <div className="text-center py-20 text-slate-400">
          <ShoppingBag className="w-12 h-12 mx-auto mb-3 text-slate-300" />
          <p>該当する商品が見つかりません</p>
        </div>
      )}

      {/* Results */}
      <div className="space-y-2">
        {sorted.map((item, idx) => {
          const { effectivePrice, totalPoints, effectiveCoupon } =
            calcEffectivePrice(
              item.price ?? 0,
              multiplier,
              item.point_rate,
              item.coupon_yen,
              item.coupon_percent
            );
          const isCheapest = idx === 0;
          const shipping = item.shipping_text_raw;
          const isFree = shipping?.includes('送料無料') || shipping?.includes('送料込');

          return (
            <div
              key={`${item.product_uid}-${item.variant_code}`}
              className={cn(
                'bg-white rounded-xl border p-4 flex items-center gap-4 hover:shadow-md transition-all',
                isCheapest
                  ? 'border-blue-300 ring-1 ring-blue-100'
                  : 'border-slate-200'
              )}
            >
              {/* Rank */}
              <div className="shrink-0 w-8 text-center">
                {isCheapest ? (
                  <span className="inline-flex items-center justify-center w-7 h-7 rounded-full bg-blue-600 text-white text-xs font-bold">
                    1
                  </span>
                ) : (
                  <span className="text-sm font-bold text-slate-400">{idx + 1}</span>
                )}
              </div>

              {/* Image */}
              <div className="shrink-0 w-14 h-14 rounded-lg overflow-hidden bg-slate-100 border border-slate-200">
                {item.image_url ? (
                  <img
                    src={item.image_url}
                    alt={item.item_name ?? ''}
                    className="w-full h-full object-cover"
                    loading="lazy"
                  />
                ) : (
                  <div className="w-full h-full flex items-center justify-center text-slate-300">
                    <Tag className="w-6 h-6" />
                  </div>
                )}
              </div>

              {/* Info */}
              <div className="flex-1 min-w-0">
                <Link
                  to={`/product/${item.product_uid}`}
                  className="text-sm font-medium text-slate-800 hover:text-blue-600 line-clamp-1"
                >
                  {item.item_name ?? '(名称なし)'}
                </Link>
                <div className="flex items-center gap-2 mt-1 text-[11px] text-slate-400">
                  <Store className="w-3 h-3" />
                  <span>{item.shop_code}</span>
                  {item.variant_name && <span>/ {truncate(item.variant_name, 15)}</span>}
                </div>
              </div>

              {/* Price */}
              <div className="shrink-0 text-right w-[130px]">
                <div className="text-[11px] text-slate-400 space-y-0.5">
                  <div>定価: {formatYen(item.price)}</div>
                  {totalPoints > 0 && (
                    <div className="text-orange-500">-{totalPoints.toLocaleString()}pt</div>
                  )}
                  {effectiveCoupon > 0 && (
                    <div className="text-emerald-500">-{formatYen(effectiveCoupon)}</div>
                  )}
                </div>
                <div className={cn('text-lg font-bold', isCheapest ? 'text-blue-600' : 'text-slate-700')}>
                  {formatYen(effectivePrice)}
                </div>
              </div>

              {/* Badges */}
              <div className="shrink-0 w-[90px] flex flex-col items-end gap-1">
                {isFree && (
                  <span className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded-full text-[10px] font-bold bg-emerald-50 text-emerald-600 border border-emerald-200">
                    <Truck className="w-3 h-3" />
                    送料無料
                  </span>
                )}
                <span className="text-[10px] text-slate-400">{relativeTime(item.fetched_at)}</span>
              </div>

              {/* Action */}
              <Link
                to={`/product/${item.product_uid}`}
                className="shrink-0 px-3 py-1.5 bg-blue-600 text-white text-xs font-medium rounded-lg hover:bg-blue-700 flex items-center gap-1"
              >
                詳細
                <ExternalLink className="w-3 h-3" />
              </Link>
            </div>
          );
        })}
      </div>
    </div>
  );
}
