# -*- coding: utf-8 -*-
"""Investment plan lifecycle, deterministic evaluation, and alert delivery."""

from __future__ import annotations

import json
import logging
import math
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

from data_provider import is_us_index_code, is_us_stock_code
from data_provider.base import _exchange_aware_stock_identity, canonical_stock_code
from src.data.stock_index_loader import get_index_stock_name
from src.notification import NotificationService
from src.notification_routing import ROUTABLE_NOTIFICATION_CHANNEL_SET
from src.repositories.investment_plan_repo import (
    InvestmentPlanBusyError,
    InvestmentPlanConflictError,
    InvestmentPlanRepository,
)
from src.services.portfolio_service import PortfolioService
from src.services.stock_code_utils import normalize_code
from src.services.etf_history_service import EtfHistoryService
from src.services.stock_service import StockService

logger = logging.getLogger(__name__)

STRATEGY_TYPES = {
    "index_crash",
    "swing",
    "dividend",
    "cycle",
    "value",
    "growth",
}
PLAN_STATUSES = {"draft", "active", "paused", "closed"}
STEP_ACTIONS = {"buy", "add", "reduce", "exit", "review"}
STEP_METRICS = {"price", "benchmark_drawdown_250d_pct"}
STEP_OPERATORS = {"lte", "gte", "between"}
STEP_STATUSES = {"pending", "triggered", "completed", "skipped"}
CHECK_FREQUENCIES = {"minute", "daily", "hourly", "manual"}
MINUTE_CHECK_MARKETS = {"cn", "hk"}
MINUTE_QUOTE_MAX_AGE = timedelta(minutes=5)
MINUTE_QUOTE_FUTURE_TOLERANCE = timedelta(minutes=1)

STRATEGY_LABELS = {
    "index_crash": "指数大跌",
    "swing": "波段交易",
    "dividend": "股息收息",
    "cycle": "周期布局",
    "value": "价值投资",
    "growth": "成长投资",
}
ACTION_LABELS = {
    "buy": "买入",
    "add": "加仓",
    "reduce": "减仓",
    "exit": "退出/复查",
    "review": "人工复查",
}


class InvestmentPlanNotFoundError(Exception):
    """Raised when a plan or step does not exist."""


class InvestmentPlanStateError(Exception):
    """Raised when a lifecycle transition is not allowed."""


class InvestmentPlanService:
    """Own plan CRUD, lifecycle rules, market evaluation, and notifications."""

    def __init__(
        self,
        *,
        repo: Optional[InvestmentPlanRepository] = None,
        stock_service: Optional[StockService] = None,
        portfolio_service: Optional[PortfolioService] = None,
        notifier: Optional[NotificationService] = None,
    ):
        self.repo = repo or InvestmentPlanRepository()
        self.stock_service = stock_service or StockService()
        self.etf_history_service = EtfHistoryService(self.stock_service)
        self.portfolio_service = portfolio_service or PortfolioService()
        self.notifier = notifier
        self._quote_cache: Dict[str, Optional[Dict[str, Any]]] = {}
        self._validated_price_cache: Dict[str, Optional[float]] = {}
        self._drawdown_cache: Dict[str, Optional[float]] = {}

    # ------------------------------------------------------------------
    # CRUD and lifecycle
    # ------------------------------------------------------------------
    def create_plan(
        self,
        *,
        symbol: str,
        market: str,
        strategy_type: str,
        thesis: str,
        invalidation_note: str,
        steps: Sequence[Dict[str, Any]],
        account_id: Optional[int] = None,
        name: Optional[str] = None,
        benchmark_symbol: Optional[str] = None,
        planned_capital: Optional[float] = None,
        max_position_pct: Optional[float] = None,
        required_cash_pct: Optional[float] = None,
        review_date: Optional[date] = None,
        notify_on_trigger: bool = True,
        notification_channels: Optional[Sequence[str]] = None,
        check_frequency: str = "daily",
        status: str = "draft",
    ) -> Dict[str, Any]:
        normalized_market = self._normalize_market(market)
        normalized_symbol = self._normalize_symbol(symbol, normalized_market)
        normalized_status = self._normalize_choice(status, PLAN_STATUSES, "status")
        normalized_strategy = self._normalize_choice(strategy_type, STRATEGY_TYPES, "strategy_type")
        normalized_benchmark = self._normalize_optional_symbol(benchmark_symbol)
        normalized_channels = self._normalize_notification_channels(notification_channels)
        normalized_frequency = self._normalize_choice(
            check_frequency, CHECK_FREQUENCIES, "check_frequency"
        )
        self._validate_check_frequency_market(normalized_market, normalized_frequency)
        self._validate_account(account_id)
        fields = {
            "account_id": account_id,
            "symbol": normalized_symbol,
            "market": normalized_market,
            "name": self._clean_optional_text(name, 64) or get_index_stock_name(normalized_symbol),
            "strategy_type": normalized_strategy,
            "status": normalized_status,
            "thesis": self._require_text(thesis, "thesis", 4000),
            "invalidation_note": self._require_text(invalidation_note, "invalidation_note", 4000),
            "benchmark_symbol": normalized_benchmark,
            "planned_capital": self._optional_positive(
                planned_capital,
                "planned_capital",
            ),
            "max_position_pct": self._optional_pct(max_position_pct, "max_position_pct"),
            "required_cash_pct": self._optional_pct(required_cash_pct, "required_cash_pct"),
            "review_date": review_date,
            "notify_on_trigger": bool(notify_on_trigger),
            "notification_channels": json.dumps(normalized_channels, ensure_ascii=False),
            "check_frequency": normalized_frequency,
        }
        normalized_steps = self._normalize_steps(
            steps,
            benchmark_symbol=normalized_benchmark,
            max_position_pct=fields["max_position_pct"],
        )
        if normalized_status == "active":
            self._validate_activation(fields, normalized_steps)
        return self._decorate_plan(self.repo.create_plan(fields=fields, steps=normalized_steps))

    def list_plans(
        self,
        *,
        status: Optional[str] = None,
        strategy_type: Optional[str] = None,
        symbol: Optional[str] = None,
        account_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        normalized_status = self._normalize_choice(status, PLAN_STATUSES, "status") if status else None
        normalized_strategy = (
            self._normalize_choice(strategy_type, STRATEGY_TYPES, "strategy_type")
            if strategy_type else None
        )
        normalized_symbol = self._normalize_filter_symbol(symbol)
        items = [
            self._decorate_plan(item)
            for item in self.repo.list_plans(
                status=normalized_status,
                strategy_type=normalized_strategy,
                symbol=normalized_symbol,
                account_id=account_id,
            )
        ]
        return {
            "items": items,
            "total": len(items),
            "summary": self._summarize(items),
        }

    def get_plan(self, plan_id: int) -> Dict[str, Any]:
        plan = self.repo.get_plan(plan_id)
        if plan is None:
            raise InvestmentPlanNotFoundError("Investment plan not found")
        return self._decorate_plan(plan)

    def update_plan(
        self,
        plan_id: int,
        *,
        fields: Dict[str, Any],
        steps: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        current = self.get_plan(plan_id)
        if current["status"] == "closed":
            raise InvestmentPlanStateError("Closed plans cannot be edited")
        if steps is not None and current["status"] == "active":
            raise InvestmentPlanStateError("Pause the plan before replacing execution steps")
        if steps is not None and any(step["status"] != "pending" for step in current["steps"]):
            raise InvestmentPlanStateError(
                "Only plans whose execution steps are all pending can replace those steps; "
                "close plans with completed or skipped history and create a new plan"
            )

        normalized: Dict[str, Any] = {}
        if "name" in fields:
            normalized["name"] = self._clean_optional_text(fields.get("name"), 64)
        if "strategy_type" in fields and fields.get("strategy_type") is not None:
            next_strategy_type = self._normalize_choice(
                fields["strategy_type"], STRATEGY_TYPES, "strategy_type"
            )
            if next_strategy_type != current["strategy_type"] and current["status"] != "draft":
                raise InvestmentPlanStateError(
                    "strategy_type cannot change after a plan has been activated"
                )
            normalized["strategy_type"] = next_strategy_type
        if "thesis" in fields and fields.get("thesis") is not None:
            normalized["thesis"] = self._require_text(fields["thesis"], "thesis", 4000)
        if "invalidation_note" in fields and fields.get("invalidation_note") is not None:
            normalized["invalidation_note"] = self._require_text(
                fields["invalidation_note"], "invalidation_note", 4000
            )
        if "benchmark_symbol" in fields:
            normalized["benchmark_symbol"] = self._normalize_optional_symbol(fields.get("benchmark_symbol"))
        if "planned_capital" in fields:
            next_planned_capital = self._optional_positive(
                fields.get("planned_capital"),
                "planned_capital",
            )
            if (
                any(step.get("execution_amount") is not None for step in current["steps"])
                and next_planned_capital != current.get("planned_capital")
            ):
                raise InvestmentPlanStateError(
                    "planned_capital cannot change after an execution has been recorded"
                )
            normalized["planned_capital"] = next_planned_capital
        if "max_position_pct" in fields:
            normalized["max_position_pct"] = self._optional_pct(
                fields.get("max_position_pct"), "max_position_pct"
            )
        if "required_cash_pct" in fields:
            normalized["required_cash_pct"] = self._optional_pct(
                fields.get("required_cash_pct"), "required_cash_pct"
            )
        if "review_date" in fields:
            normalized["review_date"] = fields.get("review_date")
        if "notify_on_trigger" in fields:
            normalized["notify_on_trigger"] = bool(fields.get("notify_on_trigger"))
        if "notification_channels" in fields:
            normalized["notification_channels"] = json.dumps(
                self._normalize_notification_channels(fields.get("notification_channels")),
                ensure_ascii=False,
            )
        if "check_frequency" in fields and fields.get("check_frequency") is not None:
            normalized["check_frequency"] = self._normalize_choice(
                fields["check_frequency"], CHECK_FREQUENCIES, "check_frequency"
            )

        effective_benchmark = normalized.get("benchmark_symbol", current.get("benchmark_symbol"))
        effective_max = normalized.get("max_position_pct", current.get("max_position_pct"))
        normalized_steps = None
        if steps is not None:
            normalized_steps = self._normalize_steps(
                steps,
                benchmark_symbol=effective_benchmark,
                max_position_pct=effective_max,
            )
        effective_plan = {**current, **normalized}
        effective_steps = normalized_steps if normalized_steps is not None else current["steps"]
        self._validate_check_frequency_market(
            effective_plan["market"],
            effective_plan["check_frequency"],
        )
        self._validate_cross_field_invariants(effective_plan, effective_steps)
        updated = self.repo.update_plan(
            plan_id,
            fields=normalized,
            steps=normalized_steps,
            expected_updated_at=current.get("updated_at"),
        )
        if updated is None:
            raise InvestmentPlanNotFoundError("Investment plan not found")
        return self._decorate_plan(updated)

    def set_plan_status(self, plan_id: int, status: str) -> Dict[str, Any]:
        target = self._normalize_choice(status, PLAN_STATUSES, "status")
        current = self.get_plan(plan_id)
        allowed = {
            "draft": {"active", "closed"},
            "active": {"paused", "closed"},
            "paused": {"active", "closed"},
            "closed": set(),
        }
        if target == current["status"]:
            return current
        if target not in allowed[current["status"]]:
            raise InvestmentPlanStateError(
                f"Cannot transition investment plan from {current['status']} to {target}"
            )
        if target == "active":
            self._validate_activation(current, current["steps"])
        updated = self.repo.update_plan_status(
            plan_id,
            target,
            expected_status=current["status"],
            expected_updated_at=current.get("updated_at"),
        )
        if updated is None:
            raise InvestmentPlanNotFoundError("Investment plan not found")
        return self._decorate_plan(updated)

    def set_step_status(self, plan_id: int, step_id: int, status: str) -> Dict[str, Any]:
        target = self._normalize_choice(status, STEP_STATUSES, "step status")
        current = self.get_plan(plan_id)
        if current["status"] == "closed":
            raise InvestmentPlanStateError("Closed plans cannot change execution steps")
        step = next((item for item in current["steps"] if int(item["id"]) == step_id), None)
        if step is None:
            raise InvestmentPlanNotFoundError("Investment plan step not found")
        allowed = {
            "pending": set(),
            "triggered": {"completed", "skipped", "pending"},
            "completed": set(),
            "skipped": set(),
        }
        if target == step["status"]:
            return current
        if (
            target == "completed"
            and current["strategy_type"] == "index_crash"
            and step["action"] in {"buy", "add"}
        ):
            raise InvestmentPlanStateError(
                "Record the manual execution details before completing this ETF step"
            )
        if target not in allowed[step["status"]]:
            raise InvestmentPlanStateError(
                f"Cannot transition investment plan step from {step['status']} to {target}"
            )
        updated = self.repo.update_step_status(
            plan_id,
            step_id,
            target,
            expected_plan_status=current["status"],
            expected_step_status=step["status"],
            expected_updated_at=current.get("updated_at"),
        )
        if updated is None:
            raise InvestmentPlanNotFoundError("Investment plan or step not found")
        return self._decorate_plan(updated)

    def record_step_execution(
        self,
        plan_id: int,
        step_id: int,
        *,
        execution_at: datetime,
        price: float,
        quantity: float,
        fee: float = 0.0,
        note: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Record a user-confirmed ETF fill and complete its triggered plan step."""
        current = self.get_plan(plan_id)
        if current["strategy_type"] != "index_crash":
            raise InvestmentPlanStateError(
                "Manual execution records currently support index-crash plans only"
            )
        step = next((item for item in current["steps"] if int(item["id"]) == step_id), None)
        if step is None:
            raise InvestmentPlanNotFoundError("Investment plan step not found")
        is_legacy_backfill = (
            step["status"] == "completed"
            and step.get("execution_amount") is None
        )
        if current["status"] == "closed" and not is_legacy_backfill:
            raise InvestmentPlanStateError("Closed plans cannot record new executions")
        if step["status"] != "triggered" and not is_legacy_backfill:
            raise InvestmentPlanStateError(
                "Only a triggered or legacy unrecorded completed step can record an execution"
            )
        if step["action"] not in {"buy", "add"}:
            raise InvestmentPlanStateError("Only buy and add steps can record an ETF execution")
        if (
            step["status"] == "triggered"
            and current["execution_summary"]["unrecorded_completed_count"] > 0
        ):
            raise InvestmentPlanStateError(
                "Backfill legacy completed ETF steps before recording a new execution"
            )

        planned_capital = self._finite_positive(current.get("planned_capital"))
        if planned_capital is None and not is_legacy_backfill:
            raise InvestmentPlanStateError(
                "Set planned_capital before recording an ETF execution"
            )
        normalized_execution_at = self._normalize_execution_at(execution_at)
        triggered_at = str(step.get("triggered_at") or "")
        if (
            triggered_at
            and normalized_execution_at + timedelta(seconds=1) < datetime.fromisoformat(triggered_at)
        ):
            raise ValueError("execution_at cannot be earlier than the step trigger time")
        normalized_price = self._finite_positive(price)
        if normalized_price is None:
            raise ValueError("price must be a finite number greater than 0")
        normalized_quantity = self._finite_positive(quantity)
        if normalized_quantity is None:
            raise ValueError("quantity must be a finite number greater than 0")
        normalized_fee = self._finite_number(fee)
        if normalized_fee is None or normalized_fee < 0:
            raise ValueError("fee must be a finite number greater than or equal to 0")
        execution_amount = round(normalized_price * normalized_quantity, 6)
        prior_cost = float(current["execution_summary"]["total_cost"] or 0.0)
        if (
            planned_capital is not None
            and prior_cost + execution_amount + normalized_fee > planned_capital + 0.01
        ):
            raise InvestmentPlanStateError(
                "This execution exceeds the plan's remaining cash"
            )

        updated = self.repo.record_step_execution(
            plan_id,
            step_id,
            execution_date=normalized_execution_at.date(),
            execution_at=normalized_execution_at,
            execution_price=normalized_price,
            execution_quantity=normalized_quantity,
            execution_amount=execution_amount,
            execution_fee=normalized_fee,
            execution_note=self._clean_optional_text(note, 255),
            max_total_cost=planned_capital,
            expected_plan_status=current["status"],
            expected_updated_at=current.get("updated_at"),
        )
        if updated is None:
            raise InvestmentPlanNotFoundError("Investment plan or step not found")
        return self._decorate_plan(updated)

    # ------------------------------------------------------------------
    # Evaluation and notifications
    # ------------------------------------------------------------------
    def evaluate_plan(self, plan_id: int, *, send_notification: bool = False) -> Dict[str, Any]:
        plan = self.get_plan(plan_id)
        return self._evaluate_plan_snapshot(plan, send_notification=send_notification)

    def _evaluate_plan_snapshot(
        self,
        plan: Dict[str, Any],
        *,
        send_notification: bool = False,
    ) -> Dict[str, Any]:
        """Evaluate a caller-owned snapshot without reloading the same plan."""
        plan_id = int(plan["id"])
        if plan["status"] != "active":
            raise InvestmentPlanStateError("Only active investment plans can be evaluated")
        self._validate_check_frequency_market(
            plan["market"],
            plan["check_frequency"],
        )

        evaluated_at = datetime.now()
        pending_steps = [step for step in plan["steps"] if step["status"] == "pending"]
        metric_values: Dict[str, Optional[float]] = {}
        errors: List[str] = []

        use_realtime_price = plan.get("check_frequency") == "minute"
        quote = self._get_quote(
            plan["symbol"],
            require_observed_at=use_realtime_price,
        )
        current_price = self._get_validated_price(
            plan["symbol"],
            quote or {},
            allow_history_fallback=not use_realtime_price,
        )
        metric_values["price"] = current_price
        if current_price is None and any(step["metric"] == "price" for step in pending_steps):
            errors.append("最新价格不可用")

        if any(step["metric"] == "benchmark_drawdown_250d_pct" for step in pending_steps):
            benchmark = plan.get("benchmark_symbol")
            if not benchmark:
                errors.append("基准回撤档位缺少对标指数")
                metric_values["benchmark_drawdown_250d_pct"] = None
            else:
                drawdown = self._get_benchmark_drawdown(
                    benchmark,
                    use_realtime_price=use_realtime_price,
                )
                metric_values["benchmark_drawdown_250d_pct"] = drawdown
                if drawdown is None:
                    errors.append("基准250日回撤不可用")

        condition_matched_steps = [
            step for step in pending_steps
            if self._matches(step, metric_values.get(step["metric"]))
        ]
        constraints = self._load_constraints({
            **plan,
            "last_price": current_price,
            "last_evaluated_at": evaluated_at.isoformat(),
        })
        current_position = constraints.get("position_pct")
        target_suppressed_steps = [
            step for step in condition_matched_steps
            if current_position is not None
            and step["action"] in {"buy", "add"}
            and step.get("target_position_pct") is not None
            and float(current_position) >= float(step["target_position_pct"])
        ]
        target_suppressed_ids = {int(step["id"]) for step in target_suppressed_steps}
        matched_steps = [
            step for step in condition_matched_steps
            if int(step["id"]) not in target_suppressed_ids
        ]
        outstanding_steps = [step for step in plan["steps"] if step["status"] == "triggered"]
        actionable_steps = [*outstanding_steps, *matched_steps]
        blocked_reasons = self._blocked_reasons(plan, actionable_steps, constraints)
        review_due = bool(plan.get("review_date") and plan["review_date"] <= date.today().isoformat())

        has_risk_or_review_action = any(
            step["action"] in {"reduce", "exit", "review"} for step in actionable_steps
        )
        if actionable_steps and has_risk_or_review_action:
            status = "triggered"
        elif actionable_steps and blocked_reasons:
            status = "blocked"
        elif actionable_steps:
            status = "triggered"
        elif not pending_steps:
            status = "completed"
        elif errors:
            status = "data_missing"
        elif review_due:
            status = "review_due"
        else:
            status = "waiting"

        note_parts = []
        if matched_steps:
            note_parts.append(f"命中 {len(matched_steps)} 个待执行档位")
        if target_suppressed_steps:
            note_parts.append(
                f"{len(target_suppressed_steps)} 个买入档已达到目标仓位，未触发"
            )
        if blocked_reasons:
            note_parts.append("; ".join(blocked_reasons))
        if review_due:
            note_parts.append("已到计划复查日期")
        if errors:
            note_parts.append("; ".join(errors))
        if not note_parts:
            note_parts.append("当前没有命中待执行档位")

        applied = self.repo.apply_evaluation(
            plan_id,
            last_price=current_price,
            evaluation_status=status,
            evaluation_note=" | ".join(note_parts),
            matched_step_ids=[int(step["id"]) for step in matched_steps],
            evaluated_at=evaluated_at,
            blocked_reasons=blocked_reasons,
            expected_updated_at=plan["updated_at"],
            expected_step_statuses={
                int(step["id"]): str(step["status"])
                for step in plan["steps"]
            },
            resolved_name=(quote or {}).get("stock_name"),
        )
        if applied is None:
            raise InvestmentPlanStateError("Investment plan changed while it was being evaluated")
        updated_plan, newly_triggered_ids = applied
        result = {
            "plan": self._decorate_plan(updated_plan),
            "metric_values": metric_values,
            "matched_step_ids": [int(step["id"]) for step in matched_steps],
            "newly_triggered_step_ids": newly_triggered_ids,
            "constraints": constraints,
            "blocked_reasons": blocked_reasons,
            "review_due": review_due,
            "errors": errors,
            "notification": self._empty_notification_result(),
        }
        if send_notification:
            result["notification"] = self.send_pending_notifications(plan_ids=[plan_id])
        return result

    def evaluate_active_plans(
        self,
        *,
        send_notification: bool = False,
        markets: Optional[Iterable[str]] = None,
        check_frequencies: Optional[Iterable[str]] = None,
    ) -> Dict[str, Any]:
        self._quote_cache.clear()
        self._validated_price_cache.clear()
        self._drawdown_cache.clear()
        plans = self.repo.list_plans(status="active")
        if markets is not None:
            allowed_markets = {str(market).strip().lower() for market in markets}
            plans = [plan for plan in plans if plan.get("market") in allowed_markets]
        if check_frequencies is not None:
            allowed_frequencies = {
                self._normalize_choice(value, CHECK_FREQUENCIES, "check_frequency")
                for value in check_frequencies
            }
            plans = [plan for plan in plans if plan.get("check_frequency") in allowed_frequencies]
        results: List[Dict[str, Any]] = []
        errors: List[Dict[str, Any]] = []
        for plan in plans:
            try:
                results.append(self._evaluate_plan_snapshot(plan))
            except Exception as exc:
                logger.warning("投资策略计划评估失败 plan_id=%s: %s", plan.get("id"), exc, exc_info=True)
                errors.append({"plan_id": int(plan["id"]), "message": str(exc)})

        notification = self._empty_notification_result()
        evaluated_plan_ids = [int(item["plan"]["id"]) for item in results]
        if send_notification and evaluated_plan_ids:
            notification = self.send_pending_notifications(plan_ids=evaluated_plan_ids)
        return {
            "evaluated": len(results),
            "triggered": sum(len(item["newly_triggered_step_ids"]) for item in results),
            "errors": errors,
            "results": results,
            "notification": notification,
        }

    def send_pending_notifications(
        self,
        *,
        plan_ids: Optional[Iterable[int]] = None,
    ) -> Dict[str, Any]:
        claim_token = uuid.uuid4().hex
        claimed_at = datetime.now()
        pending = self.repo.claim_unnotified_triggered(
            claim_token=claim_token,
            claimed_at=claimed_at,
            plan_ids=plan_ids,
        )
        step_ids = [int(step["id"]) for plan in pending for step in plan["steps"]]
        if not step_ids:
            return self._empty_notification_result()

        notifier = self.notifier or NotificationService()
        if not notifier.is_available():
            self.repo.complete_notification_claim(
                step_ids,
                claim_token=claim_token,
                completed_at=datetime.now(),
                status="unavailable",
                error="未配置可用的通知渠道",
            )
            return {
                **self._empty_notification_result(),
                "status": "unavailable",
                "step_count": len(step_ids),
                "unavailable_count": len(step_ids),
                "errors": ["未配置可用的通知渠道"],
            }

        grouped: Dict[tuple[str, ...], List[Dict[str, Any]]] = {}
        for plan in pending:
            key = tuple(plan.get("notification_channels") or [])
            grouped.setdefault(key, []).append(plan)

        attempted = False
        sent = False
        sent_count = 0
        failed_count = 0
        unavailable_count = 0
        errors: List[str] = []
        for channel_values, plans in grouped.items():
            group_step_ids = [
                int(step["id"])
                for plan in plans
                for step in plan["steps"]
            ]
            group_sent = False
            group_error: Optional[str] = None
            delivery_values = list(channel_values) if channel_values else None
            target_checker = getattr(notifier, "has_delivery_target", None)
            target_available = (
                bool(target_checker(route_type="alert", channel_values=delivery_values))
                if callable(target_checker)
                else notifier.is_available()
            )
            if not target_available:
                group_error = "计划指定的通知渠道未配置或不可用"
                unavailable_count += len(group_step_ids)
                errors.append(group_error)
                self.repo.complete_notification_claim(
                    group_step_ids,
                    claim_token=claim_token,
                    completed_at=datetime.now(),
                    status="unavailable",
                    error=group_error,
                )
                continue
            try:
                attempted = True
                content = self._build_alert_content(plans)
                group_sent = bool(notifier.send(
                    content,
                    route_type="alert",
                    channel_values=delivery_values,
                ))
                sent = sent or group_sent
                if not group_sent:
                    group_error = "通知渠道返回发送失败"
            except Exception as exc:
                group_error = "通知发送异常，请查看服务日志"
                logger.warning(
                    "投资策略计划通知发送失败 channels=%s: %s",
                    list(channel_values) or ["alert-route"],
                    exc,
                    exc_info=True,
                )
            finally:
                if group_sent:
                    sent_count += len(group_step_ids)
                else:
                    failed_count += len(group_step_ids)
                    errors.append(group_error or "通知发送失败")
                self.repo.complete_notification_claim(
                    group_step_ids,
                    claim_token=claim_token,
                    completed_at=datetime.now(),
                    status="sent" if group_sent else "failed",
                    error=group_error,
                )
        if sent_count == len(step_ids):
            result_status = "sent"
        elif sent_count:
            result_status = "partial"
        elif failed_count:
            result_status = "failed"
        else:
            result_status = "unavailable"
        return {
            "status": result_status,
            "attempted": attempted,
            "sent": sent,
            "step_count": len(step_ids),
            "sent_count": sent_count,
            "failed_count": failed_count,
            "unavailable_count": unavailable_count,
            "errors": errors,
        }

    @staticmethod
    def _empty_notification_result() -> Dict[str, Any]:
        return {
            "status": "idle",
            "attempted": False,
            "sent": False,
            "step_count": 0,
            "sent_count": 0,
            "failed_count": 0,
            "unavailable_count": 0,
            "errors": [],
        }

    # ------------------------------------------------------------------
    # Evaluation helpers
    # ------------------------------------------------------------------
    def _get_quote(
        self,
        symbol: str,
        *,
        require_observed_at: bool = False,
    ) -> Optional[Dict[str, Any]]:
        cache_key = f"{symbol}:{int(require_observed_at)}"
        if cache_key not in self._quote_cache:
            self._quote_cache[cache_key] = self.stock_service.get_realtime_quote(
                symbol,
                enrich=False,
                require_observed_at=require_observed_at,
            )
        return self._quote_cache[cache_key]

    def _get_validated_price(
        self,
        symbol: str,
        quote: Dict[str, Any],
        *,
        allow_history_fallback: bool = True,
    ) -> Optional[float]:
        cache_key = f"{symbol}:{int(allow_history_fallback)}"
        if cache_key in self._validated_price_cache:
            return self._validated_price_cache[cache_key]
        raw_price = self._finite_positive(quote.get("current_price"))
        try:
            from data_provider.base import normalize_stock_code
            from src.core.trading_calendar import get_effective_trading_date, get_market_for_stock

            expected_date = get_effective_trading_date(
                get_market_for_stock(normalize_stock_code(symbol))
            )
            observed_value = (
                quote.get("price_date")
                or quote.get("quote_date")
                or quote.get("as_of_date")
            )
            observed_date = None
            if observed_value and raw_price is not None:
                try:
                    observed_date = date.fromisoformat(str(observed_value)[:10])
                except (TypeError, ValueError):
                    observed_date = None
            if (
                observed_date is not None
                and observed_date >= expected_date
                and raw_price is not None
                and (
                    allow_history_fallback
                    or self._is_minute_quote_fresh(symbol, quote)
                )
            ):
                validated_price = raw_price
            elif allow_history_fallback:
                history = self.stock_service.get_history_data(
                    symbol,
                    period="daily",
                    days=30,
                )
                if history.get("stale") is True or history.get("partial_cache") is True:
                    self._validated_price_cache[cache_key] = None
                    return None
                observed_date = date.fromisoformat(
                    str(history.get("as_of_date") or "")[:10]
                )
                rows = list(history.get("data") or [])
                validated_price = self._finite_positive(
                    rows[-1].get("close") if rows else None
                )
            else:
                validated_price = None
            if observed_date is None or observed_date < expected_date:
                validated_price = None
        except Exception as exc:
            logger.warning("校验策略计划价格时效失败 symbol=%s: %s", symbol, exc)
            validated_price = None
        self._validated_price_cache[cache_key] = validated_price
        return validated_price

    @staticmethod
    def _utc_now() -> datetime:
        return datetime.now(timezone.utc)

    def _is_minute_quote_fresh(self, symbol: str, quote: Dict[str, Any]) -> bool:
        """Require a recent, timezone-aware quote from a confirmed live session."""
        observed_value = quote.get("observed_at")
        try:
            observed_at = datetime.fromisoformat(str(observed_value or "").replace("Z", "+00:00"))
        except ValueError:
            return False
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            return False

        checked_at = self._utc_now()
        age = checked_at - observed_at.astimezone(timezone.utc)
        if age < -MINUTE_QUOTE_FUTURE_TOLERANCE or age > MINUTE_QUOTE_MAX_AGE:
            return False

        try:
            from data_provider.base import normalize_stock_code
            from src.core.trading_calendar import get_market_for_stock, is_market_trading_now

            market = get_market_for_stock(normalize_stock_code(symbol))
            return bool(
                market
                and is_market_trading_now(market, current_time=observed_at)
                and is_market_trading_now(market, current_time=checked_at)
            )
        except Exception as exc:
            logger.warning("校验分钟策略行情时间失败 symbol=%s: %s", symbol, exc)
            return False

    def _get_benchmark_drawdown(
        self,
        symbol: str,
        *,
        use_realtime_price: bool = False,
    ) -> Optional[float]:
        cache_key = f"{symbol}:{int(use_realtime_price)}"
        if cache_key in self._drawdown_cache:
            return self._drawdown_cache[cache_key]
        latest_price = None
        latest_high = None
        if use_realtime_price:
            quote = self._get_quote(symbol, require_observed_at=True)
            latest_price = self._get_validated_price(
                symbol,
                quote or {},
                allow_history_fallback=False,
            )
            if latest_price is None:
                self._drawdown_cache[cache_key] = None
                return None
            latest_high = self._finite_positive((quote or {}).get("high"))
        metrics = self.etf_history_service.get_metrics(
            symbol,
            latest_price=latest_price,
            latest_high=latest_high,
        )
        self._drawdown_cache[cache_key] = (
            metrics.drawdown_250d_pct if metrics.reliable else None
        )
        return self._drawdown_cache[cache_key]

    def _load_constraints(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        account_id = plan.get("account_id")
        if account_id is None:
            summary = self._execution_summary(plan)
            if summary["planned_capital"] is not None:
                equity = float(summary["remaining_cash"] or 0.0) + float(
                    summary["market_value"] or 0.0
                )
                return {
                    "position_pct": summary["capital_utilization_pct"],
                    "cash_pct": round(float(summary["remaining_cash"] or 0.0) / equity * 100.0, 4)
                    if equity > 0
                    else None,
                    "reliable": True,
                }
            return {"position_pct": None, "cash_pct": None, "reliable": True}
        try:
            snapshot = self.portfolio_service.get_portfolio_snapshot(account_id=int(account_id))
            accounts = snapshot.get("accounts") or []
            account = accounts[0] if accounts else None
            if not account:
                return {"position_pct": None, "cash_pct": None, "reliable": False}
            positions = list(account.get("positions") or [])
            has_unreliable_valuation = bool(
                account.get("fx_stale") or snapshot.get("fx_stale")
            ) or any(
                not self._is_position_valuation_reliable(position)
                for position in positions
            )
            if has_unreliable_valuation:
                return {"position_pct": None, "cash_pct": None, "reliable": False}
            equity = self._finite_positive(account.get("total_equity"))
            if equity is None:
                return {"position_pct": None, "cash_pct": None, "reliable": False}
            cash = self._finite_number(account.get("total_cash")) or 0.0
            position_value = 0.0
            for position in positions:
                if self._same_symbol(position.get("symbol"), plan["symbol"]):
                    position_value += self._finite_number(position.get("market_value_base")) or 0.0
            return {
                "position_pct": round(position_value / equity * 100.0, 4),
                "cash_pct": round(cash / equity * 100.0, 4),
                "reliable": True,
            }
        except Exception as exc:
            logger.warning("读取策略计划账户约束失败 plan_id=%s: %s", plan.get("id"), exc)
            return {"position_pct": None, "cash_pct": None, "reliable": False}

    @staticmethod
    def _is_position_valuation_reliable(position: Dict[str, Any]) -> bool:
        if position.get("price_available") is False:
            return False
        if position.get("price_stale") is not True:
            return True
        try:
            from src.core.trading_calendar import get_effective_trading_date, get_market_for_stock

            symbol = str(position.get("symbol") or "")
            market = str(position.get("market") or "").strip().lower()
            if market not in {"cn", "hk", "us"}:
                market = get_market_for_stock(symbol) or ""
            price_date = date.fromisoformat(str(position.get("price_date") or "")[:10])
            return price_date >= get_effective_trading_date(market or None)
        except Exception:
            return False

    @staticmethod
    def _blocked_reasons(
        plan: Dict[str, Any],
        matched_steps: Sequence[Dict[str, Any]],
        constraints: Dict[str, Any],
    ) -> List[str]:
        if not any(step["action"] in {"buy", "add"} for step in matched_steps):
            return []
        reasons: List[str] = []
        if plan.get("account_id") is not None:
            if constraints.get("reliable") is not True:
                reasons.append("账户估值数据不可用")
            max_position = plan.get("max_position_pct")
            current_position = constraints.get("position_pct")
            if max_position is not None:
                if current_position is None:
                    reasons.append("当前仓位比例不可用")
                elif current_position >= float(max_position):
                    reasons.append("已达到最大仓位")
            required_cash = plan.get("required_cash_pct")
            cash_pct = constraints.get("cash_pct")
            if required_cash is not None:
                if cash_pct is None:
                    reasons.append("现金比例不可用")
                elif cash_pct <= float(required_cash):
                    reasons.append("已触及现金底线")
                elif current_position is not None:
                    target_positions = [
                        float(step["target_position_pct"])
                        for step in matched_steps
                        if step["action"] in {"buy", "add"}
                        and step.get("target_position_pct") is not None
                    ]
                    if target_positions:
                        additional_position = max(
                            0.0,
                            max(target_positions) - float(current_position),
                        )
                        projected_cash_pct = float(cash_pct) - additional_position
                        if projected_cash_pct < float(required_cash):
                            reasons.append("执行目标仓位后将低于现金底线")
        return reasons

    @staticmethod
    def _matches(step: Dict[str, Any], value: Optional[float]) -> bool:
        if value is None:
            return False
        threshold = float(step["threshold"])
        operator = step["operator"]
        if operator == "lte":
            return value <= threshold
        if operator == "gte":
            return value >= threshold
        upper = step.get("upper_threshold")
        return upper is not None and threshold <= value <= float(upper)

    def _build_alert_content(self, plans: Sequence[Dict[str, Any]]) -> str:
        lines = ["# 📋 投资策略计划触发", ""]
        for plan in plans:
            title = plan.get("name") or plan["symbol"]
            lines.extend([
                f"## {title}（{plan['symbol']}）",
                f"- 策略：{STRATEGY_LABELS.get(plan['strategy_type'], plan['strategy_type'])}",
                f"- 当前价：{self._format_number(plan.get('last_price'))}",
            ])
            blocked_reasons = list(plan.get("last_blocked_reasons") or [])
            if blocked_reasons:
                lines.append(f"- 买入纪律：受限（{'；'.join(blocked_reasons)}）")
            for step in plan["steps"]:
                lines.append(
                    f"- 触发：{ACTION_LABELS.get(step['action'], step['action'])} · "
                    f"{self._format_step_condition(step)}"
                )
                if step.get("target_position_pct") is not None:
                    lines.append(f"  - 目标仓位：{step['target_position_pct']:.2f}%")
                if step.get("note"):
                    lines.append(f"  - 备注：{step['note']}")
            lines.extend([
                f"- 投资逻辑：{plan['thesis']}",
                f"- 失效条件：{plan['invalidation_note']}",
                "",
            ])
        lines.append("> 仅作计划触发提醒，请核对最新行情并人工决策；系统不会自动下单。")
        return "\n".join(lines)

    @staticmethod
    def _format_step_condition(step: Dict[str, Any]) -> str:
        metric = "价格" if step["metric"] == "price" else "基准250日回撤"
        suffix = "" if step["metric"] == "price" else "%"
        if step["operator"] == "between":
            return f"{metric} {step['threshold']:.2f}–{step['upper_threshold']:.2f}{suffix}"
        op = "≤" if step["operator"] == "lte" else "≥"
        return f"{metric} {op} {step['threshold']:.2f}{suffix}"

    # ------------------------------------------------------------------
    # Validation and serialization helpers
    # ------------------------------------------------------------------
    def _validate_account(self, account_id: Optional[int]) -> None:
        if account_id is None:
            return
        if self.portfolio_service.repo.get_account(int(account_id)) is None:
            raise ValueError("account_id does not reference an active portfolio account")

    @staticmethod
    def _validate_check_frequency_market(market: str, check_frequency: str) -> None:
        if check_frequency == "minute" and market not in MINUTE_CHECK_MARKETS:
            raise ValueError("minute check frequency currently supports cn and hk markets only")

    @classmethod
    def _normalize_steps(
        cls,
        steps: Sequence[Dict[str, Any]],
        *,
        benchmark_symbol: Optional[str],
        max_position_pct: Optional[float],
    ) -> List[Dict[str, Any]]:
        normalized: List[Dict[str, Any]] = []
        for index, raw in enumerate(steps):
            action = cls._normalize_choice(raw.get("action"), STEP_ACTIONS, "step action")
            metric = cls._normalize_choice(raw.get("metric"), STEP_METRICS, "step metric")
            operator = cls._normalize_choice(raw.get("operator"), STEP_OPERATORS, "step operator")
            threshold = cls._finite_number(raw.get("threshold"))
            if threshold is None:
                raise ValueError("step threshold must be a finite number")
            if metric == "price" and threshold <= 0:
                raise ValueError("price step threshold must be > 0")
            if metric == "benchmark_drawdown_250d_pct":
                if not benchmark_symbol:
                    raise ValueError("benchmark_symbol is required for benchmark drawdown steps")
                if threshold < 0 or threshold > 100:
                    raise ValueError("benchmark drawdown threshold must be between 0 and 100")
            upper = cls._finite_number(raw.get("upper_threshold"))
            if operator == "between":
                if upper is None or upper <= threshold:
                    raise ValueError("between steps require upper_threshold greater than threshold")
            else:
                upper = None
            if metric == "benchmark_drawdown_250d_pct" and upper is not None and upper > 100:
                raise ValueError("benchmark drawdown upper_threshold must be between 0 and 100")
            target = cls._optional_pct(raw.get("target_position_pct"), "target_position_pct")
            if target is not None and max_position_pct is not None and target > max_position_pct:
                raise ValueError("step target_position_pct cannot exceed max_position_pct")
            normalized.append({
                "action": action,
                "metric": metric,
                "operator": operator,
                "threshold": threshold,
                "upper_threshold": upper,
                "target_position_pct": target,
                "note": cls._clean_optional_text(raw.get("note"), 255),
                "sort_order": index,
                "status": "pending",
            })
        return normalized

    @staticmethod
    def _validate_activation(plan: Dict[str, Any], steps: Sequence[Dict[str, Any]]) -> None:
        InvestmentPlanService._validate_check_frequency_market(
            plan["market"],
            plan["check_frequency"],
        )
        if not steps:
            raise ValueError("An active investment plan requires at least one execution step")
        if not str(plan.get("thesis") or "").strip():
            raise ValueError("thesis is required")
        if not str(plan.get("invalidation_note") or "").strip():
            raise ValueError("invalidation_note is required")

    @staticmethod
    def _validate_cross_field_invariants(
        plan: Dict[str, Any],
        steps: Sequence[Dict[str, Any]],
    ) -> None:
        benchmark_symbol = plan.get("benchmark_symbol")
        max_position_pct = plan.get("max_position_pct")
        for step in steps:
            if step.get("metric") == "benchmark_drawdown_250d_pct" and not benchmark_symbol:
                raise ValueError("benchmark_symbol is required for benchmark drawdown steps")
            target = step.get("target_position_pct")
            if target is not None and max_position_pct is not None and float(target) > float(max_position_pct):
                raise ValueError("step target_position_pct cannot exceed max_position_pct")

    @staticmethod
    def _summarize(items: Sequence[Dict[str, Any]]) -> Dict[str, int]:
        today = date.today().isoformat()
        active_items = [item for item in items if item["status"] == "active"]
        return {
            "active": len(active_items),
            "triggered": sum(
                any(step["status"] == "triggered" for step in item["steps"])
                for item in active_items
            ),
            "blocked": sum(item.get("last_evaluation_status") == "blocked" for item in active_items),
            "review_due": sum(
                bool(item.get("review_date") and item["review_date"] <= today)
                for item in active_items
            ),
            "data_missing": sum(
                item.get("last_evaluation_status") == "data_missing" for item in active_items
            ),
        }

    @classmethod
    def _decorate_plan(cls, plan: Dict[str, Any]) -> Dict[str, Any]:
        payload = dict(plan)
        steps = []
        for raw_step in plan.get("steps", []):
            step = dict(raw_step)
            if step.get("notified_at"):
                step["notification_status"] = "sent"
            elif (
                step.get("status") == "triggered"
                and plan.get("status") == "active"
                and plan.get("notify_on_trigger")
                and not step.get("notification_status")
            ):
                step["notification_status"] = "pending"
            steps.append(step)
        payload["steps"] = steps
        payload["strategy_label"] = STRATEGY_LABELS.get(plan["strategy_type"], plan["strategy_type"])
        payload["review_due"] = bool(
            plan.get("review_date") and plan["review_date"] <= date.today().isoformat()
        )
        payload["triggered_step_count"] = sum(
            step["status"] == "triggered" for step in plan.get("steps", [])
        )
        payload["execution_summary"] = cls._execution_summary(plan)
        return payload

    @classmethod
    def _execution_summary(cls, plan: Dict[str, Any]) -> Dict[str, Any]:
        completed_buy_steps = [
            step
            for step in plan.get("steps", [])
            if step.get("status") == "completed" and step.get("action") in {"buy", "add"}
        ]
        executions = [
            step
            for step in completed_buy_steps
            if cls._finite_positive(step.get("execution_quantity")) is not None
            and cls._finite_positive(step.get("execution_amount")) is not None
        ]
        unrecorded_completed_count = len(completed_buy_steps) - len(executions)
        execution_data_complete = unrecorded_completed_count == 0
        planned_capital = cls._finite_positive(plan.get("planned_capital"))
        gross_amount = sum(float(step["execution_amount"]) for step in executions)
        total_fees = sum(cls._finite_number(step.get("execution_fee")) or 0.0 for step in executions)
        total_cost = gross_amount + total_fees
        total_quantity = sum(float(step["execution_quantity"]) for step in executions)
        def execution_sort_value(step: Dict[str, Any]) -> str:
            actual_time = str(step.get("execution_at") or "")
            if actual_time:
                return actual_time
            actual_date = str(step.get("execution_date") or "")[:10]
            return f"{actual_date}T23:59:59.999999" if actual_date else ""

        latest_execution = max(
            executions,
            key=lambda step: (
                execution_sort_value(step),
                int(step.get("id") or 0),
            ),
            default=None,
        )
        latest_execution_date = (
            str(latest_execution.get("execution_date") or "")[:10]
            if latest_execution
            else ""
        )
        latest_execution_at = (
            str(latest_execution.get("execution_at") or "")
            if latest_execution
            else ""
        )
        last_evaluated_at = str(plan.get("last_evaluated_at") or "")
        checked_price = cls._finite_positive(plan.get("last_price"))
        if latest_execution is None:
            check_is_newer = True
        elif latest_execution_at:
            check_is_newer = last_evaluated_at > latest_execution_at
        else:
            check_is_newer = last_evaluated_at[:10] > latest_execution_date
        use_checked_price = checked_price is not None and check_is_newer
        valuation_price = (
            checked_price
            if use_checked_price
            else cls._finite_positive(
                latest_execution.get("execution_price") if latest_execution else None
            )
        )
        valuation_price_source = (
            "plan_check"
            if use_checked_price
            else "latest_execution" if valuation_price is not None else None
        )
        valuation_as_of_date = (
            last_evaluated_at[:10]
            if use_checked_price
            else latest_execution_date or None
        )
        market_value = (
            total_quantity * valuation_price
            if execution_data_complete and valuation_price is not None
            else None
        )
        remaining_cash = (
            max(0.0, planned_capital - total_cost)
            if execution_data_complete and planned_capital is not None
            else None
        )
        average_cost = (
            total_cost / total_quantity
            if execution_data_complete and total_quantity > 0
            else None
        )
        unrealized_pnl = market_value - total_cost if market_value is not None and executions else None
        return_pct = unrealized_pnl / total_cost * 100.0 if unrealized_pnl is not None and total_cost > 0 else None
        target_values = [
            float(step["target_position_pct"])
            for step in completed_buy_steps
            if step.get("target_position_pct") is not None
        ]
        target_position_pct = max(target_values) if target_values else None
        equity = (
            remaining_cash + market_value
            if remaining_cash is not None and market_value is not None
            else None
        )
        capital_utilization_pct = (
            market_value / equity * 100.0
            if equity is not None and equity > 0 and market_value is not None
            else (
                0.0
                if execution_data_complete and planned_capital is not None and not executions
                else None
            )
        )
        target_deviation_pct = (
            capital_utilization_pct - target_position_pct
            if plan.get("account_id") is None
            and capital_utilization_pct is not None
            and target_position_pct is not None
            else None
        )

        def rounded(value: Optional[float], digits: int = 4) -> Optional[float]:
            return round(value, digits) if value is not None else None

        return {
            "completed_execution_count": len(executions),
            "unrecorded_completed_count": unrecorded_completed_count,
            "execution_data_complete": execution_data_complete,
            "planned_capital": rounded(planned_capital, 2),
            "total_quantity": rounded(total_quantity),
            "gross_amount": rounded(gross_amount, 2),
            "total_fees": rounded(total_fees, 2),
            "total_cost": rounded(total_cost, 2),
            "average_cost": rounded(average_cost),
            "remaining_cash": rounded(remaining_cash, 2),
            "valuation_price": rounded(valuation_price),
            "valuation_price_source": valuation_price_source,
            "valuation_as_of_date": valuation_as_of_date,
            "market_value": rounded(market_value, 2),
            "unrealized_pnl": rounded(unrealized_pnl, 2),
            "return_pct": rounded(return_pct),
            "target_position_pct": rounded(target_position_pct),
            "capital_utilization_pct": rounded(capital_utilization_pct),
            "target_deviation_pct": rounded(target_deviation_pct),
        }

    @staticmethod
    def _normalize_market(value: str) -> str:
        market = str(value or "").strip().lower()
        if market not in {"cn", "hk", "us"}:
            raise ValueError("market must be cn, hk or us")
        return market

    @staticmethod
    def _normalize_symbol(value: str, market: str) -> str:
        raw = str(value or "").strip().upper()
        if market == "hk":
            if raw.isdigit():
                digits = raw
            else:
                normalized = normalize_code(raw)
                if not normalized:
                    raise ValueError("symbol is invalid")
                digits = normalized.removeprefix("HK")
            if not digits.isdigit() or not 1 <= len(digits) <= 5:
                raise ValueError("Hong Kong symbols must contain one to five digits")
            return f"HK{digits.zfill(5)}"
        normalized = normalize_code(raw)
        if not normalized:
            raise ValueError("symbol is invalid")
        if market == "cn":
            if not normalized.isdigit() or len(normalized) != 6:
                raise ValueError("CN symbols must contain six digits")
            return _exchange_aware_stock_identity(str(value or ""))
        us_symbol = normalized.removesuffix(".US").upper()
        if not (is_us_stock_code(us_symbol) or is_us_index_code(us_symbol)):
            raise ValueError("US symbols must use a valid US stock or index code")
        return us_symbol

    @staticmethod
    def _normalize_optional_symbol(value: Optional[str]) -> Optional[str]:
        text = str(value or "").strip().upper()
        if not text:
            return None
        if len(text) > 16:
            raise ValueError("symbol must be at most 16 characters")
        return canonical_stock_code(text)

    @staticmethod
    def _normalize_filter_symbol(value: Optional[str]) -> Optional[str]:
        text = str(value or "").strip().upper()
        if not text:
            return None
        if text.isdigit() and 1 <= len(text) <= 5:
            return f"HK{text.zfill(5)}"
        normalized = normalize_code(text)
        if not normalized:
            raise ValueError("symbol is invalid")
        if text.startswith("HK") or text.endswith(".HK"):
            return f"HK{normalized.zfill(5)}"
        if normalized.isdigit():
            return _exchange_aware_stock_identity(text)
        return normalized.removesuffix(".US").upper()

    @staticmethod
    def _normalize_notification_channels(values: Optional[Sequence[str]]) -> List[str]:
        if values and len(values) > 1:
            raise ValueError("notification_channels accepts at most one channel")
        normalized: List[str] = []
        for value in values or []:
            channel = str(value or "").strip().lower()
            if channel not in ROUTABLE_NOTIFICATION_CHANNEL_SET:
                raise ValueError(f"Unsupported notification channel: {channel or value}")
            if channel not in normalized:
                normalized.append(channel)
        return normalized

    @staticmethod
    def _normalize_choice(value: Any, allowed: Iterable[str], field: str) -> str:
        normalized = str(value or "").strip().lower()
        if normalized not in allowed:
            raise ValueError(f"{field} must be one of: {', '.join(sorted(allowed))}")
        return normalized

    @staticmethod
    def _require_text(value: Any, field: str, max_length: int) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError(f"{field} is required")
        if len(text) > max_length:
            raise ValueError(f"{field} must be at most {max_length} characters")
        return text

    @staticmethod
    def _clean_optional_text(value: Any, max_length: int) -> Optional[str]:
        text = str(value or "").strip()
        if not text:
            return None
        if len(text) > max_length:
            raise ValueError(f"text must be at most {max_length} characters")
        return text

    @staticmethod
    def _finite_number(value: Any) -> Optional[float]:
        if value is None or value == "":
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    @classmethod
    def _finite_positive(cls, value: Any) -> Optional[float]:
        number = cls._finite_number(value)
        return number if number is not None and number > 0 else None

    @classmethod
    def _optional_positive(cls, value: Any, field: str) -> Optional[float]:
        if value is None or value == "":
            return None
        number = cls._finite_positive(value)
        if number is None:
            raise ValueError(f"{field} must be a finite number greater than 0")
        return number

    @staticmethod
    def _normalize_execution_at(value: Any) -> datetime:
        if isinstance(value, datetime):
            normalized = value
        else:
            try:
                normalized = datetime.fromisoformat(str(value or ""))
            except ValueError as exc:
                raise ValueError("execution_at must use an ISO-8601 date and time") from exc
        if normalized.tzinfo is not None:
            normalized = normalized.astimezone().replace(tzinfo=None)
        if normalized > datetime.now():
            raise ValueError("execution_at cannot be in the future")
        return normalized

    @classmethod
    def _optional_pct(cls, value: Any, field: str) -> Optional[float]:
        number = cls._finite_number(value)
        if value is None or value == "":
            return None
        if number is None or number < 0 or number > 100:
            raise ValueError(f"{field} must be between 0 and 100")
        return number

    @staticmethod
    def _same_symbol(left: Any, right: Any) -> bool:
        def _identity(value: Any) -> str:
            raw = str(value or "").strip().upper()
            if raw.isdigit() and 1 <= len(raw) <= 5:
                return f"HK:{raw.zfill(5)}"
            normalized = normalize_code(raw) or raw
            if raw.startswith("HK") or raw.endswith(".HK"):
                digits = normalized.removeprefix("HK")
                if digits.isdigit() and 1 <= len(digits) <= 5:
                    return f"HK:{digits.zfill(5)}"
            us_symbol = normalized.removesuffix(".US")
            if is_us_stock_code(us_symbol) or is_us_index_code(us_symbol):
                return f"US:{us_symbol}"
            return f"CN:{_exchange_aware_stock_identity(raw)}"

        return _identity(left) == _identity(right)

    @staticmethod
    def _format_number(value: Any) -> str:
        try:
            return f"{float(value):.4f}"
        except (TypeError, ValueError):
            return "数据不足"


__all__ = [
    "ACTION_LABELS",
    "InvestmentPlanBusyError",
    "InvestmentPlanConflictError",
    "InvestmentPlanNotFoundError",
    "InvestmentPlanService",
    "InvestmentPlanStateError",
    "PLAN_STATUSES",
    "STEP_ACTIONS",
    "STEP_METRICS",
    "STEP_OPERATORS",
    "STEP_STATUSES",
    "STRATEGY_LABELS",
    "STRATEGY_TYPES",
]
