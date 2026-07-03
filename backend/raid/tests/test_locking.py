"""Tests for the distributed lease lock decision logic (single-writer guarantee)."""

from raid.ops.locking import (
    Lease, lease_is_fresh, can_acquire, is_active_writer, next_expiry, LEASE_TTL_SECONDS,
)

NOW = 1_000_000.0


def test_fresh_and_expired():
    fresh = Lease("w1", NOW + 30)
    stale = Lease("w1", NOW - 1)
    assert lease_is_fresh(NOW, fresh) is True
    assert lease_is_fresh(NOW, stale) is False
    assert lease_is_fresh(NOW, None) is False


def test_can_acquire_rules():
    # No lease -> anyone can acquire.
    assert can_acquire(NOW, "w1", None) is True
    # Fresh lease held by another -> cannot acquire.
    assert can_acquire(NOW, "w2", Lease("w1", NOW + 30)) is False
    # Expired lease -> anyone can acquire.
    assert can_acquire(NOW, "w2", Lease("w1", NOW - 5)) is True
    # Own fresh lease -> can renew.
    assert can_acquire(NOW, "w1", Lease("w1", NOW + 30)) is True


def test_only_fresh_holder_is_active_writer():
    assert is_active_writer(NOW, "w1", Lease("w1", NOW + 10)) is True
    assert is_active_writer(NOW, "w2", Lease("w1", NOW + 10)) is False   # not holder
    assert is_active_writer(NOW, "w1", Lease("w1", NOW - 1)) is False    # expired
    assert is_active_writer(NOW, "w1", None) is False


def test_two_workers_cannot_both_be_active():
    lease = Lease("w1", NOW + 30)
    assert is_active_writer(NOW, "w1", lease) is True
    assert is_active_writer(NOW, "w2", lease) is False


def test_next_expiry():
    assert next_expiry(NOW) == NOW + LEASE_TTL_SECONDS
    assert next_expiry(NOW, ttl=10) == NOW + 10
