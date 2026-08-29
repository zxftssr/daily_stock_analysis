# -*- coding: utf-8 -*-
from __future__ import annotations

from concurrent.futures import TimeoutError as FuturesTimeout
from threading import Event, Thread
from time import monotonic
from unittest.mock import patch

import pytest

from src.data.stock_index_loader import StockIndexEntry
from src.services.etf_history_service import (
    EtfHistoryService,
    EtfHistoryWarmupInProgressError,
    warm_etf_history_pool,
)


def _entry(code: str, name: str) -> StockIndexEntry:
    suffix = "SH" if code.startswith(("5", "6")) else "SZ"
    return StockIndexEntry(
        canonical_code=f"{code}.{suffix}",
        display_code=code,
        name_zh=name,
        market="CN",
        asset_type="etf",
        active=True,
    )


def _history_payload(*, stale: bool = False) -> dict:
    rows = [
        {
            "date": f"2025-{(index // 28) + 1:02d}-{(index % 28) + 1:02d}",
            "high": 100.0,
            "close": 100.0,
        }
        for index in range(250)
    ]
    rows.append({"date": "2099-12-31", "high": 90.0, "close": 80.0})
    return {
        "source": "unit-test",
        "stale": stale,
        "partial_cache": False,
        "as_of_date": "2099-12-31",
        "actual_records": len(rows),
        "data": rows,
    }


class _StockServiceStub:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    def get_history_data(self, symbol: str, **kwargs):
        self.calls.append((symbol, bool(kwargs.get("force_refresh"))))
        if symbol == "510500":
            raise RuntimeError("provider unavailable")
        return _history_payload()


def test_stale_cache_keeps_metrics_but_is_not_strategy_reliable():
    result = EtfHistoryService.calculate_metrics("510300", _history_payload(stale=True))

    assert result.drawdown_250d_pct == 20.0
    assert result.stale is True
    assert result.reliable is False
    assert result.ranking_metrics()["history_stale"] is True


def test_current_251_bar_cache_is_reliable_even_when_calendar_window_is_partial():
    payload = _history_payload()
    payload["partial_cache"] = True

    result = EtfHistoryService.calculate_metrics("510300", payload)

    assert result.actual_records == 251
    assert result.drawdown_250d_pct == 20.0
    assert result.reliable is True
    assert result.stale is False


def test_latest_price_updates_drawdown_without_rewriting_daily_history():
    result = EtfHistoryService.calculate_metrics(
        "510300",
        _history_payload(),
        latest_price=75,
    )

    assert result.drawdown_250d_pct == 25.0
    assert result.return_20d_pct == -20.0
    assert result.reliable is True


def test_warmup_isolates_symbol_failure_and_forwards_force_refresh():
    stock_service = _StockServiceStub()
    result = warm_etf_history_pool(
        force_refresh=True,
        entries=[_entry("510300", "沪深300ETF"), _entry("510500", "中证500ETF")],
        stock_service=stock_service,
        timeout_seconds=2,
    )

    assert result["status"] == "partial"
    assert result["total"] == 2
    assert result["succeeded"] == 1
    assert result["failed"] == 1
    assert [item["status"] for item in result["items"]] == ["ok", "error"]
    assert sorted(stock_service.calls) == [("510300", True), ("510500", True)]


def test_warmup_timeout_returns_promptly_and_releases_lock_after_late_provider():
    provider_started = Event()
    release_provider = Event()
    late_completion = Event()

    class SlowStockService:
        def get_history_data(self, symbol: str, **kwargs):
            provider_started.set()
            assert release_provider.wait(timeout=2)
            return _history_payload()

    started = monotonic()
    result = warm_etf_history_pool(
        entries=[_entry("510300", "沪深300ETF")],
        stock_service=SlowStockService(),
        timeout_seconds=0.01,
        on_late_completion=late_completion.set,
    )

    assert provider_started.is_set()
    assert monotonic() - started < 0.2
    assert result["status"] == "unavailable"
    assert result["items"][0]["status"] == "timeout"
    with pytest.raises(EtfHistoryWarmupInProgressError):
        warm_etf_history_pool(entries=[_entry("510500", "中证500ETF")])

    release_provider.set()
    assert late_completion.wait(timeout=2)


def test_warmup_collects_future_that_finishes_at_timeout_boundary():
    def timeout_after_future_finishes(futures, timeout):
        del timeout
        for future in futures:
            future.result(timeout=1)
        raise FuturesTimeout()
        yield  # pragma: no cover - keeps this function an iterator

    with patch(
        "src.services.etf_history_service.as_completed",
        timeout_after_future_finishes,
    ):
        result = warm_etf_history_pool(
            entries=[_entry("510300", "沪深300ETF")],
            stock_service=_StockServiceStub(),
            timeout_seconds=0.01,
        )

    assert result["status"] == "ok"
    assert result["succeeded"] == 1
    assert result["items"][0]["status"] == "ok"


def test_warmup_reports_sqlite_persistence_failure_as_error():
    class PersistenceFailureStockService:
        def get_history_data(self, symbol: str, **kwargs):
            return {**_history_payload(), "persistence_error": "read-only database"}

    result = warm_etf_history_pool(
        entries=[_entry("510300", "沪深300ETF")],
        stock_service=PersistenceFailureStockService(),
    )

    assert result["status"] == "unavailable"
    assert result["succeeded"] == 0
    assert result["failed"] == 1
    assert result["items"][0]["status"] == "error"
    assert "SQLite 写入失败" in result["items"][0]["message"]


def test_warmup_rejects_concurrent_batch_until_first_finishes():
    provider_started = Event()
    release_provider = Event()
    first_result: list[dict] = []

    class BlockingStockService:
        def get_history_data(self, symbol: str, **kwargs):
            provider_started.set()
            assert release_provider.wait(timeout=2)
            return _history_payload()

    first = Thread(
        target=lambda: first_result.append(
            warm_etf_history_pool(
                entries=[_entry("510300", "沪深300ETF")],
                stock_service=BlockingStockService(),
                timeout_seconds=1,
            )
        )
    )
    first.start()
    assert provider_started.wait(timeout=1)
    try:
        with pytest.raises(EtfHistoryWarmupInProgressError):
            warm_etf_history_pool(
                entries=[_entry("510500", "中证500ETF")],
                stock_service=_StockServiceStub(),
                timeout_seconds=1,
            )
    finally:
        release_provider.set()
        first.join(timeout=2)

    assert not first.is_alive()
    assert first_result[0]["status"] == "ok"
