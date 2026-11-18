"""云端班次接力回归：模拟 GitHub API，不触发真实工作流。"""

import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

import requests

import workflow_relay as relay
import test_recovery as recovery
from test_attendance import FakeClient


def at(hour, minute=0):
    return datetime(2026, 10, 13, hour, minute)


PLAN = [[at(10), at(12)], [at(14, 30), at(16, 30)], [at(18), at(20)]]
ENV = {"GITHUB_REPOSITORY": "test-owner/test-repo", "GITHUB_REF": "refs/heads/main",
       "GITHUB_TOKEN": "test-token"}


class TestRelay(unittest.TestCase):
    def setUp(self):
        self.post = self.enterContext(patch.object(relay.requests, "post"))
        self.post.side_effect = AssertionError("测试禁止访问 GitHub")
        self.get = self.enterContext(patch.object(relay.requests, "get"))
        # 默认「历史健康」，让这些用例专注接力本身；熔断另有专门用例。
        self.get.return_value = Mock(status_code=200, json=lambda: {"workflow_runs": []})
        self.sleep = self.enterContext(patch.object(relay.time, "sleep"))
        self.enterContext(patch.object(relay, "log"))
        self.enterContext(patch.object(relay, "get_config", return_value={"holiday_attendance": True}))
        self.resolve = self.enterContext(patch.object(relay, "resolve_today_plan", return_value=(PLAN, None)))

    def test_morning_worker_queues_successor_without_waiting_for_cron(self):
        self.post.side_effect = None
        self.post.return_value = Mock(status_code=204)
        self.assertTrue(relay.enqueue_next_run(environ=ENV, now=at(11, 44)))
        self.post.assert_called_once()
        self.assertEqual(self.post.call_args.kwargs["json"], {"ref": "main"})
        self.assertIn("/test-owner/test-repo/actions/workflows/main.yml/dispatches",
                      self.post.call_args.args[0])

    def test_after_noon_still_queues_afternoon_worker(self):
        self.post.side_effect = None
        self.post.return_value = Mock(status_code=204)
        self.assertTrue(relay.enqueue_next_run(environ=ENV, now=at(12)))

    def test_successor_covers_afternoon_without_another_cron_event(self):
        self.post.side_effect = None
        self.post.return_value = Mock(status_code=204)
        self.assertTrue(relay.enqueue_next_run(environ=ENV, now=at(12)))
        # 创建的下一轮立即接手，虚拟时间走完整个午休与下午班，不再注入 cron。
        client = FakeClient(signed_in=False)
        result, clock, _ = recovery.TestMainRecovery().run_main(
            at(12), extra=("--finish-window",), client=client, plan=PLAN,
        )
        self.assertEqual(result, 0)
        self.assertEqual(client.actions, ["签到", "签退"])
        self.assertEqual(clock.now, at(16, 30))
        self.assertFalse(client.signed_in)

    def test_last_shift_end_stops_relay_and_does_not_create_an_overnight_loop(self):
        self.assertFalse(relay.enqueue_next_run(environ=ENV, now=at(20)))
        self.post.assert_not_called()

    def test_day_off_does_not_queue(self):
        self.resolve.return_value = ([], "今天是休息日")
        self.assertFalse(relay.enqueue_next_run(environ=ENV, now=at(12)))
        self.post.assert_not_called()

    def test_empty_special_schedule_does_not_queue(self):
        self.resolve.return_value = ([], None)
        self.assertFalse(relay.enqueue_next_run(environ=ENV, now=at(12)))
        self.post.assert_not_called()

    def test_non_main_branch_does_not_create_main_attendance(self):
        self.assertFalse(relay.enqueue_next_run(environ={**ENV, "GITHUB_REF": "refs/heads/test"}, now=at(12)))
        self.post.assert_not_called()

    def test_missing_token_cannot_claim_handover_is_ready(self):
        with self.assertRaises(RuntimeError):
            relay.enqueue_next_run(environ={**ENV, "GITHUB_TOKEN": ""}, now=at(12))
        self.post.assert_not_called()

    def test_api_permission_failure_is_visible(self):
        self.post.side_effect = None
        self.post.return_value = Mock(status_code=403)
        with self.assertRaisesRegex(RuntimeError, "403"):
            relay.enqueue_next_run(environ=ENV, now=at(12))
        self.assertEqual(self.post.call_count, 1)

    def test_transient_api_failure_retries(self):
        self.post.side_effect = [requests.ReadTimeout("timeout"), Mock(status_code=503), Mock(status_code=204)]
        self.assertTrue(relay.enqueue_next_run(environ=ENV, now=at(12)))
        self.assertEqual(self.post.call_count, 3)

    def test_persistent_api_failure_is_visible(self):
        self.post.side_effect = None
        self.post.return_value = Mock(status_code=503)
        with self.assertRaisesRegex(RuntimeError, "503"):
            relay.enqueue_next_run(environ=ENV, now=at(12))
        self.assertEqual(self.post.call_count, 3)


class TestRelayCircuitBreaker(unittest.TestCase):
    """连续失败必须停止接力，否则一次凭证错误会通宵空转。"""

    def setUp(self):
        self.post = self.enterContext(patch.object(relay.requests, "post"))
        self.get = self.enterContext(patch.object(relay.requests, "get"))
        self.enterContext(patch.object(relay.time, "sleep"))
        self.enterContext(patch.object(relay, "log"))
        self.enterContext(patch.object(relay, "get_config", return_value={"holiday_attendance": True}))
        self.enterContext(patch.object(relay, "resolve_today_plan", return_value=(PLAN, None)))
        self.post.return_value = Mock(status_code=204)
        self.post.side_effect = None

    def history(self, *conclusions):
        runs = [{"status": "completed", "conclusion": c} for c in reversed(conclusions)]
        self.get.return_value = Mock(status_code=200, json=lambda: {"workflow_runs": runs})

    def test_consecutive_failures_stop_queuing_the_next_run(self):
        self.history("failure", "failure", "failure")
        self.assertFalse(relay.enqueue_next_run(environ=ENV, now=at(12)))
        self.post.assert_not_called()

    def test_a_single_failure_does_not_stop_the_relay(self):
        self.history("failure", "success", "success")
        self.assertTrue(relay.enqueue_next_run(environ=ENV, now=at(12)))
        self.post.assert_called_once()

    def test_in_progress_current_run_is_ignored(self):
        runs = [{"status": "in_progress", "conclusion": None},
                {"status": "completed", "conclusion": "success"},
                {"status": "completed", "conclusion": "success"},
                {"status": "completed", "conclusion": "failure"}]
        self.get.return_value = Mock(status_code=200, json=lambda: {"workflow_runs": runs})
        self.assertTrue(relay.enqueue_next_run(environ=ENV, now=at(12)))

    def test_api_error_does_not_silently_stop_attendance(self):
        self.get.return_value = Mock(status_code=403, json=lambda: {})
        self.assertTrue(relay.enqueue_next_run(environ=ENV, now=at(12)))
        self.post.assert_called_once()

    def test_network_error_does_not_silently_stop_attendance(self):
        self.get.side_effect = requests.ReadTimeout("timeout")
        self.assertTrue(relay.enqueue_next_run(environ=ENV, now=at(12)))
        self.post.assert_called_once()


class TestWorkflowRelayConfig(unittest.TestCase):
    def test_successor_is_queued_before_attendance_starts(self):
        text = (Path(__file__).resolve().parent.parent / ".github/workflows/main.yml").read_text(encoding="utf-8")
        self.assertIn("actions: write", text)
        self.assertIn("GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}", text)
        self.assertLess(text.index("python workflow_relay.py"), text.index("python attendance.py"))


if __name__ == "__main__":
    unittest.main()
