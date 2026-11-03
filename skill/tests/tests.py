"""假 agy 测试套件：隔离的假家目录，不碰真实运行记录、项目和会话。用法: python -I tests.py [场景名...]"""
import subprocess as _sp
_orig_init = _sp.Popen.__init__
def _init(self, *a, **k):
    k["creationflags"] = k.get("creationflags", 0) | 0x08000000   # CREATE_NO_WINDOW：测试期间不要给用户弹窗
    _orig_init(self, *a, **k)
_sp.Popen.__init__ = _init

import json, os, pathlib, shutil, subprocess, sys, time

HERE = pathlib.Path(__file__).resolve().parent
RUNNER = HERE.parent / "agy_run.py"
HOME = HERE / "home"
WORK = HERE / "work"
WORK2 = HERE / "work2"


def env(extra=None):
    e = dict(os.environ)
    e.update(USERPROFILE=str(HOME), HOME=str(HOME), APPDATA=str(HOME / "AppData" / "Roaming"), LOCALAPPDATA=str(HOME / "AppData" / "Local"),
             AGY_BIN=str(HERE / "fake_agy.cmd"), PYTHONIOENCODING="utf-8", AGY_RUNS_DIR=str(HOME / ".claude" / "agy-runs"))
    e.update(extra or {})
    return e


def runs():
    d = HOME / ".claude" / "agy-runs"
    return sorted(d.glob("*")) if d.exists() else []


def run(scen, args=(), extra_env=None, prompt="test", name=None, cwd=WORK, timeout=120):
    name = name or scen
    e = env(dict(FAKE_SCEN=scen, **(extra_env or {})))
    cmd = [sys.executable, "-I", str(RUNNER), "run", "--cwd", str(cwd), "--prompt", prompt, "--name", name, "--no-quota-check", *args]
    t = time.time()
    p = subprocess.run(cmd, env=e, capture_output=True, timeout=timeout)
    out = p.stdout.decode("utf-8", "replace")
    mine = [r for r in runs() if r.name.endswith("-" + name) or ("-" + name + "-") in r.name]
    d = mine[-1] if mine else None
    res = json.loads((d / "result.json").read_text(encoding="utf-8")) if d and (d / "result.json").exists() else None
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8")) if d and (d / "meta.json").exists() else None
    return dict(rc=p.returncode, out=out, err=p.stderr.decode("utf-8", "replace"), res=res, meta=meta, dir=d, secs=time.time() - t)


RESULTS = []


def check(label, cond, detail=""):
    RESULTS.append((label, bool(cond)))
    print("%s  %s%s" % ("PASS" if cond else "FAIL", label, "" if cond else "   <- " + str(detail)[:300]), flush=True)


def lines(o, prefix):
    return [l for l in o.splitlines() if prefix in l]


def t_ok():
    r = run("ok")
    check("ok: DONE_UNVERIFIED rc0", r["rc"] == 0 and r["res"]["verdict"] == "DONE_UNVERIFIED", r["out"])
    n = len([l for l in r["out"].splitlines() if l.strip()])
    check("ok: stdout 只有 END 一行 (默认 act)", n == 1 and "END " in r["out"], r["out"])
    check("ok: result.json 有文件清单/汇报/agy_version", r["res"]["files_touched"] == ["C:/x/out.txt"] and "全部完成" in r["res"]["report"]["text"] and r["res"]["agy_version"] == "1.4.2", r["res"])
    check("ok: meta finished + runner_pid + rc", r["meta"]["state"] == "finished" and r["meta"]["rc"] == 0 and r["meta"].get("runner_pid"), r["meta"])
    r2 = run("ok", ["--stdout", "all"], name="okall")
    check("ok: --stdout all 输出全部行 (START/INIT/RESULT/FINAL)", all(k in r2["out"] for k in ("START", "INIT", "RESULT", "FINAL")), r2["out"])


def t_tool_quota_text():
    r = run("tool_quota_text")
    check("工具输出里的 'quota exhausted' 不再把成功运行判成 QUOTA", r["res"]["verdict"] == "DONE_UNVERIFIED" and r["rc"] == 0, r["out"])


def t_stale_error():
    r = run("stale_error")
    check("stale_error: agy 重试恢复后做完 -> DONE_UNVERIFIED(api_error_recovered)", r["res"]["verdict"] == "DONE_UNVERIFIED" and r["res"]["note"] == "api_error_recovered", (r["res"] or {}).get("verdict"))


def t_print_timeout():
    r = run("print_timeout")
    check("print_timeout: TIMEOUT_PARTIAL rc4", r["res"]["verdict"] == "TIMEOUT_PARTIAL" and r["rc"] == 4, r["out"])
    check("print_timeout: 有续接命令", r["res"]["resume_cmd"] and "--resume" in r["res"]["resume_cmd"] and "ALERT PRINT_TIMEOUT" in r["out"], r["out"])


def t_unauth_autoresume():
    r = run("unauth", extra_env={"FAKE_SCEN_RESUME": "ok"}, name="unauth")
    allr = [x.name for x in runs() if x.name.endswith("-unauth") or x.name.endswith("-unauth-r")]
    check("unauth: 自动续接成功，最终 rc0", r["rc"] == 0 and len(allr) == 2, (r["rc"], allr, r["out"]))
    check("unauth: 首次 AUTH 不叫醒 Claude (只有续接成功后的 END)", "ALERT AUTH" not in r["out"] and r["out"].count("END ") == 1 and "AUTORESUME" in r["out"], r["out"])


def t_unauth_noresume():
    r = run("unauth", ["--auto-resume", "0"], name="unauth0")
    check("unauth + --auto-resume 0: AUTH rc3 + ALERT AUTH", r["rc"] == 3 and r["res"]["verdict"] == "AUTH" and "ALERT AUTH" in r["out"], r["out"])


def t_unauth_twice():
    r = run("unauth", extra_env={"FAKE_SCEN_RESUME": "unauth"}, name="unauth2")
    check("unauth 续接后仍失败: AUTH rc3 并告警 (不无限重试)", r["rc"] == 3 and "ALERT AUTH" in r["out"], r["out"])


def t_quota_real():
    r = run("quota_real")
    check("quota_real: QUOTA rc3, 带 resets_in", r["rc"] == 3 and r["res"]["verdict"] == "QUOTA" and "resets_in=1h50m2s" in r["out"], r["out"])
    check("quota_real: 不在 ERROR 桶、不自动续接", len(runs_named("quota_real")) == 1, runs_named("quota_real"))


def runs_named(n):
    return [x.name for x in runs() if x.name.endswith("-" + n) or x.name.endswith("-" + n + "-r")]


def t_idle_died():
    r = run("idle_died", extra_env={"FAKE_SCEN_RESUME": "ok"}, name="idle")
    check("stream idle timeout: ERROR_TRANSIENT 自动续接后 rc0", r["rc"] == 0 and len(runs_named("idle")) == 2, (r["rc"], r["out"]))


def t_expect():
    prompt = "| 1 | `C:/x/in/a.png` | `C:/x/out/a.txt` |\n| 2 | `C:/x/in/b.png` | `C:/x/out/b.txt` |\n"
    r = run("partial_outputs", prompt=prompt)
    check("清单两个输出只写了一个: DONE_PARTIAL rc4 + 缺 b.txt", r["rc"] == 4 and r["res"]["verdict"] == "DONE_PARTIAL" and r["res"]["missing"] == ["c:/x/out/b.txt"], (r["rc"], r["res"] and r["res"]["missing"]))
    r2 = run("trunc_complete", prompt=prompt, name="trunc")
    check("print timeout 但清单输出全写完: DONE_UNVERIFIED(timeout_but_outputs_complete)", r2["res"]["verdict"] == "DONE_UNVERIFIED" and r2["res"]["note"] == "timeout_but_outputs_complete", (r2["res"] or {}).get("verdict"))
    r3 = run("partial_outputs", ["--no-auto-expect"], prompt=prompt, name="noexp")
    check("--no-auto-expect 关闭识别", r3["res"]["verdict"] == "DONE_UNVERIFIED", (r3["res"] or {}).get("verdict"))


def t_tool_error():
    r = run("tool_error_stall", ["--stall", "3"])
    check("tool ERROR 步骤: 不残留为'当前'，成功完成", r["res"]["verdict"] == "DONE_UNVERIFIED" and r["res"]["stats"]["tool_errors"] == 1 and "view_file" not in r["out"], r["out"])


def t_slow_think():
    r = run("slow_think", ["--stall-think", "4"], extra_env={"FAKE_SLEEP": "7"})
    check("模型思考 7 秒(阈值 4): 有 STALL 告警但不被杀、成功完成", "ALERT STALL" in r["out"] and r["res"]["verdict"] == "DONE_UNVERIFIED", r["out"])
    r2 = run("slow_think", ["--stall-think", "30"], extra_env={"FAKE_SLEEP": "7"}, name="slow2")
    check("思考 7 秒(阈值 30): 零告警，只有 END 一行", "STALL" not in r2["out"] and r2["out"].count("\n") == 1, r2["out"])


def t_hang_kill():
    pf = HERE / "pids_a.txt"
    pf.unlink(missing_ok=True)
    r = run("hang_cmd", ["--cmd-kill", "8", "--cmd-quiet", "0", "--stall-cmd", "3", "--idle-kill", "8"], extra_env={"FAKE_PIDFILE": str(pf), "FAKE_SECS": "100"}, name="hang1")
    check("命令挂起: AUTOKILL + KILLED_STALL rc4", r["rc"] == 4 and r["res"]["verdict"] == "KILLED_STALL" and "ALERT AUTOKILL" in r["out"], r["out"])
    time.sleep(1)
    pids = [int(x) for x in pf.read_text().split()] if pf.exists() else []
    alive = [p for p in pids if alive_pid(p)]
    check("命令挂起被杀后，agy 和孙进程都不在了 (Job)", pids and not alive, (pids, alive))
    check("被杀后有续接 HINT", r["res"]["resume_cmd"] is not None, r["res"])


def alive_pid(pid):
    o = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/NH"], capture_output=True).stdout.decode("gbk", "replace")
    return str(pid) in o


def t_quiet_kill():
    pf = HERE / "pids_b.txt"
    pf.unlink(missing_ok=True)
    r = run("hang_cmd", ["--cmd-quiet", "6", "--probe-every", "2", "--stall-cmd", "3", "--cmd-kill", "3600"], extra_env={"FAKE_PIDFILE": str(pf), "FAKE_SECS": "100"}, name="hang2")
    check("进程树 CPU/IO 静默: 提前判定挂死 (远早于 cmd-kill 3600)", r["res"]["verdict"] == "KILLED_STALL" and "CPU/IO 零增长" in r["out"] and r["secs"] < 60, (r["secs"], r["out"]))


def t_crash():
    r = run("crash")
    check("无 result 退出: NO_RESULT", r["res"]["verdict"] == "NO_RESULT", r["out"])


def t_manual_kill():
    pf = HERE / "pids_c.txt"
    pf.unlink(missing_ok=True)
    e = env(dict(FAKE_SCEN="hang_cmd", FAKE_PIDFILE=str(pf), FAKE_SECS="100"))
    p = subprocess.Popen([sys.executable, "-I", str(RUNNER), "run", "--cwd", str(WORK), "--prompt", "x", "--name", "mk", "--no-quota-check"], env=e, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    time.sleep(6)
    k = subprocess.run([sys.executable, "-I", str(RUNNER), "kill", "latest"], env=e, capture_output=True).stdout.decode("utf-8", "replace")
    out = p.communicate(timeout=60)[0].decode("utf-8", "replace")
    d = [x for x in runs() if x.name.endswith("-mk")][-1]
    res = json.loads((d / "result.json").read_text(encoding="utf-8"))
    check("kill 子命令: 记成 KILLED_MANUAL(rc4)，不是 NO_RESULT", res["verdict"] == "KILLED_MANUAL" and "via job" in k, (k, res["verdict"]))
    time.sleep(1)
    pids = [int(x) for x in pf.read_text().split()]
    check("kill 后 agy 和孙进程都不在", not [x for x in pids if alive_pid(x)], pids)


def t_runner_killed():
    """运行器被强杀(TerminateProcess，不带 /T)：agy 和后代必须被系统带走，不能继续干活。"""
    pf = HERE / "pids_d.txt"
    pf.unlink(missing_ok=True)
    e = env(dict(FAKE_SCEN="hang_cmd", FAKE_PIDFILE=str(pf), FAKE_SECS="100"))
    p = subprocess.Popen([sys.executable, "-I", str(RUNNER), "run", "--cwd", str(WORK), "--prompt", "x", "--name", "rk", "--no-quota-check"], env=e, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    time.sleep(6)
    pids = [int(x) for x in pf.read_text().split()]
    check("(前提) 运行器启动后 agy 和孙进程在跑", all(alive_pid(x) for x in pids), pids)
    subprocess.run(["taskkill", "/PID", str(p.pid), "/F"], capture_output=True)    # 只杀运行器本身，不带 /T
    time.sleep(2)
    check("运行器被强杀后，agy 和孙进程被 Job 带走", not [x for x in pids if alive_pid(x)], [x for x in pids if alive_pid(x)])
    d = [x for x in runs() if x.name.endswith("-rk")][-1]
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    ls = subprocess.run([sys.executable, "-I", str(RUNNER), "list", "--since", "1"], env=e, capture_output=True).stdout.decode("utf-8", "replace")
    check("被强杀的记录在 list 里显示 ORPHANED，而不是 running", meta["state"] == "running" and "ORPHANED" in [l for l in ls.splitlines() if d.name in l][0], ls)
    k = subprocess.run([sys.executable, "-I", str(RUNNER), "kill", d.name], env=e, capture_output=True).stdout.decode("utf-8", "replace")
    meta2 = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    check("kill 对僵尸记录: 标 orphaned，不按裸 pid 动任何进程", meta2["state"] == "orphaned" and "没有按 pid" in k, k)


def t_projects():
    # 1) 并发：A 是 throwaway(新目录 work2)，B 同时在 work 里首次运行；A 收尾不能删 B 的项目
    e = env(dict(FAKE_SCEN="slow_ok", FAKE_SLEEP="3", FAKE_PROJ_DELAY="1"))
    pa = subprocess.Popen([sys.executable, "-I", str(RUNNER), "run", "--cwd", str(WORK2), "--prompt", "x", "--name", "ta", "--no-quota-check", "--throwaway"], env=e, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    time.sleep(0.5)
    pb = subprocess.Popen([sys.executable, "-I", str(RUNNER), "run", "--cwd", str(WORK), "--prompt", "x", "--name", "tb", "--no-quota-check"], env=e, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    oa, ob = pa.communicate(timeout=90)[0].decode("utf-8", "replace"), pb.communicate(timeout=90)[0].decode("utf-8", "replace")
    projs = list((HOME / ".gemini" / "config" / "projects").glob("*.json"))
    dirs = [json.loads(p.read_text(encoding="utf-8"))["projectResources"]["resources"][0]["folderUri"] for p in projs]
    check("并发: throwaway 没删掉别的运行的项目 (work 的项目还在)", any(d.endswith("/work") for d in dirs), dirs)
    check("并发: throwaway 自己的项目被清理 (work2 的项目不在)", not any(d.endswith("/work2") for d in dirs), dirs)
    check("并发: B 没有'多出项目'的误报", "WARN 本次运行多出" not in ob and "WARN 本次运行多出" not in oa, (oa, ob))
    # 2) 同目录复用: 再跑一次 work 不应新建项目
    n0 = len(list((HOME / ".gemini" / "config" / "projects").glob("*.json")))
    run("ok", name="reuse", cwd=WORK)
    n1 = len(list((HOME / ".gemini" / "config" / "projects").glob("*.json")))
    check("同目录再跑一次复用项目 (项目数不变)", n1 == n0, (n0, n1))
    # 3) throwaway + resume 被拒
    r = run("ok", ["--throwaway", "--resume", "00000000-0000-4000-8000-000000000009"], name="trs")
    check("--throwaway 与 --resume 同用: BAD_ARGS rc2", r["rc"] == 2 and "BAD_ARGS" in r["out"], r["out"])


def t_dirlock():
    """同一个全新目录同时两个首次运行：只能建一个项目。"""
    d3 = HERE / "work3"
    shutil.rmtree(d3, ignore_errors=True); d3.mkdir()
    n0 = len(list((HOME / ".gemini" / "config" / "projects").glob("*.json")))
    e = env(dict(FAKE_SCEN="slow_ok", FAKE_SLEEP="2", FAKE_PROJ_DELAY="2"))
    ps = [subprocess.Popen([sys.executable, "-I", str(RUNNER), "run", "--cwd", str(d3), "--prompt", "x", "--name", "dl%d" % i, "--no-quota-check"], env=e, stdout=subprocess.PIPE, stderr=subprocess.STDOUT) for i in range(2)]
    for p in ps:
        p.communicate(timeout=120)
    n1 = len(list((HOME / ".gemini" / "config" / "projects").glob("*.json")))
    check("同目录两个首次运行同时启动: 只新建 1 个项目", n1 - n0 == 1, (n0, n1))


def t_badargs():
    r = subprocess.run([sys.executable, "-I", str(RUNNER), "run", "--cwd", str(WORK), "--prompt", "x", "--timeout", "5400x", "--no-quota-check"], env=env(), capture_output=True)
    before = len(runs())
    check("--timeout 写错: 启动 agy 之前就退出 rc2、没留下运行目录", r.returncode == 2 and len(runs()) == before, r.stdout)
    r2 = run("ok", ["--timeout", "5400"], name="bare")
    check("--timeout 5400(裸数字=秒) 被接受", r2["rc"] == 0, r2["out"])
    r3 = run("ok", ["--timeout", "1h30m"], name="hm")
    check("--timeout 1h30m 被接受", r3["rc"] == 0, r3["out"])
    r4 = run("ok", ["--effort", "low", "--model", "gemini-3.8-flash-high"], name="eff")
    cmdline = r4["meta"]["cmd"]
    check("--effort 与带档位后缀的模型名不再冲突 (后缀被去掉)", "gemini-3.8-flash" in cmdline and "gemini-3.8-flash-high" not in cmdline, cmdline)


def t_status_list():
    e = env()
    ls = subprocess.run([sys.executable, "-I", str(RUNNER), "list", "--since", "1"], env=e, capture_output=True).stdout.decode("utf-8", "replace")
    check("list 能列出运行", "finished" in ls and "共" in ls, ls)
    rs = subprocess.run([sys.executable, "-I", str(RUNNER), "result", "latest"], env=e, capture_output=True).stdout.decode("utf-8", "replace")
    check("result 子命令打印汇报全文", "汇报全文" in rs, rs[:200])


def sh(*args, extra=None, timeout=180):
    p = subprocess.run([sys.executable, "-I", str(RUNNER), *args], env=env(extra), capture_output=True, timeout=timeout)
    return p.returncode, p.stdout.decode("utf-8", "replace")


def detach(scen, args=(), extra=None, name=None):
    import re
    rc, out = sh("run", "--cwd", str(WORK), "--prompt", "x", "--name", name or scen, "--no-quota-check", "--detach", *args, extra=dict(FAKE_SCEN=scen, **(extra or {})))
    m = re.search(r"STARTED run=(\S+) runner_pid=(\d+)", out)
    return rc, out, (m.group(1) if m else None), (int(m.group(2)) if m else None)


def t_notify():
    import re
    f1 = HERE / "notified1.txt"; f2 = HERE / "notified2.txt"; f3 = HERE / "notified3.txt"
    for f in (f1, f2, f3):
        f.unlink(missing_ok=True)
    run("quota_real", ["--notify-cmd", "echo %AGY_ALERT_KIND% %AGY_RUN_ID%>" + str(f1)], name="ntf1")
    time.sleep(3)
    txt = f1.read_text(encoding="utf-8", errors="replace") if f1.exists() else ""
    check("额度告警: 运行器自己执行 --notify-cmd (带 AGY_ALERT_KIND/AGY_RUN_ID)", "QUOTA" in txt and "ntf1" in txt, txt)
    run("unauth", ["--auto-resume", "0", "--notify-cmd", "echo %AGY_ALERT_KIND%>" + str(f2)], name="ntf2")
    time.sleep(3)
    check("登录告警(不再自动续接时)也通知", f2.exists() and "AUTH" in f2.read_text(encoding="utf-8", errors="replace"), f2.exists())
    run("ok", ["--notify-cmd", "echo X>" + str(f3)], name="ntf3")
    time.sleep(2)
    check("正常完成的运行不通知", not f3.exists())
    run("unauth", ["--notify-cmd", "echo X>" + str(f3)], extra_env={"FAKE_SCEN_RESUME": "ok"}, name="ntf4")
    time.sleep(2)
    check("登录过期但自动续接成功: 不通知 (不打扰人)", not f3.exists())


def t_detach_wait():
    t = time.time()
    rc, out, rid, rpid = detach("slow_think", ["--stall-think", "4"], {"FAKE_SLEEP": "9"}, name="dtw")
    check("--detach: 立刻返回 STARTED + 下一步提示 (rc0, <6 秒)", rc == 0 and rid and "wait" in out and time.time() - t < 6, (rc, out))
    rc1, o1 = sh("wait", rid, "--timeout", "60")
    check("wait: 先返回告警 rc10 (运行仍在继续)", rc1 == 10 and "ALERT STALL" in o1, (rc1, o1))
    rc2, o2 = sh("wait", rid, "--until", "end", "--timeout", "90")
    check("wait --until end: 等到 END，返回运行自己的 rc0", rc2 == 0 and "END " in o2 and "verdict=DONE_UNVERIFIED" in o2, (rc2, o2))
    rc3, o3 = sh("wait", rid)
    check("结束后再 wait: WAIT_FINISHED，不重复输出旧事件", rc3 == 0 and "WAIT_FINISHED" in o3 and "ALERT STALL" not in o3, (rc3, o3))
    rc, out, rid2, _ = detach("slow_ok", extra={"FAKE_SLEEP": "12"}, name="dtw2")
    rc4, o4 = sh("wait", rid2, "--timeout", "4")
    check("wait: 到点没有新事件 -> WAIT_TIMEOUT rc11", rc4 == 11 and "WAIT_TIMEOUT" in o4, (rc4, o4))
    rc5, o5 = sh("wait", rid2, "--until", "end", "--timeout", "90")
    check("WAIT_TIMEOUT 之后继续等 -> 正常拿到 END", rc5 == 0 and "END " in o5, (rc5, o5))


def t_detach_orphan():
    pf = HERE / "pids_e.txt"
    pf.unlink(missing_ok=True)
    rc, out, rid, rpid = detach("hang_cmd", extra={"FAKE_PIDFILE": str(pf), "FAKE_SECS": "100"}, name="dto")
    time.sleep(7)
    pids = [int(x) for x in pf.read_text().split()] if pf.exists() else []
    subprocess.run(["taskkill", "/PID", str(rpid), "/F"], capture_output=True)       # 只杀运行器
    time.sleep(2)
    rc2, o2 = sh("wait", rid, "--timeout", "20")
    check("后台运行器被强杀: wait 报 WAIT_ORPHANED rc12", rc2 == 12 and "WAIT_ORPHANED" in o2, (rc2, o2))
    check("后台运行器被强杀: agy 和孙进程仍被 Job 带走", pids and not [x for x in pids if alive_pid(x)], pids)


def t_detach_chain():
    rc, out, rid, _ = detach("unauth", extra={"FAKE_SCEN_RESUME": "ok"}, name="dtc")
    rc1, o1 = sh("wait", rid, "--until", "end", "--timeout", "120")
    check("后台 + 登录过期自动续接: wait 一次调用自动跟到续接运行，拿到它的 END (rc0)", rc1 == 0 and "CHAIN" in o1 and "END run=" in o1 and "-dtc-r" in o1, (rc1, o1))
    check("续接成功时全程没有 ALERT AUTH (不打扰人)", "ALERT AUTH" not in o1, o1)
    rc2, o2 = sh("wait", rid)
    check("链走完后再 wait 原运行: 跟到末端 WAIT_FINISHED rc0，不重复旧事件", rc2 == 0 and "WAIT_FINISHED" in o2 and "END run=" not in o2, (rc2, o2))


def t_slow_start():
    r = run("ok", extra_env={"FAKE_START_DELAY": "46"}, name="slowstart")
    fl = (r["res"] or {}).get("flags", {})
    check("启动慢(46 秒): 不再推 ALERT SLOW_START 给调用方", "SLOW_START" not in r["out"].replace("slow_start=", ""), r["out"])
    check("启动慢: END 行带 slow_start=Ns", "slow_start=4" in r["out"], r["out"])
    check("启动慢: result.json 记录耗时和 MCP 原因", (fl.get("slow_start_s") or 0) >= 45 and "example-mcp" in (fl.get("slow_start_detail") or ""), fl)
    dg = (r["dir"] / "digest.log").read_text(encoding="utf-8", errors="replace") if r["dir"] else ""
    check("启动慢: digest.log 里仍有 SLOW_START 记录（想排查的人看得到）", "SLOW_START" in dg, dg[:300])
    check("启动慢但任务正常: 结论仍是 DONE_UNVERIFIED rc0", r["rc"] == 0 and (r["res"] or {}).get("verdict") == "DONE_UNVERIFIED", r["out"])
    r2 = run("ok", name="fast")
    check("启动不慢: END 行没有 slow_start 字段", "slow_start" not in r2["out"], r2["out"])


def t_default_dir():
    import tempfile
    code = "import importlib.util,sys;s=importlib.util.spec_from_file_location('r',sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);print(m.RUNS)"
    def runs_dir(setup):
        with tempfile.TemporaryDirectory() as td:
            pathlib.Path(td, ".claude").mkdir()
            if setup == "legacy":
                pathlib.Path(td, ".claude", "agy-runs").mkdir()
            elif setup == "both":
                pathlib.Path(td, ".claude", "agy-runs").mkdir(); pathlib.Path(td, ".agy-delegate", "runs").mkdir(parents=True)
            e = env(dict(USERPROFILE=td, HOME=td)); e.pop("AGY_RUNS_DIR", None)
            return subprocess.run([sys.executable, "-I", "-c", code, str(RUNNER)], env=e, capture_output=True).stdout.decode().strip().replace("\\", "/")
    check("默认目录: 全新安装 -> 中性位置 ~/.agy-delegate/runs", runs_dir("none").endswith("/.agy-delegate/runs"), runs_dir("none"))
    check("默认目录: 已有旧记录 -> 沿用 ~/.claude/agy-runs", runs_dir("legacy").endswith("/.claude/agy-runs"), runs_dir("legacy"))
    check("默认目录: 两个都有 -> 中性位置优先", runs_dir("both").endswith("/.agy-delegate/runs"), runs_dir("both"))
    e = env(dict(AGY_RUNS_DIR=str(HERE / "custom_runs")))
    p = subprocess.run([sys.executable, "-I", str(RUNNER), "run", "--cwd", str(WORK), "--prompt", "x", "--name", "cdir", "--no-quota-check"], env=dict(e, FAKE_SCEN="ok"), capture_output=True)
    check("AGY_RUNS_DIR 显式指定目录生效", any((HERE / "custom_runs").glob("*-cdir")), p.stdout)


def t_selftest():
    rc, out = sh("selftest")
    check("selftest: 假环境下零 token 检查全部通过", rc == 0 and "SELFTEST PASS" in out and "额度可读" in out, out)
    rc2, out2 = sh("selftest", "--live", extra=dict(FAKE_SCEN="ok"))
    check("selftest --live: 真实小任务端到端 (假 agy) 通过且清理 throwaway", rc2 == 0 and "真实小任务端到端" in out2, out2)
    import re
    rc3, out3 = sh("selftest", "--live", "--detach", extra=dict(FAKE_SCEN="ok"))
    m = re.search(r"STARTED run=(\S+)", out3)
    check("selftest --live --detach: 立刻返回 STARTED 和使用提示", rc3 == 0 and m and "wait" in out3 and "DONE_UNVERIFIED" in out3, out3)
    rc4, out4 = sh("wait", m.group(1), "--until", "end", "--timeout", "60") if m else (-1, "")
    check("selftest --live --detach: wait 到 END，verdict=DONE_UNVERIFIED", rc4 == 0 and "verdict=DONE_UNVERIFIED" in out4, (rc4, out4))


ALL = [t_badargs, t_ok, t_tool_quota_text, t_stale_error, t_print_timeout, t_unauth_autoresume, t_unauth_noresume, t_unauth_twice, t_quota_real,
       t_idle_died, t_expect, t_tool_error, t_slow_think, t_hang_kill, t_quiet_kill, t_crash, t_manual_kill, t_runner_killed, t_projects, t_dirlock, t_status_list,
       t_notify, t_detach_wait, t_detach_orphan, t_detach_chain, t_slow_start, t_default_dir, t_selftest]

if __name__ == "__main__":
    want = sys.argv[1:]
    if not want:
        shutil.rmtree(HOME, ignore_errors=True)
    HOME.mkdir(exist_ok=True)
    WORK.mkdir(exist_ok=True)       # 测试用的空工作目录：套件自己建，不能依赖手工预先建好
    WORK2.mkdir(exist_ok=True)
    for f in ALL:
        if want and f.__name__[2:] not in want:
            continue
        print("--- %s" % f.__name__, flush=True)
        try:
            f()
        except Exception as ex:
            check(f.__name__ + " 抛异常", False, repr(ex))
    bad = [l for l, ok in RESULTS if not ok]
    print("\n==== %d/%d 通过 ====" % (len(RESULTS) - len(bad), len(RESULTS)))
    for l in bad:
        print("FAIL:", l)
    sys.exit(1 if bad else 0)
