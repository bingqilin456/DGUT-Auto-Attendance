# 莞工学工系统 · 自动考勤

莞工学工系统（[stu.dgut.edu.cn](https://stu.dgut.edu.cn/)）勤工助学岗位的自动打卡工具，适用于学生助理、助理班主任等职位的日常考勤：**该签到的时候自动签到，该签退的时候自动签退，避免"忘记打卡"和"打了卡忘记签退"。**

> 本仓库基于 [Bertramoon/DGUT-Auto-Attendance](https://github.com/Bertramoon/DGUT-Auto-Attendance)
> 重写。原版依赖的第三方库和页面路径在 2022 年之后就已全部失效，**2026 年直接使用原版是无法打卡的**，
> 具体原因见 [第 2 节](#2-为什么原版失效了)。

---

# 1. 项目概述

## 1.1. 工作原理

程序采取 **对账（reconciliation）** 的思路，而不是"到点触发一次"：

```
每轮循环：
  1. 算出「此刻我应该在岗吗？」        ← 来自 schedule.json / special.json
  2. 读服务端真实状态                  ← 学工系统上「上岗考勤」页面
  3. 不一致才发动作（签到 / 签退）
  4. 动作之后再读一次页面，确认真的生效
```

这样做的好处是 **幂等 + 自愈**：

- GitHub Actions 排队、延迟、漏跑一轮，下一轮会自动补上；
- 同一个时刻重复运行也不会重复打卡；
- 每一次动作都有服务端返回的状态作为证据，不会出现"日志说成功、实际没打上"。

## 1.2. 运行方式

GitHub Actions 单个 job 最长只能跑 360 分钟，因此工作流每天启动两次
（北京时间 **07:30** 和 **13:30**，对应 UTC 的 `23:30` 和 `05:30`），
每次运行最长 5.5 小时，覆盖当天的考勤时段。

## 1.3. 桌面版：双击就能跑（推荐）

不想碰命令行的话，用桌面版：

1. 双击仓库里的 **`启动打卡助手.bat`**；
2. 第一次运行会自动装依赖（约 1–2 分钟），之后直接弹出窗口；
3. 填学号密码，勾上「记住账号」，以后就不用再填；
4. 想全自动就点 **▶ 开始自动打卡**，窗口可以最小化，它会按 `schedule.json` 自己签到签退。

窗口上的按钮：

| 按钮 | 作用 |
| --- | --- |
| 查看状态 | 只读一次服务端状态，**不产生任何考勤记录** |
| 立即签到 / 立即签退 | 手动补一次；已经在岗（或本来不在岗）时不会重复动作 |
| ▶ 开始自动打卡 | 按排班表自动签到签退，日志实时显示在窗口里 |
| ■ 停止 | 结束自动打卡 |
| 创建桌面快捷方式 | 在桌面放一个图标，以后双击图标就能打开 |

桌面版**复用同一套对账循环**，所以和云端版行为完全一致（幂等、可自愈）。
账号只保存在本机的 `account.ini`，该文件已被 `.gitignore` 忽略，**不会上传到 GitHub**。

> 前提：电脑上装了 Python 3.10+ 并勾选了 "Add python.exe to PATH"。
> 没装的话 `.bat` 会提示下载地址。

---

# 2. 为什么原版失效了

原版 `attendance.py` 依赖 PyPI 上的 `dgut-requests`（**最后发布于 2022-10-04**），它有两个致命问题：

## 2.1. 登录其实从未成功

`dgut-requests` 的 `login()` 在向中央认证（CAS）提交时 **没有携带 `service` 参数**，
于是 CAS 把登录票据发给了默认服务，而不是学工系统。

随后它去访问学工系统时，服务器返回的 body 只有 152 字节的一段 **JavaScript 跳转**：

```html
<script>window.location.href='https://auth.dgut.edu.cn/authserver/login?service=...'</script>
```

`requests` 不会执行 JS，所以会话自始至终都没有建立起来。
但库内部却无条件把 `is_authenticated` 置为 `True`，于是所有后续请求都静默失败。

## 2.2. 无论成败都报告"签到成功"

原库的 `attendance()` 方法拿到响应后 **不做任何校验**，直接返回 `"签到成功"`。
所以日志里永远是成功，实际可能什么都没发生。

## 2.3. 页面路径和表单都变了

| 项目 | 2022 原版 | 现在（2026） |
|---|---|---|
| 勤工助学模块 | `/student/partwork/` | `/student/partWorkNew/` |
| 考勤页面 | `attendance.jsp` | `attendancePre.jsp` |
| 表单字段 | 单个 `session_token` | **同名 `session_token` 出现两次** |

> 关于 `session_token`：页面上有两个同名的隐藏字段，浏览器会把两个都提交。
> 用 `requests` 时如果用 `dict` 传参会丢掉一个，必须传 `list[tuple]`。
> 这一点在 `attendance.py` 的 `_payload()` 里已经处理。

## 2.4. 本仓库的做法

- **不再依赖 `dgut-requests`**，登录逻辑重写在 `dgut_client.py` 里：
  CAS 提交时直接带上 `service=https://stu.dgut.edu.cn/`，一次拿到有效会话；
- 登录页解析改为 **逐个 `<input>` 解析**，而不是原库那种"一条按固定顺序跨 6 个字段的大正则"
  （页面只要稍微重排，那种正则就会静默匹配失败）；
- 每次动作之后 **重新读取页面确认状态**，没生效就抛异常。

---

# 3. 部署

## 3.1. fork 仓库

![fork仓库](https://gitee.com/bertramoon/img/raw/master/Auto_Attendance/Fork%20repository.png "")

## 3.2. 设置 Secrets

仓库 **Settings → Secrets and variables → Actions**，添加：

| Secret 名称 | 含义 | 必填 | 示例 |
|---|---|:---:|---|
| `USERNAME` | 中央认证账号（学号） | ✅ | `20xxxxxxxxx` |
| `PASSWORD` | 中央认证密码 | ✅ | `********` |
| `SERVER_KEY` | Server酱 SendKey（微信推送通知） | ❌ | `SCT123456...` |

> `SERVER_KEY` 只填 **SendKey 本身** 即可（例如 `SCT123456...`）。
> 为兼容旧配置，填成 `-K SCT123456...` 这种写法程序也能识别。

## 3.3. 设置考勤时间（`schedule.json`）

**不需要改 Python 代码**，只改 `schedule.json`：

- key `"0"`–`"6"` 表示 **星期日 – 星期六**（每周第一天是星期日）；
- value 是一个列表，每个元素是 `["开始时间", "结束时间"]`；
- 时间必须严格写成 `"时:分"`（`"8:30"` 可以，`"8:3"`、`"8:30:00"` 不行）；
- 空列表 `[]` 表示当天不考勤。

```json
{
    "0": [],
    "1": [["8:30", "12:00"], ["14:30", "16:00"]],
    "2": [["8:30", "12:00"]],
    "3": [],
    "4": [["8:30", "10:10"]],
    "5": [["10:25", "12:00"]],
    "6": []
}
```

上表含义：周日、周三、周六不考勤；周一是 `8:30-12:00` 和 `14:30-16:00` 两段；
以此类推。

## 3.4. 某一天特殊安排（`special.json`）

需要单独调整**某一天**时用这个文件，格式是 `"年-月-日": [[开始, 结束]]`。
**只要某天出现在这个文件里，就以它为准**，同时也会忽略"节假日不打卡"的判断。

```json
{
    "2026-10-01": [],
    "2026-10-08": [["9:00", "11:30"]]
}
```

- `"2026-10-01": []` → 这天不打卡（国庆）；
- `"2026-10-08": [["9:00", "11:30"]]` → 这天改在 9:00–11:30 打卡。

## 3.5. 配置 `config.ini`

```ini
[attendance]
holiday_attendance = True
workAssignmentId = 38605
```

- **`holiday_attendance`**：`True` = 法定节假日也照常打卡；`False` = 跳过节假日。
- **`workAssignmentId`**：考勤职位的 ID。只有一个职位时可以留空（自动取第一个）；
  有多个职位时必须指定，否则会打错岗位。

> 查看自己的 `workAssignmentId`：登录学工系统 → **勤工助学 → 上岗考勤**，
> 页面上的"工作考勤"下拉框里，`<option value="38605">计算机学院学生工作助理</option>`
> 中的数字就是它。本仓库已按当前账号填好（`38605`）。

## 3.6. 开启 Actions

仓库 **Actions** 页面 → 启用 workflow。
工作流也可以 **手动触发**（`workflow_dispatch`），方便测试。

---

# 4. 本地运行与测试

```bash
pip install -r requirements.txt

# 只看状态，不打卡
python dgut_client.py state -U <学号> -P <密码>

# 手动签到 / 签退
python dgut_client.py in  -U <学号> -P <密码>
python dgut_client.py out -U <学号> -P <密码>

# 完整跑一遍主程序（--dry-run 只报告、不真的打卡）
python attendance.py -U <学号> -P <密码> --dry-run --once
python attendance.py -U <学号> -P <密码> --once
```

也可以把账号放在环境变量里，避免出现在命令行历史中：

```bash
export DGUT_USERNAME=20xxxxxxxxx
export DGUT_PASSWORD=********
python dgut_client.py state
```

## 常用参数

| 参数 | 说明 |
|---|---|
| `--once` | 只做一轮对账就退出（适合手动测试、或高频 cron） |
| `--dry-run` | 只打印"将要做什么"，不真正打卡 |
| `--interval` | 对账间隔秒数，默认 `600`；到达考勤时刻会提前唤醒 |
| `--max-hours` | 单次运行最长小时数，默认 `5.5`（Actions 上限 6 小时） |
| `--schedule` / `--special` / `--config` | 指定配置文件路径 |
| `-W` | 指定 `workAssignmentId`，优先级高于 `config.ini` |

---

# 5. 项目结构

```
DGUT-Auto-Attendance
│  gui.py               桌面版图形界面（对账逻辑复用 attendance.py）
│  启动打卡助手.bat      双击运行桌面版（自动装依赖 + 打开窗口）
│  attendance.py        主程序：读取计划 + 对账循环
│  dgut_client.py       学工系统客户端：CAS 登录、读状态、签到、签退
│  config.ini           节假日是否打卡、考勤职位 ID
│  schedule.json        每周考勤时间表
│  special.json         某一天的特殊考勤安排
│  requirements.txt     依赖
│  README.md
│  account.ini          桌面版保存的本机账号（已 gitignore，不会上传）
│
└─.github/workflows/
      main.yml          GitHub Actions 工作流
```

依赖只有三个：`requests`、`pycryptodome`（CAS 密码加密）、`chinesecalendar`（节假日判断，可选）。

---

# 6. 常见问题

## 6.1. 设置了 8:30 打卡，为什么工作流 7:30 就启动了？

GitHub Actions 的定时任务**经常延迟几分钟到几十分钟**才真正开始运行。
所以工作流提前启动，启动后由程序自己等待到考勤时刻再打卡。
这也正是采用"对账循环"的原因：无论何时启动，只要启动时还没错过打卡时刻（或刚好在时段内），都能正确打卡。

## 6.2. 会不会泄露我的账号密码？

不会，两条路都不进仓库：

- **云端版**：账号密码存在 GitHub Actions Secrets 里，由 GitHub 保管，不会出现在仓库文件或日志里；
- **桌面版**：存在本机的 `account.ini`，该文件已在 `.gitignore` 中忽略，
  `git status` 里根本不会出现它。

## 6.3. 打卡时段中途程序退出了，会不会一直留在"在岗"状态？

不会。下次运行时会自动收尾：

- 若这条"在岗"记录是**更早的日期**开的，或**今天已经有考勤时段结束过**，程序会自动签退；
- 若都不满足（例如用户在非考勤时段自己手动签了到），程序会 **保持原样并打印原因**，
  不会贸然替你签退，以免破坏真实工时。

## 6.4. 学校又改版了怎么办？

页面结构变化时，程序会抛出 `PageChangedError` 或 `DgutError` 并在日志里说明具体哪个字段没找到，
而不是像原版那样静默地"假装成功"。看到这类报错就说明该更新解析逻辑了。

## 6.5. 节假日到底打不打卡？

由 `config.ini` 里的 `holiday_attendance` 决定：

- `True`：**每天都按排班表打**，法定节假日照打（本仓库当前是这个值，适合"节假日也要值班"的岗位）；
- `False`：先用 `chinesecalendar` 判断，是法定休息日就整天不打卡。

不管这个值是什么，**只要某一天写进了 `special.json`，就以 `special.json` 为准**：

```json
{ "2026-10-01": [] }
```

空列表的意思是 **这天不考勤**，程序会明确跳过，不会退回星期表去打卡。

## 6.6. 桌面版的窗口一闪就没了怎么办？

说明启动时崩了。崩溃信息会写进同目录的 `crash.log`，同时弹出提示框；
也可以直接在命令行里跑 `python gui.py`，这样错误会完整打印出来。

---

# 7. 更新日志

## 2026 重写版

- **重写登录逻辑**：不再依赖已停更的 `dgut-requests`，CAS 提交时携带 `service`，
  修复"会话从未建立却被标记为已登录"的问题；
- **适配新版页面**：`/student/partwork/` → `/student/partWorkNew/attendancePre.jsp`，
  处理页面上重复出现的 `session_token`；
- **改为对账循环**：幂等、可自愈，漏跑能补，重复运行不会重复打卡；
- **动作后校验状态**：不再无条件报告成功；
- **安全签退**：不会误签退用户自己手动开的在岗记录；
- **更新 CI**：`ubuntu-18.04` + Python 3.7（均已不可用）→ `ubuntu-latest` + Python 3.11，
  Actions 版本升级到 `checkout@v4` / `setup-python@v5`，并支持手动触发；
- **新增桌面版**（`gui.py` + `启动打卡助手.bat`）：双击运行，可查看状态、手动签到签退、
  一键自动打卡，账号存本机 `account.ini`（已 gitignore）；
- **修复 `special.json` 空列表失效**：过去把某天写成 `[]`（意思是"这天不考勤"），
  程序会因为空列表是假值而**退回星期表照常打卡**。现在用 `get_special_schedule()`
  区分「没表态」（`None` → 用星期表）和「明确不考勤」（`[]` → 整天不打）。

## v2022-2-1（原版）

- 修复 bug；添加 Server酱消息通知功能。

## v2022-1-31（原版）

- 重构 `attendance.py`，改用 `schedule` 定时替代 `sleep` 阻塞；
- 设置虚拟环境为 `ubuntu-18.04`。

---

# 8. 参考资料

- [原仓库 Bertramoon/DGUT-Auto-Attendance](https://github.com/Bertramoon/DGUT-Auto-Attendance)
- [chinesecalendar · PyPI](https://pypi.org/project/chinesecalendar/)
- [GitHub Actions 入门教程](http://www.ruanyifeng.com/blog/2019/09/getting-started-with-github-actions.html)

---

# 9. 致谢

原项目作者：**3233406405@qq.com**（[Bertramoon](https://github.com/Bertramoon)）。
本仓库在其基础上适配 2026 年的学工系统。
