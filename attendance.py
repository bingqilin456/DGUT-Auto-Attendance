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
# GitHub Actions 单个 job 上限 6 小时，留一点余量。
DEFAULT_MAX_HOURS = 5.5


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


def get_schedule(filename: str | Path, flag: int, now: datetime | None = None) -> list[list[datetime]]:
    """读取今天的考勤时间表。

    Args:
        filename: ``schedule.json`` 或 ``special.json``。
        flag: 1 读星期表（key 为 ``%w``，0=周日）；2 读特殊日期表（key 为 ``%Y-%m-%d``）。
        now: 以哪一天为准，默认取当前北京时间。

    Returns:
        ``[[start, end], ...]``，可能为空列表；时间已按起点排序。
    """
    now = now or beijing_naive()
    try:
        with open(filename, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        log(f"没找到文件 {filename}")
        return []
    except json.JSONDecodeError as exc:
        log(f"文件 {filename} 不是合法 JSON：{exc}")
        return []

    key = now.strftime("%w") if flag == 1 else now.strftime("%Y-%m-%d")
    today = data.get(key)
    if not today:
        return []

    day_prefix = now.strftime("%Y-%m-%d ")
    plan: list[list[datetime]] = []
    for item in today:
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
    2. 今天 **已经有考勤时段结束过** —— 说明是本程序签到后没能正常签退。
    """
    started = getattr(state, "open_started_at", None)
    if started is not None and started.date() < now.date():
        return True
    return any(end <= now for _, end in plan)


def reconcile_once(
    client: XgxtClient,
    work_assignment_id: str | None,
    plan: list[list[datetime]],
    *,
    key: str | None = None,
    dry_run: bool = False,
) -> str | None:
    """做一轮对账。

    Returns:
        本轮实际发生的变化描述（``'签到'`` / ``'签退'``），没变化则返回 ``None``。
    """
    now = beijing_naive()
    want = desired_signed_in(now, plan)

    state = client.get_attendance_state(work_assignment_id)
    actual = state.signed_in

    if want == actual:
        log(f"对账完成：{state.work_assignment_name} 当前{state.describe()}，与计划一致，无需动作")
        return None

    if not want and not _may_sign_out(state, now, plan):
        log(
            f"{state.work_assignment_name} 当前在岗，但不在今日考勤时段内，"
            "无法确认这条记录由本程序产生 —— 保持原样，不自动签退"
        )
        return None

    action = "签到" if want else "签退"
    if dry_run:
        log(f"[dry-run] 应在 {state.work_assignment_name} {action}（期望在岗={want}，实际={actual}）")
        return None

    log(f"{state.work_assignment_name} 需要{action}（期望在岗={want}，实际在岗={actual}）")
    if want:
        new_state = client.sign_in(state.work_assignment_id)
    else:
        new_state = client.sign_out(state.work_assignment_id)

    log(f"{action}完成，当前状态：{new_state.describe()}")
    if key:
        post_server(key, f"{action}成功 {now:%Y-%m-%d %H:%M:%S}")
    return action


def should_skip_today(config: dict, special: list[list[datetime]]) -> str | None:
    """判断今天是否需要整体跳过，返回跳过原因。"""
    if config["holiday_attendance"] or special:
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
    parser.add_argument("--max-hours", type=float, default=DEFAULT_MAX_HOURS, help="单次运行最长时间（小时）")
    parser.add_argument("--once", action="store_true", help="只做一轮对账就退出")
    parser.add_argument("--dry-run", action="store_true", help="只报告将要做什么，不真正打卡")
    args = parser.parse_args(argv)

    key = _normalize_key(args.key)
    config = get_config(args.config)
    waid = _resolve_work_assignment_id(args.work_assignment_id, config)

    special = get_schedule(args.special, 2)
    skip_reason = should_skip_today(config, special)
    if skip_reason:
        log(f"[程序结束] {skip_reason}")
        return 0

    plan = special if special else get_schedule(args.schedule, 1)
    log(f"[程序启动] 北京时间 {beijing_naive():%Y-%m-%d %H:%M:%S}")
    if plan:
        for start, end in plan:
            log(f"  今日考勤时段：{start:%H:%M} - {end:%H:%M}")
    else:
        log("  今天没有考勤安排，仅做一次收尾检查（不会主动签到）")
    log(f"  考勤职位：{waid or '（取列表中第一个）'}{'  [dry-run]' if args.dry_run else ''}")

    client = XgxtClient(args.username, args.password)
    try:
        client.login()
    except DgutError as exc:
        log(f"[登录失败] {exc}")
        return 1
    log("[登录成功]")

    deadline = time.monotonic() + args.max_hours * 3600
    points = boundaries(plan)

    while True:
        now = beijing_naive()
        try:
            reconcile_once(client, waid, plan, key=key, dry_run=args.dry_run)
        except DgutError as exc:
            # 单轮失败不致命：下一轮对账会自然重试
            log(f"[本轮对账失败] {exc}")

        if args.once:
            break

        now = beijing_naive()
        upcoming = [p for p in points if p > now]
        if not upcoming:
            # 关键收尾：刚跨过最后一个签退时刻时，上面那轮对账用的 `now` 是在
            # 发起 HTTP 之前取的，可能还停在「时段内」的旧值（例如 00:53:59.9
            # 采样、00:54:01 才打印），于是被判成「无需动作」。这里用全新时间
            # 再对账一次，确保不会带着「在岗」状态退出。
            try:
                reconcile_once(client, waid, plan, key=key, dry_run=args.dry_run)
            except DgutError as exc:
                log(f"[收尾对账失败] {exc}")
            log("[程序结束] 今日考勤时段已全部结束")
            break
        if time.monotonic() >= deadline:
            log("[程序结束] 已达本次运行时间上限")
            break

        sleep_for = min((upcoming[0] - now).total_seconds(), args.interval)
        sleep_for = max(5.0, min(sleep_for, deadline - time.monotonic()))
        time.sleep(sleep_for)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
