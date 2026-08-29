# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import get_database_manager
from api.v1.endpoints.backtest import run_etf_crash_backtest, run_etf_crash_robustness
from api.v1.schemas.backtest import EtfCrashBacktestRequest, EtfCrashRobustnessRequest
from src.data.stock_index_loader import StockIndexEntry
from src.services.etf_crash_backtest_service import (
    EtfCrashBacktestService,
    EtfCrashRobustnessService,
)
from src.services.etf_history_service import EtfHistoryService


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


def _second_entry() -> StockIndexEntry:
    return StockIndexEntry(
        canonical_code="510500.SH",
        display_code="510500",
        name_zh="中证500ETF",
        market="CN",
        asset_type="etf",
        active=True,
        etf_category="mid_cap",
        benchmark_code="000905.SH",
        benchmark_name="中证500",
    )


def _rows():
    first = date(2024, 1, 1)
    closes = [100.0] * 250 + [90.0, 80.0, 70.0]
    return [
        SimpleNamespace(date=first + timedelta(days=index), high=100.0, close=close)
        for index, close in enumerate(closes)
    ]


def _robustness_rows():
    first = date(2024, 1, 1)
    cycle = [100.0, 95.0, 90.0, 85.0, 90.0, 95.0]
    closes = [100.0] * 250 + [cycle[index % len(cycle)] for index in range(210)]
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


def test_live_and_backtest_drawdown_use_the_same_250_session_window():
    first = date(2025, 1, 1)
    rows = [
        SimpleNamespace(
            date=first + timedelta(days=index),
            high=200.0 if index == 0 else (120.0 if index == 250 else 100.0),
            close=80.0 if index == 250 else 100.0,
        )
        for index in range(251)
    ]
    backtest = EtfCrashBacktestService(_FakeDb(rows), [_entry()]).run(
        symbol="510300",
        start_date=rows[-1].date,
        end_date=rows[-1].date,
        initial_capital=100000,
        stages=[{"drawdown_pct": 30, "target_position_pct": 20}],
    )
    history_payload = {
        "source": "unit-test",
        "stale": False,
        "partial_cache": False,
        "as_of_date": "2099-12-31",
        "actual_records": 250,
        "data": [
            {"date": row.date.isoformat(), "high": row.high, "close": row.close}
            for row in rows[:-1]
        ],
    }
    live = EtfHistoryService.calculate_metrics(
        "510300",
        history_payload,
        latest_price=rows[-1].close,
        latest_high=rows[-1].high,
    )

    assert backtest["equity_curve"][0]["drawdown_pct"] == 33.3333
    assert live.drawdown_250d_pct == backtest["equity_curve"][0]["drawdown_pct"]


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


def _robustness_request(**overrides):
    rows = _robustness_rows()
    payload = {
        "symbols": ["510300"],
        "start_date": rows[249].date,
        "end_date": rows[-1].date,
        "initial_capital": 100000,
        "stages": [
            {"drawdown_pct": 5, "target_position_pct": 30},
            {"drawdown_pct": 10, "target_position_pct": 60},
        ],
        "window_trading_days": 60,
        "step_trading_days": 30,
        "out_of_sample_pct": 50,
        "min_windows": 3,
        "min_pass_rate_pct": 60,
        "min_window_return_pct": -100,
        "max_window_drawdown_pct": 100,
        "min_triggered_stages": 1,
    }
    payload.update(overrides)
    return payload


def test_robustness_runs_fixed_parameters_across_in_and_out_of_sample_windows():
    rows = _robustness_rows()
    service = EtfCrashRobustnessService(_FakeDb(rows), [_entry()])

    result = service.run(**_robustness_request())

    assert result["passed"] is True
    assert result["summary"]["total_windows"] == 4
    assert result["summary"]["passed_windows"] == 4
    assert result["summary"]["out_of_sample_windows"] == 2
    assert result["summary"]["out_of_sample_pass_rate_pct"] == 100.0
    assert result["summary"]["trigger_coverage_pct"] == 100.0
    assert [item["sample_type"] for item in result["windows"]] == [
        "in_sample", "in_sample", "out_of_sample", "out_of_sample",
    ]
    in_sample_end = max(
        date.fromisoformat(item["end_date"])
        for item in result["windows"]
        if item["sample_type"] == "in_sample"
    )
    out_of_sample_start = min(
        date.fromisoformat(item["start_date"])
        for item in result["windows"]
        if item["sample_type"] == "out_of_sample"
    )
    assert in_sample_end < out_of_sample_start


def test_robustness_keeps_an_in_sample_window_at_high_holdout_ratio():
    service = EtfCrashRobustnessService(_FakeDb(_robustness_rows()), [_entry()])

    result = service.run(**_robustness_request(out_of_sample_pct=80))

    assert result["passed"] is True
    assert [item["sample_type"] for item in result["windows"]] == [
        "in_sample", "out_of_sample", "out_of_sample", "out_of_sample", "out_of_sample",
    ]


def test_robustness_deduplicates_display_and_canonical_etf_codes():
    service = EtfCrashRobustnessService(_FakeDb(_robustness_rows()), [_entry()])

    result = service.run(**_robustness_request(
        symbols=["510300", "510300.SH"],
        min_windows=4,
    ))

    assert result["passed"] is True
    assert result["requested_symbols"] == ["510300"]
    assert result["eligible_symbols"] == ["510300"]
    assert result["summary"]["total_windows"] == 4


def test_robustness_rejects_too_many_windows_before_expanding_them(monkeypatch):
    rows = _robustness_rows()
    db = _FakeDb(rows)
    service = EtfCrashRobustnessService(db, [_entry()])
    span = (rows[249].date, rows[268].date)
    monkeypatch.setattr(
        EtfCrashRobustnessService,
        "_rolling_spans",
        staticmethod(lambda *_args: [span] * (service.MAX_TOTAL_WINDOWS + 1)),
    )

    with pytest.raises(ValueError, match="滚动窗口总数 1002 超过上限 500"):
        service.run(**_robustness_request(window_trading_days=20, step_trading_days=20))

    assert len(db.calls) < service.MAX_TOTAL_WINDOWS


def test_robustness_fails_when_window_and_holdout_pass_rates_miss_threshold():
    service = EtfCrashRobustnessService(_FakeDb(_robustness_rows()), [_entry()])

    result = service.run(**_robustness_request(min_window_return_pct=100))

    assert result["passed"] is False
    assert result["summary"]["pass_rate_pct"] == 0.0
    assert result["summary"]["out_of_sample_pass_rate_pct"] == 0.0
    assert any("总体通过率" in reason for reason in result["failure_reasons"])
    assert any("样本外通过率" in reason for reason in result["failure_reasons"])


def test_robustness_fails_closed_when_one_selected_etf_has_no_history():
    rows = _robustness_rows()
    service = EtfCrashRobustnessService(
        _CandidateDb({"510300": rows}),
        [_entry(), _second_entry()],
    )

    result = service.run(**_robustness_request(symbols=["510300", "510500"]))

    assert result["passed"] is False
    assert result["eligible_symbols"] == ["510300"]
    assert result["symbol_errors"][0]["symbol"] == "510500"
    assert any("部分 ETF" in reason for reason in result["failure_reasons"])


def test_robustness_endpoint_returns_typed_summary():
    request = EtfCrashRobustnessRequest(**_robustness_request())

    response = run_etf_crash_robustness(request, _FakeDb(_robustness_rows()))

    assert response.passed is True
    assert response.summary.total_windows == 4
    assert response.summary.out_of_sample_windows == 2
