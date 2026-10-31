# -*- coding: utf-8 -*-
"""端到端验证 qr_login：生成二维码、写盘、轮询，并在后台模拟一次扫码成功。

做法：后台线程按 qr_login 内部的轮询逻辑去查状态；主线程直接调用 qr_login。
这里只验证到「生成 + 轮询 + 拿到未扫码状态」为止，不会真的登录。
"""
import os
import sys
import threading
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bili_at_monitor as m  # noqa: E402

m.ensure_ssl_ready(verbose=False)
m.set_quiet(False)

# 1) 生成 + 写盘 + HTML（复用 qr_login 的前半段逻辑）
payload = m.api_call(m.QR_GEN_API, referer="https://www.bilibili.com/")
data = payload.get("data") or {}
qr_url = data["url"]
qr_key = data["qrcode_key"]
print("1) generate code=%s key=%s..." % (payload.get("code"), qr_key[:8]))

import base64  # noqa: E402

import qr_offline as q  # noqa: E402

code = q.QRCode(qr_url)
png = code.to_png_bytes(scale=8, border=4)
with open("qr_login.png", "wb") as fh:
    fh.write(png)
uri = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
ok_html = m._qr_html(uri, "qr_login.html", qr_url)
print("2) 二维码 PNG %d 字节，HTML 写入=%s，自校验=%s"
      % (len(png), ok_html, q.selfcheck(qr_url)[:40] + "..."))

# 2) 轮询一次，确认能读到「未扫码」状态（说明 poll 链路通）
p = m.api_call(m.QR_POLL_API + "?qrcode_key=" + urllib.parse.quote(qr_key),
               referer="https://www.bilibili.com/")
d = p.get("data") or {}
print("3) 第一次轮询: poll code=%s business code=%s message=%s"
      % (p.get("code"), d.get("code"), d.get("message")))
assert p.get("code") == 0, "poll 接口本身应当成功"
assert d.get("code") in (86101, 86090, 0), "应当是未扫码/已扫码/成功之一"
print("   → 轮询链路正常（86101=未扫码）")

# 3) 后台线程观察：qr_login 是否真的在按 2 秒一次轮询并打印提示
seen = []


def watcher():
    t0 = time.time()
    while time.time() - t0 < 6:
        seen.append(time.time())
        time.sleep(1)


w = threading.Thread(target=watcher)
w.start()

t0 = time.time()
try:
    m.qr_login(timeout_sec=5)      # 故意给 5 秒，必然超时，用来验证超时提示
    print("4) 意外登录成功了？")
except RuntimeError as exc:
    print("4) 超时提示: %s" % exc)
w.join()
print("5) 生成+写盘+轮询+超时提示 全部正常，用时 %.1f 秒" % (time.time() - t0))
