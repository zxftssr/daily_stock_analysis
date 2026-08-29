# -*- coding: utf-8 -*-
"""Tests for the cross-process scheduler heartbeat status store."""

from datetime import datetime, timedelta, timezone
import json
import threading
from unittest.mock import patch

from src.services.scheduler_status_service import SchedulerStatusService


def test_missing_status_file_reports_not_started(tmp_path):
    service = SchedulerStatusService(status_path=tmp_path / "scheduler_status.json")

    result = service.get_status()

    assert result["status"] == "not_started"
    assert result["online"] is False
    assert result["heartbeat_at"] is None


def test_running_status_preserves_latest_minute_check_across_heartbeats(tmp_path):
    current_time = [datetime(2026, 8, 31, 1, 30, tzinfo=timezone.utc)]
    service = SchedulerStatusService(
        status_path=tmp_path / "scheduler_status.json",
        now_provider=lambda: current_time[0],
    )
    service.mark_started(schedule_time="18:00")
    service.mark_minute_check_started(markets=["cn"])
    current_time[0] += timedelta(seconds=2)
    service.mark_minute_check_completed(
        status="completed",
        markets=["cn"],
        evaluated=2,
        triggered=1,
        notification_sent=True,
    )
    current_time[0] += timedelta(seconds=28)
    service.mark_heartbeat(schedule_time="18:30")

    result = service.get_status()

    assert result["status"] == "online"
    assert result["heartbeat_age_seconds"] == 0
    assert result["schedule_time"] == "18:30"
    assert result["minute_check"]["status"] == "completed"
    assert result["minute_check"]["evaluated"] == 2
    assert result["minute_check"]["triggered"] == 1
    assert result["minute_check"]["notification_sent"] is True
    assert list(tmp_path.glob("*.tmp")) == []


def test_invalid_runtime_schedule_time_does_not_stop_heartbeat(tmp_path):
    current_time = [datetime(2026, 8, 31, 1, 30, tzinfo=timezone.utc)]
    service = SchedulerStatusService(
        status_path=tmp_path / "scheduler_status.json",
        now_provider=lambda: current_time[0],
    )
    service.mark_started(schedule_time="18:00")
    current_time[0] += timedelta(seconds=60)

    service.mark_heartbeat(schedule_time="25:99")
    result = service.get_status()

    assert result["status"] == "online"
    assert result["heartbeat_age_seconds"] == 0
    assert result["schedule_time"] == "18:00"


def test_stale_heartbeat_reports_offline(tmp_path):
    current_time = [datetime(2026, 8, 31, 1, 30, tzinfo=timezone.utc)]
    service = SchedulerStatusService(
        status_path=tmp_path / "scheduler_status.json",
        now_provider=lambda: current_time[0],
        stale_after_seconds=90,
    )
    service.mark_started(schedule_time="18:00")
    current_time[0] += timedelta(seconds=91)

    result = service.get_status()

    assert result["status"] == "offline"
    assert result["online"] is False
    assert result["heartbeat_age_seconds"] == 91
    assert result["message"] == "调度器心跳已超时"


def test_graceful_stop_is_reported_immediately(tmp_path):
    service = SchedulerStatusService(status_path=tmp_path / "scheduler_status.json")
    service.mark_started(schedule_time="18:00")
    service.mark_stopped()

    result = service.get_status()

    assert result["status"] == "offline"
    assert result["online"] is False
    assert result["stopped_at"] is not None
    assert result["message"] == "独立 schedule 服务已停止"


def test_malformed_status_file_reports_unavailable(tmp_path):
    status_path = tmp_path / "scheduler_status.json"
    status_path.write_text("{broken", encoding="utf-8")
    service = SchedulerStatusService(status_path=status_path)

    result = service.get_status()

    assert result["status"] == "unavailable"
    assert result["online"] is False


def test_status_path_access_error_reports_unavailable(tmp_path):
    service = SchedulerStatusService(status_path=tmp_path / "scheduler_status.json")

    with patch("pathlib.Path.exists", side_effect=OSError("permission denied")):
        result = service.get_status()

    assert result["status"] == "unavailable"
    assert result["online"] is False


def test_structurally_incomplete_status_file_reports_unavailable(tmp_path):
    status_path = tmp_path / "scheduler_status.json"
    status_path.write_text(json.dumps({"state": "running"}), encoding="utf-8")
    service = SchedulerStatusService(status_path=status_path)

    result = service.get_status()

    assert result["status"] == "unavailable"
    assert result["online"] is False
    assert result["heartbeat_at"] is None


def test_empty_object_status_file_reports_unavailable(tmp_path):
    status_path = tmp_path / "scheduler_status.json"
    status_path.write_text("{}", encoding="utf-8")
    service = SchedulerStatusService(status_path=status_path)

    result = service.get_status()

    assert result["status"] == "unavailable"
    assert result["online"] is False


def test_invalid_minute_check_structure_reports_unavailable(tmp_path):
    status_path = tmp_path / "scheduler_status.json"
    service = SchedulerStatusService(status_path=status_path)
    service.mark_started(schedule_time="18:00")
    payload = json.loads(status_path.read_text(encoding="utf-8"))
    payload["minute_check"] = {
        "status": "completed",
        "started_at": payload["started_at"],
        "completed_at": None,
        "markets": ["cn"],
        "evaluated": 1,
        "triggered": 0,
        "error_count": 0,
        "notification_sent": False,
        "message": None,
    }
    status_path.write_text(json.dumps(payload), encoding="utf-8")

    result = service.get_status()

    assert result["status"] == "unavailable"
    assert result["minute_check"] is None


def test_restart_replaces_instance_but_keeps_last_minute_result(tmp_path):
    status_path = tmp_path / "scheduler_status.json"
    first = SchedulerStatusService(status_path=status_path)
    first.mark_started(schedule_time="18:00")
    first.mark_minute_check_completed(
        status="skipped_market_closed",
        markets=[],
        message="当前没有开市市场",
    )
    first_instance = json.loads(status_path.read_text(encoding="utf-8"))["instance_id"]

    second = SchedulerStatusService(status_path=status_path)
    second.mark_started(schedule_time="09:30")
    result = second.get_status()

    assert result["status"] == "online"
    assert result["instance_id"] != first_instance
    assert result["schedule_time"] == "09:30"
    assert result["minute_check"]["status"] == "skipped_market_closed"


def test_old_instance_cannot_overwrite_new_instance_status(tmp_path):
    status_path = tmp_path / "scheduler_status.json"
    first = SchedulerStatusService(status_path=status_path)
    first.mark_started(schedule_time="18:00")
    first.mark_minute_check_completed(
        status="skipped_market_closed",
        markets=[],
        message="旧实例结果",
    )

    second = SchedulerStatusService(status_path=status_path)
    second.mark_started(schedule_time="09:30")
    second.mark_minute_check_completed(
        status="completed",
        markets=["us"],
        evaluated=1,
    )
    expected = json.loads(status_path.read_text(encoding="utf-8"))

    first.mark_heartbeat(schedule_time="20:00")
    first.mark_minute_check_started(markets=["cn"])
    first.mark_minute_check_completed(status="failed", markets=["cn"])
    first.mark_stopped()

    actual = json.loads(status_path.read_text(encoding="utf-8"))
    assert actual == expected
    result = second.get_status()
    assert result["status"] == "online"
    assert result["schedule_time"] == "09:30"
    assert result["minute_check"]["status"] == "completed"
    assert result["minute_check"]["markets"] == ["us"]


def test_restart_serializes_old_update_before_new_instance_start(tmp_path):
    status_path = tmp_path / "scheduler_status.json"
    first = SchedulerStatusService(status_path=status_path)
    first.mark_started(schedule_time="18:00")

    old_read_started = threading.Event()
    allow_old_update = threading.Event()
    original_read = first._read_payload

    def pause_after_old_read(*, strict=False):
        payload = original_read(strict=strict)
        old_read_started.set()
        assert allow_old_update.wait(timeout=2)
        return payload

    first._read_payload = pause_after_old_read
    old_thread = threading.Thread(target=first.mark_heartbeat)
    old_thread.start()
    assert old_read_started.wait(timeout=2)

    second = SchedulerStatusService(status_path=status_path)
    new_start_completed = threading.Event()

    def start_new_instance():
        second.mark_started(schedule_time="09:30")
        new_start_completed.set()

    new_thread = threading.Thread(target=start_new_instance)
    new_thread.start()
    assert not new_start_completed.wait(timeout=0.1)

    allow_old_update.set()
    old_thread.join(timeout=2)
    new_thread.join(timeout=2)
    assert not old_thread.is_alive()
    assert not new_thread.is_alive()
    assert new_start_completed.is_set()

    expected = json.loads(status_path.read_text(encoding="utf-8"))
    assert expected["instance_id"] == second._instance_id
    assert expected["schedule_time"] == "09:30"

    first.mark_stopped()

    actual = json.loads(status_path.read_text(encoding="utf-8"))
    assert actual == expected
