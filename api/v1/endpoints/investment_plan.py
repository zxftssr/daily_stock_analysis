# -*- coding: utf-8 -*-
"""Investment strategy plan endpoints."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query

from api.v1.schemas.common import ErrorResponse
from api.v1.schemas.investment_plan import (
    InvestmentPlanBatchEvaluationResponse,
    InvestmentPlanCreateRequest,
    InvestmentPlanEvaluationResponse,
    InvestmentPlanItem,
    InvestmentPlanListResponse,
    InvestmentPlanStatusRequest,
    InvestmentPlanStepStatusRequest,
    InvestmentPlanUpdateRequest,
)
from src.services.investment_plan_service import (
    InvestmentPlanBusyError,
    InvestmentPlanConflictError,
    InvestmentPlanNotFoundError,
    InvestmentPlanService,
    InvestmentPlanStateError,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def _send_pending_plan_notifications(plan_ids: list[int]) -> None:
    """Send queued plan notifications after the HTTP response has completed."""
    try:
        InvestmentPlanService().send_pending_notifications(plan_ids=plan_ids)
    except Exception as exc:
        logger.warning(
            "Send queued investment plan notifications failed plan_ids=%s: %s",
            plan_ids,
            exc,
            exc_info=True,
        )


def _raise_for_plan_error(exc: Exception, operation: str) -> None:
    if isinstance(exc, InvestmentPlanNotFoundError):
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": str(exc)},
        )
    if isinstance(exc, (InvestmentPlanConflictError, InvestmentPlanStateError, InvestmentPlanBusyError)):
        raise HTTPException(
            status_code=409,
            detail={"error": "conflict", "message": str(exc)},
        )
    if isinstance(exc, ValueError):
        raise HTTPException(
            status_code=400,
            detail={"error": "validation_error", "message": str(exc)},
        )
    logger.error("%s: %s", operation, exc, exc_info=True)
    raise HTTPException(
        status_code=500,
        detail={"error": "internal_error", "message": f"{operation} failed"},
    )


@router.post(
    "",
    response_model=InvestmentPlanItem,
    responses={400: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Create an investment strategy plan",
)
def create_plan(request: InvestmentPlanCreateRequest) -> InvestmentPlanItem:
    try:
        payload = request.model_dump()
        payload["steps"] = [step.model_dump() for step in request.steps]
        return InvestmentPlanItem(**InvestmentPlanService().create_plan(**payload))
    except Exception as exc:
        _raise_for_plan_error(exc, "Create investment plan")


@router.get(
    "",
    response_model=InvestmentPlanListResponse,
    responses={400: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="List investment strategy plans",
)
def list_plans(
    status: Optional[str] = Query(None),
    strategy_type: Optional[str] = Query(None),
    symbol: Optional[str] = Query(None),
    account_id: Optional[int] = Query(None, gt=0),
) -> InvestmentPlanListResponse:
    try:
        return InvestmentPlanListResponse(**InvestmentPlanService().list_plans(
            status=status,
            strategy_type=strategy_type,
            symbol=symbol,
            account_id=account_id,
        ))
    except Exception as exc:
        _raise_for_plan_error(exc, "List investment plans")


@router.post(
    "/evaluate-active",
    response_model=InvestmentPlanBatchEvaluationResponse,
    responses={500: {"model": ErrorResponse}},
    summary="Evaluate every active investment plan",
)
def evaluate_active_plans(
    background_tasks: BackgroundTasks,
    notify: bool = Query(False, description="Send alert-routed notifications for unnotified triggered steps"),
) -> InvestmentPlanBatchEvaluationResponse:
    try:
        payload = InvestmentPlanService().evaluate_active_plans(send_notification=False)
        evaluated_plan_ids = [int(item["plan"]["id"]) for item in payload["results"]]
        if notify and evaluated_plan_ids:
            background_tasks.add_task(
                _send_pending_plan_notifications,
                evaluated_plan_ids,
            )
            payload["notification"] = {
                "attempted": False,
                "sent": False,
                "queued": True,
                "step_count": int(payload["triggered"]),
            }
        return InvestmentPlanBatchEvaluationResponse(**payload)
    except Exception as exc:
        _raise_for_plan_error(exc, "Evaluate active investment plans")


@router.get(
    "/{plan_id}",
    response_model=InvestmentPlanItem,
    responses={404: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Get one investment strategy plan",
)
def get_plan(plan_id: int) -> InvestmentPlanItem:
    try:
        return InvestmentPlanItem(**InvestmentPlanService().get_plan(plan_id))
    except Exception as exc:
        _raise_for_plan_error(exc, "Get investment plan")


@router.put(
    "/{plan_id}",
    response_model=InvestmentPlanItem,
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
    summary="Update an investment strategy plan",
)
def update_plan(plan_id: int, request: InvestmentPlanUpdateRequest) -> InvestmentPlanItem:
    try:
        payload = request.model_dump(exclude_unset=True)
        steps = payload.pop("steps", None)
        return InvestmentPlanItem(**InvestmentPlanService().update_plan(
            plan_id,
            fields=payload,
            steps=steps,
        ))
    except Exception as exc:
        _raise_for_plan_error(exc, "Update investment plan")


@router.patch(
    "/{plan_id}/status",
    response_model=InvestmentPlanItem,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
    summary="Transition investment strategy plan status",
)
def set_plan_status(plan_id: int, request: InvestmentPlanStatusRequest) -> InvestmentPlanItem:
    try:
        return InvestmentPlanItem(**InvestmentPlanService().set_plan_status(plan_id, request.status))
    except Exception as exc:
        _raise_for_plan_error(exc, "Update investment plan status")


@router.patch(
    "/{plan_id}/steps/{step_id}",
    response_model=InvestmentPlanItem,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
    summary="Update one investment strategy plan step status",
)
def set_step_status(
    plan_id: int,
    step_id: int,
    request: InvestmentPlanStepStatusRequest,
) -> InvestmentPlanItem:
    try:
        return InvestmentPlanItem(**InvestmentPlanService().set_step_status(plan_id, step_id, request.status))
    except Exception as exc:
        _raise_for_plan_error(exc, "Update investment plan step")


@router.post(
    "/{plan_id}/evaluate",
    response_model=InvestmentPlanEvaluationResponse,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Evaluate one active investment strategy plan",
)
def evaluate_plan(
    plan_id: int,
    background_tasks: BackgroundTasks,
    notify: bool = Query(False, description="Send a notification when this check newly triggers a step"),
) -> InvestmentPlanEvaluationResponse:
    try:
        payload = InvestmentPlanService().evaluate_plan(plan_id, send_notification=False)
        if notify:
            background_tasks.add_task(
                _send_pending_plan_notifications,
                [plan_id],
            )
            payload["notification"] = {
                "attempted": False,
                "sent": False,
                "queued": True,
                "step_count": len(payload["newly_triggered_step_ids"]),
            }
        return InvestmentPlanEvaluationResponse(**payload)
    except Exception as exc:
        _raise_for_plan_error(exc, "Evaluate investment plan")
