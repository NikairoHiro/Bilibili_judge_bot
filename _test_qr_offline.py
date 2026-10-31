# -*- coding: utf-8 -*-
"""qr_offline.py 的自测：矩阵 / 自校验 / PNG 渲染 三层验证。"""
import base64
import os
import re
import struct
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import qr_offline as q  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_test_out")
os.makedirs(OUT, exist_ok=True)

URL = ("https://account.bilibili.com/h5/account-h5/auth/scan-web"
       "?navhide=1&callback=close&qrcode_key=7a02a8412e1313affbf2c39ab820e655&from=")

fails = []


def check(name, cond, extra=""):
    print(("[OK ] " if cond else "[BAD] ") + name + (("  " + extra) if extra else ""))
    if not cond:
        fails.append(name)


# ---------- 1. 结构与格式 ----------
qr = q.QRCode(URL)
count = len(qr.modules)
check("矩阵为正方形且边长 = 4*version+17", count == qr.version * 4 + 17,
      "version=%d size=%d" % (qr.version, count))

check("左上定位图形中心 3x3 全深色",
      all(qr.modules[2 + r][2 + c] for r in range(3) for c in range(3)))
check("右上定位图形中心 3x3 全深色",
      all(qr.modules[2 + r][count - 5 + c] for r in range(3) for c in range(3)))
check("左下定位图形中心 3x3 全深色",
      all(qr.modules[count - 5 + r][2 + c] for r in range(3) for c in range(3)))
check("固定深色模块 (count-8, 8) 为深色", qr.modules[count - 8][8] is True)
check("水平定时图形交替",
      all(qr.modules[6][c] == (c % 2 == 0) for c in range(8, count - 8)))
check("垂直定时图形交替",
      all(qr.modules[r][6] == (r % 2 == 0) for r in range(8, count - 8)))

# ---------- 2. 自校验（反向解码） ----------
try:
    check("selfcheck 还原登录链接", q.selfcheck(URL) == URL)
except Exception as exc:
    check("selfcheck 还原登录链接", False, repr(exc))

ec_versions = {}
for ec, label in ((q.ERROR_CORRECT_L, "L"), (q.ERROR_CORRECT_M, "M"),
                  (q.ERROR_CORRECT_Q, "Q"), (q.ERROR_CORRECT_H, "H")):
    qr_ec = q.QRCode(URL, error_correction=ec)
    ec_versions[label] = qr_ec.version
    check("纠错等级 %s 可编码" % label, 1 <= qr_ec.version <= 40,
          "version=%d" % qr_ec.version)
check("纠错等级越高所需版本不减少",
      ec_versions["L"] <= ec_versions["M"] <= ec_versions["Q"] <= ec_versions["H"],
      str(ec_versions))

# ---------- 3. PNG 渲染验证 ----------
png = qr.to_png_bytes(scale=4, border=4)
check("PNG 魔数正确", png[:8] == b"\x89PNG\r\n\x1a\n")
check("PNG 以 IEND 结束", png[-12:] == b"\x00\x00\x00\x00IEND\xaeB`\x82")

# 解析 PNG：逐个校验 chunk 的 CRC，并检查尺寸/位深
pos = 8
idat = b""
width = height = 0
crc_ok = True
chunks = []
while pos < len(png):
    length = struct.unpack(">I", png[pos:pos + 4])[0]
    tag = png[pos + 4:pos + 8]
    payload = png[pos + 8:pos + 8 + length]
    crc = struct.unpack(">I", png[pos + 8 + length:pos + 12 + length])[0]
    if crc != (zlib.crc32(tag + payload) & 0xFFFFFFFF):
        crc_ok = False
    chunks.append(tag)
    if tag == b"IHDR":
        width, height, depth, ctype = struct.unpack(">IIBB", payload[:10])
    elif tag == b"IDAT":
        idat += payload
    pos += 12 + length

scale, border = 4, 4
check("PNG chunk CRC 全部正确", crc_ok)
check("PNG chunk 顺序 IHDR->IDAT->IEND",
      chunks[0] == b"IHDR" and chunks[-1] == b"IEND" and b"IDAT" in chunks)
check("PNG 尺寸 = (模块数+2*border)*scale",
      width == (len(qr.modules) + border * 2) * scale, "%dx%d" % (width, height))
check("PNG 为 8bit 灰度图", depth == 8 and ctype == 0)

raw = zlib.decompress(idat)
stride = width + 1  # 每行 1 字节滤波器
bad = 0
for r in range(len(qr.modules)):
    for c in range(len(qr.modules)):
        px = raw[(border + r) * scale * stride + 1 + (border + c) * scale]
        dark_pixel = (px == 0)
        if dark_pixel != bool(qr.modules[r][c]):
            bad += 1
check("PNG 像素与矩阵逐模块一致", bad == 0, "不一致模块数=%d" % bad)

# 安静区必须全白
quiet_ok = True
for i in range(border):
    for x in range(width):
        if raw[i * stride + 1 + x] != 0xFF:
            quiet_ok = False
        if raw[(height - 1 - i) * stride + 1 + x] != 0xFF:
            quiet_ok = False
check("PNG 四周安静区为白色", quiet_ok)

# ---------- 4. 落盘 + HTML data-URI ----------
png_path = os.path.join(OUT, "qr_test.png")
with open(png_path, "wb") as fh:
    fh.write(png)
check("PNG 文件写出成功", os.path.getsize(png_path) == len(png),
      "%d 字节" % len(png))

b64 = base64.b64encode(png).decode("ascii")
html = ("<html><body><img src='data:image/png;base64,%s'></body></html>" % b64)
check("HTML 内嵌 data-URI（离线可用）", "data:image/png;base64," in html and len(b64) > 100)

# ---------- 5. 边界情况 ----------
try:
    q.QRCode("x" * 3000)
    check("超长内容应报错", False)
except ValueError:
    check("超长内容正确报错", True)

try:
    q.QRCode("中" * 400)
    check("中文多字节可编码", True)
except Exception as exc:
    check("中文多字节可编码", False, repr(exc))

print()
if fails:
    print("[×] 失败 %d 项：%s" % (len(fails), fails))
    sys.exit(1)
print("[√] qr_offline 全部自测通过（含 PNG 逐像素校验）")
print("    PNG 样例：%s" % png_path)
