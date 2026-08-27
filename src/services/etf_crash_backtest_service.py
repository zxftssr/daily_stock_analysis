# -*- coding: utf-8 -*-
"""Deterministic backtest for staged ETF drawdown buying plans."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
import math
from statistics import mean, median
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


class EtfCrashRobustnessService:
    """Evaluate one fixed staged-buy configuration across rolling ETF windows."""

    MAX_TOTAL_WINDOWS = 500

    def __init__(
        self,
        db_manager: Optional[DatabaseManager] = None,
        index_entries: Optional[Iterable[StockIndexEntry]] = None,
    ) -> None:
        self.backtest = EtfCrashBacktestService(db_manager, index_entries)

    def run(
        self,
        *,
        symbols: Sequence[str],
        start_date: date,
        end_date: date,
        initial_capital: float,
        stages: Sequence[dict[str, float]],
        window_trading_days: int,
        step_trading_days: int,
        out_of_sample_pct: float,
        min_windows: int,
        min_pass_rate_pct: float,
        min_window_return_pct: float,
        max_window_drawdown_pct: float,
        min_triggered_stages: int,
    ) -> dict[str, Any]:
        normalized_symbols = self._validate_robustness_inputs(
            symbols=symbols,
            start_date=start_date,
            end_date=end_date,
            initial_capital=initial_capital,
            stages=stages,
            window_trading_days=window_trading_days,
            step_trading_days=step_trading_days,
            out_of_sample_pct=out_of_sample_pct,
            min_windows=min_windows,
            min_pass_rate_pct=min_pass_rate_pct,
            min_window_return_pct=min_window_return_pct,
            max_window_drawdown_pct=max_window_drawdown_pct,
            min_triggered_stages=min_triggered_stages,
        )
        windows: list[dict[str, Any]] = []
        symbol_errors: list[dict[str, str]] = []
        planned_windows: list[tuple[str, list[tuple[date, date, str]]]] = []

        for symbol in normalized_symbols:
            try:
                full_result = self.backtest.run(
                    symbol=symbol,
                    start_date=start_date,
                    end_date=end_date,
                    initial_capital=initial_capital,
                    stages=stages,
                )
                dates = [date.fromisoformat(point["date"]) for point in full_result["equity_curve"]]
                in_sample_spans, out_of_sample_spans = self._partition_rolling_spans(
                    dates,
                    window_trading_days,
                    step_trading_days,
                    out_of_sample_pct,
                )
                if not in_sample_spans or not out_of_sample_spans:
                    raise ValueError("有效交易日不足，无法形成至少 1 个样本内和 1 个样本外窗口")
                spans = [
                    (window_start, window_end, "in_sample")
                    for window_start, window_end in in_sample_spans
                ] + [
                    (window_start, window_end, "out_of_sample")
                    for window_start, window_end in out_of_sample_spans
                ]
                planned_windows.append((symbol, spans))
            except ValueError as exc:
                symbol_errors.append({"symbol": symbol, "message": str(exc)})

        total_planned_windows = sum(len(spans) for _, spans in planned_windows)
        if total_planned_windows > self.MAX_TOTAL_WINDOWS:
            raise ValueError(
                f"滚动窗口总数 {total_planned_windows} 超过上限 {self.MAX_TOTAL_WINDOWS}；"
                "请增大滚动步长或缩短日期范围"
            )

        for symbol, spans in planned_windows:
            try:
                symbol_windows: list[dict[str, Any]] = []
                for index, (window_start, window_end, sample_type) in enumerate(spans, start=1):
                    result = self.backtest.run(
                        symbol=symbol,
                        start_date=window_start,
                        end_date=window_end,
                        initial_capital=initial_capital,
                        stages=stages,
                    )
                    reasons = self._window_failure_reasons(
                        result,
                        min_window_return_pct=min_window_return_pct,
                        max_window_drawdown_pct=max_window_drawdown_pct,
                        min_triggered_stages=min_triggered_stages,
                    )
                    symbol_windows.append({
                        "window_index": index,
                        "symbol": result["symbol"],
                        "name": result["name"],
                        "sample_type": sample_type,
                        "start_date": result["effective_start_date"],
                        "end_date": result["effective_end_date"],
                        "trading_days": result["trading_days"],
                        "total_return_pct": result["total_return_pct"],
                        "buy_hold_return_pct": result["buy_hold_return_pct"],
                        "max_drawdown_pct": result["max_drawdown_pct"],
                        "capital_utilization_pct": result["capital_utilization_pct"],
                        "triggered_stage_count": result["triggered_stage_count"],
                        "passed": not reasons,
                        "failure_reasons": reasons,
                    })
                windows.extend(symbol_windows)
            except ValueError as exc:
                symbol_errors.append({"symbol": symbol, "message": str(exc)})

        summary = self._summarize(windows, len(stages))
        failure_reasons: list[str] = []
        if len(windows) < min_windows:
            failure_reasons.append(f"有效窗口少于 {min_windows} 个")
        if summary["pass_rate_pct"] < min_pass_rate_pct:
            failure_reasons.append(f"总体通过率低于 {min_pass_rate_pct:g}%")
        if summary["out_of_sample_windows"] == 0:
            failure_reasons.append("缺少样本外窗口")
        elif summary["out_of_sample_pass_rate_pct"] < min_pass_rate_pct:
            failure_reasons.append(f"样本外通过率低于 {min_pass_rate_pct:g}%")
        if symbol_errors:
            failure_reasons.append("部分 ETF 缺少可用历史窗口")

        return {
            "passed": not failure_reasons,
            "failure_reasons": failure_reasons,
            "requested_symbols": normalized_symbols,
            "eligible_symbols": sorted({item["symbol"] for item in windows}),
            "symbol_errors": symbol_errors,
            "requested_start_date": start_date.isoformat(),
            "requested_end_date": end_date.isoformat(),
            "window_trading_days": window_trading_days,
            "step_trading_days": step_trading_days,
            "out_of_sample_pct": out_of_sample_pct,
            "thresholds": {
                "min_windows": min_windows,
                "min_pass_rate_pct": min_pass_rate_pct,
                "min_window_return_pct": min_window_return_pct,
                "max_window_drawdown_pct": max_window_drawdown_pct,
                "min_triggered_stages": min_triggered_stages,
            },
            "summary": summary,
            "windows": windows,
        }

    def _validate_robustness_inputs(
        self,
        *,
        symbols: Sequence[str],
        start_date: date,
        end_date: date,
        initial_capital: float,
        stages: Sequence[dict[str, float]],
        window_trading_days: int,
        step_trading_days: int,
        out_of_sample_pct: float,
        min_windows: int,
        min_pass_rate_pct: float,
        min_window_return_pct: float,
        max_window_drawdown_pct: float,
        min_triggered_stages: int,
    ) -> list[str]:
        normalized_stages = self.backtest._validate_inputs(
            start_date=start_date,
            end_date=end_date,
            initial_capital=initial_capital,
            stages=stages,
        )
        raw_symbols = [str(symbol or "").strip().upper() for symbol in symbols]
        if not raw_symbols or len(raw_symbols) > 5 or any(not symbol for symbol in raw_symbols):
            raise ValueError("稳健性验证需要选择 1 至 5 只 ETF")
        normalized_symbols: list[str] = []
        seen_etfs: set[str] = set()
        for symbol in raw_symbols:
            entry = self.backtest._resolve_etf(symbol)
            identity = entry.canonical_code.upper()
            if identity in seen_etfs:
                continue
            seen_etfs.add(identity)
            normalized_symbols.append(entry.display_code)
        if not 20 <= window_trading_days <= 500:
            raise ValueError("滚动窗口必须在 20 至 500 个交易日之间")
        if not 1 <= step_trading_days <= window_trading_days:
            raise ValueError("滚动步长必须在 1 至窗口交易日数之间")
        if not 10 <= out_of_sample_pct <= 80:
            raise ValueError("样本外占比必须在 10% 至 80% 之间")
        if not 2 <= min_windows <= 100:
            raise ValueError("最少有效窗口必须在 2 至 100 之间")
        if not 0 <= min_pass_rate_pct <= 100:
            raise ValueError("最低通过率必须在 0% 至 100% 之间")
        if not -100 <= min_window_return_pct <= 1000:
            raise ValueError("单窗口最低收益阈值超出范围")
        if not 0 <= max_window_drawdown_pct <= 100:
            raise ValueError("单窗口最大回撤阈值必须在 0% 至 100% 之间")
        if not 0 <= min_triggered_stages <= len(normalized_stages):
            raise ValueError("最少触发档位不能超过策略档位数")
        return normalized_symbols

    @staticmethod
    def _rolling_spans(
        dates: Sequence[date],
        window_trading_days: int,
        step_trading_days: int,
    ) -> list[tuple[date, date]]:
        return [
            (dates[start], dates[start + window_trading_days - 1])
            for start in range(0, len(dates) - window_trading_days + 1, step_trading_days)
        ]

    @classmethod
    def _partition_rolling_spans(
        cls,
        dates: Sequence[date],
        window_trading_days: int,
        step_trading_days: int,
        out_of_sample_pct: float,
    ) -> tuple[list[tuple[date, date]], list[tuple[date, date]]]:
        if len(dates) < window_trading_days * 2:
            return [], []
        out_of_sample_days = min(
            len(dates) - window_trading_days,
            max(
                window_trading_days,
                math.ceil(len(dates) * out_of_sample_pct / 100.0),
            ),
        )
        boundary = len(dates) - out_of_sample_days
        return (
            cls._rolling_spans(
                dates[:boundary],
                window_trading_days,
                step_trading_days,
            ),
            cls._rolling_spans(
                dates[boundary:],
                window_trading_days,
                step_trading_days,
            ),
        )

    @staticmethod
    def _window_failure_reasons(
        result: dict[str, Any],
        *,
        min_window_return_pct: float,
        max_window_drawdown_pct: float,
        min_triggered_stages: int,
    ) -> list[str]:
        reasons: list[str] = []
        if result["total_return_pct"] < min_window_return_pct:
            reasons.append(f"收益低于 {min_window_return_pct:g}%")
        if result["max_drawdown_pct"] > max_window_drawdown_pct:
            reasons.append(f"最大回撤高于 {max_window_drawdown_pct:g}%")
        if result["triggered_stage_count"] < min_triggered_stages:
            reasons.append(f"触发档位少于 {min_triggered_stages} 个")
        return reasons

    @staticmethod
    def _summarize(windows: Sequence[dict[str, Any]], stage_count: int) -> dict[str, Any]:
        total = len(windows)
        passed = sum(bool(item["passed"]) for item in windows)
        out_windows = [item for item in windows if item["sample_type"] == "out_of_sample"]
        out_passed = sum(bool(item["passed"]) for item in out_windows)
        returns = [float(item["total_return_pct"]) for item in windows]
        drawdowns = [float(item["max_drawdown_pct"]) for item in windows]
        utilization = [float(item["capital_utilization_pct"]) for item in windows]
        triggered = sum(int(item["triggered_stage_count"]) for item in windows)
        return {
            "total_windows": total,
            "passed_windows": passed,
            "pass_rate_pct": round(passed / total * 100.0, 4) if total else 0.0,
            "out_of_sample_windows": len(out_windows),
            "out_of_sample_passed_windows": out_passed,
            "out_of_sample_pass_rate_pct": (
                round(out_passed / len(out_windows) * 100.0, 4) if out_windows else 0.0
            ),
            "average_return_pct": round(mean(returns), 4) if returns else None,
            "median_return_pct": round(median(returns), 4) if returns else None,
            "worst_return_pct": round(min(returns), 4) if returns else None,
            "worst_max_drawdown_pct": round(max(drawdowns), 4) if drawdowns else None,
            "average_capital_utilization_pct": round(mean(utilization), 4) if utilization else None,
            "trigger_coverage_pct": (
                round(triggered / (total * stage_count) * 100.0, 4)
                if total and stage_count else 0.0
            ),
        }
