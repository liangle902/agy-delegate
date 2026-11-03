"""agy-delegate 运行器 v0.6：让任意主代理（或脚本）以子进程方式驱动本机 Antigravity CLI (agy)。

设计要点（基于在 agy 1.3 / 1.4 上的实测）：
- agy 的 status=SUCCESS / 退出码 0 不可信：提问被跳过、权限被拒、--print-timeout 到点时同样报成功，所以结论由本脚本重新判定。
- 全量事件落盘到运行目录；标准输出默认只打印需要调用方行动的 ALERT/WARN 和结尾一行 END（--stdout all 输出全部行）。
  完整的结构化结果在 result.json，人看的逐步摘要在 digest.log（用 `watch` 子命令实时跟随）。
- 自检自杀：死循环、空转、长时间无事件都由本脚本自己判定并终止进程。进程退出是唯一一定能让调用方发现的信号。
- 不留尾巴：运行器进程不管怎么死（包括被强杀），系统都会通过 Job Object 把 agy 及其后代一起带走。
- 项目复用：按目录找已有项目，找不到才新建一次；"本次建的项目"靠会话库里的项目 ID 认领，不靠前后集合之差。

子命令：quota | run | wait | watch | status | list | result | kill | gc | selftest
退出码(run)：0 完成(未验证) / 2 需要人工(提问、权限、参数错误、任务书清单缺项) / 3 额度或登录问题 / 4 超时、被杀、可续接 / 5 其他错误
"""
import argparse, ctypes, ctypes.wintypes as wt, hashlib, json, os, re, shutil, sqlite3, subprocess, sys, tempfile, threading, time
from urllib.parse import unquote
from pathlib import Path

AGY = os.environ.get("AGY_BIN") or os.path.expandvars(r"%LOCALAPPDATA%\agy\bin\agy.exe")
def _default_runs():
    legacy, neutral = Path.home() / ".claude" / "agy-runs", Path.home() / ".agy-delegate" / "runs"
    return legacy if legacy.exists() and not neutral.exists() else neutral   # 已有旧记录就沿用旧位置；全新安装用中性位置


RUNS = Path(os.environ.get("AGY_RUNS_DIR") or _default_runs())   # AGY_RUNS_DIR：显式指定（测试、多套环境）
NOTIFY_KINDS = ("QUOTA", "QUOTA_LOW", "AUTH")                    # 这几类告警必须有人处理：配了 --notify-cmd 就由运行器自己通知
PROJECTS = Path.home() / ".gemini" / "config" / "projects"
CONVOS = [Path.home() / ".gemini" / "antigravity" / "conversations", Path.home() / ".gemini" / "antigravity-cli" / "conversations"]
BRAINS = [Path.home() / ".gemini" / "antigravity-cli" / "brain", Path.home() / ".gemini" / "antigravity" / "brain"]
SIDECARS = [Path.home() / ".gemini" / "antigravity-cli" / "annotations", Path.home() / ".gemini" / "antigravity-cli" / "presence"]
SUMMARY_DBS = [Path.home() / ".gemini" / "antigravity-cli" / "conversation_summaries.db", Path.home() / ".gemini" / "antigravity" / "conversation_summaries.db"]
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
QUOTA_PAT = re.compile(r"credits balance is too low|RESOURCE_EXHAUSTED|individual quota reached|quota (is |has been )?(exhausted|exceeded|used up|reached)|out of quota", re.I)
AUTH_PAT = re.compile(r"UNAUTHENTICATED|authentication expired|log back in|invalid authentication credentials|not logged in|sign in again|session expired", re.I)
PTIMEOUT_PAT = re.compile(r"print timeout after (\S+) with turn in progress")
# agy 自己重试后仍失败、或收尾阶段带出的瞬时 API 错误：新进程续接即可，不是任务本身的问题
TRANSIENT_PAT = re.compile(r"stream idle timeout|UNAVAILABLE|stream was interrupted|streamGenerateContent|Eligibility check failed|deadline exceeded|connection (reset|lost|refused|aborted)|\b50[0234]\b", re.I)
RESET_PAT = re.compile(r"Resets in (\S+?)\.?(\s|$|\")")
TABLE_PATH = re.compile(r"`([^`]+)`")
FAILISH = re.compile(r"error|fail|exception|traceback|not found|denied|cannot|无法|错误|失败", re.I)
ZERO_FAIL = re.compile(r"\b(0|no|zero)\s+(errors?|failures?|failed)\b", re.I)
EDIT_TOOL = re.compile(r"write|replace|edit|multi|delete|move|rename", re.I)
READ_TOOL = re.compile(r"^(view|read|list|find|grep|search)", re.I)                       # 分段读大文件会连续出现相同调用
POLL_TOOL = re.compile(r"^(manage_task|command_status|wait|send_command_input)", re.I)   # 轮询类：同样的调用同样的结果是正常的
TESTED = ("1.3.1", "1.3.2", "1.3.3", "1.3.4", "1.3.5", "1.3.6", "1.4.0", "1.4.1", "1.4.2", "1.4.3", "1.4.12")
KNOWN_STEPS = {"user_input", "agent_response", "tool", "system_message", "error_message", "checkpoint", "unknown", "finish"}
NOWIN = 0x08000000   # CREATE_NO_WINDOW：调用方没有控制台时，运行器启动的 git/taskkill/agy 不能各弹一个窗口
RESUMABLE = ("ERROR_TRANSIENT", "ERROR_AUTH", "TIMEOUT_PARTIAL", "DONE_PARTIAL", "KILLED_MANUAL")

for s in (sys.stdout, sys.stderr):
    s.reconfigure(encoding="utf-8", errors="replace")


# ---------- Windows Job Object：运行器无论怎么死，系统都会杀掉 agy 和它的全部后代 ----------
try:
    _K32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _K32.CreateJobObjectW.restype = wt.HANDLE
    _K32.CreateJobObjectW.argtypes = [wt.LPVOID, wt.LPCWSTR]
    _K32.OpenJobObjectW.restype = wt.HANDLE
    _K32.OpenJobObjectW.argtypes = [wt.DWORD, wt.BOOL, wt.LPCWSTR]
    _K32.SetInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, wt.LPVOID, wt.DWORD]
    _K32.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
    _K32.TerminateJobObject.argtypes = [wt.HANDLE, wt.UINT]
    _K32.CloseHandle.argtypes = [wt.HANDLE]
    _K32.OpenProcess.restype = wt.HANDLE
    _K32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    _K32.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
except Exception:
    _K32 = None


class _IOC(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in ("a", "b", "c", "d", "e", "f")]


class _BASIC(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong), ("LimitFlags", wt.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wt.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wt.DWORD), ("SchedulingClass", wt.DWORD)]


class _EXT(ctypes.Structure):
    _fields_ = [("Basic", _BASIC), ("Io", _IOC), ("ProcMem", ctypes.c_size_t), ("JobMem", ctypes.c_size_t),
                ("PeakProc", ctypes.c_size_t), ("PeakJob", ctypes.c_size_t)]


def job_name(rid):
    return "Local\\agy-run-" + rid


def make_job(rid):
    """命名 job，带 KILL_ON_JOB_CLOSE：运行器因任何原因死亡（含被 TerminateProcess），系统都会杀掉 agy 和所有后代。"""
    if not _K32:
        return None
    h = _K32.CreateJobObjectW(None, job_name(rid))
    info = _EXT()
    info.Basic.LimitFlags = 0x2000   # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not h or not _K32.SetInformationJobObject(h, 9, ctypes.byref(info), ctypes.sizeof(info)):
        return None
    return h


def job_add(h, popen):
    return bool(h) and bool(_K32.AssignProcessToJobObject(h, int(popen._handle)))


def job_kill(h):
    return bool(h) and bool(_K32.TerminateJobObject(h, 1))


def job_kill_by_name(rid):
    """别的进程里的 kill 按"身份"终止，而不是按可能已被复用的裸 pid。返回 None 表示 job 已不存在。"""
    if not _K32:
        return None
    h = _K32.OpenJobObjectW(0x0008, False, job_name(rid))   # JOB_OBJECT_TERMINATE
    if not h:
        return None
    ok = bool(_K32.TerminateJobObject(h, 1))
    _K32.CloseHandle(h)
    return ok


def pid_alive(pid):
    if not pid or not _K32:
        return False
    h = _K32.OpenProcess(0x1000, False, int(pid))   # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    code = wt.DWORD()
    ok = _K32.GetExitCodeProcess(h, ctypes.byref(code))
    _K32.CloseHandle(h)
    return bool(ok) and code.value == 259           # STILL_ACTIVE


def tree_usage(root_pid):
    """root_pid 的全部后代进程的 (CPU 秒, IO 字节)，不含 root 本身（agy 自己）。纯 ctypes，约 50ms。失败返回 None。"""
    try:
        k32 = _K32

        class PE32(ctypes.Structure):
            _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD), ("th32DefaultHeapID", ctypes.c_void_p),
                        ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", wt.LONG),
                        ("dwFlags", wt.DWORD), ("szExeFile", wt.WCHAR * 260)]

        class IOC(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in ("ro", "wo", "oo", "rb", "wb", "ob")]
        k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
        h = k32.CreateToolhelp32Snapshot(2, 0)
        pe = PE32(); pe.dwSize = ctypes.sizeof(PE32); kids = {}
        ok = k32.Process32FirstW(h, ctypes.byref(pe))
        while ok:
            kids.setdefault(pe.th32ParentProcessID, []).append(pe.th32ProcessID); ok = k32.Process32NextW(h, ctypes.byref(pe))
        k32.CloseHandle(h)
        pids, stack = [], [root_pid]
        while stack:
            for c in kids.get(stack.pop(), []):
                pids.append(c); stack.append(c)
        cpu = io = 0.0
        ft = lambda x: (x.dwHighDateTime << 32 | x.dwLowDateTime) / 1e7
        for pid in pids:
            hp = k32.OpenProcess(0x1000, False, pid)
            if not hp:
                continue
            c, e, kt, ut = wt.FILETIME(), wt.FILETIME(), wt.FILETIME(), wt.FILETIME()
            if k32.GetProcessTimes(hp, ctypes.byref(c), ctypes.byref(e), ctypes.byref(kt), ctypes.byref(ut)):
                cpu += ft(kt) + ft(ut)
            i = IOC()
            if k32.GetProcessIoCounters(hp, ctypes.byref(i)):
                io += i.rb + i.wb + i.ob
            k32.CloseHandle(hp)
        return cpu, io
    except Exception:
        return None


# ---------- 小工具 ----------
def short(v, n=140):
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    s = " ".join(s.split())
    return s if len(s) <= n else s[:n] + "…(+%d)" % (len(s) - n)


def notify(cmd, kind, msg, rid, d):
    """--notify-cmd / AGY_NOTIFY_CMD：额度、登录这类必须有人处理的告警，由运行器自己执行用户指定的命令（桌面通知、发消息都行），
    不依赖主代理有没有通知工具。命令经 shell 执行，信息放在环境变量里；失败一律忽略，不能影响运行。"""
    if not cmd:
        return
    e = dict(os.environ, AGY_ALERT_KIND=kind, AGY_ALERT_MSG=msg, AGY_RUN_ID=str(rid), AGY_RUN_DIR=str(d))
    try:
        subprocess.Popen(cmd, shell=True, env=e, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NOWIN)
    except Exception:
        pass


def atomic_write(path, text):
    """写临时文件再替换，watch/status 读到的永远是完整的 JSON。Windows 上对方正好打开着时重试几次。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    for i in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.2)
    path.write_text(text, encoding="utf-8")


def parse_dur(s):
    """5400 / 90s / 30m / 2h / 1h30m；裸数字按秒。写错抛 ValueError（在做任何有副作用的事之前就会被调用）。"""
    s = str(s).strip().lower()
    if re.fullmatch(r"\d+", s):
        return int(s)
    parts = re.findall(r"(\d+)([hms])", s)
    if not parts or "".join(n + u for n, u in parts) != s:
        raise ValueError("--timeout 形如 5400 / 90s / 30m / 2h / 1h30m，收到 %r" % s)
    return sum(int(n) * {"h": 3600, "m": 60, "s": 1}[u] for n, u in parts)


def human_tool(name, params):
    """把一次工具调用写成人能看懂的一行（给 digest.log / watch 用）。"""
    g = (lambda *ks: next((params[k] for k in ks if isinstance(params, dict) and params.get(k)), ""))
    base = lambda v: str(v).replace("\\", "/").rstrip("/").split("/")[-1]
    if name == "run_command":
        return "[运行] " + short(g("CommandLine"), 220)
    if READ_TOOL.search(name):
        target = g("AbsolutePath", "TargetFile", "DirectoryPath", "SearchPath", "Path")
        extra = g("Query", "Pattern")
        return "[读] %s%s" % (base(target) if target else name, "  搜: " + short(extra, 80) if extra else "")
    if EDIT_TOOL.search(name):
        return "[改] " + base(g("TargetFile", "AbsolutePath", "Path") or name)
    return "[工具] %s %s" % (name, short(params, 160))


def follow_thinking(cid, log, stop):
    """stream-json 不带思考过程；它只落在磁盘 transcript 里。跟随它，把思考摘要补进人读日志。"""
    path = Path.home() / ".gemini" / "antigravity-cli" / "brain" / cid / ".system_generated" / "logs" / "transcript.jsonl"
    try:
        pos = path.stat().st_size if path.exists() else 0  # 续接时跳过历史
    except Exception:
        pos = 0
    buf = b""
    while not stop():
        time.sleep(2)
        try:
            with open(path, "rb") as f:
                f.seek(pos); chunk = f.read(); pos += len(chunk)
        except Exception:
            continue
        buf += chunk
        *lines, buf = buf.split(b"\n")
        for ln in lines:
            try:
                r = json.loads(ln)
            except Exception:
                continue
            th = (r.get("thinking") or "").strip()
            if th:
                log("[想] " + short(th, 500))


def agy_version():
    try:
        return subprocess.run([AGY, "--version"], stdin=subprocess.DEVNULL, capture_output=True, timeout=20, creationflags=NOWIN).stdout.decode("utf-8", "replace").strip() or "?"
    except Exception:
        return "?"


def get_quota():
    """零 token：/usage 是本地斜杠命令。返回 {bucket_id: (剩余比例, 重置时间)}。"""
    p = subprocess.run([AGY, "-p", "/usage", "--output-format", "json", "--print-timeout", "60s"],
                       stdin=subprocess.DEVNULL, capture_output=True, timeout=90, creationflags=NOWIN)
    out = p.stdout.decode("utf-8", "replace")
    try:
        d = json.loads(out)
        return {b["id"]: (b.get("remaining_fraction", 0.0), b.get("reset_time", ""))
                for g in d["command"]["data"]["groups"] for b in g["buckets"]}
    except Exception:
        raise RuntimeError("无法读取额度: " + short(out + p.stderr.decode("utf-8", "replace"), 300))


def quota_line(q):
    return " ".join("%s=%d%%" % (k, round(v[0] * 100)) for k, v in q.items())


def quota_low(q, model, floor):
    grp = "3p" if re.search(r"claude|gpt", model or "", re.I) else "gemini"
    low = [(k, v) for k, v in q.items() if k.startswith(grp) and v[0] < floor]
    return ["%s 剩余 %d%%，重置于 %s" % (k, round(v[0] * 100), v[1]) for k, v in low]


def cmd_quota(a):
    try:
        q = get_quota()
    except Exception as e:
        print("ALERT QUOTA_UNKNOWN %s" % e); return 5
    print("QUOTA " + quota_line(q))
    low = quota_low(q, a.model, a.min_quota)
    if low:
        print("ALERT QUOTA_LOW " + "; ".join(low) + " —— 请通知用户重新登录其他账号 (agy 交互界面里 /logout 后再登录)")
        return 3
    return 0


def read_meta(d):
    try:
        return json.loads((d / "meta.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def latest_run():
    runs = sorted((p for p in RUNS.glob("*") if (p / "meta.json").exists()), key=lambda p: read_meta(p).get("started", ""))
    if not runs:
        sys.exit("没有任何运行记录")
    return runs[-1]


def run_dir(rid):
    return latest_run() if rid in (None, "latest") else RUNS / rid


VOLATILE = re.compile(r"0x[0-9a-f]+|\d{1,2}:\d{2}(:\d{2})?([.,]\d+)?|\d+([.,]\d+)?\s*(ms|s|sec|secs|seconds|m|min)\b|\d{4}-\d{2}-\d{2}", re.I)


def skeleton(o):
    """输出的骨架：去掉耗时、时间戳、地址，保留数量。两次失败只差耗时就算同一个结果。"""
    return " ".join(VOLATILE.sub("#", o[-4000:]).split())


def git_fingerprint(cwd):
    """工作区内容指纹；不是 git 仓库时返回 None。"""
    try:
        a = subprocess.run(["git", "status", "--porcelain"], cwd=cwd, capture_output=True, timeout=15, creationflags=NOWIN)
        if a.returncode != 0:
            return None
        b = subprocess.run(["git", "diff"], cwd=cwd, capture_output=True, timeout=30, creationflags=NOWIN)
        return hashlib.md5(a.stdout + b.stdout).hexdigest()
    except Exception:
        return None


def git_changes(cwd):
    try:
        p = subprocess.run(["git", "status", "--porcelain"], cwd=cwd, capture_output=True, timeout=15, creationflags=NOWIN)
        return len([l for l in p.stdout.splitlines() if l.strip()]) if p.returncode == 0 else None
    except Exception:
        return None


def norm_path(v):
    return str(v).replace("\\", "/").rstrip("/").lower()


def expected_outputs(prompt, explicit):
    """任务书里"输出文件"的清单：markdown 表格里一行有 2 个以上反引号路径时，取最后一个；再加 --expect。
    不懂任务内容，只做廉价的覆盖率核对：清单里的文件有没有被 agy 的写文件工具成功写过。"""
    exp = set(norm_path(x) for x in (explicit or []))
    for line in prompt.splitlines():
        if line.lstrip().startswith("|"):
            ps = [x for x in TABLE_PATH.findall(line) if re.search(r"[\\/]", x) and "." in norm_path(x).split("/")[-1]]
            if len(ps) >= 2:
                exp.add(norm_path(ps[-1]))
    return exp


# ---------- agy 项目管理 ----------
def uri_to_path(v):
    """file:// 地址 → 普通路径；普通路径原样返回（不能对真实路径做 URL 解码：目录名里可以有 %20 这样的字面量）。"""
    v = str(v)
    if v.startswith("file:"):
        v = unquote(re.sub(r"^file://", "", v)).lstrip("/")
    return v


def norm_dir(v):
    """目录或 file:// 地址 → 可比较的形式（两种写法都见过：file://F:/x 和 file:///d%3A/x）。"""
    return uri_to_path(v).replace("\\", "/").rstrip("/").lower()


def list_projects():
    out = []
    for f in sorted(PROJECTS.glob("*.json"), key=lambda f: f.stat().st_mtime):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        res = (d.get("projectResources") or {}).get("resources") or []
        out.append({"id": d.get("id") or f.stem, "name": d.get("name", ""), "file": f,
                    "dirs": [uri_to_path(r["folderUri"]) for r in res if r.get("folderUri")],
                    # 桌面端自己建的项目多出 settings / isWorkspaceOnly 两个字段（运行器建的项目没有）：gc 不碰
                    "ide": "settings" in d or "isWorkspaceOnly" in d})
    return out


def find_project(cwd):
    """指向这个目录的已有项目的 ID；没有则 None。"""
    want = norm_dir(cwd)
    for pr in list_projects():
        if any(norm_dir(d) == want for d in pr["dirs"]):
            return pr["id"]
    return None


def summary_query(sql, args=()):
    """查两个"会话摘要库"（有 project_id / workspace_uris 列，几百 KB 到十几 MB）。先复制到临时目录再读，
    不碰 agy 正在用的文件；任何一个读不了就返回 None（调用方退回全文扫描）。"""
    rows = []
    with tempfile.TemporaryDirectory() as td:
        for i, db in enumerate(SUMMARY_DBS):
            if not db.exists():
                continue
            try:
                for suf in ("", "-wal", "-shm"):
                    s = Path(str(db) + suf)
                    if s.exists():
                        shutil.copy2(s, Path(td) / ("%d.db%s" % (i, suf)))
                c = sqlite3.connect(str(Path(td) / ("%d.db" % i)))
                rows += c.execute(sql, args).fetchall()
                c.close()
            except Exception:
                return None
    return rows


def project_in_use(pid, skip_cid=None):
    """是否有会话属于这个项目。先查摘要库（毫秒级）；摘要库说没有，再用全文扫描复核
    （会话库里以明文存着项目 ID；只有真要删的时候才会走到这里）。skip_cid：不算这个会话本身。"""
    r = summary_query("select conversation_id from conversation_summaries where project_id=?", (pid,))
    if r and any(x[0] != skip_cid for x in r):
        return True
    key = pid.encode("ascii", "ignore")
    for base in CONVOS:
        for f in base.glob("*.db*"):
            if skip_cid and f.name.startswith(skip_cid):
                continue
            try:
                if key in f.read_bytes():
                    return True
            except Exception:
                return True   # 读不了就当作在用，宁可不删
    return False


def own_projects(cid, cands):
    """候选项目里哪些属于这个会话：会话库里以明文存着项目 ID。靠身份判断，不靠"运行前后项目集合之差"
    （并发的别的运行也会让集合变大）。"""
    found = set()
    if cid and UUID_RE.fullmatch(cid):
        for base in CONVOS:
            for f in base.glob(cid + ".db*"):
                try:
                    data = f.read_bytes()
                except Exception:
                    continue
                found |= {c for c in cands if c.encode() in data}
    return found


def remove_session(cid, log):
    """throwaway：删这次会话的全部落盘文件。cid 必须是完整 UUID（否则前缀通配会删到别人的会话）。"""
    if not (cid and UUID_RE.fullmatch(cid)):
        log("WARN throwaway 不清理：会话 ID %r 不是完整 UUID" % cid)
        return 0
    targets = [f for base in CONVOS for f in base.glob(cid + ".db*")]
    targets += [f for base in SIDECARS for f in base.glob(cid + ".*")]
    n = 0
    for f in targets:
        for _ in range(5):
            try:
                f.unlink(missing_ok=True); n += 1; break
            except PermissionError:      # agy 的子进程可能还没放手
                time.sleep(1)
        else:
            log("WARN 删除失败(被占用): %s" % f)
    for base in BRAINS:
        shutil.rmtree(base / cid, ignore_errors=True)
    return n


class DirLock:
    """同一目录"首次运行"的互斥。agy 从启动到写出项目文件约 2~3 秒，这段时间里第二个同目录运行
    查不到项目，会再建一个，而且两个项目都有会话，gc 永远清不掉。进程死了系统自动放锁，不会留下死锁。"""
    def __init__(self, cwd):
        d = RUNS.parent / "agy-run-locks"      # 放在 agy-runs 外面，免得遍历运行目录的脚本碰到它
        d.mkdir(parents=True, exist_ok=True)
        self.path, self.f = d / (hashlib.md5(norm_dir(cwd).encode()).hexdigest() + ".lock"), None

    def acquire(self, timeout=90):
        import msvcrt
        self.f = open(self.path, "a+b")
        end = time.time() + timeout
        while True:
            try:
                self.f.seek(0); msvcrt.locking(self.f.fileno(), msvcrt.LK_NBLCK, 1); return True
            except OSError:
                if time.time() > end:
                    return False
                time.sleep(0.3)

    def release(self):
        if self.f:
            try:
                import msvcrt
                self.f.seek(0); msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK, 1)
            except Exception:
                pass
            self.f.close(); self.f = None


def drop_projects(ids):
    """删掉项目定义。不再改桌面端的 app_storage.json：projectsOrder 在文件里是"JSON 字符串"不是数组，
    原来的分支从未执行过；桌面端自己维护这个文件，外部整文件改写有丢更新和写坏的风险。"""
    ids = set(ids)
    for pr in list_projects():
        if pr["id"] in ids:
            try:
                pr["file"].unlink(missing_ok=True)
            except PermissionError:
                pass


def cmd_gc(a):
    """清理空项目：目录已不存在，或与更早的项目指向同一目录，并且没有任何会话。
    没有目录的项目（如默认项目）和桌面端自建的项目不动。"""
    kept, drop = set(), []
    allp = list_projects()
    for pr in allp:
        if not pr["dirs"] or pr["ide"]:
            continue
        key = norm_dir(pr["dirs"][0])
        gone = not any(Path(d).exists() for d in pr["dirs"])
        dup = key in kept
        if (gone or dup) and not project_in_use(pr["id"]):
            drop.append(pr); print("%s %s  %s  %s" % ("DROP" if not a.dry_run else "WOULD_DROP", pr["id"], pr["name"], "目录不存在" if gone else "重复"))
        else:
            kept.add(key)
    if drop and not a.dry_run:
        drop_projects(p["id"] for p in drop)
    print("GC %s%d 个，保留 %d 个" % ("可清理 " if a.dry_run else "已清理 ", len(drop), len(allp) - len(drop)))
    return 0


# ---------- run ----------
def start_detached(a):
    """后台启动：运行器本体脱离调用方的进程树独立运行，调用方立刻拿到 run id，之后用 wait 取事件。
    给没有"后台进程 + 输出行唤醒"能力的主代理用。尝试脱离父进程的 job（BREAKAWAY），不允许时退回只脱离控制台。"""
    rid = time.strftime("%m%d-%H%M%S") + ("-" + a.name if a.name else "")
    d, n = RUNS / rid, 1
    while True:
        try:
            d.mkdir(parents=True)
            break
        except FileExistsError:
            n += 1; d = RUNS / ("%s-%d" % (rid, n))
    me = str(Path(__file__).resolve())
    argv = [sys.executable, "-u", me] + [x for x in sys.argv[1:] if x != "--detach"] + ["--rid", d.name]
    so = open(d / "stdout.log", "ab")
    p = None
    for flags in (NOWIN | 0x00000200 | 0x01000000, NOWIN | 0x00000200):   # NO_WINDOW | NEW_PROCESS_GROUP [| BREAKAWAY_FROM_JOB]；不用 DETACHED_PROCESS：那会让每个子进程各弹一个窗口
        try:
            p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=so, stderr=subprocess.STDOUT, creationflags=flags, close_fds=True)
            break
        except OSError:
            continue
    if p is None:
        print("END verdict=BAD_ARGS rc=2 reason=无法后台启动运行器", flush=True)
        return 2
    print("STARTED run=%s runner_pid=%s dir=%s" % (d.name, p.pid, d), flush=True)
    print('下一步: python "%s" wait %s' % (me, d.name), flush=True)
    return 0


def cmd_run(a):
    def bad(why):
        print("END verdict=BAD_ARGS rc=2 reason=%s" % why, flush=True)
        return 2
    try:
        tsec = parse_dur(a.timeout)
    except ValueError as e:
        return bad(e)
    a.timeout = "%ds" % tsec
    if a.throwaway and a.resume:
        return bad("--throwaway 不能和 --resume 同用：结束时会把被续接的旧会话一起删掉")
    try:
        prompt = Path(a.prompt_file).read_text(encoding="utf-8") if a.prompt_file else a.prompt
    except OSError as e:
        return bad("读不了任务书: %s" % e)
    if not prompt:
        return bad("需要 --prompt 或 --prompt-file")
    if not Path(a.cwd).is_dir():
        return bad("--cwd 不是目录: %s" % a.cwd)
    if a.detach and not a.rid:
        return start_detached(a)
    box = {}
    try:
        return _cmd_run(a, box, prompt, str(Path(a.cwd).resolve()), tsec)
    except BaseException as e:               # 崩溃 / Ctrl-C / 异常：不让 agy 变成孤儿，也不让 meta 停在 running
        p = box.get("p")
        if p is not None and p.poll() is None:
            if not job_kill(box.get("job")):
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, creationflags=NOWIN)
        if box.get("save_meta"):
            try:
                box["save_meta"](state="crashed", verdict="RUNNER_" + type(e).__name__, rc=5, ended=time.strftime("%F %T"))
            except Exception:
                pass
        try:
            print("END run=%s verdict=RUNNER_%s rc=5 reason=%s" % (box.get("rid"), type(e).__name__, short(str(e), 160)), flush=True)
        except Exception:
            pass
        raise


def _cmd_run(a, box, prompt, cwd, tsec):
    if a.rid:           # 后台启动时由父进程先定好运行名
        d = RUNS / a.rid
        d.mkdir(parents=True, exist_ok=True)
    else:
        rid = time.strftime("%m%d-%H%M%S") + ("-" + a.name if a.name else "")
        d, n = RUNS / rid, 1
        while True:
            try:
                d.mkdir(parents=True)
                break
            except FileExistsError:
                n += 1; d = RUNS / ("%s-%d" % (rid, n))
    rid = d.name
    box["rid"] = rid
    (d / "prompt.txt").write_text(prompt, encoding="utf-8")
    digest = open(d / "digest.log", "a", encoding="utf-8")
    t0 = time.time()
    lock = threading.Lock()

    def log(msg):  # 人看的全量摘要
        with lock:
            digest.write("%s [%6.1fs] %s\n" % (time.strftime("%H:%M:%S"), time.time() - t0, msg)); digest.flush()

    def out(msg):  # 给调用方的稀疏行；默认只推 ALERT/WARN/END，其余只写日志。管道断了也不能影响 agy
        log(msg)
        if a.stdout != "all" and not msg.startswith(("ALERT ", "END ", "WARN ")):
            return
        try:
            print("%s [%5.0fs] %s" % (time.strftime("%H:%M:%S"), time.time() - t0, msg), flush=True)
        except Exception:
            pass

    meta = {"run_id": rid, "cwd": cwd, "state": "starting", "conversation_id": a.resume, "started": time.strftime("%F %T"),
            "runner_pid": os.getpid(), "runner": "0.6"}

    def save_meta(**kw):
        meta.update(kw)
        atomic_write(d / "meta.json", json.dumps(meta, ensure_ascii=False, indent=1))

    box["save_meta"] = save_meta
    save_meta()
    ver = agy_version()
    save_meta(agy_version=ver)
    if ver not in TESTED:
        out("WARN agy 版本 %s 不在已实测列表（最新 %s）—— 行为可能变化，结果请多核对" % (ver, TESTED[-1]))
    q_before = None
    if not a.no_quota_check:
        try:
            q = get_quota(); q_before = q
            low = quota_low(q, a.model, a.min_quota)
            if low:
                out("ALERT QUOTA_LOW " + "; ".join(low) + " —— 未启动任务，请通知用户换账号")
                notify(a.notify_cmd, "QUOTA_LOW", "; ".join(low), rid, d)
                save_meta(state="finished", verdict="QUOTA", rc=3, ended=time.strftime("%F %T"))
                print("END run=%s verdict=QUOTA rc=3 reason=precheck %s" % (rid, short("; ".join(low), 160)), flush=True)
                return 3
            out("QUOTA " + quota_line(q))
        except Exception as e:
            out("WARN 额度预检失败，继续执行: %s" % short(str(e), 200))

    cmd = [AGY, "--input-format", "stream-json", "--output-format", "stream-json",
           "--mode", "plan" if a.plan else "accept-edits", "--print-timeout", a.timeout]
    if not a.plan:
        cmd.append("--dangerously-skip-permissions")
    model = re.sub(r"-(low|medium|high)$", "", a.model) if (a.effort and a.model) else a.model   # agy 1.4.2：带档位后缀的模型名和 --effort 冲突
    if model: cmd += ["--model", model]
    if a.effort: cmd += ["--effort", a.effort]
    cmd += ["--log-file", str(d / "agy.log")]
    dlock = DirLock(cwd)
    if not a.resume:
        dlock.acquire()                  # 同目录首次运行互斥：持锁到 INIT（那时 agy 的项目文件已经写出）
    before = {pr["id"] for pr in list_projects()}
    proj = find_project(cwd)
    if a.resume:
        cmd += ["--conversation", a.resume]
        try:
            ws = summary_query("select workspace_uris from conversation_summaries where conversation_id=?", (a.resume,))
            dirs = {norm_dir(u) for r in (ws or []) for u in json.loads(r[0] or "[]")}
        except Exception:
            dirs = set()
        if dirs and norm_dir(cwd) not in dirs:
            out("WARN --cwd 与会话所属目录不一致：agy 实际在 %s 工作，运行器的 git 监控看的是 --cwd" % " | ".join(sorted(dirs)))
    elif proj:
        cmd += ["--project", proj]       # 同一目录只用一个项目
        dlock.release()
    else:
        cmd += ["--new-project"]
    p = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=NOWIN)
    box["p"] = p
    job = make_job(rid)
    box["job"] = job
    jobbed = job_add(job, p)
    save_meta(pid=p.pid, state="running", cmd=cmd[1:], job=bool(jobbed))
    out("START run=%s pid=%s cwd=%s%s%s" % (rid, p.pid, cwd, " resume=" + a.resume if a.resume else "", "" if jobbed else " (无 job object)"))
    try:
        p.stdin.write((json.dumps({"event": "user", "message": {"content": prompt}}, ensure_ascii=False) + "\n").encode("utf-8"))
        p.stdin.flush()
    except OSError as e:
        out("ALERT AGY_EXITED_EARLY agy 在读到任务书前就退出了 (%s)；见 STDERR 行" % e)

    st = {"last": time.time(), "active": None, "steps": 0, "tools": 0, "tokens": 0, "step_usage": 0, "alerts": set(),
          "asked": False, "denied": "", "quota": False, "auth": False, "killed": "", "done": False,
          "novel": 0, "said": 0, "ptimeout": False, "reset": "", "agy_err": None, "err_steps": 0, "ok_after_err": 0,
          "last_kind": "", "written": set(), "files": [], "cmds": [], "tool_errors": 0}
    expected = set() if a.no_auto_expect and not a.expect else expected_outputs("" if a.no_auto_expect else prompt, a.expect)
    will_retry_auth = a.auto_resume > 0 and not getattr(a, "auth_retried", False) and not a.throwaway

    def alert(kind, msg, once_key=None):
        key = once_key or kind
        if key in st["alerts"]:
            return
        st["alerts"].add(key)
        out("ALERT %s %s" % (kind, msg))
        if kind in NOTIFY_KINDS:
            notify(a.notify_cmd, kind, msg, rid, d)

    def scan_text(text, where):
        if QUOTA_PAT.search(text):
            m = RESET_PAT.search(text)
            if m:
                st["reset"] = m.group(1)
            st["quota"] = True
            alert("QUOTA", "%s: %s —— 请通知用户重新登录其他账号%s" % (where, short(text, 200), "，额度重置于 " + st["reset"] + " 后" if st["reset"] else ""))
        elif AUTH_PAT.search(text):
            st["auth"] = True
            if will_retry_auth:      # 多为 token 到期的瞬时错误：运行器会用新进程续接一次，成功就不必叫醒任何人
                log("AUTH(将自动续接一次) %s: %s" % (where, short(text, 200)))
            else:
                alert("AUTH", "%s: %s —— 需要用户重新登录 agy" % (where, short(text, 200)))

    def errpump():
        for line in p.stderr:
            t = line.decode("utf-8", "replace").strip()
            if not t:
                continue
            log("STDERR " + short(t, 400))
            scan_text(t, "stderr")
            if PTIMEOUT_PAT.search(t):
                st["ptimeout"] = True
                alert("PRINT_TIMEOUT", "agy 自己的 --print-timeout 到点，在回合中途返回了半截输出（status 会报 SUCCESS，但任务没做完）")
            if t.startswith("AGY_ERROR:"):
                try:
                    st["agy_err"] = json.loads(t[len("AGY_ERROR:"):])
                except Exception:
                    pass
            if "auto-denied" in t:
                st["denied"] = st["denied"] or "stderr"
                alert("DENIED", short(t, 240))
    threading.Thread(target=errpump, daemon=True).start()

    def kill(reason, why=""):
        if st["killed"]:
            return
        st["killed"] = reason
        if why:
            out("ALERT AUTOKILL %s —— 已自动终止，成果保留，按 HINT 续接并在纠偏提示里写明这一点" % why)
        if not job_kill(job):
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, creationflags=NOWIN)

    def loop_hit(n, first, what, kind="LOOP"):
        """重复 n 次：到 first 及其整数倍时告警，到 3 倍时终止（--loop-kill 0 只告警）。"""
        if n % first:
            return
        out("ALERT %s %s，已 %d 次" % (kind, what, n))
        if a.loop_kill and n >= first * 3:
            kill("loop", "%s，已 %d 次" % (what, n))

    limit = tsec + 60

    def watchdog():
        next_hb = time.time() + a.heartbeat
        stall_n, usage_prev, quiet_since, next_probe = 0, None, time.time(), 0
        hb = {"tools": 0, "novel": 0, "fp": git_fingerprint(cwd)}
        win = {"at": time.time() + a.progress_window, "tools": 0, "novel": 0, "steps": 0, "said": 0, "fp": hb["fp"], "bad": 0}
        while not st["done"]:
            time.sleep(2)
            now = time.time()
            if now - t0 > limit:
                alert("TIMEOUT", "超过 %s，已终止进程" % a.timeout); kill("timeout"); return
            idle = now - st["last"]
            cmd_active = st["active"] is not None
            if idle < 5:
                stall_n = 0; usage_prev = None; quiet_since = now
            # 命令执行中：每隔一会儿看进程树的 CPU/IO；有增长说明命令在干活，不是挂死
            if cmd_active and now >= next_probe:
                next_probe = now + a.probe_every
                u = tree_usage(p.pid)
                if u is not None:
                    if usage_prev is None or u[0] - usage_prev[0] > 0.05 or u[1] - usage_prev[1] > 65536:
                        quiet_since = now
                    usage_prev = u
            first = max(a.stall, a.stall_cmd if cmd_active else a.stall_think)
            if cmd_active and a.cmd_quiet and idle > max(first, a.cmd_quiet) and now - quiet_since > a.cmd_quiet:
                kill("stall", "命令 %s 已 %ds 无事件且进程树 %ds 内 CPU/IO 零增长（挂死）" % (st["active"], idle, now - quiet_since)); return
            # 挂起的命令不会自己结束；模型思考中 agy 自己 5 分钟取消重试
            hard = a.idle_kill if a.idle_kill is not None else (a.cmd_kill if cmd_active else a.think_kill)
            if hard and idle > hard:
                kill("stall", "已 %ds 无任何事件；当前: %s" % (idle, st["active"] or "模型思考中")); return
            if st["steps"] == 0 and idle > 40 and "SLOW_START" not in st["alerts"]:
                st["alerts"].add("SLOW_START")      # 只记日志，不推给调用方：它不需要处理；总耗时和原因会写进 END 行和 result.json
                try:
                    lg = [l for l in (d / "agy.log").read_text(encoding="utf-8", errors="replace").splitlines() if "still connecting" in l]
                    st["slow_detail"] = short(lg[-1].split("] ", 1)[-1], 160) if lg else "未知(查 agy.log)"
                except Exception:
                    st["slow_detail"] = "未知(查 agy.log)"
                log("SLOW_START INIT 后 %ds 仍无活动；agy 在等 MCP 服务器: %s" % (idle, st["slow_detail"]))
            if stall_n < 3 and idle > first * (2 ** stall_n):
                stall_n += 1
                working = cmd_active and (now - quiet_since) < a.probe_every * 3 and stall_n < 3   # 进程树还在涨 CPU/IO：只记日志，不打扰调用方
                msg = "STALL 已 %ds 无新事件；当前: %s%s" % (idle, st["active"] or "模型思考中",
                                                             "" if not cmd_active else "；命令进程树 CPU/IO 已 %ds 无增长" % (now - quiet_since))
                if working:
                    log(msg)
                else:
                    out("ALERT " + msg)
                if a.stall_kill and stall_n >= a.stall_kill:
                    kill("stall", "连续 %d 次 STALL（%ds 无事件）" % (stall_n, idle)); return
            if a.progress_window and now >= win["at"]:
                # 空转判定：一个窗口内工具调用不少，却没有一条是新的调用，工作区也没变，agy 也没说新的话
                fp = git_fingerprint(cwd)
                dt, dn, ds = st["tools"] - win["tools"], st["novel"] - win["novel"], st["steps"] - win["steps"]
                spin = (dt >= 10 and dn == 0) or (dt == 0 and ds >= 40)
                if spin and fp == win["fp"] and st["said"] == win["said"]:
                    win["bad"] += 1
                    what = "近 %d 分钟 %d 次工具调用全是重复的、%d 步、工作区无变化" % (a.progress_window // 60, dt, ds)
                    out("ALERT NOPROGRESS %s（第 %d 个窗口）" % (what, win["bad"]))
                    if a.loop_kill and win["bad"] >= 2:
                        kill("noprogress", what + "，连续两个窗口"); return
                else:
                    win["bad"] = 0
                win.update(at=now + a.progress_window, tools=st["tools"], novel=st["novel"], steps=st["steps"], said=st["said"], fp=fp)
            if now >= next_hb:
                next_hb = now + a.heartbeat
                g, fp = git_changes(cwd), git_fingerprint(cwd)
                dt, dn = st["tools"] - hb["tools"], st["novel"] - hb["novel"]
                msg = "HEARTBEAT steps=%d tools=%d tokens=%dk%s 本段: 工具+%d(新调用 %d)%s 当前: %s" % (
                    st["steps"], st["tools"], st["tokens"] // 1000,
                    "" if g is None else " 未提交改动=%d" % g, dt, dn,
                    "" if fp is None else " 工作区%s" % ("有变化" if fp != hb["fp"] else "无变化"),
                    st["active"] or "模型思考中")
                if dt >= 10 and dn == 0:     # 只有"工具调用不少却全是旧调用"才推给调用方；正常心跳只写日志
                    out("ALERT " + msg)
                else:
                    log(msg)
                hb.update(tools=st["tools"], novel=st["novel"], fp=fp)
    threading.Thread(target=watchdog, daemon=True).start()

    texts, last_text, result, repeat = {}, "", None, {}
    seen, cycle, said, said_at = set(), {}, {}, {}
    md5 = lambda t: hashlib.md5(t.encode("utf-8", "replace")).hexdigest()
    with open(d / "events.ndjson", "ab") as raw:
        for line in p.stdout:
            raw.write(line); raw.flush()
            st["last"] = time.time()
            try:
                e = json.loads(line)
            except Exception:
                t = line.decode("utf-8", "replace")
                log("NONJSON " + short(t, 300)); scan_text(t, "stdout"); continue
            ev = e.get("event")
            if ev == "init":
                cid = e.get("conversation_id") or e.get("init", {}).get("conversation_id")
                save_meta(conversation_id=cid)
                dlock.release()
                st["init_t"] = time.time()
                ini = e.get("init", {})
                out("INIT conversation=%s mode=%s model=%s" % (cid, ini.get("permission_mode"), ini.get("model")))
                if not a.plan and ini.get("permission_mode") != "always-proceed":
                    alert("DRIFT", "permission_mode=%s，期望 always-proceed（--dangerously-skip-permissions 语义变了？写文件/跑命令可能被拒）" % ini.get("permission_mode"))
                if ini.get("model") and a.model and not str(ini["model"]).startswith(re.sub(r"-(low|medium|high)$", "", a.model)):
                    alert("DRIFT", "请求模型 %s，实际 %s" % (a.model, ini.get("model")), "DRIFT_MODEL")
                save_meta(model=ini.get("model"), permission_mode=ini.get("permission_mode"))
                if cid and not st.get("thinking_started"):
                    st["thinking_started"] = True
                    threading.Thread(target=follow_thinking, args=(cid, log, lambda: st["done"]), daemon=True).start()
            elif ev == "step_update":
                s = e["step_update"]
                idx, state, ty = s.get("step_index"), s.get("state"), s.get("step_type")
                if st.get("init_t") and st.get("first_step_t") is None:       # INIT 到第一个步骤之间的空档：启动慢（多半在等 MCP 服务器）
                    st["first_step_t"] = time.time()
                    if st["first_step_t"] - st["init_t"] >= 30:
                        st["slow_start_s"] = round(st["first_step_t"] - st["init_t"])
                if s.get("text_delta"):
                    texts[idx] = texts.get(idx, "") + s["text_delta"]
                if s.get("usage"):
                    st["tokens"] += s["usage"].get("total_tokens", 0)
                    st["step_usage"] += s["usage"].get("total_tokens", 0)
                if state in ("DONE", "ERROR"):
                    st["steps"] += 1
                if ty == "tool":
                    name, ti = s.get("tool_name") or "?", s.get("tool_info") or {}
                    if state not in ("DONE", "ERROR"):
                        params = ti.get("parameters", "")
                        st["active"] = "#%s %s %s" % (idx, name, short(params, 100))
                        if name == "run_command" and a.verbose:
                            cl = params.get("CommandLine", params) if isinstance(params, dict) else params
                            out("#%s $ %s" % (idx, short(cl, 160)))
                        else:
                            log(human_tool(name, params))
                        if name.startswith("ask_"):
                            st["asked"] = True
                    else:
                        st["tools"] += 1; st["active"] = None; st["last_kind"] = "tool"
                        if st["err_steps"]:
                            st["ok_after_err"] += 1
                        tool_err = state == "ERROR" or bool(ti.get("error"))
                        if tool_err:
                            st["tool_errors"] += 1
                            em = ti.get("error")
                            log("       ↳ TOOL_ERROR %s: %s" % (name, short((em.get("message") if isinstance(em, dict) else em) or "", 200)))
                        _p = ti.get("parameters") or {}
                        if EDIT_TOOL.search(name) and not tool_err and isinstance(_p, dict):
                            tf = _p.get("TargetFile") or _p.get("AbsolutePath") or _p.get("Path")
                            if tf:
                                st["written"].add(norm_path(tf))
                                if tf not in st["files"]:
                                    st["files"].append(tf)
                        if name == "run_command" and isinstance(_p, dict):
                            st["cmds"].append({"cmd": short(_p.get("CommandLine", ""), 120), "sec": round(s.get("duration_seconds") or 0, 1)})
                        o = ti.get("output", "") or ""
                        if not o and ti.get("error"):
                            o = "ERR:" + json.dumps(ti["error"], ensure_ascii=False)
                        o = o if isinstance(o, str) else json.dumps(o, ensure_ascii=False)
                        if name == "run_command":
                            tail = " ".join(o.split())
                            log("       ↳ %.1fs  %s" % (s.get("duration_seconds") or 0, ("…" + tail[-300:]) if len(tail) > 300 else (tail or "(无输出)")))
                        elif not (READ_TOOL.search(name) or EDIT_TOOL.search(name)) and o.strip():
                            log("       ↳ " + short(o, 200))
                        # 工具输出是任务内容（grep/cat/测试日志），不是 agy 自己的错误通道：不再扫 QUOTA/AUTH
                        if "powershell.config.json" in o and "UnauthorizedAccess" in o:
                            alert("SANDBOX", "命令在终端沙箱里失败(读不到 powershell.config.json)，检查 agy settings.json 的 enableTerminalSandbox")
                        call = name + json.dumps(ti.get("parameters", ""), sort_keys=True, ensure_ascii=False)
                        desc = "%s %s" % (name, short(ti.get("parameters", ""), 140))
                        if EDIT_TOOL.search(name):
                            # 事件流里的编辑只带文件路径，不带改了什么，所以无法判断"同一处编辑重复"；每次编辑都算一次新进展
                            st["novel"] += 1
                            repeat.clear()  # 中间改过文件，重复执行同一命令是合理的
                        elif md5(call) not in seen:
                            seen.add(md5(call)); st["novel"] += 1
                        if not EDIT_TOOL.search(name):
                            k = md5(call + o)
                            repeat[k] = repeat.get(k, 0) + 1
                            loop_hit(repeat[k], 18 if (READ_TOOL.search(name) or POLL_TOOL.search(name)) else 3, "同一调用且输出相同、期间没有改文件: " + desc)
                            if name == "run_command" and FAILISH.search(ZERO_FAIL.sub("", o)):
                                # 改文件→跑同一条命令→结果骨架不变，且结果像失败（"0 errors" 不算）：改了等于没改
                                k2 = md5(call + skeleton(o))
                                cycle[k2] = cycle.get(k2, 0) + 1
                                if cycle[k2] > repeat[k]:
                                    loop_hit(cycle[k2], 4, "反复改文件但同一命令的结果不变: " + desc, "LOOP_EDIT")
                elif state == "DONE":
                    if ty == "agent_response":
                        t = texts.pop(idx, "")
                        if t.strip():
                            last_text = t
                            st["said"] += 1; st["last_kind"] = "say"
                            if st["err_steps"]:
                                st["ok_after_err"] += 1
                            k = md5(" ".join(t.split()))
                            if said_at.get(k) is not None and said_at[k] != st["novel"]:
                                said[k] = 0       # 两次相同的话之间做过新的事：不是原地打转
                            said_at[k] = st["novel"]
                            said[k] = said.get(k, 0) + 1
                            loop_hit(said[k], 3, "agy 把同一段话原样说了多遍: " + short(t, 100))
                            log("[说] " + short(re.sub(r"\[([^\]]+)\]\(file://[^)]*\)", r"\1", t), 1200))
                    elif ty == "error_message":
                        st["err_steps"] += 1; st["ok_after_err"] = 0; st["last_kind"] = "error"
                        log("#%s error_message（agy 内部 API 重试/出错，细节在 agy.log 或 transcript.jsonl）" % idx)
                    elif ty == "unknown":
                        # 无头模式下 ask_question 被自动跳过，表现为一个 unknown 步骤
                        st["asked"] = True
                        alert("ASK", "agy 想向用户提问/请求确认，但无头模式下被自动跳过；问题见最终回复", "ASK%s" % idx)
                    elif ty not in ("user_input", "system_message"):
                        if ty not in KNOWN_STEPS:
                            alert("DRIFT", "出现未知 step_type=%s（agy 事件格式变化？）" % ty, "DRIFT_STEP_" + str(ty))
                        log("#%s %s %s" % (idx, ty, short({k: v for k, v in s.items() if k != "conversation_id"}, 300)))
            elif ev == "result":
                result = e["result"]
                try:
                    p.stdin.close()
                except Exception:
                    pass
            else:
                alert("DRIFT", "出现未知事件 event=%s（agy 事件格式变化？）" % ev, "DRIFT_EV_" + str(ev))
                log("EVENT " + short(e, 400))
    code = p.wait()
    st["done"] = True
    time.sleep(0.3)

    # ---- 重新判定结果 ----
    r = result or {}
    denied = r.get("denied_actions") or []
    if denied:
        st["denied"] = short(denied, 300)
    err = str(r.get("error") or "")
    if err:
        scan_text(err, "result")
    ae = st["agy_err"] or {}
    if ae.get("status") == "RESOURCE_EXHAUSTED":
        st["quota"] = True
    if ae.get("status") in ("UNAUTHENTICATED", "PERMISSION_DENIED"):
        st["auth"] = True
    # 汇报全文：result.response 与最后一段话取较长者（成功时 76% 的运行里 response 更长；报错收尾时常常是最后一段话更长）
    final = max((r.get("response") or "", last_text or ""), key=len)
    cid = meta.get("conversation_id")
    missing = sorted(expected - st["written"]) if expected else []
    # agy 的 --print-timeout 到点会"成功"返回半截输出；stderr 那行偶尔缺，所以再用时长兜底
    trunc = st["ptimeout"] or (result is not None and r.get("status") == "SUCCESS" and time.time() - t0 >= tsec - 5)
    transient = bool(err and TRANSIENT_PAT.search(err))
    # agy 自己重试恢复后继续干完了，但 result 里仍带着早先那次 API 错误
    recovered = transient and st["err_steps"] and st["ok_after_err"] >= 1 and (
        (st["last_kind"] == "say" and len(final.strip()) >= 100) or (expected and not missing))
    can_resume = bool(cid) and a.auto_resume > 0 and not a.throwaway
    note = ""
    if st["quota"]:
        verdict, rc = "QUOTA", 3
    elif st["auth"]:
        verdict, rc = ("ERROR_AUTH", 5) if (can_resume and not getattr(a, "auth_retried", False)) else ("AUTH", 3)
    elif st["killed"]:
        verdict, rc = "KILLED_" + st["killed"].upper(), 4
    elif result is None:
        verdict, rc = ("KILLED_MANUAL", 4) if (d / "KILL").exists() else ("NO_RESULT", (4 if code else 5))
    elif st["denied"]:
        verdict, rc = "DENIED", 2
    elif st["asked"]:
        verdict, rc = "NEEDS_INPUT", 2
    elif trunc and not (expected and not missing):
        verdict, rc = "TIMEOUT_PARTIAL", 4
    elif r.get("status") != "SUCCESS" or err:
        if recovered or (transient and expected and not missing):
            verdict, rc, note = "DONE_UNVERIFIED", 0, "api_error_recovered"
        elif transient:
            verdict, rc = "ERROR_TRANSIENT", 5
        else:
            verdict, rc = "ERROR", 5
    elif not final.strip():
        verdict, rc = "EMPTY_RESPONSE", 5
    elif missing:
        verdict, rc = "DONE_PARTIAL", 4
    else:
        verdict, rc = "DONE_UNVERIFIED", 0
        if trunc:
            note = "timeout_but_outputs_complete"
    u = r.get("usage") or {}
    g = git_changes(cwd)
    out("RESULT verdict=%s rc=%d agy_status=%s agy_exit=%s steps=%d tools=%d(err %d) dur=%ds tokens=本次%dk(会话累计%dk,+cache %dk)%s%s%s%s" % (
        verdict, rc, r.get("status"), code, st["steps"], st["tools"], st["tool_errors"], time.time() - t0,
        st["step_usage"] // 1000, u.get("total_tokens", st["tokens"]) // 1000, u.get("cache_read_tokens", 0) // 1000,
        "" if g is None else " 未提交改动=%d" % g,
        " denied=" + st["denied"] if st["denied"] else "", " error=" + short(err, 200) if err else "",
        " note=" + note if note else ""))
    if expected:
        out("OUTPUTS 任务书清单 %d 个，agy 写入 %d 个%s" % (len(expected), len(expected) - len(missing),
            "；缺: " + ", ".join(Path(m).name for m in missing[:6]) + (" …(+%d)" % (len(missing) - 6) if len(missing) > 6 else "") if missing else ""))
    (d / "final.txt").write_text(final, encoding="utf-8")
    out("FINAL " + short(final, a.final_chars))
    resumable = verdict in RESUMABLE or (verdict == "QUOTA" and st["steps"] > 0)
    resume_cmd = None
    if cid and (rc in (2, 4) or resumable):
        resume_cmd = 'python "%s" run --cwd "%s" --resume %s --prompt "<回答或纠偏>"' % (Path(__file__).resolve(), cwd, cid)
        out("HINT 续接%s: %s" % ("（额度重置后" + (" " + st["reset"] if st["reset"] else "") + "）" if verdict == "QUOTA" else "", resume_cmd))
    if rc == 0:
        out("HINT agy 的成功不可信：请自行跑测试、看 git diff 验收" + (" （note=%s）" % note if note else ""))
    qa = None
    if not a.no_quota_check:
        try:
            qa = get_quota()
            out("QUOTA_AFTER " + quota_line(qa))
        except Exception as e:
            out("WARN 结束后读取额度失败: %s" % short(str(e), 160))
    job_kill(job)   # 收尾：清掉 agy 退出后遗留的后代（MCP 服务器、孤儿 shell）
    dlock.release()
    created = [pr for pr in list_projects() if pr["id"] not in before]
    # 认领"本次的项目"靠身份（会话库里有没有这个项目 ID），不靠前后集合之差：并发的别的运行也会新建项目
    mine = own_projects(cid, {pr["id"] for pr in created} | ({proj} if proj else set()))
    if not mine and created and not a.resume:      # 会话库还没写出项目 ID（启动期失败）：退而按目录认领
        mine = {pr["id"] for pr in created if any(norm_dir(x) == norm_dir(cwd) for x in pr["dirs"])}
    mine_new = [pr["id"] for pr in created if pr["id"] in mine]
    foreign = [pr["id"] for pr in created if pr["id"] not in mine]
    if foreign:
        log("NOTE 运行期间别的运行新建了项目 %s（不是本次的，已忽略）" % " ".join(foreign))
    if len(mine_new) > 1 or (mine_new and proj):
        out("WARN 本次运行多出了 %d 个 agy 项目: %s —— 运行 gc 清理" % (len(mine_new), " ".join(mine_new)))
    if a.throwaway:
        # 一次性目录：项目、会话、思考记录都不留。之后不能再续接这次会话。清理出任何错都不能让运行器崩在这里
        try:
            drop = [x for x in mine_new if not project_in_use(x, skip_cid=cid)]
            if drop:
                drop_projects(drop)
            nfiles = remove_session(cid, log)
            out("CLEAN 已删除本次的项目(%d 个)和会话 %s (%d 个文件)" % (len(drop), cid, nfiles))
        except Exception as e:
            out("WARN throwaway 清理出错: %s" % short(str(e), 200))
    reset = st["reset"] or ((re.search(r"Resets in ([0-9hms]+)", err + str(ae.get("short_error", ""))) or [None, ""])[1])
    will_resume = verdict in ("ERROR_TRANSIENT", "ERROR_AUTH") and can_resume
    # 要自动续接时，先把"续接成哪个新运行"写进记录：wait 看到就顺着链接去等，运行结束到续接启动之间没有空档
    rid2 = (time.strftime("%m%d-%H%M%S") + "-" + (a.name or "run") + "-r") if will_resume else None
    save_meta(state="finished", verdict=verdict, rc=rc, note=note, exit_code=code, usage=u, ended=time.strftime("%F %T"),
              project=(sorted(mine)[0] if mine else proj), throwaway=bool(a.throwaway),
              expected=len(expected), missing=len(missing), reset=reset, error=err[:300], resumed_as=rid2)
    res = {"run_id": rid, "name": a.name, "cwd": cwd, "verdict": verdict, "rc": rc, "note": note, "agy_status": r.get("status"), "agy_exit": code,
           "agy_version": ver, "model": a.model, "duration_s": round(time.time() - t0), "conversation_id": cid,
           "usage": u, "usage_this_run_tokens": st["step_usage"],
           "quota_before": {k: round(v[0] * 100) for k, v in (q_before or {}).items()},
           "quota_after": {k: round(v[0] * 100) for k, v in (qa or {}).items()},
           "stats": {"steps": st["steps"], "tools": st["tools"], "tool_errors": st["tool_errors"], "alerts": sorted(st["alerts"])},
           "files_touched": st["files"], "commands": st["cmds"][-15:], "commands_total": len(st["cmds"]),
           "expected": sorted(expected), "missing": missing,
           "flags": {"print_timeout_hit": st["ptimeout"], "asked": st["asked"], "denied": st["denied"] or None,
                     "agy_error": ae or None, "quota_resets_in": reset or None, "error": err[:300] or None,
                     "slow_start_s": st.get("slow_start_s"), "slow_start_detail": st.get("slow_detail")},
           "report": {"text": final, "structured": r.get("structured_output"), "chars": len(final)},
           "resume_cmd": resume_cmd}
    atomic_write(d / "result.json", json.dumps(res, ensure_ascii=False, indent=1))
    end = "END run=%s verdict=%s rc=%d dur=%ds tok=%dk files=%d cmds=%d q5h=%s%s%s%s result=%s%s%s" % (
        rid, verdict, rc, time.time() - t0, st["step_usage"] // 1000, len(st["files"]), len(st["cmds"]),
        ("%d%%" % round(qa["gemini-5h"][0] * 100)) if qa and "gemini-5h" in qa else "-",
        " resets_in=" + reset if reset and rc == 3 else "", " note=" + note if note else "",
        (" missing=%d" % len(missing) if missing else "") + (" slow_start=%ds" % st["slow_start_s"] if st.get("slow_start_s") else ""), d / "result.json",
        "  resume: " + resume_cmd if resume_cmd and rc in (2, 4) else "",
        '  final="%s"' % short(final, 120) if final else "")
    if will_resume:
        log(end.replace("END ", "END(中间态，将自动续接) ", 1))       # 马上会自动续接，这一行不必推给调用方，也不让 wait 当成结束
    else:
        out(end)
    if will_resume:
        # 瞬时 API 错误 / token 到期：新进程续接即可
        # 服务端过载(503/容量不足/高流量)：agy 自己已经重试了几分钟，马上再来多半还是失败，按服务端提示等一分钟
        busy = bool(re.search(r"\b503\b|capacity|high traffic|UNAVAILABLE|try again in", err, re.I))
        time.sleep(60 if busy else 5)
        a2 = argparse.Namespace(**vars(a))
        a2.resume, a2.prompt_file, a2.auto_resume = cid, None, a.auto_resume - 1
        a2.rid, a2.detach = rid2, False
        a2.auth_retried = getattr(a, "auth_retried", False) or verdict == "ERROR_AUTH"
        a2.name = (a.name or "run") + "-r"
        a2.timeout = a.timeout
        a2.prompt = ("上一次运行被 API 错误中断（%s）。已有成果都还在，不要重做。先盘点任务书要求的输出哪些已经完成，"
                     "只做没完成的部分；全部完成后按任务书规定的格式汇报。%s" % (
                         short(err, 80), ("尚缺: " + ", ".join(Path(m).name for m in missing[:20])) if missing else ""))
        out("WARN AUTORESUME %s → 用新进程续接 conversation=%s（新运行 %s，wait 会自动跟过去；剩余自动续接次数 %d）" % (verdict, cid, rid2, a2.auto_resume))
        return cmd_run(a2)
    return rc


# ---------- 查看与管理 ----------
def live_state(meta):
    """state=running 的记录，运行器进程还在才算 RUNNING；不在了就是 ORPHANED（v0.4 的记录没有 runner_pid，退而看 agy 的 pid）。"""
    if meta.get("state") != "running":
        return meta.get("state") or "?"
    pid = meta.get("runner_pid") or meta.get("pid")
    return "RUNNING" if pid_alive(pid) else "ORPHANED"


def cmd_watch(a):
    """给人看的实时视图：跟随 digest.log，运行结束后自动退出。"""
    d = run_dir(a.run)
    print("== 跟随 %s ==" % d, flush=True)
    f, idle = None, 0
    while True:
        if f is None and (d / "digest.log").exists():
            f = open(d / "digest.log", encoding="utf-8")
        line = f.readline() if f else ""
        if line:
            print(line.rstrip("\n"), flush=True); idle = 0; continue
        time.sleep(0.5); idle += 1
        if idle > 4 and live_state(read_meta(d)) not in ("RUNNING", "starting"):
            break
    print("== 运行已结束 %s ==" % time.strftime("%H:%M:%S"))
    return 0


def cmd_status(a):
    d = run_dir(a.run)
    meta = read_meta(d)
    print("RUN %s state=%s verdict=%s rc=%s pid=%s conversation=%s cwd=%s" % (
        meta.get("run_id"), live_state(meta), meta.get("verdict"), meta.get("rc"), meta.get("pid"), meta.get("conversation_id"), meta.get("cwd")))
    lines = (d / "digest.log").read_text(encoding="utf-8").splitlines() if (d / "digest.log").exists() else []
    for l in lines[-a.lines:]:
        print(short(l, 260))
    return 0


def cmd_list(a):
    cutoff = time.strftime("%F %T", time.localtime(time.time() - a.since * 86400))
    rows = []
    for p in RUNS.glob("*"):
        m = read_meta(p)
        if not m or m.get("started", "") < cutoff:
            continue
        s = live_state(m)
        if a.running and s != "RUNNING":
            continue
        rows.append((m.get("started", ""), p.name, s, m.get("verdict") or "-", m.get("rc", "-"), (m.get("usage") or {}).get("total_tokens", 0) // 1000))
    for r in sorted(rows)[-a.limit:]:
        print("%-34s %-9s %-18s rc=%-2s tok=%dk" % (r[1], r[2], r[3], r[4], r[5]))
    print("共 %d 个（%s 以来）" % (len(rows), cutoff[:10]))
    return 0


def cmd_result(a):
    d = run_dir(a.run)
    f = d / "result.json"
    if not f.exists():
        print("没有 result.json（v0.4 的运行或还没结束）；用 status 看摘要"); return 1
    r = json.loads(f.read_text(encoding="utf-8"))
    rep = r.pop("report", {})
    print(json.dumps(r, ensure_ascii=False, indent=1))
    print("---- 汇报全文 (%d 字) ----" % rep.get("chars", 0))
    print(rep.get("text", ""))
    return 0


def cmd_kill(a):
    d = run_dir(a.run)
    meta = read_meta(d)
    rid = meta.get("run_id") or d.name
    if live_state(meta) != "RUNNING":
        if meta.get("state") == "running":
            atomic_write(d / "meta.json", json.dumps({**meta, "state": "orphaned", "ended": time.strftime("%F %T")}, ensure_ascii=False, indent=1))
            print("运行 %s 的运行器已不在（记录标为 orphaned）。agy 随 job 被系统终止；没有按 pid 动任何进程，因为它可能已被别的进程复用" % rid)
        else:
            print("运行 %s 不在运行中 (state=%s)" % (rid, meta.get("state")))
        return 0
    (d / "KILL").write_text(time.strftime("%F %T"), encoding="utf-8")   # 让运行器把结果记成 KILLED_MANUAL，而不是看起来像崩溃的 NO_RESULT
    ok = job_kill_by_name(rid)
    if ok is None:     # v0.4 的运行没有 job：退回按 pid 杀整棵树
        r = subprocess.run(["taskkill", "/PID", str(meta["pid"]), "/T", "/F"], capture_output=True, creationflags=NOWIN)
        print("KILLED run=%s pid=%s rc=%s conversation=%s" % (rid, meta["pid"], r.returncode, meta.get("conversation_id")))
    else:
        print("KILLED run=%s via job ok=%s conversation=%s" % (rid, ok, meta.get("conversation_id")))
    return 0


def cmd_wait(a):
    """阻塞等待一个运行的下一批事件，给没有"输出行唤醒"能力的主代理用（有 Monitor 之类工具的代理不需要它）。
    只显示上次 wait 之后新出现的 ALERT / WARN / END 行（游标存在运行目录的 wait.cursor）。
    运行如果自动续接成了新运行，会顺着链接自动去等新运行（输出一行 CHAIN），调用方不用关心。
    返回码：END 出现 -> 运行本身的返回码(0/2/3/4/5)；有新告警但运行还在继续 -> 10；到点没有新事件、运行仍在跑 -> 11；
    运行器已不在且没有 END（崩溃或被强杀）-> 12。--until end 时告警只显示、不返回，一直等到 END。"""
    d = run_dir(a.run)
    end_at = time.time() + a.timeout
    chain = 0

    def load_pos(dd):
        try:
            return int((dd / "wait.cursor").read_text())
        except Exception:
            return 0
    pos = load_pos(d)
    while True:
        lines, ended = [], False
        f = d / "digest.log"
        if f.exists():
            data = f.read_bytes()
            if pos > len(data):
                pos = 0
            chunk = data[pos:]
            nl = chunk.rfind(b"\n")
            if nl >= 0:
                pos += nl + 1
                for l in chunk[:nl + 1].decode("utf-8", "replace").splitlines():
                    if re.search(r"\] (ALERT|WARN|END) ", l) and "AUTORESUME" not in l:   # 续接由 CHAIN 行说明，不重复推
                        lines.append(l)
                        ended = ended or bool(re.search(r"\] END ", l))
        if lines:
            for l in lines:
                print(l, flush=True)
            (d / "wait.cursor").write_text(str(pos))
        meta = read_meta(d)
        if ended:
            return int(meta.get("rc") or 0)
        if lines and a.until == "alert":
            return 10
        if not meta and time.time() < end_at:       # 后台刚启动 / 续接的新运行还没写出记录
            time.sleep(1)
            continue
        state = live_state(meta)
        nxt = meta.get("resumed_as") if state == "finished" else None
        if nxt and chain < 5:                       # 这个运行已自动续接：顺着链接去等新运行
            print("CHAIN %s -> %s（自动续接）" % (d.name, nxt), flush=True)
            d = RUNS / nxt
            pos = load_pos(d)
            chain += 1
            continue
        if state not in ("RUNNING", "starting"):
            if state == "finished":
                print("WAIT_FINISHED run=%s verdict=%s（事件已全部取走）" % (d.name, meta.get("verdict")), flush=True)
                return int(meta.get("rc") or 0)
            print("WAIT_ORPHANED run=%s state=%s（运行器已不在且没有 END；用 status/result 看现场）" % (d.name, state), flush=True)
            return 12
        if time.time() >= end_at:
            print("WAIT_TIMEOUT run=%s 仍在运行，%ds 内没有新事件" % (d.name, a.timeout), flush=True)
            return 11
        time.sleep(2)


def cmd_selftest(a):
    """给新接入的主代理：确认环境能用。不加 --live 是零 token 的检查；--live 会在临时目录跑一个最小的真实任务（花一点点额度）。"""
    ok = True

    def line(good, what, detail=""):
        nonlocal ok
        ok = ok and good
        print("%s %s%s" % ("OK  " if good else "FAIL", what, "  " + detail if detail else ""))
    line(Path(AGY).exists(), "找到 agy", str(AGY))
    v = agy_version()
    line(v != "?", "agy 版本 " + v + ("" if v in TESTED else "（不在已测列表：行为可能变化，结果多核对）"))
    try:
        RUNS.mkdir(parents=True, exist_ok=True)
        t = RUNS / ".selftest"
        t.write_text("x"); t.unlink()
        line(True, "运行记录目录可写", str(RUNS))
    except Exception as e:
        line(False, "运行记录目录可写", short(str(e), 160))
    try:
        line(True, "额度可读（零 token）", quota_line(get_quota()))
    except Exception as e:
        line(False, "额度可读", short(str(e), 160))
    line(bool(_K32), "Job Object 可用（运行器被杀时系统自动带走 agy）")
    print("INFO %s" % ("已配置主动通知命令" if (a.notify_cmd or os.environ.get("AGY_NOTIFY_CMD")) else "没有配置 --notify-cmd / AGY_NOTIFY_CMD：额度、登录告警只能靠调用方自己转告用户"))
    if a.live and a.detach:
        # 给"单次命令有时间上限"的调用方：最小真实任务在后台跑，立刻返回，之后用 wait 取结果（通过的标志是 verdict=DONE_UNVERIFIED）
        wd = RUNS / "_selftest_work"
        shutil.rmtree(wd, ignore_errors=True)
        wd.mkdir(parents=True)
        p = subprocess.run([sys.executable, str(Path(__file__).resolve()), "run", "--cwd", str(wd), "--prompt", "只回复一个字：好",
                            "--name", "selftest", "--throwaway", "--timeout", "5m", "--detach"], capture_output=True, creationflags=NOWIN)
        outp = p.stdout.decode("utf-8", "replace")
        started = [l for l in outp.splitlines() if l.startswith("STARTED")]
        line(p.returncode == 0 and bool(started), "已后台启动最小真实任务", short(started[0] if started else outp[-200:], 200))
        print("INFO 之后用 `wait <运行名> --until end --timeout <小于你单次命令上限的秒数>` 反复等到 END（返回码 11 就再调一次）；"
              "END 里 verdict=DONE_UNVERIFIED 才算自检通过。agy 启动时间取决于它配置的 MCP 服务器，通常 40 秒到 2 分钟")
    elif a.live:
        with tempfile.TemporaryDirectory(prefix="agy-selftest-") as td:
            p = subprocess.run([sys.executable, str(Path(__file__).resolve()), "run", "--cwd", td, "--prompt", "只回复一个字：好",
                                "--name", "selftest", "--throwaway", "--timeout", "5m"], capture_output=True, creationflags=NOWIN)
            outp = p.stdout.decode("utf-8", "replace")
            end = [l for l in outp.splitlines() if "END " in l]
            line(p.returncode == 0 and any("verdict=DONE_UNVERIFIED" in l for l in end), "真实小任务端到端", short(end[-1] if end else outp[-200:], 200))
    print("SELFTEST " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="c", required=True)
    q = sub.add_parser("quota", help="查看剩余额度(零 token)")
    q.add_argument("--model", default=""); q.add_argument("--min-quota", type=float, default=0.05)
    r = sub.add_parser("run", help="派发一个任务并监控到结束")
    r.add_argument("--cwd", required=True)
    r.add_argument("--prompt"); r.add_argument("--prompt-file")
    r.add_argument("--resume", help="续接的 conversation id")
    r.add_argument("--name", help="运行名后缀，便于辨认")
    r.add_argument("--plan", action="store_true", help="只读规划模式，不改文件")
    r.add_argument("--model", default="gemini-3.8-flash-high"); r.add_argument("--effort")
    r.add_argument("--timeout", default="30m", help="5400 / 90s / 30m / 2h / 1h30m；裸数字按秒")
    r.add_argument("--stall", type=int, default=0, help="STALL 首次告警的下限秒数（兼容旧参数：实际阈值取它与 --stall-think/--stall-cmd 中较大者）")
    r.add_argument("--stall-think", type=int, default=420, help="模型思考中无事件多少秒首次告警")
    r.add_argument("--stall-cmd", type=int, default=600, help="命令执行中无事件多少秒首次告警")
    r.add_argument("--think-kill", type=int, default=900, help="模型思考中无事件多少秒终止(agy 自带 5 分钟取消重试)")
    r.add_argument("--cmd-kill", type=int, default=3600, help="命令无事件多少秒无条件终止")
    r.add_argument("--cmd-quiet", type=int, default=600, help="命令已静默超过该秒数、且进程树这么久 CPU/IO 零增长，判定挂死并终止；0 关闭")
    r.add_argument("--probe-every", type=int, default=15, help="命令执行中每隔多少秒取一次进程树 CPU/IO")
    r.add_argument("--stall-kill", type=int, default=0, help="连续 N 次 STALL 后自动杀进程，0 为不按次数杀")
    r.add_argument("--idle-kill", type=int, default=None, help="多少秒无任何事件就自动终止；给了就同时覆盖 --think-kill 和 --cmd-kill，0 为不杀")
    r.add_argument("--loop-kill", type=int, default=1, help="死循环/空转自动终止，0 为只告警不终止")
    r.add_argument("--progress-window", type=int, default=480, help="空转判定窗口(秒)，0 为关闭")
    r.add_argument("--heartbeat", type=int, default=600)
    r.add_argument("--min-quota", type=float, default=0.05)
    r.add_argument("--no-quota-check", action="store_true")
    r.add_argument("--verbose", action="store_true", help="把每条命令也推给调用方(默认只写 digest.log)")
    r.add_argument("--stdout", choices=("act", "all"), default="act", help="act: 只推 ALERT/WARN 和结尾的 END；all: 全部行（START/INIT/RESULT/FINAL/HEARTBEAT…）")
    r.add_argument("--final-chars", type=int, default=1500)
    r.add_argument("--expect", action="append", default=[], help="任务应当写出的文件路径（可重复）；任务书表格里反引号包着的输出路径也会自动识别")
    r.add_argument("--no-auto-expect", action="store_true", help="不要从任务书表格里自动识别输出清单")
    r.add_argument("--auto-resume", type=int, default=1, help="瞬时 API 错误/登录到期时自动用新进程续接的次数，0 关闭")
    r.add_argument("--throwaway", action="store_true", help="一次性运行(临时目录、试验)：结束后删掉本次新建的项目和会话，之后不能续接")
    r.add_argument("--notify-cmd", default=os.environ.get("AGY_NOTIFY_CMD"),
                   help="额度/登录告警时由运行器自己执行的命令（经 shell；环境变量 AGY_ALERT_KIND / AGY_ALERT_MSG / AGY_RUN_ID / AGY_RUN_DIR），也可用环境变量 AGY_NOTIFY_CMD")
    r.add_argument("--detach", action="store_true", help="后台启动：立刻返回 run id，之后用 wait 取事件（给没有后台进程能力的主代理用）")
    r.add_argument("--rid", help=argparse.SUPPRESS)
    g = sub.add_parser("gc", help="清理没有会话的空项目(目录不存在或重复)")
    g.add_argument("--dry-run", action="store_true"); g.set_defaults(fn=cmd_gc)
    for name, fn in (("watch", cmd_watch), ("status", cmd_status), ("result", cmd_result), ("kill", cmd_kill)):
        s = sub.add_parser(name); s.add_argument("run", nargs="?", default="latest"); s.set_defaults(fn=fn)
        if name == "status":
            s.add_argument("--lines", type=int, default=15)
    ls = sub.add_parser("list", help="列出运行（RUNNING / ORPHANED / finished）")
    ls.add_argument("--running", action="store_true"); ls.add_argument("--since", type=float, default=3, help="最近几天")
    ls.add_argument("--limit", type=int, default=40); ls.set_defaults(fn=cmd_list)
    w = sub.add_parser("wait", help="阻塞等待一个运行的下一批 ALERT/WARN/END（返回码见文档）")
    w.add_argument("run", nargs="?", default="latest"); w.add_argument("--timeout", type=int, default=300, help="最多等多少秒")
    w.add_argument("--until", choices=("alert", "end"), default="alert", help="alert: 有新告警就返回；end: 一直等到结束")
    w.set_defaults(fn=cmd_wait)
    st_ = sub.add_parser("selftest", help="检查环境能否使用本运行器；--live 再跑一个最小真实任务")
    st_.add_argument("--live", action="store_true"); st_.add_argument("--detach", action="store_true", help="配合 --live：最小真实任务放后台，立刻返回，用 wait 取结果")
    st_.add_argument("--notify-cmd", default=os.environ.get("AGY_NOTIFY_CMD")); st_.set_defaults(fn=cmd_selftest)
    q.set_defaults(fn=cmd_quota); r.set_defaults(fn=cmd_run)
    a = ap.parse_args()
    sys.exit(a.fn(a))


if __name__ == "__main__":
    main()
