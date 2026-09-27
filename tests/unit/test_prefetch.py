from datetime import date
from types import SimpleNamespace

from iran_market.models import Instrument
from iran_market.services.prefetch import PrefetchService
from iran_market.sessions import TradingCalendar


class Repo:
    def __init__(self):
        self.a = Instrument(symbol="A", ins_code="1", active=True)
        self.b = Instrument(symbol="B", ins_code=None, active=True)
        self.c = Instrument(symbol="C", ins_code="3", active=True)

    def list_active(self):
        return [self.a, self.b, self.c]

    def get_by_symbol(self, symbol):
        return [x for x in [self.a, self.b, self.c] if x.symbol == symbol]


def service():
    c = SimpleNamespace(instruments_repo=Repo(), calendar=TradingCalendar())
    return PrefetchService(c)


def test_prefetch_selects_all_mapped_active():
    selected, skipped = service()._select_instruments(symbols=None, include_inactive=False)
    assert [x.symbol for x in selected] == ["A", "C"]
    assert skipped == 1


def test_prefetch_symbol_subset_and_candidate_sessions():
    svc = service()
    selected, skipped = svc._select_instruments(symbols=["B", "C"], include_inactive=False)
    assert [x.symbol for x in selected] == ["C"]
    assert skipped == 1

    # Wed 2026-09-23 is open in the configured calendar; Thu/Fri are closed.
    days = svc._candidate_days(sessions=2, start=date(2026, 9, 23), end=date(2026, 9, 26))
    assert days == [date(2026, 9, 23), date(2026, 9, 26)]
