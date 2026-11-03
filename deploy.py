"""把本工程的 skill/ 部署到线上技能目录 ~/.claude/skills/agy-delegate/，以及对比、回退。

用法：
  python deploy.py status                 对比源码和线上，列出一致 / 不同 / 缺失 / 线上多出的文件
  python deploy.py deploy [--dry-run]     部署：先备份线上的当前版本到 history/<版本>/，原子替换，校验，再跑零 token 自检
  python deploy.py rollback <版本>        把 history/<版本>/ 还原到线上（还原前先备份当前线上版本）

规矩：只改本工程的 skill/，不要直接改线上目录里的文件。线上有文件比源码新（有人直接改了线上）时，deploy 会拒绝覆盖，
先用 status 看差异、把改动合回 skill/，确实要覆盖才加 --force。
"""
import argparse, hashlib, os, pathlib, py_compile, re, shutil, subprocess, sys, time

HERE = pathlib.Path(__file__).resolve().parent
SRC = HERE / "skill"
HIST = HERE / "history"
LIVE = pathlib.Path(os.environ.get("AGY_DELEGATE_LIVE") or (pathlib.Path.home() / ".claude" / "skills" / "agy-delegate"))   # AGY_DELEGATE_LIVE：测试时指到临时目录
NOWIN = 0x08000000
# 部署的文件清单（相对路径）。__pycache__、*.bak-*、测试运行时生成的目录都不算
FILES = ["agy_run.py", "GUIDE.md", "SKILL.md", "adapters/claude-code.md", "adapters/_template.md",
         "tests/fake_agy.py", "tests/fake_agy.cmd", "tests/tests.py"]
EXTRA_OK = re.compile(r"(__pycache__|\.bak-|\.new$|^tests/(home|work|custom_runs|pids_|notified))")
# adapters/ 里新代理自己补的适配层也属于部署内容
def all_files(base):
    out = set(FILES)
    ad = base / "adapters"
    if ad.exists():
        out |= {"adapters/" + p.name for p in ad.glob("*.md")}
    return sorted(out)


def md5(p):
    return hashlib.md5(pathlib.Path(p).read_bytes()).hexdigest()[:8]


def version_of(agy_run):
    try:
        m = re.search(r"运行器 (v\d+\.\d+)", pathlib.Path(agy_run).read_text(encoding="utf-8")[:600])
        return m.group(1) if m else None
    except Exception:
        return None


def compare():
    rows = []
    names = sorted(set(all_files(SRC)) | set(all_files(LIVE)))
    for rel in names:
        s, l = SRC / rel, LIVE / rel
        if s.exists() and l.exists():
            if md5(s) == md5(l):
                rows.append((rel, "same", ""))
            else:
                newer = "线上更新" if l.stat().st_mtime > s.stat().st_mtime else "源码更新"
                rows.append((rel, "differs", newer))
        elif s.exists():
            rows.append((rel, "only-src", "线上缺失"))
        else:
            rows.append((rel, "only-live", "线上多出（源码里没有）"))
    return rows


def cmd_status(a):
    rows = compare()
    for rel, st, note in rows:
        print("%-9s %-34s %s" % ({"same": "一致", "differs": "不同", "only-src": "仅源码", "only-live": "仅线上"}[st], rel, note))
    bad = [r for r in rows if r[1] != "same"]
    print("源码版本 %s | 线上版本 %s | %s" % (version_of(SRC / "agy_run.py"), version_of(LIVE / "agy_run.py"), "完全一致" if not bad else "有 %d 处不同" % len(bad)))
    return 0 if not bad else 1


def backup_live(tag):
    d = HIST / tag
    if d.exists():
        d = HIST / ("%s-%s" % (tag, time.strftime("%m%d%H%M%S")))
    for rel in all_files(LIVE):
        if (LIVE / rel).exists():
            (d / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(LIVE / rel, d / rel)
    return d


def put(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = pathlib.Path(str(dst) + ".new")
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)          # 原子替换：别人正好在读时不会读到半截


def selftest():
    p = subprocess.run([sys.executable, "-I", str(LIVE / "agy_run.py"), "selftest"], capture_output=True, creationflags=NOWIN, timeout=120)
    out = p.stdout.decode("utf-8", "replace").strip().splitlines()
    return p.returncode == 0, out[-1] if out else ""


def cmd_deploy(a):
    py_compile.compile(str(SRC / "agy_run.py"), doraise=True)
    print("源码语法检查通过")
    rows = compare()
    drift = [r for r in rows if r[1] == "differs" and r[2] == "线上更新"]
    extra = [r for r in rows if r[1] == "only-live" and not EXTRA_OK.search(r[0])]
    if (drift or extra) and not a.force:
        print("拒绝部署：线上有源码里没有的改动（可能有人直接改了线上目录），覆盖会丢掉它们：")
        for r in drift + extra:
            print("  ", r[0], r[2])
        print("先把这些改动合回 skill/，确实要覆盖再加 --force")
        return 2
    todo = [r for r in rows if r[1] in ("differs", "only-src")]
    if not todo:
        print("线上已经是最新，没有需要部署的文件"); return 0
    print("将部署 %d 个文件: %s" % (len(todo), ", ".join(r[0] for r in todo)))
    if a.dry_run:
        print("--dry-run：不做任何改动"); return 0
    tag = version_of(LIVE / "agy_run.py") or "unknown"
    d = backup_live(tag)
    print("已备份线上当前版本 ->", d.relative_to(HERE))
    for rel, st, _ in todo:
        put(SRC / rel, LIVE / rel)
    ok = all(md5(SRC / rel) == md5(LIVE / rel) for rel, *_ in todo)
    print("部署后逐个校验:", "一致" if ok else "不一致!")
    py_compile.compile(str(LIVE / "agy_run.py"), doraise=True)
    good, last = selftest()
    print("线上 selftest:", "PASS" if good else "FAIL", "|", last)
    print("线上版本:", version_of(LIVE / "agy_run.py"))
    return 0 if ok and good else 1


def cmd_rollback(a):
    src = HIST / a.version
    if not src.exists():
        print("history 里没有", a.version, "；有:", sorted(p.name for p in HIST.iterdir())); return 2
    cur = version_of(LIVE / "agy_run.py") or "unknown"
    d = backup_live("pre-rollback-" + cur)
    print("还原前已备份线上当前版本 ->", d.relative_to(HERE))
    for p in sorted(x for x in src.rglob("*") if x.is_file()):
        put(p, LIVE / p.relative_to(src))
    good, last = selftest()
    print("已还原到 %s；线上 selftest: %s | %s" % (a.version, "PASS" if good else "FAIL", last))
    print("注意：本工程 skill/ 里仍是较新的源码；要让源码也回到 %s，请手工处理" % a.version)
    return 0 if good else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="c", required=True)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    d = sub.add_parser("deploy"); d.add_argument("--dry-run", action="store_true"); d.add_argument("--force", action="store_true"); d.set_defaults(fn=cmd_deploy)
    r = sub.add_parser("rollback"); r.add_argument("version"); r.set_defaults(fn=cmd_rollback)
    a = ap.parse_args()
    sys.exit(a.fn(a))


if __name__ == "__main__":
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8", errors="replace")
    main()
