# -*- coding: utf-8 -*-
from __future__ import annotations

import csv
import logging
import time
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FuturesTimeout, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Iterable, Literal

import pandas as pd

from data_provider.realtime_types import safe_float, safe_int
from data_provider.yfinance_fetcher import YfinanceFetcher
from src.data.stock_index_loader import (
    StockIndexEntry,
    build_stock_index_lookup_keys,
    load_stock_index_entries,
)
from src.services import stock_market_metrics as market_metrics
from src.services.stock_service import StockService

logger = logging.getLogger(__name__)

UNCATEGORIZED_INDUSTRY = "__uncategorized__"
SUPPORTED_MARKETS = {"CN", "BSE", "HK", "US"}
SUPPORTED_ASSET_TYPES = {"stock", "etf"}
SUPPORTED_METRICS = {
    "change_pct", "amount", "volume", "drawdown_250d_pct",
    "return_20d_pct", "return_60d_pct", "return_250d_pct",
}
ETF_HISTORY_METRICS = {
    "drawdown_250d_pct", "return_20d_pct", "return_60d_pct", "return_250d_pct",
}
SUPPORTED_DIRECTIONS = {"asc", "desc"}
BATCH_CACHE_TTL_SECONDS = 300
BATCH_SOURCE_TIMEOUT_SECONDS = 30
NO_BATCH_CACHE_MESSAGE = "批量行情源暂不可用，且没有可用缓存"
US_CORE_POOL_LIMIT = 100
US_CONCURRENCY = 5
US_SYMBOL_TIMEOUT_SECONDS = 12
US_OVERALL_TIMEOUT_SECONDS = 25
ETF_METRICS_CACHE_TTL_SECONDS = 900
ETF_METRICS_FAILURE_TTL_SECONDS = 60
ETF_METRICS_CONCURRENCY = 4
ETF_METRICS_OVERALL_TIMEOUT_SECONDS = 40
RankingStatus = Literal["ok", "partial", "stale", "unsupported", "unavailable"]


@dataclass(frozen=True)
class BatchQuoteResult:
    df: pd.DataFrame
    source: str | None
    updated_at: datetime | None
    status: RankingStatus


@dataclass
class _BatchCache:
    df: pd.DataFrame
    source: str
    updated_at: datetime
    timestamp: float
    status: RankingStatus = "ok"


@dataclass(frozen=True)
class _EtfMetricsResult:
    metrics: dict[str, float | None]
    reliable: bool
    as_of_date: date | None = None


_BATCH_CACHE: dict[str, _BatchCache] = {}
_BATCH_CACHE_LOCK = RLock()
_ETF_METRICS_CACHE: dict[str, tuple[float, dict[str, float | None], bool, date | None]] = {}
_ETF_METRICS_CACHE_LOCK = RLock()
_ETF_METRICS_EXECUTOR = ThreadPoolExecutor(max_workers=ETF_METRICS_CONCURRENCY)
_ETF_METRICS_INFLIGHT: dict[str, Future[_EtfMetricsResult]] = {}


class StockDiscoveryService:
    """Stock discovery rankings based on static index candidates and batch quotes."""

    def __init__(
        self,
        index_entries: Iterable[StockIndexEntry] | None = None,
        stock_service: StockService | None = None,
    ):
        self.index_entries = tuple(index_entries) if index_entries is not None else load_stock_index_entries()
        self.stock_service = stock_service

    def get_rankings(
        self,
        market: str,
        industry: str | None = None,
        metric: str = "change_pct",
        direction: str = "desc",
        limit: int = 20,
        asset_type: str = "stock",
        category: str | None = None,
    ) -> dict[str, Any]:
        market = str(market or "").upper()
        metric = str(metric or "change_pct")
        direction = str(direction or "desc")
        asset_type = str(asset_type or "stock").strip().lower()

        if market not in SUPPORTED_MARKETS:
            raise ValueError(f"Unsupported market: {market}")
        if metric not in SUPPORTED_METRICS:
            raise ValueError(f"Unsupported ranking metric: {metric}")
        if asset_type not in SUPPORTED_ASSET_TYPES:
            raise ValueError(f"Unsupported asset type: {asset_type}")
        if asset_type == "etf" and market != "CN":
            raise ValueError("Broad-market ETF discovery currently supports CN only")
        if direction not in SUPPORTED_DIRECTIONS:
            raise ValueError(f"Unsupported ranking direction: {direction}")
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")

        if market == "US":
            core_entries = self._load_us_core_pool_entries()
            if not core_entries:
                return self._empty_payload("unsupported")
            candidates = self._filter_entries(core_entries, market, industry, asset_type, category)
            if not candidates:
                return self._empty_payload("ok")
            quote_result = self._coerce_batch_result(self._get_us_core_quotes(core_entries))
        else:
            candidates = self._filter_entries(self.index_entries, market, industry, asset_type, category)
            if not candidates:
                return self._empty_payload("ok")
            if asset_type == "etf":
                raw_quote_result = self._get_cn_etf_batch_quotes()
            else:
                raw_quote_result = self._get_hk_batch_quotes() if market == "HK" else self._get_cn_batch_quotes(market)
            quote_result = self._coerce_batch_result(raw_quote_result)

        if quote_result.df is None or quote_result.df.empty:
            return self._empty_payload(
                quote_result.status,
                source=quote_result.source,
                updated_at=quote_result.updated_at,
                message=NO_BATCH_CACHE_MESSAGE if quote_result.status == "unavailable" else None,
            )

        quote_lookup = self._build_quote_lookup(quote_result.df, market)
        rows: list[dict[str, Any]] = []
        missing_count = 0
        updated_at = self._format_dt(quote_result.updated_at)
        if asset_type == "etf" and metric in ETF_HISTORY_METRICS:
            etf_metrics, unreliable_etf_metrics = self._get_etf_metrics(candidates)
        else:
            etf_metrics, unreliable_etf_metrics = {}, set()

        for entry in candidates:
            quote_row = self._find_quote_row(entry, quote_lookup)
            if quote_row is None:
                missing_count += 1
                continue
            item = self._build_ranking_item(
                entry=entry,
                quote_row=quote_row,
                market=market,
                source=quote_row.get("_source") or quote_result.source,
                updated_at=quote_row.get("_updated_at") or updated_at,
                extra_metrics=etf_metrics.get(entry.canonical_code),
            )
            if item.get(metric) is None:
                missing_count += 1
                continue
            if entry.canonical_code in unreliable_etf_metrics:
                missing_count += 1
            rows.append(item)

        reverse = direction == "desc"
        rows.sort(key=lambda item: item.get(metric), reverse=reverse)

        status = quote_result.status
        if status == "ok" and missing_count > 0:
            status = "partial"

        return {
            "status": status,
            "source": quote_result.source,
            "updated_at": updated_at,
            "items": rows[:limit],
        }

    def _filter_entries(
        self,
        entries: Iterable[StockIndexEntry],
        market: str,
        industry: str | None,
        asset_type: str = "stock",
        category: str | None = None,
    ) -> tuple[StockIndexEntry, ...]:
        expected_industry = str(industry or "").strip()
        expected_category = str(category or "").strip()
        filtered: list[StockIndexEntry] = []
        for entry in entries:
            if entry.market.upper() != market:
                continue
            if not entry.active or entry.asset_type != asset_type:
                continue
            if asset_type == "etf":
                if expected_category and entry.etf_category != expected_category:
                    continue
                filtered.append(entry)
                continue
            if expected_industry == UNCATEGORIZED_INDUSTRY:
                if entry.industry:
                    continue
            elif expected_industry:
                if entry.industry != expected_industry:
                    continue
            filtered.append(entry)
        return tuple(filtered)

    @staticmethod
    def _coerce_batch_result(value: Any) -> BatchQuoteResult:
        if isinstance(value, BatchQuoteResult):
            return value
        if isinstance(value, tuple) and len(value) == 4:
            df, source, updated_at, status = value
            return BatchQuoteResult(
                df=df,
                source=source,
                updated_at=updated_at,
                status=status,
            )
        raise TypeError(f"Unexpected batch quote result: {type(value).__name__}")

    def _get_cn_batch_quotes(self, market: str) -> BatchQuoteResult:
        return self._get_cached_batch_quotes(
            cache_key="cn",
            source_name="efinance",
            fetcher=self._fetch_cn_batch_quotes,
        )

    def _get_cn_etf_batch_quotes(self) -> BatchQuoteResult:
        return self._get_cached_batch_quotes(
            cache_key="cn:etf",
            source_name="efinance_etf",
            fetcher=market_metrics.fetch_cn_etf_batch_quotes,
        )

    def _get_hk_batch_quotes(self) -> BatchQuoteResult:
        return self._get_cached_batch_quotes(
            cache_key="hk",
            source_name="akshare_hk_em",
            fetcher=self._fetch_hk_batch_quotes,
        )

    def _get_cached_batch_quotes(
        self,
        cache_key: str,
        source_name: str,
        fetcher: Any,
    ) -> BatchQuoteResult:
        now = time.time()
        with _BATCH_CACHE_LOCK:
            cached = _BATCH_CACHE.get(cache_key)
            if cached and now - cached.timestamp <= BATCH_CACHE_TTL_SECONDS:
                return BatchQuoteResult(cached.df, cached.source, cached.updated_at, cached.status)

        try:
            raw_result = fetcher()
            updated_at = datetime.now(timezone.utc)
            fetch_result = self._coerce_fetcher_batch_result(raw_result, source_name, updated_at)
            with _BATCH_CACHE_LOCK:
                _BATCH_CACHE[cache_key] = _BatchCache(
                    df=fetch_result.df,
                    source=fetch_result.source or source_name,
                    updated_at=fetch_result.updated_at or updated_at,
                    timestamp=now,
                    status=fetch_result.status,
                )
            return BatchQuoteResult(
                fetch_result.df,
                fetch_result.source or source_name,
                fetch_result.updated_at or updated_at,
                fetch_result.status,
            )
        except Exception as exc:
            logger.warning("[股票发现] 获取批量行情失败 cache_key=%s: %s", cache_key, exc)
            with _BATCH_CACHE_LOCK:
                cached = _BATCH_CACHE.get(cache_key)
                if cached:
                    return BatchQuoteResult(cached.df, cached.source, cached.updated_at, "stale")
            return BatchQuoteResult(pd.DataFrame(), None, None, "unavailable")

    @staticmethod
    def _coerce_fetcher_batch_result(
        value: Any,
        default_source: str,
        default_updated_at: datetime,
    ) -> BatchQuoteResult:
        if isinstance(value, BatchQuoteResult):
            return value
        if all(hasattr(value, attr) for attr in ("df", "source", "updated_at", "status")):
            return BatchQuoteResult(
                df=value.df,
                source=value.source,
                updated_at=value.updated_at,
                status=value.status,
            )
        if isinstance(value, pd.DataFrame):
            return BatchQuoteResult(value, default_source, default_updated_at, "ok")
        return StockDiscoveryService._coerce_batch_result(value)

    def _fetch_cn_batch_quotes(self) -> BatchQuoteResult:
        try:
            return BatchQuoteResult(self._fetch_efinance_batch_quotes(), "efinance", None, "ok")
        except Exception as efinance_exc:
            logger.debug("[股票发现] efinance 受保护批量行情失败，尝试 akshare: %s", efinance_exc)
            return BatchQuoteResult(self._fetch_akshare_cn_batch_quotes(), "akshare_em", None, "ok")

    def _fetch_efinance_batch_quotes(self) -> pd.DataFrame:
        return market_metrics.fetch_efinance_batch_quotes()

    def _fetch_akshare_cn_batch_quotes(self) -> pd.DataFrame:
        return market_metrics.fetch_akshare_cn_batch_quotes()

    def _fetch_hk_batch_quotes(self) -> BatchQuoteResult:
        result = market_metrics.fetch_hk_batch_quotes()
        return BatchQuoteResult(result.df, result.source, result.updated_at, result.status)

    def _load_us_core_pool_entries(self) -> tuple[StockIndexEntry, ...]:
        pool_path = self._repo_root() / "data" / "us_ranking_core_pool.csv"
        if not pool_path.is_file():
            return ()

        index_by_key: dict[str, StockIndexEntry] = {}
        for entry in self.index_entries:
            if entry.market.upper() != "US":
                continue
            for key in build_stock_index_lookup_keys(entry.canonical_code, entry.display_code):
                index_by_key[key.upper()] = entry

        entries: list[StockIndexEntry] = []
        try:
            with pool_path.open("r", encoding="utf-8-sig", newline="") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    symbol = str(row.get("symbol") or "").strip().upper()
                    if not symbol:
                        continue
                    index_entry = index_by_key.get(symbol)
                    csv_name = str(row.get("name") or "").strip()
                    csv_industry = str(row.get("industry") or "").strip()
                    name = csv_name or (index_entry.name_zh if index_entry else symbol)
                    industry = csv_industry or (index_entry.industry if index_entry else None)
                    entries.append(
                        StockIndexEntry(
                            canonical_code=symbol,
                            display_code=symbol,
                            name_zh=name,
                            pinyin=index_entry.pinyin if index_entry else "",
                            acronym=index_entry.acronym if index_entry else "",
                            aliases=index_entry.aliases if index_entry else (),
                            market="US",
                            asset_type="stock",
                            active=True,
                            popularity=index_entry.popularity if index_entry else None,
                            industry=industry,
                            industry_source="override" if csv_industry else (index_entry.industry_source if index_entry else None),
                        )
                    )
                    if len(entries) >= US_CORE_POOL_LIMIT:
                        break
        except OSError as exc:
            logger.warning("[股票发现] 读取美股核心池失败 %s: %s", pool_path, exc)
            return ()
        return tuple(entries)

    def _get_us_core_quotes(self, entries: Iterable[StockIndexEntry]) -> BatchQuoteResult:
        symbols = [entry.canonical_code.upper() for entry in entries][:US_CORE_POOL_LIMIT]
        if not symbols:
            return BatchQuoteResult(pd.DataFrame(), "yfinance", None, "unsupported")

        cache_key = f"us:{','.join(symbols)}"
        now = time.time()
        with _BATCH_CACHE_LOCK:
            cached = _BATCH_CACHE.get(cache_key)
            if cached and now - cached.timestamp <= BATCH_CACHE_TTL_SECONDS:
                return BatchQuoteResult(cached.df, cached.source, cached.updated_at, cached.status)

        result = self._fetch_us_core_quotes(symbols)
        with _BATCH_CACHE_LOCK:
            _BATCH_CACHE[cache_key] = _BatchCache(
                df=result.df,
                source=result.source or "yfinance",
                updated_at=result.updated_at or datetime.now(timezone.utc),
                timestamp=now,
                status=result.status,
            )
        return result

    def _fetch_us_core_quotes(self, symbols: list[str]) -> BatchQuoteResult:
        fetcher = YfinanceFetcher()
        rows: list[dict[str, Any]] = []
        failed = 0
        updated_at = datetime.now(timezone.utc)
        executor = ThreadPoolExecutor(max_workers=US_CONCURRENCY)
        future_to_symbol = {
            executor.submit(fetcher.get_realtime_quote, symbol): symbol for symbol in symbols
        }
        try:
            for future in as_completed(future_to_symbol, timeout=US_OVERALL_TIMEOUT_SECONDS):
                symbol = future_to_symbol[future]
                try:
                    quote = future.result(timeout=US_SYMBOL_TIMEOUT_SECONDS)
                except Exception as exc:
                    logger.debug("[股票发现] 美股核心池行情失败 %s: %s", symbol, exc)
                    failed += 1
                    continue
                if quote is None:
                    failed += 1
                    continue
                rows.append(
                    {
                        "code": symbol,
                        "name": getattr(quote, "name", "") or symbol,
                        "price": getattr(quote, "price", None),
                        "change_pct": getattr(quote, "change_pct", None),
                        "amount": getattr(quote, "amount", None),
                        "volume": getattr(quote, "volume", None),
                        "_source": getattr(getattr(quote, "source", None), "value", None) or "yfinance",
                        "_updated_at": self._format_dt(updated_at),
                    }
                )
        except FuturesTimeout:
            failed += len(symbols) - len(rows)
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        status: RankingStatus = "ok"
        if failed:
            status = "partial"
        if not rows and failed:
            status = "partial"

        return BatchQuoteResult(pd.DataFrame(rows), "yfinance", updated_at, status)

    @staticmethod
    def _call_with_timeout(func: Any, timeout_seconds: float, task_name: str) -> Any:
        return market_metrics.call_with_timeout(func, timeout_seconds, task_name)

    def _build_quote_lookup(self, df: pd.DataFrame, market: str) -> dict[str, pd.Series]:
        return market_metrics.build_quote_lookup(df, market)

    def _find_quote_row(
        self,
        entry: StockIndexEntry,
        quote_lookup: dict[str, pd.Series],
    ) -> pd.Series | None:
        for key in build_stock_index_lookup_keys(entry.canonical_code, entry.display_code):
            row = quote_lookup.get(key.upper())
            if row is not None:
                return row
        return None

    def _build_ranking_item(
        self,
        entry: StockIndexEntry,
        quote_row: pd.Series,
        market: str,
        source: str | None,
        updated_at: str | None,
        extra_metrics: dict[str, float | None] | None = None,
    ) -> dict[str, Any]:
        item = {
            "code": entry.canonical_code,
            "name": entry.name_zh if entry.asset_type == "etf" else (
                self._first_value(quote_row, ("名称", "股票名称", "name")) or entry.name_zh
            ),
            "market": market,
            "industry": entry.industry,
            "price": safe_float(self._first_value(quote_row, ("最新价", "最新", "price", "last_price", "close"))),
            "change_pct": safe_float(self._first_value(quote_row, ("涨跌幅", "change_pct", "pct_chg", "changePercent"))),
            "amount": safe_float(self._first_value(quote_row, ("成交额", "amount", "turnover"))),
            "volume": safe_int(self._first_value(quote_row, ("成交量", "volume"))),
            "source": source,
            "updated_at": updated_at,
            "asset_type": entry.asset_type,
            "category": entry.etf_category,
            "benchmark_code": entry.benchmark_code,
            "benchmark_name": entry.benchmark_name,
        }
        item.update(extra_metrics or {})
        return item

    def _get_etf_metrics(
        self,
        entries: Iterable[StockIndexEntry],
    ) -> tuple[dict[str, dict[str, float | None]], set[str]]:
        now = time.time()
        result: dict[str, dict[str, float | None]] = {}
        unreliable: set[str] = set()
        pending: list[StockIndexEntry] = []
        with _ETF_METRICS_CACHE_LOCK:
            for entry in entries:
                cached = _ETF_METRICS_CACHE.get(entry.canonical_code)
                cache_ttl = (
                    ETF_METRICS_CACHE_TTL_SECONDS
                    if cached and cached[2]
                    else ETF_METRICS_FAILURE_TTL_SECONDS
                )
                cache_is_current = bool(
                    cached
                    and (
                        not cached[2]
                        or self._is_etf_metrics_as_of_current(entry.canonical_code, cached[3])
                    )
                )
                if cached and cache_is_current and now - cached[0] <= cache_ttl:
                    result[entry.canonical_code] = dict(cached[1])
                    if not cached[2]:
                        unreliable.add(entry.canonical_code)
                else:
                    _ETF_METRICS_CACHE.pop(entry.canonical_code, None)
                    pending.append(entry)

        if not pending:
            return result, unreliable

        if self.stock_service is None:
            self.stock_service = StockService()

        future_to_entry: dict[Future[_EtfMetricsResult], StockIndexEntry] = {}
        with _ETF_METRICS_CACHE_LOCK:
            for entry in pending:
                future = _ETF_METRICS_INFLIGHT.get(entry.canonical_code)
                if future is None:
                    future = _ETF_METRICS_EXECUTOR.submit(
                        self._load_etf_metrics,
                        entry.display_code,
                    )
                    _ETF_METRICS_INFLIGHT[entry.canonical_code] = future
                    future.add_done_callback(
                        lambda completed, code=entry.canonical_code: self._store_etf_metrics_result(
                            code,
                            completed,
                        )
                    )
                future_to_entry[future] = entry
        try:
            for future in as_completed(future_to_entry, timeout=ETF_METRICS_OVERALL_TIMEOUT_SECONDS):
                entry = future_to_entry[future]
                try:
                    loaded = future.result()
                except Exception as exc:
                    logger.debug("[ETF发现] 历史指标失败 %s: %s", entry.display_code, exc)
                    loaded = _EtfMetricsResult(self._empty_etf_metrics(), False)
                result[entry.canonical_code] = dict(loaded.metrics)
                if not loaded.reliable:
                    unreliable.add(entry.canonical_code)
        except FuturesTimeout:
            logger.warning("[ETF发现] 历史指标批量计算超时")
            for future, entry in future_to_entry.items():
                if future.done():
                    continue
                result[entry.canonical_code] = self._empty_etf_metrics()
                unreliable.add(entry.canonical_code)
                with _ETF_METRICS_CACHE_LOCK:
                    _ETF_METRICS_CACHE[entry.canonical_code] = (
                        time.time(),
                        self._empty_etf_metrics(),
                        False,
                        None,
                    )
        return result, unreliable

    def _load_etf_metrics(self, code: str) -> _EtfMetricsResult:
        assert self.stock_service is not None
        payload = self.stock_service.get_history_data(code, days=420)
        if payload.get("stale") is True or payload.get("partial_cache") is True:
            return _EtfMetricsResult(self._empty_etf_metrics(), False)
        try:
            from data_provider.base import normalize_stock_code
            from src.core.trading_calendar import get_effective_trading_date, get_market_for_stock

            observed_date = date.fromisoformat(str(payload.get("as_of_date") or "")[:10])
            expected_date = get_effective_trading_date(
                get_market_for_stock(normalize_stock_code(code))
            )
            if observed_date < expected_date:
                return _EtfMetricsResult(self._empty_etf_metrics(), False)
        except (TypeError, ValueError):
            return _EtfMetricsResult(self._empty_etf_metrics(), False)
        closes = [
            value
            for item in payload.get("data") or []
            if (value := safe_float(item.get("close"))) is not None and value > 0
        ]
        if not closes:
            return _EtfMetricsResult(self._empty_etf_metrics(), False)

        latest = closes[-1]
        window_250 = closes[-250:]
        peak_250 = max(window_250) if len(window_250) >= 250 else None
        metrics = {
            "drawdown_250d_pct": round((peak_250 - latest) / peak_250 * 100, 4) if peak_250 else None,
            "return_20d_pct": self._period_return(closes, 20),
            "return_60d_pct": self._period_return(closes, 60),
            "return_250d_pct": self._period_return(closes, 250),
        }
        return _EtfMetricsResult(
            metrics,
            all(value is not None for value in metrics.values()),
            observed_date,
        )

    def _store_etf_metrics_result(
        self,
        code: str,
        future: Future[_EtfMetricsResult],
    ) -> None:
        try:
            loaded = future.result()
        except Exception:
            loaded = _EtfMetricsResult(self._empty_etf_metrics(), False)
        with _ETF_METRICS_CACHE_LOCK:
            if _ETF_METRICS_INFLIGHT.get(code) is future:
                _ETF_METRICS_INFLIGHT.pop(code, None)
            _ETF_METRICS_CACHE[code] = (
                time.time(),
                dict(loaded.metrics),
                loaded.reliable,
                loaded.as_of_date,
            )

    @staticmethod
    def _is_etf_metrics_as_of_current(code: str, as_of_date: date | None) -> bool:
        if as_of_date is None:
            return False
        try:
            from data_provider.base import normalize_stock_code
            from src.core.trading_calendar import get_effective_trading_date, get_market_for_stock

            expected_date = get_effective_trading_date(
                get_market_for_stock(normalize_stock_code(code))
            )
        except Exception:
            return False
        return as_of_date >= expected_date

    @staticmethod
    def _period_return(closes: list[float], periods: int) -> float | None:
        if len(closes) <= periods or closes[-periods - 1] <= 0:
            return None
        return round((closes[-1] / closes[-periods - 1] - 1) * 100, 4)

    @staticmethod
    def _empty_etf_metrics() -> dict[str, float | None]:
        return {
            "drawdown_250d_pct": None,
            "return_20d_pct": None,
            "return_60d_pct": None,
            "return_250d_pct": None,
        }

    @staticmethod
    def _first_value(row: pd.Series, columns: Iterable[str]) -> Any:
        return market_metrics.first_value(row, columns)

    @staticmethod
    def _format_dt(value: datetime | str | None) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            return value
        return value.isoformat()

    @staticmethod
    def _empty_payload(
        status: str,
        source: str | None = None,
        updated_at: datetime | str | None = None,
        message: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "status": status,
            "source": source,
            "updated_at": StockDiscoveryService._format_dt(updated_at),
            "items": [],
        }
        if message:
            payload["message"] = message
        return payload

    @staticmethod
    def _repo_root() -> Path:
        return Path(__file__).resolve().parents[2]
