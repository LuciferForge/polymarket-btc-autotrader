"""
utils/polymarket_client.py — Clean wrapper around py-clob-client.

Handles auth, retry logic, and graceful degradation so the rest of the
codebase never has to deal with raw SDK quirks.
"""

from __future__ import annotations

import time
import logging
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import config
from utils.logger import get_logger

log = get_logger("polymarket_client")

# ─── HTTP session with retry ─────────────────────────────────────────────────
def _build_session(retries: int = 3, backoff: float = 0.5) -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=retries,
        backoff_factor=backoff,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST", "DELETE"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


class GammaClient:
    """
    Wraps Gamma API (market data, orderbook snapshots).
    No auth required — public data.
    """

    def __init__(self) -> None:
        self.base = config.GAMMA_API
        self.session = _build_session()

    def get_markets(
        self,
        limit: int = 100,
        active: bool = True,
        closed: bool = False,
    ) -> list[dict]:
        """Fetch paginated market list from Gamma."""
        params: dict[str, Any] = {
            "limit": limit,
            "active": str(active).lower(),
            "closed": str(closed).lower(),
            "order": "volume24hr",
            "ascending": "false",
        }
        try:
            resp = self.session.get(
                f"{self.base}/markets",
                params=params,
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            # Gamma returns a list directly
            if isinstance(data, list):
                return data
            return data.get("markets", [])
        except requests.RequestException as e:
            log.error(f"Gamma market fetch failed: {e}")
            return []

    def get_market(self, market_id: str) -> dict | None:
        """Fetch single market detail by condition_id."""
        try:
            resp = self.session.get(
                f"{self.base}/markets/{market_id}",
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            log.warning(f"Gamma single market fetch failed ({market_id}): {e}")
            return None

    def get_orderbook(self, token_id: str) -> dict | None:
        """Fetch current orderbook for a token (outcome)."""
        try:
            resp = self.session.get(
                f"{config.CLOB_API}/book",
                params={"token_id": token_id},
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            log.warning(f"Orderbook fetch failed ({token_id}): {e}")
            return None

    def get_midpoint(self, token_id: str) -> float | None:
        """Return midpoint price for a token_id, or None on failure."""
        book = self.get_orderbook(token_id)
        if not book:
            return None
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        if not bids or not asks:
            return None
        try:
            best_bid = float(bids[0]["price"])
            best_ask = float(asks[0]["price"])
            return (best_bid + best_ask) / 2.0
        except (KeyError, IndexError, ValueError):
            return None


class ClobClient:
    """
    Wraps py-clob-client for authenticated order operations.
    Raises ImportError cleanly if SDK not installed.
    """

    def __init__(self) -> None:
        try:
            from py_clob_client.client import ClobClient as _SDK
            from py_clob_client.clob_types import ApiCreds
        except ImportError:
            raise ImportError(
                "py-clob-client not installed. Run: python3 -m pip install py-clob-client"
            )

        creds = ApiCreds(
            api_key=config.API_KEY,
            api_secret=config.API_SECRET,
            api_passphrase=config.API_PASSPHRASE,
        )
        self._client = _SDK(
            host=config.CLOB_API,
            key=config.PRIVATE_KEY,
            chain_id=config.CHAIN_ID,
            signature_type=2,   # POLY_GNOSIS_SAFE — proxy wallet
            funder=config.PROXY_ADDRESS,
            creds=creds,
        )
        log.info("ClobClient initialized (proxy wallet: %s)", config.PROXY_ADDRESS[:10] + "...")

    def place_limit_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size_usdc: float,
    ) -> dict | None:
        """
        Place a GTC limit order.
        side: 'BUY' or 'SELL'
        price: 0.01–0.99 float
        size_usdc: dollar amount (not shares)
        Returns order response dict or None on failure.
        """
        from py_clob_client.clob_types import OrderArgs, OrderType

        try:
            # Convert USDC size to shares: shares = usdc / price
            shares = round(size_usdc / price, 2)

            order_args = OrderArgs(
                token_id=token_id,
                price=round(price, 2),
                size=shares,
                side=side,
            )
            signed = self._client.create_order(order_args)
            resp = self._client.post_order(signed, OrderType.GTC)
            log.info(f"Order placed: {side} {shares} shares @ {price} | id={resp.get('orderID', 'N/A')}")
            return resp
        except Exception as e:
            log.error(f"Order placement failed: {e}")
            return None

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order by ID."""
        try:
            self._client.cancel(order_id)
            log.info(f"Order cancelled: {order_id}")
            return True
        except Exception as e:
            log.warning(f"Cancel failed ({order_id}): {e}")
            return False

    def get_open_orders(self) -> list[dict]:
        """Fetch all open orders for this account."""
        try:
            return self._client.get_orders() or []
        except Exception as e:
            log.error(f"Failed to fetch open orders: {e}")
            return []

    def get_positions(self) -> list[dict]:
        """Fetch current positions."""
        try:
            return self._client.get_positions() or []
        except Exception as e:
            log.error(f"Failed to fetch positions: {e}")
            return []

    def verify_auth(self) -> bool:
        """Quick auth sanity check — returns True if credentials work."""
        try:
            self._client.get_orders()
            return True
        except Exception as e:
            log.error(f"Auth verification failed: {e}")
            return False
