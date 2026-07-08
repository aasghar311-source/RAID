"""Appendix-C §16 A+ component scoring (PROPOSED DESIGN — shadow/measure-first).

Grades a live candidate 0-100 across the seven plan-D2 components, then maps the score to a QUALITY
leverage cap. The live leverage decision becomes MIN(quality cap, tier ceiling, Kraken pair cap) —
leverage scales with SETUP QUALITY, capped by the pair; never the pair's max cap alone.

Bands (operator, full ladder):
    score < 80        -> REJECT (no trade)
    80 <= score < 88  -> ordinary  -> 1.5x
    88 <= score < 94  -> A+        -> 2.25x
    score >= 94       -> A++       -> 3.0x

Weights are an INITIAL DESIGN to be calibrated against the live score distribution before enforcing
(plan D2: "validated against stored distributions, not blind-copied"). Pure + testable; no I/O.
Every component returns (subscore_0_100, reason) so the decision is fully explainable in the log.
"""

from __future__ import annotations

from dataclasses import dataclass

# ── Operator band ladder ────────────────────────────────────────────────────
SCORE_REJECT_BELOW = 80.0     # < this -> no trade (ordinary floor)
SCORE_APLUS = 88.0            # >= this -> A+  (2.25x)
SCORE_APLUSPLUS = 94.0        # >= this -> A++ (3.0x)
LEV_ORDINARY, LEV_APLUS, LEV_APLUSPLUS = 1.5, 2.25, 3.0

# ── Component weights (sum 100). DESIGN — calibrate vs live distribution before enforce. ──
WEIGHTS = {
    "market_state": 20,     # spine direction + portfolio conviction behind the trade
    "mtf_direction": 20,    # 15m/30m/1h EMA alignment with the trade direction
    "post_cost_econ": 15,   # net_rr headroom above the 1.20 cost hurdle
    "structure": 15,        # 5m EMA stacking in the trade direction
    "volume_book": 15,      # §10 completed-bar volume ratio + spread tightness
    "cross_sectional": 10,  # universe rank (leader for long / laggard for short)
    "execution": 5,         # spread fillability
}


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _lin(x, x0, x1):
    """Linear 0..100 as x goes x0->x1 (clamped). x1 may be < x0 (descending)."""
    if x is None or x1 == x0:
        return None
    return _clamp((x - x0) / (x1 - x0) * 100.0)


def quality_leverage(score: float) -> float:
    """Score -> quality leverage CAP. 0.0 = REJECT (below the ordinary floor)."""
    if score < SCORE_REJECT_BELOW:
        return 0.0
    if score >= SCORE_APLUSPLUS:
        return LEV_APLUSPLUS
    if score >= SCORE_APLUS:
        return LEV_APLUS
    return LEV_ORDINARY


def band_label(score: float) -> str:
    if score < SCORE_REJECT_BELOW:
        return "REJECT"
    if score >= SCORE_APLUSPLUS:
        return "A++"
    if score >= SCORE_APLUS:
        return "A+"
    return "ordinary"


@dataclass
class ScoreResult:
    score: float
    band: str
    quality_lev: float          # quality cap (0.0 = reject)
    components: dict             # {name: (subscore, reason)}

    def log_str(self) -> str:
        parts = " ".join(f"{k}={self.components[k][0]:.0f}" for k in WEIGHTS)
        return (f"SCORE={self.score:.1f} band={self.band} qual_lev="
                f"{'REJECT' if self.quality_lev == 0 else f'{self.quality_lev:.2f}x'} [{parts}]")


# ── Components (each -> (subscore_0_100, reason)) ────────────────────────────
def _market_state(ctx, d):
    sd = ctx.extras.get("spine_dir")
    pf = ctx.extras.get("spine_portfolio")
    if sd != d.upper():                       # strategies spine-gate, so this should already match
        return 20.0, f"spine {sd}!={d}"
    base = 60.0
    if (pf == "RISK_ON" and d == "long") or (pf == "RISK_OFF" and d == "short"):
        base += 40.0                          # portfolio conviction fully behind the trade
    elif pf == "MIXED":
        base += 20.0
    return _clamp(base), f"spine={sd} pf={pf}"


def _mtf_direction(ctx, d):
    aligned = avail = 0
    for tf in ("15m", "30m", "1h"):
        f = ctx.feature(tf)
        if f is None or f.ema20 is None or f.ema50 is None:
            continue
        avail += 1
        up = f.ema20 > f.ema50
        if (d == "long" and up) or (d == "short" and not up):
            aligned += 1
    if avail == 0:
        return 50.0, "no HTF data"
    return 100.0 * aligned / avail, f"{aligned}/{avail} HTF aligned"


def _post_cost_econ(candidate):
    rr = float(candidate.net_rr)
    return _lin(rr, 1.20, 2.50) or 0.0, f"net_rr={rr:.3f}"   # 1.20 floor->0, 2.50->100


def _structure(ctx, d):
    f = ctx.feature("5m")
    if f is None or f.ema20 is None or f.ema50 is None or f.ema200 is None:
        return 50.0, "no 5m emas"
    if d == "long":
        stacked = int(f.ema20 > f.ema50) + int(f.ema50 > f.ema200)
    else:
        stacked = int(f.ema20 < f.ema50) + int(f.ema50 < f.ema200)
    return 50.0 * stacked, f"emastack={stacked}/2"          # 0 / 50 / 100


def _volume_book(ctx):
    vrc = ctx.extras.get("vol_ratio_completed")
    vol_s = _lin(vrc, 0.5, 1.5)                              # vr 0.5->0, 1.5->100
    sp = ctx.spread_pct
    sp_s = _lin(sp, 0.0025, 0.0003)                         # 0.25%->0, 0.03%->100
    vol_s = 30.0 if vol_s is None else vol_s
    sp_s = 30.0 if sp_s is None else sp_s
    return _clamp(0.6 * vol_s + 0.4 * sp_s), f"vrc={vrc} spread={sp}"


def _cross_sectional(ctx, d):
    r = (ctx.extras.get("universe_rankings") or {}).get(ctx.symbol)
    if not r or (r.get("n") or 0) < 5:
        return 50.0, "no/thin rank"
    rank, n = r["rank"], r["n"]
    pct = (n - rank) / (n - 1) if d == "long" else (rank - 1) / (n - 1)   # laggard best for shorts
    return _clamp(pct * 100.0), f"rank={rank}/{n}"


def _execution(ctx):
    sp_s = _lin(ctx.spread_pct, 0.0025, 0.0003)
    return (30.0 if sp_s is None else sp_s), f"spread={ctx.spread_pct}"


def score_candidate(ctx, candidate) -> ScoreResult:
    """Compute the 0-100 A+ component score + the quality leverage cap for a live candidate."""
    d = candidate.direction.value
    comps = {
        "market_state": _market_state(ctx, d),
        "mtf_direction": _mtf_direction(ctx, d),
        "post_cost_econ": _post_cost_econ(candidate),
        "structure": _structure(ctx, d),
        "volume_book": _volume_book(ctx),
        "cross_sectional": _cross_sectional(ctx, d),
        "execution": _execution(ctx),
    }
    score = _clamp(sum(WEIGHTS[k] * comps[k][0] for k in WEIGHTS) / sum(WEIGHTS.values()))
    return ScoreResult(score, band_label(score), quality_leverage(score), comps)
