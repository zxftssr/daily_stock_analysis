# -*- coding: utf-8 -*-
"""Investment strategy plan API contracts."""

from __future__ import annotations

from datetime import date
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field

StrategyType = Literal["index_crash", "swing", "dividend", "cycle", "value", "growth"]
PlanStatus = Literal["draft", "active", "paused", "closed"]
StepAction = Literal["buy", "add", "reduce", "exit", "review"]
StepMetric = Literal["price", "benchmark_drawdown_250d_pct"]
StepOperator = Literal["lte", "gte", "between"]
StepStatus = Literal["pending", "triggered", "completed", "skipped"]
CheckFrequency = Literal["daily", "hourly", "manual"]
NotificationChannel = Literal[
    "wechat", "feishu", "telegram", "email", "pushover", "ntfy", "gotify",
    "pushplus", "serverchan3", "custom", "discord", "slack", "astrbot",
]


class InvestmentPlanStepInput(BaseModel):
    action: StepAction
    metric: StepMetric = "price"
    operator: StepOperator
    threshold: float
    upper_threshold: Optional[float] = None
    target_position_pct: Optional[float] = Field(None, ge=0, le=100)
    note: Optional[str] = Field(None, max_length=255)


class InvestmentPlanCreateRequest(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=16)
    market: Literal["cn", "hk", "us"]
    name: Optional[str] = Field(None, max_length=64)
    account_id: Optional[int] = Field(None, gt=0)
    strategy_type: StrategyType
    status: Literal["draft", "active"] = "draft"
    thesis: str = Field(..., min_length=1, max_length=4000)
    invalidation_note: str = Field(..., min_length=1, max_length=4000)
    benchmark_symbol: Optional[str] = Field(None, max_length=16)
    max_position_pct: Optional[float] = Field(None, ge=0, le=100)
    required_cash_pct: Optional[float] = Field(None, ge=0, le=100)
    review_date: Optional[date] = None
    notify_on_trigger: bool = True
    notification_channels: List[NotificationChannel] = Field(default_factory=list, max_length=1)
    check_frequency: CheckFrequency = "daily"
    steps: List[InvestmentPlanStepInput] = Field(default_factory=list)


class InvestmentPlanUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, max_length=64)
    strategy_type: Optional[StrategyType] = None
    thesis: Optional[str] = Field(None, min_length=1, max_length=4000)
    invalidation_note: Optional[str] = Field(None, min_length=1, max_length=4000)
    benchmark_symbol: Optional[str] = Field(None, max_length=16)
    max_position_pct: Optional[float] = Field(None, ge=0, le=100)
    required_cash_pct: Optional[float] = Field(None, ge=0, le=100)
    review_date: Optional[date] = None
    notify_on_trigger: Optional[bool] = None
    notification_channels: Optional[List[NotificationChannel]] = Field(None, max_length=1)
    check_frequency: Optional[CheckFrequency] = None
    steps: Optional[List[InvestmentPlanStepInput]] = None


class InvestmentPlanStatusRequest(BaseModel):
    status: PlanStatus


class InvestmentPlanStepStatusRequest(BaseModel):
    status: StepStatus


class InvestmentPlanStepItem(BaseModel):
    id: int
    plan_id: int
    action: StepAction
    metric: StepMetric
    operator: StepOperator
    threshold: float
    upper_threshold: Optional[float] = None
    target_position_pct: Optional[float] = None
    note: Optional[str] = None
    sort_order: int
    status: StepStatus
    triggered_at: Optional[str] = None
    completed_at: Optional[str] = None
    notified_at: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class InvestmentPlanItem(BaseModel):
    id: int
    account_id: Optional[int] = None
    symbol: str
    market: str
    name: Optional[str] = None
    strategy_type: StrategyType
    strategy_label: str
    status: PlanStatus
    thesis: str
    invalidation_note: str
    benchmark_symbol: Optional[str] = None
    max_position_pct: Optional[float] = None
    required_cash_pct: Optional[float] = None
    review_date: Optional[str] = None
    notify_on_trigger: bool = True
    notification_channels: List[NotificationChannel] = Field(default_factory=list)
    check_frequency: CheckFrequency = "daily"
    review_due: bool = False
    last_price: Optional[float] = None
    last_evaluated_at: Optional[str] = None
    last_evaluation_status: Optional[str] = None
    last_evaluation_note: Optional[str] = None
    last_blocked_reasons: List[str] = Field(default_factory=list)
    triggered_step_count: int = 0
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    steps: List[InvestmentPlanStepItem] = Field(default_factory=list)


class InvestmentPlanListResponse(BaseModel):
    items: List[InvestmentPlanItem] = Field(default_factory=list)
    total: int
    summary: Dict[str, int] = Field(default_factory=dict)


class InvestmentPlanConstraints(BaseModel):
    position_pct: Optional[float] = None
    cash_pct: Optional[float] = None


class InvestmentPlanEvaluationResponse(BaseModel):
    plan: InvestmentPlanItem
    metric_values: Dict[str, Optional[float]] = Field(default_factory=dict)
    matched_step_ids: List[int] = Field(default_factory=list)
    newly_triggered_step_ids: List[int] = Field(default_factory=list)
    constraints: InvestmentPlanConstraints
    blocked_reasons: List[str] = Field(default_factory=list)
    review_due: bool
    errors: List[str] = Field(default_factory=list)
    notification: Dict[str, object] = Field(default_factory=dict)


class InvestmentPlanBatchEvaluationResponse(BaseModel):
    evaluated: int
    triggered: int
    errors: List[dict] = Field(default_factory=list)
    results: List[InvestmentPlanEvaluationResponse] = Field(default_factory=list)
    notification: Dict[str, object] = Field(default_factory=dict)
