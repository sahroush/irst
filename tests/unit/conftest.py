"""Shared fixtures: fake wire data matching verified live API shapes."""

from __future__ import annotations

from datetime import date
from uuid import UUID

import pytest

from iran_market.config import Settings

DAY = date(2026, 9, 23)  # verified live trading day (Sat in Iranian week)
INS_CODE = "46348559193224090"
INS_ID = UUID("00000000-0000-0000-0000-000000000001")


def tsetmc_row(h_even: int, n_tran: int, price: float = 3350.0, volume: int = 1000, canceled: int = 0) -> dict:
    return {
        "insCode": None, "dEven": 0, "nTran": n_tran, "hEven": h_even,
        "qTitTran": volume, "pTran": price, "qTitNgJ": 0, "iSensVarP": "\u0000",
        "pPhSeaCotJ": 0.0, "pPbSeaCotJ": 0.0, "iAnuTran": 0, "xqVarPJDrPRf": 0.0,
        "canceled": canceled,
    }


@pytest.fixture
def settings():
    return Settings.for_tests()


@pytest.fixture
def day():
    return DAY


@pytest.fixture
def ins_id():
    return INS_ID
