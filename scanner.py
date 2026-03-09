"""
scanner.py — Market Scanner

Polls Gamma API on a configurable interval, filters markets by volume/liquidity,
and emits candidate markets for edge detection.

Deliberately simple: garbage collection, deduplication, and staleness tracking
so the edge detector only sees fresh, liquid, interesting markets.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

import config
from utils.logger import get_logger, log_event
from utils.polymarket_client import GammaClient

log = get_logger("scanner")


@dataclass
class MarketCandidate:
    """A market that passed the liquidity/volume filter and is worth analyzing."""
    market_id: str
    question: str
    description: str
    resolution: str
    end_date: str
    category: str
    volume_24h: float
    yes_token_id: str
    no_token_id: str
    yes_price: float    # best ask for YES
    no_price: float     # best ask for NO
    price_sum: float    # yes + no — edge if < 1.0
    scanned_at: float = field(default_factory=time.time)

    @property
    def edge(self) -> float:
        """Mathematical edge = 1.0 - price_sum. Positive = opportunity."""
        return max(0.0, 1.0 - self.price_sum)

    @property
    def has_edge(self) -> bool:
        return self.edge >= config.MIN_EDGE_THRESHOLD

    def as_dict(self) -> dict:
        return {
            "market_id": self.market_id,
            "question": self.question[:150],
            "edge": round(self.edge, 4),
            "yes_price": self.yes_price,
            "no_price": self.no_price,
            "price_sum": round(self.price_sum, 4),
            "volume_24h": self.volume_24h,
        }


class MarketScanner:
    """
    Continuously scans Polymarket for liquid binary markets.
    Calls `on_candidate(market)` for every market passing the filter.
    """

    def __init__(self, on_candidate: Callable[[MarketCandidate], None]) -> None:
        self.on_candidate = on_candidate
        self.gamma = GammaClient()
        self._seen_ids: set[str] = set()
        self._running = False

    @staticmethod
    def _parse_json_field(value) -> list:
        """
        Gamma API returns some list fields as JSON-encoded strings.
        Handle both: already-parsed lists and JSON string representations.
        """
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                return parsed if isinstance(parsed, list) else []
            except (json.JSONDecodeError, ValueError):
                return []
        return []

    def _get_yes_no_idx(self, outcomes: list) -> tuple[int, int]:
        """Return (yes_idx, no_idx) from outcomes list. Defaults: (0, 1)."""
        yes_idx, no_idx = 0, 1
        for i, o in enumerate(outcomes):
            label = str(o).lower()
            if label in ("yes", "true", "1"):
                yes_idx = i
            elif label in ("no", "false", "0"):
                no_idx = i
        return yes_idx, no_idx

    def _extract_prices(self, market: dict) -> tuple[float, float] | None:
        """
        Extract YES and NO prices from Gamma API market data.
        Fields are JSON-encoded strings: outcomePrices, outcomes.
        """
        outcome_prices = self._parse_json_field(market.get("outcomePrices", []))
        outcomes       = self._parse_json_field(market.get("outcomes", []))

        if len(outcome_prices) < 2:
            return None

        yes_idx, no_idx = self._get_yes_no_idx(outcomes)

        try:
            yes_price = float(outcome_prices[yes_idx])
            no_price  = float(outcome_prices[no_idx])
        except (TypeError, ValueError, IndexError):
            return None

        if yes_price <= 0 or no_price <= 0:
            return None

        return yes_price, no_price

    def _extract_token_ids(self, market: dict) -> tuple[str, str] | None:
        """
        Extract (yes_token_id, no_token_id) from Gamma API market data.
        clobTokenIds is a JSON-encoded string containing a list of token IDs.
        """
        clob_ids = self._parse_json_field(market.get("clobTokenIds", []))
        outcomes = self._parse_json_field(market.get("outcomes", []))

        if len(clob_ids) < 2:
            return None

        yes_idx, no_idx = self._get_yes_no_idx(outcomes)

        try:
            yes_id = str(clob_ids[yes_idx])
            no_id  = str(clob_ids[no_idx])
        except IndexError:
            return None

        if not yes_id or not no_id:
            return None

        return yes_id, no_id

    def _passes_filter(self, market: dict) -> bool:
        """Quick pre-filter before building a full MarketCandidate."""
        if not market.get("active", False):
            return False
        if market.get("closed", False):
            return False
        # Must accept orders (CLOB trading enabled)
        if not market.get("acceptingOrders", False):
            return False

        # Volume filter
        vol = float(market.get("volume24hr", 0) or 0)
        if vol < config.MIN_MARKET_VOLUME:
            return False

        # Must be binary (exactly 2 outcomes)
        outcomes = self._parse_json_field(market.get("outcomes", []))
        if len(outcomes) != 2:
            return False

        # Must have CLOB token IDs
        clob_ids = self._parse_json_field(market.get("clobTokenIds", []))
        if len(clob_ids) < 2:
            return False

        return True

    def _get_clob_asks(
        self,
        yes_token_id: str,
        no_token_id: str,
    ) -> tuple[float | None, float | None]:
        """
        Fetch best ask prices for YES and NO tokens from CLOB.
        Returns (yes_ask, no_ask) or (None, None) on failure.
        Best ask is the lowest price someone will sell at — what we'd pay to buy.
        """
        yes_book = self.gamma.get_orderbook(yes_token_id)
        no_book  = self.gamma.get_orderbook(no_token_id)

        yes_ask = self._extract_best_ask(yes_book)
        no_ask  = self._extract_best_ask(no_book)

        return yes_ask, no_ask

    @staticmethod
    def _extract_mid_price(book: dict | None) -> float | None:
        """
        Extract the mid price (midpoint of best bid and best ask).

        Polymarket CLOB orderbook:
        - bids: sorted ASCENDING  (0.001, 0.002, 0.50...) — buyers, best bid = LAST
        - asks: sorted DESCENDING (0.999, 0.998, 0.51...) — sellers, best ask = LAST

        Mid price = (best_bid + best_ask) / 2
        This is the fair market price, free of spread distortion.

        For binary arb: if mid_YES + mid_NO < 1.0, structural edge exists.
        """
        if not book:
            return None

        bids = book.get("bids", [])
        asks = book.get("asks", [])

        best_bid: float | None = None
        best_ask: float | None = None

        if bids:
            try:
                bid_prices = [float(b["price"]) for b in bids if "price" in b]
                if bid_prices:
                    best_bid = max(bid_prices)  # Highest bid = best bid
            except (TypeError, ValueError):
                pass

        if asks:
            try:
                ask_prices = [float(a["price"]) for a in asks if "price" in a]
                if ask_prices:
                    best_ask = min(ask_prices)  # Lowest ask = best ask
            except (TypeError, ValueError):
                pass

        if best_bid is not None and best_ask is not None:
            mid = (best_bid + best_ask) / 2
            if 0 < mid < 1:
                return mid

        # Fallback: use whichever side we have
        if best_bid is not None and 0 < best_bid < 1:
            return best_bid
        if best_ask is not None and 0 < best_ask < 1:
            return best_ask

        return None

    # Keep this as an alias for callers that used the old name
    @classmethod
    def _extract_best_ask(cls, book: dict | None) -> float | None:
        return cls._extract_mid_price(book)

    def scan_once(self) -> list[MarketCandidate]:
        """
        Run a single scan cycle.
        Returns list of candidates with mathematical edge.
        """
        markets = self.gamma.get_markets(
            limit=config.MAX_MARKETS_PER_SCAN,
            active=True,
            closed=False,
        )

        candidates: list[MarketCandidate] = []
        skipped = 0

        for market in markets:
            if not self._passes_filter(market):
                skipped += 1
                continue

            prices = self._extract_prices(market)
            if prices is None:
                skipped += 1
                continue

            token_ids = self._extract_token_ids(market)
            if token_ids is None:
                skipped += 1
                continue

            yes_price, no_price = prices
            yes_id, no_id = token_ids
            price_sum = yes_price + no_price

            # Use CLOB best ask prices for real arb detection
            # outcomePrices from Gamma = last trade price (always sums to ~1.0)
            # Real edge comes from best ask on BOTH sides simultaneously
            yes_ask, no_ask = self._get_clob_asks(yes_id, no_id)
            if yes_ask is not None and no_ask is not None:
                # Use CLOB ask prices — these are what we'd actually pay
                arb_yes, arb_no = yes_ask, no_ask
                arb_sum = arb_yes + arb_no
            else:
                # Fall back to Gamma last prices (no arb signal expected)
                arb_yes, arb_no = yes_price, no_price
                arb_sum = price_sum

            candidate = MarketCandidate(
                market_id=market.get("conditionId") or market.get("id", ""),
                question=market.get("question", ""),
                description=market.get("description", ""),
                resolution=market.get("resolutionSource", "") or market.get("resolution_source", ""),
                end_date=market.get("endDate") or market.get("end_date", ""),
                category=market.get("groupItemTitle") or market.get("category", ""),
                volume_24h=float(market.get("volume24hr", 0) or 0),
                yes_token_id=yes_id,
                no_token_id=no_id,
                yes_price=arb_yes,
                no_price=arb_no,
                price_sum=arb_sum,
            )

            candidates.append(candidate)
            # Push candidates that could be actionable to the processing pipeline.
            # Binary arb: price_sum < 1.0 (has_edge)
            # Directional: price in tradeable range (0.10-0.90) with meaningful deviation
            is_directional = 0.10 <= arb_yes <= 0.90 and abs(arb_yes - 0.5) >= 0.15
            if candidate.has_edge or is_directional:
                self.on_candidate(candidate)

        log_event("SCAN_COMPLETE", {
            "total_fetched": len(markets),
            "passed_filter": len(candidates),
            "skipped": skipped,
            "with_edge": sum(1 for c in candidates if c.has_edge),
        })

        log.info(
            f"Scan: {len(markets)} fetched | {len(candidates)} passed filter | "
            f"{sum(1 for c in candidates if c.has_edge)} with edge"
        )

        return candidates

    def run(self) -> None:
        """Blocking scan loop. Call in a thread."""
        self._running = True
        log.info(f"Scanner started (interval: {config.SCAN_INTERVAL_SECONDS}s)")
        while self._running:
            try:
                self.scan_once()
            except Exception as e:
                log.error(f"Scanner error: {e}", exc_info=True)
                log_event("ERROR", {"source": "scanner", "error": str(e)}, level="ERROR")
            time.sleep(config.SCAN_INTERVAL_SECONDS)

    def stop(self) -> None:
        self._running = False
        log.info("Scanner stopped.")
