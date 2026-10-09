# -*- coding: utf-8 -*-
"""东莞理工学院 学生工作管理系统（学工系统）客户端。

替代已停止维护的第三方包 ``dgut-requests``（最后发布 2022-10-04，1.x 时代的
``/student/partwork/*.jsp`` 路径已全部失效）。

与原库的关键差异
----------------
1. ``dgut-requests`` 的 ``login()`` 不携带 ``service`` 参数，CAS 会把票据发给默认
   服务；随后 ``_auth()`` 只能拿到学工系统返回的一段 **JavaScript** 跳转
   (``window.location.href=...``)，而 requests 不执行 JS，于是会话其实从未建立，
   却在库内部被标记为 ``is_authenticated = True``。
   本模块改为 **带 service 直接登录**，一次拿到有效会话。
2. 原库的 ``attendance()`` 无论服务器返回什么，都无条件返回 “签到成功”。
   本模块在动作之后 **重新读取页面状态** 来验证结果，失败会抛异常。
3. 老路径 ``/student/partwork/attendance.jsp`` → 新路径
   ``/student/partWorkNew/attendancePre.jsp``。
"""

from __future__ import annotations

import base64
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from random import choices
from typing import Any

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

__all__ = [
    "DgutError", "LoginError", "PageChangedError", "ActionFailedError",
    "WorkAssignment", "AttendanceState", "XgxtClient", "beijing_now",
]

BASE_URL = "https://stu.dgut.edu.cn"
AUTH_URL = "https://auth.dgut.edu.cn/authserver/login"
ATTENDANCE_PAGE = "/student/partWorkNew/attendancePre.jsp"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 金智教育 authserver 密码加密所用的字符集（服务端已内置同一张表）
_SALT_CHARS = "ABCDEFGHJKMNPQRSTWXYZabcdefhijkmnprstwxyz2345678"
# 密码前需要拼接的随机噪声长度；服务端解密后只取第 64 字节之后的内容，
# 因此随机 IV 破坏掉的第一个分组正好落在这段噪声里。
_RANDOM_PREFIX_LEN = 64

BEIJING_TZ = timezone(timedelta(hours=8))

# 网络抖动重试：校园网 / 云主机偶发读超时，一次失败不该毁掉整轮打卡
# （2026-09-29 就因为没有重试、且调用方只捕获 DgutError，一次 ReadTimeout
#   让进程直接崩掉，会话挂着没人签退，凭空多算了 6.79 小时工时）
REQUEST_RETRIES = 3
RETRY_BACKOFF = 5.0


def beijing_now() -> datetime:
    """返回当前北京时间（带时区信息）。"""
    return datetime.now(BEIJING_TZ)


class DgutError(Exception):
    """本模块所有异常的基类。"""


class LoginError(DgutError):
    """中央认证失败。"""


class PageChangedError(DgutError):
    """页面结构不符合预期——通常意味着学校又改版了。"""


class ActionFailedError(DgutError):
    """服务器接受了请求，但状态没有变成预期值。"""


# --------------------------------------------------------------------------- #
# HTML 解析小工具
# --------------------------------------------------------------------------- #

def _parse_inputs(html: str) -> list[dict[str, str]]:
    """把页面上所有 ``<input>`` 解析成 ``{'id','name','value'}`` 列表。"""
    inputs = []
    for m in re.finditer(r"<input\b[^>]*>", html, re.I):
        tag = m.group(0)

        def attr(name: str, _tag: str = tag) -> str:
            am = re.search(r'\b%s\s*=\s*"([^"]*)"' % name, _tag, re.I)
            return am.group(1) if am else ""

        inputs.append({"id": attr("id"), "name": attr("name"), "value": attr("value")})
    return inputs


def _session_tokens(html: str) -> list[str]:
    """收集页面上的 ``session_token``（同一个页面可能出现多次，且值可能相同）。"""
    tokens = re.findall(r'name="session_token"[^>]*value="([^"]*)"', html)
    return [t for t in tokens if t]


def _select_options(html: str, name: str) -> list[tuple[str, str]]:
    """解析指定 ``<select>`` 的 ``(value, text)`` 列表。"""
    m = re.search(r'<select\b[^>]*name="%s"[^>]*>(.*?)</select>' % re.escape(name), html, re.S | re.I)
    if not m:
        return []
    out = []
    for om in re.finditer(r'<option\b[^>]*value="([^"]*)"[^>]*>(.*?)</option>', m.group(1), re.S | re.I):
        out.append((om.group(1), re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", om.group(2))).strip()))
    return out


def _enabled_buttons(html: str) -> list[tuple[str, str]]:
    """返回 ``(onclick, 按钮文字)``，只包含未 disabled 的按钮。"""
    out = []
    for m in re.finditer(r"<button\b([^>]*)>(.*?)</button>", html, re.S | re.I):
        attrs = m.group(1)
        if re.search(r"\bdisabled\b", attrs, re.I):
            continue
        om = re.search(r'onclick="([^"]*)"', attrs, re.I)
        if not om:
            continue
        text = re.sub(r"\s+", "", re.sub(r"<[^>]+>", "", m.group(2)))
        out.append((om.group(1), text))
    return out


def _looks_like_login_page(resp: requests.Response) -> bool:
    """判断这次响应是不是被踢回中央认证登录页了。

    会话过期时学工系统有两种表现：

    1. 返回一段短小的 JS 跳转，正文里出现 ``authserver/login``；
    2. requests 自动跟随跳转，直接拿到那张 19 KB 的登录页 —— 此时
       ``resp.url`` 会落在 authserver 上，正文里有 ``pwdEncryptSalt``。

    第二种以前没被识别，于是「状态读回来是未签到、动作却发不出去」，
    每轮都以一堆看不懂的解析错误收场。两种都识别出来才能自己重建会话。
    """
    if "authserver/login" in resp.url:
        return True
    if len(resp.text) < 600 and "authserver/login" in resp.text:
        return True
    return "pwdEncryptSalt" in resp.text


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class WorkAssignment:
    """一个勤工助学考勤职位。"""

    id: str
    name: str

    def __str__(self) -> str:  # pragma: no cover - 仅用于日志
        return f"{self.name}(workAssignmentId={self.id})"


@dataclass
class AttendanceState:
    """某职位当天的考勤状态。"""

    work_assignment_id: str = ""
    work_assignment_name: str = ""
    signed_in: bool = False
    can_sign_in: bool = False
    can_sign_out: bool = False
    open_record_id: str = ""
    open_end_work_assignment_id: str = ""
    # 这条「在岗」记录的起始时间，用于判断它是不是隔夜遗留
    open_started_at: datetime | None = None
    rows: list[dict[str, Any]] = field(default_factory=list)

    def describe(self) -> str:
        if self.signed_in:
            return "已签到（在岗中）"
        return "未签到"


# --------------------------------------------------------------------------- #
# 客户端
# --------------------------------------------------------------------------- #

class XgxtClient:
    """学工系统（学生工作管理系统）客户端。

    典型用法::

        c = XgxtClient(username, password)
        c.login()
        state = c.get_attendance_state()      # 不传则用第一个职位
        if not state.signed_in:
            c.sign_in()
    """

    def __init__(self, username: str, password: str, timeout: int = 30) -> None:
        if not username or not password:
            raise ValueError("用户名和密码不能为空")
        self.username = username
        self._password = password
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.logged_in = False

    # ---------------------------------------------------------------- 登录 --
    def login(self, service: str = BASE_URL + "/") -> None:
        """登录中央认证，并让 CAS 直接把票据发给学工系统。

        Args:
            service: CAS 回跳地址。必须是绝对 URL。
        """
        login_url = f"{AUTH_URL}?service={requests.utils.quote(service, safe='')}"
        referer_headers = {"Referer": login_url, "Origin": "https://auth.dgut.edu.cn"}

        inputs, by_id = self._fetch_login_form(login_url)
        salt = by_id["pwdEncryptSalt"]
        execution = by_id["execution"]

        # 页面上有 4 个登录区块（fido/短信/账号密码/扫码），只有账号密码区块的
        # cllt 是 userNameLogin，必须挑对，否则会被当成其它登录方式。
        cllt = next(
            (i["value"] for i in inputs if i["id"] == "cllt" and i["value"] == "userNameLogin"),
            next((i["value"] for i in inputs if i["id"] == "cllt"), ""),
        )
        data = {
            "username": self.username,
            "password": self._encrypt_password(self._password, salt),
            "execution": execution,
            "captcha": "",
            "_eventId": by_id.get("_eventId", "submit"),
            "cllt": cllt,
            "dllt": by_id.get("dllt", "generalLogin"),
            "lt": by_id.get("lt", ""),
        }

        resp = self._request("POST", login_url, data=data, headers=referer_headers)
        if resp.status_code == 401 or "密码错误" in resp.text or "用户名或密码" in resp.text:
            raise LoginError("账号或密码错误")
        if not resp.history and "authserver/login" in resp.url:
            raise LoginError("登录未跳转，账号或密码可能不正确")
        self.logged_in = True

    def _fetch_login_form(self, login_url: str) -> tuple[list[dict[str, Any]], dict[str, str]]:
        """取登录表单，返回 ``(所有 input, 按 id 索引的 input)``。

        这里有个坑，2026-10-08 的线上事故正是它造成的：

        CAS 只要还认得当前会话（会话里留着 CASTGC），
        ``GET /authserver/login?service=…`` 就会直接 302 回业务系统，返回的 HTML
        里既没有 ``pwdEncryptSalt`` 也没有 ``execution``。于是「会话过期 → 重新登录」
        一路失败，程序只报一句「登录页结构已变化」，之后每一轮重试都是同样的结果。
        10-08 15:00 服务端抖了一下之后，两次运行就这样瞎了 80 分钟，
        17:00 的签退也因此彻底丢掉（那天的工时于是变成 14:30~21:00 一条 6.5 小时）。

        所以取表单之前先清掉旧的认证 cookie，让 CAS 老老实实把登录页给出来；
        仍然拿不到就重试一次，最后才报错，并把被重定向到的地址与页面大小带出去。
        """
        last: Any = None
        for attempt in (1, 2):
            # 关键：丢掉旧的 CASTGC，CAS 才会重新返回登录页而不是直接跳走。
            self.session.cookies.clear()
            resp = self._request("GET", login_url)
            last = resp
            if resp.status_code == 200:
                inputs = _parse_inputs(resp.text)
                by_id: dict[str, str] = {}
                for item in inputs:
                    if item["id"] and item["id"] not in by_id:
                        by_id[item["id"]] = item["value"]
                if by_id.get("pwdEncryptSalt") and by_id.get("execution"):
                    return inputs, by_id
            if attempt == 1:
                time.sleep(RETRY_BACKOFF)

        if last is not None and last.status_code != 200:
            raise LoginError(f"打开中央认证登录页失败：HTTP {last.status_code}")
        redirected = f"；本次被重定向到 {last.url}" if getattr(last, "history", None) else ""
        size = len(last.text) if last is not None else 0
        raise PageChangedError(
            "中央认证登录页结构已变化，未找到 pwdEncryptSalt / execution 字段"
            f"{redirected}（返回 {size} 字节）"
        )

    @staticmethod
    def _encrypt_password(password: str, salt: str) -> str:
        """按金智 authserver 的规则加密密码，返回 base64 密文。

        明文 = 64 位随机字符 + 密码，PKCS7 填充，AES-128-CBC，密钥即 salt。
        """
        plain = pad(
            ("".join(choices(_SALT_CHARS, k=_RANDOM_PREFIX_LEN)) + password).encode("utf-8"),
            AES.block_size,
            "pkcs7",
        )
        iv = "".join(choices(_SALT_CHARS, k=16)).encode("utf-8")
        cipher = AES.new(salt.encode("utf-8"), AES.MODE_CBC, iv).encrypt(plain)
        return base64.b64encode(cipher).decode("ascii")

    def _ensure_login(self) -> None:
        if not self.logged_in:
            self.login()

    # ------------------------------------------------------------ HTTP 层 --
    def _request(self, method: str, url: str, *, headers=None, data=None):
        """带重试的请求。

        网络层的异常（超时、连接被重置）统一转成 :class:`DgutError`，
        这样调用方只需要捕获一种异常，不会像以前那样把 ``ReadTimeout``
        漏出去、把整个进程搞崩。
        """
        last: Exception | None = None
        for attempt in range(1, REQUEST_RETRIES + 1):
            try:
                resp = self.session.request(
                    method, url, timeout=self.timeout, headers=headers, data=data)
            except requests.RequestException as exc:
                last = exc
                if attempt < REQUEST_RETRIES:
                    time.sleep(RETRY_BACKOFF * attempt)
                    continue
            else:
                # 5xx 多半是服务端打了个嗝，值得再试；4xx 直接返回给上层判断
                if resp.status_code < 500 or attempt == REQUEST_RETRIES:
                    return resp
                last = DgutError(f"HTTP {resp.status_code}")
                time.sleep(RETRY_BACKOFF * attempt)
        raise DgutError(
            f"网络请求失败（已重试 {REQUEST_RETRIES} 次）："
            f"{type(last).__name__}: {last}"
        ) from last

    def _get(self, path: str) -> str:
        self._ensure_login()
        url = path if path.startswith("http") else BASE_URL + path
        resp = self._request("GET", url)
        if resp.status_code != 200:
            raise DgutError(f"GET {path} 失败：HTTP {resp.status_code}")
        # 会话过期时系统会跳回中央认证，这里做一个兜底重建
        if _looks_like_login_page(resp):
            self.logged_in = False
            self.login()
            resp = self._request("GET", url)
            if resp.status_code != 200:
                raise DgutError(f"GET {path} 失败：HTTP {resp.status_code}")
        return resp.text

    def _post(self, path: str, data: list[tuple[str, str]]) -> str:
        self._ensure_login()
        url = path if path.startswith("http") else BASE_URL + path
        resp = self._request(
            "POST", url, data=data,
            headers={"Referer": url, "Origin": BASE_URL},
        )
        if resp.status_code != 200:
            raise DgutError(f"POST {path} 失败：HTTP {resp.status_code}")
        return resp.text

    # ------------------------------------------------------------- 职位 --
    def get_work_assignments(self) -> list[WorkAssignment]:
        """获取当前账号所有可考勤的职位。"""
        html = self._get(ATTENDANCE_PAGE)
        out = []
        for value, text in _select_options(html, "workAssignmentId"):
            if value in ("", "-1"):
                continue
            out.append(WorkAssignment(id=value, name=text))
        return out

    # ------------------------------------------------------------- 状态 --
    def get_attendance_state(self, work_assignment_id: str | int | None = None) -> AttendanceState:
        """读取指定职位当天的考勤状态。

        这一步只提交 ``action_name=''``（等价于在页面里切换下拉框），不会产生考勤记录。
        """
        html = self._get(ATTENDANCE_PAGE)
        options = [o for o in _select_options(html, "workAssignmentId") if o[0] not in ("", "-1")]
        if not options:
            raise DgutError("该账号没有任何可考勤的职位（勤工助学岗位）")

        chosen = None
        if work_assignment_id is not None:
            wanted = str(work_assignment_id)
            chosen = next((o for o in options if o[0] == wanted), None)
            if chosen is None:
                raise DgutError(
                    f"workAssignmentId={wanted} 不在可用职位列表中："
                    + ", ".join(f"{v}={t}" for v, t in options)
                )
        if chosen is None:
            chosen = options[0]
        waid, waname = chosen

        selected_html = self._post(ATTENDANCE_PAGE, self._payload(html, waid, action_name=""))
        return self._parse_state(selected_html, waid, waname)

    def _payload(
        self,
        html: str,
        work_assignment_id: str,
        action_name: str,
        day_attend_pre_id: str = "",
        end_work_assignment_id: str = "",
    ) -> list[tuple[str, str]]:
        """按浏览器提交的顺序构造表单。

        注意 ``session_token`` 在页面上出现了两次，浏览器会把两个都提交，
        因此这里用 ``list[tuple]`` 而不是 dict。
        """
        tokens = _session_tokens(html) or [""]
        data: list[tuple[str, str]] = [
            ("action_name", action_name),
            ("modifying", "true"),
            ("salaryInfoId", ""),
            ("endWorkAssignmentId", end_work_assignment_id),
            ("dayAttendPreId", day_attend_pre_id),
            ("session_token", tokens[0]),
            ("backUrl", ""),
        ]
        for extra in tokens[1:]:
            data.append(("session_token", extra))
        data.append(("workAssignmentId", work_assignment_id))
        return data

    @staticmethod
    def _parse_state(html: str, waid: str, waname: str) -> AttendanceState:
        state = AttendanceState(work_assignment_id=waid, work_assignment_name=waname)

        for onclick, text in _enabled_buttons(html):
            if "beginWork" in onclick and "签到" in text:
                state.can_sign_in = True
            m = re.search(r"doEndWork\(\s*'?(\d+)'?\s*,\s*'?(\d+)'?\s*\)", onclick)
            if m:
                state.can_sign_out = True
                state.open_record_id = m.group(1)
                state.open_end_work_assignment_id = m.group(2)
            elif "endWork" in onclick and "签退" in text:
                state.can_sign_out = True

        # 行级信息：用于判断“在岗中”以及留下可读的日志
        tbody = re.search(r"<tbody[^>]*>(.*?)</tbody>", html, re.S | re.I)
        if tbody:
            for row in re.finditer(r"<tr[^>]*>(.*?)</tr>", tbody.group(1), re.S | re.I):
                cells = [
                    re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c)).strip()
                    for c in re.findall(r"<td[^>]*>(.*?)</td>", row.group(1), re.S | re.I)
                ]
                if not any(cells):
                    continue
                joined = " | ".join(cells)
                if "暂未签到" in joined:
                    continue
                state.rows.append({"cells": cells, "raw": joined})
                if "已签退" not in joined and re.search(r"\d{4}-\d{2}-\d{2}", joined):
                    state.signed_in = True
                    m = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", joined)
                    if m:
                        try:
                            state.open_started_at = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
                        except ValueError:
                            pass

        # 更可靠的判据：存在可用的签退按钮 ⇒ 有一条未结束的在岗记录
        if state.can_sign_out:
            state.signed_in = True
        return state

    # ------------------------------------------------------------- 动作 --
    def sign_in(self, work_assignment_id: str | int | None = None) -> AttendanceState:
        """签到。若已在岗则直接返回当前状态。"""
        state = self.get_attendance_state(work_assignment_id)
        if state.signed_in:
            return state
        if not state.can_sign_in:
            raise ActionFailedError(
                f"{state.work_assignment_name} 当前没有可用的签到按钮，无法签到"
            )
        html = self._get(ATTENDANCE_PAGE)
        self._post(ATTENDANCE_PAGE, self._payload(html, state.work_assignment_id, "beginWork"))
        after = self.get_attendance_state(state.work_assignment_id)
        if not after.signed_in:
            raise ActionFailedError(
                f"已提交签到请求，但 {state.work_assignment_name} 仍显示未签到，签到可能失败"
            )
        return after

    def sign_out(self, work_assignment_id: str | int | None = None) -> AttendanceState:
        """签退。若当前不在岗则直接返回当前状态。"""
        state = self.get_attendance_state(work_assignment_id)
        if not state.signed_in:
            return state
        if not state.can_sign_out:
            raise ActionFailedError(
                f"{state.work_assignment_name} 处于在岗状态，但页面上没有可用的签退按钮"
            )
        html = self._get(ATTENDANCE_PAGE)
        self._post(
            ATTENDANCE_PAGE,
            self._payload(
                html,
                state.work_assignment_id,
                "endWork",
                day_attend_pre_id=state.open_record_id,
                end_work_assignment_id=state.open_end_work_assignment_id,
            ),
        )
        after = self.get_attendance_state(state.work_assignment_id)
        if after.signed_in:
            raise ActionFailedError(
                f"已提交签退请求，但 {state.work_assignment_name} 仍显示在岗，签退可能失败"
            )
        return after


# --------------------------------------------------------------------------- #
# 命令行自检入口：python dgut_client.py [state|in|out]
# --------------------------------------------------------------------------- #

def _cli() -> int:  # pragma: no cover - 手工使用
    import argparse
    import os
    import sys

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="莞工学工系统考勤客户端自检")
    parser.add_argument("action", choices=["state", "in", "out"], help="要执行的操作")
    parser.add_argument("-U", "--username", default=os.environ.get("DGUT_USERNAME"))
    parser.add_argument("-P", "--password", default=os.environ.get("DGUT_PASSWORD"))
    parser.add_argument("-W", "--work-assignment-id", default=os.environ.get("DGUT_WORK_ASSIGNMENT_ID"))
    args = parser.parse_args()

    if not args.username or not args.password:
        print("缺少账号密码：请设置 DGUT_USERNAME / DGUT_PASSWORD，或使用 -U / -P")
        return 2

    client = XgxtClient(args.username, args.password)
    client.login()
    print(f"[登录成功] {beijing_now():%Y-%m-%d %H:%M:%S} {args.username}")

    waid = args.work_assignment_id or None
    if args.action == "state":
        for wa in client.get_work_assignments():
            print(f"  可考勤职位：{wa}")
        state = client.get_attendance_state(waid)
        print(f"  当前状态：{state.work_assignment_name} → {state.describe()}")
        for row in state.rows:
            print(f"    · {row['raw'][:120]}")
        return 0

    if args.action == "in":
        state = client.sign_in(waid)
        print(f"[签到完成] {state.work_assignment_name} → {state.describe()}")
    else:
        state = client.sign_out(waid)
        print(f"[签退完成] {state.work_assignment_name} → {state.describe()}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_cli())
