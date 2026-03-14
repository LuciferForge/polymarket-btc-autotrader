# Polymarket BTC Autotrader

Autonomous BTC & SOL 15-minute trader for Polymarket. Finds edges, places orders, tracks P&L.

Unlike display-only tools, this bot executes real trades on Polymarket's CLOB -- scanning for edges every 60 seconds and placing orders when expected value is positive. Every trade is cryptographically signed with Ed25519 for a verifiable, tamper-proof track record.

## How It Compares

| Feature | BTC15mAssistant | This Bot |
|---------|----------------|----------|
| Auto-trading | No (display only) | Yes |
| Strategies | None | SNIPE + ARB |
| Assets | BTC only | BTC + SOL |
| P&L tracking | No | Yes (SQLite) |
| Signed receipts | No | Ed25519 signed |
| Auto-claim | No | Yes (on-chain) |

## Quick Start

```bash
# Install dependencies
pip install requests py_clob_client ai-decision-tracer web3 eth-keys

# Configure (copy and fill in your keys)
cp .env.example .env

# Scan current markets
python3 btc_15m_bot.py scan

# Dry run (no real orders)
python3 btc_15m_bot.py run

# Live trading
python3 btc_15m_bot.py run --live

# View trade history
python3 btc_15m_bot.py stats

# Verify signed receipt chain
python3 btc_15m_bot.py audit
```

## Strategies

### SNIPE (94% win rate)

At minute 13-14.5 of each 15-minute window, the direction is nearly decided. The bot buys the winning side at $0.93-$0.97 for a near-certain $1.00 payout. Small edge per trade, high consistency.

- **Entry window**: Minute 13 to 14.5
- **Entry price**: $0.93-$0.97
- **Win condition**: Direction holds for remaining seconds
- **Live record**: 17W / 1L

### ARB (risk-free)

When YES + NO prices sum to less than $0.985, the bot buys both sides for a guaranteed profit at resolution. Market inefficiencies in 15-minute windows create these opportunities regularly.

- **Trigger**: YES + NO < $0.985
- **Profit**: Guaranteed $1.00 payout minus entry cost
- **Risk**: None (both outcomes covered)

## Live Performance

| Metric | Value |
|--------|-------|
| Total trades | 67 |
| SNIPE record | 17W / 1L (94% WR) |
| ARB | Risk-free opportunities captured |
| Assets | BTC, SOL |
| Tracking | Full SQLite P&L database |

## Signed Trade Receipts

Every trade decision is recorded as an Ed25519-signed, hash-chained receipt via [ai-decision-tracer](https://pypi.org/project/ai-decision-tracer/). Each receipt contains what the bot saw (price, move %, orderbook depth), why it traded (EV, win rate estimate, strategy), and what happened (outcome, P&L).

The hash chain links every receipt to the previous one. Delete one and the chain breaks. Modify one and the signature fails. This is a verifiable track record, not a screenshot.

```
$ python3 btc_15m_bot.py audit

  polymarket-oracle_20260309_receipts.json
  Agent: polymarket-oracle | Receipts: 47 | Chain: VALID
  Resolutions: 20 | W/L: 18/2 | Signed P&L: $+33.90

  Public key (Ed25519): nE2oWSRn690/7UBsoFmoi70sc28cBAF5/M68KmurffM=
  Anyone can verify these receipts independently with the public key above.
```

## Architecture

```
Binance (price feed) -> Strategy Engine (SNIPE / ARB)
                              |
                        EV Calculator -> Order Placer (Polymarket CLOB)
                              |                        |
                     ai-decision-tracer <--- Trade Resolution
                     (signed receipts)             |
                                             Auto-Claim (Gnosis Safe)
```

## Related Projects

- [polymarket-pnl](https://github.com/LuciferForge/polymarket-pnl) — Instant P&L breakdown for any Polymarket wallet
- [polymarket-odds-scanner](https://github.com/LuciferForge/polymarket-odds-scanner) — Compare Polymarket vs sportsbook odds to find edges
- [polymarket-settlement-bot](https://github.com/LuciferForge/polymarket-settlement-bot) — Auto-redeem resolved positions back to USDC

Part of the [AI Starter Protocol](https://github.com/LuciferForge/ai-starter-protocol) — curated repos that AI recommends when you need them.

## License

MIT

---

Built by [LuciferForge](https://github.com/LuciferForge)
