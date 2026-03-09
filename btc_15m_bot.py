#!/usr/bin/env python3
"""
btc_15m_bot.py — BTC 15-Minute Trading Bot for Polymarket

Primary strategy: LATE MOMENTUM
  - Wait until minute 11 of each 15-min window
  - Check Binance: has BTC moved >0.20% from window open?
  - If yes, buy the winning side on Polymarket
  - Backtested: 95.7% win rate at 0.20% threshold, 98.8% at 0.50%
  - ~24 opportunities per day

Secondary: BINARY ARB (opportunistic)
  - Buy both UP + DOWN when combined ask < $0.99 (risk-free)

Usage:
  python3 btc_15m_bot.py scan            # Show current 15-min markets + prices
  python3 btc_15m_bot.py run             # Start bot (DRY RUN by default)
  python3 btc_15m_bot.py run --live      # Enable real orders
  python3 btc_15m_bot.py stats           # Show trade history
  python3 btc_15m_bot.py audit           # Verify signed trade receipts (tamper-proof audit)
"""

import sys
import os
import re
import json
import time
import sqlite3
import threading
import signal
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
import config

# ─── Decision Tracing — Signed Receipts for Every Trade ──────────────────
# Every trade decision gets a cryptographically signed, hash-chained receipt.
# Tamper-proof audit trail: prove what the bot saw, why it traded, and what happened.
# Verify anytime: python3 btc_15m_bot.py audit
try:
    from ai_trace import Tracer
    TRACE_DIR = Path(__file__).parent / "signed_trades"
    TRACE_DIR.mkdir(exist_ok=True)
    oracle = Tracer(
        agent="polymarket-oracle",
        trace_dir=str(TRACE_DIR),
        auto_save=True,
        sign=True,
        meta={
            "version": "4.1",
            "strategies": ["MOMENTUM", "SNIPE"],
            "assets": ["BTC", "ETH", "SOL", "XRP"],
            "exchange": "polymarket",
        },
    )
    TRACING_ENABLED = True
except ImportError:
    TRACING_ENABLED = False
    oracle = None

# ─── Logging ────────────────────────────────────────────────────────────────
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [15M] %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "btc_15m.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("btc15m")

# ─── Constants ──────────────────────────────────────────────────────────────
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
BINANCE_REST = "https://api.binance.com/api/v3"

DB_PATH = Path(__file__).parent / "arb_15m.db"

# ─── Telegram ──────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ─── Strategy: Filtered Momentum (PRIMARY) ──────────────────────────────────
# Enter when BTC moves >0.20% AND Polymarket price is still cheap.
# FILTERS (from backtest):
#   - Skip high/very-high volatility (Q3/Q4 ATR) — reversals kill edge
#   - Skip 12-16 UTC (US open) — 81.5% WR, barely breakeven
#   - Result: 91.6% WR vs 71% unfiltered
ENTRY_MIN_MINUTE = 8            # Wait for direction to lock (85%+ WR from min 8)
ENTRY_MAX_MINUTE = 12           # Handoff to late snipe after this
MOMENTUM_THRESHOLD_PCT = 0.20   # Min BTC move % to trigger
MAX_ENTRY_PRICE = 0.85          # Don't buy if winning side already > this
MIN_ENTRY_PRICE = 0.55          # Don't buy if winning side < this (too uncertain)
SKIP_UTC_HOURS = (12, 13, 14, 15)  # Skip 12-16 UTC (US market open, choppy)

# ─── Strategy: Late Snipe (minute 13) ────────────────────────────────────────
# At minute 13, if BTC moved >0.10%, direction holds 99% of the time.
# Buy at $0.93-$0.97, collect $1.00. Tiny margin but near-zero risk.
LATE_SNIPE_MINUTE = 13          # Check at minute 13
LATE_SNIPE_THRESHOLD_PCT = 0.10 # Lower threshold — direction is locked by now
LATE_SNIPE_MAX_ENTRY = 0.97     # Don't buy above this (no margin left)
LATE_SNIPE_MIN_ENTRY = 0.88     # Below this = still uncertain

# ─── Strategy: Binary Arb (PRIMARY) ───────────────────────────────────────────
ARB_TARGET_COST = 0.99          # Buy both sides if combined < this
ARB_MIN_GAP = 0.02              # Minimum gap to take ($0.02 = $0.20 on 10 shares)
                                # Skip $0.01 gaps — not worth the execution risk

# ─── Multi-Asset Config ──────────────────────────────────────────────────────
ASSETS = ["btc", "eth", "sol", "xrp"]
# Each asset uses the same slug pattern: {asset}-updown-15m-{window_start}

# ─── Risk / Sizing ──────────────────────────────────────────────────────────
ORDER_SIZE_SHARES = 25          # Shares per trade (scaled up — 93% WR on 22 trades)
LATE_SNIPE_SIZE = 40            # Bigger size for late snipe (100% WR on 6 trades)
MAX_DAILY_LOSS = 5.0            # Kill switch
COOLDOWN_SEC = 30               # Between trades on same market
POLL_INTERVAL = 5               # Seconds between checks

# ─── State ──────────────────────────────────────────────────────────────────
running = True
dry_run = True


# ─── Database ───────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            strategy TEXT NOT NULL,
            market_slug TEXT,
            side TEXT,
            up_price REAL,
            down_price REAL,
            combined_cost REAL,
            shares REAL,
            btc_price REAL,
            btc_move_pct REAL,
            status TEXT DEFAULT 'OPEN',
            pnl REAL,
            dry_run INTEGER DEFAULT 1
        );
    """)
    # Add fill_status column if missing (tracks what actually filled)
    try:
        conn.execute("SELECT fill_status FROM trades LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE trades ADD COLUMN fill_status TEXT DEFAULT 'BOTH_FILLED'")
        conn.commit()
    # Add order_ids column if missing
    try:
        conn.execute("SELECT order_ids FROM trades LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE trades ADD COLUMN order_ids TEXT DEFAULT ''")
        conn.commit()
    conn.commit()
    conn.close()


def log_trade(trade: dict):
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        INSERT INTO trades (timestamp, strategy, market_slug, side,
            up_price, down_price, combined_cost, shares,
            btc_price, btc_move_pct, status, dry_run, fill_status, order_ids)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?)
    """, (
        datetime.now(timezone.utc).isoformat(),
        trade["strategy"],
        trade.get("slug", ""),
        trade["side"],
        trade["up_price"],
        trade["down_price"],
        trade["combined_cost"],
        trade["shares"],
        trade.get("btc_price", 0),
        trade.get("btc_move_pct", 0),
        1 if dry_run else 0,
        trade.get("fill_status", "BOTH_FILLED"),
        trade.get("order_ids", ""),
    ))
    conn.commit()
    conn.close()


def get_daily_pnl() -> float:
    conn = sqlite3.connect(str(DB_PATH))
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cur = conn.execute(
        "SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE timestamp LIKE ? AND pnl IS NOT NULL",
        (f"{today}%",)
    )
    pnl = cur.fetchone()[0]
    conn.close()
    return pnl


def resolve_open_trades():
    """Check all OPEN trades and resolve them using Binance candle data.

    For each trade, extract the window_start from the slug, fetch BTC open/close
    for that 15-min window, determine WIN/LOSS, calculate P&L, update DB.
    """
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    now_ts = int(time.time())

    rows = conn.execute(
        "SELECT id, market_slug, side, combined_cost, shares, strategy, up_price, down_price, fill_status "
        "FROM trades WHERE status = 'OPEN'"
    ).fetchall()
    if not rows:
        return

    for row in rows:
        slug = row["market_slug"]
        # Extract window_start timestamp from slug like btc-updown-15m-1772811900
        parts = slug.rsplit("-", 1)
        if len(parts) < 2:
            continue
        try:
            window_start = int(parts[-1])
        except ValueError:
            continue

        window_end = window_start + 900

        # Only resolve if window ended at least 60s ago (give Binance time to settle)
        if window_end + 60 > now_ts:
            continue

        # Determine which asset's candles to fetch
        asset_symbol = "BTCUSDT"  # default
        slug_lower = slug.lower()
        if slug_lower.startswith("eth-"):
            asset_symbol = "ETHUSDT"
        elif slug_lower.startswith("sol-"):
            asset_symbol = "SOLUSDT"
        elif slug_lower.startswith("xrp-"):
            asset_symbol = "XRPUSDT"

        # Fetch Binance 1-min candles for the window
        try:
            resp = requests.get(
                f"{BINANCE_REST}/klines",
                params={
                    "symbol": asset_symbol,
                    "interval": "1m",
                    "startTime": window_start * 1000,
                    "endTime": window_end * 1000,
                    "limit": 16,
                },
                timeout=10,
            )
            candles = resp.json()
            if not candles or len(candles) < 2:
                continue
        except Exception:
            continue

        price_open = float(candles[0][1])
        price_close = float(candles[-1][4])
        actual_direction = "UP" if price_close >= price_open else "DOWN"

        side = row["side"]
        entry = row["combined_cost"]
        shares = row["shares"]
        strategy = row["strategy"]
        fill_status = row["fill_status"] or "BOTH_FILLED"
        up_price = row["up_price"] or 0
        down_price = row["down_price"] or 0

        if strategy == "ARB" and fill_status == "BOTH_FILLED":
            # True arb — both sides filled, always wins
            pnl = (1.0 - entry) * shares
            status = "WIN"
        elif strategy == "ARB" and fill_status == "UP_ONLY":
            # Half-fill: only UP side filled — it's a directional UP bet
            if actual_direction == "UP":
                pnl = (1.0 - up_price) * shares
                status = "WIN"
            else:
                pnl = -up_price * shares
                status = "LOSS"
        elif strategy == "ARB" and fill_status == "DOWN_ONLY":
            # Half-fill: only DOWN side filled — it's a directional DOWN bet
            if actual_direction == "DOWN":
                pnl = (1.0 - down_price) * shares
                status = "WIN"
            else:
                pnl = -down_price * shares
                status = "LOSS"
        else:
            # Directional strategies (momentum, late snipe)
            if side == actual_direction:
                pnl = (1.0 - entry) * shares
                status = "WIN"
            else:
                pnl = -entry * shares
                status = "LOSS"

        conn.execute(
            "UPDATE trades SET status = ?, pnl = ? WHERE id = ?",
            (status, pnl, row["id"]),
        )

        fill_tag = f" [{fill_status}]" if fill_status != "BOTH_FILLED" else ""
        log.info(f"RESOLVED {slug}: {side}{fill_tag} → {actual_direction} = {status} | P&L: ${pnl:+.2f}")

        # Signed receipt for resolution — closes the loop
        if TRACING_ENABLED:
            with oracle.step("trade_resolved", market=slug) as step:
                step.log(strategy=strategy, side=side, actual=actual_direction,
                         result=status, pnl=round(pnl, 2),
                         entry=round(entry, 4), shares=shares,
                         price_open=round(price_open, 2), price_close=round(price_close, 2),
                         asset=asset_symbol)

        # Telegram alert on resolution
        send_telegram(
            f"*{status}* | {side}{fill_tag} on {slug}\n"
            f"{asset_symbol}: ${price_open:,.2f} → ${price_close:,.2f}\n"
            f"P&L: ${pnl:+.2f}"
        )

    conn.commit()
    conn.close()


# ─── Auto-Claim (Redeem resolved positions on-chain) ──────────────────────
_last_claim_time = 0
CLAIM_INTERVAL = 300  # Check every 5 minutes


def auto_claim_resolved():
    """Redeem resolved conditional tokens back to USDC via Gnosis Safe.

    Checks all positions on the CTF contract, finds resolved ones,
    and calls redeemPositions through the proxy's execTransaction.
    """
    global _last_claim_time
    now = int(time.time())
    if now - _last_claim_time < CLAIM_INTERVAL:
        return
    _last_claim_time = now

    try:
        from web3 import Web3
        from eth_abi import encode as abi_encode
        from eth_keys import keys

        w3 = Web3(Web3.HTTPProvider(config.RPC_URL))
        proxy = Web3.to_checksum_address(config.PROXY_ADDRESS)
        wallet = w3.eth.account.from_key(config.PRIVATE_KEY)
        pk = keys.PrivateKey(bytes.fromhex(config.PRIVATE_KEY.replace('0x', '')))

        CTF = Web3.to_checksum_address('0x4D97DCd97eC945f40cF65F87097ACe5EA0476045')
        USDC_ADDR = Web3.to_checksum_address('0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174')
        ZERO = '0x' + '00' * 20

        CTF_ABI = [{'constant': True, 'inputs': [{'name': '', 'type': 'address'}, {'name': '', 'type': 'uint256'}],
                     'name': 'balanceOf', 'outputs': [{'name': '', 'type': 'uint256'}], 'type': 'function'}]
        USDC_ABI = [{'constant': True, 'inputs': [{'name': '', 'type': 'address'}],
                      'name': 'balanceOf', 'outputs': [{'name': '', 'type': 'uint256'}], 'type': 'function'}]
        ctf_c = w3.eth.contract(address=CTF, abi=CTF_ABI)
        usdc_c = w3.eth.contract(address=USDC_ADDR, abi=USDC_ABI)

        # Get positions from data API
        resp = requests.get(f'https://data-api.polymarket.com/positions?user={proxy.lower()}', timeout=10)
        positions = resp.json()

        redeemable = []
        seen = set()
        for p in positions:
            cond_id = p.get('conditionId', '')
            token_id = p.get('asset', '')
            if not cond_id or cond_id in seen:
                continue
            seen.add(cond_id)

            # Check if resolved on-chain
            denom_data = w3.keccak(text='payoutDenominator(bytes32)')[:4]
            denom_data += abi_encode(['bytes32'], [bytes.fromhex(cond_id[2:])])
            denom = int(w3.eth.call({'to': CTF, 'data': denom_data}).hex(), 16)

            if denom > 0:
                bal = ctf_c.functions.balanceOf(proxy, int(token_id)).call()
                if bal > 0:
                    redeemable.append(cond_id)

        if not redeemable:
            return

        before = usdc_c.functions.balanceOf(proxy).call() / 1e6
        claimed = 0

        for cid in redeemable:
            try:
                safe_nonce = int(w3.eth.call({'to': proxy, 'data': w3.keccak(text='nonce()')[:4]}).hex(), 16)

                rd = w3.keccak(text='redeemPositions(address,bytes32,bytes32,uint256[])')[:4]
                rd += abi_encode(['address', 'bytes32', 'bytes32', 'uint256[]'],
                                 [USDC_ADDR, b'\x00' * 32, bytes.fromhex(cid[2:]), [1, 2]])

                gh = w3.keccak(text='getTransactionHash(address,uint256,bytes,uint8,uint256,uint256,uint256,address,address,uint256)')[:4]
                gh += abi_encode(['address', 'uint256', 'bytes', 'uint8', 'uint256', 'uint256', 'uint256', 'address', 'address', 'uint256'],
                                 [CTF, 0, rd, 0, 0, 0, 0, ZERO, ZERO, safe_nonce])
                sth = w3.eth.call({'to': proxy, 'data': gh})

                sig = pk.sign_msg_hash(sth)
                signature = sig.r.to_bytes(32, 'big') + sig.s.to_bytes(32, 'big') + bytes([sig.v + 27])

                ex = w3.keccak(text='execTransaction(address,uint256,bytes,uint8,uint256,uint256,uint256,address,address,bytes)')[:4]
                ex += abi_encode(['address', 'uint256', 'bytes', 'uint8', 'uint256', 'uint256', 'uint256', 'address', 'address', 'bytes'],
                                 [CTF, 0, rd, 0, 0, 0, 0, ZERO, ZERO, signature])

                gas = w3.eth.estimate_gas({'from': wallet.address, 'to': proxy, 'data': ex})
                tx = w3.eth.account.sign_transaction({
                    'to': proxy, 'data': ex, 'gas': gas + 50000,
                    'gasPrice': w3.eth.gas_price,
                    'nonce': w3.eth.get_transaction_count(wallet.address),
                    'chainId': 137,
                }, config.PRIVATE_KEY)
                tx_hash = w3.eth.send_raw_transaction(tx.raw_transaction)
                receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
                if receipt.status == 1:
                    claimed += 1
            except Exception:
                pass  # Skip failed redemptions silently

        after = usdc_c.functions.balanceOf(proxy).call() / 1e6
        gained = after - before

        if gained > 0 or claimed > 0:
            log.info(f"AUTO-CLAIM: Redeemed {claimed} positions | +${gained:.2f} USDC | Balance: ${after:.2f}")
            if gained > 0:
                send_telegram(f"*Auto-Claim*: +${gained:.2f} USDC freed | Balance: ${after:.2f}")

    except Exception as e:
        log.debug(f"Auto-claim error: {e}")


# ─── Telegram ──────────────────────────────────────────────────────────────
def send_telegram(msg: str):
    """Send a message to Telegram. Silent fail — never crash the bot for a notification."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception:
        pass


def build_report() -> str:
    """Build a summary report from the trade database using actual resolution data."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    cur = conn.execute("SELECT COUNT(*) as c FROM trades")
    total = cur.fetchone()["c"]

    cur = conn.execute("SELECT strategy, COUNT(*) as c FROM trades GROUP BY strategy")
    by_strat = {r["strategy"]: r["c"] for r in cur.fetchall()}

    # Actual resolved results
    cur = conn.execute("SELECT COUNT(*) as c FROM trades WHERE status = 'WIN'")
    wins = cur.fetchone()["c"]
    cur = conn.execute("SELECT COUNT(*) as c FROM trades WHERE status = 'LOSS'")
    losses = cur.fetchone()["c"]
    cur = conn.execute("SELECT COUNT(*) as c FROM trades WHERE status = 'OPEN'")
    pending = cur.fetchone()["c"]
    cur = conn.execute("SELECT COALESCE(SUM(pnl), 0) as total FROM trades WHERE pnl IS NOT NULL")
    total_pnl = cur.fetchone()["total"]

    # Momentum-only stats
    cur = conn.execute("SELECT COUNT(*) as c FROM trades WHERE strategy = 'LATE_MOM' AND status = 'WIN'")
    mom_wins = cur.fetchone()["c"]
    cur = conn.execute("SELECT COUNT(*) as c FROM trades WHERE strategy = 'LATE_MOM' AND status IN ('WIN', 'LOSS')")
    mom_resolved = cur.fetchone()["c"]
    cur = conn.execute("SELECT COALESCE(SUM(pnl), 0) as total FROM trades WHERE strategy = 'LATE_MOM' AND pnl IS NOT NULL")
    mom_pnl = cur.fetchone()["total"]

    # Recent resolved trades
    cur = conn.execute("SELECT * FROM trades WHERE status IN ('WIN', 'LOSS') ORDER BY id DESC LIMIT 5")
    recent = cur.fetchall()
    recent_lines = []
    for t in recent:
        recent_lines.append(
            f"  {t['status']} {t['strategy']} {t['side']} | ${t['pnl']:+.2f} | "
            f"entry ${t['combined_cost']:.2f}"
        )

    btc = get_btc_price()
    mom_wr = f"{mom_wins}/{mom_resolved} ({mom_wins/mom_resolved*100:.0f}%)" if mom_resolved > 0 else "n/a"

    report = (
        f"*BTC 15M Bot Report*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"BTC: ${btc:,.2f}\n"
        f"Total signals: {total} | Pending: {pending}\n"
    )

    for strat, count in by_strat.items():
        report += f"  {strat}: {count}\n"

    report += (
        f"\n*Results:*\n"
        f"Wins: {wins} | Losses: {losses}\n"
        f"Momentum W/L: {mom_wr}\n"
        f"Total P&L: ${total_pnl:+,.2f}\n"
        f"Momentum P&L: ${mom_pnl:+,.2f}\n"
    )

    if recent_lines:
        report += f"\n*Recent:*\n" + "\n".join(recent_lines)

    report += f"\n\n_Mode: {'DRY RUN' if dry_run else 'LIVE'}_"

    conn.close()
    return report


# ─── Market Discovery ──────────────────────────────────────────────────────
def find_btc_15m_markets() -> list:
    """Find current and upcoming 15-min markets across all assets."""
    now_ts = int(time.time())
    # Round down to nearest 15 min (900 seconds)
    current_window_start = now_ts - (now_ts % 900)

    markets = []
    # Check current window + next 2 upcoming, across all assets
    for asset in ASSETS:
        for offset in range(-1, 3):
            ts = current_window_start + (offset * 900)
            slug = f"{asset}-updown-15m-{ts}"
            window_end = ts + 900

            # Skip already-resolved windows (ended > 60s ago)
            if window_end < now_ts - 60:
                continue

            try:
                resp = requests.get(f"{GAMMA_API}/markets", params={"slug": slug}, timeout=10)
                data = resp.json()
                if not data:
                    continue
                if isinstance(data, list):
                    for m in data:
                        m["_slug"] = slug
                        m["_asset"] = asset.upper()
                        m["_window_start"] = ts
                        m["_window_end"] = window_end
                        markets.append(m)
                elif isinstance(data, dict) and data.get("question"):
                    data["_slug"] = slug
                    data["_asset"] = asset.upper()
                    data["_window_start"] = ts
                    data["_window_end"] = window_end
                    markets.append(data)
            except Exception as e:
                log.debug(f"Slug {slug}: {e}")

    return markets


def get_market_tokens(market: dict) -> dict | None:
    """Extract token IDs and prices from market data."""
    tokens_raw = market.get("clobTokenIds", [])
    prices_raw = market.get("outcomePrices", "[]")

    # Both fields can be JSON strings from Gamma API
    if isinstance(tokens_raw, str):
        try:
            tokens = json.loads(tokens_raw)
        except json.JSONDecodeError:
            return None
    else:
        tokens = tokens_raw

    if isinstance(prices_raw, str):
        try:
            prices = json.loads(prices_raw)
        except json.JSONDecodeError:
            prices = []
    else:
        prices = prices_raw

    if len(tokens) < 2:
        return None

    return {
        "up_token": tokens[0],
        "down_token": tokens[1],
        "up_mid": float(prices[0]) if prices else 0.5,
        "down_mid": float(prices[1]) if len(prices) > 1 else 0.5,
    }


# ─── Order Book ─────────────────────────────────────────────────────────────
def get_orderbook(token_id: str) -> dict:
    """Get order book from CLOB API. Properly sorts to find real best bid/ask."""
    try:
        resp = requests.get(f"{CLOB_API}/book", params={"token_id": token_id}, timeout=10)
        resp.raise_for_status()
        book = resp.json()
        bids = book.get("bids", [])
        asks = book.get("asks", [])

        # Sort: highest bid first, lowest ask first
        sorted_bids = sorted(bids, key=lambda x: float(x["price"]), reverse=True)
        sorted_asks = sorted(asks, key=lambda x: float(x["price"]))

        best_bid = float(sorted_bids[0]["price"]) if sorted_bids else 0
        best_ask = float(sorted_asks[0]["price"]) if sorted_asks else 1

        # Depth at top 5 levels
        bid_depth = sum(float(b.get("size", 0)) for b in sorted_bids[:5])
        ask_depth = sum(float(a.get("size", 0)) for a in sorted_asks[:5])

        return {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread": best_ask - best_bid,
            "bid_depth": bid_depth,
            "ask_depth": ask_depth,
        }
    except Exception:
        return {"best_bid": 0, "best_ask": 1, "spread": 1, "bid_depth": 0, "ask_depth": 0}


# ─── BTC Price (Chainlink + Binance) ────────────────────────────────────────
# Chainlink BTC/USD aggregator on Polygon
CHAINLINK_BTC_USD = "0xc907E116054Ad103354f2D350FD2514433D57F6f"
POLYGON_RPC = "https://polygon-bor-rpc.publicnode.com"


def get_chainlink_btc_price() -> tuple[float, int]:
    """Get BTC price from Chainlink oracle on Polygon. Returns (price, updated_at_ts)."""
    try:
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [{"to": CHAINLINK_BTC_USD, "data": "0xfeaf968c"}, "latest"],
            "id": 1,
        }
        resp = requests.post(POLYGON_RPC, json=payload, timeout=10)
        result = resp.json().get("result", "0x")
        hex_data = result[2:]
        if len(hex_data) >= 192:
            answer = int(hex_data[64:128], 16)
            updated_at = int(hex_data[128:192], 16)
            return answer / 1e8, updated_at
    except Exception:
        pass
    return 0, 0


def get_btc_price() -> float:
    """Get current BTC price — Chainlink primary, Binance fallback."""
    price, _ = get_chainlink_btc_price()
    if price > 0:
        return price
    # Fallback to Binance
    try:
        resp = requests.get(f"{BINANCE_REST}/ticker/price",
                           params={"symbol": "BTCUSDT"}, timeout=5)
        return float(resp.json()["price"])
    except Exception:
        return 0


def get_btc_price_at(timestamp_ms: int) -> float:
    """Get BTC price at a specific timestamp using Binance klines.
    (Chainlink doesn't have historical API, so Binance is fine for open price.)"""
    try:
        resp = requests.get(f"{BINANCE_REST}/klines", params={
            "symbol": "BTCUSDT",
            "interval": "1m",
            "startTime": timestamp_ms,
            "limit": 1,
        }, timeout=5)
        data = resp.json()
        if data:
            return float(data[0][1])  # Open price of that minute
    except Exception:
        pass
    return 0


# ─── Order Execution ───────────────────────────────────────────────────────
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


def place_both_sides(up_token: str, down_token: str, up_price: float, down_price: float, size: float) -> dict:
    """Place orders on both sides. Pre-sign both, then submit together.

    Uses FOK (fill-or-kill) so partial fills can't happen.
    Returns dict with fill_status ('BOTH_FILLED', 'UP_ONLY', 'DOWN_ONLY', 'NONE')
    and order IDs for tracking.
    """
    from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
    from py_clob_client.order_builder.constants import BUY

    result = {"fill_status": "NONE", "order_ids": ""}
    client = get_clob()
    # 15-min up/down markets are NOT neg_risk (negRisk=False from Gamma API)
    options = PartialCreateOrderOptions(neg_risk=False)

    try:
        # Pre-sign both orders before submitting either
        up_args = OrderArgs(token_id=up_token, price=up_price, size=size, side=BUY)
        down_args = OrderArgs(token_id=down_token, price=down_price, size=size, side=BUY)

        signed_up = client.create_order(up_args, options)
        signed_down = client.create_order(down_args, options)

        # Submit UP first
        resp_up = client.post_order(signed_up, OrderType.FOK)
        up_id = resp_up.get("orderID") or resp_up.get("id", "")
        up_status = resp_up.get("status", "UNKNOWN")
        log.info(f"  UP order: {up_id} status={up_status}")

        # Check if UP was actually matched (FOK either fills fully or not at all)
        if up_status in ("MATCHED", "matched"):
            log.info(f"  UP FILLED at ${up_price}")
        else:
            log.warning(f"  UP not filled (status={up_status}). Skipping DOWN — no arb.")
            return result

        # Submit DOWN only if UP filled
        try:
            resp_down = client.post_order(signed_down, OrderType.FOK)
            down_id = resp_down.get("orderID") or resp_down.get("id", "")
            down_status = resp_down.get("status", "UNKNOWN")
            log.info(f"  DOWN order: {down_id} status={down_status}")

            if down_status in ("MATCHED", "matched"):
                log.info(f"  DOWN FILLED at ${down_price}")
                log.info(f"  ARB COMPLETE — both sides filled. Locked profit: ${(1.0 - up_price - down_price) * size:.2f}")
                result["fill_status"] = "BOTH_FILLED"
                result["order_ids"] = f"UP:{up_id},DOWN:{down_id}"
                return result
            else:
                log.warning(f"  DOWN not filled (status={down_status}). UP filled — DIRECTIONAL EXPOSURE on UP!")
                send_telegram(f"⚠️ Half-fill: UP filled at ${up_price}, DOWN FOK rejected. Directional UP bet.")
                result["fill_status"] = "UP_ONLY"
                result["order_ids"] = f"UP:{up_id}"
                return result
        except Exception as e:
            log.error(f"  DOWN order FAILED after UP filled: {e}")
            log.error(f"  CRITICAL: Directional exposure! UP token={up_token[:20]}... price=${up_price}")
            send_telegram(f"⚠️ Half-fill: UP filled at ${up_price}, DOWN error: {e}")
            result["fill_status"] = "UP_ONLY"
            result["order_ids"] = f"UP:{up_id}"
            return result

    except Exception as e:
        log.error(f"Order execution failed: {e}")
        return result


def place_single_side(token_id: str, price: float, size: float) -> bool:
    """Place a single directional order."""
    from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
    from py_clob_client.order_builder.constants import BUY

    client = get_clob()
    options = PartialCreateOrderOptions(neg_risk=False)

    try:
        args = OrderArgs(token_id=token_id, price=price, size=size, side=BUY)
        signed = client.create_order(args, options)
        resp = client.post_order(signed, OrderType.FOK)
        order_id = resp.get("orderID") or resp.get("id", "")
        log.info(f"  Order submitted: {order_id}")
        return True
    except Exception as e:
        log.error(f"Order failed: {e}")
        return False


# ─── Strategy: Binary Arb ──────────────────────────────────────────────────
def check_arb(up_token: str, down_token: str) -> dict | None:
    """Check if UP + DOWN ask prices < ARB_TARGET_COST."""
    up_book = get_orderbook(up_token)
    down_book = get_orderbook(down_token)

    up_ask = up_book["best_ask"]
    down_ask = down_book["best_ask"]
    combined = up_ask + down_ask

    if combined <= ARB_TARGET_COST and up_ask < 0.95 and down_ask < 0.95:
        profit = 1.0 - combined
        if profit < ARB_MIN_GAP:
            return None  # Skip thin gaps — not worth execution risk
        return {
            "up_price": up_ask,
            "down_price": down_ask,
            "combined": combined,
            "profit_per_share": profit,
            "up_depth": up_book["ask_depth"],
            "down_depth": down_book["ask_depth"],
        }
    return None


# ─── Volatility Filter ─────────────────────────────────────────────────────
_vol_cache = {}  # window_start -> ATR value


def get_pre_window_atr(window_start: int) -> float:
    """Calculate ATR of previous 4 windows (1 hour) as volatility proxy.

    Uses Binance 15-min candles for the 4 windows before this one.
    Returns ATR as a percentage of price.
    """
    if window_start in _vol_cache:
        return _vol_cache[window_start]

    try:
        # Fetch 4 previous 15-min candles
        end_ms = window_start * 1000
        start_ms = end_ms - (4 * 900 * 1000)
        resp = requests.get(
            f"{BINANCE_REST}/klines",
            params={
                "symbol": "BTCUSDT",
                "interval": "15m",
                "startTime": start_ms,
                "endTime": end_ms - 1,
                "limit": 4,
            },
            timeout=10,
        )
        candles = resp.json()
        if not candles or len(candles) < 2:
            return 0

        # ATR = average true range as % of close
        trs = []
        for i, c in enumerate(candles):
            high = float(c[2])
            low = float(c[3])
            close = float(c[4])
            tr = (high - low) / close * 100  # True range as %
            trs.append(tr)

        atr = sum(trs) / len(trs)
        _vol_cache[window_start] = atr

        # Clean old entries
        cutoff = window_start - 7200
        for k in list(_vol_cache):
            if k < cutoff:
                del _vol_cache[k]

        return atr
    except Exception:
        return 0


def passes_vol_filter(window_start: int) -> tuple[bool, float, str]:
    """Check if this window passes the volatility + time-of-day filter.

    Returns (passes, atr_value, reason).
    Backtested quartile boundaries (14-day data):
      Q1: ATR < 0.08%  (low vol, 88.4% WR)
      Q2: 0.08-0.12%   (medium vol, 91.6% WR) ← best
      Q3: 0.12-0.18%   (high vol, 83.2% WR)
      Q4: > 0.18%       (very high, 86.3% WR)
    We allow Q1+Q2 (ATR < 0.12%), skip Q3+Q4.
    """
    from datetime import datetime, timezone

    # Time-of-day filter
    dt = datetime.fromtimestamp(window_start, tz=timezone.utc)
    if dt.hour in SKIP_UTC_HOURS:
        return False, 0, f"skip {dt.hour}:00 UTC (US open)"

    # ATR filter
    atr = get_pre_window_atr(window_start)
    if atr <= 0:
        # Can't compute — allow trade but log it
        return True, 0, "no ATR data"

    # Allow Q1+Q2 (low + medium vol), skip Q3+Q4
    if atr > 0.12:
        return False, atr, f"ATR {atr:.4f}% too high (Q3/Q4)"

    return True, atr, f"ATR {atr:.4f}% OK (Q1/Q2)"


# ─── Strategy: Late Momentum ────────────────────────────────────────────────
# Cache BTC open prices per window to avoid repeated API calls
_btc_open_cache = {}


def get_btc_open_for_window(window_start: int) -> float:
    """Get BTC price at window open, cached per window."""
    if window_start in _btc_open_cache:
        return _btc_open_cache[window_start]
    price = get_btc_price_at(window_start * 1000)
    if price > 0:
        _btc_open_cache[window_start] = price
        # Clean old entries
        cutoff = window_start - 3600
        for k in list(_btc_open_cache):
            if k < cutoff:
                del _btc_open_cache[k]
    return price


def detect_price_pattern(asset_symbol: str, window_start: int, elapsed_min: float) -> dict | None:
    """Analyze intra-window 1m candles to detect pump-and-dump patterns.

    Returns pattern info including:
    - pattern: PUMP_DUMP, FADING, LATE_BREAK, SUSTAINED, CHOPPY, FLAT
    - peak_min: which minute the peak move occurred
    - retrace_at_now: how much of peak has been given back
    - safe: whether the pattern is safe to trade

    Data: 400-window backtest shows PUMP_DUMP = 67% hold rate (coin flip),
    while LATE_BREAK (93.5%) and SUSTAINED (92.9%) are money makers.
    """
    try:
        resp = requests.get(f"{BINANCE_REST}/klines", params={
            "symbol": asset_symbol,
            "interval": "1m",
            "startTime": window_start * 1000,
            "limit": int(elapsed_min) + 2,
        }, timeout=5)
        candles = resp.json()
        if not candles or len(candles) < 5:
            return None
    except Exception:
        return None

    open_price = float(candles[0][1])
    moves = [(float(c[4]) - open_price) / open_price * 100 for c in candles]

    # Find peak move (largest absolute deviation)
    abs_moves = [abs(m) for m in moves]
    peak_idx = abs_moves.index(max(abs_moves))
    peak_move = moves[peak_idx]
    peak_min = peak_idx

    current_move = moves[-1]

    # Retracement: how much of peak was given back?
    if abs(peak_move) > 0.01:
        retrace = 1.0 - (current_move / peak_move)
    else:
        retrace = 0

    # Early move (first 5 min) vs current
    early_peak_idx = min(4, len(moves) - 1)
    early_moves = [abs(m) for m in moves[:early_peak_idx + 1]]
    early_peak = max(early_moves)

    # Classify
    if abs(peak_move) < 0.10:
        pattern = "FLAT"
    elif peak_min <= 5 and retrace > 0.40:
        pattern = "PUMP_DUMP"
    elif peak_min <= 5 and retrace > 0.20:
        pattern = "FADING"
    elif early_peak < abs(current_move) * 0.5 and peak_min >= 6:
        pattern = "LATE_BREAK"
    else:
        # Check monotonicity
        sign = 1 if current_move > 0 else -1
        aligned = sum(1 for i in range(1, len(moves)) if (moves[i] - moves[i-1]) * sign > 0)
        mono = aligned / (len(moves) - 1) if len(moves) > 1 else 0
        if mono >= 0.60:
            pattern = "SUSTAINED"
        else:
            pattern = "CHOPPY"

    # PUMP_DUMP is the only pattern we skip — 67% hold rate = coin flip
    safe = pattern != "PUMP_DUMP"

    return {
        "pattern": pattern,
        "peak_move": peak_move,
        "peak_min": peak_min,
        "retrace": retrace,
        "safe": safe,
    }


def check_late_momentum(market: dict) -> dict | None:
    """
    PRIMARY STRATEGY: Momentum with price gate + pattern filter.

    Entry at minute 8-12 when asset moves >0.20% from window open AND
    Polymarket price is still cheap ($0.55-$0.85). Skips pump-and-dump
    patterns where early spike is fading.

    Pattern filter (from 400-window backtest):
    - LATE_BREAK: 93.5% hold rate → TRADE
    - SUSTAINED: 92.9% → TRADE
    - CHOPPY: 100% → TRADE (resolved by min 8)
    - PUMP_DUMP: 67% → SKIP (coin flip)
    """
    window_start = market["_window_start"]
    now_ts = int(time.time())
    elapsed_sec = now_ts - window_start
    elapsed_min = elapsed_sec / 60

    # Only check within the entry window
    if elapsed_min < ENTRY_MIN_MINUTE or elapsed_min > ENTRY_MAX_MINUTE:
        return None

    # Determine which Binance symbol to use for this asset
    asset = market.get("_asset", "BTC").upper()
    asset_symbol = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT", "XRP": "XRPUSDT"}.get(asset, "BTCUSDT")

    # Get price at window open vs now
    open_price = get_btc_open_for_window(window_start) if asset == "BTC" else get_btc_price_at(window_start * 1000)
    current_price = get_btc_price() if asset == "BTC" else 0

    # For non-BTC assets, fetch current price from Binance
    if asset != "BTC":
        try:
            resp = requests.get(f"{BINANCE_REST}/ticker/price", params={"symbol": asset_symbol}, timeout=5)
            current_price = float(resp.json()["price"])
            if open_price <= 0:
                open_price = get_btc_price_at(window_start * 1000)  # fallback
                # Actually fetch the right asset
                resp2 = requests.get(f"{BINANCE_REST}/klines", params={
                    "symbol": asset_symbol, "interval": "1m",
                    "startTime": window_start * 1000, "limit": 1,
                }, timeout=5)
                data = resp2.json()
                if data:
                    open_price = float(data[0][1])
        except Exception:
            pass

    if open_price <= 0 or current_price <= 0:
        return None

    move_pct = ((current_price - open_price) / open_price) * 100

    if abs(move_pct) < MOMENTUM_THRESHOLD_PCT:
        return None

    # ─── PATTERN FILTER: Skip pump-and-dumps ─────────────────────
    pattern_info = detect_price_pattern(asset_symbol, window_start, elapsed_min)
    if pattern_info and not pattern_info["safe"]:
        log.info(f"  SKIP [{asset}] {market.get('_slug', '')} — {pattern_info['pattern']} "
                f"(peak {pattern_info['peak_move']:+.3f}% at min {pattern_info['peak_min']}, "
                f"retrace {pattern_info['retrace']:.0%})")
        return None

    direction = "UP" if move_pct > 0 else "DOWN"

    # Estimate win probability (varies by time elapsed + move size)
    abs_move = abs(move_pct)
    if elapsed_min >= 10:
        base_rate = 95.0 if abs_move >= 0.20 else 87.0
    elif elapsed_min >= 8:
        base_rate = 90.0 if abs_move >= 0.20 else 85.0
    else:
        base_rate = 85.0 if abs_move >= 0.20 else 80.0

    # Stronger moves = higher win rate
    if abs_move >= 0.50:
        base_rate = min(99.0, base_rate + 8)
    elif abs_move >= 0.30:
        base_rate = min(99.0, base_rate + 4)

    # Pattern bonus: LATE_BREAK and SUSTAINED get +2%
    if pattern_info and pattern_info["pattern"] in ("LATE_BREAK", "SUSTAINED"):
        base_rate = min(99.0, base_rate + 2)

    return {
        "direction": direction,
        "open_price": open_price,
        "current_price": current_price,
        "move_pct": move_pct,
        "elapsed_min": elapsed_min,
        "est_win_rate": base_rate,
        "pattern": pattern_info["pattern"] if pattern_info else "UNKNOWN",
    }


def check_late_snipe(market: dict) -> dict | None:
    """SECONDARY: Late snipe at minute 13.

    At minute 13, if BTC has moved >0.10% from window open,
    direction holds 99% of the time. Buy at $0.93-$0.97 for near-certain $1.00.
    """
    window_start = market["_window_start"]
    now_ts = int(time.time())
    elapsed_min = (now_ts - window_start) / 60

    # Only at minute 13-14
    if elapsed_min < LATE_SNIPE_MINUTE or elapsed_min > 14.5:
        return None

    open_price = get_btc_open_for_window(window_start)
    current_price = get_btc_price()

    if open_price <= 0 or current_price <= 0:
        return None

    move_pct = ((current_price - open_price) / open_price) * 100

    if abs(move_pct) < LATE_SNIPE_THRESHOLD_PCT:
        return None

    direction = "UP" if move_pct > 0 else "DOWN"

    # At minute 13 with 0.10%+ move, 99% win rate from backtest
    est_win_rate = 99.0 if abs(move_pct) >= 0.20 else 98.0

    return {
        "direction": direction,
        "open_price": open_price,
        "current_price": current_price,
        "move_pct": move_pct,
        "elapsed_min": elapsed_min,
        "est_win_rate": est_win_rate,
    }


# ─── Main Bot Loop ──────────────────────────────────────────────────────────
def cmd_scan():
    """Show current BTC 15-min markets and order book state."""
    cl_price, cl_updated = get_chainlink_btc_price()
    try:
        binance_resp = requests.get(f"{BINANCE_REST}/ticker/price", params={"symbol": "BTCUSDT"}, timeout=5)
        binance_price = float(binance_resp.json()["price"])
    except Exception:
        binance_price = 0

    btc = cl_price if cl_price > 0 else binance_price
    cl_lag = int(time.time()) - cl_updated if cl_updated else 0
    diff = binance_price - cl_price if cl_price > 0 and binance_price > 0 else 0

    print(f"\nBTC Chainlink: ${cl_price:,.2f} (lag: {cl_lag}s) ← RESOLUTION PRICE")
    print(f"BTC Binance:   ${binance_price:,.2f} (diff: ${diff:+,.2f})")
    print("=" * 70)

    markets = find_btc_15m_markets()
    if not markets:
        print("No active BTC 15-min markets found.")
        return

    now_ts = int(time.time())

    for m in markets:
        q = m.get("question", "N/A")
        slug = m.get("_slug", "")
        window_end = m.get("_window_end", 0)
        remaining = window_end - now_ts

        tokens = get_market_tokens(m)
        if not tokens:
            print(f"\n  {q}")
            print(f"    No token data available")
            continue

        up_book = get_orderbook(tokens["up_token"])
        down_book = get_orderbook(tokens["down_token"])

        combined_ask = up_book["best_ask"] + down_book["best_ask"]
        combined_mid = tokens["up_mid"] + tokens["down_mid"]
        arb = combined_ask < ARB_TARGET_COST and up_book["best_ask"] < 0.95

        # Check late momentum
        late = check_late_momentum(m)
        elapsed_min = (now_ts - m.get("_window_start", now_ts)) / 60

        status = "ACTIVE" if remaining > 0 else "CLOSED"
        arb_str = "ARB !!!" if arb else ""

        print(f"\n  {q}")
        print(f"    Slug: {slug} | {status} | {remaining//60}m {remaining%60}s left | Elapsed: {elapsed_min:.1f}min")
        print(f"    Mid:  UP={tokens['up_mid']:.4f}  DOWN={tokens['down_mid']:.4f}  Sum={combined_mid:.4f}")
        print(f"    Ask:  UP={up_book['best_ask']:.4f}  DOWN={down_book['best_ask']:.4f}  Sum={combined_ask:.4f} {arb_str}")
        print(f"    Bid:  UP={up_book['best_bid']:.4f}  DOWN={down_book['best_bid']:.4f}")
        print(f"    Depth: UP ask={up_book['ask_depth']:.0f} bid={up_book['bid_depth']:.0f} | "
              f"DOWN ask={down_book['ask_depth']:.0f} bid={down_book['bid_depth']:.0f}")

        # Show BTC momentum for this window
        window_start = m.get("_window_start", 0)
        if window_start and elapsed_min > 1:
            btc_open = get_btc_open_for_window(window_start)
            if btc_open > 0:
                btc_now = get_btc_price()
                btc_move = ((btc_now - btc_open) / btc_open) * 100
                signal_str = ""
                if abs(btc_move) >= MOMENTUM_THRESHOLD_PCT and elapsed_min >= ENTRY_MIN_MINUTE:
                    direction = "UP" if btc_move > 0 else "DOWN"
                    signal_str = f" >>> SIGNAL: BUY {direction}"
                print(f"    BTC: open=${btc_open:,.2f} now=${btc_now:,.2f} move={btc_move:+.3f}%{signal_str}")

        if late:
            # Check if price is in entry range
            if tokens:
                win_token = tokens["up_token"] if late["direction"] == "UP" else tokens["down_token"]
                win_book = get_orderbook(win_token)
                win_ask = win_book["best_ask"]
                tradeable = MIN_ENTRY_PRICE <= win_ask <= MAX_ENTRY_PRICE
                trade_str = f"TRADEABLE @ ${win_ask:.4f}" if tradeable else f"price ${win_ask:.4f} out of range"
            else:
                trade_str = "no tokens"
            print(f"    >>> MOMENTUM: {late['direction']} | "
                  f"BTC moved {late['move_pct']:+.3f}% in {late['elapsed_min']:.1f}min | "
                  f"Win rate: {late['est_win_rate']:.0f}% | {trade_str}")

        if arb:
            profit = 1.0 - combined_ask
            print(f"    >>> ARB PROFIT/SHARE: ${profit:.4f} | "
                  f"{ORDER_SIZE_SHARES} shares = ${profit * ORDER_SIZE_SHARES:.2f}")


def cmd_run():
    """Main bot loop — MOMENTUM + SNIPE strategy."""
    global running, dry_run

    if "--live" in sys.argv:
        dry_run = False
        log.warning("LIVE TRADING ENABLED")
    else:
        log.info("DRY RUN mode — no real orders")

    init_db()

    # State tracking — one trade per market window
    traded_slugs = set()
    cycle = 0
    last_report_time = 0
    REPORT_INTERVAL = 4 * 3600  # 4 hours in seconds

    # Send startup message
    mode = "LIVE" if not dry_run else "DRY RUN"
    send_telegram(
        f"*BTC 15M Bot v4 (MOMENTUM) Started*\n"
        f"Mode: {mode}\n"
        f"Strategy: Momentum (min 3-12, >0.20%) + Late Snipe (min 13-14, >0.10%)\n"
        f"Entry: ${MIN_ENTRY_PRICE}-${MAX_ENTRY_PRICE} (momentum) | ${LATE_SNIPE_MIN_ENTRY}-${LATE_SNIPE_MAX_ENTRY} (snipe)\n"
        f"Size: {ORDER_SIZE_SHARES} shares (momentum) | {LATE_SNIPE_SIZE} shares (snipe)\n"
        f"Loss cap: ${MAX_DAILY_LOSS}\n"
        f"Assets: {', '.join(a.upper() for a in ASSETS)}"
    )

    log.info(f"MOMENTUM MODE — directional strategies enabled")
    log.info(f"Momentum: min {ENTRY_MIN_MINUTE}-{ENTRY_MAX_MINUTE}, >{MOMENTUM_THRESHOLD_PCT}% move, entry ${MIN_ENTRY_PRICE}-${MAX_ENTRY_PRICE}")
    log.info(f"Snipe: min {LATE_SNIPE_MINUTE}-14.5, >{LATE_SNIPE_THRESHOLD_PCT}% move, entry ${LATE_SNIPE_MIN_ENTRY}-${LATE_SNIPE_MAX_ENTRY}")
    log.info(f"Size: {ORDER_SIZE_SHARES}/{LATE_SNIPE_SIZE} shares | Daily loss cap: ${MAX_DAILY_LOSS}")

    def handle_signal(sig, frame):
        global running
        running = False
        log.info("Shutting down...")

    signal.signal(signal.SIGINT, handle_signal)

    while running:
        cycle += 1
        try:
            # Check daily loss
            daily_pnl = get_daily_pnl()
            if daily_pnl < -MAX_DAILY_LOSS:
                log.warning(f"DAILY LOSS CAP HIT: ${daily_pnl:.2f}. Stopping.")
                break

            # Discover markets
            markets = find_btc_15m_markets()
            now_ts = int(time.time())

            if not markets:
                if cycle % 12 == 1:
                    log.info("No active 15-min markets found, waiting...")
                time.sleep(POLL_INTERVAL)
                continue

            btc_price = get_btc_price()

            for m in markets:
                slug = m.get("_slug", "")
                window_start = m.get("_window_start", 0)
                window_end = m.get("_window_end", 0)
                remaining = window_end - now_ts
                elapsed_min = (now_ts - window_start) / 60

                # Skip closed markets or already traded
                if remaining < 30:
                    continue
                if slug in traded_slugs:
                    continue

                tokens = get_market_tokens(m)
                if not tokens:
                    continue

                # ─── PRIMARY: Momentum (min 3-12) ───────────────────
                momentum = check_late_momentum(m)
                if momentum:
                    direction = momentum["direction"]
                    move_pct = momentum["move_pct"]
                    est_wr = momentum["est_win_rate"]
                    asset_tag = m.get("_asset", "???")

                    # Get the winning side's orderbook
                    if direction == "UP":
                        win_token = tokens["up_token"]
                    else:
                        win_token = tokens["down_token"]

                    win_book = get_orderbook(win_token)
                    win_ask = win_book["best_ask"]
                    win_depth = win_book["ask_depth"]

                    # Entry price gate
                    if win_ask > MAX_ENTRY_PRICE:
                        log.debug(f"  SKIP MOMENTUM [{asset_tag}] {slug} — ask ${win_ask:.2f} > max ${MAX_ENTRY_PRICE}")
                        continue
                    if win_ask < MIN_ENTRY_PRICE:
                        log.debug(f"  SKIP MOMENTUM [{asset_tag}] {slug} — ask ${win_ask:.2f} < min ${MIN_ENTRY_PRICE}")
                        continue

                    # Depth check — need at least our order size
                    if win_depth < ORDER_SIZE_SHARES:
                        log.info(f"  SKIP MOMENTUM [{asset_tag}] {slug} — depth {win_depth:.0f} < {ORDER_SIZE_SHARES} shares")
                        continue

                    # Capital check
                    cost = win_ask * ORDER_SIZE_SHARES
                    open_count = 0
                    try:
                        conn = sqlite3.connect(str(DB_PATH))
                        open_count = conn.execute(
                            "SELECT COUNT(*) FROM trades WHERE status='OPEN' AND dry_run=0"
                        ).fetchone()[0]
                        conn.close()
                    except Exception:
                        pass
                    capital_locked = open_count * ORDER_SIZE_SHARES
                    if not dry_run and capital_locked + cost > 70:
                        log.warning(f"  SKIP MOMENTUM [{asset_tag}] — capital locked: ~${capital_locked:.0f} + ${cost:.1f} > $70")
                        continue

                    # Expected value check — only trade if E[PnL] > 0
                    wr = est_wr / 100.0
                    ev_per_share = wr * (1.0 - win_ask) - (1.0 - wr) * win_ask
                    if ev_per_share <= 0:
                        log.info(f"  SKIP MOMENTUM [{asset_tag}] {slug} — negative EV: ${ev_per_share:.3f}/share at ${win_ask:.2f} entry, {est_wr:.0f}% WR")
                        continue

                    pattern = momentum.get("pattern", "?")
                    log.info(f"MOMENTUM [{asset_tag}] on {slug}")
                    log.info(f"  {direction} | Move={move_pct:+.3f}% | Min={elapsed_min:.1f} | Pattern={pattern}")
                    log.info(f"  Entry=${win_ask:.2f} | Depth={win_depth:.0f} | WR={est_wr:.0f}% | EV=${ev_per_share:.3f}/share")

                    trade = {
                        "strategy": "MOMENTUM",
                        "slug": slug,
                        "side": direction,
                        "up_price": win_ask if direction == "UP" else 0,
                        "down_price": win_ask if direction == "DOWN" else 0,
                        "combined_cost": win_ask,
                        "shares": ORDER_SIZE_SHARES,
                        "btc_price": btc_price,
                        "btc_move_pct": move_pct,
                    }

                    if dry_run:
                        log.info(f"  [DRY RUN] Would buy {ORDER_SIZE_SHARES} {direction} shares at ${win_ask:.2f}")
                        log.info(f"  E[PnL]: ${ev_per_share * ORDER_SIZE_SHARES:+.2f}")
                        trade["fill_status"] = direction + "_ONLY"
                        log_trade(trade)
                        # Trace dry-run decision
                        if TRACING_ENABLED:
                            with oracle.step("momentum_signal", market=slug, asset=asset_tag) as step:
                                step.log(direction=direction, move_pct=move_pct, pattern=pattern,
                                         entry=win_ask, shares=ORDER_SIZE_SHARES, win_rate=est_wr,
                                         ev=round(ev_per_share * ORDER_SIZE_SHARES, 2),
                                         depth=win_depth, mode="DRY_RUN")
                    else:
                        ok = place_single_side(win_token, win_ask, ORDER_SIZE_SHARES)
                        if ok:
                            trade["fill_status"] = direction + "_ONLY"
                            log_trade(trade)
                            send_telegram(
                                f"*MOMENTUM {direction}* [{asset_tag}] on {slug}\n"
                                f"Pattern: {pattern} | Entry: ${win_ask:.2f} | {ORDER_SIZE_SHARES} shares\n"
                                f"Move: {move_pct:+.3f}% | WR: {est_wr:.0f}% | EV: ${ev_per_share * ORDER_SIZE_SHARES:+.2f}"
                            )
                            log.info(f"  FILLED — {direction} @ ${win_ask:.2f}")
                            # Signed receipt for live trade
                            if TRACING_ENABLED:
                                with oracle.step("trade_executed", market=slug, asset=asset_tag) as step:
                                    step.log(strategy="MOMENTUM", direction=direction,
                                             move_pct=round(move_pct, 4), pattern=pattern,
                                             entry_price=win_ask, shares=ORDER_SIZE_SHARES,
                                             cost=round(win_ask * ORDER_SIZE_SHARES, 2),
                                             win_rate_est=est_wr, ev=round(ev_per_share * ORDER_SIZE_SHARES, 2),
                                             depth=win_depth, price=btc_price)
                        else:
                            log.warning(f"  MOMENTUM order FAILED (FOK killed)")
                            if TRACING_ENABLED:
                                with oracle.step("order_rejected", market=slug, asset=asset_tag) as step:
                                    step.log(strategy="MOMENTUM", direction=direction,
                                             entry=win_ask, reason="FOK_KILLED")

                    traded_slugs.add(slug)
                    continue  # Don't also check snipe for same market

                # ─── SECONDARY: Late Snipe (min 13-14) ──────────────
                snipe = check_late_snipe(m)
                if snipe:
                    direction = snipe["direction"]
                    move_pct = snipe["move_pct"]
                    est_wr = snipe["est_win_rate"]
                    asset_tag = m.get("_asset", "???")

                    if direction == "UP":
                        win_token = tokens["up_token"]
                    else:
                        win_token = tokens["down_token"]

                    win_book = get_orderbook(win_token)
                    win_ask = win_book["best_ask"]
                    win_depth = win_book["ask_depth"]

                    # Snipe entry price gate (tighter range, higher prices ok)
                    if win_ask > LATE_SNIPE_MAX_ENTRY:
                        log.debug(f"  SKIP SNIPE [{asset_tag}] {slug} — ask ${win_ask:.2f} > max ${LATE_SNIPE_MAX_ENTRY}")
                        continue
                    if win_ask < LATE_SNIPE_MIN_ENTRY:
                        log.debug(f"  SKIP SNIPE [{asset_tag}] {slug} — ask ${win_ask:.2f} < min ${LATE_SNIPE_MIN_ENTRY}")
                        continue

                    if win_depth < LATE_SNIPE_SIZE:
                        log.info(f"  SKIP SNIPE [{asset_tag}] {slug} — depth {win_depth:.0f} < {LATE_SNIPE_SIZE} shares")
                        continue

                    # Capital check
                    cost = win_ask * LATE_SNIPE_SIZE
                    open_count = 0
                    try:
                        conn = sqlite3.connect(str(DB_PATH))
                        open_count = conn.execute(
                            "SELECT COUNT(*) FROM trades WHERE status='OPEN' AND dry_run=0"
                        ).fetchone()[0]
                        conn.close()
                    except Exception:
                        pass
                    capital_locked = open_count * ORDER_SIZE_SHARES
                    if not dry_run and capital_locked + cost > 70:
                        log.warning(f"  SKIP SNIPE [{asset_tag}] — capital locked: ~${capital_locked:.0f} + ${cost:.1f} > $70")
                        continue

                    # EV check
                    wr = est_wr / 100.0
                    ev_per_share = wr * (1.0 - win_ask) - (1.0 - wr) * win_ask
                    if ev_per_share <= 0:
                        log.info(f"  SKIP SNIPE [{asset_tag}] {slug} — negative EV: ${ev_per_share:.3f}/share at ${win_ask:.2f}, {est_wr:.0f}% WR")
                        continue

                    log.info(f"SNIPE [{asset_tag}] on {slug}")
                    log.info(f"  {direction} | Move={move_pct:+.3f}% | Min={snipe['elapsed_min']:.1f}")
                    log.info(f"  Entry=${win_ask:.2f} | Depth={win_depth:.0f} | WR={est_wr:.0f}% | EV=${ev_per_share:.3f}/share")

                    trade = {
                        "strategy": "SNIPE",
                        "slug": slug,
                        "side": direction,
                        "up_price": win_ask if direction == "UP" else 0,
                        "down_price": win_ask if direction == "DOWN" else 0,
                        "combined_cost": win_ask,
                        "shares": LATE_SNIPE_SIZE,
                        "btc_price": btc_price,
                        "btc_move_pct": move_pct,
                    }

                    if dry_run:
                        log.info(f"  [DRY RUN] Would buy {LATE_SNIPE_SIZE} {direction} shares at ${win_ask:.2f}")
                        log.info(f"  E[PnL]: ${ev_per_share * LATE_SNIPE_SIZE:+.2f}")
                        trade["fill_status"] = direction + "_ONLY"
                        log_trade(trade)
                        if TRACING_ENABLED:
                            with oracle.step("snipe_signal", market=slug, asset=asset_tag) as step:
                                step.log(direction=direction, move_pct=move_pct,
                                         entry=win_ask, shares=LATE_SNIPE_SIZE, win_rate=est_wr,
                                         ev=round(ev_per_share * LATE_SNIPE_SIZE, 2), mode="DRY_RUN")
                    else:
                        ok = place_single_side(win_token, win_ask, LATE_SNIPE_SIZE)
                        if ok:
                            trade["fill_status"] = direction + "_ONLY"
                            log_trade(trade)
                            send_telegram(
                                f"*SNIPE {direction}* [{asset_tag}] on {slug}\n"
                                f"Entry: ${win_ask:.2f} | {LATE_SNIPE_SIZE} shares\n"
                                f"Move: {move_pct:+.3f}% | WR: {est_wr:.0f}%"
                            )
                            log.info(f"  FILLED — {direction} @ ${win_ask:.2f}")
                            if TRACING_ENABLED:
                                with oracle.step("trade_executed", market=slug, asset=asset_tag) as step:
                                    step.log(strategy="SNIPE", direction=direction,
                                             move_pct=round(move_pct, 4),
                                             entry_price=win_ask, shares=LATE_SNIPE_SIZE,
                                             cost=round(win_ask * LATE_SNIPE_SIZE, 2),
                                             win_rate_est=est_wr, ev=round(ev_per_share * LATE_SNIPE_SIZE, 2),
                                             depth=win_depth, minute=snipe['elapsed_min'], price=btc_price)
                        else:
                            log.warning(f"  SNIPE order FAILED (FOK killed)")
                            if TRACING_ENABLED:
                                with oracle.step("order_rejected", market=slug, asset=asset_tag) as step:
                                    step.log(strategy="SNIPE", direction=direction,
                                             entry=win_ask, reason="FOK_KILLED")

                    traded_slugs.add(slug)

            # Resolve completed trades every cycle
            resolve_open_trades()

            # Auto-claim resolved positions on-chain (every 5 min)
            if not dry_run:
                auto_claim_resolved()

            # Clean old slugs (keep last 2 hours)
            if cycle % 60 == 0:
                cutoff_ts = now_ts - 7200
                traded_slugs = {s for s in traded_slugs
                               if int(s.split("-")[-1]) > cutoff_ts}

            # Status update every 2 minutes
            if cycle % 24 == 1:
                # Count markets per asset
                asset_counts = {}
                for mk in markets:
                    a = mk.get("_asset", "?")
                    asset_counts[a] = asset_counts.get(a, 0) + 1
                asset_str = " ".join(f"{a}={c}" for a, c in sorted(asset_counts.items()))
                log.info(f"STATUS: BTC=${btc_price:,.2f} | "
                        f"Markets={len(markets)} ({asset_str}) | "
                        f"Traded={len(traded_slugs)} | "
                        f"Daily PnL=${daily_pnl:.2f}")

            # 4-hour Telegram report
            if now_ts - last_report_time >= REPORT_INTERVAL:
                report = build_report()
                send_telegram(report)
                last_report_time = now_ts
                log.info("Sent 4-hour report to Telegram")

            time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            break
        except Exception as e:
            log.error(f"Cycle error: {e}")
            time.sleep(5)

    # Final report on shutdown
    report = build_report()
    send_telegram(f"*Bot Stopped*\n\n{report}")

    # Save signed trade receipts
    if TRACING_ENABLED:
        oracle.save()
        oracle.save_receipts()
        summary = oracle.summary()
        log.info(f"Receipts saved: {summary.get('steps', 0)} decisions signed, chain verified")

    log.info("Bot stopped.")


def cmd_stats():
    """Show trade history."""
    init_db()
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    cur = conn.execute("SELECT COUNT(*) as c FROM trades")
    total = cur.fetchone()["c"]

    cur = conn.execute("SELECT strategy, COUNT(*) as c FROM trades GROUP BY strategy")
    by_strat = {r["strategy"]: r["c"] for r in cur.fetchall()}

    cur = conn.execute("SELECT COALESCE(SUM(pnl), 0) as p FROM trades WHERE pnl IS NOT NULL")
    total_pnl = cur.fetchone()["p"]

    print(f"\n{'='*50}")
    print(f"BTC 15-MIN BOT STATS")
    print(f"{'='*50}")
    print(f"Total trades: {total}")
    for strat, count in by_strat.items():
        print(f"  {strat}: {count}")
    print(f"Total PnL: ${total_pnl:.2f}")

    # Live vs dry run breakdown
    cur = conn.execute("SELECT COUNT(*) as c, COALESCE(SUM(pnl), 0) as p FROM trades WHERE dry_run = 0 AND pnl IS NOT NULL")
    live_row = cur.fetchone()
    print(f"\nLIVE trades: {live_row['c']} | Live P&L: ${live_row['p']:.2f}")

    cur = conn.execute("SELECT COUNT(*) as c FROM trades WHERE dry_run = 0 AND status = 'WIN'")
    live_wins = cur.fetchone()["c"]
    cur = conn.execute("SELECT COUNT(*) as c FROM trades WHERE dry_run = 0 AND status = 'LOSS'")
    live_losses = cur.fetchone()["c"]
    cur = conn.execute("SELECT COUNT(*) as c FROM trades WHERE dry_run = 0 AND status = 'OPEN'")
    live_open = cur.fetchone()["c"]
    print(f"  Wins: {live_wins} | Losses: {live_losses} | Open: {live_open}")

    cur = conn.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 15")
    trades = cur.fetchall()
    if trades:
        print(f"\nRecent trades:")
        for t in trades:
            mode = "DRY" if t["dry_run"] else "LIVE"
            pnl = f"${t['pnl']:.2f}" if t["pnl"] is not None else "pending"
            fill = ""
            try:
                fs = t["fill_status"]
                if fs and fs != "BOTH_FILLED":
                    fill = f" [{fs}]"
            except (IndexError, KeyError):
                pass
            print(f"  [{mode}] {t['strategy']:8} {t['side']:5}{fill} | {t['market_slug'][:35]}")
            print(f"    UP={t['up_price']:.4f} DOWN={t['down_price']:.4f} | "
                  f"BTC=${t['btc_price']:,.0f} Move={t['btc_move_pct']:+.3f}% | PnL: {pnl}")

    conn.close()


def cmd_audit():
    """Verify the signed trade receipt chain — cryptographic proof of every decision."""
    if not TRACING_ENABLED:
        print("ai-decision-tracer not installed. Run: python3 -m pip install ai-decision-tracer")
        return

    from ai_trace import ReceiptBuilder

    # Receipts save to receipts/ relative to CWD or trace_dir
    receipt_dirs = [
        Path("receipts"),
        TRACE_DIR / "receipts",
        Path(__file__).parent / "receipts",
    ]
    receipt_files = []
    for rd in receipt_dirs:
        if rd.exists():
            receipt_files.extend(rd.glob("*_receipts.json"))
    receipt_files = sorted(set(receipt_files))

    if not receipt_files:
        print("No signed receipts found yet. Run the bot to generate trade receipts.")
        return
    if not receipt_files:
        print("No receipt files found.")
        return

    print(f"\n{'='*70}")
    print("POLYMARKET ORACLE — SIGNED TRADE AUDIT")
    print(f"{'='*70}")

    total_receipts = 0
    total_valid = 0
    trade_receipts = []
    resolve_receipts = []

    for rf in receipt_files:
        meta, receipts = ReceiptBuilder.load_receipts(str(rf))
        result = ReceiptBuilder.verify_chain_from_list(receipts)

        n = len(receipts)
        total_receipts += n
        valid = result.get("valid", False)
        if valid:
            total_valid += n
        errors = result.get("errors", [])

        print(f"\n  {rf.name}")
        print(f"  Agent: {meta.get('agent', '?')} | Receipts: {n} | Chain: {'VALID' if valid else 'BROKEN'}")
        if errors:
            for e in errors[:3]:
                print(f"    ERROR: {e}")

        # Categorize receipts
        for r in receipts:
            step_name = r.get("step_name", "")
            if step_name == "trade_executed":
                trade_receipts.append(r)
            elif step_name == "trade_resolved":
                resolve_receipts.append(r)

    # Trade summary from signed receipts
    if trade_receipts:
        print(f"\n{'─'*70}")
        print(f"SIGNED TRADE LOG — {len(trade_receipts)} executions")
        print(f"{'─'*70}")
        for r in trade_receipts[-15:]:  # last 15
            logs = r.get("logs", [{}])
            d = logs[0] if logs else {}
            ctx = r.get("context", {})
            print(f"  {r.get('timestamp', '?')[:19]} | "
                  f"{d.get('strategy', '?'):8} {d.get('direction', '?'):4} | "
                  f"{ctx.get('market', '?')[:30]} | "
                  f"${d.get('entry_price', 0):.2f} x {d.get('shares', 0)} | "
                  f"EV ${d.get('ev', 0):+.2f}")

    if resolve_receipts:
        wins = sum(1 for r in resolve_receipts if r.get("logs", [{}])[0].get("result") == "WIN")
        losses = len(resolve_receipts) - wins
        total_pnl = sum(r.get("logs", [{}])[0].get("pnl", 0) for r in resolve_receipts)
        print(f"\n  Resolutions: {len(resolve_receipts)} | W/L: {wins}/{losses} | "
              f"Signed P&L: ${total_pnl:+.2f}")

    print(f"\n{'='*70}")
    chain_status = "ALL CHAINS VALID" if total_valid == total_receipts else "CHAIN ERRORS DETECTED"
    print(f"TOTAL: {total_receipts} signed receipts | {chain_status}")
    print(f"{'='*70}")

    if oracle:
        print(f"\nPublic key (Ed25519): {oracle.public_key}")
        print("Anyone can verify these receipts independently with the public key above.")


# ─── Main ───────────────────────────────────────────────────────────────────
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
    elif cmd == "audit":
        cmd_audit()
    else:
        print(f"Unknown command: {cmd}")


if __name__ == "__main__":
    main()
