# -*- coding: utf-8 -*-
"""检查 login 生成的 HTML 里内嵌的二维码是否可用。"""
import base64
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import qr_offline as q  # noqa: E402

html = open("qr_login.html", encoding="utf-8").read()
m = re.search(r"data:image/png;base64,([A-Za-z0-9+/=]+)", html)
print("HTML 内嵌二维码图片 :", bool(m))
if not m:
    sys.exit(1)
b64 = m.group(1)
png = base64.b64decode(b64)
print("base64 长度        :", len(b64))
print("PNG 魔数正确        :", png[:8] == b"\x89PNG\r\n\x1a\n")
with open("_qr_from_html.png", "wb") as fh:
    fh.write(png)
print("导出文件           :", "_qr_from_html.png", os.path.getsize("_qr_from_html.png"), "字节")
print("qr_login.png       :", os.path.getsize("qr_login.png"), "字节")
print("两者一致           :", png == open("qr_login.png", "rb").read())

# 从 PNG 里反解出模块矩阵，确认二维码内容与登录链接一致
import struct
import zlib

pos, idat, width = 8, b"", 0
while pos < len(png):
    length = struct.unpack(">I", png[pos:pos + 4])[0]
    tag = png[pos + 4:pos + 8]
    payload = png[pos + 8:pos + 8 + length]
    if tag == b"IHDR":
        width = struct.unpack(">I", payload[:4])[0]
    elif tag == b"IDAT":
        idat += payload
    pos += 12 + length
raw = zlib.decompress(idat)
print("PNG 尺寸            :", width, "px")

# 用 qr_offline 的解码逻辑验证：重建矩阵 → 读格式信息 → 解出文本
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def decode_matrix(modules):
    count = len(modules)
    bits = 0
    for i in range(15):
        if i < 6:
            bit = modules[i][8]
        elif i < 8:
            bit = modules[i + 1][8]
        else:
            bit = modules[count - 15 + i][8]
        if bit:
            bits |= 1 << i
    found = None
    for ec in range(4):
        for mask in range(8):
            if q._bch_type_info((ec << 3) | mask) == bits:
                found = (ec, mask)
                break
        if found:
            break
    if not found:
        raise ValueError("无法解析格式信息")
    ec, mask_p = found
    blank, _ = q._blank_modules(1 if count == 21 else (count - 17) // 4)
    version = (count - 17) // 4
    blank, _ = q._blank_modules(version)
    mask = q._mask_func(mask_p)
    data = []
    inc, row = -1, count - 1
    for col in range(count - 1, 0, -2):
        if col <= 6:
            col -= 1
        while True:
            for c in (col, col - 1):
                if blank[row][c] is None:
                    v = modules[row][c]
                    if mask(row, c):
                        v = not v
                    data.append(1 if v else 0)
            row += inc
            if row < 0 or count <= row:
                row -= inc
                inc = -inc
                break
    pos2 = 0

    def take(n):
        nonlocal pos2
        val = 0
        for _ in range(n):
            val = (val << 1) | data[pos2]
            pos2 += 1
        return val

    mode = take(4)
    length = take(8 if version < 10 else 16)
    payload = bytes(take(8) for _ in range(length))
    return payload.decode("utf-8")


# 从 PNG 像素还原矩阵（scale=8, border=4）
scale, border = 8, 4
mods = []
for r in range((width - border * 2 * scale) // scale):
    rowbits = []
    for c in range((width - border * 2 * scale) // scale):
        px = raw[(border + r) * scale * (width + 1) + 1 + (border + c) * scale]
        rowbits.append(px == 0)
    mods.append(rowbits)
text = decode_matrix(mods)
print("从二维码解出的内容  :", text[:100])
print("是 B站登录链接      :", "account.bilibili.com" in text and "qrcode_key=" in text)
