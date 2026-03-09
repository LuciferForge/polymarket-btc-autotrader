#!/usr/bin/env python3
"""
Backtest: Volatility Filter + Momentum for Polymarket BTC 15-min Up/Down markets.

Strategy: Buy momentum direction when BTC moves >0.20% from window open (minute 3-12).
Hypothesis: Filtering for HIGH pre-window volatility increases win rate.

Data: 14 days of Binance 1-min BTCUSDT candles.
"""

import requests
import time
import numpy as np
from datetime import datetime, timezone
from collections import defaultdict

# ─── CONFIG ───────────────────────────────────────────────────────────────
SYMBOL = "BTCUSDT"
INTERVAL = "1m"
DAYS = 14
MOMENTUM_THRESHOLD = 0.0020  # 0.20%
ENTRY_WINDOW = (3, 12)  # minutes 3-12 within each 15-min window
WINDOW_SIZE = 15  # 15-minute windows


def fetch_candles(symbol, interval, days):
    """Fetch 1-min candles from Binance. Returns list of dicts."""
    url = "https://api.binance.com/api/v3/klines"
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - (days * 24 * 60 * 60 * 1000)

    all_candles = []
    current_start = start_ms

    while current_start < end_ms:
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": current_start,
            "endTime": end_ms,
            "limit": 1000,
        }
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        if not data:
            break

        for k in data:
            all_candles.append({
                "open_time": k[0],
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
                "close_time": k[6],
            })

        current_start = data[-1][0] + 1  # next ms after last candle
        time.sleep(0.1)  # rate limit

    print(f"Fetched {len(all_candles)} candles ({days} days)")
    return all_candles


def group_into_windows(candles, window_size=15):
    """Group 1-min candles into 15-min windows aligned to clock."""
    windows = defaultdict(list)
    for c in candles:
        # Align to 15-min boundary
        ts_sec = c["open_time"] // 1000
        dt = datetime.fromtimestamp(ts_sec, tz=timezone.utc)
        minute_of_day = dt.hour * 60 + dt.minute
        window_start_min = (minute_of_day // window_size) * window_size
        window_key = dt.replace(minute=window_start_min % 60, hour=(window_start_min // 60), second=0, microsecond=0)
        # Adjust date if needed
        window_ts = int(window_key.timestamp())
        windows[window_ts].append(c)

    # Only keep complete windows
    complete = {k: v for k, v in windows.items() if len(v) == window_size}
    print(f"Complete 15-min windows: {len(complete)}")
    return complete


def compute_1min_returns(candles):
    """Compute 1-min close-to-close returns for all candles."""
    returns = {}
    sorted_candles = sorted(candles, key=lambda c: c["open_time"])
    for i in range(1, len(sorted_candles)):
        prev_close = sorted_candles[i-1]["close"]
        curr_close = sorted_candles[i]["close"]
        ret = (curr_close - prev_close) / prev_close
        returns[sorted_candles[i]["open_time"]] = ret
    return returns, sorted_candles


def analyze_windows(candles, windows):
    """Analyze each window for momentum signal, win/loss, and pre-window volatility."""
    # Sort all candles by time for lookback
    sorted_candles = sorted(candles, key=lambda c: c["open_time"])
    candle_by_time = {c["open_time"]: c for c in sorted_candles}

    # Compute 1-min returns
    one_min_returns = []
    for i in range(1, len(sorted_candles)):
        prev_close = sorted_candles[i-1]["close"]
        curr_close = sorted_candles[i]["close"]
        ret = (curr_close - prev_close) / prev_close
        one_min_returns.append((sorted_candles[i]["open_time"], ret))
    returns_by_time = {t: r for t, r in one_min_returns}

    # Sort window keys
    sorted_window_keys = sorted(windows.keys())

    results = []

    for i, wk in enumerate(sorted_window_keys):
        w_candles = sorted(windows[wk], key=lambda c: c["open_time"])
        window_open = w_candles[0]["open"]
        window_close = w_candles[-1]["close"]

        dt = datetime.fromtimestamp(wk, tz=timezone.utc)
        hour = dt.hour

        # ── Momentum signal: check minutes 3-12 ──
        signal = None
        signal_minute = None
        for j in range(ENTRY_WINDOW[0], min(ENTRY_WINDOW[1] + 1, len(w_candles))):
            price_at_j = w_candles[j]["close"]
            move = (price_at_j - window_open) / window_open
            if abs(move) >= MOMENTUM_THRESHOLD:
                signal = "UP" if move > 0 else "DOWN"
                signal_minute = j
                break

        if signal is None:
            continue  # No signal this window

        # ── Win/Loss ──
        final_move = (window_close - window_open) / window_open
        if final_move == 0:
            continue  # Push, skip

        actual_direction = "UP" if final_move > 0 else "DOWN"
        win = signal == actual_direction

        # ── Pre-window volatility: ATR of previous 4 windows (1 hour) ──
        prev_window_ranges = []
        for pi in range(max(0, i - 4), i):
            pk = sorted_window_keys[pi]
            pw_candles = windows[pk]
            pw_high = max(c["high"] for c in pw_candles)
            pw_low = min(c["low"] for c in pw_candles)
            pw_open = sorted(pw_candles, key=lambda c: c["open_time"])[0]["open"]
            prev_window_ranges.append((pw_high - pw_low) / pw_open)  # normalized range

        atr_vol = np.mean(prev_window_ranges) if prev_window_ranges else 0

        # ── Pre-window volatility: stddev of 1-min returns over previous 60 minutes ──
        window_open_time = w_candles[0]["open_time"]
        lookback_start = window_open_time - 60 * 60 * 1000  # 60 min back

        prev_returns = []
        for t, r in one_min_returns:
            if lookback_start <= t < window_open_time:
                prev_returns.append(r)

        ret_std = np.std(prev_returns) if len(prev_returns) > 10 else 0

        results.append({
            "window_time": dt,
            "hour": hour,
            "signal": signal,
            "signal_minute": signal_minute,
            "win": win,
            "final_move_pct": final_move * 100,
            "atr_vol": atr_vol,
            "ret_std": ret_std,
            "window_open": window_open,
        })

    return results


def pnl_per_trade(win, entry_price, shares=10):
    """P&L for a single trade. Win pays $1/share, loss pays $0."""
    if win:
        return (1.0 - entry_price) * shares
    else:
        return -entry_price * shares


def print_table(headers, rows, col_widths=None):
    """Print a formatted table."""
    if col_widths is None:
        col_widths = [max(len(str(h)), max((len(str(r[i])) for r in rows), default=0)) + 2 for i, h in enumerate(headers)]

    header_line = "".join(str(h).ljust(w) for h, w in zip(headers, col_widths))
    print(header_line)
    print("-" * sum(col_widths))
    for row in rows:
        print("".join(str(v).ljust(w) for v, w in zip(row, col_widths)))


def main():
    print("=" * 70)
    print("POLYMARKET BTC 15-MIN MOMENTUM + VOLATILITY FILTER BACKTEST")
    print(f"Period: {DAYS} days | Threshold: {MOMENTUM_THRESHOLD*100:.2f}% | Entry: min {ENTRY_WINDOW[0]}-{ENTRY_WINDOW[1]}")
    print("=" * 70)
    print()

    # 1. Fetch data
    candles = fetch_candles(SYMBOL, INTERVAL, DAYS)
    if not candles:
        print("ERROR: No candles fetched")
        return

    # 2. Group into windows
    windows = group_into_windows(candles, WINDOW_SIZE)

    # 3. Analyze
    results = analyze_windows(candles, windows)
    print(f"Total momentum signals: {len(results)}")

    if not results:
        print("No signals found.")
        return

    # ── BASELINE ──
    wins = sum(1 for r in results if r["win"])
    total = len(results)
    wr = wins / total * 100
    pnl_80 = sum(pnl_per_trade(r["win"], 0.80) for r in results)
    pnl_75 = sum(pnl_per_trade(r["win"], 0.75) for r in results)

    print(f"\n{'='*70}")
    print("BASELINE (all signals, no filter)")
    print(f"{'='*70}")
    print(f"Signals: {total} | Wins: {wins} | Win Rate: {wr:.1f}%")
    print(f"P&L @ $0.80 entry (10 shares): ${pnl_80:+.2f}")
    print(f"P&L @ $0.75 entry (10 shares): ${pnl_75:+.2f}")

    # ── 4/5. VOLATILITY QUARTILE ANALYSIS ──
    # Using ATR-based vol
    atr_vals = [r["atr_vol"] for r in results]
    q25, q50, q75 = np.percentile(atr_vals, [25, 50, 75])

    def atr_quartile(v):
        if v <= q25: return "Q1 (Low)"
        elif v <= q50: return "Q2 (Med)"
        elif v <= q75: return "Q3 (High)"
        else: return "Q4 (VHigh)"

    for r in results:
        r["atr_q"] = atr_quartile(r["atr_vol"])

    # Using stddev-based vol
    std_vals = [r["ret_std"] for r in results]
    sq25, sq50, sq75 = np.percentile(std_vals, [25, 50, 75])

    def std_quartile(v):
        if v <= sq25: return "Q1 (Low)"
        elif v <= sq50: return "Q2 (Med)"
        elif v <= sq75: return "Q3 (High)"
        else: return "Q4 (VHigh)"

    for r in results:
        r["std_q"] = std_quartile(r["ret_std"])

    print(f"\n{'='*70}")
    print("VOLATILITY QUARTILE ANALYSIS (ATR of prev 4 windows)")
    print(f"Quartile thresholds: Q1<={q25*100:.4f}% | Q2<={q50*100:.4f}% | Q3<={q75*100:.4f}%")
    print(f"{'='*70}")

    headers = ["Quartile", "Signals", "Wins", "Win%", "P&L@0.80", "P&L@0.75", "Avg ATR%"]
    rows = []
    for q_name in ["Q1 (Low)", "Q2 (Med)", "Q3 (High)", "Q4 (VHigh)"]:
        q_results = [r for r in results if r["atr_q"] == q_name]
        if not q_results:
            continue
        q_wins = sum(1 for r in q_results if r["win"])
        q_total = len(q_results)
        q_wr = q_wins / q_total * 100
        q_pnl80 = sum(pnl_per_trade(r["win"], 0.80) for r in q_results)
        q_pnl75 = sum(pnl_per_trade(r["win"], 0.75) for r in q_results)
        q_avg_atr = np.mean([r["atr_vol"] for r in q_results]) * 100
        rows.append([q_name, q_total, q_wins, f"{q_wr:.1f}%", f"${q_pnl80:+.2f}", f"${q_pnl75:+.2f}", f"{q_avg_atr:.4f}%"])

    print_table(headers, rows, [14, 10, 8, 8, 12, 12, 12])

    print(f"\n{'='*70}")
    print("VOLATILITY QUARTILE ANALYSIS (StdDev of prev 60-min returns)")
    print(f"Quartile thresholds: Q1<={sq25*10000:.2f}bps | Q2<={sq50*10000:.2f}bps | Q3<={sq75*10000:.2f}bps")
    print(f"{'='*70}")

    rows = []
    for q_name in ["Q1 (Low)", "Q2 (Med)", "Q3 (High)", "Q4 (VHigh)"]:
        q_results = [r for r in results if r["std_q"] == q_name]
        if not q_results:
            continue
        q_wins = sum(1 for r in q_results if r["win"])
        q_total = len(q_results)
        q_wr = q_wins / q_total * 100
        q_pnl80 = sum(pnl_per_trade(r["win"], 0.80) for r in q_results)
        q_pnl75 = sum(pnl_per_trade(r["win"], 0.75) for r in q_results)
        q_avg_std = np.mean([r["ret_std"] for r in q_results]) * 10000
        rows.append([q_name, q_total, q_wins, f"{q_wr:.1f}%", f"${q_pnl80:+.2f}", f"${q_pnl75:+.2f}", f"{q_avg_std:.2f}bps"])

    headers[-1] = "Avg StdDev"
    print_table(headers, rows, [14, 10, 8, 8, 12, 12, 12])

    # ── 6. TIME OF DAY ANALYSIS ──
    print(f"\n{'='*70}")
    print("TIME OF DAY ANALYSIS (4-hour blocks, UTC)")
    print(f"{'='*70}")

    time_blocks = [
        ("00-04", 0, 4),
        ("04-08", 4, 8),
        ("08-12", 8, 12),
        ("12-16", 12, 16),
        ("16-20", 16, 20),
        ("20-24", 20, 24),
    ]

    headers = ["Block(UTC)", "Signals", "Wins", "Win%", "P&L@0.80", "P&L@0.75"]
    rows = []
    for label, h_start, h_end in time_blocks:
        block_results = [r for r in results if h_start <= r["hour"] < h_end]
        if not block_results:
            rows.append([label, 0, 0, "N/A", "$0.00", "$0.00"])
            continue
        b_wins = sum(1 for r in block_results if r["win"])
        b_total = len(block_results)
        b_wr = b_wins / b_total * 100
        b_pnl80 = sum(pnl_per_trade(r["win"], 0.80) for r in block_results)
        b_pnl75 = sum(pnl_per_trade(r["win"], 0.75) for r in block_results)
        rows.append([label, b_total, b_wins, f"{b_wr:.1f}%", f"${b_pnl80:+.2f}", f"${b_pnl75:+.2f}"])

    print_table(headers, rows, [14, 10, 8, 8, 12, 12])

    # ── 7. COMBINED: VOL QUARTILE + TIME BLOCK ──
    print(f"\n{'='*70}")
    print("COMBINED FILTER: ATR Quartile x Time Block")
    print("(only showing combos with 5+ signals)")
    print(f"{'='*70}")

    headers = ["ATR Q + Time", "Signals", "Wins", "Win%", "P&L@0.80", "P&L@0.75"]
    rows = []

    best_combo = None
    best_wr = 0
    best_pnl = -999

    for q_name in ["Q1 (Low)", "Q2 (Med)", "Q3 (High)", "Q4 (VHigh)"]:
        for label, h_start, h_end in time_blocks:
            combo_results = [r for r in results if r["atr_q"] == q_name and h_start <= r["hour"] < h_end]
            if len(combo_results) < 5:
                continue
            c_wins = sum(1 for r in combo_results if r["win"])
            c_total = len(combo_results)
            c_wr = c_wins / c_total * 100
            c_pnl80 = sum(pnl_per_trade(r["win"], 0.80) for r in combo_results)
            c_pnl75 = sum(pnl_per_trade(r["win"], 0.75) for r in combo_results)

            combo_label = f"{q_name[:2]}+{label}"
            rows.append([combo_label, c_total, c_wins, f"{c_wr:.1f}%", f"${c_pnl80:+.2f}", f"${c_pnl75:+.2f}"])

            if c_pnl80 > best_pnl:
                best_pnl = c_pnl80
                best_wr = c_wr
                best_combo = combo_label

    # Sort by win rate descending
    rows.sort(key=lambda r: float(r[3].replace("%", "")), reverse=True)
    print_table(headers, rows, [16, 10, 8, 8, 12, 12])

    # ── ALSO: StdDev quartile combined ──
    print(f"\n{'='*70}")
    print("COMBINED FILTER: StdDev Quartile x Time Block")
    print("(only showing combos with 5+ signals)")
    print(f"{'='*70}")

    rows2 = []
    for q_name in ["Q1 (Low)", "Q2 (Med)", "Q3 (High)", "Q4 (VHigh)"]:
        for label, h_start, h_end in time_blocks:
            combo_results = [r for r in results if r["std_q"] == q_name and h_start <= r["hour"] < h_end]
            if len(combo_results) < 5:
                continue
            c_wins = sum(1 for r in combo_results if r["win"])
            c_total = len(combo_results)
            c_wr = c_wins / c_total * 100
            c_pnl80 = sum(pnl_per_trade(r["win"], 0.80) for r in combo_results)
            c_pnl75 = sum(pnl_per_trade(r["win"], 0.75) for r in combo_results)

            combo_label = f"{q_name[:2]}+{label}"
            rows2.append([combo_label, c_total, c_wins, f"{c_wr:.1f}%", f"${c_pnl80:+.2f}", f"${c_pnl75:+.2f}"])

    rows2.sort(key=lambda r: float(r[3].replace("%", "")), reverse=True)
    print_table(headers, rows2, [16, 10, 8, 8, 12, 12])

    # ── SUMMARY ──
    print(f"\n{'='*70}")
    print("SUMMARY & RECOMMENDATIONS")
    print(f"{'='*70}")

    # Find best single vol filter
    for vol_type, q_key in [("ATR", "atr_q"), ("StdDev", "std_q")]:
        for q_name in ["Q3 (High)", "Q4 (VHigh)"]:
            q_results = [r for r in results if r[q_key] == q_name]
            if q_results:
                q_wins = sum(1 for r in q_results if r["win"])
                q_total = len(q_results)
                q_wr = q_wins / q_total * 100
                q_pnl80 = sum(pnl_per_trade(r["win"], 0.80) for r in q_results)
                print(f"  {vol_type} {q_name}: {q_total} trades, {q_wr:.1f}% WR, ${q_pnl80:+.2f} P&L@0.80")

    # Top 3 half-vol (Q3+Q4)
    print()
    for vol_type, q_key in [("ATR", "atr_q"), ("StdDev", "std_q")]:
        high_vol = [r for r in results if r[q_key] in ["Q3 (High)", "Q4 (VHigh)"]]
        if high_vol:
            hv_wins = sum(1 for r in high_vol if r["win"])
            hv_total = len(high_vol)
            hv_wr = hv_wins / hv_total * 100
            hv_pnl80 = sum(pnl_per_trade(r["win"], 0.80) for r in high_vol)
            hv_pnl75 = sum(pnl_per_trade(r["win"], 0.75) for r in high_vol)
            print(f"  {vol_type} Q3+Q4 combined: {hv_total} trades, {hv_wr:.1f}% WR, ${hv_pnl80:+.2f}@0.80 / ${hv_pnl75:+.2f}@0.75")

    # Breakeven analysis
    print(f"\n  Breakeven WR: 80.0% @ $0.80 entry | 75.0% @ $0.75 entry")
    print(f"  Baseline WR: {wr:.1f}% ({'+' if wr > 80 else '-'}profitable @ $0.80)")


if __name__ == "__main__":
    main()
