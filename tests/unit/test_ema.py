"""ema_series: SMA seed, recursion, None padding, edge cases."""
from __future__ import annotations

import pytest

from iran_market.indicators.ema import ema_series


def test_short_series_all_none():
    assert ema_series([1.0, 2.0], period=3) == [None, None]
    assert ema_series([], period=3) == []


def test_seed_is_sma_of_first_period():
    values = ema_series([10.0, 20.0, 30.0, 40.0], period=4)
    assert values == [None, None, None, 25.0]


def test_recursion_matches_manual_ema():
    closes = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    values = ema_series(closes, period=3)
    alpha = 2.0 / 4.0
    assert values[2] == pytest.approx(2.0)
    assert values[3] == pytest.approx(closes[3] * alpha + 2.0 * (1 - alpha))
    assert values[4] == pytest.approx(closes[4] * alpha + values[3] * (1 - alpha))
    assert values[5] == pytest.approx(closes[5] * alpha + values[4] * (1 - alpha))


def test_flat_series_is_flat():
    values = ema_series([7.0] * 10, period=5)
    assert all(v == pytest.approx(7.0) for v in values[4:])


def test_period_one_tracks_closes():
    assert ema_series([3.0, 5.0, 8.0], period=1) == [3.0, 5.0, 8.0]


def test_invalid_period():
    with pytest.raises(ValueError):
        ema_series([1.0], period=0)
