#!/usr/bin/env python3
"""
Oracle Lag Snipe Backtest
=========================
Polymarket BTC 15-min markets resolve via Chainlink oracle on Polygon,
which lags Binance by ~28-30 seconds. This backtest simulates:

1. Binance 1-min candles as "real-time" price
2. Chainlink as Binance delayed by 30 seconds (simulated via prior candle close)
3. Entry signals when Binance moves >X% in 1 minute but Chainlink hasn't caught up
4. P&L from both scalp (2-min hold) and resolution (hold to 15-min window end)

Uses Binance public API for BTCUSDT 1m candles, 14 days.
"""

import requests
import time
import sys
from datetime import datetime, timezone
from collections import defaultdict

# ─── CONFIG ───────────────────────────────────────────────────────────────────
SYMBOL = "BTCUSDT"
INTERVAL = "1m"
DAYS = 14
THRESHOLDS = [0.05, 0.10, 0.15, 0.20]  # % move in 1 minute
ENTRY_WINDOWS = [(1, 5), (5, 10), (10, 14)]  # minutes within 15-min window
ORACLE_LAG_CANDLES = 1  # 30s lag ~ 1 candle behind at 1m resolution
SCALP_HOLD_MINUTES = 2
POLYMARKET_FEE = 0.02  # 2% fee on profit (Polymarket takes 2% of winnings)
POSITION_SIZE = 100  # $100 per trade for P&L calc

# ─── FETCH DATA ───────────────────────────────────────────────────────────────
def fetch_binance_klines(symbol, interval, days):
    """Fetch 1m klines from Binance. Returns list of candles."""
    url = "https://api.binance.com/api/v3/klines"
    all_klines = []

    end_ms = int(time.time() * 1000)
    start_ms = end_ms - (days * 24 * 60 * 60 * 1000)

    print(f"Fetching {days} days of {interval} candles for {symbol}...")

    current_start = start_ms
    batch = 0
    while current_start < end_ms:
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": current_start,
            "limit": 1000
        }

        resp = requests.get(url, params=params, timeout=30)
        if resp.status_code == 429:
            print("  Rate limited, waiting 10s...")
            time.sleep(10)
            continue
        resp.raise_for_status()
        data = resp.json()

        if not data:
            break

        all_klines.extend(data)
        current_start = data[-1][0] + 60000  # next candle
        batch += 1

        if batch % 5 == 0:
            print(f"  Fetched {len(all_klines)} candles...")

        time.sleep(0.1)  # rate limit courtesy

    print(f"  Total: {len(all_klines)} candles")
    return all_klines


def parse_klines(raw):
    """Parse raw klines into list of dicts."""
    candles = []
    for k in raw:
        candles.append({
            "open_time": k[0],
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
            "close_time": k[6],
        })
    return candles


# ─── SIMULATION ───────────────────────────────────────────────────────────────
def assign_15min_window(candle_time_ms):
    """Given a candle open time (ms), return the 15-min window start and minute index (0-14)."""
    ts_sec = candle_time_ms // 1000
    dt = datetime.fromtimestamp(ts_sec, tz=timezone.utc)
    minute_of_hour = dt.minute
    window_start_minute = (minute_of_hour // 15) * 15
    minute_in_window = minute_of_hour - window_start_minute

    window_start_ts = dt.replace(minute=window_start_minute, second=0, microsecond=0)
    window_key = int(window_start_ts.timestamp() * 1000)

    return window_key, minute_in_window


def run_backtest(candles):
    """Run the oracle lag snipe backtest."""

    # Group candles by 15-min window
    windows = defaultdict(list)
    for c in candles:
        wk, min_idx = assign_15min_window(c["open_time"])
        c["minute_in_window"] = min_idx
        c["window_key"] = wk
        windows[wk].append(c)

    # Only keep complete windows (15 candles)
    complete_windows = {k: sorted(v, key=lambda x: x["open_time"])
                        for k, v in windows.items() if len(v) >= 14}

    print(f"\nComplete 15-min windows: {len(complete_windows)}")
    print(f"Date range: {datetime.fromtimestamp(min(complete_windows.keys())//1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')} to {datetime.fromtimestamp(max(complete_windows.keys())//1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')}")

    # Build candle index for quick lookup
    candle_by_time = {c["open_time"]: c for c in candles}
    candle_list = sorted(candles, key=lambda x: x["open_time"])
    candle_idx = {c["open_time"]: i for i, c in enumerate(candle_list)}

    # Results storage: (threshold, window_range) -> list of trades
    results = defaultdict(list)

    for wk in sorted(complete_windows.keys()):
        window_candles = complete_windows[wk]

        # Window resolution: based on close of last candle vs open of first
        window_open = window_candles[0]["open"]
        # Resolution price = close of final candle in window
        window_close = window_candles[-1]["close"]
        window_up = window_close >= window_open

        for candle in window_candles:
            min_idx = candle["minute_in_window"]
            ci = candle_idx.get(candle["open_time"])
            if ci is None or ci < ORACLE_LAG_CANDLES:
                continue

            # Binance "real-time" price = this candle's close
            binance_now = candle["close"]
            binance_prev = candle["open"]  # 1 minute ago = this candle's open

            # Chainlink "delayed" price = previous candle's close (30s lag approximation)
            oracle_candle = candle_list[ci - ORACLE_LAG_CANDLES]
            chainlink_price = oracle_candle["close"]

            # 1-minute move on Binance
            if binance_prev == 0:
                continue
            binance_1m_move_pct = ((binance_now - binance_prev) / binance_prev) * 100
            binance_1m_move_abs = abs(binance_1m_move_pct)

            # Direction of the move
            direction = "UP" if binance_1m_move_pct > 0 else "DOWN"

            # Gap between Binance real-time and what Chainlink shows
            if chainlink_price == 0:
                continue
            gap_pct = ((binance_now - chainlink_price) / chainlink_price) * 100
            gap_abs = abs(gap_pct)

            # For each threshold, check if this is an entry signal
            for thresh in THRESHOLDS:
                if binance_1m_move_abs < thresh:
                    continue

                # Must have a meaningful gap (oracle hasn't caught up)
                # The gap should be in the SAME direction as the move
                if direction == "UP" and gap_pct <= 0.01:
                    continue
                if direction == "DOWN" and gap_pct >= -0.01:
                    continue

                # Determine entry window bucket
                for w_start, w_end in ENTRY_WINDOWS:
                    if w_start <= min_idx < w_end:
                        break
                else:
                    continue  # minute 0 or 14+ excluded

                # ── POLYMARKET PRICING MODEL ──
                # If Chainlink shows price hasn't moved much yet,
                # Polymarket "Up" token is still near 50/50 pricing.
                # We buy the direction token at the "stale" implied price.

                # Stale implied probability (simplified):
                # Based on how far chainlink price is from window open
                minutes_left = 14 - min_idx
                if minutes_left <= 0:
                    continue

                chainlink_vs_open_pct = ((chainlink_price - window_open) / window_open) * 100
                binance_vs_open_pct = ((binance_now - window_open) / window_open) * 100

                # Rough implied probability model:
                # At 50/50, token costs $0.50. Each 0.01% move from open shifts by ~2c.
                # This is a simplification but captures the mechanics.
                stale_prob = 0.50 + chainlink_vs_open_pct * 20  # ~2c per 0.001%
                stale_prob = max(0.05, min(0.95, stale_prob))

                real_prob = 0.50 + binance_vs_open_pct * 20
                real_prob = max(0.05, min(0.95, real_prob))

                if direction == "UP":
                    entry_price = stale_prob  # buy UP token at stale (cheap) price
                    fair_price = real_prob    # it should be worth this
                else:
                    entry_price = 1 - stale_prob  # buy DOWN token at stale price
                    fair_price = 1 - real_prob

                # Edge = fair - entry (we're buying below fair value)
                edge = fair_price - entry_price
                if edge <= 0:
                    continue  # no edge, skip

                # ── SCALP P&L (2-min hold) ──
                scalp_exit_idx = ci + SCALP_HOLD_MINUTES
                if scalp_exit_idx < len(candle_list):
                    scalp_candle = candle_list[scalp_exit_idx]
                    scalp_price = scalp_candle["close"]
                    scalp_vs_open_pct = ((scalp_price - window_open) / window_open) * 100
                    scalp_prob = 0.50 + scalp_vs_open_pct * 20
                    scalp_prob = max(0.05, min(0.95, scalp_prob))

                    if direction == "UP":
                        scalp_exit_price = scalp_prob
                    else:
                        scalp_exit_price = 1 - scalp_prob

                    scalp_pnl = (scalp_exit_price - entry_price) * POSITION_SIZE
                else:
                    scalp_pnl = 0
                    scalp_exit_price = entry_price

                # ── RESOLUTION P&L ──
                if direction == "UP":
                    resolution_payout = 1.0 if window_up else 0.0
                else:
                    resolution_payout = 1.0 if not window_up else 0.0

                resolution_pnl = (resolution_payout - entry_price) * POSITION_SIZE
                # Apply Polymarket fee on profit only
                if resolution_pnl > 0:
                    resolution_pnl *= (1 - POLYMARKET_FEE)

                trade = {
                    "timestamp": candle["open_time"],
                    "minute_in_window": min_idx,
                    "direction": direction,
                    "binance_1m_move": binance_1m_move_pct,
                    "gap_pct": gap_pct,
                    "entry_price": entry_price,
                    "fair_price": fair_price,
                    "edge": edge,
                    "scalp_exit_price": scalp_exit_price,
                    "scalp_pnl": scalp_pnl,
                    "resolution_payout": resolution_payout,
                    "resolution_pnl": resolution_pnl,
                    "resolution_win": resolution_payout == 1.0,
                }

                results[(thresh, (w_start, w_end))].append(trade)

    return results


def print_results(results):
    """Print formatted results table."""

    print("\n" + "=" * 130)
    print("ORACLE LAG SNIPE BACKTEST RESULTS")
    print("=" * 130)
    print(f"{'Threshold':>10} | {'Window':>8} | {'Signals':>8} | {'Avg Gap':>8} | {'Avg Edge':>9} | {'Res WR%':>8} | {'Avg Res P&L':>12} | {'Avg Scalp':>10} | {'Tot Res P&L':>12} | {'Tot Scalp':>10}")
    print("-" * 130)

    # Collect all for summary
    all_trades_summary = []

    for thresh in THRESHOLDS:
        for w_start, w_end in ENTRY_WINDOWS:
            key = (thresh, (w_start, w_end))
            trades = results.get(key, [])

            if not trades:
                print(f"{thresh:>9.2f}% | {f'm{w_start}-{w_end}':>8} | {'0':>8} |      --- |       --- |      --- |          --- |        --- |          --- |        ---")
                continue

            n = len(trades)
            avg_gap = sum(abs(t["gap_pct"]) for t in trades) / n
            avg_edge = sum(t["edge"] for t in trades) / n
            wins = sum(1 for t in trades if t["resolution_win"])
            win_rate = (wins / n) * 100
            avg_res_pnl = sum(t["resolution_pnl"] for t in trades) / n
            avg_scalp_pnl = sum(t["scalp_pnl"] for t in trades) / n
            tot_res_pnl = sum(t["resolution_pnl"] for t in trades)
            tot_scalp_pnl = sum(t["scalp_pnl"] for t in trades)

            all_trades_summary.append({
                "thresh": thresh, "window": f"m{w_start}-{w_end}",
                "n": n, "win_rate": win_rate, "tot_res": tot_res_pnl, "tot_scalp": tot_scalp_pnl
            })

            print(f"{thresh:>9.2f}% | {f'm{w_start}-{w_end}':>8} | {n:>8} | {avg_gap:>7.4f}% | ${avg_edge:>7.4f} | {win_rate:>7.1f}% | ${avg_res_pnl:>10.2f} | ${avg_scalp_pnl:>8.2f} | ${tot_res_pnl:>10.2f} | ${tot_scalp_pnl:>8.2f}")

    print("=" * 130)

    # Best combos
    if all_trades_summary:
        print("\n--- TOP 5 BY TOTAL RESOLUTION P&L ---")
        by_res = sorted(all_trades_summary, key=lambda x: x["tot_res"], reverse=True)[:5]
        for r in by_res:
            print(f"  {r['thresh']:.2f}% / {r['window']}: {r['n']} signals, {r['win_rate']:.1f}% WR, Res P&L=${r['tot_res']:.2f}, Scalp P&L=${r['tot_scalp']:.2f}")

        print("\n--- TOP 5 BY TOTAL SCALP P&L ---")
        by_scalp = sorted(all_trades_summary, key=lambda x: x["tot_scalp"], reverse=True)[:5]
        for r in by_scalp:
            print(f"  {r['thresh']:.2f}% / {r['window']}: {r['n']} signals, Scalp P&L=${r['tot_scalp']:.2f}, Res P&L=${r['tot_res']:.2f}")

        print("\n--- BEST WIN RATE (min 10 signals) ---")
        filtered = [x for x in all_trades_summary if x["n"] >= 10]
        if filtered:
            by_wr = sorted(filtered, key=lambda x: x["win_rate"], reverse=True)[:5]
            for r in by_wr:
                print(f"  {r['thresh']:.2f}% / {r['window']}: {r['n']} signals, {r['win_rate']:.1f}% WR, Res P&L=${r['tot_res']:.2f}")

    # Overall summary
    all_trades = []
    for v in results.values():
        all_trades.extend(v)

    if all_trades:
        print(f"\n--- OVERALL STATS (0.10% threshold, all windows combined) ---")
        t10 = [t for t in all_trades if abs(t["binance_1m_move"]) >= 0.10]
        if t10:
            n = len(t10)
            avg_edge = sum(t["edge"] for t in t10) / n
            wins = sum(1 for t in t10 if t["resolution_win"])
            print(f"  Total signals: {n}")
            print(f"  Avg edge (entry discount): ${avg_edge:.4f}")
            print(f"  Resolution win rate: {(wins/n)*100:.1f}%")
            print(f"  Total resolution P&L: ${sum(t['resolution_pnl'] for t in t10):.2f}")
            print(f"  Total scalp P&L: ${sum(t['scalp_pnl'] for t in t10):.2f}")
            print(f"  Avg resolution P&L per trade: ${sum(t['resolution_pnl'] for t in t10)/n:.2f}")
            print(f"  Avg scalp P&L per trade: ${sum(t['scalp_pnl'] for t in t10)/n:.2f}")

    print()


# ─── MAIN ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    raw = fetch_binance_klines(SYMBOL, INTERVAL, DAYS)
    if not raw:
        print("ERROR: No data fetched from Binance")
        sys.exit(1)

    candles = parse_klines(raw)
    print(f"Parsed {len(candles)} candles")
    print(f"Price range: ${min(c['low'] for c in candles):,.0f} - ${max(c['high'] for c in candles):,.0f}")

    results = run_backtest(candles)
    print_results(results)
