#!/usr/bin/env python3
"""
backtest_15m.py — Backtest BTC 15-min strategies for Polymarket

Two strategies tested:
1. ORACLE LAG: Use Binance momentum in first N minutes to predict 15-min candle direction.
   If BTC moves up in first 3 min, bet UP on Polymarket before odds adjust.
2. BINARY ARB: Check how often UP + DOWN combined cost < $1.00 on live markets.

Usage:
  python3 backtest_15m.py momentum          # Backtest momentum/oracle lag strategy
  python3 backtest_15m.py momentum --days 30  # Last 30 days
  python3 backtest_15m.py arb               # Live arb opportunity scanner
"""

import sys
import json
import time
import statistics
from datetime import datetime, timezone, timedelta

import requests

# ─── Binance Historical Data ────────────────────────────────────────────────

def fetch_binance_klines(symbol="BTCUSDT", interval="1m", days=7):
    """Fetch 1-minute candles from Binance."""
    url = "https://api.binance.com/api/v3/klines"
    all_candles = []

    end_ms = int(time.time() * 1000)
    start_ms = end_ms - (days * 24 * 60 * 60 * 1000)

    print(f"Fetching {days} days of {interval} BTC candles from Binance...")

    current = start_ms
    while current < end_ms:
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": current,
            "limit": 1000,
        }
        resp = requests.get(url, params=params, timeout=15)
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

        current = data[-1][6] + 1  # Next candle after last close_time
        time.sleep(0.1)  # Rate limit

    print(f"  Fetched {len(all_candles)} candles")
    return all_candles


def group_into_15min_windows(candles_1m):
    """Group 1-minute candles into 15-minute windows."""
    windows = []

    for i in range(0, len(candles_1m) - 14, 15):
        window = candles_1m[i:i+15]
        if len(window) < 15:
            break

        window_open = window[0]["open"]
        window_close = window[-1]["close"]
        window_high = max(c["high"] for c in window)
        window_low = min(c["low"] for c in window)
        total_volume = sum(c["volume"] for c in window)

        windows.append({
            "open_time": window[0]["open_time"],
            "open": window_open,
            "close": window_close,
            "high": window_high,
            "low": window_low,
            "volume": total_volume,
            "candles": window,
            "direction": "UP" if window_close >= window_open else "DOWN",
        })

    return windows


# ─── Strategy 1: Oracle Lag / Momentum ──────────────────────────────────────

def backtest_momentum(candles_1m, signal_minutes=3, threshold_pct=0.05):
    """
    Test: If BTC moves > threshold in first N minutes of a 15-min window,
    does the full 15-min candle close in the same direction?

    This simulates the oracle lag edge: you see the move on Binance,
    Polymarket hasn't repriced yet, you bet the direction.
    """
    windows = group_into_15min_windows(candles_1m)

    results = {
        "total_windows": len(windows),
        "signals_generated": 0,
        "correct": 0,
        "wrong": 0,
        "trades": [],
    }

    for w in windows:
        candles = w["candles"]

        # Price at window open
        open_price = candles[0]["open"]

        # Price after signal_minutes
        if signal_minutes > len(candles):
            continue
        signal_price = candles[signal_minutes - 1]["close"]

        # Movement in signal window
        move_pct = ((signal_price - open_price) / open_price) * 100

        # Only trade if movement exceeds threshold
        if abs(move_pct) < threshold_pct:
            continue

        results["signals_generated"] += 1

        # Our bet: same direction as the signal
        predicted_direction = "UP" if move_pct > 0 else "DOWN"
        actual_direction = w["direction"]

        correct = predicted_direction == actual_direction
        if correct:
            results["correct"] += 1
        else:
            results["wrong"] += 1

        results["trades"].append({
            "time": datetime.fromtimestamp(w["open_time"]/1000, tz=timezone.utc).isoformat(),
            "open": open_price,
            "signal_price": signal_price,
            "close": w["close"],
            "move_pct": move_pct,
            "predicted": predicted_direction,
            "actual": actual_direction,
            "correct": correct,
        })

    return results


def backtest_momentum_with_pnl(candles_1m, signal_minutes=3, threshold_pct=0.05,
                                bet_size=2.0, entry_price=0.50):
    """
    Same as backtest_momentum but with P&L calculation.

    Assumes:
    - You buy the predicted direction at entry_price (e.g., $0.50)
    - If correct: you get $1.00 back → profit = $1.00 - entry_price per share
    - If wrong: you get $0.00 → loss = entry_price per share
    - Shares per trade = bet_size / entry_price
    """
    results = backtest_momentum(candles_1m, signal_minutes, threshold_pct)

    shares_per_trade = bet_size / entry_price
    win_payout = (1.0 - entry_price) * shares_per_trade  # Profit per win
    loss_amount = entry_price * shares_per_trade           # Loss per loss

    total_pnl = (results["correct"] * win_payout) - (results["wrong"] * loss_amount)

    results["pnl"] = {
        "bet_size": bet_size,
        "entry_price": entry_price,
        "shares_per_trade": shares_per_trade,
        "win_payout": win_payout,
        "loss_amount": loss_amount,
        "total_pnl": total_pnl,
        "total_wagered": results["signals_generated"] * bet_size,
        "roi_pct": (total_pnl / (results["signals_generated"] * bet_size) * 100) if results["signals_generated"] > 0 else 0,
    }

    return results


def run_momentum_sweep(candles_1m):
    """Sweep across different parameters to find optimal settings."""
    print("\n" + "="*80)
    print("MOMENTUM / ORACLE LAG STRATEGY BACKTEST")
    print("="*80)

    windows = group_into_15min_windows(candles_1m)

    # Base stats
    up_count = sum(1 for w in windows if w["direction"] == "UP")
    down_count = len(windows) - up_count
    print(f"\nTotal 15-min windows: {len(windows)}")
    print(f"UP: {up_count} ({up_count/len(windows)*100:.1f}%) | DOWN: {down_count} ({down_count/len(windows)*100:.1f}%)")

    # Test different signal windows and thresholds
    print(f"\n{'Signal Min':>10} {'Threshold%':>10} {'Signals':>8} {'Win%':>8} {'Correct':>8} {'Wrong':>8} {'PnL($2)':>10} {'ROI%':>8}")
    print("-" * 80)

    best_params = None
    best_roi = -999

    for sig_min in [1, 2, 3, 5, 7, 10]:
        for thresh in [0.01, 0.03, 0.05, 0.08, 0.10, 0.15, 0.20, 0.30]:
            r = backtest_momentum_with_pnl(candles_1m, sig_min, thresh)

            if r["signals_generated"] < 10:
                continue

            win_rate = (r["correct"] / r["signals_generated"] * 100) if r["signals_generated"] > 0 else 0
            roi = r["pnl"]["roi_pct"]

            marker = ""
            if roi > best_roi and r["signals_generated"] >= 50:
                best_roi = roi
                best_params = (sig_min, thresh, r)
                marker = " <<<"

            print(f"{sig_min:>10} {thresh:>10.2f} {r['signals_generated']:>8} "
                  f"{win_rate:>7.1f}% {r['correct']:>8} {r['wrong']:>8} "
                  f"${r['pnl']['total_pnl']:>9.2f} {roi:>7.1f}%{marker}")

    if best_params:
        sig_min, thresh, r = best_params
        win_rate = r["correct"] / r["signals_generated"] * 100
        print(f"\n{'='*80}")
        print(f"BEST PARAMS: signal_minutes={sig_min}, threshold={thresh:.2f}%")
        print(f"  Win Rate: {win_rate:.1f}%")
        print(f"  Signals/Day: ~{r['signals_generated'] / (len(windows) / 96):.1f}")
        print(f"  Total PnL: ${r['pnl']['total_pnl']:.2f} on ${r['pnl']['total_wagered']:.2f} wagered")
        print(f"  ROI: {r['pnl']['roi_pct']:.1f}%")

        # Simulate daily PnL
        trades = r["trades"]
        if trades:
            daily_pnl = {}
            for t in trades:
                day = t["time"][:10]
                pnl = r["pnl"]["win_payout"] if t["correct"] else -r["pnl"]["loss_amount"]
                daily_pnl[day] = daily_pnl.get(day, 0) + pnl

            pnl_values = list(daily_pnl.values())
            print(f"\n  Daily PnL Stats:")
            print(f"    Avg: ${statistics.mean(pnl_values):.2f}/day")
            print(f"    Min: ${min(pnl_values):.2f}")
            print(f"    Max: ${max(pnl_values):.2f}")
            print(f"    Median: ${statistics.median(pnl_values):.2f}")
            winning_days = sum(1 for v in pnl_values if v > 0)
            print(f"    Winning days: {winning_days}/{len(pnl_values)} ({winning_days/len(pnl_values)*100:.0f}%)")

    # Also test mean reversion (bet AGAINST the first N minutes)
    print(f"\n{'='*80}")
    print("MEAN REVERSION (bet AGAINST early momentum)")
    print("="*80)
    print(f"\n{'Signal Min':>10} {'Threshold%':>10} {'Signals':>8} {'Win%':>8} {'PnL($2)':>10} {'ROI%':>8}")
    print("-" * 80)

    for sig_min in [1, 2, 3, 5]:
        for thresh in [0.05, 0.10, 0.15, 0.20, 0.30, 0.50]:
            r = backtest_momentum_with_pnl(candles_1m, sig_min, thresh)

            if r["signals_generated"] < 10:
                continue

            # Invert: mean reversion means if we're "wrong" with momentum, we'd be "right" with reversion
            rev_correct = r["wrong"]
            rev_wrong = r["correct"]
            rev_win_rate = (rev_correct / r["signals_generated"] * 100) if r["signals_generated"] > 0 else 0
            rev_pnl = -r["pnl"]["total_pnl"]  # Inverted
            rev_roi = -r["pnl"]["roi_pct"]

            print(f"{sig_min:>10} {thresh:>10.2f} {r['signals_generated']:>8} "
                  f"{rev_win_rate:>7.1f}% ${rev_pnl:>9.2f} {rev_roi:>7.1f}%")


# ─── Strategy 2: Live Binary Arb Scanner ───────────────────────────────────

def scan_live_arb():
    """Scan current BTC 15-min markets for arb opportunities."""
    print("\n" + "="*80)
    print("LIVE BINARY ARB SCANNER")
    print("="*80)

    GAMMA_API = "https://gamma-api.polymarket.com"
    CLOB_API = "https://clob.polymarket.com"

    # Find active BTC 15-min markets
    print("\nSearching for active BTC 15-min markets...")

    try:
        # Search for BTC up/down markets
        resp = requests.get(f"{GAMMA_API}/markets", params={
            "limit": 20,
            "active": "true",
            "closed": "false",
            "tag": "crypto",
        }, timeout=15)
        all_markets = resp.json()
    except Exception as e:
        print(f"Error fetching markets: {e}")
        # Try alternative: search by slug pattern
        all_markets = []

    btc_15m_markets = []
    for m in all_markets:
        q = m.get("question", "").lower()
        slug = m.get("slug", "").lower()
        if ("bitcoin" in q or "btc" in q) and ("15" in q or "15m" in slug or "15min" in slug):
            btc_15m_markets.append(m)

    if not btc_15m_markets:
        # Try direct slug search
        print("Trying slug-based search...")
        import math
        now_ts = int(time.time())
        # Round down to nearest 15 min
        rounded = now_ts - (now_ts % 900)

        for offset in range(0, 5):
            ts = rounded + (offset * 900)
            slug = f"btc-updown-15m-{ts}"
            try:
                resp = requests.get(f"{GAMMA_API}/markets", params={"slug": slug}, timeout=10)
                data = resp.json()
                if data:
                    if isinstance(data, list):
                        btc_15m_markets.extend(data)
                    else:
                        btc_15m_markets.append(data)
            except Exception:
                pass

    if not btc_15m_markets:
        print("No active BTC 15-min markets found via API.")
        print("Trying to fetch order books for known token patterns...")
        return

    print(f"\nFound {len(btc_15m_markets)} BTC 15-min markets\n")

    arb_opportunities = 0

    for m in btc_15m_markets:
        question = m.get("question", "N/A")
        tokens = m.get("clobTokenIds", [])
        prices_raw = m.get("outcomePrices", "[]")

        if isinstance(prices_raw, str):
            prices = json.loads(prices_raw)
        else:
            prices = prices_raw

        if len(tokens) < 2 or len(prices) < 2:
            continue

        yes_price = float(prices[0])
        no_price = float(prices[1])
        combined = yes_price + no_price

        # Check order book for actual executable prices
        yes_book = get_orderbook(CLOB_API, tokens[0])
        no_book = get_orderbook(CLOB_API, tokens[1])

        yes_ask = yes_book["best_ask"]
        no_ask = no_book["best_ask"]
        combined_ask = yes_ask + no_ask

        spread = 1.0 - combined_ask
        is_arb = combined_ask < 0.995  # After accounting for gas/fees

        status = "ARB !!!" if is_arb else "no arb"

        print(f"  {question[:60]}")
        print(f"    Mid: YES={yes_price:.4f} NO={no_price:.4f} Combined={combined:.4f}")
        print(f"    Ask: YES={yes_ask:.4f} NO={no_ask:.4f} Combined={combined_ask:.4f}")
        print(f"    Spread: ${spread:.4f} | {status}")
        print(f"    YES depth: bid={yes_book['best_bid']:.4f} ask={yes_ask:.4f} | "
              f"NO depth: bid={no_book['best_bid']:.4f} ask={no_ask:.4f}")
        print()

        if is_arb:
            arb_opportunities += 1
            profit_per_pair = spread
            shares_at_50 = 50 / combined_ask
            print(f"    >>> PROFIT PER $1 PAIR: ${profit_per_pair:.4f}")
            print(f"    >>> 50-share trade profit: ${profit_per_pair * 50:.2f}")

    print(f"\n{'='*80}")
    print(f"ARB OPPORTUNITIES FOUND: {arb_opportunities}/{len(btc_15m_markets)}")


def get_orderbook(clob_api, token_id):
    """Fetch order book for a token."""
    try:
        resp = requests.get(f"{clob_api}/book", params={"token_id": token_id}, timeout=10)
        resp.raise_for_status()
        book = resp.json()
        bids = book.get("bids", [])
        asks = book.get("asks", [])

        best_bid = float(bids[0]["price"]) if bids else 0
        best_ask = float(asks[0]["price"]) if asks else 1
        bid_depth = sum(float(b.get("size", 0)) for b in bids[:3])
        ask_depth = sum(float(a.get("size", 0)) for a in asks[:3])

        return {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "bid_depth": bid_depth,
            "ask_depth": ask_depth,
            "spread": best_ask - best_bid,
        }
    except Exception as e:
        return {"best_bid": 0, "best_ask": 1, "bid_depth": 0, "ask_depth": 0, "spread": 1}


# ─── Main ───────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    cmd = sys.argv[1]

    if cmd == "momentum":
        days = 7
        for i, arg in enumerate(sys.argv):
            if arg == "--days" and i + 1 < len(sys.argv):
                days = int(sys.argv[i + 1])

        candles = fetch_binance_klines(days=days)
        if not candles:
            print("Failed to fetch candle data")
            return

        run_momentum_sweep(candles)

    elif cmd == "arb":
        scan_live_arb()

    else:
        print(f"Unknown command: {cmd}")
        print("Commands: momentum, arb")


if __name__ == "__main__":
    main()
