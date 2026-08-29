# -*- coding: utf-8 -*-
"""Regression tests for fetcher routing and optional-source pruning."""

import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd

if "litellm" not in sys.modules:
    sys.modules["litellm"] = MagicMock()
if "json_repair" not in sys.modules:
    sys.modules["json_repair"] = MagicMock()

from data_provider.base import DataFetcherManager, DataFetchError
from data_provider.realtime_types import RealtimeSource, UnifiedRealtimeQuote


class _StubFetcher:
    def __init__(self, name: str, priority: int):
        self.name = name
        self.priority = priority


def _make_quote(code: str = "AAPL") -> UnifiedRealtimeQuote:
    return UnifiedRealtimeQuote(
        code=code,
        name="Apple",
        source=RealtimeSource.FALLBACK,
        price=188.8,
        change_pct=1.2,
        volume_ratio=1.0,
        turnover_rate=0.2,
        pe_ratio=20.0,
        pb_ratio=3.0,
        total_mv=1000.0,
        circ_mv=900.0,
        amplitude=2.0,
    )


def _make_sparse_public_quote(code: str = "AAPL") -> UnifiedRealtimeQuote:
    return UnifiedRealtimeQuote(
        code=code,
        name="Apple",
        source=RealtimeSource.TENCENT,
        price=188.8,
        change_pct=1.2,
        volume=1000,
        amount=188800.0,
    )


def _make_daily_df() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "date": "2026-05-01",
                "open": 100.0,
                "high": 102.0,
                "low": 99.0,
                "close": 101.0,
                "volume": 1000,
                "amount": 101000.0,
                "pct_chg": 1.0,
            }
        ]
    )


class TestFetcherSourceOptimization(unittest.TestCase):
    @patch("src.config.get_config")
    def test_daily_history_routes_hk_aliases_and_us_suffix_consistently(self, mock_get_config):
        mock_get_config.return_value = SimpleNamespace(
            longbridge_app_key="",
            longbridge_app_secret="",
            longbridge_access_token="",
        )
        yfinance = MagicMock()
        yfinance.name = "YfinanceFetcher"
        yfinance.priority = 4
        yfinance.get_daily_data.return_value = _make_daily_df()
        manager = DataFetcherManager(fetchers=[yfinance])

        expected = {
            "HSI": "HSI",
            "HSCEI": "HSCEI",
            "HSTECH": "HSTECH",
            "AAPL.US": "AAPL",
        }
        for raw_symbol, routed_symbol in expected.items():
            with self.subTest(symbol=raw_symbol):
                yfinance.get_daily_data.reset_mock()
                df, source = manager.get_daily_data(
                    raw_symbol,
                    start_date="2026-05-01",
                    end_date="2026-05-08",
                )
                self.assertFalse(df.empty)
                self.assertEqual(source, "YfinanceFetcher")
                self.assertEqual(
                    yfinance.get_daily_data.call_args.kwargs["stock_code"],
                    routed_symbol,
                )

    def test_hk_index_aliases_skip_higher_priority_public_market(self):
        public_market = MagicMock()
        public_market.name = "PublicMarketFetcher"
        public_market.priority = 0
        public_market.get_daily_data.return_value = _make_daily_df()
        yfinance = MagicMock()
        yfinance.name = "YfinanceFetcher"
        yfinance.priority = 4
        yfinance.get_daily_data.return_value = _make_daily_df()
        manager = DataFetcherManager(fetchers=[public_market, yfinance])

        for symbol in ("HSI", "HSCEI", "HSTECH"):
            with self.subTest(symbol=symbol):
                public_market.get_daily_data.reset_mock()
                yfinance.get_daily_data.reset_mock()
                df, source = manager.get_daily_data(
                    symbol,
                    start_date="2026-05-01",
                    end_date="2026-05-08",
                )
                self.assertFalse(df.empty)
                self.assertEqual(source, "YfinanceFetcher")
                public_market.get_daily_data.assert_not_called()
                self.assertEqual(
                    yfinance.get_daily_data.call_args.kwargs["stock_code"],
                    symbol,
                )

    @patch("src.config.get_config")
    def test_manager_skips_unconfigured_optional_fetchers(self, mock_get_config):
        mock_get_config.return_value = SimpleNamespace(
            tushare_token="",
            longbridge_app_key="",
            longbridge_app_secret="",
            longbridge_access_token="",
        )

        with patch("data_provider.efinance_fetcher.EfinanceFetcher", return_value=_StubFetcher("EfinanceFetcher", 0)), patch(
            "data_provider.public_market_fetcher.PublicMarketFetcher",
            return_value=_StubFetcher("PublicMarketFetcher", 0),
        ), patch(
            "data_provider.akshare_fetcher.AkshareFetcher",
            return_value=_StubFetcher("AkshareFetcher", 1),
        ), patch(
            "data_provider.pytdx_fetcher.PytdxFetcher",
            return_value=_StubFetcher("PytdxFetcher", 2),
        ), patch(
            "data_provider.baostock_fetcher.BaostockFetcher",
            return_value=_StubFetcher("BaostockFetcher", 3),
        ), patch(
            "data_provider.yfinance_fetcher.YfinanceFetcher",
            return_value=_StubFetcher("YfinanceFetcher", 4),
        ), patch(
            "data_provider.tushare_fetcher.TushareFetcher",
            return_value=_StubFetcher("TushareFetcher", -1),
        ) as mock_tushare, patch(
            "data_provider.longbridge_fetcher.LongbridgeFetcher",
            return_value=_StubFetcher("LongbridgeFetcher", 5),
        ) as mock_longbridge:
            manager = DataFetcherManager()

        self.assertEqual(
            manager.available_fetchers,
            [
                "PublicMarketFetcher",
                "EfinanceFetcher",
                "AkshareFetcher",
                "PytdxFetcher",
                "BaostockFetcher",
                "YfinanceFetcher",
            ],
        )
        mock_tushare.assert_not_called()
        mock_longbridge.assert_not_called()

    @patch("src.config.get_config")
    def test_a_share_public_auto_dispatches_to_shared_fetcher(self, mock_get_config):
        mock_get_config.return_value = SimpleNamespace(
            enable_realtime_quote=True,
            realtime_source_priority="public_auto,efinance,akshare_em",
        )
        public_market = MagicMock()
        public_market.name = "PublicMarketFetcher"
        public_market.priority = 0
        public_market.get_realtime_quote.return_value = _make_quote("600519")

        manager = DataFetcherManager(fetchers=[public_market])
        quote = manager.get_realtime_quote("600519.SH")

        self.assertIsNotNone(quote)
        public_market.get_realtime_quote.assert_called_once_with("600519.SH", source="auto")

    @patch("src.config.get_config")
    def test_price_only_quotes_stop_after_first_valid_source(self, mock_get_config):
        mock_get_config.return_value = SimpleNamespace(
            enable_realtime_quote=True,
            realtime_source_priority="public_auto,efinance,akshare_em",
            longbridge_app_key="",
            longbridge_app_secret="",
            longbridge_access_token="",
        )
        for symbol in ("600519", "00700.HK"):
            with self.subTest(symbol=symbol):
                public_market = MagicMock()
                public_market.name = "PublicMarketFetcher"
                public_market.priority = 0
                public_market.get_realtime_quote.return_value = _make_sparse_public_quote(symbol)
                efinance = MagicMock()
                efinance.name = "EfinanceFetcher"
                efinance.priority = 0
                akshare = MagicMock()
                akshare.name = "AkshareFetcher"
                akshare.priority = 1

                manager = DataFetcherManager(fetchers=[public_market, efinance, akshare])
                quote = manager.get_realtime_quote(symbol, enrich=False)

                self.assertIsNotNone(quote)
                public_market.get_realtime_quote.assert_called_once()
                efinance.get_realtime_quote.assert_not_called()
                akshare.get_realtime_quote.assert_not_called()

    @patch("src.config.get_config")
    def test_default_quote_enrichment_stops_after_one_successful_supplement(self, mock_get_config):
        mock_get_config.return_value = SimpleNamespace(
            enable_realtime_quote=True,
            realtime_source_priority="public_auto,efinance,akshare_em",
        )
        primary = _make_sparse_public_quote("600519")
        primary.observed_at = "2026-08-27T15:00:03+08:00"
        secondary = UnifiedRealtimeQuote(
            code="600519",
            source=RealtimeSource.EFINANCE,
            price=188.9,
            volume_ratio=1.1,
            observed_at="2026-08-27T15:00:04+08:00",
        )
        public_market = MagicMock()
        public_market.name = "PublicMarketFetcher"
        public_market.priority = 0
        public_market.get_realtime_quote.return_value = primary
        efinance = MagicMock()
        efinance.name = "EfinanceFetcher"
        efinance.priority = 0
        efinance.get_realtime_quote.return_value = secondary
        akshare = MagicMock()
        akshare.name = "AkshareFetcher"
        akshare.priority = 1
        akshare.get_realtime_quote.return_value = _make_quote("600519")

        manager = DataFetcherManager(fetchers=[public_market, efinance, akshare])
        quote = manager.get_realtime_quote("600519")

        self.assertIs(quote, primary)
        self.assertEqual(quote.volume_ratio, 1.1)
        self.assertEqual(quote.price, 188.8)
        self.assertEqual(quote.observed_at, "2026-08-27T15:00:03+08:00")
        efinance.get_realtime_quote.assert_called_once()
        akshare.get_realtime_quote.assert_not_called()

    @patch("src.config.get_config")
    def test_timestamp_required_skips_undated_primary_source(self, mock_get_config):
        mock_get_config.return_value = SimpleNamespace(
            enable_realtime_quote=True,
            realtime_source_priority="efinance,public_auto",
        )
        undated = _make_quote("510300")
        timed = _make_sparse_public_quote("510300")
        timed.observed_at = "2026-08-28T10:15:00+08:00"

        efinance = MagicMock()
        efinance.name = "EfinanceFetcher"
        efinance.priority = 0
        efinance.get_realtime_quote.return_value = undated
        public_market = MagicMock()
        public_market.name = "PublicMarketFetcher"
        public_market.priority = 0
        public_market.get_realtime_quote.return_value = timed

        manager = DataFetcherManager(fetchers=[efinance, public_market])
        quote = manager.get_realtime_quote(
            "510300",
            enrich=False,
            require_observed_at=True,
        )

        self.assertIs(quote, timed)
        efinance.get_realtime_quote.assert_called_once()
        public_market.get_realtime_quote.assert_called_once()

    @patch("src.config.get_config")
    def test_etf_quote_does_not_require_equity_valuation_fields(self, mock_get_config):
        mock_get_config.return_value = SimpleNamespace(
            enable_realtime_quote=True,
            realtime_source_priority="public_auto,efinance,akshare_em",
        )
        etf_quote = UnifiedRealtimeQuote(
            code="510500",
            source=RealtimeSource.TENCENT,
            price=7.973,
            volume_ratio=1.2,
            turnover_rate=8.26,
            amplitude=2.5,
        )
        public_market = MagicMock()
        public_market.name = "PublicMarketFetcher"
        public_market.priority = 0
        public_market.get_realtime_quote.return_value = etf_quote
        efinance = MagicMock()
        efinance.name = "EfinanceFetcher"
        efinance.priority = 0

        manager = DataFetcherManager(fetchers=[public_market, efinance])
        quote = manager.get_realtime_quote("510500")

        self.assertIs(quote, etf_quote)
        efinance.get_realtime_quote.assert_not_called()

    @patch("src.config.get_config")
    def test_hk_realtime_prefers_public_auto_without_longbridge(self, mock_get_config):
        mock_get_config.return_value = SimpleNamespace(
            enable_realtime_quote=True,
            realtime_source_priority="public_auto,efinance,akshare_em",
        )
        public_market = MagicMock()
        public_market.name = "PublicMarketFetcher"
        public_market.priority = 0
        public_market.get_realtime_quote.return_value = _make_quote("HK00700")

        akshare = MagicMock()
        akshare.name = "AkshareFetcher"
        akshare.priority = 1

        manager = DataFetcherManager(fetchers=[public_market, akshare])
        quote = manager.get_realtime_quote("00700.HK")

        self.assertIsNotNone(quote)
        public_market.get_realtime_quote.assert_called_once_with("HK00700", source="auto")
        akshare.get_realtime_quote.assert_not_called()

    @patch("src.config.get_config")
    def test_us_daily_prefers_public_auto_and_reports_actual_provider(self, mock_get_config):
        mock_get_config.return_value = SimpleNamespace(
            longbridge_app_key="",
            longbridge_app_secret="",
            longbridge_access_token="",
        )
        public_market = MagicMock()
        public_market.name = "PublicMarketFetcher"
        public_market.priority = 0
        public_df = _make_daily_df()
        public_df.attrs["source"] = "public_tencent"
        public_market.get_daily_data.return_value = public_df

        yfinance = MagicMock()
        yfinance.name = "YfinanceFetcher"
        yfinance.priority = 4

        manager = DataFetcherManager(fetchers=[public_market, yfinance])
        df, source = manager.get_daily_data(
            "AAPL",
            start_date="2026-05-01",
            end_date="2026-05-08",
        )

        self.assertFalse(df.empty)
        self.assertEqual(source, "public_tencent")
        public_market.get_daily_data.assert_called_once()
        yfinance.get_daily_data.assert_not_called()

    @patch("src.config.get_config")
    def test_us_daily_respects_configured_fetcher_priority(self, mock_get_config):
        mock_get_config.return_value = SimpleNamespace(
            longbridge_app_key="",
            longbridge_app_secret="",
            longbridge_access_token="",
        )
        public_market = MagicMock()
        public_market.name = "PublicMarketFetcher"
        public_market.priority = 9
        public_market.get_daily_data.return_value = _make_daily_df()

        yfinance = MagicMock()
        yfinance.name = "YfinanceFetcher"
        yfinance.priority = 4
        yfinance.get_daily_data.return_value = _make_daily_df()

        manager = DataFetcherManager(fetchers=[public_market, yfinance])
        df, source = manager.get_daily_data(
            "AAPL",
            start_date="2026-05-01",
            end_date="2026-05-08",
        )

        self.assertFalse(df.empty)
        self.assertEqual(source, "YfinanceFetcher")
        yfinance.get_daily_data.assert_called_once()
        public_market.get_daily_data.assert_not_called()

    @patch("src.config.get_config")
    def test_us_sparse_public_quote_does_not_force_yfinance_supplement(self, mock_get_config):
        mock_get_config.return_value = SimpleNamespace(
            enable_realtime_quote=True,
            realtime_source_priority="public_auto,efinance,akshare_em",
        )
        public_market = MagicMock()
        public_market.name = "PublicMarketFetcher"
        public_market.priority = 0
        public_market.get_realtime_quote.return_value = _make_sparse_public_quote()

        yfinance = MagicMock()
        yfinance.name = "YfinanceFetcher"
        yfinance.priority = 4
        yfinance.get_realtime_quote.return_value = _make_quote()

        manager = DataFetcherManager(fetchers=[public_market, yfinance])
        quote = manager.get_realtime_quote("AAPL")

        self.assertIsNotNone(quote)
        self.assertEqual(quote.source, RealtimeSource.TENCENT)
        public_market.get_realtime_quote.assert_called_once_with("AAPL", source="auto")
        yfinance.get_realtime_quote.assert_not_called()

    @patch("src.config.get_config")
    def test_cn_daily_keeps_explicit_exchange_hint_for_public_fetcher(self, mock_get_config):
        mock_get_config.return_value = SimpleNamespace()
        public_market = MagicMock()
        public_market.name = "PublicMarketFetcher"
        public_market.priority = 0
        public_market.get_daily_data.return_value = _make_daily_df()

        manager = DataFetcherManager(fetchers=[public_market])
        manager.get_daily_data(
            "000001.SH",
            start_date="2026-05-01",
            end_date="2026-05-08",
        )

        public_market.get_daily_data.assert_called_once_with(
            stock_code="000001.SH",
            start_date="2026-05-01",
            end_date="2026-05-08",
            days=30,
        )

    @patch("src.config.get_config")
    def test_nondefault_sh_daily_fallback_never_fetches_same_digits_from_sz(self, mock_get_config):
        mock_get_config.return_value = SimpleNamespace()
        public_market = MagicMock()
        public_market.name = "PublicMarketFetcher"
        public_market.priority = 0
        public_market.get_daily_data.side_effect = DataFetchError("public unavailable")

        efinance = MagicMock()
        efinance.name = "EfinanceFetcher"
        efinance.priority = 1
        efinance.get_daily_data.return_value = _make_daily_df()

        tushare = MagicMock()
        tushare.name = "TushareFetcher"
        tushare.priority = 2
        tushare.get_daily_data.return_value = _make_daily_df()

        manager = DataFetcherManager(fetchers=[public_market, efinance, tushare])
        df, source = manager.get_daily_data(
            "000001.SH",
            start_date="2026-05-01",
            end_date="2026-05-08",
        )

        self.assertFalse(df.empty)
        self.assertEqual(source, "TushareFetcher")
        public_market.get_daily_data.assert_called_once_with(
            stock_code="000001.SH",
            start_date="2026-05-01",
            end_date="2026-05-08",
            days=30,
        )
        tushare.get_daily_data.assert_called_once_with(
            stock_code="000001.SH",
            start_date="2026-05-01",
            end_date="2026-05-08",
            days=30,
        )
        efinance.get_daily_data.assert_not_called()

    @patch("src.config.get_config")
    def test_same_digits_realtime_fallback_respects_sh_vs_sz_identity(self, mock_get_config):
        mock_get_config.return_value = SimpleNamespace(
            enable_realtime_quote=True,
            realtime_source_priority="public_auto,efinance,tushare",
        )
        public_market = MagicMock()
        public_market.name = "PublicMarketFetcher"
        public_market.priority = 0
        public_market.get_realtime_quote.return_value = None

        efinance = MagicMock()
        efinance.name = "EfinanceFetcher"
        efinance.priority = 1
        efinance.get_realtime_quote.return_value = _make_quote("000001")

        tushare = MagicMock()
        tushare.name = "TushareFetcher"
        tushare.priority = 2
        tushare.get_realtime_quote.return_value = _make_quote("000001")

        manager = DataFetcherManager(fetchers=[public_market, efinance, tushare])

        sh_quote = manager.get_realtime_quote("000001.SH")

        self.assertIsNotNone(sh_quote)
        public_market.get_realtime_quote.assert_called_once_with("000001.SH", source="auto")
        efinance.get_realtime_quote.assert_not_called()
        tushare.get_realtime_quote.assert_called_once_with("000001.SH")

        public_market.reset_mock()
        efinance.reset_mock()
        tushare.reset_mock()
        public_market.get_realtime_quote.return_value = None
        efinance.get_realtime_quote.return_value = _make_quote("000001")
        tushare.get_realtime_quote.return_value = _make_quote("000001")

        sz_quote = manager.get_realtime_quote("000001.SZ")

        self.assertIsNotNone(sz_quote)
        public_market.get_realtime_quote.assert_called_once_with("000001.SZ", source="auto")
        efinance.get_realtime_quote.assert_called_once_with("000001")
        tushare.get_realtime_quote.assert_not_called()

    @patch("src.config.get_config")
    def test_watchlist_prefetch_uses_targeted_public_batch(self, mock_get_config):
        mock_get_config.return_value = SimpleNamespace(
            enable_realtime_quote=True,
            prefetch_realtime_quotes=True,
            realtime_source_priority="public_auto,efinance,akshare_em",
        )
        public_market = MagicMock()
        public_market.name = "PublicMarketFetcher"
        public_market.priority = 0
        public_market.get_realtime_quotes.return_value = [
            _make_quote(code)
            for code in ("600519", "000001", "HK00700", "AAPL", "TSLA")
        ]

        manager = DataFetcherManager(fetchers=[public_market])
        count = manager.prefetch_realtime_quotes(
            ["600519.SH", "000001.SZ", "00700.HK", "AAPL", "TSLA"]
        )

        self.assertEqual(count, 5)
        public_market.get_realtime_quotes.assert_called_once_with(
            ["600519.SH", "000001.SZ", "00700.HK", "AAPL", "TSLA"],
            source="auto",
        )

    @patch("src.config.get_config")
    def test_us_realtime_route_skips_temporarily_unavailable_longbridge(self, mock_get_config):
        mock_get_config.return_value = SimpleNamespace(
            enable_realtime_quote=True,
            realtime_source_priority="efinance,akshare_em,tushare",
        )

        longbridge = MagicMock()
        longbridge.name = "LongbridgeFetcher"
        longbridge.priority = 5
        longbridge.is_available_for_request.return_value = False

        yfinance = MagicMock()
        yfinance.name = "YfinanceFetcher"
        yfinance.priority = 4
        yfinance.get_realtime_quote.return_value = _make_quote("AAPL")

        manager = DataFetcherManager(fetchers=[longbridge, yfinance])

        quote = manager.get_realtime_quote("AAPL")

        self.assertIsNotNone(quote)
        self.assertEqual(quote.code, "AAPL")
        yfinance.get_realtime_quote.assert_called_once_with("AAPL")
        longbridge.get_realtime_quote.assert_not_called()

    @patch("src.config.get_config")
    def test_us_timestamp_required_route_uses_yahoo_minute_method(self, mock_get_config):
        mock_get_config.return_value = SimpleNamespace(
            enable_realtime_quote=True,
            realtime_source_priority="public_auto",
        )
        yfinance = MagicMock()
        yfinance.name = "YfinanceFetcher"
        yfinance.priority = 4
        quote = _make_quote("AAPL")
        quote.observed_at = "2026-08-28T10:00:00-04:00"
        yfinance.get_realtime_quote_with_timestamp.return_value = quote
        manager = DataFetcherManager(fetchers=[yfinance])

        result = manager.get_realtime_quote("AAPL", require_observed_at=True)

        self.assertIs(result, quote)
        yfinance.get_realtime_quote_with_timestamp.assert_called_once_with("AAPL")
        yfinance.get_realtime_quote.assert_not_called()

    @patch("src.config.get_config")
    def test_us_daily_route_skips_temporarily_unavailable_longbridge(self, mock_get_config):
        mock_get_config.return_value = SimpleNamespace(
            longbridge_app_key="app-key",
            longbridge_app_secret="app-secret",
            longbridge_access_token="access-token",
        )

        longbridge = MagicMock()
        longbridge.name = "LongbridgeFetcher"
        longbridge.priority = 5
        longbridge.is_available_for_request.return_value = False

        yfinance = MagicMock()
        yfinance.name = "YfinanceFetcher"
        yfinance.priority = 4
        yfinance.get_daily_data.return_value = _make_daily_df()

        manager = DataFetcherManager(fetchers=[longbridge, yfinance])

        df, source = manager.get_daily_data("AAPL", start_date="2026-05-01", end_date="2026-05-08")

        self.assertFalse(df.empty)
        self.assertEqual(source, "YfinanceFetcher")
        yfinance.get_daily_data.assert_called_once()
        longbridge.get_daily_data.assert_not_called()

    @patch("src.config.get_config")
    def test_hk_daily_route_skips_temporarily_unavailable_longbridge(self, mock_get_config):
        mock_get_config.return_value = SimpleNamespace(
            longbridge_app_key="app-key",
            longbridge_app_secret="app-secret",
            longbridge_access_token="access-token",
        )

        longbridge = MagicMock()
        longbridge.name = "LongbridgeFetcher"
        longbridge.priority = 5
        longbridge.is_available_for_request.return_value = False

        akshare = MagicMock()
        akshare.name = "AkshareFetcher"
        akshare.priority = 1
        akshare.get_daily_data.return_value = _make_daily_df()

        manager = DataFetcherManager(fetchers=[longbridge, akshare])

        df, source = manager.get_daily_data("HK00700", start_date="2026-05-01", end_date="2026-05-08")

        self.assertFalse(df.empty)
        self.assertEqual(source, "AkshareFetcher")
        akshare.get_daily_data.assert_called_once()
        longbridge.get_daily_data.assert_not_called()

    @patch("src.config.get_config")
    def test_chip_distribution_keeps_nondefault_exchange_identity(self, mock_get_config):
        mock_get_config.return_value = SimpleNamespace(enable_chip_distribution=True)
        unsafe = MagicMock()
        unsafe.name = "EfinanceFetcher"
        unsafe.priority = 0
        tushare = MagicMock()
        tushare.name = "TushareFetcher"
        tushare.priority = 1
        tushare.get_chip_distribution.return_value = {"winner": "sh"}

        manager = DataFetcherManager(fetchers=[unsafe, tushare])
        chip = manager.get_chip_distribution("000001.SH")

        self.assertEqual(chip, {"winner": "sh"})
        unsafe.get_chip_distribution.assert_not_called()
        tushare.get_chip_distribution.assert_called_once_with("000001.SH")


if __name__ == "__main__":
    unittest.main()
