# 适配层：Claude Code（含桌面端）

本文件只讲在 Claude Code 里**怎么用**通用调用说明（`../GUIDE.md`）。事件含义、返回码、任务书怎么写，都以 `GUIDE.md` 为准，这里不重复。

## 能力盘点

| 能力 | Claude Code 里的做法 | 模式 |
|---|---|---|
| A. 后台长命令 + 输出唤醒 | **Monitor 工具**：每输出一行就唤醒你 | 模式 A（首选） |
| B. 主动通知用户 | **PushNotification 工具**（桌面通知） | 额度/登录告警必用 |
| C. 给用户开实时窗口 | `mcp__terminal__run_in_terminal` 开一个终端标签跑 `watch latest`，把标签链接告诉用户。**有的会话没有接这个工具**：那就把完整的 `python <RUNNER路径> watch latest` 命令写进回复，让用户自己在终端跑 | 可用时用 |
| D. 验收 | Read / Grep / Bash（`git diff`、跑测试） | 必备 |

## 启动并监控（模式 A）

用 **Monitor** 工具运行（不要用后台 Bash）：

```
python -u <RUNNER路径> run --cwd <仓库> --prompt-file <任务书> --name <短名> --timeout 40m
```

- **必须用 Monitor**。用后台 Bash 启动时，中途的告警不会唤醒你，只有进程退出才会。Monitor 不可用而只能用后台 Bash 时，**绝不能**关掉自动终止（`--idle-kill 0`、`--loop-kill 0`）。
- Monitor 的 `timeout_ms` 要大于 `--timeout`，**但 Monitor 自己有单次上限（约 30 分钟）**。任务可能超过 25 分钟时，改用下面的"长任务"写法，否则 Monitor 到期会连带杀掉运行器。
- 一两分钟的小任务（提交、查状态）直接前台运行。
- 任务书放在 scratchpad 目录里。

### 长任务（可能超过 25 分钟）
```
python <RUNNER路径> run --cwd <仓库> --prompt-file <任务书> --name <短名> --timeout 90m --detach
```
拿到 `STARTED run=<运行名>` 后，用 Monitor 监控等待命令：
```
python -u <RUNNER路径> wait <运行名> --until end --timeout 1500
```
Monitor 到期（或 wait 返回码 11）后**重新挂上同一条命令**，运行不受影响。这是模式 B 和 Monitor 的组合用法，运行器不依赖 Monitor 存活。

## 给用户开实时窗口（不花你的 token）

启动后用 `mcp__terminal__run_in_terminal` 开一个标签，跑 `python <RUNNER路径> watch latest`，并把标签链接告诉用户。

**如果这个会话没有该工具**（工具列表里找不到 `mcp__terminal__*`），不要硬找替代办法：在回复里直接给出完整命令，请用户自己在终端运行，并说明这是给人看的实时视图、不影响任务本身。

## 事件处理的几点补充（Claude Code 特有）

- 每个 Monitor 事件都会唤醒你重读整段上下文，这是你这一侧 token 开销的大头：**没有告警时不要回应、不要评论进度**。
- `ALERT QUOTA*` / `ALERT AUTH` / 返回码 3：**立刻用 PushNotification 发桌面通知**让用户换账号重新登录，然后停下等待，不要重试。（如果启动时配了 `--notify-cmd`，运行器已经自己通知过，你仍要发 PushNotification 并在回复里告诉用户。）
- `ALERT SANDBOX`：提醒用户在 agy 的 `/config` 里关掉 Sandbox Mode。你自己改这个设置会被自动模式分类器拦截。
- `END` 之后先回一句"已结束，正在验收"，再用 `result` 读结果。

## 已知的坑

- **Git Bash 路径改写**：在 Bash 工具里直接敲 `agy -p "/usage"`，`/usage` 会被改写成 `<Git 安装目录>/usage` 并真的发给模型（花约 1.3 万 token，还会建会话）。查额度用 `python <RUNNER路径> quota`，别手敲 `agy`。需要手敲时加 `MSYS_NO_PATHCONV=1`。
- **弹窗**：在没有控制台的环境里（桌面端的工具进程）启动命令行程序会各弹一个窗口。运行器自己的 `git`/`taskkill`/`agy` 调用已经带了不弹窗标记；你自己写的辅助命令（`tasklist`、`powershell`…）也要注意，能用 Python 的 `creationflags=0x08000000` 就用。
- 自动模式的安全检查可能拦下"批量删除/改动 `~/.gemini` 里文件"这类命令；被拦了不要改写绕过，告诉用户并建议在默认权限模式下执行。
- 任务期间 agy 在用户工作区里改文件，用户若同时在跑热重载的应用，要提醒。

## 谁来做什么（Claude Code 里的分工）

- **你（调用方）**：澄清需求、定方案、写任务书、监控、纠偏、验收、向用户汇报。几行能改完的事你直接改，不要派。
- **派发 Claude 子代理之前**，按用户的习惯决定要不要先问；批量的机械活优先交给 agy。
- 汇报时附上 `END` 行里的额度（`q5h`）、用时、用量，明确写出没验证到的部分。
