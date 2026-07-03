# RAID Omega Rebuild — System Handoff

Complete rebuild of RAID from a single LLM-string-parsing bot into a deterministic,
typed, multi-strategy paper-trading platform. Built **alongside** the legacy engine in
a new `backend/raid/` package, so the live paper bot ran untouched throughout and every
phase is reversible.

## Commit ledger (backend `RAID`, branch `main`)

| Phase | Commit | Summary |
|---|---|---|
| 1 | `968477c` | remove date-based auto-flip → paper permanent (safety) |
| 1 | `6282ed1` | authoritative cost model `costs.py` (+8 tests) |
| 1 | `83874fd` | paginate DB aggregations past 1000-row cap (live-verified) |
| 1 | `ada412a` | immutable legacy-archive migration (`migrations/001`) |
| 2+3 | `d20f0e1` | typed candidates, features, regime, risk, allocator, registry, data provider (46 tests) |
| 4 | `4f98eba` | all 10 RAID-C strategy modules (53 tests) |
| 5 | `d82f643` | order state machine + realistic fill simulator (67 tests) |
| 6 | `edf22a2` | promotion/quarantine evidence engine (75 tests) |
| 7 | `3a6abcf` | remove verified-dead config constants |
| 9 | `7cd1887` | distributed lease lock + staged RLS + security plan (80 tests) |
| 10 | (this) | validation + readiness + handoff docs |

Frontend (`raid-terminal`, separate repo): `47a147c` — light 16-page terminal.

## Architecture

```
LEGACY (still live, paper):  scanner → brain(Haiku string checklist) → worker → executor → Supabase
NEW (raid/, shadow, tested): provider → marketdata(validate) → features → regime
                              → 10 strategies → typed Candidate → risk manager (sized)
                              → order state machine → fill simulator → per-strategy + unified ledgers
                              → promotion/quarantine → allocator ; lease lock guards single-writer
```
AI has **zero** authority over prices, sizes, risk, or execution (Section 7.1). No prose
in the financial path — malformed candidates are rejected, never repaired.

## `backend/raid/` map
- `core/candidate.py` — strict Pydantic `Candidate` + `Rejection` + lifecycle `CandidateStatus`
- `core/features.py` — deterministic indicators (EMA/RSI/ATR/Bollinger/Donchian/slope)
- `core/regime.py` — trend/range/volatile/crisis classifier
- `core/risk.py` — tiers 0–5, drawdown de-risk ladder, 1.50% ceiling, sizing
- `core/allocator.py` — expectancy-weighted, shrinkage; cash is valid
- `core/promotion.py` — Section-12 promote/quarantine gates
- `core/strategy.py` / `registry.py` — interface + feature-flagged registry
- `core/marketdata.py` / `provider.py` — normalized data, quality events, Kraken normalizers
- `strategies/` — C1–C10 (`catalog.build_default_registry()`)
- `execution/state_machine.py` / `fills.py` — lifecycle + realistic fills
- `ops/locking.py` — distributed lease lock
- `tests/` — 80 unit tests (`python -m raid.tests.run_all`)
- top-level `costs.py` — authoritative cost model (shared)

## Operate
- Tests: `cd backend && python -m raid.tests.run_all` (80) · `python test_costs.py` · `python test_pagination.py`
- Frontend: `cd raid-terminal && npm run build`
- Migrations to run in Supabase SQL editor (service role), in order:
  `001_legacy_trades_archive.sql`, `002_security_and_locking.sql` (Part A now; Part B after the service-role switch).

## Remaining before live (the cutover — see READINESS.md)
1. Wire `raid/` into the worker (candidate→risk→order manager→fill sim→per-strategy ledgers); retire the legacy string-parse path.
2. Accumulate real per-strategy scorecards in shadow/paper; promote only on Section-12 evidence.
3. Verify Kraken fee tier / precision / min-orders / account capabilities.
4. Apply staged RLS + wire lease lock + reconciliation; build/enable WebSocket + dead-man's switch.
5. Failure-injection + restart-recovery drills; operator sign-off.

## Adversarial review instructions (Section 29)
A separate agent should independently: (1) re-run every test (`raid.tests.run_all`, `test_costs`, `test_pagination`) and confirm counts; (2) attempt to construct a **malformed `Candidate`** that the schema accepts (wrong-side stop, negative price, inconsistent `net_rr`, missing stop) — all must raise; (3) verify the **state machine** rejects `SUBMITTING→FILLED` and freezes on `UNKNOWN`; (4) verify the **risk manager** enforces the 1.50% ceiling and 20% shutdown, and that `effective_tier` only ever de-risks; (5) verify the **fill simulator** partial-fills on thin depth, rejects empty books, and gaps stops; (6) grep the whole backend for any live-order path or `PAPER_MODE=False` (must find none); (7) confirm the frontend build is green and no page fabricates data; (8) re-check the Aug-14 gates and produce an independent verdict. Build to survive this review.
