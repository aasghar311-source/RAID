"""Integration test: db._fetch_all beats PostgREST's 1000-row response cap.

Read-only against the live Supabase DB. Uses regime_log (thousands of rows) to
prove that an unbounded select truncates at 1000 while _fetch_all returns all rows.
Run: python test_pagination.py
"""

import asyncio
import db


async def main():
    await db.init()

    # Unbounded select — truncates at the PostgREST default cap.
    capped = await db.supabase.table("regime_log").select("id").execute()
    n_capped = len(capped.data or [])

    # Exact row count via HEAD + count.
    head = await db.supabase.table("regime_log").select("id", count="exact", head=True).execute()
    n_exact = head.count

    # Paginated fetch — must return every row.
    all_rows = await db._fetch_all("regime_log", "id")
    n_all = len(all_rows)

    print(f"  exact count (HEAD):   {n_exact}")
    print(f"  unbounded select:     {n_capped}  (truncated)")
    print(f"  _fetch_all paginated: {n_all}")

    assert n_exact > 1000, f"test table must exceed 1000 rows to be meaningful (got {n_exact})"
    assert n_capped < n_exact, f"unbounded select should truncate, got {n_capped} of {n_exact}"
    assert n_all == n_exact, f"_fetch_all returned {n_all}, expected {n_exact}"

    # No duplicate ids across pages (range boundaries correct).
    ids = [r["id"] for r in all_rows]
    assert len(ids) == len(set(ids)), "duplicate ids across pages — range boundaries wrong"

    print("PASS — _fetch_all returns all rows past the 1000-row cap, no dupes")


if __name__ == "__main__":
    asyncio.run(main())
