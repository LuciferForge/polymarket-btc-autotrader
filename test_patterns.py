#!/usr/bin/env python3
"""
test_patterns.py — Analyze intra-window price patterns on 15m BTC windows.

Pull 1-minute candles for recent 15m windows and classify:
1. SUSTAINED TREND — steady move, good signal
2. PUMP & DUMP — big early move, reversal, BAD signal
3. LATE BREAK — flat early, move late, good signal
4. CHOPPY — no direction, skip

For each window, measure:
- Peak move (max deviation from open)
- Retracement: how much of peak was given back by minute 8+
- Late vs early momentum ratio
- Candle direction alignment (monotonicity)
- Final outcome: did the direction at min 8 hold through min 15?
"""

import time
import requests

BINANCE = "https://fapi.binance.com"


def get_window_candles(symbol: str, window_start: int) -> list:
    """Get 1-minute candles for a full 15-minute window."""
    resp = requests.get(f"{BINANCE}/fapi/v1/klines", params={
        "symbol": symbol,
        "interval": "1m",
        "startTime": window_start * 1000,
        "endTime": (window_start + 900) * 1000,
        "limit": 16,
    }, timeout=10)
    return resp.json()


def analyze_window(candles: list) -> dict:
    """Analyze a 15-minute window's price action pattern."""
    if len(candles) < 14:
        return None

    open_price = float(candles[0][1])
    closes = [float(c[4]) for c in candles]
    highs = [float(c[2]) for c in candles]
    lows = [float(c[3]) for c in candles]

    # Move at each minute as % from open
    moves = [(c - open_price) / open_price * 100 for c in closes]

    # Final move (end of window)
    final_move = moves[-1]
    final_direction = "UP" if final_move > 0 else "DOWN"

    # Peak move (signed — max absolute deviation in either direction)
    max_up = max(moves)
    max_down = min(moves)
    if abs(max_up) >= abs(max_down):
        peak_move = max_up
        peak_min = moves.index(max_up)
    else:
        peak_move = max_down
        peak_min = moves.index(max_down)

    # Move at minute 8 (entry point)
    move_at_8 = moves[min(7, len(moves)-1)]
    direction_at_8 = "UP" if move_at_8 > 0 else "DOWN"

    # Retracement: how much of peak was given back by the end?
    if abs(peak_move) > 0.001:
        retracement = 1.0 - (final_move / peak_move) if peak_move != 0 else 0
    else:
        retracement = 0

    # Retracement from peak to minute 8
    if abs(peak_move) > 0.001 and peak_min < 8:
        retrace_at_8 = 1.0 - (move_at_8 / peak_move) if peak_move != 0 else 0
    else:
        retrace_at_8 = 0

    # Early momentum (min 0-5) vs late momentum (min 5-10)
    early_move = moves[min(4, len(moves)-1)]  # move at minute 5
    late_move = moves[min(9, len(moves)-1)] - moves[min(4, len(moves)-1)]  # move from min 5 to 10

    # Direction alignment: how many 1-min candles moved in the final direction?
    final_sign = 1 if final_move > 0 else -1
    aligned = sum(1 for i in range(1, len(moves)) if (moves[i] - moves[i-1]) * final_sign > 0)
    monotonicity = aligned / (len(moves) - 1)

    # Did direction at min 8 match final direction?
    direction_held = direction_at_8 == final_direction

    # Classify pattern
    if abs(peak_move) < 0.10:
        pattern = "FLAT"
    elif peak_min <= 5 and retrace_at_8 > 0.40:
        pattern = "PUMP_DUMP"
    elif peak_min <= 5 and retrace_at_8 > 0.20:
        pattern = "FADING"
    elif abs(early_move) < abs(late_move) and peak_min >= 6:
        pattern = "LATE_BREAK"
    elif monotonicity >= 0.60:
        pattern = "SUSTAINED"
    else:
        pattern = "CHOPPY"

    return {
        "final_move": final_move,
        "final_direction": final_direction,
        "move_at_8": move_at_8,
        "direction_at_8": direction_at_8,
        "direction_held": direction_held,
        "peak_move": peak_move,
        "peak_min": peak_min,
        "retracement": retracement,
        "retrace_at_8": retrace_at_8,
        "early_move": early_move,
        "late_move": late_move,
        "monotonicity": monotonicity,
        "pattern": pattern,
    }


def main():
    print("=" * 90)
    print("15M WINDOW PATTERN ANALYSIS — Last 100 windows (25 hours)")
    print("=" * 90)

    now_ts = int(time.time())
    current_window = now_ts - (now_ts % 900)

    # Analyze last 400 windows (about 4 days)
    N_WINDOWS = 400
    pattern_stats = {}
    direction_held_by_pattern = {}
    total_windows = 0
    total_signal_windows = 0  # windows where move_at_8 > 0.20%

    print(f"\nScanning {N_WINDOWS} windows (~{N_WINDOWS*15/60/24:.0f} days)...")
    print(f"\n{'At8%':>7} {'Peak%':>7} {'PkMin':>5} {'Final%':>7} {'Retrace':>8} {'Mono':>5} {'Pattern':>12} {'Held?':>5}")
    print("-" * 90)

    for i in range(N_WINDOWS, 0, -1):
        ws = current_window - (i * 900)
        candles = get_window_candles("BTCUSDT", ws)

        result = analyze_window(candles)
        if not result:
            continue

        total_windows += 1
        pat = result["pattern"]
        pattern_stats[pat] = pattern_stats.get(pat, 0) + 1

        if pat not in direction_held_by_pattern:
            direction_held_by_pattern[pat] = {"held": 0, "total": 0, "signal": 0, "signal_held": 0}

        direction_held_by_pattern[pat]["total"] += 1
        if result["direction_held"]:
            direction_held_by_pattern[pat]["held"] += 1

        # Signal windows: move_at_8 > 0.20%
        if abs(result["move_at_8"]) >= 0.20:
            total_signal_windows += 1
            direction_held_by_pattern[pat]["signal"] += 1
            if result["direction_held"]:
                direction_held_by_pattern[pat]["signal_held"] += 1

        held = "YES" if result["direction_held"] else "NO"

        # Only print interesting windows (had a signal)
        if abs(result["move_at_8"]) >= 0.10:
            print(f"{result['move_at_8']:>+7.3f} {result['peak_move']:>+7.3f} "
                  f"{result['peak_min']:>5} {result['move_at_8']:>+7.3f} "
                  f"{result['final_move']:>+7.3f} {result['retrace_at_8']:>7.1%} "
                  f"{result['monotonicity']:>5.0%} {pat:>12} {held:>5}")

    print(f"\n{'=' * 90}")
    print(f"PATTERN BREAKDOWN — {total_windows} windows analyzed, {total_signal_windows} had signal (>0.20% at min 8)")
    print(f"{'=' * 90}")

    print(f"\n{'Pattern':>12} {'Count':>6} {'%':>5} {'Dir Held':>9} {'HeldRate':>9} │ {'Signals':>8} {'SigHeld':>8} {'SigRate':>8}")
    print("-" * 90)

    for pat in sorted(direction_held_by_pattern.keys()):
        d = direction_held_by_pattern[pat]
        count = d["total"]
        held = d["held"]
        rate = held / count * 100 if count else 0
        sig = d["signal"]
        sig_held = d["signal_held"]
        sig_rate = sig_held / sig * 100 if sig else 0

        print(f"{pat:>12} {count:>6} {count/total_windows*100:>4.0f}% {held:>5}/{count:<3} {rate:>7.1f}%  │ {sig:>8} {sig_held:>8} {sig_rate:>7.1f}%")

    # Key insight: which patterns should we trade vs skip?
    print(f"\n{'=' * 90}")
    print("VERDICT — Which patterns to trade?")
    print(f"{'=' * 90}")
    for pat in sorted(direction_held_by_pattern.keys()):
        d = direction_held_by_pattern[pat]
        sig = d["signal"]
        if sig == 0:
            continue
        sig_rate = d["signal_held"] / sig * 100
        if sig_rate >= 85:
            verdict = "TRADE — high hold rate"
        elif sig_rate >= 75:
            verdict = "TRADE WITH CAUTION"
        elif sig_rate >= 60:
            verdict = "MARGINAL — thin edge"
        else:
            verdict = "SKIP — direction reverses too often"
        print(f"  {pat:>12}: {sig_rate:.0f}% hold rate on {sig} signals → {verdict}")


if __name__ == "__main__":
    main()
