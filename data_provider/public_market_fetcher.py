# -*- coding: utf-8 -*-
"""Lightweight public market-data adapters with automatic provider fallback.

The provider order and normalized surface are inspired by the MIT-licensed
``zhangxiangliang/stock-api`` project, while this implementation stays native
to the repository's Python runtime and reuses its cache/circuit-breaker types.
"""

from __future__ import annotations

import logging
import math
import os
import re
import time
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from threading import BoundedSemaphore, Event, Lock, RLock, Thread
from typing import Any, Iterable

import pandas as pd
import requests

from .base import (
    BaseFetcher,
    DataFetchError,
    STANDARD_COLUMNS,
    _is_hk_market,
    is_bse_code,
    normalize_stock_code,
)
from .realtime_types import (
    RealtimeSource,
    UnifiedRealtimeQuote,
    get_realtime_circuit_breaker,
    normalize_cn_quote_observed_at,
    safe_float,
    safe_int,
)
from .us_index_mapping import is_us_index_code, is_us_stock_code

logger = logging.getLogger(__name__)

SUPPORTED_PUBLIC_SOURCES = ("tencent", "sina", "eastmoney")
_KNOWN_SHANGHAI_INDEX_CODES = frozenset({"000300"})
MIN_HISTORY_WEEKDAY_COVERAGE = 0.60
TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q={symbols}"
TENCENT_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
SINA_QUOTE_URL = "https://hq.sinajs.cn/list={symbols}"
SINA_KLINE_URL = "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketData.getKLineData"
EASTMONEY_QUOTE_HOSTS = (
    "push2delay.eastmoney.com",
    "push2.eastmoney.com",
    "82.push2.eastmoney.com",
)
EASTMONEY_KLINE_HOSTS = (
    "push2his.eastmoney.com",
    "7.push2his.eastmoney.com",
    "33.push2his.eastmoney.com",
    "63.push2his.eastmoney.com",
    "91.push2his.eastmoney.com",
)
MAX_PUBLIC_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_PUBLIC_RESPONSE_READERS = 8

_TENCENT_ROW_PATTERN = re.compile(r'v_([^=]+)="([^"]*)"')
_SINA_ROW_PATTERN = re.compile(r'var\s+hq_str_([^=]+)="([^"]*)"')


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        logger.warning("%s 配置无效，使用默认值 %s", name, default)
        return default


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        logger.warning("%s 配置无效，使用默认值 %s", name, default)
        return default


def normalize_public_source_order(value: str | Iterable[str] | None) -> tuple[str, ...]:
    """Return a de-duplicated, supported source order with safe defaults."""
    if isinstance(value, str):
        raw_sources = value.split(",")
    elif value is None:
        raw_sources = []
    else:
        raw_sources = list(value)

    sources: list[str] = []
    for raw_source in raw_sources:
        source = str(raw_source or "").strip().lower()
        if source in SUPPORTED_PUBLIC_SOURCES and source not in sources:
            sources.append(source)
    return tuple(sources or SUPPORTED_PUBLIC_SOURCES)


def _normalize_history_volume(value: Any, market: str) -> float | None:
    """Normalize mainland daily volume from lots to shares."""
    parsed = safe_float(value)
    if parsed is None:
        return None
    if market in {"cn", "bse"}:
        parsed *= 100
    return parsed


@dataclass(frozen=True)
class PublicMarketCode:
    original: str
    canonical: str
    market: str
    digits_or_symbol: str
    exchange: str | None = None

    @property
    def cache_identity(self) -> str:
        """Return an exchange-aware identity used only for internal caches."""
        if self.market in {"cn", "bse"}:
            return f"{self.market}:{self.exchange}:{self.digits_or_symbol}"
        return f"{self.market}:{self.digits_or_symbol}"

    @property
    def tencent_symbol(self) -> str | None:
        if self.market == "hk":
            return f"hk{self.digits_or_symbol}"
        if self.market == "us":
            return f"us{self.digits_or_symbol}"
        if self.market == "bse":
            return f"bj{self.digits_or_symbol}"
        if self.market == "cn":
            return f"{self.exchange}{self.digits_or_symbol}"
        return None

    @property
    def sina_symbol(self) -> str | None:
        if self.market == "hk":
            return f"hk{self.digits_or_symbol}"
        if self.market == "us":
            return f"gb_{self.digits_or_symbol.lower()}"
        if self.market == "cn":
            return f"{self.exchange}{self.digits_or_symbol}"
        return None

    @property
    def eastmoney_secids(self) -> tuple[str, ...]:
        if self.market == "hk":
            return (f"116.{self.digits_or_symbol}",)
        if self.market == "bse":
            return (f"0.{self.digits_or_symbol}",)
        if self.market == "cn":
            market_id = "1" if self.exchange == "sh" else "0"
            return (f"{market_id}.{self.digits_or_symbol}",)
        if self.market == "us":
            variants = [self.digits_or_symbol]
            if "." in self.digits_or_symbol:
                variants.append(self.digits_or_symbol.replace(".", "-"))
            return tuple(
                f"{market_id}.{symbol}"
                for symbol in variants
                for market_id in ("105", "106", "107")
            )
        return ()


def resolve_public_market_code(stock_code: str) -> PublicMarketCode:
    """Normalize the repository's accepted stock-code forms for public APIs."""
    original = str(stock_code or "").strip()
    if not original:
        raise DataFetchError("股票代码不能为空")

    upper = original.upper()
    if is_us_index_code(upper):
        return PublicMarketCode(original, upper, "us_index", upper)

    normalized = normalize_stock_code(original).upper()
    if _is_hk_market(original) or normalized.startswith("HK"):
        digits = normalized[2:] if normalized.startswith("HK") else normalized
        if not digits.isdigit():
            raise DataFetchError(f"无法识别港股代码: {stock_code}")
        digits = digits.zfill(5)
        return PublicMarketCode(original, f"HK{digits}", "hk", digits)

    if is_us_stock_code(normalized):
        return PublicMarketCode(original, normalized, "us", normalized)

    if normalized.isdigit() and len(normalized) == 6:
        if is_bse_code(normalized):
            return PublicMarketCode(original, normalized, "bse", normalized, "bj")
        exchange_hint = None
        if upper.startswith(("SH", "SZ")) and not upper.startswith(("SH.", "SZ.")):
            exchange_hint = upper[:2].lower()
        elif "." in upper:
            suffix = upper.rsplit(".", 1)[1]
            if suffix in {"SH", "SS", "SZ"}:
                exchange_hint = "sh" if suffix in {"SH", "SS"} else "sz"
        exchange = exchange_hint or (
            "sh"
            if normalized in _KNOWN_SHANGHAI_INDEX_CODES
            or normalized.startswith(("5", "6", "9"))
            else "sz"
        )
        return PublicMarketCode(original, normalized, "cn", normalized, exchange)

    raise DataFetchError(f"无法识别股票代码: {stock_code}")


@dataclass
class _CachedQuote:
    quote: UnifiedRealtimeQuote
    expires_at: float


class _WorkerSlotLease:
    """Transfer one semaphore slot from the caller to a worker exactly once."""

    def __init__(self, semaphore: BoundedSemaphore) -> None:
        self._semaphore = semaphore
        self._lock = Lock()
        self._owner = "caller"

    def claim_for_worker(self) -> bool:
        with self._lock:
            if self._owner != "caller":
                return False
            self._owner = "worker"
            return True

    def release_from_caller(self) -> None:
        self._release("caller")

    def release_from_worker(self) -> None:
        self._release("worker")

    def _release(self, expected_owner: str) -> None:
        should_release = False
        with self._lock:
            if self._owner == expected_owner:
                self._owner = "released"
                should_release = True
        if should_release:
            self._semaphore.release()


class PublicMarketFetcher(BaseFetcher):
    """Tencent -> Sina -> Eastmoney lightweight auto fallback.

    This fetcher is intentionally focused on targeted quotes and K-lines. Full
    market rankings continue to use the protected batch adapters that expose
    richer market-wide fields.
    """

    name = "PublicMarketFetcher"
    priority = 0
    concurrent_safe = True

    _quote_cache: dict[str, _CachedQuote] = {}
    _quote_cache_lock = RLock()
    _quote_key_locks = tuple(RLock() for _ in range(64))
    _batch_lock = RLock()
    _response_reader_slots = BoundedSemaphore(MAX_PUBLIC_RESPONSE_READERS)
    _cache_max_entries = 2048

    def __init__(
        self,
        *,
        enabled: bool | None = None,
        source_order: str | Iterable[str] | None = None,
        timeout_seconds: float | None = None,
        overall_timeout_seconds: float | None = None,
        quote_cache_ttl_seconds: float | None = None,
        min_interval_seconds: float | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.priority = _env_int("PUBLIC_MARKET_PRIORITY", 0)
        self.enabled = (
            enabled
            if enabled is not None
            else os.getenv("PUBLIC_MARKET_ENABLED", "true").strip().lower()
            not in {"0", "false", "no", "off"}
        )
        configured_order = source_order
        if configured_order is None:
            configured_order = os.getenv(
                "PUBLIC_MARKET_SOURCE_ORDER", ",".join(SUPPORTED_PUBLIC_SOURCES)
            )
        self.source_order = normalize_public_source_order(configured_order)
        self.timeout_seconds = (
            max(0.1, float(timeout_seconds))
            if timeout_seconds is not None
            else _env_float("PUBLIC_MARKET_TIMEOUT_SECONDS", 4.0, 0.1)
        )
        self.overall_timeout_seconds = (
            max(0.1, float(overall_timeout_seconds))
            if overall_timeout_seconds is not None
            else _env_float("PUBLIC_MARKET_OVERALL_TIMEOUT_SECONDS", 8.0, 0.1)
        )
        self.quote_cache_ttl_seconds = (
            max(0.0, float(quote_cache_ttl_seconds))
            if quote_cache_ttl_seconds is not None
            else _env_float("PUBLIC_MARKET_QUOTE_CACHE_TTL_SECONDS", 15.0, 0.0)
        )
        self.min_interval_seconds = (
            max(0.0, float(min_interval_seconds))
            if min_interval_seconds is not None
            else _env_float("PUBLIC_MARKET_MIN_INTERVAL_SECONDS", 0.05, 0.0)
        )
        self._owns_session = session is None
        self.session = session or requests.Session()
        self._closed = False
        self._request_lock = RLock()
        self._transport_lock = Lock()
        self._last_request_at = 0.0

    def is_available_for_request(self, _capability: str = "") -> bool:
        return self.enabled and not self._closed

    def close(self) -> None:
        """Close the internally-created HTTP session exactly once."""
        if self._closed:
            return
        self._closed = True
        if self._owns_session:
            self.session.close()

    @staticmethod
    def _close_response_safely(response: requests.Response | None) -> None:
        if response is None:
            return
        try:
            response.close()
        except Exception as exc:
            logger.debug("[公开行情-auto] 关闭 HTTP 响应失败: %s", exc)

    @staticmethod
    def _acquire_before_deadline(lock: Any, deadline: float) -> bool:
        if math.isinf(deadline):
            lock.acquire()
            return True
        remaining = deadline - time.monotonic()
        return remaining > 0 and lock.acquire(timeout=remaining)

    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        code = resolve_public_market_code(stock_code)
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
        end = datetime.strptime(end_date, "%Y-%m-%d").date()
        deadline = time.monotonic() + self.overall_timeout_seconds
        errors: list[str] = []
        partial_candidates: list[tuple[str, pd.DataFrame]] = []

        for source in self.source_order:
            if not self._supports(source, "history", code.market):
                continue
            source_key = f"public_{source}_history"
            circuit = get_realtime_circuit_breaker()
            if not circuit.is_available(source_key):
                errors.append(f"{source}: circuit open")
                continue
            if time.monotonic() >= deadline:
                errors.append("overall timeout exceeded")
                break

            try:
                raw = self._fetch_history_from_source(source, code, start, end, deadline)
                cleaned = self._sanitize_history(raw, code, start, end, source)
                if cleaned.empty:
                    circuit.record_inconclusive(source_key)
                    errors.append(f"{source}: empty history")
                    continue

                if self._history_has_sufficient_coverage(cleaned, start, end):
                    circuit.record_success(source_key)
                    logger.info(
                        "[公开行情-auto] %s 日线使用 %s: rows=%s",
                        code.canonical,
                        source,
                        len(cleaned),
                    )
                    return cleaned

                partial_candidates.append((source, cleaned))
                circuit.record_inconclusive(source_key)
                errors.append(f"{source}: partial history rows={len(cleaned)}")
            except Exception as exc:
                circuit.record_failure(source_key, str(exc))
                errors.append(f"{source}: {type(exc).__name__}: {exc}")
                logger.info(
                    "[公开行情-auto] %s 日线源 %s 失败，继续 fallback: %s",
                    code.canonical,
                    source,
                    exc,
                )

        if partial_candidates:
            source, best = max(partial_candidates, key=lambda item: len(item[1]))
            errors.append(f"best partial source={source}, rows={len(best)}")

        raise DataFetchError(
            f"公开行情源均未返回 {code.canonical} 日线: {'; '.join(errors) or 'unsupported'}"
        )

    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        normalized = df.copy()
        normalized["code"] = normalize_stock_code(stock_code)
        for column in STANDARD_COLUMNS:
            if column not in normalized.columns:
                normalized[column] = 0.0 if column != "date" else pd.NaT
        ordered = ["code", *STANDARD_COLUMNS]
        result = normalized[ordered]
        result.attrs.update(df.attrs)
        return result

    def get_realtime_quote(
        self,
        stock_code: str,
        *,
        source: str = "auto",
    ) -> UnifiedRealtimeQuote | None:
        code = resolve_public_market_code(stock_code)
        normalized_source = str(source or "auto").strip().lower()
        if normalized_source != "auto" and normalized_source not in SUPPORTED_PUBLIC_SOURCES:
            raise ValueError(f"Unsupported public market source: {source}")
        cache_key = f"{normalized_source}:{code.cache_identity}"
        cached = self._get_cached_quote(cache_key)
        if cached is not None:
            return cached

        deadline = time.monotonic() + self.overall_timeout_seconds
        key_lock = self._get_quote_key_lock(cache_key)
        if not self._acquire_before_deadline(key_lock, deadline):
            logger.info("[公开行情-auto] %s 等待同代码请求超时", code.canonical)
            return None
        try:
            cached = self._get_cached_quote(cache_key)
            if cached is not None:
                return cached

            sources = self.source_order if normalized_source == "auto" else (normalized_source,)
            quote = self._fetch_quote_with_fallback(code, sources, deadline)
            if quote is not None:
                self._cache_quote(cache_key, quote)
                self._cache_quote(f"{quote.source.value}:{code.cache_identity}", quote)
                self._cache_quote(f"auto:{code.cache_identity}", quote)
            return replace(quote) if quote is not None else None
        finally:
            key_lock.release()

    def get_realtime_quotes(
        self,
        stock_codes: Iterable[str],
        *,
        source: str = "auto",
    ) -> list[UnifiedRealtimeQuote]:
        """Fetch a small/medium watchlist efficiently and warm the quote cache."""
        normalized_source = str(source or "auto").strip().lower()
        if normalized_source != "auto" and normalized_source not in SUPPORTED_PUBLIC_SOURCES:
            raise ValueError(f"Unsupported public market source: {source}")

        codes: list[PublicMarketCode] = []
        seen: set[str] = set()
        for stock_code in stock_codes:
            try:
                code = resolve_public_market_code(stock_code)
            except DataFetchError:
                continue
            if code.cache_identity not in seen:
                seen.add(code.cache_identity)
                codes.append(code)

        if not codes:
            return []

        results: dict[str, UnifiedRealtimeQuote] = {}
        pending: list[PublicMarketCode] = []

        def merge_cached(candidates: Iterable[PublicMarketCode]) -> None:
            for candidate in candidates:
                cached_quote = self._get_cached_quote(
                    f"{normalized_source}:{candidate.cache_identity}"
                )
                if cached_quote is not None:
                    results[candidate.cache_identity] = cached_quote

        for code in codes:
            cached = self._get_cached_quote(f"{normalized_source}:{code.cache_identity}")
            if cached is not None:
                results[code.cache_identity] = cached
            else:
                pending.append(code)

        if pending:
            deadline = time.monotonic() + self.overall_timeout_seconds
            if not self._acquire_before_deadline(self._batch_lock, deadline):
                merge_cached(pending)
                logger.info("[公开行情-auto] 批量请求等待超时，返回已有缓存")
            else:
                try:
                    sources = (
                        self.source_order
                        if normalized_source == "auto"
                        else (normalized_source,)
                    )
                    # Another batch may have filled the cache while this request waited.
                    merge_cached(pending)
                    unresolved = [
                        code
                        for code in pending
                        if code.cache_identity not in results
                    ]
                    circuit = get_realtime_circuit_breaker()
                    for provider in sources:
                        if not unresolved or time.monotonic() >= deadline:
                            break
                        source_key = f"public_{provider}_quote"
                        if not circuit.is_available(source_key):
                            continue
                        provider_codes = [
                            code
                            for code in unresolved
                            if self._supports(provider, "quote", code.market)
                        ]
                        if not provider_codes:
                            continue
                        try:
                            quotes = self._fetch_quotes_from_source(
                                provider,
                                provider_codes,
                                deadline,
                            )
                        except Exception as exc:
                            circuit.record_failure(source_key, str(exc))
                            logger.info(
                                "[公开行情-auto] 批量实时源 %s 失败，继续 fallback: %s",
                                provider,
                                exc,
                            )
                            continue
                        if quotes:
                            circuit.record_success(source_key)
                        else:
                            circuit.record_inconclusive(source_key)
                        for cache_identity, quote in quotes.items():
                            if quote.has_basic_data():
                                results[cache_identity] = quote
                                self._cache_quote(
                                    f"{normalized_source}:{cache_identity}", quote
                                )
                                self._cache_quote(f"{provider}:{cache_identity}", quote)
                                self._cache_quote(f"auto:{cache_identity}", quote)
                        unresolved = [
                            code
                            for code in unresolved
                            if code.cache_identity not in results
                        ]
                finally:
                    self._batch_lock.release()

        return [
            replace(results[code.cache_identity])
            for code in codes
            if code.cache_identity in results
        ]

    def _fetch_quote_with_fallback(
        self,
        code: PublicMarketCode,
        sources: Iterable[str],
        deadline: float,
    ) -> UnifiedRealtimeQuote | None:
        errors: list[str] = []
        circuit = get_realtime_circuit_breaker()
        for source in sources:
            if not self._supports(source, "quote", code.market):
                continue
            source_key = f"public_{source}_quote"
            if not circuit.is_available(source_key):
                errors.append(f"{source}: circuit open")
                continue
            if time.monotonic() >= deadline:
                errors.append("overall timeout exceeded")
                break
            try:
                quotes = self._fetch_quotes_from_source(source, [code], deadline)
                quote = quotes.get(code.cache_identity)
                if quote is not None and quote.has_basic_data():
                    circuit.record_success(source_key)
                    logger.info(
                        "[公开行情-auto] %s 实时行情使用 %s",
                        code.canonical,
                        source,
                    )
                    return quote
                circuit.record_inconclusive(source_key)
                errors.append(f"{source}: empty quote")
            except Exception as exc:
                circuit.record_failure(source_key, str(exc))
                errors.append(f"{source}: {type(exc).__name__}: {exc}")
                logger.info(
                    "[公开行情-auto] %s 实时源 %s 失败，继续 fallback: %s",
                    code.canonical,
                    source,
                    exc,
                )
        logger.info(
            "[公开行情-auto] %s 无可用实时行情: %s",
            code.canonical,
            "; ".join(errors) or "unsupported",
        )
        return None

    def _fetch_quotes_from_source(
        self,
        source: str,
        codes: list[PublicMarketCode],
        deadline: float,
    ) -> dict[str, UnifiedRealtimeQuote]:
        if source == "tencent":
            return self._fetch_tencent_quotes(codes, deadline)
        if source == "sina":
            return self._fetch_sina_quotes(codes, deadline)
        if source == "eastmoney":
            return self._fetch_eastmoney_quotes(codes, deadline)
        return []

    def _fetch_tencent_quotes(
        self,
        codes: list[PublicMarketCode],
        deadline: float,
    ) -> dict[str, UnifiedRealtimeQuote]:
        results: dict[str, UnifiedRealtimeQuote] = {}
        for chunk_start in range(0, len(codes), 50):
            chunk = codes[chunk_start : chunk_start + 50]
            symbols = [code.tencent_symbol for code in chunk if code.tencent_symbol]
            if not symbols:
                continue
            response = self._request(
                TENCENT_QUOTE_URL.format(symbols=",".join(symbols)),
                deadline,
                headers={"Referer": "https://finance.qq.com/"},
            )
            text = response.content.decode("gbk", errors="replace")
            payload_by_symbol = {
                symbol.lower(): payload
                for symbol, payload in _TENCENT_ROW_PATTERN.findall(text)
            }
            for code in chunk:
                symbol = code.tencent_symbol
                payload = payload_by_symbol.get((symbol or "").lower())
                if payload:
                    quote = self._parse_tencent_quote(code, payload.split("~"))
                    if quote is not None:
                        results[code.cache_identity] = quote
        return results

    @staticmethod
    def _parse_tencent_quote(
        code: PublicMarketCode,
        fields: list[str],
    ) -> UnifiedRealtimeQuote | None:
        if len(fields) < 35:
            return None
        price = safe_float(fields[3])
        pre_close = safe_float(fields[4])
        if price is None or price <= 0:
            return None
        volume_value = safe_int(fields[6])
        if volume_value is not None and code.market in {"cn", "bse"}:
            volume_value *= 100
        amount = safe_float(fields[37]) if len(fields) > 37 else None
        if amount is not None and code.market in {"cn", "bse"}:
            amount *= 10000
        change_amount = safe_float(fields[31]) if len(fields) > 31 else None
        change_pct = safe_float(fields[32]) if len(fields) > 32 else None
        if change_amount is None and pre_close:
            change_amount = price - pre_close
        if change_pct is None and pre_close:
            change_pct = (price / pre_close - 1) * 100

        if code.market in {"cn", "bse"}:
            turnover_rate = safe_float(fields[38]) if len(fields) > 38 else None
            pe_ratio = safe_float(fields[39]) if len(fields) > 39 else None
            amplitude = safe_float(fields[43]) if len(fields) > 43 else None
            circ_mv = _scaled_market_cap(fields, 44)
            total_mv = _scaled_market_cap(fields, 45)
            pb_ratio = safe_float(fields[46]) if len(fields) > 46 else None
            volume_ratio = safe_float(fields[49]) if len(fields) > 49 else None
            high_52w = None
            low_52w = None
        elif code.market == "hk":
            turnover_rate = safe_float(fields[50]) if len(fields) > 50 else None
            pe_ratio = safe_float(fields[39]) if len(fields) > 39 else None
            amplitude = safe_float(fields[43]) if len(fields) > 43 else None
            circ_mv = _scaled_market_cap(fields, 44)
            total_mv = _scaled_market_cap(fields, 45)
            pb_ratio = safe_float(fields[58]) if len(fields) > 58 else None
            volume_ratio = safe_float(fields[47]) if len(fields) > 47 else None
            high_52w = safe_float(fields[48]) if len(fields) > 48 else None
            low_52w = safe_float(fields[49]) if len(fields) > 49 else None
        else:
            turnover_rate = safe_float(fields[38]) if len(fields) > 38 else None
            pe_ratio = safe_float(fields[39]) if len(fields) > 39 else None
            amplitude = safe_float(fields[43]) if len(fields) > 43 else None
            circ_mv = _scaled_market_cap(fields, 44)
            total_mv = _scaled_market_cap(fields, 45)
            pb_ratio = None
            volume_ratio = None
            high_52w = safe_float(fields[48]) if len(fields) > 48 else None
            low_52w = safe_float(fields[49]) if len(fields) > 49 else None

        return UnifiedRealtimeQuote(
            code=code.canonical,
            name=str(fields[1] or "").strip(),
            source=RealtimeSource.TENCENT,
            observed_at=(
                normalize_cn_quote_observed_at(fields[30])
                if code.market in {"cn", "bse", "hk"} and len(fields) > 30
                else None
            ),
            price=price,
            change_pct=change_pct,
            change_amount=change_amount,
            volume=volume_value,
            amount=amount,
            open_price=safe_float(fields[5]),
            high=safe_float(fields[33]),
            low=safe_float(fields[34]),
            pre_close=pre_close,
            turnover_rate=turnover_rate,
            pe_ratio=pe_ratio,
            amplitude=amplitude,
            circ_mv=circ_mv,
            total_mv=total_mv,
            pb_ratio=pb_ratio,
            volume_ratio=volume_ratio,
            high_52w=high_52w,
            low_52w=low_52w,
        )

    def _fetch_sina_quotes(
        self,
        codes: list[PublicMarketCode],
        deadline: float,
    ) -> dict[str, UnifiedRealtimeQuote]:
        symbols = [code.sina_symbol for code in codes if code.sina_symbol]
        if not symbols:
            return []
        response = self._request(
            SINA_QUOTE_URL.format(symbols=",".join(symbols)),
            deadline,
            headers={"Referer": "https://finance.sina.com.cn/"},
        )
        text = response.content.decode("gb18030", errors="replace")
        payload_by_symbol = {
            symbol.lower(): payload for symbol, payload in _SINA_ROW_PATTERN.findall(text)
        }
        results: dict[str, UnifiedRealtimeQuote] = {}
        for code in codes:
            payload = payload_by_symbol.get((code.sina_symbol or "").lower())
            if not payload:
                continue
            quote = self._parse_sina_quote(code, payload.split(","))
            if quote is not None:
                results[code.cache_identity] = quote
        return results

    @staticmethod
    def _parse_sina_quote(
        code: PublicMarketCode,
        fields: list[str],
    ) -> UnifiedRealtimeQuote | None:
        if code.market == "cn" and len(fields) >= 10:
            name, open_value, pre_close, price, high, low = (
                fields[0], fields[1], fields[2], fields[3], fields[4], fields[5]
            )
            volume, amount = fields[8], fields[9]
            extra: dict[str, Any] = {
                "observed_at": normalize_cn_quote_observed_at(
                    fields[30] if len(fields) > 30 else None,
                    fields[31] if len(fields) > 31 else None,
                ),
            }
        elif code.market == "hk" and len(fields) >= 13:
            name, open_value, pre_close, price, high, low = (
                fields[1], fields[2], fields[3], fields[6], fields[4], fields[5]
            )
            volume, amount = fields[12], fields[11]
            extra = {
                "change_amount": safe_float(fields[7]),
                "change_pct": safe_float(fields[8]),
                "high_52w": safe_float(fields[15]) if len(fields) > 15 else None,
                "low_52w": safe_float(fields[16]) if len(fields) > 16 else None,
            }
        elif code.market == "us" and len(fields) >= 31:
            name, open_value, pre_close, price, high, low = (
                fields[0], fields[5], fields[26], fields[1], fields[6], fields[7]
            )
            volume, amount = fields[10], fields[30]
            shares_outstanding = safe_float(fields[19]) if len(fields) > 19 else None
            volume_value = safe_float(volume)
            turnover_rate = None
            if shares_outstanding and volume_value is not None:
                turnover_rate = volume_value / shares_outstanding * 100
            extra = {
                "change_amount": safe_float(fields[4]),
                "change_pct": safe_float(fields[2]),
                "total_mv": safe_float(fields[12]),
                "pe_ratio": safe_float(fields[14]),
                "turnover_rate": turnover_rate,
                "high_52w": safe_float(fields[8]),
                "low_52w": safe_float(fields[9]),
            }
        else:
            return None

        price_value = safe_float(price)
        pre_close_value = safe_float(pre_close)
        if price_value is None or price_value <= 0:
            return None
        change_amount = extra.get("change_amount")
        change_pct = extra.get("change_pct")
        if change_amount is None and pre_close_value:
            change_amount = price_value - pre_close_value
        if change_pct is None and pre_close_value:
            change_pct = (price_value / pre_close_value - 1) * 100

        return UnifiedRealtimeQuote(
            code=code.canonical,
            name=str(name or "").strip(),
            source=RealtimeSource.SINA,
            observed_at=extra.get("observed_at"),
            price=price_value,
            change_pct=change_pct,
            change_amount=change_amount,
            volume=safe_int(volume),
            amount=safe_float(amount),
            open_price=safe_float(open_value),
            high=safe_float(high),
            low=safe_float(low),
            pre_close=pre_close_value,
            turnover_rate=extra.get("turnover_rate"),
            pe_ratio=extra.get("pe_ratio"),
            total_mv=extra.get("total_mv"),
            high_52w=extra.get("high_52w"),
            low_52w=extra.get("low_52w"),
        )

    def _fetch_eastmoney_quotes(
        self,
        codes: list[PublicMarketCode],
        deadline: float,
    ) -> dict[str, UnifiedRealtimeQuote]:
        results: dict[str, UnifiedRealtimeQuote] = {}
        errors: list[str] = []
        received_valid_response = False
        fields = (
            "f43,f44,f45,f46,f47,f48,f57,f58,f60,f116,f117,"
            "f162,f164,f167,f168,f170"
        )
        for code in codes:
            if time.monotonic() >= deadline:
                break
            quote_data: dict[str, Any] | None = None
            for secid in code.eastmoney_secids:
                for host in EASTMONEY_QUOTE_HOSTS:
                    if time.monotonic() >= deadline:
                        break
                    try:
                        response = self._request(
                            f"https://{host}/api/qt/stock/get",
                            deadline,
                            params={
                                "fltt": "2",
                                "invt": "2",
                                "secid": secid,
                                "fields": fields,
                            },
                            headers={"Referer": "https://quote.eastmoney.com/"},
                        )
                        payload = response.json()
                        received_valid_response = True
                        data = payload.get("data") if isinstance(payload, dict) else None
                        if isinstance(data, dict) and (data.get("f57") or data.get("f58")):
                            quote_data = data
                            break
                    except requests.exceptions.ProxyError:
                        raise
                    except (requests.RequestException, ValueError) as exc:
                        errors.append(f"{host}: {type(exc).__name__}: {exc}")
                        continue
                if quote_data is not None:
                    break
            if quote_data is not None:
                quote = self._parse_eastmoney_quote(code, quote_data)
                if quote is not None:
                    results[code.cache_identity] = quote
        if not results and errors and not received_valid_response:
            raise DataFetchError(
                "东方财富实时行情所有候选地址均失败: " + "; ".join(errors)
            )
        return results

    @staticmethod
    def _parse_eastmoney_quote(
        code: PublicMarketCode,
        data: dict[str, Any],
    ) -> UnifiedRealtimeQuote | None:
        price = safe_float(data.get("f43"))
        pre_close = safe_float(data.get("f60"))
        if price is None or price <= 0:
            return None
        volume = safe_int(data.get("f47"))
        if volume is not None and code.market in {"cn", "bse"}:
            volume *= 100
        change_amount = price - pre_close if pre_close else None
        change_pct = safe_float(data.get("f170"))
        if change_pct is None and pre_close:
            change_pct = (price / pre_close - 1) * 100
        high = safe_float(data.get("f44"))
        low = safe_float(data.get("f45"))
        amplitude = None
        if high is not None and low is not None and pre_close:
            amplitude = (high - low) / pre_close * 100

        return UnifiedRealtimeQuote(
            code=code.canonical,
            name=str(data.get("f58") or data.get("f57") or "").strip(),
            source=RealtimeSource.EASTMONEY,
            price=price,
            change_pct=change_pct,
            change_amount=change_amount,
            volume=volume,
            amount=safe_float(data.get("f48")),
            open_price=safe_float(data.get("f46")),
            high=high,
            low=low,
            pre_close=pre_close,
            turnover_rate=safe_float(data.get("f168")),
            amplitude=amplitude,
            pe_ratio=safe_float(data.get("f162")) or safe_float(data.get("f164")),
            pb_ratio=safe_float(data.get("f167")),
            total_mv=safe_float(data.get("f116")),
            circ_mv=safe_float(data.get("f117")),
        )

    def _fetch_history_from_source(
        self,
        source: str,
        code: PublicMarketCode,
        start: date,
        end: date,
        deadline: float,
    ) -> pd.DataFrame:
        if source == "tencent":
            return self._fetch_tencent_history(code, start, end, deadline)
        if source == "sina":
            return self._fetch_sina_history(code, start, end, deadline)
        if source == "eastmoney":
            return self._fetch_eastmoney_history(code, start, end, deadline)
        return pd.DataFrame()

    def _fetch_tencent_history(
        self,
        code: PublicMarketCode,
        start: date,
        end: date,
        deadline: float,
    ) -> pd.DataFrame:
        symbol = code.tencent_symbol
        if not symbol:
            return pd.DataFrame()
        count = _history_request_count(start, end)
        response = self._request(
            TENCENT_KLINE_URL,
            deadline,
            params={"param": f"{symbol},day,,,{count},qfq"},
            headers={"Referer": "https://gu.qq.com/"},
        )
        payload = response.json()
        data = payload.get("data", {}).get(symbol, {}) if isinstance(payload, dict) else {}
        rows = data.get("qfqday") or []
        return pd.DataFrame(
            [
                {
                    "date": row[0],
                    "open": row[1],
                    "close": row[2],
                    "high": row[3],
                    "low": row[4],
                    "volume": _normalize_history_volume(
                        row[5] if len(row) > 5 else None,
                        code.market,
                    ),
                }
                for row in rows
                if isinstance(row, list) and len(row) >= 5
            ]
        )

    def _fetch_sina_history(
        self,
        code: PublicMarketCode,
        start: date,
        end: date,
        deadline: float,
    ) -> pd.DataFrame:
        symbol = code.sina_symbol
        if not symbol:
            return pd.DataFrame()
        response = self._request(
            SINA_KLINE_URL,
            deadline,
            params={
                "symbol": symbol,
                "scale": "240",
                "ma": "no",
                "datalen": str(_history_request_count(start, end)),
            },
            headers={"Referer": "https://finance.sina.com.cn/"},
        )
        payload = response.json()
        if not isinstance(payload, list):
            return pd.DataFrame()
        return pd.DataFrame(
            [
                {
                    "date": row.get("day"),
                    "open": row.get("open"),
                    "close": row.get("close"),
                    "high": row.get("high"),
                    "low": row.get("low"),
                    "volume": row.get("volume"),
                }
                for row in payload
                if isinstance(row, dict)
            ]
        )

    def _fetch_eastmoney_history(
        self,
        code: PublicMarketCode,
        start: date,
        end: date,
        deadline: float,
    ) -> pd.DataFrame:
        errors: list[str] = []
        received_valid_response = False
        deadline_exhausted = False
        for secid in code.eastmoney_secids:
            for host in EASTMONEY_KLINE_HOSTS:
                if time.monotonic() >= deadline:
                    errors.append("overall timeout exceeded")
                    deadline_exhausted = True
                    break
                try:
                    response = self._request(
                        f"https://{host}/api/qt/stock/kline/get",
                        deadline,
                        params={
                            "fields1": "f1,f2,f3,f4,f5,f6",
                            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                            "ut": "7eea3edcaed734bea9cbfc24409ed989",
                            "klt": "101",
                            "fqt": "1",
                            "secid": secid,
                            "beg": start.strftime("%Y%m%d"),
                            "end": end.strftime("%Y%m%d"),
                            "lmt": str(_history_request_count(start, end)),
                        },
                        headers={"Referer": "https://quote.eastmoney.com/"},
                    )
                    payload = response.json()
                    received_valid_response = True
                    data = payload.get("data") if isinstance(payload, dict) else None
                    rows = data.get("klines") if isinstance(data, dict) else None
                    if rows:
                        parsed_rows = []
                        for line in rows:
                            fields = str(line).split(",")
                            if len(fields) < 6:
                                continue
                            parsed_rows.append(
                                {
                                    "date": fields[0],
                                    "open": fields[1],
                                    "close": fields[2],
                                    "high": fields[3],
                                    "low": fields[4],
                                    "volume": _normalize_history_volume(fields[5], code.market),
                                    "amount": fields[6] if len(fields) > 6 else None,
                                    "pct_chg": fields[8] if len(fields) > 8 else None,
                                }
                            )
                        return pd.DataFrame(parsed_rows)
                except requests.exceptions.ProxyError:
                    raise
                except (requests.RequestException, ValueError) as exc:
                    errors.append(f"{host}: {type(exc).__name__}: {exc}")
                    continue
            if deadline_exhausted:
                break
        if errors and not received_valid_response:
            raise DataFetchError(
                "东方财富历史行情所有候选地址均失败: " + "; ".join(errors)
            )
        return pd.DataFrame()

    def _sanitize_history(
        self,
        df: pd.DataFrame,
        code: PublicMarketCode,
        start: date,
        end: date,
        source: str,
    ) -> pd.DataFrame:
        if df is None or df.empty:
            return pd.DataFrame()
        cleaned = df.copy()
        if "date" not in cleaned.columns:
            return pd.DataFrame()
        cleaned["date"] = pd.to_datetime(cleaned["date"], errors="coerce")
        for column in ("open", "high", "low", "close", "volume", "amount", "pct_chg"):
            if column not in cleaned.columns:
                cleaned[column] = math.nan
            cleaned[column] = pd.to_numeric(cleaned[column], errors="coerce")

        cleaned = cleaned[
            cleaned["date"].notna()
            & (cleaned["date"].dt.date >= start)
            & (cleaned["date"].dt.date <= end)
        ]
        # Provider payload order is authoritative for duplicate dates. Keep the
        # final row first, then validate it rather than reviving an older value.
        cleaned = cleaned.drop_duplicates(subset=["date"], keep="last")
        cleaned = cleaned.dropna(subset=["open", "high", "low", "close", "volume"])
        cleaned = cleaned[
            (cleaned["open"] > 0)
            & (cleaned["high"] > 0)
            & (cleaned["low"] > 0)
            & (cleaned["close"] > 0)
            & (cleaned["volume"] >= 0)
            & (cleaned["high"] >= cleaned[["open", "close", "low"]].max(axis=1))
            & (cleaned["low"] <= cleaned[["open", "close", "high"]].min(axis=1))
        ]
        cleaned = cleaned.sort_values("date").reset_index(drop=True)
        if cleaned.empty:
            return cleaned

        if cleaned["pct_chg"].isna().all():
            cleaned["pct_chg"] = cleaned["close"].pct_change() * 100
        else:
            calculated = cleaned["close"].pct_change() * 100
            cleaned["pct_chg"] = cleaned["pct_chg"].fillna(calculated)
        cleaned["pct_chg"] = cleaned["pct_chg"].fillna(0.0)

        # A qfq close multiplied by actual volume is not the real traded amount
        # around corporate actions. Keep amount missing when the provider omits it.
        cleaned["code"] = code.canonical
        cleaned.attrs["source"] = f"public_{source}"
        return cleaned[["code", *STANDARD_COLUMNS]]

    @staticmethod
    def _history_has_sufficient_coverage(df: pd.DataFrame, start: date, end: date) -> bool:
        if df.empty:
            return False
        latest = pd.to_datetime(df["date"], errors="coerce").max()
        if pd.isna(latest) or latest.date() < end - timedelta(days=7):
            return False
        weekdays = sum(
            1
            for offset in range((end - start).days + 1)
            if (start + timedelta(days=offset)).weekday() < 5
        )
        minimum_records = max(1, math.ceil(weekdays * MIN_HISTORY_WEEKDAY_COVERAGE))
        return len(df) >= minimum_records

    @staticmethod
    def _supports(source: str, capability: str, market: str) -> bool:
        if market == "us_index":
            return False
        if source == "tencent":
            if capability == "history":
                # The adjusted endpoint returns qfqday for mainland A-shares.
                # HK/BSE currently fall back to an unadjusted `day` payload, and
                # US rows can be sparse or mixed-era, so do not accept them.
                return market == "cn"
            return market in {"cn", "bse", "hk", "us"}
        if source == "sina":
            if capability == "history":
                # Sina's public K-line endpoint has no adjusted-price mode.
                return False
            return market in {"cn", "hk", "us"}
        if source == "eastmoney":
            return market in {"cn", "bse", "hk", "us"}
        return False

    def _request(
        self,
        url: str,
        deadline: float,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
    ) -> requests.Response:
        if self._closed:
            raise DataFetchError("public market fetcher is closed")
        if not self._acquire_before_deadline(self._request_lock, deadline):
            raise TimeoutError("public market overall timeout exceeded while queued")

        response: requests.Response | None = None
        try:
            now = time.monotonic()
            wait_seconds = self.min_interval_seconds - (now - self._last_request_at)
            if wait_seconds > 0:
                if now + wait_seconds >= deadline:
                    raise TimeoutError("public market overall timeout exceeded")
                time.sleep(wait_seconds)

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("public market overall timeout exceeded")
            timeout = max(0.001, min(self.timeout_seconds, remaining))
            request_headers = {
                "Accept": "*/*",
                "User-Agent": "Mozilla/5.0 (compatible; daily-stock-analysis/1.0)",
            }
            request_headers.update(headers or {})
            response = self._start_response_before_deadline(
                url,
                deadline,
                request_headers=request_headers,
                params=params,
                timeout=timeout,
            )
            try:
                body = self._read_response_body_before_deadline(response, deadline)
            except TimeoutError:
                # Closing the response interrupts the bounded reader in requests/urllib3.
                self._close_response_safely(response)
                response = None
                raise
            response._content = body
            response._content_consumed = True
            return response
        finally:
            try:
                self._close_response_safely(response)
            finally:
                self._last_request_at = time.monotonic()
                self._request_lock.release()

    def _start_response_before_deadline(
        self,
        url: str,
        deadline: float,
        *,
        request_headers: dict[str, str],
        params: dict[str, str] | None,
        timeout: float,
    ) -> requests.Response:
        """Bound connection, redirects, and response headers by the operation deadline."""
        if not self._acquire_before_deadline(self._response_reader_slots, deadline):
            raise TimeoutError("public market overall timeout exceeded while waiting for transport")
        slot_lease = _WorkerSlotLease(self._response_reader_slots)

        completed = Event()
        state_lock = Lock()
        state: dict[str, Any] = {"cancelled": False}

        def fetch_response_headers() -> None:
            if not slot_lease.claim_for_worker():
                completed.set()
                return
            response: requests.Response | None = None
            acquired_transport = False
            try:
                with state_lock:
                    if state["cancelled"]:
                        return
                acquired_transport = self._acquire_before_deadline(
                    self._transport_lock,
                    deadline,
                )
                if not acquired_transport:
                    raise TimeoutError(
                        "public market overall timeout exceeded while waiting for transport"
                    )
                with state_lock:
                    if state["cancelled"]:
                        return
                response = self.session.get(
                    url,
                    headers=request_headers,
                    params=params,
                    timeout=(timeout, timeout),
                    stream=True,
                    allow_redirects=False,
                )
                status_code = getattr(response, "status_code", 200)
                try:
                    status_code = int(status_code)
                except (TypeError, ValueError):
                    status_code = 200
                if 300 <= status_code < 400:
                    raise requests.TooManyRedirects(
                        "public market redirects are disabled"
                    )
                response.raise_for_status()
                with state_lock:
                    if not state["cancelled"]:
                        state["response"] = response
                        response = None
            except BaseException as exc:
                with state_lock:
                    if not state["cancelled"]:
                        state["error"] = exc
            finally:
                try:
                    self._close_response_safely(response)
                finally:
                    try:
                        if acquired_transport:
                            self._transport_lock.release()
                    finally:
                        completed.set()
                        slot_lease.release_from_worker()

        try:
            worker = Thread(
                target=fetch_response_headers,
                name="public-market-response-headers",
                daemon=True,
            )
            worker.start()
        except BaseException:
            with state_lock:
                state["cancelled"] = True
                published_response = state.pop("response", None)
            self._close_response_safely(published_response)
            slot_lease.release_from_caller()
            raise
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not completed.wait(remaining):
            with state_lock:
                state["cancelled"] = True
                late_response = state.pop("response", None)
            self._close_response_safely(late_response)
            raise TimeoutError("public market overall timeout exceeded while awaiting headers")

        with state_lock:
            error = state.get("error")
            response = state.get("response")
        if error is not None:
            raise error
        if response is None:
            raise TimeoutError("public market overall timeout exceeded while awaiting headers")
        return response

    def _read_response_body_before_deadline(
        self,
        response: requests.Response,
        deadline: float,
    ) -> bytes:
        """Read a streamed body without allowing an idle socket to exceed the total deadline."""
        if not self._acquire_before_deadline(self._response_reader_slots, deadline):
            raise TimeoutError("public market overall timeout exceeded while waiting for reader")
        slot_lease = _WorkerSlotLease(self._response_reader_slots)

        completed = Event()
        cancelled = Event()
        result: dict[str, Any] = {}

        def consume() -> None:
            if not slot_lease.claim_for_worker():
                completed.set()
                return
            acquired_transport = False
            try:
                if cancelled.is_set():
                    return
                acquired_transport = self._acquire_before_deadline(
                    self._transport_lock,
                    deadline,
                )
                if not acquired_transport:
                    raise TimeoutError(
                        "public market overall timeout exceeded while waiting for transport"
                    )
                if cancelled.is_set():
                    return
                body = bytearray()
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if cancelled.is_set():
                        return
                    if not chunk:
                        continue
                    body.extend(chunk)
                    if len(body) > MAX_PUBLIC_RESPONSE_BYTES:
                        raise DataFetchError("public market response exceeds size limit")
                result["body"] = bytes(body)
            except BaseException as exc:  # Propagate reader failures to the request thread.
                result["error"] = exc
            finally:
                if acquired_transport:
                    self._transport_lock.release()
                completed.set()
                slot_lease.release_from_worker()

        try:
            worker = Thread(
                target=consume,
                name="public-market-response-reader",
                daemon=True,
            )
            worker.start()
        except BaseException:
            cancelled.set()
            slot_lease.release_from_caller()
            raise
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not completed.wait(remaining):
            cancelled.set()
            raise TimeoutError("public market overall timeout exceeded while reading")

        error = result.get("error")
        if error is not None:
            raise error
        return result.get("body", b"")

    @classmethod
    def _get_quote_key_lock(cls, cache_key: str) -> RLock:
        return cls._quote_key_locks[hash(cache_key) % len(cls._quote_key_locks)]

    @classmethod
    def _get_cached_quote(cls, cache_key: str) -> UnifiedRealtimeQuote | None:
        now = time.monotonic()
        with cls._quote_cache_lock:
            cached = cls._quote_cache.get(cache_key)
            if cached is None:
                return None
            if cached.expires_at <= now:
                cls._quote_cache.pop(cache_key, None)
                return None
            return replace(cached.quote)

    def _cache_quote(self, cache_key: str, quote: UnifiedRealtimeQuote) -> None:
        if self.quote_cache_ttl_seconds <= 0:
            return
        now = time.monotonic()
        with self._quote_cache_lock:
            if len(self._quote_cache) >= self._cache_max_entries:
                expired = [
                    key for key, value in self._quote_cache.items() if value.expires_at <= now
                ]
                for key in expired:
                    self._quote_cache.pop(key, None)
                while len(self._quote_cache) >= self._cache_max_entries:
                    self._quote_cache.pop(next(iter(self._quote_cache)), None)
            self._quote_cache[cache_key] = _CachedQuote(
                quote=replace(quote),
                expires_at=now + self.quote_cache_ttl_seconds,
            )

    @classmethod
    def clear_quote_cache(cls) -> None:
        with cls._quote_cache_lock:
            cls._quote_cache.clear()


def _history_request_count(start: date, end: date) -> int:
    calendar_days = max(1, (end - start).days + 1)
    return min(1000, max(30, math.ceil(calendar_days * 5 / 7) + 20))


def _scaled_market_cap(fields: list[str], index: int) -> float | None:
    if len(fields) <= index:
        return None
    value = safe_float(fields[index])
    return value * 100000000 if value is not None else None


__all__ = [
    "PublicMarketCode",
    "PublicMarketFetcher",
    "SUPPORTED_PUBLIC_SOURCES",
    "normalize_public_source_order",
    "resolve_public_market_code",
]
