# Polymarket Oracle

**Autonomous trading bot for Polymarket 15-minute binary markets with cryptographically signed decision receipts.**

Every trade decision — what the bot saw, why it entered, and what happened — is recorded as an Ed25519-signed, hash-chained receipt. Tamper-proof. Independently verifiable. No trust required.

## How It Works

The bot trades 15-minute up/down binary markets on Polymarket across BTC, ETH, SOL, and XRP:

1. **Momentum (min 8-12)**: If price moves >0.20% from window open, buy the winning side. Pattern filter skips pump-and-dump setups.
2. **Snipe (min 13-14)**: At minute 13, if direction is clear (>0.10% move), buy at $0.93-0.97 for near-certain $1.00 payout.
3. **Auto-claim**: Resolved positions are redeemed on-chain automatically through Gnosis Safe.

## Signed Trade Receipts

Every decision gets a signed receipt via [ai-decision-tracer](https://pypi.org/project/ai-decision-tracer/):

```
$ python3 btc_15m_bot.py audit

══════════════════════════════════════════════════════════════════════
POLYMARKET ORACLE — SIGNED TRADE AUDIT
══════════════════════════════════════════════════════════════════════

  polymarket-oracle_20260309_receipts.json
  Agent: polymarket-oracle | Receipts: 47 | Chain: VALID

SIGNED TRADE LOG — 22 executions
──────────────────────────────────────────────────────────────────────
  2026-03-09T10:08 | MOMENTUM DOWN | xrp-updown-15m-1773050400 | $0.82 x 25 | EV $+2.15
  2026-03-09T10:23 | MOMENTUM DOWN | xrp-updown-15m-1773051300 | $0.79 x 25 | EV $+3.40
  ...

  Resolutions: 20 | W/L: 18/2 | Signed P&L: $+33.90

══════════════════════════════════════════════════════════════════════
TOTAL: 47 signed receipts | ALL CHAINS VALID
══════════════════════════════════════════════════════════════════════

Public key (Ed25519): nE2oWSRn690/7UBsoFmoi70sc28cBAF5/M68KmurffM=
Anyone can verify these receipts independently with the public key above.
```

Each receipt contains:
- **What the bot saw**: price, move %, pattern classification, orderbook depth
- **Why it traded**: estimated win rate, expected value, strategy type
- **What happened**: actual outcome, P&L, resolution price
- **Proof**: Ed25519 signature + hash chain linking to previous receipt

## Usage

```bash
python3 btc_15m_bot.py scan            # Show current markets + prices
python3 btc_15m_bot.py run             # Dry run (no real orders)
python3 btc_15m_bot.py run --live      # Live trading
python3 btc_15m_bot.py stats           # Trade history
python3 btc_15m_bot.py audit           # Verify signed receipt chain
```

## Performance (Live)

| Metric | Value |
|--------|-------|
| Strategy | Momentum + Snipe |
| Win Rate | 91% (20W / 2L) |
| Live P&L | +$33.90 |
| Assets | BTC, ETH, SOL, XRP |
| Entry Window | Min 8-14 of each 15m window |

## Architecture

```
Binance (price feed) → Pattern Classifier → Signal Generator
                                                    ↓
                                              EV Calculator → Order Placer (Polymarket CLOB)
                                                    ↓                        ↓
                                           ai-decision-tracer ←── Trade Resolution
                                           (signed receipts)          ↓
                                                              Auto-Claim (Gnosis Safe)
```

## Dependencies

```bash
pip install requests py_clob_client ai-decision-tracer web3 eth-keys
```

## Why Signed Receipts?

Anyone can claim a win rate. Signed receipts prove it. The hash chain means you can't cherry-pick — every decision is linked to the previous one. Delete one receipt and the chain breaks. Modify one and the signature fails.

This is what "verifiable track record" looks like for autonomous agents.

---

Built with [ai-decision-tracer](https://github.com/LuciferForge/ai-trace) by [LuciferForge](https://github.com/LuciferForge).
