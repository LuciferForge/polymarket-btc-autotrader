#!/usr/bin/env python3
"""
test_liquidity.py — Pull LIVE orderbook data from current 15m markets
and test which order approach actually fills.

Tests:
1. FOK @ 10 shares (current approach)
2. FOK @ 5 shares (reduced size)
3. FOK @ 3 shares (minimum viable)
4. GTC limit @ 10 shares (would it rest on book?)
5. Combined cost threshold: 0.99 vs 0.97 vs 0.95

Output: hard numbers on fillable depth per market.
"""

import json
import time
import requests

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
ASSETS = ["btc", "eth", "sol", "xrp"]


def get_full_orderbook(token_id: str) -> dict:
    """Get FULL order book with all levels."""
    try:
        resp = requests.get(f"{CLOB_API}/book", params={"token_id": token_id}, timeout=10)
        resp.raise_for_status()
        book = resp.json()
        bids = sorted(book.get("bids", []), key=lambda x: float(x["price"]), reverse=True)
        asks = sorted(book.get("asks", []), key=lambda x: float(x["price"]))
        return {"bids": bids, "asks": asks}
    except Exception as e:
        return {"bids": [], "asks": [], "error": str(e)}


def simulate_fok_fill(asks: list, size: float, max_price: float = None) -> dict:
    """Simulate FOK order against ask side.

    FOK = must fill entire size at best ask price (single level).
    Returns whether it would fill, at what price, and available depth.
    """
    if not asks:
        return {"fills": False, "reason": "no asks", "best_ask": None, "depth_at_best": 0}

    best_ask_price = float(asks[0]["price"])
    best_ask_size = float(asks[0]["size"])

    if max_price and best_ask_price > max_price:
        return {"fills": False, "reason": f"best ask ${best_ask_price} > max ${max_price}",
                "best_ask": best_ask_price, "depth_at_best": best_ask_size}

    # FOK fills at best ask only — can it fill the full size?
    if best_ask_size >= size:
        return {"fills": True, "price": best_ask_price, "depth_at_best": best_ask_size,
                "surplus": best_ask_size - size}
    else:
        return {"fills": False, "reason": f"depth {best_ask_size:.1f} < size {size}",
                "best_ask": best_ask_price, "depth_at_best": best_ask_size}


def simulate_gtc_fill(asks: list, size: float) -> dict:
    """Simulate GTC limit order — walks through multiple price levels.

    GTC can fill across levels. Shows total fillable and worst price.
    """
    if not asks:
        return {"fillable": 0, "levels_needed": 0, "worst_price": None}

    filled = 0
    levels = 0
    worst_price = None

    for level in asks:
        price = float(level["price"])
        available = float(level["size"])

        take = min(available, size - filled)
        filled += take
        levels += 1
        worst_price = price

        if filled >= size:
            break

    return {
        "fillable": filled,
        "full_fill": filled >= size,
        "levels_needed": levels,
        "best_price": float(asks[0]["price"]),
        "worst_price": worst_price,
        "slippage": (worst_price - float(asks[0]["price"])) if worst_price else 0,
    }


def find_active_markets():
    """Find current 15m markets across all assets."""
    now_ts = int(time.time())
    current_window = now_ts - (now_ts % 900)

    markets = []
    for asset in ASSETS:
        for offset in range(0, 3):  # current + next 2
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
                                "slug": slug,
                                "asset": asset.upper(),
                                "up_token": tokens[0],
                                "down_token": tokens[1],
                                "window_start": ts,
                                "minutes_left": max(0, (ts + 900 - now_ts) / 60),
                            })
            except Exception:
                pass

    return markets


def main():
    print("=" * 80)
    print("POLYMARKET 15M LIQUIDITY TEST — LIVE ORDERBOOK DATA")
    print("=" * 80)

    markets = find_active_markets()
    print(f"\nFound {len(markets)} active markets\n")

    # Track results across all markets
    results = {
        "fok_10": {"fill": 0, "fail": 0},
        "fok_5": {"fill": 0, "fail": 0},
        "fok_3": {"fill": 0, "fail": 0},
        "gtc_10": {"fill": 0, "fail": 0},
    }
    arb_opportunities = {"0.99": 0, "0.97": 0, "0.95": 0}
    arb_fillable = {"0.99": {"fok10": 0, "fok5": 0, "fok3": 0, "gtc10": 0},
                    "0.97": {"fok10": 0, "fok5": 0, "fok3": 0, "gtc10": 0},
                    "0.95": {"fok10": 0, "fok5": 0, "fok3": 0, "gtc10": 0}}

    for mkt in markets:
        print(f"\n{'─' * 70}")
        print(f"  {mkt['slug']}  ({mkt['minutes_left']:.0f}m left)")
        print(f"{'─' * 70}")

        up_book = get_full_orderbook(mkt["up_token"])
        down_book = get_full_orderbook(mkt["down_token"])

        up_asks = up_book["asks"]
        down_asks = down_book["asks"]

        # Show raw book depth
        up_total_ask = sum(float(a["size"]) for a in up_asks)
        down_total_ask = sum(float(a["size"]) for a in down_asks)

        up_best = float(up_asks[0]["price"]) if up_asks else None
        down_best = float(down_asks[0]["price"]) if down_asks else None

        print(f"  UP:   {len(up_asks)} ask levels | Total depth: {up_total_ask:.0f} shares | Best: ${up_best}")
        if up_asks:
            for lvl in up_asks[:5]:
                print(f"         ${float(lvl['price']):.2f} x {float(lvl['size']):.1f}")

        print(f"  DOWN: {len(down_asks)} ask levels | Total depth: {down_total_ask:.0f} shares | Best: ${down_best}")
        if down_asks:
            for lvl in down_asks[:5]:
                print(f"         ${float(lvl['price']):.2f} x {float(lvl['size']):.1f}")

        # Combined cost check
        if up_best and down_best:
            combined = up_best + down_best
            print(f"\n  Combined best ask: ${combined:.4f}  (gap to $1: ${1-combined:.4f})")

            for threshold in ["0.99", "0.97", "0.95"]:
                if combined <= float(threshold):
                    arb_opportunities[threshold] += 1

                    # Test FOK fillability for arb (BOTH sides must fill)
                    for size_label, size in [("fok10", 10), ("fok5", 5), ("fok3", 3)]:
                        up_fok = simulate_fok_fill(up_asks, size)
                        down_fok = simulate_fok_fill(down_asks, size)
                        if up_fok["fills"] and down_fok["fills"]:
                            arb_fillable[threshold][size_label] += 1

                    # GTC
                    up_gtc = simulate_gtc_fill(up_asks, 10)
                    down_gtc = simulate_gtc_fill(down_asks, 10)
                    if up_gtc["full_fill"] and down_gtc["full_fill"]:
                        arb_fillable[threshold]["gtc10"] += 1

        # Individual side fill tests
        for side, asks, label in [("UP", up_asks, "up"), ("DOWN", down_asks, "down")]:
            for size in [10, 5, 3]:
                key = f"fok_{size}"
                r = simulate_fok_fill(asks, size)
                if r["fills"]:
                    results[key]["fill"] += 1
                else:
                    results[key]["fail"] += 1

            gtc = simulate_gtc_fill(asks, 10)
            if gtc["full_fill"]:
                results["gtc_10"]["fill"] += 1
            else:
                results["gtc_10"]["fail"] += 1

        # Per-market fill summary
        print(f"\n  Fill test results (per side):")
        for size in [10, 5, 3]:
            up_r = simulate_fok_fill(up_asks, size)
            down_r = simulate_fok_fill(down_asks, size)
            up_ok = "✓" if up_r["fills"] else "✗"
            down_ok = "✓" if down_r["fills"] else "✗"
            print(f"    FOK {size:>2} shares:  UP={up_ok}  DOWN={down_ok}  {'BOTH' if up_r['fills'] and down_r['fills'] else 'FAIL'}")

        up_gtc = simulate_gtc_fill(up_asks, 10)
        down_gtc = simulate_gtc_fill(down_asks, 10)
        up_ok = "✓" if up_gtc["full_fill"] else f"✗ ({up_gtc['fillable']:.0f}/10)"
        down_ok = "✓" if down_gtc["full_fill"] else f"✗ ({down_gtc['fillable']:.0f}/10)"
        slip_up = f"+${up_gtc['slippage']:.2f}" if up_gtc["slippage"] else ""
        slip_dn = f"+${down_gtc['slippage']:.2f}" if down_gtc["slippage"] else ""
        print(f"    GTC 10 shares:  UP={up_ok} {slip_up}  DOWN={down_ok} {slip_dn}")

    # ═══ SUMMARY ═══
    total_sides = len(markets) * 2  # UP + DOWN for each market

    print(f"\n{'=' * 80}")
    print(f"RESULTS — {len(markets)} markets, {total_sides} order sides tested")
    print(f"{'=' * 80}")

    print(f"\n  INDIVIDUAL SIDE FILL RATE (can the order fill on one side?):")
    for key, label in [("fok_10", "FOK 10 shares"), ("fok_5", "FOK  5 shares"),
                       ("fok_3", "FOK  3 shares"), ("gtc_10", "GTC 10 shares")]:
        r = results[key]
        total = r["fill"] + r["fail"]
        rate = r["fill"] / total * 100 if total else 0
        print(f"    {label}:  {r['fill']}/{total} = {rate:.0f}% fill rate")

    print(f"\n  ARB OPPORTUNITIES (combined ask < threshold):")
    for threshold in ["0.99", "0.97", "0.95"]:
        count = arb_opportunities[threshold]
        print(f"    < ${threshold}:  {count}/{len(markets)} markets")
        if count > 0:
            af = arb_fillable[threshold]
            print(f"      Fillable (BOTH sides):  FOK@10={af['fok10']}/{count}  FOK@5={af['fok5']}/{count}  FOK@3={af['fok3']}/{count}  GTC@10={af['gtc10']}/{count}")

    # ═══ VERDICT ═══
    print(f"\n{'=' * 80}")
    print("VERDICT")
    print(f"{'=' * 80}")

    fok10_rate = results["fok_10"]["fill"] / total_sides * 100 if total_sides else 0
    fok5_rate = results["fok_5"]["fill"] / total_sides * 100 if total_sides else 0
    fok3_rate = results["fok_3"]["fill"] / total_sides * 100 if total_sides else 0
    gtc10_rate = results["gtc_10"]["fill"] / total_sides * 100 if total_sides else 0

    best_approach = max([
        ("FOK 10", fok10_rate), ("FOK 5", fok5_rate),
        ("FOK 3", fok3_rate), ("GTC 10", gtc10_rate)
    ], key=lambda x: x[1])

    print(f"\n  Best approach: {best_approach[0]} ({best_approach[1]:.0f}% fill rate)")

    # For arb specifically, both sides must fill
    print(f"\n  For ARB (both sides must fill):")
    arb99 = arb_opportunities["0.99"]
    if arb99:
        af = arb_fillable["0.99"]
        for label, key in [("FOK@10", "fok10"), ("FOK@5", "fok5"), ("FOK@3", "fok3"), ("GTC@10", "gtc10")]:
            rate = af[key] / arb99 * 100
            print(f"    {label} at <$0.99: {af[key]}/{arb99} = {rate:.0f}%")

    arb97 = arb_opportunities["0.97"]
    if arb97:
        af = arb_fillable["0.97"]
        for label, key in [("FOK@10", "fok10"), ("FOK@5", "fok5"), ("FOK@3", "fok3"), ("GTC@10", "gtc10")]:
            rate = af[key] / arb97 * 100
            print(f"    {label} at <$0.97: {af[key]}/{arb97} = {rate:.0f}%")


if __name__ == "__main__":
    main()
