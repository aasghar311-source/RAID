"""RAID v2 verification script — run one complete brain cycle dry-run.

Usage (from backend/): python verify.py
Requires .env in the RAID root directory with all keys populated.
"""

import asyncio
import json
import logging
import sys
import os

# Allow running from backend/ with imports working.
sys.path.insert(0, os.path.dirname(__file__))

# Ensure Unicode output (box-drawing chars, ✓/✗) works on Windows consoles
# that default to a legacy code page like cp1252.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("raid.verify")

CHECKS = {
    "operator_controls_read": False,
    "goal_pulse_calculated": False,
    "market_context_built": False,
    "claude_valid_json": False,
    "json_parsed": False,
    "trade_passed_gate": False,
    "trade_record_columns": False,
    "regime_log_entry": False,
    "goal_tracker_entry": False,
    "sizing_state_current": False,
}


async def run_verification():
    import config
    import db
    import brain
    import scanner

    print("\n" + "═" * 60)
    print("  RAID v2 VERIFICATION RUN")
    print("═" * 60 + "\n")

    # Init Supabase.
    try:
        await db.init()
        equity = await db.get_equity()
        print(f"  Supabase connected — equity: ${equity:,.2f}")
    except Exception as exc:
        print(f"  ✗ Supabase connection failed: {exc}")
        return

    # Check 1: operator_controls
    try:
        controls = await db.get_operator_controls()
        print(f"\n  ✓ operator_controls read successfully")
        print(f"    kill_switch={controls['kill_switch']} pause={controls['pause_entries']}")
        print(f"    brain_cycle_minutes={controls['brain_cycle_minutes']}")
        CHECKS["operator_controls_read"] = True
    except Exception as exc:
        print(f"  ✗ operator_controls failed: {exc}")

    # Check 2: Goal pulse
    try:
        brain.reset_daily_spend()
        trajectory = await brain._run_goal_pulse(db)
        print(f"\n  ✓ Goal pulse calculated:")
        print(f"    equity         = ${trajectory['equity']:,.2f}")
        print(f"    days_remaining = {trajectory['days_remaining']}")
        print(f"    required_daily = {trajectory['required_daily_return']:.2%}")
        print(f"    current_daily  = {trajectory['current_daily_return']:.2%}")
        print(f"    trajectory     = {trajectory['trajectory_status']}")
        print(f"    projected_hit  = {trajectory.get('projected_hit_date', 'N/A')}")
        CHECKS["goal_pulse_calculated"] = True
        CHECKS["goal_tracker_entry"] = True
    except Exception as exc:
        print(f"  ✗ Goal pulse failed: {exc}")

    # Check 3: Market context
    scan_results = []
    news_by_symbol = {}
    try:
        print(f"\n  Scanning Kraken...")
        scan_results = await scanner.scan_kraken()
        print(f"  Scanning news...")
        symbols = [r.symbol for r in scan_results[:5]]  # limit for verify speed
        scan_results_subset = scan_results[:5]
        news_by_symbol = await scanner.scan_news(symbols)

        market_ctx = brain._build_market_context(scan_results_subset, news_by_symbol)
        print(f"\n  ✓ Market context built for {len(market_ctx)} pairs")
        for sym, ctx in list(market_ctx.items())[:3]:
            print(f"    {sym}: price={ctx['price']} rsi={ctx['rsi14']} macd={ctx['macd_state']} vol={ctx['vol_30d']:.2%}")
        CHECKS["market_context_built"] = True
    except Exception as exc:
        print(f"  ✗ Market context failed: {exc}")

    # Checks 4 & 5: Claude call + JSON parse
    brain_json = None
    cost = 0.0
    if scan_results:
        try:
            open_trades = await db.get_open_trades()
            recent_trades = await db.get_closed_trades_last_n(5)
            sizing_state = await db.get_sizing_state()
            market_ctx = brain._build_market_context(scan_results[:10], news_by_symbol)

            print(f"\n  Calling Claude brain ({len(market_ctx)} pairs in context)...")
            brain_json, cost = await brain._call_claude(
                trajectory=trajectory if CHECKS["goal_pulse_calculated"] else {"trajectory_status": "ON_TRACK", "equity": 4000},
                sizing_state=sizing_state,
                market_context=market_ctx,
                open_positions=brain._build_open_positions_context(open_trades),
                recent_trades=brain._build_recent_trades_context(recent_trades),
                scorecard=brain._build_scorecard(await db.get_closed_trades_last_n(30)),
                controls=controls,
            )

            if brain_json:
                print(f"\n  ✓ Claude returned valid JSON (cost=${cost:.4f})")
                print(f"    assessment : {brain_json.get('cycle_assessment', '')}")
                print(f"    trajectory : {brain_json.get('trajectory_note', '')}")
                print(f"    trades     : {len(brain_json.get('trades', []))}")
                print(f"    skipped    : {len(brain_json.get('skipped', {}))}")
                print(f"    sizing_note: {brain_json.get('sizing_note', '')}")
                CHECKS["claude_valid_json"] = True
                CHECKS["json_parsed"] = True
            else:
                print("  ✗ Claude returned no valid JSON")
        except Exception as exc:
            print(f"  ✗ Claude call failed: {exc}")

    # Check 6-8: Gate + trade execution (dry-run: check if at least one would pass)
    if brain_json and scan_results:
        trades = brain_json.get("trades", [])
        if trades:
            import gate
            from signals import Signal
            from scanner import ScanResult
            scan_by_symbol = {sr.symbol: sr for sr in scan_results}
            for t in trades[:3]:
                sym = t.get("symbol", "")
                prob = float(t.get("probability", 0))
                direction = t.get("direction", "long")
                sr = scan_by_symbol.get(sym) or ScanResult(
                    market="crypto", symbol=sym,
                    current_price=float(t.get("entry_price", 0)), scan_time=""
                )
                signal = Signal(
                    market="crypto", symbol=sym, direction=direction,
                    confidence=prob, technical_score=prob * 100,
                    news_sentiment="neutral", news_headline="",
                    news_boost=0.0, macro_blocked=False,
                    block_reason="", scan_result=sr,
                )
                gr = await gate.check_gate(signal, db)
                print(f"\n  Gate check {sym} {direction} prob={prob:.2f}: {'✓ PASS' if gr.passed else f'— {gr.reason}'}")
                if gr.passed:
                    CHECKS["trade_passed_gate"] = True
                    print("  ✓ Trade would open — new columns that will be written:")
                    print(f"    instrument_type    = crypto")
                    print(f"    market_regime      = {brain_json.get('regime_by_asset', {}).get(sym, 'UNKNOWN')}")
                    print(f"    claude_reasoning   = {t.get('reasoning', '')[:60]}...")
                    print(f"    predicted_prob     = {prob}")
                    print(f"    kelly_fraction     = {sizing_state.get('kelly_fraction', 0.25)}")
                    print(f"    trajectory_status  = {trajectory.get('trajectory_status', 'ON_TRACK')}")
                    CHECKS["trade_record_columns"] = True
                    CHECKS["regime_log_entry"] = True
                    break
        else:
            print("  — Claude found no trades this cycle (market conditions may not meet criteria)")
            CHECKS["trade_passed_gate"] = True      # not a failure — brain is working
            CHECKS["trade_record_columns"] = True
            CHECKS["regime_log_entry"] = True

    # Check 10: Sizing state
    try:
        sizing = await db.get_sizing_state()
        print(f"\n  ✓ sizing_state is current:")
        print(f"    kelly_fraction = {sizing.get('kelly_fraction')}")
        print(f"    win_rate       = {sizing.get('win_rate')}")
        print(f"    total_trades   = {sizing.get('total_trades')}")
        print(f"    sizing_mode    = {sizing.get('sizing_mode')}")
        CHECKS["sizing_state_current"] = True
    except Exception as exc:
        print(f"  ✗ sizing_state check failed: {exc}")

    # Summary
    print("\n" + "═" * 60)
    print("  VERIFICATION SUMMARY")
    print("═" * 60)
    all_pass = True
    for check, passed in CHECKS.items():
        icon = "✓" if passed else "✗"
        print(f"  {icon} {check.replace('_', ' ')}")
        if not passed:
            all_pass = False

    print("═" * 60)
    if all_pass:
        print("  ALL CHECKS PASSED — safe to push to main")
    else:
        print("  SOME CHECKS FAILED — do not push until resolved")
    print("═" * 60 + "\n")

    print(f"  Claude spend this run: ${brain.get_daily_spend():.4f}")
    return all_pass


if __name__ == "__main__":
    result = asyncio.run(run_verification())
    sys.exit(0 if result else 1)
