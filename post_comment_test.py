#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
post_comment_test.py —— 最小可用的 B站评论发送测试（纯标准库，无需 pip 安装）

用途
    验证「账号能真正发出评论」这一件事，排除签名/风控/登录的干扰。

用法
    python post_comment_test.py                      # 发默认测试评论
    python post_comment_test.py --message "你好"      # 自定义内容
    python post_comment_test.py --bvid BV1xx411c7mD

可选：用 DeepSeek 生成评论内容（需要环境变量 DEEPSEEK_API_KEY）
    set DEEPSEEK_API_KEY=sk-xxxx
    python post_comment_test.py --deepseek

说明
    · 2026-10 实测：只要有有效的 SESSDATA + bili_jct，直接 POST
      https://api.bilibili.com/x/v2/reply/add 就能发出去，不需要 WBI 签名。
    · 若返回 -352/-403（风控）脚本会自动带上 WBI+DM 签名重试一次。
    · 若返回 -101，说明 SESSDATA 会话已失效 —— 换新 Cookie，代码救不了。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# ============================ 配置区 ============================
# ===============================================================
# 配置：不要在源码里写 Cookie。
# 优先读环境变量 BILI_SESSDATA / BILI_JCT，其次读同目录 cookie.txt
# ===============================================================
def _load_cookie_pair():
    sess = os.environ.get("BILI_SESSDATA", "").strip()
    jct = os.environ.get("BILI_JCT", "").strip()
    if sess and jct:
        return sess, jct
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookie.txt")
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                txt = fh.read()
            m1 = re.search(r"SESSDATA=([^;\s]+)", txt)
            m2 = re.search(r"bili_jct=([0-9a-fA-F]+)", txt)
            if m1:
                sess = sess or m1.group(1)
            if m2:
                jct = jct or m2.group(1)
        except OSError:
            pass
    return sess, jct


SESSDATA, BILI_JCT = _load_cookie_pair()
TARGET_BVID = ""                       # 必须用 --bvid 指定目标视频
DEFAULT_MESSAGE = "测试评论：来自自动化脚本，{ts}"
# ===============================================================

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

# WBI 重排表（风控签名用，普通 POST 走不通时才会用到）
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

# 常见错误码 -> 人话
ERROR_HINTS = {
    -101: "账号未登录：SESSDATA 会话已失效，必须换新 Cookie（在别处登录会立刻作废旧值）",
    -111: "CSRF 校验失败：bili_jct 与 SESSDATA 不是同一次登录产生的",
    -352: "风控校验失败：需要 WBI/DM 签名或账号被风控盯上",
    -403: "权限不足：可能被风控拦截，或评论区有权限限制",
    -509: "请求过于频繁",
    12002: "评论区已关闭",
    12006: "没有该评论",
    12015: "需要评论验证码",
    12016: "评论内容包含敏感信息，换个说法",
    12025: "评论字数过多",
    12035: "该账号被 UP 主列入评论黑名单",
    12051: "重复评论，请勿刷屏",
    12074: "账号等级不足（需 LV2+）",
}

_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE


def log(msg):
    print(msg, flush=True)


def build_headers(referer="https://www.bilibili.com/"):
    return {
        "User-Agent": UA,
        "Referer": referer,
        "Origin": "https://www.bilibili.com",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Cookie": "SESSDATA=%s; bili_jct=%s" % (SESSDATA, BILI_JCT),
    }


def get_json(url, referer="https://www.bilibili.com/"):
    req = urllib.request.Request(url, headers=build_headers(referer))
    with urllib.request.urlopen(req, timeout=20, context=_ctx) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def post_form(url, data: dict, referer="https://www.bilibili.com/"):
    headers = build_headers(referer)
    headers["Content-Type"] = "application/x-www-form-urlencoded"
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=20, context=_ctx) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


# ---------------------------- 步骤 1：验证登录 ----------------------------
def check_login():
    d = get_json("https://api.bilibili.com/x/web-interface/nav")
    if d.get("code") != 0:
        log("[×] 登录校验失败：%s %s" % (d.get("code"), d.get("message")))
        return None
    data = d.get("data") or {}
    if not data.get("isLogin"):
        log("[×] 未登录：SESSDATA 无效或已过期")
        log("    解决办法：重新登录 bilibili.com，F12 复制新的 SESSDATA + bili_jct")
        return None
    li = data.get("level_info") or {}
    log("[√] 已登录：%s (UID: %s) 等级 LV%s（经验 %s/%s）" % (
        data.get("uname"), data.get("mid"), li.get("current_level"),
        li.get("current_exp"), li.get("next_exp")))
    return data


# ---------------------------- 步骤 2：bvid -> aid ----------------------------
def get_aid(bvid):
    d = get_json("https://api.bilibili.com/x/web-interface/view?bvid=" + bvid)
    if d.get("code") != 0:
        raise RuntimeError("获取视频信息失败：%s %s" % (d.get("code"), d.get("message")))
    v = d.get("data") or {}
    log("[√] 视频：%s | aid=%s" % (v.get("title"), v.get("aid")))
    return int(v["aid"])


# ---------------------------- 步骤 3：准备评论内容 ----------------------------
def generate_with_deepseek(hint=""):
    """可选：用 DeepSeek 生成一句评论（模仿网上 DeepSeek 评论机器人的做法）。"""
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        return None
    prompt = ("你是一个B站用户，请用一句自然、口语化的中文评论这个视频，"
              "不超过40字，不要用引号，不要带任何前缀。" + (("视频标题：" + hint) if hint else ""))
    body = json.dumps({
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 1.0,
        "max_tokens": 100,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.deepseek.com/chat/completions", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + key}, method="POST")
    with urllib.request.urlopen(req, timeout=30, context=_ctx) as resp:
        d = json.loads(resp.read().decode("utf-8", errors="replace"))
    return d["choices"][0]["message"]["content"].strip()


def prepare_message(args):
    if args.deepseek:
        try:
            txt = generate_with_deepseek()
            if txt:
                log("[i] DeepSeek 生成内容：%s" % txt)
                return txt
            log("[!] 未设置 DEEPSEEK_API_KEY，改用默认内容")
        except Exception as exc:
            log("[!] DeepSeek 调用失败（%s），改用默认内容" % exc)
    return args.message or DEFAULT_MESSAGE.format(ts=int(time.time()))


# ---------------------------- 步骤 4：发评论 ----------------------------
def wbi_sign(query_params: dict):
    """风控兜底：给 query 参数做 WBI 签名，返回 (w_rid, mixin_key 已算好)。"""
    nav = get_json("https://api.bilibili.com/x/web-interface/nav")
    wbi = ((nav.get("data") or {}).get("wbi_img")) or {}
    img = (wbi.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
    sub = (wbi.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
    if not img or not sub:
        return None
    raw = img + sub
    mixin_key = "".join(raw[i] for i in MIXIN_KEY_ENC_TAB)[:32]
    params = dict(query_params)
    params["wts"] = str(int(time.time()))
    cleaned = {k: "".join(c for c in str(v) if c not in "!'()*") for k, v in params.items()}
    query = urllib.parse.urlencode(sorted(cleaned.items()))
    w_rid = hashlib.md5((query + mixin_key).encode()).hexdigest()
    params["w_rid"] = w_rid
    return params


def post_comment(aid, message, bvid=""):
    base_url = "https://api.bilibili.com/x/v2/reply/add"
    data = {
        "type": 1,               # 1 = 视频
        "oid": aid,              # 视频 aid
        "message": message,
        "plat": 1,               # 1 = web 端
        "csrf": BILI_JCT,
    }
    referer = "https://www.bilibili.com/video/%s/" % (bvid or ("av%s" % aid))

    log("[i] 尝试直接发送 ...")
    d = post_form(base_url, data, referer)
    log("    -> code=%s message=%s" % (d.get("code"), d.get("message")))

    if d.get("code") in (-352, -403):
        log("[!] 被风控拦截，改为带 WBI+DM 签名重试 ...")
        try:
            signed = wbi_sign({
                "dm_img_list": "[]",
                "dm_img_str": "".join(random.sample(
                    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789", 2)),
                "dm_cover_img_str": "".join(random.sample(
                    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789", 2)),
                "dm_img_inter": json.dumps({"ds": [], "wh": [0, 0, 0], "of": [0, 0, 0]}),
                "web_location": "1550101",
            })
            url = base_url
            if signed:
                url += "?" + urllib.parse.urlencode(sorted(signed.items()))
            data.update({
                "statistics": json.dumps({"appId": 100, "platform": 5}),
                "gaia_source": "main_web",
                "csrf_token": BILI_JCT,
            })
            d = post_form(url, data, referer)
            log("    -> code=%s message=%s" % (d.get("code"), d.get("message")))
        except Exception as exc:
            log("[!] 签名重试失败：%s" % exc)

    if d.get("code") != 0:
        code = d.get("code")
        log("[×] 发送失败！")
        log("    code=%s message=%s" % (code, d.get("message")))
        hint = ERROR_HINTS.get(code)
        if hint:
            log("    原因：%s" % hint)
        return None
    rpid = (d.get("data") or {}).get("rpid")
    log("[√] 发送成功！rpid=%s" % rpid)
    return str(rpid)


# ---------------------------- 步骤 5：验证评论真的在 ----------------------------
def verify_comment(aid, rpid):
    time.sleep(1)
    d = get_json("https://api.bilibili.com/x/v2/reply?"
                 "type=1&oid=%s&sort=0&ps=20&pn=1" % aid)   # sort=0 按时间倒序
    for r in ((d.get("data") or {}).get("replies") or []):
        if str(r.get("rpid")) == str(rpid):
            member = r.get("member") or {}
            log("[√] 评论已可在评论区看到：%s：%s" % (
                member.get("uname"),
                ((r.get("content") or {}).get("message") or "")[:50]))
            return True
    log("[!] 评论列表里暂时没找到 rpid=%s（可能刚发有延迟，可稍后手动刷新查看）" % rpid)
    return False


def main():
    # Windows 控制台默认 GBK，强制 UTF-8，避免中文/emoji 打印报错
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="B站评论发送测试")
    parser.add_argument("--bvid", default=TARGET_BVID, required=not TARGET_BVID,
                        help="目标视频 BV 号（必填，例如 BV1xx411c7mD）")
    parser.add_argument("--message", default=None, help="自定义评论内容")
    parser.add_argument("--deepseek", action="store_true",
                        help="用 DeepSeek 生成评论（需环境变量 DEEPSEEK_API_KEY）")
    args = parser.parse_args()

    log("=" * 60)
    log("B站评论发送测试  |  %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    log("=" * 60)

    if not check_login():
        return 1
    aid = get_aid(args.bvid)
    message = prepare_message(args)
    log("[i] 将发送：%s" % message)

    rpid = post_comment(aid, message, args.bvid)
    if not rpid:
        return 1
    verify_comment(aid, rpid)
    log("-" * 60)
    log("完成。评论地址：https://www.bilibili.com/video/%s/#reply%s" % (args.bvid, rpid))
    return 0


if __name__ == "__main__":
    sys.exit(main())
