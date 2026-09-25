# -*- coding: utf-8 -*-
"""莞工学工系统 · 桌面版打卡助手（图形界面）。

这是 ``attendance.py`` 的图形外壳，**核心逻辑完全复用同一套对账循环**，
所以桌面版和 GitHub Actions 云端版的行为完全一致：

    每一轮先算「此刻我应该在岗吗？」→ 读服务端真实状态 →
    两者不一致才发动作 → 动作之后再读一次页面确认生效。

因此它是幂等的：重复点「立即签到」不会重复打卡，漏跑一轮下一轮会自动补上。

账号密码保存在同目录的 ``account.ini``，该文件已在 ``.gitignore`` 中忽略，
不会被提交到 GitHub；也可以用环境变量 ``DGUT_USERNAME`` / ``DGUT_PASSWORD``。

双击 ``启动打卡助手.bat`` 即可打开本窗口。
"""

from __future__ import annotations

import configparser
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import traceback
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import attendance as core  # noqa: E402  （必须在 sys.path 处理之后导入）
from dgut_client import DgutError, XgxtClient  # noqa: E402

ACCOUNT_FILE = BASE_DIR / "account.ini"
SCHEDULE_FILE = BASE_DIR / "schedule.json"
SPECIAL_FILE = BASE_DIR / "special.json"
CONFIG_FILE = BASE_DIR / "config.ini"
CRASH_LOG = BASE_DIR / "crash.log"

TITLE = "莞工自动打卡助手"
UI_FONT = ("Microsoft YaHei UI", 10)
BIG_FONT = ("Microsoft YaHei UI", 20, "bold")
COLOR_IDLE = "#57606a"      # 未签到 / 未知
COLOR_WORKING = "#1a7f37"   # 在岗中
COLOR_BUSY = "#9a6700"      # 运行中
COLOR_ERROR = "#cf222e"     # 出错


# --------------------------------------------------------------------------- #
# 账号存取
# --------------------------------------------------------------------------- #

def load_account() -> tuple[str, str]:
    """优先读本机 ``account.ini``，没有就退回环境变量。"""
    if ACCOUNT_FILE.exists():
        cfg = configparser.ConfigParser()
        try:
            cfg.read(str(ACCOUNT_FILE), encoding="utf-8")
            if cfg.has_section("account"):
                return (
                    cfg.get("account", "username", fallback="").strip(),
                    cfg.get("account", "password", fallback=""),
                )
        except (configparser.Error, OSError):
            pass
    return os.environ.get("DGUT_USERNAME", "").strip(), os.environ.get("DGUT_PASSWORD", "")


def save_account(username: str, password: str) -> None:
    """把账号写进本机 ``account.ini``（该文件不会上传 GitHub）。"""
    cfg = configparser.ConfigParser()
    cfg["account"] = {"username": username, "password": password}
    with open(ACCOUNT_FILE, "w", encoding="utf-8") as f:
        f.write("# 本机专用，已在 .gitignore 中忽略，不会上传 GitHub\n")
        cfg.write(f)
    try:  # 顺手收紧权限（Windows 上是否生效取决于文件系统）
        os.chmod(ACCOUNT_FILE, 0o600)
    except OSError:
        pass


def today_plan() -> list[list[datetime]]:
    """今天实际生效的考勤时段：特殊日期表优先，否则用星期表。

    ``get_special_schedule`` 返回 ``None`` 才表示「今天没特殊安排」；
    返回空列表 ``[]`` 是明确表态「今天不考勤」，此时不能再退回星期表。
    """
    special = core.get_special_schedule(SPECIAL_FILE)
    if special is not None:
        return special
    return core.get_schedule(SCHEDULE_FILE, 1)


# --------------------------------------------------------------------------- #
# 主窗口
# --------------------------------------------------------------------------- #

class Dashboard:
    """一个窗口 + 一个工作线程。所有网络请求都在后台线程里跑，界面不卡。"""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.stop_flag = threading.Event()
        self.worker: threading.Thread | None = None
        self.auto_running = False
        self._job_kind: str | None = None
        self._pending_remember = False

        # 把 attendance.py 里的 log() 接到界面上（它内部调用的是模块全局名，
        # 所以在这里替换即可捕获对账循环的全部输出）
        core.log = self._core_log

        username, password = load_account()
        self.username_var = tk.StringVar(value=username)
        self.password_var = tk.StringVar(value=password)
        self.show_password = tk.BooleanVar(value=False)
        self.remember_var = tk.BooleanVar(value=True)
        self.interval_var = tk.IntVar(value=10)
        self.status_var = tk.StringVar(value="尚未连接")
        self.detail_var = tk.StringVar(value="填好账号后点「查看状态」")

        self._build_ui()
        self._refresh_plan_summary()
        self._drain_events()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------- 界面 --

    def _build_ui(self) -> None:
        self.root.title(TITLE)
        self.root.geometry("780x620")
        self.root.minsize(700, 560)

        pad = {"padx": 12, "pady": 6}

        # --- 账号 ---
        account = ttk.LabelFrame(self.root, text=" 账号 ")
        account.pack(fill="x", **pad)
        account.columnconfigure(1, weight=1)

        ttk.Label(account, text="学号：").grid(row=0, column=0, sticky="w", padx=(10, 4), pady=8)
        ttk.Entry(account, textvariable=self.username_var).grid(row=0, column=1, sticky="ew", pady=8)
        ttk.Label(account, text="密码：").grid(row=1, column=0, sticky="w", padx=(10, 4), pady=(0, 8))
        self.password_entry = ttk.Entry(account, textvariable=self.password_var, show="●")
        self.password_entry.grid(row=1, column=1, sticky="ew", pady=(0, 8))

        opts = ttk.Frame(account)
        opts.grid(row=2, column=0, columnspan=3, sticky="w", padx=10, pady=(0, 8))
        ttk.Checkbutton(opts, text="显示密码", variable=self.show_password,
                        command=self._toggle_password).pack(side="left")
        ttk.Checkbutton(opts, text="记住账号（存本机 account.ini，不上传）",
                        variable=self.remember_var).pack(side="left", padx=(12, 0))
        ttk.Button(account, text="保存账号", command=self._save_account).grid(
            row=0, column=2, rowspan=2, padx=10, pady=8, sticky="ns")

        # --- 状态 ---
        status = ttk.LabelFrame(self.root, text=" 当前状态 ")
        status.pack(fill="x", **pad)
        self.status_label = ttk.Label(status, textvariable=self.status_var,
                                      font=BIG_FONT, foreground=COLOR_IDLE)
        self.status_label.pack(anchor="w", padx=12, pady=(10, 0))
        ttk.Label(status, textvariable=self.detail_var, font=UI_FONT,
                  foreground="#57606a", wraplength=720, justify="left").pack(
            anchor="w", padx=12, pady=(2, 4))
        self.plan_label = ttk.Label(status, text="", font=UI_FONT, foreground="#0969da")
        self.plan_label.pack(anchor="w", padx=12, pady=(0, 10))

        # --- 手动操作 ---
        manual = ttk.LabelFrame(self.root, text=" 手动操作 ")
        manual.pack(fill="x", **pad)
        row = ttk.Frame(manual)
        row.pack(fill="x", padx=10, pady=10)
        self.btn_state = ttk.Button(row, text="查看状态", command=self.action_state)
        self.btn_in = ttk.Button(row, text="立即签到", command=self.action_sign_in)
        self.btn_out = ttk.Button(row, text="立即签退", command=self.action_sign_out)
        self.btn_state.pack(side="left")
        self.btn_in.pack(side="left", padx=8)
        self.btn_out.pack(side="left")

        # --- 自动打卡 ---
        auto = ttk.LabelFrame(self.root, text=" 自动打卡（按 schedule.json 的时间段自动签到/签退） ")
        auto.pack(fill="x", **pad)
        row2 = ttk.Frame(auto)
        row2.pack(fill="x", padx=10, pady=10)
        self.btn_start = ttk.Button(row2, text="▶ 开始自动打卡", command=self.action_start_auto)
        self.btn_stop = ttk.Button(row2, text="■ 停止", command=self.action_stop_auto, state="disabled")
        self.btn_start.pack(side="left")
        self.btn_stop.pack(side="left", padx=8)
        ttk.Label(row2, text="对账间隔（分钟）：").pack(side="left", padx=(20, 4))
        ttk.Spinbox(row2, from_=1, to=60, width=4,
                    textvariable=self.interval_var).pack(side="left")
        ttk.Button(row2, text="创建桌面快捷方式", command=self._create_shortcut).pack(side="right")

        # --- 日志 ---
        logbox = ttk.LabelFrame(self.root, text=" 运行日志 ")
        logbox.pack(fill="both", expand=True, **pad)
        self.log_text = scrolledtext.ScrolledText(
            logbox, height=12, font=("Consolas", 9), wrap="word",
            background="#0d1117", foreground="#c9d1d9", insertbackground="#c9d1d9",
        )
        self.log_text.pack(fill="both", expand=True, padx=10, pady=10)
        self.log_text.configure(state="disabled")

        self._log_line(f"{TITLE} 已启动，工作目录：{BASE_DIR}")

    def _toggle_password(self) -> None:
        self.password_entry.configure(show="" if self.show_password.get() else "●")

    def _refresh_plan_summary(self) -> None:
        try:
            plan = today_plan()
        except Exception as exc:  # 配置坏了也不该让界面起不来
            self.plan_label.configure(text=f"读取排班失败：{exc}")
            return
        now = core.beijing_naive()
        if not plan:
            self.plan_label.configure(text=f"今日（{now:%m-%d %a}）没有考勤安排")
            return
        spans = "、".join(f"{s:%H:%M}-{e:%H:%M}" for s, e in plan)
        current = "在时段内" if core.desired_signed_in(now, plan) else "不在时段内"
        self.plan_label.configure(text=f"今日（{now:%m-%d %a}）考勤时段：{spans}（此刻{current}）")

    # --------------------------------------------------------- 线程工具 --

    def _core_log(self, message: str) -> None:
        """替换 attendance.log，把核心逻辑的输出转发到界面。"""
        self.events.put(("log", f"[{core.beijing_naive():%H:%M:%S}] {message}"))

    def _log_line(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _set_status(self, text: str, color: str, detail: str = "") -> None:
        self.status_var.set(text)
        self.status_label.configure(foreground=color)
        if detail:
            self.detail_var.set(detail)

    def _credentials(self) -> tuple[str, str] | None:
        username = self.username_var.get().strip()
        password = self.password_var.get()
        if not username or not password:
            messagebox.showwarning(TITLE, "请先填写学号和密码。")
            return None
        # Tk 变量只能在主线程读，先取值存下来给工作线程用
        self._pending_remember = bool(self.remember_var.get())
        return username, password

    def _busy(self, running: bool) -> None:
        state = "disabled" if running else "normal"
        for btn in (self.btn_state, self.btn_in, self.btn_out, self.btn_start):
            btn.configure(state=state)
        self.btn_stop.configure(state="normal" if self.auto_running else "disabled")

    def _spawn(self, func, *args, kind: str = "manual") -> None:
        """本方法的调用者负责先用 _busy(True) 锁住按钮。"""
        self._job_kind = kind

        def runner() -> None:
            try:
                func(*args)
            except DgutError as exc:
                self.events.put(("error", str(exc)))
            except Exception:
                self.events.put(("error", "程序异常：\n" + traceback.format_exc()))
            finally:
                self.events.put(("done", None))

        self.worker = threading.Thread(target=runner, daemon=True)
        self.worker.start()

    # ------------------------------------------------------- 一次性动作 --

    def _connect(self, username: str, password: str) -> XgxtClient:
        client = XgxtClient(username, password)
        client.login()
        self.events.put(("log", "[登录成功]"))
        if getattr(self, "_pending_remember", False):
            save_account(username, password)
        return client

    def _waid(self) -> str | None:
        return core.get_config(CONFIG_FILE).get("workAssignmentId")

    def _report_state(self, state) -> None:
        detail = f"职位：{state.work_assignment_name}（workAssignmentId={state.work_assignment_id}）"
        if state.signed_in and state.open_started_at:
            detail += f"　本次签到于 {state.open_started_at:%H:%M:%S}"
        self.events.put(("state", (state, detail)))

    def action_state(self) -> None:
        creds = self._credentials()
        if not creds:
            return
        self._busy(True)
        self._log_line("— 正在查看服务端状态 —")

        def job(username: str, password: str) -> None:
            client = self._connect(username, password)
            state = client.get_attendance_state(self._waid())
            self._report_state(state)
            self.events.put(("log", f"服务端返回：{state.describe()}"))

        self._spawn(job, *creds)

    def action_sign_in(self) -> None:
        self._manual_sign("in")

    def action_sign_out(self) -> None:
        self._manual_sign("out")

    def _manual_sign(self, which: str) -> None:
        creds = self._credentials()
        if not creds:
            return
        action = "签到" if which == "in" else "签退"
        if not messagebox.askyesno(TITLE, f"确定要立即{action}吗？"):
            return
        self._busy(True)
        self._log_line(f"— 正在执行手动{action} —")

        def job(username: str, password: str) -> None:
            client = self._connect(username, password)
            waid = self._waid()
            before = client.get_attendance_state(waid)
            if which == "in":
                if before.signed_in:
                    self.events.put(("log", f"当前已经是「{before.describe()}」，无需重复签到"))
                    self._report_state(before)
                    return
                after = client.sign_in(waid)
            else:
                if not before.signed_in:
                    self.events.put(("log", "当前本来就不在岗，无需签退"))
                    self._report_state(before)
                    return
                after = client.sign_out(waid)
            self.events.put(("log", f"{action}完成，当前状态：{after.describe()}"))
            self._report_state(after)

        self._spawn(job, *creds)

    # --------------------------------------------------------- 自动打卡 --

    def action_start_auto(self) -> None:
        creds = self._credentials()
        if not creds:
            return
        try:
            interval = max(1, int(self.interval_var.get())) * 60
        except (tk.TclError, ValueError):
            interval = 600
        self.stop_flag.clear()
        self.auto_running = True
        self._busy(True)
        self._set_status("自动打卡运行中", COLOR_BUSY, "关闭窗口或点「停止」即可结束")
        self._log_line(f"— 启动自动打卡，对账间隔 {interval // 60} 分钟 —")
        self._spawn(self._auto_loop, *creds, interval, kind="auto")

    def _auto_loop(self, username: str, password: str, interval: int) -> None:
        config = core.get_config(CONFIG_FILE)
        waid = config.get("workAssignmentId")
        try:
            plan = today_plan()
        except Exception as exc:
            self.events.put(("log", f"[读取排班失败] {exc}"))
            plan = []

        if not plan:
            self.events.put(("log", "[程序结束] 今日没有考勤安排，仅做一次收尾检查"))
        else:
            for start, end in plan:
                self.events.put(("log", f"  今日考勤时段：{start:%H:%M} - {end:%H:%M}"))

        skip = core.should_skip_today(config, core.get_special_schedule(SPECIAL_FILE))
        if skip:
            self.events.put(("log", f"[程序结束] {skip}"))
            return

        client = self._connect(username, password)
        points = core.boundaries(plan)

        while not self.stop_flag.is_set():
            try:
                core.reconcile_once(client, waid, plan)
                self._report_state(client.get_attendance_state(waid))
            except DgutError as exc:
                # 单轮失败不致命：下一轮对账会自动重试
                self.events.put(("log", f"[本轮对账失败] {exc}"))

            now = core.beijing_naive()
            upcoming = [p for p in points if p > now]
            if not upcoming:
                # 关键收尾：上面那轮用的 now 取自 HTTP 之前，可能还停在「时段内」。
                # 这里用全新时间再对账一次，确保不会带着「在岗」状态退出。
                try:
                    core.reconcile_once(client, waid, plan)
                    self._report_state(client.get_attendance_state(waid))
                except DgutError as exc:
                    self.events.put(("log", f"[收尾对账失败] {exc}"))
                self.events.put(("log", "[程序结束] 今日考勤时段已全部结束"))
                break

            sleep_for = max(5.0, min((upcoming[0] - now).total_seconds(), interval))
            self.stop_flag.wait(sleep_for)  # 可被「停止」立刻打断

    def action_stop_auto(self) -> None:
        self.stop_flag.set()
        self.auto_running = False
        self.btn_stop.configure(state="disabled")
        self._log_line("— 已请求停止，等待当前一轮结束 —")

    # ----------------------------------------------------------- 收尾 --

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self._log_line(str(payload))
                elif kind == "state":
                    state, detail = payload  # type: ignore[misc]
                    if state.signed_in:
                        self._set_status("已签到（在岗中）", COLOR_WORKING, detail)
                    else:
                        self._set_status("未签到", COLOR_IDLE, detail)
                    self._refresh_plan_summary()
                elif kind == "error":
                    text = str(payload)
                    self._log_line("[错误] " + text)
                    self._set_status("出错了", COLOR_ERROR, text.splitlines()[0][:200])
                elif kind == "done":
                    was_auto = self._job_kind == "auto"
                    self.auto_running = False
                    self._job_kind = None
                    self._busy(False)
                    if was_auto:
                        self._log_line("[界面] 自动打卡线程已退出")
        except queue.Empty:
            pass
        self.root.after(120, self._drain_events)

    def _save_account(self) -> None:
        creds = self._credentials()
        if not creds:
            return
        save_account(*creds)
        self._log_line(f"账号已保存到 {ACCOUNT_FILE.name}（本机文件，不会上传 GitHub）")
        messagebox.showinfo(TITLE, "账号已保存到本机 account.ini。\n\n"
                                   "该文件已在 .gitignore 中忽略，不会被提交到 GitHub。")

    def _create_shortcut(self) -> None:
        bat = BASE_DIR / "启动打卡助手.bat"
        if not bat.exists():
            messagebox.showwarning(TITLE, f"没找到 {bat.name}，无法创建快捷方式。")
            return
        script = (
            "$d=[Environment]::GetFolderPath('Desktop');"
            "$s=(New-Object -ComObject WScript.Shell).CreateShortcut("
            f"\"$d\\{TITLE}.lnk\");"
            f"$s.TargetPath='{bat}';"
            f"$s.WorkingDirectory='{BASE_DIR}';"
            f"$s.Description='{TITLE}';"
            "$s.Save()"
        )
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                check=True, capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:
            self._log_line(f"[创建快捷方式失败] {exc}")
            messagebox.showerror(TITLE, f"创建快捷方式失败：\n{exc}")
            return
        self._log_line("已在桌面创建快捷方式")
        messagebox.showinfo(TITLE, f"已创建桌面快捷方式「{TITLE}」。")

    def _on_close(self) -> None:
        if self.worker and self.worker.is_alive() and self.auto_running:
            if not messagebox.askyesno(TITLE, "自动打卡正在运行，确定要退出吗？\n"
                                              "退出后就不会再自动打卡了。"):
                return
        self.stop_flag.set()
        self.root.destroy()


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #

def main() -> int:
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        # 极少数情况下（例如通过某些远程/无桌面环境启动）会失败，给出可读提示
        CRASH_LOG.write_text(f"无法创建窗口：{exc}\n", encoding="utf-8")
        messagebox.showerror(TITLE, f"无法启动图形界面：{exc}")
        return 1

    try:
        ttk.Style().theme_use("vista" if sys.platform == "win32" else "default")
    except tk.TclError:
        pass

    Dashboard(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        # pythonw 下没有控制台，把崩溃信息同时写文件和弹窗，否则会「双击没反应」
        detail = traceback.format_exc()
        try:
            CRASH_LOG.write_text(detail, encoding="utf-8")
        except OSError:
            pass
        try:
            messagebox.showerror(TITLE, "程序崩溃：\n\n" + detail[-1500:])
        except Exception:
            pass
        raise
