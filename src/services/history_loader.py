"""DB-first K-line history loader for Agent tools.

Provides:
- ContextVar-based frozen target_date propagation across threads
- ``load_history_df``: read from DB first, DataFetcherManager fallback

Fixes #1066 – eliminates 45+ redundant HTTP requests per stock in Agent mode.
"""
from __future__ import annotations

import contextvars
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from threading import Lock
from typing import Any, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)
_CACHE_MIN_RECORDS = 30
_HISTORY_MAX_DAYS = 550
_WINDOW_MIN_WEEKDAY_COVERAGE = 0.60

# ---------------------------------------------------------------------------
# Frozen target date (ContextVar) – set once per stock in pipeline, read by
# all agent tool threads via copy_context().run().
# ---------------------------------------------------------------------------
_frozen_target_date: contextvars.ContextVar[Optional[date]] = contextvars.ContextVar(
    "_frozen_target_date", default=None,
)


def set_frozen_target_date(d: date) -> contextvars.Token:
    return _frozen_target_date.set(d)


def get_frozen_target_date() -> Optional[date]:
    return _frozen_target_date.get()


def reset_frozen_target_date(token: contextvars.Token) -> None:
    _frozen_target_date.reset(token)


# ---------------------------------------------------------------------------
# Internal DataFetcherManager singleton (fallback only)
# ---------------------------------------------------------------------------
_fetcher_singleton = None
_fetcher_lock = Lock()


def _get_fetcher_manager():
    global _fetcher_singleton
    if _fetcher_singleton is None:
        with _fetcher_lock:
            if _fetcher_singleton is None:
                from data_provider import DataFetcherManager
                _fetcher_singleton = DataFetcherManager()
    return _fetcher_singleton


# ---------------------------------------------------------------------------
# DB-first history loader
# ---------------------------------------------------------------------------
def _history_code_candidates(stock_code: str) -> Tuple[List[str], str]:
    from data_provider.base import (
        _exchange_aware_stock_identity,
        _requires_exchange_aware_fetcher,
        canonical_stock_code,
        normalize_stock_code,
    )

    raw_code = str(stock_code or "").strip()
    normalized_code = canonical_stock_code(normalize_stock_code(raw_code))
    storage_code = _exchange_aware_stock_identity(raw_code)
    candidates: List[str] = []
    raw_candidate = canonical_stock_code(raw_code)
    candidate_values = (
        (raw_candidate, storage_code)
        if _requires_exchange_aware_fetcher(raw_code)
        else (raw_candidate, normalized_code)
    )
    for candidate in candidate_values:
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    return candidates, storage_code


def _normalize_history_days(days: Any) -> int:
    try:
        if isinstance(days, bool):
            raise ValueError("bool is not a valid days value")
        effective = int(days)
    except (TypeError, ValueError):
        effective = 60
    return max(1, min(effective, _HISTORY_MAX_DAYS))


def _coerce_bar_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.strptime(value[:10], "%Y-%m-%d").date()
        except ValueError:
            return date.min
    if hasattr(value, "date"):
        try:
            coerced = value.date()
            return coerced if isinstance(coerced, date) else date.min
        except Exception:
            return date.min
    return date.min


def _bar_date(bar: Any) -> date:
    row_date = _coerce_bar_date(getattr(bar, "date", None))
    if row_date != date.min:
        return row_date
    if hasattr(bar, "to_dict"):
        try:
            return _coerce_bar_date((bar.to_dict() or {}).get("date"))
        except Exception:
            return date.min
    return date.min


def _select_best_bars(db, stock_code: str, start: date, end: date) -> Tuple[Optional[str], list]:
    candidates, normalized_code = _history_code_candidates(stock_code)
    best_code = None
    best_bars = []
    best_key = None

    for candidate in candidates:
        bars = list(db.get_data_range(candidate, start, end) or [])
        if not bars:
            continue
        latest_date = max(_bar_date(bar) for bar in bars)
        key = (latest_date, len(bars), candidate == normalized_code)
        if best_key is None or key > best_key:
            best_key = key
            best_code = candidate
            best_bars = bars

    return best_code, best_bars


@dataclass(frozen=True)
class HistoryLoadResult:
    df: Optional[pd.DataFrame]
    source: Optional[str]
    cache_hit: bool
    stale: bool
    partial_cache: bool
    as_of_date: Optional[str]
    requested_days: int
    effective_days: int
    actual_records: int
    message: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None


def _bars_to_df(bars: list) -> pd.DataFrame:
    return pd.DataFrame([bar.to_dict() for bar in bars])


def _latest_df_date(df: Optional[pd.DataFrame]) -> Optional[date]:
    if df is None or df.empty or "date" not in df.columns:
        return None
    latest = date.min
    for value in df["date"].tolist():
        latest = max(latest, _coerce_bar_date(value))
    return latest if latest != date.min else None


def _market_for_history_stock(stock_code: str) -> Optional[str]:
    from data_provider.base import normalize_stock_code
    from src.core.trading_calendar import get_market_for_stock

    return get_market_for_stock(normalize_stock_code(str(stock_code or "")))


def _effective_history_end(stock_code: str, target_date: Optional[date]) -> date:
    if target_date is not None:
        return target_date

    from src.core.trading_calendar import get_effective_trading_date

    return get_effective_trading_date(_market_for_history_stock(stock_code))


def _make_result(
    *,
    df: Optional[pd.DataFrame],
    source: Optional[str],
    cache_hit: bool,
    stale: bool,
    requested_days: int,
    effective_days: int,
    start: Optional[date] = None,
    end: Optional[date] = None,
    partial_cache: bool = False,
    message: Optional[str] = None,
) -> HistoryLoadResult:
    if df is not None and not df.empty:
        actual_records = len(df)
        as_of = _latest_df_date(df)
    else:
        actual_records = 0
        as_of = None

    return HistoryLoadResult(
        df=df,
        source=source,
        cache_hit=cache_hit,
        stale=stale,
        partial_cache=partial_cache,
        as_of_date=as_of.isoformat() if as_of else None,
        requested_days=requested_days,
        effective_days=effective_days,
        actual_records=actual_records,
        message=message,
        start_date=start.isoformat() if start else None,
        end_date=end.isoformat() if end else None,
    )


def _bars_cover_calendar_window(bars: list, start: date, end: date) -> bool:
    if not bars:
        return False
    dates = [_bar_date(bar) for bar in bars]
    latest_date = max(dates, default=date.min)
    earliest_date = min(dates, default=date.max)
    # A range may begin on a weekend or market holiday, so allow the first
    # available bar to be a few calendar days after the requested window start.
    if latest_date < end or earliest_date > start + timedelta(days=7):
        return False

    weekday_count = sum(
        1
        for offset in range((end - start).days + 1)
        if (start + timedelta(days=offset)).weekday() < 5
    )
    minimum_records = max(1, int(weekday_count * _WINDOW_MIN_WEEKDAY_COVERAGE))
    return len({bar_date for bar_date in dates if bar_date != date.min}) >= minimum_records


def load_history_snapshot(
    stock_code: str,
    days: int = 60,
    force_refresh: bool = False,
    target_date: Optional[date] = None,
) -> HistoryLoadResult:
    """Load daily history for Web/API callers with DB-first cache metadata.

    Unlike ``load_history_df`` this helper preserves stale DB candidates so the
    Web K-line drawer can fall back to cached data when live sources fail.
    """
    from src.storage import get_db

    requested_days = days
    effective_days = _normalize_history_days(days)
    end = _effective_history_end(stock_code, target_date)
    start = end - timedelta(days=max(effective_days - 1, 0))

    db = None
    stale_df: Optional[pd.DataFrame] = None
    fresh_df: Optional[pd.DataFrame] = None
    stale_is_partial = False

    try:
        db = get_db()
        _code, bars = _select_best_bars(db, stock_code, start, end)
        if bars:
            candidate_df = _bars_to_df(bars)
            if _bars_cover_calendar_window(bars, start, end):
                fresh_df = candidate_df
            else:
                stale_df = candidate_df
                stale_is_partial = True
    except Exception as e:
        logger.debug("load_history_snapshot(%s): DB read failed: %s", stock_code, e)

    if fresh_df is not None and not force_refresh:
        return _make_result(
            df=fresh_df,
            source="db_cache",
            cache_hit=True,
            stale=False,
            requested_days=requested_days,
            effective_days=effective_days,
            start=start,
            end=end,
        )

    try:
        manager = _get_fetcher_manager()
        df, source = manager.get_daily_data(
            stock_code,
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            days=effective_days,
        )
        if df is not None and not df.empty:
            latest_external_date = _latest_df_date(df)
            external_stale = latest_external_date is None or latest_external_date < end
            _, normalized_code = _history_code_candidates(stock_code)
            if db is None:
                try:
                    db = get_db()
                except Exception:
                    db = None
            if db is not None:
                try:
                    db.save_daily_data(df, normalized_code, source or "Unknown")
                except Exception as exc:
                    logger.warning(
                        "load_history_snapshot(%s): daily history persistence failed: %s",
                        normalized_code,
                        exc,
                    )
            return _make_result(
                df=df,
                source=source or "unknown",
                cache_hit=False,
                stale=external_stale,
                requested_days=requested_days,
                effective_days=effective_days,
                start=start,
                end=end,
                partial_cache=external_stale,
                message=(
                    "外部行情未覆盖最近完整交易日，正在展示过期 K 线数据。"
                    if external_stale else None
                ),
            )
    except Exception as e:
        logger.warning("load_history_snapshot(%s): DataFetcherManager failed: %s", stock_code, e)

    fallback_df = stale_df if stale_df is not None else fresh_df
    if fallback_df is not None and not fallback_df.empty:
        return _make_result(
            df=fallback_df,
            source="db_cache",
            cache_hit=True,
            stale=True,
            requested_days=requested_days,
            effective_days=effective_days,
            start=start,
            end=end,
            partial_cache=stale_is_partial,
            message="实时行情源暂不可用，正在展示本地缓存 K 线数据。",
        )

    return _make_result(
        df=None,
        source=None,
        cache_hit=False,
        stale=False,
        requested_days=requested_days,
        effective_days=effective_days,
        start=start,
        end=end,
        message="暂无 K 线数据，可能是行情源暂不可用。",
    )


def load_history_df(
    stock_code: str,
    days: int = 60,
    target_date: Optional[date] = None,
) -> Tuple[Optional[pd.DataFrame], str]:
    """Load K-line history, DB first with DataFetcherManager fallback.

    Returns ``(df, source)`` where *source* is ``"db_cache"`` on DB hit or the
    actual provider name on network fallback.  Returns ``(None, "none")`` when
    both paths fail.
    """
    from src.storage import get_db

    # Resolve effective end date
    if target_date is not None:
        end = target_date
    else:
        frozen = get_frozen_target_date()
        end = frozen if frozen else date.today()

    # Calendar-day buffer: ~1.8x trading days + margin for long holidays
    start = end - timedelta(days=int(days * 1.8) + 10)

    # --- 1. DB lookup (canonical code, then prefix-stripped fallback) ------
    try:
        db = get_db()
        _code, bars = _select_best_bars(db, stock_code, start, end)
        required_records = max(min(days, _CACHE_MIN_RECORDS), 1)
        latest_date = max((_bar_date(bar) for bar in bars), default=date.min)
        if bars and latest_date >= end and len(bars) >= required_records:
            df = pd.DataFrame([b.to_dict() for b in bars])
            logger.debug(
                "load_history_df(%s): %d bars from DB (requested %d)",
                stock_code, len(df), days,
            )
            return df, "db_cache"
    except Exception as e:
        logger.debug("load_history_df(%s): DB read failed: %s", stock_code, e)

    # --- 2. Network fallback via singleton DataFetcherManager -------------
    try:
        manager = _get_fetcher_manager()
        df, source = manager.get_daily_data(stock_code, days=days)
        if df is not None and not df.empty:
            return df, source
    except Exception as e:
        logger.warning("load_history_df(%s): DataFetcherManager failed: %s", stock_code, e)

    return None, "none"
