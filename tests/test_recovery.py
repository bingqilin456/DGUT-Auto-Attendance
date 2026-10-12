"""主循环故障恢复测试：虚拟时间和客户端，不联网、不实际等待。"""

import unittest
from datetime import timedelta
from unittest.mock import Mock, patch

import attendance as att
import requests
from dgut_client import DgutError, XgxtClient
from test_attendance import FakeClient, PLAN, at
from test_dgut_client import CLOSED, EMPTY, OPEN, response


class VirtualClock:
    def __init__(self, now):
        self.now = now
        self.elapsed = 0.0
        self.sleeps = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += timedelta(seconds=seconds)
        self.elapsed += seconds


class TestMainRecovery(unittest.TestCase):
    def run_main(self, now, outcomes=None, extra=(), client=None, plan=PLAN):
        clock = VirtualClock(now)
        client = client or FakeClient(signed_in=False)
        client.login = Mock()
        with (
            patch.object(att, "beijing_naive", side_effect=lambda: clock.now),
            patch.object(att.time, "monotonic", side_effect=lambda: clock.elapsed),
            patch.object(att.time, "sleep", side_effect=clock.sleep),
            patch.object(att, "get_config", return_value={"holiday_attendance": True}),
            patch.object(att, "resolve_today_plan", return_value=(plan, None)),
            patch.object(att, "XgxtClient", return_value=client),
            patch.object(att, "log"),
        ):
            args = ["-U", "test-user", "-P", "test-password", *extra]
            if outcomes is None:
                result = att.main(args)
                calls = None
            else:
                with patch.object(att, "reconcile_once", side_effect=outcomes) as reconcile:
                    result = att.main(args)
                    calls = reconcile.call_count
        return result, clock, calls

    def test_once_failure_is_visible(self):
        result, _, _ = self.run_main(at(9), DgutError("network unavailable"), ("--once",))
        self.assertEqual(result, 1)

    def test_final_checkout_retries_until_success(self):
        result, clock, calls = self.run_main(
            at(21), [DgutError("timeout"), DgutError("timeout"), None],
            ("--retry-interval", "5", "--cleanup-seconds", "30"),
        )
        self.assertEqual(result, 0)
        self.assertEqual(calls, 3)
        self.assertEqual(clock.sleeps, [5, 5])

    def test_persistent_final_failure_exits_nonzero(self):
        result, clock, _ = self.run_main(
            at(21), DgutError("timeout"), ("--retry-interval", "5", "--cleanup-seconds", "10"),
        )
        self.assertEqual(result, 1)
        self.assertGreaterEqual(clock.elapsed, 10)
        self.assertLessEqual(clock.elapsed, 15)

    def test_finish_window_checks_out_before_handover(self):
        client = FakeClient(signed_in=True, started_at=at(8, 30))
        result, clock, _ = self.run_main(at(11, 59, 50), extra=("--finish-window",), client=client)
        self.assertEqual(result, 0)
        self.assertEqual(client.actions, ["签退"])
        self.assertEqual(clock.now, at(12))

    def test_finish_window_retries_checkout_at_first_end(self):
        result, clock, calls = self.run_main(
            at(11, 59, 50), [None, DgutError("timeout"), None],
            ("--finish-window", "--retry-interval", "5"),
        )
        self.assertEqual(result, 0)
        self.assertEqual(calls, 3)
        self.assertEqual(clock.now, at(12, 0, 5))

    def test_finish_window_preserves_continuous_adjacent_shifts(self):
        client = FakeClient(signed_in=True, started_at=at(8, 30))
        plan = [[at(8, 30), at(12)], [at(12), at(13)]]
        result, clock, _ = self.run_main(at(11, 59, 50), extra=("--finish-window",), client=client, plan=plan)
        self.assertEqual(result, 0)
        self.assertEqual(client.actions, ["签退"])
        self.assertEqual(clock.now, at(13))

    def test_unknown_open_record_is_preserved_but_not_reported_as_checkout_success(self):
        client = FakeClient(signed_in=True, started_at=None)
        result, _, _ = self.run_main(at(21), extra=("--finish-window", "--retry-interval", "5",
                                                 "--cleanup-seconds", "10"), client=client)
        self.assertEqual(client.actions, [])
        self.assertEqual(result, 1)

    def test_regular_failure_retries_before_normal_interval(self):
        result, clock, calls = self.run_main(
            at(20, 59, 50), [DgutError("timeout"), None, None], ("--retry-interval", "5",),
        )
        self.assertEqual(result, 0)
        self.assertEqual(calls, 3)
        self.assertEqual(clock.sleeps, [5, 5])

    def test_read_crossing_last_boundary_gets_fresh_reconciliation(self):
        clock = VirtualClock(at(20, 59, 59))
        client = FakeClient(signed_in=True, started_at=at(18))
        client.login = Mock()
        real_reconcile = att.reconcile_once
        calls = []

        def reconcile(*args, **kwargs):
            calls.append(clock.now)
            if len(calls) == 1:
                # 模拟本轮只读状态还没发现边界，返回时已经过了签退时间。
                clock.now += timedelta(seconds=2)
                return None
            return real_reconcile(*args, **kwargs)

        with (
            patch.object(att, "beijing_naive", side_effect=lambda: clock.now),
            patch.object(att.time, "monotonic", side_effect=lambda: clock.elapsed),
            patch.object(att.time, "sleep", side_effect=clock.sleep),
            patch.object(att, "get_config", return_value={"holiday_attendance": True}),
            patch.object(att, "resolve_today_plan", return_value=(PLAN, None)),
            patch.object(att, "XgxtClient", return_value=client),
            patch.object(att, "log"),
            patch.object(att, "reconcile_once", side_effect=reconcile),
        ):
            self.assertEqual(att.main(["-U", "test-user", "-P", "test-password"]), 0)
        self.assertEqual(client.actions, ["签退"])
        self.assertEqual(len(calls), 2)


class TestAmbiguousActionRecovery(unittest.TestCase):
    def test_recovery_does_not_close_a_replacement_current_shift_record(self):
        client = XgxtClient("test-user", "test-password")
        client.logged_in = True
        old = OPEN.replace("2026-10-12", "2026-10-08")
        current = old.replace("14:30:04", "18:05:00").replace("'888'", "'999'")
        closed = CLOSED.replace("2026-10-12", "2026-10-08")
        with (
            patch("requests.sessions.Session.request", side_effect=[
                response(old), response(old),
                response(current), response(current), response(current),
                response(closed), response(closed), response(closed),
            ]) as http,
            patch.object(att, "log"),
        ):
            with self.assertRaises(DgutError):
                att.reconcile_once(client, "38605", PLAN, clock=lambda: at(18, 37))
        actions = [dict(call.kwargs.get("data") or []).get("action_name")
                   for call in http.call_args_list if call.args[0] == "POST"]
        self.assertNotIn("endWork", actions)

    def test_completed_checkout_with_lost_response_is_not_submitted_twice(self):
        client = XgxtClient("test-user", "test-password")
        client.logged_in = True
        opened = OPEN.replace("2026-10-12", "2026-10-08")
        closed = CLOSED.replace("2026-10-12", "2026-10-08")
        with (
            patch("requests.sessions.Session.request", side_effect=[
                response(opened), response(opened),  # 第一轮对账读取状态
                response(opened), response(opened), response(opened),  # 动作前准备
                requests.ReadTimeout("签退成功，但响应丢失"),
                response(closed), response(closed),  # 下一轮先读真实状态
            ]) as http,
            patch.object(att, "log"),
        ):
            with self.assertRaises(DgutError):
                att.reconcile_once(client, "38605", PLAN, clock=lambda: at(17))
            self.assertIsNone(att.reconcile_once(client, "38605", PLAN, clock=lambda: at(17)))
        actions = [dict(call.kwargs.get("data") or []).get("action_name")
                   for call in http.call_args_list if call.args[0] == "POST"]
        self.assertEqual(actions.count("endWork"), 1)

    def test_completed_checkin_with_lost_response_is_not_submitted_twice(self):
        client = XgxtClient("test-user", "test-password")
        client.logged_in = True
        opened = OPEN.replace("2026-10-12", "2026-10-08")
        with (
            patch("requests.sessions.Session.request", side_effect=[
                response(EMPTY), response(EMPTY),
                response(EMPTY), response(EMPTY), response(EMPTY),
                requests.ReadTimeout("签到成功，但响应丢失"),
                response(opened), response(opened),
            ]) as http,
            patch.object(att, "log"),
        ):
            with self.assertRaises(DgutError):
                att.reconcile_once(client, "38605", PLAN, clock=lambda: at(15))
            self.assertIsNone(att.reconcile_once(client, "38605", PLAN, clock=lambda: at(15)))
        actions = [dict(call.kwargs.get("data") or []).get("action_name")
                   for call in http.call_args_list if call.args[0] == "POST"]
        self.assertEqual(actions.count("beginWork"), 1)


if __name__ == "__main__":
    unittest.main()
