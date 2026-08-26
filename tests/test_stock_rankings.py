# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from threading import Event
from types import SimpleNamespace
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import pandas as pd
from fastapi.testclient import TestClient

import src.auth as auth
from api.app import create_app
from src.data.stock_index_loader import StockIndexEntry
from src.services.stock_discovery_service import (
    StockDiscoveryService,
    _BATCH_CACHE,
    _ETF_METRICS_CACHE,
    _ETF_METRICS_INFLIGHT,
)
from src.services import stock_market_metrics


def _entry(
    canonical: str,
    display: str,
    name: str,
    market: str,
    industry: str | None = None,
    asset_type: str = "stock",
    category: str | None = None,
    benchmark_code: str | None = None,
    benchmark_name: str | None = None,
) -> StockIndexEntry:
    return StockIndexEntry(
        canonical_code=canonical,
        display_code=display,
        name_zh=name,
        market=market,
        asset_type=asset_type,
        active=True,
        industry=industry,
        industry_source="test" if industry else None,
        etf_category=category,
        benchmark_code=benchmark_code,
        benchmark_name=benchmark_name,
    )


class _HistoryServiceStub:
    def __init__(self, closes_by_code, *, stale=False, partial_cache=False):
        self.closes_by_code = closes_by_code
        self.stale = stale
        self.partial_cache = partial_cache

    def get_history_data(self, code, days=420):
        return {
            "stale": self.stale,
            "partial_cache": self.partial_cache,
            "as_of_date": "2099-01-01",
            "data": [
                {"date": f"2025-01-{(index % 28) + 1:02d}", "close": close}
                for index, close in enumerate(self.closes_by_code.get(code, []))
            ]
        }


class _BlockingHistoryService(_HistoryServiceStub):
    def __init__(self, closes_by_code):
        super().__init__(closes_by_code)
        self.release = Event()
        self.calls = 0

    def get_history_data(self, code, days=420):
        self.calls += 1
        self.release.wait(timeout=2)
        return super().get_history_data(code, days=days)


class StockRankingsTestCase(unittest.TestCase):
    def setUp(self):
        _BATCH_CACHE.clear()
        _ETF_METRICS_CACHE.clear()
        _ETF_METRICS_INFLIGHT.clear()
        from data_provider import akshare_fetcher, efinance_fetcher

        efinance_fetcher._realtime_cache["data"] = None
        efinance_fetcher._realtime_cache["timestamp"] = 0
        akshare_fetcher._realtime_cache["data"] = None
        akshare_fetcher._realtime_cache["timestamp"] = 0
        efinance_fetcher._etf_realtime_cache["data"] = None
        efinance_fetcher._etf_realtime_cache["timestamp"] = 0
        akshare_fetcher._etf_realtime_cache["data"] = None
        akshare_fetcher._etf_realtime_cache["timestamp"] = 0

    def test_etf_rankings_filter_category_and_sort_by_250d_drawdown(self):
        entries = [
            _entry(
                "510300.SH", "510300", "沪深300ETF", "CN",
                asset_type="etf", category="broad_market",
                benchmark_code="000300.SH", benchmark_name="沪深300",
            ),
            _entry(
                "510500.SH", "510500", "中证500ETF", "CN",
                asset_type="etf", category="mid_cap",
                benchmark_code="000905.SH", benchmark_name="中证500",
            ),
        ]
        service = StockDiscoveryService(
            index_entries=entries,
            stock_service=_HistoryServiceStub({
                "510300": [100.0] * 250 + [80.0],
                "510500": [100.0] * 250 + [90.0],
            }),
        )
        quotes = pd.DataFrame([
            {"代码": "510300", "名称": "沪深300ETF", "最新价": 80.0, "涨跌幅": -2.0, "成交额": 3000, "成交量": 300},
            {"代码": "510500", "名称": "中证500ETF", "最新价": 90.0, "涨跌幅": -1.0, "成交额": 2000, "成交量": 200},
        ])

        with patch.object(
            service,
            "_get_cn_etf_batch_quotes",
            return_value=(quotes, "mock-etf", datetime(2026, 8, 26, tzinfo=timezone.utc), "ok"),
        ):
            payload = service.get_rankings(
                market="CN", asset_type="etf", metric="drawdown_250d_pct", direction="desc"
            )
            filtered = service.get_rankings(
                market="CN", asset_type="etf", category="mid_cap", metric="drawdown_250d_pct"
            )

        self.assertEqual([item["code"] for item in payload["items"]], ["510300.SH", "510500.SH"])
        self.assertEqual(payload["items"][0]["drawdown_250d_pct"], 20.0)
        self.assertEqual(payload["items"][0]["return_20d_pct"], -20.0)
        self.assertEqual(payload["items"][0]["asset_type"], "etf")
        self.assertEqual(payload["items"][0]["benchmark_name"], "沪深300")
        self.assertEqual([item["code"] for item in filtered["items"]], ["510500.SH"])

    def test_etf_rankings_reject_non_cn_market(self):
        service = StockDiscoveryService(index_entries=[])
        with self.assertRaisesRegex(ValueError, "currently supports CN only"):
            service.get_rankings(market="HK", asset_type="etf")

    def test_etf_rankings_mark_stale_history_metrics_as_partial(self):
        entry = _entry(
            "510300.SH", "510300", "沪深300ETF", "CN",
            asset_type="etf", category="broad_market",
            benchmark_code="000300.SH", benchmark_name="沪深300",
        )
        service = StockDiscoveryService(
            index_entries=[entry],
            stock_service=_HistoryServiceStub(
                {"510300": [100.0] * 250 + [80.0]},
                stale=True,
            ),
        )
        quotes = pd.DataFrame([{
            "代码": "510300", "名称": "沪深300ETF", "最新价": 80.0,
            "涨跌幅": -2.0, "成交额": 3000, "成交量": 300,
        }])
        with patch.object(
            service,
            "_get_cn_etf_batch_quotes",
            return_value=(quotes, "mock-etf", datetime(2026, 8, 26, tzinfo=timezone.utc), "ok"),
        ):
            payload = service.get_rankings(
                market="CN", asset_type="etf", metric="drawdown_250d_pct", direction="desc"
            )

        self.assertEqual(payload["status"], "partial")
        self.assertEqual(payload["items"], [])

    def test_etf_realtime_rankings_skip_history_metrics(self):
        entry = _entry(
            "510300.SH", "510300", "沪深300ETF", "CN",
            asset_type="etf", category="broad_market",
        )
        service = StockDiscoveryService(index_entries=[entry])
        quotes = pd.DataFrame([{
            "代码": "510300", "名称": "沪深300ETF", "最新价": 80.0,
            "涨跌幅": -2.0, "成交额": 3000, "成交量": 300,
        }])

        with patch.object(
            service,
            "_get_cn_etf_batch_quotes",
            return_value=(quotes, "mock-etf", datetime(2026, 8, 26, tzinfo=timezone.utc), "ok"),
        ), patch.object(service, "_get_etf_metrics") as get_metrics:
            payload = service.get_rankings(
                market="CN", asset_type="etf", metric="volume", direction="desc"
            )

        get_metrics.assert_not_called()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["items"][0]["volume"], 300)

    def test_etf_metric_timeout_is_bounded_and_negative_cached(self):
        entry = _entry(
            "510300.SH", "510300", "沪深300ETF", "CN",
            asset_type="etf", category="broad_market",
        )
        history = _BlockingHistoryService({"510300": [100.0] * 250 + [80.0]})
        service = StockDiscoveryService(index_entries=[entry], stock_service=history)

        try:
            with patch(
                "src.services.stock_discovery_service.ETF_METRICS_OVERALL_TIMEOUT_SECONDS",
                0.01,
            ):
                first, first_unreliable = service._get_etf_metrics([entry])
                second, second_unreliable = service._get_etf_metrics([entry])

            self.assertEqual(history.calls, 1)
            self.assertIn(entry.canonical_code, first_unreliable)
            self.assertIn(entry.canonical_code, second_unreliable)
            self.assertIsNone(first[entry.canonical_code]["drawdown_250d_pct"])
            self.assertIsNone(second[entry.canonical_code]["drawdown_250d_pct"])
        finally:
            history.release.set()

    def test_etf_metric_cache_is_revalidated_across_trading_date_boundary(self):
        entry = _entry(
            "510300.SH", "510300", "沪深300ETF", "CN",
            asset_type="etf", category="broad_market",
        )
        history = _HistoryServiceStub({"510300": [100.0] * 250 + [80.0]})
        history.get_history_data = Mock(wraps=history.get_history_data)
        service = StockDiscoveryService(index_entries=[entry], stock_service=history)
        _ETF_METRICS_CACHE[entry.canonical_code] = (
            time.time(),
            {
                "drawdown_250d_pct": 10.0,
                "return_20d_pct": 1.0,
                "return_60d_pct": 2.0,
                "return_250d_pct": 3.0,
            },
            True,
            date(2026, 8, 25),
        )

        with patch(
            "src.core.trading_calendar.get_effective_trading_date",
            return_value=date(2026, 8, 26),
        ):
            metrics, unreliable = service._get_etf_metrics([entry])

        self.assertEqual(history.get_history_data.call_count, 1)
        self.assertNotIn(entry.canonical_code, unreliable)
        self.assertEqual(metrics[entry.canonical_code]["drawdown_250d_pct"], 20.0)

    def test_etf_batch_quotes_fall_back_to_akshare(self):
        quotes = pd.DataFrame([{"代码": "510300", "最新价": 4.2}])
        with patch.object(
            stock_market_metrics,
            "fetch_efinance_etf_batch_quotes",
            side_effect=RuntimeError("efinance down"),
        ), patch.object(
            stock_market_metrics,
            "fetch_akshare_etf_batch_quotes",
            return_value=quotes,
        ):
            result = stock_market_metrics.fetch_cn_etf_batch_quotes()

        self.assertEqual(result.source, "akshare_etf")
        self.assertEqual(result.df.iloc[0]["代码"], "510300")

    def test_cn_batch_quotes_uses_efinance_timeout_adapter_before_fallback(self):
        service = StockDiscoveryService(index_entries=[])
        quotes = pd.DataFrame(
            [
                {"代码": "000001", "名称": "平安银行", "最新价": 10.0, "涨跌幅": 1.0, "成交额": 1000, "成交量": 100},
            ]
        )
        raw_ef_call = Mock(side_effect=AssertionError("raw efinance call should not be used"))
        fake_ef = SimpleNamespace(stock=SimpleNamespace(get_realtime_quotes=raw_ef_call))
        fake_ak = SimpleNamespace(stock_zh_a_spot_em=Mock(return_value=quotes))
        circuit = SimpleNamespace(
            is_available=Mock(return_value=True),
            record_success=Mock(),
            record_failure=Mock(),
        )

        with patch.dict(sys.modules, {"efinance": fake_ef, "akshare": fake_ak}), \
             patch("data_provider.efinance_fetcher.get_realtime_circuit_breaker", return_value=circuit), \
             patch("data_provider.efinance_fetcher.EfinanceFetcher._set_random_user_agent"), \
             patch("data_provider.efinance_fetcher.EfinanceFetcher._enforce_rate_limit"), \
             patch("data_provider.efinance_fetcher._ef_call_with_timeout", return_value=quotes) as call_with_timeout:
            result = service._fetch_cn_batch_quotes()

        self.assertEqual(result.df.iloc[0]["代码"], "000001")
        self.assertEqual(result.source, "efinance")
        call_with_timeout.assert_called_once()
        raw_ef_call.assert_not_called()
        fake_ak.stock_zh_a_spot_em.assert_not_called()
        circuit.record_success.assert_called_once_with("efinance")

    def test_etf_batch_quotes_use_efinance_etf_snapshot_and_cache(self):
        quotes = pd.DataFrame([
            {"代码": "510300", "名称": "沪深300ETF", "最新价": 4.2},
        ])
        raw_ef_call = Mock(side_effect=AssertionError("raw efinance call should not be used"))
        fake_ef = SimpleNamespace(stock=SimpleNamespace(get_realtime_quotes=raw_ef_call))
        circuit = SimpleNamespace(
            is_available=Mock(return_value=True),
            record_success=Mock(),
            record_failure=Mock(),
        )

        with patch.dict(sys.modules, {"efinance": fake_ef}), \
             patch("data_provider.efinance_fetcher.get_realtime_circuit_breaker", return_value=circuit), \
             patch("data_provider.efinance_fetcher.EfinanceFetcher._set_random_user_agent"), \
             patch("data_provider.efinance_fetcher.EfinanceFetcher._enforce_rate_limit"), \
             patch("data_provider.efinance_fetcher._ef_call_with_timeout", return_value=quotes) as call_with_timeout:
            first = stock_market_metrics.fetch_efinance_etf_batch_quotes()
            second = stock_market_metrics.fetch_efinance_etf_batch_quotes()

        self.assertEqual(first.iloc[0]["代码"], "510300")
        self.assertIs(second, first)
        call_with_timeout.assert_called_once_with(raw_ef_call, ["ETF"])
        circuit.record_success.assert_called_once_with("efinance_etf")

    def test_empty_efinance_etf_snapshot_falls_back_to_akshare(self):
        empty = pd.DataFrame()
        quotes = pd.DataFrame([{"代码": "510300", "最新价": 4.2}])
        fake_ef = SimpleNamespace(stock=SimpleNamespace(get_realtime_quotes=Mock()))
        circuit = SimpleNamespace(
            is_available=Mock(return_value=True),
            record_success=Mock(),
            record_failure=Mock(),
        )
        with patch.dict(sys.modules, {"efinance": fake_ef}), \
             patch("data_provider.efinance_fetcher.get_realtime_circuit_breaker", return_value=circuit), \
             patch("data_provider.efinance_fetcher.EfinanceFetcher._set_random_user_agent"), \
             patch("data_provider.efinance_fetcher.EfinanceFetcher._enforce_rate_limit"), \
             patch("data_provider.efinance_fetcher._ef_call_with_timeout", return_value=empty), \
             patch.object(stock_market_metrics, "fetch_akshare_etf_batch_quotes", return_value=quotes) as fallback:
            result = stock_market_metrics.fetch_cn_etf_batch_quotes()

        self.assertEqual(result.source, "akshare_etf")
        self.assertEqual(result.df.iloc[0]["代码"], "510300")
        fallback.assert_called_once_with()
        circuit.record_failure.assert_called_once()

    def test_rankings_sort_change_pct_asc_and_match_bse_codes(self):
        service = StockDiscoveryService(
            index_entries=[
                _entry("832566.BJ", "832566", "梓橦宫", "BSE", "医药商业"),
                _entry("920118.BJ", "920118", "太湖远大", "BSE", "化工"),
            ]
        )
        quotes = pd.DataFrame(
            [
                {"代码": "832566", "名称": "梓橦宫", "最新价": 12.3, "涨跌幅": -4.2, "成交额": 8000, "成交量": 100},
                {"代码": "920118", "名称": "太湖远大", "最新价": 9.1, "涨跌幅": 2.1, "成交额": 5000, "成交量": 90},
            ]
        )

        with patch.object(service, "_get_cn_batch_quotes", return_value=(quotes, "mock-cn", datetime(2026, 6, 21, tzinfo=timezone.utc), "ok")):
            payload = service.get_rankings(market="BSE", metric="change_pct", direction="asc", limit=10)

        self.assertEqual(payload["status"], "ok")
        self.assertEqual([item["code"] for item in payload["items"]], ["832566.BJ", "920118.BJ"])
        self.assertEqual(payload["items"][0]["change_pct"], -4.2)

    def test_rankings_uncategorized_filter_only_returns_missing_industry(self):
        service = StockDiscoveryService(
            index_entries=[
                _entry("000001.SZ", "000001", "平安银行", "CN", "银行"),
                _entry("000002.SZ", "000002", "万科A", "CN", None),
            ]
        )
        quotes = pd.DataFrame(
            [
                {"代码": "000001", "名称": "平安银行", "最新价": 10.0, "涨跌幅": 1.0, "成交额": 1000, "成交量": 100},
                {"代码": "000002", "名称": "万科A", "最新价": 8.0, "涨跌幅": 3.0, "成交额": 2000, "成交量": 200},
            ]
        )

        with patch.object(service, "_get_cn_batch_quotes", return_value=(quotes, "mock-cn", datetime(2026, 6, 21, tzinfo=timezone.utc), "ok")):
            payload = service.get_rankings(market="CN", industry="__uncategorized__", metric="amount", direction="desc")

        self.assertEqual(payload["status"], "ok")
        self.assertEqual([item["code"] for item in payload["items"]], ["000002.SZ"])
        self.assertIsNone(payload["items"][0]["industry"])

    def test_rankings_returns_empty_without_fetching_when_filter_has_no_candidates(self):
        service = StockDiscoveryService(
            index_entries=[
                _entry("000001.SZ", "000001", "平安银行", "CN", "银行"),
            ]
        )

        with patch.object(service, "_get_cn_batch_quotes") as get_quotes:
            payload = service.get_rankings(market="CN", industry="不存在的行业")

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["items"], [])
        get_quotes.assert_not_called()

    def test_rankings_reports_partial_when_fresh_batch_misses_some_candidates(self):
        service = StockDiscoveryService(
            index_entries=[
                _entry("000001.SZ", "000001", "平安银行", "CN", "银行"),
                _entry("000002.SZ", "000002", "万科A", "CN", "房地产"),
            ]
        )
        quotes = pd.DataFrame(
            [
                {"代码": "000001", "名称": "平安银行", "最新价": 10.0, "涨跌幅": 1.0, "成交额": 1000, "成交量": 100},
            ]
        )

        with patch.object(service, "_get_cn_batch_quotes", return_value=(quotes, "mock-cn", datetime(2026, 6, 21, tzinfo=timezone.utc), "ok")):
            payload = service.get_rankings(market="CN", metric="change_pct", direction="desc")

        self.assertEqual(payload["status"], "partial")
        self.assertEqual([item["code"] for item in payload["items"]], ["000001.SZ"])

    def test_rankings_preserves_stale_status_when_using_old_batch_cache(self):
        service = StockDiscoveryService(
            index_entries=[
                _entry("000001.SZ", "000001", "平安银行", "CN", "银行"),
            ]
        )
        quotes = pd.DataFrame(
            [
                {"代码": "000001", "名称": "平安银行", "最新价": 10.0, "涨跌幅": 1.0, "成交额": 1000, "成交量": 100},
            ]
        )

        with patch.object(service, "_get_cn_batch_quotes", return_value=(quotes, "mock-cn", datetime(2026, 6, 21, tzinfo=timezone.utc), "stale")):
            payload = service.get_rankings(market="CN", metric="change_pct", direction="desc")

        self.assertEqual(payload["status"], "stale")
        self.assertEqual(payload["items"][0]["source"], "mock-cn")

    def test_cn_fallback_reports_akshare_source(self):
        service = StockDiscoveryService(
            index_entries=[
                _entry("000001.SZ", "000001", "平安银行", "CN", "银行"),
            ]
        )
        quotes = pd.DataFrame(
            [
                {"代码": "000001", "名称": "平安银行", "最新价": 10.0, "涨跌幅": 1.0, "成交额": 1000, "成交量": 100},
            ]
        )

        with patch.object(service, "_fetch_efinance_batch_quotes", side_effect=RuntimeError("efinance down")), \
             patch.object(service, "_fetch_akshare_cn_batch_quotes", return_value=quotes):
            payload = service.get_rankings(market="CN", metric="change_pct", direction="desc")

        self.assertEqual(payload["source"], "akshare_em")
        self.assertEqual(payload["items"][0]["source"], "akshare_em")

    def test_hk_sina_fallback_reports_sina_source(self):
        service = StockDiscoveryService(
            index_entries=[
                _entry("00700.HK", "00700", "腾讯控股", "HK", "互联网"),
            ]
        )
        quotes = pd.DataFrame(
            [
                {"代码": "00700", "名称": "腾讯控股", "最新价": 390.0, "涨跌幅": 2.0, "成交额": 3000, "成交量": 200},
            ]
        )
        fake_ak = SimpleNamespace(
            stock_hk_spot_em=Mock(side_effect=RuntimeError("em down")),
            stock_hk_spot=Mock(return_value=quotes),
        )
        circuit = SimpleNamespace(
            is_available=Mock(return_value=True),
            record_success=Mock(),
            record_failure=Mock(),
        )

        with patch.dict(sys.modules, {"akshare": fake_ak}), \
             patch("data_provider.akshare_fetcher.get_realtime_circuit_breaker", return_value=circuit), \
             patch("data_provider.akshare_fetcher.AkshareFetcher._set_random_user_agent"), \
             patch("data_provider.akshare_fetcher.AkshareFetcher._enforce_rate_limit"):
            payload = service.get_rankings(market="HK", metric="change_pct", direction="desc")

        self.assertEqual(payload["source"], "akshare_hk_sina")
        self.assertEqual(payload["items"][0]["source"], "akshare_hk_sina")

    def test_rankings_reports_unavailable_when_all_batch_sources_fail_without_cache(self):
        service = StockDiscoveryService(
            index_entries=[
                _entry("000001.SZ", "000001", "平安银行", "CN", "银行"),
            ]
        )

        with patch.object(service, "_fetch_cn_batch_quotes", side_effect=RuntimeError("all sources down")):
            payload = service.get_rankings(market="CN", metric="change_pct", direction="desc")

        self.assertEqual(payload["status"], "unavailable")
        self.assertIsNone(payload["source"])
        self.assertIsNone(payload["updated_at"])
        self.assertIn("没有可用缓存", payload["message"])
        self.assertEqual(payload["items"], [])

    def test_rankings_reports_unsupported_when_us_core_pool_is_empty(self):
        service = StockDiscoveryService(index_entries=[])

        with patch.object(service, "_load_us_core_pool_entries", return_value=()):
            payload = service.get_rankings(market="US")

        self.assertEqual(payload["status"], "unsupported")
        self.assertEqual(payload["items"], [])

    def test_us_core_quotes_are_cached_across_metric_changes(self):
        service = StockDiscoveryService(
            index_entries=[
                _entry("AAPL", "AAPL", "Apple Inc.", "US", "Consumer Electronics"),
            ]
        )
        quote = SimpleNamespace(
            name="Apple Inc.",
            price=190.0,
            change_pct=1.5,
            amount=123456789.0,
            volume=987654,
            source=SimpleNamespace(value="yfinance"),
        )

        with patch.object(service, "_load_us_core_pool_entries", return_value=service.index_entries), \
             patch("src.services.stock_discovery_service.YfinanceFetcher") as fetcher_cls:
            fetcher = fetcher_cls.return_value
            fetcher.get_realtime_quote.return_value = quote

            first = service.get_rankings(market="US", metric="change_pct", direction="desc")
            second = service.get_rankings(market="US", metric="volume", direction="desc")

        self.assertEqual(fetcher.get_realtime_quote.call_count, 1)
        self.assertEqual(first["status"], "ok")
        self.assertEqual(second["items"][0]["code"], "AAPL")

    def test_us_core_pool_industry_source_is_override(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            data_dir.mkdir()
            (data_dir / "us_ranking_core_pool.csv").write_text(
                "symbol,name,industry\nAAPL,Apple Inc.,Consumer Electronics\n",
                encoding="utf-8",
            )

            with patch.object(StockDiscoveryService, "_repo_root", return_value=Path(temp_dir)):
                service = StockDiscoveryService(index_entries=[])
                entries = service._load_us_core_pool_entries()

        self.assertEqual(entries[0].industry_source, "override")

    def test_rankings_route_is_not_captured_by_dynamic_stock_routes(self):
        auth._auth_enabled = None
        app = create_app()
        client = TestClient(app)

        with patch("api.middlewares.auth.is_auth_enabled", return_value=False), \
             patch("src.auth.is_auth_enabled", return_value=False), \
             patch(
                 "api.v1.endpoints.stocks.StockDiscoveryService.get_rankings",
                 return_value={
                     "status": "unsupported",
                     "source": None,
                     "updated_at": None,
                     "items": [],
                 },
             ):
            response = client.get("/api/v1/stocks/rankings?market=CN")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "unsupported")

    def test_rankings_route_accepts_unavailable_status(self):
        auth._auth_enabled = None
        app = create_app()
        client = TestClient(app)

        with patch("api.middlewares.auth.is_auth_enabled", return_value=False), \
             patch("src.auth.is_auth_enabled", return_value=False), \
             patch(
                 "api.v1.endpoints.stocks.StockDiscoveryService.get_rankings",
                 return_value={
                     "status": "unavailable",
                     "source": None,
                     "updated_at": None,
                     "message": "批量行情源暂不可用，且没有可用缓存",
                     "items": [],
                 },
             ):
            response = client.get("/api/v1/stocks/rankings?market=CN")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "unavailable")
        self.assertIn("没有可用缓存", response.json()["message"])

    def test_rankings_route_forwards_etf_filters_and_extended_metrics(self):
        auth._auth_enabled = None
        app = create_app()
        client = TestClient(app)
        payload = {
            "status": "ok",
            "source": "mock-etf",
            "updated_at": "2026-08-26T00:00:00+00:00",
            "items": [{
                "code": "510300.SH",
                "name": "沪深300ETF",
                "market": "CN",
                "asset_type": "etf",
                "category": "broad_market",
                "benchmark_code": "000300.SH",
                "benchmark_name": "沪深300",
                "drawdown_250d_pct": 20.0,
                "return_20d_pct": -4.0,
                "return_60d_pct": -8.0,
                "return_250d_pct": -12.0,
            }],
        }
        with patch("api.middlewares.auth.is_auth_enabled", return_value=False), \
             patch("src.auth.is_auth_enabled", return_value=False), \
             patch(
                 "api.v1.endpoints.stocks.StockDiscoveryService.get_rankings",
                 return_value=payload,
             ) as get_rankings:
            response = client.get(
                "/api/v1/stocks/rankings?market=CN&asset_type=etf&category=broad_market"
                "&metric=drawdown_250d_pct&direction=desc"
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"][0]["benchmark_name"], "沪深300")
        get_rankings.assert_called_once_with(
            market="CN",
            industry=None,
            metric="drawdown_250d_pct",
            direction="desc",
            limit=20,
            asset_type="etf",
            category="broad_market",
        )


if __name__ == "__main__":
    unittest.main()
