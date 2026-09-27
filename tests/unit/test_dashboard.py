from fastapi.testclient import TestClient
from iran_market.bootstrap import build
from iran_market.config import Settings
from iran_market.dashboard.app import create_app
from iran_market.models import Instrument


def test_dashboard_empty_and_instrument(tmp_path):
    c=build(Settings.for_tests(database_url=f"sqlite:///{tmp_path/'db.sqlite'}"))
    c.instruments_repo.upsert(Instrument(symbol="فولاد",isin="IRO1TEST0001"))
    client=TestClient(create_app(c))
    assert client.get("/").status_code==200
    h=client.get("/api/health").json(); assert h["active_instruments"]==1
    rows=client.get("/api/instruments?q=فولاد").json(); assert rows[0]["symbol"]=="فولاد"
