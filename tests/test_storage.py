# -*- coding: utf-8 -*-
import unittest
import sys
import os
import sqlite3
import tempfile
import threading
from datetime import date
from unittest.mock import patch

import pandas as pd
from sqlalchemy import and_, select
from sqlalchemy.sql import func

# Ensure src module can be imported
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.config import Config
from src.storage import DatabaseManager, InvestmentPlan, InvestmentPlanStep, StockDaily

class TestStorage(unittest.TestCase):
    
    def test_parse_sniper_value(self):
        """测试解析狙击点位数值"""
        
        # 1. 正常数值
        self.assertEqual(DatabaseManager._parse_sniper_value(100), 100.0)
        self.assertEqual(DatabaseManager._parse_sniper_value(100.5), 100.5)
        self.assertEqual(DatabaseManager._parse_sniper_value("100"), 100.0)
        self.assertEqual(DatabaseManager._parse_sniper_value("100.5"), 100.5)
        
        # 2. 包含中文描述和"元"
        self.assertEqual(DatabaseManager._parse_sniper_value("建议在 100 元附近买入"), 100.0)
        self.assertEqual(DatabaseManager._parse_sniper_value("价格：100.5元"), 100.5)
        
        # 3. 包含干扰数字（修复的Bug场景）
        # 之前 "MA5" 会被错误提取为 5.0，现在应该提取 "元" 前面的 100
        text_bug = "无法给出。需等待MA5数据恢复，在股价回踩MA5且乖离率<2%时考虑100元"
        self.assertEqual(DatabaseManager._parse_sniper_value(text_bug), 100.0)
        
        # 4. 更多干扰场景
        text_complex = "MA10为20.5，建议在30元买入"
        self.assertEqual(DatabaseManager._parse_sniper_value(text_complex), 30.0)
        
        text_multiple = "支撑位10元，阻力位20元" # 应该提取最后一个"元"前面的数字，即20，或者更复杂的逻辑？
        # 当前逻辑是找最后一个冒号，然后找之后的第一个"元"，提取中间的数字。
        # 测试没有冒号的情况
        self.assertEqual(DatabaseManager._parse_sniper_value("30元"), 30.0)
        
        # 测试多个数字在"元"之前
        self.assertEqual(DatabaseManager._parse_sniper_value("MA5 10 20元"), 20.0)
        
        # 5. Fallback: no "元" character — extracts last non-MA number
        self.assertEqual(DatabaseManager._parse_sniper_value("102.10-103.00（MA5附近）"), 103.0)
        self.assertEqual(DatabaseManager._parse_sniper_value("97.62-98.50（MA10附近）"), 98.5)
        self.assertEqual(DatabaseManager._parse_sniper_value("93.40下方（MA20支撑）"), 93.4)
        self.assertEqual(DatabaseManager._parse_sniper_value("108.00-110.00（前期高点阻力）"), 110.0)

        # 6. 无效输入
        self.assertIsNone(DatabaseManager._parse_sniper_value(None))
        self.assertIsNone(DatabaseManager._parse_sniper_value(""))
        self.assertIsNone(DatabaseManager._parse_sniper_value("没有数字"))
        self.assertIsNone(DatabaseManager._parse_sniper_value("MA5但没有元"))

        # 7. 回归：括号内技术指标数字不应被提取
        self.assertNotEqual(DatabaseManager._parse_sniper_value("1.52-1.53 (回踩MA5/10附近)"), 10.0)
        self.assertNotEqual(DatabaseManager._parse_sniper_value("1.55-1.56(MA5/M20支撑)"), 20.0)
        self.assertNotEqual(DatabaseManager._parse_sniper_value("1.49-1.50(MA60附近企稳)"), 60.0)
        # 验证正确值在区间内
        self.assertIn(DatabaseManager._parse_sniper_value("1.52-1.53 (回踩MA5/10附近)"), [1.52, 1.53])
        self.assertIn(DatabaseManager._parse_sniper_value("1.55-1.56(MA5/M20支撑)"), [1.55, 1.56])
        self.assertIn(DatabaseManager._parse_sniper_value("1.49-1.50(MA60附近企稳)"), [1.49, 1.50])

    def test_get_chat_sessions_prefix_is_scoped_by_colon_boundary(self):
        DatabaseManager.reset_instance()
        db = DatabaseManager(db_url="sqlite:///:memory:")

        db.save_conversation_message("telegram_12345:chat", "user", "first user")
        db.save_conversation_message("telegram_123456:chat", "user", "second user")

        sessions = db.get_chat_sessions(session_prefix="telegram_12345")

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["session_id"], "telegram_12345:chat")

        DatabaseManager.reset_instance()

    def test_get_chat_sessions_can_include_legacy_exact_session_id(self):
        DatabaseManager.reset_instance()
        db = DatabaseManager(db_url="sqlite:///:memory:")

        db.save_conversation_message("feishu_u1", "user", "legacy chat")
        db.save_conversation_message("feishu_u1:ask_600519", "user", "ask session")

        sessions = db.get_chat_sessions(
            session_prefix="feishu_u1:",
            extra_session_ids=["feishu_u1"],
        )

        self.assertEqual({item["session_id"] for item in sessions}, {"feishu_u1", "feishu_u1:ask_600519"})

        DatabaseManager.reset_instance()

    def test_file_sqlite_enables_wal_and_busy_timeout(self):
        temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(temp_dir.name, "sqlite_pragmas.db")
        original_env = {
            "DATABASE_PATH": os.environ.get("DATABASE_PATH"),
            "SQLITE_BUSY_TIMEOUT_MS": os.environ.get("SQLITE_BUSY_TIMEOUT_MS"),
            "SQLITE_WAL_ENABLED": os.environ.get("SQLITE_WAL_ENABLED"),
        }

        try:
            os.environ["DATABASE_PATH"] = db_path
            os.environ["SQLITE_BUSY_TIMEOUT_MS"] = "1234"
            os.environ["SQLITE_WAL_ENABLED"] = "true"
            Config.reset_instance()
            DatabaseManager.reset_instance()

            db = DatabaseManager.get_instance()
            with db.get_session() as session:
                journal_mode = session.connection().exec_driver_sql("PRAGMA journal_mode").scalar()
                busy_timeout = session.connection().exec_driver_sql("PRAGMA busy_timeout").scalar()

            self.assertEqual(str(journal_mode).lower(), "wal")
            self.assertEqual(int(busy_timeout), 1234)
        finally:
            DatabaseManager.reset_instance()
            Config.reset_instance()
            for key, value in original_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            temp_dir.cleanup()

    def test_get_instance_waits_for_first_initialization(self):
        DatabaseManager.reset_instance()
        temp_dir = tempfile.TemporaryDirectory()
        db_url = f"sqlite:///{os.path.join(temp_dir.name, 'cold_start.db')}"
        init_started = threading.Event()
        release_init = threading.Event()
        second_started = threading.Event()
        second_returned = threading.Event()
        results = []
        errors = []
        result_lock = threading.Lock()
        original_init = DatabaseManager.__init__

        def blocking_init(instance, _db_url_override=None):
            init_started.set()
            if not release_init.wait(timeout=2):
                raise TimeoutError("test initialization release timed out")
            original_init(instance, db_url=db_url)

        def worker(name):
            try:
                if name == "second":
                    second_started.set()
                db = DatabaseManager.get_instance()
                session = db.get_session()
                session.close()
                with result_lock:
                    results.append(db)
            except Exception as exc:
                with result_lock:
                    errors.append(exc)
            finally:
                if name == "second":
                    second_returned.set()

        first = threading.Thread(target=worker, args=("first",))
        second = threading.Thread(target=worker, args=("second",))
        try:
            with patch.object(DatabaseManager, "__init__", new=blocking_init):
                first.start()
                self.assertTrue(init_started.wait(timeout=1))
                second.start()
                self.assertTrue(second_started.wait(timeout=1))
                self.assertFalse(second_returned.wait(timeout=0.1))
                release_init.set()
                first.join(timeout=2)
                second.join(timeout=2)

            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 2)
            self.assertIs(results[0], results[1])
            self.assertTrue(results[0]._initialized)
        finally:
            release_init.set()
            first.join(timeout=2)
            second.join(timeout=2)
            DatabaseManager.reset_instance()
            temp_dir.cleanup()

    def test_get_instance_discards_failed_partial_instance(self):
        DatabaseManager.reset_instance()
        try:
            with patch.object(DatabaseManager, "__init__", side_effect=RuntimeError("boom")):
                with self.assertRaisesRegex(RuntimeError, "boom"):
                    DatabaseManager.get_instance()
            self.assertIsNone(DatabaseManager._instance)
        finally:
            DatabaseManager.reset_instance()

    def test_existing_investment_plan_tables_are_upgraded_in_place(self):
        temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(temp_dir.name, "legacy_investment_plans.db")

        connection = sqlite3.connect(db_path)
        try:
            connection.executescript(
                """
                CREATE TABLE investment_plans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER,
                    symbol VARCHAR(16) NOT NULL,
                    market VARCHAR(8) NOT NULL,
                    name VARCHAR(64),
                    strategy_type VARCHAR(24) NOT NULL,
                    status VARCHAR(16) NOT NULL,
                    thesis TEXT NOT NULL,
                    invalidation_note TEXT NOT NULL,
                    benchmark_symbol VARCHAR(16),
                    max_position_pct FLOAT,
                    required_cash_pct FLOAT,
                    review_date DATE,
                    last_price FLOAT,
                    last_evaluated_at DATETIME,
                    last_evaluation_status VARCHAR(24),
                    last_evaluation_note TEXT,
                    created_at DATETIME,
                    updated_at DATETIME
                );
                CREATE TABLE investment_plan_steps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plan_id INTEGER NOT NULL,
                    action VARCHAR(16) NOT NULL,
                    metric VARCHAR(40) NOT NULL,
                    operator VARCHAR(16) NOT NULL,
                    threshold FLOAT NOT NULL,
                    upper_threshold FLOAT,
                    target_position_pct FLOAT,
                    note VARCHAR(255),
                    sort_order INTEGER NOT NULL,
                    status VARCHAR(16) NOT NULL,
                    triggered_at DATETIME,
                    completed_at DATETIME,
                    notified_at DATETIME,
                    created_at DATETIME,
                    updated_at DATETIME
                );
                INSERT INTO investment_plans (
                    id, symbol, market, strategy_type, status, thesis, invalidation_note
                ) VALUES (1, '600519', 'cn', 'value', 'active', 'legacy thesis', 'legacy invalidation');
                INSERT INTO investment_plan_steps (
                    id, plan_id, action, metric, operator, threshold, sort_order, status
                ) VALUES (1, 1, 'buy', 'price', 'lte', 1200, 0, 'pending');
                INSERT INTO investment_plan_steps (
                    id, plan_id, action, metric, operator, threshold, sort_order, status,
                    triggered_at, notified_at
                ) VALUES (
                    2, 1, 'add', 'price', 'lte', 1100, 1, 'completed',
                    '2026-08-20 10:00:00', '2026-08-20 10:00:03'
                );
                INSERT INTO investment_plan_steps (
                    id, plan_id, action, metric, operator, threshold, sort_order, status,
                    triggered_at
                ) VALUES (
                    3, 1, 'add', 'price', 'lte', 1000, 2, 'triggered',
                    '2026-08-21 10:00:00'
                );
                """
            )
            connection.commit()
        finally:
            connection.close()

        try:
            DatabaseManager.reset_instance()
            db = DatabaseManager(db_url=f"sqlite:///{db_path}")
            with db.get_session() as session:
                plan = session.get(InvestmentPlan, 1)
                step = session.get(InvestmentPlanStep, 1)
                sent_step = session.get(InvestmentPlanStep, 2)
                pending_step = session.get(InvestmentPlanStep, 3)
                plan_columns = {
                    row[1] for row in session.connection().exec_driver_sql(
                        'PRAGMA table_info("investment_plans")'
                    )
                }
                step_columns = {
                    row[1] for row in session.connection().exec_driver_sql(
                        'PRAGMA table_info("investment_plan_steps")'
                    )
                }
                step_indexes = {
                    row[1] for row in session.connection().exec_driver_sql(
                        'PRAGMA index_list("investment_plan_steps")'
                    )
                }
                plan_indexes = {
                    row[1] for row in session.connection().exec_driver_sql(
                        'PRAGMA index_list("investment_plans")'
                    )
                }

            self.assertEqual(plan.thesis, "legacy thesis")
            self.assertIsNone(plan.last_blocked_reasons)
            self.assertTrue(plan.notify_on_trigger)
            self.assertIsNone(plan.notification_channels)
            self.assertEqual(plan.check_frequency, "daily")
            self.assertIsNone(plan.planned_capital)
            self.assertEqual(step.threshold, 1200)
            self.assertIsNone(step.notification_claim_token)
            self.assertIsNone(step.notification_claimed_at)
            self.assertIsNone(step.notification_status)
            self.assertIsNone(step.notification_status_at)
            self.assertIsNone(step.notification_error)
            self.assertEqual(sent_step.notification_status, "sent")
            self.assertEqual(sent_step.notification_status_at, sent_step.notified_at)
            self.assertEqual(pending_step.notification_status, "pending")
            self.assertEqual(pending_step.notification_status_at, pending_step.triggered_at)
            self.assertIsNone(step.execution_date)
            self.assertIsNone(step.execution_at)
            self.assertIsNone(step.execution_price)
            self.assertIsNone(step.execution_quantity)
            self.assertIsNone(step.execution_amount)
            self.assertIsNone(step.execution_fee)
            self.assertIsNone(step.execution_note)
            self.assertIn("last_blocked_reasons", plan_columns)
            self.assertIn("notify_on_trigger", plan_columns)
            self.assertIn("notification_channels", plan_columns)
            self.assertIn("check_frequency", plan_columns)
            self.assertIn("planned_capital", plan_columns)
            self.assertIn("notification_claim_token", step_columns)
            self.assertIn("notification_claimed_at", step_columns)
            self.assertIn("notification_status", step_columns)
            self.assertIn("notification_status_at", step_columns)
            self.assertIn("notification_error", step_columns)
            self.assertIn("execution_date", step_columns)
            self.assertIn("execution_at", step_columns)
            self.assertIn("execution_price", step_columns)
            self.assertIn("execution_quantity", step_columns)
            self.assertIn("execution_amount", step_columns)
            self.assertIn("execution_fee", step_columns)
            self.assertIn("execution_note", step_columns)
            self.assertIn("ix_investment_plan_steps_notification_claim_token", step_indexes)
            self.assertIn("ix_investment_plan_steps_notification_claimed_at", step_indexes)
            self.assertIn("ix_investment_plans_check_frequency", plan_indexes)

            DatabaseManager.reset_instance()
            DatabaseManager(db_url=f"sqlite:///{db_path}")
        finally:
            DatabaseManager.reset_instance()
            temp_dir.cleanup()

    def test_sqlite_write_transactions_begin_immediate(self):
        DatabaseManager.reset_instance()
        db = DatabaseManager(db_url="sqlite:///:memory:")
        session = db.get_session()
        connection = session.connection()

        try:
            with patch.object(db, "get_session", return_value=session):
                with patch.object(connection, "exec_driver_sql", wraps=connection.exec_driver_sql) as mock_exec:
                    result = db._run_write_transaction("unit-test", lambda current_session: 7)

            self.assertEqual(result, 7)
            self.assertTrue(
                any(call.args == ("BEGIN IMMEDIATE",) for call in mock_exec.call_args_list)
            )
        finally:
            DatabaseManager.reset_instance()

    def test_save_daily_data_sqlite_concurrent_same_code_date_counts_only_new_rows(self):
        DatabaseManager.reset_instance()
        temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(temp_dir.name, "sqlite_daily_concurrency.db")
        db = DatabaseManager(db_url=f"sqlite:///{db_path}")

        results = []
        results_lock = threading.Lock()
        start_barrier = threading.Barrier(2)

        def worker() -> None:
            start_barrier.wait()
            count = db.save_daily_data(
                pd.DataFrame(
                    [
                        {
                            'date': date(2026, 4, 1),
                            'open': 10,
                            'high': 11,
                            'low': 9,
                            'close': 10.5,
                            'volume': 100,
                            'amount': 1050,
                            'pct_chg': 1.2,
                            'ma5': 10.1,
                            'ma10': 10.2,
                            'ma20': 10.3,
                            'volume_ratio': 1.0,
                        }
                    ]
                ),
                code='600519',
                data_source='test',
            )
            with results_lock:
                results.append(count)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        try:
            self.assertCountEqual(results, [1, 0])

            with db.get_session() as session:
                total = session.execute(
                    select(func.count()).select_from(StockDaily).where(
                        and_(
                            StockDaily.code == '600519',
                            StockDaily.date == date(2026, 4, 1),
                        )
                    )
                ).scalar()

            self.assertEqual(total, 1)
        finally:
            temp_dir.cleanup()
            DatabaseManager.reset_instance()

if __name__ == '__main__':
    unittest.main()
