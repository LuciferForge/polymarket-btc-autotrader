#!/usr/bin/env python3
"""
backtest_late_entry.py — Test the "late entry" strategy

Question: At the 11th minute of a 15-min BTC window, if the price is
already at 60-80 cents or 80+ cents, how often does it resolve at $1.00?

This tests: "If I buy the winning side late at $0.70-0.85, do I reliably
collect $1.00 at resolution?"

Uses Binance 1-min candles to simulate Polymarket pricing.
"""

import sys
import time
import statistics
from datetime import datetime, timezone

import requests


def fetch_binance_klines(days=14):
    """Fetch 1-minute BTC candles from Binance."""
    url = "https://api.binance.com/api/v3/klines"
    all_candles = []

    end_ms = int(time.time() * 1000)
    start_ms = end_ms - (days * 24 * 60 * 60 * 1000)

    print(f"Fetching {days} days of 1m BTC candles...")
    current = start_ms
    while current < end_ms:
        params = {"symbol": "BTCUSDT", "interval": "1m", "startTime": current, "limit": 1000}
        resp = requests.get(url, params=params, timeout=15)
        data = resp.json()
        if not data:
            break
        for k in data:
            all_candles.append({
                "open_time": k[0],
                "open": float(k[1]),
                "close": float(k[4]),
                "high": float(k[2]),
                "low": float(k[3]),
                "volume": float(k[5]),
            })
        current = data[-1][6] + 1
        time.sleep(0.1)

    print(f"  Fetched {len(all_candles)} candles")
    return all_candles


def estimate_polymarket_price(btc_open, btc_current, minutes_elapsed):
    """
    Estimate what the UP token price would be on Polymarket.

    Simple model: price reflects probability of BTC closing above open.
    Uses momentum strength and time elapsed to estimate implied probability.

    At minute 0: ~0.50 (unknown)
    At minute 14: converges toward 0.95+ or 0.05 based on direction
    """
    if btc_open <= 0:
        return 0.5

    move_pct = ((btc_current - btc_open) / btc_open) * 100
    time_factor = minutes_elapsed / 15.0  # 0 to 1

    # Base probability from momentum (logistic-like curve)
    # 0.1% move = 55% prob, 0.3% = 70%, 0.5% = 80%, 1.0% = 90%
    import math
    raw_prob = 1 / (1 + math.exp(-move_pct * 15))  # Steeper sigmoid

    # Time decay: as time passes, probability converges to certainty
    # At minute 1: price barely moves from 0.50
    # At minute 14: price nearly equals final outcome
    # Blend: early = weak signal, late = strong signal
    price = 0.5 + (raw_prob - 0.5) * (time_factor ** 0.5)

    return max(0.01, min(0.99, price))


def backtest_late_entry(candles_1m, days):
    """
    For each 15-min window:
    - Calculate what the UP price would be at each minute
    - At minute 11: check if UP is in 60-80 or 80+ range
    - Track: does it resolve as UP or DOWN?
    - Track: what happens to price in last 2 minutes?
    """
    # Group into 15-min windows
    windows = []
    for i in range(0, len(candles_1m) - 14, 15):
        window = candles_1m[i:i+15]
        if len(window) < 15:
            break
        windows.append(window)

    print(f"\nTotal 15-min windows: {len(windows)}")

    # Analyze price at each minute checkpoint
    checkpoints = [3, 5, 7, 9, 11, 13, 14]

    print(f"\n{'='*90}")
    print("PRICE DISTRIBUTION AT EACH MINUTE")
    print(f"{'='*90}")
    print(f"{'Minute':>6} {'<40c':>8} {'40-50c':>8} {'50-60c':>8} {'60-70c':>8} {'70-80c':>8} {'80-90c':>8} {'>90c':>8}")
    print("-" * 90)

    for checkpoint in checkpoints:
        buckets = {"<40": 0, "40-50": 0, "50-60": 0, "60-70": 0, "70-80": 0, "80-90": 0, ">90": 0}
        for w in windows:
            open_price = w[0]["open"]
            current = w[checkpoint - 1]["close"]
            up_price = estimate_polymarket_price(open_price, current, checkpoint)

            # Use the higher of UP or DOWN (the "winning" side)
            winning_price = max(up_price, 1 - up_price)

            if winning_price < 0.40: buckets["<40"] += 1
            elif winning_price < 0.50: buckets["40-50"] += 1
            elif winning_price < 0.60: buckets["50-60"] += 1
            elif winning_price < 0.70: buckets["60-70"] += 1
            elif winning_price < 0.80: buckets["70-80"] += 1
            elif winning_price < 0.90: buckets["80-90"] += 1
            else: buckets[">90"] += 1

        total = len(windows)
        print(f"{checkpoint:>6} "
              f"{buckets['<40']/total*100:>7.1f}% "
              f"{buckets['40-50']/total*100:>7.1f}% "
              f"{buckets['50-60']/total*100:>7.1f}% "
              f"{buckets['60-70']/total*100:>7.1f}% "
              f"{buckets['70-80']/total*100:>7.1f}% "
              f"{buckets['80-90']/total*100:>7.1f}% "
              f"{buckets['>90']/total*100:>7.1f}%")

    # ─── Core Analysis: Late Entry at Minute 11 ────────────────────────
    print(f"\n{'='*90}")
    print("LATE ENTRY ANALYSIS: BUY THE WINNING SIDE AT MINUTE 11")
    print(f"{'='*90}")

    # For each price bucket at minute 11, what % resolve correctly?
    price_ranges = [
        ("50-60c", 0.50, 0.60),
        ("60-70c", 0.60, 0.70),
        ("70-80c", 0.70, 0.80),
        ("80-90c", 0.80, 0.90),
        ("90c+",   0.90, 1.00),
    ]

    for label, lo, hi in price_ranges:
        trades = []
        for w in windows:
            open_price = w[0]["open"]
            btc_at_11 = w[10]["close"]  # Minute 11 (index 10)
            btc_at_13 = w[12]["close"]  # Minute 13
            btc_at_14 = w[13]["close"]  # Minute 14
            btc_close = w[14]["close"]  # Final close

            up_at_11 = estimate_polymarket_price(open_price, btc_at_11, 11)
            winning_side = "UP" if up_at_11 >= 0.50 else "DOWN"
            winning_price = max(up_at_11, 1 - up_at_11)

            if winning_price < lo or winning_price >= hi:
                continue

            # Did the winning side at minute 11 actually win?
            actual_direction = "UP" if btc_close >= open_price else "DOWN"
            correct = winning_side == actual_direction

            # Price progression
            up_at_13 = estimate_polymarket_price(open_price, btc_at_13, 13)
            up_at_14 = estimate_polymarket_price(open_price, btc_at_14, 14)
            winning_at_13 = max(up_at_13, 1 - up_at_13)
            winning_at_14 = max(up_at_14, 1 - up_at_14)

            trades.append({
                "correct": correct,
                "entry_price": winning_price,
                "price_at_13": winning_at_13,
                "price_at_14": winning_at_14,
                "payout": 1.0 if correct else 0.0,
            })

        if not trades:
            continue

        n = len(trades)
        wins = sum(1 for t in trades if t["correct"])
        win_rate = wins / n * 100
        avg_entry = statistics.mean(t["entry_price"] for t in trades)
        avg_13 = statistics.mean(t["price_at_13"] for t in trades)
        avg_14 = statistics.mean(t["price_at_14"] for t in trades)

        # P&L per $2 bet
        total_pnl = sum((t["payout"] - t["entry_price"]) * (2.0 / t["entry_price"]) for t in trades)
        roi = total_pnl / (n * 2) * 100

        # Price change from minute 11 to 13 and 14
        price_change_11_to_13 = avg_13 - avg_entry
        price_change_11_to_14 = avg_14 - avg_entry

        print(f"\n  Entry at {label} (n={n})")
        print(f"    Win rate: {win_rate:.1f}% ({wins}/{n})")
        print(f"    Avg entry: ${avg_entry:.4f}")
        print(f"    Avg price at min 13: ${avg_13:.4f} (change: {price_change_11_to_13:+.4f})")
        print(f"    Avg price at min 14: ${avg_14:.4f} (change: {price_change_11_to_14:+.4f})")
        print(f"    PnL on $2 bets: ${total_pnl:.2f} | ROI: {roi:.1f}%")
        if trades:
            payouts = [t["payout"] - t["entry_price"] for t in trades]
            print(f"    Per-trade P&L range: ${min(payouts):.4f} to ${max(payouts):.4f}")

    # ─── Last 2 Minutes Analysis ────────────────────────────────────────
    print(f"\n{'='*90}")
    print("LAST 2 MINUTES: HOW MUCH DOES THE PRICE MOVE?")
    print(f"{'='*90}")

    for label, lo, hi in price_ranges:
        moves_13_to_15 = []
        reversals = 0
        for w in windows:
            open_price = w[0]["open"]
            btc_at_13 = w[12]["close"]
            btc_close = w[14]["close"]

            up_at_13 = estimate_polymarket_price(open_price, btc_at_13, 13)
            winning_price = max(up_at_13, 1 - up_at_13)
            winning_side = "UP" if up_at_13 >= 0.50 else "DOWN"

            if winning_price < lo or winning_price >= hi:
                continue

            actual = "UP" if btc_close >= open_price else "DOWN"
            if winning_side != actual:
                reversals += 1

            # BTC price move in last 2 minutes (absolute %)
            btc_move = abs((btc_close - btc_at_13) / btc_at_13) * 100
            moves_13_to_15.append(btc_move)

        if not moves_13_to_15:
            continue

        n = len(moves_13_to_15)
        print(f"\n  At min 13 in {label} range (n={n})")
        print(f"    BTC moves last 2 min: avg={statistics.mean(moves_13_to_15):.4f}% "
              f"max={max(moves_13_to_15):.4f}%")
        print(f"    Reversals (leading side loses): {reversals}/{n} ({reversals/n*100:.1f}%)")

    # ─── Direct BTC analysis (no price model) ──────────────────────────
    print(f"\n{'='*90}")
    print("RAW BTC MOMENTUM ANALYSIS (NO PRICE MODEL)")
    print(f"{'='*90}")

    for checkpoint in [11, 13]:
        print(f"\n  At minute {checkpoint}:")
        btc_move_ranges = [
            ("BTC move 0.00-0.05%", 0.00, 0.05),
            ("BTC move 0.05-0.10%", 0.05, 0.10),
            ("BTC move 0.10-0.20%", 0.10, 0.20),
            ("BTC move 0.20-0.50%", 0.20, 0.50),
            ("BTC move 0.50%+",     0.50, 100),
        ]

        for label, lo, hi in btc_move_ranges:
            count = 0
            correct = 0
            for w in windows:
                open_price = w[0]["open"]
                btc_at_cp = w[checkpoint - 1]["close"]
                btc_close = w[14]["close"]

                move = abs((btc_at_cp - open_price) / open_price) * 100
                if move < lo or move >= hi:
                    continue

                count += 1
                direction_at_cp = "UP" if btc_at_cp >= open_price else "DOWN"
                actual = "UP" if btc_close >= open_price else "DOWN"
                if direction_at_cp == actual:
                    correct += 1

            if count > 0:
                print(f"    {label}: {count} windows, {correct/count*100:.1f}% resolve same direction")


def main():
    days = 14
    for i, arg in enumerate(sys.argv):
        if arg == "--days" and i + 1 < len(sys.argv):
            days = int(sys.argv[i + 1])

    candles = fetch_binance_klines(days=days)
    if not candles:
        print("Failed to fetch data")
        return

    backtest_late_entry(candles, days)


if __name__ == "__main__":
    main()
