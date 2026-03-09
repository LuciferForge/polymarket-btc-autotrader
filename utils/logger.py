"""
utils/logger.py — Structured JSONL logging for every trade and event.

Every decision is logged with full context: timestamp, reasoning, outcome.
This is your audit trail, your debugging tool, and your ML training set.
"""

import json
import sys
import time
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config

# ─── Console logger ──────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)

def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


# ─── JSONL writers ───────────────────────────────────────────────────────────
def _write_jsonl(path: Path, record: dict) -> None:
    """Append a single JSON record to a JSONL file."""
    record["_ts"] = datetime.now(timezone.utc).isoformat()
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str) + "\n")


def log_trade(
    action: str,
    market_id: str,
    token_id: str,
    side: str,
    price: float,
    size_usd: float,
    dry_run: bool,
    reasoning: str = "",
    order_id: str | None = None,
    fill_price: float | None = None,
    pnl_usd: float | None = None,
    extra: dict | None = None,
) -> None:
    """
    Log a trade decision or fill event.
    action: 'ORDER_PLACED' | 'ORDER_CANCELLED' | 'FILL' | 'DRY_RUN_ORDER'
    """
    record = {
        "type": "TRADE",
        "action": action,
        "market_id": market_id,
        "token_id": token_id,
        "side": side,
        "price": price,
        "size_usd": size_usd,
        "dry_run": dry_run,
        "reasoning": reasoning,
        "order_id": order_id,
        "fill_price": fill_price,
        "pnl_usd": pnl_usd,
    }
    if extra:
        record.update(extra)
    _write_jsonl(config.TRADE_LOG, record)


def log_event(
    event_type: str,
    data: dict[str, Any],
    level: str = "INFO",
) -> None:
    """
    Log a system event (scan result, AI signal, risk trigger, error).
    event_type: e.g. 'SCAN_COMPLETE', 'AI_SIGNAL', 'KILL_SWITCH', 'ERROR'
    """
    record = {"type": "EVENT", "event_type": event_type, "level": level, **data}
    _write_jsonl(config.EVENT_LOG, record)


def log_opportunity(
    market_id: str,
    question: str,
    edge: float,
    yes_price: float,
    no_price: float,
    ai_confidence: float | None,
    ai_verdict: str | None,
    action_taken: str,
) -> None:
    """Log a detected arbitrage opportunity, whether acted on or not."""
    record = {
        "type": "OPPORTUNITY",
        "market_id": market_id,
        "question": question[:200],
        "edge": round(edge, 4),
        "yes_price": yes_price,
        "no_price": no_price,
        "price_sum": round(yes_price + no_price, 4),
        "ai_confidence": ai_confidence,
        "ai_verdict": ai_verdict,
        "action_taken": action_taken,
    }
    _write_jsonl(config.EVENT_LOG, record)
