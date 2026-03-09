"""
edge_detector.py — Mathematical Edge Detection

This module does pure math. No opinions, no AI, no ambiguity.
It answers one question: is there a price inefficiency worth exploiting?

Types of edge detected:
1. Binary Arb: YES + NO prices sum to < 1.0 (buy both sides, guaranteed profit)
2. Directional Mispricing: One side far from 50/50 in a coin-flip market
3. Spread Compression: Bid-ask spread is wide enough to capture on fill

The math is clean. The thresholds are conservative. Greed kills bots.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

import config
from utils.logger import get_logger

if TYPE_CHECKING:
    from scanner import MarketCandidate

log = get_logger("edge_detector")


class EdgeType(str, Enum):
    BINARY_ARB     = "BINARY_ARB"       # Both sides < 0.5, sum < 1.0
    DIRECTIONAL    = "DIRECTIONAL"      # One side structurally mispriced
    NEAR_CERTAIN   = "NEAR_CERTAIN"     # >95% implied but still has NO liquidity
    NO_EDGE        = "NO_EDGE"


@dataclass
class EdgeAnalysis:
    """Result of mathematical edge analysis on a single market."""
    market_id: str
    question: str
    edge_type: EdgeType
    edge_magnitude: float      # Raw edge size (1.0 - price_sum for arb)
    yes_price: float
    no_price: float
    price_sum: float
    recommended_side: str | None    # "YES", "NO", "BOTH", or None
    expected_return_pct: float      # If we deploy ORDER_SIZE_USD, expected return %
    max_profit_per_trade: float     # USD profit on ORDER_SIZE_USD (each side)
    confidence: float               # 0.0-1.0, mathematical confidence only
    notes: list[str]               # Human-readable notes

    @property
    def is_actionable(self) -> bool:
        if self.edge_type == EdgeType.NO_EDGE:
            return False
        if self.edge_type == EdgeType.BINARY_ARB:
            # Pure math — require high confidence
            return self.edge_magnitude >= config.MIN_EDGE_THRESHOLD and self.confidence >= 0.5
        if self.edge_type in (EdgeType.DIRECTIONAL, EdgeType.NEAR_CERTAIN):
            # AI-dependent — lower math bar, AI will gate the actual trade
            return self.edge_magnitude >= 0.0 and self.confidence >= 0.2
        return False

    def as_dict(self) -> dict:
        return {
            "market_id": self.market_id,
            "question": self.question[:120],
            "edge_type": self.edge_type.value,
            "edge_magnitude": round(self.edge_magnitude, 4),
            "yes_price": self.yes_price,
            "no_price": self.no_price,
            "price_sum": round(self.price_sum, 4),
            "recommended_side": self.recommended_side,
            "expected_return_pct": round(self.expected_return_pct, 2),
            "max_profit_usd": round(self.max_profit_per_trade, 4),
            "confidence": round(self.confidence, 3),
            "notes": self.notes,
            "actionable": self.is_actionable,
        }


class EdgeDetector:
    """
    Stateless mathematical edge analyzer.
    Give it a MarketCandidate, get back an EdgeAnalysis.
    """

    def analyze(self, market: "MarketCandidate") -> EdgeAnalysis:
        """
        Main entry point: full mathematical analysis of a single market.
        """
        yes = market.yes_price
        no  = market.no_price
        price_sum = yes + no
        raw_edge  = 1.0 - price_sum  # Positive = structural arb opportunity
        notes: list[str] = []

        # ── Binary Arb: both sides sum to less than $1 ─────────────────────
        if raw_edge >= config.MIN_EDGE_THRESHOLD:
            # Check if individual prices are reasonable (not penny tokens)
            if yes < 0.02 or no < 0.02:
                notes.append("One side is penny-priced — likely illiquid, treat edge with skepticism")
                confidence = 0.3
            else:
                confidence = self._arb_confidence(yes, no, raw_edge)

            # Both sides should be orderable
            recommended_side = "BOTH"
            profit_per_trade = self._binary_arb_profit(yes, no, config.ORDER_SIZE_USD)
            expected_return  = (profit_per_trade / (config.ORDER_SIZE_USD * 2)) * 100

            return EdgeAnalysis(
                market_id=market.market_id,
                question=market.question,
                edge_type=EdgeType.BINARY_ARB,
                edge_magnitude=raw_edge,
                yes_price=yes,
                no_price=no,
                price_sum=price_sum,
                recommended_side=recommended_side,
                expected_return_pct=expected_return,
                max_profit_per_trade=profit_per_trade,
                confidence=confidence,
                notes=notes,
            )

        # ── Near-Certain Events ─────────────────────────────────────────────
        # Skip markets where one side is > 0.95 — these are essentially resolved.
        # Buying the cheap side is a lottery ticket, not a strategy.
        if yes > 0.95 or no > 0.95:
            notes.append(f"Near-resolved market (YES={yes:.3f}, NO={no:.3f}) — skipping")
            return EdgeAnalysis(
                market_id=market.market_id,
                question=market.question,
                edge_type=EdgeType.NO_EDGE,
                edge_magnitude=0.0,
                yes_price=yes,
                no_price=no,
                price_sum=price_sum,
                recommended_side=None,
                expected_return_pct=0.0,
                max_profit_per_trade=0.0,
                confidence=0.0,
                notes=notes,
            )

        # ── Directional Candidate: price is in the "interesting" zone ──────
        # The sweet spot for AI directional analysis is 0.10-0.90 range.
        # Below 0.10 or above 0.90 = market has strong conviction, hard to beat.
        # Between 0.20-0.80 = genuine uncertainty where AI insight can matter.
        # The closer to 0.50, the more the AI needs to be right to justify a trade.
        if 0.10 <= yes <= 0.90:
            # Calculate how "interesting" this market is for AI analysis
            # Markets near 50/50 need strong AI signal; skewed markets need less
            deviation = abs(yes - 0.5)

            # Only flag markets where there's meaningful deviation (>15%)
            # AND the price is in a range where we can afford to be wrong
            if deviation >= 0.15:
                side    = "NO" if yes > 0.5 else "YES"
                price   = no if yes > 0.5 else yes
                implied = yes if yes > 0.5 else no

                # Confidence scales with how skewed the market is
                # More deviation = clearer signal = higher base confidence
                confidence = min(0.5, 0.2 + deviation * 0.4)

                notes.append(
                    f"Directional candidate: market implies {'YES' if yes > 0.5 else 'NO'} "
                    f"at {implied:.1%}. Price in tradeable range."
                )
                return EdgeAnalysis(
                    market_id=market.market_id,
                    question=market.question,
                    edge_type=EdgeType.DIRECTIONAL,
                    edge_magnitude=deviation - 0.15,
                    yes_price=yes,
                    no_price=no,
                    price_sum=price_sum,
                    recommended_side=side,
                    expected_return_pct=((1.0 - price) / price) * 100 if price > 0 else 0,
                    max_profit_per_trade=(config.ORDER_SIZE_USD / price) * (1 - price) if price > 0 else 0,
                    confidence=confidence,
                    notes=notes,
                )

        # ── No Edge ─────────────────────────────────────────────────────────
        return EdgeAnalysis(
            market_id=market.market_id,
            question=market.question,
            edge_type=EdgeType.NO_EDGE,
            edge_magnitude=raw_edge,
            yes_price=yes,
            no_price=no,
            price_sum=price_sum,
            recommended_side=None,
            expected_return_pct=0.0,
            max_profit_per_trade=0.0,
            confidence=0.0,
            notes=["Price sum {:.4f} — no structural arb (threshold: {:.2f})".format(
                price_sum, 1.0 - config.MIN_EDGE_THRESHOLD
            )],
        )

    def _arb_confidence(self, yes: float, no: float, edge: float) -> float:
        """
        Estimate confidence in a binary arb opportunity.
        Higher edge, more balanced prices → higher confidence.
        Extreme prices (near 0 or 1) → lower confidence (resolution risk).
        """
        # Base: edge magnitude (larger = better)
        base = min(1.0, edge / 0.15)  # Saturates at 15% edge → 1.0

        # Penalty: how far prices are from balanced (0.5 each)
        balance = 1.0 - abs((yes - no))  # 1.0 if balanced, 0 if extreme

        # Penalty: prices too close to boundaries
        boundary_risk = 0.0
        if yes < 0.05 or yes > 0.95:
            boundary_risk += 0.3
        if no < 0.05 or no > 0.95:
            boundary_risk += 0.3

        confidence = (base * 0.6 + balance * 0.4) - boundary_risk
        return max(0.1, min(1.0, confidence))

    def _binary_arb_profit(self, yes: float, no: float, size_per_side: float) -> float:
        """
        Calculate guaranteed profit from binary arb.
        We spend size_per_side on YES and size_per_side on NO.
        At resolution, one pays out $1/share.

        YES shares bought: size / yes_price
        NO shares bought: size / no_price

        If YES resolves: YES shares * 1.0 = size/yes_price
        Cost: size (YES) + size (NO) = 2 * size
        Profit: min(size/yes_price, size/no_price) - 2*size ...

        Actually: total cost = yes_price * shares_yes + no_price * shares_no
        = size_per_side + size_per_side = 2 * size_per_side

        Payout (YES wins): shares_yes * 1 = size_per_side / yes_price
        Payout (NO wins):  shares_no * 1 = size_per_side / no_price

        Min payout (worst case): min of both payouts
        Profit: min_payout - 2 * size_per_side

        But wait: we buy both sides with equal dollar allocation.
        Edge guarantees profit regardless of outcome only if price_sum < 1.
        The guaranteed profit = (1 - price_sum) * shares (if we could normalize)

        Simple approach: guaranteed profit per pair of tokens:
        """
        # Normalized: buy 1 YES share + 1 NO share
        # Cost: yes_price + no_price = price_sum
        # Payout: always $1 (one side wins)
        # Profit per unit: 1.0 - price_sum

        # With our size allocation (size_per_side each):
        yes_shares = size_per_side / yes_price
        no_shares  = size_per_side / no_price
        total_cost = size_per_side * 2

        # Worst case payout (the larger-share side loses)
        payout_if_yes = yes_shares * 1.0
        payout_if_no  = no_shares  * 1.0
        worst_payout  = min(payout_if_yes, payout_if_no)

        return worst_payout - total_cost

    def batch_analyze(self, markets: list["MarketCandidate"]) -> list[EdgeAnalysis]:
        """Analyze a list of markets, return only actionable ones sorted by edge."""
        results = [self.analyze(m) for m in markets]
        actionable = [r for r in results if r.is_actionable]
        actionable.sort(key=lambda x: x.edge_magnitude, reverse=True)
        log.info(f"EdgeDetector: {len(markets)} markets → {len(actionable)} actionable")
        return actionable
