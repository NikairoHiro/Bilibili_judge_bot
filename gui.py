#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gui.py —— @ 回复机器人的本地网页控制台（纯标准库，无第三方依赖）

用法：
    python gui.py            # 启动并自动打开浏览器（默认 http://127.0.0.1:8765）
    python gui.py --port 9000 --no-browser

只监听 127.0.0.1，不对外网开放。
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "gui_config.json")
LOG_PATH = os.path.join(APP_DIR, "gui_run.log")

sys.path.insert(0, APP_DIR)
import bili_at_monitor as bot  # noqa: E402

DEFAULT_CONFIG = {
    "outdir": APP_DIR,
    "state": os.path.join(APP_DIR, "_state.json"),
    "outfile": "bilibili_at.txt",
    "interval": 300,
    "ai": True,
    "reply": False,
    "ai_model": "deepseek-flash",
    "ai_key": "",
    "ai_max_per_hour": 8,
    "ai_max_per_day": 40,
    "ai_min_interval": 45,
    "no_search": False,
    "auto_relogin": False,
    "ai_opinion_mode": "loose",
    "admins": "",
    "since": "",
    "quiet_hours": "",
    "allow_users": "",
    "block_users": "",
}


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            saved = json.load(fh)
        if isinstance(saved, dict):
            cfg.update({k: v for k, v in saved.items() if k in DEFAULT_CONFIG})
    except Exception:
        pass
    # 配置里的字符串去掉首尾空白（手滑空格会让 API Key / 路径失效）
    for k, v in list(cfg.items()):
        if isinstance(v, str):
            cfg[k] = v.strip()

    # 迁移自愈：换机器后旧配置里的绝对路径不存在 → 回落到脚本所在目录
    if not os.path.isdir(str(cfg.get("outdir") or "")):
        cfg["outdir"] = DEFAULT_CONFIG["outdir"]
    state_dir = os.path.dirname(str(cfg.get("state") or ""))
    if not state_dir or not os.path.isdir(state_dir):
        cfg["state"] = DEFAULT_CONFIG["state"]
    return cfg


def save_config(cfg: dict) -> dict:
    out = {}
    for k, default in DEFAULT_CONFIG.items():
        v = cfg.get(k, default)
        if isinstance(default, bool):
            v = bool(v)
        elif isinstance(default, int):
            try:
                v = int(v)
            except (TypeError, ValueError):
                v = default
        elif isinstance(default, str):
            # 去掉首尾空白：手滑输入的空格会让 Authorization 头变成 "Bearer  sk-..."
            v = str(v or "").strip()
        out[k] = v
    with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
    return out


def build_argv(cfg: dict) -> list:
    argv = [sys.executable, "-u", os.path.join(APP_DIR, "bili_at_monitor.py")]
    argv += ["--outdir", str(cfg["outdir"]), "--state", str(cfg["state"]),
             "--outfile", str(cfg["outfile"])]
    argv += ["--interval", str(cfg["interval"])]
    if cfg.get("ai"):
        argv.append("--ai")
        argv += ["--ai-model", str(cfg["ai_model"])]
        argv += ["--ai-max-per-hour", str(cfg["ai_max_per_hour"])]
        argv += ["--ai-max-per-day", str(cfg["ai_max_per_day"])]
        argv += ["--ai-min-interval", str(cfg["ai_min_interval"])]
        if str(cfg.get("ai_key") or "").strip():
            argv += ["--ai-key", str(cfg["ai_key"]).strip()]
        if cfg.get("no_search"):
            argv.append("--ai-no-search")
        if cfg.get("ai_opinion_mode"):
            argv += ["--ai-opinion-mode", str(cfg["ai_opinion_mode"])]
        if cfg.get("admins"):
            argv += ["--admins", str(cfg["admins"])]
    if cfg.get("reply"):
        argv.append("--reply")
    if cfg.get("since"):
        argv += ["--since", str(cfg["since"])]
    if cfg.get("auto_relogin"):
        argv.append("--auto-relogin")
    if cfg.get("quiet_hours"):
        argv += ["--quiet-hours", str(cfg["quiet_hours"])]
    if cfg.get("allow_users"):
        argv += ["--allow-users", str(cfg["allow_users"])]
    if cfg.get("block_users"):
        argv += ["--block-users", str(cfg["block_users"])]
    return argv


# --------------------------------------------------------------------------
# 进程管理
# --------------------------------------------------------------------------
class Runner:
    def __init__(self):
        self.proc = None
        self.lines = collections.deque(maxlen=3000)
        self.next_id = 1
        self.lock = threading.RLock()      # 可重入：_emit 会在持锁时被调用

    def running(self) -> bool:
        proc = self.proc
        return proc is not None and proc.poll() is None

    def start(self, cfg: dict):
        if self.running():
            return False, "已经在运行"
        argv = build_argv(cfg)
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        flags = 0
        if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
            flags = subprocess.CREATE_NO_WINDOW
        try:
            proc = subprocess.Popen(
                argv, cwd=APP_DIR, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                errors="replace", env=env, creationflags=flags)
        except Exception as exc:
            return False, "启动失败：%s" % exc
        with self.lock:
            self.proc = proc
        self._emit("[启动] %s" % " ".join(argv[2:]))
        threading.Thread(target=self._reader, args=(proc,), daemon=True).start()
        return True, "已启动（PID %s）" % proc.pid

    def stop(self):
        with self.lock:
            proc = self.proc
        if proc is None or proc.poll() is not None:
            return False, "当前没有在运行"
        try:
            proc.terminate()
            try:
                proc.wait(timeout=6)
            except subprocess.TimeoutExpired:
                proc.kill()
            self._emit("[停止] 已终止进程")
            return True, "已停止"
        except Exception as exc:
            return False, "停止失败：%s" % exc

    def _reader(self, proc):
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as fh:
                for line in proc.stdout:
                    line = line.rstrip("\r\n")
                    self._emit(line)
                    fh.write(line + "\n")
                    fh.flush()
        except Exception:
            pass
        code = proc.poll()
        self._emit("[退出] 进程结束，返回码 %s" % code)

    def _emit(self, text: str):
        with self.lock:
            self.lines.append({"id": self.next_id, "t": time.strftime("%H:%M:%S"),
                               "text": text})
            self.next_id += 1

    def preload_log(self, path: str, limit: int = 200):
        """启动时把上次的日志读进来，页面不会一片空白。"""
        try:
            if not os.path.isfile(path):
                return
            with open(path, encoding="utf-8", errors="replace") as fh:
                tail = collections.deque(fh, maxlen=limit)
            with self.lock:
                for line in tail:
                    line = line.rstrip("\r\n")
                    if not line:
                        continue
                    self.lines.append({"id": self.next_id, "t": "", "text": line})
                    self.next_id += 1
        except Exception:
            pass

    # 说明：控制台本身跑在浏览器里，浏览器开着就无法读取其 Cookie 库，
    # 所以这里不提供「一键登录」，只给出命令，让用户在终端里执行。


RUNNER = Runner()


# --------------------------------------------------------------------------
# 数据读取
# --------------------------------------------------------------------------
def read_state() -> dict:
    try:
        st = bot.load_state(load_config()["state"])
        return st if isinstance(st, dict) else {}
    except Exception:
        return {}


def api_summary() -> dict:
    cfg = load_config()
    st = read_state()
    stats = bot.build_stats(st)
    return {
        "running": RUNNER.running(),
        "pid": RUNNER.proc.pid if RUNNER.running() else None,
        "config": {k: ("" if k == "ai_key" else v) for k, v in cfg.items()},
        "has_key": bool(cfg.get("ai_key")),
        "round": st.get("round") or 0,
        "last_time": st.get("last_run") or "",
        "stats": {
            "events": stats["events"], "replied": stats["replied"],
            "users": stats["users"], "pending": stats["pending"],
            "done": stats["done"], "dropped": stats["dropped"],
        },
        "categories": stats["categories"],
        "daily": stats["daily"],
    }


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "BiliAtGui/1.0"

    def log_message(self, fmt, *args):      # 静音访问日志
        pass

    # ---- 工具 ----
    def _send(self, code, body: bytes, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            return json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return {}

    def _local_only(self) -> bool:
        host = (self.client_address or ["", 0])[0]
        return host in ("127.0.0.1", "::1", "localhost")

    # ---- 路由 ----
    def do_GET(self):
        if not self._local_only():
            return self._json({"ok": False, "error": "only localhost"}, 403)
        path = urlparse(self.path).path
        qs = parse_qs(urlparse(self.path).query)
        if path in ("/", "/index.html"):
            return self._send(200, HTML.encode("utf-8"), "text/html; charset=utf-8")
        if path == "/api/summary":
            return self._json({"ok": True, **api_summary()})
        if path == "/api/logs":
            try:
                since = int((qs.get("since") or ["0"])[0])
            except ValueError:
                since = 0
            with RUNNER.lock:
                lines = [x for x in RUNNER.lines if x["id"] > since]
                nxt = RUNNER.next_id - 1
            return self._json({"ok": True, "lines": lines, "next": nxt})
        if path == "/api/queue":
            which = (qs.get("which") or ["pending"])[0]
            st = read_state()
            q = st.get("reply_queue") or {}
            if which == "pending":
                items = [{
                    "key": it.get("key"),
                    "mid": (it.get("ev") or {}).get("mid"),
                    "nickname": (it.get("ev") or {}).get("nickname"),
                    "content": ((it.get("ev") or {}).get("content") or "")[:60],
                    "parent": ((it.get("ev") or {}).get("parent_content") or "")[:60],
                    "attempts": it.get("attempts", 0),
                    "added": time.strftime("%m-%d %H:%M", time.localtime(it.get("added") or 0)),
                } for it in (q.get("pending") or [])]
            elif which == "done":
                items = [{"key": k, **v} for k, v in (q.get("done") or {}).items()]
            else:
                items = [{"key": k, "reason": v} for k, v in (q.get("dropped") or {}).items()]
            return self._json({"ok": True, "items": items})
        if path == "/api/users":
            st = read_state()
            users = st.get("users") or {}
            rows = [{"uid": k, **(v if isinstance(v, dict) else {})} for k, v in users.items()]
            rows.sort(key=lambda r: -(int(r.get("at_count") or 0)))
            return self._json({"ok": True, "items": rows})
        if path == "/api/stats":
            st = read_state()
            stat = bot.build_stats(st)
            return self._json({"ok": True, "stats": stat,
                               "text": bot.render_stats(stat)})
        return self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        if not self._local_only():
            return self._json({"ok": False, "error": "only localhost"}, 403)
        path = urlparse(self.path).path
        if path == "/api/start":
            cfg = load_config()
            ok, msg = RUNNER.start(cfg)
            return self._json({"ok": ok, "message": msg})
        if path == "/api/stop":
            ok, msg = RUNNER.stop()
            return self._json({"ok": ok, "message": msg})
        if path == "/api/config":
            body = self._body()
            cfg = load_config()
            for k in DEFAULT_CONFIG:
                if k in body:
                    cfg[k] = body[k]
            try:
                saved = save_config(cfg)
            except Exception as exc:
                return self._json({"ok": False, "message": "保存失败：%s" % exc})
            return self._json({"ok": True, "message": "已保存", "config": {
                k: ("" if k == "ai_key" else v) for k, v in saved.items()}})
        if path == "/api/queue/cancel":
            body = self._body()
            cfg = load_config()
            st = bot.load_state(cfg["state"])
            q = bot.ReplyQueue(st)
            keys = [str(k) for k in (body.get("keys") or [])]
            if body.get("all"):
                keys = [it.get("key") for it in q.pending]
            n = 0
            for key in keys:
                if key in q.keys:
                    q.drop(key, "手动取消")
                    n += 1
            if n:
                bot.save_state(cfg["state"], st)
            return self._json({"ok": True, "message": "已取消 %d 条队列任务" % n})
        return self._json({"ok": False, "error": "not found"}, 404)


# --------------------------------------------------------------------------
# 页面
# --------------------------------------------------------------------------
HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>@ 回复机器人 · 控制台</title>
<style>
  :root{--bg:#f5f6f8;--card:#fff;--line:#e3e6ea;--txt:#1f2329;--dim:#7a8290;--ok:#12a150;--bad:#d93026;--blue:#2563eb}
  *{box-sizing:border-box}
  body{margin:0;font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--txt)}
  header{display:flex;align-items:center;gap:12px;padding:14px 20px;background:var(--card);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:5}
  header h1{font-size:16px;margin:0}
  .pill{font-size:12px;padding:2px 10px;border-radius:999px;background:#eceff3;color:var(--dim)}
  .pill.on{background:#e6f6ec;color:var(--ok)}
  .pill.off{background:#fdeceb;color:var(--bad)}
  .spacer{flex:1}
  button{font:inherit;padding:6px 14px;border-radius:8px;border:1px solid var(--line);background:#fff;cursor:pointer}
  button:hover{border-color:#c9ced6}
  button.primary{background:var(--blue);border-color:var(--blue);color:#fff}
  button.danger{background:#fff;border-color:#f0b6b1;color:var(--bad)}
  nav{display:flex;gap:4px;padding:10px 20px 0;background:var(--card);border-bottom:1px solid var(--line)}
  nav a{padding:8px 14px;border-radius:8px 8px 0 0;color:var(--dim);text-decoration:none;font-size:13px}
  nav a.active{background:var(--bg);color:var(--txt);font-weight:600}
  main{padding:18px 20px 40px;max-width:1200px;margin:0 auto}
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:16px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px}
  .card .k{font-size:12px;color:var(--dim)}
  .card .v{font-size:24px;font-weight:700;margin-top:2px}
  .panel{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:16px}
  .panel h2{font-size:14px;margin:0 0 10px}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top}
  th{color:var(--dim);font-weight:600;font-size:12px}
  pre{margin:0;white-space:pre-wrap;font-family:Consolas,"Courier New",monospace;font-size:12.5px}
  #log{height:420px;overflow:auto;background:#0f1115;color:#d7dce2;border-radius:10px;padding:12px;font-family:Consolas,monospace;font-size:12.5px}
  #log div{white-space:pre-wrap;word-break:break-all}
  #log .t{color:#6b7280;margin-right:8px}
  .row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:10px 20px}
  label{display:block;font-size:12px;color:var(--dim);margin-bottom:4px}
  input[type=text],input[type=number],select{width:100%;padding:7px 10px;border:1px solid var(--line);border-radius:8px;font:inherit;background:#fff}
  input[type=checkbox]{width:16px;height:16px;vertical-align:-3px}
  .hint{font-size:12px;color:var(--dim);margin-top:6px}
  .toast{position:fixed;right:18px;bottom:18px;background:#1f2329;color:#fff;padding:10px 16px;border-radius:10px;opacity:0;transition:.25s;font-size:13px}
  .toast.show{opacity:1}
  .bar{height:8px;background:#eef1f5;border-radius:4px;overflow:hidden;min-width:80px}
  .bar > i{display:block;height:100%;background:var(--blue)}
  .tag{display:inline-block;font-size:11px;padding:1px 7px;border-radius:999px;background:#eef1f5;color:#4b5563}
  .muted{color:var(--dim)}
</style>
</head>
<body>
<header>
  <h1>@ 回复机器人</h1>
  <span id="state" class="pill">读取中…</span>
  <span class="spacer"></span>
  <button class="primary" onclick="act('start')">启动</button>
  <button class="danger" onclick="act('stop')">停止</button>
</header>

<nav>
  <a href="#dash" class="active" data-tab="dash">控制台</a>
  <a href="#queue" data-tab="queue">队列</a>
  <a href="#report" data-tab="report">报表</a>
  <a href="#users" data-tab="users">用户</a>
  <a href="#logs" data-tab="logs">日志</a>
  <a href="#settings" data-tab="settings">设置</a>
</nav>

<main>
  <section id="tab-dash">
    <div class="cards">
      <div class="card"><div class="k">累计 @ 事件</div><div class="v" id="c-events">-</div></div>
      <div class="card"><div class="k">成功回复</div><div class="v" id="c-replied">-</div></div>
      <div class="card"><div class="k">@ 我的人</div><div class="v" id="c-users">-</div></div>
      <div class="card"><div class="k">队列待回复</div><div class="v" id="c-pending">-</div></div>
      <div class="card"><div class="k">已回复事件</div><div class="v" id="c-done">-</div></div>
      <div class="card"><div class="k">已丢弃</div><div class="v" id="c-dropped">-</div></div>
    </div>
    <div class="panel">
      <h2>最近日志</h2>
      <div id="log-mini" class="muted">（等待日志…）</div>
    </div>
  </section>

  <section id="tab-queue" hidden>
    <div class="panel">
      <div class="row" style="margin-bottom:10px">
        <select id="q-which" onchange="loadQueue()" style="width:200px">
          <option value="pending">待回复</option>
          <option value="done">已回复</option>
          <option value="dropped">已丢弃</option>
        </select>
        <span class="muted" id="q-count"></span>
        <span class="spacer"></span>
        <button class="danger" onclick="cancelAll()">清空待回复</button>
      </div>
      <table><thead id="q-head"></thead><tbody id="q-body"></tbody></table>
    </div>
  </section>

  <section id="tab-report" hidden>
    <div class="panel">
      <h2>统计报表</h2>
      <pre id="report">读取中…</pre>
    </div>
  </section>

  <section id="tab-users" hidden>
    <div class="panel">
      <h2>@ 我的人（次数 / 信用）</h2>
      <table>
        <thead><tr><th>UID</th><th>昵称</th><th>次数</th><th>信用</th><th>@我次数</th><th>最后互动</th></tr></thead>
        <tbody id="u-body"></tbody>
      </table>
    </div>
  </section>

  <section id="tab-logs" hidden>
    <div class="panel">
      <div class="row" style="margin-bottom:10px">
        <button onclick="document.getElementById('log').innerHTML=''">清屏</button>
        <label style="margin:0"><input type="checkbox" id="autoscroll" checked> 自动滚动</label>
      </div>
      <div id="log"></div>
    </div>
  </section>

  <section id="tab-settings" hidden>
    <div class="panel">
      <h2>运行参数</h2>
      <div class="grid2">
        <div><label>输出目录</label><input type="text" id="f-outdir"></div>
        <div><label>状态文件</label><input type="text" id="f-state"></div>
        <div><label>txt 文件名</label><input type="text" id="f-outfile"></div>
        <div><label>抓取间隔（秒）</label><input type="number" id="f-interval"></div>
        <div><label>模型</label><input type="text" id="f-ai_model"></div>
        <div><label>DeepSeek Key（留空=读环境变量 DEEPSEEK_API_KEY）</label><input type="text" id="f-ai_key" placeholder="sk-..."></div>
        <div><label>每小时上限</label><input type="number" id="f-ai_max_per_hour"></div>
        <div><label>每日上限</label><input type="number" id="f-ai_max_per_day"></div>
        <div><label>最小间隔（秒）</label><input type="number" id="f-ai_min_interval"></div>
        <div><label>夜间静默（如 23:00-07:00）</label><input type="text" id="f-quiet_hours"></div>
        <div><label>「只有观点」判定</label>
          <select id="f-ai_opinion_mode">
            <option value="loose">loose（默认，提到视频/上下文就不判）</option>
            <option value="off">off（关闭，一律按论证处理）</option>
            <option value="strict">strict（保留原判定）</option>
          </select>
        </div>
        <div><label>只回这些 UID（逗号分隔）</label><input type="text" id="f-allow_users"></div>
        <div><label>永不回复这些 UID</label><input type="text" id="f-block_users"></div>
        <div><label>管理员 UID（不限次数/信用，显示 ∞）</label><input type="text" id="f-admins" placeholder="1331065292,123456"></div>
        <div><label>只回复此时间之后的 @（留空=不限）</label><input type="text" id="f-since" placeholder="2026-11-01 08:30"></div>
      </div>
      <div class="row" style="margin-top:12px">
        <label style="margin:0"><input type="checkbox" id="f-ai"> 开启 AI 判定</label>
        <label style="margin:0"><input type="checkbox" id="f-reply"> 真正发送（关=dry-run）</label>
        <label style="margin:0"><input type="checkbox" id="f-no_search"> 关闭联网查证</label>
        <label style="margin:0"><input type="checkbox" id="f-auto_relogin"> 失效自动重登</label>
      </div>
      <div class="row" style="margin-top:14px">
        <button class="primary" onclick="saveCfg()">保存配置</button>
        <span class="muted" id="cfg-hint"></span>
      </div>
    </div>
    <div class="panel">
      <h2>登录：必须用命令行（重要）</h2>
      <div class="hint">
        控制台本身就在浏览器里运行，<strong>浏览器开着时 Cookie 库被独占锁定，读不到</strong>，
        所以这里不能「一键登录」。请按下面两步操作：
      </div>
      <ol class="hint" style="margin:8px 0 8px 18px;padding:0">
        <li>完全退出 Edge（任务栏图标右键 → 退出，不是只关窗口）</li>
        <li>在项目目录打开终端，执行下面这条命令</li>
      </ol>
      <div class="row">
        <code style="background:#f1f3f5;padding:6px 10px;border-radius:6px">python bili_at_monitor.py --login</code>
        <button onclick="copyLoginCmd()">复制命令</button>
        <span class="muted" id="copy-hint"></span>
      </div>
      <div class="hint" style="margin-top:8px">
        用 Chrome 就加 <code>--browser chrome</code>；多配置用 <code>--profile "Profile 1"</code>。<br>
        如果 Cookie 是 <strong>v20（App-Bound）</strong> 加密，程序会自动启动一个无头浏览器，
        用浏览器自己解密后取回，无需手动导出；<br>
        实在读不到时，也可以手动复制 <code>SESSDATA</code> 与 <code>bili_jct</code>，
        用 <code>--cookie "SESSDATA=...; bili_jct=..."</code> 传入。
      </div>
    </div>
  </section>
</main>

<div id="toast" class="toast"></div>

<script>
const $ = (id) => document.getElementById(id);
let lastLogId = 0;
let curTab = 'dash';
const allLines = [];       // 客户端日志缓冲（控制台只看最后 8 行）

function toast(msg, ok=true){
  const t = $('toast');
  t.textContent = msg;
  t.style.background = ok ? '#1f2329' : '#b3261e';
  t.classList.add('show');
  setTimeout(()=>t.classList.remove('show'), 2200);
}

async function api(path, opts){
  const r = await fetch(path, opts);
  return await r.json();
}

function switchTab(name){
  curTab = name;
  document.querySelectorAll('nav a').forEach(a=>a.classList.toggle('active', a.dataset.tab===name));
  ['dash','queue','report','users','logs','settings'].forEach(t=>{
    $('tab-'+t).hidden = (t!==name);
  });
  if(name==='queue') loadQueue();
  if(name==='report') loadReport();
  if(name==='users') loadUsers();
  if(name==='settings') loadCfg();
  if(name==='logs') pollLogs();
}
document.querySelectorAll('nav a').forEach(a=>{
  a.onclick = (e)=>{ e.preventDefault(); switchTab(a.dataset.tab); };
});

async function act(what){
  const r = await api('/api/'+what, {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
  toast(r.message || (r.ok?'完成':'失败'), !!r.ok);
  refresh();
}

async function refresh(){
  try{
    const s = await api('/api/summary');
    const p = $('state');
    p.textContent = s.running ? ('运行中 · PID '+s.pid) : '未运行';
    p.className = 'pill ' + (s.running ? 'on':'off');
    $('c-events').textContent = s.stats.events;
    $('c-replied').textContent = s.stats.replied;
    $('c-users').textContent = s.stats.users;
    $('c-pending').textContent = s.stats.pending;
    $('c-done').textContent = s.stats.done;
    $('c-dropped').textContent = s.stats.dropped;
  }catch(e){}
}

async function pollLogs(){
  try{
    const r = await api('/api/logs?since='+lastLogId);
    if(r.lines && r.lines.length){
      const log = $('log');
      for(const ln of r.lines){
        allLines.push(ln);
        const d = document.createElement('div');
        d.innerHTML = ln.t ? ('<span class="t">'+ln.t+'</span>') : '';
        d.appendChild(document.createTextNode(ln.text));
        log.appendChild(d);
      }
      if(allLines.length > 3000) allLines.splice(0, allLines.length-3000);
      // 控制台只留最近 8 行
      if(curTab==='dash'){
        const mini = $('log-mini');
        mini.classList.remove('muted');
        mini.innerHTML = allLines.slice(-8).map(l=>'<div>'+(l.t?'<span class="muted">'+l.t+'</span> ':'')+escapeHtml(l.text)+'</div>').join('');
      }
      lastLogId = r.next;
      if($('autoscroll') && $('autoscroll').checked) log.scrollTop = log.scrollHeight;
    }
  }catch(e){}
}

function escapeHtml(s){
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function copyLoginCmd(){
  const cmd = 'python bili_at_monitor.py --login';
  const done = () => { $('copy-hint').textContent = '已复制'; setTimeout(()=>{ $('copy-hint').textContent=''; }, 1500); };
  if(navigator.clipboard && navigator.clipboard.writeText){
    navigator.clipboard.writeText(cmd).then(done).catch(()=>prompt('复制这条命令：', cmd));
  }else{
    prompt('复制这条命令：', cmd);
  }
}

async function loadQueue(){
  const which = $('q-which').value;
  const r = await api('/api/queue?which='+which);
  const items = r.items || [];
  $('q-count').textContent = '共 ' + items.length + ' 条';
  if(which==='pending'){
    $('q-head').innerHTML = '<tr><th>事件</th><th>UID</th><th>昵称</th><th>@ 内容</th><th>父级评论</th><th>尝试</th><th>入队时间</th><th>操作</th></tr>';
    $('q-body').innerHTML = items.map(x=>'<tr><td>'+escapeHtml(x.key)+'</td><td>'+escapeHtml(String(x.mid||''))+'</td><td>'+escapeHtml(x.nickname||'')+'</td><td>'+escapeHtml(x.content||'')+'</td><td>'+escapeHtml(x.parent||'')+'</td><td>'+x.attempts+'</td><td>'+escapeHtml(x.added||'')+'</td><td><button class="danger" onclick="cancelOne(\''+escapeHtml(x.key)+'\')">取消</button></td></tr>').join('') || '<tr><td colspan="8" class="muted">空</td></tr>';
  }else if(which==='done'){
    $('q-head').innerHTML = '<tr><th>事件</th><th>评论 rpid</th><th>时间</th><th>验证</th><th>目标</th></tr>';
    $('q-body').innerHTML = items.map(x=>'<tr><td>'+escapeHtml(x.key)+'</td><td>'+escapeHtml(x.rpid||'')+'</td><td>'+escapeHtml(x.time||'')+'</td><td>'+(x.verified?'<span class="tag" style="background:#e6f6ec;color:#12a150">通过</span>':'<span class="tag" style="background:#fdeceb;color:#d93026">未通过</span>')+'</td><td>'+escapeHtml(x.target||'')+'</td></tr>').join('') || '<tr><td colspan="5" class="muted">空</td></tr>';
  }else{
    $('q-head').innerHTML = '<tr><th>事件</th><th>原因</th></tr>';
    $('q-body').innerHTML = items.map(x=>'<tr><td>'+escapeHtml(x.key)+'</td><td>'+escapeHtml(x.reason||'')+'</td></tr>').join('') || '<tr><td colspan="2" class="muted">空</td></tr>';
  }
}

async function cancelOne(key){
  if(!confirm('确定取消这条队列任务？\n' + key)) return;
  const r = await api('/api/queue/cancel', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({keys:[key]})});
  toast(r.message || '已取消', !!r.ok);
  loadQueue();
}

async function cancelAll(){
  if(!confirm('确定清空所有待回复队列？此操作不可撤销。')) return;
  const r = await api('/api/queue/cancel', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({all:true})});
  toast(r.message || '已清空', !!r.ok);
  loadQueue();
}

async function loadReport(){
  const r = await api('/api/stats');
  $('report').textContent = r.text || '无数据';
}

async function loadUsers(){
  const r = await api('/api/users');
  const items = r.items || [];
  $('u-body').innerHTML = items.map(x=>'<tr><td>'+escapeHtml(x.uid)+'</td><td>'+escapeHtml(x.nickname||'')+'</td><td>'+x.times+'</td><td>'+x.credit+'</td><td>'+x.at_count+'</td><td>'+escapeHtml(x.last||'')+'</td></tr>').join('') || '<tr><td colspan="6" class="muted">空</td></tr>';
}

async function loadCfg(){
  const r = await api('/api/summary');
  const c = r.config || {};
  for(const k of ['outdir','state','outfile','interval','ai_model','ai_key','ai_max_per_hour','ai_max_per_day','ai_min_interval','quiet_hours','allow_users','block_users','ai_opinion_mode','admins','since']){
    const el = $('f-'+k);
    if(el) el.value = (c[k]===undefined?'':c[k]);
  }
  for(const k of ['ai','reply','no_search','auto_relogin']){
    const el = $('f-'+k);
    if(el) el.checked = !!c[k];
  }
  $('cfg-hint').textContent = r.has_key ? '（已保存 Key）' : '（未保存 Key，将读环境变量 DEEPSEEK_API_KEY）';
}

async function saveCfg(){
  const body = {};
  for(const k of ['outdir','state','outfile','interval','ai_model','ai_key','ai_max_per_hour','ai_max_per_day','ai_min_interval','quiet_hours','allow_users','block_users','ai_opinion_mode','admins','since']){
    const el = $('f-'+k);
    if(el) body[k] = el.value;
  }
  for(const k of ['ai','reply','no_search','auto_relogin']){
    const el = $('f-'+k);
    if(el) body[k] = el.checked;
  }
  const r = await api('/api/config', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  toast(r.message || '已保存', !!r.ok);
}

refresh();
pollLogs();
switchTab('dash');
setInterval(()=>{ refresh(); pollLogs(); }, 2000);
setInterval(()=>{ if(curTab==='queue') loadQueue(); }, 4000);
setInterval(()=>{ if(curTab==='report') loadReport(); }, 8000);
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(description="本地网页控制台")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    if not os.path.exists(cfg["state"]):
        pass  # 没跑过也没关系，页面会显示 0

    RUNNER.preload_log(LOG_PATH)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = "http://%s:%d/" % (args.host, args.port)
    print("控制台已启动：%s" % url)
    print("按 Ctrl+C 退出（不会自动停止后台机器人进程）")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
