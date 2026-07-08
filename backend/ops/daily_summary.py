"""RAID once-daily summary — READ-ONLY.

A human-readable readout of the live paper window so you don't have to read raw logs. Queries only
(SELECT) — it changes NO trading behavior, NO config, NO thresholds. Every section is wrapped so one
failing query never blocks the rest.

Run:  railway run python backend/ops/daily_summary.py     (uses the prod Supabase env)
  or: python backend/ops/daily_summary.py                 (with SUPABASE_URL / SUPABASE_KEY set)
"""

import asyncio
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))  # put backend/ on the path

import config                                    # noqa: E402
import db                                         # noqa: E402
from db import _parse_strategy_tag               # noqa: E402
from raid.core import tiers                       # noqa: E402
from raid.core.universe import kraken_max_leverage  # noqa: E402

WORKING_SET = ["RAID-C1", "RAID-C2", "RAID-C3"]   # the live-paper strategies


async def _latest(table, order_col):
    r = await db.supabase.table(table).select("*").order(order_col, desc=True).limit(1).execute()
    return (r.data or [None])[0]


async def main():
    await db.init()
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=24)).isoformat()

    print("=" * 66)
    print(f"  RAID DAILY SUMMARY — {now.strftime('%Y-%m-%d %H:%M UTC')}   (read-only)")
    print("=" * 66)

    # Equity vs the starting base
    try:
        eq = float(await db.get_realized_equity())
        base = float(config.STARTING_EQUITY)
        print(f"Equity         : ${eq:,.2f}   (start ${base:,.0f}, {((eq-base)/base*100 if base else 0):+.2f}%)")
    except Exception as e:  # noqa: BLE001
        print(f"Equity         : ERR {repr(e)[:70]}")

    # Current regime (spine portfolio state)
    try:
        row = await _latest("market_state_log", "created_at") or {}
        print(f"Regime         : {row.get('portfolio_state') or row.get('portfolio') or '?'}   "
              f"(as of {row.get('created_at', '?')})")
    except Exception as e:  # noqa: BLE001
        print(f"Regime         : ERR {repr(e)[:70]}")

    # Active-tier pair count (classify the latest pair_liquidity cycle)
    try:
        r = await db.supabase.table("pair_liquidity_metrics").select("*").order(
            "cycle_ts", desc=True).limit(60).execute()
        rows = r.data or []
        latest_ts = rows[0]["cycle_ts"] if rows else None
        cyc = [x for x in rows if x.get("cycle_ts") == latest_ts]
        tally = {}
        for x in cyc:
            try:
                tier, _, _ = tiers.classify_pair(x, kraken_max_leverage(x.get("symbol")))
            except Exception:  # noqa: BLE001
                tier = "ERR"
            tally[tier] = tally.get(tier, 0) + 1
        active = sum(v for k, v in tally.items() if k not in ("DISABLED", "ERR"))
        print(f"Active tiers   : {active} tradeable of {len(cyc)}   {dict(sorted(tally.items()))}")
    except Exception as e:  # noqa: BLE001
        print(f"Active tiers   : ERR {repr(e)[:70]}  (else see C8_ENFORCE_GATE logs)")

    # Open positions
    try:
        openp = await db.get_open_trades()
        print(f"Open positions : {len(openp)} / {config.MAX_OPEN_TRADES} max")
    except Exception as e:  # noqa: BLE001
        print(f"Open positions : ERR {repr(e)[:70]}")

    # Last-24h trades per strategy (net-of-cost)
    print("-" * 66)
    print("Last 24h trades (net-of-cost P&L):")
    try:
        trades = await db._fetch_all("trades", "open_time,status,pnl,direction,symbol,claude_reasoning")
        recent = [t for t in trades if str(t.get("open_time") or "") >= cutoff]
        agg = {s: {"n": 0, "closed": 0, "open": 0, "wins": 0, "losses": 0, "pnl": 0.0} for s in WORKING_SET}
        other = {"n": 0, "pnl": 0.0}
        for t in recent:
            sid = _parse_strategy_tag(t.get("claude_reasoning")) or "?"
            d = agg.get(sid)
            if d is None:
                other["n"] += 1
                other["pnl"] += (t.get("pnl") or 0.0)
                continue
            d["n"] += 1
            if t.get("status") == "closed":
                d["closed"] += 1
                p = t.get("pnl") or 0.0
                d["pnl"] += p
                if p > 0:
                    d["wins"] += 1
                elif p < 0:
                    d["losses"] += 1
            else:
                d["open"] += 1
        total = 0.0
        for s in WORKING_SET:
            d = agg[s]
            total += d["pnl"]
            print(f"  {s}: {d['n']} trades ({d['closed']} closed / {d['open']} open) | "
                  f"W-L {d['wins']}-{d['losses']} | net P&L ${d['pnl']:+,.2f}")
        if other["n"]:
            print(f"  other/untagged: {other['n']} trades | net P&L ${other['pnl']:+,.2f}")
        print(f"  TOTAL 24h net-of-cost P&L: ${total:+,.2f}")
        if not recent:
            print("  (no trades in the last 24h — correct if the tape offered no qualifying setup)")
    except Exception as e:  # noqa: BLE001
        print(f"  ERR {repr(e)[:70]}")

    # GATE_PASSED_ON_SWALLOW — log-derived (not DB-persisted); MUST stay 0
    print("-" * 66)
    swc = None
    try:
        out = subprocess.run(["railway", "logs"], capture_output=True, text=True, timeout=60)
        swc = out.stdout.count("GATE_PASSED_ON_SWALLOW")
    except Exception:  # noqa: BLE001
        swc = None
    if swc is not None:
        flag = "OK" if swc == 0 else "!! INVESTIGATE"
        print(f"GATE_PASSED_ON_SWALLOW (recent logs): {swc}   [{flag}]  (must stay 0)")
    else:
        print("GATE_PASSED_ON_SWALLOW: run `railway logs | grep -c GATE_PASSED_ON_SWALLOW` "
              "(must be 0; not DB-persisted)")
    print("=" * 66)


if __name__ == "__main__":
    asyncio.run(main())
