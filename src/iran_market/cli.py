"""Development CLI. Jobs here are callable by cron/systemd/etc.

Examples:
    python -m iran_market.cli test-connection
    python -m iran_market.cli search فولاد
    python -m iran_market.cli fetch-trades --ins-code 46348559193224090 --date 2026-09-23
    python -m iran_market.cli candles --ins-code 46348559193224090 --date 2026-09-23 --timeframe 1h
    python -m iran_market.cli market-watch [--json] [--symbol فولاد]
    python -m iran_market.cli backfill --ins-codes 46348559193224090 --start 2026-09-01 --end 2026-09-23
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from datetime import date

from .bootstrap import build
from .candles.models import Timeframe
from .indicators.rsi import rsi_confirms
from .logging_setup import setup_logging


def _print(obj, as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, ensure_ascii=False, default=str, indent=2))
    else:
        if isinstance(obj, dict):
            for k, v in obj.items():
                print(f"{k}: {v}")
        else:
            print(obj)


async def cmd_test_connection(args, c) -> int:
    from .timeutil import now_tehran
    day = now_tehran().date()
    snap = await c.webgw.get_market_snapshot()
    print(f"webgw market watch: HTTP OK, {len(snap)} instruments, sample={snap[0].symbol if snap else '-'}")
    trades = await c.tsetmc.get_trades("46348559193224090", day)
    print(f"tsetmc trades (46348559193224090, {day}): {len(trades)} rows (0 expected outside session days)")
    return 0


async def cmd_search(args, c) -> int:
    matches = c.instruments_repo.get_by_symbol(args.symbol)
    if not matches:
        print(f"no instrument with symbol {args.symbol!r} in registry; try market-watch --symbol")
        return 1
    for inst in matches:
        _print(inst.model_dump(mode="json"), args.json)
    return 0


async def cmd_fetch_trades(args, c) -> int:
    from .services.history import TradeHistoryCollector
    collector = TradeHistoryCollector(
        c.tsetmc, c.http_tsetmc, c.instruments_repo, c.trades_repo, c.fetch_repo,
        archive=c.archive, concurrency=c.settings.http_concurrency,
    )
    # ensure instrument registered (deterministic UUID5 ins_code placeholder ID)
    from .providers.tsetmc import _uuid_from_ins_code
    from .models import Instrument
    from .timeutil import now_tehran
    existing = c.instruments_repo.get_by_ins_code(args.ins_code)
    ins_id = existing.id if existing else c.instruments_repo.upsert(
        Instrument(
            ins_code=args.ins_code, symbol=args.symbol,
            id=_uuid_from_ins_code(args.ins_code),
            metadata={"provider": "tsetmc", "id_source": "uuid5(ins_code)"},
            updated_at=now_tehran(),
        )
    ).id
    outcome = await collector.fetch_day(args.ins_code, args.date, instrument_id=ins_id)
    print(f"Instrument: {outcome.ins_code}")
    print(f"Date: {outcome.day.isoformat()}")
    if outcome.status == "OK":
        trades = c.trades_repo.get_trades(ins_id, args.date)
        first, last = (trades[0], trades[-1]) if trades else (None, None)
        print(f"Trades: {len(trades)}")
        if first:
            print(f"First trade: ts={first.timestamp.isoformat()} seq={first.sequence} price={first.price} vol={first.volume}")
            print(f"Last trade:  ts={last.timestamp.isoformat()} seq={last.sequence} price={last.price} vol={last.volume}")
    else:
        print(f"Status: {outcome.status} error={outcome.error}")
        return 1
    return 0


async def cmd_candles(args, c) -> int:
    from .candles.service import CandleService
    inst = c.instruments_repo.get_by_ins_code(args.ins_code)
    if inst is None:
        print(f"instrument {args.ins_code} not in registry — run fetch-trades first", file=sys.stderr)
        return 1
    trades = c.trades_repo.get_trades(inst.id, args.date)
    if not trades:
        print(f"no stored trades for {args.ins_code} on {args.date} — run fetch-trades first", file=sys.stderr)
        return 1
    svc = CandleService(c.trades_repo, c.candles_repo, c.instruments_repo, c.calendar)
    candles = svc.build_and_store(inst.id, args.date, Timeframe(args.timeframe or c.settings.scanner_timeframe), trades=trades)
    if args.json:
        _print([x.model_dump(mode="json") for x in candles], True)
        return 0
    print(f"{'start':<25} {'end':<25} {'open':>8} {'high':>8} {'low':>8} {'close':>8} {'volume':>12} {'n':>6}")
    for x in candles:
        print(f"{x.start_time.isoformat():<25} {x.end_time.isoformat():<25} {x.open:>8} {x.high:>8} {x.low:>8} {x.close:>8} {x.volume:>12} {x.trade_count:>6}")
    # programmatic sanity checks
    for x in candles:
        assert x.low <= x.open <= x.high, f"OHLC violated: {x}"
        assert x.low <= x.close <= x.high, f"OHLC violated: {x}"
        assert x.volume >= 0 and x.trade_count >= 0
    starts = [x.start_time for x in candles]
    assert starts == sorted(starts), "candles not sorted"
    print(f"checks OK: {len(candles)} candles, OHLC/monotonic invariants hold", file=sys.stderr)
    return 0


async def cmd_market_watch(args, c) -> int:
    from .services.collector import MarketWatchCollector
    collector = MarketWatchCollector(
        c.webgw,
        watch_symbols={args.symbol} if args.symbol else None,
    )
    snaps = await collector.poll()
    if args.json:
        _print([s.model_dump(mode="json", exclude={"raw"}) for s in snaps], True)
    else:
        print(f"returned={len(snaps)} matched={collector.stats.instruments_matched} "
              f"ms={collector.stats.last_duration_ms}")
        for s in snaps[:args.limit]:
            print(f"{s.symbol or s.isin:<12} last={s.last_price} close={s.closing_price} "
                  f"yest={s.yesterday_price} vol={s.cumulative_volume} state={s.state}")
    return 0


async def cmd_backfill(args, c) -> int:
    from .services.backfill import BackfillService
    svc = BackfillService(
        c.tsetmc, c.http_tsetmc, c.instruments_repo, c.trades_repo, c.candles_repo,
        c.fetch_repo, concurrency=c.settings.http_concurrency,
    )
    tfs = [Timeframe(t) for t in (args.timeframes or [c.settings.scanner_timeframe])]
    summary = await svc.run(args.ins_codes, args.start, args.end, timeframes=tfs)
    _print(summary, args.json)
    return 0


async def cmd_registry_sync(args, c) -> int:
    from .registry.sync import RegistrySyncService
    report = await RegistrySyncService(c.webgw, c.legacy, c.instruments_repo).sync()
    _print(report.to_dict(), args.json)
    return 0


async def cmd_scanner_once(args, c) -> int:
    from .scanner.runner import LiveScannerRunner
    from .scanner.service import ScannerConfig
    tf = args.timeframe or c.settings.scanner_timeframe
    runner = LiveScannerRunner(c, ScannerConfig(timeframe=tf, period=args.period, multiplier=args.multiplier))
    if args.sync_registry:
        await runner.sync_registry()
    stats = await runner.once()
    _print(stats.__dict__, args.json)
    return 0


async def cmd_scanner_run(args, c) -> int:
    from .scanner.runner import LiveScannerRunner
    from .scanner.service import ScannerConfig
    tf = args.timeframe or c.settings.scanner_timeframe
    runner = LiveScannerRunner(c, ScannerConfig(timeframe=tf, period=args.period, multiplier=args.multiplier))
    print(f"scanner running: {tf} Supertrend({args.period}, {args.multiplier}); Ctrl-C to stop")
    await runner.run()
    return 0


async def cmd_signals(args, c) -> int:
    rows = c.signals_repo.get_latest(args.limit, args.scanner)
    data = []
    for s in rows:
        if not rsi_confirms(s.direction, s.payload.get("rsi"), s.payload.get("previous_rsi")):
            continue
        inst = c.instruments_repo.get(s.instrument_id)
        data.append({**s.model_dump(mode="json"), "symbol": inst.symbol if inst else None})
    _print(data, args.json)
    if not args.json:
        for x in data:
            print(f"{x.get('candle_time')} {x.get('symbol') or x.get('instrument_id')} {x.get('direction')} {x.get('signal_type')}")
    return 0


async def cmd_health(args, c) -> int:
    from .monitoring.health import HealthService
    _print(HealthService(c).report(), args.json)
    return 0


async def cmd_fetch_client_types(args, c) -> int:
    """One TSETMC call per instrument: individual/legal buy/sell split."""
    import asyncio
    from .storage.sqlite import upsert_client_types
    from .timeutil import now_tehran

    instruments = [i for i in c.instruments_repo.list_active() if i.ins_code]
    if args.limit:
        instruments = instruments[: args.limit]
    sem = asyncio.Semaphore(c.settings.http_concurrency)
    day = now_tehran().date().isoformat()
    ok = fail = 0

    async def fetch(inst):
        nonlocal ok, fail
        async with sem:
            try:
                breakdown = await c.tsetmc.get_client_types(inst.ins_code)
                upsert_client_types(c.store, breakdown, day)
                ok += 1
            except Exception as exc:
                fail += 1
                print(f"{inst.symbol or inst.ins_code}: {type(exc).__name__}: {exc}")

    await asyncio.gather(*(fetch(inst) for inst in instruments))
    print(f"client types: {ok} stored, {fail} failed (day={day})")
    return 0 if fail == 0 else 1

async def cmd_prune(args, c) -> int:
    from .storage.sqlite import prune_before
    days = args.days if args.days is not None else c.settings.retention_days
    report = prune_before(c.store, days)
    _print(report, args.json)
    if not args.json:
        total = sum(report.values())
        if total:
            print(f"pruned {total} rows across {len(report)} tables")
        else:
            print(f"nothing to prune (retention_days={days})")
    return 0


async def cmd_demo(args, c) -> int:
    """Populate the dashboard with real historical data, even outside market hours."""
    from .services.demo import DemoBootstrapService
    svc = DemoBootstrapService(c)
    try:
        report = await svc.run(
            symbol=args.symbol, ins_code=args.ins_code, sessions=args.sessions,
            timeframe=args.timeframe or c.settings.scanner_timeframe,
            period=args.period, multiplier=args.multiplier,
            sync_registry=not args.no_sync_registry,
            max_lookback_days=args.max_lookback_days,
        )
    except (ValueError, RuntimeError) as exc:
        print(f"demo failed: {exc}", file=sys.stderr)
        return 1
    _print(report.to_dict(), args.json)
    if not args.json:
        print("Dashboard is now populated with authoritative TSETMC candles.")
        print("Run: python -m iran_market.cli dashboard --host 127.0.0.1 --port 8000")
    return 0


def _prefetch_progress(done: int, total: int, outcomes, phase: str = "fetch", symbol=None) -> None:
    if phase == "replay":
        print(
            f"prefetch replay: {done}/{total} instruments ({done * 100 / total:.1f}%)"
            f"  symbol={symbol or '-'}",
            file=sys.stderr,
            flush=True,
        )
        return
    ok = sum(o.status == "OK" for o in outcomes)
    skipped = sum(o.status == "SKIPPED_DONE" for o in outcomes)
    failed = sum(o.status == "FAILED" for o in outcomes)
    print(
        f"prefetch fetch: {done}/{total} instrument-days "
        f"({done * 100 / total:.1f}%)  batch ok={ok} skipped={skipped} failed={failed}",
        file=sys.stderr,
        flush=True,
    )


async def cmd_prefetch(args, c) -> int:
    """First-run whole-universe historical bootstrap."""
    from .services.prefetch import PrefetchService
    svc = PrefetchService(c)
    try:
        report = await svc.run(
            sessions=args.sessions,
            start=args.start,
            end=args.end,
            symbols=args.symbols or None,
            timeframes=args.timeframes or [c.settings.scanner_timeframe],
            signal_timeframe=args.signal_timeframe or c.settings.scanner_timeframe,
            period=args.period,
            multiplier=args.multiplier,
            sync_registry=not args.no_sync_registry,
            replay_signals=not args.no_signals,
            batch_size=args.batch_size,
            progress=(None if args.json else _prefetch_progress),
        )
    except (ValueError, RuntimeError) as exc:
        print(f"prefetch failed: {exc}", file=sys.stderr)
        return 1
    _print(report.to_dict(), args.json)
    if not args.json:
        print("Prefetch complete. Dashboard can now be used even while the market is closed.")
    return 0


async def cmd_dashboard(args, c) -> int:
    import uvicorn
    # uvicorn owns the web loop; run programmatically inside current async context.
    from .dashboard.app import create_app
    config = uvicorn.Config(create_app(c), host=args.host, port=args.port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()
    return 0


async def cmd_serve(args, c) -> int:
    """All-in-one: catch-up -> scanner + auto-reconcile + dashboard, one process."""
    import uvicorn
    from .dashboard.app import create_app
    from .runtime import MarketRuntime

    tf = args.timeframe or c.settings.scanner_timeframe
    runtime = MarketRuntime(
        c, timeframe=tf, period=args.period, multiplier=args.multiplier,
        catch_up_sessions=args.sessions,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover (Windows)
            pass

    print(f"serve: tf={tf} Supertrend({args.period}, {args.multiplier}) "
          f"sessions={args.sessions} dashboard=:{args.port}")

    async def _scanner():
        await runtime.run_with_catch_up(stop, interval=c.settings.market_watch_interval_seconds)

    async def _web():
        app = create_app(c)
        config = uvicorn.Config(app, host=args.host, port=args.port, log_level="info")
        server = uvicorn.Server(config)
        print("dashboard ready: http://%s:%d" % (args.host, args.port))
        await server.serve()

    done, pending = await asyncio.wait(
        {asyncio.create_task(_scanner()), asyncio.create_task(_web())},
        return_when=asyncio.FIRST_COMPLETED,
    )
    stop.set()
    for t in pending:
        t.cancel()
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass
    rc = 0
    for t in done:
        if not t.cancelled() and t.exception() is not None:
            print(f"serve: task failed: {t.exception()!r}", file=sys.stderr)
            rc = 1
    return rc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="iran_market.cli", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true", help="JSON output")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("test-connection").set_defaults(func=cmd_test_connection)

    p = sub.add_parser("search")
    p.add_argument("symbol")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("fetch-trades")
    p.add_argument("--ins-code", required=True)
    p.add_argument("--date", required=True, type=date.fromisoformat)
    p.add_argument("--symbol", default=None, help="optional symbol stored in registry")
    p.set_defaults(func=cmd_fetch_trades)

    p = sub.add_parser("candles")
    p.add_argument("--ins-code", required=True)
    p.add_argument("--date", required=True, type=date.fromisoformat)
    p.add_argument("--timeframe", default=None, choices=["1m", "5m", "15m", "30m", "1h", "1d"])
    p.set_defaults(func=cmd_candles)

    p = sub.add_parser("market-watch")
    p.add_argument("--symbol", default=None, help="filter to one symbol")
    p.add_argument("--limit", type=int, default=15)
    p.set_defaults(func=cmd_market_watch)

    p = sub.add_parser("backfill")
    p.add_argument("--ins-codes", nargs="+", required=True)
    p.add_argument("--start", required=True, type=date.fromisoformat)
    p.add_argument("--end", required=True, type=date.fromisoformat)
    p.add_argument("--timeframes", nargs="+", default=None,
                   choices=["1m", "5m", "15m", "30m", "1h", "1d"])
    p.set_defaults(func=cmd_backfill)

    sub.add_parser("registry-sync").set_defaults(func=cmd_registry_sync)

    for name, func in (("scanner-once", cmd_scanner_once), ("scanner-run", cmd_scanner_run)):
        p = sub.add_parser(name)
        p.add_argument("--timeframe", default=None, choices=["1m","5m","15m","30m","1h","1d"])
        p.add_argument("--period", type=int, default=10)
        p.add_argument("--multiplier", type=float, default=3.0)
        p.add_argument("--sync-registry", action="store_true")
        p.set_defaults(func=func)

    p = sub.add_parser("signals")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--scanner", default=None)
    p.set_defaults(func=cmd_signals)

    sub.add_parser("health").set_defaults(func=cmd_health)

    p = sub.add_parser("fetch-client-types",
                        help="store individual/legal buy/sell split for active instruments")
    p.add_argument("--limit", type=int, default=0, help="first N instruments only (0 = all)")
    p.set_defaults(func=cmd_fetch_client_types)

    p = sub.add_parser("prune", help="delete rows older than RETENTION_DAYS, then VACUUM")
    p.add_argument("--days", type=int, default=None,
                   help="override RETENTION_DAYS setting (0 to disable)")
    p.set_defaults(func=cmd_prune)

    p = sub.add_parser("demo", help="populate dashboard from real TSETMC history")
    p.add_argument("--symbol", default="فولاد")
    p.add_argument("--ins-code", default=None, help="optional TSETMC InsCode override")
    p.add_argument("--sessions", type=int, default=5, help="non-empty sessions to backfill")
    p.add_argument("--max-lookback-days", type=int, default=30)
    p.add_argument("--timeframe", default=None, choices=["1m","5m","15m","30m","1h"])
    p.add_argument("--period", type=int, default=10)
    p.add_argument("--multiplier", type=float, default=3.0)
    p.add_argument("--no-sync-registry", action="store_true")
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("prefetch", help="bootstrap historical candles/signals for first run")
    p.add_argument("--sessions", type=int, default=30,
                   help="recent configured trading sessions when --start is omitted (default: 30)")
    p.add_argument("--start", type=date.fromisoformat, default=None,
                   help="explicit historical start YYYY-MM-DD; overrides --sessions")
    p.add_argument("--end", type=date.fromisoformat, default=None,
                   help="historical end YYYY-MM-DD (default: today)")
    p.add_argument("--symbols", nargs="*", default=None,
                   help="optional symbol subset; default is every active TSETMC-mapped instrument")
    p.add_argument("--timeframes", nargs="+", default=None,
                   choices=["1m", "5m", "15m", "30m", "1h", "1d"])
    p.add_argument("--signal-timeframe", default=None, choices=["1m", "5m", "15m", "30m", "1h", "1d"])
    p.add_argument("--period", type=int, default=10)
    p.add_argument("--multiplier", type=float, default=3.0)
    p.add_argument("--no-signals", action="store_true", help="skip Supertrend replay")
    p.add_argument("--batch-size", type=int, default=250,
                   help="max instrument-day tasks created per batch (default: 250)")
    p.add_argument("--no-sync-registry", action="store_true")
    p.set_defaults(func=cmd_prefetch)

    p = sub.add_parser("dashboard")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(func=cmd_dashboard)

    p = sub.add_parser("serve", help="all-in-one: catch-up + live scanner + auto-reconcile + dashboard")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--timeframe", default=None, choices=["1m", "5m", "15m", "30m", "1h", "1d"])
    p.add_argument("--period", type=int, default=10)
    p.add_argument("--multiplier", type=float, default=3.0)
    p.add_argument("--sessions", type=int, default=30,
                   help="catch-up history sessions on startup (default: 30)")
    p.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    setup_logging("WARNING")  # CLI stays readable; structured logs to stderr at WARN+
    c = build()

    async def _run() -> int:
        try:
            code = await args.func(args, c)
            return code
        finally:
            await c.aclose()

    try:
        return asyncio.run(_run())
    finally:
        try:
            c.store.connect().close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
