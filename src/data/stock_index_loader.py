# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import logging
import csv
from dataclasses import dataclass, replace
from pathlib import Path
from threading import RLock
from typing import Any, Dict, Iterable

from src.data.stock_mapping import is_meaningful_stock_name

logger = logging.getLogger(__name__)

_STOCK_INDEX_FILENAME = "stocks.index.json"
_STOCK_INDEX_CACHE: Dict[str, str] | None = None
_STOCK_INDEX_ENTRIES_CACHE: tuple["StockIndexEntry", ...] | None = None
_STOCK_INDEX_CACHE_LOCK = RLock()


@dataclass(frozen=True)
class StockIndexEntry:
    canonical_code: str
    display_code: str
    name_zh: str
    pinyin: str = ""
    acronym: str = ""
    aliases: tuple[str, ...] = ()
    market: str = "CN"
    asset_type: str = "stock"
    active: bool = True
    popularity: int | None = None
    industry: str | None = None
    industry_source: str | None = None
    etf_category: str | None = None
    benchmark_code: str | None = None
    benchmark_name: str | None = None


def get_stock_index_candidate_paths() -> tuple[Path, ...]:
    """Return the supported locations for the generated stock index."""
    repo_root = Path(__file__).resolve().parents[2]
    return (
        repo_root / "apps" / "dsa-web" / "public" / _STOCK_INDEX_FILENAME,
        repo_root / "static" / _STOCK_INDEX_FILENAME,
    )


def get_stock_industry_overrides_path() -> Path:
    """Return the optional static industry override CSV path."""
    repo_root = Path(__file__).resolve().parents[2]
    return repo_root / "data" / "stock_industry_overrides.csv"


def _clean_optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _clean_text(value: Any) -> str:
    return _clean_optional_text(value) or ""


def _normalize_industry_source(value: Any, default: str | None = None) -> str | None:
    source = (_clean_optional_text(value) or default or "").strip().lower()
    if not source:
        return None
    if source in {"override", "manual", "core_pool"}:
        return "override"
    if source in {"tushare", "csv", "industry", "classify", "sector"}:
        return "tushare"
    return "unknown"


def _coerce_bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "active"}:
        return True
    if text in {"0", "false", "no", "n", "inactive"}:
        return False
    return default


def _coerce_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _coerce_aliases(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple, set)):
        return tuple(text for item in value if (text := _clean_optional_text(item)))
    text = _clean_optional_text(value)
    return (text,) if text else ()


def _dict_value(item: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in item:
            return item[key]
    return None


def _parse_stock_index_item(item: Any) -> StockIndexEntry | None:
    if isinstance(item, (list, tuple)):
        if len(item) < 3:
            return None

        entry = StockIndexEntry(
            canonical_code=_clean_text(item[0]),
            display_code=_clean_text(item[1]) or _clean_text(item[0]),
            name_zh=_clean_text(item[2]),
            pinyin=_clean_text(item[3]) if len(item) > 3 else "",
            acronym=_clean_text(item[4]) if len(item) > 4 else "",
            aliases=_coerce_aliases(item[5]) if len(item) > 5 else (),
            market=_clean_text(item[6]) if len(item) > 6 else "CN",
            asset_type=_clean_text(item[7]) if len(item) > 7 else "stock",
            active=_coerce_bool(item[8]) if len(item) > 8 else True,
            popularity=_coerce_int(item[9]) if len(item) > 9 else None,
            industry=_clean_optional_text(item[10]) if len(item) > 10 else None,
            industry_source=_normalize_industry_source(item[11]) if len(item) > 11 else None,
            etf_category=_clean_optional_text(item[12]) if len(item) > 12 else None,
            benchmark_code=_clean_optional_text(item[13]) if len(item) > 13 else None,
            benchmark_name=_clean_optional_text(item[14]) if len(item) > 14 else None,
        )
    elif isinstance(item, dict):
        canonical_code = _dict_value(item, "canonicalCode", "canonical_code", "code")
        display_code = _dict_value(item, "displayCode", "display_code", "symbol")
        name_zh = _dict_value(item, "nameZh", "name_zh", "name", "stockName", "stock_name")
        entry = StockIndexEntry(
            canonical_code=_clean_text(canonical_code),
            display_code=_clean_text(display_code) or _clean_text(canonical_code),
            name_zh=_clean_text(name_zh),
            pinyin=_clean_text(_dict_value(item, "pinyin")),
            acronym=_clean_text(_dict_value(item, "acronym")),
            aliases=_coerce_aliases(_dict_value(item, "aliases")),
            market=_clean_text(_dict_value(item, "market")) or "CN",
            asset_type=_clean_text(_dict_value(item, "assetType", "asset_type")) or "stock",
            active=_coerce_bool(_dict_value(item, "active"), default=True),
            popularity=_coerce_int(_dict_value(item, "popularity")),
            industry=_clean_optional_text(_dict_value(item, "industry")),
            industry_source=_normalize_industry_source(_dict_value(item, "industrySource", "industry_source")),
            etf_category=_clean_optional_text(_dict_value(item, "etfCategory", "etf_category")),
            benchmark_code=_clean_optional_text(_dict_value(item, "benchmarkCode", "benchmark_code")),
            benchmark_name=_clean_optional_text(_dict_value(item, "benchmarkName", "benchmark_name")),
        )
    else:
        return None

    if not entry.canonical_code or not entry.display_code or not entry.name_zh:
        return None
    if not is_meaningful_stock_name(entry.name_zh, entry.display_code or entry.canonical_code):
        return None
    return entry


def _add_lookup_key(keys: set[str], value: str) -> None:
    candidate = str(value or "").strip()
    if not candidate:
        return
    keys.add(candidate)
    keys.add(candidate.upper())


def _build_lookup_keys(canonical_code: str, display_code: str) -> Iterable[str]:
    keys: set[str] = set()
    _add_lookup_key(keys, canonical_code)
    _add_lookup_key(keys, display_code)

    canonical_upper = str(canonical_code or "").strip().upper()
    display_upper = str(display_code or "").strip().upper()

    if "." in canonical_upper:
        base, suffix = canonical_upper.rsplit(".", 1)
        if suffix in {"SH", "SZ", "SS", "BJ"} and base.isdigit():
            _add_lookup_key(keys, base)
        elif suffix == "HK" and base.isdigit() and 1 <= len(base) <= 5:
            digits = base.zfill(5)
            _add_lookup_key(keys, digits)
            _add_lookup_key(keys, f"HK{digits}")

    for candidate in (canonical_upper, display_upper):
        if candidate.startswith("HK"):
            digits = candidate[2:]
            if digits.isdigit() and 1 <= len(digits) <= 5:
                digits = digits.zfill(5)
                _add_lookup_key(keys, digits)
                _add_lookup_key(keys, f"HK{digits}")

    return keys


def build_stock_index_lookup_keys(canonical_code: str, display_code: str | None = None) -> tuple[str, ...]:
    """Build lookup keys for stock codes across display, canonical, HK and suffix forms."""
    return tuple(_build_lookup_keys(str(canonical_code or ""), str(display_code or canonical_code or "")))


def _load_industry_overrides() -> dict[str, tuple[str, str]]:
    override_path = get_stock_industry_overrides_path()
    if not override_path.is_file():
        return {}

    overrides: dict[str, tuple[str, str]] = {}
    try:
        with override_path.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                code = _clean_optional_text(
                    row.get("code") or row.get("symbol") or row.get("canonical_code")
                )
                industry = _clean_optional_text(row.get("industry"))
                if not code or not industry:
                    continue
                source = _normalize_industry_source(
                    row.get("industry_source") or row.get("industrySource") or row.get("source"),
                    default="override",
                ) or "override"
                for key in build_stock_index_lookup_keys(code, code):
                    overrides[key] = (industry, source)
    except OSError as exc:
        logger.debug("[股票索引] 读取行业覆盖文件失败 %s: %s", override_path, exc)
    return overrides


def _apply_industry_overrides(entries: Iterable[StockIndexEntry]) -> tuple[StockIndexEntry, ...]:
    overrides = _load_industry_overrides()
    if not overrides:
        return tuple(entries)

    resolved: list[StockIndexEntry] = []
    for entry in entries:
        override: tuple[str, str] | None = None
        for key in build_stock_index_lookup_keys(entry.canonical_code, entry.display_code):
            override = overrides.get(key)
            if override:
                break
        if override:
            resolved.append(
                replace(entry, industry=override[0], industry_source=override[1])
            )
        else:
            resolved.append(entry)
    return tuple(resolved)


def _load_stock_index_entries_file(index_path: Path) -> tuple[StockIndexEntry, ...]:
    with index_path.open("r", encoding="utf-8") as fh:
        raw_items = json.load(fh)

    if not isinstance(raw_items, list):
        raise ValueError(
            f"Unexpected {_STOCK_INDEX_FILENAME} payload type: {type(raw_items).__name__}"
        )

    entries: list[StockIndexEntry] = []
    for item in raw_items:
        entry = _parse_stock_index_item(item)
        if entry is None:
            continue
        entries.append(entry)

    return _apply_industry_overrides(entries)


def _entries_to_name_map(entries: Iterable[StockIndexEntry]) -> Dict[str, str]:
    stock_name_map: Dict[str, str] = {}
    for entry in entries:
        for key in build_stock_index_lookup_keys(entry.canonical_code, entry.display_code):
            stock_name_map[key] = entry.name_zh.strip()
    return stock_name_map


def _load_stock_index_file(index_path: Path) -> Dict[str, str]:
    return _entries_to_name_map(_load_stock_index_entries_file(index_path))


def load_stock_index_entries() -> tuple[StockIndexEntry, ...]:
    """Lazily load and cache the generated stock index as full entries."""
    global _STOCK_INDEX_CACHE, _STOCK_INDEX_ENTRIES_CACHE

    if _STOCK_INDEX_ENTRIES_CACHE is not None:
        return _STOCK_INDEX_ENTRIES_CACHE

    with _STOCK_INDEX_CACHE_LOCK:
        if _STOCK_INDEX_ENTRIES_CACHE is not None:
            return _STOCK_INDEX_ENTRIES_CACHE

        for candidate_path in get_stock_index_candidate_paths():
            if not candidate_path.is_file():
                continue

            try:
                _STOCK_INDEX_ENTRIES_CACHE = _load_stock_index_entries_file(candidate_path)
                _STOCK_INDEX_CACHE = _entries_to_name_map(_STOCK_INDEX_ENTRIES_CACHE)
                logger.debug(
                    "[股票索引] 已加载前端完整股票索引: %s (%d 条)",
                    candidate_path,
                    len(_STOCK_INDEX_ENTRIES_CACHE),
                )
                return _STOCK_INDEX_ENTRIES_CACHE
            except (OSError, TypeError, ValueError) as exc:
                logger.debug("[股票索引] 读取完整股票索引失败 %s: %s", candidate_path, exc)

        _STOCK_INDEX_ENTRIES_CACHE = ()
        _STOCK_INDEX_CACHE = {}
        return _STOCK_INDEX_ENTRIES_CACHE


def get_stock_name_index_map() -> Dict[str, str]:
    """Lazily load and cache the generated stock-name index."""
    global _STOCK_INDEX_CACHE

    if _STOCK_INDEX_CACHE is not None:
        return _STOCK_INDEX_CACHE

    with _STOCK_INDEX_CACHE_LOCK:
        if _STOCK_INDEX_CACHE is not None:
            return _STOCK_INDEX_CACHE

        for candidate_path in get_stock_index_candidate_paths():
            if not candidate_path.is_file():
                continue

            try:
                _STOCK_INDEX_CACHE = _load_stock_index_file(candidate_path)
                logger.debug(
                    "[股票名称] 已加载前端股票索引映射: %s (%d 条)",
                    candidate_path,
                    len(_STOCK_INDEX_CACHE),
                )
                return _STOCK_INDEX_CACHE
            except (OSError, TypeError, ValueError) as exc:
                logger.debug("[股票名称] 读取股票索引失败 %s: %s", candidate_path, exc)

        _STOCK_INDEX_CACHE = {}
        return _STOCK_INDEX_CACHE


def get_index_stock_name(stock_code: str) -> str | None:
    """Resolve a stock name from the generated frontend stock index."""
    code = str(stock_code or "").strip()
    if not code:
        return None

    stock_name_map = get_stock_name_index_map()
    for key in _build_lookup_keys(code, code):
        name = stock_name_map.get(key)
        if is_meaningful_stock_name(name, code):
            return name

    return None


def _clear_stock_index_cache_for_tests() -> None:
    global _STOCK_INDEX_CACHE, _STOCK_INDEX_ENTRIES_CACHE
    with _STOCK_INDEX_CACHE_LOCK:
        _STOCK_INDEX_CACHE = None
        _STOCK_INDEX_ENTRIES_CACHE = None
