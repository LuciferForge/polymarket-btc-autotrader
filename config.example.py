"""
config.example.py — Copy to config.py and fill in your credentials.

Required environment variables (or set directly):
  PRIVATE_KEY              — Polygon wallet private key (for Gnosis Safe signing)
  POLYMARKET_PROXY_ADDRESS — Your Polymarket proxy/trading address
  POLYMARKET_API_KEY       — CLOB API key
  POLYMARKET_SECRET        — CLOB API secret
  POLYMARKET_PASSPHRASE    — CLOB API passphrase
  RPC_URL                  — Polygon RPC endpoint (default: public node)
"""

import os

# ─── Wallet / Auth ───────────────────────────────────────────────────────────
PRIVATE_KEY: str = os.environ.get("PRIVATE_KEY", "")
PROXY_ADDRESS: str = os.environ.get("POLYMARKET_PROXY_ADDRESS", "")
API_KEY: str = os.environ.get("POLYMARKET_API_KEY", "")
API_SECRET: str = os.environ.get("POLYMARKET_SECRET", "")
API_PASSPHRASE: str = os.environ.get("POLYMARKET_PASSPHRASE", "")
RPC_URL: str = os.environ.get("RPC_URL", "https://polygon-bor-rpc.publicnode.com")
CHAIN_ID: int = 137  # Polygon mainnet

# ─── API Endpoints ───────────────────────────────────────────────────────────
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
