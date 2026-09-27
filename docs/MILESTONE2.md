# Milestone 2 Completion Notes

Milestone 2 was resumed from a partially completed agent run and finished in this repository.

## Completed

- Persistent live snapshot state and signal repositories.
- WebGW/legacy registry synchronization with ISIN → real InsCode mapping where available.
- Corrected WebGW instrument discovery so an ISIN is never fabricated as an InsCode.
- Session calendar corrected to the default Saturday–Wednesday cash-market week.
- Live provisional candle construction from bulk market-watch snapshots.
- Positive cumulative-volume / trade-count delta handling.
- Out-of-order snapshot protection and restart state persistence.
- `BUILDING → CLOSED` transition with `CandleClosed` events.
- Wilder RMA ATR implementation.
- Configurable Supertrend implementation.
- Closed/reconciled-candle-only confirmed indicator path.
- Idempotent Supertrend flip signals.
- `IndicatorCalculated` and `SignalGenerated` internal events.
- Single-process live scanner runner using one bulk WebGW poll per cycle.
- Registry, scanner, signals, health, and dashboard CLI commands.
- Database-derived health view that distinguishes a closed market from a stale feed during an open session.
- FastAPI read-only MVP dashboard.
- README and operations documentation.

## Verified locally

- Python compile pass over `src/`.
- 42 unit tests passing.
- Dashboard root/API tested through FastAPI TestClient.
- CLI parser and `health` smoke-tested.

The execution sandbox used to finish this milestone had no outbound package index/network access, so live exchange integration tests were not re-run here. The repository already contains the earlier live API integration tests; run them on the target Iranian host with:

```bash
pytest -m integration -q
```

## Deliberately left outside the MVP

- Telegram/email/webhook alert delivery.
- Authentication / users for the dashboard.
- Automatic historical signal correction after a reconciled candle changes an old indicator state.
- PostgreSQL production repository implementation.
- Multiple strategies/indicators beyond Supertrend.
- Distributed workers or external queues.
