# -*- coding: utf-8 -*-
"""Cross-process scheduler heartbeat and minute-plan check status."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import threading
from typing import Any, Callable, Dict, Iterator, Optional
import uuid

from src.config import get_config

logger = logging.getLogger(__name__)

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows only
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX only
    msvcrt = None

SCHEDULER_STATUS_VERSION = 1
SCHEDULER_HEARTBEAT_STALE_SECONDS = 90
_VALID_STATES = {"running", "stopped"}
_VALID_MINUTE_STATUSES = {
    "running",
    "completed",
    "partial",
    "failed",
    "skipped_market_closed",
}
_VALID_MARKETS = {"cn", "hk", "us"}


class SchedulerStatusService:
    """Persist scheduler liveness beside the shared application database."""

    def __init__(
        self,
        *,
        status_path: Optional[Path] = None,
        now_provider: Optional[Callable[[], datetime]] = None,
        stale_after_seconds: int = SCHEDULER_HEARTBEAT_STALE_SECONDS,
    ) -> None:
        self.status_path = status_path or self._default_status_path()
        self._now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self.stale_after_seconds = max(1, int(stale_after_seconds))
        self._lock = threading.RLock()
        self._instance_id = uuid.uuid4().hex

    @staticmethod
    def _default_status_path() -> Path:
        raw_path = str(get_config().database_path or "").strip()
        if raw_path.startswith("sqlite:///"):
            raw_path = raw_path[len("sqlite:///"):]
        if not raw_path or raw_path == ":memory:":
            raw_path = "./data/stock_analysis.db"
        return Path(raw_path).expanduser().resolve().parent / "scheduler_status.json"

    def mark_started(self, *, schedule_time: str) -> None:
        try:
            with self._lock, self._interprocess_lock():
                now = self._now_iso()
                previous = self._read_payload()
                minute_check = previous.get("minute_check")
                if (
                    minute_check is not None
                    and not self._is_valid_minute_check(minute_check)
                ):
                    minute_check = None
                payload = {
                    "version": SCHEDULER_STATUS_VERSION,
                    "instance_id": self._instance_id,
                    "pid": os.getpid(),
                    "state": "running",
                    "started_at": now,
                    "heartbeat_at": now,
                    "stopped_at": None,
                    "schedule_time": self._normalize_schedule_time(schedule_time),
                    "minute_check": minute_check,
                }
                self._write_payload(payload)
        except (OSError, ValueError, TypeError) as exc:
            logger.warning("写入调度器状态失败（继续运行）: %s", exc)

    def mark_heartbeat(self, *, schedule_time: Optional[str] = None) -> None:
        def update(payload: Dict[str, Any]) -> None:
            payload.update({
                "version": SCHEDULER_STATUS_VERSION,
                "instance_id": self._instance_id,
                "pid": os.getpid(),
                "state": "running",
                "heartbeat_at": self._now_iso(),
                "stopped_at": None,
            })
            if schedule_time is not None:
                normalized_schedule_time = self._normalize_schedule_time(schedule_time)
                if normalized_schedule_time is not None:
                    payload["schedule_time"] = normalized_schedule_time
            payload.setdefault("started_at", payload["heartbeat_at"])

        self._safe_update(update)

    def mark_stopped(self) -> None:
        def update(payload: Dict[str, Any]) -> None:
            now = self._now_iso()
            payload.update({
                "version": SCHEDULER_STATUS_VERSION,
                "instance_id": self._instance_id,
                "pid": os.getpid(),
                "state": "stopped",
                "heartbeat_at": now,
                "stopped_at": now,
            })

        self._safe_update(update)

    def mark_minute_check_started(self, *, markets: list[str]) -> None:
        def update(payload: Dict[str, Any]) -> None:
            payload["minute_check"] = {
                "status": "running",
                "started_at": self._now_iso(),
                "completed_at": None,
                "markets": sorted(set(markets)),
                "evaluated": 0,
                "triggered": 0,
                "error_count": 0,
                "notification_sent": False,
                "message": None,
            }

        self._safe_update(update)

    def mark_minute_check_completed(
        self,
        *,
        status: str,
        markets: list[str],
        evaluated: int = 0,
        triggered: int = 0,
        error_count: int = 0,
        notification_sent: bool = False,
        message: Optional[str] = None,
    ) -> None:
        def update(payload: Dict[str, Any]) -> None:
            previous = payload.get("minute_check") or {}
            payload["minute_check"] = {
                "status": status,
                "started_at": previous.get("started_at") or self._now_iso(),
                "completed_at": self._now_iso(),
                "markets": sorted(set(markets)),
                "evaluated": max(0, int(evaluated)),
                "triggered": max(0, int(triggered)),
                "error_count": max(0, int(error_count)),
                "notification_sent": bool(notification_sent),
                "message": str(message).strip() if message else None,
            }

        self._safe_update(update)

    def get_status(self) -> Dict[str, Any]:
        try:
            if not self.status_path.exists():
                return self._empty_status(
                    status="not_started",
                    message="尚未检测到独立 schedule 服务",
                )
            payload = self._read_payload(strict=True)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("读取调度器状态失败: %s", exc)
            return self._empty_status(
                status="unavailable",
                message="调度器状态文件暂时无法读取",
            )
        if not self._is_valid_payload(payload):
            logger.warning("调度器状态文件结构无效: %s", self.status_path)
            return self._empty_status(
                status="unavailable",
                message="调度器状态文件内容无效",
            )

        heartbeat_at = self._parse_datetime(payload.get("heartbeat_at"))
        age_seconds = None
        if heartbeat_at is not None:
            age_seconds = max(0, int((self._now() - heartbeat_at).total_seconds()))
        online = bool(
            payload.get("state") == "running"
            and age_seconds is not None
            and age_seconds <= self.stale_after_seconds
        )
        status = "online" if online else "offline"
        if online:
            message = "独立 schedule 服务运行中"
        elif payload.get("state") == "stopped":
            message = "独立 schedule 服务已停止"
        else:
            message = "调度器心跳已超时"
        return {
            "status": status,
            "online": online,
            "message": message,
            "stale_after_seconds": self.stale_after_seconds,
            "heartbeat_age_seconds": age_seconds,
            "instance_id": payload.get("instance_id"),
            "pid": payload.get("pid"),
            "started_at": payload.get("started_at"),
            "heartbeat_at": payload.get("heartbeat_at"),
            "stopped_at": payload.get("stopped_at"),
            "schedule_time": payload.get("schedule_time"),
            "minute_check": payload.get("minute_check"),
        }

    def _empty_status(self, *, status: str, message: str) -> Dict[str, Any]:
        return {
            "status": status,
            "online": False,
            "message": message,
            "stale_after_seconds": self.stale_after_seconds,
            "heartbeat_age_seconds": None,
            "instance_id": None,
            "pid": None,
            "started_at": None,
            "heartbeat_at": None,
            "stopped_at": None,
            "schedule_time": None,
            "minute_check": None,
        }

    def _safe_update(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        try:
            with self._lock, self._interprocess_lock():
                payload = self._read_payload()
                if (
                    not self._is_valid_payload(payload)
                    or payload.get("instance_id") != self._instance_id
                ):
                    logger.debug(
                        "跳过非当前调度器实例的状态更新: current=%s file=%s",
                        self._instance_id,
                        payload.get("instance_id"),
                    )
                    return
                callback(payload)
                payload.setdefault("started_at", payload.get("heartbeat_at"))
                payload.setdefault("schedule_time", None)
                payload.setdefault("minute_check", None)
                if not self._is_valid_payload(payload):
                    raise ValueError("生成的调度器状态结构无效")
                self._write_payload(payload)
        except (OSError, ValueError, TypeError) as exc:
            logger.warning("写入调度器状态失败（继续运行）: %s", exc)

    @contextmanager
    def _interprocess_lock(self) -> Iterator[None]:
        """Serialize read-check-write updates across scheduler processes."""
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.status_path.with_name(f"{self.status_path.name}.lock")
        with lock_path.open("a+b") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                return

            if msvcrt is None:  # pragma: no cover - unsupported platform
                raise OSError("当前平台不支持调度器状态文件锁")

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:  # pragma: no cover - Windows only
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

    def _read_payload(self, *, strict: bool = False) -> Dict[str, Any]:
        if not self.status_path.exists():
            return {}
        try:
            payload = json.loads(self.status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            if strict:
                raise
            return {}
        if not isinstance(payload, dict):
            if strict:
                raise ValueError("调度器状态必须是 JSON 对象")
            return {}
        return payload

    def _write_payload(self, payload: Dict[str, Any]) -> None:
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.status_path.with_name(
            f".{self.status_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        try:
            temp_path.write_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            os.replace(temp_path, self.status_path)
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _now(self) -> datetime:
        value = self._now_provider()
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _now_iso(self) -> str:
        return self._now().isoformat()

    @staticmethod
    def _normalize_schedule_time(value: Any) -> Optional[str]:
        candidate = str(value or "").strip()
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", candidate):
            return None
        return candidate

    @staticmethod
    def _parse_datetime(value: Any) -> Optional[datetime]:
        try:
            parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc)

    @classmethod
    def _is_valid_payload(cls, payload: Any) -> bool:
        if not isinstance(payload, dict):
            return False
        if payload.get("version") != SCHEDULER_STATUS_VERSION:
            return False
        instance_id = payload.get("instance_id")
        if not isinstance(instance_id, str) or not instance_id.strip():
            return False
        pid = payload.get("pid")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            return False
        state = payload.get("state")
        if state not in _VALID_STATES:
            return False
        if cls._parse_datetime(payload.get("started_at")) is None:
            return False
        if cls._parse_datetime(payload.get("heartbeat_at")) is None:
            return False
        stopped_at = payload.get("stopped_at")
        if state == "stopped":
            if cls._parse_datetime(stopped_at) is None:
                return False
        elif stopped_at is not None:
            return False
        schedule_time = payload.get("schedule_time")
        if schedule_time is not None:
            if not isinstance(schedule_time, str) or not re.fullmatch(
                r"(?:[01]\d|2[0-3]):[0-5]\d",
                schedule_time,
            ):
                return False
        minute_check = payload.get("minute_check")
        return minute_check is None or cls._is_valid_minute_check(minute_check)

    @classmethod
    def _is_valid_minute_check(cls, minute_check: Any) -> bool:
        if not isinstance(minute_check, dict):
            return False
        status = minute_check.get("status")
        if status not in _VALID_MINUTE_STATUSES:
            return False
        if cls._parse_datetime(minute_check.get("started_at")) is None:
            return False
        completed_at = minute_check.get("completed_at")
        if status == "running":
            if completed_at is not None:
                return False
        elif cls._parse_datetime(completed_at) is None:
            return False
        markets = minute_check.get("markets")
        if not isinstance(markets, list) or any(
            not isinstance(market, str) or market not in _VALID_MARKETS
            for market in markets
        ):
            return False
        for field in ("evaluated", "triggered", "error_count"):
            value = minute_check.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return False
        if not isinstance(minute_check.get("notification_sent"), bool):
            return False
        message = minute_check.get("message")
        return message is None or isinstance(message, str)
