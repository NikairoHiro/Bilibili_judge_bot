#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""edge_cookie.py —— 直接从 Edge / Chrome 的本地 Cookie 库读取 B站登录凭证。

两条路径：
  A. v10 / v11（传统加密）
     <Local State>/os_crypt/encrypted_key 是 DPAPI 保护的 AES 主密钥；
     encrypted_value = b"v10" + nonce(12) + ciphertext + tag(16)，AES-256-GCM 解密（bcrypt.dll）。
  B. v20（App-Bound 加密，Chromium 127+ 起）
     只能由浏览器自己解密：复制一份配置到临时目录，启动无头浏览器，
     通过 DevTools 协议 Network.getCookies 取回（浏览器进程天然能解 v20）。

限制：
  · 浏览器运行时 Cookie 库被独占锁定，必须先完全退出浏览器；
  · v20 路径需要能启动本机 Edge/Chrome（企业策略禁用 headless 时不可用）。
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wt
import json
import os
import shutil
import socket
import sqlite3
import struct
import subprocess
import tempfile
import time

CHROME_EPOCH_OFFSET = 11644473600  # 1601-01-01 到 1970-01-01 的秒数


class EdgeCookieError(RuntimeError):
    """读取浏览器 Cookie 失败（附可操作的原因）。"""


# --------------------------------------------------------------------------
# DPAPI：解出 AES 主密钥
# --------------------------------------------------------------------------
class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi_decrypt(data: bytes) -> bytes:
    buf = ctypes.create_string_buffer(data, len(data))
    in_blob = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out_blob = _DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob))
    if not ok:
        raise EdgeCookieError("DPAPI 解密失败（可能不是同一 Windows 用户）")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)


def load_master_key(local_state_path: str) -> bytes:
    try:
        with open(local_state_path, encoding="utf-8") as fh:
            state = json.load(fh)
        enc = base64.b64decode(state["os_crypt"]["encrypted_key"])
    except Exception as exc:
        raise EdgeCookieError("读取 Local State 失败：%s" % exc)
    if enc[:5] != b"DPAPI":
        raise EdgeCookieError("Cookie 密钥格式不认识（不是 DPAPI）")
    key = _dpapi_decrypt(enc[5:])
    if len(key) != 32:
        raise EdgeCookieError("主密钥长度异常：%d" % len(key))
    return key


# --------------------------------------------------------------------------
# AES-256-GCM（bcrypt.dll）
# --------------------------------------------------------------------------
_bcrypt = ctypes.windll.bcrypt

BCRYPT_AES_ALGORITHM = "AES"
BCRYPT_CHAIN_MODE_GCM = "ChainingModeGCM"


class _AUTH_INFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.ULONG),
        ("dwInfoVersion", wt.ULONG),
        ("pbNonce", ctypes.POINTER(ctypes.c_ubyte)),
        ("cbNonce", wt.ULONG),
        ("pbAuthData", ctypes.POINTER(ctypes.c_ubyte)),
        ("cbAuthData", wt.ULONG),
        ("pbTag", ctypes.POINTER(ctypes.c_ubyte)),
        ("cbTag", wt.ULONG),
        ("pbMacContext", ctypes.POINTER(ctypes.c_ubyte)),
        ("cbMacContext", wt.ULONG),
        ("cbAAD", wt.ULONG),
        ("cbData", ctypes.c_ulonglong),
        ("dwFlags", wt.ULONG),
    ]


def _open_aes_gcm():
    h_alg = wt.HANDLE()
    if _bcrypt.BCryptOpenAlgorithmProvider(
            ctypes.byref(h_alg), BCRYPT_AES_ALGORITHM, None, 0) != 0:
        raise EdgeCookieError("BCrypt 打开 AES 失败")
    mode = ctypes.create_unicode_buffer(BCRYPT_CHAIN_MODE_GCM)
    if _bcrypt.BCryptSetProperty(
            h_alg, "ChainingMode", mode,
            ctypes.sizeof(mode), 0) != 0:
        _bcrypt.BCryptCloseAlgorithmProvider(h_alg, 0)
        raise EdgeCookieError("BCrypt 设置 GCM 模式失败")
    return h_alg


def _gen_key(h_alg, key: bytes):
    h_key = wt.HANDLE()
    key_buf = ctypes.create_string_buffer(key, len(key))
    if _bcrypt.BCryptGenerateSymmetricKey(
            h_alg, ctypes.byref(h_key), None, 0,
            key_buf, len(key), 0) != 0:
        raise EdgeCookieError("BCrypt 生成对称密钥失败")
    return h_key


def aes_gcm_decrypt(key: bytes, nonce: bytes, ciphertext: bytes, tag: bytes) -> bytes:
    h_alg = _open_aes_gcm()
    h_key = None
    try:
        h_key = _gen_key(h_alg, key)
        nonce_buf = (ctypes.c_ubyte * len(nonce)).from_buffer_copy(nonce)
        tag_buf = (ctypes.c_ubyte * len(tag)).from_buffer_copy(tag)
        info = _AUTH_INFO()
        info.cbSize = ctypes.sizeof(_AUTH_INFO)
        info.dwInfoVersion = 1
        info.pbNonce = ctypes.cast(nonce_buf, ctypes.POINTER(ctypes.c_ubyte))
        info.cbNonce = len(nonce)
        info.pbTag = ctypes.cast(tag_buf, ctypes.POINTER(ctypes.c_ubyte))
        info.cbTag = len(tag)

        ct_buf = ctypes.create_string_buffer(ciphertext, len(ciphertext))
        out_buf = ctypes.create_string_buffer(len(ciphertext) + 16)
        out_len = wt.ULONG(0)
        status = _bcrypt.BCryptDecrypt(
            h_key, ct_buf, len(ciphertext), ctypes.byref(info),
            None, 0, out_buf, len(out_buf), ctypes.byref(out_len), 0)
        if status != 0:
            raise EdgeCookieError("Cookie 解密失败（GCM 校验不通过，status=%d）" % status)
        return out_buf.raw[:out_len.value]
    finally:
        if h_key:
            _bcrypt.BCryptDestroyKey(h_key)
        _bcrypt.BCryptCloseAlgorithmProvider(h_alg, 0)


# --------------------------------------------------------------------------
# 浏览器 Cookie 库
# --------------------------------------------------------------------------
BROWSERS = {
    "edge": ("Microsoft", "Edge"),
    "chrome": ("Google", "Chrome"),
}

PROFILES = ("Default",) + tuple("Profile %d" % i for i in range(1, 10))

# 只取这几个：带上 buvid3 反而可能让评论被隐藏（实测）
WANTED = ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5")


def user_data_dir(browser: str = "edge") -> str:
    vendor, product = BROWSERS.get(browser, BROWSERS["edge"])
    return os.path.join(os.environ.get("LOCALAPPDATA", ""), vendor, product, "User Data")


def find_profiles(browser: str = "edge"):
    """返回 [(profile_name, cookie_db_path, local_state_path), ...]"""
    base = user_data_dir(browser)
    local_state = os.path.join(base, "Local State")
    out = []
    for name in PROFILES:
        db = os.path.join(base, name, "Network", "Cookies")
        if os.path.isfile(db):
            out.append((name, db, local_state))
    return out


def _writable_dir(path: str) -> bool:
    try:
        probe = os.path.join(path, ".write_test_%d" % os.getpid())
        with open(probe, "wb") as fh:
            fh.write(b"x")
        os.remove(probe)
        return True
    except OSError:
        return False


def _copy_db(path: str) -> str:
    """把被浏览器占用的 Cookie 库复制到可写目录。"""
    base_dirs = [tempfile.gettempdir(), os.path.dirname(os.path.abspath(__file__))]
    last_err = None
    for base in base_dirs:
        if not _writable_dir(base):
            continue
        tmp = os.path.join(base, "bili_cookies_%d_%d.db"
                           % (os.getpid(), int(time.time() * 1000)))
        try:
            shutil.copy2(path, tmp)
            return tmp
        except PermissionError as exc:
            last_err = exc
            break          # 源文件被锁，换目录也没用
        except OSError as exc:
            last_err = exc
            continue
    if isinstance(last_err, PermissionError):
        raise EdgeCookieError(
            "浏览器正在运行，Cookie 库被锁定。请先完全退出 Edge"
            "（任务栏图标右键 → 退出），再重新运行。")
    raise EdgeCookieError("复制 Cookie 库失败：%s" % last_err)


def decrypt_value(enc: bytes, key: bytes) -> str:
    if not enc:
        return ""
    if enc[:3] in (b"v10", b"v11"):
        nonce, ciphertext, tag = enc[3:15], enc[15:-16], enc[-16:]
        raw = aes_gcm_decrypt(key, nonce, ciphertext, tag)
        return raw.decode("utf-8", errors="replace")
    if enc[:3] == b"v20":
        raise EdgeCookieError(
            "该 Cookie 使用了 v20（App-Bound）加密，无法直接解密。"
            "请在浏览器里退出登录后重新登录，或用 --cookie 手动传入。")
    # 极老版本是裸 DPAPI
    try:
        return _dpapi_decrypt(enc).decode("utf-8", errors="replace")
    except Exception:
        raise EdgeCookieError("无法识别的 Cookie 加密格式")


def read_bilibili_cookies(browser: str = "edge", profile: str = "",
                          allow_cdp: bool = True) -> dict:
    """从浏览器读 B站 Cookie，返回 {name: value}。

    先走 v10/v11 直接解密；一旦发现 v20（App-Bound），改用「无头浏览器 + 调试协议」
    让浏览器自己解密（allow_cdp=False 时直接报错）。
    """
    profiles = find_profiles(browser)
    if not profiles:
        raise EdgeCookieError("没有找到 %s 的 Cookie 库（可能没装或没登录过）" % browser)
    if profile:
        profiles = [p for p in profiles if p[0].lower() == profile.lower()] or profiles

    errors = []
    need_cdp = False
    for name, db_path, local_state in profiles:
        try:
            key = load_master_key(local_state)
            tmp = _copy_db(db_path)
            try:
                conn = sqlite3.connect(tmp)
                try:
                    now_us = int((time.time() + CHROME_EPOCH_OFFSET) * 1_000_000)
                    rows = conn.execute(
                        "select host_key, name, value, encrypted_value, expires_utc "
                        "from cookies where host_key like '%bilibili.com'").fetchall()
                finally:
                    conn.close()
            finally:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        except EdgeCookieError as exc:
            errors.append("%s: %s" % (name, exc))
            continue

        found = {}
        for host, cname, value, enc, expires in rows:
            if cname not in WANTED:
                continue
            if expires and int(expires) < now_us:
                continue
            try:
                val = value or decrypt_value(enc, key)
            except EdgeCookieError as exc:
                if "v20" in str(exc):
                    need_cdp = True
                errors.append("%s/%s: %s" % (name, cname, exc))
                continue
            if not val:
                continue
            # 同一名字只保留一个（优先 .bilibili.com 这种更长的域）
            old = found.get(cname)
            if old is None or len(host) > old[1]:
                found[cname] = (val, len(host))
        if found:
            result = {k: v[0] for k, v in found.items()}
            result["_profile"] = name
            return result

    if need_cdp:
        if not allow_cdp:
            raise EdgeCookieError(
                "该浏览器的 Cookie 使用 v20（App-Bound）加密，无法直接解密。")
        try:
            return read_via_cdp(browser, profile)
        except EdgeCookieError as exc:
            raise EdgeCookieError(
                "%s\n    兜底 1：手动复制 SESSDATA 与 bili_jct，用 "
                "--cookie \"SESSDATA=...; bili_jct=...\" 传入。\n"
                "    兜底 2（管理员）：关闭 Edge 的 App-Bound 加密后重新登录，"
                "Cookie 会变回 v10：\n"
                "      reg add \"HKLM\\SOFTWARE\\Policies\\Microsoft\\Edge\" "
                "/v ApplicationBoundEncryptionEnabled /t REG_DWORD /d 0 /f" % exc)
    if errors:
        raise EdgeCookieError("；".join(errors[:3]))
    raise EdgeCookieError("在 %s 的 Cookie 里没找到 B站登录信息（可能没登录过）" % browser)


def cookie_header(cookies: dict) -> str:
    """按固定顺序拼成 Cookie 头。"""
    order = ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5")
    parts = []
    for name in order:
        if cookies.get(name):
            parts.append("%s=%s" % (name, cookies[name]))
    return "; ".join(parts)


# --------------------------------------------------------------------------
# v20 兜底：复制配置 → 启动无头浏览器 → DevTools 协议取 Cookie
# --------------------------------------------------------------------------
BROWSER_EXES = {
    "edge": (
        r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
        r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
        r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe",
    ),
    "chrome": (
        r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
        r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
        r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
    ),
}


def find_browser_exe(browser: str = "edge") -> str:
    for tpl in BROWSER_EXES.get(browser, BROWSER_EXES["edge"]):
        path = os.path.expandvars(tpl)
        if os.path.isfile(path):
            return path
    raise EdgeCookieError("找不到 %s 的可执行文件（可能没安装）" % browser)


class _WebSocket:
    """够用就好的 WebSocket 客户端（只连本机 CDP，消息都很小）。"""

    def __init__(self, url: str, timeout: float = 10):
        import re as _re
        m = _re.match(r"ws://([^/:]+):(\d+)(/.*)?$", url)
        if not m:
            raise EdgeCookieError("调试地址无法解析：%s" % url)
        host, port, path = m.group(1), int(m.group(2)), m.group(3) or "/"
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = ("GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\n"
               "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n" % (path, host, port, key))
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise EdgeCookieError("调试连接握手失败")
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise EdgeCookieError("调试连接被拒绝：%s" % head.split(b"\r\n")[0][:60])
        self.buf = rest

    def _read_exact(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EdgeCookieError("调试连接已断开")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, obj: dict):
        payload = json.dumps(obj).encode("utf-8")
        mask = os.urandom(4)
        n = len(payload)
        header = bytes([0x81])
        if n < 126:
            header += bytes([0x80 | n])
        elif n < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def recv(self) -> dict:
        while True:
            b0, b1 = self._read_exact(2)
            opcode = b0 & 0x0F
            masked = b1 & 0x80
            ln = b1 & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", self._read_exact(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if masked else None
            data = self._read_exact(ln)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if opcode == 8:
                raise EdgeCookieError("调试连接被关闭")
            if opcode in (1, 2):
                try:
                    return json.loads(data.decode("utf-8", errors="replace"))
                except ValueError:
                    continue

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def _http_json(port: int, path: str, timeout: float = 5):
    import urllib.request
    url = "http://127.0.0.1:%d%s" % (port, path)
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def read_via_cdp(browser: str = "edge", profile: str = "", timeout: float = 40) -> dict:
    """v20 兜底：用无头浏览器自己解密 Cookie。"""
    exe = find_browser_exe(browser)
    profiles = find_profiles(browser)
    if not profiles:
        raise EdgeCookieError("没有找到可用的浏览器配置")
    if profile:
        profiles = [p for p in profiles if p[0].lower() == profile.lower()] or profiles
    name, db_path, local_state = profiles[0]

    base = tempfile.gettempdir() if _writable_dir(tempfile.gettempdir()) \
        else os.path.dirname(os.path.abspath(__file__))
    tmp = os.path.join(base, "bili_cdp_%d_%d" % (os.getpid(), int(time.time())))
    net_dir = os.path.join(tmp, "Default", "Network")
    proc = None
    try:
        try:
            os.makedirs(net_dir, exist_ok=True)
            shutil.copy2(local_state, os.path.join(tmp, "Local State"))
            for fname in ("Cookies", "Cookies-wal", "Cookies-shm"):
                src = os.path.join(os.path.dirname(db_path), fname)
                if os.path.isfile(src):
                    shutil.copy2(src, os.path.join(net_dir, fname))
        except PermissionError:
            raise EdgeCookieError(
                "浏览器正在运行，Cookie 库被锁定。请先完全退出浏览器，再重新运行。")
        except OSError as exc:
            raise EdgeCookieError("复制浏览器配置失败：%s" % exc)

        try:
            proc = subprocess.Popen(
                [exe, "--headless=new", "--disable-gpu", "--no-first-run",
                 "--no-default-browser-check", "--disable-extensions",
                 "--remote-debugging-port=0", "--user-data-dir=%s" % tmp,
                 "about:blank"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            raise EdgeCookieError("无法启动无头浏览器（%s）：%s" % (os.path.basename(exe), exc))

        port_file = os.path.join(tmp, "DevToolsActivePort")
        deadline = time.time() + timeout
        port = 0
        while time.time() < deadline:
            if os.path.isfile(port_file):
                try:
                    with open(port_file, encoding="utf-8") as fh:
                        port = int((fh.readline() or "0").strip())
                except (OSError, ValueError):
                    port = 0
                if port:
                    break
            if proc.poll() is not None:
                raise EdgeCookieError("无头浏览器启动后立即退出了（可能被安全策略阻止）")
            time.sleep(0.3)
        if not port:
            raise EdgeCookieError("等待浏览器调试端口超时")

        try:
            targets = _http_json(port, "/json/list")
        except Exception as exc:
            raise EdgeCookieError("连不上无头浏览器的调试端口：%s" % exc)
        ws_url = ""
        for t in targets:
            if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                ws_url = t["webSocketDebuggerUrl"]
                break
        if not ws_url:
            raise EdgeCookieError("没有找到可用的调试页面")

        try:
            ws = _WebSocket(ws_url, timeout=15)
        except (OSError, ValueError) as exc:
            raise EdgeCookieError("与调试页面握手失败：%s" % exc)
        try:
            ws.send({"id": 1, "method": "Network.enable"})
            ws.send({"id": 2, "method": "Network.getCookies",
                     "params": {"urls": ["https://www.bilibili.com",
                                         "https://api.bilibili.com",
                                         "https://passport.bilibili.com"]}})
            cookies = None
            while time.time() < deadline:
                msg = ws.recv()
                if msg.get("id") == 2:
                    cookies = (msg.get("result") or {}).get("cookies") or []
                    break
        except (OSError, ValueError) as exc:
            raise EdgeCookieError("读取调试数据失败：%s" % exc)
        finally:
            ws.close()
        if cookies is None:
            raise EdgeCookieError("浏览器没有返回 Cookie（调试超时）")

        found = {}
        for c in cookies:
            cname = c.get("name") or ""
            if cname not in WANTED or not c.get("value"):
                continue
            if c.get("expires", -1) not in (-1, 0) and c["expires"] < time.time():
                continue
            old = found.get(cname)
            if old is None or len(c.get("domain") or "") > old[1]:
                found[cname] = (c["value"], len(c.get("domain") or ""))
        if not found:
            raise EdgeCookieError("浏览器里没有 B站登录 Cookie（可能没登录）")
        result = {k: v[0] for k, v in found.items()}
        result["_profile"] = name
        return result
    finally:
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        shutil.rmtree(tmp, ignore_errors=True)
