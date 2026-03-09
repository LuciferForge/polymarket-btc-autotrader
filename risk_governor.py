"""
risk_governor.py — Risk Governor & Kill Switch

This module has veto power over everything else. If risk limits are breached,
no order goes through, period. Not negotiable. Not configurable at runtime.

The governor is the last line of defense before real money moves.

Responsibilities:
1. Daily loss cap enforcement (stop trading when down $DAILY_LOSS_CAP_USD)
2. Capital exposure limits (max deployed capital)
3. Position count limits (no more than MAX_CONCURRENT_POS)
4. Per-market position size enforcement
5. Kill switch (manual or automatic)
6. Position sizing recommendation (Kelly fraction, capped)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import config
from utils.logger import get_logger, log_event

if TYPE_CHECKING:
    from edge_detector import EdgeAnalysis
    from ai_analyst import AISignal

log = get_logger("risk_governor")


@dataclass
class RiskDecision:
    """Result of risk check for a proposed trade."""
    approved: bool
    reason: str
    recommended_size_usd: float  # 0.0 if rejected
    warnings: list[str] = field(default_factory=list)


class RiskGovernor:
    """
    Stateful risk manager. Tracks daily PnL, open positions, and capital at risk.
    Thread-safe enough for single-threaded bot use (no concurrent order placement).
    """

    def __init__(self) -> None:
        self._killed: bool = False
        self._kill_reason: str = ""

        # Daily tracking (resets at UTC midnight)
        self._day: str = self._today()
        self._daily_pnl: float = 0.0
        self._daily_trades: int = 0

        # Capital tracking
        self._deployed_capital: float = 0.0     # Sum of open position sizes
        self._open_positions: dict[str, float] = {}  # market_id → size_usd

        # Session stats
        self._total_trades: int = 0
        self._session_pnl: float = 0.0

        log.info(
            f"RiskGovernor initialized | DRY_RUN={config.DRY_RUN} | "
            f"Daily loss cap=${config.DAILY_LOSS_CAP_USD} | "
            f"Max capital=${config.MAX_CAPITAL_USD}"
        )

    # ─── Kill Switch ─────────────────────────────────────────────────────────

    def kill(self, reason: str = "manual") -> None:
        """Engage kill switch. No trades will be approved until restart."""
        self._killed = True
        self._kill_reason = reason
        log.critical(f"KILL SWITCH ENGAGED: {reason}")
        log_event("KILL_SWITCH", {"reason": reason}, level="CRITICAL")

    def is_killed(self) -> bool:
        return self._killed

    @property
    def kill_reason(self) -> str:
        return self._kill_reason

    # ─── Daily Reset ─────────────────────────────────────────────────────────

    def _today(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _check_day_rollover(self) -> None:
        today = self._today()
        if today != self._day:
            log.info(f"Day rollover: resetting daily PnL ({self._daily_pnl:.2f}) for {today}")
            self._day = today
            self._daily_pnl = 0.0
            self._daily_trades = 0

    # ─── Core Risk Check ─────────────────────────────────────────────────────

    def check_trade(
        self,
        market_id: str,
        edge: "EdgeAnalysis",
        signal: "AISignal | None" = None,
        requested_size_usd: float = config.ORDER_SIZE_USD,
        end_date: str = "",
    ) -> RiskDecision:
        """
        Full risk check for a proposed trade.
        Returns RiskDecision with approved=True/False and recommended size.
        """
        self._check_day_rollover()
        warnings: list[str] = []

        # 1. Kill switch
        if self._killed:
            return RiskDecision(
                approved=False,
                reason=f"Kill switch active: {self._kill_reason}",
                recommended_size_usd=0.0,
            )

        # 2. Daily loss cap
        if self._daily_pnl <= -config.DAILY_LOSS_CAP_USD:
            self.kill(f"Daily loss cap hit (${self._daily_pnl:.2f})")
            return RiskDecision(
                approved=False,
                reason=f"Daily loss cap breached: ${self._daily_pnl:.2f}",
                recommended_size_usd=0.0,
            )

        # 3. Capital cap
        if self._deployed_capital >= config.MAX_CAPITAL_USD:
            return RiskDecision(
                approved=False,
                reason=f"Capital cap reached: ${self._deployed_capital:.2f} / ${config.MAX_CAPITAL_USD}",
                recommended_size_usd=0.0,
            )

        # 4. Position count
        if len(self._open_positions) >= config.MAX_CONCURRENT_POS:
            return RiskDecision(
                approved=False,
                reason=f"Max concurrent positions ({config.MAX_CONCURRENT_POS}) reached",
                recommended_size_usd=0.0,
            )

        # 5. Existing position in this market (no doubling down)
        if market_id in self._open_positions:
            return RiskDecision(
                approved=False,
                reason=f"Already have open position in {market_id[:20]}",
                recommended_size_usd=0.0,
            )

        # 6. Horizon check — never lock capital in long-dated markets
        if end_date:
            try:
                from datetime import datetime, timezone
                end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                now = datetime.now(timezone.utc)
                days_to_resolution = (end_dt - now).days
                if days_to_resolution > config.MAX_HORIZON_DAYS:
                    return RiskDecision(
                        approved=False,
                        reason=f"Market resolves in {days_to_resolution}d (max {config.MAX_HORIZON_DAYS}d)",
                        recommended_size_usd=0.0,
                    )
                # Scale position size by horizon
                for max_days, multiplier in sorted(config.HORIZON_SIZE_MULTIPLIER.items()):
                    if days_to_resolution <= max_days:
                        requested_size_usd = requested_size_usd * multiplier
                        if multiplier < 1.0:
                            warnings.append(f"Size reduced to {multiplier:.0%} — resolves in {days_to_resolution}d")
                        break
            except (ValueError, TypeError):
                warnings.append("Could not parse end_date — using default size")

        # 7. Edge minimum
        if edge.edge_magnitude < config.MIN_EDGE_THRESHOLD:
            return RiskDecision(
                approved=False,
                reason=f"Edge {edge.edge_magnitude:.4f} below threshold {config.MIN_EDGE_THRESHOLD}",
                recommended_size_usd=0.0,
            )

        # 8. AI signal check (if provided)
        if signal is not None:
            if not signal.is_actionable:
                return RiskDecision(
                    approved=False,
                    reason=f"AI signal not actionable: {signal.verdict} (conf={signal.confidence:.2f}, risk={signal.resolution_risk})",
                    recommended_size_usd=0.0,
                )
            if signal.resolution_risk == "HIGH":
                warnings.append("AI flagged HIGH resolution risk")
            if signal.risk_flags:
                warnings.extend([f"AI flag: {f}" for f in signal.risk_flags])

        # ── Size calculation ──────────────────────────────────────────────
        available_capital = config.MAX_CAPITAL_USD - self._deployed_capital
        size = min(
            requested_size_usd,
            config.MAX_POSITION_USD,
            available_capital,
        )

        # Kelly fraction adjustment (conservative: 0.25 Kelly)
        if edge.edge_magnitude > 0:
            kelly = edge.edge_magnitude / (1.0 - edge.edge_magnitude)  # simplified
            quarter_kelly_usd = kelly * 0.25 * available_capital
            size = min(size, max(1.0, quarter_kelly_usd))

        size = round(size, 2)

        if size < 1.0:
            return RiskDecision(
                approved=False,
                reason=f"Recommended size ${size:.2f} too small to be worth trading",
                recommended_size_usd=0.0,
            )

        log.info(
            f"Trade APPROVED: {market_id[:20]} | size=${size:.2f} | "
            f"edge={edge.edge_magnitude:.4f} | "
            f"deployed=${self._deployed_capital:.2f}/{config.MAX_CAPITAL_USD}"
        )

        return RiskDecision(
            approved=True,
            reason="All checks passed",
            recommended_size_usd=size,
            warnings=warnings,
        )

    # ─── State Updates ────────────────────────────────────────────────────────

    def record_order_placed(self, market_id: str, size_usd: float) -> None:
        """Call after a successful order placement."""
        self._open_positions[market_id] = self._open_positions.get(market_id, 0.0) + size_usd
        self._deployed_capital += size_usd
        self._total_trades += 1
        self._daily_trades += 1
        log.info(f"Position opened: {market_id[:20]} +${size_usd:.2f} | total deployed=${self._deployed_capital:.2f}")

    def record_fill(self, market_id: str, pnl_usd: float) -> None:
        """Call when a position fills/resolves."""
        position_size = self._open_positions.pop(market_id, 0.0)
        self._deployed_capital = max(0.0, self._deployed_capital - position_size)
        self._daily_pnl += pnl_usd
        self._session_pnl += pnl_usd

        log.info(
            f"Position closed: {market_id[:20]} | PnL=${pnl_usd:.2f} | "
            f"daily=${self._daily_pnl:.2f} | session=${self._session_pnl:.2f}"
        )

        # Auto-kill on daily loss cap
        if self._daily_pnl <= -config.DAILY_LOSS_CAP_USD:
            self.kill(f"Auto-kill: daily loss cap hit (${self._daily_pnl:.2f})")

    # ─── State Queries ────────────────────────────────────────────────────────

    def get_status(self) -> dict:
        """Return current risk state for dashboard display."""
        self._check_day_rollover()
        remaining_daily_loss = config.DAILY_LOSS_CAP_USD + self._daily_pnl
        return {
            "killed": self._killed,
            "kill_reason": self._kill_reason,
            "dry_run": config.DRY_RUN,
            "day": self._day,
            "daily_pnl": round(self._daily_pnl, 4),
            "daily_trades": self._daily_trades,
            "daily_loss_remaining": round(remaining_daily_loss, 4),
            "deployed_capital": round(self._deployed_capital, 4),
            "max_capital": config.MAX_CAPITAL_USD,
            "open_positions": len(self._open_positions),
            "max_positions": config.MAX_CONCURRENT_POS,
            "session_pnl": round(self._session_pnl, 4),
            "total_trades": self._total_trades,
        }
