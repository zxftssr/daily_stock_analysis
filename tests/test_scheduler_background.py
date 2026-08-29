# -*- coding: utf-8 -*-
"""Tests for Scheduler background task support."""

from datetime import datetime
from threading import Event
import sys
import unittest
from unittest.mock import MagicMock, patch


class _FakeJob:
    def __init__(self, schedule_module):
        self._schedule_module = schedule_module
        self.next_run = datetime(2026, 1, 1, 18, 0, 0)
        self.at_time = None

    @property
    def day(self):
        return self

    def at(self, value):
        self.at_time = value
        hour, minute = [int(part) for part in value.split(":")]
        self.next_run = datetime(2026, 1, 1, hour, minute, 0)
        return self

    def do(self, fn):
        self.job_func = fn
        self._schedule_module.jobs.append(self)
        return self


class _FakeScheduleModule:
    def __init__(self):
        self.jobs = []

    def every(self):
        return _FakeJob(self)

    def get_jobs(self):
        return list(self.jobs)

    def run_pending(self):
        return None

    def cancel_job(self, job):
        self.jobs.remove(job)


class SchedulerBackgroundTaskTestCase(unittest.TestCase):
    def test_scheduler_heartbeat_callback_isolated_from_failure(self):
        fake_schedule = _FakeScheduleModule()
        heartbeat = MagicMock(side_effect=RuntimeError("status unavailable"))
        with patch.dict(sys.modules, {"schedule": fake_schedule}):
            from src.scheduler import Scheduler

            scheduler = Scheduler(
                schedule_time="18:00",
                heartbeat_callback=heartbeat,
            )
            scheduler._emit_heartbeat()

        heartbeat.assert_called_once_with()

    def test_shutdown_status_is_emitted_before_waiting_for_daily_worker(self):
        fake_schedule = _FakeScheduleModule()
        order = []
        worker = MagicMock()
        worker.is_alive.return_value = True
        worker.join.side_effect = lambda: order.append("joined")
        with patch.dict(sys.modules, {"schedule": fake_schedule}):
            from src.scheduler import Scheduler

            scheduler = Scheduler(
                schedule_time="18:00",
                shutdown_callback=lambda: order.append("stopped"),
            )
            scheduler._daily_thread = worker
            scheduler.shutdown_handler.request_shutdown()
            scheduler.run()

        self.assertEqual(order, ["stopped", "joined"])

    def test_shutdown_waits_for_active_background_worker(self):
        fake_schedule = _FakeScheduleModule()
        worker = MagicMock()
        worker.is_alive.return_value = True
        with patch.dict(sys.modules, {"schedule": fake_schedule}):
            from src.scheduler import Scheduler

            scheduler = Scheduler(schedule_time="18:00")
            scheduler._background_tasks.append({
                "name": "minute",
                "thread": worker,
            })
            scheduler.shutdown_handler.request_shutdown()
            scheduler.run()

        worker.join.assert_called_once_with()

    def test_background_task_runs_when_interval_elapsed(self):
        fake_schedule = _FakeScheduleModule()
        with patch.dict(sys.modules, {"schedule": fake_schedule}):
            from src.scheduler import Scheduler

            scheduler = Scheduler(schedule_time="18:00")
            calls = []
            fake_thread = MagicMock()
            fake_thread.is_alive.return_value = False

            def _make_thread(target=None, **kwargs):
                fake_thread.start.side_effect = target
                return fake_thread

            with patch("src.scheduler.threading.Thread", side_effect=_make_thread):
                scheduler.add_background_task(lambda: calls.append("ran"), interval_seconds=1, run_immediately=True, name="test")

        self.assertEqual(calls, ["ran"])

    def test_background_task_waits_for_interval(self):
        fake_schedule = _FakeScheduleModule()
        with patch.dict(sys.modules, {"schedule": fake_schedule}):
            from src.scheduler import Scheduler

            scheduler = Scheduler(schedule_time="18:00")
            calls = []
            scheduler.add_background_task(lambda: calls.append("ran"), interval_seconds=60, run_immediately=False, name="test")

            with patch("src.scheduler.time.time", return_value=scheduler._background_tasks[0]["last_run"] + 10):
                scheduler._run_background_tasks()

        self.assertEqual(calls, [])

    def test_blocking_daily_task_does_not_block_background_or_reenter(self):
        fake_schedule = _FakeScheduleModule()
        daily_started = Event()
        release_daily = Event()
        background_ran = Event()
        daily_calls = []

        def daily_task():
            daily_calls.append("daily")
            daily_started.set()
            release_daily.wait(timeout=2)

        with patch.dict(sys.modules, {"schedule": fake_schedule}):
            from src.scheduler import Scheduler

            scheduler = Scheduler(schedule_time="18:00")
            scheduler.add_background_task(
                background_ran.set,
                interval_seconds=60,
                run_immediately=False,
                name="minute",
            )
            background_entry = scheduler._background_tasks[0]
            scheduler.set_daily_task(daily_task, run_immediately=True)
            self.assertTrue(daily_started.wait(timeout=1))
            daily_worker = scheduler._daily_thread

            with patch(
                "src.scheduler.time.time",
                return_value=background_entry["last_run"] + 60,
            ):
                scheduler._run_background_tasks()

            self.assertTrue(background_ran.wait(timeout=1))
            self.assertFalse(scheduler._start_daily_task())
            self.assertEqual(daily_calls, ["daily"])
            release_daily.set()
            self.assertIsNotNone(daily_worker)
            daily_worker.join(timeout=1)

        self.assertFalse(daily_worker.is_alive())

    def test_run_with_schedule_registers_background_tasks_before_immediate_daily_task(self):
        fake_schedule = _FakeScheduleModule()
        with patch.dict(sys.modules, {"schedule": fake_schedule}):
            from src import scheduler as scheduler_module

            order = []

            class FakeScheduler:
                def __init__(
                    self,
                    schedule_time="18:00",
                    schedule_time_provider=None,
                    heartbeat_callback=None,
                    shutdown_callback=None,
                ):
                    order.append(("init", schedule_time))
                    order.append(("provider", callable(schedule_time_provider)))
                    order.append(("heartbeat", callable(heartbeat_callback)))
                    order.append(("shutdown", callable(shutdown_callback)))

                def add_background_task(self, **kwargs):
                    order.append(("background", kwargs["name"]))

                def set_daily_task(self, task, run_immediately=True):
                    order.append(("daily", run_immediately))

                def run(self):
                    order.append(("run", None))

            with patch.object(scheduler_module, "Scheduler", FakeScheduler):
                scheduler_module.run_with_schedule(
                    task=lambda: None,
                    run_immediately=True,
                    background_tasks=[{
                        "task": lambda: None,
                        "interval_seconds": 60,
                        "run_immediately": True,
                        "name": "event_monitor",
                    }],
                    heartbeat_callback=lambda: None,
                    shutdown_callback=lambda: None,
                )

        self.assertEqual(order[:6], [
            ("init", "18:00"),
            ("provider", False),
            ("heartbeat", True),
            ("shutdown", True),
            ("background", "event_monitor"),
            ("daily", True),
        ])

    def test_scheduler_reloads_daily_job_when_schedule_time_changes(self):
        fake_schedule = _FakeScheduleModule()
        with patch.dict(sys.modules, {"schedule": fake_schedule}):
            from src.scheduler import Scheduler

            scheduler = Scheduler(
                schedule_time="18:00",
                schedule_time_provider=lambda: "09:30",
            )
            scheduler.set_daily_task(lambda: None, run_immediately=False)

            self.assertEqual(len(fake_schedule.jobs), 1)
            self.assertEqual(fake_schedule.jobs[0].at_time, "18:00")

            scheduler._refresh_daily_schedule_if_needed()

        self.assertEqual(len(fake_schedule.jobs), 1)
        self.assertEqual(fake_schedule.jobs[0].at_time, "09:30")
        self.assertEqual(scheduler.schedule_time, "09:30")

    def test_scheduler_keeps_existing_daily_job_when_schedule_time_invalid(self):
        fake_schedule = _FakeScheduleModule()
        with patch.dict(sys.modules, {"schedule": fake_schedule}):
            from src.scheduler import Scheduler

            scheduler = Scheduler(
                schedule_time="18:00",
                schedule_time_provider=lambda: "25:99",
            )
            scheduler.set_daily_task(lambda: None, run_immediately=False)

            scheduler._refresh_daily_schedule_if_needed()

        self.assertEqual(len(fake_schedule.jobs), 1)
        self.assertEqual(fake_schedule.jobs[0].at_time, "18:00")
        self.assertEqual(scheduler.schedule_time, "18:00")

    def test_scheduler_keeps_current_daily_job_when_schedule_time_provider_fails(self):
        fake_schedule = _FakeScheduleModule()
        with patch.dict(sys.modules, {"schedule": fake_schedule}):
            from src.scheduler import Scheduler

            provider_calls = {"count": 0}

            def provider():
                provider_calls["count"] += 1
                if provider_calls["count"] == 1:
                    return "09:30"
                raise RuntimeError("boom")

            scheduler = Scheduler(
                schedule_time="18:00",
                schedule_time_provider=provider,
            )
            scheduler.set_daily_task(lambda: None, run_immediately=False)

            scheduler._refresh_daily_schedule_if_needed()
            scheduler._refresh_daily_schedule_if_needed()

        self.assertEqual(len(fake_schedule.jobs), 1)
        self.assertEqual(fake_schedule.jobs[0].at_time, "09:30")
        self.assertEqual(scheduler.schedule_time, "09:30")

    def test_scheduler_rejects_invalid_initial_schedule_time(self):
        fake_schedule = _FakeScheduleModule()
        with patch.dict(sys.modules, {"schedule": fake_schedule}):
            from src.scheduler import Scheduler

            scheduler = Scheduler(schedule_time="25:99")
            calls = []

            with self.assertRaisesRegex(ValueError, "25:99"):
                scheduler.set_daily_task(lambda: calls.append("ran"), run_immediately=True)

        self.assertEqual(calls, [])
        self.assertEqual(fake_schedule.jobs, [])


if __name__ == "__main__":
    unittest.main()
