# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Iterable, Literal

import pandas as pd
import requests

from data_provider.realtime_types import safe_float, safe_int
from src.data.stock_index_loader import StockIndexEntry, build_stock_index_lookup_keys

CACHE_VERSION = 1
DEFAULT_CACHE_TTL_HOURS = 72
DEFAULT_MAX_STALE_DAYS = 30
BATCH_SOURCE_TIMEOUT_SECONDS = 30
EASTMONEY_LIGHTWEIGHT_PAGE_SIZE = 20
EASTMONEY_LIGHTWEIGHT_TIMEOUT_SECONDS = 8
EASTMONEY_LIGHTWEIGHT_PAGE_DELAY_SECONDS = 0.25
US_CONCURRENCY = 5
US_OVERALL_TIMEOUT_SECONDS = 30

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
class StockPopularityMetrics:
    code: str
    market: str
    market_cap: float | None = None
    amount: float | None = None
    volume: float | None = None
    avg_volume: float | None = None
    source: str | None = None
    updated_at: datetime | None = None

    @property
    def key(self) -> str:
        return cache_key_for_stock(self.market, self.code)

    @property
    def liquidity_value(self) -> float | None:
        for value in (self.amount, self.avg_volume, self.volume):
            normalized = _positive_float(value)
            if normalized is not None:
                return normalized
        return None

    @property
    def has_effective_metric(self) -> bool:
        return _positive_float(self.market_cap) is not None or self.liquidity_value is not None


_BATCH_CACHE: dict[str, _BatchCache] = {}
_BATCH_CACHE_LOCK = RLock()


def normalize_canonical_code(market: str, code: str) -> str:
    market_upper = str(market or "").strip().upper()
    raw = str(code or "").strip().upper()
    if not raw:
        return raw

    if market_upper == "HK":
        base = raw.removeprefix("HK")
        if base.endswith(".HK"):
            base = base[:-3]
        if base.isdigit() and 1 <= len(base) <= 5:
            return f"{base.zfill(5)}.HK"
        return raw

    if market_upper == "BSE":
        base = raw.removeprefix("BJ")
        if base.endswith(".BJ"):
            base = base[:-3]
        if base.isdigit():
            return f"{base}.BJ"
        return raw

    if market_upper == "CN":
        if raw.startswith(("SH", "SZ")) and raw[2:].isdigit():
            suffix = raw[:2]
            return f"{raw[2:]}.{suffix}"
        if raw.endswith((".SH", ".SZ")):
            return raw
        if raw.isdigit() and len(raw) == 6:
            suffix = "SH" if raw.startswith(("6", "9")) else "SZ"
            return f"{raw}.{suffix}"
        return raw

    if market_upper == "US":
        # Legacy index generation may synthesize AAPL.US-style codes. US class
        # tickers such as BRK.B are already canonical and must keep their dot.
        if raw.endswith(".US"):
            return raw[:-3]
        return raw

    return raw


def cache_key_for_stock(market: str, canonical_code: str) -> str:
    market_upper = str(market or "").strip().upper()
    return f"{market_upper}:{normalize_canonical_code(market_upper, canonical_code)}"


def _positive_float(value: Any) -> float | None:
    normalized = safe_float(value)
    if normalized is None or not math.isfinite(normalized) or normalized <= 0:
        return None
    return normalized


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _percentile(value: float | None, values: list[float]) -> float | None:
    if value is None or not values:
        return None
    unique_values = sorted(set(values))
    if len(unique_values) == 1:
        return 0.5
    lower_count = sum(1 for item in values if item < value)
    return max(0.0, min(1.0, lower_count / (len(values) - 1)))


def _score_metric(cap_pct: float | None, liquidity_pct: float | None) -> int:
    if cap_pct is not None and liquidity_pct is not None:
        value = 100 * (0.45 * cap_pct + 0.45 * liquidity_pct + 0.10)
    elif cap_pct is not None:
        value = 100 * (0.90 * cap_pct + 0.05)
    elif liquidity_pct is not None:
        value = 100 * (0.90 * liquidity_pct + 0.05)
    else:
        return 0
    return max(0, min(100, round(value)))


def compute_popularity_scores(metrics: Iterable[StockPopularityMetrics]) -> dict[str, int]:
    metrics_by_market: dict[str, list[StockPopularityMetrics]] = {}
    for metric in metrics:
        market = str(metric.market or "").upper()
        metrics_by_market.setdefault(market, []).append(metric)

    scores: dict[str, int] = {}
    for market_metrics in metrics_by_market.values():
        cap_values = [value for metric in market_metrics if (value := _positive_float(metric.market_cap)) is not None]
        liquidity_values = [
            value for metric in market_metrics if (value := metric.liquidity_value) is not None
        ]
        for metric in market_metrics:
            cap_pct = _percentile(_positive_float(metric.market_cap), cap_values)
            liquidity_pct = _percentile(metric.liquidity_value, liquidity_values)
            scores[metric.key] = _score_metric(cap_pct, liquidity_pct)
    return scores


def load_popularity_cache(cache_path: Path) -> dict[str, Any]:
    if not cache_path.is_file():
        return {}
    try:
        with cache_path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def get_cache_scores(cache_payload: dict[str, Any], now: datetime | None = None) -> dict[str, int]:
    if cache_payload.get("version") != CACHE_VERSION:
        return {}
    max_stale_days = int(cache_payload.get("max_stale_days") or DEFAULT_MAX_STALE_DAYS)
    entries = cache_payload.get("entries")
    if not isinstance(entries, dict):
        return {}

    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    scores: dict[str, int] = {}
    for key, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        updated_at = _parse_datetime(entry.get("updated_at"))
        if updated_at is None or (now - updated_at).days > max_stale_days:
            continue
        try:
            score = int(entry.get("score"))
        except (TypeError, ValueError):
            continue
        scores[str(key)] = max(0, min(100, score))
    return scores


def build_popularity_cache(
    metrics: Iterable[StockPopularityMetrics],
    scores: dict[str, int],
    now: datetime | None = None,
    previous_cache: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    previous_entries = (previous_cache or {}).get("entries") if isinstance(previous_cache, dict) else {}
    entries = dict(previous_entries) if isinstance(previous_entries, dict) else {}
    for metric in metrics:
        key = metric.key
        updated_at = (metric.updated_at or now).astimezone(timezone.utc)
        entries[key] = {
            "code": normalize_canonical_code(metric.market, metric.code),
            "market": metric.market.upper(),
            "score": scores.get(key, 0),
            "status": "fresh",
            "source": metric.source or "unknown",
            "updated_at": updated_at.isoformat(),
            "metrics": {
                "market_cap": metric.market_cap,
                "amount": metric.amount,
                "volume": metric.volume,
                "avg_volume": metric.avg_volume,
            },
        }
    return {
        "version": CACHE_VERSION,
        "updated_at": now.isoformat(),
        "ttl_hours": DEFAULT_CACHE_TTL_HOURS,
        "max_stale_days": DEFAULT_MAX_STALE_DAYS,
        "entries": entries,
    }


def write_popularity_cache_atomic(cache_path: Path, payload: dict[str, Any]) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = cache_path.with_name(f"{cache_path.name}.tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp_path, cache_path)


def _index_item_market_and_code(item: Any) -> tuple[str | None, str | None]:
    if isinstance(item, list):
        if len(item) < 10:
            return None, None
        return str(item[6]), str(item[0])
    if isinstance(item, dict):
        return str(item.get("market") or ""), str(item.get("canonicalCode") or "")
    return None, None


def apply_popularity_to_index_data(
    index_data: list[Any],
    scores: dict[str, int],
    skipped_markets: set[str] | None = None,
    allowed_keys: set[str] | None = None,
) -> tuple[list[Any], dict[str, int]]:
    skipped_markets = {market.upper() for market in (skipped_markets or set())}
    stats = {"updated": 0, "missing": 0, "skipped": 0}
    updated_items: list[Any] = []

    for item in index_data:
        market, code = _index_item_market_and_code(item)
        if not market or not code:
            updated_items.append(item)
            continue
        market_upper = market.upper()
        if market_upper in skipped_markets:
            stats["skipped"] += 1
            updated_items.append(list(item) if isinstance(item, list) else dict(item))
            continue

        key = cache_key_for_stock(market_upper, code)
        if allowed_keys is not None and key not in allowed_keys:
            stats["skipped"] += 1
            updated_items.append(list(item) if isinstance(item, list) else dict(item))
            continue

        score = scores.get(key)
        if score is None:
            score = 0
            stats["missing"] += 1
        else:
            stats["updated"] += 1

        if isinstance(item, list):
            copied = list(item)
            while len(copied) <= 9:
                copied.append(None)
            copied[9] = score
            updated_items.append(copied)
        else:
            copied = dict(item)
            copied["popularity"] = score
            updated_items.append(copied)

    return updated_items, stats


def write_index_json_atomic(index_path: Path, index_data: list[Any]) -> None:
    index_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = index_path.with_name(f"{index_path.name}.tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        fh.write("[\n")
        for index, item in enumerate(index_data):
            json.dump(item, fh, ensure_ascii=False, separators=(",", ":"))
            fh.write(",\n" if index < len(index_data) - 1 else "\n")
        fh.write("]\n")
    os.replace(tmp_path, index_path)


def first_value(row: pd.Series, columns: Iterable[str]) -> Any:
    for column in columns:
        if column in row and pd.notna(row[column]):
            return row[column]
    return None


def metric_from_quote_row(
    entry: StockIndexEntry,
    quote_row: pd.Series,
    market: str,
    source: str | None,
    updated_at: datetime | None,
) -> StockPopularityMetrics:
    return StockPopularityMetrics(
        code=entry.canonical_code,
        market=market,
        market_cap=safe_float(first_value(quote_row, ("总市值", "total_mv", "market_cap", "marketCap"))),
        amount=safe_float(first_value(quote_row, ("成交额", "amount", "turnover"))),
        volume=safe_float(first_value(quote_row, ("成交量", "volume"))),
        avg_volume=safe_float(first_value(quote_row, ("averageVolume", "avg_volume", "average_volume"))),
        source=source,
        updated_at=updated_at,
    )


def build_quote_lookup(df: pd.DataFrame, market: str) -> dict[str, pd.Series]:
    lookup: dict[str, pd.Series] = {}
    for _, row in df.iterrows():
        raw_code = first_value(row, ("代码", "股票代码", "code", "symbol", "ts_code"))
        if raw_code is None:
            continue
        keys = set(build_stock_index_lookup_keys(str(raw_code), str(raw_code)))
        if market == "HK":
            digits = str(raw_code).strip().upper().removeprefix("HK")
            if digits.isdigit() and 1 <= len(digits) <= 5:
                keys.update(build_stock_index_lookup_keys(digits.zfill(5), digits.zfill(5)))
        for key in keys:
            lookup[key.upper()] = row
    return lookup


def dataframe_to_popularity_metrics(
    entries: Iterable[StockIndexEntry],
    df: pd.DataFrame,
    market: str,
    source: str | None,
    updated_at: datetime | None,
) -> list[StockPopularityMetrics]:
    quote_lookup = build_quote_lookup(df, market)
    metrics: list[StockPopularityMetrics] = []
    for entry in entries:
        row = None
        for key in build_stock_index_lookup_keys(entry.canonical_code, entry.display_code):
            row = quote_lookup.get(key.upper())
            if row is not None:
                break
        if row is None:
            continue
        metrics.append(metric_from_quote_row(entry, row, market, source, updated_at))
    return metrics


def call_with_timeout(func: Any, timeout_seconds: float, task_name: str) -> Any:
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(func)
        return future.result(timeout=timeout_seconds)
    except FuturesTimeout as exc:
        raise TimeoutError(f"{task_name} timeout after {timeout_seconds}s") from exc
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def fetch_efinance_batch_quotes() -> pd.DataFrame:
    import efinance as ef
    import data_provider.efinance_fetcher as efinance_fetcher

    source_key = "efinance"
    circuit_breaker = efinance_fetcher.get_realtime_circuit_breaker()
    if not circuit_breaker.is_available(source_key):
        raise RuntimeError(f"{source_key} circuit breaker is open")

    current_time = time.time()
    cache = efinance_fetcher._realtime_cache
    if cache["data"] is not None and current_time - cache["timestamp"] < cache["ttl"]:
        return cache["data"]

    fetcher = efinance_fetcher.EfinanceFetcher()
    fetcher._set_random_user_agent()
    fetcher._enforce_rate_limit()
    try:
        df = efinance_fetcher._ef_call_with_timeout(ef.stock.get_realtime_quotes)
        circuit_breaker.record_success(source_key)
        cache["data"] = df
        cache["timestamp"] = current_time
        return df
    except FuturesTimeout as exc:
        circuit_breaker.record_failure(source_key, "timeout")
        raise TimeoutError("efinance batch quote timeout") from exc
    except Exception as exc:
        circuit_breaker.record_failure(source_key, str(exc))
        raise


def fetch_akshare_cn_batch_quotes() -> pd.DataFrame:
    import akshare as ak
    import data_provider.akshare_fetcher as akshare_fetcher

    source_key = "akshare_em"
    circuit_breaker = akshare_fetcher.get_realtime_circuit_breaker()
    if not circuit_breaker.is_available(source_key):
        raise RuntimeError(f"{source_key} circuit breaker is open")

    current_time = time.time()
    cache = akshare_fetcher._realtime_cache
    if cache["data"] is not None and current_time - cache["timestamp"] < cache["ttl"]:
        return cache["data"]

    fetcher = akshare_fetcher.AkshareFetcher()
    fetcher._set_random_user_agent()
    fetcher._enforce_rate_limit()
    try:
        df = call_with_timeout(
            ak.stock_zh_a_spot_em,
            BATCH_SOURCE_TIMEOUT_SECONDS,
            "ak.stock_zh_a_spot_em",
        )
        circuit_breaker.record_success(source_key)
        cache["data"] = df
        cache["timestamp"] = current_time
        return df
    except Exception as exc:
        circuit_breaker.record_failure(source_key, str(exc))
        raise


def fetch_efinance_etf_batch_quotes() -> pd.DataFrame:
    """Fetch the exchange-traded fund snapshot through the protected efinance adapter."""
    import efinance as ef
    import data_provider.efinance_fetcher as efinance_fetcher

    source_key = "efinance_etf"
    circuit_breaker = efinance_fetcher.get_realtime_circuit_breaker()
    if not circuit_breaker.is_available(source_key):
        raise RuntimeError(f"{source_key} circuit breaker is open")

    current_time = time.time()
    cache = efinance_fetcher._etf_realtime_cache
    if (
        cache["data"] is not None
        and not cache["data"].empty
        and current_time - cache["timestamp"] < cache["ttl"]
    ):
        return cache["data"]

    fetcher = efinance_fetcher.EfinanceFetcher()
    fetcher._set_random_user_agent()
    fetcher._enforce_rate_limit()
    try:
        df = efinance_fetcher._ef_call_with_timeout(ef.stock.get_realtime_quotes, ['ETF'])
        if df is None or df.empty:
            raise RuntimeError("efinance ETF batch quote returned empty data")
        circuit_breaker.record_success(source_key)
        cache["data"] = df
        cache["timestamp"] = current_time
        return df
    except FuturesTimeout as exc:
        circuit_breaker.record_failure(source_key, "timeout")
        raise TimeoutError("efinance ETF batch quote timeout") from exc
    except Exception as exc:
        circuit_breaker.record_failure(source_key, str(exc))
        raise


def fetch_akshare_etf_batch_quotes() -> pd.DataFrame:
    """Fetch the exchange-traded fund snapshot through the protected AkShare adapter."""
    import akshare as ak
    import data_provider.akshare_fetcher as akshare_fetcher

    source_key = "akshare_etf"
    circuit_breaker = akshare_fetcher.get_realtime_circuit_breaker()
    if not circuit_breaker.is_available(source_key):
        raise RuntimeError(f"{source_key} circuit breaker is open")

    current_time = time.time()
    cache = akshare_fetcher._etf_realtime_cache
    if (
        cache["data"] is not None
        and not cache["data"].empty
        and current_time - cache["timestamp"] < cache["ttl"]
    ):
        return cache["data"]

    fetcher = akshare_fetcher.AkshareFetcher()
    fetcher._set_random_user_agent()
    fetcher._enforce_rate_limit()
    try:
        df = call_with_timeout(
            ak.fund_etf_spot_em,
            BATCH_SOURCE_TIMEOUT_SECONDS,
            "ak.fund_etf_spot_em",
        )
        if df is None or df.empty:
            raise RuntimeError("AkShare ETF batch quote returned empty data")
        circuit_breaker.record_success(source_key)
        cache["data"] = df
        cache["timestamp"] = current_time
        return df
    except Exception as exc:
        circuit_breaker.record_failure(source_key, str(exc))
        raise


def fetch_cn_etf_batch_quotes() -> BatchQuoteResult:
    """Fetch A-share ETF quotes with the same efinance -> AkShare fallback contract as stocks."""
    try:
        return BatchQuoteResult(fetch_efinance_etf_batch_quotes(), "efinance_etf", None, "ok")
    except Exception:
        return BatchQuoteResult(fetch_akshare_etf_batch_quotes(), "akshare_etf", None, "ok")


def fetch_cn_batch_quotes() -> BatchQuoteResult:
    try:
        return BatchQuoteResult(fetch_efinance_batch_quotes(), "efinance", None, "ok")
    except Exception:
        return BatchQuoteResult(fetch_akshare_cn_batch_quotes(), "akshare_em", None, "ok")


def _eastmoney_lightweight_fs(market: str) -> str:
    market_upper = str(market or "").upper()
    if market_upper == "BSE":
        return "m:0 t:81 s:2048"
    return "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23"


def _new_eastmoney_session(trust_env: bool = True) -> requests.Session:
    session = requests.Session()
    session.trust_env = trust_env
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
            ),
            "Referer": "https://quote.eastmoney.com/center/gridlist.html",
            "Accept": "application/json,text/plain,*/*",
        }
    )
    return session


def _fetch_eastmoney_lightweight_page(
    session: requests.Session,
    params: dict[str, Any],
) -> dict[str, Any]:
    last_error: Exception | None = None
    for candidate in (session, _new_eastmoney_session(trust_env=False)):
        try:
            response = candidate.get(
                "https://82.push2.eastmoney.com/api/qt/clist/get",
                params=params,
                timeout=EASTMONEY_LIGHTWEIGHT_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict):
                return payload
            raise ValueError("Eastmoney lightweight response is not a JSON object")
        except Exception as exc:
            last_error = exc
    assert last_error is not None
    raise last_error


def _eastmoney_page_size_candidates(page_size: int) -> list[int]:
    candidates = [max(1, min(int(page_size), 100)), 10, 5, 1]
    result: list[int] = []
    for candidate in candidates:
        if candidate not in result:
            result.append(candidate)
    return result


def _fetch_eastmoney_lightweight_batch_quotes_once(
    market: str,
    page_size: int,
    max_pages: int | None = None,
) -> BatchQuoteResult:
    session = _new_eastmoney_session(trust_env=True)
    fields = "f5,f6,f12,f14,f20,f21"
    rows: list[dict[str, Any]] = []
    total: int | None = None
    page = 1

    while True:
        if max_pages is not None and page > max_pages:
            break
        payload = _fetch_eastmoney_lightweight_page(
            session,
            {
                "pn": page,
                "pz": page_size,
                "po": 1,
                "np": 1,
                "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                "fltt": 2,
                "invt": 2,
                "fid": "f12",
                "fs": _eastmoney_lightweight_fs(market),
                "fields": fields,
            },
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            break
        diff = data.get("diff") or []
        if not diff:
            break
        total = safe_int(data.get("total")) or total
        for item in diff:
            if not isinstance(item, dict):
                continue
            rows.append(
                {
                    "代码": item.get("f12"),
                    "名称": item.get("f14"),
                    "成交量": item.get("f5"),
                    "成交额": item.get("f6"),
                    "总市值": item.get("f20"),
                    "流通市值": item.get("f21"),
                }
            )
        if total is not None and len(rows) >= total:
            break
        page += 1
        time.sleep(EASTMONEY_LIGHTWEIGHT_PAGE_DELAY_SECONDS)

    if not rows:
        raise RuntimeError(f"eastmoney lightweight {market} returned no rows")
    return BatchQuoteResult(pd.DataFrame(rows), "eastmoney_lightweight", datetime.now(timezone.utc), "ok")


def fetch_eastmoney_lightweight_batch_quotes(
    market: str,
    page_size: int = EASTMONEY_LIGHTWEIGHT_PAGE_SIZE,
    max_pages: int | None = None,
) -> BatchQuoteResult:
    """Fetch CN/BSE quote metrics with smaller Eastmoney pages for offline popularity refresh."""
    last_error: Exception | None = None
    for candidate_page_size in _eastmoney_page_size_candidates(page_size):
        try:
            return _fetch_eastmoney_lightweight_batch_quotes_once(
                market,
                page_size=candidate_page_size,
                max_pages=max_pages,
            )
        except Exception as exc:
            last_error = exc
    assert last_error is not None
    raise last_error


def fetch_hk_batch_quotes() -> BatchQuoteResult:
    import akshare as ak
    import data_provider.akshare_fetcher as akshare_fetcher

    fetcher = akshare_fetcher.AkshareFetcher()
    fetcher._set_random_user_agent()
    fetcher._enforce_rate_limit()
    circuit_breaker = akshare_fetcher.get_realtime_circuit_breaker()
    em_key = "akshare_hk_em"
    sina_key = "akshare_hk_sina"

    if circuit_breaker.is_available(em_key):
        try:
            df = call_with_timeout(ak.stock_hk_spot_em, BATCH_SOURCE_TIMEOUT_SECONDS, "ak.stock_hk_spot_em")
            circuit_breaker.record_success(em_key)
            return BatchQuoteResult(df, em_key, None, "ok")
        except Exception as exc:
            circuit_breaker.record_failure(em_key, str(exc))

    if not circuit_breaker.is_available(sina_key):
        raise RuntimeError(f"{sina_key} circuit breaker is open")

    try:
        df = call_with_timeout(ak.stock_hk_spot, BATCH_SOURCE_TIMEOUT_SECONDS, "ak.stock_hk_spot")
        circuit_breaker.record_success(sina_key)
        return BatchQuoteResult(df, sina_key, None, "ok")
    except Exception as exc:
        circuit_breaker.record_failure(sina_key, str(exc))
        raise


def fetch_yfinance_popularity_metrics(symbols: Iterable[str]) -> list[StockPopularityMetrics]:
    import yfinance as yf

    normalized_symbols = [str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()]
    metrics: list[StockPopularityMetrics] = []
    updated_at = datetime.now(timezone.utc)

    def fetch_symbol(symbol: str) -> StockPopularityMetrics | None:
        ticker = yf.Ticker(symbol)
        try:
            info = ticker.get_info()
        except Exception:
            info = {}
        market_cap = info.get("marketCap")
        avg_volume = info.get("averageVolume") or info.get("averageVolume10days")
        if not market_cap and not avg_volume:
            hist = ticker.history(period="20d", interval="1d")
            if not hist.empty and "Volume" in hist:
                avg_volume = safe_float(hist["Volume"].tail(20).mean())
        metric = StockPopularityMetrics(
            code=symbol,
            market="US",
            market_cap=safe_float(market_cap),
            avg_volume=safe_float(avg_volume),
            source="yfinance",
            updated_at=updated_at,
        )
        return metric if metric.has_effective_metric else None

    executor = ThreadPoolExecutor(max_workers=US_CONCURRENCY)
    future_to_symbol = {executor.submit(fetch_symbol, symbol): symbol for symbol in normalized_symbols}
    try:
        for future in as_completed(future_to_symbol, timeout=US_OVERALL_TIMEOUT_SECONDS):
            try:
                metric = future.result()
            except Exception:
                continue
            if metric is not None:
                metrics.append(metric)
    except FuturesTimeout:
        pass
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    return metrics
