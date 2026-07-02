"""Tests for the strict typed Candidate schema — construction must fail closed."""

from decimal import Decimal

from pydantic import ValidationError

from raid.core.candidate import (
    Candidate, Direction, EntryType, MarketRegime, Rejection,
    CandidateStatus, TERMINAL_STATUSES,
)


def _valid_long_kwargs() -> dict:
    # 1.0% stop, 2.5% target, 0.16%/side fees. net_reward=0.0218, net_risk=0.0132.
    return dict(
        candidate_id="c1", strategy_id="RAID-C1", strategy_version="1.0.0",
        code_version="abc123", symbol="SOLUSD", instrument_id="SOLUSD",
        direction=Direction.LONG, setup_timeframe="15m", market_regime=MarketRegime.TREND_UP,
        entry_type=EntryType.MARKET, reference_price=Decimal("100"),
        stop_price=Decimal("99"), targets=(Decimal("102.5"),),
        expected_entry_fee=Decimal("0.0016"), expected_exit_fee=Decimal("0.0016"),
        expected_spread=Decimal("0"), expected_slippage=Decimal("0"),
        gross_reward=Decimal("0.025"), gross_risk=Decimal("0.01"),
        net_reward=Decimal("0.0218"), net_risk=Decimal("0.0132"), net_rr=Decimal("1.6515"),
        planned_risk_dollars=Decimal("20"), quantity=Decimal("0.2"),
        setup_timestamp="2026-07-02T00:00:00Z", expiry_timestamp="2026-07-02T00:20:00Z",
        market_data_snapshot_id="md1", feature_snapshot_id="ft1",
    )


def _expect_reject(mutate: dict):
    kw = _valid_long_kwargs()
    kw.update(mutate)
    try:
        Candidate(**kw)
        raise AssertionError(f"expected ValidationError for {mutate}")
    except ValidationError:
        pass


def test_valid_candidate_builds():
    c = Candidate(**_valid_long_kwargs())
    assert c.direction == Direction.LONG
    assert c.is_economic(Decimal("1.25")) is True
    assert c.entry_reference_price() == Decimal("100")


def test_frozen_and_no_extra_fields():
    c = Candidate(**_valid_long_kwargs())
    try:
        c.symbol = "X"  # frozen
        raise AssertionError("expected frozen model")
    except ValidationError:
        pass
    _expect_reject({"unexpected_field": 1})  # extra='forbid'


def test_reject_wrong_side_stop():
    _expect_reject({"stop_price": Decimal("101")})   # long stop above entry


def test_reject_wrong_side_target():
    _expect_reject({"targets": (Decimal("99.5"),)})  # long target below entry


def test_reject_nonpositive_price():
    _expect_reject({"reference_price": Decimal("0")})
    _expect_reject({"stop_price": Decimal("-1")})


def test_reject_bad_quantity_and_risk():
    _expect_reject({"quantity": Decimal("0")})
    _expect_reject({"planned_risk_dollars": Decimal("0")})


def test_reject_stop_entry_without_trigger():
    _expect_reject({"entry_type": EntryType.STOP})   # trigger_price is None


def test_reject_inconsistent_net_rr():
    _expect_reject({"net_rr": Decimal("3.0")})       # 3.0 != 0.0218/0.0132 (~1.65)


def test_reject_net_exceeds_gross():
    _expect_reject({"net_reward": Decimal("0.030")})  # > gross_reward 0.025


def test_short_candidate_valid():
    kw = _valid_long_kwargs()
    kw.update(
        direction=Direction.SHORT, market_regime=MarketRegime.TREND_DOWN,
        reference_price=Decimal("100"), stop_price=Decimal("101"),
        targets=(Decimal("97.5"),),
    )
    c = Candidate(**kw)
    assert c.direction == Direction.SHORT


def test_rejection_model():
    r = Rejection(strategy_id="RAID-C4", symbol="AAVEUSD", reasons=("no_range",))
    assert r.reasons == ("no_range",)
    try:
        Rejection(strategy_id="x", symbol="y", reasons=())  # min_length=1
        raise AssertionError("expected reasons min_length")
    except ValidationError:
        pass


def test_terminal_statuses():
    assert CandidateStatus.CLOSED in TERMINAL_STATUSES
    assert CandidateStatus.ARMED not in TERMINAL_STATUSES
