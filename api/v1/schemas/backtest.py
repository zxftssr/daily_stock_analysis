# -*- coding: utf-8 -*-
"""Backtest API schemas."""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class BacktestRunRequest(BaseModel):
    code: Optional[str] = Field(None, description="仅回测指定股票")
    force: bool = Field(False, description="强制重新计算")
    eval_window_days: Optional[int] = Field(None, ge=1, le=120, description="评估窗口（交易日数）")
    min_age_days: Optional[int] = Field(None, ge=0, le=365, description="分析记录最小天龄（0=不限）")
    limit: int = Field(200, ge=1, le=2000, description="最多处理的分析记录数")


class BacktestRunResponse(BaseModel):
    processed: int = Field(..., description="候选记录数")
    saved: int = Field(..., description="写入回测结果数")
    completed: int = Field(..., description="完成回测数")
    insufficient: int = Field(..., description="数据不足数")
    errors: int = Field(..., description="错误数")


class BacktestResultItem(BaseModel):
    analysis_history_id: int
    code: str
    stock_name: Optional[str] = None
    analysis_date: Optional[str] = None
    eval_window_days: int
    engine_version: str
    eval_status: str
    evaluated_at: Optional[str] = None
    operation_advice: Optional[str] = None
    trend_prediction: Optional[str] = None
    position_recommendation: Optional[str] = None
    start_price: Optional[float] = None
    end_close: Optional[float] = None
    max_high: Optional[float] = None
    min_low: Optional[float] = None
    stock_return_pct: Optional[float] = None
    actual_return_pct: Optional[float] = None
    actual_movement: Optional[str] = None
    direction_expected: Optional[str] = None
    direction_correct: Optional[bool] = None
    outcome: Optional[str] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    hit_stop_loss: Optional[bool] = None
    hit_take_profit: Optional[bool] = None
    first_hit: Optional[str] = None
    first_hit_date: Optional[str] = None
    first_hit_trading_days: Optional[int] = None
    simulated_entry_price: Optional[float] = None
    simulated_exit_price: Optional[float] = None
    simulated_exit_reason: Optional[str] = None
    simulated_return_pct: Optional[float] = None


class BacktestResultsResponse(BaseModel):
    total: int
    page: int
    limit: int
    items: List[BacktestResultItem] = Field(default_factory=list)


class PerformanceMetrics(BaseModel):
    scope: str
    code: Optional[str] = None
    eval_window_days: int
    engine_version: str
    computed_at: Optional[str] = None

    total_evaluations: int
    completed_count: int
    insufficient_count: int
    long_count: int
    cash_count: int
    win_count: int
    loss_count: int
    neutral_count: int

    direction_accuracy_pct: Optional[float] = None
    win_rate_pct: Optional[float] = None
    neutral_rate_pct: Optional[float] = None
    avg_stock_return_pct: Optional[float] = None
    avg_simulated_return_pct: Optional[float] = None

    stop_loss_trigger_rate: Optional[float] = None
    take_profit_trigger_rate: Optional[float] = None
    ambiguous_rate: Optional[float] = None
    avg_days_to_first_hit: Optional[float] = None

    advice_breakdown: Dict[str, Any] = Field(default_factory=dict)
    diagnostics: Dict[str, Any] = Field(default_factory=dict)


class EtfCrashBacktestStage(BaseModel):
    drawdown_pct: float = Field(..., gt=0, le=80)
    target_position_pct: float = Field(..., gt=0, le=100)


class EtfCrashBacktestRequest(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=16)
    start_date: date
    end_date: date
    initial_capital: float = Field(100000, gt=0, le=1_000_000_000)
    stages: List[EtfCrashBacktestStage] = Field(..., min_length=1, max_length=6)


class EtfCrashBacktestTrade(BaseModel):
    date: str
    action: str
    drawdown_pct: float
    threshold_pct: float
    target_position_pct: float
    price: float
    shares: float
    cash_after: float
    position_pct: float


class EtfCrashEquityPoint(BaseModel):
    date: str
    equity: float
    drawdown_pct: float
    position_pct: float


class EtfCrashBacktestResponse(BaseModel):
    symbol: str
    canonical_code: str
    name: str
    benchmark_code: Optional[str] = None
    benchmark_name: Optional[str] = None
    source: str
    storage_code: str
    requested_start_date: str
    requested_end_date: str
    effective_start_date: str
    effective_end_date: str
    trading_days: int
    initial_capital: float
    final_equity: float
    cash_remaining: float
    position_value: float
    total_return_pct: float
    buy_hold_return_pct: float
    excess_return_pct: float
    max_drawdown_pct: float
    capital_utilization_pct: float
    max_position_pct: float
    trigger_count: int
    triggered_stage_count: int
    untriggered_stage_count: int
    first_trigger_wait_trading_days: int
    longest_wait_trading_days: int
    average_entry_price: Optional[float] = None
    stages: List[EtfCrashBacktestStage] = Field(default_factory=list)
    trades: List[EtfCrashBacktestTrade] = Field(default_factory=list)
    equity_curve: List[EtfCrashEquityPoint] = Field(default_factory=list)


class EtfCrashRobustnessRequest(BaseModel):
    symbols: List[str] = Field(..., min_length=1, max_length=5)
    start_date: date
    end_date: date
    initial_capital: float = Field(100000, gt=0, le=1_000_000_000)
    stages: List[EtfCrashBacktestStage] = Field(..., min_length=1, max_length=6)
    window_trading_days: int = Field(60, ge=20, le=500)
    step_trading_days: int = Field(30, ge=1, le=500)
    out_of_sample_pct: float = Field(40, ge=10, le=80)
    min_windows: int = Field(3, ge=2, le=100)
    min_pass_rate_pct: float = Field(60, ge=0, le=100)
    min_window_return_pct: float = Field(0, ge=-100, le=1000)
    max_window_drawdown_pct: float = Field(15, ge=0, le=100)
    min_triggered_stages: int = Field(1, ge=0, le=6)


class EtfCrashRobustnessWindow(BaseModel):
    window_index: int
    symbol: str
    name: str
    sample_type: str
    start_date: str
    end_date: str
    trading_days: int
    total_return_pct: float
    buy_hold_return_pct: float
    max_drawdown_pct: float
    capital_utilization_pct: float
    triggered_stage_count: int
    passed: bool
    failure_reasons: List[str] = Field(default_factory=list)


class EtfCrashRobustnessSummary(BaseModel):
    total_windows: int
    passed_windows: int
    pass_rate_pct: float
    out_of_sample_windows: int
    out_of_sample_passed_windows: int
    out_of_sample_pass_rate_pct: float
    average_return_pct: Optional[float] = None
    median_return_pct: Optional[float] = None
    worst_return_pct: Optional[float] = None
    worst_max_drawdown_pct: Optional[float] = None
    average_capital_utilization_pct: Optional[float] = None
    trigger_coverage_pct: float


class EtfCrashRobustnessResponse(BaseModel):
    passed: bool
    failure_reasons: List[str] = Field(default_factory=list)
    requested_symbols: List[str] = Field(default_factory=list)
    eligible_symbols: List[str] = Field(default_factory=list)
    symbol_errors: List[Dict[str, str]] = Field(default_factory=list)
    requested_start_date: str
    requested_end_date: str
    window_trading_days: int
    step_trading_days: int
    out_of_sample_pct: float
    thresholds: Dict[str, Any] = Field(default_factory=dict)
    summary: EtfCrashRobustnessSummary
    windows: List[EtfCrashRobustnessWindow] = Field(default_factory=list)
