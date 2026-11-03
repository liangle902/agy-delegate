# agy-delegate

[English](README.md)

一个运行器：让任意编程代理（或普通脚本）把活交给本机的 [Antigravity CLI](https://antigravity.google/)（`agy`）去做，并负责监督：启动任务、跟踪事件、卡住或打转时终止它、重新判定结果。

需要判断的事仍由调用方（主代理）负责：澄清需求、写任务书、审 diff。`agy` 负责量大的活：批量修改、补测试、按定好的方案实现功能、机械性重构、跑检查。

> 本项目与 Google 无关。"Antigravity" 是其所有者的商标。

## 为什么需要运行器

直接调用 `agy -p` 做无人值守的任务并不可靠：

- 无头模式下提问被跳过、权限被拒、`--print-timeout` 把回答截断时，agy 同样报 `status: SUCCESS`、退出码 0。运行器根据事件流重新判定结论。
- 挂起的命令或反复重试的循环可能很久都没有任何输出。运行器能识别卡住、死循环和长时间没有进展，并自己终止 agy。
- 每个任务都会留下项目和会话。运行器按目录复用同一个项目，一次性运行结束后可以自动清理。
- 运行器进程不管怎么退出，Windows 的 Job Object 都会把 agy 和它启动的所有子进程一起带走。

## 环境要求

- Windows 10/11
- Python 3.10 及以上
- 已安装并登录 Antigravity CLI（`agy --version` 能正常输出）
- Git，用来查看 agy 改了什么

## 安装

```bash
git clone https://github.com/liangle902/agy-delegate.git
cd agy-delegate
python deploy.py deploy
```

`deploy.py` 会把 `skill/` 复制到 `~/.claude/skills/agy-delegate/`，Claude Code 会把它当作技能加载。其他代理可以直接运行 `skill/agy_run.py`。想部署到别的位置，设置环境变量 `AGY_DELEGATE_LIVE`。

检查环境（不消耗额度）：

```bash
python skill/agy_run.py selftest
```

## 用法

```bash
python skill/agy_run.py run --cwd <仓库目录> --prompt-file task.md --name fix-tests --timeout 40m
```

默认只输出需要处理的行（`ALERT …`、`WARN …`），以及每个运行恰好一行 `END …`，里面有结论、返回码和 `result.json` 的路径。只能"执行命令并等它返回"的代理，用 `--detach` 启动，再反复调用 `wait`。

其他子命令：`quota`、`wait`、`watch`、`status`、`list`、`result`、`kill`、`gc`、`selftest`。

完整说明见 [`skill/GUIDE.md`](skill/GUIDE.md)：命令、输出协议、返回码、任务书怎么写、怎么续接，以及新的主代理怎么自己补一份适配层。各主代理的具体做法在 [`skill/adapters/`](skill/adapters/)。

## 测试

测试用一个假的 `agy`，在隔离的家目录里跑，不消耗额度，也不会碰你真实的项目和会话：

```bash
python -I skill/tests/tests.py
```

## 许可证

[Apache-2.0](LICENSE)
