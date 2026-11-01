# -*- coding: utf-8 -*-
"""验证 cookie.txt 各种写法都能被解析。"""
import argparse
import os
import sys

sys.path.insert(0, ".")
import bili_at_monitor as m

BASE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(BASE, "_cookie_fmt_test.txt")
VALUE = "SESSDATA=abc%2C123%2Cxx; bili_jct=deadbeef"

CASES = [
    ("普通一行", VALUE, "utf-8", True),
    ("带 BOM", VALUE, "utf-8-sig", True),
    ("记事本 UTF-16", VALUE, "utf-16", True),
    ("换行分两行", "SESSDATA=abc%2C123%2Cxx\nbili_jct=deadbeef", "utf-8", True),
    ("带 Cookie: 前缀", "Cookie: " + VALUE, "utf-8", True),
    ("带引号", '"%s"' % VALUE, "utf-8", True),
    ("带注释", "# 我的 cookie\n" + VALUE, "utf-8", True),
    ("只有裸值", "abc%2C123%2Cxx", "utf-8", False),
]


def load(text, enc):
    with open(PATH, "wb") as fh:
        fh.write(text.encode(enc))
    args = argparse.Namespace(cookie=None, sessdata=None, cookie_file=PATH,
                              state="_state.json")
    return m.load_cookie(args)


fails = []
for name, text, enc, need_jct in CASES:
    got = load(text, enc)
    ok = bool(m.extract_cookie_value(got, "SESSDATA"))
    if need_jct:
        ok = ok and "bili_jct=deadbeef" in got
    print(("[OK ] " if ok else "[BAD] ") + "%-14s -> %s" % (name, got[:60]))
    if not ok:
        fails.append(name)

os.remove(PATH)
print()
if fails:
    print("[×] 失败：", fails)
    sys.exit(1)
print("[√] cookie.txt 各种格式都能解析")
