import sys, json, os, time
args = sys.argv[1:]
scen = os.environ.get("FAKE_SCEN", "ok")
def emit(o):
    sys.stdout.write(json.dumps(o, ensure_ascii=False) + "\n"); sys.stdout.flush()
CID = os.environ.get("FAKE_CID", "00000000-0000-4000-8000-%012d" % os.getpid())
def su(idx, state, ty, **kw):
    d = {"conversation_id": CID, "step_index": idx, "state": state, "step_type": ty}
    d.update(kw); emit({"event": "step_update", "step_update": d})
def tool(idx, name, params, output=""):
    su(idx, "ACTIVE", "tool", tool_name=name, tool_info={"name": name, "parameters": params})
    time.sleep(0.05)
    su(idx, "DONE", "tool", tool_name=name, duration_seconds=0.1, tool_info={"name": name, "parameters": params, "output": output})
def say(idx, text):
    su(idx, "ACTIVE", "agent_response", text_delta=text)
    su(idx, "DONE", "agent_response", usage={"total_tokens": 1000})
def result(status="SUCCESS", response="", error=""):
    r = {"conversation_id": CID, "status": status, "response": response, "duration_seconds": 1.0, "num_turns": 1,
         "usage": {"input_tokens": 10, "output_tokens": 5, "thinking_tokens": 0, "cache_read_tokens": 0, "total_tokens": 15}}
    if error: r["error"] = error
    emit({"event": "result", "result": r})
def err(line):
    sys.stderr.write(line + "\n"); sys.stderr.flush()

if "--version" in args:
    print("1.4.2"); sys.exit(0)
if "--new-project" in args:
    import uuid, pathlib
    _pid = str(uuid.uuid4()); _pd = pathlib.Path(os.path.expanduser("~")) / ".gemini" / "config" / "projects"; _pd.mkdir(parents=True, exist_ok=True)
    (_pd / (_pid + ".json")).write_text(json.dumps({"id": _pid, "name": os.path.basename(os.getcwd()), "projectResources": {"resources": [{"folderUri": "file://" + os.getcwd().replace(chr(92), "/")}]}}), encoding="utf-8")
    time.sleep(float(os.environ.get("FAKE_PROJ_DELAY", "0")))
if "-p" in args and "/usage" in args:
    f = float(os.environ.get("FAKE_QUOTA_FRAC", "0.5"))
    print(json.dumps({"command": {"data": {"groups": [{"buckets": [
        {"id": "gemini-weekly", "remaining_fraction": f, "reset_time": "2026-10-20T00:00:00Z"},
        {"id": "gemini-5h", "remaining_fraction": f, "reset_time": "2026-10-19T20:00:00Z"},
        {"id": "3p-weekly", "remaining_fraction": 1.0, "reset_time": ""}, {"id": "3p-5h", "remaining_fraction": 1.0, "reset_time": ""}]}]}}}))
    sys.exit(0)

if "--conversation" in args:
    scen = os.environ.get("FAKE_SCEN_RESUME", "ok")
line = sys.stdin.readline()
emit({"event": "init", "conversation_id": CID, "init": {"model": os.environ.get("FAKE_MODEL", "gemini-3.8-flash-high"), "cwd": os.getcwd(), "tools": [], "permission_mode": "always-proceed"}})
_dly = float(os.environ.get("FAKE_START_DELAY", "0"))
if _dly:
    if "--log-file" in args:
        open(args[args.index("--log-file") + 1], "a", encoding="utf-8").write("[12:00:00] MCP: 1 server(s) still connecting after 30s: example-mcp" + chr(10))
    time.sleep(_dly)
su(0, "DONE", "user_input")
say(1, "我先看看文件。")
if scen == "ok":
    tool(2, "view_file", {"AbsolutePath": "C:/x/a.txt"}, "3 lines, 20 bytes")
    tool(3, "write_to_file", {"TargetFile": "C:/x/out.txt"}, "")
    say(4, "全部完成。已写入 out.txt。")
    result("SUCCESS", "全部完成。已写入 out.txt。")
elif scen == "tool_quota_text":
    tool(2, "run_command", {"CommandLine": "Get-Content internal/facility/pcc/client.go"},
         'return httpErrorf(connect.CodeResourceExhausted, "pcc: daily API quota exhausted, requests paused until PointClickCare resets the quota")')
    tool(3, "write_to_file", {"TargetFile": "C:/x/out.txt"}, "")
    say(4, "审计完成：client.go 没有问题。")
    result("SUCCESS", "审计完成：client.go 没有问题。")
elif scen == "stale_error":
    tool(2, "view_file", {"AbsolutePath": "C:/x/a.png"}, "png")
    su(3, "DONE", "error_message")     # agy 内部 5 分钟流空闲超时，自动重试
    tool(4, "write_to_file", {"TargetFile": "C:/x/out1.md"}, "")
    tool(5, "write_to_file", {"TargetFile": "C:/x/out2.md"}, "")
    say(6, "### 转写完成汇报\n1. 处理 2 页，跳过 0 页，失败 0 页。\n2. 无 [?]。" + "字" * 150)
    result("ERROR", "### 转写完成汇报", "API error (attempt 2): request failed: stream idle timeout (5m0s): context deadline exceeded")
elif scen == "print_timeout":
    tool(2, "write_to_file", {"TargetFile": "C:/x/out1.md"}, "")
    say(3, "我将转写内容写入 page062.md。")
    err("[agy] print timeout after 28m0s with turn in progress; returning partial output")
    result("SUCCESS", "我将转写内容写入 page062.md。")
elif scen == "unauth":
    tool(2, "write_to_file", {"TargetFile": "C:/x/out1.md"}, "")
    su(3, "DONE", "error_message")
    msg = "UNAUTHENTICATED (code 401): Request had invalid authentication credentials. Expected OAuth 2 access token, login cookie or other valid authentication credential."
    err("error: " + msg)
    err('AGY_ERROR: {"short_error":"%s","status":"UNAUTHENTICATED","error_code":401,"code_kind":"http","retryable":false}' % msg)
    result("ERROR", "", msg)
elif scen == "quota_real":
    tool(2, "write_to_file", {"TargetFile": "C:/x/out1.md"}, "")
    err("error: Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 1h50m2s. (response may be truncated)")
    err('AGY_ERROR: {"short_error":"RESOURCE_EXHAUSTED (code 429): Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 1h50m2s.","status":"RESOURCE_EXHAUSTED","error_code":429}')
    result("ERROR", "", "Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 1h50m2s.")
elif scen == "quota_nopat":
    # 假设 AGY_ERROR 那一行消失（版本变化）：只剩 "Individual quota reached"
    tool(2, "write_to_file", {"TargetFile": "C:/x/out1.md"}, "")
    err("error: Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 1h50m2s.")
    result("ERROR", "", "Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 1h50m2s.")
elif scen == "auth_new":
    tool(2, "write_to_file", {"TargetFile": "C:/x/out1.md"}, "")
    err("error: Authentication expired. Please log back in.")
    result("ERROR", "", "Authentication expired. Please log back in.")
elif scen == "credits":
    err("error: Your AI credits balance is too low to continue.")
    result("ERROR", "", "Your AI credits balance is too low to continue.")
elif scen == "slow_ok":
    time.sleep(float(os.environ.get("FAKE_SLEEP", "4")))
    tool(2, "write_to_file", {"TargetFile": "C:/x/out.txt"}, "")
    say(3, "完成。")
    result("SUCCESS", "完成。")
elif scen == "partial_outputs":
    tool(2, "write_to_file", {"TargetFile": "C:/x/out/a.txt"}, "")
    say(3, "处理完成 1 题，未完成 1 题。")
    result("SUCCESS", "处理完成 1 题，未完成 1 题。")
elif scen == "trunc_complete":
    tool(2, "write_to_file", {"TargetFile": "C:/x/out/a.txt"}, "")
    tool(3, "write_to_file", {"TargetFile": "C:/x/out/b.txt"}, "")
    say(4, "正在做最后的检查。")
    err("[agy] print timeout after 28m0s with turn in progress; returning partial output")
    result("SUCCESS", "正在做最后的检查。")
elif scen == "tool_error_stall":
    su(2, "ACTIVE", "tool", tool_name="view_file", tool_info={"name": "view_file", "parameters": {"AbsolutePath": "C:/x/missing.csv"}})
    su(2, "ERROR", "tool", tool_name="view_file", tool_info={"name": "view_file", "parameters": {"AbsolutePath": "C:/x/missing.csv"}, "error": {"type": "TOOL_ERROR", "message": "cannot find the file"}})
    time.sleep(9)
    say(3, "文件不存在，改用别的办法。")
    result("SUCCESS", "文件不存在，改用别的办法。")
elif scen == "idle_died":
    tool(2, "view_file", {"AbsolutePath": "C:/x/a.png"}, "png")
    su(3, "DONE", "error_message")
    err_msg = "API error (attempt 3): request failed: stream idle timeout (5m0s): context deadline exceeded"
    result("ERROR", "", err_msg)
elif scen == "hang":
    tool(2, "write_to_file", {"TargetFile": "C:/x/out1.md"}, "")
    su(3, "ACTIVE", "tool", tool_name="run_command", tool_info={"name": "run_command", "parameters": {"CommandLine": "sleep"}})
    time.sleep(600)
elif scen == "crash":
    tool(2, "write_to_file", {"TargetFile": "C:/x/out1.md"}, "")
    sys.exit(0)   # 没有 result 事件就退出
elif scen == "unknown_step":
    su(2, "DONE", "unknown")
    say(3, "我想问你一个问题：用哪个库？")
    result("SUCCESS", "我想问你一个问题：用哪个库？")
elif scen == "denied":
    err("some tool call was auto-denied by policy")
    say(2, "命令被拒绝了")
    result("SUCCESS", "命令被拒绝了")
elif scen == "hang_cmd":
    tool(2, "write_to_file", {"TargetFile": "C:/x/out1.md"}, "")
    su(3, "ACTIVE", "tool", tool_name="run_command", tool_info={"name": "run_command", "parameters": {"CommandLine": "sleep"}})
    import subprocess
    gc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
    open(os.environ.get("FAKE_PIDFILE", "fake_pids.txt"), "w").write("%d %d" % (os.getpid(), gc.pid))
    time.sleep(float(os.environ.get("FAKE_SECS", "120")))
    result("SUCCESS", "late")
elif scen == "slow_think":
    tool(2, "write_to_file", {"TargetFile": "C:/x/out.txt"}, "")
    time.sleep(float(os.environ.get("FAKE_SLEEP", "8")))
    say(3, "想了很久，完成。" + "字" * 120)
    result("SUCCESS", "想了很久，完成。" + "字" * 120)
else:
    result("SUCCESS", "unknown scen")
