# -*- coding: utf-8 -*-
"""Investment strategy plan service tests."""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

from src.config import Config
from src.services.investment_plan_service import (
    InvestmentPlanConflictError,
    InvestmentPlanService,
    InvestmentPlanStateError,
)
from src.services.portfolio_service import PortfolioService
from src.services.stock_service import StockService
from src.storage import DatabaseManager


class _StockServiceStub:
    def __init__(
        self,
        *,
        price=95.0,
        price_date=None,
        include_price_date=True,
        history=None,
        history_meta=None,
    ):
        self.price = price
        self.price_date = price_date or date.today().isoformat()
        self.include_price_date = include_price_date
        self.history = history or []
        self.history_meta = history_meta or {}

    def get_realtime_quote(self, symbol):
        if self.price is None:
            return None
        payload = {
            "stock_code": symbol,
            "stock_name": "测试股票",
            "current_price": self.price,
            "source": "unit-test",
        }
        if self.include_price_date:
            payload["price_date"] = self.price_date
        return payload

    def get_history_data(self, symbol, period="daily", days=400):
        return {
            "stock_code": symbol,
            "period": period,
            "data": list(self.history),
            "as_of_date": self.history[-1].get("date") if self.history else None,
            **self.history_meta,
        }


class _NotifierStub:
    def __init__(self, *, send_result=True):
        self.send_result = send_result
        self.messages = []

    def is_available(self):
        return True

    def send(self, content, route_type=None, channel_values=None):
        self.messages.append((content, route_type, channel_values))
        return self.send_result


class _BlockingNotifier(_NotifierStub):
    def __init__(self):
        super().__init__(send_result=True)
        self.started = threading.Event()
        self.release = threading.Event()

    def send(self, content, route_type=None, channel_values=None):
        self.messages.append((content, route_type, channel_values))
        self.started.set()
        self.release.wait(timeout=5)
        return True


class InvestmentPlanServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "investment_plans.db"
        self.env_path = Path(self.temp_dir.name) / ".env"
        self.env_path.write_text(
            "\n".join([
                "STOCK_LIST=600519",
                "GEMINI_API_KEY=test",
                "ADMIN_AUTH_ENABLED=false",
                f"DATABASE_PATH={self.db_path}",
            ]) + "\n",
            encoding="utf-8",
        )
        os.environ["ENV_FILE"] = str(self.env_path)
        os.environ["DATABASE_PATH"] = str(self.db_path)
        Config.reset_instance()
        DatabaseManager.reset_instance()
        self.db = DatabaseManager.get_instance()
        self.portfolio = PortfolioService()

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        os.environ.pop("ENV_FILE", None)
        os.environ.pop("DATABASE_PATH", None)
        self.temp_dir.cleanup()

    @staticmethod
    def _step(**overrides):
        payload = {
            "action": "buy",
            "metric": "price",
            "operator": "lte",
            "threshold": 100,
            "target_position_pct": 10,
        }
        payload.update(overrides)
        return payload

    def _create_active(self, service: InvestmentPlanService, **overrides):
        payload = {
            "symbol": "600519",
            "market": "cn",
            "strategy_type": "value",
            "status": "active",
            "thesis": "现金流稳定且估值具备安全边际",
            "invalidation_note": "盈利能力持续恶化",
            "steps": [self._step()],
        }
        payload.update(overrides)
        return service.create_plan(**payload)

    def test_price_step_triggers_once_and_notification_is_deduplicated(self) -> None:
        notifier = _NotifierStub()
        service = InvestmentPlanService(
            stock_service=_StockServiceStub(price=95),
            portfolio_service=self.portfolio,
            notifier=notifier,
        )
        plan = self._create_active(service)

        first = service.evaluate_plan(plan["id"])
        self.assertEqual(first["newly_triggered_step_ids"], [plan["steps"][0]["id"]])
        self.assertEqual(first["plan"]["last_evaluation_status"], "triggered")

        second = service.evaluate_plan(plan["id"])
        self.assertEqual(second["newly_triggered_step_ids"], [])
        self.assertEqual(second["plan"]["steps"][0]["status"], "triggered")
        self.assertEqual(second["plan"]["last_evaluation_status"], "triggered")

        sent = service.send_pending_notifications()
        self.assertTrue(sent["sent"])
        self.assertEqual(sent["step_count"], 1)
        self.assertEqual(notifier.messages[0][1], "alert")
        self.assertIn("系统不会自动下单", notifier.messages[0][0])

        duplicate = service.send_pending_notifications()
        self.assertFalse(duplicate["attempted"])
        self.assertEqual(len(notifier.messages), 1)

    def test_manual_check_sends_to_selected_channel_when_enabled(self) -> None:
        notifier = _NotifierStub()
        service = InvestmentPlanService(
            stock_service=_StockServiceStub(price=95),
            portfolio_service=self.portfolio,
            notifier=notifier,
        )
        plan = self._create_active(
            service,
            notification_channels=["ntfy"],
            check_frequency="manual",
        )

        result = service.evaluate_plan(plan["id"], send_notification=True)

        self.assertTrue(result["notification"]["sent"])
        self.assertEqual(notifier.messages[0][2], ["ntfy"])
        self.assertEqual(result["plan"]["check_frequency"], "manual")

    def test_notification_disabled_plan_keeps_trigger_unnotified(self) -> None:
        notifier = _NotifierStub()
        service = InvestmentPlanService(
            stock_service=_StockServiceStub(price=95),
            portfolio_service=self.portfolio,
            notifier=notifier,
        )
        plan = self._create_active(service, notify_on_trigger=False)
        service.evaluate_plan(plan["id"])

        result = service.send_pending_notifications()

        self.assertFalse(result["attempted"])
        self.assertEqual(notifier.messages, [])
        self.assertIsNone(service.get_plan(plan["id"])["steps"][0]["notified_at"])

    def test_plan_rejects_more_than_one_notification_channel(self) -> None:
        service = InvestmentPlanService(
            stock_service=_StockServiceStub(price=95),
            portfolio_service=self.portfolio,
        )

        with self.assertRaisesRegex(ValueError, "at most one"):
            self._create_active(
                service,
                notification_channels=["wechat", "custom"],
            )

    def test_failed_notification_remains_pending_for_retry(self) -> None:
        notifier = _NotifierStub(send_result=False)
        service = InvestmentPlanService(
            stock_service=_StockServiceStub(price=95),
            portfolio_service=self.portfolio,
            notifier=notifier,
        )
        plan = self._create_active(service)
        service.evaluate_plan(plan["id"])

        first = service.send_pending_notifications()
        second = service.send_pending_notifications()
        self.assertFalse(first["sent"])
        self.assertFalse(second["sent"])
        self.assertEqual(len(notifier.messages), 2)

    def test_missing_price_never_triggers_pending_step(self) -> None:
        service = InvestmentPlanService(
            stock_service=_StockServiceStub(price=None),
            portfolio_service=self.portfolio,
        )
        plan = self._create_active(service)

        result = service.evaluate_plan(plan["id"])
        self.assertEqual(result["plan"]["last_evaluation_status"], "data_missing")
        self.assertEqual(result["plan"]["steps"][0]["status"], "pending")
        self.assertIn("最新价格不可用", result["errors"])

    def test_missing_realtime_quote_uses_fresh_history_close(self) -> None:
        history = [{
            "date": date.today().isoformat(),
            "open": 95,
            "high": 96,
            "low": 94,
            "close": 95,
        }]
        service = InvestmentPlanService(
            stock_service=_StockServiceStub(price=None, history=history),
            portfolio_service=self.portfolio,
        )
        plan = self._create_active(service)

        result = service.evaluate_plan(plan["id"])
        self.assertEqual(result["metric_values"]["price"], 95)
        self.assertEqual(result["plan"]["steps"][0]["status"], "triggered")

    def test_positive_but_stale_price_never_triggers_pending_step(self) -> None:
        service = InvestmentPlanService(
            stock_service=_StockServiceStub(price=95, price_date="2020-01-02"),
            portfolio_service=self.portfolio,
        )
        plan = self._create_active(service)

        result = service.evaluate_plan(plan["id"])
        self.assertEqual(result["plan"]["last_evaluation_status"], "data_missing")
        self.assertEqual(result["plan"]["steps"][0]["status"], "pending")
        self.assertIn("最新价格不可用", result["errors"])

    def test_undated_quote_uses_validated_latest_history_close(self) -> None:
        history = [{
            "date": date.today().isoformat(),
            "open": 120,
            "high": 121,
            "low": 119,
            "close": 120,
        }]
        service = InvestmentPlanService(
            stock_service=_StockServiceStub(
                price=95,
                include_price_date=False,
                history=history,
            ),
            portfolio_service=self.portfolio,
        )
        plan = self._create_active(service)

        result = service.evaluate_plan(plan["id"])
        self.assertEqual(result["metric_values"]["price"], 120)
        self.assertEqual(result["plan"]["last_evaluation_status"], "waiting")
        self.assertEqual(result["plan"]["steps"][0]["status"], "pending")

    def test_benchmark_drawdown_step_uses_last_250_bars(self) -> None:
        history = [
            {"date": f"2026-{(index // 28) + 1:02d}-{(index % 28) + 1:02d}", "high": 100, "close": 100}
            for index in range(249)
        ]
        history.append({"date": "2026-12-31", "high": 90, "close": 80})
        service = InvestmentPlanService(
            stock_service=_StockServiceStub(price=8, history=history),
            portfolio_service=self.portfolio,
        )
        plan = self._create_active(
            service,
            symbol="510300",
            strategy_type="index_crash",
            benchmark_symbol="000300",
            steps=[self._step(
                metric="benchmark_drawdown_250d_pct",
                operator="gte",
                threshold=20,
            )],
        )

        result = service.evaluate_plan(plan["id"])
        self.assertEqual(result["metric_values"]["benchmark_drawdown_250d_pct"], 20.0)
        self.assertEqual(result["plan"]["steps"][0]["status"], "triggered")

    def test_default_index_window_reaches_250_bars_through_real_history_loader(self) -> None:
        dates = pd.bdate_range(end=date.today(), periods=260)
        history_df = pd.DataFrame({
            "date": dates,
            "open": [100.0] * 260,
            "high": [100.0] * 260,
            "low": [80.0] * 260,
            "close": [100.0] * 255 + [80.0] * 5,
            "volume": [1000.0] * 260,
        })
        db_stub = MagicMock()
        db_stub.get_data_range.return_value = []
        fetcher_stub = MagicMock()
        fetcher_stub.get_daily_data.return_value = (history_df, "integration-test")
        stock_service = StockService()
        stock_service.get_realtime_quote = MagicMock(return_value={
            "stock_code": "510300",
            "stock_name": "沪深300ETF",
            "current_price": 4.0,
        })
        service = InvestmentPlanService(
            stock_service=stock_service,
            portfolio_service=self.portfolio,
        )
        plan = self._create_active(
            service,
            symbol="510300",
            strategy_type="index_crash",
            benchmark_symbol="000300",
            steps=[self._step(
                metric="benchmark_drawdown_250d_pct",
                operator="gte",
                threshold=20,
            )],
        )

        with (
            patch("src.storage.get_db", return_value=db_stub),
            patch("src.services.history_loader._get_fetcher_manager", return_value=fetcher_stub),
        ):
            result = service.evaluate_plan(plan["id"])

        self.assertEqual(result["metric_values"]["benchmark_drawdown_250d_pct"], 20.0)
        self.assertEqual(result["plan"]["steps"][0]["status"], "triggered")
        self.assertEqual(fetcher_stub.get_daily_data.call_args.kwargs["days"], 550)

    def test_stale_or_partial_benchmark_history_never_triggers(self) -> None:
        history = [
            {"date": f"2026-{(index // 28) + 1:02d}-{(index % 28) + 1:02d}", "high": 100, "close": 80}
            for index in range(250)
        ]
        for metadata in ({"stale": True}, {"partial_cache": True}):
            with self.subTest(metadata=metadata):
                service = InvestmentPlanService(
                    stock_service=_StockServiceStub(price=8, history=history, history_meta=metadata),
                    portfolio_service=self.portfolio,
                )
                plan = service.create_plan(
                    symbol="510300",
                    market="cn",
                    strategy_type="index_crash",
                    status="active",
                    thesis="指数大幅回撤后分批布局",
                    invalidation_note="基准或产品契约发生变化",
                    benchmark_symbol="000300",
                    steps=[self._step(
                        metric="benchmark_drawdown_250d_pct",
                        operator="gte",
                        threshold=20,
                    )],
                )
                result = service.evaluate_plan(plan["id"])
                self.assertEqual(result["plan"]["last_evaluation_status"], "data_missing")
                self.assertEqual(result["plan"]["steps"][0]["status"], "pending")
                service.set_plan_status(plan["id"], "closed")

    def test_outdated_benchmark_as_of_date_never_triggers(self) -> None:
        history = [
            {"date": f"2020-{(index // 28) + 1:02d}-{(index % 28) + 1:02d}", "high": 100, "close": 80}
            for index in range(250)
        ]
        service = InvestmentPlanService(
            stock_service=_StockServiceStub(price=8, history=history),
            portfolio_service=self.portfolio,
        )
        plan = self._create_active(
            service,
            symbol="510300",
            strategy_type="index_crash",
            benchmark_symbol="000300",
            steps=[self._step(
                metric="benchmark_drawdown_250d_pct",
                operator="gte",
                threshold=20,
            )],
        )

        result = service.evaluate_plan(plan["id"])
        self.assertEqual(result["plan"]["last_evaluation_status"], "data_missing")
        self.assertEqual(result["plan"]["steps"][0]["status"], "pending")

    def test_account_cash_floor_blocks_new_buy_but_records_trigger(self) -> None:
        account = self.portfolio.create_account(
            name="Main", broker="Demo", market="cn", base_currency="CNY"
        )
        constrained_portfolio = MagicMock(wraps=self.portfolio)
        constrained_portfolio.get_portfolio_snapshot.return_value = {
            "accounts": [{
                "total_equity": 100000,
                "total_cash": 10000,
                "positions": [],
            }]
        }
        service = InvestmentPlanService(
            stock_service=_StockServiceStub(price=95),
            portfolio_service=constrained_portfolio,
        )
        plan = self._create_active(
            service,
            account_id=account["id"],
            required_cash_pct=20,
            max_position_pct=20,
        )

        result = service.evaluate_plan(plan["id"])
        self.assertEqual(result["plan"]["last_evaluation_status"], "blocked")
        self.assertIn("已触及现金底线", result["blocked_reasons"])
        self.assertEqual(result["plan"]["steps"][0]["status"], "triggered")

    def test_target_position_projection_cannot_breach_cash_floor(self) -> None:
        account = self.portfolio.create_account(
            name="Main", broker="Demo", market="cn", base_currency="CNY"
        )
        constrained_portfolio = MagicMock(wraps=self.portfolio)
        constrained_portfolio.get_portfolio_snapshot.return_value = {
            "accounts": [{
                "total_equity": 100000,
                "total_cash": 30000,
                "positions": [],
            }]
        }
        service = InvestmentPlanService(
            stock_service=_StockServiceStub(price=95),
            portfolio_service=constrained_portfolio,
        )
        plan = self._create_active(
            service,
            account_id=account["id"],
            required_cash_pct=25,
            max_position_pct=30,
            steps=[self._step(target_position_pct=20)],
        )

        result = service.evaluate_plan(plan["id"])
        self.assertEqual(result["plan"]["last_evaluation_status"], "blocked")
        self.assertIn("执行目标仓位后将低于现金底线", result["blocked_reasons"])

    def test_reached_buy_target_is_suppressed_without_hiding_higher_target(self) -> None:
        account = self.portfolio.create_account(
            name="Main", broker="Demo", market="cn", base_currency="CNY"
        )
        constrained_portfolio = MagicMock(wraps=self.portfolio)
        constrained_portfolio.get_portfolio_snapshot.return_value = {
            "accounts": [{
                "total_equity": 100000,
                "total_cash": 50000,
                "positions": [{
                    "symbol": "600519",
                    "market_value_base": 15000,
                    "price_available": True,
                    "price_stale": False,
                }],
            }]
        }
        service = InvestmentPlanService(
            stock_service=_StockServiceStub(price=95),
            portfolio_service=constrained_portfolio,
        )
        plan = self._create_active(
            service,
            account_id=account["id"],
            required_cash_pct=25,
            max_position_pct=30,
            steps=[
                self._step(target_position_pct=10),
                self._step(action="add", target_position_pct=20),
            ],
        )

        result = service.evaluate_plan(plan["id"])
        low_step, high_step = result["plan"]["steps"]
        self.assertEqual(result["newly_triggered_step_ids"], [high_step["id"]])
        self.assertEqual(low_step["status"], "pending")
        self.assertEqual(high_step["status"], "triggered")
        self.assertIn("已达到目标仓位", result["plan"]["last_evaluation_note"])

    def test_unreliable_portfolio_valuation_blocks_bound_buy_constraints(self) -> None:
        account = self.portfolio.create_account(
            name="Main", broker="Demo", market="cn", base_currency="CNY"
        )
        constrained_portfolio = MagicMock(wraps=self.portfolio)
        constrained_portfolio.get_portfolio_snapshot.return_value = {
            "accounts": [{
                "total_equity": 100000,
                "total_cash": 50000,
                "positions": [{
                    "symbol": "600519",
                    "market_value_base": 50000,
                    "price_available": False,
                }],
            }]
        }
        service = InvestmentPlanService(
            stock_service=_StockServiceStub(price=95),
            portfolio_service=constrained_portfolio,
        )
        plan = self._create_active(
            service,
            account_id=account["id"],
            required_cash_pct=20,
            max_position_pct=60,
        )

        result = service.evaluate_plan(plan["id"])
        self.assertEqual(
            result["constraints"],
            {"position_pct": None, "cash_pct": None, "reliable": False},
        )
        self.assertEqual(result["plan"]["last_evaluation_status"], "blocked")
        self.assertIn("账户估值数据不可用", result["blocked_reasons"])
        self.assertIn("当前仓位比例不可用", result["blocked_reasons"])
        self.assertIn("现金比例不可用", result["blocked_reasons"])

    def test_unreliable_bound_account_blocks_buy_without_optional_limits(self) -> None:
        account = self.portfolio.create_account(
            name="Main", broker="Demo", market="cn", base_currency="CNY"
        )
        constrained_portfolio = MagicMock(wraps=self.portfolio)
        constrained_portfolio.get_portfolio_snapshot.return_value = {
            "accounts": [],
        }
        service = InvestmentPlanService(
            stock_service=_StockServiceStub(price=95),
            portfolio_service=constrained_portfolio,
        )
        plan = self._create_active(
            service,
            account_id=account["id"],
            max_position_pct=None,
            required_cash_pct=None,
        )

        result = service.evaluate_plan(plan["id"])
        self.assertEqual(result["plan"]["last_evaluation_status"], "blocked")
        self.assertIn("账户估值数据不可用", result["blocked_reasons"])

    def test_previous_completed_us_close_is_reliable_before_market_open(self) -> None:
        completed_session = date(2026, 8, 21)
        account = self.portfolio.create_account(
            name="US", broker="Demo", market="us", base_currency="USD"
        )
        constrained_portfolio = MagicMock(wraps=self.portfolio)
        constrained_portfolio.get_portfolio_snapshot.return_value = {
            "accounts": [{
                "total_equity": 100000,
                "total_cash": 50000,
                "positions": [{
                    "symbol": "AAPL",
                    "market": "us",
                    "market_value_base": 10000,
                    "price_available": True,
                    "price_stale": True,
                    "price_date": completed_session.isoformat(),
                }],
            }]
        }
        service = InvestmentPlanService(
            stock_service=_StockServiceStub(price=95),
            portfolio_service=constrained_portfolio,
        )
        plan = self._create_active(
            service,
            symbol="AAPL",
            market="us",
            account_id=account["id"],
            required_cash_pct=25,
            max_position_pct=30,
            steps=[self._step(target_position_pct=20)],
        )

        with patch(
            "src.core.trading_calendar.get_effective_trading_date",
            return_value=completed_session,
        ) as effective_date:
            result = service.evaluate_plan(plan["id"])

        self.assertTrue(result["constraints"]["reliable"])
        self.assertEqual(result["constraints"]["position_pct"], 10.0)
        self.assertEqual(result["plan"]["last_evaluation_status"], "triggered")
        self.assertTrue(all(item.args == ("us",) for item in effective_date.call_args_list))

    def test_exchange_qualified_cn_position_counts_toward_plan_limit(self) -> None:
        account = self.portfolio.create_account(
            name="Main", broker="Demo", market="cn", base_currency="CNY"
        )
        constrained_portfolio = MagicMock(wraps=self.portfolio)
        constrained_portfolio.get_portfolio_snapshot.return_value = {
            "accounts": [{
                "total_equity": 100000,
                "total_cash": 20000,
                "positions": [{
                    "symbol": "SH600519",
                    "market_value_base": 80000,
                    "price_available": True,
                    "price_stale": False,
                }],
            }]
        }
        service = InvestmentPlanService(
            stock_service=_StockServiceStub(price=95),
            portfolio_service=constrained_portfolio,
        )
        plan = self._create_active(
            service,
            symbol="SH600519",
            account_id=account["id"],
            max_position_pct=50,
            required_cash_pct=10,
            steps=[self._step(target_position_pct=None)],
        )

        result = service.evaluate_plan(plan["id"])
        self.assertEqual(result["plan"]["symbol"], "600519")
        self.assertEqual(result["constraints"]["position_pct"], 80.0)
        self.assertIn("已达到最大仓位", result["blocked_reasons"])

    def test_hk_and_us_position_variants_count_toward_plan_limit(self) -> None:
        scenarios = [
            ("hk", "HK00700", "00700", "HKD"),
            ("us", "AAPL", "AAPL.US", "USD"),
        ]
        for market, plan_symbol, position_symbol, currency in scenarios:
            with self.subTest(market=market):
                account = self.portfolio.create_account(
                    name=f"{market}-account",
                    broker="Demo",
                    market=market,
                    base_currency=currency,
                )
                constrained_portfolio = MagicMock(wraps=self.portfolio)
                constrained_portfolio.get_portfolio_snapshot.return_value = {
                    "accounts": [{
                        "total_equity": 100000,
                        "total_cash": 20000,
                        "positions": [{
                            "symbol": position_symbol,
                            "market_value_base": 80000,
                            "price_available": True,
                            "price_stale": False,
                        }],
                    }]
                }
                service = InvestmentPlanService(
                    stock_service=_StockServiceStub(price=95),
                    portfolio_service=constrained_portfolio,
                )
                plan = self._create_active(
                    service,
                    symbol=plan_symbol,
                    market=market,
                    account_id=account["id"],
                    max_position_pct=50,
                    required_cash_pct=10,
                    steps=[self._step(target_position_pct=None)],
                )

                result = service.evaluate_plan(plan["id"])
                self.assertEqual(result["constraints"]["position_pct"], 80.0)
                self.assertIn("已达到最大仓位", result["blocked_reasons"])
                service.set_plan_status(plan["id"], "closed")

    def test_exit_trigger_takes_priority_over_buy_constraint_block(self) -> None:
        account = self.portfolio.create_account(
            name="Main", broker="Demo", market="cn", base_currency="CNY"
        )
        constrained_portfolio = MagicMock(wraps=self.portfolio)
        constrained_portfolio.get_portfolio_snapshot.return_value = {
            "accounts": [{"total_equity": 100000, "total_cash": 0, "positions": []}]
        }
        notifier = _NotifierStub()
        service = InvestmentPlanService(
            stock_service=_StockServiceStub(price=90),
            portfolio_service=constrained_portfolio,
            notifier=notifier,
        )
        plan = self._create_active(
            service,
            account_id=account["id"],
            required_cash_pct=20,
            steps=[
                self._step(action="buy", threshold=100),
                self._step(action="exit", threshold=95, target_position_pct=None),
            ],
        )

        result = service.evaluate_plan(plan["id"])
        self.assertEqual(result["plan"]["last_evaluation_status"], "triggered")
        self.assertIn("已触及现金底线", result["blocked_reasons"])
        sent = service.send_pending_notifications()
        self.assertTrue(sent["sent"])
        self.assertIn("买入纪律：受限", notifier.messages[0][0])
        self.assertIn("已触及现金底线", notifier.messages[0][0])

    def test_active_steps_require_pause_before_replacement(self) -> None:
        service = InvestmentPlanService(
            stock_service=_StockServiceStub(price=95),
            portfolio_service=self.portfolio,
        )
        plan = self._create_active(service)
        with self.assertRaises(InvestmentPlanStateError):
            service.update_plan(plan["id"], fields={}, steps=[self._step(threshold=80)])

        paused = service.set_plan_status(plan["id"], "paused")
        self.assertEqual(paused["status"], "paused")
        updated = service.update_plan(plan["id"], fields={}, steps=[self._step(threshold=80)])
        self.assertEqual(updated["steps"][0]["threshold"], 80)

    def test_paused_plan_can_replace_steps_only_after_triggered_step_is_reset(self) -> None:
        service = InvestmentPlanService(
            stock_service=_StockServiceStub(price=95),
            portfolio_service=self.portfolio,
        )
        plan = self._create_active(service)
        triggered = service.evaluate_plan(plan["id"])["plan"]
        service.set_plan_status(plan["id"], "paused")

        with self.assertRaisesRegex(InvestmentPlanStateError, "all pending"):
            service.update_plan(plan["id"], fields={}, steps=[self._step(threshold=80)])

        service.set_step_status(plan["id"], triggered["steps"][0]["id"], "pending")
        updated = service.update_plan(plan["id"], fields={}, steps=[self._step(threshold=80)])
        self.assertEqual(updated["steps"][0]["status"], "pending")

    def test_completed_or_skipped_steps_are_immutable_execution_history(self) -> None:
        service = InvestmentPlanService(
            stock_service=_StockServiceStub(price=95),
            portfolio_service=self.portfolio,
        )
        completed_plan = self._create_active(service)
        triggered = service.evaluate_plan(completed_plan["id"])["plan"]
        service.set_plan_status(completed_plan["id"], "paused")
        completed = service.set_step_status(
            completed_plan["id"], triggered["steps"][0]["id"], "completed"
        )
        with self.assertRaisesRegex(InvestmentPlanStateError, "all pending"):
            service.update_plan(completed_plan["id"], fields={}, steps=[self._step(threshold=80)])
        with self.assertRaises(InvestmentPlanStateError):
            service.set_step_status(
                completed_plan["id"], completed["steps"][0]["id"], "pending"
            )

        service.set_plan_status(completed_plan["id"], "closed")
        skipped_plan = self._create_active(service)
        skipped_triggered = service.evaluate_plan(skipped_plan["id"])["plan"]
        service.set_plan_status(skipped_plan["id"], "paused")
        skipped = service.set_step_status(
            skipped_plan["id"], skipped_triggered["steps"][0]["id"], "skipped"
        )
        with self.assertRaisesRegex(InvestmentPlanStateError, "all pending"):
            service.update_plan(skipped_plan["id"], fields={}, steps=[self._step(threshold=80)])
        with self.assertRaises(InvestmentPlanStateError):
            service.set_step_status(skipped_plan["id"], skipped["steps"][0]["id"], "pending")

    def test_pending_steps_cannot_be_skipped(self) -> None:
        service = InvestmentPlanService(
            stock_service=_StockServiceStub(price=120),
            portfolio_service=self.portfolio,
        )
        plan = self._create_active(service)
        with self.assertRaises(InvestmentPlanStateError):
            service.set_step_status(plan["id"], plan["steps"][0]["id"], "skipped")

    def test_closed_plan_rejects_step_mutation_and_stale_step_transition(self) -> None:
        service = InvestmentPlanService(
            stock_service=_StockServiceStub(price=95),
            portfolio_service=self.portfolio,
        )
        plan = self._create_active(service)
        triggered = service.evaluate_plan(plan["id"])["plan"]
        step_id = triggered["steps"][0]["id"]
        service.set_plan_status(plan["id"], "closed")
        with self.assertRaisesRegex(InvestmentPlanStateError, "Closed plans"):
            service.set_step_status(plan["id"], step_id, "completed")

        replacement = self._create_active(service)
        replacement = service.evaluate_plan(replacement["id"])["plan"]
        replacement_step_id = replacement["steps"][0]["id"]
        service.repo.update_step_status(
            replacement["id"],
            replacement_step_id,
            "completed",
            expected_plan_status="active",
            expected_step_status="triggered",
            expected_updated_at=replacement["updated_at"],
        )
        with self.assertRaises(InvestmentPlanConflictError):
            service.repo.update_step_status(
                replacement["id"],
                replacement_step_id,
                "pending",
                expected_plan_status="active",
                expected_step_status="triggered",
                expected_updated_at=replacement["updated_at"],
            )

    def test_market_and_symbol_must_match(self) -> None:
        service = InvestmentPlanService(
            stock_service=_StockServiceStub(price=95),
            portfolio_service=self.portfolio,
        )
        with self.assertRaisesRegex(ValueError, "US symbols"):
            self._create_active(service, symbol="600519", market="us")
        with self.assertRaisesRegex(ValueError, "Hong Kong symbols"):
            self._create_active(service, symbol="600519", market="hk")

    def test_cn_symbol_identity_preserves_exchange_ambiguity(self) -> None:
        self.assertTrue(InvestmentPlanService._same_symbol("SH600519", "600519"))
        self.assertFalse(InvestmentPlanService._same_symbol("SH000001", "SZ000001"))

    def test_exchange_qualified_cn_freshness_uses_cn_completed_session(self) -> None:
        completed_session = date(2026, 8, 21)
        price_service = InvestmentPlanService(
            stock_service=_StockServiceStub(
                price=95,
                price_date=completed_session.isoformat(),
            ),
            portfolio_service=self.portfolio,
        )
        price_plan = self._create_active(
            price_service,
            symbol="SH000001",
        )

        with patch(
            "src.core.trading_calendar.get_effective_trading_date",
            return_value=completed_session,
        ) as effective_date:
            price_result = price_service.evaluate_plan(price_plan["id"])

        self.assertEqual(price_plan["symbol"], "000001.SH")
        self.assertEqual(price_result["plan"]["steps"][0]["status"], "triggered")
        effective_date.assert_called_once_with("cn")

        history = [
            {
                "date": completed_session.isoformat(),
                "high": 100,
                "close": 80,
            }
            for _ in range(250)
        ]
        drawdown_service = InvestmentPlanService(
            stock_service=_StockServiceStub(
                price=8,
                price_date=completed_session.isoformat(),
                history=history,
            ),
            portfolio_service=self.portfolio,
        )
        drawdown_plan = self._create_active(
            drawdown_service,
            symbol="510300",
            strategy_type="index_crash",
            benchmark_symbol="000001.SH",
            steps=[self._step(
                metric="benchmark_drawdown_250d_pct",
                operator="gte",
                threshold=20,
            )],
        )

        with patch(
            "src.core.trading_calendar.get_effective_trading_date",
            return_value=completed_session,
        ) as effective_date:
            drawdown_result = drawdown_service.evaluate_plan(drawdown_plan["id"])

        self.assertEqual(
            drawdown_result["plan"]["steps"][0]["status"],
            "triggered",
        )
        self.assertTrue(
            all(item.args == ("cn",) for item in effective_date.call_args_list)
        )

    def test_plan_status_update_rejects_a_stale_source_state(self) -> None:
        service = InvestmentPlanService(
            stock_service=_StockServiceStub(price=95),
            portfolio_service=self.portfolio,
        )
        plan = self._create_active(service)
        service.repo.update_plan_status(
            plan["id"],
            "closed",
            expected_status="active",
            expected_updated_at=plan["updated_at"],
        )
        with self.assertRaises(InvestmentPlanConflictError):
            service.repo.update_plan_status(
                plan["id"],
                "paused",
                expected_status="active",
                expected_updated_at=plan["updated_at"],
            )

    def test_evaluation_rejects_a_concurrent_plan_edit(self) -> None:
        service = InvestmentPlanService(
            stock_service=_StockServiceStub(price=95),
            portfolio_service=self.portfolio,
        )
        plan = self._create_active(service, required_cash_pct=25)

        def quote_after_edit(_symbol):
            service.update_plan(plan["id"], fields={"required_cash_pct": 35})
            return {
                "stock_code": "600519",
                "stock_name": "测试股票",
                "current_price": 95,
            }

        service._get_quote = MagicMock(side_effect=quote_after_edit)
        with self.assertRaisesRegex(InvestmentPlanConflictError, "being evaluated"):
            service.evaluate_plan(plan["id"])

        current = service.get_plan(plan["id"])
        self.assertEqual(current["required_cash_pct"], 35)
        self.assertIsNone(current["last_evaluated_at"])
        self.assertEqual(current["steps"][0]["status"], "pending")

    def test_partial_update_revalidates_existing_step_invariants(self) -> None:
        service = InvestmentPlanService(
            stock_service=_StockServiceStub(price=95),
            portfolio_service=self.portfolio,
        )
        plan = self._create_active(
            service,
            strategy_type="index_crash",
            benchmark_symbol="000300",
            max_position_pct=20,
            steps=[self._step(
                metric="benchmark_drawdown_250d_pct",
                operator="gte",
                threshold=20,
                target_position_pct=15,
            )],
        )

        with self.assertRaisesRegex(ValueError, "benchmark_symbol"):
            service.update_plan(plan["id"], fields={"benchmark_symbol": None})
        with self.assertRaisesRegex(ValueError, "target_position_pct"):
            service.update_plan(plan["id"], fields={"max_position_pct": 10})

        unchanged = service.get_plan(plan["id"])
        self.assertEqual(unchanged["benchmark_symbol"], "000300")
        self.assertEqual(unchanged["max_position_pct"], 20)

    def test_concurrent_notification_workers_claim_a_step_once(self) -> None:
        notifier = _BlockingNotifier()
        service_one = InvestmentPlanService(
            stock_service=_StockServiceStub(price=95),
            portfolio_service=self.portfolio,
            notifier=notifier,
        )
        plan = self._create_active(service_one)
        service_one.evaluate_plan(plan["id"])
        service_two = InvestmentPlanService(
            stock_service=_StockServiceStub(price=95),
            portfolio_service=self.portfolio,
            notifier=notifier,
        )

        holder = {}
        worker = threading.Thread(
            target=lambda: holder.setdefault("first", service_one.send_pending_notifications()),
            daemon=True,
        )
        worker.start()
        self.assertTrue(notifier.started.wait(timeout=5))
        second = service_two.send_pending_notifications()
        notifier.release.set()
        worker.join(timeout=5)

        self.assertFalse(second["attempted"])
        self.assertEqual(second["step_count"], 0)
        self.assertTrue(holder["first"]["sent"])
        self.assertEqual(len(notifier.messages), 1)

    def test_duplicate_open_plan_in_same_scope_is_rejected(self) -> None:
        service = InvestmentPlanService(
            stock_service=_StockServiceStub(price=95),
            portfolio_service=self.portfolio,
        )
        self._create_active(service)
        with self.assertRaises(InvestmentPlanConflictError):
            self._create_active(service)

    def test_batch_evaluation_filters_by_plan_market(self) -> None:
        service = InvestmentPlanService(
            stock_service=_StockServiceStub(price=95),
            portfolio_service=self.portfolio,
        )
        cn_plan = self._create_active(service)
        hk_plan = service.create_plan(
            symbol="HK00700",
            market="hk",
            strategy_type="value",
            status="active",
            thesis="现金流稳定",
            invalidation_note="核心业务持续恶化",
            steps=[self._step()],
        )

        result = service.evaluate_active_plans(markets={"hk"})
        self.assertEqual(result["evaluated"], 1)
        self.assertEqual(result["results"][0]["plan"]["id"], hk_plan["id"])
        self.assertIsNone(service.get_plan(cn_plan["id"])["last_evaluated_at"])

    def test_batch_evaluation_filters_by_check_frequency(self) -> None:
        service = InvestmentPlanService(
            stock_service=_StockServiceStub(price=95),
            portfolio_service=self.portfolio,
        )
        hourly_plan = self._create_active(service, check_frequency="hourly")
        service.set_plan_status(hourly_plan["id"], "closed")
        daily_plan = self._create_active(service, check_frequency="daily")

        result = service.evaluate_active_plans(check_frequencies={"daily"})

        self.assertEqual(result["evaluated"], 1)
        self.assertEqual(result["results"][0]["plan"]["id"], daily_plan["id"])

    def test_frequency_batch_notifies_only_plans_evaluated_in_that_batch(self) -> None:
        notifier = _NotifierStub()
        service = InvestmentPlanService(
            stock_service=_StockServiceStub(price=95),
            portfolio_service=self.portfolio,
            notifier=notifier,
        )
        daily_plan = self._create_active(service, check_frequency="daily")
        hourly_plan = service.create_plan(
            symbol="HK00700",
            market="hk",
            strategy_type="value",
            status="active",
            thesis="现金流稳定",
            invalidation_note="核心业务持续恶化",
            check_frequency="hourly",
            steps=[self._step()],
        )
        service.evaluate_plan(daily_plan["id"])
        service.evaluate_plan(hourly_plan["id"])

        result = service.evaluate_active_plans(
            check_frequencies={"daily"},
            send_notification=True,
        )

        self.assertTrue(result["notification"]["sent"])
        self.assertEqual(result["notification"]["step_count"], 1)
        self.assertIn("600519", notifier.messages[0][0])
        self.assertNotIn("HK00700", notifier.messages[0][0])
        self.assertIsNotNone(service.get_plan(daily_plan["id"])["steps"][0]["notified_at"])
        self.assertIsNone(service.get_plan(hourly_plan["id"])["steps"][0]["notified_at"])

    def test_review_due_is_exposed_without_forcing_trigger(self) -> None:
        service = InvestmentPlanService(
            stock_service=_StockServiceStub(price=120),
            portfolio_service=self.portfolio,
        )
        plan = self._create_active(service, review_date=date(2020, 1, 1))
        result = service.evaluate_plan(plan["id"])
        self.assertTrue(result["review_due"])
        self.assertEqual(result["plan"]["last_evaluation_status"], "review_due")


if __name__ == "__main__":
    unittest.main()
