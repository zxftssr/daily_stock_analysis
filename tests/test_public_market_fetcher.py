# -*- coding: utf-8 -*-
"""Tests for lightweight Tencent/Sina/Eastmoney auto fallback."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import BoundedSemaphore, Event, Lock
import threading
import time
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
import requests

from data_provider.base import DataFetcherManager, DataFetchError
from data_provider.public_market_fetcher import (
    PublicMarketFetcher,
    normalize_public_source_order,
    resolve_public_market_code,
)
from data_provider.realtime_types import (
    RealtimeSource,
    UnifiedRealtimeQuote,
    get_realtime_circuit_breaker,
)


def _quote(code: str, source: RealtimeSource = RealtimeSource.TENCENT):
    return UnifiedRealtimeQuote(code=code, name="Sample", source=source, price=10.5)


class _InterruptAfterWorkerThread:
    """Simulate Thread.start() being interrupted after the worker already finished."""

    def __init__(self, *args, **kwargs):
        self._finished = Event()
        target = kwargs.pop("target")

        def wrapped_target():
            try:
                target()
            finally:
                self._finished.set()

        self._thread = threading.Thread(target=wrapped_target, *args, **kwargs)

    def start(self):
        self._thread.start()
        if not self._finished.wait(0.50):
            raise RuntimeError("worker did not finish")
        raise KeyboardInterrupt()


def setup_function():
    PublicMarketFetcher.clear_quote_cache()
    get_realtime_circuit_breaker().reset()


def test_source_order_is_supported_deduplicated_and_has_safe_default():
    assert normalize_public_source_order("sina,invalid,sina,eastmoney") == (
        "sina",
        "eastmoney",
    )
    assert normalize_public_source_order("") == ("tencent", "sina", "eastmoney")


def test_code_mapping_covers_cn_bse_hk_and_us():
    cn = resolve_public_market_code("600519.SH")
    bse = resolve_public_market_code("832566.BJ")
    hk = resolve_public_market_code("00700.HK")
    us = resolve_public_market_code("brk.b")

    assert (cn.canonical, cn.tencent_symbol, cn.eastmoney_secids) == (
        "600519",
        "sh600519",
        ("1.600519",),
    )
    assert (bse.market, bse.tencent_symbol, bse.eastmoney_secids) == (
        "bse",
        "bj832566",
        ("0.832566",),
    )
    assert (hk.canonical, hk.sina_symbol, hk.eastmoney_secids) == (
        "HK00700",
        "hk00700",
        ("116.00700",),
    )
    assert us.canonical == "BRK.B"
    assert us.tencent_symbol == "usBRK.B"
    assert "105.BRK-B" in us.eastmoney_secids


def test_code_mapping_preserves_explicit_mainland_exchange_hint():
    sh = resolve_public_market_code("000001.SH")
    sz = resolve_public_market_code("SZ000001")

    assert sh.tencent_symbol == "sh000001"
    assert sh.eastmoney_secids == ("1.000001",)
    assert sz.tencent_symbol == "sz000001"
    assert sz.eastmoney_secids == ("0.000001",)
    assert sh.cache_identity == "cn:sh:000001"
    assert sz.cache_identity == "cn:sz:000001"


def test_tencent_quote_parser_normalizes_mainland_units():
    code = resolve_public_market_code("600519")
    fields = [""] * 50
    fields[1] = "贵州茅台"
    fields[3] = "1500.00"
    fields[4] = "1480.00"
    fields[5] = "1490.00"
    fields[6] = "123"
    fields[30] = "20260827150003"
    fields[31] = "20.00"
    fields[32] = "1.35"
    fields[33] = "1510.00"
    fields[34] = "1475.00"
    fields[37] = "456.7"
    fields[38] = "0.42"
    fields[44] = "1000"
    fields[45] = "2000"

    quote = PublicMarketFetcher._parse_tencent_quote(code, fields)

    assert quote is not None
    assert quote.source is RealtimeSource.TENCENT
    assert quote.observed_at == "2026-08-27T15:00:03+08:00"
    assert quote.volume == 12300
    assert quote.amount == 4567000
    assert quote.circ_mv == 100000000000
    assert quote.total_mv == 200000000000


def test_tencent_quote_parser_keeps_price_when_observed_time_is_invalid():
    code = resolve_public_market_code("510500")
    fields = [""] * 50
    fields[1] = "中证500ETF南方"
    fields[3] = "7.973"
    fields[4] = "7.789"
    fields[30] = "20261399150003"

    quote = PublicMarketFetcher._parse_tencent_quote(code, fields)

    assert quote is not None
    assert quote.price == 7.973
    assert quote.observed_at is None


def test_tencent_quote_parser_uses_market_specific_hk_and_us_fields():
    hk_fields = [""] * 60
    hk_fields[1] = "腾讯控股"
    hk_fields[3] = "457.6"
    hk_fields[4] = "460.2"
    hk_fields[47] = "1.16"
    hk_fields[48] = "677.7"
    hk_fields[49] = "411.0"
    hk_fields[50] = "0.51"
    hk_fields[58] = "3.31"
    hk_quote = PublicMarketFetcher._parse_tencent_quote(
        resolve_public_market_code("00700.HK"),
        hk_fields,
    )

    us_fields = [""] * 55
    us_fields[1] = "Apple"
    us_fields[3] = "320.0"
    us_fields[4] = "315.0"
    us_fields[38] = "0.08"
    us_fields[48] = "323.45"
    us_fields[49] = "200.72"
    us_quote = PublicMarketFetcher._parse_tencent_quote(
        resolve_public_market_code("AAPL"),
        us_fields,
    )

    assert hk_quote is not None
    assert hk_quote.volume_ratio == 1.16
    assert hk_quote.turnover_rate == 0.51
    assert hk_quote.pb_ratio == 3.31
    assert (hk_quote.high_52w, hk_quote.low_52w) == (677.7, 411.0)
    assert us_quote is not None
    assert us_quote.volume_ratio is None
    assert us_quote.turnover_rate == 0.08
    assert (us_quote.high_52w, us_quote.low_52w) == (323.45, 200.72)


def test_eastmoney_quote_parser_supports_bse_and_rich_fields():
    code = resolve_public_market_code("920118.BJ")
    quote = PublicMarketFetcher._parse_eastmoney_quote(
        code,
        {
            "f43": 12.3,
            "f44": 12.8,
            "f45": 12.0,
            "f46": 12.1,
            "f47": 100,
            "f48": 1234567,
            "f57": "920118",
            "f58": "太湖远大",
            "f60": 12.0,
            "f116": 5000000000,
            "f117": 3000000000,
            "f167": 2.1,
            "f168": 1.2,
            "f170": 2.5,
        },
    )

    assert quote is not None
    assert quote.source is RealtimeSource.EASTMONEY
    assert quote.code == "920118"
    assert quote.volume == 10000
    assert quote.total_mv == 5000000000


def test_quote_auto_falls_back_and_reuses_short_cache():
    fetcher = PublicMarketFetcher(
        source_order="tencent,sina,eastmoney",
        quote_cache_ttl_seconds=30,
    )
    calls = []

    def fake_fetch(source, codes, _deadline):
        calls.append(source)
        if source == "tencent":
            raise requests.Timeout("timeout")
        return {
            codes[0].cache_identity: _quote(
                codes[0].canonical,
                RealtimeSource.SINA,
            )
        }

    with patch.object(fetcher, "_fetch_quotes_from_source", side_effect=fake_fetch):
        first = fetcher.get_realtime_quote("000001.SZ")
        second = fetcher.get_realtime_quote("000001.SZ")

    assert first is not None and first.source is RealtimeSource.SINA
    assert second is not None and second.source is RealtimeSource.SINA
    assert calls == ["tencent", "sina"]


def test_history_rejects_sparse_primary_and_uses_complete_fallback():
    fetcher = PublicMarketFetcher(source_order="tencent,eastmoney")
    sparse = pd.DataFrame(
        [
            {"date": "2026-06-01", "open": 10, "high": 11, "low": 9, "close": 10, "volume": 1},
            {"date": "2026-06-30", "open": 10, "high": 11, "low": 9, "close": 10, "volume": 1},
        ]
    )
    complete = pd.DataFrame(
        [
            {
                "date": value.strftime("%Y-%m-%d"),
                "open": 10,
                "high": 11,
                "low": 9,
                "close": 10.5,
                "volume": 100,
            }
            for value in pd.bdate_range("2026-06-01", "2026-06-30")
        ]
    )

    def fake_history(source, *_args):
        return sparse if source == "tencent" else complete

    with patch.object(fetcher, "_fetch_history_from_source", side_effect=fake_history):
        result = fetcher._fetch_raw_data("600519.SH", "2026-06-01", "2026-06-30")

    assert len(result) == len(complete)
    assert result.attrs["source"] == "public_eastmoney"


def test_history_does_not_return_sparse_rows_as_a_successful_source():
    fetcher = PublicMarketFetcher(source_order="tencent")
    sparse = pd.DataFrame(
        [
            {
                "date": "2026-06-30",
                "open": 10,
                "high": 11,
                "low": 9,
                "close": 10,
                "volume": 1,
            }
        ]
    )

    with patch.object(fetcher, "_fetch_history_from_source", return_value=sparse):
        with pytest.raises(DataFetchError, match="partial"):
            fetcher._fetch_raw_data("600519", "2026-06-01", "2026-06-30")


def test_tencent_us_quote_is_supported_but_sparse_us_history_is_skipped():
    assert PublicMarketFetcher._supports("tencent", "quote", "us") is True
    assert PublicMarketFetcher._supports("tencent", "history", "us") is False
    assert PublicMarketFetcher._supports("tencent", "history", "hk") is False
    assert PublicMarketFetcher._supports("tencent", "history", "bse") is False
    assert PublicMarketFetcher._supports("tencent", "history", "cn") is True
    assert PublicMarketFetcher._supports("sina", "quote", "cn") is True
    assert PublicMarketFetcher._supports("sina", "history", "cn") is False
    assert PublicMarketFetcher._supports("eastmoney", "history", "us") is True


def test_batch_cache_keeps_same_digits_on_different_exchanges_separate():
    fetcher = PublicMarketFetcher(source_order="tencent", quote_cache_ttl_seconds=30)

    def fake_fetch(_source, codes, _deadline):
        return {
            code.cache_identity: UnifiedRealtimeQuote(
                code=code.canonical,
                name=code.exchange or "",
                source=RealtimeSource.TENCENT,
                price=10.0 if code.exchange == "sh" else 20.0,
            )
            for code in codes
        }

    with patch.object(fetcher, "_fetch_quotes_from_source", side_effect=fake_fetch) as mocked:
        batch = fetcher.get_realtime_quotes(["000001.SH", "000001.SZ"])
        sh_cached = fetcher.get_realtime_quote("000001.SH")
        sz_cached = fetcher.get_realtime_quote("000001.SZ")

    assert [(quote.name, quote.price) for quote in batch] == [("sh", 10.0), ("sz", 20.0)]
    assert sh_cached is not None and sh_cached.price == 10.0
    assert sz_cached is not None and sz_cached.price == 20.0
    assert mocked.call_count == 1


def test_identical_concurrent_batches_share_the_first_network_result():
    fetcher = PublicMarketFetcher(
        source_order="tencent",
        quote_cache_ttl_seconds=30,
        min_interval_seconds=0,
        overall_timeout_seconds=1,
    )
    entered = Event()
    release = Event()

    def fake_fetch(_source, codes, _deadline):
        entered.set()
        release.wait(1)
        code = codes[0]
        return {code.cache_identity: _quote(code.canonical)}

    with patch.object(fetcher, "_fetch_quotes_from_source", side_effect=fake_fetch) as request:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(fetcher.get_realtime_quotes, ["600519.SH"])
            assert entered.wait(0.5)
            second = executor.submit(fetcher.get_realtime_quotes, ["600519.SH"])
            time.sleep(0.03)
            release.set()
            first_result = first.result(timeout=1)
            second_result = second.result(timeout=1)

    assert len(first_result) == 1
    assert len(second_result) == 1
    assert request.call_count == 1


def test_batch_lock_timeout_returns_cache_filled_by_lock_holder():
    fetcher = PublicMarketFetcher(
        source_order="tencent",
        quote_cache_ttl_seconds=30,
        min_interval_seconds=0,
        overall_timeout_seconds=0.08,
    )
    fetch_entered = Event()
    allow_fetch_return = Event()
    waiter_entered = Event()
    cache_written = Event()
    release_holder = Event()

    class SignalingLock:
        def __init__(self):
            self._lock = Lock()

        def acquire(self, timeout=-1):
            if self._lock.locked():
                waiter_entered.set()
            return self._lock.acquire(timeout=timeout)

        def release(self):
            self._lock.release()

    fetcher._batch_lock = SignalingLock()
    original_cache_quote = fetcher._cache_quote

    def fake_fetch(_source, codes, _deadline):
        fetch_entered.set()
        allow_fetch_return.wait(1)
        code = codes[0]
        return {code.cache_identity: _quote(code.canonical)}

    def cache_then_hold(key, quote):
        original_cache_quote(key, quote)
        if key.startswith("auto:") and not cache_written.is_set():
            cache_written.set()
            release_holder.wait(1)

    with patch.object(fetcher, "_fetch_quotes_from_source", side_effect=fake_fetch) as request, patch.object(
        fetcher,
        "_cache_quote",
        side_effect=cache_then_hold,
    ):
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(fetcher.get_realtime_quotes, ["600519.SH"])
            assert fetch_entered.wait(0.5)
            second = executor.submit(fetcher.get_realtime_quotes, ["600519.SH"])
            assert waiter_entered.wait(0.5)
            allow_fetch_return.set()
            assert cache_written.wait(0.5)
            try:
                second_result = second.result(timeout=1)
            finally:
                release_holder.set()
            first_result = first.result(timeout=1)

    assert len(first_result) == 1
    assert len(second_result) == 1
    assert request.call_count == 1


def test_adjusted_history_requests_use_qfq_endpoints_and_parameters():
    fetcher = PublicMarketFetcher()
    tencent_response = MagicMock()
    tencent_response.json.return_value = {
        "data": {
            "sh600519": {
                "qfqday": [["2026-06-30", "10", "11", "12", "9", "100"]]
            }
        }
    }
    eastmoney_response = MagicMock()
    eastmoney_response.json.return_value = {"data": {"klines": []}}

    with patch.object(fetcher, "_request", return_value=tencent_response) as request:
        rows = fetcher._fetch_tencent_history(
            resolve_public_market_code("600519.SH"),
            date(2026, 6, 1),
            date(2026, 6, 30),
            float("inf"),
        )
    assert len(rows) == 1
    assert rows.iloc[0]["volume"] == 10000
    assert "/fqkline/get" in request.call_args.args[0]
    assert request.call_args.kwargs["params"]["param"].endswith(",qfq")

    with patch.object(fetcher, "_request", return_value=eastmoney_response) as request:
        fetcher._fetch_eastmoney_history(
            resolve_public_market_code("600519.SH"),
            date(2026, 6, 1),
            date(2026, 6, 30),
            float("inf"),
        )
    assert request.call_args.kwargs["params"]["fqt"] == "1"


def test_eastmoney_history_normalizes_mainland_volume_but_not_hk_volume():
    fetcher = PublicMarketFetcher()
    response = MagicMock()
    response.json.return_value = {
        "data": {
            "klines": ["2026-06-30,10,11,12,9,100,100000,1,2,3,4"]
        }
    }

    with patch.object(fetcher, "_request", return_value=response):
        mainland = fetcher._fetch_eastmoney_history(
            resolve_public_market_code("920118.BJ"),
            date(2026, 6, 1),
            date(2026, 6, 30),
            float("inf"),
        )
        hk = fetcher._fetch_eastmoney_history(
            resolve_public_market_code("00700.HK"),
            date(2026, 6, 1),
            date(2026, 6, 30),
            float("inf"),
        )

    assert mainland.iloc[0]["volume"] == 10000
    assert hk.iloc[0]["volume"] == 100


def test_eastmoney_transport_failures_open_circuit_after_threshold():
    fetcher = PublicMarketFetcher(
        source_order="eastmoney",
        quote_cache_ttl_seconds=0,
    )

    with patch.object(fetcher, "_request", side_effect=requests.Timeout("offline")) as request:
        for _ in range(3):
            assert fetcher.get_realtime_quote("600519.SH", source="eastmoney") is None
        calls_after_threshold = request.call_count
        assert fetcher.get_realtime_quote("600519.SH", source="eastmoney") is None

    circuit = get_realtime_circuit_breaker()
    assert circuit.get_status()["public_eastmoney_quote"] == circuit.OPEN
    assert calls_after_threshold > 0
    assert request.call_count == calls_after_threshold


def test_eastmoney_history_raises_when_all_hosts_have_transport_errors():
    fetcher = PublicMarketFetcher()
    with patch.object(fetcher, "_request", side_effect=requests.Timeout("offline")):
        with pytest.raises(DataFetchError, match="所有候选地址均失败"):
            fetcher._fetch_eastmoney_history(
                resolve_public_market_code("600519.SH"),
                date(2026, 6, 1),
                date(2026, 6, 30),
                float("inf"),
            )


def test_eastmoney_history_deadline_after_network_error_is_a_failure():
    fetcher = PublicMarketFetcher()
    with (
        patch.object(fetcher, "_request", side_effect=requests.Timeout("offline")),
        patch(
            "data_provider.public_market_fetcher.time.monotonic",
            side_effect=[0.0, 2.0],
        ),
    ):
        with pytest.raises(DataFetchError, match="overall timeout exceeded"):
            fetcher._fetch_eastmoney_history(
                resolve_public_market_code("600519.SH"),
                date(2026, 6, 1),
                date(2026, 6, 30),
                1.0,
            )


def test_history_sanitizer_drops_invalid_ohlc_and_duplicate_last_row_wins():
    fetcher = PublicMarketFetcher()
    raw = pd.DataFrame(
        [
            {"date": "2026-06-01", "open": 10, "high": 11, "low": 9, "close": 10, "volume": 10},
            {"date": "2026-06-01", "open": 20, "high": 21, "low": 19, "close": 20, "volume": 20},
            {"date": "2026-06-02", "open": 10, "high": 8, "low": 9, "close": 10, "volume": 10},
        ]
    )

    result = fetcher._sanitize_history(
        raw,
        resolve_public_market_code("000001"),
        date(2026, 6, 1),
        date(2026, 6, 30),
        "tencent",
    )

    assert len(result) == 1
    assert result.iloc[0]["close"] == 20
    assert pd.isna(result.iloc[0]["amount"])


def test_history_sanitizer_drops_date_when_last_duplicate_is_invalid():
    fetcher = PublicMarketFetcher()
    raw = pd.DataFrame(
        [
            {
                "date": "2026-06-01",
                "open": 10,
                "high": 11,
                "low": 9,
                "close": 10,
                "volume": 10,
            },
            {
                "date": "2026-06-01",
                "open": 10,
                "high": 8,
                "low": 9,
                "close": 10,
                "volume": 10,
            },
        ]
    )

    result = fetcher._sanitize_history(
        raw,
        resolve_public_market_code("000001"),
        date(2026, 6, 1),
        date(2026, 6, 30),
        "tencent",
    )

    assert result.empty


def test_history_coverage_rejects_less_than_sixty_percent_of_weekdays():
    start = date(2026, 6, 1)
    end = date(2026, 6, 30)
    dates = pd.bdate_range(start, end)
    sparse = pd.DataFrame({"date": dates[:13]})
    sufficient = pd.DataFrame({"date": dates[:14]})
    sparse.loc[len(sparse) - 1, "date"] = pd.Timestamp(end)
    sufficient.loc[len(sufficient) - 1, "date"] = pd.Timestamp(end)

    assert PublicMarketFetcher._history_has_sufficient_coverage(sparse, start, end) is False
    assert PublicMarketFetcher._history_has_sufficient_coverage(sufficient, start, end) is True


def test_request_queue_wait_counts_toward_overall_deadline():
    session = MagicMock()
    fetcher = PublicMarketFetcher(
        session=session,
        min_interval_seconds=0,
        overall_timeout_seconds=0.05,
    )
    fetcher._request_lock = Lock()
    fetcher._request_lock.acquire()
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match="while queued"):
            fetcher._request("https://example.invalid", started + 0.05)
    finally:
        fetcher._request_lock.release()

    assert time.monotonic() - started < 0.25
    session.get.assert_not_called()


def test_request_passes_header_mapping_to_session():
    session = MagicMock()
    response = MagicMock()
    response.status_code = 200
    response.iter_content.return_value = iter((b"ok",))
    session.get.return_value = response
    fetcher = PublicMarketFetcher(session=session, min_interval_seconds=0)

    fetcher._request(
        "https://example.invalid",
        time.monotonic() + 1,
        headers={"Referer": "https://example.invalid/quote"},
    )

    request_headers = session.get.call_args.kwargs["headers"]
    assert request_headers == {
        "Accept": "*/*",
        "User-Agent": "Mozilla/5.0 (compatible; daily-stock-analysis/1.0)",
        "Referer": "https://example.invalid/quote",
    }


def test_slow_response_headers_are_stopped_by_wall_clock_deadline():
    session = MagicMock()
    response = MagicMock()
    response.status_code = 200
    response.iter_content.return_value = iter(())
    closed = Event()
    response.close.side_effect = closed.set

    def delayed_get(*_args, **_kwargs):
        time.sleep(0.20)
        return response

    session.get.side_effect = delayed_get
    fetcher = PublicMarketFetcher(
        session=session,
        min_interval_seconds=0,
        timeout_seconds=1,
    )

    started = time.monotonic()
    with pytest.raises(TimeoutError, match="awaiting headers"):
        fetcher._request("https://example.invalid", started + 0.03)

    assert time.monotonic() - started < 0.10
    assert session.get.call_args.kwargs["allow_redirects"] is False
    assert closed.wait(0.50)
    response.close.assert_called_once_with()


def test_late_header_close_failure_still_releases_transport_and_worker_slot():
    session = MagicMock()
    response = MagicMock()
    response.status_code = 200
    cleanup_attempted = Event()

    def delayed_get(*_args, **_kwargs):
        time.sleep(0.05)
        return response

    def broken_close():
        cleanup_attempted.set()
        raise RuntimeError("close failed")

    session.get.side_effect = delayed_get
    response.close.side_effect = broken_close
    fetcher = PublicMarketFetcher(
        session=session,
        min_interval_seconds=0,
        timeout_seconds=1,
    )
    worker_slot = BoundedSemaphore(1)

    with patch.object(PublicMarketFetcher, "_response_reader_slots", worker_slot):
        started = time.monotonic()
        with pytest.raises(TimeoutError, match="awaiting headers"):
            fetcher._request("https://example.invalid", started + 0.01)

        assert cleanup_attempted.wait(0.50)
        assert fetcher._transport_lock.acquire(timeout=0.20)
        fetcher._transport_lock.release()
        assert worker_slot.acquire(timeout=0.20)
        worker_slot.release()

    response.close.assert_called_once_with()


def test_header_worker_creation_failure_returns_worker_slot():
    session = MagicMock()
    fetcher = PublicMarketFetcher(session=session, min_interval_seconds=0)
    worker_slot = BoundedSemaphore(1)

    with patch.object(PublicMarketFetcher, "_response_reader_slots", worker_slot), patch(
        "data_provider.public_market_fetcher.Thread",
        side_effect=RuntimeError("thread unavailable"),
    ):
        with pytest.raises(RuntimeError, match="thread unavailable"):
            fetcher._request("https://example.invalid", time.monotonic() + 1)

        assert worker_slot.acquire(timeout=0.20)
        worker_slot.release()
        assert fetcher._request_lock.acquire(timeout=0.20)
        fetcher._request_lock.release()

    session.get.assert_not_called()


def test_body_worker_start_failure_returns_worker_slot_and_closes_response():
    session = MagicMock()
    response = MagicMock()
    response.status_code = 200
    session.get.return_value = response
    fetcher = PublicMarketFetcher(session=session, min_interval_seconds=0)
    worker_slot = BoundedSemaphore(1)

    class StartFailingThread:
        def start(self):
            raise RuntimeError("thread start unavailable")

    def thread_factory(*args, **kwargs):
        if kwargs.get("name") == "public-market-response-headers":
            return threading.Thread(*args, **kwargs)
        return StartFailingThread()

    with patch.object(PublicMarketFetcher, "_response_reader_slots", worker_slot), patch(
        "data_provider.public_market_fetcher.Thread",
        side_effect=thread_factory,
    ):
        with pytest.raises(RuntimeError, match="thread start unavailable"):
            fetcher._request("https://example.invalid", time.monotonic() + 1)

        assert worker_slot.acquire(timeout=0.20)
        worker_slot.release()
        assert fetcher._request_lock.acquire(timeout=0.20)
        fetcher._request_lock.release()

    response.close.assert_called_once_with()


def test_header_start_interrupted_after_worker_run_keeps_single_slot_owner():
    session = MagicMock()
    response = MagicMock()
    response.status_code = 200
    session.get.return_value = response
    fetcher = PublicMarketFetcher(session=session, min_interval_seconds=0)
    worker_slot = BoundedSemaphore(1)

    with patch.object(PublicMarketFetcher, "_response_reader_slots", worker_slot), patch(
        "data_provider.public_market_fetcher.Thread",
        _InterruptAfterWorkerThread,
    ):
        with pytest.raises(KeyboardInterrupt):
            fetcher._request("https://example.invalid", time.monotonic() + 1)

        assert worker_slot.acquire(timeout=0.20)
        worker_slot.release()
        assert fetcher._request_lock.acquire(timeout=0.20)
        fetcher._request_lock.release()

    response.close.assert_called_once_with()


def test_body_start_interrupted_after_worker_run_keeps_single_slot_owner():
    session = MagicMock()
    response = MagicMock()
    response.status_code = 200
    response.iter_content.return_value = iter((b"ok",))
    session.get.return_value = response
    fetcher = PublicMarketFetcher(session=session, min_interval_seconds=0)
    worker_slot = BoundedSemaphore(1)

    def thread_factory(*args, **kwargs):
        if kwargs.get("name") == "public-market-response-headers":
            return threading.Thread(*args, **kwargs)
        return _InterruptAfterWorkerThread(*args, **kwargs)

    with patch.object(PublicMarketFetcher, "_response_reader_slots", worker_slot), patch(
        "data_provider.public_market_fetcher.Thread",
        side_effect=thread_factory,
    ):
        with pytest.raises(KeyboardInterrupt):
            fetcher._request("https://example.invalid", time.monotonic() + 1)

        assert worker_slot.acquire(timeout=0.20)
        worker_slot.release()
        assert fetcher._request_lock.acquire(timeout=0.20)
        fetcher._request_lock.release()

    response.close.assert_called_once_with()


def test_slow_stream_is_stopped_by_wall_clock_deadline():
    session = MagicMock()
    response = MagicMock()
    closed = Event()

    def slow_chunks():
        closed.wait(1.0)
        return
        yield b"unreachable"

    response.iter_content.return_value = slow_chunks()
    def broken_close():
        closed.set()
        raise RuntimeError("close failed")

    response.close.side_effect = broken_close
    session.get.return_value = response
    fetcher = PublicMarketFetcher(
        session=session,
        min_interval_seconds=0,
        timeout_seconds=1,
    )

    started = time.monotonic()
    with pytest.raises(TimeoutError, match="while reading"):
        fetcher._request("https://example.invalid", started + 0.03)

    assert time.monotonic() - started < 0.10
    assert session.get.call_args.kwargs["stream"] is True
    response.close.assert_called_once_with()
    assert fetcher._request_lock.acquire(timeout=0.20)
    fetcher._request_lock.release()


def test_fetcher_and_manager_close_only_owned_sessions_once():
    injected_session = MagicMock()
    injected = PublicMarketFetcher(session=injected_session)
    injected.close()
    injected_session.close.assert_not_called()

    owned_session = MagicMock()
    with patch(
        "data_provider.public_market_fetcher.requests.Session",
        return_value=owned_session,
    ):
        owned = PublicMarketFetcher()
    manager = DataFetcherManager(fetchers=[owned])

    manager.close()
    manager.close()

    owned_session.close.assert_called_once_with()


def test_manager_does_not_wrap_concurrent_safe_fetcher_in_outer_lock():
    fetcher = PublicMarketFetcher(session=MagicMock())
    manager = DataFetcherManager(fetchers=[fetcher])

    with (
        patch.object(fetcher, "get_realtime_quote", return_value=_quote("600519")),
        patch.object(
            manager,
            "_get_fetcher_call_lock",
            side_effect=AssertionError("outer lock should not be used"),
        ),
    ):
        result = manager._call_fetcher_method(
            fetcher,
            "get_realtime_quote",
            "600519.SH",
        )

    assert result is not None
