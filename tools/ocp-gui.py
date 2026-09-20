#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ocp-gui - opencode 会话与共享目录集中管理器 (Tkinter).

Tab1 会话管理: 跨目录查看/搜索全部历史会话, 一键进入、定位、删除、上传所选会话, 查看会话详情(提问+回答, Markdown 渲染).
Tab2 会话同步: 勾选的会话导出成独立会话包, 经 GitHub 私库(HTTPS)在多台电脑间增量同步.
Tab3 工作目录: 列出会话用过的工作目录, 大小/最后修改时间后台统计, 一键进入.

用法:
  python ocp-gui.py            启动界面
  python ocp-gui.py selftest   无界面自检(级联删除在内存副本库验证 + REST 连通)
"""
import datetime as dt
import json
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zlib

HOME = os.path.expanduser("~")
DATA_DIR = os.path.join(HOME, ".local", "share", "opencode")
DB = os.environ.get("OPENCODE_DB", os.path.join(DATA_DIR, "opencode.db"))
MARKER = os.path.join(DATA_DIR, ".opencode-active-host")
CREATE_NEW_CONSOLE = 0x00000010
CREATE_NO_WINDOW = 0x08000000

SESSION_QUERY = """
select s.id, s.title, s.directory, s.time_updated,
       (select count(*) from message m where m.session_id = s.id) as msgs
  from session s
 where s.parent_id is null
 order by s.time_updated desc
"""


# ---------------- GitHub 私库会话同步 ----------------

_FROZEN_ENV_KEYS = ("_MEIPASS2", "TCL_LIBRARY", "TK_LIBRARY", "_PYI_APPLICATION_HOME_DIR",
                    "_PYI_ARCHIVE_FILE", "_PYI_PARENT_PROCESS_LEVEL", "_PYI_SPLASH_IPC")


def child_env():
    """PyInstaller onefile 会把 _MEIPASS2/_PYI_*/TCL_LIBRARY 传给子进程. 若原样继承,
    从 GUI 启动的 opencode 里再启动 GUI, 新进程会复用已被删除的 _MEI 解压目录,
    随即以 "Can't find a usable init.tcl" 报错打不开窗口. 启动子进程前必须剥离."""
    env = dict(os.environ)
    for k in [k for k in env if k.startswith("_PYI_") or k in _FROZEN_ENV_KEYS]:
        env.pop(k, None)
    return env


SYNC_CFG_FILE = os.path.join(DATA_DIR, "ocp-sync.json")
SYNC_WORKDIR = os.path.join(os.environ.get("LOCALAPPDATA", ""), "opencode-git-sync")
BUNDLE_DIR = "bundles"
BUNDLE_EXT = ".db"
MANIFEST_TABLE = "ocp_manifest"
DB_WARN_MB, DB_LIMIT_MB = 50, 95
BUNDLE_TABLES = ("project", "project_directory", "workspace", "session", "message", "part",
                 "todo", "session_share", "session_input", "session_message",
                 "session_context_epoch", "event_sequence", "event")
_COPY_MODE = {"project": "ignore", "project_directory": "ignore", "workspace": "ignore",
              "session": "replace", "message": "replace", "part": "replace", "todo": "replace",
              "session_share": "replace", "session_input": "replace",
              "session_message": "replace", "session_context_epoch": "replace",
              "event_sequence": "replace", "event": "replace"}
_GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never"}


def load_sync_cfg():
    try:
        with open(SYNC_CFG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_sync_cfg(cfg):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(SYNC_CFG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def db_stats():
    """本地会话库体积统计."""
    st = {"db": 0, "wal": 0, "sessions": 0, "messages": 0, "parts": 0, "zdb": 0}
    if os.path.isfile(DB):
        st["db"] = os.path.getsize(DB)
        try:
            with open(DB, "rb") as f:
                st["zdb"] = len(zlib.compress(f.read(), 6))
        except OSError:
            pass
    if os.path.isfile(DB + "-wal"):
        st["wal"] = os.path.getsize(DB + "-wal")
    try:
        con = db_ro()
        try:
            st["sessions"] = con.execute(
                "select count(*) from session where parent_id is null").fetchone()[0]
            st["messages"] = con.execute("select count(*) from message").fetchone()[0]
            st["parts"] = con.execute("select count(*) from part").fetchone()[0]
        finally:
            con.close()
    except Exception:
        pass
    return st


SYNC_TOKEN = {"v": ""}


def _git_prefix(token):
    """有 PAT 时给 git 关掉凭据助手: 既不查也不存.

    否则 git 会把地址里的 PAT 通过 credential approve 存进 Git Credential Manager,
    于是 github.com 下多出个 x-access-token 账号, 之后你自己 push 时 GCM 会弹出
    "选哪个账号"的窗口. 有 PAT 时我们每次都把 PAT 直接写进地址, 本就不需要助手.
    """
    if (token or "").strip():
        return ["-c", "credential.helper=", "-c", "credential.interactive=false"]
    return []


def _git(args, check=True):
    env = child_env()
    env.update(_GIT_ENV)
    cmd = ["git"] + _git_prefix(SYNC_TOKEN["v"]) + args
    r = subprocess.run(cmd, cwd=SYNC_WORKDIR, capture_output=True,
                       timeout=900, creationflags=CREATE_NO_WINDOW, env=env)
    if check and r.returncode != 0:
        msg = (r.stderr or r.stdout).decode("utf-8", "replace").strip()
        raise RuntimeError(msg or f"git {' '.join(args[:2])} 失败 (exit {r.returncode})")
    return r.stdout.decode("utf-8", "replace")


def _auth_url(url, token):
    """HTTPS 地址叠加 PAT; SSH 地址原样返回(走本机 git 凭据)."""
    token = (token or "").strip()
    if token and url.lower().startswith("https://"):
        return url.replace("://", "://x-access-token:" + token + "@", 1)
    return url


def _normalize_url(url):
    """私库地址默认按 HTTPS 规范化: owner/repo 或 github.com/owner/repo 补全为 https 地址."""
    u = (url or "").strip().rstrip("/")
    if not u:
        return ""
    if re.match(r"^(https?|git|ssh)://", u) or u.startswith("git@"):
        return u
    u = u.lstrip("/")
    if u.lower().startswith("github.com/"):
        u = u[len("github.com/"):]
    parts = [p for p in u.split("/") if p]
    if len(parts) >= 2:
        owner, repo = parts[0], parts[1]
        if not repo.endswith(".git"):
            repo += ".git"
        return f"https://github.com/{owner}/{repo}"
    return u


SYNC_ERR = {"last": ""}


def _err_remember(msg):
    """记住最近一次同步失败原因, 供"未就绪"之类的提示带上具体理由."""
    SYNC_ERR["last"] = (msg or "").strip()


def sync_last_error():
    return SYNC_ERR["last"]


def git_hint(msg):
    """把 git 的常见报错翻成可执行的中文提示(认不出来就返回空串)."""
    low = (msg or "").lower()
    if "write access to repository not granted" in low or "403" in low:
        return ("PAT 未授权该仓库(403). 细粒度 token: Repository access 勾上这个仓库, "
                "Permissions → Contents 设为 Read and write; classic token: 需要 repo 范围")
    if "401" in low or "bad credentials" in low or "authentication failed" in low:
        return "PAT 无效或已过期(401), 请重新生成后粘贴"
    if "repository not found" in low or "404" in low:
        return "仓库地址不对, 或 PAT 无权访问该仓库"
    if "not an empty directory" in low or "already exists and is not an empty" in low:
        return f"同步工作区不是空目录, 请先删除 {SYNC_WORKDIR} 再重试"
    if ("could not resolve host" in low or "failed to connect" in low
            or "timed out" in low or "connection reset" in low):
        return "网络不通或代理问题, 确认能访问 github.com"
    if "terminal prompts disabled" in low or "could not read username" in low:
        return "git 需要交互式凭证但已禁用; 请改用 HTTPS+PAT, 或确认本机 SSH 凭据可用"
    return ""


def _with_hint(e):
    msg = str(e).strip()
    hint = git_hint(msg)
    return f"{msg}\n\n提示: {hint}" if hint else msg


def _gh_api(path, token):
    """GitHub API: 返回 (状态码, 解析后的 JSON 或错误文本); 网络异常返回 (None, 原因)."""
    req = urllib.request.Request("https://api.github.com" + path,
                                 headers={"Accept": "application/vnd.github+json",
                                          "User-Agent": "opencode-anywhere"})
    tok = (token or "").strip()
    if tok:
        req.add_header("Authorization", "Bearer " + tok)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = str(e)
        return e.code, body
    except Exception as e:
        return None, str(e)


def _visible_repos(token, limit=8):
    code, body = _gh_api("/user/repos?per_page=100&affiliation="
                         "owner,collaborator,organization_member", token)
    if code != 200 or not isinstance(body, list):
        return []
    return [r.get("full_name") for r in body if r.get("full_name")][:limit]


def check_repo_access(url, token):
    """克隆前用 API 预检: 返回 None 表示检查通过(或无法判断), 否则返回中文原因."""
    slug = _github_slug(url)
    if not slug:
        return None
    owner, repo = slug
    code, body = _gh_api(f"/repos/{owner}/{repo}", token)
    if code == 200:
        if isinstance(body, dict) and not (body.get("permissions") or {}).get("push"):
            return (f"PAT 对 {owner}/{repo} 只有只读权限, 无法上传.\n"
                    "请把 token 的 Repository access 加上该仓库, "
                    "Permissions → Contents 设为 Read and write")
        return None
    if code == 401:
        return "PAT 无效或已过期(401), 请重新生成后粘贴"
    if code == 404:
        names = _visible_repos(token)
        extra = ("\n该 PAT 目前能看到: " + ", ".join(names)) if names else ""
        return (f"PAT 访问不到 {owner}/{repo} (404).\n"
                f"常见原因: token 的 Repository access 没勾选这个仓库, 地址写错, 或仓库不存在."
                f"{extra}\n"
                "细粒度 token: Repository access 勾上该仓库 + Contents: Read and write; "
                "classic token: 需要 repo 范围")
    return None


def repo_ready():
    return os.path.isdir(os.path.join(SYNC_WORKDIR, ".git"))


def repo_prepare(url, token):
    """clone(空仓库亦可) 到本地同步工作区, 幂等."""
    url = _normalize_url(url)
    if not url:
        raise RuntimeError("未配置私库地址")
    SYNC_TOKEN["v"] = (token or "").strip()
    if not repo_ready():
        # 有 PAT 且是 HTTPS 时先做 API 预检, 让"权限不足"在克隆前就以中文说清楚
        if url.lower().startswith("https://") and (token or "").strip():
            tip = check_repo_access(url, token)
            if tip:
                _err_remember(tip)
                raise RuntimeError(tip)
        if os.path.isdir(SYNC_WORKDIR):
            shutil.rmtree(SYNC_WORKDIR, ignore_errors=True)
        os.makedirs(SYNC_WORKDIR, exist_ok=True)
        try:
            _git(["clone", _auth_url(url, token), "."])
        except Exception as e:
            # 失败不留空工作区: 否则后续只会看到含糊的"未就绪"
            shutil.rmtree(SYNC_WORKDIR, ignore_errors=True)
            msg = _with_hint(e)
            _err_remember(msg)
            raise RuntimeError(msg)
    # 工作区可能之前就克隆好了, 而 remote.origin.url 是"克隆那一次"写进去的带令牌地址.
    # 换过 PAT 之后必须重写: 幂等地直接返回会让 fetch/push 一直用旧令牌, 报
    # "remote: Invalid username or token"(换 PAT 后所有同步都失败的原因).
    _git(["remote", "set-url", "origin", _auth_url(url, token)])
    _git(["config", "user.name", "opencode-sync"])
    _git(["config", "user.email", "opencode-sync@local"])
    _err_remember("")
    return url


def fetch_remote():
    """fetch origin; 失败时记住原因并附中文提示(让[同步工作区]那行反映真实理由)."""
    try:
        _git(["fetch", "origin"])
    except RuntimeError as e:
        msg = _with_hint(e)
        _err_remember(msg)
        raise RuntimeError(msg)
    _err_remember("")


def _head_branch():
    return _git(["symbolic-ref", "--short", "HEAD"], check=False).strip() or "main"


def _remote_rev(branch):
    return _git(["rev-parse", "--verify", "origin/" + branch], check=False).strip()


def _github_slug(url):
    m = re.search(r"github\.com[:/](.+?)/(.+?)(?:\.git)?/?$", (url or "").strip())
    return (m.group(1), m.group(2)) if m else None


def remote_repo_mb(url, token):
    """GitHub API 查询仓库总大小(KB->MB), 失败返回 None."""
    slug = _github_slug(url)
    if not slug:
        return None
    req = urllib.request.Request(
        f"https://api.github.com/repos/{slug[0]}/{slug[1]}",
        headers={"Accept": "application/vnd.github+json"})
    tok = (token or "").strip()
    if tok:
        req.add_header("Authorization", "Bearer " + tok)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode("utf-8")).get("size", 0) / 1024.0
    except Exception:
        return None


def _table_cols(con, table, schema="main"):
    return [r[1] for r in con.execute(f"pragma {schema}.table_info({table})")]


def _bundle_where(table, alias):
    """会话包内某张表按单个会话过滤的条件表达式."""
    if table == "project":
        return f"id in (select project_id from {alias}.session where id=?)"
    if table == "workspace":
        return f"id in (select workspace_id from {alias}.session where id=?)"
    if table == "project_directory":
        return f"project_id in (select project_id from {alias}.session where id=?)"
    if table == "session":
        return "id=?"
    if table in ("event", "event_sequence"):
        return "aggregate_id=?"
    return "session_id=?"


def _session_meta(src, ids):
    """导出用的会话清单: 标题/目录/消息数/字节估算."""
    ph = ",".join("?" * len(ids))
    metas = {i: {"id": i, "title": "", "directory": "", "msgs": 0, "bytes": 0} for i in ids}
    for i, t, d, m in src.execute(
            f"select s.id, s.title, s.directory, "
            f"(select count(*) from message m where m.session_id=s.id) "
            f"from session s where s.id in ({ph})", ids):
        metas[i].update(title=t or "", directory=d or "", msgs=m or 0)
    for table, col in (("message", "session_id"), ("part", "session_id"), ("event", "aggregate_id")):
        for sid, b in src.execute(
                f"select {col}, sum(length(data)) from {table} where {col} in ({ph}) group by {col}", ids):
            if sid in metas:
                metas[sid]["bytes"] += b or 0
    return [metas[i] for i in ids]


def export_bundle(root_ids, dest_path, src_path=None):
    """把 root_ids(含全部子会话)导出成独立会话包, 返回包清单."""
    src_path = src_path or DB
    ids = sorted(session_closure(root_ids, src_path))
    if not ids:
        raise RuntimeError("没有可导出的会话")
    ph = ",".join("?" * len(ids))
    where = {
        "project": f"id in (select project_id from session where id in ({ph}))",
        "workspace": f"id in (select workspace_id from session where id in ({ph}))",
        "project_directory": f"project_id in (select project_id from session where id in ({ph}))",
        "session": f"id in ({ph})",
        "message": f"session_id in ({ph})",
        "part": f"session_id in ({ph})",
        "todo": f"session_id in ({ph})",
        "session_share": f"session_id in ({ph})",
        "session_input": f"session_id in ({ph})",
        "session_message": f"session_id in ({ph})",
        "session_context_epoch": f"session_id in ({ph})",
        "event_sequence": f"aggregate_id in ({ph})",
        "event": f"aggregate_id in ({ph})",
    }
    src = db_ro(src_path)
    os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
    if os.path.isfile(dest_path):
        os.remove(dest_path)
    dest = sqlite3.connect(dest_path)
    try:
        for t in BUNDLE_TABLES:
            sql = src.execute("select sql from sqlite_master where type='table' and name=?",
                              (t,)).fetchone()
            if sql and sql[0]:
                dest.execute(sql[0])
        dest.execute(f"create table {MANIFEST_TABLE} (data text not null)")
        for t in BUNDLE_TABLES:
            cols = _table_cols(src, t)
            if not cols:
                continue
            cl = ",".join(cols)
            rows = src.execute(f"select {cl} from {t} where {where[t]}", ids)
            dest.executemany(f"insert into {t} ({cl}) values ({','.join('?' * len(cols))})", rows)
        manifest = {"host": os.environ.get("COMPUTERNAME", "?"),
                    "time": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "sessions": _session_meta(src, ids)}
        dest.execute(f"insert into {MANIFEST_TABLE} (data) values (?)",
                     (json.dumps(manifest, ensure_ascii=False),))
        dest.commit()
        dest.execute("VACUUM")
    finally:
        dest.close()
        src.close()
    manifest["size"] = os.path.getsize(dest_path)
    with open(dest_path, "rb") as f:
        manifest["zsize"] = len(zlib.compress(f.read(), 6))
    return manifest


def read_bundle_manifest(path):
    try:
        con = sqlite3.connect(f"file:{path.replace(os.sep, '/')}?mode=ro", uri=True)
        try:
            row = con.execute(f"select data from {MANIFEST_TABLE}").fetchone()
            return json.loads(row[0]) if row else {}
        finally:
            con.close()
    except Exception:
        return {}


def bundle_flags(bundle, host=None):
    """会话包的显示标记.

    new = 含本机缺少的会话(值得并入); own = 本机自己传的包; old = 本机已全部拥有.
    本机上传的包其会话必然都在本地, 所以 [本机缺] 恒为 0(不是算错).
    """
    host = os.environ.get("COMPUTERNAME", "?") if host is None else host
    if int(bundle.get("new") or 0) > 0:
        return "new"
    return "own" if (bundle.get("host") or "") == host else "old"


def _copy_from_bundle(con, alias, table, sid):
    """把会话包 alias 中某会话在 table 里的行并入本地(表结构取两边列名交集)."""
    lcols = _table_cols(con, table)
    bcols = _table_cols(con, table, alias)
    cols = [c for c in lcols if c in bcols]
    if not cols:
        return
    cl = ",".join(cols)
    con.execute(f"insert or {_COPY_MODE[table]} into main.{table} ({cl}) "
                f"select {cl} from {alias}.{table} where {_bundle_where(table, alias)}", (sid,))


def _drop_session_rows(con, sid):
    con.execute("delete from event where aggregate_id=?", (sid,))
    con.execute("delete from event_sequence where aggregate_id=?", (sid,))
    con.execute("delete from session where id=?", (sid,))


def sync_merge_bundles(paths, dst_path=None):
    """把选定的会话包并入本地库; 包内会话以包内版本为准覆盖同名会话."""
    paths = [p for p in paths if os.path.isfile(p)]
    if not paths:
        raise RuntimeError("没有选择会话包")
    added, replaced, hosts = [], [], []
    con = sqlite3.connect(dst_path or DB, timeout=30, isolation_level=None)
    aliases = []
    try:
        con.execute("PRAGMA foreign_keys=ON")
        # ATTACH 必须在事务之外, DETACH 必须在提交之后, 否则报 database is locked.
        for k, path in enumerate(paths):
            alias = f"b{k}"
            con.execute(f"ATTACH DATABASE ? AS {alias}", (path,))
            aliases.append(alias)
        con.execute("BEGIN IMMEDIATE")
        for alias in aliases:
            row = con.execute(f"select data from {alias}.{MANIFEST_TABLE}").fetchone()
            manifest = json.loads(row[0]) if row else {}
            hosts.append(manifest.get("host", "?"))
            sids = [s.get("id") for s in (manifest.get("sessions") or []) if s.get("id")]
            if not sids:
                sids = [r[0] for r in con.execute(f"select id from {alias}.session")]
            for sid in sids:
                existed = con.execute("select count(*) from session where id=?",
                                      (sid,)).fetchone()[0]
                _drop_session_rows(con, sid)
                for t in BUNDLE_TABLES:
                    _copy_from_bundle(con, alias, t, sid)
                (replaced if existed else added).append(sid)
        con.execute("COMMIT")
    except Exception:
        try:
            con.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        for alias in aliases:
            try:
                con.execute(f"DETACH DATABASE {alias}")
            except sqlite3.Error:
                pass
        con.close()
    return {"added": added, "replaced": replaced, "hosts": sorted(set(hosts))}


def sync_push_sessions(host, root_ids):
    """把所选会话导出成会话包并推送到私库; 返回包清单."""
    cfg = load_sync_cfg()
    url = repo_prepare(cfg.get("repo_url"), cfg.get("token"))
    branch = _head_branch()
    fetch_remote()
    if _remote_rev(branch):
        _git(["reset", "--hard", "origin/" + branch])
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_host = re.sub(r"[^A-Za-z0-9_.-]", "_", host or "pc") or "pc"
    dest = os.path.join(SYNC_WORKDIR, BUNDLE_DIR, f"{stamp}-{safe_host}{BUNDLE_EXT}")
    manifest = export_bundle(root_ids, dest)
    if manifest["zsize"] > DB_LIMIT_MB << 20:
        os.remove(dest)
        msg = (f"会话包压缩后约 {human_size(manifest['zsize'])}, 超过 "
               f"{DB_LIMIT_MB}MB (GitHub 单文件硬限 100MB).\n"
               "请减少所选会话(尤其是超大的旧会话), 或先删除部分旧会话再上传.")
        _err_remember(msg)
        raise RuntimeError(msg)
    _git(["add", "-A"])
    _git(["commit", "-m", f"sessions {safe_host} {stamp} "
                          f"({len(manifest['sessions'])} sessions)"])
    auth = _auth_url(url, cfg.get("token"))
    try:
        try:
            _git(["push", auth, branch])
        except RuntimeError:
            fetch_remote()
            _git(["rebase", "origin/" + branch], check=False)
            _git(["push", auth, branch])
    except RuntimeError as e:
        msg = _with_hint(e)
        _err_remember(msg)
        raise RuntimeError(msg)
    _err_remember("")
    write_marker(host)
    return manifest


def sync_remote():
    """拉取远端: 最近提交 + 仓库总量 + 会话包清单(含是否本机已有)."""
    cfg = load_sync_cfg()
    url = repo_prepare(cfg.get("repo_url"), cfg.get("token"))
    branch = _head_branch()
    fetch_remote()
    out = {"commit": None, "repo_mb": remote_repo_mb(url, cfg.get("token")), "bundles": []}
    if _remote_rev(branch):
        out["commit"] = _git(["log", "-1", "--date=iso",
                              "--format=%h %ci", "origin/" + branch]).strip()
        _git(["reset", "--hard", "origin/" + branch])
    local = set()
    try:
        con = db_ro()
        try:
            local = {r[0] for r in con.execute("select id from session")}
        finally:
            con.close()
    except Exception:
        pass
    d = os.path.join(SYNC_WORKDIR, BUNDLE_DIR)
    if os.path.isdir(d):
        for name in os.listdir(d):
            if not name.endswith(BUNDLE_EXT):
                continue
            p = os.path.join(d, name)
            m = read_bundle_manifest(p)
            if not m:
                continue
            sess = m.get("sessions") or []
            out["bundles"].append({
                "file": name, "path": p,
                "host": m.get("host", "?"), "time": m.get("time", "-"),
                "count": len(sess), "size": os.path.getsize(p),
                "bytes": sum(int(s.get("bytes") or 0) for s in sess),
                "msgs": sum(int(s.get("msgs") or 0) for s in sess),
                "new": sum(1 for s in sess if s.get("id") not in local),
                "titles": [s.get("title", "") for s in sess],
                "dirs": sorted({s.get("directory", "") for s in sess if s.get("directory")}),
            })
    out["bundles"].sort(key=lambda b: (b["time"], b["file"]), reverse=True)
    return out


# ---------------- sqlite ----------------

def db_ro(path=None):
    return sqlite3.connect(f"file:{(path or DB).replace(os.sep, '/')}?mode=ro", uri=True)


def list_sessions():
    con = db_ro()
    try:
        return con.execute(SESSION_QUERY).fetchall()
    finally:
        con.close()


def session_closure(root_ids, src_path=None):
    con = db_ro(src_path)
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


def session_size_map():
    child = {}
    roots = set()
    con = db_ro()
    try:
        for sid, pid in con.execute("select id, parent_id from session"):
            if pid:
                child[sid] = pid
            else:
                roots.add(sid)
        sums = {}
        for sql in ("select session_id, sum(length(data)) from message group by session_id",
                    "select session_id, sum(length(data)) from part group by session_id",
                    "select aggregate_id, sum(length(data)) from event group by aggregate_id"):
            for sid, b in con.execute(sql):
                sums[sid] = sums.get(sid, 0) + (b or 0)
    finally:
        con.close()

    def top(sid):
        seen = set()
        while sid in child and sid not in seen:
            seen.add(sid)
            sid = child[sid]
        return sid

    out = {}
    for sid, b in sums.items():
        r = top(sid)
        out[r] = out.get(r, 0) + b
    for r in roots:
        out.setdefault(r, 0)
    return out


PROMPT_OK, PROMPT_PARTIAL, PROMPT_EMPTY = "ok", "partial", "empty"


def dir_name(path):
    """目录的最后一级名称(D:/a/b -> b); 兼容 \\/ 混写与结尾分隔符."""
    p = (path or "").replace("/", os.sep).rstrip("\\/")
    return os.path.basename(p) or (path or "")


def short_dir(path, keep=1):
    """目录名 + 上一级, 用于区分同名目录(如下拉框显示)."""
    p = (path or "").replace("/", os.sep).rstrip("\\/")
    parts = [x for x in p.split(os.sep) if x]
    if not parts:
        return path or ""
    if len(parts) <= keep + 1:
        return os.sep.join(parts)
    return os.sep.join(parts[-(keep + 1):])


def dir_stats(path):
    """递归统计目录: (字节数, 文件数, 最新修改时间戳).

    用 os.scandir 手工递归(实测 31 个目录 0.37s; os.walk+getsize 要 6.5s),
    顺便在同一个循环里取最新 mtime. 无权限/半路消失的项跳过, 不抛异常.
    """
    total = files = 0
    newest = 0.0
    stack = [path]
    while stack:
        cur = stack.pop()
        try:
            entries = list(os.scandir(cur))
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    stack.append(e.path)
                else:
                    st = e.stat()
                    total += st.st_size
                    files += 1
                    if st.st_mtime > newest:
                        newest = st.st_mtime
            except OSError:
                continue
    return total, files, newest


def session_dirs(src_path=None, hide_missing=True):
    """会话使用的工作目录清单, 按会话数降序.

    返回 [{"path","name","short","sessions","roots","exists"}]; path 保持库里原样,
    name 是最后一级目录名. hide_missing=True 时只留磁盘上还在的目录.
    """
    con = db_ro(src_path)
    try:
        rows = con.execute(
            "select directory, count(*), sum(case when parent_id is null then 1 else 0 end) "
            "from session group by directory order by 2 desc, 1").fetchall()
    finally:
        con.close()
    out = []
    for d, c, r in rows:
        if not d:
            continue
        ok = os.path.isdir(d)
        if hide_missing and not ok:
            continue
        out.append({"path": d, "name": dir_name(d), "short": short_dir(d),
                    "sessions": c or 0, "roots": r or 0, "exists": ok})
    out.sort(key=lambda x: (-x["sessions"], x["name"].lower()))
    return out


def dirs_stats(paths):
    """批量统计(供后台线程调用): {path: (bytes, files, newest_mtime)}."""
    out = {}
    for p in paths:
        try:
            out[p] = dir_stats(p)
        except OSError:
            out[p] = (0, 0, 0.0)
    return out


def session_prompts(session_id, src_path=None):
    """某个会话的提问及对应回答, 按提问时间升序.

    返回 [{"id","time","q","a","state"}], state 取值:
      ok      = 该提问有收尾步骤(finish=stop), a 就是这一步的文字, 即真正的回答
      partial = 回合没跑完(没有 stop 步骤), a 是全部步骤文字的拼接(过程旁白)
      empty   = 没有 assistant 消息, 或整个回合一个字都没有

    一个提问会派生多个 assistant "步骤", 每步是 step-start -> 文字/工具 -> step-finish.
    中间步骤 finish=tool-calls, 文字都是"先检查一下配置""Now writing..."这类旁白;
    只有最后一步 finish=stop 的文字才是回答(实测 stop 永远是最后一步, 且通常只有一个文字块).
    所以这里只认 stop 那一步, 免得把过程旁白当成回答.
    """
    if not session_id:
        return []
    con = db_ro(src_path)
    try:
        qtime, qtext = {}, {}
        for pid, tc, t in con.execute(
                "select m.id, m.time_created, json_extract(p.data,'$.text') "
                "from message m join part p on p.message_id=m.id "
                "where m.session_id=? and json_extract(m.data,'$.role')='user' "
                "and json_extract(p.data,'$.type')='text' order by m.time_created",
                (session_id,)):
            qtime[pid] = tc or 0
            if t:
                qtext.setdefault(pid, []).append(t)
        if not qtime:
            return []
        steps = {}
        for aid, tc, fin, pid in con.execute(
                "select id, time_created, json_extract(data,'$.finish'), "
                "json_extract(data,'$.parentID') from message "
                "where session_id=? and json_extract(data,'$.role')='assistant' "
                "order by time_created", (session_id,)):
            if pid:
                steps.setdefault(pid, []).append((tc or 0, aid, fin))
        texts = {}
        for aid, t in con.execute(
                "select message_id, json_extract(data,'$.text') from part "
                "where session_id=? and json_extract(data,'$.type')='text' "
                "order by time_created", (session_id,)):
            if t:
                texts.setdefault(aid, []).append(t)
    finally:
        con.close()

    out = []
    for pid, tc in sorted(qtime.items(), key=lambda kv: kv[1]):
        ss = steps.get(pid) or []
        stop = next((a for _t, a, f in ss if f == "stop"), None)
        if stop is not None:
            ans = "\n".join(texts.get(stop) or [])
            state = PROMPT_OK if ans.strip() else PROMPT_EMPTY
        else:
            ans = "\n".join(t for _t, a, _f in ss for t in (texts.get(a) or []))
            state = PROMPT_PARTIAL if ans.strip() else PROMPT_EMPTY
        out.append({"id": pid, "time": tc, "q": "\n".join(qtext.get(pid) or []),
                    "a": ans, "state": state})
    return out


def prompt_state_note(state):
    return {PROMPT_OK: "完成", PROMPT_PARTIAL: "未完成, 以下为过程文字",
            PROMPT_EMPTY: "无内容"}.get(state, "")


def prompt_pair_md(item):
    """单条问答的 Markdown(用于渲染和复制)."""
    note = prompt_state_note(item.get("state"))
    head = f"**{human_time(item.get('time'))}**" + (f" · {note}" if note else "")
    ans = (item.get("a") or "").strip()
    return (f"{head}\n\n#### 提问\n\n{(item.get('q') or '').strip()}\n\n"
            f"#### 回答\n\n{ans or '> (无内容)'}\n")


def prompts_to_md(title, directory, items):
    """把提问/回答列表导出成 Markdown(纯函数, 便于自检)."""
    lines = [f"# 会话: {title or '-'}", "",
             f"- 目录: `{directory or '-'}`",
             f"- 导出时间: {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
             f"- 提问数: {len(items)}", ""]
    for i, it in enumerate(items, 1):
        lines += [f"## {i}. {human_time(it.get('time'))}", ""]
        note = prompt_state_note(it.get("state"))
        if note:
            lines += [f"> {note}", ""]
        ans = (it.get("a") or "").strip()
        lines += ["### 提问", "", (it.get("q") or "").strip(), "",
                  "### 回答", "", ans or "> (无内容)", ""]
    return "\n".join(lines).rstrip() + "\n"


# ---------------- Markdown 轻量渲染 ----------------
# opencode 的回答本身是 Markdown(标题/粗体/行内代码/代码块/表格/列表/引用/链接),
# 直接塞进 Text 会满屏 ** 和 ```. 这里自己解析并排版, 不引入第三方渲染库.

_MD_FENCE = re.compile(r"^\s*(```|~~~)\s*(\S*)\s*$")
_MD_HEAD = re.compile(r"^(#{1,6})\s+(.*)$")
_MD_HR = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$")
_MD_UL = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_MD_OL = re.compile(r"^(\s*)(\d+)[.)]\s+(.*)$")
_MD_SEPCELL = re.compile(r":?-{2,}:?")
_MD_INLINE = re.compile(
    r"(\*\*[^*\n]+\*\*|__[^_\n]+__)"          # 粗体
    r"|(`[^`\n]+`)"                            # 行内代码
    r"|(\[[^\]\n]+\]\([^)\s]+\))"              # 链接
    r"|(?<!\*)(\*[^*\n]+\*)(?!\*)"             # 斜体
)


def _disp_w(s):
    """显示宽度: CJK/全角算 2 格(表格对齐用)."""
    w = 0
    for ch in s or "":
        o = ord(ch)
        if (0x1100 <= o <= 0x115F or 0x2E80 <= o <= 0xA4CF or 0xAC00 <= o <= 0xD7A3
                or 0xF900 <= o <= 0xFAFF or 0xFE30 <= o <= 0xFE6F
                or 0xFF00 <= o <= 0xFF60 or 0xFFE0 <= o <= 0xFFE6
                or 0x1F300 <= o <= 0x1FAFF):
            w += 2
        else:
            w += 1
    return w


def md_strip(text):
    """去掉 Markdown 标记, 供列表预览和表格对齐显示(纯函数, 便于自检)."""
    s = text or ""
    s = re.sub(r"```.*?```", " ", s, flags=re.S)
    s = re.sub(r"`([^`]*)`", r"\1", s)
    s = re.sub(r"\*\*([^*]*)\*\*", r"\1", s)
    s = re.sub(r"__([^_]*)__", r"\1", s)
    s = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\1", s)
    s = re.sub(r"\[([^\]\n]+)\]\([^)\s]+\)", r"\1", s)
    s = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", s)
    s = re.sub(r"(?m)^\s*\|", "", s)
    s = re.sub(r"(?m)^\s*[-*+]\s+", "", s)
    s = re.sub(r"(?m)^\s*\d+[.)]\s+", "", s)
    s = re.sub(r"[ \t]+", " ", s)
    return s.strip()


def md_inline(text):
    """把一行拆成 [(片段, 样式, url)], 样式: plain/bold/italic/code/link(纯函数)."""
    out, pos, s = [], 0, text or ""
    for m in _MD_INLINE.finditer(s):
        if m.start() > pos:
            out.append((s[pos:m.start()], "plain", ""))
        g = m.group(0)
        if g.startswith("**") or g.startswith("__"):
            out.append((g[2:-2], "bold", ""))
        elif g.startswith("`"):
            out.append((g[1:-1], "code", ""))
        elif g.startswith("["):
            label, _, url = g[1:-1].partition("](")
            out.append((label, "link", url.rstrip(")")))
        else:
            out.append((g[1:-1], "italic", ""))
        pos = m.end()
    if pos < len(s):
        out.append((s[pos:], "plain", ""))
    return out


def md_parse(md):
    """把 Markdown 拆成块, 供 render_md 排版(纯函数, 便于自检).

    块: heading(level,text) / code(lang,lines) / table(rows,aligns) /
        list(ordered,items) / quote(lines) / hr / para(text)
    """
    lines = (md or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks, i = [], 0
    while i < len(lines):
        ln = lines[i]
        fence = _MD_FENCE.match(ln)
        if fence:
            mark = fence.group(1)
            lang = fence.group(2)
            i += 1
            body = []
            while i < len(lines) and not lines[i].strip().startswith(mark):
                body.append(lines[i])
                i += 1
            i += 1
            blocks.append({"kind": "code", "lang": lang, "lines": body})
            continue
        if _MD_HR.match(ln) and not _MD_UL.match(ln):
            blocks.append({"kind": "hr"})
            i += 1
            continue
        head = _MD_HEAD.match(ln)
        if head:
            blocks.append({"kind": "heading", "level": len(head.group(1)),
                           "text": head.group(2).strip()})
            i += 1
            continue
        if ln.lstrip().startswith("|") and ln.count("|") >= 2:
            rows = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            aligns, body = [], rows
            if len(rows) >= 2 and rows[1] and all(
                    _MD_SEPCELL.fullmatch(c) for c in rows[1] if c != ""):
                aligns = [("right" if c.endswith(":") and not c.startswith(":")
                           else "center" if c.startswith(":") and c.endswith(":")
                           else "left") for c in rows[1]]
                body = [rows[0]] + rows[2:]
            blocks.append({"kind": "table", "rows": body, "aligns": aligns})
            continue
        if ln.lstrip().startswith(">"):
            body = []
            while i < len(lines) and lines[i].lstrip().startswith(">"):
                body.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            blocks.append({"kind": "quote", "lines": body})
            continue
        ul, ol = _MD_UL.match(ln), _MD_OL.match(ln)
        if ul or ol:
            ordered = bool(ol)
            items = []
            while i < len(lines):
                mm = _MD_OL.match(lines[i]) if ordered else _MD_UL.match(lines[i])
                if not mm:
                    break
                items.append(mm.group(3) if ordered else mm.group(2))
                i += 1
            blocks.append({"kind": "list", "ordered": ordered, "items": items})
            continue
        if not ln.strip():
            i += 1
            continue
        para = []
        while i < len(lines) and lines[i].strip() and not (
                _MD_FENCE.match(lines[i]) or _MD_HEAD.match(lines[i])
                or lines[i].lstrip().startswith("|") or lines[i].lstrip().startswith(">")
                or _MD_UL.match(lines[i]) or _MD_OL.match(lines[i])
                or _MD_HR.match(lines[i])):
            para.append(lines[i].strip())
            i += 1
        blocks.append({"kind": "para", "text": " ".join(para)})
    return blocks


def render_md(widget, md):
    """把 Markdown 渲染进 tk.Text(调用方负责 state 与清空)."""
    mono = ("Consolas", 10)
    widget.configure(font=(UI_FONT, 10), wrap="word", spacing1=1, spacing3=2)
    widget.tag_configure("md_h1", font=(UI_FONT, 15, "bold"), spacing1=10, spacing3=6)
    widget.tag_configure("md_h2", font=(UI_FONT, 13, "bold"), spacing1=8, spacing3=4)
    widget.tag_configure("md_h3", font=(UI_FONT, 12, "bold"), spacing1=6, spacing3=3)
    widget.tag_configure("md_h4", font=(UI_FONT, 11, "bold"), spacing1=4, spacing3=2)
    widget.tag_configure("md_bold", font=(UI_FONT, 10, "bold"))
    widget.tag_configure("md_italic", font=(UI_FONT, 10, "italic"))
    widget.tag_configure("md_code", font=mono, background=ODD)
    widget.tag_configure("md_pre", font=mono, background=ODD, lmargin1=14, lmargin2=14,
                         spacing1=4, spacing3=4)
    widget.tag_configure("md_table", font=mono, lmargin1=14)
    widget.tag_configure("md_tablehead", font=(mono[0], mono[1], "bold"), lmargin1=14)
    widget.tag_configure("md_quote", lmargin1=16, lmargin2=16, foreground=MUT)
    widget.tag_configure("md_hr", foreground=GRID)
    widget.tag_configure("md_bullet", lmargin1=18, lmargin2=34)
    widget.tag_configure("md_link", foreground=ACCENT, underline=True)
    link_no = [0]

    def put(text, tags=()):
        widget.insert("end", text, tuple(tags))

    def put_inline(text, extra=()):
        for seg, style, url in md_inline(text):
            if style == "bold":
                put(seg, extra + ("md_bold",))
            elif style == "italic":
                put(seg, extra + ("md_italic",))
            elif style == "code":
                put(seg, extra + ("md_code",))
            elif style == "link":
                tag = f"md_url_{link_no[0]}"
                link_no[0] += 1
                try:
                    widget.tag_configure(tag, foreground=ACCENT, underline=True)
                    widget.tag_bind(tag, "<Button-1>", lambda e, u=url: webbrowser.open(u))
                    widget.tag_bind(tag, "<Enter>", lambda e: widget.configure(cursor="hand2"))
                    widget.tag_bind(tag, "<Leave>", lambda e: widget.configure(cursor=""))
                    put(seg, extra + ("md_link", tag))
                except tk.TclError:
                    put(seg, extra)
            else:
                put(seg, extra)

    for blk in md_parse(md):
        kind = blk["kind"]
        if kind == "heading":
            put_inline(blk["text"], (f"md_h{min(blk['level'], 4)}",))
            put("\n")
        elif kind == "para":
            put_inline(blk["text"])
            put("\n\n")
        elif kind == "hr":
            put("\u2500" * 46 + "\n", ("md_hr",))
        elif kind == "code":
            put("".join(ln + "\n" for ln in blk["lines"]) or "\n", ("md_pre",))
            put("\n")
        elif kind == "quote":
            for ln in blk["lines"]:
                put_inline(ln, ("md_quote",))
                put("\n")
            put("\n")
        elif kind == "list":
            for n, item in enumerate(blk["items"], 1):
                put(f"{n}. " if blk["ordered"] else "\u2022 ", ("md_bullet",))
                put_inline(item, ("md_bullet",))
                put("\n")
            put("\n")
        elif kind == "table":
            rows, aligns = blk["rows"], blk["aligns"]
            if not rows:
                continue
            ncol = max(len(r) for r in rows)
            widths = [0] * ncol
            for r in rows:
                for c in range(min(ncol, len(r))):
                    widths[c] = max(widths[c], _disp_w(md_strip(r[c])))
            for ri, r in enumerate(rows):
                cells = []
                for c in range(ncol):
                    plain = md_strip(r[c]) if c < len(r) else ""
                    pad = max(0, widths[c] - _disp_w(plain))
                    al = aligns[c] if c < len(aligns) else "left"
                    if al == "right":
                        cells.append(" " * pad + plain)
                    elif al == "center":
                        cells.append(" " * (pad // 2) + plain + " " * (pad - pad // 2))
                    else:
                        cells.append(plain + " " * pad)
                put("  ".join(cells).rstrip() + "\n",
                    ("md_tablehead",) if ri == 0 else ("md_table",))
            put("\n")


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


def vacuum_db():
    """重建库文件回收空闲页(删除会话后调用), 需 opencode 已退出."""
    before = os.path.getsize(DB) if os.path.isfile(DB) else 0
    con = sqlite3.connect(DB, timeout=60, isolation_level=None)
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.execute("VACUUM")
    finally:
        con.close()
    after = os.path.getsize(DB) if os.path.isfile(DB) else 0
    return before, after


def opencode_running():
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq opencode.exe", "/NH"],
                             capture_output=True, timeout=10, env=child_env(),
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
from tkinter import filedialog, messagebox, ttk

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
RUN_FG = "#0d6b47"
ACT_FG = "#8a5a08"
C_SUCCESS = "#1a7f4e"
C_WARNING = "#a86800"
C_DANGER = "#b02b2b"
THEME_FILE = os.path.join(DATA_DIR, "ocp-gui-theme.json")
WINDOW_FILE = os.path.join(DATA_DIR, "ocp-gui-window.json")


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


def fit_col_width(text, pad=32, minw=48):
    """按表头文字实测宽度算列宽.

    Treeview 表头用的是粗体 TkHeadingFont, 比正文字宽, 必须用它来量,
    否则表头文字会被截断. pad 覆盖 Treeview.Heading 的 padding=(12,10)
    (左右共 24px) 与排序/图标余量.
    """
    try:
        w = tkfont.nametofont("TkHeadingFont").measure(text)
    except tk.TclError:
        w = len(str(text)) * 12
    return max(minw, w + pad)


def content_col_width(samples, head_text, pad=40, minw=48, maxw=None):
    """按"表头 + 内容样本"里更宽的那个算列宽, 保证内容不被截断.

    samples 用全量数据的候选值(不是当前可见行), 这样筛选/刷新时列宽不会跳动.
    maxw 给列宽设上限, 免得单列把拉伸列(路径)挤没.
    """
    try:
        w = max(tkfont.nametofont("TkHeadingFont").measure(head_text),
                max((tkfont.nametofont("TkDefaultFont").measure(str(s))
                     for s in samples), default=0))
    except tk.TclError:
        w = max(len(str(head_text)), max((len(str(s)) for s in samples), default=0)) * 12
    want = max(minw, w + pad)
    return min(want, maxw) if maxw else want


def fit_columns(tree, cols, stretch_cols=(), pad=32, minw=48, content_cols=None, caps=None):
    """把各列宽收到刚好放得下: 非拉伸列固定, 拉伸列吃掉剩余空间.

    默认只按表头宽度算, 不跟随内容变化 —— 否则筛选/刷新时列宽会来回跳.
    content_cols: {列id: [内容样本]} 这些列按表头与样本里更宽的一个算, 保证内容显示全.
    caps: {列id: 最大宽度} 给个别列(尤其内容列)设上限, 避免挤掉拉伸列.
    cols: [(列id, 表头文字, anchor), ...]
    """
    stretch_cols = tuple(stretch_cols)
    content_cols = content_cols or {}
    caps = caps or {}
    for cid, text, anchor in cols:
        st = cid in stretch_cols
        if cid in content_cols:
            want = content_col_width(content_cols[cid], text, pad, minw, caps.get(cid))
        else:
            want = fit_col_width(text, pad, minw)
            if cid in caps:
                want = min(want, caps[cid])
        tree.heading(cid, text=text)
        tree.column(cid, width=want, anchor=anchor, stretch=st,
                    minwidth=minw if not st else max(minw, want))
    return tree


def load_theme():
    try:
        with open(THEME_FILE, encoding="utf-8") as f:
            return "light" if json.load(f).get("theme") == "light" else "dark"
    except (OSError, ValueError, TypeError):
        return "dark"


def save_theme(theme):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(THEME_FILE, "w", encoding="utf-8") as f:
        json.dump({"theme": theme}, f)


def load_window_geometry():
    try:
        with open(WINDOW_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return tuple(int(data[k]) for k in ("width", "height", "x", "y"))
    except (OSError, ValueError, TypeError, KeyError):
        return None


def save_window_geometry(width, height, x, y):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(WINDOW_FILE, "w", encoding="utf-8") as f:
            json.dump({"width": width, "height": height, "x": x, "y": y}, f)
    except OSError:
        pass


def setup_style(app, theme=None):
    global BG, SIDEBAR, CARD, FG, MUT, ACCENT, ACCENT_DARK, SUCCESS, WARN, DANGER
    global SEL, ODD, EVEN, GRID, MENU_BG, RUN_BG, ACT_BG, C_SUCCESS, C_WARNING, C_DANGER
    global RUN_FG, ACT_FG
    theme = theme or load_theme()
    style = tb.Style(theme="darkly" if theme == "dark" else "flatly") if HAS_TB else ttk.Style(app)
    # 调色板独立于 ttkbootstrap: 冻结 exe 里若 ttkbootstrap 导入失败(如缺少 PIL),
    # 深色主题仍必须生效, 不能走硬编码浅色回退.
    if theme == "dark":
        BG, CARD, FG = "#11151c", "#1a202b", "#f3f6fb"
        SIDEBAR, GRID, MENU_BG = "#0b0e13", "#2a3443", "#1d2430"
        MUT = "#8d99aa"
        ACCENT, ACCENT_DARK = "#788cff", "#5d70e6"
        SUCCESS, WARN, DANGER = "#45d6a0", "#e5ad58", "#ef6f7b"
        SEL, ODD, EVEN = "#293452", "#151b24", "#11151c"
        RUN_BG, ACT_BG = "#173a35", "#3d3018"
        RUN_FG, ACT_FG = "#45d6a0", "#e5ad58"
    else:
        BG, CARD, FG = "#f4f6fa", "#ffffff", "#202938"
        SIDEBAR, GRID, MENU_BG = "#e9edf5", "#d9e0eb", "#ffffff"
        MUT = "#657186"
        ACCENT, ACCENT_DARK = "#5267d9", "#4053bd"
        SUCCESS, WARN, DANGER = "#16805b", "#ae710e", "#c43f4b"
        SEL, ODD, EVEN = "#e8edff", "#f8faff", "#ffffff"
        RUN_BG, ACT_BG = "#c9ecd9", "#ffe7b8"
        RUN_FG, ACT_FG = "#0d6b47", "#8a5a08"
    C_SUCCESS, C_WARNING, C_DANGER = SUCCESS, WARN, DANGER
    if not HAS_TB:
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", background=BG, foreground=FG)
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("Muted.TLabel", background=BG, foreground=MUT)
        style.configure("TButton", padding=(12, 6), background=CARD, foreground=FG,
                        bordercolor=GRID, relief="flat", focusthickness=0)
        style.map("TButton", background=[("active", SEL), ("disabled", ODD)],
                  foreground=[("disabled", MUT)])
    for fname in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkIconFont",
                  "TkTooltipFont", "TkCaptionFont", "TkSmallCaptionFont"):
        _f(fname, size=10)
    _f("TkHeadingFont", size=10, weight="bold")
    app.configure(bg=BG)
    style.configure("TFrame", background=BG)
    style.configure("Treeview", rowheight=34, borderwidth=0, relief="flat",
                    background=EVEN, fieldbackground=EVEN, foreground=FG)
    style.configure("Treeview.Heading", padding=(12, 10), background=CARD,
                    foreground=MUT, relief="flat")
    style.map("Treeview", background=[("selected", SEL)],
              foreground=[("selected", FG)])
    style.configure("TLabelframe", background=BG, bordercolor=GRID)
    style.configure("TLabelframe.Label", background=BG, foreground=MUT)
    style.configure("Card.TFrame", background=CARD)
    style.configure("PageTitle.TLabel", background=BG, foreground=FG,
                    font=(UI_FONT, 20, "bold"))
    style.configure("PageSubtitle.TLabel", background=BG, foreground=MUT,
                    font=(UI_FONT, 9))
    style.configure("Status.TLabel", background=CARD, foreground=MUT,
                    padding=(12, 8))
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


def _is_frozen():
    return getattr(sys, "frozen", False)


def _set_app_identity():
    """脱离 pythonw 默认分组, 让任务栏显示窗口自己的图标而不是 Python 图标."""
    try:
        import ctypes
        # 冻结成 exe 后, 主程序图标已由 PyInstaller 注入; AUMID 与可执行文件绑定,
        # 保证任务栏分组稳定, 不会因为改名/换目录而漂回 Python 图标.
        if _is_frozen():
            appid = "opencode.anywhere.exe"
        else:
            appid = "opencode.ocp-gui"
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(appid)
    except Exception:
        pass


def _app_icon_path():
    if _is_frozen():
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "app.ico")


_BG_RESULTS = queue.Queue()


def run_bg(fn, on_ok=None, on_err=None):
    """子线程跑 fn, 结果交回主线程处理.

    子线程不直接碰 Tk: 只把 (回调, 结果) 放进队列, 由主线程的 _bg_poll 取出执行.
    这样避开 Tk 的跨线程调用, 也避开 "except ... as e 结束后 e 被删除" 的闭包坑:
    旧写法 app_ref.after(0, lambda: on_err(e)) 里取 e 会抛 NameError, 异常被 Tk
    静默吞掉 -> 失败既不弹窗也不改文字, 界面就永远停在"正在...中".
    """
    def wrap():
        try:
            r = fn()
        except Exception as exc:
            _BG_RESULTS.put((on_err, exc))
        else:
            _BG_RESULTS.put((on_ok, r))
    threading.Thread(target=wrap, daemon=True).start()


def _bg_drain(on_default_error):
    """取空队列里已完成的子线程结果, 逐个回调; 返回处理条数(无 Tk 依赖, 便于自检)."""
    n = 0
    while True:
        try:
            cb, payload = _BG_RESULTS.get_nowait()
        except queue.Empty:
            return n
        n += 1
        try:
            if cb is None:
                if isinstance(payload, BaseException):
                    on_default_error(payload)
            else:
                cb(payload)
        except Exception as exc:
            on_default_error(exc)


def _bg_poll():
    """主线程轮询子线程结果(约 8 次/秒)."""
    _bg_drain(lambda e: Msg.error("错误", str(e)))
    if app_ref is not None:
        app_ref.after(120, _bg_poll)


# ---------------- 会话活跃度检测 ----------------

_WMI_PS = ("Get-CimInstance Win32_Process -Filter \"Name='opencode.exe'\" | "
           "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress")


def running_sessions():
    """扫 opencode.exe 命令行里的 --session/-s ses_xxx -> {sid: [pid]}.
    直接敲 opencode 打开的情况命令行里没有 id, 由 5 分钟时间启发兜底."""
    out = {}
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", _WMI_PS],
                           capture_output=True, timeout=15, env=child_env(),
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
        mkbtn(bar, "查看会话详情", self.view_prompts).pack(side="left", padx=2)
        mkbtn(bar, "上传所选会话", self.upload_selected).pack(side="left", padx=2)
        mkbtn(bar, "删除会话", self.delete_session, "danger").pack(side="left", padx=2)
        self.toolbar = bar
        cols = ("status", "msgs", "size", "title", "dir", "time", "path")
        self.tree = ttk.Treeview(self, columns=cols, show="headings", selectmode="extended")
        self.COLS = (("status", "状态", "w"), ("msgs", "消息", "w"), ("size", "大小", "w"),
                     ("title", "标题", "w"), ("dir", "目录", "w"), ("time", "更新时间", "w"),
                     ("path", "路径", "w"))
        self.apply_col_widths()
        self.tree.column("path", minwidth=200)
        self.tree.tag_configure("odd", background=ODD)
        self.tree.tag_configure("even", background=EVEN)
        self.tree.tag_configure("run", background=RUN_BG, foreground=RUN_FG,
                                font=(UI_FONT, 10, "bold"))
        self.tree.tag_configure("act", background=ACT_BG, foreground=ACT_FG)
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
        menu.add_command(label="查看会话详情", command=self.view_prompts)
        menu.add_command(label="上传所选会话", command=self.upload_selected)
        menu.add_command(label="删除会话", command=self.delete_session)
        self.tree.bind("<Button-3>", lambda e: (self.tree.identify_row(e.y)
                     and self.tree.selection_set(self.tree.identify_row(e.y)),
                     menu.tk_popup(e.x_root, e.y_root)))
        self.rows = []
        self.smap = {}
        self.sizes = {}
        self.dir_labels = {"全部": "全部"}
        self._loading = False

    def apply_col_widths(self):
        """状态/消息/大小/标题/目录/更新时间 取"表头与全量内容里更宽的"以显示全.

        样本来自 self.rows(全量, 不是可见行), 所以筛选/刷新时列宽不跳动;
        路径是唯一拉伸列, 吃剩余宽度.
        """
        rows = getattr(self, "rows", [])
        title = [ (r[1] or "").strip().replace("\n", " ")[:56] for r in rows ] or [""]
        dirs = [ dir_name(r[2]) for r in rows ] or [""]
        msgs = [ r[4] for r in rows ] or ["0"]
        times = [ human_time(r[3]) for r in rows ] or ["2026-09-20 17:34"]
        fit_columns(self.tree, self.COLS, stretch_cols=("path",),
                    content_cols={
                        "status": ["● 运行中", "近期活跃", "空闲"],
                        "msgs": msgs,
                        "size": [human_size(n) for n in (1024, 5 << 20, 250 << 20, 3 << 30)] + ["9999.9GB"],
                        "title": title,
                        "dir": dirs,
                        "time": times,
                    },
                    caps={"status": 88, "msgs": 76, "size": 104, "title": 320, "dir": 250})

    def load(self):
        if self._loading:
            return
        self._loading = True
        self.status.config(text="加载会话中...")

        def work():
            rows = list_sessions()
            return rows, session_status_map([r[0] for r in rows]), session_size_map()
        run_bg(work, on_ok=self._after_load,
               on_err=lambda e: (self._set_load_flag(False),
                                 self.status.config(text=f"读取会话失败: {e}")))

    def _set_load_flag(self, v):
        self._loading = v

    def _after_load(self, res):
        self._loading = False
        rows, smap, sizes = res
        self.rows = rows
        self.smap = smap
        self.sizes = sizes
        # 下拉框显示"名称(上级目录)"以区分同名目录; 值仍是库里的全路径, 靠 dirf 反查
        self.dir_labels = {"全部": "全部"}
        for d in sorted({r[2] for r in rows}):
            self.dir_labels.setdefault(d, short_dir(d))
        cur = self.dirf.get()
        labels = ["全部"] + sorted({v for k, v in self.dir_labels.items() if k != "全部"})
        self.dircb.config(values=labels)
        if cur not in labels:
            self.dirf.set("全部")
        self.apply_col_widths()
        self.refresh()

    def _dir_filter(self):
        """当前选中的目录(库里原样路径); "全部" 表示不过滤."""
        sel = self.dirf.get()
        if sel == "全部":
            return "全部"
        for path, label in self.dir_labels.items():
            if path != "全部" and label == sel:
                return path
        return "全部"

    def filter_dir(self, path):
        """供[工作目录]页跳转过来时预置目录筛选."""
        self.load()
        if not path or path == "全部":
            self.dirf.set("全部")
            self.refresh()
            return
        if path not in self.dir_labels:
            self.dir_labels[path] = short_dir(path)
            labels = ["全部"] + sorted({v for k, v in self.dir_labels.items() if k != "全部"})
            self.dircb.config(values=labels)
        self.dirf.set(self.dir_labels[path])
        self.refresh()

    def refresh(self):
        kw = self.kw.get().lower().strip()
        d = self._dir_filter()
        sf = self.statf.get()
        t = self.tree
        keep = set(t.selection())
        t.delete(*t.get_children())
        n = runc = actc = 0
        for r in self.rows:
            if d != "全部" and r[2] != d:
                continue
            if kw and kw not in f"{r[1]} {r[2]} {dir_name(r[2])}".lower():
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
            disp = f"● {txt}" if kind == "run" else txt
            b = self.sizes.get(r[0], 0)
            t.insert("", "end", iid=r[0], tags=tags,
                     values=(disp, r[4], human_size(b) if b else "-",
                             (r[1] or "").strip().replace("\n", " ")[:56],
                             dir_name(r[2]), human_time(r[3]), r[2]))
            n += 1
        for k in keep:
            if t.exists(k):
                try:
                    t.selection_add(k)
                except Exception:
                    pass
        total = sum(self.sizes.values())
        self.status.config(text=f"显示 {n} / {len(self.rows)} · 运行中 {runc} · 近期活跃 {actc}"
                                f" · 会话合计 {human_size(total)} · 可多选后点[上传所选会话]"
                                f" · 双击进入, F5 刷新")

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
            disp = f"● {txt}" if kind == "run" else txt
            if vals[0] != disp:
                vals[0] = disp
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
            subprocess.Popen([exe, "--session", sid], cwd=d, env=child_env(),
                             creationflags=CREATE_NEW_CONSOLE)
        self.after(1500, self.update_status)

    def reveal_dir(self):
        sids = self.selected()
        if not sids:
            return
        d = self._rows_map()[sids[0]][2].replace("/", os.sep)
        if os.path.isdir(d):
            subprocess.Popen(["explorer", d], env=child_env())
        else:
            Msg.info("提示", f"目录不存在:\n{d}")

    def view_prompts(self):
        """查看所选会话的详情: 会话信息 + 全部提问与对应回答(一次只能看一个会话)."""
        sids = self.selected()
        if not sids:
            Msg.info("提示", "先在列表中选中一个会话")
            return
        if len(sids) > 1:
            Msg.info("提示", "一次只能查看一个会话的详情, 请只选中一个")
            return
        row = self._rows_map().get(sids[0])
        if not row:
            return
        size = self.sizes.get(sids[0], 0)
        meta = {"msgs": row[4], "updated": human_time(row[3]),
                "size": human_size(size) if size else "-"}
        PromptDialog(self, sids[0], row[1] or "", row[2] or "", meta)

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

    def upload_selected(self):
        sids = self.selected()
        if not sids:
            Msg.info("提示", "先在列表中选中要上传的会话(可多选)")
            return
        cfg = load_sync_cfg()
        if not (cfg.get("repo_url") or "").strip():
            Msg.warning("未配置私库", "请先到[会话同步]页填写私库地址和 PAT, 点[保存并准备仓库].")
            return
        if not repo_ready():
            last = sync_last_error()
            Msg.warning("同步工作区未就绪",
                        "请先到[会话同步]页点[保存并准备仓库].\n\n"
                        + (f"上次失败原因:\n{last}" if last else f"工作区: {SYNC_WORKDIR}"))
            return
        if not shutil.which("git"):
            Msg.error("缺少 Git", "未找到 git 命令, 请先安装 Git.")
            return
        ids = session_closure(sids)
        rows = self._rows_map()
        est = sum(self.sizes.get(s, 0) for s in sids)
        names = "\n".join(f"  · {(rows[s][1] or '').strip()[:44]}" for s in sids[:8] if s in rows)
        more = f"\n  ... 等共 {len(sids)} 个" if len(sids) > 8 else ""
        if not Msg.askyesno("上传所选会话",
                            f"将上传 {len(sids)} 个会话(连同子会话共 {len(ids)} 个):\n{names}{more}\n\n"
                            f"本地数据量约 {human_size(est)}\n\n"
                            "会把会话导出成独立会话包并推送到 GitHub 私库, 供其他电脑按需并入. 继续?"):
            return
        host = os.environ.get("COMPUTERNAME", "?")

        def done(m):
            self.status.config(text=f"已上传 {len(m['sessions'])} 个会话"
                                    f" · 包 {human_size(m['size'])}")
            Msg.info("上传完成",
                     f"会话包: {human_size(m['size'])} (压缩后约 {human_size(m['zsize'])})\n"
                     f"含 {len(m['sessions'])} 个会话\n\n"
                     "到另一台电脑[会话同步]页点[刷新远端会话包], 选中后点[下载并并入所选].")

        self.status.config(text="正在导出并上传所选会话...")
        run_bg(lambda: sync_push_sessions(host, sids),
               on_ok=done,
               on_err=lambda e: (self.status.config(text="上传失败"),
                                 Msg.error("上传失败", str(e))))


class PromptDialog(TL):
    """单个会话的详情窗: 会话信息 + 提问与回答(只读).

    列表显示提问和回答的截断预览, 选中一行在下方窗格里看完整问答(按 Markdown 排版),
    双击开大窗. 回答取的是最后一步 finish=stop 的文字; 回合没跑完的会标注"未完成"
    并回退显示过程文字.
    """

    def __init__(self, master, sid, title, directory, meta=None):
        super().__init__(master)
        self.sid = sid
        self.s_title = title or "(无标题)"
        self.s_dir = directory or ""
        meta = meta or {}
        self.items = []
        self.shown = []
        self.title(f"会话详情 — {self.s_title[:40]}")
        self.geometry("1060x700")
        self.minsize(760, 480)
        try:
            self.configure(bg=BG)
        except tk.TclError:
            pass

        bar = Fr(self)
        bar.pack(fill="x", padx=10, pady=(10, 6))
        L(bar, text="搜索:").pack(side="left")
        self.kw = tk.StringVar()
        self.kw.trace_add("write", lambda *a: self.refresh())
        Ent(bar, textvariable=self.kw, width=22).pack(side="left", padx=(4, 10))
        mkbtn(bar, "查看完整内容", self.show_full, "primary").pack(side="left", padx=2)
        mkbtn(bar, "复制提问", lambda: self.copy_part("q")).pack(side="left", padx=2)
        mkbtn(bar, "复制回答", lambda: self.copy_part("a")).pack(side="left", padx=2)
        mkbtn(bar, "复制问答", self.copy_pair).pack(side="left", padx=2)
        mkbtn(bar, "导出...", self.export).pack(side="left", padx=2)
        self.toolbar = bar

        info = " · ".join(str(x) for x in (
            f"目录: {self.s_dir or '-'}",
            f"消息: {meta.get('msgs', '-')}",
            f"占用: {meta.get('size') or '-'}",
            f"更新: {meta.get('updated') or '-'}",
        ))
        self.info = L(self, text=info, style="Muted.TLabel" if not HAS_TB else None,
                      anchor="w", justify="left")
        self.info.pack(fill="x", padx=12, pady=(0, 4))

        pane = tk.PanedWindow(self, orient="vertical", bg=BG, sashwidth=6,
                              bd=0, sashrelief="flat")
        pane.pack(fill="both", expand=True, padx=10, pady=(0, 6))
        top = Fr(pane)
        cols = ("no", "time", "q", "a")
        self.tree = ttk.Treeview(top, columns=cols, show="headings", selectmode="browse")
        fit_columns(self.tree, (("no", "#", "w"), ("time", "时间", "w"),
                                ("q", "提问", "w"), ("a", "回答", "w")),
                    stretch_cols=("q", "a"))
        self.tree.column("q", width=380, minwidth=200)
        self.tree.column("a", width=380, minwidth=200)
        self.tree.tag_configure("odd", background=ODD)
        self.tree.tag_configure("even", background=EVEN)
        self.tree.tag_configure("todo", foreground=MUT)
        vs = ttk.Scrollbar(top, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        vs.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.show_detail())
        self.tree.bind("<Double-1>", lambda e: self.show_full())
        menu = tk.Menu(self, tearoff=0, **_menu_colors())
        menu.add_command(label="查看完整内容", command=self.show_full)
        menu.add_command(label="复制提问", command=lambda: self.copy_part("q"))
        menu.add_command(label="复制回答", command=lambda: self.copy_part("a"))
        menu.add_command(label="复制问答", command=self.copy_pair)
        self.tree.bind("<Button-3>", lambda e: (self.tree.identify_row(e.y)
                     and self.tree.selection_set(self.tree.identify_row(e.y)),
                     menu.tk_popup(e.x_root, e.y_root)))
        pane.add(top, minsize=180)

        low = Fr(pane)
        self.detail = tk.Text(low, wrap="word", height=12, relief="flat", bd=0,
                              bg=CARD, fg=FG, insertbackground=FG,
                              font=(UI_FONT, 10), padx=12, pady=8)
        ds = ttk.Scrollbar(low, orient="vertical", command=self.detail.yview)
        self.detail.configure(yscrollcommand=ds.set)
        ds.pack(side="right", fill="y")
        self.detail.pack(side="left", fill="both", expand=True)
        self.detail.configure(state="disabled")
        pane.add(low, minsize=120)

        self.status = L(self)
        self.status.pack(side="bottom", fill="x", padx=10, pady=(0, 8))
        self.status.config(text="读取提问中...")
        run_bg(lambda: session_prompts(sid), on_ok=self._after_load,
               on_err=self._fail)
        self.bind("<Escape>", lambda e: self.destroy())

    def _alive(self):
        try:
            return bool(self.winfo_exists())
        except tk.TclError:
            return False

    def _after_load(self, items):
        if not self._alive():
            return
        self.items = items or []
        self.refresh()

    def _fail(self, exc):
        if not self._alive():
            return
        self.status.config(text=f"读取提问失败: {exc}")

    def refresh(self):
        kw = self.kw.get().strip().lower()
        t = self.tree
        t.delete(*t.get_children())
        self.shown = []
        n = 0
        first_iid = None
        for i, it in enumerate(self.items):
            if kw and kw not in ((it.get("q") or "") + "\n" + (it.get("a") or "")).lower():
                continue
            n += 1
            tags = ("odd",) if n % 2 else ("even",)
            if it.get("state") != PROMPT_OK:
                tags += ("todo",)
            ans = md_strip(it.get("a") or "").replace("\n", " ")
            if it.get("state") == PROMPT_PARTIAL:
                ans = "未完成 · " + ans
            elif it.get("state") == PROMPT_EMPTY:
                ans = "(无内容)"
            t.insert("", "end", iid=str(i), tags=tags,
                     values=(n, human_time(it.get("time")),
                             md_strip(it.get("q") or "").replace("\n", " ")[:200],
                             ans[:200]))
            self.shown.append(it)
            if first_iid is None:
                first_iid = str(i)
        cnt = {"ok": 0, "partial": 0, "empty": 0}
        for it in self.items:
            cnt[it.get("state") or PROMPT_EMPTY] = cnt.get(it.get("state") or PROMPT_EMPTY, 0) + 1
        if first_iid is not None:
            t.selection_set(first_iid)
            self.show_detail()
        else:
            self._set_detail("")
        if not self.items:
            self.status.config(text="该会话没有用户提问")
            return
        self.status.config(
            text=f"共 {len(self.items)} 条提问 · 完成 {cnt.get('ok', 0)} · "
                 f"未完成 {cnt.get('partial', 0)} · 无内容 {cnt.get('empty', 0)} · "
                 f"命中 {len(self.shown)} 条 · 导出按当前筛选")

    def _sel_item(self):
        sel = self.tree.selection()
        if not sel:
            return None
        try:
            return self.items[int(sel[0])]
        except (ValueError, IndexError):
            return None

    def _set_detail(self, md_text):
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        if md_text:
            render_md(self.detail, md_text)
        self.detail.configure(state="disabled")

    def show_detail(self):
        it = self._sel_item()
        self._set_detail(prompt_pair_md(it) if it else "")

    def show_full(self):
        it = self._sel_item()
        if not it:
            Msg.info("提示", "先在列表里选中一条提问")
            return
        body = prompt_pair_md(it)
        win = TL(self)
        win.title(f"完整内容 — {self.s_title[:30]}")
        win.geometry("900x620")
        win.minsize(560, 380)
        try:
            win.configure(bg=BG)
        except tk.TclError:
            pass
        bar = Fr(win)
        bar.pack(side="bottom", fill="x", padx=10, pady=8)
        mkbtn(bar, "复制", lambda: self._clip(body)).pack(side="left", padx=2)
        mkbtn(bar, "关闭", win.destroy, "primary").pack(side="right", padx=2)
        txt = tk.Text(win, wrap="word", relief="flat", bd=0, bg=CARD, fg=FG,
                      font=(UI_FONT, 10), padx=12, pady=10)
        sb = ttk.Scrollbar(win, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y", padx=(0, 10), pady=10)
        txt.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=10)
        render_md(txt, body)
        txt.configure(state="disabled")
        win.bind("<Escape>", lambda e: win.destroy())

    def _clip(self, text):
        self.clipboard_clear()
        self.clipboard_append(text)

    def copy_part(self, which):
        it = self._sel_item()
        if not it:
            Msg.info("提示", "先在列表里选中一条提问")
            return
        body = (it.get("q") if which == "q" else it.get("a")) or ""
        if not body.strip():
            Msg.info("提示", "这一条没有可复制的内容")
            return
        self._clip(body)
        self.status.config(text=("已复制提问" if which == "q" else "已复制回答")
                                + f" ({len(body)} 字)")

    def copy_pair(self):
        it = self._sel_item()
        if not it:
            Msg.info("提示", "先在列表里选中一条提问")
            return
        self._clip(prompt_pair_md(it))
        self.status.config(text="已复制问答")

    def export(self):
        if not self.shown:
            Msg.info("提示", "没有可导出的提问")
            return
        safe = re.sub(r'[\\/:*?"<>|]', "_", self.s_title)[:40] or "会话"
        path = filedialog.asksaveasfilename(
            parent=self, title="导出提问与回答", defaultextension=".md",
            initialfile=f"{safe}-提问.md",
            filetypes=[("Markdown", "*.md")])
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8-sig") as f:
                f.write(prompts_to_md(self.s_title, self.s_dir, self.shown))
        except OSError as e:
            Msg.error("导出失败", str(e))
            return
        self.status.config(text=f"已导出 {len(self.shown)} 条到 {path}")
        Msg.info("导出完成", f"已导出 {len(self.shown)} 条提问到:\n{path}")


class SyncTab(Fr):
    def __init__(self, master):
        super().__init__(master, padding=(10, 8))
        self.cfg = load_sync_cfg()
        top = Fr(self)
        top.pack(fill="x")
        self.info = L(top, text="", wraplength=980, justify="left")
        self.info.pack(anchor="w")
        rowc = Fr(self)
        rowc.pack(fill="x", pady=4)
        L(rowc, text="私库(HTTPS):").pack(side="left")
        self.url_ent = Ent(rowc, width=38)
        self.url_ent.pack(side="left", padx=4)
        self.url_ent.insert(0, self.cfg.get("repo_url", ""))
        L(rowc, text="PAT:").pack(side="left")
        self.tok_ent = Ent(rowc, width=16, show="*")
        self.tok_ent.pack(side="left", padx=4)
        self.tok_ent.insert(0, self.cfg.get("token", ""))
        mkbtn(rowc, "保存并准备仓库", self.prepare_repo, "primary").pack(side="left", padx=2)
        row2 = Fr(self)
        row2.pack(fill="x", pady=(0, 4))
        mkbtn(row2, "刷新远端会话包", self.show_remote).pack(side="left", padx=2)
        mkbtn(row2, "下载并并入所选", self.do_merge, "primary").pack(side="left", padx=2)
        mkbtn(row2, "刷新统计", self.refresh).pack(side="left", padx=2)
        mkbtn(row2, "压缩数据库(VACUUM)", self.vacuum).pack(side="left", padx=2)
        self.toolbar = rowc
        self.toolbar2 = row2
        L(self, text="上传: 到[会话管理]勾选会话, 点[上传所选会话]导出成独立会话包推送(可多次增量上传).\n"
                     "下载: 点[刷新远端会话包], 选中要并入的包, 再点[下载并并入所选]; "
                     "包内会话会以包内版本覆盖本机同名会话, 其它会话不受影响.\n"
                     "[本机缺] = 该包里本机还没有的会话数, 0 表示本机已全部拥有(无需并入); "
                     "本机自己传的包恒为 0. 项目代码请照常用 Git 手动同步; 私库请确保仅自己可见.",
          foreground=MUT, justify="left").pack(anchor="w")
        cols = ("item", "value", "note")
        self.stats = ttk.Treeview(self, columns=cols, show="headings", height=7, selectmode="none")
        fit_columns(self.stats, (("item", "项目", "w"), ("value", "数值", "w"),
                                 ("note", "说明", "w")), stretch_cols=("note",))
        self.stats.column("item", width=180, minwidth=90)
        self.stats.column("value", width=200, minwidth=90)
        self.stats.column("note", width=420, minwidth=90)
        self.stats.tag_configure("odd", background=ODD)
        self.stats.tag_configure("even", background=EVEN)
        lblrow = Fr(self)
        lblrow.pack(fill="x", pady=(6, 0))
        L(lblrow, text="远端会话包(可多选):").pack(side="left")
        self.only_new = tk.BooleanVar(value=False)
        if HAS_TB:
            self.onlychk = tb.Checkbutton(lblrow, text="只看本机缺的包", variable=self.only_new,
                                          command=self._draw_remote, bootstyle="round-toggle")
        else:
            self.onlychk = tk.Checkbutton(lblrow, text="只看本机缺的包", variable=self.only_new,
                                          command=self._draw_remote, bg=BG, fg=MUT,
                                          activebackground=BG, activeforeground=FG,
                                          selectcolor=CARD, highlightthickness=0, bd=0,
                                          font=(UI_FONT, 9))
        self.onlychk.pack(side="left", padx=(12, 0))
        cols2 = ("time", "host", "count", "size", "new", "file")
        self.tree = ttk.Treeview(self, columns=cols2, show="headings", selectmode="extended")
        fit_columns(self.tree, (("time", "上传时间", "w"), ("host", "来源机器", "w"),
                                ("count", "会话数", "w"), ("size", "包大小", "w"),
                                ("new", "本机缺", "w"), ("file", "会话包", "w")),
                    stretch_cols=("file",))
        self.tree.column("file", width=250, minwidth=58)
        self.tree.tag_configure("odd", background=ODD)
        self.tree.tag_configure("even", background=EVEN)
        # 只有前景色不同, 背景仍由 odd/even 提供(排后面的 tag 只覆盖自己声明的选项)
        self.tree.tag_configure("new", foreground=SUCCESS)
        self.tree.tag_configure("own", foreground=MUT)
        self.tree.tag_configure("old", foreground=MUT)
        self.progress = L(self, text="")
        self.progress.pack(side="bottom", fill="x")
        self.stats.pack(fill="x", pady=(4, 0))
        self.tree.pack(fill="both", expand=True, pady=4)
        self._st = None
        self._remote = None
        self._bundles = []
        self.refresh()

    # ---------- 本地统计 ----------

    def refresh(self):
        self.info.config(text=f"配置文件: {SYNC_CFG_FILE}\n同步工作区: {SYNC_WORKDIR}")
        run_bg(db_stats, on_ok=self._fill_local,
               on_err=lambda e: self._fill_local(
                   {"db": 0, "wal": 0, "sessions": 0, "messages": 0, "parts": 0, "zdb": 0}))

    def _fill_local(self, st):
        self._st = st
        self._render()

    def _render(self):
        st = self._st or {"db": 0, "wal": 0, "sessions": 0, "messages": 0, "parts": 0, "zdb": 0}
        rows = [
            ("本地会话库", human_size(st["db"]), DB if st["db"] else "未找到会话数据库"),
            ("WAL 日志", human_size(st["wal"]), "checkpoint 后归零"),
            ("压缩后估算", "约 " + human_size(st["zdb"]), "整库上传的参考值(会话包按所选会话另行计算)"),
            ("会话 / 消息 / 数据块",
             f"{st['sessions']} / {st['messages']} / {st['parts']}", "顶层会话与关联消息统计"),
        ]
        warn = st["zdb"] > DB_WARN_MB << 20
        limit = st["zdb"] > DB_LIMIT_MB << 20
        rows.append(("整库体积状态",
                     "超限, 无法整库上传" if limit else ("偏大, 注意清理" if warn else "正常"),
                     f"预警线 {DB_WARN_MB}MB, 上限 {DB_LIMIT_MB}MB (按压缩后估算, GitHub 单文件硬限 100MB)"))
        url_cfg = (self.cfg.get("repo_url") or "").strip()
        if not url_cfg:
            rows.append(("同步工作区", "未配置", "先在上面填私库地址和 PAT, 再点[保存并准备仓库]"))
        elif repo_ready():
            last = sync_last_error()
            rows.append(("同步工作区", "就绪(上次失败)" if last else "就绪",
                         last.replace("\n", "  ") if last else SYNC_WORKDIR))
        else:
            last = sync_last_error()
            rows.append(("同步工作区", "未准备",
                         (last.replace("\n", "  ") if last else
                          f"点[保存并准备仓库]完成克隆 ({SYNC_WORKDIR})")))
        r = self._remote
        if r:
            mb = r.get("repo_mb")
            rows.append(("远端最近上传", (r.get("commit") or "从未上传"),
                         "origin 分支最新提交" if r.get("commit") else "还没有任何会话包"))
            rows.append(("远端会话包", f"{len(r['bundles'])} 个",
                         "选中后可并入本机; 每个包含上传时勾选的会话"))
            rows.append(("远端仓库总量", (f"约 {mb:.1f} MB" if mb is not None else "-"),
                         "含全部历史提交, 可到 GitHub 清理历史压缩体积"))
        t = self.stats
        t.delete(*t.get_children())
        for i, row in enumerate(rows):
            t.insert("", "end", iid="L" + str(i), tags=("odd",) if i % 2 else ("even",), values=row)

    # ---------- 配置与准备 ----------

    def prepare_repo(self):
        url = _normalize_url(self.url_ent.get())
        if not url:
            Msg.warning("缺少配置", "请先填写 GitHub 私库地址, 例如:\n"
                        "you/opencode-sync   或   https://github.com/you/opencode-sync.git\n"
                        "默认按 HTTPS 方式访问.")
            return
        if not shutil.which("git"):
            Msg.error("缺少 Git", "未找到 git 命令, 请先安装 Git.")
            return
        self.url_ent.delete(0, "end")
        self.url_ent.insert(0, url)
        self.cfg = {"repo_url": url, "token": self.tok_ent.get().strip()}
        save_sync_cfg(self.cfg)
        self.progress.config(text="正在检查仓库与权限...")
        run_bg(lambda: repo_prepare(url, self.cfg.get("token")),
               on_ok=lambda _: (self.progress.config(text="仓库就绪"), self.refresh(),
                                Msg.info("仓库就绪", f"同步工作区已就绪:\n{url}\n\n"
                                                     "可以到[会话管理]勾选会话上传, 或点[刷新远端会话包]查看远端.")),
               on_err=self._prepare_failed)

    def _prepare_failed(self, e):
        self.progress.config(text="准备失败, 详见下方[同步工作区]")
        self.refresh()
        Msg.error("准备失败", str(e))

    def _need_ready(self):
        if not (self.cfg.get("repo_url") or "").strip():
            Msg.warning("缺少配置", "请先填写私库地址并点[保存并准备仓库].")
            return False
        if not repo_ready():
            last = sync_last_error()
            Msg.warning("同步工作区未就绪",
                        "请先点[保存并准备仓库]完成克隆.\n\n"
                        + (f"上次失败原因:\n{last}" if last else f"工作区: {SYNC_WORKDIR}"))
            return False
        return True

    # ---------- 远端会话包 ----------

    def show_remote(self):
        if not self._need_ready():
            return
        self.progress.config(text="正在拉取远端会话包列表...")
        run_bg(sync_remote, on_ok=self._fill_remote,
               on_err=lambda e: (self.progress.config(text=""), Msg.error("刷新失败", str(e))))

    def _fill_remote(self, r):
        self.progress.config(text="")
        self._remote = r
        self._bundles = r.get("bundles") or []
        self._draw_remote()
        self._render()
        if not self._bundles:
            self.progress.config(text="远端还没有会话包, 请先在另一台电脑上传所选会话")

    def _draw_remote(self):
        """按[只看本机缺的包]过滤后重画远端列表."""
        t = self.tree
        t.delete(*t.get_children())
        host = os.environ.get("COMPUTERNAME", "?")
        shown = [b for b in self._bundles if not self.only_new.get() or b["new"] > 0]
        for i, b in enumerate(shown):
            t.insert("", "end", iid=b["file"], tags=("odd" if i % 2 else "even", bundle_flags(b, host)),
                     values=(b["time"], b["host"], b["count"], human_size(b["size"]),
                             b["new"], b["file"]))
        if not self._bundles:
            self.progress.config(text="")
            return
        miss = sum(1 for b in self._bundles if b["new"] > 0)
        hidden = len(self._bundles) - len(shown)
        self.progress.config(
            text=f"远端共 {len(self._bundles)} 个会话包, 其中 {miss} 个含本机缺少的会话"
                 + (f"; 已按[只看本机缺的包]隐藏 {hidden} 个" if hidden else ""))

    # ---------- 压缩数据库 ----------

    def vacuum(self):
        if opencode_running():
            Msg.warning("opencode 正在运行", "请在全部 opencode 退出后再压缩数据库.")
            return
        cur = (self._st or {}).get("db", 0)
        if not Msg.askyesno("压缩数据库",
                            f"当前库 {human_size(cur)}.\n\n"
                            "VACUUM 会重建数据库文件, 回收删除会话后残留的空闲页,\n"
                            "期间需要约两倍磁盘空间, 且必须已退出全部 opencode.\n继续?"):
            return
        self.progress.config(text="正在压缩数据库(可能需要一两分钟)...")
        run_bg(vacuum_db, on_ok=self._vacuumed,
               on_err=lambda e: (self.progress.config(text=""), Msg.error("压缩失败", str(e))))

    def _vacuumed(self, r):
        before, after = r
        self.refresh()
        self.progress.config(text=f"压缩完成: {human_size(before)} → {human_size(after)}")
        Msg.info("压缩完成", f"库体积 {human_size(before)} → {human_size(after)}.\n"
                 "请到[会话管理]按 F5 确认会话数据仍在.")

    # ---------- 下载并并入所选 ----------

    def do_merge(self):
        if not self._need_ready():
            return
        sel = self.tree.selection()
        if not sel:
            Msg.info("提示", "先点[刷新远端会话包], 再选中要并入的会话包(可多选)")
            return
        if opencode_running():
            Msg.warning("opencode 正在运行", "并入会话库前请先退出本机全部 opencode 窗口.")
            return
        picks = [b for b in self._bundles if b["file"] in set(sel)]
        n = sum(b["count"] for b in picks)
        over = sum(b["count"] - b["new"] for b in picks)
        detail = "\n".join(f"  · {b['time']} [{b['host']}] {b['count']} 个会话"
                           for b in picks[:6])
        more = f"\n  ... 共 {len(picks)} 个包" if len(picks) > 6 else ""
        dirs = sorted({d for b in picks for d in b["dirs"]})
        if over == n and n:
            miss = sum(b["new"] for b in picks)
            if not Msg.askyesno(
                    "无需并入",
                    f"选中的 {len(picks)} 个包共 {n} 个会话, 本机都已存在(缺 {miss} 个).\n\n"
                    "并入只会用包内版本覆盖本机同名会话, 不会新增任何会话.\n"
                    "如果只是想拿到其它电脑上的新对话, 请选[本机缺]大于 0 的包.\n\n仍要并入?"):
                return
        elif not Msg.askyesno("下载并并入",
                              f"将并入 {len(picks)} 个会话包, 共 {n} 个会话:\n{detail}{more}\n\n"
                              f"其中 {over} 个本机已存在, 会被包内版本覆盖; "
                              f"{n - over} 个是本机缺少的; 其它会话不受影响.\n"
                              f"涉及目录: {', '.join(dirs[:3]) or '-'}"
                              f"{' 等' if len(dirs) > 3 else ''}\n"
                              "项目代码请自行在对应目录 clone/pull. 继续?"):
            return
        self.progress.config(text="正在并入会话包...")
        run_bg(lambda: sync_merge_bundles([b["path"] for b in picks]),
               on_ok=self._merged,
               on_err=lambda e: (self.progress.config(text=""), Msg.error("并入失败", str(e))))

    def _merged(self, r):
        self.progress.config(text="")
        if app_ref:
            app_ref.tab_sessions.load()
        run_bg(sync_remote, on_ok=self._fill_remote, on_err=lambda e: None)
        Msg.info("并入完成",
                 f"新增 {len(r['added'])} 个会话, 覆盖 {len(r['replaced'])} 个.\n"
                 f"来源机器: {', '.join(r['hosts']) or '-'}\n\n"
                 "到[会话管理]按 F5 查看; 若提示目录不存在, 先进目录 clone/pull 项目代码.")


class BrowseTab(Fr):
    """工作目录: 列出会话用过的工作目录, 大小/修改时间后台补齐, 一键进入."""

    COLS = (("name", "目录名称", "w"), ("sessions", "会话", "e"),
            ("mtime", "最后修改", "w"), ("size", "大小", "e"),
            ("path", "绝对路径", "w"))

    def __init__(self, master):
        super().__init__(master, padding=(10, 8))
        bar = Fr(self)
        bar.pack(fill="x")
        L(bar, text="搜索:").pack(side="left")
        self.kw = tk.StringVar()
        self.kw.trace_add("write", lambda *a: self.refresh())
        Ent(bar, textvariable=self.kw, width=22).pack(side="left", padx=(4, 10))
        mkbtn(bar, "打开目录", self.reveal, "primary").pack(side="left", padx=2)
        mkbtn(bar, "启动 opencode", self.open_opencode).pack(side="left", padx=2)
        mkbtn(bar, "开终端", self.open_terminal).pack(side="left", padx=2)
        mkbtn(bar, "复制路径", self.copy_path).pack(side="left", padx=2)
        mkbtn(bar, "刷新", self.load).pack(side="left", padx=2)
        self.toolbar = bar

        self.tree = ttk.Treeview(self, columns=[c for c, _t, _a in self.COLS],
                                 show="headings", selectmode="extended")
        fit_columns(self.tree, self.COLS, stretch_cols=("path",),
                    content_cols={"mtime": ["2026-09-20 17:34"], "size": ["999.9GB"]},
                    caps={"name": 280, "mtime": 210, "size": 110})
        self.tree.column("path", minwidth=200)
        self.tree.tag_configure("odd", background=ODD)
        self.tree.tag_configure("even", background=EVEN)
        self.tree.tag_configure("miss", foreground=MUT)
        for cid, label, _a in self.COLS:
            self.tree.heading(cid, text=label, command=lambda c=cid: self.sort_by(c))
        vs = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        self.status = L(self)
        self.status.pack(side="bottom", fill="x")
        self.hint = L(self, text="双击=在资源管理器中打开  右键=启动 opencode / 开终端 / 筛选会话  "
                                 "· 大小与修改时间由后台统计, 表头可点击排序", wraplength=900)
        if HAS_TB:
            self.hint.configure(style="Muted.TLabel")
        self.hint.pack(side="bottom", fill="x")
        vs.pack(side="right", fill="y", pady=4)
        self.tree.pack(side="left", fill="both", expand=True, pady=4, padx=(0, 2))
        self.tree.bind("<Double-1>", lambda e: self.reveal())
        menu = tk.Menu(self, tearoff=0, **_menu_colors())
        menu.add_command(label="资源管理器中打开", command=self.reveal)
        menu.add_command(label="启动 opencode", command=self.open_opencode)
        menu.add_command(label="开终端", command=self.open_terminal)
        menu.add_command(label="复制完整路径", command=self.copy_path)
        menu.add_command(label="在会话管理中筛选", command=self.filter_sessions)
        self.tree.bind("<Button-3>", lambda e: (self.tree.identify_row(e.y)
                     and self.tree.selection_set(self.tree.identify_row(e.y)),
                     menu.tk_popup(e.x_root, e.y_root)))
        self.toolbar2 = self.hint
        self.rows = []
        self.stats = {}
        self.sort_key = "mtime"
        self.sort_desc = True
        self._loading = False
        self.apply_col_widths()

    def apply_col_widths(self):
        """目录名称/会话/最后修改/大小 按"表头与全量内容里更宽的"取宽, 路径吃剩余宽度.

        样本只来自 self.rows/self.stats(全量, 不是可见行), 所以筛选时列宽不跳动.
        """
        rows = [d for d in self.rows if d["exists"]]
        names = [d["name"] for d in rows] or [""]
        mtimes = [dt.datetime.fromtimestamp(self.stats[d["path"]][2]).strftime("%Y-%m-%d %H:%M")
                  for d in rows if self.stats.get(d["path"], (0, 0, 0))[2]] or ["2026-09-20 17:34"]
        sizes = [human_size(self.stats[d["path"]][0])
                 for d in rows if self.stats.get(d["path"], (0, 0, 0))[0]] or ["999.9GB"]
        fit_columns(self.tree, self.COLS, stretch_cols=("path",),
                    content_cols={"name": names, "mtime": mtimes, "size": sizes},
                    caps={"name": 280, "mtime": 210, "size": 110})

    def load(self):
        if self._loading:
            return
        self._loading = True
        self.status.config(text="读取工作目录中...")

        def work():
            hidden = session_dirs(hide_missing=False)
            return [d for d in hidden if d["exists"]], sum(1 for d in hidden if not d["exists"])

        def ok(res):
            self._loading = False
            dirs, missing = res
            self.rows = dirs
            self.missing = missing
            self.stats = {}
            self.apply_col_widths()
            self.refresh()
            self.status.config(text=f"统计大小与修改时间中… (共 {len(dirs)} 个目录)")
            paths = [d["path"] for d in dirs if d["exists"]]
            run_bg(lambda: dirs_stats(paths), on_ok=self._after_stats,
                   on_err=lambda e: self.status.config(text=f"统计失败: {e}"))

        run_bg(work, on_ok=ok, on_err=lambda e: self._fail(e))

    def _fail(self, e):
        self._loading = False
        self.status.config(text=f"读取工作目录失败: {e}")

    def _after_stats(self, st):
        if not self._alive():
            return
        self.stats = st or {}
        self.apply_col_widths()
        self.refresh()

    def _alive(self):
        try:
            return bool(self.winfo_exists())
        except tk.TclError:
            return False

    def sort_by(self, col):
        if self.sort_key == col:
            self.sort_desc = not self.sort_desc
        else:
            self.sort_key, self.sort_desc = col, col != "name"
        self.refresh()

    def _sorted_rows(self):
        key = self.sort_key

        def val(d):
            if key == "name":
                return d["name"].lower()
            if key == "sessions":
                return d["sessions"]
            if key == "path":
                return d["path"].lower()
            if key == "size":
                return self.stats.get(d["path"], (0, 0, 0))[0]
            return self.stats.get(d["path"], (0, 0, 0))[2]

        rows = [d for d in self.rows if d["exists"]]
        rows.sort(key=val, reverse=self.sort_desc)
        return rows

    def refresh(self):
        kw = self.kw.get().strip().lower()
        t = self.tree
        keep = set(t.selection())
        t.delete(*t.get_children())
        n = 0
        total = 0
        counted = 0
        for d in self._sorted_rows():
            if kw and kw not in f"{d['name']} {d['path']}".lower():
                continue
            b, _files, newest = self.stats.get(d["path"], (0, 0, 0))
            if d["path"] in self.stats:
                total += b
                counted += 1
            tags = ("odd",) if n % 2 else ("even",)
            if not d["exists"]:
                tags += ("miss",)
            t.insert("", "end", iid=d["path"], tags=tags,
                     values=(d["name"], d["sessions"],
                             dt.datetime.fromtimestamp(newest).strftime("%Y-%m-%d %H:%M")
                             if newest else "-",
                             human_size(b) if d["path"] in self.stats else "-",
                             d["path"]))
            n += 1
        for k in keep:
            if t.exists(k):
                try:
                    t.selection_add(k)
                except Exception:
                    pass
        extra = f" · 已隐藏 {getattr(self, 'missing', 0)} 个不存在的目录" if getattr(self, "missing", 0) else ""
        cnt = f" · 合计 {human_size(total)}" if counted else ""
        self.status.config(text=f"显示 {n} 个目录{cnt}{extra} · 双击进入, 右键更多操作")

    def _sel_path(self):
        s = self.tree.selection()
        return s[0] if s else None

    def _sel_win(self):
        p = self._sel_path()
        return p.replace("/", os.sep) if p else None

    def reveal(self):
        p = self._sel_win()
        if not p:
            Msg.info("提示", "先在列表中选中一个目录")
            return
        if not os.path.isdir(p):
            Msg.info("提示", f"目录不存在:\n{p}")
            return
        subprocess.Popen(["explorer", p], env=child_env())

    def open_opencode(self):
        p = self._sel_win()
        if not p:
            Msg.info("提示", "先在列表中选中一个目录")
            return
        if not os.path.isdir(p):
            Msg.info("提示", f"目录不存在:\n{p}")
            return
        exe = find_opencode_exe()
        if not exe:
            Msg.error("错误", "找不到 opencode 可执行文件")
            return
        subprocess.Popen([exe], cwd=p, env=child_env(), creationflags=CREATE_NEW_CONSOLE)

    def open_terminal(self):
        p = self._sel_win()
        if not p:
            Msg.info("提示", "先在列表中选中一个目录")
            return
        if not os.path.isdir(p):
            Msg.info("提示", f"目录不存在:\n{p}")
            return
        subprocess.Popen(["cmd"], cwd=p, env=child_env(), creationflags=CREATE_NEW_CONSOLE)

    def copy_path(self):
        p = self._sel_win()
        if p:
            self.clipboard_clear()
            self.clipboard_append(p)
            self.status.config(text=f"已复制路径: {p}")

    def filter_sessions(self):
        p = self._sel_path()
        if not p:
            Msg.info("提示", "先在列表中选中一个目录")
            return
        if app_ref is None:
            return
        app_ref.show("sessions")
        app_ref.tab_sessions.filter_dir(p)


_AppBase = tb.Window if HAS_TB else tk.Tk
NAV_ITEMS = (("会话管理", "sessions"), ("会话同步", "sync"), ("工作目录", "browse"))


class App(_AppBase):
    def __init__(self, tab=0):
        global app_ref
        self.theme_name = load_theme()
        try:
            import ctypes
            user32 = ctypes.windll.user32
            sw, sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        except Exception:
            sw, sh = 1920, 1080
        w = min(1280, int(sw * 0.78))
        h = min(880, int(sh * 0.82))
        px, py = (sw - w) // 2, (sh - h) // 2 - 20
        ico = _app_icon_path()
        has_ico = os.path.isfile(ico)
        if HAS_TB:
            # 必须显式传入 app.ico: tb.Window 默认 iconphoto='' 会在构造时应用
            # ttkbootstrap 品牌图标, 覆盖窗口左上角的应用图标.
            # iconphoto=None 表示"不动图标", 交给下面的 iconbitmap 兜底.
            super().__init__(title="opencode 会话与共享管理",
                             themename="darkly" if self.theme_name == "dark" else "flatly",
                             size=(w, h), position=(px, py),
                             iconphoto=ico if has_ico else None)
        else:
            super().__init__()
            self.title("opencode 会话与共享管理")
            self.geometry(f"{w}x{h}+{px}+{py}")
        # tb.Window 构造中会把 AUMID 覆盖成 "ttkbootstrap.app", 任务栏分组/图标
        # 会漂移, 必须在其之后再设置我们自己的应用标识.
        _set_app_identity()
        app_ref = self
        self.after(120, _bg_poll)
        setup_style(self, self.theme_name)
        self.minsize(820, 520)
        try:
            if has_ico:
                self.iconbitmap(default=ico)
        except tk.TclError:
            pass
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        nav = tk.Frame(self, bg=SIDEBAR, width=232)
        nav.grid(row=0, column=0, sticky="ns")
        nav.grid_propagate(False)
        brand = tk.Frame(nav, bg=SIDEBAR)
        brand.pack(fill="x", padx=18, pady=(22, 28))
        tk.Label(brand, text="O", bg=ACCENT, fg="#ffffff", width=2,
                 font=(UI_FONT, 16, "bold"), padx=4, pady=2).pack(side="left")
        brand_text = tk.Frame(brand, bg=SIDEBAR)
        brand_text.pack(side="left", padx=(10, 0))
        tk.Label(brand_text, text="opencode", bg=SIDEBAR, fg=FG,
                 font=(UI_FONT, 14, "bold")).pack(anchor="w")
        tk.Label(brand_text, text="会话与工作空间", bg=SIDEBAR, fg=MUT,
                 font=(UI_FONT, 8)).pack(anchor="w")
        tk.Label(nav, text="WORKSPACE", bg=SIDEBAR, fg="#5f6d80",
                 font=(UI_FONT, 8, "bold"), anchor="w").pack(fill="x", padx=23, pady=(0, 8))
        self._navlbls = {}
        for text, key in NAV_ITEMS:
            lbl = tk.Label(nav, text="  " + text, bg=SIDEBAR, fg=MUT, anchor="w",
                           font=(UI_FONT, 10), padx=22, pady=11, cursor="hand2")
            lbl.pack(fill="x")
            lbl.bind("<Button-1>", lambda e, k=key: self.show(k))
            lbl.bind("<Enter>", lambda e, k=key: self._nav_hover(k, True))
            lbl.bind("<Leave>", lambda e, k=key: self._nav_hover(k, False))
            self._navlbls[key] = lbl
        status_card = tk.Frame(nav, bg=CARD, highlightthickness=1,
                               highlightbackground=GRID)
        status_card.pack(side="bottom", fill="x", padx=14, pady=(8, 18))
        tk.Label(status_card, text="SYSTEM STATUS", bg=CARD, fg="#6f7d90",
                 font=(UI_FONT, 8, "bold"), anchor="w").pack(fill="x", padx=12, pady=(10, 4))
        self.badge_sync = tk.Label(status_card, text="●  同步: -", bg=CARD, fg=MUT, anchor="w",
                                   font=(UI_FONT, 9), padx=12)
        self.badge_host = tk.Label(status_card, text="●  主机: -", bg=CARD, fg=MUT, anchor="w",
                                   font=(UI_FONT, 9), padx=12)
        self.badge_run = tk.Label(status_card, text="●  运行: -", bg=CARD, fg=MUT, anchor="w",
                                  font=(UI_FONT, 9), padx=12)
        for bdg in (self.badge_run, self.badge_host, self.badge_sync):
            bdg.pack(fill="x", pady=2)

        content = tk.Frame(self, bg=BG)
        content.grid(row=0, column=1, sticky="nsew")
        self.content = content
        content.rowconfigure(1, weight=1)
        content.columnconfigure(0, weight=1)
        self.page_header = tk.Frame(content, bg=BG, height=104)
        self.page_header.grid(row=0, column=0, sticky="ew")
        self.page_header.grid_propagate(False)
        header_line = tk.Frame(self.page_header, bg=BG)
        header_line.pack(fill="x", padx=24, pady=(18, 0))
        self.page_title = tk.Label(header_line, text="", bg=BG, fg=FG,
                                   font=(UI_FONT, 20, "bold"))
        self.page_title.pack(side="left")
        self.machine_badge = tk.Label(header_line, text="LOCAL WORKSPACE", bg=CARD, fg=MUT,
                                      font=(UI_FONT, 8, "bold"), padx=10, pady=5)
        self.machine_badge.pack(side="right", pady=3)
        self.page_subtitle = tk.Label(self.page_header, text="", bg=BG, fg=MUT,
                                      font=(UI_FONT, 9))
        self.page_subtitle.pack(anchor="w", padx=25, pady=(2, 0))
        self.theme_button = tk.Button(header_line,
                                      text="☀ 浅色" if self.theme_name == "dark" else "☾ 深色",
                                      command=self.toggle_theme, bg=CARD, fg=MUT,
                                      activebackground=SEL, activeforeground=FG,
                                      relief="flat", bd=0, padx=10, pady=5,
                                      font=(UI_FONT, 9), cursor="hand2")
        self.theme_button.pack(side="right", padx=(0, 10), pady=3)
        tk.Frame(content, bg=GRID, height=1).grid(row=0, column=0, sticky="sew")
        page_host = tk.Frame(content, bg=BG)
        page_host.grid(row=1, column=0, sticky="nsew")
        self.tab_sessions = SessionsTab(page_host)
        self.tab_sync = SyncTab(page_host)
        self.tab_browse = BrowseTab(page_host)
        self.pages = {"sessions": self.tab_sessions, "sync": self.tab_sync,
                      "browse": self.tab_browse}
        self.page_meta = {
            "sessions": ("会话管理", "跨目录查看、进入和维护所有 opencode 历史会话"),
            "sync": ("会话同步", "勾选的会话导出成独立会话包, 经 GitHub 私库(HTTPS)增量同步; 项目代码请用 Git 自行同步"),
            "browse": ("工作目录", "会话用过的工作目录: 大小、最后修改时间, 一键进入/开终端/启动 opencode"),
        }
        self._cur = None
        self._wrappers = (self.tab_sync.info, self.tab_browse.hint, self.tab_sessions.status)
        self.bind("<Configure>", self._on_resize, add="+")
        keys = [k for _t, k in NAV_ITEMS]
        self.show(keys[tab] if 0 <= tab < len(keys) else "sessions")
        self.bind("<F5>", lambda e: self.refresh_current())
        self._nav_job = self.after(6000, self._tick)
        self._geometry_job = None
        self._last_geometry = None
        saved = load_window_geometry()
        if saved:
            self._apply_saved_geometry(saved, sw, sh)
        self.after(180, self._fit_window_to_content)
        self.after(600, self._remember_geometry)

    def _fit_window_to_content(self):
        """启动时只需保证工具栏一行完整可见; minsize 保持较小值, 窗口仍可自由缩小."""
        try:
            self.update_idletasks()
            sw = self.winfo_screenwidth()
            need = 0
            for page in (self.tab_sessions, self.tab_sync, self.tab_browse):
                for attr in ("toolbar", "toolbar2"):
                    t = getattr(page, attr, None)
                    if t is not None:
                        need = max(need, t.winfo_reqwidth())
            target = min(sw - 32, need + 232 + 48)
            if target > self.winfo_width():
                x = max(0, (sw - target) // 2)
                self.geometry(f"{target}x{self.winfo_height()}+{x}+{self.winfo_y()}")
            self.minsize(820, 520)
            self._remember_geometry()
        except tk.TclError:
            pass

    def _apply_saved_geometry(self, saved, sw, sh):
        width, height, x, y = saved
        width = max(820, min(width, sw - 32))
        height = max(520, min(height, sh - 80))
        x = max(0, min(x, sw - width))
        y = max(0, min(y, sh - height))
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _remember_geometry(self):
        try:
            if self.state() == "normal":
                current = (self.winfo_width(), self.winfo_height(),
                           self.winfo_x(), self.winfo_y())
                if current != self._last_geometry:
                    self._last_geometry = current
                    save_window_geometry(*current)
            self._geometry_job = self.after(1000, self._remember_geometry)
        except tk.TclError:
            pass

    def show(self, key):
        if self._cur:
            self.pages[self._cur].pack_forget()
        self.pages[key].pack(fill="both", expand=True, padx=16, pady=(4, 16))
        self._cur = key
        title, subtitle = self.page_meta[key]
        self.page_title.config(text=title)
        self.page_subtitle.config(text=subtitle)
        for k, lbl in self._navlbls.items():
            if k == key:
                lbl.config(bg=_shift(SIDEBAR, 18), fg=FG, font=(UI_FONT, 10, "bold"))
            else:
                lbl.config(bg=SIDEBAR, fg=MUT, font=(UI_FONT, 10))
        if key == "sessions" and not self.tab_sessions.rows:
            self.tab_sessions.load()
        elif key == "browse" and not self.tab_browse.rows:
            self.tab_browse.load()

    def refresh_current(self):
        """F5: 刷新当前页."""
        if self._cur == "sync":
            self.tab_sync.show_remote()
        elif self._cur == "browse":
            self.tab_browse.load()
        else:
            self.tab_sessions.load()

    def _nav_hover(self, key, enter):
        lbl = self._navlbls[key]
        if key == self._cur:
            return
        lbl.config(bg=_shift(SIDEBAR, 10) if enter else SIDEBAR)

    def toggle_theme(self):
        """Persist the choice and restart the process so every Tk widget changes theme consistently."""
        save_theme("light" if self.theme_name == "dark" else "dark")
        self._relaunch()

    def _relaunch(self):
        # PyInstaller onefile 引导器靠 _PYI_*/_MEIPASS2 环境变量向子进程传递解压目录.
        # 自重启时若原样继承, 新实例会跳过解压、复用旧进程正在被清理的 _MEI 临时目录,
        # 随即在 Tcl 初始化时报 "Can't find a usable init.tcl". 重启前必须剥离.
        env = child_env()
        if _is_frozen():
            args = [sys.executable] + sys.argv[1:]
        else:
            args = [sys.executable, os.path.abspath(__file__)] + sys.argv[1:]
        # 无控制台进程里标准句柄无效, 必须显式 DEVNULL; os.execv 在
        # onefile/无窗口 exe 上不可靠, 改用 Popen 新进程 + 销毁本进程.
        try:
            subprocess.Popen(args, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             close_fds=True, env=env, creationflags=CREATE_NO_WINDOW)
            self.destroy()
        except Exception:
            os.execv(args[0], args)

    def _tick(self):
        sids = [r[0] for r in self.tab_sessions.rows]

        def work():
            cfg = load_sync_cfg()
            return (session_status_map(sids) if sids else {}, read_marker(),
                    bool((cfg.get("repo_url") or "").strip()), repo_ready())

        def done(res):
            smap, marker, has_cfg, ready = res
            runc = sum(1 for v in smap.values() if v[1] == "run")
            actc = sum(1 for v in smap.values() if v[1] == "act")
            self.badge_run.config(text=f"运行: {runc}  近期: {actc}",
                                  fg=C_SUCCESS if runc else (C_WARNING if actc else MUT))
            me = os.environ.get("COMPUTERNAME", "?")
            self.badge_host.config(text=f"主机: {marker or '-'}" + (" (本机)" if marker == me else ""))
            summ = "未配置" if not has_cfg else ("就绪" if ready else "未准备")
            self.badge_sync.config(text=f"会话同步: {summ}",
                                   fg=C_SUCCESS if summ == "就绪" else
                                   (C_WARNING if summ == "未准备" else C_DANGER))
            if self._cur == "sessions":
                self.tab_sessions.apply_status(smap)
            self._nav_job = self.after(6000, self._tick)

        def err(_e):
            self.badge_sync.config(text="会话同步: 未知", fg=MUT)
            self._nav_job = self.after(6000, self._tick)
        run_bg(work, on_ok=done, on_err=err)

    def _on_resize(self, e):
        if e.widget is self:
            wl = max(320, self.content.winfo_width() - 40)
            for wdg in self._wrappers:
                try:
                    wdg.configure(wraplength=wl)
                except tk.TclError:
                    pass


def selftest():
    print("== 1. 会话查询 ==")
    rows = list_sessions()
    print(f"   顶层会话 {len(rows)} 条, 示例: {(rows[0][1] or '')[:20] if rows else '无'}")
    sizes = session_size_map()
    top = sorted(sizes.items(), key=lambda kv: -kv[1])[:3]
    print(f"   会话合计 {human_size(sum(sizes.values()))}; 最大会话: "
          + (", ".join(f"{sid[:16]}... {human_size(b)}" for sid, b in top) if top else "无"))
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
    print("== 3. 会话库统计与同步配置 ==")
    stt = db_stats()
    print(f"   库 {human_size(stt['db'])} (WAL {human_size(stt['wal'])})  "
          f"压缩估算 {human_size(stt['zdb'])}  "
          f"会话 {stt['sessions']}/消息 {stt['messages']}/数据块 {stt['parts']}")
    cfg = load_sync_cfg()
    print(f"   私库: {'已配置 ' + cfg.get('repo_url', '') if cfg.get('repo_url') else '未配置'}"
          f"  工作区就绪: {repo_ready()}  git 可用: {bool(shutil.which('git'))}")
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
    print("== 6. 会话包导出/并入 往返 ==")
    import tempfile
    tmp = tempfile.gettempdir()
    src_db = os.path.join(tmp, "ocp-st-src.db")
    dst_db = os.path.join(tmp, "ocp-st-dst.db")
    pkg = os.path.join(tmp, "ocp-st-bundle.db")
    for p in (src_db, dst_db, pkg):
        if os.path.isfile(p):
            os.remove(p)
    schema = [r[0] for r in db_ro().execute(
        "select sql from sqlite_master where type='table' and sql is not null")]
    for p in (src_db, dst_db):
        c = sqlite3.connect(p)
        for sql in schema:
            try:
                c.execute(sql)
            except Exception:
                pass
        c.commit()
        c.close()
    c = sqlite3.connect(src_db)
    c.execute("insert into project (id,worktree,sandboxes,time_created,time_updated) "
              "values ('p1','/x','[]',1,1)")
    c.execute("insert into session (id,project_id,slug,directory,title,version,time_created,time_updated) "
              "values ('s1','p1','a','/x','t1','v',1,1)")
    c.execute("insert into session (id,project_id,parent_id,slug,directory,title,version,"
              "time_created,time_updated) values ('s2','p1','s1','b','/x','sub','v',1,1)")
    c.execute("insert into message (id,session_id,time_created,time_updated,data) values ('m1','s1',1,1,'{}')")
    c.execute("insert into part (id,message_id,session_id,time_created,time_updated,data) "
              "values ('pa1','m1','s1',1,1,'{}')")
    c.execute("insert into todo (session_id,content,status,priority,position,time_created,time_updated) "
              "values ('s1','x','pending','high',0,1,1)")
    c.execute("insert into event_sequence (aggregate_id,seq) values ('s1',1)")
    c.execute("insert into event (id,aggregate_id,seq,type,data) values ('e1','s1',1,'t','{}')")
    c.commit()
    c.close()
    m = export_bundle(["s1"], pkg, src_path=src_db)
    print(f"   导出 1 个顶层会话 -> 包内 {len(m['sessions'])} 个会话(含子会话), "
          f"包 {human_size(m['size'])} (压缩约 {human_size(m['zsize'])})")
    assert len(m["sessions"]) == 2, "子会话未随包导出!"
    c = sqlite3.connect(dst_db)
    c.execute("insert into project (id,worktree,sandboxes,time_created,time_updated) "
              "values ('p9','/y','[]',1,1)")
    c.execute("insert into session (id,project_id,slug,directory,title,version,time_created,time_updated) "
              "values ('s1','p9','z','/z','LOCAL','v',1,1)")
    c.execute("insert into session (id,project_id,slug,directory,title,version,time_created,time_updated) "
              "values ('keep','p9','k','/z','keep','v',1,1)")
    c.commit()
    c.close()
    r1 = sync_merge_bundles([pkg], dst_path=dst_db)
    print(f"   首次并入: 新增 {len(r1['added'])} 覆盖 {len(r1['replaced'])} (来源 {r1['hosts']})")
    r2 = sync_merge_bundles([pkg], dst_path=dst_db)
    print(f"   重复并入: 新增 {len(r2['added'])} 覆盖 {len(r2['replaced'])} (应幂等)")
    c = sqlite3.connect(dst_db)
    cnt = {t: c.execute(f"select count(*) from {t}").fetchone()[0]
           for t in ("session", "message", "part", "todo", "event", "event_sequence")}
    title = c.execute("select title, project_id from session where id='s1'").fetchone()
    keep = c.execute("select count(*) from session where id='keep'").fetchone()[0]
    c.close()
    print(f"   并入后统计: {cnt}; s1 标题/项目={title}; 本地独有会话保留={bool(keep)}")
    assert cnt["session"] == 3, "并入后会话数不对(应为 s1+s2+keep)"
    assert cnt["message"] == 1 and cnt["part"] == 1 and cnt["todo"] == 1
    assert cnt["event"] == 1 and cnt["event_sequence"] == 1, "事件重复或丢失!"
    assert title == ("t1", "p1"), "包内会话未覆盖本地同名会话!"
    assert keep == 1, "并入误删了本地独有会话!"
    for p in (src_db, dst_db, pkg):
        os.remove(p)
    print("   会话包导出/并入 OK")
    print("== 7. 私库地址规范化与错误提示 ==")
    assert _normalize_url("me/repo") == "https://github.com/me/repo.git"
    assert _normalize_url("github.com/me/repo") == "https://github.com/me/repo.git"
    assert _normalize_url("https://github.com/me/repo.git") == "https://github.com/me/repo.git"
    assert _normalize_url("git@github.com:me/repo.git") == "git@github.com:me/repo.git"
    assert _normalize_url("") == ""
    assert _github_slug("https://github.com/me/repo.git") == ("me", "repo")
    assert "未授权" in git_hint("remote: Write access to repository not granted.")
    assert "仓库地址不对" in git_hint("remote: Repository not found.")
    assert "PAT 无效" in git_hint("fatal: Authentication failed for 'https://...'")
    assert "不是空目录" in git_hint("fatal: destination path '.' already exists and is not an empty directory.")
    assert "网络" in git_hint("fatal: unable to access 'https://github.com/x': Could not resolve host: github.com")
    assert git_hint("") == "" and git_hint("some random failure") == ""
    _err_remember("x")
    assert sync_last_error() == "x"
    _err_remember("")
    print("   地址规范化/错误提示 OK")
    print("== 8. 后台线程结果回传 ==")
    got, ev = [], threading.Event()

    def keep(e):
        got.append(e)
        ev.set()

    run_bg(lambda: (_ for _ in ()).throw(RuntimeError("boom")), on_err=keep)
    while not ev.wait(0.01):
        _bg_drain(keep)
    assert got and isinstance(got[0], RuntimeError), "后台异常没回传给 on_err!"
    assert "boom" in str(got[0]), "回传的异常内容不对!"
    ok, ev2 = [], threading.Event()
    run_bg(lambda: "fine", on_ok=lambda r: (ok.append(r), ev2.set()))
    while not ev2.wait(0.01):
        _bg_drain(keep)
    assert ok == ["fine"], "后台成功结果没回传给 on_ok!"
    boom = []
    _BG_RESULTS.put((None, RuntimeError("no on_err")))
    assert _bg_drain(boom.append) == 1 and boom, "缺 on_err 时没有兜底回调!"
    print(f"   异常/成功/兜底回调 OK ({got[0]!r})")
    print("== 9. 会话包标记 ==")
    me = os.environ.get("COMPUTERNAME", "?")
    assert bundle_flags({"new": 2, "host": "OTHER-PC"}) == "new"
    assert bundle_flags({"new": 0, "host": me}) == "own", "本机上传的包应标记为 own"
    assert bundle_flags({"new": 0, "host": "OTHER-PC"}) == "old"
    assert bundle_flags({"count": 3, "new": 0, "host": me}) == "own", "本机缺 0 不应算异常"
    print("   标记 new/own/old OK")
    print("== 10. 有 PAT 时禁用凭据助手 ==")
    assert _git_prefix("github_pat_xxx") == ["-c", "credential.helper=",
                                             "-c", "credential.interactive=false"]
    assert _git_prefix("") == [] and _git_prefix(None) == [] and _git_prefix("  ") == []
    print("   凭据助手开关 OK")
    print("== 11. 提问与回答 ==")
    pdb = os.path.join(tmp, "ocp-st-prompt.db")
    if os.path.isfile(pdb):
        os.remove(pdb)
    c = sqlite3.connect(pdb)
    for sql in schema:
        try:
            c.execute(sql)
        except Exception:
            pass
    c.execute("insert into project (id,worktree,sandboxes,time_created,time_updated) "
              "values ('p1','/x','[]',1,1)")
    c.execute("insert into session (id,project_id,slug,directory,title,version,time_created,time_updated) "
              "values ('s1','p1','a','/x','t1','v',1,1)")
    c.execute("insert into session (id,project_id,parent_id,slug,directory,title,version,"
              "time_created,time_updated) values ('s2','p1','s1','b','/x','sub','v',1,1)")

    def _msg(mid, sid, t, data):
        c.execute("insert into message (id,session_id,time_created,time_updated,data) "
                  "values (?,?,?,?,?)", (mid, sid, t, t, json.dumps(data)))

    def _part(pid, mid, sid, t, data):
        c.execute("insert into part (id,message_id,session_id,time_created,time_updated,data) "
                  "values (?,?,?,?,?,?)", (pid, mid, sid, t, t, json.dumps(data)))

    _msg("u1", "s1", 100, {"role": "user"})
    _part("pu1", "u1", "s1", 100, {"type": "text", "text": "q1"})
    _msg("a1", "s1", 101, {"role": "assistant", "parentID": "u1", "finish": "tool-calls"})
    _part("pa1", "a1", "s1", 101, {"type": "text", "text": "先看看配置"})
    _msg("a2", "s1", 102, {"role": "assistant", "parentID": "u1", "finish": "stop"})
    _part("pa2", "a2", "s1", 102, {"type": "text", "text": "答案在此"})
    _msg("u2", "s1", 200, {"role": "user"})
    _part("pu2", "u2", "s1", 200, {"type": "text", "text": "q2"})
    _msg("a3", "s1", 201, {"role": "assistant", "parentID": "u2", "finish": "tool-calls"})
    _part("pa3", "a3", "s1", 201, {"type": "text", "text": "正在处理"})
    _msg("u3", "s1", 300, {"role": "user"})
    _part("pu3", "u3", "s1", 300, {"type": "text", "text": "q3"})
    _msg("a4", "s1", 301, {"role": "assistant", "parentID": "u3", "finish": "tool-calls"})
    _part("pa4", "a4", "s1", 301, {"type": "tool", "tool": "bash"})
    _msg("u4", "s1", 400, {"role": "user"})
    _part("pu4", "u4", "s1", 400, {"type": "text", "text": "q4"})
    _msg("uc", "s2", 150, {"role": "user"})
    _part("puc", "uc", "s2", 150, {"type": "text", "text": "child-q"})
    _msg("ac", "s2", 151, {"role": "assistant", "parentID": "uc", "finish": "stop"})
    _part("pac", "ac", "s2", 151, {"type": "text", "text": "child-a"})
    c.commit()
    c.close()

    ps = session_prompts("s1", src_path=pdb)
    got = [(p["id"], p["state"]) for p in ps]
    print("   提问:", got)
    assert [p["id"] for p in ps] == ["u1", "u2", "u3", "u4"], "提问顺序或数量不对!"
    assert got[0][1] == PROMPT_OK and ps[0]["a"] == "答案在此", \
        "最终回答没取到, 或把过程旁白当成了回答!"
    assert got[1][1] == PROMPT_PARTIAL and "正在处理" in ps[1]["a"], "无 stop 时应回退过程文字!"
    assert got[2][1] == PROMPT_EMPTY and got[3][1] == PROMPT_EMPTY, "无内容应标记 empty!"
    assert not any("child-q" in p["q"] for p in ps), "子会话的提问混进来了!"
    assert session_prompts("", src_path=pdb) == [] and session_prompts("nope", src_path=pdb) == []
    md = prompts_to_md("t1", "/x", ps)
    for frag in ("# 会话: t1", "- 目录: `/x`", "- 提问数: 4", "## 1. ",
                 "### 提问", "q1", "### 回答", "答案在此", "> 未完成, 以下为过程文字",
                 "> (无内容)"):
        assert frag in md, f"导出 Markdown 缺少 {frag!r}!"
    pair = prompt_pair_md(ps[0])
    assert "#### 提问" in pair and "#### 回答" in pair and "答案在此" in pair
    print(f"   4 种情形(最终回答/回退/无内容/无 assistant) OK; 导出 {len(md)} 字符")
    print("== 12. Markdown 解析与渲染 ==")
    assert _disp_w("abc") == 3 and _disp_w("中文") == 4 and _disp_w("a中") == 3
    assert md_strip("**粗体** 与 `代码` 和 [链接](http://x)") == "粗体 与 代码 和 链接"
    assert md_strip("| a | b |") == "a | b" or "a" in md_strip("| a | b |")
    inl = md_inline("前 **粗** 后 `码` 与 [文](http://u) 及 *斜*")
    kinds = [(s, t) for _x, s, t in inl]
    assert ("bold", "") in kinds and ("code", "") in kinds, "行内粗体/代码没解析出来!"
    assert ("link", "http://u") in kinds and ("italic", "") in kinds, "行内链接/斜体没解析出来!"
    blocks = md_parse("# 标题\n\n正文 **粗**\n\n```text\ncode line\n```\n\n"
                      "| 名称 | 值 |\n|---|---:|\n| 甲 | 1 |\n\n- 项目一\n- 项目二\n\n"
                      "1. 第一\n\n> 引用\n\n---\n")
    got_kinds = [b["kind"] for b in blocks]
    print("   块类型:", got_kinds)
    assert got_kinds == ["heading", "para", "code", "table", "list", "list",
                         "quote", "hr"], "Markdown 块解析不对!"
    assert blocks[0]["level"] == 1 and blocks[0]["text"] == "标题"
    assert blocks[2]["lines"] == ["code line"] and blocks[2]["lang"] == "text"
    assert blocks[3]["rows"] == [["名称", "值"], ["甲", "1"]], "表格行解析不对!"
    assert blocks[3]["aligns"][1] == "right", "表格右对齐没识别!"
    assert blocks[4]["ordered"] is False and blocks[4]["items"] == ["项目一", "项目二"]
    assert blocks[5]["ordered"] is True and blocks[5]["items"] == ["第一"]
    assert md_parse("```\n未闭合\n")[0]["kind"] == "code", "未闭合代码块应容错!"
    assert md_parse("") == []
    # 渲染进真实 Tk 控件(隐藏窗口), 验证不抛异常且内容写进去了
    root = tk.Tk()
    root.withdraw()
    t = tk.Text(root)
    render_md(t, "# 标题\n\n正文 **粗** `码` [链接](https://example.com)\n\n"
                 "```text\ncode\n```\n\n| a | b |\n|---|---:|\n| 中 | 1 |\n\n- x\n\n> q\n\n---\n")
    body = t.get("1.0", "end")
    assert "标题" in body and "code" in body and "中" in body and "q" in body, "渲染后内容缺失!"
    assert "md_h1" in t.tag_names() and "md_pre" in t.tag_names(), "渲染 tag 没建立!"
    assert t.tag_ranges("md_bold") and t.tag_ranges("md_link"), "粗体/链接 tag 没应用!"
    assert "**" not in body, "渲染后仍残留 Markdown 标记!"
    root.destroy()
    print("   解析(标题/段落/代码块/表格右对齐/有序无序列表/引用/分割线/容错) + 渲染 OK")
    os.remove(pdb)
    print("== 13. 工作目录 ==")
    assert dir_name("D:/a/b") == "b" and dir_name("D:\\a\\b") == "b"
    assert dir_name("D:/a/b/") == "b" and dir_name("D:/a/b\\") == "b"
    assert dir_name("b") == "b" and dir_name("") == ""
    assert short_dir("D:/a/b") == os.sep.join(("a", "b")), "同名目录消歧显示不对!"
    assert short_dir("D:/a/b/c/d") == os.sep.join(("c", "d"))
    assert short_dir("b") == "b"
    dtmp = os.path.join(tmp, "ocp-st-dirs")
    if os.path.isdir(dtmp):
        shutil.rmtree(dtmp, ignore_errors=True)
    os.makedirs(os.path.join(dtmp, "inner", "deep"))
    os.makedirs(os.path.join(dtmp, "empty"))
    with open(os.path.join(dtmp, "a.txt"), "wb") as f:
        f.write(b"x" * 1000)
    with open(os.path.join(dtmp, "inner", "b.txt"), "wb") as f:
        f.write(b"y" * 2000)
    with open(os.path.join(dtmp, "inner", "deep", "c.txt"), "wb") as f:
        f.write(b"z" * 40)
    total, files, newest = dir_stats(dtmp)
    print(f"   dir_stats: {total} 字节 / {files} 文件 / mtime {dt.datetime.fromtimestamp(newest):%Y-%m-%d %H:%M}")
    assert total == 3040 and files == 3, "递归大小/文件数统计不对!"
    assert newest > 0, "最新修改时间没取到!"
    assert dir_stats(os.path.join(dtmp, "empty")) == (0, 0, 0.0), "空目录应全为 0!"
    assert dir_stats(os.path.join(dtmp, "nope")) == (0, 0, 0.0), "不存在的目录应容错返回 0!"
    st = dirs_stats([dtmp, os.path.join(dtmp, "empty")])
    assert st[dtmp][0] == 3040 and st[os.path.join(dtmp, "empty")] == (0, 0, 0.0)
    # session_dirs: 临时库里放"存在的目录"与"已缺失的目录"
    sdb = os.path.join(tmp, "ocp-st-sdirs.db")
    if os.path.isfile(sdb):
        os.remove(sdb)
    c = sqlite3.connect(sdb)
    for sql in schema:
        try:
            c.execute(sql)
        except Exception:
            pass
    c.execute("insert into project (id,worktree,sandboxes,time_created,time_updated) "
              "values ('p1','/x','[]',1,1)")
    for sid, d in (("d1", dtmp), ("d2", dtmp), ("d3", os.path.join(tmp, "ocp-st-gone"))):
        c.execute("insert into session (id,project_id,slug,directory,title,version,"
                  "time_created,time_updated) values (?,?,'a',?,'t','v',1,1)", (sid, "p1", d))
    c.commit()
    c.close()
    got = session_dirs(src_path=sdb)
    print("   会话目录:", [(x["name"], x["sessions"]) for x in got])
    assert len(got) == 1 and got[0]["path"] == dtmp and got[0]["sessions"] == 2, \
        "session_dirs 归并/隐藏缺失目录不对!"
    assert got[0]["exists"] is True and got[0]["name"] == os.path.basename(dtmp)
    allrows = session_dirs(src_path=sdb, hide_missing=False)
    assert len(allrows) == 2 and any(not x["exists"] for x in allrows), \
        "hide_missing=False 时应带出缺失目录并标记!"
    os.remove(sdb)
    shutil.rmtree(dtmp, ignore_errors=True)
    # 列宽自适应: 表头必须放得下
    root2 = tk.Tk()
    root2.withdraw()
    tv = ttk.Treeview(root2, columns=("a",), show="headings")
    fit_columns(tv, (("a", "更新时间", "w"),), minw=40)
    w = tv.column("a", "width")
    assert w >= fit_col_width("更新时间") >= 40, "列宽没有按表头自适应!"
    assert tv.column("a", "stretch") is False or not tv.column("a", "stretch")
    tv2 = ttk.Treeview(root2, columns=("a", "b"), show="headings")
    fit_columns(tv2, (("a", "状态", "w"), ("b", "路径", "w")), stretch_cols=("b",))
    assert bool(tv2.column("b", "stretch")) and not bool(tv2.column("a", "stretch")), \
        "拉伸列标记不对!"
    assert tv2.column("b", "minwidth") >= fit_col_width("路径")
    root2.destroy()
    print(f"   路径名称/短名/递归统计/缺失容错/列宽自适应 OK (列宽={w})")

    print("== 14. 内容列宽与布局 ==")
    root3 = tk.Tk()
    root3.withdraw()
    # content_col_width: 内容比表头宽时按内容, 否则回退表头
    wc = content_col_width(["● 运行中", "空闲"], "状态")
    wh = fit_col_width("状态")
    assert wc > wh, "内容比表头宽时应按内容取宽!"
    assert content_col_width([], "更新时间") == fit_col_width("更新时间", 40), \
        "没有内容样本时应回退到表头宽度!"
    assert content_col_width(["x"], "很长很长的表头文字", minw=40) == \
        fit_col_width("很长很长的表头文字", 40, minw=40), "表头更宽时应按表头!"
    # fit_columns 的 content_cols 只影响指定列
    tv3 = ttk.Treeview(root3, columns=("a", "b", "c"), show="headings")
    fit_columns(tv3, (("a", "状态", "w"), ("b", "标题", "w"), ("c", "路径", "w")),
                stretch_cols=("c",),
                content_cols={"a": ["● 运行中"], "b": ["一个相当长的标题内容样本"]})
    assert tv3.column("a", "width") > fit_col_width("状态"), "content_cols 没生效!"
    assert tv3.column("b", "width") > fit_col_width("标题"), "content_cols 没生效!"
    assert bool(tv3.column("c", "stretch")), "拉伸列应保持 stretch!"
    # caps 必须能限制内容列宽, 避免挤掉拉伸列
    tv4 = ttk.Treeview(root3, columns=("a", "b"), show="headings")
    fit_columns(tv4, (("a", "标题", "w"), ("b", "路径", "w")), stretch_cols=("b",),
                content_cols={"a": ["x" * 200]}, caps={"a": 120})
    assert tv4.column("a", "width") == 120, f"caps 没生效: {tv4.column('a','width')}"
    # 工作目录页: hint 必须排在 tree 之前(pack 在 tree 之后会把 tree 宽度钉死)
    if app_ref is None:
        bt = BrowseTab(root3)
        order = [str(x) for x in bt.pack_slaves()]
        itv = order.index(str(bt.tree))
        ihint = order.index(str(bt.hint))
        assert ihint < itv, "工作目录页的 hint 必须排在 tree 之前, 否则窗口拉伸列表不跟随!"
        assert bt.hint.cget("wraplength") not in ("", "0"), "hint 需要 wraplength!"
        print(f"   内容列宽 OK (状态={tv3.column('a','width')}, 标题={tv3.column('b','width')})")
        print("   工作目录页 pack 顺序 OK (hint 在 tree 之前)")
    else:
        print(f"   内容列宽 OK (状态={tv3.column('a','width')}, 标题={tv3.column('b','width')})")
    root3.destroy()
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
