# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import get_database_manager
from api.v1.endpoints.backtest import run_etf_crash_backtest
from api.v1.schemas.backtest import EtfCrashBacktestRequest
from src.data.stock_index_loader import StockIndexEntry
from src.services.etf_crash_backtest_service import EtfCrashBacktestService


class _FakeDb:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def get_data_range(self, code, start_date, end_date):
        self.calls.append((code, start_date, end_date))
        return [row for row in self.rows if start_date <= row.date <= end_date]


class _CandidateDb:
    def __init__(self, rows_by_code):
        self.rows_by_code = rows_by_code

    def get_data_range(self, code, start_date, end_date):
        return [
            row for row in self.rows_by_code.get(code, [])
            if start_date <= row.date <= end_date
        ]


def _entry() -> StockIndexEntry:
    return StockIndexEntry(
        canonical_code="510300.SH",
        display_code="510300",
        name_zh="沪深300ETF",
        market="CN",
        asset_type="etf",
        active=True,
        etf_category="broad_market",
        benchmark_code="000300.SH",
        benchmark_name="沪深300",
    )


def _rows():
    first = date(2024, 1, 1)
    closes = [100.0] * 250 + [90.0, 80.0, 70.0]
    return [
        SimpleNamespace(date=first + timedelta(days=index), high=100.0, close=close)
        for index, close in enumerate(closes)
    ]


def _run(service: EtfCrashBacktestService):
    rows = _rows()
    return service.run(
        symbol="510300",
        start_date=rows[249].date,
        end_date=rows[-1].date,
        initial_capital=100000,
        stages=[
            {"drawdown_pct": 10, "target_position_pct": 20},
            {"drawdown_pct": 20, "target_position_pct": 50},
            {"drawdown_pct": 30, "target_position_pct": 80},
        ],
    )


def test_staged_drawdown_backtest_triggers_cumulative_target_positions():
    result = _run(EtfCrashBacktestService(_FakeDb(_rows()), [_entry()]))

    assert result["symbol"] == "510300"
    assert result["effective_start_date"] == _rows()[249].date.isoformat()
    assert result["trading_days"] == 4
    assert result["trigger_count"] == 3
    assert result["triggered_stage_count"] == 3
    assert [trade["threshold_pct"] for trade in result["trades"]] == [10.0, 20.0, 30.0]
    assert result["max_position_pct"] <= 80.0
    assert result["cash_remaining"] > 0
    assert result["total_return_pct"] < 0
    assert result["buy_hold_return_pct"] == -30.0
    assert result["capital_utilization_pct"] > 0


def test_backtest_rejects_non_monotonic_stages():
    service = EtfCrashBacktestService(_FakeDb(_rows()), [_entry()])
    with pytest.raises(ValueError, match="回撤阈值必须严格递增"):
        service.run(
            symbol="510300",
            start_date=_rows()[249].date,
            end_date=_rows()[-1].date,
            initial_capital=100000,
            stages=[
                {"drawdown_pct": 20, "target_position_pct": 20},
                {"drawdown_pct": 10, "target_position_pct": 50},
            ],
        )


def test_backtest_requires_local_history():
    service = EtfCrashBacktestService(_FakeDb([]), [_entry()])
    with pytest.raises(ValueError, match="请先执行历史行情预热"):
        service.run(
            symbol="510300",
            start_date=date(2025, 1, 1),
            end_date=date(2026, 1, 1),
            initial_capital=100000,
            stages=[{"drawdown_pct": 10, "target_position_pct": 20}],
        )


def test_backtest_prefers_latest_candidate_over_older_candidate_with_more_rows():
    first = date(2024, 1, 1)
    canonical_rows = [
        SimpleNamespace(date=first + timedelta(days=index), high=100.0, close=100.0)
        for index in range(300)
    ]
    display_rows = [
        SimpleNamespace(date=first + timedelta(days=index + 80), high=100.0, close=100.0)
        for index in range(260)
    ]
    service = EtfCrashBacktestService(
        _CandidateDb({"510300.SH": canonical_rows, "510300": display_rows}),
        [_entry()],
    )

    bars, storage_code = service._load_bars(_entry(), first, first + timedelta(days=400))

    assert storage_code == "510300"
    assert bars[-1].date == display_rows[-1].date


def test_backtest_prefers_complete_history_over_newer_incomplete_alias():
    first = date(2024, 1, 1)
    complete_rows = [
        SimpleNamespace(date=first + timedelta(days=index), high=100.0, close=100.0)
        for index in range(300)
    ]
    incomplete_rows = [
        SimpleNamespace(date=first + timedelta(days=index + 200), high=100.0, close=100.0)
        for index in range(200)
    ]
    service = EtfCrashBacktestService(
        _CandidateDb({"510300.SH": complete_rows, "510300": incomplete_rows}),
        [_entry()],
    )

    bars, storage_code = service._load_bars(_entry(), first + timedelta(days=249), first + timedelta(days=420))

    assert storage_code == "510300.SH"
    assert len(bars) == 300


def test_no_trigger_wait_metrics_cover_the_full_simulation():
    rows = _rows()
    service = EtfCrashBacktestService(_FakeDb(rows), [_entry()])
    result = service.run(
        symbol="510300",
        start_date=rows[249].date,
        end_date=rows[-1].date,
        initial_capital=100000,
        stages=[{"drawdown_pct": 50, "target_position_pct": 20}],
    )

    assert result["trigger_count"] == 0
    assert result["first_trigger_wait_trading_days"] == result["trading_days"]
    assert result["longest_wait_trading_days"] == result["trading_days"]


def test_endpoint_returns_typed_etf_crash_result():
    rows = _rows()
    request = EtfCrashBacktestRequest(
        symbol="510300",
        start_date=rows[249].date,
        end_date=rows[-1].date,
        initial_capital=100000,
        stages=[
            {"drawdown_pct": 10, "target_position_pct": 20},
            {"drawdown_pct": 20, "target_position_pct": 50},
        ],
    )

    response = run_etf_crash_backtest(request, _FakeDb(rows))

    assert response.symbol == "510300"
    assert response.trigger_count == 2
    assert response.source == "sqlite"


def test_http_endpoint_returns_etf_crash_contract():
    rows = _rows()
    app = create_app()
    app.dependency_overrides[get_database_manager] = lambda: _FakeDb(rows)
    payload = {
        "symbol": "510300",
        "start_date": rows[249].date.isoformat(),
        "end_date": rows[-1].date.isoformat(),
        "initial_capital": 100000,
        "stages": [
            {"drawdown_pct": 10, "target_position_pct": 20},
            {"drawdown_pct": 20, "target_position_pct": 50},
        ],
    }
    with patch("api.middlewares.auth.is_auth_enabled", return_value=False), \
         patch("src.auth.is_auth_enabled", return_value=False):
        response = TestClient(app).post("/api/v1/backtest/etf-crash", json=payload)

    assert response.status_code == 200, response.text
    assert response.json()["symbol"] == "510300"
    assert response.json()["trigger_count"] == 2
    assert response.json()["source"] == "sqlite"
