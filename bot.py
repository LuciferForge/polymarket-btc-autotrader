"""
bot.py — Main Orchestrator

This is the spine. It wires together scanner → edge_detector → ai_analyst
→ risk_governor → executor, and manages the full bot lifecycle.

Architecture:
- Scanner runs in a background thread, pushing candidates into a queue
- Main thread processes queue: edge → AI → risk → execute
- Dashboard runs in its own thread for UI
- Signal handlers ensure clean shutdown

Startup sequence:
1. Validate config and credentials
2. Check Ollama health
3. Start scanner thread
4. Start dashboard thread
5. Main loop: process candidate queue

Run with:
    python3 bot.py                  # Dry run (default, safe)
    LIVE_TRADING=1 python3 bot.py   # Live trading (real money)
"""

from __future__ import annotations

import os
import sys
import time
import signal
import queue
import threading
import argparse
from datetime import datetime, timezone

# ─── Import guard: must have credentials ─────────────────────────────────────
try:
    import config
except FileNotFoundError as e:
    print(f"[FATAL] {e}")
    sys.exit(1)

from scanner import MarketScanner, MarketCandidate
from edge_detector import EdgeDetector, EdgeType
from ai_analyst import AIAnalyst
from risk_governor import RiskGovernor
from executor import Executor
from portfolio import Portfolio
from dashboard import Dashboard, DashboardState, print_status, RICH_AVAILABLE
from utils.logger import get_logger, log_event

log = get_logger("bot")


class PolymarketBot:
    """
    Main bot class. Single instance per process.
    """

    def __init__(self, skip_ai: bool = False) -> None:
        self.skip_ai = skip_ai

        # ── Core components ──────────────────────────────────────────────────
        self.portfolio  = Portfolio()
        self.risk       = RiskGovernor()
        self.edge       = EdgeDetector()
        self.analyst    = AIAnalyst()
        self.executor   = Executor(self.portfolio)

        # ── Dashboard state (shared across threads) ───────────────────────────
        self.dash_state = DashboardState()
        self.dashboard  = Dashboard(self.dash_state, self.risk, self.portfolio, self.analyst)

        # ── Candidate queue (scanner → main loop) ──────────────────────────
        self._candidate_queue: queue.Queue[MarketCandidate] = queue.Queue(maxsize=200)

        # ── Scanner ────────────────────────────────────────────────────────
        self.scanner = MarketScanner(on_candidate=self._enqueue_candidate)

        # ── Shutdown coordination ──────────────────────────────────────────
        self._shutdown = threading.Event()

    def _enqueue_candidate(self, candidate: MarketCandidate) -> None:
        """Called by scanner thread for each market with edge. Thread-safe."""
        try:
            self._candidate_queue.put_nowait(candidate)
            self.dash_state.scan_count += 1
        except queue.Full:
            log.warning("Candidate queue full — dropping market (scanner too fast?)")

    def _process_candidate(self, market: MarketCandidate) -> None:
        """
        Full processing pipeline for a single candidate.
        scanner → edge → AI → risk → execute
        """
        # 1. Mathematical edge analysis
        edge_analysis = self.edge.analyze(market)
        if not edge_analysis.is_actionable:
            return

        # 2. AI analysis (skip if --no-ai flag or Ollama down)
        signal = None
        if not self.skip_ai:
            signal = self.analyst.analyze(market, edge_analysis)
        else:
            # Create a permissive dummy signal for testing without AI
            from ai_analyst import AISignal
            signal = AISignal(
                market_id=market.market_id,
                verdict=edge_analysis.recommended_side or "BUY_BOTH",
                confidence=0.7,
                reasoning="AI skipped (--no-ai mode)",
                implied_fair_yes=None,
                implied_fair_no=None,
                risk_flags=[],
                resolution_risk="MEDIUM",
                model_used="none",
                latency_sec=0.0,
            )

        # 3. Risk check (with horizon awareness)
        risk_decision = self.risk.check_trade(
            market_id=market.market_id,
            edge=edge_analysis,
            signal=signal,
            end_date=market.end_date,
        )

        action = "SKIPPED"
        if risk_decision.approved:
            # 4. Execute
            success = self.executor.execute(
                edge=edge_analysis,
                signal=signal,
                decision=risk_decision,
                market_question=market.question,
                yes_token_id=market.yes_token_id,
                no_token_id=market.no_token_id,
                market_id=market.market_id,
            )
            if success:
                # Update risk governor with position size
                self.risk.record_order_placed(
                    market_id=market.market_id,
                    size_usd=risk_decision.recommended_size_usd,
                )
                self.dash_state.orders_placed += 1
                action = "DRY_RUN_ORDER" if config.DRY_RUN else "ORDER_PLACED"
            else:
                action = "EXECUTION_FAILED"
        else:
            reason = risk_decision.reason
            log.debug(f"Trade blocked: {reason} | {market.question[:50]}...")
            action = f"BLOCKED: {reason[:30]}"

        # 5. Update dashboard state
        self.dash_state.add_opportunity(market, edge_analysis, signal, action)

    def _main_loop(self) -> None:
        """Process candidates from the queue until shutdown."""
        log.info("Main processing loop started")
        while not self._shutdown.is_set():
            if self.risk.is_killed():
                log.warning(f"Kill switch active: {self.risk.kill_reason} — main loop paused")
                time.sleep(5)
                continue

            try:
                market = self._candidate_queue.get(timeout=1.0)
                self._process_candidate(market)
                self._candidate_queue.task_done()
            except queue.Empty:
                continue
            except Exception as e:
                log.error(f"Error processing candidate: {e}", exc_info=True)
                log_event("ERROR", {"source": "main_loop", "error": str(e)}, level="ERROR")
                self.dash_state.add_event(f"Processing error: {str(e)[:60]}", "ERROR")

    def start(self) -> None:
        """
        Start all threads and begin trading.
        Blocks until Ctrl+C or kill switch.
        """
        self._print_startup_banner()

        # ── Ollama health check ────────────────────────────────────────────
        if not self.skip_ai:
            health = self.analyst.check_ollama_health()
            if not health["primary_available"]:
                log.warning(
                    "Claude Haiku API not available. "
                    "Set ANTHROPIC_API_KEY in .env OR use --no-ai flag"
                )
                self.dash_state.add_event("Claude Haiku unavailable — AI analysis disabled", "WARNING")

        # ── Start dashboard ───────────────────────────────────────────────
        self.dashboard.start()

        # ── Start scanner thread ──────────────────────────────────────────
        scanner_thread = threading.Thread(
            target=self.scanner.run,
            name="scanner",
            daemon=True,
        )
        scanner_thread.start()

        # ── Register signal handlers ──────────────────────────────────────
        signal.signal(signal.SIGINT,  self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

        log.info(f"Bot started | DRY_RUN={config.DRY_RUN} | skip_ai={self.skip_ai}")
        log_event("BOT_STARTED", {
            "dry_run": config.DRY_RUN,
            "skip_ai": self.skip_ai,
            "scan_interval": config.SCAN_INTERVAL_SECONDS,
            "max_capital": config.MAX_CAPITAL_USD,
            "daily_loss_cap": config.DAILY_LOSS_CAP_USD,
        })

        # ── Main loop (blocks) ────────────────────────────────────────────
        self._main_loop()

    def _handle_shutdown(self, sig, frame) -> None:
        log.info(f"Shutdown signal received ({sig})")
        self._shutdown.set()
        self.scanner.stop()
        self.dashboard.stop()

        # Cancel live orders on clean shutdown
        if not config.DRY_RUN:
            log.info("Cancelling open orders before exit...")
            cancelled = self.executor.cancel_all_open_orders()
            log.info(f"Cancelled {cancelled} orders")

        log_event("BOT_STOPPED", {
            "reason": f"signal_{sig}",
            "session_pnl": self.risk.get_status()["session_pnl"],
            "total_trades": self.risk.get_status()["total_trades"],
        })

        # Print final summary if no dashboard
        if not RICH_AVAILABLE:
            print_status(self.risk, self.portfolio)

        sys.exit(0)

    def _print_startup_banner(self) -> None:
        mode = "LIVE TRADING" if not config.DRY_RUN else "DRY RUN (paper trading)"
        kill_note = "Set LIVE_TRADING=1 to enable real orders" if config.DRY_RUN else "WARNING: REAL MONEY MODE"

        print(f"""
{'='*65}
  POLYMARKET AI BOT
  Mode:         {mode}
  {kill_note}
  Capital cap:  ${config.MAX_CAPITAL_USD:.2f}
  Daily loss:   ${config.DAILY_LOSS_CAP_USD:.2f} cap
  Scan interval:{config.SCAN_INTERVAL_SECONDS}s
  AI model:     {config.PRIMARY_MODEL}
  Logs:         {config.LOG_DIR}
  Started:      {datetime.now(timezone.utc).isoformat()}
{'='*65}

Press Ctrl+C to stop cleanly.
""")


# ─── CLI ─────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Polymarket AI Bot — Python trading bot with local LLM intelligence"
    )
    parser.add_argument(
        "--no-ai",
        action="store_true",
        help="Skip AI analysis (useful for testing edge detection without Ollama)",
    )
    parser.add_argument(
        "--scan-once",
        action="store_true",
        help="Run a single scan cycle and print results, then exit (diagnostic mode)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check credentials, Ollama health, and API connectivity, then exit",
    )
    args = parser.parse_args()

    # ── Check mode ────────────────────────────────────────────────────────
    if args.check:
        _run_health_check()
        return

    # ── Scan-once mode ────────────────────────────────────────────────────
    if args.scan_once:
        _run_scan_once()
        return

    # ── Full bot ─────────────────────────────────────────────────────────
    bot = PolymarketBot(skip_ai=args.no_ai)
    bot.start()


def _run_health_check() -> None:
    """Quick connectivity and credential check."""
    from utils.polymarket_client import GammaClient
    from ai_analyst import AIAnalyst

    print("\n--- Health Check ---\n")

    # Config loaded
    print(f"[OK] Config loaded")
    print(f"     Mode: {'DRY RUN' if config.DRY_RUN else 'LIVE TRADING'}")
    print(f"     Proxy: {config.PROXY_ADDRESS[:12]}...")
    print(f"     API Key: {config.API_KEY[:12]}...")

    # Gamma API
    gamma = GammaClient()
    markets = gamma.get_markets(limit=5)
    if markets:
        print(f"[OK] Gamma API reachable — {len(markets)} markets returned")
    else:
        print("[FAIL] Gamma API unreachable or returned empty")

    # Ollama
    analyst = AIAnalyst()
    health = analyst.check_ollama_health()
    primary_status = "OK" if health["primary_available"] else "DOWN"
    fallback_status = "OK" if health["fallback_available"] else "DOWN"
    print(f"[{primary_status}] Ollama primary: {health['primary_model']}")
    print(f"[{fallback_status}] Ollama fallback: {health['fallback_model']}")

    # CLOB (auth only in live mode or if explicitly checked)
    if not config.DRY_RUN:
        try:
            from utils.polymarket_client import ClobClient
            clob = ClobClient()
            if clob.verify_auth():
                print("[OK] CLOB authentication verified")
            else:
                print("[FAIL] CLOB auth failed")
        except Exception as e:
            print(f"[FAIL] CLOB init error: {e}")
    else:
        print("[SKIP] CLOB auth (dry run mode — set LIVE_TRADING=1 to test)")

    print("\n--- Health Check Complete ---\n")


def _run_scan_once() -> None:
    """Run a single scan and print opportunities without executing."""
    from scanner import MarketScanner, MarketCandidate
    from edge_detector import EdgeDetector, EdgeType

    print("\n--- Single Scan Mode ---\n")

    all_candidates: list[MarketCandidate] = []
    scanner = MarketScanner(on_candidate=lambda m: None)  # suppress auto-callback

    # Collect ALL candidates (not just binary arb)
    all_markets = scanner.scan_once()
    # scan_once returns only markets that passed filter — they are all candidates
    # Re-run without the has_edge filter by collecting from raw scan logic
    from utils.polymarket_client import GammaClient
    g = GammaClient()
    raw = g.get_markets(limit=config.MAX_MARKETS_PER_SCAN, active=True, closed=False)

    all_candidates = []
    for market in raw:
        if not scanner._passes_filter(market):
            continue
        prices   = scanner._extract_prices(market)
        tids     = scanner._extract_token_ids(market)
        if not prices or not tids:
            continue
        yes_p, no_p = prices
        yes_id, no_id = tids
        yes_ask, no_ask = scanner._get_clob_asks(yes_id, no_id)
        if yes_ask and no_ask:
            yes_p, no_p = yes_ask, no_ask

        from scanner import MarketCandidate
        c = MarketCandidate(
            market_id=market.get("conditionId") or market.get("id", ""),
            question=market.get("question", ""),
            description=market.get("description", ""),
            resolution=market.get("resolutionSource", ""),
            end_date=market.get("endDate", ""),
            category=market.get("groupItemTitle", ""),
            volume_24h=float(market.get("volume24hr", 0) or 0),
            yes_token_id=yes_id,
            no_token_id=no_id,
            yes_price=yes_p,
            no_price=no_p,
            price_sum=yes_p + no_p,
        )
        all_candidates.append(c)

    detector = EdgeDetector()
    analyses = [detector.analyze(m) for m in all_candidates]
    actionable = [a for a in analyses if a.is_actionable]
    actionable.sort(key=lambda x: (x.edge_type == EdgeType.BINARY_ARB, x.edge_magnitude), reverse=True)

    print(f"Scanned {len(raw)} markets | {len(all_candidates)} passed filter | {len(actionable)} actionable\n")

    if not actionable:
        print("No actionable opportunities found. Market is efficient — AI directional analysis required.")
        print("Run bot.py (without --scan-once) to enable AI analysis via Ollama.\n")
        return

    print(f"{'Type':<12} {'Question':<52} {'Edge':>6} {'YES':>5} {'NO':>5} {'Conf':>5}")
    print("-" * 90)
    for a in actionable[:15]:
        print(
            f"{a.edge_type.value:<12} {a.question[:52]:<52} "
            f"{a.edge_magnitude:>6.4f} {a.yes_price:>5.3f} {a.no_price:>5.3f} {a.confidence:>5.2f}"
        )

    print("\n--- Scan Complete ---\n")


if __name__ == "__main__":
    main()
