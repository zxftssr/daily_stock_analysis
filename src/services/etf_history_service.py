# -*- coding: utf-8 -*-
"""Shared ETF history metrics and local SQLite warmup orchestration."""

from __future__ import annotations

import logging
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FuturesTimeout, as_completed
from dataclasses import dataclass
from datetime import date, datetime
from threading import Lock, Thread
from typing import Any, Callable, Iterable, Optional

from data_provider.realtime_types import safe_float
from src.data.stock_index_loader import StockIndexEntry, load_stock_index_entries
from src.services.stock_service import StockService

logger = logging.getLogger(__name__)

ETF_HISTORY_DAYS = 550
ETF_HISTORY_WARMUP_WORKERS = 4
ETF_HISTORY_WARMUP_TIMEOUT_SECONDS = 120
_ETF_HISTORY_WARMUP_LOCK = Lock()


class EtfHistoryWarmupInProgressError(RuntimeError):
    """Raised when another ETF history warmup is already running."""


@dataclass(frozen=True)
class EtfHistoryMetrics:
    drawdown_250d_pct: Optional[float]
    return_20d_pct: Optional[float]
    return_60d_pct: Optional[float]
    return_250d_pct: Optional[float]
    reliable: bool
    stale: bool
    as_of_date: Optional[date]
    source: Optional[str]
    actual_records: int
    message: Optional[str] = None
    persistence_error: Optional[str] = None

    def ranking_metrics(self) -> dict[str, Any]:
        return {
            "drawdown_250d_pct": self.drawdown_250d_pct,
            "return_20d_pct": self.return_20d_pct,
            "return_60d_pct": self.return_60d_pct,
            "return_250d_pct": self.return_250d_pct,
            "history_as_of_date": self.as_of_date.isoformat() if self.as_of_date else None,
            "history_stale": self.stale or not self.reliable,
            "_history_source": self.source,
        }


class EtfHistoryService:
    """Load ETF daily history once and derive all strategy/discovery metrics."""

    def __init__(self, stock_service: Optional[StockService] = None):
        self.stock_service = stock_service or StockService()

    def get_metrics(
        self,
        symbol: str,
        *,
        force_refresh: bool = False,
        allow_network: bool = True,
    ) -> EtfHistoryMetrics:
        kwargs: dict[str, Any] = {"days": ETF_HISTORY_DAYS}
        if force_refresh:
            kwargs["force_refresh"] = True
        if not allow_network:
            kwargs["allow_network"] = False
        payload = self.stock_service.get_history_data(symbol, **kwargs)
        return self.calculate_metrics(symbol, payload)

    @classmethod
    def calculate_metrics(cls, symbol: str, payload: dict[str, Any]) -> EtfHistoryMetrics:
        rows = list(payload.get("data") or [])
        closes = [
            value
            for row in rows
            if (value := cls._positive(row.get("close"))) is not None
        ]
        recent_rows = rows[-250:]
        latest = cls._positive(recent_rows[-1].get("close")) if recent_rows else None
        highs = [
            value
            for row in recent_rows
            if (value := cls._positive(row.get("high")) or cls._positive(row.get("close"))) is not None
        ]
        peak = max(highs) if len(recent_rows) >= 250 and highs else None
        drawdown = (
            round(max(0.0, (peak - latest) / peak * 100.0), 4)
            if peak is not None and latest is not None
            else None
        )
        metrics = {
            "drawdown_250d_pct": drawdown,
            "return_20d_pct": cls._period_return(closes, 20),
            "return_60d_pct": cls._period_return(closes, 60),
            "return_250d_pct": cls._period_return(closes, 250),
        }

        observed_date = cls._parse_date(payload.get("as_of_date"))
        expected_date = cls._expected_date(symbol)
        actual_records = int(payload.get("actual_records") or len(rows))
        stale = bool(
            payload.get("stale") is True
            or (payload.get("partial_cache") is True and actual_records < 251)
        )
        if observed_date is None or expected_date is None or observed_date < expected_date:
            stale = True
        reliable = not stale and drawdown is not None

        return EtfHistoryMetrics(
            **metrics,
            reliable=reliable,
            stale=stale,
            as_of_date=observed_date,
            source=str(payload.get("source") or "") or None,
            actual_records=actual_records,
            message=str(payload.get("message") or "") or None,
            persistence_error=str(payload.get("persistence_error") or "") or None,
        )

    @staticmethod
    def _positive(value: Any) -> Optional[float]:
        parsed = safe_float(value)
        return parsed if parsed is not None and parsed > 0 else None

    @staticmethod
    def _period_return(closes: list[float], periods: int) -> Optional[float]:
        if len(closes) <= periods or closes[-periods - 1] <= 0:
            return None
        return round((closes[-1] / closes[-periods - 1] - 1) * 100.0, 4)

    @staticmethod
    def _parse_date(value: Any) -> Optional[date]:
        try:
            return date.fromisoformat(str(value or "")[:10])
        except ValueError:
            return None

    @staticmethod
    def _expected_date(symbol: str) -> Optional[date]:
        try:
            from data_provider.base import normalize_stock_code
            from src.core.trading_calendar import get_effective_trading_date, get_market_for_stock

            normalized = normalize_stock_code(symbol)
            return get_effective_trading_date(get_market_for_stock(normalized))
        except Exception as exc:
            logger.debug("ETF history expected-date resolution failed for %s: %s", symbol, exc)
            return None


def _active_etf_entries(
    entries: Optional[Iterable[StockIndexEntry]] = None,
) -> tuple[StockIndexEntry, ...]:
    source = entries if entries is not None else load_stock_index_entries()
    return tuple(entry for entry in source if entry.active and entry.asset_type == "etf")


def warm_etf_history_pool(
    *,
    force_refresh: bool = False,
    entries: Optional[Iterable[StockIndexEntry]] = None,
    stock_service: Optional[StockService] = None,
    timeout_seconds: float = ETF_HISTORY_WARMUP_TIMEOUT_SECONDS,
    on_late_completion: Optional[Callable[[], None]] = None,
) -> dict[str, Any]:
    """Warm the curated ETF pool without allowing one symbol failure to abort the batch."""
    if not _ETF_HISTORY_WARMUP_LOCK.acquire(blocking=False):
        raise EtfHistoryWarmupInProgressError("ETF 历史行情预热正在进行")
    release_lock = True
    try:
        pool = _active_etf_entries(entries)
        started_at = datetime.now()
        if not pool:
            return {
                "status": "unavailable",
                "started_at": started_at.isoformat(),
                "completed_at": datetime.now().isoformat(),
                "total": 0,
                "succeeded": 0,
                "stale": 0,
                "failed": 0,
                "items": [],
            }

        metric_service = EtfHistoryService(stock_service=stock_service)
        executor = ThreadPoolExecutor(max_workers=ETF_HISTORY_WARMUP_WORKERS)
        future_to_entry: dict[Future[EtfHistoryMetrics], StockIndexEntry] = {
            executor.submit(
                metric_service.get_metrics,
                entry.display_code,
                force_refresh=force_refresh,
            ): entry
            for entry in pool
        }
        results: dict[str, dict[str, Any]] = {}

        def collect(future: Future[EtfHistoryMetrics], entry: StockIndexEntry) -> None:
            try:
                metrics = future.result()
                if metrics.persistence_error:
                    results[entry.canonical_code] = {
                        "code": entry.canonical_code,
                        "name": entry.name_zh,
                        "status": "error",
                        "source": metrics.source,
                        "as_of_date": metrics.as_of_date.isoformat() if metrics.as_of_date else None,
                        "actual_records": metrics.actual_records,
                        "drawdown_250d_pct": metrics.drawdown_250d_pct,
                        "message": f"SQLite 写入失败: {metrics.persistence_error}",
                    }
                    return
                has_metrics = metrics.drawdown_250d_pct is not None
                status = "ok" if metrics.reliable else "stale" if has_metrics else "unavailable"
                results[entry.canonical_code] = {
                    "code": entry.canonical_code,
                    "name": entry.name_zh,
                    "status": status,
                    "source": metrics.source,
                    "as_of_date": metrics.as_of_date.isoformat() if metrics.as_of_date else None,
                    "actual_records": metrics.actual_records,
                    "drawdown_250d_pct": metrics.drawdown_250d_pct,
                    "message": metrics.message,
                }
            except Exception as exc:
                logger.warning("ETF history warmup failed for %s: %s", entry.display_code, exc)
                results[entry.canonical_code] = {
                    "code": entry.canonical_code,
                    "name": entry.name_zh,
                    "status": "error",
                    "message": str(exc),
                }

        timed_out = False
        try:
            for future in as_completed(future_to_entry, timeout=max(0.01, timeout_seconds)):
                collect(future, future_to_entry[future])
        except FuturesTimeout:
            timed_out = True
            logger.warning(
                "ETF history warmup exceeded %.1fs; returning timeout results for unfinished symbols",
                timeout_seconds,
            )
        unfinished: list[Future[EtfHistoryMetrics]] = []
        if timed_out:
            for future, entry in future_to_entry.items():
                if entry.canonical_code in results:
                    continue
                if future.done():
                    collect(future, entry)
                    continue
                unfinished.append(future)
                future.cancel()
                results[entry.canonical_code] = {
                    "code": entry.canonical_code,
                    "name": entry.name_zh,
                    "status": "timeout",
                    "message": f"预热超过批次时限 {timeout_seconds:g} 秒",
                }
        if unfinished:
            def finish_late_providers() -> None:
                try:
                    executor.shutdown(wait=True, cancel_futures=True)
                    if on_late_completion is not None:
                        on_late_completion()
                except Exception as exc:
                    logger.warning("ETF history late-completion cleanup failed: %s", exc)
                finally:
                    _ETF_HISTORY_WARMUP_LOCK.release()

            release_lock = False
            Thread(
                target=finish_late_providers,
                name="etf-history-warmup-cleanup",
                daemon=True,
            ).start()
        else:
            executor.shutdown(wait=True, cancel_futures=False)

        ordered = [results[entry.canonical_code] for entry in pool]
        succeeded = sum(item["status"] == "ok" for item in ordered)
        stale = sum(item["status"] == "stale" for item in ordered)
        failed = len(ordered) - succeeded - stale
        if succeeded == len(ordered):
            status = "ok"
        elif succeeded or stale:
            status = "partial"
        else:
            status = "unavailable"
        return {
            "status": status,
            "started_at": started_at.isoformat(),
            "completed_at": datetime.now().isoformat(),
            "total": len(ordered),
            "succeeded": succeeded,
            "stale": stale,
            "failed": failed,
            "items": ordered,
        }
    finally:
        if release_lock:
            _ETF_HISTORY_WARMUP_LOCK.release()
