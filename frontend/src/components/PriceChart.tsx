import { useState, useMemo, useCallback, useRef } from 'react';
import {
  ResponsiveContainer,
  ComposedChart,
  Line,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
} from 'recharts';
import { format, parseISO } from 'date-fns';
import { ja } from 'date-fns/locale';
import {
  RotateCcw,
  Download,
  Maximize2,
  ArrowDownUp,
  ArrowLeftRight,
  Info,
} from 'lucide-react';
import type { HistoryRecord } from '../lib/types';
import { cn, formatYen } from '../lib/utils';

/* ════════════════════════════════════════════════════════════
   Props
   ════════════════════════════════════════════════════════════ */

interface Props {
  history: HistoryRecord[];
  pointMultiplier: number;
  loading?: boolean;
  onDaysChange: (days: number) => void;
  selectedDays: number;
}

/* ════════════════════════════════════════════════════════════
   Period buttons matching the design: 1D 1W 1M 3M 1Y ALL
   ════════════════════════════════════════════════════════════ */

const PERIOD_OPTIONS = [
  { value: 1, label: '1D' },
  { value: 7, label: '1W' },
  { value: 30, label: '1M' },
  { value: 90, label: '3M' },
  { value: 365, label: '1Y' },
  { value: 730, label: 'ALL' },
];

/* ════════════════════════════════════════════════════════════
   Helpers
   ════════════════════════════════════════════════════════════ */

/** Format date for X-axis: M/d */
function fmtDate(v: string): string {
  try {
    return format(parseISO(v), 'M/d', { locale: ja });
  } catch {
    return v;
  }
}

/** Build computed data array from history */
function useChartData(
  history: HistoryRecord[],
  pointMultiplier: number,
  dateFrom: string,
  dateTo: string,
) {
  return useMemo(() => {
    return history
      .filter((r) => {
        if (dateFrom && r.record_date < dateFrom) return false;
        if (dateTo && r.record_date > dateTo) return false;
        return true;
      })
      .map((r) => {
        const price = r.price ?? 0;
        const points = Math.floor(
          (price * (pointMultiplier + (r.point_rate ?? 0))) / 100
        );
        const couponFromPercent = Math.floor(
          (price * (r.coupon_percent ?? 0)) / 100
        );
        const coupon = Math.max(r.coupon_yen ?? 0, couponFromPercent);
        const priceMinusPoints = price - points;
        const effectivePrice = price - points - coupon;

        return {
          date: r.record_date,
          price,
          points,
          coupon,
          priceMinusPoints,
          effectivePrice,
        };
      });
  }, [history, pointMultiplier, dateFrom, dateTo]);
}

/**
 * Compute Y-axis domain using auto-range logic from spec:
 *   step = (max - min) / 8
 *   axis_min = min - step
 *   axis_max = max + step
 *   → gives 10-division scale
 */
function computeYDomain(
  values: number[],
  yMinOverride?: number,
  yMaxOverride?: number,
  tickInterval?: number,
): { domain: [number, number]; ticks: number[] } {
  if (values.length === 0) {
    return { domain: [0, 1000], ticks: [0, 100, 200, 300, 400, 500, 600, 700, 800, 900, 1000] };
  }

  const rawMin = Math.min(...values);
  const rawMax = Math.max(...values);

  // If both overrides are provided, use them directly
  if (yMinOverride != null && yMaxOverride != null) {
    const interval = tickInterval ?? (yMaxOverride - yMinOverride) / 10;
    const ticks: number[] = [];
    for (let v = yMinOverride; v <= yMaxOverride + 0.001; v += interval) {
      ticks.push(Math.round(v));
    }
    return { domain: [yMinOverride, yMaxOverride], ticks };
  }

  // Auto-range: (max - min) / 8 for step, then ±1 step for margins → 10 divisions
  const range = rawMax - rawMin || 1;
  const rawStep = range / 8;

  // Nice step rounding
  const magnitude = Math.pow(10, Math.floor(Math.log10(rawStep)));
  const residual = rawStep / magnitude;
  let niceStep: number;
  if (residual <= 1.5) niceStep = magnitude;
  else if (residual <= 3) niceStep = 2 * magnitude;
  else if (residual <= 7) niceStep = 5 * magnitude;
  else niceStep = 10 * magnitude;

  const step = tickInterval ?? niceStep;
  const niceMin = yMinOverride ?? Math.floor((rawMin - niceStep) / step) * step;
  const niceMax = yMaxOverride ?? Math.ceil((rawMax + niceStep) / step) * step;

  const ticks: number[] = [];
  for (let v = niceMin; v <= niceMax + 0.001; v += step) {
    ticks.push(Math.round(v));
  }

  return { domain: [niceMin, niceMax], ticks };
}

/* ════════════════════════════════════════════════════════════
   CSV Export
   ════════════════════════════════════════════════════════════ */

function exportCSV(data: ReturnType<typeof useChartData>) {
  const header = '日付,販売価格,ポイント,価格-ポイント,クーポン,価格-ポイント-クーポン';
  const rows = data.map(
    (d) => `${d.date},${d.price},${d.points},${d.priceMinusPoints},${d.coupon},${d.effectivePrice}`
  );
  const csv = '\uFEFF' + header + '\n' + rows.join('\n');
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `price_history_${data[0]?.date ?? 'export'}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}

/* ════════════════════════════════════════════════════════════
   Y-Axis Click Modal
   ════════════════════════════════════════════════════════════ */

interface AxisModalProps {
  type: 'y' | 'x';
  onClose: () => void;
  // Y-axis
  yMin: string;
  yMax: string;
  yInterval: string;
  onYChange: (min: string, max: string, interval: string) => void;
  // X-axis
  xFrom: string;
  xTo: string;
  xDivisions: string;
  onXChange: (from: string, to: string, divisions: string) => void;
}

function AxisModal({
  type,
  onClose,
  yMin,
  yMax,
  yInterval,
  onYChange,
  xFrom,
  xTo,
  xDivisions,
  onXChange,
}: AxisModalProps) {
  const [localYMin, setLocalYMin] = useState(yMin);
  const [localYMax, setLocalYMax] = useState(yMax);
  const [localYInterval, setLocalYInterval] = useState(yInterval);
  const [localXFrom, setLocalXFrom] = useState(xFrom);
  const [localXTo, setLocalXTo] = useState(xTo);
  const [localXDivisions, setLocalXDivisions] = useState(xDivisions);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30" onClick={onClose}>
      <div
        className="bg-white rounded-xl shadow-2xl border border-slate-200 p-5 min-w-[320px]"
        onClick={(e) => e.stopPropagation()}
      >
        {type === 'y' ? (
          <>
            <h4 className="text-[14px] font-bold text-slate-700 mb-4">縦軸設定</h4>
            <div className="space-y-3">
              <div>
                <label className="text-[12px] text-slate-500 block mb-1">最小値</label>
                <input
                  type="number"
                  placeholder="Auto"
                  value={localYMin}
                  onChange={(e) => setLocalYMin(e.target.value)}
                  className="w-full px-3 py-2 text-[13px] border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
                />
              </div>
              <div>
                <label className="text-[12px] text-slate-500 block mb-1">最大値</label>
                <input
                  type="number"
                  placeholder="Auto"
                  value={localYMax}
                  onChange={(e) => setLocalYMax(e.target.value)}
                  className="w-full px-3 py-2 text-[13px] border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
                />
              </div>
              <div>
                <label className="text-[12px] text-slate-500 block mb-1">目盛り間隔</label>
                <input
                  type="number"
                  placeholder="Auto"
                  value={localYInterval}
                  onChange={(e) => setLocalYInterval(e.target.value)}
                  className="w-full px-3 py-2 text-[13px] border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
                />
              </div>
            </div>
            <div className="flex gap-2 mt-4">
              <button
                onClick={() => {
                  onYChange(localYMin, localYMax, localYInterval);
                  onClose();
                }}
                className="flex-1 px-4 py-2 text-[13px] font-semibold text-white bg-blue-600 rounded-lg hover:bg-blue-700"
              >
                適用
              </button>
              <button
                onClick={() => {
                  onYChange('', '', '');
                  onClose();
                }}
                className="px-4 py-2 text-[13px] text-slate-600 bg-slate-100 rounded-lg hover:bg-slate-200"
              >
                リセット
              </button>
            </div>
          </>
        ) : (
          <>
            <h4 className="text-[14px] font-bold text-slate-700 mb-4">横軸設定</h4>
            <div className="space-y-3">
              <div>
                <label className="text-[12px] text-slate-500 block mb-1">一番古い日</label>
                <input
                  type="date"
                  value={localXFrom}
                  onChange={(e) => setLocalXFrom(e.target.value)}
                  className="w-full px-3 py-2 text-[13px] border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
                />
              </div>
              <div>
                <label className="text-[12px] text-slate-500 block mb-1">最新の日</label>
                <input
                  type="date"
                  value={localXTo}
                  onChange={(e) => setLocalXTo(e.target.value)}
                  className="w-full px-3 py-2 text-[13px] border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
                />
              </div>
              <div>
                <label className="text-[12px] text-slate-500 block mb-1">分割日数</label>
                <input
                  type="number"
                  placeholder="Auto"
                  value={localXDivisions}
                  onChange={(e) => setLocalXDivisions(e.target.value)}
                  className="w-full px-3 py-2 text-[13px] border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
                />
              </div>
            </div>
            <div className="flex gap-2 mt-4">
              <button
                onClick={() => {
                  onXChange(localXFrom, localXTo, localXDivisions);
                  onClose();
                }}
                className="flex-1 px-4 py-2 text-[13px] font-semibold text-white bg-blue-600 rounded-lg hover:bg-blue-700"
              >
                適用
              </button>
              <button
                onClick={() => {
                  onXChange('', '', '');
                  onClose();
                }}
                className="px-4 py-2 text-[13px] text-slate-600 bg-slate-100 rounded-lg hover:bg-slate-200"
              >
                リセット
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

/* ════════════════════════════════════════════════════════════
   Custom Legend (matching the design screenshot)
   ════════════════════════════════════════════════════════════ */

/* ════════════════════════════════════════════════════════════
   Main PriceChart Component — SINGLE combined chart
   ════════════════════════════════════════════════════════════ */

export default function PriceChart({
  history,
  pointMultiplier,
  loading,
  onDaysChange,
  selectedDays,
}: Props) {
  // State for axis customization
  const [yMin, setYMin] = useState('');
  const [yMax, setYMax] = useState('');
  const [yInterval, setYInterval] = useState('');
  const [dateFrom, setDateFrom] = useState('');
  const [dateTo, setDateTo] = useState('');
  const [xDivisions, setXDivisions] = useState('');
  const [axisModal, setAxisModal] = useState<'y' | 'x' | null>(null);
  const [autoRange, setAutoRange] = useState(true);
  const chartRef = useRef<HTMLDivElement>(null);

  const data = useChartData(history, pointMultiplier, dateFrom, dateTo);

  // Compute Y domain from all 3 series
  const allValues = useMemo(
    () => data.flatMap((d) => [d.price, d.priceMinusPoints, d.effectivePrice]),
    [data]
  );
  const { domain, ticks } = useMemo(() => {
    if (!autoRange && !yMin && !yMax) {
      // No auto-range, no overrides → basic domain
      return computeYDomain(allValues);
    }
    return computeYDomain(
      allValues,
      yMin ? Number(yMin) : undefined,
      yMax ? Number(yMax) : undefined,
      yInterval ? Number(yInterval) : undefined,
    );
  }, [allValues, yMin, yMax, yInterval, autoRange]);

  // X-axis tick count
  const xTickCount = xDivisions ? Number(xDivisions) : undefined;

  // Zoom reset
  const handleZoomReset = useCallback(() => {
    setYMin('');
    setYMax('');
    setYInterval('');
    setDateFrom('');
    setDateTo('');
    setXDivisions('');
    setAutoRange(true);
  }, []);

  // Fullscreen toggle
  const handleFullscreen = useCallback(() => {
    if (!chartRef.current) return;
    if (document.fullscreenElement) {
      document.exitFullscreen();
    } else {
      chartRef.current.requestFullscreen();
    }
  }, []);

  // Loading state
  if (loading) {
    return (
      <div className="bg-white rounded-2xl border border-slate-200 shadow-sm p-6">
        <div className="h-[400px] flex items-center justify-center">
          <div className="w-8 h-8 border-3 border-blue-200 border-t-blue-600 rounded-full animate-spin" />
        </div>
      </div>
    );
  }

  const isEmpty = data.length === 0;

  // Actual day count for "ALL" button
  let allLabel = 'ALL';
  if (data.length > 1 && selectedDays === 730) {
    const firstDate = new Date(data[0].date);
    const lastDate = new Date(data[data.length - 1].date);
    const totalDays = Math.ceil(
      (lastDate.getTime() - firstDate.getTime()) / (1000 * 60 * 60 * 24)
    );
    if (totalDays > 0) allLabel = `ALL`;
  }

  return (
    <div
      ref={chartRef}
      className="bg-white rounded-2xl border border-slate-200 shadow-sm overflow-hidden"
    >
      {/* ══════ Top Bar: Period + Actions ══════ */}
      <div className="flex items-center justify-between px-6 py-4 border-b border-slate-100">
        {/* Left: Period label + buttons */}
        <div className="flex items-center gap-3">
          <span className="text-[13px] font-medium text-slate-500">
            期間:{' '}
            {PERIOD_OPTIONS.find((o) => o.value === selectedDays)?.label ?? `${selectedDays}D`}
          </span>
          <div className="flex items-center gap-1">
            {PERIOD_OPTIONS.map((opt) => (
              <button
                key={opt.value}
                onClick={() => onDaysChange(opt.value)}
                className={cn(
                  'px-3 py-1.5 text-[13px] font-semibold rounded-full transition-all',
                  selectedDays === opt.value
                    ? 'bg-blue-600 text-white shadow-sm'
                    : 'text-slate-500 hover:bg-slate-100 hover:text-slate-700'
                )}
              >
                {opt.value === 730 ? allLabel : opt.label}
              </button>
            ))}
          </div>
        </div>

        {/* Right: Action buttons */}
        <div className="flex items-center gap-2">
          <button
            onClick={handleZoomReset}
            className="flex items-center gap-1.5 px-3 py-1.5 text-[12px] font-medium text-slate-500 border border-slate-200 rounded-lg hover:bg-slate-50 transition-colors"
          >
            <RotateCcw className="w-3.5 h-3.5" />
            ズームリセット
          </button>
          <button
            onClick={() => exportCSV(data)}
            disabled={isEmpty}
            className="flex items-center gap-1.5 px-3 py-1.5 text-[12px] font-medium text-slate-500 border border-slate-200 rounded-lg hover:bg-slate-50 transition-colors disabled:opacity-40"
          >
            <Download className="w-3.5 h-3.5" />
            CSV出力
          </button>
          <button
            onClick={handleFullscreen}
            className="p-1.5 text-slate-400 hover:text-slate-600 transition-colors"
          >
            <Maximize2 className="w-4 h-4" />
          </button>
        </div>
      </div>

      {/* ══════ Chart Area ══════ */}
      {isEmpty ? (
        <div className="h-[380px] flex items-center justify-center text-[14px] text-slate-400">
          履歴データがありません
        </div>
      ) : (
        <div className="px-4 pt-6 pb-2">
          <ResponsiveContainer width="100%" height={380}>
            <ComposedChart data={data} margin={{ top: 10, right: 20, bottom: 5, left: 10 }}>
              <defs>
                <linearGradient id="areaGradient" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="#3b82f6" stopOpacity={0.12} />
                  <stop offset="100%" stopColor="#3b82f6" stopOpacity={0.02} />
                </linearGradient>
              </defs>

              <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" vertical={false} />

              {/* X-Axis (clickable) */}
              <XAxis
                dataKey="date"
                tickFormatter={fmtDate}
                tick={{ fontSize: 11, fill: '#94a3b8' }}
                axisLine={{ stroke: '#e2e8f0' }}
                tickLine={false}
                tickCount={xTickCount}
                label={{
                  value: '年月日',
                  position: 'insideBottomRight',
                  offset: -5,
                  style: { fontSize: 11, fill: '#94a3b8' },
                }}
                onClick={() => setAxisModal('x')}
                style={{ cursor: 'pointer' }}
              />

              {/* Y-Axis (clickable) */}
              <YAxis
                domain={domain}
                ticks={ticks}
                tickFormatter={(v: number) => `¥${v.toLocaleString()}`}
                tick={{ fontSize: 11, fill: '#94a3b8' }}
                axisLine={false}
                tickLine={false}
                width={70}
                label={{
                  value: '販売価格(円)',
                  angle: -90,
                  position: 'insideLeft',
                  offset: 5,
                  style: { fontSize: 11, fill: '#94a3b8', textAnchor: 'middle' },
                }}
                onClick={() => setAxisModal('y')}
                style={{ cursor: 'pointer' }}
              />

              {/* Tooltip */}
              <Tooltip
                content={({ active, payload }) => {
                  if (!active || !payload?.length) return null;
                  const d = payload[0].payload;
                  return (
                    <div className="bg-white rounded-xl shadow-2xl border border-slate-200 px-4 py-3 text-[12px] min-w-[220px]">
                      <div className="font-semibold text-slate-600 mb-2 pb-2 border-b border-slate-100">
                        {d.date}
                      </div>
                      <div className="space-y-1.5">
                        <div className="flex items-center justify-between gap-4">
                          <span className="flex items-center gap-1.5">
                            <span className="w-2.5 h-2.5 rounded-full bg-blue-600 inline-block" />
                            <span className="text-slate-500">販売価格</span>
                          </span>
                          <span className="font-bold text-blue-600">{formatYen(d.price)}</span>
                        </div>
                        <div className="flex items-center justify-between gap-4">
                          <span className="flex items-center gap-1.5">
                            <span className="w-2.5 h-2.5 rounded-full bg-cyan-400 inline-block" />
                            <span className="text-slate-500">価格−ポイント</span>
                          </span>
                          <span className="font-bold text-cyan-600">{formatYen(d.priceMinusPoints)}</span>
                        </div>
                        <div className="flex items-center justify-between gap-4">
                          <span className="flex items-center gap-1.5">
                            <span className="w-2.5 h-2.5 rounded-full bg-blue-500 inline-block" />
                            <span className="text-slate-500">価格−ポイント−クーポン</span>
                          </span>
                          <span className="font-bold text-blue-500">{formatYen(d.effectivePrice)}</span>
                        </div>
                      </div>
                    </div>
                  );
                }}
              />

              {/* Legend at bottom */}
              <Legend
                verticalAlign="bottom"
                height={40}
                iconType="square"
                iconSize={10}
                wrapperStyle={{ fontSize: '12px', paddingTop: '12px' }}
                formatter={(value: string) => (
                  <span className="text-slate-600 text-[12px] ml-1">{value}</span>
                )}
              />

              {/* ── Area: 価格-ポイント-クーポン (filled area, blue) ── */}
              <Area
                type="monotone"
                dataKey="effectivePrice"
                name="価格−ポイント−クーポン"
                stroke="#3b82f6"
                strokeWidth={2}
                fill="url(#areaGradient)"
                dot={false}
                activeDot={{ r: 4, fill: '#3b82f6', stroke: '#fff', strokeWidth: 2 }}
              />

              {/* ── Line: 販売価格 (solid blue with dots) ── */}
              <Line
                type="monotone"
                dataKey="price"
                name="販売価格"
                stroke="#2563eb"
                strokeWidth={2.5}
                dot={{ r: 3.5, fill: '#2563eb', stroke: '#fff', strokeWidth: 2 }}
                activeDot={{ r: 5, fill: '#2563eb', stroke: '#fff', strokeWidth: 2 }}
              />

              {/* ── Line: 価格-ポイント (cyan dashed with dots) ── */}
              <Line
                type="monotone"
                dataKey="priceMinusPoints"
                name="価格−ポイント"
                stroke="#22d3ee"
                strokeWidth={2}
                strokeDasharray="6 3"
                dot={{ r: 3.5, fill: '#22d3ee', stroke: '#fff', strokeWidth: 2 }}
                activeDot={{ r: 5, fill: '#22d3ee', stroke: '#fff', strokeWidth: 2 }}
              />

              {/* ── Line: 価格-ポイント-クーポン (step line, light blue) ── */}
              <Line
                type="stepAfter"
                dataKey="effectivePrice"
                name="価格−ポイント−クーポン"
                stroke="#93c5fd"
                strokeWidth={1.5}
                dot={false}
                activeDot={false}
                legendType="none"
              />
            </ComposedChart>
          </ResponsiveContainer>
        </div>
      )}

      {/* ══════ Bottom Info Tips ══════ */}
      <div className="px-6 py-4 border-t border-slate-100 flex flex-wrap items-start gap-6">
        <button
          onClick={() => setAxisModal('y')}
          className="flex items-start gap-2 text-[12px] text-slate-500 hover:text-blue-600 transition-colors"
        >
          <ArrowDownUp className="w-4 h-4 mt-0.5 text-blue-500 flex-shrink-0" />
          <span>
            縦軸をクリックすると、最小値/最大値/目盛り間隔を直接指定可能です。
          </span>
        </button>
        <button
          onClick={() => setAxisModal('x')}
          className="flex items-start gap-2 text-[12px] text-slate-500 hover:text-blue-600 transition-colors"
        >
          <ArrowLeftRight className="w-4 h-4 mt-0.5 text-blue-500 flex-shrink-0" />
          <span>
            横軸をクリックすると、一番古い日と最新の日、および分割日数を指定可能です。
          </span>
        </button>
        <div className="flex items-start gap-2 text-[12px] text-slate-400">
          <Info className="w-4 h-4 mt-0.5 flex-shrink-0" />
          <span>
            「オートレンジ」有効時は、(最高値-最安値)/8 のロジックで軸を自動最適化します。
          </span>
        </div>
      </div>

      {/* ══════ Axis Modal ══════ */}
      {axisModal && (
        <AxisModal
          type={axisModal}
          onClose={() => setAxisModal(null)}
          yMin={yMin}
          yMax={yMax}
          yInterval={yInterval}
          onYChange={(min, max, interval) => {
            setYMin(min);
            setYMax(max);
            setYInterval(interval);
            if (min || max) setAutoRange(false);
          }}
          xFrom={dateFrom}
          xTo={dateTo}
          xDivisions={xDivisions}
          onXChange={(from, to, divisions) => {
            setDateFrom(from);
            setDateTo(to);
            setXDivisions(divisions);
          }}
        />
      )}
    </div>
  );
}
