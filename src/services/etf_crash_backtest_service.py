# -*- coding: utf-8 -*-
"""Deterministic backtest for staged ETF drawdown buying plans."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
import math
from typing import Any, Iterable, Optional, Sequence

from src.data.stock_index_loader import StockIndexEntry, load_stock_index_entries
from src.services.history_loader import _history_code_candidates
from src.storage import DatabaseManager


@dataclass(frozen=True)
class _Bar:
    date: date
    high: float
    close: float


class EtfCrashBacktestService:
    """Run a staged-buy simulation using local ``stock_daily`` rows only."""

    ROLLING_WINDOW = 250
    PREHISTORY_CALENDAR_DAYS = 500

    def __init__(
        self,
        db_manager: Optional[DatabaseManager] = None,
        index_entries: Optional[Iterable[StockIndexEntry]] = None,
    ) -> None:
        self.db = db_manager or DatabaseManager.get_instance()
        self.index_entries = tuple(index_entries) if index_entries is not None else load_stock_index_entries()

    def run(
        self,
        *,
        symbol: str,
        start_date: date,
        end_date: date,
        initial_capital: float,
        stages: Sequence[dict[str, float]],
    ) -> dict[str, Any]:
        entry = self._resolve_etf(symbol)
        normalized_stages = self._validate_inputs(
            start_date=start_date,
            end_date=end_date,
            initial_capital=initial_capital,
            stages=stages,
        )
        bars, source_code = self._load_bars(entry, start_date, end_date)
        simulation = self._simulation_rows(bars, start_date, end_date)
        if not simulation:
            raise ValueError(
                "本地历史行情不足 250 个交易日，无法计算回撤；请先预热该 ETF 历史行情"
            )

        cash = float(initial_capital)
        shares = 0.0
        total_cost = 0.0
        triggered: set[int] = set()
        trades: list[dict[str, Any]] = []
        equity_curve: list[dict[str, Any]] = []
        position_samples: list[float] = []
        trigger_indexes: list[int] = []

        for trading_index, (bar, drawdown_pct) in enumerate(simulation):
            for stage_index, stage in enumerate(normalized_stages):
                if stage_index in triggered or drawdown_pct < stage["drawdown_pct"]:
                    continue
                current_value = shares * bar.close
                current_equity = cash + current_value
                target_value = current_equity * stage["target_position_pct"] / 100.0
                spend = min(cash, max(0.0, target_value - current_value))
                triggered.add(stage_index)
                trigger_indexes.append(trading_index)
                if spend <= 0:
                    continue
                purchased = spend / bar.close
                shares += purchased
                cash -= spend
                total_cost += spend
                equity = cash + shares * bar.close
                trades.append({
                    "date": bar.date.isoformat(),
                    "action": "buy" if len(trades) == 0 else "add",
                    "drawdown_pct": self._round(drawdown_pct),
                    "threshold_pct": stage["drawdown_pct"],
                    "target_position_pct": stage["target_position_pct"],
                    "price": self._round(bar.close),
                    "shares": self._round(purchased, 6),
                    "cash_after": self._round(cash),
                    "position_pct": self._round(shares * bar.close / equity * 100.0),
                })

            equity = cash + shares * bar.close
            position_pct = shares * bar.close / equity * 100.0 if equity > 0 else 0.0
            position_samples.append(position_pct)
            equity_curve.append({
                "date": bar.date.isoformat(),
                "equity": self._round(equity),
                "drawdown_pct": self._round(drawdown_pct),
                "position_pct": self._round(position_pct),
            })

        first_bar = simulation[0][0]
        last_bar = simulation[-1][0]
        final_equity = cash + shares * last_bar.close
        buy_hold_return = (last_bar.close / first_bar.close - 1.0) * 100.0
        total_return = (final_equity / initial_capital - 1.0) * 100.0
        max_drawdown = self._max_drawdown([float(point["equity"]) for point in equity_curve])
        waits = self._waiting_periods(len(simulation), trigger_indexes)

        return {
            "symbol": entry.display_code,
            "canonical_code": entry.canonical_code,
            "name": entry.name_zh,
            "benchmark_code": entry.benchmark_code,
            "benchmark_name": entry.benchmark_name,
            "source": "sqlite",
            "storage_code": source_code,
            "requested_start_date": start_date.isoformat(),
            "requested_end_date": end_date.isoformat(),
            "effective_start_date": first_bar.date.isoformat(),
            "effective_end_date": last_bar.date.isoformat(),
            "trading_days": len(simulation),
            "initial_capital": self._round(initial_capital),
            "final_equity": self._round(final_equity),
            "cash_remaining": self._round(cash),
            "position_value": self._round(shares * last_bar.close),
            "total_return_pct": self._round(total_return),
            "buy_hold_return_pct": self._round(buy_hold_return),
            "excess_return_pct": self._round(total_return - buy_hold_return),
            "max_drawdown_pct": self._round(max_drawdown),
            "capital_utilization_pct": self._round(
                sum(position_samples) / len(position_samples) if position_samples else 0.0
            ),
            "max_position_pct": self._round(max(position_samples, default=0.0)),
            "trigger_count": len(trades),
            "triggered_stage_count": len(triggered),
            "untriggered_stage_count": len(normalized_stages) - len(triggered),
            "first_trigger_wait_trading_days": trigger_indexes[0] if trigger_indexes else len(simulation),
            "longest_wait_trading_days": max(waits, default=0),
            "average_entry_price": self._round(total_cost / shares) if shares > 0 else None,
            "stages": normalized_stages,
            "trades": trades,
            "equity_curve": equity_curve,
        }

    def _resolve_etf(self, symbol: str) -> StockIndexEntry:
        raw = str(symbol or "").strip().upper()
        display = raw.split(".", 1)[0]
        for entry in self.index_entries:
            if not entry.active or entry.asset_type != "etf" or entry.market != "CN":
                continue
            if raw == entry.canonical_code.upper() or display == entry.display_code.upper():
                return entry
        raise ValueError("仅支持宽基 ETF 精选池中的有效标的")

    @staticmethod
    def _validate_inputs(
        *,
        start_date: date,
        end_date: date,
        initial_capital: float,
        stages: Sequence[dict[str, float]],
    ) -> list[dict[str, float]]:
        if start_date > end_date:
            raise ValueError("start_date 不能晚于 end_date")
        if not math.isfinite(initial_capital) or initial_capital <= 0:
            raise ValueError("initial_capital 必须大于 0")
        if not stages or len(stages) > 6:
            raise ValueError("回测需要 1 至 6 个买入档位")

        normalized: list[dict[str, float]] = []
        previous_drawdown = 0.0
        previous_position = 0.0
        for raw in stages:
            drawdown = float(raw["drawdown_pct"])
            position = float(raw["target_position_pct"])
            if not math.isfinite(drawdown) or not 0 < drawdown <= 80:
                raise ValueError("回撤阈值必须在 0 至 80 之间")
            if not math.isfinite(position) or not 0 < position <= 100:
                raise ValueError("目标仓位必须在 0 至 100 之间")
            if drawdown <= previous_drawdown:
                raise ValueError("回撤阈值必须严格递增")
            if position <= previous_position:
                raise ValueError("目标仓位必须严格递增")
            normalized.append({
                "drawdown_pct": round(drawdown, 4),
                "target_position_pct": round(position, 4),
            })
            previous_drawdown = drawdown
            previous_position = position
        return normalized

    def _load_bars(
        self,
        entry: StockIndexEntry,
        start_date: date,
        end_date: date,
    ) -> tuple[list[_Bar], str]:
        query_start = start_date - timedelta(days=self.PREHISTORY_CALENDAR_DAYS)
        candidates, _storage_code = _history_code_candidates(entry.canonical_code)
        if entry.display_code not in candidates:
            candidates.append(entry.display_code)

        best: list[_Bar] = []
        best_code = entry.display_code
        best_key: Optional[tuple[bool, date, int, bool]] = None
        for code in candidates:
            rows = self.db.get_data_range(code, query_start, end_date)
            parsed = self._parse_bars(rows)
            if not parsed:
                continue
            has_simulation = bool(self._simulation_rows(parsed, start_date, end_date))
            key = (has_simulation, parsed[-1].date, len(parsed), code == entry.display_code)
            if best_key is None or key > best_key:
                best_key = key
                best = parsed
                best_code = code
        if not best:
            raise ValueError("本地尚无该 ETF 历史行情，请先执行历史行情预热")
        return best, best_code

    @classmethod
    def _simulation_rows(
        cls,
        bars: Sequence[_Bar],
        start_date: date,
        end_date: date,
    ) -> list[tuple[_Bar, float]]:
        rows: list[tuple[_Bar, float]] = []
        for index, bar in enumerate(bars):
            if bar.date < start_date or bar.date > end_date or index + 1 < cls.ROLLING_WINDOW:
                continue
            window = bars[index - cls.ROLLING_WINDOW + 1:index + 1]
            peak = max(item.high for item in window)
            drawdown = max(0.0, (peak - bar.close) / peak * 100.0)
            rows.append((bar, drawdown))
        return rows

    @staticmethod
    def _parse_bars(rows: Iterable[Any]) -> list[_Bar]:
        parsed: dict[date, _Bar] = {}
        for row in rows:
            row_date = getattr(row, "date", None)
            close = getattr(row, "close", None)
            high = getattr(row, "high", None)
            if isinstance(row, dict):
                row_date = row.get("date")
                close = row.get("close")
                high = row.get("high")
            if isinstance(row_date, str):
                try:
                    row_date = date.fromisoformat(row_date[:10])
                except ValueError:
                    continue
            try:
                close_value = float(close)
                high_value = float(high if high is not None else close)
            except (TypeError, ValueError):
                continue
            if not isinstance(row_date, date) or close_value <= 0 or high_value <= 0:
                continue
            parsed[row_date] = _Bar(row_date, max(high_value, close_value), close_value)
        return [parsed[key] for key in sorted(parsed)]

    @staticmethod
    def _max_drawdown(equities: Sequence[float]) -> float:
        peak = 0.0
        maximum = 0.0
        for equity in equities:
            peak = max(peak, equity)
            if peak > 0:
                maximum = max(maximum, (peak - equity) / peak * 100.0)
        return maximum

    @staticmethod
    def _waiting_periods(trading_days: int, triggers: Sequence[int]) -> list[int]:
        if trading_days <= 0:
            return []
        if not triggers:
            return [trading_days]
        points = [0, *triggers, trading_days - 1]
        return [max(0, right - left) for left, right in zip(points, points[1:])]

    @staticmethod
    def _round(value: float, digits: int = 4) -> float:
        return round(float(value), digits)
