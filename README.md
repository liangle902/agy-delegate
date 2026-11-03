# agy-delegate

[中文说明](README.zh-CN.md)

A runner that lets any coding agent (or a plain script) hand work to the local [Antigravity CLI](https://antigravity.google/) (`agy`) and supervise it: start a task, watch its events, stop it when it hangs or loops, and judge the result.

The calling agent keeps the decisions — clarifying the task, writing the brief, reviewing the diff. `agy` does the bulk work: batch edits, adding tests, implementing a settled design, mechanical refactors, running checks.

> Not affiliated with Google. "Antigravity" is a trademark of its owner.

## Why a runner

Driving `agy -p` directly is unreliable for unattended work:

- `status: SUCCESS` and exit code 0 are also reported when a question was skipped in headless mode, a permission was denied, or `--print-timeout` cut the answer short. The runner re-derives the verdict from the event stream.
- A hung command or a retry loop can run for a long time without any output. The runner detects stalls, loops and no-progress windows and stops `agy` itself.
- Every task leaves a project and a conversation behind. The runner reuses one project per directory and can clean up throwaway runs.
- If the runner dies, a Windows job object takes `agy` and all of its child processes with it.

## Requirements

- Windows 10/11
- Python 3.10+
- Antigravity CLI installed and signed in (`agy --version` works)
- Git, for reviewing what `agy` changed

## Install

```bash
git clone https://github.com/liangle902/agy-delegate.git
cd agy-delegate
python deploy.py deploy
```

`deploy.py` copies `skill/` to `~/.claude/skills/agy-delegate/`, where Claude Code picks it up as a skill. Other agents can run `skill/agy_run.py` from anywhere. Set `AGY_DELEGATE_LIVE` to deploy somewhere else.

Check the environment (uses no quota):

```bash
python skill/agy_run.py selftest
```

## Usage

```bash
python skill/agy_run.py run --cwd <repo> --prompt-file task.md --name fix-tests --timeout 40m
```

By default the runner prints only lines that need attention (`ALERT …`, `WARN …`) and exactly one `END …` line with the verdict, the exit code and the path to `result.json`. Agents that can only run a command and wait use `--detach` and then call `wait` repeatedly.

Other commands: `quota`, `wait`, `watch`, `status`, `list`, `result`, `kill`, `gc`, `selftest`.

The full guide — commands, output protocol, exit codes, how to write a task brief, resuming, and how a new agent writes its own adapter — is in [`skill/GUIDE.md`](skill/GUIDE.md) (Chinese). Agent-specific notes live in [`skill/adapters/`](skill/adapters/).

## Tests

The test suite runs the runner against a fake `agy` in an isolated home directory, so it uses no quota and does not touch your real projects:

```bash
python -I skill/tests/tests.py
```

## License

[Apache-2.0](LICENSE)
