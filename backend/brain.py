"""RAID brain — Claude validation for gray-zone signals plus the weekly learning loop."""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from anthropic import AsyncAnthropic

import config
from signals import Signal

log = logging.getLogger("raid.brain")

# Single async Anthropic client, created once.
_client = AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)

# Daily AI spend tracking (reset at UTC midnight by the worker / lazily here).
_daily_spend = 0.0
_last_reset_date = ""


@dataclass
class BrainResult:
    """Claude's verdict on a signal, with the cost it took to produce."""

    decision: str  # ENTER or SKIP
    confidence: float
    reasoning: str
    cost_usd: float
    skipped_budget: bool = False


def reset_daily_spend():
    """Reset the daily AI spend counter and stamp today's date."""
    global _daily_spend, _last_reset_date
    _daily_spend = 0.0
    _last_reset_date = datetime.now(timezone.utc).date().isoformat()


def get_daily_spend():
    """Return the AI spend accumulated so far today."""
    return _daily_spend


def _parse_response(text: str, default_conf: float):
    """Parse DECISION/CONFIDENCE/REASONING out of Claude's formatted reply."""
    decision = "SKIP"
    confidence = default_conf
    reasoning = ""
    for line in text.splitlines():
        line = line.strip()
        if line.upper().startswith("DECISION:"):
            value = line.split(":", 1)[1].strip().upper()
            decision = "ENTER" if "ENTER" in value else "SKIP"
        elif line.upper().startswith("CONFIDENCE:"):
            match = re.search(r"[0-9]*\.?[0-9]+", line.split(":", 1)[1])
            if match:
                try:
                    confidence = max(0.0, min(float(match.group()), 1.0))
                except ValueError:
                    pass
        elif line.upper().startswith("REASONING:"):
            reasoning = line.split(":", 1)[1].strip()
    return decision, confidence, reasoning


async def validate_signal(signal: Signal, db, portfolio_summary: dict):
    """Validate a gray-zone signal with Claude; bypass for high confidence or spent budget."""
    global _daily_spend

    today = datetime.now(timezone.utc).date().isoformat()
    if today != _last_reset_date:
        reset_daily_spend()

    # High confidence — skip Claude, enter directly.
    if signal.confidence >= config.CLAUDE_SKIP_THRESHOLD:
        return BrainResult("ENTER", signal.confidence, "High confidence — skipped AI validation", 0.0)

    # Outside the gray zone — not Claude's job.
    if signal.confidence < config.CLAUDE_GRAY_ZONE_MIN or signal.confidence > config.CLAUDE_GRAY_ZONE_MAX:
        return BrainResult("SKIP", signal.confidence, "Outside gray zone", 0.0)

    # Budget exhausted — fall back to the technical score only.
    if _daily_spend >= config.CLAUDE_BUDGET_DAILY:
        decision = "ENTER" if signal.technical_score > config.BUDGET_TECH_THRESHOLD else "SKIP"
        return BrainResult(
            decision,
            signal.confidence,
            "Budget exhausted — technical only",
            0.0,
            skipped_budget=True,
        )

    prompt = f"""
You are RAID, an aggressive AI trading bot. Analyze this signal and decide ENTER or SKIP.

SIGNAL:
Market: {signal.market}
Symbol: {signal.symbol}
Direction: {signal.direction}
Confidence: {signal.confidence:.0%}
Technical Score: {signal.technical_score:.0f}/100
News: {signal.news_headline} (sentiment: {signal.news_sentiment})
News Adjustment: {signal.news_boost:+.0%}

PORTFOLIO:
Open trades: {portfolio_summary['open_count']}
Today's PnL: ${portfolio_summary['daily_pnl']:.2f}
Today's win rate: {portfolio_summary['win_rate']:.0%}
Consecutive losses: {portfolio_summary['consecutive_losses']}

MACRO STATUS: {portfolio_summary['macro_status']}

Rules: Enter if signal is technically valid, news is not strongly opposing, and portfolio is not overexposed. Skip if signal is noise, news strongly conflicts, or portfolio is at risk.

Respond in exactly this format:
DECISION: ENTER or SKIP
CONFIDENCE: 0.XX
REASONING: one sentence max
""".strip()

    try:
        resp = await _client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=config.CLAUDE_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(block.text for block in resp.content if getattr(block, "type", "") == "text")
        prompt_tokens = resp.usage.input_tokens
        response_tokens = resp.usage.output_tokens
        cost = (
            prompt_tokens * config.CLAUDE_INPUT_COST_PER_TOKEN
            + response_tokens * config.CLAUDE_OUTPUT_COST_PER_TOKEN
        )
        _daily_spend += cost

        decision, confidence, reasoning = _parse_response(text, signal.confidence)

        signal_id = getattr(signal, "_signal_id", None)
        await db.log_brain_decision(
            {
                "signal_id": signal_id,
                "prompt_tokens": prompt_tokens,
                "response_tokens": response_tokens,
                "cost_usd": cost,
                "decision": decision,
                "reasoning": reasoning,
            }
        )
        return BrainResult(decision, confidence, reasoning, cost)
    except Exception as exc:  # noqa: BLE001
        log.error("validate_signal Claude call failed: %s", exc)
        return BrainResult("SKIP", signal.confidence, f"AI error: {exc}", 0.0)


async def run_weekly_learning(db):
    """Review the last week's closed trades and log win-rate-based weight adjustments."""
    try:
        trades = await db.get_trades_for_learning(config.LEARNING_INTERVAL_DAYS)
        groups = {}
        for t in trades:
            key = (t.get("market", "unknown"), t.get("direction", "unknown"))
            groups.setdefault(key, []).append(t)

        adjustments = 0
        for (market, signal_type), group in groups.items():
            total = len(group)
            if total < config.LEARNING_MIN_SAMPLE:
                continue
            wins = sum(1 for t in group if (t.get("pnl") or 0) > 0)
            win_rate = wins / total

            if win_rate < config.LEARNING_LOW_WIN_RATE:
                await db.log_learning_adjustment(
                    {
                        "market": market,
                        "signal_type": signal_type,
                        "old_weight": 1.0,
                        "new_weight": config.LEARNING_WEIGHT_DOWN,
                        "win_rate": win_rate,
                        "sample_size": total,
                    }
                )
                adjustments += 1
            elif win_rate > config.LEARNING_HIGH_WIN_RATE:
                await db.log_learning_adjustment(
                    {
                        "market": market,
                        "signal_type": signal_type,
                        "old_weight": 1.0,
                        "new_weight": config.LEARNING_WEIGHT_UP,
                        "win_rate": win_rate,
                        "sample_size": total,
                    }
                )
                adjustments += 1

        log.info(
            "RAID LEARNING COMPLETE — groups=%d adjustments=%d trades=%d",
            len(groups),
            adjustments,
            len(trades),
        )
    except Exception as exc:  # noqa: BLE001
        log.error("run_weekly_learning failed: %s", exc)
