#!/usr/bin/env python3
"""
sell_positions.py — Sell all live Polymarket positions to free up USDC.

Fetches positions from data-api, checks orderbooks, places sell orders
at best bid for quick fills.
"""

import sys
import time
import json
import requests

# Add project root to path
sys.path.insert(0, "/Users/apple/Documents/LuciferForge/polymarket-ai")
import config

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType

PROXY_ADDRESS = config.PROXY_ADDRESS
DATA_API = "https://data-api.polymarket.com"

def get_positions():
    """Fetch live positions from data-api."""
    resp = requests.get(
        f"{DATA_API}/positions",
        params={"user": PROXY_ADDRESS, "sizeThreshold": "0.1"},
        timeout=15,
    )
    resp.raise_for_status()
    positions = resp.json()
    # Filter to non-zero, non-resolved
    live = [p for p in positions if float(p.get("size", 0)) > 0.1]
    return live


def get_best_bid(token_id: str) -> float:
    """Get best bid price from CLOB orderbook."""
    try:
        resp = requests.get(
            f"{config.CLOB_API}/book",
            params={"token_id": token_id},
            timeout=10,
        )
        resp.raise_for_status()
        book = resp.json()
        bids = book.get("bids", [])
        if bids:
            return float(bids[0]["price"])
    except Exception as e:
        print(f"  Orderbook error for {token_id[:20]}...: {e}")
    return 0.0


def init_clob():
    """Initialize authenticated CLOB client."""
    creds = ApiCreds(
        api_key=config.API_KEY,
        api_secret=config.API_SECRET,
        api_passphrase=config.API_PASSPHRASE,
    )
    client = ClobClient(
        host=config.CLOB_API,
        key=config.PRIVATE_KEY,
        chain_id=config.CHAIN_ID,
        signature_type=2,
        funder=config.PROXY_ADDRESS,
        creds=creds,
    )
    return client


def sell_position(client, token_id: str, shares: float, price: float):
    """Place a sell order for exact share count at given price."""
    # Round shares down to avoid overselling
    shares = round(shares, 1)
    price = round(price, 2)

    if price < 0.01:
        print(f"  SKIP: price {price} too low to sell")
        return None
    if shares < 1:
        print(f"  SKIP: only {shares} shares, not worth selling")
        return None

    order_args = OrderArgs(
        token_id=token_id,
        price=price,
        size=shares,
        side="SELL",
    )
    try:
        signed = client.create_order(order_args)
        resp = client.post_order(signed, OrderType.GTC)
        return resp
    except Exception as e:
        print(f"  ORDER FAILED: {e}")
        return None


def main():
    print("Fetching positions...")
    positions = get_positions()

    if not positions:
        print("No live positions found.")
        return

    print(f"Found {len(positions)} live positions\n")

    # Show positions and orderbook state
    sell_plan = []
    for p in positions:
        token_id = p.get("asset", "")
        size = float(p.get("size", 0))
        title = p.get("market", {}).get("question", p.get("title", "Unknown"))[:60]

        best_bid = get_best_bid(token_id)
        est_value = size * best_bid

        print(f"  {title}")
        print(f"    Shares: {size:.1f} | Best bid: ${best_bid:.3f} | Est value: ${est_value:.2f}")

        if best_bid >= 0.01 and size >= 1:
            sell_plan.append((token_id, size, best_bid, title, est_value))
        else:
            print(f"    -> SKIP (no viable bid)")
        time.sleep(0.3)  # Rate limit

    total_est = sum(x[4] for x in sell_plan)
    print(f"\nSell plan: {len(sell_plan)} positions, est ~${total_est:.2f} USDC")

    if not sell_plan:
        print("Nothing to sell.")
        return

    # Execute sells
    print("\nInitializing CLOB client...")
    client = init_clob()

    print("Placing sell orders...\n")
    results = []
    for token_id, shares, price, title, est in sell_plan:
        print(f"  SELL {shares:.1f} shares of '{title[:40]}...' @ ${price:.3f}")
        resp = sell_position(client, token_id, shares, price)
        if resp:
            order_id = resp.get("orderID", "N/A")
            print(f"    -> Order placed: {order_id}")
            results.append({"title": title, "shares": shares, "price": price, "order_id": order_id})
        else:
            print(f"    -> Failed")
        time.sleep(0.5)  # Rate limit between orders

    print(f"\n{'='*50}")
    print(f"SUMMARY: {len(results)}/{len(sell_plan)} sell orders placed")
    print(f"Estimated USDC recovery: ~${total_est:.2f}")
    print(f"Orders are GTC — check back in a few minutes for fills.")


if __name__ == "__main__":
    main()
