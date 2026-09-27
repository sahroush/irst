"""RegistrySyncService: WebGW discovery + legacy InsCode mapping -> registry.

Separation: sync OWNS provider calls; the registry storage stays provider-free.

Deactivation policy (conservative): an instrument previously seen active is
marked inactive only after it is absent from `stale_after_syncs` consecutive
successful syncs (default 3). First absence never deactivates — the bulk
endpoint occasionally drops rows transiently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from ..logging_setup import get_logger
from ..models import Instrument
from ..providers.tse_webgw import PROVIDER_NAME as WEBGW_NAME, to_str
from ..storage.base import InstrumentRepository
from ..timeutil import now_tehran

log = get_logger("registry_sync")

TEHRAN = ZoneInfo("Asia/Tehran")


@dataclass
class SyncReport:
    added: int = 0
    updated: int = 0
    mapping_filled: int = 0
    deactivated: int = 0
    reactivated: int = 0
    total: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class RegistrySyncService:
    def __init__(
        self,
        webgw,  # TseWebGwProvider
        legacy,  # TsetmcLegacyProvider
        instruments_repo: InstrumentRepository,
        stale_after_syncs: int = 3,
    ) -> None:
        self._webgw = webgw
        self._legacy = legacy
        self._repo = instruments_repo
        self._stale_after = stale_after_syncs

    async def sync(self) -> SyncReport:
        """One registry sync pass. Never raises on individual row errors."""
        report = SyncReport()
        webgw_items = await self._fetch_webgw_items()
        legacy_map = await self._fetch_legacy_map()  # isin -> (ins_code, symbol, name)

        seen_isins: set[str] = set()
        for idx, row in enumerate(webgw_items):
            try:
                isin = to_str(row.get("instrumentId"))
                if not isin:
                    continue
                seen_isins.add(isin)
                legacy = legacy_map.get(isin)
                inst = self._repo.get_by_isin(isin)
                new_fields = {
                    "symbol": to_str(row.get("instrumentName")),
                    "name": to_str(row.get("companyNamePersian")),
                    "market": to_str(row.get("marketname")),
                    "instrument_type": to_str(row.get("securitiesname")),
                    "ins_code": legacy.ins_code if legacy else None,
                    "active": True,
                    "metadata": {
                        "provider": WEBGW_NAME,
                        "state": to_str(row.get("statename")),
                        "market_id": row.get("marketid"),
                        "last_seen": now_tehran().isoformat(),
                    },
                }
                if inst is None:
                    self._repo.upsert(Instrument(isin=isin, **new_fields))
                    report.added += 1
                    continue
                changed = self._differs(inst, new_fields)
                if legacy and inst.ins_code != legacy.ins_code:
                    changed = True
                    report.mapping_filled += 1
                if inst.active is False:
                    changed = True
                    report.reactivated += 1
                if changed:
                    self._repo.upsert(Instrument(
                        id=inst.id, isin=isin, **new_fields,
                        created_at=inst.created_at,
                    ))
                    report.updated += 1
            except Exception as exc:  # one bad row must not kill sync
                report.errors.append(f"row {idx}: {exc}")

        report.deactivated = self._deactivate_missing(seen_isins)
        report.total = len(seen_isins)
        log.info("registry sync complete", extra=report.to_dict())
        return report

    async def _fetch_webgw_items(self) -> list[dict]:
        return await self._webgw.get_market_watch_items()

    async def _fetch_legacy_map(self) -> dict[str, object]:
        """ISIN -> legacy row. Best-effort: empty dict on failure (graceful degradation)."""
        try:
            rows = await self._legacy.fetch_instrument_list()
        except Exception as exc:
            log.warning("legacy mapping unavailable, continuing without InsCode: %s", exc)
            return {}
        return {r.isin: r for r in rows}

    def _differs(self, existing: Instrument, new_fields: dict) -> bool:
        return any(
            getattr(existing, k) != v
            for k, v in new_fields.items()
            if k not in ("ins_code", "active", "metadata")
        )

    def _deactivate_missing(self, seen_isins: set[str]) -> int:
        """Mark instruments inactive after repeated absences (conservative)."""
        deactivated = 0
        for inst in self._repo.list_active():
            if inst.isin and inst.isin in seen_isins:
                continue
            if inst.isin is None:
                continue  # legacy-mapped-only rows: keep active, mapping pass handles
            meta = dict(inst.metadata)
            misses = int(meta.get("sync_misses", 0)) + 1
            meta["sync_misses"] = misses
            if misses >= self._stale_after:
                self._repo.upsert(Instrument(
                    **inst.model_dump(exclude={"metadata", "updated_at"}),
                    metadata={**meta, "deactivated_at": now_tehran().isoformat()},
                ))
                self._repo.set_active(inst.id, False)
                deactivated += 1
                log.info("instrument deactivated after repeated absence",
                         extra={"symbol": inst.symbol, "isin": inst.isin, "misses": misses})
            else:
                self._repo.upsert(Instrument(
                    **inst.model_dump(exclude={"metadata", "updated_at"}), metadata=meta,
                ))
        return deactivated
