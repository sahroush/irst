# 0.1.1 hotfix

This hotfix addresses issues found during the first real local demo.

## Fixed

- `RegistrySyncService` no longer accesses `TseWebGwProvider.MARKET_WATCH_PATH` as an instance attribute. `TseWebGwProvider.get_market_watch_items()` is now the public metadata path used by registry sync.
- Historical scanner replay now evaluates indicator state only with candles available at the target candle time; future candles cannot influence older signals.
- Project metadata now declares Python 3.10+ to match the supported syntax/runtime used by the demo host.

## Added

- `python -m iran_market.cli demo` convenience command.
- The demo command can populate the dashboard outside market hours using real TSETMC trade history, authoritative candles, and chronological Supertrend replay.
- Regression tests for registry synchronization and historical signal replay.

## Real-data demo

```bash
python3.10 -m iran_market.cli demo \
  --symbol فولاد \
  --ins-code 46348559193224090 \
  --sessions 5

python3.10 -m iran_market.cli dashboard --host 127.0.0.1 --port 8000
```

If you only want to verify registry discovery first:

```bash
python3.10 -m iran_market.cli registry-sync
python3.10 -m iran_market.cli search فولاد
```
