"""
ai_analyst.py — AI Market Analysis via Claude Haiku

This is where the bot gets smart. Instead of blind math, we reason about:
- Is this edge real or a data artifact?
- Is the implied probability reasonable for this type of event?
- What are the hidden resolution risks?
- Is there directional alpha beyond just the arb?

Uses Claude Haiku API (~$0.01-0.03/day at typical scan rates).
Fallback: Ollama local models if API key is missing.

Design: every analysis call is cached by market_id with a TTL so we don't
hammer the API with redundant analysis on every scan cycle.
"""

from __future__ import annotations

import json
import time
import hashlib
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import requests

import config
from models.prompts import MARKET_EDGE_ANALYSIS, MARKET_CONTEXT_PROMPT
from utils.logger import get_logger, log_event

if TYPE_CHECKING:
    from scanner import MarketCandidate
    from edge_detector import EdgeAnalysis

log = get_logger("ai_analyst")


@dataclass
class AISignal:
    """Result of AI market analysis."""
    market_id: str
    verdict: str           # "BUY_YES" | "BUY_NO" | "BUY_BOTH" | "SKIP"
    confidence: float      # 0.0 – 1.0
    reasoning: str
    implied_fair_yes: float | None
    implied_fair_no: float | None
    risk_flags: list[str]
    resolution_risk: str   # "LOW" | "MEDIUM" | "HIGH"
    model_used: str
    latency_sec: float
    raw_response: str = ""

    @property
    def is_actionable(self) -> bool:
        return (
            self.verdict != "SKIP"
            and self.confidence >= config.AI_CONFIDENCE_MIN
            and self.resolution_risk != "HIGH"
        )

    def as_dict(self) -> dict:
        return {
            "market_id": self.market_id,
            "verdict": self.verdict,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "implied_fair_yes": self.implied_fair_yes,
            "implied_fair_no": self.implied_fair_no,
            "risk_flags": self.risk_flags,
            "resolution_risk": self.resolution_risk,
            "model_used": self.model_used,
            "latency_sec": round(self.latency_sec, 2),
            "actionable": self.is_actionable,
        }


class HaikuClient:
    """Claude Haiku API client for market analysis."""

    MODEL = "claude-haiku-4-5-20251001"

    def __init__(self) -> None:
        self._api_key = config.ANTHROPIC_API_KEY
        self._client = None

    def _get_client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(api_key=self._api_key)
        return self._client

    def generate(self, prompt: str, timeout: float = 30.0) -> str | None:
        """Send a prompt to Claude Haiku, return the response text."""
        try:
            client = self._get_client()
            message = client.messages.create(
                model=self.MODEL,
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
            )
            return message.content[0].text.strip()
        except Exception as e:
            log.error(f"Claude Haiku error: {e}")
            return None

    def is_available(self) -> bool:
        """Check if API key is configured and valid."""
        if not self._api_key:
            return False
        try:
            client = self._get_client()
            # Minimal call to verify key
            resp = client.messages.create(
                model=self.MODEL,
                max_tokens=5,
                messages=[{"role": "user", "content": "ping"}],
            )
            return True
        except Exception as e:
            log.warning(f"Claude Haiku health check failed: {e}")
            return False


def _extract_json(text: str) -> dict | None:
    """
    Extract JSON from LLM output.
    Handles: raw JSON, JSON in code blocks, JSON with surrounding text.
    """
    if not text:
        return None

    # Try direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try extracting from code block
    import re
    patterns = [
        r"```json\s*([\s\S]*?)\s*```",
        r"```\s*([\s\S]*?)\s*```",
        r"\{[\s\S]*\}",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            candidate = match.group(1) if "```" in pattern else match.group(0)
            try:
                return json.loads(candidate.strip())
            except json.JSONDecodeError:
                continue

    return None


class AIAnalyst:
    """
    Orchestrates AI analysis of market opportunities via Claude Haiku.

    Flow:
    1. Check cache (TTL = AI_ANALYSIS_COOLDOWN seconds)
    2. Build prompt from MarketCandidate + EdgeAnalysis
    3. Call Claude Haiku API
    4. Parse JSON response into AISignal
    5. Cache result
    6. Log to event log
    """

    def __init__(self) -> None:
        self.haiku = HaikuClient()
        # Cache: market_id → (timestamp, AISignal)
        self._cache: dict[str, tuple[float, AISignal]] = {}

    def _is_cached(self, market_id: str) -> AISignal | None:
        entry = self._cache.get(market_id)
        if not entry:
            return None
        ts, signal = entry
        if time.time() - ts < config.AI_ANALYSIS_COOLDOWN:
            log.debug(f"Cache hit for {market_id}")
            return signal
        return None

    def _cache_result(self, market_id: str, signal: AISignal) -> None:
        self._cache[market_id] = (time.time(), signal)
        # Evict old entries if cache grows large
        if len(self._cache) > 500:
            oldest = sorted(self._cache.items(), key=lambda x: x[1][0])[:100]
            for k, _ in oldest:
                del self._cache[k]

    def _build_prompt(
        self,
        market: "MarketCandidate",
        edge: "EdgeAnalysis",
    ) -> str:
        return MARKET_EDGE_ANALYSIS.format(
            question=market.question,
            description=market.description or "No description provided.",
            resolution=market.resolution or "Standard resolution criteria.",
            end_date=market.end_date or "Unknown",
            yes_price=market.yes_price,
            no_price=market.no_price,
            price_sum=market.price_sum,
            yes_pct=market.yes_price * 100,
            no_pct=market.no_price * 100,
        )

    def _parse_response(
        self,
        raw: str,
        market_id: str,
        model: str,
        latency: float,
    ) -> AISignal:
        """Parse LLM JSON response into AISignal. Falls back to SKIP on parse failure."""
        parsed = _extract_json(raw)

        if not parsed:
            log.warning(f"Could not parse AI response for {market_id}: {raw[:200]}")
            return AISignal(
                market_id=market_id,
                verdict="SKIP",
                confidence=0.0,
                reasoning="Failed to parse AI response.",
                implied_fair_yes=None,
                implied_fair_no=None,
                risk_flags=["parse_failure"],
                resolution_risk="HIGH",
                model_used=model,
                latency_sec=latency,
                raw_response=raw[:500],
            )

        # Validate and normalize fields
        verdict = parsed.get("verdict", "SKIP")
        if verdict not in {"BUY_YES", "BUY_NO", "BUY_BOTH", "SKIP"}:
            verdict = "SKIP"

        confidence = float(parsed.get("confidence", 0.0))
        confidence = max(0.0, min(1.0, confidence))

        resolution_risk = parsed.get("resolution_risk", "HIGH")
        if resolution_risk not in {"LOW", "MEDIUM", "HIGH"}:
            resolution_risk = "HIGH"

        return AISignal(
            market_id=market_id,
            verdict=verdict,
            confidence=confidence,
            reasoning=str(parsed.get("reasoning", ""))[:500],
            implied_fair_yes=parsed.get("implied_fair_yes"),
            implied_fair_no=parsed.get("implied_fair_no"),
            risk_flags=parsed.get("risk_flags", []),
            resolution_risk=resolution_risk,
            model_used=model,
            latency_sec=latency,
            raw_response=raw[:500],
        )

    def analyze(
        self,
        market: "MarketCandidate",
        edge: "EdgeAnalysis",
    ) -> AISignal:
        """
        Analyze a market with local LLM. Returns AISignal.
        Never raises — always returns a valid (possibly SKIP) signal.
        """
        # Cache check
        cached = self._is_cached(market.market_id)
        if cached:
            return cached

        prompt = self._build_prompt(market, edge)
        model_used = HaikuClient.MODEL

        t0 = time.time()
        raw = self.haiku.generate(prompt)
        latency = time.time() - t0

        if raw is None:
            log.error("Claude Haiku unavailable — returning SKIP signal")
            signal = AISignal(
                market_id=market.market_id,
                verdict="SKIP",
                confidence=0.0,
                reasoning="Claude Haiku API unavailable.",
                implied_fair_yes=None,
                implied_fair_no=None,
                risk_flags=["api_unavailable"],
                resolution_risk="HIGH",
                model_used="none",
                latency_sec=latency,
            )
        else:
            signal = self._parse_response(raw, market.market_id, model_used, latency)

        self._cache_result(market.market_id, signal)

        log_event("AI_SIGNAL", {
            "market_id": market.market_id,
            "question": market.question[:100],
            "verdict": signal.verdict,
            "confidence": signal.confidence,
            "resolution_risk": signal.resolution_risk,
            "model": model_used,
            "latency_sec": round(latency, 2),
        })

        log.info(
            f"AI [{model_used}] {market.question[:60]}... → "
            f"{signal.verdict} (conf={signal.confidence:.2f}, "
            f"risk={signal.resolution_risk}, {latency:.1f}s)"
        )

        return signal

    def check_ollama_health(self) -> dict:
        """Return health status for dashboard display. Kept name for compatibility."""
        haiku_ok = self.haiku.is_available()
        return {
            "ollama_url": "Claude API",
            "primary_model": HaikuClient.MODEL,
            "primary_available": haiku_ok,
            "fallback_model": "none",
            "fallback_available": False,
            "cache_size": len(self._cache),
        }
