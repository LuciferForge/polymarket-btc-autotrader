"""
portfolio.py — Position Tracking & PnL Accounting

Tracks all open positions, fills, and historical PnL.
In dry-run mode, simulates fills at the posted price.
In live mode, polls CLOB for actual fill status.

This is the source of truth for what we own and what we made.
"""

from __future__ import annotations

import time
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import config
from utils.logger import get_logger, log_trade, log_event

if TYPE_CHECKING:
    from utils.polymarket_client import ClobClient

log = get_logger("portfolio")


@dataclass
class Position:
    """A single open position in a prediction market."""
    position_id: str                # UUID for this position
    market_id: str
    question: str
    token_id: str
    side: str                       # "YES" or "NO"
    entry_price: float
    size_usd: float
    shares: float                   # size_usd / entry_price
    opened_at: float = field(default_factory=time.time)
    order_id: str | None = None
    dry_run: bool = True

    # Fill tracking
    is_filled: bool = False
    fill_price: float | None = None
    filled_at: float | None = None

    # Resolution
    is_resolved: bool = False
    resolved_at: float | None = None
    pnl_usd: float | None = None
    resolution_side: str | None = None  # "YES" or "NO"

    @property
    def current_value_usd(self) -> float:
        """Estimated current value (entry price * shares for unresolved)."""
        if self.pnl_usd is not None:
            return self.size_usd + self.pnl_usd
        return self.size_usd  # Approximate until we get live price

    @property
    def age_hours(self) -> float:
        return (time.time() - self.opened_at) / 3600.0

    def as_dict(self) -> dict:
        d = asdict(self)
        d["age_hours"] = round(self.age_hours, 2)
        return d


class Portfolio:
    """
    Tracks positions and PnL across sessions.
    Persists state to a JSON file for crash recovery.
    """

    PORTFOLIO_FILE = config.LOG_DIR / "portfolio.json"

    def __init__(self) -> None:
        self._positions: dict[str, Position] = {}  # position_id → Position
        self._history: list[dict] = []
        self._next_id: int = 1
        self._load()

    def _load(self) -> None:
        """Load portfolio state from disk."""
        if self.PORTFOLIO_FILE.exists():
            try:
                with open(self.PORTFOLIO_FILE) as f:
                    data = json.load(f)
                self._next_id = data.get("next_id", 1)
                # Reload open positions only (closed ones go to history)
                for p_dict in data.get("open_positions", []):
                    pos = Position(**p_dict)
                    self._positions[pos.position_id] = pos
                log.info(f"Loaded {len(self._positions)} open positions from disk")
            except Exception as e:
                log.warning(f"Could not load portfolio state: {e} — starting fresh")

    def _save(self) -> None:
        """Persist portfolio state to disk."""
        try:
            data = {
                "next_id": self._next_id,
                "saved_at": datetime.now(timezone.utc).isoformat(),
                "open_positions": [p.as_dict() for p in self._positions.values()],
            }
            with open(self.PORTFOLIO_FILE, "w") as f:
                json.dump(data, f, indent=2, default=str)
        except Exception as e:
            log.error(f"Failed to save portfolio: {e}")

    def _new_id(self) -> str:
        pid = f"pos_{self._next_id:06d}"
        self._next_id += 1
        return pid

    # ─── Position Lifecycle ───────────────────────────────────────────────────

    def open_position(
        self,
        market_id: str,
        question: str,
        token_id: str,
        side: str,
        price: float,
        size_usd: float,
        order_id: str | None = None,
        dry_run: bool = config.DRY_RUN,
        reasoning: str = "",
    ) -> Position:
        """Record a new position opening."""
        pos = Position(
            position_id=self._new_id(),
            market_id=market_id,
            question=question,
            token_id=token_id,
            side=side,
            entry_price=price,
            size_usd=size_usd,
            shares=round(size_usd / price, 4) if price > 0 else 0,
            order_id=order_id,
            dry_run=dry_run,
        )

        # In dry run, mark as immediately "filled" at entry price
        if dry_run:
            pos.is_filled = True
            pos.fill_price = price
            pos.filled_at = time.time()

        self._positions[pos.position_id] = pos
        self._save()

        action = "DRY_RUN_ORDER" if dry_run else "ORDER_PLACED"
        log_trade(
            action=action,
            market_id=market_id,
            token_id=token_id,
            side=side,
            price=price,
            size_usd=size_usd,
            dry_run=dry_run,
            reasoning=reasoning,
            order_id=order_id,
        )

        log.info(
            f"Position opened {'[DRY RUN] ' if dry_run else ''}| "
            f"{side} ${size_usd:.2f} @ {price:.3f} | "
            f"{question[:60]}..."
        )

        return pos

    def mark_filled(
        self,
        position_id: str,
        fill_price: float,
    ) -> Position | None:
        """Update a position with actual fill price."""
        pos = self._positions.get(position_id)
        if not pos:
            log.warning(f"Position not found for fill: {position_id}")
            return None

        pos.is_filled = True
        pos.fill_price = fill_price
        pos.filled_at = time.time()
        self._save()

        log_trade(
            action="FILL",
            market_id=pos.market_id,
            token_id=pos.token_id,
            side=pos.side,
            price=fill_price,
            size_usd=pos.size_usd,
            dry_run=pos.dry_run,
            order_id=pos.order_id,
            fill_price=fill_price,
        )

        return pos

    def close_position(
        self,
        position_id: str,
        resolution_side: str,  # Which side won: "YES" or "NO"
        payout_per_share: float = 1.0,  # Normally $1 at resolution
    ) -> float | None:
        """
        Close a position at resolution. Calculate and record PnL.
        Returns PnL in USD, or None if position not found.
        """
        pos = self._positions.pop(position_id, None)
        if not pos:
            log.warning(f"Cannot close unknown position: {position_id}")
            return None

        # PnL = payout - cost
        won = pos.side == resolution_side
        payout = pos.shares * payout_per_share if won else 0.0
        pnl = payout - pos.size_usd

        pos.is_resolved = True
        pos.resolved_at = time.time()
        pos.pnl_usd = pnl
        pos.resolution_side = resolution_side

        self._history.append(pos.as_dict())
        self._save()

        log_trade(
            action="FILL",
            market_id=pos.market_id,
            token_id=pos.token_id,
            side=pos.side,
            price=pos.entry_price,
            size_usd=pos.size_usd,
            dry_run=pos.dry_run,
            order_id=pos.order_id,
            fill_price=payout_per_share if won else 0.0,
            pnl_usd=pnl,
        )

        log.info(
            f"Position closed: {pos.question[:60]}... | "
            f"{'WIN' if won else 'LOSS'} ${pnl:.2f} | "
            f"resolution={resolution_side}"
        )

        return pnl

    def cancel_position(self, position_id: str) -> bool:
        """Remove a position that was cancelled before fill."""
        pos = self._positions.pop(position_id, None)
        if not pos:
            return False
        log_trade(
            action="ORDER_CANCELLED",
            market_id=pos.market_id,
            token_id=pos.token_id,
            side=pos.side,
            price=pos.entry_price,
            size_usd=pos.size_usd,
            dry_run=pos.dry_run,
        )
        self._save()
        return True

    # ─── Queries ─────────────────────────────────────────────────────────────

    def get_open_positions(self) -> list[Position]:
        return list(self._positions.values())

    def get_positions_by_market(self, market_id: str) -> list[Position]:
        return [p for p in self._positions.values() if p.market_id == market_id]

    def total_deployed(self) -> float:
        return sum(p.size_usd for p in self._positions.values())

    def session_pnl(self) -> float:
        """PnL from resolved positions this session."""
        return sum(
            p.get("pnl_usd", 0.0)
            for p in self._history
            if p.get("pnl_usd") is not None
        )

    def get_summary(self) -> dict:
        open_pos = self.get_open_positions()
        return {
            "open_count": len(open_pos),
            "deployed_usd": round(self.total_deployed(), 4),
            "session_resolved": len(self._history),
            "session_pnl": round(self.session_pnl(), 4),
            "positions": [
                {
                    "id": p.position_id,
                    "question": p.question[:60],
                    "side": p.side,
                    "price": p.entry_price,
                    "size": p.size_usd,
                    "filled": p.is_filled,
                    "dry_run": p.dry_run,
                    "age_h": round(p.age_hours, 1),
                }
                for p in open_pos
            ],
        }
