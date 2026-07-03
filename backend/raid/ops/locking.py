"""Distributed lease lock — single-writer guarantee across Railway workers.

Railway runs `restartPolicyType=always` and a deploy can briefly overlap the old and
new container, so two workers could place orders at once. This lease lock ensures
exactly one worker is the ACTIVE writer at a time: a worker must hold a fresh,
heartbeated lease to open trades; a worker that cannot acquire the lease runs passive
(monitoring only). The acquire is an atomic compare-and-set UPDATE (row-level atomic
in Postgres), so two workers can never both win.

The decision logic here is pure and unit-tested; the async manager wires it to the DB
at the Phase-5 cutover.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

LEASE_TTL_SECONDS = 60      # a lease is valid for this long after each heartbeat
HEARTBEAT_SECONDS = 20      # holder renews well within the TTL
LEASE_ROW_ID = 1            # single-row lease table


@dataclass(frozen=True)
class Lease:
    holder_id: str
    expires_at: float        # unix seconds


def lease_is_fresh(now: float, lease: Optional[Lease]) -> bool:
    return lease is not None and now < lease.expires_at


def can_acquire(now: float, worker_id: str, lease: Optional[Lease]) -> bool:
    """A worker may acquire when there is no lease, the lease has expired, or it
    already holds the lease (renewal)."""
    if lease is None:
        return True
    if now >= lease.expires_at:
        return True
    return lease.holder_id == worker_id


def is_active_writer(now: float, worker_id: str, lease: Optional[Lease]) -> bool:
    """Only the fresh-lease holder is allowed to open new trades."""
    return lease is not None and lease.holder_id == worker_id and now < lease.expires_at


def next_expiry(now: float, ttl: float = LEASE_TTL_SECONDS) -> float:
    return now + ttl


class DistributedLock:
    """DB-backed lease manager. `db` must expose an async
    `try_claim_lease(row_id, worker_id, now, new_expiry) -> bool` that performs the
    atomic compare-and-set UPDATE (WHERE holder IS NULL OR expires_at < now OR
    holder = worker_id) and returns whether this worker now holds the lease.
    """

    def __init__(self, db, worker_id: str, now_fn):
        self._db = db
        self.worker_id = worker_id
        self._now = now_fn

    async def acquire_or_renew(self) -> bool:
        now = self._now()
        return await self._db.try_claim_lease(
            LEASE_ROW_ID, self.worker_id, now, next_expiry(now)
        )

    async def am_i_active(self) -> bool:
        lease = await self._db.get_lease(LEASE_ROW_ID)
        return is_active_writer(self._now(), self.worker_id, lease)
