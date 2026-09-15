#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ocp - opencode 全局会话选择器.

列出所有目录的历史会话, 选择后 cd 到会话目录并启动 opencode --session <id>.

用法:
  ocp                 交互式菜单
  ocp list [关键字]   仅打印列表, 不启动
  ocp go <id前缀>     直接按会话 ID 前缀启动
"""
import datetime
import os
import shutil
import sqlite3
import subprocess
import sys

DB = os.environ.get(
    "OPENCODE_DB",
    os.path.join(os.path.expanduser("~"), ".local", "share", "opencode", "opencode.db"),
)
QUERY = """
select s.id, s.title, s.directory, s.time_updated, s.agent, s.model,
       (select count(*) from message m where m.session_id = s.id) as msgs
  from session s
 where s.parent_id is null
 order by s.time_updated desc
"""


def load():
    if not os.path.isfile(DB):
        sys.exit(f"[x] 找不到 opencode 数据库: {DB}")
    con = sqlite3.connect(f"file:{DB.replace(os.sep, '/')}?mode=ro", uri=True)
    rows = con.execute(QUERY).fetchall()
    con.close()
    return rows


def ts(ms):
    if not ms:
        return "-"
    return datetime.datetime.fromtimestamp(ms / 1000).strftime("%m-%d %H:%M")


def render(rows):
    if not rows:
        print("(无匹配会话)")
        return
    print(f"{'#':>3}  {'更新时间':<12} {'消息':>4}  {'标题'}")
    for i, r in enumerate(rows, 1):
        title = (r[1] or "").strip().replace("\n", " ")
        if len(title) > 46:
            title = title[:45] + "…"
        proj = (r[2] or "").replace("\\", "/").rstrip("/").split("/")[-1]
        print(f"{i:>3}  {ts(r[3]):<12} {r[6]:>4}  {title}")
        print(f"{'':19}目录: {r[2]}   项目: {proj}")


def find_opencode():
    base = shutil.which("opencode.cmd") or shutil.which("opencode.ps1") or shutil.which("opencode")
    if base:
        exe = os.path.join(os.path.dirname(base), "node_modules", "opencode-ai", "bin", "opencode.exe")
        if os.path.isfile(exe):
            return exe
        if base.endswith(".cmd"):
            return base
    return None


def launch(row):
    sid, directory = row[0], row[2]
    d = directory.replace("/", os.sep)
    print(f"[>] 会话 {sid}  目录 {d}")
    if not os.path.isdir(d):
        ans = input(f"[!] 目录不存在（可能该项目尚未在本机 clone/pull）。仍要创建并继续? [y/N] ").strip().lower()
        if ans != "y":
            return
        os.makedirs(d, exist_ok=True)
    exe = find_opencode()
    if not exe:
        sys.exit("[x] 找不到 opencode 可执行文件")
    if exe.endswith(".cmd"):
        subprocess.call(f'"{exe}" --session {sid}', cwd=d, shell=True)
    else:
        subprocess.call([exe, "--session", sid], cwd=d)


def match(rows, kw):
    kw = kw.lower()
    return [r for r in rows if kw in " ".join(map(str, r)).lower()]


def interactive():
    try:
        rows = load()
    except sqlite3.OperationalError as e:
        sys.exit(f"[x] 读取数据库失败(可能 opencode 正以独占方式运行): {e}")
    shown = rows
    print(f"共 {len(rows)} 个顶层会话。输入序号启动; 输入关键字过滤; /all 回到全部; q 退出")
    while True:
        render(shown)
        try:
            ans = input("ocp> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not ans:
            continue
        if ans.lower() in ("q", "quit", "exit"):
            return
        if ans in ("/all", "all"):
            shown = rows
            continue
        if ans.isdigit():
            n = int(ans)
            if 1 <= n <= len(shown):
                launch(shown[n - 1])
                return
            print(f"[x] 序号超出范围 1-{len(shown)}")
            continue
        if ans.startswith("/"):
            ans = ans[1:]
        filtered = match(rows, ans)
        if not filtered:
            print(f"[x] 无匹配: {ans}")
            continue
        shown = filtered


def main():
    args = sys.argv[1:]
    rows = load()
    if not args or args[0] in ("list", "ls"):
        kw = args[1] if len(args) > 1 else None
        render(match(rows, kw) if kw else rows)
        return
    if args[0] == "go":
        if len(args) < 2:
            sys.exit("用法: ocp go <session-id前缀>")
        hits = [r for r in rows if r[0].startswith(args[1])]
        if len(hits) != 1:
            sys.exit(f"[x] 前缀 '{args[1]}' 匹配 {len(hits)} 个会话, 请更精确")
        launch(hits[0])
        return
    sys.exit(f"[x] 未知命令: {args[0]}")


if __name__ == "__main__":
    if sys.stdin and sys.stdin.isatty() and len(sys.argv) == 1:
        interactive()
    elif len(sys.argv) == 1:
        main()
    else:
        main()
