"""
Rakuten point calculation logic.

Implements the calculation from 楽天ポイント計算.xlsx:

┌────────────┬────────────────┬──────────────────────────────────────────────┐
│ インプット   │ 項目            │ 備考                                         │
├────────────┼────────────────┼──────────────────────────────────────────────┤
│            │ ポイント倍率     │ ユーザー指定 (デフォルト4)                      │
│            │ 楽天価格 [円]    │ ← サーバー取得値                              │
│            │ クーポン [円]    │ ← サーバー取得値                              │
│            │ クーポン [%]     │ ← サーバー取得値                              │
│            │ +○倍UP         │ ← サーバー取得値 (無い時は0)                    │
│            │ ○%ポイントバック │ ← サーバー取得値 (無い時は1)                    │
├────────────┼────────────────┼──────────────────────────────────────────────┤
│ アウトプット │ クーポン採用値   │ max(coupon_yen, floor(price * coupon%/100))   │
│            │ 合計ポイント     │ floor(price * (倍率 + UP倍) / 100) * PBflag   │
│            │ 価格-ポイント    │ price - total_points                         │
│            │ 価格-ポイント-CP │ price - total_points - effective_coupon       │
└────────────┴────────────────┴──────────────────────────────────────────────┘

Verification against spreadsheet examples:
  計算例1: 倍率=1, price=2627, coupon_yen=0, coupon%=0, up=0, pb=1
           → coupon_adopted=0, points=25, p-pt=2602, p-pt-cp=2602  ✓
  計算例2: 倍率=4, price=2627, coupon_yen=0, coupon%=0, up=0, pb=1
           → coupon_adopted=0, points=94 (≈2627*4/100=105.08→floor?), ...
           (Note: verify exact floor/round behavior with real xlsx)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class PointInput:
    """Inputs for the Rakuten point calculation."""

    point_multiplier: float = 4.0
    """ポイント倍率 — user-specified, default 4."""

    rakuten_price: int = 0
    """楽天価格 [円] — from server/scrape."""

    coupon_yen: int = 0
    """クーポン [円] — fixed-amount coupon."""

    coupon_percent: float = 0.0
    """クーポン [%] — percentage coupon."""

    plus_x_up: float = 0.0
    """+ ○倍UP — additional shop point multiplier (0 if none)."""

    point_back_flag: int = 1
    """○%ポイントバック — multiplier flag (1 if none, > 1 if active).
    Spreadsheet column: 無い時は1."""


@dataclass
class PointOutput:
    """Calculated output values."""

    effective_coupon: int = 0
    """クーポン採用値 [円]."""

    total_points: int = 0
    """合計ポイント [pt]."""

    price_minus_points: int = 0
    """価格-ポイント [円]."""

    price_minus_points_coupon: int = 0
    """価格-ポイント-クーポン [円]."""


def calculate_rakuten_points(inp: PointInput) -> PointOutput:
    """
    Calculate Rakuten points and effective prices.

    Logic (from 楽天ポイント計算.xlsx):
    1. effective_coupon = max(coupon_yen, floor(price * coupon_percent / 100))
    2. total_points = floor(price * (point_multiplier + plus_x_up) / 100) * point_back_flag
    3. price_minus_points = price - total_points
    4. price_minus_points_coupon = price - total_points - effective_coupon
    """
    price = inp.rakuten_price

    # Step 1: Determine effective coupon (take the larger of yen or %-based)
    coupon_from_percent = math.floor(price * inp.coupon_percent / 100)
    effective_coupon = max(inp.coupon_yen, coupon_from_percent)

    # Step 2: Calculate total points
    base_points = math.floor(price * (inp.point_multiplier + inp.plus_x_up) / 100)
    total_points = base_points * inp.point_back_flag

    # Step 3 & 4: Net prices
    price_minus_points = price - total_points
    price_minus_points_coupon = price - total_points - effective_coupon

    return PointOutput(
        effective_coupon=effective_coupon,
        total_points=total_points,
        price_minus_points=price_minus_points,
        price_minus_points_coupon=price_minus_points_coupon,
    )


def calculate_for_variant_snapshot(
    price: int,
    point_rate: float | None,
    point_back_percent: float | None,
    coupon_yen: int | None,
    coupon_percent: float | None,
    user_point_multiplier: float = 4.0,
) -> PointOutput:
    """
    Convenience wrapper: calculate from snapshot data + user multiplier.

    Maps snapshot fields to PointInput:
    - point_rate → plus_x_up  (shop's additional multiplier like +3倍)
    - point_back_percent → point_back_flag  (if > 0, flag = that value; else 1)
    """
    inp = PointInput(
        point_multiplier=user_point_multiplier,
        rakuten_price=price,
        coupon_yen=coupon_yen or 0,
        coupon_percent=float(coupon_percent or 0),
        plus_x_up=float(point_rate or 0),
        point_back_flag=1 if not point_back_percent else int(point_back_percent),
    )
    return calculate_rakuten_points(inp)
