# -*- coding: utf-8 -*-
"""客户端可靠性回归：所有 HTTP 响应均在本地模拟，不联网、不打卡。"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dgut_client as dgut  # noqa: E402


AUTH_FORM = """
<input id="pwdEncryptSalt" value="1234567890123456">
<input id="execution" value="execution-test">
<input id="cllt" value="userNameLogin">
"""


def page(rows: str, day: str = "2026-10-12") -> str:
    return f"""<html><body><div>{day}</div>
<select name="workAssignmentId"><option value="38605">测试职位</option></select>
<input name="session_token" value="test-token">
<table><thead><tr><th>签到时间</th><th>状态</th><th>操作</th></tr></thead>
<tbody>{rows}</tbody></table></body></html>"""


EMPTY = page("""<tr><td>暂未签到</td><td>暂未签到</td><td>
<button onclick="beginWork()">签到</button></td></tr>""")
OPEN = page("""<tr><td>14:30:04 ~</td><td>状态：已签到</td><td>
<button disabled onclick="beginWork()">签到</button>
<button onclick="doEndWork('888', '38605')">签退</button></td></tr>""")
CLOSED = page("""<tr><td>14:30:04 ~17:00:03</td><td>状态：已签退</td><td>
<button disabled onclick="beginWork()">签到</button>
<button disabled onclick="doEndWork('888', '38605')">签退</button></td></tr>
<tr><td>暂未签到</td><td>暂未签到</td><td>
<button onclick="beginWork()">签到</button></td></tr>""")


def response(text: str, *, status: int = 200, url: str | None = None,
             redirected: bool = False) -> requests.Response:
    result = requests.Response()
    result.status_code = status
    result.url = url or dgut.BASE_URL + dgut.ATTENDANCE_PAGE
    result.encoding = "utf-8"
    result._content = text.encode("utf-8")
    result.history = [requests.Response()] if redirected else []
    return result


class OfflineClientTest(unittest.TestCase):
    def setUp(self):
        # 未提供本地响应的任何请求都会失败，防止回归测试意外联网。
        self.http = self.enterContext(patch("requests.sessions.Session.request"))
        self.http.side_effect = AssertionError("测试禁止联网")
        self.enterContext(patch("dgut_client.time.sleep"))
        self.enterContext(patch("dgut_client.beijing_now", return_value=
                                datetime(2026, 10, 12, 15, 0, tzinfo=dgut.BEIJING_TZ)))
        self.client = dgut.XgxtClient("test-user", "test-password")
        self.client.logged_in = True

    def replies(self, *responses):
        self.http.side_effect = list(responses)

    def posted_actions(self):
        return [dict(call.kwargs.get("data") or []).get("action_name")
                for call in self.http.call_args_list if call.args[0] == "POST"]


class TestAuthentication(OfflineClientTest):
    def test_post_login_page_invalidates_session_without_resubmitting_old_token(self):
        self.replies(response(AUTH_FORM, url=dgut.AUTH_URL, redirected=True))
        with self.assertRaises(dgut.LoginError):
            self.client._post(dgut.ATTENDANCE_PAGE,
                              [("action_name", "endWork"), ("session_token", "old-token")])
        self.assertFalse(self.client.logged_in)
        self.assertEqual(self.posted_actions(), ["endWork"])

    def test_get_still_on_login_page_after_reauthentication_is_an_error(self):
        self.replies(response(AUTH_FORM, url=dgut.AUTH_URL), response(AUTH_FORM, url=dgut.AUTH_URL),
                     response("business landing", url=dgut.BASE_URL + "/", redirected=True),
                     response(AUTH_FORM, url=dgut.AUTH_URL))
        with self.assertRaises(dgut.LoginError):
            self.client._get(dgut.ATTENDANCE_PAGE)
        self.assertFalse(self.client.logged_in)

    def test_login_rejects_authentication_page_even_with_redirect_history(self):
        self.client.logged_in = False
        self.replies(response(AUTH_FORM, url=dgut.AUTH_URL),
                     response(AUTH_FORM, url=dgut.AUTH_URL, redirected=True))
        with self.assertRaises(dgut.LoginError):
            self.client.login()
        self.assertFalse(self.client.logged_in)

    def test_login_rejects_non_200_business_response(self):
        self.client.logged_in = False
        self.replies(response(AUTH_FORM, url=dgut.AUTH_URL),
                     response("maintenance", status=403, redirected=True))
        with self.assertRaises(dgut.LoginError):
            self.client.login()
        self.assertFalse(self.client.logged_in)


class TestAttendanceParsing(OfflineClientTest):
    def parse(self, html):
        return self.client._parse_state(html, "38605", "测试职位")

    def test_unknown_html_cannot_be_misread_as_successful_sign_out(self):
        for html in ("", "<html>维护中</html>", AUTH_FORM,
                     '<select name="workAssignmentId"><option value="38605">测试职位</option></select>'):
            with self.subTest(html=html), self.assertRaises(dgut.PageChangedError):
                self.parse(html)

    def test_valid_empty_attendance_row_is_not_signed_in(self):
        state = self.parse(EMPTY)
        self.assertFalse(state.signed_in)
        self.assertTrue(state.can_sign_in)

    def test_time_only_open_row_uses_attendance_page_date(self):
        state = self.parse(OPEN.replace("2026-10-12", "2026-10-11"))
        self.assertTrue(state.signed_in)
        self.assertEqual(state.open_started_at, datetime(2026, 10, 11, 14, 30, 4))

    def test_time_only_open_row_defaults_to_beijing_today(self):
        state = self.parse(OPEN.replace("<div>2026-10-12</div>", ""))
        self.assertEqual(state.open_started_at, datetime(2026, 10, 12, 14, 30, 4))

    def test_closed_historical_row_does_not_supply_open_record_details(self):
        historical = """<tr><td>08:30:09 ~12:00:03</td><td>状态：已签退</td>
<td><button onclick="doEndWork('777', '38605')">签退</button></td></tr>"""
        state = self.parse(OPEN.replace("<tbody>", "<tbody>" + historical))
        self.assertTrue(state.signed_in)
        self.assertEqual(state.open_record_id, "888")
        self.assertEqual(state.open_started_at, datetime(2026, 10, 12, 14, 30, 4))

    def test_closed_row_with_button_is_not_open(self):
        state = self.parse(CLOSED.replace("disabled onclick=\"doEndWork", "onclick=\"doEndWork"))
        self.assertFalse(state.signed_in)
        self.assertFalse(state.can_sign_out)
        self.assertIsNone(state.open_started_at)

    def test_explicit_open_record_datetime_is_preserved(self):
        state = self.parse(OPEN.replace("14:30:04 ~", "2026-10-10 14:30:04 ~"))
        self.assertEqual(state.open_started_at, datetime(2026, 10, 10, 14, 30, 4))

    def test_expired_selection_post_cannot_claim_not_signed_in(self):
        self.replies(response(OPEN), response(AUTH_FORM, url=dgut.AUTH_URL))
        with self.assertRaises(dgut.LoginError):
            self.client.get_attendance_state("38605")


class TestRequestRetries(OfflineClientTest):
    def test_action_timeout_is_not_replayed_without_checking_state(self):
        self.replies(requests.ReadTimeout("response lost"), response(CLOSED))
        with self.assertRaises(dgut.DgutError):
            self.client._post(dgut.ATTENDANCE_PAGE, [("action_name", "endWork")])
        self.assertEqual(self.posted_actions(), ["endWork"])

    def test_action_server_error_is_not_replayed(self):
        self.replies(response("unavailable", status=503), response(EMPTY))
        with self.assertRaises(dgut.DgutError):
            self.client._post(dgut.ATTENDANCE_PAGE, [("action_name", "beginWork")])
        self.assertEqual(self.posted_actions(), ["beginWork"])

    def test_read_requests_keep_network_retries(self):
        self.replies(requests.ReadTimeout("timeout"), response("unavailable", status=503),
                     response(OPEN))
        self.assertEqual(self.client._get(dgut.ATTENDANCE_PAGE), OPEN)
        self.assertEqual(self.http.call_count, 3)

    def test_selection_posts_keep_network_retries(self):
        self.replies(requests.ReadTimeout("timeout"), response(OPEN))
        self.assertEqual(self.client._post(dgut.ATTENDANCE_PAGE, [("action_name", "")]), OPEN)
        self.assertEqual(self.posted_actions(), ["", ""])


class TestSubmissionGuard(OfflineClientTest):
    def test_sign_in_guard_runs_after_all_preparation_requests(self):
        self.replies(response(EMPTY), response(EMPTY), response(EMPTY))
        called_at = []

        def guard():
            called_at.append(self.http.call_count)
            return False

        state = self.client.sign_in("38605", can_submit=guard)
        self.assertFalse(state.signed_in)
        self.assertEqual(called_at, [3])
        self.assertEqual(self.posted_actions(), [""])

    def test_sign_out_guard_prevents_action_when_window_changed(self):
        self.replies(response(OPEN), response(OPEN), response(OPEN))
        state = self.client.sign_out("38605", can_submit=lambda: False)
        self.assertTrue(state.signed_in)
        self.assertEqual(self.posted_actions(), [""])

    def test_sign_in_guard_allows_current_action_and_confirmation(self):
        self.replies(response(EMPTY), response(EMPTY), response(EMPTY), response(OPEN),
                     response(OPEN), response(OPEN))
        state = self.client.sign_in("38605", can_submit=lambda: True)
        self.assertTrue(state.signed_in)
        self.assertEqual(self.posted_actions(), ["", "beginWork", ""])

    def test_sign_out_confirmation_login_page_cannot_report_success(self):
        self.replies(response(OPEN), response(OPEN), response(OPEN), response(CLOSED),
                     response(CLOSED), response(AUTH_FORM, url=dgut.AUTH_URL))
        with self.assertRaises(dgut.LoginError):
            self.client.sign_out("38605")


if __name__ == "__main__":
    unittest.main()
