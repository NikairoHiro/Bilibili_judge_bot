# -*- coding: utf-8 -*-
"""edge_cookie 的自测：AES-GCM 往返、v10 解析、错误处理。"""
import ctypes
import ctypes.wintypes as wt
import os
import sys

sys.path.insert(0, ".")
import edge_cookie as ec

FAILS = []


def check(name, ok, extra=""):
    extra = "" if extra is None else str(extra)
    print(("[OK ] " if ok else "[BAD] ") + name + (("  " + extra) if extra else ""))
    if not ok:
        FAILS.append(name)


# ---- 用 bcrypt 加密，验证我们的解密实现 ----
def gcm_encrypt(key, nonce, plaintext, aad=b""):
    h_alg = ec._open_aes_gcm()
    h_key = None
    try:
        h_key = ec._gen_key(h_alg, key)
        nonce_buf = (ctypes.c_ubyte * len(nonce)).from_buffer_copy(nonce)
        tag_buf = (ctypes.c_ubyte * 16)()
        info = ec._AUTH_INFO()
        info.cbSize = ctypes.sizeof(ec._AUTH_INFO)
        info.dwInfoVersion = 1
        info.pbNonce = ctypes.cast(nonce_buf, ctypes.POINTER(ctypes.c_ubyte))
        info.cbNonce = len(nonce)
        info.pbTag = ctypes.cast(tag_buf, ctypes.POINTER(ctypes.c_ubyte))
        info.cbTag = 16
        pt = ctypes.create_string_buffer(plaintext, len(plaintext))
        out = ctypes.create_string_buffer(len(plaintext) + 16)
        out_len = wt.ULONG(0)
        status = ec._bcrypt.BCryptEncrypt(
            h_key, pt, len(plaintext), ctypes.byref(info), None, 0,
            out, len(out), ctypes.byref(out_len), 0)
        if status != 0:
            raise RuntimeError("encrypt status %d" % status)
        return out.raw[:out_len.value], bytes(tag_buf)
    finally:
        if h_key:
            ec._bcrypt.BCryptDestroyKey(h_key)
        ec._bcrypt.BCryptCloseAlgorithmProvider(h_alg, 0)


key = bytes(range(32))
nonce = bytes(range(12))
plain = "SESSDATA=abc%2Cdef; bili_jct=xyz".encode("utf-8")
ct, tag = gcm_encrypt(key, nonce, plain)
check("bcrypt 加解密往返", ec.aes_gcm_decrypt(key, nonce, ct, tag) == plain)

bad_tag = bytes([tag[0] ^ 0xFF]) + tag[1:]
try:
    ec.aes_gcm_decrypt(key, nonce, ct, bad_tag)
    check("篡改 tag 应当失败", False, "居然解密成功")
except ec.EdgeCookieError:
    check("篡改 tag 应当失败", True)

# v10 结构
v10 = b"v10" + nonce + ct + tag
check("v10 解码", ec.decrypt_value(v10, key) == plain.decode())

try:
    ec.decrypt_value(b"v20" + b"\x00" * 40, key)
    check("v20 应明确报错", False)
except ec.EdgeCookieError as exc:
    check("v20 应明确报错", "v20" in str(exc), str(exc)[:30])

# Cookie 头拼接
header = ec.cookie_header({"SESSDATA": "s", "bili_jct": "j", "DedeUserID": "1"})
check("Cookie 头顺序", header == "SESSDATA=s; bili_jct=j; DedeUserID=1", header)

# 找配置
profiles = ec.find_profiles("edge")
check("能定位 Edge 配置目录", len(profiles) >= 1, [p[0] for p in profiles])


# ---- 端到端：造一个假的 Edge 数据目录，验证整条读取链路 ----
def dpapi_protect(data: bytes) -> bytes:
    buf = ctypes.create_string_buffer(data, len(data))
    in_blob = ec._DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out_blob = ec._DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob))
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)


import base64
import json
import shutil
import sqlite3
import tempfile

tmpdir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "_ec_test_tmp_%d" % os.getpid())
os.makedirs(tmpdir, exist_ok=True)
try:
    user_data = os.path.join(tmpdir, "User Data")
    net = os.path.join(user_data, "Default", "Network")
    os.makedirs(net, exist_ok=True)
    master = bytes(range(32))
    enc_key = base64.b64encode(b"DPAPI" + dpapi_protect(master)).decode()
    with open(os.path.join(user_data, "Local State"), "w", encoding="utf-8") as fh:
        json.dump({"os_crypt": {"encrypted_key": enc_key}}, fh)

    def enc_cookie(text):
        n = bytes(range(12))
        c, t = gcm_encrypt(master, n, text.encode())
        return b"v10" + n + c + t

    db = os.path.join(net, "Cookies")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE cookies(host_key TEXT, name TEXT, value TEXT, "
                 "encrypted_value BLOB, expires_utc INTEGER)")
    conn.execute("INSERT INTO cookies VALUES(?,?,?,?,0)",
                 (".bilibili.com", "SESSDATA", "", enc_cookie("sess-abc")))
    conn.execute("INSERT INTO cookies VALUES(?,?,?,?,0)",
                 (".bilibili.com", "bili_jct", "", enc_cookie("jct-xyz")))
    conn.execute("INSERT INTO cookies VALUES(?,?,?,?,0)",
                 (".bilibili.com", "buvid3", "", enc_cookie("should-be-ignored")))
    conn.execute("INSERT INTO cookies VALUES(?,?,?,?,1)",   # 过期
                 (".bilibili.com", "DedeUserID", "", enc_cookie("expired")))
    conn.commit()
    conn.close()

    orig = ec.user_data_dir
    ec.user_data_dir = lambda browser="edge": user_data
    try:
        got = ec.read_bilibili_cookies("edge")
    finally:
        ec.user_data_dir = orig

    check("端到端：SESSDATA 解出", got.get("SESSDATA") == "sess-abc", got.get("SESSDATA"))
    check("端到端：bili_jct 解出", got.get("bili_jct") == "jct-xyz", got.get("bili_jct"))
    check("端到端：无关 Cookie 被忽略", "buvid3" not in got, list(got))
    check("端到端：过期 Cookie 被跳过", "DedeUserID" not in got, list(got))
    check("端到端：拼出的 Cookie 头",
          ec.cookie_header(got) == "SESSDATA=sess-abc; bili_jct=jct-xyz",
          ec.cookie_header(got))
finally:
    shutil.rmtree(tmpdir, ignore_errors=True)


# ---- WebSocket 客户端（CDP 用）用本地回环服务器验证 ----
import base64 as _b64
import hashlib as _hashlib
import json as _json
import socket as _socket
import threading as _threading


def _ws_server(port_holder):
    srv = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    srv.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port_holder.append(srv.getsockname()[1])

    def run():
        conn, _ = srv.accept()
        data = b""
        while b"\r\n\r\n" not in data:
            data += conn.recv(4096)
        head = data.split(b"\r\n\r\n")[0].decode()
        key = ""
        for line in head.split("\r\n"):
            if line.lower().startswith("sec-websocket-key:"):
                key = line.split(":", 1)[1].strip()
        accept = _b64.b64encode(_hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        conn.sendall(("HTTP/1.1 101 Switching Protocols\r\n"
                      "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                      "Sec-WebSocket-Accept: %s\r\n\r\n" % accept).encode())
        # 读一帧，回一帧 JSON
        hdr = conn.recv(2)
        ln = hdr[1] & 0x7F
        if ln == 126:
            ln = int.from_bytes(conn.recv(2), "big")
        mask = conn.recv(4)
        payload = b""
        while len(payload) < ln:
            payload += conn.recv(ln - len(payload))
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        resp = _json.dumps({"id": 2, "result": {"cookies": [{"name": "echo",
                                                             "value": payload.decode()}]}}).encode()
        frame = bytes([0x81, len(resp)]) + resp
        conn.sendall(frame)
        conn.close()
        srv.close()

    _threading.Thread(target=run, daemon=True).start()


ports = []
_ws_server(ports)
import time as _time

for _ in range(50):
    if ports:
        break
    _time.sleep(0.02)
ws = ec._WebSocket("ws://127.0.0.1:%d/devtools/page/1" % ports[0])
ws.send({"id": 1, "method": "ping"})
msg = ws.recv()
ws.close()
check("WebSocket 收发（CDP 帧）",
      msg.get("id") == 2 and msg["result"]["cookies"][0]["value"].startswith("{\"id\": 1"),
      str(msg)[:60])

check("能定位浏览器可执行文件", os.path.isfile(ec.find_browser_exe("edge")),
      ec.find_browser_exe("edge"))

print()
if FAILS:
    print("[×] 失败 %d 项：%s" % (len(FAILS), FAILS))
    sys.exit(1)
print("[√] edge_cookie 自测通过")
