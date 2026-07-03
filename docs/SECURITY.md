# RAID Omega — Security & Resilience Plan (Phase 9)

## 1. Credential audit (current state)

| Secret | Where | Finding |
|---|---|---|
| `SUPABASE_KEY` | Railway backend | **anon key** (JWT role claim = `anon`), used for **writes** with RLS disabled. |
| `NEXT_PUBLIC_SUPABASE_ANON_KEY` | Vercel frontend | anon key, client-exposed (normal for anon) — but same key class as backend. |
| Kraken / Anthropic / etc. | Railway env | Backend-only, never in git (`.env` gitignored; `.env.example` has placeholders). |

**Risk:** anyone holding the anon key can read *and* write every table (RLS off). The frontend, being public, effectively publishes a read/write key.

## 2. RLS remediation (staged — `migrations/002_security_and_locking.sql` Part B)

Enabling RLS naively would break the anon-key backend writes and silently stop trading (the exact failure `disable_rls.sql` was written to avoid). Correct sequence:

1. **Add a `service_role` key to the backend** (`SUPABASE_SERVICE_KEY` on Railway); point the backend Supabase client at it. `service_role` bypasses RLS, so writes keep working.
2. **Verify** backend writes persist (trades/signals).
3. **Confirm** the frontend uses *only* the anon key.
4. **Enable RLS** (uncomment Part B): anon gets `SELECT` on display tables **only** — no `insert/update/delete` policy for anon anywhere. The frontend becomes strictly read-only; `operator_controls` cannot be mutated from the exposed key (kill-switch/mode safe).

Every policy is explicit, per-table, and reversible (rollback in the migration).

## 3. Distributed locking (`raid/ops/locking.py` + migration 002 Part A)

Railway runs `restartPolicyType=always`, and a deploy can briefly overlap old/new containers → **two workers could open trades simultaneously.** Mitigation: a single-row **lease lock**. A worker must hold a fresh, heartbeated lease (TTL 60s, renew 20s) to be the *active writer*; a worker that can't acquire it runs **passive (monitor-only)**. Acquisition is an atomic compare-and-set `UPDATE ... WHERE holder IS NULL OR expires_at < now OR holder = self RETURNING id` — row-level atomic, so two workers can never both win. Decision logic is unit-tested (`test_locking.py`).

## 4. Failure recovery & health

- **Fail-closed everywhere:** uncertain state → no trade (Section 9); UNKNOWN order state freezes new risk for the symbol until reconciliation (state machine).
- **Restart recovery:** on boot, a worker reconciles open positions against venue truth before taking new risk (cutover wiring); the lease lock prevents a restarted duplicate from trading.
- **Health surface:** Command Center + Execution Health pages show last-cycle / last-regime freshness, order-state distribution, and (post-cutover) reconciliation status. `restartPolicyMaxRetries=10` bounds crash loops.
- **No live path:** paper mode is permanent; the date-based auto-flip was removed in Phase 1 (`968477c`).

## 5. Residual items before live (Aug-14 gates)
Service-role key rotation, Part-B RLS enablement + policy tests, WebSocket + dead-man's-switch (built-not-enabled), verified Kraken fee tier, and reconciliation wiring at cutover.
