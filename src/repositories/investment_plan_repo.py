# -*- coding: utf-8 -*-
"""Database access for investment strategy plans and deterministic steps."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
import json
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.exc import OperationalError

from src.storage import DatabaseManager, InvestmentPlan, InvestmentPlanStep


class InvestmentPlanConflictError(Exception):
    """Raised when a scope already has an open plan for the same symbol."""


class InvestmentPlanBusyError(Exception):
    """Raised when SQLite cannot acquire the plan writer lock."""


class InvestmentPlanRepository:
    """Persist plans, steps, lifecycle transitions, and evaluation state."""

    def __init__(self, db_manager: Optional[DatabaseManager] = None):
        self.db = db_manager or DatabaseManager.get_instance()

    @contextmanager
    def write_session(self):
        session = self.db.get_session()
        try:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        except OperationalError as exc:
            session.close()
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise InvestmentPlanBusyError("Investment plan store is busy; please retry shortly.") from exc
            raise

        try:
            yield session
            session.commit()
        except OperationalError as exc:
            session.rollback()
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise InvestmentPlanBusyError("Investment plan store is busy; please retry shortly.") from exc
            raise
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def create_plan(self, *, fields: Dict[str, Any], steps: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        with self.write_session() as session:
            existing = self._find_open_plan_in_session(
                session,
                symbol=str(fields["symbol"]),
                account_id=fields.get("account_id"),
            )
            if existing is not None:
                raise InvestmentPlanConflictError("An open plan already exists for this symbol and scope")

            row = InvestmentPlan(**fields)
            session.add(row)
            session.flush()
            for payload in steps:
                session.add(InvestmentPlanStep(plan_id=int(row.id), **payload))
            session.flush()
            return self._plan_to_dict(row, self._list_steps_in_session(session, int(row.id)))

    def list_plans(
        self,
        *,
        status: Optional[str] = None,
        strategy_type: Optional[str] = None,
        symbol: Optional[str] = None,
        account_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        with self.db.get_session() as session:
            conditions = []
            if status:
                conditions.append(InvestmentPlan.status == status)
            if strategy_type:
                conditions.append(InvestmentPlan.strategy_type == strategy_type)
            if symbol:
                conditions.append(InvestmentPlan.symbol == symbol)
            if account_id is not None:
                conditions.append(InvestmentPlan.account_id == account_id)
            query = select(InvestmentPlan)
            if conditions:
                query = query.where(and_(*conditions))
            rows = session.execute(
                query.order_by(InvestmentPlan.updated_at.desc(), InvestmentPlan.id.desc())
            ).scalars().all()
            return [
                self._plan_to_dict(row, self._list_steps_in_session(session, int(row.id)))
                for row in rows
            ]

    def get_plan(self, plan_id: int) -> Optional[Dict[str, Any]]:
        with self.db.get_session() as session:
            row = session.execute(
                select(InvestmentPlan).where(InvestmentPlan.id == plan_id).limit(1)
            ).scalar_one_or_none()
            if row is None:
                return None
            return self._plan_to_dict(row, self._list_steps_in_session(session, plan_id))

    def update_plan(
        self,
        plan_id: int,
        *,
        fields: Dict[str, Any],
        steps: Optional[Sequence[Dict[str, Any]]] = None,
        expected_updated_at: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        with self.write_session() as session:
            row = session.execute(
                select(InvestmentPlan).where(InvestmentPlan.id == plan_id).limit(1)
            ).scalar_one_or_none()
            if row is None:
                return None
            if expected_updated_at and self._datetime_to_str(row.updated_at) != expected_updated_at:
                raise InvestmentPlanConflictError("Investment plan changed; reload and retry")
            for key, value in fields.items():
                setattr(row, key, value)
            row.updated_at = datetime.now()
            if steps is not None:
                session.execute(delete(InvestmentPlanStep).where(InvestmentPlanStep.plan_id == plan_id))
                for payload in steps:
                    session.add(InvestmentPlanStep(plan_id=plan_id, **payload))
            session.flush()
            return self._plan_to_dict(row, self._list_steps_in_session(session, plan_id))

    def update_plan_status(
        self,
        plan_id: int,
        status: str,
        *,
        expected_status: Optional[str] = None,
        expected_updated_at: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        with self.write_session() as session:
            row = session.execute(
                select(InvestmentPlan).where(InvestmentPlan.id == plan_id).limit(1)
            ).scalar_one_or_none()
            if row is None:
                return None
            if expected_status and row.status != expected_status:
                raise InvestmentPlanConflictError("Investment plan status changed; reload and retry")
            if expected_updated_at and self._datetime_to_str(row.updated_at) != expected_updated_at:
                raise InvestmentPlanConflictError("Investment plan changed; reload and retry")
            row.status = status
            row.updated_at = datetime.now()
            session.flush()
            return self._plan_to_dict(row, self._list_steps_in_session(session, plan_id))

    def update_step_status(
        self,
        plan_id: int,
        step_id: int,
        status: str,
        *,
        expected_plan_status: str,
        expected_step_status: str,
        expected_updated_at: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        with self.write_session() as session:
            plan = session.execute(
                select(InvestmentPlan).where(InvestmentPlan.id == plan_id).limit(1)
            ).scalar_one_or_none()
            if plan is None:
                return None
            if plan.status != expected_plan_status or plan.status == "closed":
                raise InvestmentPlanConflictError("Investment plan status changed; reload and retry")
            if expected_updated_at and self._datetime_to_str(plan.updated_at) != expected_updated_at:
                raise InvestmentPlanConflictError("Investment plan changed; reload and retry")
            step = session.execute(
                select(InvestmentPlanStep).where(
                    and_(InvestmentPlanStep.id == step_id, InvestmentPlanStep.plan_id == plan_id)
                ).limit(1)
            ).scalar_one_or_none()
            if step is None:
                return None
            if step.status != expected_step_status:
                raise InvestmentPlanConflictError("Investment plan step changed; reload and retry")
            now = datetime.now()
            step.status = status
            step.notification_claim_token = None
            step.notification_claimed_at = None
            if status == "pending":
                step.triggered_at = None
                step.completed_at = None
                step.notified_at = None
            elif status in {"completed", "skipped"}:
                step.completed_at = now
            step.updated_at = now
            plan.updated_at = now
            session.flush()
            return self._plan_to_dict(plan, self._list_steps_in_session(session, plan_id))

    def apply_evaluation(
        self,
        plan_id: int,
        *,
        last_price: Optional[float],
        evaluation_status: str,
        evaluation_note: str,
        matched_step_ids: Iterable[int],
        evaluated_at: datetime,
        blocked_reasons: Sequence[str],
        expected_updated_at: str,
        expected_step_statuses: Dict[int, str],
        resolved_name: Optional[str] = None,
    ) -> Optional[Tuple[Dict[str, Any], List[int]]]:
        """Atomically persist evaluation state and return newly triggered step ids."""
        with self.write_session() as session:
            plan = session.execute(
                select(InvestmentPlan).where(InvestmentPlan.id == plan_id).limit(1)
            ).scalar_one_or_none()
            if plan is None or plan.status != "active":
                return None
            if self._datetime_to_str(plan.updated_at) != expected_updated_at:
                raise InvestmentPlanConflictError(
                    "Investment plan changed while it was being evaluated"
                )

            steps = self._list_steps_in_session(session, plan_id)
            current_step_statuses = {int(step.id): str(step.status) for step in steps}
            normalized_expected_statuses = {
                int(step_id): str(status)
                for step_id, status in expected_step_statuses.items()
            }
            if current_step_statuses != normalized_expected_statuses:
                raise InvestmentPlanConflictError(
                    "Investment plan steps changed while the plan was being evaluated"
                )

            matched_ids = {int(step_id) for step_id in matched_step_ids}
            newly_triggered: List[int] = []
            if matched_ids:
                for step in steps:
                    if int(step.id) not in matched_ids:
                        continue
                    step.status = "triggered"
                    step.triggered_at = evaluated_at
                    step.updated_at = evaluated_at
                    newly_triggered.append(int(step.id))

            plan.last_price = last_price
            plan.last_evaluated_at = evaluated_at
            plan.last_evaluation_status = evaluation_status
            plan.last_evaluation_note = evaluation_note or None
            plan.last_blocked_reasons = json.dumps(
                [str(reason) for reason in blocked_reasons],
                ensure_ascii=False,
            )
            if resolved_name and not plan.name:
                plan.name = resolved_name[:64]
            plan.updated_at = evaluated_at
            session.flush()
            payload = self._plan_to_dict(plan, self._list_steps_in_session(session, plan_id))
            return payload, sorted(newly_triggered)

    def claim_unnotified_triggered(
        self,
        *,
        claim_token: str,
        claimed_at: datetime,
        lease_seconds: int = 300,
    ) -> List[Dict[str, Any]]:
        """Atomically claim alert steps so concurrent workers cannot send duplicates."""
        lease_cutoff = claimed_at - timedelta(seconds=max(30, int(lease_seconds)))
        with self.write_session() as session:
            steps = session.execute(
                select(InvestmentPlanStep)
                .join(InvestmentPlan, InvestmentPlan.id == InvestmentPlanStep.plan_id)
                .where(
                    and_(
                        InvestmentPlan.status == "active",
                        InvestmentPlanStep.status == "triggered",
                        InvestmentPlanStep.notified_at.is_(None),
                        or_(
                            InvestmentPlanStep.notification_claim_token.is_(None),
                            InvestmentPlanStep.notification_claimed_at.is_(None),
                            InvestmentPlanStep.notification_claimed_at < lease_cutoff,
                        ),
                    )
                )
                .order_by(InvestmentPlanStep.plan_id.asc(), InvestmentPlanStep.id.asc())
            ).scalars().all()
            if not steps:
                return []
            plan_ids = sorted({int(step.plan_id) for step in steps})
            claimed_step_ids = {int(step.id) for step in steps}
            for step in steps:
                step.notification_claim_token = claim_token
                step.notification_claimed_at = claimed_at
                step.updated_at = claimed_at

            plans = session.execute(
                select(InvestmentPlan)
                .where(InvestmentPlan.id.in_(plan_ids))
                .order_by(InvestmentPlan.id.asc())
            ).scalars().all()
            results: List[Dict[str, Any]] = []
            for plan in plans:
                steps = self._list_steps_in_session(session, int(plan.id))
                payload = self._plan_to_dict(plan, steps)
                payload["steps"] = [
                    step for step in payload["steps"]
                    if int(step["id"]) in claimed_step_ids
                ]
                results.append(payload)
            return results

    def complete_notification_claim(
        self,
        step_ids: Iterable[int],
        *,
        claim_token: str,
        completed_at: datetime,
        sent: bool,
    ) -> None:
        ids = {int(step_id) for step_id in step_ids}
        if not ids:
            return
        with self.write_session() as session:
            steps = session.execute(
                select(InvestmentPlanStep).where(
                    and_(
                        InvestmentPlanStep.id.in_(ids),
                        InvestmentPlanStep.notification_claim_token == claim_token,
                    )
                )
            ).scalars().all()
            for step in steps:
                if sent and step.status == "triggered" and step.notified_at is None:
                    step.notified_at = completed_at
                step.notification_claim_token = None
                step.notification_claimed_at = None
                step.updated_at = completed_at

    @staticmethod
    def _find_open_plan_in_session(session: Any, *, symbol: str, account_id: Optional[int]):
        scope_condition = (
            InvestmentPlan.account_id.is_(None)
            if account_id is None
            else InvestmentPlan.account_id == account_id
        )
        return session.execute(
            select(InvestmentPlan).where(
                and_(
                    InvestmentPlan.symbol == symbol,
                    scope_condition,
                    InvestmentPlan.status != "closed",
                )
            ).limit(1)
        ).scalar_one_or_none()

    @staticmethod
    def _list_steps_in_session(session: Any, plan_id: int) -> List[InvestmentPlanStep]:
        return list(
            session.execute(
                select(InvestmentPlanStep)
                .where(InvestmentPlanStep.plan_id == plan_id)
                .order_by(InvestmentPlanStep.sort_order.asc(), InvestmentPlanStep.id.asc())
            ).scalars().all()
        )

    @classmethod
    def _plan_to_dict(cls, row: InvestmentPlan, steps: Sequence[InvestmentPlanStep]) -> Dict[str, Any]:
        return {
            "id": int(row.id),
            "account_id": int(row.account_id) if row.account_id is not None else None,
            "symbol": row.symbol,
            "market": row.market,
            "name": row.name,
            "strategy_type": row.strategy_type,
            "status": row.status,
            "thesis": row.thesis,
            "invalidation_note": row.invalidation_note,
            "benchmark_symbol": row.benchmark_symbol,
            "max_position_pct": row.max_position_pct,
            "required_cash_pct": row.required_cash_pct,
            "review_date": row.review_date.isoformat() if row.review_date else None,
            "last_price": row.last_price,
            "last_evaluated_at": row.last_evaluated_at.isoformat() if row.last_evaluated_at else None,
            "last_evaluation_status": row.last_evaluation_status,
            "last_evaluation_note": row.last_evaluation_note,
            "last_blocked_reasons": cls._load_string_list(row.last_blocked_reasons),
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            "steps": [cls._step_to_dict(step) for step in steps],
        }

    @staticmethod
    def _step_to_dict(row: InvestmentPlanStep) -> Dict[str, Any]:
        return {
            "id": int(row.id),
            "plan_id": int(row.plan_id),
            "action": row.action,
            "metric": row.metric,
            "operator": row.operator,
            "threshold": float(row.threshold),
            "upper_threshold": float(row.upper_threshold) if row.upper_threshold is not None else None,
            "target_position_pct": (
                float(row.target_position_pct) if row.target_position_pct is not None else None
            ),
            "note": row.note,
            "sort_order": int(row.sort_order or 0),
            "status": row.status,
            "triggered_at": row.triggered_at.isoformat() if row.triggered_at else None,
            "completed_at": row.completed_at.isoformat() if row.completed_at else None,
            "notified_at": row.notified_at.isoformat() if row.notified_at else None,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }

    @staticmethod
    def _datetime_to_str(value: Optional[datetime]) -> Optional[str]:
        return value.isoformat() if value else None

    @staticmethod
    def _load_string_list(value: Optional[str]) -> List[str]:
        if not value:
            return []
        try:
            parsed = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return []
        if not isinstance(parsed, list):
            return []
        return [str(item) for item in parsed if str(item).strip()]
