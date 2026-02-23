import { useState, useEffect, useMemo } from 'react';
import { useParams, Link } from 'react-router-dom';
import {
  ExternalLink,
  ChevronRight,
  Copy,
  Check,
  Download,
  Loader2,
  AlertCircle,
  Bookmark,
  BookmarkCheck,
  Clock,
  ShoppingBag,
  Package,
  Coins,
  Tag,
  Ticket,
  Zap,
  Truck,
} from 'lucide-react';
import {
  getProductByUid,
  getVariantHistory,
  calculatePoints,
} from '../api/client';
import PriceChart from '../components/PriceChart';
import type {
  Product,
  HistoryRecord,
  PointCalcResponse,
  WatchlistItem,
} from '../lib/types';
import { formatYen, formatDate, shippingText, calcEffectivePrice, cn } from '../lib/utils';

interface Props {
  multiplier: number;
  isWatching: (uid: string) => boolean;
  addWatch: (item: WatchlistItem) => void;
  removeWatch: (uid: string) => void;
}

export default function ProductDetailPage({
  multiplier,
  isWatching,
  addWatch,
  removeWatch,
}: Props) {
  const { '*': wildcard } = useParams();
  const productUid = wildcard ?? '';

  const [product, setProduct] = useState<Product | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [selectedVariantId, setSelectedVariantId] = useState<number | null>(
    null
  );
  const [history, setHistory] = useState<HistoryRecord[]>([]);
  const [historyDays, setHistoryDays] = useState(30);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [calcResult, setCalcResult] = useState<PointCalcResponse | null>(null);

  const [showChangesOnly, setShowChangesOnly] = useState(false);
  const [copied, setCopied] = useState(false);

  // Load product
  useEffect(() => {
    if (!productUid) return;
    setLoading(true);
    setError(null);
    getProductByUid(productUid)
      .then((p) => {
        setProduct(p);
        if (p.variants.length > 0) {
          setSelectedVariantId(p.variants[0].variant_id);
        }
      })
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }, [productUid]);

  // Load history
  useEffect(() => {
    if (!selectedVariantId) return;
    setHistoryLoading(true);
    getVariantHistory(selectedVariantId, historyDays)
      .then((res) => setHistory(res.history))
      .catch(() => setHistory([]))
      .finally(() => setHistoryLoading(false));
  }, [selectedVariantId, historyDays]);

  // Calculate points
  useEffect(() => {
    if (!selectedVariantId) return;
    calculatePoints(selectedVariantId, multiplier)
      .then(setCalcResult)
      .catch(() => setCalcResult(null));
  }, [selectedVariantId, multiplier]);

  const selectedVariant = useMemo(
    () =>
      product?.variants.find((v) => v.variant_id === selectedVariantId) ?? null,
    [product, selectedVariantId]
  );

  const snapshot = selectedVariant?.snapshot ?? null;
  const price = snapshot?.price ?? 0;
  const calc = calcResult?.output ?? null;

  const localCalc = calcEffectivePrice(
    price,
    multiplier,
    snapshot?.point_rate ?? null,
    snapshot?.coupon_yen ?? null,
    snapshot?.coupon_percent ?? null,
    snapshot?.point_back_percent ?? null,
  );

  const effectivePrice = calc?.price_minus_points_coupon ?? localCalc.effectivePrice;
  const totalPoints = calc?.total_points ?? localCalc.totalPoints;
  const pointBackAmount = localCalc.pointBackAmount;
  const effectiveCoupon = calc?.effective_coupon ?? localCalc.effectiveCoupon;

  const janCode =
    selectedVariant?.jan_code ??
    product?.variants.find((v) => v.jan_code)?.jan_code;

  const watching = isWatching(productUid);

  const handleCopyUid = () => {
    navigator.clipboard.writeText(productUid);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const handleToggleWatch = () => {
    if (watching) {
      removeWatch(productUid);
    } else {
      addWatch({
        product_uid: productUid,
        item_name: product?.item_name ?? '',
        image_url: snapshot?.image_url ?? null,
        added_at: new Date().toISOString(),
      });
    }
  };

  // History for table
  const historyForTable = useMemo(() => {
    if (!showChangesOnly) return [...history].reverse();
    const reversed = [...history].reverse();
    return reversed.filter((r, i) => {
      if (i === 0) return true;
      const prev = reversed[i - 1];
      return (
        r.price !== prev.price ||
        r.coupon_yen !== prev.coupon_yen ||
        r.coupon_percent !== prev.coupon_percent ||
        r.point_rate !== prev.point_rate
      );
    });
  }, [history, showChangesOnly]);

  // CSV export
  const handleExportCsv = () => {
    const header =
      '取得日時,価格,ポイント(pt),価格-ポイント,クーポン,価格-ポイント-クーポン\n';
    const rows = historyForTable
      .map((r) => {
        const p = r.price ?? 0;
        const pts = Math.floor(
          (p * (multiplier + (r.point_rate ?? 0))) / 100
        );
        const cpFromPct = Math.floor((p * (r.coupon_percent ?? 0)) / 100);
        const cp = Math.max(r.coupon_yen ?? 0, cpFromPct);
        const pMinusPts = p - pts;
        const eff = p - pts - cp;
        return `${r.record_date},${p},${pts},${pMinusPts},${cp},${eff}`;
      })
      .join('\n');

    const blob = new Blob(['\uFEFF' + header + rows], {
      type: 'text/csv;charset=utf-8',
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `pricetrace_${productUid.replace(':', '_')}_history.csv`;
    a.click();
    URL.revokeObjectURL(url);
  };

  if (loading) {
    return (
      <div className="flex flex-col items-center justify-center py-32">
        <Loader2 className="w-10 h-10 text-blue-500 animate-spin mb-3" />
        <p className="text-sm text-slate-500">読み込み中...</p>
      </div>
    );
  }

  if (error || !product) {
    return (
      <div className="max-w-3xl mx-auto mt-12 px-4">
        <div className="bg-red-50 border border-red-200 rounded-xl p-6 flex items-center gap-3">
          <AlertCircle className="w-6 h-6 text-red-500 shrink-0" />
          <p className="text-red-700">{error ?? 'Product not found'}</p>
        </div>
      </div>
    );
  }

  return (
    <div className="max-w-[1200px] mx-auto px-5 py-6">
      {/* Breadcrumb */}
      <nav className="flex items-center gap-1.5 text-[12px] text-slate-400 mb-5">
        <Link to="/" className="hover:text-blue-600 transition-colors">
          ホーム
        </Link>
        <ChevronRight className="w-3 h-3" />
        <Link to="/" className="hover:text-blue-600 transition-colors">
          検索結果
        </Link>
        <ChevronRight className="w-3 h-3" />
        <span className="text-slate-600 font-medium">商品詳細</span>
      </nav>

      {/* ══════ Product Header Card ═══════════════════ */}
      <div className="bg-white rounded-xl border border-slate-200 shadow-sm p-6 mb-6">
        <div className="flex gap-6">
          {/* Image */}
          <div className="shrink-0 w-[160px] h-[160px] rounded-xl overflow-hidden bg-slate-50 border border-slate-200">
            {snapshot?.image_url ? (
              <img
                src={snapshot.image_url}
                alt={product.item_name ?? ''}
                className="w-full h-full object-cover"
              />
            ) : (
              <div className="w-full h-full flex items-center justify-center text-slate-300">
                <ShoppingBag className="w-16 h-16" />
              </div>
            )}
          </div>

          {/* Details */}
          <div className="flex-1 min-w-0">
            {/* ── Badges row ── */}
            <div className="flex items-center flex-wrap gap-2 mb-2.5">
              <span className="badge badge-rakuten text-[12px]">楽天市場</span>
              {(snapshot?.point_rate ?? 0) > 0 && (
                <span className="badge badge-rakuten text-[11px]">
                  ポイント+{snapshot!.point_rate}倍
                </span>
              )}
              {(snapshot?.point_back_percent ?? 0) > 0 && (
                <span className="inline-flex items-center px-2 py-0.5 rounded bg-pink-50 text-pink-600 border border-pink-200 text-[11px] font-bold whitespace-nowrap">
                  +{snapshot!.point_back_percent}%ポイントバック
                </span>
              )}
              {((snapshot?.coupon_yen ?? 0) > 0 || (snapshot?.coupon_percent ?? 0) > 0) && (
                <span className="badge badge-coupon text-[11px]">
                  <Tag className="w-3 h-3" />
                  {(snapshot?.coupon_yen ?? 0) > 0
                    ? `${snapshot!.coupon_yen}円OFF`
                    : `${snapshot!.coupon_percent}%OFF`}
                </span>
              )}
              {(snapshot?.shipping_text_raw?.includes('送料無料') ||
                snapshot?.shipping_text_raw?.includes('送料込')) && (
                <span className="badge badge-free-ship text-[11px]">
                  <Truck className="w-3 h-3" />
                  送料無料
                </span>
              )}
            </div>

            {/* ── 商品名 ── */}
            <h1 className="text-[18px] font-bold text-slate-800 leading-snug mb-3">
              {product.item_name ?? '(名称なし)'}
            </h1>

            {/* ── Meta: 店舗名 / 店舗コード / JAN / 更新日時 ── */}
            <div className="flex items-center flex-wrap gap-x-5 gap-y-1 text-[12px] text-slate-400 mb-4">
              <span className="flex items-center gap-1.5">
                <ShoppingBag className="w-3.5 h-3.5" />
                店舗名:{' '}
                {product.shop_url ? (
                  <a
                    href={product.shop_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-blue-600 hover:text-blue-800 hover:underline font-semibold transition-colors"
                  >
                    {product.shop_name ?? product.shop_code}
                    <ExternalLink className="w-3 h-3 inline-block ml-0.5 -mt-0.5" />
                  </a>
                ) : (
                  <strong className="text-slate-600">{product.shop_name ?? '---'}</strong>
                )}
              </span>
              <span className="flex items-center gap-1.5">
                店舗コード: <strong className="text-slate-600">{product.shop_code}</strong>
              </span>
              {janCode && (
                <Link
                  to={`/jan/${janCode}`}
                  className="flex items-center gap-1.5 hover:text-blue-500 transition-colors"
                >
                  JAN: <strong className="text-slate-600">{janCode}</strong>
                </Link>
              )}
              <span className="flex items-center gap-1.5">
                <Clock className="w-3.5 h-3.5" />
                最終同期: {formatDate(snapshot?.fetched_at)}
              </span>
            </div>

            <div className="flex items-center gap-2.5">
              <a
                href={product.canonical_url}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1.5 px-5 py-2.5 bg-blue-600 text-white text-[13px] font-semibold rounded-lg hover:bg-blue-700 transition-colors shadow-sm shadow-blue-200"
              >
                <ExternalLink className="w-4 h-4" />
                元ページを開く
              </a>

              {product.variants.length > 1 && (
                <select
                  value={selectedVariantId ?? ''}
                  onChange={(e) =>
                    setSelectedVariantId(Number(e.target.value))
                  }
                  className="px-4 py-2.5 bg-white border border-slate-200 rounded-lg text-[13px] focus:outline-none focus:ring-2 focus:ring-blue-500 cursor-pointer"
                >
                  {product.variants.map((v) => (
                    <option key={v.variant_id} value={v.variant_id}>
                      {v.variant_name ?? v.variant_code}
                    </option>
                  ))}
                </select>
              )}

              <button
                onClick={handleToggleWatch}
                className={cn(
                  'inline-flex items-center gap-1.5 px-4 py-2.5 rounded-lg text-[13px] font-medium transition-colors border',
                  watching
                    ? 'bg-amber-50 border-amber-200 text-amber-600'
                    : 'bg-white border-slate-200 text-slate-600 hover:bg-slate-50'
                )}
              >
                {watching ? (
                  <BookmarkCheck className="w-4 h-4" />
                ) : (
                  <Bookmark className="w-4 h-4" />
                )}
                {watching ? 'ウォッチ中' : 'ウォッチ'}
              </button>

              <button
                onClick={handleCopyUid}
                className="inline-flex items-center gap-1.5 px-3 py-2.5 bg-white border border-slate-200 rounded-lg text-[13px] text-slate-500 hover:bg-slate-50 transition-colors"
                title="Product UID をコピー"
              >
                {copied ? (
                  <Check className="w-4 h-4 text-emerald-500" />
                ) : (
                  <Copy className="w-4 h-4" />
                )}
              </button>
            </div>
          </div>
        </div>
      </div>

      {/* ══════ Key Metrics ═══════════════════════════ */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-6">
        {/* 現在価格 */}
        <MetricCard
          label="現在価格"
          value={formatYen(price)}
          unit="円"
          sub="前日比: ±0円"
          icon={<Package className="w-5 h-5" />}
        />
        {/* ショップポイント (+X倍) */}
        <MetricCard
          label="ショップポイント"
          value={totalPoints.toLocaleString()}
          unit="pt"
          sub={
            (snapshot?.point_rate ?? 0) > 0
              ? `+${snapshot!.point_rate}倍 × ${multiplier}倍`
              : `倍率 ${multiplier}倍で計算`
          }
          icon={<Coins className="w-5 h-5" />}
          highlight="orange"
        />
        {/* ポイントバック (+X%バック) */}
        <MetricCard
          label="ポイントバック"
          value={pointBackAmount > 0 ? pointBackAmount.toLocaleString() : '0'}
          unit="pt"
          sub={
            (snapshot?.point_back_percent ?? 0) > 0
              ? `+${snapshot!.point_back_percent}%ポイントバック`
              : 'バックなし'
          }
          icon={<Coins className="w-5 h-5" />}
          highlight={(snapshot?.point_back_percent ?? 0) > 0 ? 'orange' : undefined}
        />
        {/* クーポン割引 */}
        <MetricCard
          label="クーポン割引"
          value={effectiveCoupon.toLocaleString()}
          unit="円"
          sub={
            effectiveCoupon > 0
              ? (snapshot?.coupon_percent ?? 0) > 0
                ? `${snapshot!.coupon_percent}%の割引率`
                : '円額クーポン'
              : 'クーポンなし'
          }
          icon={<Ticket className="w-5 h-5" />}
          highlight="emerald"
        />
      </div>
      <div className="grid grid-cols-2 md:grid-cols-3 gap-3 mb-6">
        {/* 有効価格（実質） */}
        <MetricCard
          label="有効価格（実質）"
          value={formatYen(effectivePrice)}
          unit="円"
          sub="送料別"
          icon={<Zap className="w-5 h-5" />}
          highlight="blue"
          featured
        />
        {/* 発送日数 */}
        <MetricCard
          label="発送日数"
          value={shippingText(
            snapshot?.shipping_days_min,
            snapshot?.shipping_days_max
          )}
          unit=""
          sub={snapshot?.shipping_text_raw ?? '---'}
          icon={<Truck className="w-5 h-5" />}
        />
      </div>

      {/* ══════ Price Chart (combined) ═══ */}
      <div className="mb-6">
        <PriceChart
          history={history}
          pointMultiplier={multiplier}
          loading={historyLoading}
          onDaysChange={setHistoryDays}
          selectedDays={historyDays}
        />
      </div>

      {/* ══════ Price History Table ════════════════════ */}
      <div className="bg-white rounded-xl border border-slate-200 shadow-sm p-6">
        <div className="flex items-center justify-between mb-5">
          <div>
            <h3 className="text-[15px] font-bold text-slate-700">
              価格変動履歴
            </h3>
            <p className="text-[12px] text-slate-400 mt-1">
              取得日時ごとの価格・ポイント・クーポン推移
            </p>
          </div>
          <div className="flex items-center gap-4">
            <label className="flex items-center gap-2.5 text-[12px] text-slate-500 cursor-pointer select-none">
              <div
                className={cn(
                  'w-9 h-5 rounded-full transition-colors relative cursor-pointer',
                  showChangesOnly ? 'bg-blue-600' : 'bg-slate-300'
                )}
                onClick={() => setShowChangesOnly(!showChangesOnly)}
              >
                <div
                  className={cn(
                    'absolute top-0.5 w-4 h-4 bg-white rounded-full transition-all shadow-sm',
                    showChangesOnly ? 'left-[18px]' : 'left-0.5'
                  )}
                />
              </div>
              変更点のみ表示
            </label>
            <button
              onClick={handleExportCsv}
              className="flex items-center gap-1.5 px-4 py-2 text-[12px] text-slate-600 bg-white border border-slate-200 rounded-lg hover:bg-slate-50 transition-colors font-medium"
            >
              <Download className="w-3.5 h-3.5" />
              CSVエクスポート
            </button>
          </div>
        </div>

        {historyForTable.length === 0 ? (
          <p className="text-[14px] text-slate-400 text-center py-12">
            履歴データがありません
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-[13px]">
              <thead>
                <tr className="border-b-2 border-slate-200 bg-slate-50">
                  <th className="text-left py-3 px-4 text-[12px] text-slate-500 font-semibold whitespace-nowrap">
                    &nbsp;
                  </th>
                  <th className="text-left py-3 px-4 text-[12px] text-slate-500 font-semibold whitespace-nowrap">
                    取得日時
                  </th>
                  <th className="text-right py-3 px-4 text-[12px] text-slate-500 font-semibold whitespace-nowrap">
                    価格
                  </th>
                  <th className="text-right py-3 px-4 text-[12px] text-slate-500 font-semibold whitespace-nowrap">
                    ポイント(pt)
                  </th>
                  <th className="text-right py-3 px-4 text-[12px] text-slate-500 font-semibold whitespace-nowrap">
                    価格−ポイント
                  </th>
                  <th className="text-right py-3 px-4 text-[12px] text-slate-500 font-semibold whitespace-nowrap">
                    クーポン
                  </th>
                  <th className="text-right py-3 px-4 text-[12px] text-slate-600 font-bold whitespace-nowrap">
                    価格−ポイント−クーポン
                  </th>
                </tr>
              </thead>
              <tbody>
                {historyForTable.map((r, i) => {
                  const p = r.price ?? 0;
                  const pts = Math.floor(
                    (p * (multiplier + (r.point_rate ?? 0))) / 100
                  );
                  const cpFromPct = Math.floor(
                    (p * (r.coupon_percent ?? 0)) / 100
                  );
                  const cp = Math.max(r.coupon_yen ?? 0, cpFromPct);
                  const pMinusPts = p - pts;
                  const eff = p - pts - cp;

                  const isFirst = i === 0;

                  return (
                    <tr
                      key={r.record_date}
                      className={cn(
                        'border-b border-slate-100 hover:bg-slate-50/70 transition-colors',
                        isFirst && 'bg-blue-50/40'
                      )}
                    >
                      <td className="py-3 px-4 text-[11px] text-slate-400 whitespace-nowrap font-medium">
                        {isFirst ? (
                          <span className="inline-flex items-center px-2 py-0.5 bg-blue-100 text-blue-700 rounded text-[10px] font-bold">
                            現在
                          </span>
                        ) : (
                          <span className="text-slate-300">履歴</span>
                        )}
                      </td>
                      <td className="py-3 px-4 text-slate-600 whitespace-nowrap">
                        {r.record_date}
                      </td>
                      <td className="py-3 px-4 text-right text-slate-700 font-medium tabular-nums">
                        {p.toLocaleString()}
                      </td>
                      <td className="py-3 px-4 text-right text-orange-500 font-medium tabular-nums">
                        {pts.toLocaleString()}
                      </td>
                      <td className="py-3 px-4 text-right text-slate-600 tabular-nums">
                        {pMinusPts.toLocaleString()}
                      </td>
                      <td className="py-3 px-4 text-right text-emerald-600 font-medium tabular-nums">
                        {cp > 0 ? cp.toLocaleString() : '0'}
                      </td>
                      <td className="py-3 px-4 text-right font-bold text-blue-600 tabular-nums">
                        {eff.toLocaleString()}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}

        {historyForTable.length >= 10 && (
          <button className="w-full mt-4 py-2.5 text-[13px] text-blue-500 hover:text-blue-600 font-medium transition-colors hover:bg-blue-50 rounded-lg">
            さらに読み込む
          </button>
        )}
      </div>
    </div>
  );
}

/* ══════ MetricCard ════════════════════════════════════ */

function MetricCard({
  label,
  value,
  unit,
  sub,
  highlight,
  featured,
  bordered,
  icon,
}: {
  label: string;
  value: string;
  unit?: string;
  sub?: string;
  highlight?: 'blue' | 'orange' | 'emerald' | 'sky';
  featured?: boolean;
  bordered?: boolean;
  icon?: React.ReactNode;
}) {
  const colorMap = {
    blue: 'text-blue-600',
    orange: 'text-orange-500',
    emerald: 'text-emerald-600',
    sky: 'text-sky-600',
  };

  return (
    <div
      className={cn(
        'bg-white rounded-xl border p-4 flex flex-col transition-all',
        featured
          ? 'border-blue-200 bg-gradient-to-br from-blue-50/50 to-white shadow-sm'
          : bordered
          ? 'border-blue-200 border-dashed'
          : 'border-slate-200',
        'hover:shadow-md hover:-translate-y-0.5 transition-all duration-200'
      )}
    >
      <div className="flex items-center justify-between mb-2">
        <span className="text-[11px] text-slate-400 font-medium">{label}</span>
        {icon && (
          <span
            className={cn(
              'opacity-40',
              highlight ? colorMap[highlight] : 'text-slate-500'
            )}
          >
            {icon}
          </span>
        )}
      </div>
      <div className="flex items-baseline gap-1">
        <span
          className={cn(
            'font-bold leading-none',
            featured ? 'text-[24px]' : 'text-[22px]',
            highlight ? colorMap[highlight] : 'text-slate-700'
          )}
        >
          {value}
        </span>
        {unit && (
          <span className="text-[12px] text-slate-400 font-medium">{unit}</span>
        )}
      </div>
      {sub && (
        <span className="text-[11px] text-slate-400 mt-1.5 truncate leading-snug">
          {sub}
        </span>
      )}
    </div>
  );
}
