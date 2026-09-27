# MVP Operations Runbook

## Recommended process layout

For an initial internal deployment, run two processes against the same SQLite database:

```text
Process A: iran_market.cli scanner-run
Process B: iran_market.cli dashboard
```

SQLite WAL mode is enabled by the storage layer and is sufficient for this MVP workload. If write volume, concurrency, or multiple scanner workers grow materially, move the repository implementation to PostgreSQL before horizontally scaling the scanner.

## Immediate real-data demo

This path is useful after installation or when the market is closed:

```bash
python -m iran_market.cli demo \
  --symbol فولاد \
  --ins-code 46348559193224090 \
  --sessions 5
python -m iran_market.cli dashboard --host 127.0.0.1 --port 8000
```

The demo command uses authoritative TSETMC trade history rather than fabricating live snapshots, so the resulting candles are real historical OHLCV. It also replays the configured Supertrend scanner over those candles.

## Daily operation

1. Keep `scanner-run` alive during market hours.
2. Run `registry-sync` at startup and optionally once each trading day.
3. Backfill/reconcile exact trade-history data after the session for instruments being monitored.
4. Inspect `/api/health` or the dashboard for stale market data.

## Scanner startup

```bash
source .venv/bin/activate
python -m iran_market.cli registry-sync
python -m iran_market.cli scanner-run --timeframe 1h --period 10 --multiplier 3
```

A systemd service or container restart policy can supervise the process later. The application itself does not require a scheduler or broker.

## Dashboard startup

```bash
python -m iran_market.cli dashboard --host 0.0.0.0 --port 8000
```

The dashboard is read-only but has no authentication. Do not expose it directly to the public internet. Put it behind a VPN, firewall, or authenticated reverse proxy.

## Historical seeding

A Supertrend scanner needs sufficient closed candles before producing a valid state. Backfill the intended watch universe before relying on the scanner.

```bash
python -m iran_market.cli backfill \
  --ins-codes <INSCODE_1> <INSCODE_2> ... \
  --start YYYY-MM-DD \
  --end YYYY-MM-DD \
  --timeframes 1h
```

## End-of-session exactness

Live WebGW candles are intentionally provisional. After trading, TSETMC trade history should be used to build/reconcile exact candles. This matters for highs/lows and for any bucket whose first trade happened before the scanner first observed it.

The existing `CandleService.reconcile()` marks exact replacements as `RECONCILED`.

Full automatic post-session reconciliation scheduling is not part of the MVP runner yet; call the existing backfill/reconciliation services from cron/systemd if exact same-day closure is required.

## Failure modes

### WebGW stale / unavailable

- `health` becomes `DEGRADED` when no recent snapshot exists.
- Existing candles/signals remain in SQLite.
- The next successful polling cycle resumes from the last persisted cumulative counters.

### Cumulative counter reset

Negative deltas are never stored. A reset produces a zero increment for that observation rather than negative volume.

### TSETMC blocked / HTML response

The HTTP/provider layer recognizes unexpected HTML/content-type responses and enters cooldown rather than hammering the upstream service.

### Registry mapping unavailable

WebGW instruments remain valid ISIN-keyed instruments. TSETMC-specific historical operations require a real `InsCode`; missing mappings fail explicitly rather than fabricating IDs.

## Backup

For the MVP, back up:

```text
data/iran_market.db
```

and `data/raw/` if raw archival is enabled.

Use SQLite's online backup facilities or stop the processes before copying the DB file if you want a simple consistent filesystem backup.

## Fresh database bootstrap / prefetch

The live scanner is not responsible for manufacturing historical candles when
TSE is closed. On a fresh database, run the authoritative prefetch job first:

```bash
python -m iran_market.cli prefetch --sessions 30 --timeframes 1h
```

This performs registry sync, historical TSETMC trade downloads, candle building,
and Supertrend signal replay for all active instruments with a resolved InsCode.
It is safe to rerun after interruption.

Useful variants:

```bash
# Small real-data test
python -m iran_market.cli prefetch --symbols فولاد فملی خودرو --sessions 30

# Full explicit range
python -m iran_market.cli prefetch --start 2026-01-01 --end 2026-09-26

# Candles only, no scanner replay
python -m iran_market.cli prefetch --sessions 30 --no-signals
```

After prefetch:

```bash
python -m iran_market.cli dashboard --host 127.0.0.1 --port 8000
```
