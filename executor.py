"""
executor.py — Order Execution Engine

The only module that can place real orders. Everything else is analysis.
This module treats real money with paranoid respect.

In DRY_RUN mode: logs what would happen, updates portfolio, no CLOB calls.
In LIVE mode: uses ClobClient, validates response, updates portfolio.

Every call path results in a logged record. Silence means something broke.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import config
from portfolio import Portfolio
from utils.polymarket_client import ClobClient
from utils.logger import get_logger, log_event

if TYPE_CHECKING:
    from edge_detector import EdgeAnalysis
    from ai_analyst import AISignal
    from risk_governor import RiskDecision

log = get_logger("executor")


class Executor:
    """
    Executes approved trade decisions.
    Requires: RiskDecision.approved == True before calling execute().
    """

    def __init__(self, portfolio: Portfolio) -> None:
        self.portfolio = portfolio
        self._clob: ClobClient | None = None

        if not config.DRY_RUN:
            log.warning("LIVE TRADING MODE — real orders will be placed")
            self._init_clob()
        else:
            log.info("DRY RUN MODE — no real orders will be placed")

    def _init_clob(self) -> None:
        """Initialize CLOB client (only in live mode)."""
        try:
            self._clob = ClobClient()
            if not self._clob.verify_auth():
                log.critical("CLOB auth verification FAILED — falling back to dry run")
                self._clob = None
                log_event("ERROR", {
                    "source": "executor",
                    "error": "CLOB auth failed — live trading disabled"
                }, level="CRITICAL")
        except ImportError as e:
            log.critical(f"py-clob-client not installed: {e}")
            self._clob = None
        except Exception as e:
            log.critical(f"CLOB init failed: {e}")
            self._clob = None

    def execute(
        self,
        edge: "EdgeAnalysis",
        signal: "AISignal",
        decision: "RiskDecision",
        market_question: str,
        yes_token_id: str,
        no_token_id: str,
        market_id: str,
    ) -> bool:
        """
        Execute an approved trade. Returns True if executed successfully.

        For BINARY_ARB: places orders on BOTH sides.
        For DIRECTIONAL: places order on recommended_side only.
        """
        if not decision.approved:
            log.error("execute() called with non-approved decision — refusing")
            return False

        size = decision.recommended_size_usd
        success = False

        if edge.recommended_side == "BOTH":
            success = self._execute_both_sides(
                market_id=market_id,
                question=market_question,
                yes_token_id=yes_token_id,
                no_token_id=no_token_id,
                yes_price=edge.yes_price,
                no_price=edge.no_price,
                size_per_side=size,
                reasoning=signal.reasoning,
            )
        elif edge.recommended_side in ("YES", "NO"):
            token_id = yes_token_id if edge.recommended_side == "YES" else no_token_id
            price    = edge.yes_price if edge.recommended_side == "YES" else edge.no_price
            success  = self._execute_single(
                market_id=market_id,
                question=market_question,
                token_id=token_id,
                side=edge.recommended_side,
                price=price,
                size_usd=size,
                reasoning=signal.reasoning,
            )
        else:
            log.warning(f"Unknown recommended_side: {edge.recommended_side}")
            return False

        if success:
            log_event("ORDER_EXECUTED", {
                "market_id": market_id,
                "question": market_question[:100],
                "side": edge.recommended_side,
                "size_usd": size,
                "edge": edge.edge_magnitude,
                "ai_verdict": signal.verdict,
                "ai_confidence": signal.confidence,
                "dry_run": config.DRY_RUN,
            })

        return success

    def _execute_single(
        self,
        market_id: str,
        question: str,
        token_id: str,
        side: str,
        price: float,
        size_usd: float,
        reasoning: str = "",
    ) -> bool:
        """Place a single-side order."""
        if config.DRY_RUN or self._clob is None:
            # Dry run: simulate the order
            self.portfolio.open_position(
                market_id=market_id,
                question=question,
                token_id=token_id,
                side=side,
                price=price,
                size_usd=size_usd,
                order_id=f"dry_{int(time.time())}",
                dry_run=True,
                reasoning=reasoning,
            )
            log.info(f"[DRY RUN] {side} ${size_usd:.2f} @ {price:.3f}")
            return True

        # Live: call CLOB
        resp = self._clob.place_limit_order(
            token_id=token_id,
            side=side,
            price=price,
            size_usdc=size_usd,
        )

        if resp is None:
            log.error(f"Order placement returned None for {market_id}")
            return False

        order_id = resp.get("orderID") or resp.get("id", "unknown")
        self.portfolio.open_position(
            market_id=market_id,
            question=question,
            token_id=token_id,
            side=side,
            price=price,
            size_usd=size_usd,
            order_id=order_id,
            dry_run=False,
            reasoning=reasoning,
        )
        log.info(f"[LIVE] {side} ${size_usd:.2f} @ {price:.3f} → order_id={order_id}")
        return True

    def _execute_both_sides(
        self,
        market_id: str,
        question: str,
        yes_token_id: str,
        no_token_id: str,
        yes_price: float,
        no_price: float,
        size_per_side: float,
        reasoning: str = "",
    ) -> bool:
        """
        Binary arb: buy both YES and NO.
        Atomicity note: if YES order succeeds but NO fails, we have a naked position.
        We log this clearly and let the risk governor know on next cycle.
        """
        yes_ok = self._execute_single(
            market_id=f"{market_id}_YES",
            question=question,
            token_id=yes_token_id,
            side="YES",
            price=yes_price,
            size_usd=size_per_side,
            reasoning=reasoning,
        )

        if not yes_ok:
            log.error("YES order failed — skipping NO to avoid naked position")
            return False

        no_ok = self._execute_single(
            market_id=f"{market_id}_NO",
            question=question,
            token_id=no_token_id,
            side="NO",
            price=no_price,
            size_usd=size_per_side,
            reasoning=reasoning,
        )

        if not no_ok:
            log.critical(
                f"YES order placed but NO order FAILED for {market_id}. "
                f"Naked YES position — manual intervention may be required."
            )
            log_event("NAKED_POSITION", {
                "market_id": market_id,
                "question": question[:100],
                "yes_ok": yes_ok,
                "no_ok": no_ok,
            }, level="CRITICAL")
            return False

        return True

    def cancel_all_open_orders(self) -> int:
        """Cancel all open CLOB orders. Returns count cancelled."""
        if config.DRY_RUN or self._clob is None:
            log.info("[DRY RUN] Would cancel all open orders")
            return 0

        orders = self._clob.get_open_orders()
        cancelled = 0
        for order in orders:
            order_id = order.get("id") or order.get("orderID", "")
            if order_id and self._clob.cancel_order(order_id):
                cancelled += 1

        log.info(f"Cancelled {cancelled}/{len(orders)} open orders")
        return cancelled
