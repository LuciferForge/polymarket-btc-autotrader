#!/usr/bin/env python3
"""
momentum_bot.py — BTC Momentum Scalper for Polymarket Daily Markets

Watches Binance BTC price via WebSocket. When momentum pushes BTC toward
a daily threshold (e.g. $74K), buys the corresponding Polymarket YES token
before the market reprices.

Strategy:
- Track real-time BTC price from Binance
- Find daily "BTC above $X on [date]?" markets on Polymarket
- When BTC moves >0.3% toward a threshold in <5 min, buy YES at current price
- When BTC moves away from a threshold, buy NO (or sell YES)
- $2-3 per trade, max 3 concurrent, auto kill-switch at $5 daily loss

Usage:
  python3 momentum_bot.py scan          # Show live markets + BTC price
  python3 momentum_bot.py run           # Start the bot (DRY_RUN by default)
  python3 momentum_bot.py run --live    # Enable real orders
  python3 momentum_bot.py stats         # Show trade history + win rate
"""

import sys
import os
import json
import time
import sqlite3
import threading
import signal
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
import websocket  # websocket-client

# ─── Project imports ─────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))
import config

# ─── Logging ─────────────────────────────────────────────────────────────────
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [MOMENTUM] %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "momentum.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("momentum")

# ─── Constants ───────────────────────────────────────────────────────────────
BINANCE_WS = "wss://ws-api.binance.com/ws-api/v3"
# stream.binance.com has no DNS A record on this network — use fstream instead
BINANCE_STREAM = "wss://fstream.binance.com/ws/btcusdt@aggTrade"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

DB_PATH = Path(__file__).parent / "momentum.db"

# Strategy params
MOMENTUM_THRESHOLD_PCT = 0.30    # 0.3% move = signal
MOMENTUM_WINDOW_SEC = 300        # Measure momentum over 5 min
PROXIMITY_USD = 2000             # Only trade markets within $2K of current price
ORDER_SIZE_USD = 2.0             # Per-trade size
MAX_CONCURRENT = 3               # Max open positions
DAILY_LOSS_CAP = 5.0             # Stop trading after $5 loss
MIN_MARKET_VOLUME = 10000        # Minimum 24h volume
COOLDOWN_SEC = 180               # Don't re-enter same market within 3 min
MAX_HOURS_TO_RESOLVE = 24        # NEVER enter markets resolving > 24h out
PRICE_HISTORY_SIZE = 120         # Keep 2 min of tick data (1/sec)

# ─── State ───────────────────────────────────────────────────────────────────
btc_price = {"current": 0.0, "history": [], "lock": threading.Lock()}
running = True
dry_run = True


# ─── Database ────────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            market_question TEXT,
            token_id TEXT,
            side TEXT,
            entry_price REAL,
            size_usd REAL,
            shares REAL,
            btc_price_at_entry REAL,
            btc_momentum_pct REAL,
            strike_price REAL,
            status TEXT DEFAULT 'OPEN',
            exit_price REAL,
            pnl REAL,
            resolved_at TEXT,
            dry_run INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS price_log (
            timestamp TEXT NOT NULL,
            btc_price REAL NOT NULL
        );
    """)
    conn.commit()
    conn.close()


def log_trade(trade: dict):
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    c.execute("""
        INSERT INTO trades (timestamp, market_question, token_id, side,
            entry_price, size_usd, shares, btc_price_at_entry,
            btc_momentum_pct, strike_price, status, dry_run)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)
    """, (
        datetime.now(timezone.utc).isoformat(),
        trade["question"],
        trade["token_id"],
        trade["side"],
        trade["entry_price"],
        trade["size_usd"],
        trade["shares"],
        trade["btc_price"],
        trade["momentum_pct"],
        trade["strike"],
        1 if dry_run else 0,
    ))
    conn.commit()
    conn.close()


def get_daily_pnl() -> float:
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    c.execute(
        "SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE timestamp LIKE ? AND pnl IS NOT NULL",
        (f"{today}%",)
    )
    pnl = c.fetchone()[0]
    conn.close()
    return pnl


def get_open_positions() -> list:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM trades WHERE status = 'OPEN'")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def get_recent_trade_tokens() -> set:
    """Get token_ids traded within cooldown period."""
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=COOLDOWN_SEC)).isoformat()
    c.execute("SELECT token_id FROM trades WHERE timestamp > ?", (cutoff,))
    tokens = {r[0] for r in c.fetchall()}
    conn.close()
    return tokens


# ─── Binance WebSocket ───────────────────────────────────────────────────────
def poll_binance_price():
    """Poll Binance REST API for BTC price (fallback if WS fails)."""
    global running
    while running:
        try:
            resp = requests.get(
                "https://api.binance.com/api/v3/ticker/price",
                params={"symbol": "BTCUSDT"},
                timeout=5,
            )
            price = float(resp.json()["price"])
            now = time.time()
            with btc_price["lock"]:
                btc_price["current"] = price
                btc_price["history"].append((now, price))
                # Trim history
                cutoff = now - MOMENTUM_WINDOW_SEC
                btc_price["history"] = [
                    (t, p) for t, p in btc_price["history"] if t > cutoff
                ]
        except Exception as e:
            log.warning(f"Binance REST poll failed: {e}")
        time.sleep(2)


def start_binance_ws():
    """Start Binance WebSocket for real-time BTC price."""
    def on_message(ws, message):
        try:
            data = json.loads(message)
            price = float(data.get("p", 0))
            if price > 0:
                now = time.time()
                with btc_price["lock"]:
                    btc_price["current"] = price
                    btc_price["history"].append((now, price))
                    cutoff = now - MOMENTUM_WINDOW_SEC
                    btc_price["history"] = [
                        (t, p) for t, p in btc_price["history"] if t > cutoff
                    ]
        except Exception:
            pass

    def on_error(ws, error):
        log.warning(f"Binance WS error: {error}")

    def on_close(ws, code, msg):
        log.info("Binance WS closed, falling back to REST polling")
        # Start REST fallback
        threading.Thread(target=poll_binance_price, daemon=True).start()

    def on_open(ws):
        log.info("Binance WS connected")

    try:
        ws = websocket.WebSocketApp(
            BINANCE_STREAM,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
            on_open=on_open,
        )
        ws.run_forever()
    except Exception as e:
        log.warning(f"Binance WS failed to start: {e}, using REST fallback")
        poll_binance_price()


def get_momentum() -> float:
    """Calculate BTC momentum as % change over the window."""
    with btc_price["lock"]:
        if len(btc_price["history"]) < 5:
            return 0.0
        oldest_price = btc_price["history"][0][1]
        current = btc_price["current"]
        if oldest_price == 0:
            return 0.0
        return ((current - oldest_price) / oldest_price) * 100


# ─── Polymarket Market Discovery ─────────────────────────────────────────────
def find_btc_daily_markets() -> list:
    """Find active BTC daily threshold markets."""
    markets = []
    try:
        resp = requests.get(f"{GAMMA_API}/events", params={
            "limit": 50,
            "active": "true",
            "closed": "false",
            "order": "volume24hr",
            "ascending": "false",
        }, timeout=15)
        events = resp.json()
    except Exception as e:
        log.error(f"Failed to fetch events: {e}")
        return []

    for event in events:
        title = event.get("title", "").lower()
        # Match "Bitcoin above ___ on March X?" events
        if "bitcoin above" not in title:
            continue

        for m in event.get("markets", []):
            q = m.get("question", "")
            vol = float(m.get("volume24hr", 0))
            active = m.get("active", False)
            closed = m.get("closed", False)
            tokens = m.get("clobTokenIds", [])
            prices_raw = m.get("outcomePrices", "[]")

            if not active or closed or vol < MIN_MARKET_VOLUME or len(tokens) < 2:
                continue

            # HARD RULE: Never lock capital > 24h
            end_date = m.get("endDate", "")
            if end_date:
                try:
                    end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                    hours_left = (end_dt - datetime.now(timezone.utc)).total_seconds() / 3600
                    if hours_left > MAX_HOURS_TO_RESOLVE or hours_left < 0.5:
                        continue  # Too far out or about to close
                except Exception:
                    continue

            # Parse prices
            if isinstance(prices_raw, str):
                prices = json.loads(prices_raw)
            else:
                prices = prices_raw
            yes_price = float(prices[0]) if prices else 0
            no_price = float(prices[1]) if len(prices) > 1 else 0

            # Extract strike price from question
            strike = extract_strike(q)
            if strike is None:
                continue

            # Skip already-resolved (YES at 0.999+)
            if yes_price >= 0.995 or no_price >= 0.995:
                continue

            markets.append({
                "question": q,
                "strike": strike,
                "yes_token": tokens[0],
                "no_token": tokens[1],
                "yes_price": yes_price,
                "no_price": no_price,
                "volume24h": vol,
                "end_date": m.get("endDate", ""),
                "condition_id": m.get("conditionId", ""),
            })

    # Sort by proximity to current BTC price
    current = btc_price["current"]
    if current > 0:
        markets.sort(key=lambda m: abs(m["strike"] - current))

    return markets


def extract_strike(question: str) -> float | None:
    """Extract dollar threshold from question like 'Will the price of Bitcoin be above $74,000 on March 5?'"""
    import re
    match = re.search(r'\$([0-9,]+)', question)
    if match:
        return float(match.group(1).replace(",", ""))
    return None


def get_orderbook_price(token_id: str) -> dict:
    """Get best bid/ask for a token."""
    try:
        resp = requests.get(
            f"{CLOB_API}/book",
            params={"token_id": token_id},
            timeout=10,
        )
        resp.raise_for_status()
        book = resp.json()
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        best_bid = float(bids[0]["price"]) if bids else 0
        best_ask = float(asks[0]["price"]) if asks else 1
        return {"bid": best_bid, "ask": best_ask, "mid": (best_bid + best_ask) / 2}
    except Exception:
        return {"bid": 0, "ask": 1, "mid": 0.5}


# ─── Order Execution ─────────────────────────────────────────────────────────
_clob_client = None

def get_clob():
    global _clob_client
    if _clob_client is None:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds
        creds = ApiCreds(
            api_key=config.API_KEY,
            api_secret=config.API_SECRET,
            api_passphrase=config.API_PASSPHRASE,
        )
        _clob_client = ClobClient(
            host=config.CLOB_API,
            key=config.PRIVATE_KEY,
            chain_id=config.CHAIN_ID,
            signature_type=2,
            funder=config.PROXY_ADDRESS,
            creds=creds,
        )
    return _clob_client


def place_order(token_id: str, side: str, price: float, shares: float) -> dict | None:
    """Place a real order via CLOB."""
    from py_clob_client.clob_types import OrderArgs, OrderType

    client = get_clob()
    order_args = OrderArgs(
        token_id=token_id,
        price=round(price, 2),
        size=round(shares, 1),
        side=side,
    )
    try:
        signed = client.create_order(order_args)
        resp = client.post_order(signed, OrderType.GTC)
        return resp
    except Exception as e:
        log.error(f"Order failed: {e}")
        return None


# ─── Strategy Logic ──────────────────────────────────────────────────────────
def evaluate_signals(markets: list) -> list:
    """Evaluate which markets have a tradeable signal."""
    signals = []
    current_btc = btc_price["current"]
    momentum = get_momentum()

    if current_btc == 0:
        return signals

    recent_tokens = get_recent_trade_tokens()
    open_positions = get_open_positions()
    open_tokens = {p["token_id"] for p in open_positions}

    for m in markets:
        strike = m["strike"]
        distance = current_btc - strike  # positive = above strike
        distance_pct = (distance / strike) * 100

        # Only trade markets where BTC is within PROXIMITY_USD of strike
        if abs(distance) > PROXIMITY_USD:
            continue

        yes_token = m["yes_token"]
        no_token = m["no_token"]

        # Skip if we already have a position or recently traded
        if yes_token in open_tokens or no_token in open_tokens:
            continue
        if yes_token in recent_tokens or no_token in recent_tokens:
            continue

        # --- SIGNAL: BTC momentum pushing TOWARD strike from above ---
        # BTC above strike + positive momentum = YES is getting more likely
        if distance > 0 and momentum > MOMENTUM_THRESHOLD_PCT:
            # BTC is above strike and pumping further up — YES should be worth more
            # Only if YES is still underpriced (< 0.85)
            if m["yes_price"] < 0.85:
                signals.append({
                    "market": m,
                    "action": "BUY_YES",
                    "token_id": yes_token,
                    "reason": f"BTC ${current_btc:.0f} above ${strike:.0f} strike, momentum +{momentum:.2f}%",
                    "price": m["yes_price"],
                })

        # --- SIGNAL: BTC pumping toward strike from below ---
        elif distance < 0 and distance > -PROXIMITY_USD and momentum > MOMENTUM_THRESHOLD_PCT:
            # BTC below strike but pumping toward it
            if m["yes_price"] < 0.40:  # Only buy cheap YES options
                signals.append({
                    "market": m,
                    "action": "BUY_YES",
                    "token_id": yes_token,
                    "reason": f"BTC ${current_btc:.0f} pumping toward ${strike:.0f}, momentum +{momentum:.2f}%",
                    "price": m["yes_price"],
                })

        # --- SIGNAL: BTC dumping away from strike ---
        elif distance > 0 and distance < PROXIMITY_USD and momentum < -MOMENTUM_THRESHOLD_PCT:
            # BTC above strike but dumping toward it
            if m["no_price"] < 0.40:  # Buy cheap NO
                signals.append({
                    "market": m,
                    "action": "BUY_NO",
                    "token_id": no_token,
                    "reason": f"BTC ${current_btc:.0f} dumping toward ${strike:.0f}, momentum {momentum:.2f}%",
                    "price": m["no_price"],
                })

        # --- SIGNAL: BTC below strike and dumping further ---
        elif distance < 0 and momentum < -MOMENTUM_THRESHOLD_PCT:
            if m["no_price"] < 0.85:
                signals.append({
                    "market": m,
                    "action": "BUY_NO",
                    "token_id": no_token,
                    "reason": f"BTC ${current_btc:.0f} below ${strike:.0f} and dumping, momentum {momentum:.2f}%",
                    "price": m["no_price"],
                })

    return signals


def execute_signal(signal: dict):
    """Execute a trading signal."""
    m = signal["market"]
    token_id = signal["token_id"]
    action = signal["action"]
    side = "BUY"

    # Get live orderbook price
    book = get_orderbook_price(token_id)
    entry_price = book["ask"]  # We're buying, so we hit the ask

    if entry_price <= 0.01 or entry_price >= 0.99:
        log.info(f"SKIP: {m['question'][:50]} — price {entry_price} out of range")
        return

    shares = round(ORDER_SIZE_USD / entry_price, 1)

    trade = {
        "question": m["question"],
        "token_id": token_id,
        "side": action,
        "entry_price": entry_price,
        "size_usd": ORDER_SIZE_USD,
        "shares": shares,
        "btc_price": btc_price["current"],
        "momentum_pct": get_momentum(),
        "strike": m["strike"],
    }

    if dry_run:
        log.info(f"[DRY RUN] {action}: {shares:.1f} shares of '{m['question'][:50]}' @ ${entry_price:.3f}")
        log.info(f"  Reason: {signal['reason']}")
        log_trade(trade)
    else:
        log.info(f"[LIVE] {action}: {shares:.1f} shares @ ${entry_price:.3f}")
        log.info(f"  Reason: {signal['reason']}")
        resp = place_order(token_id, side, entry_price, shares)
        if resp:
            log.info(f"  Order ID: {resp.get('orderID', 'N/A')}")
            log_trade(trade)
        else:
            log.error(f"  Order FAILED for {m['question'][:50]}")


# ─── Commands ────────────────────────────────────────────────────────────────
def cmd_scan():
    """Show live markets + BTC price."""
    # Get BTC price via REST (quick check)
    try:
        resp = requests.get(
            "https://api.binance.com/api/v3/ticker/price",
            params={"symbol": "BTCUSDT"}, timeout=5,
        )
        price = float(resp.json()["price"])
        btc_price["current"] = price
    except Exception as e:
        print(f"Failed to get BTC price: {e}")
        return

    print(f"\nBTC Price: ${price:,.2f}")
    print(f"{'='*70}")

    markets = find_btc_daily_markets()
    if not markets:
        print("No active BTC daily markets found.")
        return

    print(f"\nFound {len(markets)} tradeable BTC markets:\n")
    for m in markets:
        strike = m["strike"]
        distance = price - strike
        direction = "ABOVE" if distance > 0 else "BELOW"
        proximity = abs(distance)

        # Highlight battleground markets
        marker = " ***" if proximity < PROXIMITY_USD else ""

        print(f"  {m['question'][:65]}")
        print(f"    YES: ${m['yes_price']:.3f} | NO: ${m['no_price']:.3f} | "
              f"Vol: ${m['volume24h']:,.0f} | BTC {direction} by ${proximity:,.0f}{marker}")


def cmd_run():
    """Main bot loop."""
    global running, dry_run

    if "--live" in sys.argv:
        dry_run = False
        log.warning("LIVE TRADING ENABLED — real orders will be placed!")
    else:
        log.info("DRY RUN mode — no real orders")

    init_db()

    # Start Binance price feed in background
    log.info("Starting Binance price feed...")
    ws_thread = threading.Thread(target=start_binance_ws, daemon=True)
    ws_thread.start()

    # Also start REST polling as backup
    rest_thread = threading.Thread(target=poll_binance_price, daemon=True)
    rest_thread.start()

    # Wait for first price
    log.info("Waiting for BTC price...")
    for _ in range(30):
        if btc_price["current"] > 0:
            break
        time.sleep(1)

    if btc_price["current"] == 0:
        log.error("Failed to get BTC price. Exiting.")
        return

    log.info(f"BTC price: ${btc_price['current']:,.2f}")
    log.info(f"Order size: ${ORDER_SIZE_USD} | Max concurrent: {MAX_CONCURRENT}")
    log.info(f"Momentum threshold: {MOMENTUM_THRESHOLD_PCT}% over {MOMENTUM_WINDOW_SEC}s")
    log.info(f"Daily loss cap: ${DAILY_LOSS_CAP}")

    # Handle Ctrl+C
    def signal_handler(sig, frame):
        global running
        running = False
        log.info("Shutting down...")

    signal.signal(signal.SIGINT, signal_handler)

    cycle = 0
    while running:
        cycle += 1
        try:
            current = btc_price["current"]
            momentum = get_momentum()

            if cycle % 10 == 1:
                log.info(f"BTC: ${current:,.2f} | Momentum: {momentum:+.3f}% | "
                         f"History: {len(btc_price['history'])} ticks")

            # Check daily loss cap
            daily_pnl = get_daily_pnl()
            if daily_pnl < -DAILY_LOSS_CAP:
                log.warning(f"DAILY LOSS CAP HIT: ${daily_pnl:.2f}. Stopping.")
                break

            # Check concurrent positions
            open_pos = get_open_positions()
            if len(open_pos) >= MAX_CONCURRENT:
                if cycle % 30 == 1:
                    log.info(f"Max concurrent positions ({MAX_CONCURRENT}). Waiting.")
                time.sleep(10)
                continue

            # Need enough price history for momentum calc
            if len(btc_price["history"]) < 10:
                time.sleep(5)
                continue

            # Scan markets every 30 seconds
            if cycle % 6 == 1:
                markets = find_btc_daily_markets()
                if markets and cycle % 30 == 1:
                    log.info(f"Tracking {len(markets)} BTC markets "
                             f"(nearest strike: ${markets[0]['strike']:,.0f})")

                # Evaluate signals
                signals = evaluate_signals(markets)
                for sig in signals[:1]:  # Max 1 trade per cycle
                    log.info(f"SIGNAL: {sig['action']} — {sig['reason']}")
                    execute_signal(sig)

            time.sleep(5)

        except Exception as e:
            log.error(f"Cycle error: {e}")
            time.sleep(10)

    log.info("Bot stopped.")


def cmd_stats():
    """Show trade history and win rate."""
    init_db()
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.execute("SELECT COUNT(*) FROM trades")
    total = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM trades WHERE status = 'OPEN'")
    open_count = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM trades WHERE pnl > 0")
    wins = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM trades WHERE pnl IS NOT NULL AND pnl <= 0")
    losses = c.fetchone()[0]

    c.execute("SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE pnl IS NOT NULL")
    total_pnl = c.fetchone()[0]

    resolved = wins + losses
    win_rate = (wins / resolved * 100) if resolved > 0 else 0

    print(f"\n{'='*50}")
    print(f"MOMENTUM BOT STATS")
    print(f"{'='*50}")
    print(f"Total trades: {total}")
    print(f"Open: {open_count}")
    print(f"Resolved: {resolved} (W: {wins} / L: {losses})")
    print(f"Win rate: {win_rate:.1f}%")
    print(f"Total P&L: ${total_pnl:.2f}")
    print()

    # Recent trades
    c.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 10")
    trades = c.fetchall()
    if trades:
        print("Recent trades:")
        for t in trades:
            mode = "DRY" if t["dry_run"] else "LIVE"
            pnl_str = f"${t['pnl']:.2f}" if t["pnl"] is not None else "pending"
            print(f"  [{mode}] {t['side']} | {t['market_question'][:45]}...")
            print(f"    Entry: ${t['entry_price']:.3f} | BTC: ${t['btc_price_at_entry']:,.0f} | "
                  f"Mom: {t['btc_momentum_pct']:+.2f}% | P&L: {pnl_str}")

    conn.close()


# ─── Main ────────────────────────────────────────────────────────────────────
def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    cmd = sys.argv[1]
    if cmd == "scan":
        cmd_scan()
    elif cmd == "run":
        cmd_run()
    elif cmd == "stats":
        cmd_stats()
    else:
        print(f"Unknown command: {cmd}")
        print("Commands: scan, run, stats")


if __name__ == "__main__":
    main()
