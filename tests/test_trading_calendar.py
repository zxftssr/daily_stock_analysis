# -*- coding: utf-8 -*-
"""Regression tests for effective trading date resolution."""

from datetime import date, datetime, time, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

from src.core import trading_calendar


class _FakeCalendar:
    def __init__(self, sessions, close_hour: int, tz_name: str):
        self._sessions = sorted(sessions)
        self._close_hour = close_hour
        self._tz_name = tz_name

    def is_session(self, check_date: date) -> bool:
        return check_date in self._sessions

    def date_to_session(self, check_date: date, direction: str = "previous") -> pd.Timestamp:
        if direction == "previous":
            candidates = [d for d in self._sessions if d <= check_date]
        elif direction == "next":
            candidates = [d for d in self._sessions if d >= check_date]
        else:
            raise ValueError(f"unsupported direction: {direction}")

        if not candidates:
            raise ValueError(f"no session for {check_date} ({direction})")
        return pd.Timestamp(candidates[-1] if direction == "previous" else candidates[0])

    def previous_session(self, session: pd.Timestamp) -> pd.Timestamp:
        session_date = session.date()
        index = self._sessions.index(session_date)
        if index == 0:
            raise ValueError("no previous session")
        return pd.Timestamp(self._sessions[index - 1])

    def session_close(self, session: pd.Timestamp) -> pd.Timestamp:
        local_close = datetime.combine(
            session.date(),
            time(self._close_hour, 0),
            tzinfo=ZoneInfo(self._tz_name),
        )
        return pd.Timestamp(local_close).tz_convert("UTC")


class _FakeMinuteCalendar:
    def __init__(self, *, is_open: bool):
        self.is_open = is_open
        self.minutes = []

    def is_open_on_minute(self, minute: datetime) -> bool:
        self.minutes.append(minute)
        return self.is_open


class EffectiveTradingDateTestCase(unittest.TestCase):
    def test_market_inference_handles_index_aliases_and_suffixes(self):
        expected = {
            "HSI": "hk",
            "HSCEI": "hk",
            "HSTECH": "hk",
            "AAPL.US": "us",
            "000001.SH": "cn",
        }
        for symbol, market in expected.items():
            with self.subTest(symbol=symbol):
                self.assertEqual(trading_calendar.get_market_for_stock(symbol), market)

    def test_weekend_returns_previous_session(self):
        fake_calendar = _FakeCalendar(
            sessions=[date(2026, 3, 26), date(2026, 3, 27)],
            close_hour=15,
            tz_name="Asia/Shanghai",
        )
        current_time = datetime(2026, 3, 28, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

        with patch.object(trading_calendar, "_XCALS_AVAILABLE", True), patch.object(
            trading_calendar,
            "xcals",
            SimpleNamespace(get_calendar=lambda _ex: fake_calendar),
            create=True,
        ):
            result = trading_calendar.get_effective_trading_date("cn", current_time=current_time)

        self.assertEqual(result, date(2026, 3, 27))

    def test_holiday_returns_previous_session(self):
        fake_calendar = _FakeCalendar(
            sessions=[date(2025, 12, 31), date(2026, 1, 5)],
            close_hour=15,
            tz_name="Asia/Shanghai",
        )
        current_time = datetime(2026, 1, 1, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

        with patch.object(trading_calendar, "_XCALS_AVAILABLE", True), patch.object(
            trading_calendar,
            "xcals",
            SimpleNamespace(get_calendar=lambda _ex: fake_calendar),
            create=True,
        ):
            result = trading_calendar.get_effective_trading_date("cn", current_time=current_time)

        self.assertEqual(result, date(2025, 12, 31))

    def test_intraday_returns_previous_completed_session(self):
        fake_calendar = _FakeCalendar(
            sessions=[date(2026, 3, 26), date(2026, 3, 27)],
            close_hour=16,
            tz_name="America/New_York",
        )
        current_time = datetime(
            2026,
            3,
            27,
            15,
            59,
            tzinfo=ZoneInfo("America/New_York"),
        )

        with patch.object(trading_calendar, "_XCALS_AVAILABLE", True), patch.object(
            trading_calendar,
            "xcals",
            SimpleNamespace(get_calendar=lambda _ex: fake_calendar),
            create=True,
        ):
            result = trading_calendar.get_effective_trading_date("us", current_time=current_time)

        self.assertEqual(result, date(2026, 3, 26))

    def test_after_close_returns_current_session(self):
        fake_calendar = _FakeCalendar(
            sessions=[date(2026, 3, 26), date(2026, 3, 27)],
            close_hour=16,
            tz_name="America/New_York",
        )
        current_time = datetime(
            2026,
            3,
            27,
            16,
            1,
            tzinfo=ZoneInfo("America/New_York"),
        )

        with patch.object(trading_calendar, "_XCALS_AVAILABLE", True), patch.object(
            trading_calendar,
            "xcals",
            SimpleNamespace(get_calendar=lambda _ex: fake_calendar),
            create=True,
        ):
            result = trading_calendar.get_effective_trading_date("us", current_time=current_time)

        self.assertEqual(result, date(2026, 3, 27))

    def test_market_timezone_controls_cross_timezone_resolution(self):
        fake_calendar = _FakeCalendar(
            sessions=[date(2026, 3, 25), date(2026, 3, 26), date(2026, 3, 27)],
            close_hour=16,
            tz_name="America/New_York",
        )
        current_time = datetime(2026, 3, 27, 1, 0, tzinfo=timezone.utc)

        with patch.object(trading_calendar, "_XCALS_AVAILABLE", True), patch.object(
            trading_calendar,
            "xcals",
            SimpleNamespace(get_calendar=lambda _ex: fake_calendar),
            create=True,
        ):
            result = trading_calendar.get_effective_trading_date("us", current_time=current_time)

        self.assertEqual(result, date(2026, 3, 26))

    def test_calendar_error_falls_back_to_market_local_date(self):
        current_time = datetime(2026, 3, 27, 18, 0, tzinfo=timezone.utc)

        with patch.object(trading_calendar, "_XCALS_AVAILABLE", True), patch.object(
            trading_calendar,
            "xcals",
            SimpleNamespace(get_calendar=lambda _ex: (_ for _ in ()).throw(RuntimeError("boom"))),
            create=True,
        ):
            result = trading_calendar.get_effective_trading_date("hk", current_time=current_time)

        self.assertEqual(result, date(2026, 3, 28))


class LiveMarketSessionTestCase(unittest.TestCase):
    def test_exchange_calendar_controls_live_session(self):
        fake_calendar = _FakeMinuteCalendar(is_open=True)
        current_time = datetime(2026, 8, 28, 2, 15, tzinfo=timezone.utc)

        with patch.object(trading_calendar, "_XCALS_AVAILABLE", True), patch.object(
            trading_calendar,
            "xcals",
            SimpleNamespace(get_calendar=lambda _ex: fake_calendar),
            create=True,
        ):
            result = trading_calendar.is_market_trading_now(
                "cn",
                current_time=current_time,
            )

        self.assertTrue(result)
        self.assertEqual(fake_calendar.minutes[0].hour, 10)
        self.assertEqual(fake_calendar.minutes[0].minute, 15)

    def test_missing_calendar_fails_closed(self):
        with patch.object(trading_calendar, "_XCALS_AVAILABLE", False):
            self.assertFalse(trading_calendar.is_market_trading_now(
                "cn",
                current_time=datetime(2026, 8, 28, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
            ))

    def test_calendar_error_fails_closed_during_weekday_session(self):
        current_time = datetime(2027, 1, 1, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

        with patch.object(trading_calendar, "_XCALS_AVAILABLE", True), patch.object(
            trading_calendar,
            "xcals",
            SimpleNamespace(
                get_calendar=lambda _ex: (_ for _ in ()).throw(RuntimeError("out of range"))
            ),
            create=True,
        ):
            result = trading_calendar.is_market_trading_now(
                "cn",
                current_time=current_time,
            )

        self.assertFalse(result)

    def test_open_market_set_uses_one_shared_instant(self):
        current_time = datetime(2026, 8, 28, 2, 15, tzinfo=timezone.utc)

        with patch.object(
            trading_calendar,
            "is_market_trading_now",
            side_effect=lambda market, current_time=None: market in {"cn", "hk"},
        ) as is_open:
            result = trading_calendar.get_markets_open_now(current_time=current_time)

        self.assertEqual(result, {"cn", "hk"})
        self.assertEqual(is_open.call_count, 3)
        self.assertTrue(all(
            item.kwargs["current_time"] is current_time
            for item in is_open.call_args_list
        ))


class ComputeEffectiveRegionTestCase(unittest.TestCase):
    """Regression tests for compute_effective_region subset logic."""

    def test_both_all_open_returns_comma_joined_three(self):
        result = trading_calendar.compute_effective_region("both", {"cn", "hk", "us"})
        self.assertEqual(result, "cn,hk,us")

    def test_both_cn_us_open_returns_comma_joined_two(self):
        result = trading_calendar.compute_effective_region("both", {"cn", "us"})
        self.assertEqual(result, "cn,us")

    def test_both_cn_hk_open_returns_comma_joined_two(self):
        result = trading_calendar.compute_effective_region("both", {"cn", "hk"})
        self.assertEqual(result, "cn,hk")

    def test_both_single_market_open_returns_single(self):
        result = trading_calendar.compute_effective_region("both", {"us"})
        self.assertEqual(result, "us")

    def test_both_no_market_open_returns_empty(self):
        result = trading_calendar.compute_effective_region("both", set())
        self.assertEqual(result, "")

    def test_single_region_open(self):
        self.assertEqual(trading_calendar.compute_effective_region("hk", {"cn", "hk", "us"}), "hk")

    def test_single_region_closed(self):
        self.assertEqual(trading_calendar.compute_effective_region("hk", {"cn", "us"}), "")

    def test_invalid_region_defaults_to_cn(self):
        result = trading_calendar.compute_effective_region("invalid", {"cn"})
        self.assertEqual(result, "cn")


if __name__ == "__main__":
    unittest.main()
