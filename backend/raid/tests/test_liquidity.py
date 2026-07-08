"""C.6 — Appendix-C §2 pair-liquidity metrics (measure-only). Pure functions tested directly:
the completed-candle guard (B.4 fold-in), volume/liquidity/cost metrics, fail-closed None on
bad/thin/crossed data, and the compute_pair_liquidity aggregate shape. No DB. Run_all-discovered."""

import costs
from raid.core import liquidity as L


def _c5(n=25, vol=100.0, latest_vol=None, ts0=1_000_000, step=300):
    bars = [[ts0 + i * step, 100.0, 100.5, 99.5, 100.0, vol] for i in range(n)]
    if latest_vol is not None:
        bars[-1][5] = latest_vol
    return bars


def _book(bid=99.9, ask=100.1, bid_usd=5000.0, ask_usd=5000.0):
    return {"bid_walls": [{"price": bid, "usd": bid_usd}], "ask_walls": [{"price": ask, "usd": ask_usd}]}


# ---- completed-candle guard (B.4) ----
def test_drop_forming():
    bars = _c5(n=5, ts0=0, step=300)                      # bar timestamps 0,300,600,900,1200
    assert len(L.drop_forming(bars, now_epoch=1400)) == 4  # inside 1200-1500 -> last bar forming -> drop
    assert len(L.drop_forming(bars, now_epoch=1600)) == 5  # 1200 interval closed -> keep
    assert L.drop_forming(bars, now_epoch=None) == bars    # no epoch -> unchanged
    assert L.drop_forming([], 1400) == []


# ---- volume (7) ----
def test_volume_ratio_and_rates():
    assert abs(L.volume_ratio(_c5(n=25, vol=100.0, latest_vol=200.0)) - 2.0) < 1e-9
    assert L.volume_ratio(_c5(n=10)) is None                       # <21 bars -> None
    z = _c5(n=25, vol=100.0)
    z[-1][5] = 0.0
    assert L.zero_volume_rate(z) > 0.0
    assert L.zero_volume_rate(_c5(n=25, vol=100.0)) == 0.0
    assert L.low_volume_rate(_c5(n=25, vol=100.0)) == 0.0           # steady vol -> ratio ~1.0 -> not thin


def test_usd_volume_conversion():
    b = _c5(n=25, vol=100.0, latest_vol=300.0)
    assert abs(L.latest_5m_vol_usd(b) - 30000.0) < 1e-6            # 300 base * 100 close = 30000 USD


def test_trailing20_vol_usd_robust_to_one_quiet_latest_bar():
    # [ts,o,h,l,close,base_vol]; USD = base_vol*close. §2 trailing_20 average judges tradeability over
    # ~100 min, so a genuinely-active pair whose ONE latest bar is quiet is NOT gated (mirrors live NEAR
    # latest $55 vs a trailing avg well over $250) — while the single-bar read would spuriously gate it.
    active = [[i * 300, 0, 0, 0, 100.0, 5.0] for i in range(19)] + [[19 * 300, 0, 0, 0, 100.0, 0.05]]
    assert abs(L.latest_5m_vol_usd(active) - 5.0) < 1e-6          # single latest bar = 0.05*100 = $5 (would gate)
    assert L.trailing20_vol_usd(active, 20) >= 250                # trailing-20 avg ~= $475 -> passes (recovered)
    thin = [[i * 300, 0, 0, 0, 100.0, 0.1] for i in range(20)]
    assert L.trailing20_vol_usd(thin, 20) < 250                  # genuinely thin ($10 avg) -> still gated
    assert L.trailing20_vol_usd(active[:10], 20) is None         # < window bars -> None (fail-closed)


def test_low_volume_rate_absolute():
    assert L.low_volume_rate(_c5(n=25, vol=100.0)) == 0.0          # $10,000/bar -> none below $250
    assert L.low_volume_rate(_c5(n=25, vol=1.0)) == 1.0           # $100/bar -> all below $250
    bars = _c5(n=25, vol=100.0)
    for i in range(10):
        bars[i][5] = 1.0                                          # 10 thin bars ($100) of 25
    assert abs(L.low_volume_rate(bars) - 10 / 25) < 1e-9


def test_dollar_vol_30d_median():
    daily = [[i * 86400, 100, 101, 99, 100, 1000.0] for i in range(30)]   # USD = 1000*100 = 100000
    assert abs(L.dollar_vol_30d_median(daily) - 100000.0) < 1e-6
    assert L.dollar_vol_30d_median([]) is None                     # no daily history -> None
    assert L.dollar_vol_30d_median(None) is None


def test_trailing20():
    b = _c5(n=25, vol=100.0)                                        # each bar 100*100 = 10000 USD
    assert abs(L.trailing20_vol_usd(b) - 10000.0) < 1e-6           # mean of last 20
    assert L.trailing20_vol_usd(_c5(n=10)) is None                 # < 20 bars -> None


def test_depth_uses_full_book_levels():
    # 5 levels/side all within 10bps of mid 100; walls-only would see just the top-3.
    book = {"bid_levels": [{"price": 99.99 - i * 0.001, "usd": 1000.0} for i in range(5)],
            "ask_levels": [{"price": 100.01 + i * 0.001, "usd": 1000.0} for i in range(5)],
            "bid_walls": [{"price": 99.99, "usd": 1000.0}], "ask_walls": [{"price": 100.01, "usd": 1000.0}]}
    assert L.depth_within_bps(book, 25) == 10000.0                 # full book (10x1000), not walls (2000)


# ---- liquidity (5) ----
def test_spread_depth_slippage():
    bk = _book()
    assert abs(L.spread_pct(bk) - 0.002) < 1e-9                    # (100.1-99.9)/100
    assert L.spread_pct({"bid_walls": [], "ask_walls": []}) is None    # empty -> None
    assert L.spread_pct(_book(bid=100.2, ask=100.1)) is None            # crossed -> None
    assert abs(L.depth_within_bps(bk, 25) - 10000.0) < 1e-6            # both walls within 25bps
    slip = L.slippage_estimate(bk, 500.0)
    assert slip is not None and abs(slip - 0.001) < 1e-4              # 500 filled at 100.1 -> 0.1%
    assert L.slippage_estimate(bk, 100000.0) is None                 # walls can't cover -> None


# ---- cost (3) ----
def test_cost_metrics():
    cost, mult, nrr = L.cost_metrics(0.002, atr_pct=0.02)
    exp = costs.dynamic_round_trip_cost_pct(spread_pct=0.002, uncertainty_buffer_pct=0.0)["total_pct"]
    assert abs(cost - exp) < 1e-12                                 # real-spread cost, buffer excluded
    assert abs(mult - 0.02 / cost) < 1e-9                          # 1-ATR move / cost
    assert nrr is not None
    assert L.cost_metrics(None, 0.02) == (None, None, None)        # unknown spread -> all None
    c2, m2, n2 = L.cost_metrics(0.002, atr_pct=None)               # no atr -> cost only
    assert c2 is not None and m2 is None and n2 is None


def test_atr_pct_1h():
    bars = [[i, 100, 101, 99, 100] for i in range(20)]            # TR=2 each vs prev close 100
    assert abs(L.atr_pct_1h(bars, 100.0) - 0.02) < 1e-6
    assert L.atr_pct_1h([], 100.0) is None
    assert L.atr_pct_1h(bars, 0.0) is None


# ---- aggregate (15 metrics + instrumentation) ----
def test_compute_pair_liquidity_shape():
    daily = [[i * 86400, 100, 101, 99, 100, 5000.0] for i in range(30)]
    m = L.compute_pair_liquidity("SOLUSD", _c5(n=25, vol=100.0, latest_vol=200.0), _book(),
                                 price=100.0, now_epoch=None, atr_pct=0.02, volume_24h_usd=1_000_000.0,
                                 ohlcv_1d=daily)
    for k in ("symbol", "dollar_vol_24h", "dollar_vol_30d_median", "dollar_vol_5m_median",
              "trailing20_vol_usd", "latest_5m_vol_usd", "volume_ratio", "zero_volume_rate",
              "low_volume_rate", "spread_pct", "depth_10bps_usd", "depth_25bps_usd", "slippage_p50",
              "slippage_p90", "dynamic_cost_pct", "target_cost_multiple", "net_rr",
              "volume_ratio_forming", "completed_bars"):
        assert k in m
    assert m["dollar_vol_30d_median"] is not None                 # available once daily is supplied
    assert m["dollar_vol_24h"] == 1_000_000.0
    assert m["volume_ratio"] is not None and m["spread_pct"] is not None
    # no daily -> 30d median fails closed to None
    assert L.compute_pair_liquidity("X", _c5(), _book(), 100.0, None)["dollar_vol_30d_median"] is None
