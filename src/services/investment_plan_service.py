# -*- coding: utf-8 -*-
"""Investment plan lifecycle, deterministic evaluation, and alert delivery."""

from __future__ import annotations

import json
import logging
import math
import uuid
from datetime import date, datetime
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
CHECK_FREQUENCIES = {"daily", "hourly", "manual"}

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
            normalized["strategy_type"] = self._normalize_choice(
                fields["strategy_type"], STRATEGY_TYPES, "strategy_type"
            )
        if "thesis" in fields and fields.get("thesis") is not None:
            normalized["thesis"] = self._require_text(fields["thesis"], "thesis", 4000)
        if "invalidation_note" in fields and fields.get("invalidation_note") is not None:
            normalized["invalidation_note"] = self._require_text(
                fields["invalidation_note"], "invalidation_note", 4000
            )
        if "benchmark_symbol" in fields:
            normalized["benchmark_symbol"] = self._normalize_optional_symbol(fields.get("benchmark_symbol"))
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

    # ------------------------------------------------------------------
    # Evaluation and notifications
    # ------------------------------------------------------------------
    def evaluate_plan(self, plan_id: int, *, send_notification: bool = False) -> Dict[str, Any]:
        plan = self.get_plan(plan_id)
        if plan["status"] != "active":
            raise InvestmentPlanStateError("Only active investment plans can be evaluated")

        evaluated_at = datetime.now()
        pending_steps = [step for step in plan["steps"] if step["status"] == "pending"]
        metric_values: Dict[str, Optional[float]] = {}
        errors: List[str] = []

        quote = self._get_quote(plan["symbol"])
        current_price = self._get_validated_price(plan["symbol"], quote or {})
        metric_values["price"] = current_price
        if current_price is None and any(step["metric"] == "price" for step in pending_steps):
            errors.append("最新价格不可用")

        if any(step["metric"] == "benchmark_drawdown_250d_pct" for step in pending_steps):
            benchmark = plan.get("benchmark_symbol")
            if not benchmark:
                errors.append("基准回撤档位缺少对标指数")
                metric_values["benchmark_drawdown_250d_pct"] = None
            else:
                drawdown = self._get_benchmark_drawdown(benchmark)
                metric_values["benchmark_drawdown_250d_pct"] = drawdown
                if drawdown is None:
                    errors.append("基准250日回撤不可用")

        condition_matched_steps = [
            step for step in pending_steps
            if self._matches(step, metric_values.get(step["metric"]))
        ]
        constraints = self._load_constraints(plan)
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
            "notification": {"attempted": False, "sent": False, "step_count": 0},
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
                results.append(self.evaluate_plan(int(plan["id"])))
            except Exception as exc:
                logger.warning("投资策略计划评估失败 plan_id=%s: %s", plan.get("id"), exc, exc_info=True)
                errors.append({"plan_id": int(plan["id"]), "message": str(exc)})

        notification = {"attempted": False, "sent": False, "step_count": 0}
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
            return {"attempted": False, "sent": False, "step_count": 0}

        notifier = self.notifier or NotificationService()
        if not notifier.is_available():
            self.repo.complete_notification_claim(
                step_ids,
                claim_token=claim_token,
                completed_at=datetime.now(),
                sent=False,
            )
            return {"attempted": False, "sent": False, "step_count": len(step_ids)}

        grouped: Dict[tuple[str, ...], List[Dict[str, Any]]] = {}
        for plan in pending:
            key = tuple(plan.get("notification_channels") or [])
            grouped.setdefault(key, []).append(plan)

        attempted = False
        sent = False
        for channel_values, plans in grouped.items():
            group_step_ids = [
                int(step["id"])
                for plan in plans
                for step in plan["steps"]
            ]
            group_sent = False
            try:
                attempted = True
                content = self._build_alert_content(plans)
                group_sent = bool(notifier.send(
                    content,
                    route_type="alert",
                    channel_values=list(channel_values) if channel_values else None,
                ))
                sent = sent or group_sent
            except Exception as exc:
                logger.warning(
                    "投资策略计划通知发送失败 channels=%s: %s",
                    list(channel_values) or ["alert-route"],
                    exc,
                    exc_info=True,
                )
            finally:
                self.repo.complete_notification_claim(
                    group_step_ids,
                    claim_token=claim_token,
                    completed_at=datetime.now(),
                    sent=group_sent,
                )
        return {"attempted": attempted, "sent": sent, "step_count": len(step_ids)}

    # ------------------------------------------------------------------
    # Evaluation helpers
    # ------------------------------------------------------------------
    def _get_quote(self, symbol: str) -> Optional[Dict[str, Any]]:
        if symbol not in self._quote_cache:
            self._quote_cache[symbol] = self.stock_service.get_realtime_quote(symbol)
        return self._quote_cache[symbol]

    def _get_validated_price(self, symbol: str, quote: Dict[str, Any]) -> Optional[float]:
        if symbol in self._validated_price_cache:
            return self._validated_price_cache[symbol]
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
            if observed_value and raw_price is not None:
                observed_date = date.fromisoformat(str(observed_value)[:10])
                validated_price = raw_price
            else:
                history = self.stock_service.get_history_data(
                    symbol,
                    period="daily",
                    days=30,
                )
                if history.get("stale") is True or history.get("partial_cache") is True:
                    self._validated_price_cache[symbol] = None
                    return None
                observed_date = date.fromisoformat(
                    str(history.get("as_of_date") or "")[:10]
                )
                rows = list(history.get("data") or [])
                validated_price = self._finite_positive(
                    rows[-1].get("close") if rows else None
                )
            if observed_date < expected_date:
                validated_price = None
        except Exception as exc:
            logger.warning("校验策略计划价格时效失败 symbol=%s: %s", symbol, exc)
            validated_price = None
        self._validated_price_cache[symbol] = validated_price
        return validated_price

    def _get_benchmark_drawdown(self, symbol: str) -> Optional[float]:
        if symbol in self._drawdown_cache:
            return self._drawdown_cache[symbol]
        history = self.stock_service.get_history_data(symbol, period="daily", days=550)
        if history.get("stale") is True or history.get("partial_cache") is True:
            self._drawdown_cache[symbol] = None
            return None
        try:
            from data_provider.base import normalize_stock_code
            from src.core.trading_calendar import get_effective_trading_date, get_market_for_stock

            as_of_date = date.fromisoformat(str(history.get("as_of_date") or "")[:10])
            expected_date = get_effective_trading_date(
                get_market_for_stock(normalize_stock_code(symbol))
            )
        except (TypeError, ValueError):
            self._drawdown_cache[symbol] = None
            return None
        if as_of_date < expected_date:
            self._drawdown_cache[symbol] = None
            return None
        rows = list(history.get("data") or [])[-250:]
        if len(rows) < 250:
            self._drawdown_cache[symbol] = None
            return None
        highs = [self._finite_positive(row.get("high")) for row in rows]
        highs = [value for value in highs if value is not None]
        latest = self._finite_positive(rows[-1].get("close"))
        if not highs or latest is None:
            self._drawdown_cache[symbol] = None
            return None
        peak = max(highs)
        drawdown = max(0.0, (peak - latest) / peak * 100.0)
        self._drawdown_cache[symbol] = round(drawdown, 4)
        return self._drawdown_cache[symbol]

    def _load_constraints(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        account_id = plan.get("account_id")
        if account_id is None:
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

    @staticmethod
    def _decorate_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
        payload = dict(plan)
        payload["strategy_label"] = STRATEGY_LABELS.get(plan["strategy_type"], plan["strategy_type"])
        payload["review_due"] = bool(
            plan.get("review_date") and plan["review_date"] <= date.today().isoformat()
        )
        payload["triggered_step_count"] = sum(
            step["status"] == "triggered" for step in plan.get("steps", [])
        )
        return payload

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
