"""hEven parsing, timezone assignment, reverse-order handling."""

from datetime import date, datetime

import pytest

from iran_market.providers.tsetmc import TsetmcProvider
from iran_market.timeutil import heven_to_time, parse_heven
from iran_market.timeutil import TEHRAN

from .conftest import INS_CODE, INS_ID, tsetmc_row


def test_parse_heven_090102():
    assert parse_heven(90102) == (9, 1, 2)


def test_parse_heven_122959():
    assert parse_heven(122959) == (12, 29, 59)


def test_parse_heven_string_zero_fill():
    assert parse_heven("090000") == (9, 0, 0)


def test_parse_heven_invalid():
    # '001234' is legal (00:12:34, pre-open); truly invalid values raise:
    with pytest.raises(ValueError):
        parse_heven(1234567)  # 7 digits
    with pytest.raises(ValueError):
        parse_heven("12a34")


def test_heven_to_time_tehran_tz():
    ts = heven_to_time(122959).replace(year=2026, month=9, day=23)
    assert ts.tzinfo is not None
    assert ts.utcoffset().total_seconds() == 3.5 * 3600  # UTC+3:30, no DST since 2022
    assert (ts.hour, ts.minute, ts.second) == (12, 29, 59)


def test_normalize_reverse_ordered_api():
    """API returns newest-first; normalization must yield chronological ascending."""
    raw = [
        tsetmc_row(122959, 21174, 3350.0),
        tsetmc_row(122958, 21171, 3349.0),
        tsetmc_row(90102, 319, 3300.0),
    ]
    trades = TsetmcProvider._normalize(INS_CODE, date(2026, 9, 23), raw)
    assert [t.timestamp for t in trades] == sorted(t.timestamp for t in trades)
    assert trades[0].timestamp.hour == 9
    assert trades[0].sequence == 319
    assert trades[-1].sequence == 21174


def test_normalize_tehran_dates():
    raw = [tsetmc_row(122959, 1)]
    trades = TsetmcProvider._normalize(INS_CODE, date(2026, 9, 23), raw)
    ts = trades[0].timestamp
    assert ts.date() == date(2026, 9, 23)
    assert ts.tzinfo == TEHRAN


def test_normalize_canceled_excluded():
    raw = [
        tsetmc_row(100000, 1, canceled=0),
        tsetmc_row(100001, 2, canceled=1),
        tsetmc_row(100002, 3),
    ]
    trades = TsetmcProvider._normalize(INS_CODE, date(2026, 9, 23), raw)
    assert len(trades) == 2
    assert all(not t.canceled for t in trades)


def test_normalize_price_float_to_int():
    raw = [tsetmc_row(100000, 1, price=3350.0)]
    trades = TsetmcProvider._normalize(INS_CODE, date(2026, 9, 23), raw)
    assert trades[0].price == 3350 and isinstance(trades[0].price, int)
