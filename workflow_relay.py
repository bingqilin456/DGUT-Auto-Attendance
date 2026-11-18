"""考勤开始前排好当天的下一轮；不登录学校、不产生考勤记录。"""

from __future__ import annotations

import os
import time
from datetime import datetime
from typing import Mapping

import requests

from attendance import BASE_DIR, beijing_naive, get_config, log, resolve_today_plan

# 连续这么多轮都失败就不再接力 —— 足以排除偶发的网络抖动，又不会把
# 一次凭证错误放大成通宵空转。
MAX_CONSECUTIVE_FAILURES = 3


def _recent_runs_keep_failing(repository: str, token: str) -> bool:
    """最近 ``MAX_CONSECUTIVE_FAILURES`` 轮是否全部失败。

    查不到历史时返回 ``False`` —— 宁可多排一轮，也不要因为 API 抽风漏掉考勤。
    """
    url = f"https://api.github.com/repos/{repository}/actions/workflows/main.yml/runs"
    try:
        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"},
            params={"per_page": MAX_CONSECUTIVE_FAILURES},
            timeout=30,
        )
        if response.status_code != 200:
            return False
        runs = response.json().get("workflow_runs") or []
    except (requests.RequestException, ValueError):
        return False

    # 本轮自己还在跑（status=in_progress），不计入判断。
    finished = [r for r in runs if r.get("status") == "completed"]
    if len(finished) < MAX_CONSECUTIVE_FAILURES:
        return False
    return all(r.get("conclusion") == "failure" for r in finished)


def enqueue_next_run(*, environ: Mapping[str, str] | None = None,
                     now: datetime | None = None) -> bool:
    """当天还有排班时创建一个等待接手的 main 运行，无剩余排班则停止接力。"""
    environ = os.environ if environ is None else environ
    if environ.get("GITHUB_REF") != "refs/heads/main":
        log("[接力] 当前不是 main 分支，不启动后续考勤")
        return False

    now = now or beijing_naive()
    plan, skip_reason = resolve_today_plan(
        BASE_DIR / "schedule.json", BASE_DIR / "special.json",
        get_config(BASE_DIR / "config.ini"), now=now,
    )
    if skip_reason or not any(end > now for _, end in plan):
        log("[接力] 当天没有未结束的排班，不再创建下一轮")
        return False

    token = environ.get("GITHUB_TOKEN")
    repository = environ.get("GITHUB_REPOSITORY")
    if not token or not repository:
        raise RuntimeError("无法安排接力：缺少 GITHUB_TOKEN 或 GITHUB_REPOSITORY")

    # 凭证被拒/要求验证码时，下一轮必然以同样方式失败。接力在考勤之前执行，
    # 看不到本轮结果，所以只能回头看最近几轮的结论：连着失败就停止接力，
    # 否则每一轮 90 秒的失败会被一路排到天亮（2026-11-17 就这样烧了 600 多次运行）。
    if _recent_runs_keep_failing(repository, token):
        log(f"[接力] 最近连续 {MAX_CONSECUTIVE_FAILURES} 轮都失败，"
            "判定为凭证/风控问题，停止接力；请先更新 USERNAME / PASSWORD 或手动登录解除风控")
        return False

    url = f"https://api.github.com/repos/{repository}/actions/workflows/main.yml/dispatches"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    last_reason = ""
    for attempt in range(1, 4):
        try:
            response = requests.post(url, headers=headers, json={"ref": "main"}, timeout=30)
        except requests.RequestException as exc:
            # 不输出令牌或请求头；响应丢失时重复派发仍由同一并发组串行执行。
            last_reason = type(exc).__name__
        else:
            if response.status_code == 204:
                log("[接力] 下一轮已创建，将等待本轮结束后接手（无需等待 cron）")
                return True
            last_reason = f"HTTP {response.status_code}"
            if response.status_code < 500:
                raise RuntimeError(f"无法安排接力：{last_reason}，请检查工作流 actions: write 权限")
        if attempt < 3:
            time.sleep(5 * attempt)

    raise RuntimeError(f"无法安排接力：已尝试 3 次，{last_reason}")


def main() -> int:
    try:
        enqueue_next_run()
    except Exception as exc:
        log(f"[接力失败] {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
