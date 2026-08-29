# -*- coding: utf-8 -*-
"""Investment strategy plan API contract tests."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

try:
    import litellm  # noqa: F401
except ModuleNotFoundError:
    sys.modules["litellm"] = MagicMock()

import src.auth as auth
from api.app import create_app
from src.config import Config
from src.services.investment_plan_service import InvestmentPlanService
from src.services.scheduler_status_service import SchedulerStatusService
from src.storage import DatabaseManager


def _reset_auth_globals() -> None:
    auth._auth_enabled = None
    auth._session_secret = None
    auth._password_hash_salt = None
    auth._password_hash_stored = None
    auth._rate_limit = {}


class InvestmentPlanApiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        _reset_auth_globals()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.db_path = self.data_dir / "investment_plan_api.db"
        self.env_path = self.data_dir / ".env"
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
        self.client = TestClient(create_app(static_dir=self.data_dir / "empty-static"))

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        os.environ.pop("ENV_FILE", None)
        os.environ.pop("DATABASE_PATH", None)
        self.temp_dir.cleanup()

    @staticmethod
    def _payload():
        return {
            "symbol": "600519",
            "market": "cn",
            "name": "贵州茅台",
            "strategy_type": "value",
            "status": "active",
            "thesis": "行业龙头且现金流稳定",
            "invalidation_note": "核心盈利能力持续恶化",
            "max_position_pct": 20,
            "required_cash_pct": 25,
            "notify_on_trigger": True,
            "notification_channels": ["ntfy"],
            "check_frequency": "hourly",
            "steps": [{
                "action": "buy",
                "metric": "price",
                "operator": "lte",
                "threshold": 1400,
                "target_position_pct": 5,
            }],
        }

    def test_crud_lifecycle_and_evaluation_contract(self) -> None:
        created = self.client.post("/api/v1/investment-plans", json=self._payload())
        self.assertEqual(created.status_code, 200, created.text)
        plan = created.json()
        self.assertEqual(plan["strategy_label"], "价值投资")
        self.assertEqual(plan["status"], "active")
        self.assertTrue(plan["notify_on_trigger"])
        self.assertEqual(plan["notification_channels"], ["ntfy"])
        self.assertEqual(plan["check_frequency"], "hourly")
        self.assertIsNone(plan["steps"][0]["notification_status"])

        listed = self.client.get("/api/v1/investment-plans", params={"status": "active"})
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["summary"]["active"], 1)

        with patch(
            "src.services.stock_service.StockService.get_realtime_quote",
            return_value={
                "stock_code": "600519",
                "stock_name": "贵州茅台",
                "current_price": 1350,
                "price_date": date.today().isoformat(),
            },
        ):
            evaluated = self.client.post(f"/api/v1/investment-plans/{plan['id']}/evaluate")
        self.assertEqual(evaluated.status_code, 200, evaluated.text)
        evaluation = evaluated.json()
        self.assertEqual(evaluation["plan"]["last_evaluation_status"], "triggered")
        self.assertEqual(len(evaluation["newly_triggered_step_ids"]), 1)
        self.assertFalse(evaluation["notification"]["attempted"])
        self.assertEqual(
            evaluation["plan"]["steps"][0]["notification_status"],
            "pending",
        )

        step_id = evaluation["plan"]["steps"][0]["id"]
        completed = self.client.patch(
            f"/api/v1/investment-plans/{plan['id']}/steps/{step_id}",
            json={"status": "completed"},
        )
        self.assertEqual(completed.status_code, 200)
        self.assertEqual(completed.json()["steps"][0]["status"], "completed")

        paused = self.client.patch(
            f"/api/v1/investment-plans/{plan['id']}/status",
            json={"status": "paused"},
        )
        self.assertEqual(paused.status_code, 200)
        self.assertEqual(paused.json()["status"], "paused")

    def test_accepts_minute_check_frequency(self) -> None:
        payload = self._payload()
        payload["check_frequency"] = "minute"

        created = self.client.post("/api/v1/investment-plans", json=payload)

        self.assertEqual(created.status_code, 200, created.text)
        self.assertEqual(created.json()["check_frequency"], "minute")

    def test_scheduler_status_contract_reports_shared_heartbeat(self) -> None:
        SchedulerStatusService().mark_started(schedule_time="18:00")

        response = self.client.get("/api/v1/investment-plans/scheduler-status")

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["status"], "online")
        self.assertTrue(payload["online"])
        self.assertEqual(payload["schedule_time"], "18:00")
        self.assertIsNotNone(payload["heartbeat_at"])

    def test_scheduler_status_invalid_structure_returns_unavailable(self) -> None:
        status_path = self.db_path.parent / "scheduler_status.json"
        status_path.write_text("{}", encoding="utf-8")

        response = self.client.get("/api/v1/investment-plans/scheduler-status")

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["status"], "unavailable")
        self.assertFalse(payload["online"])
        self.assertIsNone(payload["heartbeat_at"])

    def test_accepts_minute_check_frequency_for_us_market(self) -> None:
        payload = self._payload()
        payload.update({
            "symbol": "AAPL",
            "market": "us",
            "check_frequency": "minute",
        })

        created = self.client.post("/api/v1/investment-plans", json=payload)

        self.assertEqual(created.status_code, 200, created.text)
        self.assertEqual(created.json()["check_frequency"], "minute")

    def test_duplicate_and_invalid_plan_errors(self) -> None:
        first = self.client.post("/api/v1/investment-plans", json=self._payload())
        self.assertEqual(first.status_code, 200)
        duplicate = self.client.post("/api/v1/investment-plans", json=self._payload())
        self.assertEqual(duplicate.status_code, 409)

        invalid = self._payload()
        invalid["symbol"] = "bad symbol"
        invalid["status"] = "draft"
        response = self.client.post("/api/v1/investment-plans", json=invalid)
        self.assertEqual(response.status_code, 400)

        too_many_channels = self._payload()
        too_many_channels["symbol"] = "000001"
        too_many_channels["notification_channels"] = ["wechat", "custom"]
        response = self.client.post("/api/v1/investment-plans", json=too_many_channels)
        self.assertEqual(response.status_code, 422)

    def test_manual_evaluation_queues_notification_after_response(self) -> None:
        created = self.client.post("/api/v1/investment-plans", json=self._payload())
        self.assertEqual(created.status_code, 200, created.text)
        plan_id = created.json()["id"]

        with patch(
            "src.services.stock_service.StockService.get_realtime_quote",
            return_value={
                "stock_code": "600519",
                "stock_name": "贵州茅台",
                "current_price": 1350,
                "price_date": date.today().isoformat(),
            },
        ), patch.object(
            InvestmentPlanService,
            "send_pending_notifications",
            return_value={"attempted": True, "sent": True, "step_count": 1},
        ) as send_notifications:
            evaluated = self.client.post(
                f"/api/v1/investment-plans/{plan_id}/evaluate",
                params={"notify": "true"},
            )

        self.assertEqual(evaluated.status_code, 200, evaluated.text)
        notification = evaluated.json()["notification"]
        self.assertTrue(notification["queued"])
        self.assertFalse(notification["attempted"])
        self.assertFalse(notification["sent"])
        self.assertEqual(notification["step_count"], 1)
        send_notifications.assert_called_once_with(plan_ids=[plan_id])

    def test_batch_evaluation_queues_notification_after_response(self) -> None:
        created = self.client.post("/api/v1/investment-plans", json=self._payload())
        self.assertEqual(created.status_code, 200, created.text)
        plan_id = created.json()["id"]

        with patch(
            "src.services.stock_service.StockService.get_realtime_quote",
            return_value={
                "stock_code": "600519",
                "stock_name": "贵州茅台",
                "current_price": 1350,
                "price_date": date.today().isoformat(),
            },
        ), patch.object(
            InvestmentPlanService,
            "send_pending_notifications",
            return_value={"attempted": True, "sent": True, "step_count": 1},
        ) as send_notifications:
            evaluated = self.client.post(
                "/api/v1/investment-plans/evaluate-active",
                params={"notify": "true"},
            )

        self.assertEqual(evaluated.status_code, 200, evaluated.text)
        notification = evaluated.json()["notification"]
        self.assertTrue(notification["queued"])
        self.assertFalse(notification["attempted"])
        self.assertFalse(notification["sent"])
        self.assertEqual(notification["step_count"], 1)
        send_notifications.assert_called_once_with(plan_ids=[plan_id])

    def test_active_drawdown_plan_requires_benchmark(self) -> None:
        payload = self._payload()
        payload["strategy_type"] = "index_crash"
        payload["steps"] = [{
            "action": "buy",
            "metric": "benchmark_drawdown_250d_pct",
            "operator": "gte",
            "threshold": 20,
        }]
        response = self.client.post("/api/v1/investment-plans", json=payload)
        self.assertEqual(response.status_code, 400)

    def test_pending_step_cannot_be_skipped_through_api(self) -> None:
        created = self.client.post("/api/v1/investment-plans", json=self._payload())
        self.assertEqual(created.status_code, 200, created.text)
        plan = created.json()
        response = self.client.patch(
            f"/api/v1/investment-plans/{plan['id']}/steps/{plan['steps'][0]['id']}",
            json={"status": "skipped"},
        )
        self.assertEqual(response.status_code, 409)

    def test_etf_execution_contract_records_manual_fill_and_review(self) -> None:
        payload = self._payload()
        payload.update({
            "symbol": "510300",
            "name": "华泰柏瑞沪深300ETF",
            "strategy_type": "index_crash",
            "planned_capital": 100000,
            "max_position_pct": 20,
            "steps": [{
                "action": "buy",
                "metric": "price",
                "operator": "lte",
                "threshold": 10,
                "target_position_pct": 20,
            }],
        })
        created = self.client.post("/api/v1/investment-plans", json=payload)
        self.assertEqual(created.status_code, 200, created.text)
        plan = created.json()
        self.assertEqual(plan["planned_capital"], 100000)
        changed_strategy = self.client.put(
            f"/api/v1/investment-plans/{plan['id']}",
            json={"strategy_type": "value"},
        )
        self.assertEqual(changed_strategy.status_code, 409)

        with patch(
            "src.services.stock_service.StockService.get_realtime_quote",
            return_value={
                "stock_code": "510300",
                "stock_name": "华泰柏瑞沪深300ETF",
                "current_price": 10,
                "price_date": date.today().isoformat(),
            },
        ):
            evaluated = self.client.post(
                f"/api/v1/investment-plans/{plan['id']}/evaluate"
            )
        self.assertEqual(evaluated.status_code, 200, evaluated.text)
        step_id = evaluated.json()["plan"]["steps"][0]["id"]

        completed_without_fill = self.client.patch(
            f"/api/v1/investment-plans/{plan['id']}/steps/{step_id}",
            json={"status": "completed"},
        )
        self.assertEqual(completed_without_fill.status_code, 409)

        for invalid_execution_at in (
            date.today().isoformat(),
            datetime.now().replace(microsecond=0).isoformat(),
        ):
            invalid_execution = self.client.post(
                f"/api/v1/investment-plans/{plan['id']}/steps/{step_id}/execution",
                json={
                    "execution_at": invalid_execution_at,
                    "price": 10,
                    "quantity": 2000,
                },
            )
            self.assertEqual(invalid_execution.status_code, 422, invalid_execution.text)

        execution = self.client.post(
            f"/api/v1/investment-plans/{plan['id']}/steps/{step_id}/execution",
            json={
                "execution_at": datetime.now().astimezone().replace(microsecond=0).isoformat(),
                "price": 10,
                "quantity": 2000,
                "fee": 5,
                "note": "真实成交",
            },
        )
        self.assertEqual(execution.status_code, 200, execution.text)
        body = execution.json()
        self.assertEqual(body["steps"][0]["execution_amount"], 20000)
        self.assertIsNotNone(body["steps"][0]["execution_at"])
        self.assertEqual(body["steps"][0]["status"], "completed")
        self.assertEqual(body["execution_summary"]["total_cost"], 20005)
        self.assertEqual(body["execution_summary"]["remaining_cash"], 79995)

    def test_plain_numeric_hk_symbol_is_normalized_for_create_and_list(self) -> None:
        payload = self._payload()
        payload.update({"symbol": "700", "market": "hk", "name": "腾讯控股"})
        created = self.client.post("/api/v1/investment-plans", json=payload)
        self.assertEqual(created.status_code, 200, created.text)
        self.assertEqual(created.json()["symbol"], "HK00700")

        listed = self.client.get(
            "/api/v1/investment-plans",
            params={"symbol": "00700"},
        )
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(listed.json()["total"], 1)
        self.assertEqual(listed.json()["items"][0]["symbol"], "HK00700")

    def test_partial_update_cannot_clear_required_benchmark(self) -> None:
        payload = self._payload()
        payload["strategy_type"] = "index_crash"
        payload["benchmark_symbol"] = "000300"
        payload["steps"] = [{
            "action": "buy",
            "metric": "benchmark_drawdown_250d_pct",
            "operator": "gte",
            "threshold": 20,
        }]
        created = self.client.post("/api/v1/investment-plans", json=payload)
        self.assertEqual(created.status_code, 200, created.text)

        response = self.client.put(
            f"/api/v1/investment-plans/{created.json()['id']}",
            json={"benchmark_symbol": None},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json().get("error"), "validation_error")


if __name__ == "__main__":
    unittest.main()
