# -*- coding: utf-8 -*-
"""Investment strategy plan API contracts."""

from __future__ import annotations

from datetime import date, datetime
import re
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator
from pydantic_core import PydanticCustomError

StrategyType = Literal["index_crash", "swing", "dividend", "cycle", "value", "growth"]
PlanStatus = Literal["draft", "active", "paused", "closed"]
StepAction = Literal["buy", "add", "reduce", "exit", "review"]
StepMetric = Literal["price", "benchmark_drawdown_250d_pct"]
StepOperator = Literal["lte", "gte", "between"]
StepStatus = Literal["pending", "triggered", "completed", "skipped"]
CheckFrequency = Literal["minute", "daily", "hourly", "manual"]
NotificationChannel = Literal[
    "wechat", "feishu", "telegram", "email", "pushover", "ntfy", "gotify",
    "pushplus", "serverchan3", "custom", "discord", "slack", "astrbot",
]
NotificationStatus = Literal["pending", "sent", "failed", "unavailable"]

EXECUTION_AT_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$",
    re.IGNORECASE,
)


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
    planned_capital: Optional[float] = Field(None, gt=0)
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
    planned_capital: Optional[float] = Field(None, gt=0)
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


class InvestmentPlanExecutionRequest(BaseModel):
    execution_at: datetime
    price: float = Field(..., gt=0)
    quantity: float = Field(..., gt=0)
    fee: float = Field(0, ge=0)
    note: Optional[str] = Field(None, max_length=255)

    @field_validator("execution_at", mode="before")
    @classmethod
    def validate_execution_at_contract(cls, value: Any) -> Any:
        if isinstance(value, datetime):
            if value.tzinfo is None or value.utcoffset() is None:
                raise PydanticCustomError(
                    "execution_at_timezone",
                    "execution_at must include a timezone offset",
                )
            return value
        if not EXECUTION_AT_PATTERN.fullmatch(str(value or "")):
            raise PydanticCustomError(
                "execution_at_format",
                "execution_at must include date, time to seconds, and timezone offset",
            )
        return value


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
    execution_date: Optional[str] = None
    execution_at: Optional[str] = None
    execution_price: Optional[float] = None
    execution_quantity: Optional[float] = None
    execution_amount: Optional[float] = None
    execution_fee: Optional[float] = None
    execution_note: Optional[str] = None
    notified_at: Optional[str] = None
    notification_status: Optional[NotificationStatus] = None
    notification_status_at: Optional[str] = None
    notification_error: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class InvestmentPlanExecutionSummary(BaseModel):
    completed_execution_count: int = 0
    unrecorded_completed_count: int = 0
    execution_data_complete: bool = True
    planned_capital: Optional[float] = None
    total_quantity: float = 0
    gross_amount: float = 0
    total_fees: float = 0
    total_cost: float = 0
    average_cost: Optional[float] = None
    remaining_cash: Optional[float] = None
    valuation_price: Optional[float] = None
    valuation_price_source: Optional[Literal["plan_check", "latest_execution"]] = None
    valuation_as_of_date: Optional[str] = None
    market_value: Optional[float] = None
    unrealized_pnl: Optional[float] = None
    return_pct: Optional[float] = None
    target_position_pct: Optional[float] = None
    capital_utilization_pct: Optional[float] = None
    target_deviation_pct: Optional[float] = None


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
    planned_capital: Optional[float] = None
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
    execution_summary: InvestmentPlanExecutionSummary = Field(
        default_factory=InvestmentPlanExecutionSummary
    )


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


class InvestmentPlanMinuteCheckStatus(BaseModel):
    status: Literal["running", "completed", "partial", "failed", "skipped_market_closed"]
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    markets: List[Literal["cn", "hk", "us"]] = Field(default_factory=list)
    evaluated: int = 0
    triggered: int = 0
    error_count: int = 0
    notification_sent: bool = False
    message: Optional[str] = None


class InvestmentPlanSchedulerStatusResponse(BaseModel):
    status: Literal["online", "offline", "not_started", "unavailable"]
    online: bool
    message: str
    stale_after_seconds: int
    heartbeat_age_seconds: Optional[int] = None
    instance_id: Optional[str] = None
    pid: Optional[int] = None
    started_at: Optional[str] = None
    heartbeat_at: Optional[str] = None
    stopped_at: Optional[str] = None
    schedule_time: Optional[str] = None
    minute_check: Optional[InvestmentPlanMinuteCheckStatus] = None
