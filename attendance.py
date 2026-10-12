# -*- coding: utf-8 -*-
"""莞工学工系统 · 自动考勤主程序。

与原版（2022）的区别
====================

原版用 ``schedule`` 库把「签到 / 签退」挂成两个定时任务，有两个致命问题：

1. **不能补签**。GitHub Actions 排队或网络抖动导致某次运行没按时启动，那一整段
   时间就再也没人去打卡了。
2. **从不校验结果**。它所依赖的第三方库 ``dgut-requests`` 无论服务器返回什么
   都无条件返回「签到成功」，所以日志里全是成功，实际却可能什么都没发生。

本版改成 **对账循环（reconciliation loop）**：

    每一轮先算「此刻我应该在岗吗？」，再读服务端的真实状态；
    两者不一致才发动作，动作之后 **重新读页面确认生效**。

因此程序是幂等的、可自愈的：漏跑一轮下一轮会补上，同一时刻重复运行也不会
重复打卡，而且每一次动作都有服务端状态作为证据。
"""

from __future__ import annotations

import argparse
import configparser
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

from dgut_client import DgutError, XgxtClient, beijing_now

try:  # 节假日判断是可选增强，缺了也能正常打卡
    import chinese_calendar as _calendar
except ImportError:  # pragma: no cover
    _calendar = None

BASE_DIR = Path(__file__).resolve().parent

# 默认每轮对账间隔（秒）。到达考勤时间点时会提前唤醒，不受此值影响。
DEFAULT_INTERVAL = 600
# 故障时尽快重新读状态，不让一次失败额外拖延十分钟。
DEFAULT_RETRY_INTERVAL = 30
DEFAULT_CLEANUP_SECONDS = 600
# 「软上限」：跑到这个时长，就该考虑收摊了。
DEFAULT_MAX_HOURS = 5.5
# 「硬上限」：本次运行绝对不能超过的时长，必须小于 workflow 里的
# timeout-minutes（main.yml 里是 358 分钟 ≈ 5.97 小时）。到达软上限后，
# 只有当「当前时段的结束」还落在硬上限之内时，才会继续撑下去把它签退掉。
DEFAULT_HARD_MAX_HOURS = 5.8
# 签退动作本身要留的余量（秒），别卡在硬上限上被 GitHub 掐断。
SIGN_OUT_MARGIN_SEC = 120


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #

def beijing_naive() -> datetime:
    """当前北京时间（不带时区信息，便于与 schedule.json 里的 \"8:30\" 直接比较）。"""
    return beijing_now().replace(tzinfo=None)


def get_beijing_datetime() -> datetime:
    """兼容旧版本的函数名。"""
    return beijing_naive()


def log(message: str) -> None:
    print(f"[{beijing_naive():%Y-%m-%d %H:%M:%S}] {message}", flush=True)


# --------------------------------------------------------------------------- #
# 配置文件
# --------------------------------------------------------------------------- #

def get_config(filename: str | Path = "config.ini") -> dict:
    """读取 ``config.ini``。

    返回 ``{'holiday_attendance': bool, 'workAssignmentId': str | None}``。
    任何读取失败都退回默认值，不让配置问题中断打卡。
    """
    demand = {"holiday_attendance": False, "workAssignmentId": None}
    try:
        config = configparser.ConfigParser()
        config.read(str(filename), encoding="utf-8")
        if config.has_section("attendance"):
            if config.has_option("attendance", "holiday_attendance"):
                demand["holiday_attendance"] = config.getboolean("attendance", "holiday_attendance")
            if config.has_option("attendance", "workAssignmentId"):
                raw = config.get("attendance", "workAssignmentId").strip()
                # 值可能被引号包住，也可能被写成 "-1"（表示未指定）
                raw = raw.strip("'\"")
                if raw and raw != "-1":
                    demand["workAssignmentId"] = raw
    except (ValueError, configparser.Error) as exc:
        log(f"配置读取失败（{exc}），改用默认配置：节假日不打折、职位取列表中第一个")
    return demand


def _read_json_map(filename: str | Path) -> dict | None:
    """读一个 JSON 对象；读失败返回 ``None``（同时记一条日志）。"""
    try:
        with open(filename, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        log(f"没找到文件 {filename}")
        return None
    except json.JSONDecodeError as exc:
        log(f"文件 {filename} 不是合法 JSON：{exc}")
        return None
    if not isinstance(data, dict):
        log(f"文件 {filename} 的顶层必须是 JSON 对象，已忽略")
        return None
    return data


def _parse_windows(day_prefix: str, items) -> list[list[datetime]]:
    """把 ``[["8:30", "12:00"], ...]`` 解析成 ``[[start, end], ...]``。"""
    plan: list[list[datetime]] = []
    for item in items:
        try:
            start, end = (datetime.strptime(day_prefix + t, "%Y-%m-%d %H:%M") for t in item)
        except (TypeError, ValueError) as exc:
            log(f"考勤时间段 {item!r} 格式不合法，已跳过（{exc}）")
            continue
        if end <= start:
            log(f"考勤时间段 {start:%H:%M}-{end:%H:%M} 的签退时间不晚于签到时间，已跳过")
            continue
        plan.append([start, end])
    plan.sort()
    return plan


def get_schedule(filename: str | Path, flag: int, now: datetime | None = None) -> list[list[datetime]]:
    """读取今天的考勤时间表。

    Args:
        filename: ``schedule.json`` 或 ``special.json``。
        flag: 1 读星期表（key 为 ``%w``，0=周日）；2 读特殊日期表（key 为 ``%Y-%m-%d``）。
        now: 以哪一天为准，默认取当前北京时间。

    Returns:
        ``[[start, end], ...]``，可能为空列表；时间已按起点排序。
        这一天没出现在文件里、或者写的就是空列表，都会得到空列表；
        需要区分这两种情况时请用 :func:`get_special_schedule`。
    """
    now = now or beijing_naive()
    data = _read_json_map(filename)
    if data is None:
        return []

    key = now.strftime("%w") if flag == 1 else now.strftime("%Y-%m-%d")
    today = data.get(key)
    if not today:
        return []

    return _parse_windows(now.strftime("%Y-%m-%d "), today)


def get_special_schedule(filename: str | Path, now: datetime | None = None) -> list[list[datetime]] | None:
    """读取某一天在 ``special.json`` 里的特殊安排。

    它和 :func:`get_schedule` 的关键区别是 **能区分「没表态」和「明确不考勤」**：

    Returns:
        ``None``                —— 这天根本没出现在文件里，请按星期表走；
        ``[]``                  —— 这天写了空列表，意思是 **当天不考勤**；
        ``[[start, end], ...]`` —— 这天的临时安排，直接覆盖星期表。
    """
    now = now or beijing_naive()
    data = _read_json_map(filename)
    if data is None:
        return None

    key = now.strftime("%Y-%m-%d")
    if key not in data:
        return None
    today = data[key]
    if not today:
        return []
    return _parse_windows(now.strftime("%Y-%m-%d "), today)


# --------------------------------------------------------------------------- #
# 对账核心
# --------------------------------------------------------------------------- #

def desired_signed_in(now: datetime, plan: list[list[datetime]]) -> bool:
    """此刻是否应该处于「在岗」状态。"""
    return any(start <= now < end for start, end in plan)


def boundaries(plan: list[list[datetime]]) -> list[datetime]:
    """今天所有的状态切换时刻（每个时间段的起点与终点），已排序去重。"""
    points = {t for window in plan for t in window}
    return sorted(points)


def continuous_windows(plan: list[list[datetime]]) -> list[list[datetime]]:
    """合并相连或重叠的班次，其间不需要签退或交接。"""
    merged: list[list[datetime]] = []
    for start, end in sorted(plan):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def active_window(now: datetime, plan: list[list[datetime]]) -> tuple[datetime, datetime] | None:
    """``now`` 所处的考勤时段；不在任何时段内时返回 ``None``。"""
    for start, end in continuous_windows(plan):
        if start <= now < end:
            return start, end
    return None


def extension_seconds(
    now: datetime,
    plan: list[list[datetime]],
    monotonic_now: float,
    hard_deadline: float,
) -> float | None:
    """到达运行时间上限时，判断还值不值得撑到当前时段结束。

    返回还需要继续运行的秒数；返回 ``None`` 表示撑不住（当前不在任何时段内，
    或者时段的结束时刻已经超出 job 的硬上限），此时应该如实退出，让下一次运行接手。
    """
    window = active_window(now, plan)
    if window is None:
        return None
    need = (window[1] - now).total_seconds() + SIGN_OUT_MARGIN_SEC
    if monotonic_now + need > hard_deadline:
        return None
    return need


def post_server(key: str, content: str) -> None:
    """通过 Server酱 推送一条消息（可选功能，失败不影响打卡）。"""
    try:
        res = requests.post(
            f"https://sctapi.ftqq.com/{key}.send",
            data={"title": content},
            headers={"Content-type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        if res.status_code == 200 and res.json().get("code") == 0:
            log("推送成功")
        else:
            log(f"推送失败：HTTP {res.status_code} {res.text[:200]}")
    except Exception as exc:  # 通知失败绝不能影响考勤
        log(f"推送异常：{exc}")


def _may_sign_out(state, now: datetime, plan: list[list[datetime]]) -> bool:
    """判断「是否应该把这条在岗记录收尾」。

    自动签退是有风险的动作：如果用户自己在非考勤时段手动签到了，程序贸然签退
    反而会破坏他的真实工时。所以只有下面两种情况才动手：

    1. 在岗记录是 **更早的日期** 开的 —— 一定是遗留，必须收尾；
    2. 记录的签到时间属于今天已经结束的考勤时段。

    同时用于恢复时识别上一班次遗留记录；不能只比较「是否在岗」，否则上一班
    漏签退的记录会被误认为是下一班的正常签到，两个班次被合并。
    """
    started = getattr(state, "open_started_at", None)
    if started is not None and started.date() < now.date():
        return True
    if started is None:
        return False
    return any(start <= started < end <= now for start, end in continuous_windows(plan))


def reconcile_once(
    client: XgxtClient,
    work_assignment_id: str | None,
    plan: list[list[datetime]],
    *,
    key: str | None = None,
    dry_run: bool = False,
    clock=None,
) -> str | None:
    """做一轮对账。

    Args:
        clock: 取当前北京时间的函数，默认 :func:`beijing_naive`（测试时可注入假时钟）。

    Returns:
        本轮实际发生的变化描述（``'签到'`` / ``'签退'``），没变化则返回 ``None``。
    """
    clock = clock or beijing_naive
    now = clock()
    want = desired_signed_in(now, plan)

    state = client.get_attendance_state(work_assignment_id)
    actual = state.signed_in
    stale_record = actual and _may_sign_out(state, now, plan)

    if want == actual and not stale_record:
        log(f"对账完成：{state.work_assignment_name} 当前{state.describe()}，与计划一致，无需动作")
        return None

    if not want and not _may_sign_out(state, now, plan):
        log(
            f"{state.work_assignment_name} 当前在岗，但不在今日考勤时段内，"
            "无法确认这条记录由本程序产生 —— 保持原样，不自动签退"
        )
        return None

    # ---- 动手之前，必须用「刚刚」的时间再判一次（TOCTOU 防护）----
    # 上面那次读状态要走两个请求，校园网慢的时候要十几秒甚至几十秒。
    # 2026-10-08 12:00 就是这么出错的：11:59:33 采样判定「该在岗」，读状态期间
    # 另一个进程正好在 12:00:05 签退，本进程却拿着 11:59 的旧结论在 12:00:14
    # 又签到，凭空多出一条 12:00:13~12:10:12 的记录，整天的考勤因此变成「需确认」。
    act_time = clock()
    if desired_signed_in(act_time, plan) != want:
        log(
            f"读状态期间跨过了考勤时段边界（判定时 {now:%H:%M:%S}，现在 {act_time:%H:%M:%S}），"
            "放弃本轮动作，下一轮重新判断"
        )
        return None

    # 即使已经进入下一班，也先收尾旧记录。下一轮重新读状态后再签到，
    # 不伪造原来漏掉的签退时间，也不把两次动作作为一次未经校验的提交。
    signing_in = want and not stale_record
    action = "签到" if signing_in else "签退"
    if dry_run:
        log(f"[dry-run] 应在 {state.work_assignment_name} {action}（期望在岗={want}，实际={actual}）")
        return None

    log(f"{state.work_assignment_name} 需要{action}（期望在岗={want}，实际在岗={actual}）")
    if stale_record and want:
        log(f"发现上一班次遗留记录（{state.open_started_at}），先签退，再重新对账当前班次")

    def can_submit() -> bool:
        submit_time = clock()
        if signing_in:
            return desired_signed_in(submit_time, plan) and submit_time.date() == now.date()
        if stale_record:
            return _may_sign_out(state, submit_time, plan)
        return not desired_signed_in(submit_time, plan) and _may_sign_out(state, submit_time, plan)

    if signing_in:
        new_state = client.sign_in(state.work_assignment_id, can_submit=can_submit)
    else:
        new_state = client.sign_out(state.work_assignment_id, can_submit=can_submit,
                                    expected_open_record_id=state.open_record_id)

    if new_state.signed_in != signing_in:
        log(f"实际提交前考勤时段已变化，取消{action}，下一轮重新对账")
        return None

    log(f"{action}完成，当前状态：{new_state.describe()}")
    if key:
        post_server(key, f"{action}成功 {act_time:%Y-%m-%d %H:%M:%S}")
    return action


def should_skip_today(config: dict, special: list[list[datetime]] | None) -> str | None:
    """判断今天是否需要整体跳过，返回跳过原因。

    ``special`` 传 :func:`get_special_schedule` 的结果：只要这一天在 special.json
    里出现过（哪怕是空列表），就以它为准，不再套用节假日判断。
    """
    if config["holiday_attendance"] or special is not None:
        return None
    if _calendar is None:
        return None  # 装不上 chinesecalendar 时，宁可打卡也不要漏
    today = beijing_naive().date()
    if _calendar.is_holiday(today) and not _calendar.is_workday(today):
        return "今天是休息日"
    return None


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #

def _resolve_work_assignment_id(cli_value: str | None, config: dict) -> str | None:
    if cli_value:
        return str(cli_value)
    return config.get("workAssignmentId")


def _normalize_key(raw: str | None) -> str | None:
    """兼容旧写法：SERVER_KEY 里可能填的是 ``-K sctp...`` 而不是纯粹的 key。"""
    if not raw:
        return None
    value = raw.strip().strip("'\"")
    for prefix in ("--key", "-K", "-k"):
        if value.startswith(prefix):
            value = value[len(prefix):].strip()
            break
    return value or None


def resolve_today_plan(
    schedule_file: str | Path,
    special_file: str | Path,
    config: dict,
    now: datetime | None = None,
):
    """算出「今天」的考勤时段，返回 ``(plan, 跳过原因)``。"""
    special = get_special_schedule(special_file, now)
    skip_reason = should_skip_today(config, special)
    if skip_reason:
        return [], skip_reason
    # special 为 None 表示「今天没特殊安排」，才轮到星期表；
    # 空列表 [] 是明确表态「今天不考勤」，不能再退回星期表去打卡。
    return (special if special is not None else get_schedule(schedule_file, 1, now)), None


def log_plan(plan: list[list[datetime]]) -> None:
    """把今天的时段打印出来（0 个时段时说明只做收尾）。"""
    if plan:
        for start, end in plan:
            log(f"  今日考勤时段：{start:%H:%M} - {end:%H:%M}")
    else:
        log("  今天没有考勤安排，仅做一次收尾检查（不会主动签到）")


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="莞工学工系统自动考勤")
    parser.add_argument("-U", "--username", help="中央认证账号", required=True)
    parser.add_argument("-P", "--password", help="中央认证密码", required=True)
    parser.add_argument("-K", "--key", help="Server酱 SendKey（可选）")
    parser.add_argument("-W", "--work-assignment-id", help="考勤职位 ID，默认读 config.ini")
    parser.add_argument("-c", "--config", default=str(BASE_DIR / "config.ini"), help="配置文件路径")
    parser.add_argument("--schedule", default=str(BASE_DIR / "schedule.json"), help="星期考勤表")
    parser.add_argument("--special", default=str(BASE_DIR / "special.json"), help="特殊日期考勤表")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL, help="对账间隔（秒）")
    parser.add_argument("--retry-interval", type=int, default=DEFAULT_RETRY_INTERVAL,
                        help="对账失败后的重试间隔（秒）")
    parser.add_argument("--cleanup-seconds", type=int, default=DEFAULT_CLEANUP_SECONDS,
                        help="结束班次后签退失败的重试窗口（秒）")
    parser.add_argument("--finish-window", action="store_true",
                        help="完成当前或下一班次并确认收尾后退出，让下一次运行接手")
    parser.add_argument("--max-hours", type=float, default=DEFAULT_MAX_HOURS,
                        help="单次运行的软上限（小时），到点后准备收摊")
    parser.add_argument("--hard-max-hours", type=float, default=DEFAULT_HARD_MAX_HOURS,
                        help="单次运行的硬上限（小时），必须小于 workflow 的 timeout-minutes")
    parser.add_argument("--once", action="store_true", help="只做一轮对账就退出")
    parser.add_argument("--dry-run", action="store_true", help="只报告将要做什么，不真正打卡")
    args = parser.parse_args(argv)
    if min(args.interval, args.retry_interval, args.cleanup_seconds, args.max_hours, args.hard_max_hours) <= 0:
        parser.error("对账/重试间隔、收尾时间和运行上限必须大于 0")
    if args.max_hours > args.hard_max_hours:
        parser.error("软上限不能大于硬上限")

    key = _normalize_key(args.key)
    config = get_config(args.config)
    waid = _resolve_work_assignment_id(args.work_assignment_id, config)

    plan, skip_reason = resolve_today_plan(args.schedule, args.special, config)
    if skip_reason:
        log(f"[程序结束] {skip_reason}")
        return 0

    log(f"[程序启动] 北京时间 {beijing_naive():%Y-%m-%d %H:%M:%S}")
    log_plan(plan)
    log(f"  考勤职位：{waid or '（取列表中第一个）'}{'  [dry-run]' if args.dry_run else ''}")
    log(f"  运行时长：软上限 {args.max_hours} 小时，硬上限 {args.hard_max_hours} 小时")

    client = XgxtClient(args.username, args.password)
    login_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            client.login()
            login_error = None
            break
        except DgutError as exc:
            login_error = exc
            log(f"[登录失败 第 {attempt}/3 次] {exc}")
            if attempt < 3:
                time.sleep(30)
    if login_error is not None:
        log("[程序结束] 连续 3 次都没登上，本次运行放弃（没有产生任何考勤记录）")
        return 1
    log("[登录成功]")

    started = time.monotonic()
    deadline = started + args.max_hours * 3600
    hard_deadline = started + args.hard_max_hours * 3600
    initial_now = beijing_naive()
    plan_date = initial_now.date()
    points = boundaries(plan)
    pending_ends = [end for _, end in continuous_windows(plan) if end > initial_now]
    finish_at = (min(pending_ends) if args.finish_window and pending_ends
                 else max(points, default=initial_now))
    if args.finish_window and pending_ends:
        log(f"  本次完成 {finish_at:%H:%M} 结束的班次后交接")
    cleanup_deadline: float | None = None
    last_error: Exception | None = None

    while True:
        # 跨过午夜之后必须重新读「今天」的排班：桌面版常常整夜开着，云端也会遇到
        # 运行跨越 00:00 的情况。否则第二天整个上午都会拿着昨天的时段表空转，
        # 该签到的时候一声不响。
        today = beijing_naive().date()
        if today != plan_date:
            plan_date = today
            plan, skip_reason = resolve_today_plan(args.schedule, args.special, config)
            points = boundaries(plan)
            log(f"[跨天] 进入 {today:%Y-%m-%d}，重新读取排班")
            if skip_reason:
                log(f"[程序结束] {skip_reason}")
                return 0
            log_plan(plan)
            finish_at = max(points, default=beijing_naive())
            cleanup_deadline = None

        monotonic_now = time.monotonic()
        if monotonic_now >= hard_deadline:
            log("[程序结束] 到达硬上限" + ("，仍未确认对账成功" if last_error else ""))
            return 1 if last_error else 0
        if cleanup_deadline is not None and monotonic_now >= cleanup_deadline:
            log("[程序结束] 收尾重试超时，未能确认签退；请检查学校考勤页面")
            if key:
                post_server(key, "签退未确认：收尾重试超时，请检查学校考勤页面")
            return 1

        # 仅当这一轮从结束时刻之后开始，且状态读取/动作验证成功，才允许退出。
        # 若读状态期间跨过边界，下一轮必须用新时间再读一次，不能把旧结果当收尾。
        round_time = beijing_naive()
        if round_time >= finish_at and cleanup_deadline is None:
            cleanup_deadline = min(time.monotonic() + args.cleanup_seconds, hard_deadline)
        action = None
        try:
            action = reconcile_once(client, waid, plan, key=key, dry_run=args.dry_run)
            if round_time >= finish_at and not args.dry_run and action != "签退":
                # 保留未知归属/手动记录也会返回 None，不能据此宣称已签退。
                # 尊重保留策略，同时让云端明确报告未确认收尾，便于人工处理。
                final_state = client.get_attendance_state(waid)
                if final_state.signed_in:
                    raise DgutError("收尾时仍在岗，记录未自动签退；请人工核对考勤记录")
            last_error = None
        except Exception as exc:
            # 单轮失败不致命：下一轮对账会自然重试。
            # 这里必须捕获 Exception 而不只是 DgutError —— 一旦漏出去进程就会
            # 直接崩掉，而此时可能正处于「已签到」状态，会话就挂在那儿没人签退，
            # 工时一直涨到下一次运行才发现（2026-09-29 就因此多算了 6.79 小时）。
            log(f"[本轮对账失败] {type(exc).__name__}: {exc}")
            last_error = exc

        if args.once:
            return 1 if last_error else 0

        now = beijing_naive()
        upcoming = [p for p in points if p > now]
        if now >= finish_at:
            if last_error is None and round_time >= finish_at:
                log("[程序结束] 本次班次收尾检查完成" if args.finish_window
                    else "[程序结束] 今日考勤时段已全部结束，收尾检查完成")
                return 0
            if cleanup_deadline is None:
                cleanup_deadline = min(time.monotonic() + args.cleanup_seconds, hard_deadline)
            # 收尾期间优先重试，不因软上限到期而吞掉签退错误。
            delay = args.retry_interval if last_error else 0
            time.sleep(max(0, min(delay, cleanup_deadline - time.monotonic())))
            continue

        if time.monotonic() >= deadline:
            # 到点了。只有当「当前时段的结束 + 签退余量」还落在硬上限之内，
            # 才继续撑下去把签退做完；否则如实退出，交给下一次运行收尾。
            # 2026-10-08 的 17:00 就是这么丢的：两次运行都在时段中途退出，
            # 17:00 时没有任何进程活着，那次签到一直挂到 21:00 才被收尾，
            # 于是 14:30-17:00 和 18:00-21:00 被合并成一条 6.5 小时的记录。
            need = extension_seconds(now, plan, time.monotonic(), hard_deadline)
            if need is not None:
                window = active_window(now, plan)
                log(f"[延长运行] 当前时段 {window[1]:%H:%M} 才结束，"
                    f"再跑 {need / 60:.0f} 分钟把签退做完")
                deadline = time.monotonic() + need
                continue
            window = active_window(now, plan)
            if window is not None:
                log(f"[警告] 已到运行时间上限（{args.max_hours} 小时），但当前仍在考勤时段内"
                    f"（{now:%H:%M}，要到 {window[1]:%H:%M} 才结束，"
                    f"硬上限 {args.hard_max_hours} 小时内来不及收尾）："
                    "本次运行按时收摊，这条在岗记录留给下一次运行收尾")
            else:
                log(f"[程序结束] 已达本次运行时间上限（{args.max_hours} 小时）")
            return 1 if last_error else 0

        interval = args.retry_interval if last_error else args.interval
        # 关闭上一班的遗留记录后，立即重新读状态，开始当前班次。
        if action == "签退" and desired_signed_in(now, plan):
            interval = 0
        next_boundary = min(upcoming[0] if upcoming else finish_at, finish_at)
        sleep_for = min(max(0, (next_boundary - now).total_seconds()), interval,
                        max(0, deadline - time.monotonic()))
        time.sleep(sleep_for)


if __name__ == "__main__":
    raise SystemExit(main())
