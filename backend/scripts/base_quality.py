"""Compute base-quality metrics for a ticker as of a given date.

Validates the proposed scanner rescore (issue #49) against known past trades.
Usage: python base_quality.py TICKER YYYY-MM-DD
"""
import json
import os
import sys

import numpy as np
import pandas as pd

# corporate TLS interception breaks python SSL here; data is pre-fetched via curl
FIXTURE_DIR = os.path.join(
    os.path.dirname(__file__), "..", "tests", "fixtures", "scanner"
)

ticker, as_of = sys.argv[1], sys.argv[2]
with open(os.path.join(FIXTURE_DIR, f"{ticker}.json")) as f:
    rows = json.load(f)
if isinstance(rows, dict):
    rows = rows.get("historical", rows)
df = pd.DataFrame(rows)
df["date"] = pd.to_datetime(df["date"])
df = df[df["date"] <= pd.Timestamp(as_of)]
df = df.sort_values("date").reset_index(drop=True).tail(252).reset_index(drop=True)

price = df["close"].iloc[-1]
returns = df["close"].pct_change()

# --- snapshot metrics (what the current scanner sees) ---
high_52w, low_52w = df["high"].max(), df["low"].min()
range_pos = (price - low_52w) / (high_52w - low_52w) * 100
drawdown = (price - high_52w) / high_52w * 100

delta = df["close"].diff()
gain = delta.where(delta > 0, 0.0)
loss = -delta.where(delta < 0, 0.0)
avg_gain = gain.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean().iloc[-1]
avg_loss = loss.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean().iloc[-1]
rsi = 100 - 100 / (1 + avg_gain / avg_loss) if avg_loss else 50.0

hv = returns.rolling(20).std() * np.sqrt(252) * 100
hv_now = hv.iloc[-1]
hv_min, hv_max = hv.min(), hv.max()
hv_rank = (hv_now - hv_min) / (hv_max - hv_min) * 100 if hv_max > hv_min else 50

# --- time-series metrics (the proposed base-quality separators) ---
low_idx = df["low"].idxmin()
days_since_low = len(df) - 1 - low_idx

# retests: post-low days whose low came within 3% of the 52w low
post = df.iloc[low_idx + 1 :]
near = post["low"] <= low_52w * 1.03
retest_episodes = int((near & ~near.shift(fill_value=False)).sum())

# HV decay: current HV vs its peak over the last 90 days
hv_peak_90 = hv.tail(90).max()
hv_decay = hv_now / hv_peak_90 * 100 if hv_peak_90 else 100

# higher lows: 10-day block minima since the low, strictly rising?
blocks = [post["low"].iloc[i : i + 10].min() for i in range(0, len(post), 10)]
blocks = [b for b in blocks if not np.isnan(b)]
higher_lows = all(b2 > b1 for b1, b2 in zip(blocks, blocks[1:])) if len(blocks) >= 2 else None

# reclaim streak: consecutive closes above the 20-day MA
ma20 = df["close"].rolling(20).mean()
above = (df["close"] > ma20).values
streak = 0
for v in above[::-1]:
    if v:
        streak += 1
    else:
        break

ma50 = df["close"].rolling(50).mean()
ma50_slope = (ma50.iloc[-1] - ma50.iloc[-11]) / ma50.iloc[-11] * 100

pct_above_low = (price - low_52w) / low_52w * 100

print(f"=== {ticker} as of {as_of} (close ${price:.2f}) ===")
print(f"-- snapshot (current scanner's view) --")
print(f"drawdown from 52w high : {drawdown:+.1f}%")
print(f"52w range position     : {range_pos:.0f}%")
print(f"RSI(14)                : {rsi:.0f}")
print(f"HV rank                : {hv_rank:.0f}  (current HV {hv_now:.0f}%)")
print(f"-- time series (proposed base-quality view) --")
print(f"age of 52w low         : {days_since_low} trading days")
print(f"price above low        : {pct_above_low:+.1f}%")
print(f"retest episodes held   : {retest_episodes}")
print(f"HV vs 90d peak         : {hv_decay:.0f}% of peak")
print(f"higher 10d lows since  : {higher_lows}  (blocks: {[round(b,2) for b in blocks]})")
print(f"closes above 20-MA     : {streak} consecutive")
print(f"50-MA slope (10d)      : {ma50_slope:+.2f}%")

# swing lows over the trailing ~6 months, with RSI/HV at each (divergence check)
df["rsi_series"] = 100 - 100 / (
    1
    + gain.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    / loss.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
)
recent = df.tail(126)
print(f"-- swing lows (trailing 6mo) --")
last_idx = None
for i in range(len(recent)):
    idx = recent.index[i]
    lo = max(recent.index[0], idx - 7)
    hi = min(df.index[-1], idx + 7)
    win = recent.loc[lo:hi, "low"]
    if recent.loc[idx, "low"] == win.min() and (last_idx is None or idx - last_idx > 10):
        r = df.loc[idx]
        print(
            f"  {r['date'].date()}  low ${r['low']:.2f}  "
            f"RSI {r['rsi_series']:.0f}  HV {hv.loc[idx]:.0f}%"
        )
        last_idx = idx
