"""考勤开始前排好当天的下一轮；不登录学校、不产生考勤记录。"""

from __future__ import annotations

import os
import time
from datetime import datetime
from typing import Mapping

import requests

from attendance import BASE_DIR, beijing_naive, get_config, log, resolve_today_plan


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
