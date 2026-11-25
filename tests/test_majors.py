from datetime import timedelta

from bot import engine, majors


def series(trend):
    base = 100.0
    out = []
    for i in range(260):
        base *= (1.002 if trend == "up" else 0.998)
        out.append((i * 3600, base))
    return out


def test_trend_buys_in_uptrend_and_sells_when_it_turns():
    now = engine.now_utc()
    st = majors.new_state(now, majors.TREND_EPOCH, "trend")
    majors.trend_cycle(st, now, closes_fn=lambda c: series("up"))
    assert len(st["positions"]) == 3 and st["balance"] < 0.02
    majors.trend_cycle(st, now + timedelta(hours=1), closes_fn=lambda c: series("down"))
    assert not st["positions"] and len(st["closed"]) == 3
    assert all(t["reason"] == "η τάση γύρισε" for t in st["closed"])


def test_funding_accrues_hourly_and_never_double_counts():
    now = engine.now_utc().replace(minute=5)
    st = majors.new_state(now, majors.FUND_EPOCH, "funding")
    rates = lambda: {c: (0.0001, "hyperliquid") for c in majors.COINS}
    majors.funding_cycle(st, now, rates_fn=rates, price_fn=lambda c: 100.0)
    start = majors.equity(st)
    assert 99.8 < start < 100
    majors.funding_cycle(st, now + timedelta(minutes=20), rates_fn=rates, price_fn=lambda c: 100.0)
    assert majors.equity(st) == start      # ίδια ώρα: τίποτα
    majors.funding_cycle(st, now + timedelta(hours=1), rates_fn=rates, price_fn=lambda c: 100.0)
    gain = majors.equity(st) - start
    assert abs(gain - 3 * (majors.SLOT / 2) * 0.0001) < 1e-6
