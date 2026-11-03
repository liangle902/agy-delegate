---
name: agy-delegate
description: >
  把脏活累活（批量改代码、补测试、按明确方案实现功能、机械性重构、跑验证、独立审计、批量数据清洗）派给本机
  Antigravity CLI (agy) 执行，由主代理负责拆任务、监控、纠偏和验收。当用户说 "让 agy 做 / 派给 agy /
  用 Gemini 额度干这个"，或任务量大、方案已明确、不值得消耗主代理额度时使用。不用于需求还没澄清的任务、
  需要生产环境凭据或不可逆操作的任务。
---

# agy-delegate（v0.6）

这是 **Claude Code 的入口文件**。完整的、与具体主代理无关的调用说明在同目录的 **`GUIDE.md`**，Claude Code 里怎么用在 **`adapters/claude-code.md`**。

## 先做这两步

1. 读 `GUIDE.md`（通用调用说明：命令、输出协议、返回码、任务书怎么写、干预与续接、适配层规程）。
2. 读 `adapters/claude-code.md`（Monitor 怎么启动、PushNotification 什么时候发、怎么给用户开实时窗口、已知的坑）。

运行器：

```
python ~/.claude/skills/agy-delegate/agy_run.py <quota|run|wait|watch|status|list|result|kill|gc|selftest>
```

## 必须记住的几条

- **所有调用都经过运行器**，不要直接拼 `agy` 命令（也不要在 Git Bash 里手敲 `agy -p "/usage"`，查额度用 `quota` 子命令）。
- agy 的 SUCCESS 和退出码 0 都不可信；`DONE_UNVERIFIED` 只表示自称完成，必须自己验收（看 diff、重跑测试）。
- agy 汇报里的数字（测试数、覆盖率）要自己重跑核对；任务书里要写明**不要改动 git 暂存区**，验收时看 `git diff --cached`。
- **没有告警时不要评论进度**；有告警必须当次处理完，并用一句话告诉用户你的决定。
- `QUOTA` / `AUTH` 告警：立刻 PushNotification 通知用户换账号，然后停下，不要重试。换账号只能用户做。
- 长任务（可能超过 25 分钟）用 `--detach` + `wait`，因为 Monitor 单次有时间上限。
- 临时目录里的试验一律加 `--throwaway`。
- 几行就能改完的事自己改，不要派。

## 其他主代理

如果你不是 Claude Code，或者这里没有你的适配层，**不要用本文件**，改读 `GUIDE.md` 第 11 节，按规程自己补一份适配层。
