# RAID Go-Live Runbook — enforce the working set (C1/C2/C3)

First honest measurement window. After this, every trade runs under the full post-cost ruleset
(A.1 real spread, A.2 volume floor, B.5 portfolio caps, C.8 tier gate, B.3 fail-closed gate) and the
equity curve is meaningful. **Nothing here is executed for you — run it step by step.**

## What the go-live commit changes (review the diff before pushing)

| Change | Effect | Reversible by |
|--------|--------|---------------|
| `STRATEGY_SHADOW` drops C1/C2/C3 | C1/C2/C3 book live-paper; C4/C5/C6/C7/C10 stay SHADOW; C8/C9 stubs | re-add the 3 ids |
| `ENFORCE_TIER_GATE=True` (C.8) | non-active-tier pairs rejected; tier leverage + risk-mult bind | flag `False` |
| `ENFORCE_GATE_FAIL_CLOSED=True` (B.3) | a swallowed gate-check exception REJECTS instead of passing | flag `False` |
| `_real_spread_depth` top-of-book fix (**A.1**) | runtime spread = true best-bid/ask, not the biggest-wall gap | revert the fn |
| `MAX_OPEN_TRADES` 60 → 5 | Appendix-C hard cap: ≤5 concurrent positions (binds in gate CHECK 4, independent of the deployment cap) | value |
| boot banner (resolved) | derives LIVE/SHADOW/DISABLED from the registry so it can't drift from config | — |

### ⚠️ Read before you push — the A.1 spread fix is a discovered blocker, not a requested change
Staging surfaced that the C.8 gate priced on `ctx.spread_pct`, which A.1 was computing as the gap
between the two **largest size-sorted walls** (`bid_walls[0]`/`ask_walls[0]`) — a phantom spread
(BTC 0.175%, XMR 4.3%) 3–70× wider than the true top-of-book (classifier `spread_med` ≈ **0.05%**).
Under the 0.15–0.25% tier caps that would have **rejected nearly the whole universe except BTC/ETH**
when the tape turned — an empty first window. The fix makes `_real_spread_depth` use the same
top-of-book best-bid/ask the tier classifier already uses, so A.1 / C.8 / classifier agree on ONE
spread. It's the same "top-3-walls" class of bug you already approved fixing in the depth path.
**If you'd rather ship go-live without it and accept BTC/ETH-only trading, say so and I'll drop it.**

Suite: **301/301**. Commit is staged **locally, not pushed**.

---

## Operator sequence

### STEP 1 — Disable crypto (halt new entries)
Supabase SQL editor (or the dashboard toggle if you prefer):
```sql
UPDATE public.operator_controls SET crypto_enabled = false;
```
Confirm the current worker stops opening (it's all-SHADOW now anyway; this makes it explicit and
survives the reset since `operator_controls` is preserved).

### STEP 2 — Backup (fast in-DB snapshot of trading history)
```sql
CREATE TABLE IF NOT EXISTS trades_backup_golive           AS SELECT * FROM public.trades;
CREATE TABLE IF NOT EXISTS equity_snapshots_backup_golive AS SELECT * FROM public.equity_snapshots;
```
Also trigger a Supabase point-in-time / manual backup (Dashboard → Database → Backups) for a full
snapshot. `ohlcv_5m` is **preserved by the reset**, so no separate export is needed.

### STEP 3 — Reset SQL (fresh trading slate; preserves ohlcv_5m + operator_controls)
Run `backend/ops/golive_reset.sql` in the SQL editor. It truncates trading state + learning + run
logs, restarts identities, and prints a row-count check. Verify the output: reset tables = 0 rows,
`ohlcv_5m` + `operator_controls` > 0 rows, and `crypto_enabled = false`.

> Race note: the old worker cycles every ~5 min. Running the reset (a sub-second `DO` block) while it
> idles crypto-disabled is safe — any stray re-seed row is harmless. For a spotless reset, pause the
> Railway service first and let STEP 4 restart it.

### STEP 4 — Deploy the enforcement commit
```
git push origin main
```
Railway auto-deploys (~2–3 min). This boots the new code (C1/C2/C3 live + C.8/B.3 enforce + A.1 fix)
against the clean DB.

### STEP 5 — Boot-verify (see checklist below). Do NOT re-enable crypto until every line is green.

### STEP 6 — Re-enable crypto (go live)
```sql
UPDATE public.operator_controls SET crypto_enabled = true;
```
The bot begins trading C1/C2/C3 under the full ruleset. Watch the first entries in the logs.

---

## Boot-verify checklist (after STEP 4, before STEP 6)

Pull logs: `railway logs`. Confirm each:

- [ ] **Deploy SUCCESS** on the new commit — `railway deployment list` top row = SUCCESS.
- [ ] **Boot banner** (one line, `grep "RAID ENGINE ONLINE"`) — states are RESOLVED from the registry:
      `RAID ENGINE ONLINE — worker=… lease=ACQUIRED — go-live resolved:
      LIVE=['RAID-C1','RAID-C2','RAID-C3']
      SHADOW=['RAID-C10','RAID-C4','RAID-C5','RAID-C6','RAID-C7']
      DISABLED=['RAID-C8','RAID-C9'] | tier_gate=True fail_closed_gate=True real_spread=True max_open=5`
      — verify LIVE is **exactly** C1/C2/C3 (nothing benched leaked in) and `max_open=5`.
- [ ] **Lease = ACQUIRED** (not PASSIVE). PASSIVE ⇒ a stale worker still holds the lease; wait one
      TTL (~15 min) or confirm `worker_leases` was truncated in STEP 3.
- [ ] **Safety flags**: `PAPER_ONLY=True`, `LIVE_TRADING_ENABLED=False`, `KRAKEN_LIVE_ENABLED=False`
      (fail-closed defaults; no live-trading env vars set).
- [ ] **A.1 spread now top-of-book** — `grep SPREAD_DEPTH_SHADOW`: `real_spread` ≈ **0.0004–0.0010**
      for liquid pairs (matches `PAIR_LIQUIDITY_SHADOW spread_med`), **not** 0.02–0.04. This is the
      single most important line — it proves the fix landed and the universe isn't spread-starved.
- [ ] **C.8 enforcing** — `grep C8_ENFORCE_GATE` (label flipped from `C8_SHADOW_GATE`); active-tier
      pairs show `would=ADMIT`, DISABLED show `would=REJECT`. When a candidate on a DISABLED pair
      appears: `skip … — tier gate REJECT`.
- [ ] **C1/C2/C3 live** — no `STRATEGY_SHADOW RAID-C1|C2|C3` lines; when they fire you see `SIZING …`
      then a booking. **`booked 0` for many cycles is EXPECTED, not a failure — see the tape note.**
- [ ] **C4/C5/C6/C7/C10 shadow** — `STRATEGY_SHADOW` lines appear ONLY for these ids (they log, never
      book).
- [ ] **B.3 armed** — `fail_closed_gate=True` in the banner. (`GATE_PASSED_ON_SWALLOW … fail_closed=True`
      only prints if a check actually throws — none expected.)
- [ ] **No PGRST / tracebacks** — `railway logs | grep -iE "PGRST|traceback|exception"` is clean.
- [ ] **Cycle completes** — `RAID ENGINE: cycle complete — booked N …` with no errors.

> ### Tape note — zero trades post-go-live is the CORRECT finding, not a failed deploy
> At last check the book was **RISK_OFF (spine LONG=0)**. In that tape: **C1/C2 are dormant** (they
> need a LONG spine) and **C3 only fires on a volume-qualifying breakdown** — so expect `booked 0`
> for **potentially many cycles** after go-live. That is the honest system finding no qualifying
> setup in a risk-off grind — **it is correct behavior, not a broken deploy.**
>
> **Do NOT loosen thresholds to "get trades."** Wait for a qualifying setup or a regime shift (broad
> risk-on → C1/C2 longs; broad risk-off with down-pairs → C3 shorts). The old bot's negative
> expectancy came from trading noise; an empty risk-off window is the ruleset working as designed.
> Flat ≠ broken. The only failure signals are the boot-verify checks above (errors, wrong resolved
> map, spread-starvation) — not a low trade count.

## Rollback (if boot-verify fails or behavior is wrong)
- Fastest: re-disable crypto (`crypto_enabled=false`) — stops new entries immediately.
- Revert the go-live: set `ENFORCE_TIER_GATE=False`, `ENFORCE_GATE_FAIL_CLOSED=False`, and re-add
  `"RAID-C1","RAID-C2","RAID-C3"` to `STRATEGY_SHADOW`, then push. Restores full-SHADOW.
- Restore data: `trades` / `equity_snapshots` from the `*_backup_golive` tables (STEP 2).

## Post-go-live (do NOT build now)
**Order-book capture is the next infrastructure step.** It unlocks C10 (the sweep detector's
book-support condition can't be calibrated without it) and gives the tier system a live depth signal
instead of per-cycle snapshots. Scope it after the first measurement window, not before.
