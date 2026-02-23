import { clsx, type ClassValue } from 'clsx';

export function cn(...inputs: ClassValue[]) {
  return clsx(inputs);
}

/** Format number as Japanese yen: ¥12,345 */
export function formatYen(value: number | null | undefined): string {
  if (value == null) return '---';
  return `¥${value.toLocaleString('ja-JP')}`;
}

/** Format number as points: 1,234pt */
export function formatPoints(value: number | null | undefined): string {
  if (value == null) return '---';
  return `${value.toLocaleString('ja-JP')}pt`;
}

/** Shorten text with ellipsis */
export function truncate(text: string | null | undefined, maxLen: number): string {
  if (!text) return '';
  return text.length > maxLen ? text.slice(0, maxLen) + '...' : text;
}

/** Format date: 2024-04-30 15:30 */
export function formatDate(iso: string | null | undefined): string {
  if (!iso) return '---';
  const d = new Date(iso);
  return d.toLocaleDateString('ja-JP', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
}

/** Format relative time */
export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return '---';
  const diff = Date.now() - new Date(iso).getTime();
  const minutes = Math.floor(diff / 60000);
  if (minutes < 1) return 'たった今';
  if (minutes < 60) return `${minutes}分前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}時間前`;
  const days = Math.floor(hours / 24);
  return `${days}日前`;
}

/** Build shipping text from days */
export function shippingText(daysMin: number | null | undefined, daysMax: number | null | undefined): string {
  if (daysMin == null && daysMax == null) return '---';
  if (daysMin != null && daysMax != null) {
    if (daysMin === daysMax) return `${daysMin}日`;
    return `${daysMin}〜${daysMax}日`;
  }
  return `${daysMin ?? daysMax}日`;
}

/** Calculate effective price: price - points - pointBack - coupon */
export function calcEffectivePrice(
  price: number,
  pointMultiplier: number,
  pointRate: number | null,
  couponYen: number | null,
  couponPercent: number | null,
  pointBackPercent?: number | null,
): { effectivePrice: number; totalPoints: number; pointBackAmount: number; effectiveCoupon: number } {
  // Shop point multiplier (e.g. +3倍 → pointRate=3.0)
  const plusXUp = pointRate ?? 0;
  const totalPoints = Math.floor(price * (pointMultiplier + plusXUp) / 100);

  // Point back (e.g. +30% ポイントバック → 30.0)
  const pointBackAmount = Math.floor(price * (pointBackPercent ?? 0) / 100);

  // Coupon: take the better of yen or percent coupon
  const couponFromPercent = Math.floor(price * (couponPercent ?? 0) / 100);
  const effectiveCoupon = Math.max(couponYen ?? 0, couponFromPercent);

  const effectivePrice = price - totalPoints - pointBackAmount - effectiveCoupon;
  return { effectivePrice, totalPoints, pointBackAmount, effectiveCoupon };
}
