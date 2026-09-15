#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ocp-gui - opencode 会话与共享目录集中管理器 (Tkinter).

Tab1 会话管理: 跨目录查看/搜索全部历史会话, 一键进入、定位、删除.
Tab2 共享同步: Syncthing 目录状态/立即同步/暂停恢复/忽略规则编辑, 切换助手(oc-out/oc-in).
Tab3 目录浏览: 浏览共享目录内容, 高亮同步冲突/临时文件.

用法:
  python ocp-gui.py            启动界面
  python ocp-gui.py selftest   无界面自检(级联删除在内存副本库验证 + REST 连通)
"""
import datetime as dt
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

HOME = os.path.expanduser("~")
DATA_DIR = os.path.join(HOME, ".local", "share", "opencode")
DB = os.environ.get("OPENCODE_DB", os.path.join(DATA_DIR, "opencode.db"))
MARKER = os.path.join(DATA_DIR, ".opencode-active-host")
ST_CONFIG = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Syncthing", "config.xml")
ST_TIMEOUT = 120
CREATE_NEW_CONSOLE = 0x00000010
CREATE_NO_WINDOW = 0x08000000

SESSION_QUERY = """
select s.id, s.title, s.directory, s.time_updated,
       (select count(*) from message m where m.session_id = s.id) as msgs
  from session s
 where s.parent_id is null
 order by s.time_updated desc
"""


# ---------------- Syncthing REST ----------------

def _q(s):
    return urllib.parse.quote(s, safe="")


class Syncthing:
    def __init__(self):
        self.base = None
        self.key = None
        self.err = None
        if not os.path.isfile(ST_CONFIG):
            self.err = "未找到 Syncthing 配置(未安装或未初始化)"
            return
        try:
            gui = ET.parse(ST_CONFIG).getroot().find("gui")
            addr = gui.findtext("address") or "127.0.0.1:8384"
            if not addr.startswith(("127.", "localhost")):
                addr = "127.0.0.1" + addr[addr.rindex(":"):]
            self.base = "http://" + addr
            self.key = gui.findtext("apikey")
        except Exception as e:
            self.err = f"解析 config.xml 失败: {e}"

    def req(self, path, method="GET", body=None, timeout=10):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        r = urllib.request.Request(self.base + path, data=data, method=method,
                                   headers={"X-API-Key": self.key,
                                            "Content-Type": "application/json"})
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else None

    def alive(self):
        if self.err:
            return False
        try:
            self.req("/rest/system/status", timeout=3)
            return True
        except Exception:
            return False

    def my_id(self):
        return (self.req("/rest/system/status") or {}).get("myID", "")

    def folders(self):
        return self.req("/rest/config/folders") or []

    def folder_status(self, fid):
        return self.req("/rest/db/status?folder=" + _q(fid)) or {}

    def scan(self, fid):
        self.req("/rest/db/scan?folder=" + _q(fid), method="POST")

    def set_paused(self, fid, paused):
        f = self.req("/rest/config/folders/" + _q(fid))
        f["paused"] = bool(paused)
        self.req("/rest/config/folders/" + _q(fid), method="PUT", body=f)

    def get_ignores(self, fid):
        return (self.req("/rest/db/ignores?folder=" + _q(fid)) or {}).get("ignore") or []

    def set_ignores(self, fid, lines):
        self.req("/rest/db/ignores?folder=" + _q(fid), method="POST", body={"ignore": lines})

    def device_names(self):
        return {d.get("deviceID"): (d.get("name") or d.get("deviceID", "")[:7])
                for d in (self.req("/rest/config/devices") or [])}

    def connections(self):
        cs = (self.req("/rest/system/connections") or {}).get("connections") or {}
        return {k: v for k, v in cs.items() if isinstance(v, dict) and v.get("connected")}


# ---------------- sqlite ----------------

def db_ro():
    return sqlite3.connect(f"file:{DB.replace(os.sep, '/')}?mode=ro", uri=True)


def list_sessions():
    con = db_ro()
    try:
        return con.execute(SESSION_QUERY).fetchall()
    finally:
        con.close()


def session_closure(root_ids):
    con = db_ro()
    try:
        todo, allids = list(root_ids), set(root_ids)
        while todo:
            ph = ",".join("?" * len(todo))
            kids = [r[0] for r in con.execute(
                f"select id from session where parent_id in ({ph})", todo)]
            todo = [k for k in kids if k not in allids]
            allids.update(todo)
        return allids
    finally:
        con.close()


def related_counts(ids):
    ids = list(ids)
    if not ids:
        return {}
    ph = ",".join("?" * len(ids))
    con = db_ro()
    try:
        return {
            "session": len(ids),
            "message": con.execute(f"select count(*) from message where session_id in ({ph})", ids).fetchone()[0],
            "part": con.execute(f"select count(*) from part where session_id in ({ph})", ids).fetchone()[0],
            "event": con.execute(f"select count(*) from event where aggregate_id in ({ph})", ids).fetchone()[0],
        }
    finally:
        con.close()


def delete_sessions(root_ids):
    ids = list(session_closure(root_ids))
    if not ids:
        return []
    ph = ",".join("?" * len(ids))
    con = sqlite3.connect(DB, timeout=10, isolation_level=None)
    try:
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("BEGIN IMMEDIATE")
        con.execute(f"delete from event where aggregate_id in ({ph})", ids)
        con.execute(f"delete from event_sequence where aggregate_id in ({ph})", ids)
        con.execute(f"delete from session where id in ({ph})", ids)
        con.execute("COMMIT")
        return ids
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.close()


def wal_checkpoint():
    con = sqlite3.connect(DB, timeout=10)
    try:
        r = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        con.commit()
        return r
    finally:
        con.close()


def opencode_running():
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq opencode.exe", "/NH"],
                             capture_output=True, timeout=10,
                             creationflags=CREATE_NO_WINDOW).stdout.decode("gbk", "replace")
        return "opencode.exe" in out.lower()
    except Exception:
        return False


def find_opencode_exe():
    base = None
    for name in ("opencode.cmd", "opencode.ps1", "opencode"):
        base = shutil.which(name)
        if base:
            break
    if not base:
        return None
    exe = os.path.join(os.path.dirname(base), "node_modules", "opencode-ai", "bin", "opencode.exe")
    return exe if os.path.isfile(exe) else base


def human_time(ms):
    if not ms:
        return "-"
    return dt.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")


def human_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024


def write_marker(host):
    with open(MARKER, "w", encoding="utf-8") as f:
        f.write(f"{host}\t{dt.datetime.now():%Y-%m-%d %H:%M:%S}\n")


def read_marker():
    try:
        with open(MARKER, encoding="utf-8") as f:
            return f.readline().split("\t")[0].strip()
    except Exception:
        return ""


# ---------------- GUI ----------------

import re as _re
import time as _time

import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox, ttk

try:
    import ttkbootstrap as tb
    HAS_TB = True
except Exception:
    tb = None
    HAS_TB = False

app_ref = None
UI_FONT = "Microsoft YaHei UI"
RECENT_MS = 5 * 60 * 1000

# 调色板(先浅色回退值, setup_style 可能整体改写为深色)
BG = "#f5f6f8"
SIDEBAR = "#e8ebf0"
CARD = "#ffffff"
FG = "#1f2328"
MUT = "#57606a"
ACCENT = "#2f6fed"
ACCENT_DARK = "#2a5fd2"
SUCCESS = "#1a9c6b"
WARN = "#e8a33d"
DANGER = "#d64545"
SEL = "#dbe7ff"
ODD = "#fafbfc"
EVEN = "#ffffff"
GRID = "#dfe3e8"
MENU_BG = "#ffffff"
RUN_BG = "#e0f5ea"
ACT_BG = "#fdf2d9"
C_SUCCESS = "#1a7f4e"
C_WARNING = "#a86800"
C_DANGER = "#b02b2b"


def _shift(hexcol, amt):
    h = hexcol.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    f = lambda v: max(0, min(255, int(v + amt)))
    return "#{:02x}{:02x}{:02x}".format(f(r), f(g), f(b))


def _f(name, **kw):
    try:
        tkfont.nametofont(name).configure(family=UI_FONT, **kw)
    except tk.TclError:
        pass


def setup_style(app):
    global BG, SIDEBAR, CARD, FG, MUT, ACCENT, ACCENT_DARK, SUCCESS, WARN, DANGER
    global SEL, ODD, EVEN, GRID, MENU_BG, RUN_BG, ACT_BG, C_SUCCESS, C_WARNING, C_DANGER
    style = tb.Style(theme="darkly") if HAS_TB else ttk.Style(app)
    if HAS_TB:
        c = style.colors
        BG, CARD, FG = c.bg, _shift(c.bg, 8), c.fg
        SIDEBAR, GRID, MENU_BG = _shift(c.bg, -12), c.border, _shift(c.bg, 12)
        MUT = "#98a2ad"
        ACCENT, ACCENT_DARK = c.primary, _shift(c.primary, -16)
        SUCCESS, WARN, DANGER = c.success, c.warning, c.danger
        SEL, ODD, EVEN = _shift(c.primary, 45), _shift(c.bg, -6), c.bg
        RUN_BG, ACT_BG = "#14382c", "#3a2e12"
        C_SUCCESS, C_WARNING, C_DANGER = c.success, c.warning, c.danger
    else:
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", background=BG, foreground=FG)
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("Muted.TLabel", background=BG, foreground=MUT)
        style.configure("TButton", padding=(12, 6), background="#fbfcfe", foreground=FG,
                        bordercolor="#c9d1da", relief="flat", focusthickness=0)
        style.map("TButton", background=[("active", "#e8effc"), ("disabled", "#f0f1f3")],
                  foreground=[("disabled", "#9aa3ad")])
    for fname in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkIconFont",
                  "TkTooltipFont", "TkCaptionFont", "TkSmallCaptionFont"):
        _f(fname, size=10)
    _f("TkHeadingFont", size=10, weight="bold")
    app.configure(bg=BG)
    style.configure("Treeview", rowheight=32, borderwidth=0)
    style.configure("Treeview.Heading", padding=(10, 8))
    style.configure("TLabelframe", background=BG)
    style.configure("TLabelframe.Label", background=BG, foreground=MUT)
    return style


# 统一对话框: 用标准 tkinter.messagebox, 返回真正的 bool, 不受语言包影响;
# 挂到主窗口为父级并临时置顶, 保证弹出在最前(修复"点删除没反应")
def _mbox_parent():
    try:
        if app_ref is not None:
            app_ref.lift()
            app_ref.attributes("-topmost", True)
    except Exception:
        pass
    return app_ref


def _mbox_clear_topmost():
    try:
        if app_ref is not None:
            app_ref.attributes("-topmost", False)
    except Exception:
        pass


class Msg:
    @staticmethod
    def info(t, m):
        p = _mbox_parent()
        messagebox.showinfo(t, m, parent=p) if p else messagebox.showinfo(t, m)
        _mbox_clear_topmost()

    @staticmethod
    def warning(t, m):
        p = _mbox_parent()
        messagebox.showwarning(t, m, parent=p) if p else messagebox.showwarning(t, m)
        _mbox_clear_topmost()

    @staticmethod
    def error(t, m):
        p = _mbox_parent()
        messagebox.showerror(t, m, parent=p) if p else messagebox.showerror(t, m)
        _mbox_clear_topmost()

    @staticmethod
    def askyesno(t, m):
        p = _mbox_parent()
        r = messagebox.askyesno(t, m, parent=p) if p else messagebox.askyesno(t, m)
        _mbox_clear_topmost()
        return bool(r)


# 控件别名: 有 ttkbootstrap 用 bootstyle 语义色, 否则退回普通样式
if HAS_TB:
    L = tb.Label
    Fr = tb.Frame
    Ent = tb.Entry
    Combo = tb.Combobox

    def mkbtn(parent, text, cmd, kind=None):
        kw = {"bootstyle": kind} if kind else {}
        return tb.Button(parent, text=text, command=cmd, **kw)
else:

    L = ttk.Label
    Fr = ttk.Frame
    Ent = ttk.Entry
    Combo = ttk.Combobox

    _STYLE = {"primary": "Accent.TButton", "danger": "Danger.TButton",
              "success": "Success.TButton", None: "TButton"}

    def mkbtn(parent, text, cmd, kind=None):
        return ttk.Button(parent, text=text, command=cmd, style=_STYLE.get(kind, "TButton"))


def _menu_colors():
    return dict(bg=MENU_BG, fg=FG, activebackground=SEL, activeforeground=FG, borderwidth=1)


def _set_app_identity():
    """脱离 pythonw 默认分组, 让任务栏显示窗口自己的图标而不是 Python 图标."""
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("opencode.ocp-gui")
    except Exception:
        pass


def _app_icon_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.ico")


def run_bg(fn, on_ok=None, on_err=None):
    def wrap():
        try:
            r = fn()
            if on_ok and app_ref:
                app_ref.after(0, lambda: on_ok(r))
        except Exception as e:
            if app_ref:
                app_ref.after(0, lambda: (on_err(e) if on_err else messagebox.showerror("错误", str(e))))
    threading.Thread(target=wrap, daemon=True).start()


# ---------------- 会话活跃度检测 ----------------

_WMI_PS = ("Get-CimInstance Win32_Process -Filter \"Name='opencode.exe'\" | "
           "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress")


def running_sessions():
    """扫 opencode.exe 命令行里的 --session/-s ses_xxx -> {sid: [pid]}.
    直接敲 opencode 打开的情况命令行里没有 id, 由 5 分钟时间启发兜底."""
    out = {}
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", _WMI_PS],
                           capture_output=True, timeout=15,
                           creationflags=CREATE_NO_WINDOW)
        txt = r.stdout.decode("utf-8", "replace").strip()
        if not txt:
            return out
        data = json.loads(txt)
        if isinstance(data, dict):
            data = [data]
        for p in data:
            cl = p.get("CommandLine") or ""
            m = _re.search(r"(?:--session|-s)[= ]+(ses_[A-Za-z0-9]+)", cl)
            if m:
                out.setdefault(m.group(1), []).append(p.get("ProcessId"))
    except Exception:
        pass
    return out


_ACTIVITY_SQL = """
select coalesce(x.parent_id, x.id) as root, max(t.tm)
  from session x
  join (select session_id, max(time_created) as tm from part group by session_id
        union all
        select session_id, max(time_created) as tm from message group by session_id) t
    on t.session_id = x.id
 group by root
"""


def last_activity_map():
    """{顶层会话id: 最后活动毫秒}(含其子会话的消息/数据块)"""
    try:
        con = db_ro()
        try:
            return {r[0]: (r[1] or 0) for r in con.execute(_ACTIVITY_SQL)}
        finally:
            con.close()
    except Exception:
        return {}


def session_status_map(sids):
    """{sid: (显示文本, kind, pids)}  kind: run / act / idle"""
    run = running_sessions()
    act = last_activity_map()
    now = _time.time() * 1000
    out = {}
    for sid in sids:
        if sid in run:
            out[sid] = ("运行中", "run", run[sid])
        elif now - act.get(sid, 0) < RECENT_MS:
            out[sid] = ("近期活跃", "act", None)
        else:
            out[sid] = ("空闲", "idle", None)
    return out



TL = tb.Toplevel if HAS_TB else tk.Toplevel


class SessionsTab(Fr):
    def __init__(self, master):
        super().__init__(master, padding=(10, 8))
        bar = Fr(self)
        bar.pack(fill="x")
        L(bar, text="搜索:").pack(side="left")
        self.kw = tk.StringVar()
        self.kw.trace_add("write", lambda *a: self.refresh())
        Ent(bar, textvariable=self.kw, width=18).pack(side="left", padx=(4, 10))
        L(bar, text="目录:").pack(side="left")
        self.dirf = tk.StringVar(value="全部")
        self.dircb = Combo(bar, textvariable=self.dirf, state="readonly", width=24, values=["全部"])
        self.dircb.pack(side="left", padx=(4, 10))
        self.dircb.bind("<<ComboboxSelected>>", lambda e: self.refresh())
        L(bar, text="状态:").pack(side="left")
        self.statf = tk.StringVar(value="全部状态")
        self.statcb = Combo(bar, textvariable=self.statf, state="readonly", width=9,
                            values=["全部状态", "空闲", "近期活跃", "运行中"])
        self.statcb.pack(side="left", padx=(4, 10))
        self.statcb.bind("<<ComboboxSelected>>", lambda e: self.refresh())
        mkbtn(bar, "进入会话", self.open_session, "primary").pack(side="left", padx=2)
        mkbtn(bar, "打开目录", self.reveal_dir).pack(side="left", padx=2)
        mkbtn(bar, "删除会话", self.delete_session, "danger").pack(side="left", padx=2)
        cols = ("status", "time", "msgs", "title", "dir")
        self.tree = ttk.Treeview(self, columns=cols, show="headings", selectmode="extended")
        for c, w, txt, st in (("status", 84, "状态", False), ("time", 124, "更新时间", False),
                              ("msgs", 56, "消息", False), ("title", 320, "标题", True),
                              ("dir", 320, "目录", True)):
            self.tree.heading(c, text=txt)
            self.tree.column(c, width=w, anchor="w", stretch=st, minwidth=56 if not st else 160)
        self.tree.tag_configure("odd", background=ODD)
        self.tree.tag_configure("even", background=EVEN)
        self.tree.tag_configure("run", background=RUN_BG)
        self.tree.tag_configure("act", background=ACT_BG)
        vs = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        self.status = L(self)
        self.status.pack(side="bottom", fill="x")
        vs.pack(side="right", fill="y", pady=(4, 0))
        self.tree.pack(side="left", fill="both", expand=True, pady=4, padx=(0, 2))
        self.tree.bind("<Double-1>", lambda e: self.open_session())
        menu = tk.Menu(self, tearoff=0, **_menu_colors())
        menu.add_command(label="进入会话", command=self.open_session)
        menu.add_command(label="打开所在目录", command=self.reveal_dir)
        menu.add_command(label="删除会话", command=self.delete_session)
        self.tree.bind("<Button-3>", lambda e: (self.tree.identify_row(e.y)
                     and self.tree.selection_set(self.tree.identify_row(e.y)),
                     menu.tk_popup(e.x_root, e.y_root)))
        self.rows = []
        self.smap = {}
        self._loading = False

    def load(self):
        if self._loading:
            return
        self._loading = True
        self.status.config(text="加载会话中...")

        def work():
            rows = list_sessions()
            return rows, session_status_map([r[0] for r in rows])
        run_bg(work, on_ok=self._after_load,
               on_err=lambda e: (self._set_load_flag(False),
                                 self.status.config(text=f"读取会话失败: {e}")))

    def _set_load_flag(self, v):
        self._loading = v

    def _after_load(self, res):
        self._loading = False
        rows, smap = res
        self.rows = rows
        self.smap = smap
        cur = self.dirf.get()
        dirs = ["全部"] + sorted({r[2] for r in rows})
        self.dircb.config(values=dirs)
        if cur not in dirs:
            self.dirf.set("全部")
        self.refresh()

    def refresh(self):
        kw = self.kw.get().lower().strip()
        d = self.dirf.get()
        sf = self.statf.get()
        t = self.tree
        keep = set(t.selection())
        t.delete(*t.get_children())
        n = runc = actc = 0
        for r in self.rows:
            if d != "全部" and r[2] != d:
                continue
            if kw and kw not in f"{r[1]} {r[2]}".lower():
                continue
            txt, kind, _pids = self.smap.get(r[0], ("空闲", "idle", None))
            if sf == "空闲" and kind != "idle":
                continue
            if sf == "近期活跃" and kind != "act":
                continue
            if sf == "运行中" and kind != "run":
                continue
            runc += kind == "run"
            actc += kind == "act"
            tags = ("odd",) if n % 2 else ("even",)
            if kind != "idle":
                tags += (kind,)
            t.insert("", "end", iid=r[0], tags=tags,
                     values=(txt, human_time(r[3]), r[4],
                             (r[1] or "").strip().replace("\n", " ")[:56], r[2]))
            n += 1
        for k in keep:
            if t.exists(k):
                try:
                    t.selection_add(k)
                except Exception:
                    pass
        self.status.config(text=f"显示 {n} / {len(self.rows)} · 运行中 {runc} · 近期活跃 {actc} · 双击进入, F5 刷新")

    def update_status(self):
        if not self.rows or self._loading:
            return
        sids = list(self.tree.get_children())
        if not sids:
            return

        def work():
            return session_status_map(sids)

        def done(smap):
            self.apply_status(smap)
        run_bg(work, on_ok=done, on_err=lambda e: None)

    def apply_status(self, smap):
        if not smap:
            return
        self.smap.update(smap)
        t = self.tree
        changed = False
        for i, iid in enumerate(t.get_children()):
            info = smap.get(iid)
            if not info:
                continue
            txt, kind, _p = info
            vals = list(t.item(iid, "values"))
            if vals[0] != txt:
                vals[0] = txt
                zebra = "odd" if i % 2 else "even"
                t.item(iid, values=vals, tags=(zebra,) if kind == "idle" else (zebra, kind))
                changed = True
        if changed and self.statf.get() != "全部状态":
            self.refresh()

    def selected(self):
        return list(self.tree.selection())

    def _rows_map(self):
        return {r[0]: r for r in self.rows}

    def open_session(self):
        sids = self.selected()
        if not sids:
            Msg.info("提示", "先在列表中选中会话")
            return
        exe = find_opencode_exe()
        if not exe:
            Msg.error("错误", "找不到 opencode 可执行文件")
            return
        rows = self._rows_map()
        for sid in sids:
            d = rows[sid][2].replace("/", os.sep)
            if not os.path.isdir(d):
                if not Msg.askyesno("目录不存在", f"{d}\n\n可能是该项目尚未在本机 clone/pull.\n"
                                           "仍要创建空目录并进入?"):
                    continue
                os.makedirs(d, exist_ok=True)
            subprocess.Popen([exe, "--session", sid], cwd=d, creationflags=CREATE_NEW_CONSOLE)
        self.after(1500, self.update_status)

    def reveal_dir(self):
        sids = self.selected()
        if not sids:
            return
        d = self._rows_map()[sids[0]][2].replace("/", os.sep)
        if os.path.isdir(d):
            subprocess.Popen(["explorer", d])
        else:
            Msg.info("提示", f"目录不存在:\n{d}")

    def delete_session(self):
        sids = self.selected()
        if not sids:
            Msg.info("提示", "先在列表中选中会话")
            return

        def kind_of(s):
            return self.smap.get(s, ("空闲", "idle", None))[1]

        run_ids = [s for s in sids if kind_of(s) == "run"]
        rest = [s for s in sids if s not in run_ids]
        rows = self._rows_map()
        if run_ids:
            Msg.warning("已跳过运行中会话",
                        f"{len(run_ids)} 个会话正在 opencode 进程中运行, 不能删除:\n" +
                        "\n".join(f"  · {rows[s][1][:40]}" for s in run_ids[:6]))
        if not rest:
            return
        act_ids = [s for s in rest if kind_of(s) == "act"]
        if act_ids and not Msg.askyesno(
                "近期活跃确认",
                f"其中 {len(act_ids)} 个会话最近 5 分钟内有活动, 可能正被某个手动打开的 opencode "
                "窗口使用(进程命令行识别不到).\n\n确认相关窗口已关闭, 继续?"):
            return
        ids = session_closure(rest)
        counts = related_counts(ids)
        names = "\n".join(f"  · {rows[s][1][:44]}  [{rows[s][2]}]" for s in rest)
        if not Msg.askyesno("确认删除",
                            f"将永久删除 {len(rest)} 个会话(连同子会话共 {len(ids)} 个):\n{names}\n\n"
                            f"连带数据: 消息 {counts['message']} 条 / 数据块 {counts['part']} 条 / "
                            f"事件 {counts['event']} 条\n\n删除后不可恢复, 确定?"):
            return
        if not Msg.askyesno("再次确认", "最终确认: 真的要删除吗?"):
            return
        run_bg(lambda: delete_sessions(rest),
               on_ok=lambda gone: (self.load(),
                                   Msg.info("完成", f"已删除 {len(gone)} 个会话(含子会话).")),
               on_err=lambda e: Msg.error("删除失败", str(e)))


class SyncTab(Fr):
    def __init__(self, master):
        super().__init__(master, padding=(10, 8))
        self.st = Syncthing()
        self.names = {}
        top = Fr(self)
        top.pack(fill="x")
        self.info = L(top, text="", wraplength=980, justify="left")
        self.info.pack(anchor="w")
        row = Fr(self)
        row.pack(fill="x", pady=4)
        for text, cmd in (("刷新", self.load), ("立即同步(选中)", self.scan_sel),
                          ("暂停/恢复", self.toggle_pause), ("忽略规则(选中)", self.edit_ignores),
                          ("打开目录(选中)", self.open_dir)):
            mkbtn(row, text, cmd).pack(side="left", padx=2)
        row2 = Fr(self)
        row2.pack(fill="x", pady=(0, 4))
        mkbtn(row2, "本机收尾并等同步完成 (oc-out)", self.do_out).pack(side="left", padx=2)
        mkbtn(row2, "等待拉取并接管本机 (oc-in)", self.do_in, "primary").pack(side="left", padx=2)
        L(self, text="切换助手: 同一时刻只在一台电脑活跃. 换机前在旧机点 [收尾], 到新机点 [接管].",
          foreground=MUT).pack(anchor="w")
        cols = ("label", "state", "need", "path", "paused")
        self.tree = ttk.Treeview(self, columns=cols, show="headings", selectmode="browse")
        for c, w, txt, st in (("label", 170, "目录", False), ("state", 88, "状态", False),
                              ("need", 96, "待传", False), ("path", 330, "路径", True),
                              ("paused", 48, "暂停", False)):
            self.tree.heading(c, text=txt)
            self.tree.column(c, width=w, anchor="w", stretch=st, minwidth=44 if not st else 160)
        self.tree.tag_configure("odd", background=ODD)
        self.tree.tag_configure("even", background=EVEN)
        self.progress = L(self, text="")
        self.progress.pack(side="bottom", fill="x")
        self.tree.pack(fill="both", expand=True, pady=4)
        self.poll_job = None
        self.summary = "连接中..."
        if self.st.err:
            self.info.config(text=f"Syncthing: {self.st.err}")
        else:
            self.load()

    def load(self):
        self.progress.config(text="")

        def work():
            if not self.st.alive():
                return ("down", [], {}, {})
            fs = self.st.folders()
            self.names = self.st.device_names()
            st = {}
            for f in fs:
                try:
                    st[f["id"]] = self.st.folder_status(f["id"])
                except Exception:
                    st[f["id"]] = {}
            return ("ok", fs, st, self.st.connections())
        run_bg(work, on_ok=self._fill,
               on_err=lambda e: self.info.config(text=f"Syncthing 查询失败: {e}"))

    def _fill(self, r):
        kind, fs, st, conns = r
        self._last = (kind, fs, st, conns)
        marker = read_marker() or "-"
        online = ", ".join(self.names.get(k, k[:7]) for k in conns) if conns else "(无对端在线)"
        self.info.config(text=f"Syncthing: {'运行中' if kind == 'ok' else '未运行/未安装'}"
                              f"    活跃标记机器: {marker}    在线设备: {online}")
        t = self.tree
        keep = t.selection()
        t.delete(*t.get_children())
        for i, f in enumerate(fs):
            s = st.get(f["id"], {})
            need = s.get("needBytes", 0)
            t.insert("", "end", iid=f["id"], tags=("odd",) if i % 2 else ("even",),
                     values=(f.get("label") or f["id"], s.get("state", "?"),
                             (human_size(need) + " 待传") if need else "-",
                             f.get("path"), "是" if f.get("paused") else ""))
        for k in keep:
            if t.exists(k):
                t.selection_set(k)
        if self.poll_job:
            t.after_cancel(self.poll_job)
        if kind == "ok":
            self.poll_job = t.after(4000, self._light_refresh)

    def _light_refresh(self):
        self.poll_job = None
        ids = self.tree.get_children()
        if not ids:
            return

        def work():
            out = {}
            for iid in ids:
                try:
                    s = self.st.folder_status(iid)
                    need = s.get("needBytes", 0)
                    out[iid] = (s.get("state", "?"),
                                (human_size(need) + " 待传") if need else "-")
                except Exception:
                    out[iid] = None
            return out

        def done(res):
            for iid, val in (res or {}).items():
                if val and self.tree.exists(iid):
                    vals = list(self.tree.item(iid, "values"))
                    vals[1], vals[2] = val
                    self.tree.item(iid, values=vals)
            if self.tree.winfo_exists():
                self.poll_job = self.tree.after(4000, self._light_refresh)
        run_bg(work, on_ok=done, on_err=lambda e: None)

    def _sel(self):
        s = self.tree.selection()
        if not s:
            Msg.info("提示", "先在表中选中一个同步目录")
        return s[0] if s else None

    def scan_sel(self):
        fid = self._sel()
        if fid:
            run_bg(lambda: self.st.scan(fid), on_ok=lambda _: self.progress.config(text="已触发扫描"),
                   on_err=lambda e: Msg.error("失败", str(e)))

    def toggle_pause(self):
        fid = self._sel()
        if not fid:
            return
        want = self.tree.item(fid, "values")[4] != "是"
        run_bg(lambda: self.st.set_paused(fid, want), on_ok=lambda _: self.load(),
               on_err=lambda e: Msg.error("失败", str(e)))

    def open_dir(self):
        fid = self._sel()
        if fid:
            p = self.tree.item(fid, "values")[3]
            if os.path.isdir(p):
                subprocess.Popen(["explorer", p])

    def edit_ignores(self):
        fid = self._sel()
        if not fid:
            return
        try:
            lines = self.st.get_ignores(fid)
        except Exception as e:
            Msg.error("读取失败", str(e))
            return
        win = TL(self)
        win.title(f"忽略规则 - {self.tree.item(fid, 'values')[0]}")
        win.geometry("560x400")
        L(win, text="每行一条 Syncthing 忽略模式(留空行将被移除), 保存即生效并同步到对端:").pack(anchor="w", padx=6, pady=4)
        txt = tk.Text(win, wrap="none", bg=CARD, fg=FG, insertbackground=FG, relief="flat",
                      highlightthickness=1, highlightbackground=GRID, font=(UI_FONT, 10))
        txt.insert("1.0", "\n".join(lines))
        txt.pack(fill="both", expand=True, padx=6, pady=4)

        def save():
            new = [l for l in txt.get("1.0", "end").splitlines() if l.strip()]
            run_bg(lambda: self.st.set_ignores(fid, new),
                   on_ok=lambda _: (Msg.info("已保存", "忽略规则已写入."), win.destroy()),
                   on_err=lambda e: Msg.error("保存失败", str(e)))
        mkbtn(win, "保存", save, "primary").pack(anchor="e", padx=6, pady=6)

    def _st_ready(self):
        if self.st.err or not self.st.alive():
            Msg.warning("不可用", "Syncthing 未运行, 无法执行切换流程.")
            return False
        return True

    def _poll_sync(self, done_cb):
        start = _time.time()

        def tick():
            def work():
                pend = []
                for f in self.st.folders():
                    s = self.st.folder_status(f["id"])
                    if s.get("state") != "idle" or s.get("needBytes", 0) > 0:
                        pend.append(f.get("label") or f["id"])
                return pend

            def ok(pend):
                if not pend:
                    done_cb(True, "同步完成")
                    return
                if _time.time() - start > ST_TIMEOUT:
                    done_cb(False, f"等待超时({ST_TIMEOUT}s), 请确认对端在线后刷新状态")
                    return
                self.progress.config(text="同步中: " + ", ".join(pend))
                self.tree.after(2000, tick)
            run_bg(work, on_ok=ok, on_err=lambda e: done_cb(False, f"状态查询失败: {e}"))
        tick()

    def do_out(self):
        if not self._st_ready():
            return
        if opencode_running():
            Msg.warning("opencode 正在运行", "请先退出全部 opencode 窗口, 再执行收尾同步.")
            return
        if not Msg.askyesno("切换收尾", "将执行: WAL checkpoint → 写活跃标记 → 等待同步完成.\n"
                            "对端电脑在此完成前不要启动 opencode. 继续?"):
            return

        def work():
            wal_checkpoint()
            write_marker(os.environ.get("COMPUTERNAME", "?"))
            for f in self.st.folders():
                self.st.scan(f["id"])
            return True
        run_bg(work, on_ok=lambda _: self._poll_sync(
            lambda ok, msg: self.progress.config(text=("A→B 收尾完成, 可去另一台电脑点[接管]" if ok else msg))),
            on_err=lambda e: Msg.error("收尾失败", str(e)))

    def do_in(self):
        if not self._st_ready():
            return
        marker = read_marker()
        me = os.environ.get("COMPUTERNAME", "?")
        if marker and marker != me and not Msg.askyesno(
                "确认接管", f"上次活跃机器是 [{marker}], 确认它已退出 opencode 并完成收尾?\n"
                "否则可能造成会话数据丢失!"):
            return

        def finish(ok, msg):
            if not ok:
                self.progress.config(text=msg)
                Msg.warning("未完成", msg)
                return
            write_marker(me)
            self.load()
            Msg.info("接管完成", "本机已成为活跃机器, 到[会话管理]即可进入任意历史会话.")
        self._poll_sync(finish)


def _safe_isdir(e):
    try:
        return e.is_dir(follow_symlinks=False)
    except OSError:
        return False


class BrowseTab(Fr):
    def __init__(self, master):
        super().__init__(master, padding=(10, 8))
        bar = Fr(self)
        bar.pack(fill="x")
        mkbtn(bar, "↑ 上一级", self.up).pack(side="left")
        self.pathvar = tk.StringVar()
        self.pathcb = Combo(bar, textvariable=self.pathvar, width=48)
        self.pathcb.pack(side="left", fill="x", expand=True, padx=4)
        self.pathcb.bind("<Return>", lambda e: self.goto(self.pathvar.get()))
        mkbtn(bar, "进入", lambda: self.goto(self.pathvar.get())).pack(side="left")
        self.roots = {}
        try:
            st = Syncthing()
            if not st.err and st.alive():
                self.roots = {f.get("label") or f["id"]: f["path"] for f in st.folders()}
        except Exception:
            pass
        if not self.roots:
            self.roots = {"opencode会话数据": DATA_DIR,
                          "opencode配置": os.path.join(HOME, ".config", "opencode"),
                          "claudeproject": os.path.join("D:", os.sep, "PythonProjects", "claudeproject")}
        self.hint = L(self, style="Muted.TLabel" if not HAS_TB else None,
                      text="橙色=同步冲突文件  红色=Syncthing 传输残留  绿色=目录    文件删除/整理请右键“资源管理器中显示”后操作")
        self.hint.pack(side="bottom", fill="x")
        quick = ttk.LabelFrame(self, text="共享目录")
        quick.pack(fill="x", pady=2)
        for label, path in self.roots.items():
            mkbtn(quick, label, lambda p=path: self.goto(p)).pack(side="left", padx=3, pady=3)
        cols = ("name", "size", "mtime")
        self.tree = ttk.Treeview(self, columns=cols, show="headings")
        for c, w, txt, a, st in (("name", 420, "名称", "w", True), ("size", 80, "大小", "e", False),
                                 ("mtime", 130, "修改时间", "w", False)):
            self.tree.heading(c, text=txt)
            self.tree.column(c, width=w, anchor=a, stretch=st, minwidth=140 if st else 70)
        vs = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        vs.pack(side="right", fill="y", pady=4)
        self.tree.pack(side="left", fill="both", expand=True, pady=4, padx=(0, 2))
        dk = HAS_TB
        self.tree.tag_configure("dir", foreground="#2ecc71" if dk else "#0a7a44")
        self.tree.tag_configure("conflict", background="#6b4a12" if dk else "#ffe0b3")
        self.tree.tag_configure("sttmp", background="#6b2424" if dk else "#f4c7c7")
        self.tree.tag_configure("odd", background=ODD)
        self.tree.tag_configure("even", background=EVEN)
        self.tree.bind("<Double-1>", self.activate)
        menu = tk.Menu(self, tearoff=0, **_menu_colors())
        menu.add_command(label="打开/进入", command=self.activate)
        menu.add_command(label="资源管理器中显示", command=self.reveal)
        menu.add_command(label="复制完整路径", command=self.copy_path)
        self.tree.bind("<Button-3>", lambda e: (self.tree.identify_row(e.y)
                     and self.tree.selection_set(self.tree.identify_row(e.y)),
                     menu.tk_popup(e.x_root, e.y_root)))
        self.cur = None
        first = next(iter(self.roots.values()), None)
        if first:
            self.goto(first)

    def goto(self, path):
        path = (path or "").strip().strip('"')
        if not path or not os.path.isdir(path):
            Msg.info("提示", f"目录不存在: {path}")
            return
        self.cur = path
        self.pathvar.set(path)
        t = self.tree
        t.delete(*t.get_children())
        try:
            entries = list(os.scandir(path))
        except OSError as e:
            Msg.info("提示", f"无法读取: {e}")
            return
        entries.sort(key=lambda e: (not _safe_isdir(e), e.name.lower()))
        for j, e in enumerate(entries):
            try:
                isdir = _safe_isdir(e)
                stt = e.stat()
                nm = e.name
                tag = "dir" if isdir else ""
                if "sync-conflict" in nm:
                    tag = "conflict"
                elif nm.startswith(".syncthing."):
                    tag = "sttmp"
                t.insert("", "end", iid=e.path,
                         values=(("[目录] " if isdir else "") + nm,
                                 "" if isdir else human_size(stt.st_size),
                                 dt.datetime.fromtimestamp(stt.st_mtime).strftime("%Y-%m-%d %H:%M")),
                         tags=("odd" if j % 2 else "even", tag))
            except OSError:
                continue

    def up(self):
        if self.cur:
            parent = os.path.dirname(self.cur.rstrip("\\/"))
            if parent and parent != self.cur:
                self.goto(parent)

    def _sel_path(self):
        s = self.tree.selection()
        return s[0] if s else None

    def activate(self, _=None):
        p = self._sel_path()
        if not p:
            return
        if os.path.isdir(p):
            self.goto(p)
        else:
            try:
                os.startfile(p)
            except Exception as e:
                Msg.error("打开失败", str(e))

    def reveal(self):
        p = self._sel_path()
        if p:
            subprocess.Popen(["explorer", f"/select,{p}"])

    def copy_path(self):
        p = self._sel_path()
        if p:
            self.clipboard_clear()
            self.clipboard_append(p)


_AppBase = tb.Window if HAS_TB else tk.Tk
NAV_ITEMS = (("会话管理", "sessions"), ("共享同步", "sync"), ("目录浏览", "browse"))


class App(_AppBase):
    def __init__(self, tab=0):
        global app_ref
        _set_app_identity()
        try:
            import ctypes
            user32 = ctypes.windll.user32
            sw, sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        except Exception:
            sw, sh = 1920, 1080
        w, h = min(1280, int(sw * 0.78)), min(880, int(sh * 0.82))
        px, py = (sw - w) // 2, (sh - h) // 2 - 20
        if HAS_TB:
            super().__init__(title="opencode 会话与共享管理", themename="darkly",
                             size=(w, h), position=(px, py))
        else:
            super().__init__()
            self.title("opencode 会话与共享管理")
            self.geometry(f"{w}x{h}+{px}+{py}")
        app_ref = self
        setup_style(self)
        self.minsize(900, 540)
        ico = _app_icon_path()
        try:
            if os.path.isfile(ico):
                self.iconbitmap(default=ico)
        except tk.TclError:
            pass
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        nav = tk.Frame(self, bg=SIDEBAR, width=186)
        nav.grid(row=0, column=0, sticky="ns")
        nav.grid_propagate(False)
        tk.Label(nav, text="opencode 管理器", bg=SIDEBAR, fg=FG,
                 font=(UI_FONT, 12, "bold")).pack(anchor="w", padx=18, pady=(20, 14))
        self._navlbls = {}
        for text, key in NAV_ITEMS:
            lbl = tk.Label(nav, text=text, bg=SIDEBAR, fg=MUT, anchor="w",
                           font=(UI_FONT, 10), padx=22, pady=10, cursor="hand2")
            lbl.pack(fill="x")
            lbl.bind("<Button-1>", lambda e, k=key: self.show(k))
            lbl.bind("<Enter>", lambda e, k=key: self._nav_hover(k, True))
            lbl.bind("<Leave>", lambda e, k=key: self._nav_hover(k, False))
            self._navlbls[key] = lbl
        self.badge_sync = tk.Label(nav, text="同步: -", bg=SIDEBAR, fg=MUT, anchor="w",
                                   font=(UI_FONT, 9), padx=18)
        self.badge_host = tk.Label(nav, text="主机: -", bg=SIDEBAR, fg=MUT, anchor="w",
                                   font=(UI_FONT, 9), padx=18)
        self.badge_run = tk.Label(nav, text="运行: -", bg=SIDEBAR, fg=MUT, anchor="w",
                                  font=(UI_FONT, 9), padx=18)
        for bdg in (self.badge_run, self.badge_host, self.badge_sync):
            bdg.pack(side="bottom")
        tk.Frame(nav, bg=GRID, height=1).pack(side="bottom", fill="x", pady=8, padx=12)

        content = tk.Frame(self, bg=BG)
        content.grid(row=0, column=1, sticky="nsew")
        self.tab_sessions = SessionsTab(content)
        self.tab_sync = SyncTab(content)
        self.tab_browse = BrowseTab(content)
        self.pages = {"sessions": self.tab_sessions, "sync": self.tab_sync,
                      "browse": self.tab_browse}
        self._cur = None
        self._wrappers = (self.tab_sync.info, self.tab_browse.hint, self.tab_sessions.status)
        self.bind("<Configure>", self._on_resize, add="+")
        keys = [k for _t, k in NAV_ITEMS]
        self.show(keys[tab] if 0 <= tab < len(keys) else "sessions")
        self.bind("<F5>", lambda e: self.tab_sessions.load())
        self._nav_job = self.after(6000, self._tick)

    def show(self, key):
        if self._cur:
            self.pages[self._cur].pack_forget()
        self.pages[key].pack(fill="both", expand=True)
        self._cur = key
        for k, lbl in self._navlbls.items():
            if k == key:
                lbl.config(bg=_shift(SIDEBAR, 18), fg=FG, font=(UI_FONT, 10, "bold"))
            else:
                lbl.config(bg=SIDEBAR, fg=MUT, font=(UI_FONT, 10))
        if key == "sessions" and not self.tab_sessions.rows:
            self.tab_sessions.load()

    def _nav_hover(self, key, enter):
        lbl = self._navlbls[key]
        if key == self._cur:
            return
        lbl.config(bg=_shift(SIDEBAR, 10) if enter else SIDEBAR)

    def _tick(self):
        sids = [r[0] for r in self.tab_sessions.rows]

        def work():
            return (session_status_map(sids) if sids else {}, read_marker())

        def done(res):
            smap, marker = res
            runc = sum(1 for v in smap.values() if v[1] == "run")
            actc = sum(1 for v in smap.values() if v[1] == "act")
            self.badge_run.config(text=f"运行: {runc}  近期: {actc}",
                                  fg=C_SUCCESS if runc else (C_WARNING if actc else MUT))
            me = os.environ.get("COMPUTERNAME", "?")
            self.badge_host.config(text=f"主机: {marker or '-'}" + (" (本机)" if marker == me else ""))
            st = self.tab_sync
            summ = "未连接"
            last = getattr(st, "_last", None)
            if last:
                kind, _fs, fst, _c = last
                if kind != "ok":
                    summ = "未运行"
                else:
                    busy = any(s.get("state") != "idle" or s.get("needBytes", 0) > 0
                               for s in fst.values())
                    summ = "同步中" if busy else "已同步"
            self.badge_sync.config(text=f"同步: {summ}",
                                   fg=C_WARNING if summ == "同步中" else
                                   (MUT if summ in ("已同步", "未连接") else C_DANGER))
            if self._cur == "sessions":
                self.tab_sessions.apply_status(smap)
            self._nav_job = self.after(6000, self._tick)

        def err(_e):
            self.badge_sync.config(text="同步: 查询失败", fg=C_DANGER)
            self._nav_job = self.after(6000, self._tick)
        run_bg(work, on_ok=done, on_err=err)

    def _on_resize(self, e):
        if e.widget is self:
            wl = max(320, e.width - 480)
            for wdg in self._wrappers:
                try:
                    wdg.configure(wraplength=wl)
                except tk.TclError:
                    pass


def selftest():
    print("== 1. 会话查询 ==")
    rows = list_sessions()
    print(f"   顶层会话 {len(rows)} 条, 示例: {(rows[0][1] or '')[:20] if rows else '无'}")
    print("== 2. 级联删除 (真实 schema 的内存副本库) ==")
    src = db_ro()
    con = sqlite3.connect(":memory:")
    for (sql,) in src.execute("select sql from sqlite_master where type='table' and sql is not null"):
        try:
            con.execute(sql)
        except Exception as e:
            print("   建表跳过:", e)
    con.execute("insert into project (id,worktree,sandboxes,time_created,time_updated) values ('p1','/x','[]',1,1)")
    con.execute("insert into session (id,project_id,slug,directory,title,version,time_created,time_updated) "
                "values ('s1','p1','a','/x','t','v',1,1)")
    con.execute("insert into session (id,project_id,parent_id,slug,directory,title,version,time_created,time_updated) "
                "values ('s2','p1','s1','b','/x','sub','v',1,1)")
    con.execute("insert into message (id,session_id,time_created,time_updated,data) values ('m1','s2',1,1,'{}')")
    con.execute("insert into part (id,message_id,session_id,time_created,time_updated,data) values ('pa1','m1','s2',1,1,'{}')")
    con.execute("insert into todo (session_id,content,status,priority,position,time_created,time_updated) "
                "values ('s1','x','pending','high',0,1,1)")
    con.execute("insert into session_share (session_id,id,secret,url,time_created,time_updated) "
                "values ('s1','sh','sec','http://u',1,1)")
    con.execute("insert into event_sequence (aggregate_id,seq) values ('s1',5)")
    con.execute("insert into event (id,aggregate_id,seq,type,data) values ('e1','s1',1,'t','{}')")
    con.commit()
    ids = [r[0] for r in con.execute("select id from session")]
    ph = ",".join("?" * len(ids))
    con.execute("PRAGMA foreign_keys=ON")
    con.execute(f"delete from event where aggregate_id in ({ph})", ids)
    con.execute(f"delete from event_sequence where aggregate_id in ({ph})", ids)
    con.execute(f"delete from session where id in ({ph})", ids)
    left = {t: con.execute(f"select count(*) from {t}").fetchone()[0]
            for t in ("session", "message", "part", "todo", "session_share", "event", "event_sequence")}
    print("   删除后残留:", left)
    assert all(v == 0 for v in left.values()), "级联删除有残留!"
    src.close()
    con.close()
    print("   级联删除 OK")
    print("== 3. Syncthing REST ==")
    st = Syncthing()
    if st.err:
        print("   ", st.err)
    else:
        fs = st.folders()
        print(f"   myID={st.my_id()[:7]}...  folders={[f['id'] for f in fs]}")
        print(f"   忽略规则[{fs[0]['id']}]: {len(st.get_ignores(fs[0]['id']))} 条")
    print("== 4. 活跃度检测 ==")
    import re as _r
    cmd_re = _r.compile(r"(?:--session|-s)[= ]+(ses_[A-Za-z0-9]+)")
    assert cmd_re.search('x\\opencode.exe --session ses_ABC123abc')
    assert cmd_re.search('"opencode.exe" -s ses_XY9')
    assert not cmd_re.search('"opencode.exe"')
    print("   命令行解析正则 OK")
    run = running_sessions()
    act = last_activity_map()
    print(f"   运行中(命令行识别): {run or '无'}")
    print(f"   活动映射覆盖: {len(act)} 个顶层会话")
    if act:
        top = sorted(act.items(), key=lambda kv: -kv[1])[:1]
        print(f"   最近活动: {top[0][0]} @ {human_time(top[0][1])}")
    print("== 5. opencode ==")
    print("   running =", opencode_running(), "  exe =", find_opencode_exe())
    print("ALL PASS")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        selftest()
        return
    tab = 0
    if len(sys.argv) > 1:
        if sys.argv[1] == "smoke":
            app = App()
            app.after(1500, app.destroy)
            app.mainloop()
            print("GUI SMOKE OK")
            return
        if sys.argv[1].isdigit():
            tab = int(sys.argv[1])
    App(tab=tab).mainloop()


if __name__ == "__main__":
    main()
