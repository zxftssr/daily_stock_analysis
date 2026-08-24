# -*- coding: utf-8 -*-
"""Daily workflow bridge tests for investment plan evaluation."""

from __future__ import annotations

from unittest.mock import MagicMock, patch
from types import SimpleNamespace

import pytest

import main


def test_daily_plan_bridge_reuses_notifier_and_notification_flag() -> None:
    notifier = MagicMock()
    service = MagicMock()
    service.evaluate_active_plans.return_value = {
        "evaluated": 1,
        "triggered": 1,
        "errors": [],
        "notification": {"sent": True},
    }

    with patch(
        "src.services.investment_plan_service.InvestmentPlanService",
        return_value=service,
    ) as service_cls:
        result = main._evaluate_investment_plans(
            notifier=notifier,
            send_notification=True,
            markets=None,
        )

    service_cls.assert_called_once_with(notifier=notifier)
    service.evaluate_active_plans.assert_called_once_with(send_notification=True, markets=None)
    assert result["triggered"] == 1


def test_daily_run_still_checks_open_plan_markets_when_watchlist_markets_are_closed() -> None:
    config = MagicMock()
    config.trading_day_check_enabled = True
    args = SimpleNamespace(
        dry_run=False,
        no_notify=False,
        force_run=False,
    )

    with (
        patch.object(main, "_compute_trading_day_filter", return_value=([], "", True)),
        patch.object(main, "_get_plan_evaluation_markets", return_value={"us"}),
        patch.object(main, "_evaluate_investment_plans") as evaluate,
    ):
        main.run_full_analysis(config, args, stock_codes=[])

    evaluate.assert_called_once_with(
        notifier=None,
        send_notification=True,
        markets={"us"},
    )


def test_daily_run_checks_plans_even_when_stock_pipeline_fails() -> None:
    config = MagicMock()
    config.trading_day_check_enabled = True
    config.market_review_enabled = False
    config.single_stock_notify = False
    args = SimpleNamespace(
        dry_run=False,
        no_notify=False,
        force_run=False,
        single_notify=False,
        no_context_snapshot=False,
        workers=1,
        no_market_review=True,
    )
    notifier = MagicMock()

    with (
        patch.object(main, "_compute_trading_day_filter", return_value=(["600519"], "cn", False)),
        patch.object(main, "_get_plan_evaluation_markets", return_value={"cn"}),
        patch.object(main, "_evaluate_investment_plans") as evaluate,
        patch("src.core.pipeline.StockAnalysisPipeline") as pipeline_cls,
    ):
        pipeline_cls.return_value.notifier = notifier
        pipeline_cls.return_value.run.side_effect = RuntimeError("analysis failed")
        main.run_full_analysis(config, args, stock_codes=["600519"])

    evaluate.assert_called_once_with(
        notifier=notifier,
        send_notification=True,
        markets={"cn"},
    )


def test_import_failure_still_checks_plans_before_propagating() -> None:
    config = MagicMock()
    config.trading_day_check_enabled = True
    args = SimpleNamespace(dry_run=False, no_notify=False, force_run=False)

    with (
        patch.object(main, "_get_plan_evaluation_markets", return_value={"us"}),
        patch.object(main, "_evaluate_investment_plans") as evaluate,
        patch.dict("sys.modules", {"src.core.pipeline": None}),
        pytest.raises(ModuleNotFoundError),
    ):
        main.run_full_analysis(config, args, stock_codes=[])

    evaluate.assert_called_once_with(
        notifier=None,
        send_notification=True,
        markets={"us"},
    )
