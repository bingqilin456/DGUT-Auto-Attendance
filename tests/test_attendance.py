# -*- coding: utf-8 -*-
"""对账核心逻辑的离线单元测试：不联网、不打卡。

跑法::

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import attendance as att  # noqa: E402
from dgut_client import AttendanceState  # noqa: E402

REPO = Path(__file__).resolve().parent.parent


def at(hour: int, minute: int = 0, second: int = 0) -> datetime:
    """2026-10-08（星期四）的某个时刻。"""
    return datetime(2026, 10, 8, hour, minute, second)


PLAN = [
    [at(8, 30), at(12, 0)],
    [at(14, 30), at(17, 0)],
    [at(18, 0), at(21, 0)],
]


class FakeClock:
    """按顺序吐出预设时间的假时钟，最后一个会一直重复。"""

    def __init__(self, *times: datetime):
        self._times = list(times)

    def __call__(self) -> datetime:
        if len(self._times) > 1:
            return self._times.pop(0)
        return self._times[0]


class FakeClient:
    """只记录动作、不做网络的假客户端。"""

    def __init__(self, signed_in: bool = False, started_at: datetime | None = None):
        self.signed_in = signed_in
        self.started_at = started_at
        self.actions: list[str] = []

    def _state(self) -> AttendanceState:
        return AttendanceState(
            work_assignment_id="38605",
            work_assignment_name="计算机学院学生工作助理",
            signed_in=self.signed_in,
            can_sign_in=not self.signed_in,
            can_sign_out=self.signed_in,
            open_record_id="888" if self.signed_in else "",
            open_end_work_assignment_id="38605" if self.signed_in else "",
            open_started_at=self.started_at,
        )

    def get_attendance_state(self, work_assignment_id=None) -> AttendanceState:
        return self._state()

    def sign_in(self, work_assignment_id=None) -> AttendanceState:
        self.actions.append("签到")
        self.signed_in = True
        self.started_at = att.beijing_naive()
        return self._state()

    def sign_out(self, work_assignment_id=None) -> AttendanceState:
        self.actions.append("签退")
        self.signed_in = False
        self.started_at = None
        return self._state()


class LogCapture(unittest.TestCase):
    def setUp(self):
        self.logs: list[str] = []
        self._real_log = att.log
        att.log = self.logs.append

    def tearDown(self):
        att.log = self._real_log

    def assert_logged(self, needle: str):
        joined = "\n".join(self.logs)
        self.assertIn(needle, joined, f"日志里没有 {needle!r}：\n{joined}")


class TestDesiredSignedIn(unittest.TestCase):
    def test_boundaries(self):
        self.assertFalse(att.desired_signed_in(at(8, 29, 59), PLAN))
        self.assertTrue(att.desired_signed_in(at(8, 30), PLAN))
        self.assertTrue(att.desired_signed_in(at(11, 59, 59), PLAN))
        self.assertFalse(att.desired_signed_in(at(12, 0), PLAN))
        self.assertFalse(att.desired_signed_in(at(13, 0), PLAN))
        self.assertTrue(att.desired_signed_in(at(20, 59), PLAN))
        self.assertFalse(att.desired_signed_in(at(21, 0), PLAN))

    def test_active_window(self):
        self.assertEqual(att.active_window(at(15, 0), PLAN), (at(14, 30), at(17, 0)))
        self.assertIsNone(att.active_window(at(13, 0), PLAN))


class TestReconcile(LogCapture):
    def test_sign_in_inside_window(self):
        client = FakeClient(signed_in=False)
        clock = FakeClock(at(9, 0), at(9, 0, 5))
        self.assertEqual(att.reconcile_once(client, "38605", PLAN, clock=clock), "签到")
        self.assertEqual(client.actions, ["签到"])

    def test_sign_out_after_window_end(self):
        client = FakeClient(signed_in=True, started_at=at(14, 30))
        clock = FakeClock(at(17, 0, 2), at(17, 0, 5))
        self.assertEqual(att.reconcile_once(client, "38605", PLAN, clock=clock), "签退")
        self.assertEqual(client.actions, ["签退"])

    def test_nothing_to_do_when_consistent(self):
        client = FakeClient(signed_in=True, started_at=at(9, 0))
        clock = FakeClock(at(9, 30), at(9, 30, 5))
        self.assertIsNone(att.reconcile_once(client, "38605", PLAN, clock=clock))
        self.assertEqual(client.actions, [])

    def test_does_not_sign_out_a_record_it_cannot_claim(self):
        """用户自己在非考勤时段签到时，程序不许替他签退。"""
        plan = [[at(14, 30), at(17, 0)]]
        client = FakeClient(signed_in=True, started_at=at(13, 0))
        clock = FakeClock(at(13, 30), at(13, 30, 5))
        self.assertIsNone(att.reconcile_once(client, "38605", plan, clock=clock))
        self.assertEqual(client.actions, [])

    # ---- 2026-10-08 12:00:13 那条假记录的直接回归测试 ----
    def test_stale_sign_in_across_window_end_is_abandoned(self):
        """11:59:33 判定「该在岗」，读状态耗时到 12:00:14 才动手 —— 必须放弃。

        线上原样：12:00:13 签到、12:10:12 被别的进程签退，
        凭空多出一条 0.17 小时的记录，整天的考勤因此变成「需确认」。
        """
        client = FakeClient(signed_in=False)
        clock = FakeClock(at(11, 59, 33), at(12, 0, 14))
        self.assertIsNone(att.reconcile_once(client, "38605", PLAN, clock=clock))
        self.assertEqual(client.actions, [])
        self.assert_logged("放弃本轮动作")

    def test_stale_sign_out_across_window_start_is_abandoned(self):
        """反过来也一样：判定「该签退」，动手时已经进入下一个时段。"""
        client = FakeClient(signed_in=True, started_at=at(9, 0))
        clock = FakeClock(at(13, 59, 58), at(14, 30, 5))
        self.assertIsNone(att.reconcile_once(client, "38605", PLAN, clock=clock))
        self.assertEqual(client.actions, [])
        self.assert_logged("放弃本轮动作")

    def test_dry_run_does_not_act(self):
        client = FakeClient(signed_in=False)
        clock = FakeClock(at(9, 0), at(9, 0, 5))
        self.assertIsNone(att.reconcile_once(client, "38605", PLAN, dry_run=True, clock=clock))
        self.assertEqual(client.actions, [])


class TestExtension(unittest.TestCase):
    """硬上限规则：撑得到时段结束才延长，撑不到就如实退出。"""

    def test_no_extension_outside_window(self):
        self.assertIsNone(att.extension_seconds(at(13, 0), PLAN, 0.0, 5.8 * 3600))

    def test_extension_when_window_end_fits(self):
        # 4 小时前启动（14400s），现在 16:30，17:00 结束 → 需要 30 分钟 + 余量
        need = att.extension_seconds(at(16, 30), PLAN, 14400.0, 5.8 * 3600)
        self.assertIsNotNone(need)
        self.assertAlmostEqual(need, 30 * 60 + att.SIGN_OUT_MARGIN_SEC)

    def test_no_extension_when_window_end_exceeds_hard_limit(self):
        # 5.75 小时前启动（20700s），现在 16:45，17:00 结束 → 超出硬上限
        self.assertIsNone(att.extension_seconds(at(16, 45), PLAN, 20700.0, 5.8 * 3600))

    def test_hard_limit_stays_under_workflow_timeout(self):
        # main.yml 的 timeout-minutes: 358，绝不能撞上它
        self.assertLess(att.DEFAULT_HARD_MAX_HOURS * 60, 358)
        self.assertGreater(att.DEFAULT_HARD_MAX_HOURS, att.DEFAULT_MAX_HOURS)


class TestPlanResolution(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.schedule = self.dir / "schedule.json"
        self.special = self.dir / "special.json"
        # 和线上一样的结构：key 是 %w，值是 [["08:30","12:00"], ...]
        windows = [["08:30", "12:00"], ["14:30", "17:00"]]
        self.schedule.write_text(
            json.dumps({str(i): windows for i in range(7)}), encoding="utf-8"
        )

    def tearDown(self):
        self.tmp.cleanup()

    def resolve(self):
        return att.resolve_today_plan(
            self.schedule, self.special, {"holiday_attendance": True}, now=at(9)
        )

    def test_empty_special_means_no_attendance(self):
        """special.json 里写 [] 是「今天不考勤」，不能退回星期表去打卡。"""
        self.special.write_text(json.dumps({"2026-10-08": []}), encoding="utf-8")
        plan, skip = self.resolve()
        self.assertEqual(plan, [])
        self.assertIsNone(skip)

    def test_missing_special_falls_back_to_weekday_table(self):
        self.special.write_text("{}", encoding="utf-8")
        plan, skip = self.resolve()
        self.assertIsNone(skip)
        self.assertEqual(plan, [[at(8, 30), at(12, 0)], [at(14, 30), at(17, 0)]])

    def test_special_overrides_weekday_table(self):
        self.special.write_text(
            json.dumps({"2026-10-08": [["10:00", "11:00"]]}), encoding="utf-8"
        )
        plan, _ = self.resolve()
        self.assertEqual(plan, [[at(10, 0), at(11, 0)]])


class TestWorkflowConfig(unittest.TestCase):
    """workflow 必须把运行串成接力链，并且和硬上限对齐。"""

    def setUp(self):
        self.text = (REPO / ".github" / "workflows" / "main.yml").read_text(encoding="utf-8")

    def test_has_concurrency_group(self):
        self.assertIn("group: dgut-attendance", self.text)
        self.assertIn("cancel-in-progress: true", self.text)

    def test_runs_every_hour(self):
        # :05 起跑，避开 12:00 / 17:00 / 18:00 / 21:00 这些整点边界
        self.assertIn("cron: '5 * * * *'", self.text)

    def test_passes_hard_max_hours(self):
        self.assertIn("--hard-max-hours", self.text)


if __name__ == "__main__":
    unittest.main()
