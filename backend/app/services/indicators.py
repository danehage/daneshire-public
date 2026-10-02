"""
Technical Indicators
Calculations for swing trading and options strategies
"""

from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd
import pytz

# Constants
TRADING_MINUTES_PER_DAY = 390  # 6.5 hours
TRADING_DAYS_PER_YEAR = 252
MARKET_OPEN_HOUR = 9
MARKET_OPEN_MINUTE = 30
MARKET_CLOSE_HOUR = 16

_ET = pytz.timezone("America/New_York")


def is_regular_session(now_et: datetime) -> bool:
    """True during the regular US-equity session: weekdays 09:30–16:00 ET.

    Minute resolution, both ends inclusive, so the scheduler tick that lands
    a few seconds after 16:00:00 still runs the closing check. A tz-aware
    datetime is converted to ET first; a naive one is assumed to be ET.

    Exchange holidays and half-days are NOT handled (ADR-0001 defers a
    market-calendar module); a holiday weekday counts as a session day.
    """
    if now_et.tzinfo is not None:
        now_et = now_et.astimezone(_ET)
    if now_et.weekday() >= 5:
        return False
    hm = (now_et.hour, now_et.minute)
    return (MARKET_OPEN_HOUR, MARKET_OPEN_MINUTE) <= hm <= (MARKET_CLOSE_HOUR, 0)


def current_et_time() -> datetime:
    """Current time in America/New_York."""
    return datetime.now(_ET)


class TechnicalIndicators:
    """Calculate technical indicators from price data."""

    @staticmethod
    def calculate_volume_pace(
        current_volume: int, avg_volume: int
    ) -> tuple[float, bool]:
        """
        Calculate volume pace normalized for time of day.

        Returns:
            tuple: (pace_ratio, is_market_hours)
            - pace_ratio: how current volume compares to expected volume
            - is_market_hours: whether calculation was made during market hours
        """
        if avg_volume <= 0:
            return 0.0, False

        et_tz = pytz.timezone("US/Eastern")
        now_et = datetime.now(et_tz)

        market_open = now_et.replace(
            hour=MARKET_OPEN_HOUR, minute=MARKET_OPEN_MINUTE, second=0, microsecond=0
        )
        market_close = now_et.replace(
            hour=MARKET_CLOSE_HOUR, minute=0, second=0, microsecond=0
        )

        # Before/after market: return raw ratio with flag
        if now_et < market_open or now_et >= market_close:
            return current_volume / avg_volume, False

        # During market: adjust for time elapsed
        minutes_elapsed = (now_et - market_open).total_seconds() / 60
        percent_elapsed = max(minutes_elapsed / TRADING_MINUTES_PER_DAY, 0.01)

        expected_volume = avg_volume * percent_elapsed
        pace = current_volume / expected_volume if expected_volume > 0 else 0.0
        return pace, True

    @staticmethod
    def calculate_avg_volume(df: pd.DataFrame, days: int = 20) -> Optional[int]:
        """Calculate average volume over recent trading days."""
        if df is None or len(df) < days:
            return None

        recent_volume = df["volume"].tail(days)
        avg_vol = recent_volume.mean()

        return int(avg_vol) if not pd.isna(avg_vol) else None

    @staticmethod
    def calculate_rsi(df: pd.DataFrame, period: int = 14) -> float:
        """
        Calculate Relative Strength Index using Wilder's smoothing.
        """
        if df is None or len(df) < period + 1:
            return 50.0

        delta = df["close"].diff()
        gain = delta.where(delta > 0, 0.0)
        loss = -delta.where(delta < 0, 0.0)

        # Wilder's smoothing: alpha = 1/period
        alpha = 1.0 / period
        avg_gain = gain.ewm(alpha=alpha, min_periods=period, adjust=False).mean().iloc[-1]
        avg_loss = loss.ewm(alpha=alpha, min_periods=period, adjust=False).mean().iloc[-1]

        if avg_loss == 0:
            return 100.0 if avg_gain > 0 else 50.0

        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    @staticmethod
    def _rsi_series(close: pd.Series, period: int = 14) -> pd.Series:
        """Full Wilder-smoothed RSI series (calculate_rsi returns only the last value)."""
        delta = close.diff()
        gain = delta.where(delta > 0, 0.0)
        loss = -delta.where(delta < 0, 0.0)
        alpha = 1.0 / period
        avg_gain = gain.ewm(alpha=alpha, min_periods=period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=alpha, min_periods=period, adjust=False).mean()
        rsi = 100 - (100 / (1 + avg_gain / avg_loss))
        # avg_loss == 0 -> inf ratio -> RSI 100; 0/0 -> NaN -> neutral
        return rsi.where(avg_loss > 0, 100.0).fillna(50.0)

    @staticmethod
    def calculate_recovery_metrics(
        current_price: float, df: pd.DataFrame
    ) -> Optional[dict]:
        """
        Time-series metrics that separate a stock that FINISHED falling from one
        still falling (issue #49). All derived from the sequence of swing lows,
        not the current snapshot — two stocks can be identical today and opposite
        situations over time.

        Validated against real trades (see #49): rsi_floor_delta rising preceded
        the ESTC/EOG/APD wins; falling marked the ADBE loss and AMC.
        """
        if df is None or len(df) < 60:
            return None

        lows = df["low"].reset_index(drop=True)
        closes = df["close"].reset_index(drop=True)
        rsi = TechnicalIndicators._rsi_series(closes)

        # Swing lows: local minima of the daily low over a +/-7-bar window,
        # spaced >10 bars apart, searched over the trailing ~6 months. A new
        # period low landing <=10 bars after the prior swing low is part of
        # the same selling event (a capitulation flush, e.g. ESTC's final
        # leg) — it must NOT open a new floor entry or it poisons the RSI
        # divergence read; it still drives the post-low metrics below.
        window, spacing = 7, 10
        start = max(0, len(lows) - 126)
        swing_idx: list[int] = []
        for i in range(start, len(lows)):
            lo, hi = max(0, i - window), min(len(lows), i + window + 1)
            if lows.iloc[i] == lows.iloc[lo:hi].min() and (
                not swing_idx or i - swing_idx[-1] > spacing
            ):
                swing_idx.append(i)

        # Descending floor sequence: swing lows that set a new running low.
        # Divergence and undercuts are measured across these — a higher swing
        # low in between is basing noise, not a new floor (EOG 2025-11-24).
        floor_idx: list[int] = []
        for i in swing_idx:
            if not floor_idx or lows.iloc[i] < lows.iloc[floor_idx[-1]]:
                floor_idx.append(i)

        # RSI floor direction: force of selling at the newest swing low vs
        # the last established floor before it. Rising = sellers exhausting;
        # falling = decline accelerating. Comparing against the prior FLOOR
        # (not the prior swing low) is what lets a weak higher low (ADBE
        # 2026-01-12, RSI below the floor's) read as deterioration.
        rsi_floor_delta = None
        if swing_idx:
            newest = swing_idx[-1]
            prior_floors = [i for i in floor_idx if i < newest]
            if prior_floors:
                rsi_floor_delta = float(rsi.iloc[newest] - rsi.iloc[prior_floors[-1]])

        # Undercuts: % each new floor broke below its predecessor.
        undercuts = [
            (lows.iloc[a] - lows.iloc[b]) / lows.iloc[a] * 100
            for a, b in zip(floor_idx, floor_idx[1:])
        ]
        undercut_decelerating = None
        if len(undercuts) >= 2:
            undercut_decelerating = bool(undercuts[-1] < undercuts[-2])

        min_idx = int(lows.idxmin())
        low_252 = lows.min()
        days_since_low = len(lows) - 1 - min_idx
        pct_above_low = (current_price - low_252) / low_252 * 100 if low_252 > 0 else 0.0

        # Retests: post-low episodes where price came within 3% of the low and
        # (by definition of the period min) held it.
        post = lows.iloc[min_idx + 1 :]
        near = post <= low_252 * 1.03
        retests_held = int((near & ~near.shift(fill_value=False)).sum())

        # Post-low trajectory: minima of successive 10-bar blocks since the low.
        # Last block above the previous = bounce holding; below = bounce fading
        # back toward the low (the ADBE/value-trap signature).
        blocks = [
            float(post.iloc[i : i + 10].min()) for i in range(0, len(post), 10)
        ]
        post_low_trend = None
        if len(blocks) >= 2:
            post_low_trend = "rising" if blocks[-1] > blocks[-2] else "fading"

        # Volatility decay: current 20d HV vs its peak over the last 90 bars.
        # Well below peak = fear draining. Only meaningful when a climax
        # happened, so it confirms and never disqualifies.
        returns = closes.pct_change()
        hv = returns.rolling(20).std() * np.sqrt(TRADING_DAYS_PER_YEAR) * 100
        hv_peak_90 = hv.tail(90).max()
        hv_vs_peak = (
            float(hv.iloc[-1] / hv_peak_90 * 100)
            if hv_peak_90 and hv_peak_90 > 0 and not pd.isna(hv.iloc[-1])
            else None
        )

        # Reclaim streak: consecutive closes above the 20-day MA.
        ma20 = closes.rolling(20).mean()
        reclaim_streak = 0
        for c, m in zip(closes.iloc[::-1], ma20.iloc[::-1]):
            if pd.isna(m) or c <= m:
                break
            reclaim_streak += 1

        return {
            "rsi_floor_delta": round(rsi_floor_delta, 1)
            if rsi_floor_delta is not None
            else None,
            "undercut_decelerating": undercut_decelerating,
            "post_low_trend": post_low_trend,
            "days_since_low": days_since_low,
            "pct_above_low": round(pct_above_low, 1),
            "retests_held": retests_held,
            "hv_vs_peak": round(hv_vs_peak, 0) if hv_vs_peak is not None else None,
            "reclaim_streak": reclaim_streak,
            "swing_low_count": len(swing_idx),
        }

    @staticmethod
    def calculate_volatility_rank(df: pd.DataFrame) -> Optional[dict]:
        """
        Calculate Historical Volatility Rank (HV Rank).

        NOTE: This is NOT implied volatility. It measures where current
        realized/historical volatility sits in its 52-week range.
        """
        if df is None or len(df) < 30:
            return None

        returns = df["close"].pct_change()
        current_hv = returns.tail(20).std() * np.sqrt(TRADING_DAYS_PER_YEAR) * 100

        rolling_vol = returns.rolling(20).std() * np.sqrt(TRADING_DAYS_PER_YEAR) * 100
        vol_52w = (
            rolling_vol.tail(TRADING_DAYS_PER_YEAR)
            if len(rolling_vol) >= TRADING_DAYS_PER_YEAR
            else rolling_vol
        )

        vol_min = vol_52w.min()
        vol_max = vol_52w.max()

        if vol_max > vol_min:
            rank = ((current_hv - vol_min) / (vol_max - vol_min)) * 100
        else:
            rank = 50.0

        return {
            "rank": round(rank, 1),
            "current_hv": round(current_hv, 1),
            "is_proxy": True,
        }

    @staticmethod
    def calculate_trend(current_price: float, df: pd.DataFrame) -> Optional[dict]:
        """
        Classify trend based on moving averages and price position.
        """
        if df is None or len(df) < 50:
            return None

        ma_50 = df["close"].tail(50).mean()
        ma_200 = df["close"].tail(200).mean() if len(df) >= 200 else None

        dist_50 = ((current_price - ma_50) / ma_50) * 100
        dist_200 = ((current_price - ma_200) / ma_200) * 100 if ma_200 else None

        # Calculate MA slope
        ma_slope = 0.0
        if len(df) >= 60:
            ma_50_series = df["close"].tail(60).rolling(50).mean()
            valid_ma = ma_50_series.dropna()
            if len(valid_ma) >= 10:
                ma_now = valid_ma.iloc[-1]
                ma_10_ago = valid_ma.iloc[-10]
                if ma_10_ago > 0:
                    ma_slope = ((ma_now - ma_10_ago) / ma_10_ago) * 100

        # Classify trend
        if ma_slope > 2:
            if dist_50 > 10:
                trend = "Extended Uptrend"
            elif dist_50 > 0:
                trend = "Uptrend"
            else:
                trend = "Pullback in Uptrend"
        elif ma_slope < -2:
            if dist_50 < -10:
                trend = "Oversold"
            elif dist_50 < 0:
                trend = "Downtrend"
            else:
                trend = "Bounce in Downtrend"
        else:
            trend = "Consolidation"

        return {
            "trend": trend,
            "dist_50": round(dist_50, 1),
            "dist_200": round(dist_200, 1) if dist_200 else None,
            "ma_50": round(ma_50, 2),
            "ma_200": round(ma_200, 2) if ma_200 else None,
            "ma_slope": round(ma_slope, 2),
        }

    @staticmethod
    def calculate_52w_position(current_price: float, df: pd.DataFrame) -> dict:
        """
        Calculate where price sits in its 52-week range.
        0% = at yearly low, 100% = at yearly high
        """
        if df is None or len(df) == 0:
            return {
                "position_pct": 50.0,
                "high_52w": current_price,
                "low_52w": current_price,
            }

        lookback = min(len(df), TRADING_DAYS_PER_YEAR)
        high_52w = df["high"].tail(lookback).max()
        low_52w = df["low"].tail(lookback).min()

        if high_52w > low_52w:
            position = ((current_price - low_52w) / (high_52w - low_52w)) * 100
        else:
            position = 50.0

        return {
            "position_pct": round(position, 1),
            "high_52w": round(high_52w, 2),
            "low_52w": round(low_52w, 2),
        }

    @staticmethod
    def calculate_support_resistance(current_price: float, df: pd.DataFrame) -> dict:
        """
        Calculate key support and resistance levels.
        """
        if df is None or len(df) == 0:
            return {
                "support": round(current_price * 0.95, 2),
                "resistance": round(current_price * 1.05, 2),
                "support_type": "estimated",
                "resistance_type": "estimated",
            }

        # Determine step size based on price
        if current_price >= 100:
            step = 10
        elif current_price >= 50:
            step = 5
        elif current_price >= 10:
            step = 1
        else:
            step = 0.5

        base = int(current_price / step) * step
        round_levels = [base - step, base, base + step, base + step * 2]

        # Recent highs/lows
        recent_20 = df.tail(20)
        recent_60 = df.tail(min(60, len(df)))

        recent_high_20 = recent_20["high"].max()
        recent_low_20 = recent_20["low"].min()
        recent_high_60 = recent_60["high"].max()
        recent_low_60 = recent_60["low"].min()

        # Find swing lows (potential support)
        support_levels = []
        support_levels.append((recent_low_20, "recent_low_20d"))
        support_levels.append((recent_low_60, "recent_low_60d"))
        for level in round_levels:
            if level < current_price:
                support_levels.append((level, "round_number"))

        # Find swing highs (potential resistance)
        resistance_levels = []
        resistance_levels.append((recent_high_20, "recent_high_20d"))
        resistance_levels.append((recent_high_60, "recent_high_60d"))
        for level in round_levels:
            if level > current_price:
                resistance_levels.append((level, "round_number"))

        # Get nearest support below current price
        valid_supports = [(p, t) for p, t in support_levels if p < current_price]
        if valid_supports:
            support, support_type = max(valid_supports, key=lambda x: x[0])
        else:
            support, support_type = current_price * 0.95, "estimated"

        # Get nearest resistance above current price
        valid_resistances = [(p, t) for p, t in resistance_levels if p > current_price]
        if valid_resistances:
            resistance, resistance_type = min(valid_resistances, key=lambda x: x[0])
        else:
            resistance, resistance_type = current_price * 1.05, "estimated"

        return {
            "support": round(support, 2),
            "resistance": round(resistance, 2),
            "support_type": support_type,
            "resistance_type": resistance_type,
        }

    @staticmethod
    def calculate_momentum(
        current_price: float, current_volume: int, df: pd.DataFrame
    ) -> dict:
        """
        Calculate momentum indicators.
        """
        if df is None or len(df) < 15:
            return {"volume_ratio": 1.0, "rsi": 50.0, "momentum_10d": 0.0}

        # Volume ratio
        avg_volume_20 = df["volume"].tail(min(20, len(df))).mean()
        volume_ratio = current_volume / avg_volume_20 if avg_volume_20 > 0 else 1.0

        # 10-day momentum
        lookback = min(10, len(df) - 1)
        price_ago = df["close"].iloc[-lookback] if lookback > 0 else current_price
        momentum_10d = (
            ((current_price - price_ago) / price_ago) * 100 if price_ago > 0 else 0.0
        )

        # RSI
        rsi = TechnicalIndicators.calculate_rsi(df)

        return {
            "volume_ratio": round(volume_ratio, 2),
            "momentum_10d": round(momentum_10d, 1),
            "rsi": round(rsi, 1),
        }

    @staticmethod
    def calculate_plateau_metrics(
        current_price: float,
        df: pd.DataFrame,
        open_date: Optional[str] = None,
        pre_entry_high_52w: Optional[float] = None,
        entry_price: Optional[float] = None,
    ) -> Optional[dict]:
        """
        Time-series metrics that separate a stock that has STOPPED rising from one
        still rising (issue #57). Mirror of calculate_recovery_metrics() with swing
        HIGHS instead of lows, RSI CEILINGS instead of floors.

        Requires open_date (ISO string) to anchor the swing-high search from the
        position open date forward — prevents a pre-entry crash peak from reading
        as "the run ceiling" and poisoning the grade.
        """
        if df is None or len(df) < 60:
            return None

        highs = df["high"].reset_index(drop=True)
        lows = df["low"].reset_index(drop=True)
        closes = df["close"].reset_index(drop=True)
        rsi = TechnicalIndicators._rsi_series(closes)

        # Run anchoring: search from max(open_date bar index, trailing 126 bars).
        # Without this, a stock bought after a crash reads its pre-crash peak as
        # "the ceiling" and the entire recovery grades as already-cresting.
        anchor = max(0, len(highs) - 126)
        if open_date is not None and "date" in df.columns:
            dates = pd.to_datetime(df["date"]).reset_index(drop=True)
            open_ts = pd.Timestamp(open_date)
            matches = dates[dates >= open_ts]
            if not matches.empty:
                anchor = max(int(matches.index[0]), anchor)

        # Swing highs: local maxima of the daily high over a +/-7-bar window,
        # spaced >10 bars apart, searched from the run anchor onward.
        window, spacing = 7, 10
        swing_idx: list[int] = []
        for i in range(anchor, len(highs)):
            lo, hi = max(0, i - window), min(len(highs), i + window + 1)
            if highs.iloc[i] == highs.iloc[lo:hi].max() and (
                not swing_idx or i - swing_idx[-1] > spacing
            ):
                swing_idx.append(i)

        # Ascending ceiling sequence: swing highs that set a new running high.
        ceiling_idx: list[int] = []
        for i in swing_idx:
            if not ceiling_idx or highs.iloc[i] > highs.iloc[ceiling_idx[-1]]:
                ceiling_idx.append(i)

        rsi_at_newest_ceiling = (
            float(rsi.iloc[ceiling_idx[-1]]) if ceiling_idx else None
        )

        # RSI ceiling direction: RSI at the newest SWING HIGH vs the last
        # ceiling before it. Falling = buyers exhausting (sell signal). Lower
        # swing highs never become ceilings, so a ceilings-only delta would
        # read an EOG-style fade (RSI 85.5 -> 59 -> 58) as strengthening —
        # the newest swing high is what exposes the exhaustion.
        rsi_ceiling_delta = None
        if swing_idx:
            newest_swing = swing_idx[-1]
            prior_ceilings = [i for i in ceiling_idx if i < newest_swing]
            if prior_ceilings:
                rsi_ceiling_delta = float(
                    rsi.iloc[newest_swing] - rsi.iloc[prior_ceilings[-1]]
                )

        # Overshoot %: how much each ceiling cleared the prior, relative to prior.
        # Deceleration = run extension narrowing, momentum gas running out.
        overshoots = [
            (highs.iloc[b] - highs.iloc[a]) / highs.iloc[a] * 100
            for a, b in zip(ceiling_idx, ceiling_idx[1:])
        ]
        overshoot_decelerating = None
        if len(overshoots) >= 2:
            overshoot_decelerating = bool(overshoots[-1] < overshoots[-2])

        # Post-high trajectory: maxima of successive 10-bar blocks of CLOSES since
        # the newest run ceiling. Fading = price losing altitude (EOG signature).
        post_high_trend = None
        if ceiling_idx:
            newest_ceiling_bar = ceiling_idx[-1]
            post_high_closes = closes.iloc[newest_ceiling_bar + 1:]
            blocks = [
                float(post_high_closes.iloc[i : i + 10].max())
                for i in range(0, len(post_high_closes), 10)
                if len(post_high_closes.iloc[i : i + 10]) >= 5
            ]
            if len(blocks) >= 2:
                post_high_trend = "rising" if blocks[-1] > blocks[-2] else "fading"

        # Extension vs 50-MA: parabolic stretch above medium-term trend.
        ma50 = closes.rolling(50).mean()
        ext_vs_50ma = None
        ma50_val = float(ma50.iloc[-1]) if not pd.isna(ma50.iloc[-1]) else None
        if ma50_val and ma50_val > 0:
            ext_vs_50ma = (current_price - ma50_val) / ma50_val * 100

        # ATR(14) for stop sizing.
        tr = pd.concat(
            [
                highs - lows,
                (highs - closes.shift(1)).abs(),
                (lows - closes.shift(1)).abs(),
            ],
            axis=1,
        ).max(axis=1)
        atr14_series = tr.ewm(alpha=1.0 / 14, min_periods=14, adjust=False).mean()
        atr14 = (
            float(atr14_series.iloc[-1])
            if not pd.isna(atr14_series.iloc[-1])
            else None
        )

        # Minor swing floor (±5 bars, >7 spacing, last 60 bars) for structure stop.
        # Tighter than the main ±7 detector to catch the most recent support shelf.
        # Only CONFIRMED floors count — a bar needs window_f bars after it, else a
        # declining stock's newest low reads as its own "floor" and price can
        # never break it (Broken would never fire).
        window_f, spacing_f = 5, 7
        start_f = max(0, len(lows) - 60)
        minor_floors: list[int] = []
        for i in range(start_f, len(lows) - window_f):
            lo_f = max(0, i - window_f)
            hi_f = min(len(lows), i + window_f + 1)
            if lows.iloc[i] == lows.iloc[lo_f:hi_f].min() and (
                not minor_floors or i - minor_floors[-1] > spacing_f
            ):
                minor_floors.append(i)
        minor_swing_floor = (
            float(lows.iloc[minor_floors[-1]]) if minor_floors else None
        )

        # Gap-closed: where the current price sits vs the pre-entry 52-week high.
        # >= 88% triggers Thesis complete.
        gap_closed_pct = None
        if pre_entry_high_52w and pre_entry_high_52w > 0:
            gap_closed_pct = current_price / pre_entry_high_52w * 100

        # Down/up volume ratio (recent 20 bars) as distribution confirmation.
        recent_20 = df.tail(20).reset_index(drop=True)
        up_mask = recent_20["close"] > recent_20["open"]
        down_mask = recent_20["close"] < recent_20["open"]
        up_vol = (
            float(recent_20.loc[up_mask, "volume"].mean()) if up_mask.any() else 0.0
        )
        down_vol = (
            float(recent_20.loc[down_mask, "volume"].mean()) if down_mask.any() else 0.0
        )
        down_up_vol_ratio = down_vol / up_vol if up_vol > 0 else 1.0

        days_since_high = (
            int(len(closes) - 1 - ceiling_idx[-1]) if ceiling_idx else None
        )

        # HV (20d, annualized %) — display-only; never feeds the sell-pressure score
        # (confirmed #57 design decision: HV moved in opposite directions for the
        # two real Climax cases — NFLX 57→28, ESTC 53→64). Matches calculate_recovery_metrics.
        returns_hv = closes.pct_change()
        hv_series = returns_hv.rolling(20).std() * np.sqrt(TRADING_DAYS_PER_YEAR) * 100
        hv_now = (
            round(float(hv_series.iloc[-1]), 1)
            if not pd.isna(hv_series.iloc[-1])
            else None
        )

        return {
            "rsi_ceiling_delta": (
                round(rsi_ceiling_delta, 1) if rsi_ceiling_delta is not None else None
            ),
            "rsi_at_newest_ceiling": (
                round(rsi_at_newest_ceiling, 1)
                if rsi_at_newest_ceiling is not None
                else None
            ),
            "overshoot_decelerating": overshoot_decelerating,
            "post_high_trend": post_high_trend,
            "ext_vs_50ma": round(ext_vs_50ma, 1) if ext_vs_50ma is not None else None,
            "atr14": round(atr14, 2) if atr14 is not None else None,
            "minor_swing_floor": (
                round(minor_swing_floor, 2) if minor_swing_floor is not None else None
            ),
            "gap_closed_pct": (
                round(gap_closed_pct, 1) if gap_closed_pct is not None else None
            ),
            "down_up_vol_ratio": round(down_up_vol_ratio, 2),
            "days_since_high": days_since_high,
            "ceiling_count": len(ceiling_idx),
            "swing_high_count": len(swing_idx),
            "entry_price": entry_price,
            "hv_now": hv_now,
        }

    @staticmethod
    def grade_holding(result: dict) -> tuple[str, int, list[str], Optional[str]]:
        """
        Grade a holding's sell-side posture from plateau metrics + current price.

        Returns (label, sell_pressure_score, signals, stop_suggestion).
        Label precedence: Broken / Climax / Thesis complete / Distribution /
        Cresting / Running. Score measures sell pressure; higher = more reason
        to exit. Running positions score near 0; climax/thesis positions > 30.
        Broken triggers below the newest confirmed swing floor or below the
        entry base (``entry_price`` in the metrics dict) — underwater positions
        read Broken, not Running.
        """
        signals: list[str] = []
        score = 0

        rsi_ceiling_delta = result.get("rsi_ceiling_delta")
        rsi_at_newest_ceiling = result.get("rsi_at_newest_ceiling")
        overshoot_decelerating = result.get("overshoot_decelerating")
        post_high_trend = result.get("post_high_trend")
        ext_vs_50ma = result.get("ext_vs_50ma")
        gap_closed_pct = result.get("gap_closed_pct")
        down_up_vol_ratio = result.get("down_up_vol_ratio", 1.0)
        minor_swing_floor = result.get("minor_swing_floor")
        atr14 = result.get("atr14")
        entry_price = result.get("entry_price")
        current_price = float(result.get("price", 0.0))

        # --- Structure track (sell pressure from metric deterioration) ---
        if rsi_ceiling_delta is not None and rsi_ceiling_delta < 0:
            score += 20
            signals.append("RSI ceiling divergence (sellers absorbing at new highs)")

        if overshoot_decelerating:
            score += 10
            signals.append("Overshoot decelerating (extension narrowing)")

        if post_high_trend == "fading":
            score += 10
            signals.append("Post-high blocks fading (price losing altitude)")

        # --- Climax track (both required; no partial credit for AJG-style flat) ---
        climax_rsi = (
            rsi_at_newest_ceiling is not None and rsi_at_newest_ceiling >= 75.0
        )
        climax_ext = ext_vs_50ma is not None and ext_vs_50ma >= 15.0
        if climax_rsi and climax_ext:
            score += 30
            # Insert first so the distinct "sell into strength" copy leads the
            # Pushover message even when structure signals also fired.
            signals.insert(
                0,
                f"Parabolic — RSI {rsi_at_newest_ceiling:.0f} at run high, "
                f"+{ext_vs_50ma:.0f}% over 50-MA (sell into strength)",
            )

        # --- Thesis gauge (gap-closed scaling into the 88% label threshold) ---
        if gap_closed_pct is not None:
            if gap_closed_pct >= 88.0:
                score += 20
                signals.append(
                    f"Thesis target reached — {gap_closed_pct:.0f}% of pre-entry 52w high"
                )
            elif gap_closed_pct >= 75.0:
                pts = int((gap_closed_pct - 75.0) / (88.0 - 75.0) * 20)
                score += pts

        # --- Volume confirmation (small) ---
        if down_up_vol_ratio > 1.05:
            score += 3
            signals.append(
                f"Distribution vol ({down_up_vol_ratio:.2f}× more volume on down days)"
            )

        # --- Label (precedence order) ---
        # Broken = below structure: under the newest confirmed swing floor OR
        # underwater vs the entry base (positions that never ran get full
        # information, not "—").
        broken_floor = minor_swing_floor is not None and current_price < minor_swing_floor
        broken_entry = entry_price is not None and entry_price > 0 and current_price < entry_price
        label: str
        if broken_floor or broken_entry:
            label = "Broken"
            if broken_floor:
                signals.insert(0, f"Below newest swing floor (${minor_swing_floor:.2f})")
            else:
                signals.insert(0, f"Underwater vs entry base (${entry_price:.2f})")
        elif climax_rsi and climax_ext:
            label = "Climax"
        elif gap_closed_pct is not None and gap_closed_pct >= 88.0:
            label = "Thesis complete"
        elif post_high_trend == "fading" and down_up_vol_ratio > 1.05:
            label = "Distribution"
        elif (rsi_ceiling_delta is not None and rsi_ceiling_delta < 0) or (
            overshoot_decelerating
        ):
            label = "Cresting"
        else:
            label = "Running"

        # --- Stop suggestion: minor floor stop + 1.5× ATR trail ---
        stop_suggestion: Optional[str] = None
        parts: list[str] = []
        if minor_swing_floor is not None and current_price > 0:
            floor_pct = (current_price - minor_swing_floor) / current_price * 100
            if floor_pct <= 12.0:
                parts.append(
                    f"Structure stop <${minor_swing_floor:.2f} ({floor_pct:.0f}% away)"
                )
        if atr14 is not None and current_price > 0:
            trail_price = current_price - 1.5 * atr14
            parts.append(f"Trail ${trail_price:.2f} (1.5× ATR)")
        if parts:
            stop_suggestion = " or ".join(parts)

        return label, score, signals, stop_suggestion
