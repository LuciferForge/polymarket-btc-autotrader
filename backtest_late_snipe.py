#!/usr/bin/env python3
"""
Backtest: Late Snipe Strategy for Polymarket BTC 15-min Up/Down Markets

Strategy: Wait until minute 13-14 of a 15-min window when direction is nearly certain.
Buy the winning side at ~$0.93-$0.98, collect $1.00 at resolution.

Uses Binance 1-min BTCUSDT candle data (14 days).
"""

import requests
import time
import math
import sys
from datetime import datetime, timezone
from collections import defaultdict

# ─── CONFIG ───
SYMBOL = "BTCUSDT"
INTERVAL = "1m"
DAYS = 14
SHARES_PER_TRADE = 10
BUCKETS = [
    (0.0, 0.05, "0.00-0.05%"),
    (0.05, 0.10, "0.05-0.10%"),
    (0.10, 0.20, "0.10-0.20%"),
    (0.20, 0.50, "0.20-0.50%"),
    (0.50, 999.0, "0.50%+"),
]

# ─── FETCH DATA ───
def fetch_binance_klines(symbol, interval, days):
    """Fetch 1-min candles from Binance. Max 1000 per request, so paginate."""
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - (days * 24 * 60 * 60 * 1000)

    all_candles = []
    current_start = start_ms

    print(f"Fetching {days} days of {interval} candles for {symbol}...")

    while current_start < end_ms:
        url = "https://api.binance.com/api/v3/klines"
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": current_start,
            "endTime": end_ms,
            "limit": 1000,
        }

        resp = requests.get(url, params=params, timeout=30)
        if resp.status_code != 200:
            print(f"Error {resp.status_code}: {resp.text}")
            sys.exit(1)

        data = resp.json()
        if not data:
            break

        all_candles.extend(data)
        current_start = data[-1][0] + 60000  # next minute

        # Rate limit courtesy
        time.sleep(0.2)

    print(f"Fetched {len(all_candles)} candles")
    return all_candles


def parse_candles(raw):
    """Parse raw Binance kline data into dicts."""
    candles = []
    for k in raw:
        candles.append({
            "open_time": k[0],
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
        })
    return candles


def group_into_windows(candles):
    """
    Group 1-min candles into 15-min windows.
    Windows start at :00, :15, :30, :45.
    """
    windows = defaultdict(list)

    for c in candles:
        # Get the minute within the hour
        dt = datetime.fromtimestamp(c["open_time"] / 1000, tz=timezone.utc)
        # Window start: floor to nearest 15 min
        window_minute = (dt.minute // 15) * 15
        window_start = dt.replace(minute=window_minute, second=0, microsecond=0)
        window_key = int(window_start.timestamp() * 1000)

        # Minute index within window (0-14)
        minute_in_window = dt.minute - window_minute

        windows[window_key].append((minute_in_window, c))

    return windows


def estimate_polymarket_price(move_pct, minute, direction):
    """
    Estimate Polymarket YES price for the winning side.
    price = 0.50 + direction * sigmoid(move% * 20) * sqrt(minute/15)
    Capped at [0.01, 0.99].

    direction: +1 if UP side, -1 if DOWN side
    move_pct: absolute percentage move from window open
    """
    sigmoid_val = 2.0 / (1.0 + math.exp(-move_pct * 20)) - 1.0  # maps 0->0, large->~1
    time_factor = math.sqrt(minute / 15.0)
    price = 0.50 + direction * sigmoid_val * 0.50 * time_factor
    return max(0.01, min(0.99, price))


def run_backtest():
    # Fetch and parse
    raw = fetch_binance_klines(SYMBOL, INTERVAL, DAYS)
    candles = parse_candles(raw)
    windows = group_into_windows(candles)

    # Only use complete windows (15 candles)
    complete_windows = {k: v for k, v in windows.items() if len(v) == 15}
    print(f"Complete 15-min windows: {len(complete_windows)}")
    print()

    # ─── ANALYSIS ───
    # For each window, determine:
    # - Window open price (minute 0 open)
    # - Window close price (minute 14 close) → actual resolution
    # - At minute 13 and 14: current price, move from open, direction signal

    results = {13: [], 14: []}
    consistency_results = {13: [], 14: []}

    for wkey in sorted(complete_windows.keys()):
        w = sorted(complete_windows[wkey], key=lambda x: x[0])

        # Window open = minute 0 open price
        window_open = w[0][1]["open"]
        # Window close = minute 14 close price
        window_close = w[14][1]["close"]

        # Actual direction: UP if close > open, DOWN otherwise
        actual_direction = "UP" if window_close >= window_open else "DOWN"
        actual_move_pct = abs(window_close - window_open) / window_open * 100

        # Check consistency from minute 10 onwards
        # Direction at each minute from 10-14
        directions_from_10 = []
        for min_idx, candle in w:
            if min_idx >= 10:
                current_close = candle["close"]
                d = "UP" if current_close >= window_open else "DOWN"
                directions_from_10.append(d)

        for check_minute in [13, 14]:
            # Find the candle at this minute
            check_candle = None
            for min_idx, candle in w:
                if min_idx == check_minute:
                    check_candle = candle
                    break

            if check_candle is None:
                continue

            current_price = check_candle["close"]
            move_from_open = (current_price - window_open) / window_open * 100
            abs_move = abs(move_from_open)
            signal_direction = "UP" if move_from_open >= 0 else "DOWN"

            # Did signal match actual?
            correct = signal_direction == actual_direction

            # Estimate entry price (buying the winning side based on our signal)
            # If we think UP, we buy YES-UP at estimated price
            entry_price = estimate_polymarket_price(abs_move, check_minute, 1)  # always buying the side we predict

            results[check_minute].append({
                "abs_move": abs_move,
                "correct": correct,
                "entry_price": entry_price,
                "signal_direction": signal_direction,
                "actual_direction": actual_direction,
                "window_time": datetime.fromtimestamp(wkey / 1000, tz=timezone.utc),
            })

            # Consistency check: direction consistent from minute 10 through check_minute
            # directions_from_10 has indices for minutes 10,11,12,13,14
            # For minute 13: check minutes 10,11,12,13 (indices 0-3)
            # For minute 14: check minutes 10,11,12,13,14 (indices 0-4)
            end_idx = check_minute - 10 + 1  # how many minutes from 10 to include
            relevant_dirs = directions_from_10[:end_idx]
            consistent = len(set(relevant_dirs)) == 1  # all same direction

            consistency_results[check_minute].append({
                "abs_move": abs_move,
                "correct": correct,
                "entry_price": entry_price,
                "consistent": consistent,
                "signal_direction": signal_direction,
            })

    # ─── REPORT ───
    print("=" * 90)
    print("LATE SNIPE BACKTEST — BTC 15-MIN UP/DOWN MARKETS")
    print(f"Data: {DAYS} days of 1-min BTCUSDT candles from Binance")
    print(f"Windows analyzed: {len(complete_windows)}")
    print("=" * 90)

    for check_minute in [13, 14]:
        print(f"\n{'─' * 90}")
        print(f"ENTRY AT MINUTE {check_minute}")
        print(f"{'─' * 90}")

        data = results[check_minute]

        # Overall stats
        total = len(data)
        wins = sum(1 for d in data if d["correct"])
        overall_wr = wins / total * 100 if total else 0

        print(f"Total trades: {total}  |  Wins: {wins}  |  Win rate: {overall_wr:.1f}%")
        print()

        # By bucket
        header = f"{'Move Bucket':<14} {'Trades':>7} {'Wins':>6} {'WinRate':>8} {'Avg Entry':>10} {'Avg Profit':>11} {'Net P&L':>10} {'P&L/Trade':>10}"
        print(header)
        print("-" * len(header))

        for low, high, label in BUCKETS:
            bucket_data = [d for d in data if low <= d["abs_move"] < high]
            if not bucket_data:
                print(f"{label:<14} {'--':>7}")
                continue

            n = len(bucket_data)
            bwins = sum(1 for d in bucket_data if d["correct"])
            wr = bwins / n * 100
            avg_entry = sum(d["entry_price"] for d in bucket_data) / n

            # P&L calculation
            total_pnl = 0
            for d in bucket_data:
                if d["correct"]:
                    profit = (1.0 - d["entry_price"]) * SHARES_PER_TRADE
                else:
                    profit = -d["entry_price"] * SHARES_PER_TRADE
                total_pnl += profit

            pnl_per_trade = total_pnl / n

            print(f"{label:<14} {n:>7} {bwins:>6} {wr:>7.1f}% ${avg_entry:>8.4f} ${pnl_per_trade:>9.4f} ${total_pnl:>8.2f} ${pnl_per_trade:>8.4f}")

        # ─── CONSISTENCY FILTER ───
        print(f"\n  CONSISTENCY FILTER (direction stable since minute 10):")
        cons_data = consistency_results[check_minute]
        cons_filtered = [d for d in cons_data if d["consistent"]]

        if cons_filtered:
            cons_total = len(cons_filtered)
            cons_wins = sum(1 for d in cons_filtered if d["correct"])
            cons_wr = cons_wins / cons_total * 100
            print(f"  Trades passing filter: {cons_total}/{len(cons_data)} ({cons_total/len(cons_data)*100:.0f}%)")
            print(f"  Win rate with filter: {cons_wr:.1f}% (vs {overall_wr:.1f}% without)")

            # By bucket with consistency filter
            print()
            print(f"  {'Move Bucket':<14} {'Trades':>7} {'Wins':>6} {'WinRate':>8} {'Avg Entry':>10} {'Net P&L':>10} {'P&L/Trade':>10}")
            print(f"  {'-'*75}")

            for low, high, label in BUCKETS:
                bucket_data = [d for d in cons_filtered if low <= d["abs_move"] < high]
                if not bucket_data:
                    print(f"  {label:<14} {'--':>7}")
                    continue

                n = len(bucket_data)
                bwins = sum(1 for d in bucket_data if d["correct"])
                wr = bwins / n * 100
                avg_entry = sum(d["entry_price"] for d in bucket_data) / n

                total_pnl = 0
                for d in bucket_data:
                    if d["correct"]:
                        profit = (1.0 - d["entry_price"]) * SHARES_PER_TRADE
                    else:
                        profit = -d["entry_price"] * SHARES_PER_TRADE
                    total_pnl += profit

                pnl_per_trade = total_pnl / n

                print(f"  {label:<14} {n:>7} {bwins:>6} {wr:>7.1f}% ${avg_entry:>8.4f} ${total_pnl:>8.2f} ${pnl_per_trade:>8.4f}")

    # ─── BREAKEVEN ANALYSIS ───
    print(f"\n{'=' * 90}")
    print("BREAKEVEN ANALYSIS")
    print(f"{'=' * 90}")
    print()
    print("At what entry price does the strategy break even, given observed win rates?")
    print("Breakeven entry = win_rate / 1.0  (since payout is $1.00)")
    print("Profitable if: entry_price < win_rate")
    print()

    for check_minute in [13, 14]:
        print(f"Minute {check_minute}:")
        data = results[check_minute]

        for low, high, label in BUCKETS:
            bucket_data = [d for d in data if low <= d["abs_move"] < high]
            if not bucket_data:
                continue

            n = len(bucket_data)
            bwins = sum(1 for d in bucket_data if d["correct"])
            wr = bwins / n
            avg_entry = sum(d["entry_price"] for d in bucket_data) / n

            margin = wr - avg_entry
            status = "PROFITABLE" if margin > 0 else "UNPROFITABLE"

            print(f"  {label:<14} WR={wr:.3f}  AvgEntry=${avg_entry:.4f}  Margin={margin:+.4f}  → {status}")

            # Example: what if entry was $0.95?
            for test_entry in [0.93, 0.95, 0.97]:
                pnl = wr * (1.0 - test_entry) * SHARES_PER_TRADE - (1 - wr) * test_entry * SHARES_PER_TRADE
                print(f"    @${test_entry}: P&L/trade = ${pnl:.4f}  ({'+' if pnl > 0 else ''}{pnl/test_entry/SHARES_PER_TRADE*100:.1f}% ROI)")

        print()

    # ─── REVERSAL ANALYSIS ───
    print(f"{'=' * 90}")
    print("REVERSAL RISK — How often does direction flip in final 1-2 minutes?")
    print(f"{'=' * 90}")
    print()

    for check_minute in [13, 14]:
        data = results[check_minute]
        flips = [d for d in data if not d["correct"]]

        print(f"Minute {check_minute}: {len(flips)} flips out of {len(data)} trades ({len(flips)/len(data)*100:.1f}%)")

        if flips:
            # Show some examples
            print(f"  Flip examples (showing first 5):")
            for f in flips[:5]:
                print(f"    {f['window_time'].strftime('%Y-%m-%d %H:%M')} UTC  "
                      f"Signal={f['signal_direction']}  Actual={f['actual_direction']}  "
                      f"Move={f['abs_move']:.3f}%  Entry=${f['entry_price']:.4f}")
        print()

    # ─── SUMMARY ───
    print(f"{'=' * 90}")
    print("KEY TAKEAWAYS")
    print(f"{'=' * 90}")

    for cm in [13, 14]:
        data = results[cm]
        total = len(data)
        wins = sum(1 for d in data if d["correct"])
        wr = wins / total

        # Best bucket
        best_label = ""
        best_margin = -999
        for low, high, label in BUCKETS:
            bd = [d for d in data if low <= d["abs_move"] < high]
            if not bd:
                continue
            bwr = sum(1 for d in bd if d["correct"]) / len(bd)
            bavg = sum(d["entry_price"] for d in bd) / len(bd)
            margin = bwr - bavg
            if margin > best_margin:
                best_margin = margin
                best_label = label

        print(f"\nMinute {cm}: Overall WR={wr:.1%}, Best bucket={best_label} (margin={best_margin:+.4f})")

    # Consistency impact
    for cm in [13, 14]:
        cd = consistency_results[cm]
        cf = [d for d in cd if d["consistent"]]
        if cf:
            base_wr = sum(1 for d in cd if d["correct"]) / len(cd)
            filt_wr = sum(1 for d in cf if d["correct"]) / len(cf)
            print(f"Minute {cm} consistency filter: WR {base_wr:.1%} → {filt_wr:.1%} (kept {len(cf)}/{len(cd)} trades)")

    print()


if __name__ == "__main__":
    run_backtest()
