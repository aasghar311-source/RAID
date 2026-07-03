# RAID Omega — August 14, 2026 Live-Readiness Report (Phase 10)

**Verdict: NOT READY FOR LIVE — and correctly so.** The rebuild delivered the paper-
validation platform; live trading is gated on evidence the system has not yet produced.
No gate may be waived because a date arrives (Section 25).

## Validation evidence (this run)
- **80/80** RAID Omega unit tests pass (`python -m raid.tests.run_all`)
- **8/8** cost-model tests; **live** pagination test PASS (8,163 rows, no dupes)
- All legacy modules import clean; **10/10** strategies register
- Frontend `next build` passes (20/20 static pages)

## Gate-by-gate (Section 25)

| Gate | Status | Notes |
|---|---|---|
| No live-trading code enabled | ✅ PASS | auto-flip removed; paper permanent; order fns are stubs |
| ≥1 independently profitable *validated* strategy | ⛔ BLOCKING | all strategies in shadow; none promoted on evidence |
| Positive combined portfolio expectancy after costs | ⛔ BLOCKING | legacy −$0.36/trade; new engine not yet trading |
| No malformed candidate reaches execution | ✅ PASS (new) / ⛔ (legacy) | typed candidates fail-closed; legacy string-parse retires at cutover |
| No duplicate orders | 🟡 DESIGN | lease lock built + tested; wired at cutover |
| No unreconciled positions / unknown states | 🟡 DESIGN | state machine + reconciliation designed; wired at cutover |
| Verified restart recovery | 🟡 PENDING | design done; needs cutover + a live restart test |
| Verified kill switch | ✅ PASS | legacy gate auto-kills on daily-loss; operator control present |
| Verified daily / weekly stops | 🟡 PARTIAL | risk manager built+tested (daily 4% / weekly 8%); legacy has daily only |
| Verified 20% hard shutdown | 🟡 PARTIAL | risk manager built+tested; not yet on the live path |
| Verified account capability detection | 🟡 PENDING | capability model built; not checked against the real Kraken account |
| Verified fee model | 🟡 PENDING | cost model built; **fee rate 0.16% unverified vs Kraken taker 0.26%** |
| Verified precision / minimum orders | ⛔ PENDING | not implemented |
| Verified WebSocket recovery / dead-man's switch | ⛔ PENDING | built-not-enabled; deferred |
| Verified Supabase security (RLS) | 🟡 STAGED | plan + migration ready; needs service-role key then Part-B enable |
| Verified dashboard accuracy | ✅ PASS | real data, paginated, build passes |
| Verified audit trail | 🟡 PARTIAL | state machine records transitions; persistence at cutover |
| Explicit operator approval | ⛔ PENDING | required |

## The critical path to a live decision
1. **Cutover** — wire `raid/` into the worker (candidate → risk → order manager → fill sim → per-strategy ledgers); retire the legacy string-parse path (Phase 7 deep cleanup).
2. **Run all strategies in shadow/paper** and let real per-strategy scorecards accumulate.
3. **Promote** only strategies that clear the Section-12 evidence gates; confirm positive net portfolio expectancy.
4. **Verify** the real Kraken fee tier, precision, min-order sizes, and account capabilities.
5. **Apply** the staged RLS (service-role key first) and wire the lease lock + reconciliation.
6. Build/enable WebSocket + dead-man's switch; run failure-injection + restart-recovery drills.
7. **Operator sign-off.**

Until every blocking gate is green, RAID stays in paper. This report is the fail-closed
default: it blocks rather than silently launches.
