import { Link } from 'react-router-dom';
import {
  Star,
  Truck,
  Tag,
  Clock,
  ChevronRight,
  ExternalLink,
} from 'lucide-react';
import type { ProductSearchResult } from '../lib/types';
import {
  formatYen,
  truncate,
  relativeTime,
  shippingText,
  calcEffectivePrice,
  cn,
} from '../lib/utils';

interface Props {
  item: ProductSearchResult;
  multiplier: number;
  isWatching: boolean;
  onToggleWatch: () => void;
}

export default function ProductCard({
  item,
  multiplier,
  isWatching,
  onToggleWatch,
}: Props) {
  const price = item.price ?? 0;
  const { effectivePrice, totalPoints, pointBackAmount, effectiveCoupon } =
    calcEffectivePrice(
      price,
      multiplier,
      item.point_rate,
      item.coupon_yen,
      item.coupon_percent,
      item.point_back_percent,
    );

  const hasCoupon =
    (item.coupon_yen ?? 0) > 0 || (item.coupon_percent ?? 0) > 0;
  const shipping = item.shipping_text_raw;
  const isFreeShipping =
    shipping?.includes('送料無料') || shipping?.includes('送料込');
  const hasPointRate = (item.point_rate ?? 0) > 0;
  const hasPointBack = (item.point_back_percent ?? 0) > 0;

  return (
    <tr className="group border-b border-slate-100 hover:bg-blue-50/30 transition-colors">
      {/* ── 画像 ────────── */}
      <td className="py-3 px-3 w-[80px]">
        <Link
          to={`/product/${item.product_uid}`}
          className="block w-[68px] h-[68px] rounded-lg overflow-hidden bg-slate-100 border border-slate-200 hover:border-blue-300 transition-colors"
        >
          {item.image_url ? (
            <img
              src={item.image_url}
              alt={item.item_name ?? ''}
              className="w-full h-full object-cover group-hover:scale-105 transition-transform duration-200"
              loading="lazy"
            />
          ) : (
            <div className="w-full h-full flex items-center justify-center text-slate-300">
              <Tag className="w-6 h-6" />
            </div>
          )}
        </Link>
      </td>

      {/* ── 商品情報 (商品名 + 店舗名 + 店舗コード + 購入URL) ────── */}
      <td className="py-3 px-3 min-w-[200px] max-w-[320px]">
        <div className="flex items-center gap-1.5 mb-1">
          <span className="badge badge-rakuten">楽天</span>
          {item.variant_name && (
            <span className="inline-flex items-center px-1.5 py-0.5 rounded bg-slate-100 text-[11px] font-medium text-slate-500 border border-slate-200">
              {truncate(item.variant_name, 12)}
            </span>
          )}
        </div>
        {/* 商品名 */}
        <Link
          to={`/product/${item.product_uid}`}
          className="text-[13px] font-semibold text-slate-800 hover:text-blue-600 transition-colors line-clamp-2 leading-snug"
        >
          {item.item_name ?? '(名称なし)'}
        </Link>
        {/* 店舗名 + 店舗コード */}
        <div className="text-[11px] text-slate-500 mt-1 leading-snug">
          {item.shop_url ? (
            <a
              href={item.shop_url}
              target="_blank"
              rel="noopener noreferrer"
              className="font-medium text-blue-600 hover:text-blue-800 hover:underline transition-colors"
            >
              {item.shop_name ?? item.shop_code}
              <ExternalLink className="w-2.5 h-2.5 inline-block ml-0.5 -mt-0.5" />
            </a>
          ) : (
            <span className="font-medium text-slate-600">
              {item.shop_name ?? item.shop_code}
            </span>
          )}
          {item.shop_name && (
            <span className="text-slate-400 ml-1">({item.shop_code})</span>
          )}
        </div>
        {/* 購入サイトURL */}
        <a
          href={item.canonical_url}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex items-center gap-0.5 text-[11px] text-blue-500 hover:text-blue-600 mt-0.5 transition-colors"
          onClick={(e) => e.stopPropagation()}
        >
          <ExternalLink className="w-3 h-3" />
          楽天で見る
        </a>
      </td>

      {/* ── JANコード ───── */}
      <td className="py-3 px-3 w-[130px]">
        {item.jan_code ? (
          <div className="flex flex-col gap-1">
            <span className="inline-flex items-center gap-1 px-1.5 py-0.5 bg-emerald-50 border border-emerald-200 rounded text-[10px] font-bold text-emerald-600 w-fit">
              JAN
            </span>
            <Link
              to={`/jan/${item.jan_code}`}
              className="font-mono text-[12px] text-slate-700 hover:text-blue-600 transition-colors font-medium"
              title="同一JAN商品を比較"
            >
              {item.jan_code}
            </Link>
          </div>
        ) : (
          <span className="text-[12px] text-slate-300 italic">未取得</span>
        )}
      </td>

      {/* ── 価格・ポイント・クーポン内訳 ── */}
      <td className="py-3 px-3 w-[190px]">
        <div className="space-y-0.5 text-[12px]">
          {/* 定価 */}
          <div className="flex justify-between items-center">
            <span className="text-slate-400">定価:</span>
            <span className="font-medium text-slate-600">
              {formatYen(price)}
            </span>
          </div>
          {/* ショップポイント (+X倍) */}
          {totalPoints > 0 && (
            <div className="flex justify-between items-center text-orange-500">
              <span className="flex items-center gap-0.5">
                ポイント
                {hasPointRate && (
                  <span className="text-[10px] font-bold">
                    (+{item.point_rate}倍)
                  </span>
                )}
                :
              </span>
              <span className="font-medium">-{formatYen(totalPoints)}</span>
            </div>
          )}
          {/* ポイントバック (+X%バック) */}
          {pointBackAmount > 0 && (
            <div className="flex justify-between items-center text-pink-500">
              <span className="flex items-center gap-0.5">
                Pバック
                <span className="text-[10px] font-bold">
                  (+{item.point_back_percent}%)
                </span>
                :
              </span>
              <span className="font-medium">-{formatYen(pointBackAmount)}</span>
            </div>
          )}
          {/* クーポン */}
          {effectiveCoupon > 0 && (
            <div className="flex justify-between items-center text-emerald-600">
              <span>クーポン:</span>
              <span className="font-medium">-{formatYen(effectiveCoupon)}</span>
            </div>
          )}
        </div>
        {/* 有効価格 */}
        <div className="mt-1.5 pt-1.5 border-t border-slate-100">
          <div className="flex justify-between items-baseline">
            <span className="text-[11px] text-slate-400">有効価格:</span>
            <span className="text-[17px] font-bold text-blue-600 leading-none">
              {formatYen(effectivePrice)}
            </span>
          </div>
        </div>
      </td>

      {/* ── ポイント・クーポンバッジ ── */}
      <td className="py-3 px-3 w-[120px]">
        <div className="flex flex-col gap-1">
          {hasPointRate && (
            <span className="badge badge-rakuten text-[10px]">
              ポイント+{item.point_rate}倍
            </span>
          )}
          {hasPointBack && (
            <span className="inline-flex items-center px-1.5 py-0.5 rounded bg-pink-50 text-pink-600 border border-pink-200 text-[10px] font-bold whitespace-nowrap">
              +{item.point_back_percent}%バック
            </span>
          )}
          {hasCoupon && (
            <span className="badge badge-coupon text-[10px]">
              <Tag className="w-3 h-3" />
              {(item.coupon_yen ?? 0) > 0
                ? `${item.coupon_yen}円OFF`
                : `${item.coupon_percent}%OFF`}
            </span>
          )}
        </div>
      </td>

      {/* ── 発送日数 ── */}
      <td className="py-3 px-3 w-[110px]">
        <div className="flex flex-col gap-1">
          {isFreeShipping ? (
            <span className="badge badge-free-ship text-[10px]">
              <Truck className="w-3 h-3" />
              送料無料
            </span>
          ) : shipping ? (
            <span className="text-[11px] text-slate-500 leading-snug">
              {truncate(shipping, 20)}
            </span>
          ) : (
            <span className="text-[11px] text-slate-300">---</span>
          )}
          {/* 発送日数 (structured) */}
          {(item.shipping_days_min != null || item.shipping_days_max != null) && (
            <span className="text-[11px] text-slate-500 font-medium">
              📦 {shippingText(item.shipping_days_min, item.shipping_days_max)}
            </span>
          )}
        </div>
      </td>

      {/* ── 最終更新 ────── */}
      <td className="py-3 px-3 w-[100px]">
        <div className="flex items-center gap-1 text-[12px] text-slate-500">
          <Clock className="w-3.5 h-3.5 text-slate-400 shrink-0" />
          <span>{relativeTime(item.fetched_at)}</span>
        </div>
      </td>

      {/* ── 操作 ─────────── */}
      <td className="py-3 px-3 w-[100px]">
        <div className="flex items-center gap-1.5">
          <button
            onClick={(e) => {
              e.stopPropagation();
              onToggleWatch();
            }}
            className={cn(
              'p-1.5 rounded-md transition-colors',
              isWatching
                ? 'text-amber-500 bg-amber-50 hover:bg-amber-100'
                : 'text-slate-300 hover:text-amber-400 hover:bg-slate-50'
            )}
            title={isWatching ? 'ウォッチ解除' : 'ウォッチに追加'}
          >
            <Star
              className={cn('w-4 h-4', isWatching && 'fill-amber-400')}
            />
          </button>
          <Link
            to={`/product/${item.product_uid}`}
            className="inline-flex items-center gap-0.5 px-3 py-1.5 bg-blue-600 text-white text-[12px] font-semibold rounded-md hover:bg-blue-700 transition-colors shadow-sm shadow-blue-200"
          >
            詳細
            <ChevronRight className="w-3.5 h-3.5" />
          </Link>
        </div>
      </td>
    </tr>
  );
}
