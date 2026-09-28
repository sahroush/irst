# IRST — Iranian Market Data & Supertrend Scanner

IRST is a small, reusable Python toolkit for collecting Iranian market data, constructing session-aware candles, reconciling them against TSETMC trade history, scanning closed candles for Supertrend changes, and viewing the result in a local dashboard.

The current codebase is an **MVP**. It is intentionally a single-process application with SQLite and no message broker, Redis, Celery, Kubernetes, user accounts, or alerting stack.

## What is implemented

### Market data

- TSETMC exact trade-history fetching via `GetTradeHistory/{InsCode}/{YYYYMMDD}/false`.
- TSE WebGW bulk cash-market snapshots (one request for the market rather than one request per stock).
- Registry synchronization keyed by ISIN, with best-effort TSETMC `InsCode` mapping from the legacy bulk instrument list.
- Persistent fetch bookkeeping and optional raw `.json.zst` archival.

### Candles

- Exact trade-derived `1m`, `5m`, `15m`, `1h`, and `1d` candles.
- Live provisional candles from WebGW snapshots.
- Session-aware boundaries in `Asia/Tehran`.
- Candle lifecycle: `BUILDING → CLOSED → RECONCILED`.
- Cumulative market-watch counters are converted to deltas; duplicate snapshots do not create fake volume.
- Restart recovery of the last cumulative snapshot counters through `live_snapshot_state`.

### Scanner

- Deterministic Wilder RMA ATR.
- Supertrend with configurable ATR period and multiplier.
- Confirmed scanning uses only `CLOSED` / `RECONCILED` candles.
- Signals are generated only on Supertrend state changes (`bullish` / `bearish`).
- Signal persistence is idempotent.

### Dashboard

A read-only FastAPI dashboard showing:

- scanner/database health;
- active instrument count;
- live BUILDING candle count;
- latest market snapshot age;
- searchable instrument watchlist;
- latest scanner-timeframe candle per instrument;
- recent Supertrend flips;
- selected-instrument OHLC candlesticks, volume histogram, confirmed Supertrend overlay, and signal markers;
- separate feed-observation and candle timestamps, with explicit empty/stale states.

The dashboard has no Node/frontend build step or runtime CDN dependency. It serves a pinned Lightweight Charts 4.2.3 bundle locally; upstream Apache-2.0 license is included beside the asset.

---

## Architecture

```text
TSE WebGW (bulk snapshot)
          │
          ▼
 Registry / ISIN resolution
          │
          ▼
  MarketSnapshot normalization
          │
          ▼
 LiveCandleService ────────► BUILDING candle
          │                       │
          │                 bucket/session ends
          │                       ▼
          └────────────────► CLOSED candle
                                  │
                                  ▼
                           Supertrend scanner
                                  │
                         state changed only?
                                  │ yes
                                  ▼
                              Signal DB
                                  │
                                  ▼
                              Dashboard

TSETMC trade history
          │
          ▼
 exact Trade objects
          │
          ▼
 authoritative CandleBuilder
          │
          ▼
 RECONCILED candles
```

The key boundaries are deliberate:

- **Provider:** external API behavior.
- **Normalizer/domain models:** stable internal representation.
- **Candle engine:** OHLCV construction.
- **Indicator:** mathematical state only.
- **Scanner:** detects configured state transitions.
- **Signal repository:** persistence/idempotency.
- **Dashboard:** read-only presentation.

## Requirements

- Python 3.10+
- Network access to the Iranian market endpoints from the runtime host.

Install:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Core dependencies are declared in `pyproject.toml`, including `httpx`, `pydantic`, `polars`, `zstandard`, `fastapi`, and `uvicorn`.

## Configuration

Environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `TSETMC_BASE_URL` | `https://cdn.tsetmc.com` | exact trade history host |
| `TSE_WEBGW_BASE_URL` | `https://webgw.tse.ir` | bulk market-watch host |
| `DATABASE_URL` | `sqlite:///data/iran_market.db` | SQLite database |
| `MARKET_WATCH_INTERVAL_SECONDS` | `15` | live scanner poll interval |
| `HTTP_TIMEOUT` | `30` | HTTP request timeout |
| `HTTP_CONCURRENCY` | `5` | bounded HTTP concurrency |
| `HTTP_MAX_RETRIES` | `4` | retry count |
| `RAW_ARCHIVE_ENABLED` | `0` | save compressed raw API payloads |
| `RAW_ARCHIVE_DIR` | `data/raw` | raw archive directory |
| `LOG_LEVEL` | `INFO` | application logging level |

All timestamps use `Asia/Tehran` internally.

## First-time setup

### Fastest real-data demo (works outside market hours)

If you want to verify the complete pipeline and populate the dashboard immediately, use the demo bootstrap. It syncs the registry, downloads real TSETMC trade history for several recent non-empty sessions, builds authoritative hourly candles, replays Supertrend chronologically, and persists any trend flips.

```bash
python -m iran_market.cli demo \
  --symbol فولاد \
  --ins-code 46348559193224090 \
  --sessions 5

python -m iran_market.cli dashboard --host 127.0.0.1 --port 8000
```

Then open `http://127.0.0.1:8000`. The explicit InsCode above is optional if registry sync successfully obtains the legacy InsCode↔ISIN mapping, but including it makes the فولاد demo independent of that best-effort legacy mapping endpoint.

### 1. Sync the registry

```bash
python -m iran_market.cli registry-sync
```

WebGW gives ISINs but not TSETMC InsCodes. Registry sync preserves ISIN as the canonical external join key and fills real InsCodes only where a mapping is available. It never fabricates one.

### 2. Seed historical candles

Supertrend requires history. For a newly created database, backfill the instruments you want to scan before expecting flip signals.

Example:

```bash
python -m iran_market.cli backfill \
  --ins-codes 46348559193224090 \
  --start 2026-09-01 \
  --end 2026-09-23 \
  --timeframes 1h
```

The default Supertrend is `(period=10, multiplier=3.0)`. More historical candles than the bare minimum are recommended.

### 3. Test one live polling cycle

```bash
python -m iran_market.cli scanner-once --sync-registry
```

### 4. Run the live scanner

```bash
python -m iran_market.cli scanner-run \
  --timeframe 1h \
  --period 10 \
  --multiplier 3
```

The scanner uses the WebGW bulk endpoint. It does **not** issue hundreds of requests per polling cycle.

### 5. Run the dashboard

In another terminal:

```bash
python -m iran_market.cli dashboard --host 127.0.0.1 --port 8000
```

Open:

```text
http://127.0.0.1:8000
```

For LAN access, use `--host 0.0.0.0` and secure access at the network/reverse-proxy layer.

## Useful CLI commands

```bash
# APIs / connection
python -m iran_market.cli test-connection
python -m iran_market.cli market-watch --limit 20

# Registry
python -m iran_market.cli registry-sync
python -m iran_market.cli search فولاد

# Exact trades and candles
python -m iran_market.cli fetch-trades --ins-code 46348559193224090 --date 2026-09-23 --symbol فولاد
python -m iran_market.cli candles --ins-code 46348559193224090 --date 2026-09-23 --timeframe 1h

# Real-data dashboard bootstrap
python -m iran_market.cli demo --symbol فولاد --ins-code 46348559193224090 --sessions 5

# Scanner
python -m iran_market.cli scanner-once --sync-registry
python -m iran_market.cli scanner-run --timeframe 1h --period 10 --multiplier 3
python -m iran_market.cli signals --limit 50

# Operational status
python -m iran_market.cli health

# Dashboard
python -m iran_market.cli dashboard
```

Use the global `--json` flag when machine-readable CLI output is needed.

## Live candle semantics

WebGW market watch exposes session-cumulative volume and trade counts. IRST stores the last snapshot per instrument and calculates positive deltas:

```text
previous cumulative volume: 1,000,000
current cumulative volume:  1,025,000
live candle increment:          25,000
```

Important limitations of provisional candles:

1. On a fresh process/database, the first observed cumulative counter becomes the baseline, so earlier volume in the same bucket is not attributed to the live candle.
2. If the first observation of a bucket occurs after trades have already happened, its provisional `open` is the first price IRST observed, not necessarily the exchange's true first trade.
3. Snapshot sampling can miss intrapoll high/low prints.

These are expected properties of a snapshot-derived provisional candle. TSETMC trade-history reconciliation exists to produce exact OHLCV later.

## Supertrend semantics

- Default ATR period: `10`.
- Default multiplier: `3.0`.
- ATR uses Wilder's RMA/SMMA seeding and smoothing.
- `BUILDING` candles are ignored for confirmed signals.
- A signal is stored only when the state changes:
  - `BEARISH → BULLISH`
  - `BULLISH → BEARISH`
- Re-running the same closed candle does not create a duplicate signal.

## Health semantics

`python -m iran_market.cli health` and `/api/health` report database-derived state including:

- active instruments;
- BUILDING candles;
- total persisted signals;
- last snapshot state timestamp;
- snapshot age;
- last closed/reconciled candle;
- last successful historical fetch.

`serve` polls WebGW during and after the configured market session. `HEALTHY` requires a live snapshot within `max(120 seconds, 2 × MARKET_WATCH_INTERVAL_SECONDS)`; otherwise health is `DEGRADED`. An empty registry reports `UNHEALTHY`.

## Testing

Normal unit suite:

```bash
pytest tests/unit -q
```

Live API integration tests are marked separately:

```bash
pytest -m integration -q
```

They require network access and should not be part of an offline CI run.

## Current MVP limitations

- SQLite only for the wired application container (repository interfaces allow PostgreSQL later).
- No authentication or multi-user dashboard.
- No Telegram/email/webhook alerts yet.
- No automatic correction/retraction of a previously emitted signal after a later candle reconciliation changes historical values.
- The WebGW live path is snapshot-derived and therefore provisional by design.
- Trading holidays/special sessions still require explicit calendar configuration or a future reliable exchange-status adapter.
- External Iranian exchange APIs are not formal stability contracts; upstream schema/availability may change.

## Project structure

```text
src/iran_market/
├── providers/       # TSETMC + TSE WebGW adapters
├── registry/        # canonical instrument registry and synchronization
├── services/        # historical/live collection and candle orchestration
├── candles/         # generic exact candle builder
├── indicators/      # generic indicators + Supertrend
├── scanner/         # scanner config, signals, live runner
├── storage/         # repository contracts + SQLite + archive
├── monitoring/      # health state
├── dashboard/       # FastAPI read-only UI/API
├── models.py        # normalized domain models
├── sessions.py      # Tehran trading-session boundaries
└── cli.py
```

See [`docs/OPERATIONS.md`](docs/OPERATIONS.md) for the recommended MVP runbook.

## First-run historical prefetch

A fresh installation can be populated while the exchange is closed. The prefetch
job synchronizes the registry, selects every active instrument that has a real
TSETMC `InsCode`, downloads authoritative historical trade sessions, builds
candles, and replays Supertrend chronologically into the signal store.

Recommended first run for the whole market:

```bash
python -m iran_market.cli prefetch --sessions 30 --timeframes 1h
```

For a quick smoke test first:

```bash
python -m iran_market.cli prefetch --symbols فولاد فملی خودرو --sessions 30
```

For an explicit larger history range:

```bash
python -m iran_market.cli prefetch \
  --start 2026-01-01 \
  --end 2026-09-26 \
  --timeframes 15m 1h
```

`prefetch` is resumable. Successful instrument/date fetches are recorded in
`fetch_runs`; rerunning the same command skips their network download while still
rebuilding missing candles if an earlier run was interrupted. Signals use
idempotent keys, so replay does not duplicate existing flips.

For hundreds of instruments, start with 25-30 sessions rather than years of raw
trade history. Thirty sessions supplies roughly 120 hourly bars for a normal
09:00-12:30 session, enough for the default Supertrend history window and a useful
dashboard. Use `--start/--end` only when deeper history is actually required.
