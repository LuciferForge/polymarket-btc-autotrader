#!/usr/bin/env python3
"""
test_snipe.py — Test the late momentum / snipe strategy against LIVE data.

For each active 15m market:
1. Get BTC/ETH/SOL/XRP price move since window open
2. If move > threshold, check winning side ask price
3. Check if FOK fill is possible at 10/20 shares
4. Calculate expected profit per trade

This tells us: when the signal fires, can we actually execute profitably?
"""

import json
import time
import requests

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
BINANCE_REST = "https://fapi.binance.com"
ASSETS = ["btc", "eth", "sol", "xrp"]
ASSET_SYMBOLS = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT", "xrp": "XRPUSDT"}


def get_binance_price(symbol: str) -> float:
    try:
        resp = requests.get(f"{BINANCE_REST}/fapi/v1/ticker/price", params={"symbol": symbol}, timeout=5)
        return float(resp.json()["price"])
    except:
        return 0


def get_binance_open(symbol: str, window_start: int) -> float:
    """Get the price at window open from Binance 1m klines."""
    try:
        resp = requests.get(f"{BINANCE_REST}/fapi/v1/klines", params={
            "symbol": symbol,
            "interval": "1m",
            "startTime": window_start * 1000,
            "limit": 1,
        }, timeout=5)
        data = resp.json()
        if data:
            return float(data[0][1])  # open price
    except:
        pass
    return 0


def get_full_orderbook(token_id: str) -> dict:
    try:
        resp = requests.get(f"{CLOB_API}/book", params={"token_id": token_id}, timeout=10)
        resp.raise_for_status()
        book = resp.json()
        bids = sorted(book.get("bids", []), key=lambda x: float(x["price"]), reverse=True)
        asks = sorted(book.get("asks", []), key=lambda x: float(x["price"]))
        return {"bids": bids, "asks": asks}
    except:
        return {"bids": [], "asks": []}


def find_active_markets():
    now_ts = int(time.time())
    current_window = now_ts - (now_ts % 900)
    markets = []
    for asset in ASSETS:
        for offset in range(-1, 3):
            ts = current_window + (offset * 900)
            slug = f"{asset}-updown-15m-{ts}"
            if ts + 900 < now_ts - 60:
                continue
            try:
                resp = requests.get(f"{GAMMA_API}/markets", params={"slug": slug}, timeout=10)
                data = resp.json()
                if not data:
                    continue
                if isinstance(data, list):
                    for m in data:
                        tokens_raw = m.get("clobTokenIds", [])
                        if isinstance(tokens_raw, str):
                            tokens = json.loads(tokens_raw)
                        else:
                            tokens = tokens_raw
                        if len(tokens) >= 2:
                            markets.append({
                                "slug": slug, "asset": asset,
                                "up_token": tokens[0], "down_token": tokens[1],
                                "window_start": ts, "window_end": ts + 900,
                                "elapsed_min": (now_ts - ts) / 60,
                            })
            except:
                pass
    return markets


def main():
    print("=" * 80)
    print("MOMENTUM / SNIPE STRATEGY — LIVE EXECUTION TEST")
    print("=" * 80)

    markets = find_active_markets()
    print(f"\nFound {len(markets)} active markets\n")

    momentum_signals = 0
    snipe_signals = 0
    fillable_momentum_10 = 0
    fillable_momentum_20 = 0
    profitable_trades = 0
    total_expected_pnl = 0
    entry_prices = []

    for mkt in markets:
        symbol = ASSET_SYMBOLS[mkt["asset"]]
        open_price = get_binance_open(symbol, mkt["window_start"])
        current_price = get_binance_price(symbol)

        if open_price <= 0 or current_price <= 0:
            continue

        move_pct = ((current_price - open_price) / open_price) * 100
        direction = "UP" if move_pct > 0 else "DOWN"
        elapsed = mkt["elapsed_min"]

        # Determine which token to buy
        if direction == "UP":
            win_token = mkt["up_token"]
            lose_token = mkt["down_token"]
        else:
            win_token = mkt["down_token"]
            lose_token = mkt["up_token"]

        win_book = get_full_orderbook(win_token)
        win_asks = win_book["asks"]
        win_best = float(win_asks[0]["price"]) if win_asks else None
        win_depth_best = float(win_asks[0]["size"]) if win_asks else 0

        # Also check losing side bid (to sell if wrong)
        lose_book = get_full_orderbook(lose_token)

        status_line = f"  {mkt['slug']}  min={elapsed:.1f}  {symbol}={current_price:.2f}  move={move_pct:+.3f}%"

        # Is this a momentum signal? (min 3-12, move > 0.20%)
        is_momentum = 3 <= elapsed <= 12 and abs(move_pct) >= 0.20
        # Is this a snipe signal? (min 13-14.5, move > 0.10%)
        is_snipe = 13 <= elapsed <= 14.5 and abs(move_pct) >= 0.10

        if not is_momentum and not is_snipe and abs(move_pct) < 0.10:
            print(f"{status_line}  → No signal (move too small)")
            continue

        print(f"\n{'─' * 70}")
        print(status_line)

        signal_type = "SNIPE" if is_snipe else ("MOMENTUM" if is_momentum else "WATCH")
        if is_momentum:
            momentum_signals += 1
        if is_snipe:
            snipe_signals += 1

        print(f"  Signal: {signal_type} {direction}")

        if win_best is None:
            print(f"  No asks on winning side — can't execute")
            continue

        print(f"  Winning side ({direction}) best ask: ${win_best:.2f} x {win_depth_best:.0f} shares")

        # Show top 5 ask levels
        total_depth_5 = sum(float(a["size"]) for a in win_asks[:5])
        for lvl in win_asks[:5]:
            print(f"    ${float(lvl['price']):.2f} x {float(lvl['size']):.1f}")
        print(f"  Top-5 depth: {total_depth_5:.0f} shares")

        # Entry price check
        entry = win_best
        entry_prices.append(entry)

        # FOK fillability
        can_fok_10 = win_depth_best >= 10
        can_fok_20 = win_depth_best >= 20
        if is_momentum or is_snipe:
            if can_fok_10:
                fillable_momentum_10 += 1
            if can_fok_20:
                fillable_momentum_20 += 1

        # Profit calculation
        # Win: pay entry, receive $1.00. Profit = (1 - entry) * shares
        # Loss: pay entry, receive $0. Loss = entry * shares
        # Need to estimate win rate from backtest data
        if is_snipe:
            win_rate = 0.99 if abs(move_pct) >= 0.20 else 0.98
        elif is_momentum and elapsed >= 10:
            win_rate = 0.95 if abs(move_pct) >= 0.20 else 0.87
        elif is_momentum and elapsed >= 7:
            win_rate = 0.85 if abs(move_pct) >= 0.20 else 0.80
        elif is_momentum:
            win_rate = 0.84 if abs(move_pct) >= 0.20 else 0.77
        else:
            win_rate = 0.70

        shares = 10
        expected_win = (1.0 - entry) * shares * win_rate
        expected_loss = entry * shares * (1 - win_rate)
        expected_pnl = expected_win - expected_loss

        is_profitable = expected_pnl > 0
        if is_profitable and (is_momentum or is_snipe):
            profitable_trades += 1
            total_expected_pnl += expected_pnl

        profit_str = f"${expected_pnl:+.2f}" if expected_pnl >= 0 else f"${expected_pnl:.2f}"

        # Max entry price for profitability: win_rate * $1 > entry
        # entry < win_rate (breakeven)
        breakeven = win_rate

        print(f"\n  Entry: ${entry:.2f} | Win rate: {win_rate*100:.0f}% | Breakeven: ${breakeven:.2f}")
        print(f"  E[PnL] per 10 shares: {profit_str} {'✓ PROFITABLE' if is_profitable else '✗ LOSING'}")
        print(f"  FOK 10: {'✓' if can_fok_10 else '✗'}  |  FOK 20: {'✓' if can_fok_20 else '✗'}")

        if entry >= breakeven:
            print(f"  ⚠ ENTRY TOO HIGH — ${entry:.2f} >= breakeven ${breakeven:.2f}")
        elif entry >= 0.85:
            margin = breakeven - entry
            print(f"  Thin margin: ${margin:.2f} below breakeven")
        else:
            margin = breakeven - entry
            print(f"  Good margin: ${margin:.2f} below breakeven")

    # ═══ SUMMARY ═══
    total_signals = momentum_signals + snipe_signals
    print(f"\n{'=' * 80}")
    print(f"SUMMARY — {len(markets)} markets scanned")
    print(f"{'=' * 80}")
    print(f"\n  Momentum signals (min 3-12, >0.20%): {momentum_signals}")
    print(f"  Snipe signals (min 13-14, >0.10%):    {snipe_signals}")
    print(f"  Total actionable signals:              {total_signals}")

    if total_signals > 0:
        print(f"\n  FOK 10 fillable: {fillable_momentum_10}/{total_signals} = {fillable_momentum_10/total_signals*100:.0f}%")
        print(f"  FOK 20 fillable: {fillable_momentum_20}/{total_signals} = {fillable_momentum_20/total_signals*100:.0f}%")
        print(f"  Profitable after entry cost: {profitable_trades}/{total_signals}")
        print(f"  Total E[PnL] across signals: ${total_expected_pnl:.2f}")

    if entry_prices:
        avg_entry = sum(entry_prices) / len(entry_prices)
        print(f"\n  Average winning-side entry price: ${avg_entry:.2f}")
        print(f"  Price range: ${min(entry_prices):.2f} — ${max(entry_prices):.2f}")

    # Key question: is the entry cheap enough?
    print(f"\n{'=' * 80}")
    print("VERDICT")
    print(f"{'=' * 80}")
    if entry_prices:
        avg = sum(entry_prices) / len(entry_prices)
        if avg <= 0.70:
            print(f"\n  Entry prices are GOOD (avg ${avg:.2f}). Momentum strategy is viable.")
            print(f"  At ${avg:.2f} entry with 85%+ win rate: E[PnL] = +${(1-avg)*0.85 - avg*0.15:.2f}/share")
        elif avg <= 0.85:
            print(f"\n  Entry prices are MARGINAL (avg ${avg:.2f}). Profitable but thin edge.")
            print(f"  At ${avg:.2f} entry with 85% win rate: E[PnL] = +${(1-avg)*0.85 - avg*0.15:.2f}/share")
            print(f"  At ${avg:.2f} entry with 95% win rate: E[PnL] = +${(1-avg)*0.95 - avg*0.05:.2f}/share")
        else:
            print(f"\n  Entry prices are TOO HIGH (avg ${avg:.2f}). Edge is negative or near-zero.")
            print(f"  Markets reprice faster than you can enter. Snipe-only at min 13+ may work")
            print(f"  if entry stays < ${0.95:.2f}, but margin is razor thin.")
    else:
        print(f"\n  No signals fired. Markets are flat. Run again during volatile windows.")


if __name__ == "__main__":
    main()
