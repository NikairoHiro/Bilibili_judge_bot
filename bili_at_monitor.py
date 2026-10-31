#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bili_at_monitor.py —— B站网页端「@我的」事件抓取脚本（后台定时版）

版本：1.0.0
许可：MIT

功能
    抓取 B 站「消息中心 → @我的」列表（api.bilibili.com/x/msgfeed/at），
    为每条 @ 事件额外抓取「被 @ 的那条评论的父级评论」，并写入同一个 txt 文件：
        · @人（谁 @ 了我）
        · @视频 BV 号
        · 父级评论（内容 / 作者 / 时间）
    如果那条 @ 本身就是父级评论（一级评论），父级评论字段记为 0。

主要行为
    1. 纯标准库，不需要 pip 安装任何依赖（Python 3.8+）。
    2. 默认以后台常驻方式运行：立刻抓一次，然后每隔 --interval 秒抓一次。
    3. 只维护固定文件名（默认 bilibili_at.txt），每次都是「在原文件上更新」，
       不会生成新的 txt；历史记录累计保留在同一个文件里。
    4. 不再输出 @时间，只有父级评论的时间。
    5. 增量去重：同一条 @ 只写一次；上次父级评论没抓到、这次抓到了会自动补全。

用法
    # 前台常驻（默认每 300 秒抓一次，Ctrl+C 退出）
    python bili_at_monitor.py --sessdata 你的SESSDATA

    # 指定抓取间隔（秒）
    python bili_at_monitor.py --sessdata xxx --interval 600

    # 只抓一次就退出（定时任务里用这个）
    python bili_at_monitor.py --sessdata xxx --once

    # 停止正在后台运行的实例
    python bili_at_monitor.py --stop

    # 先验证 Cookie / 看未读数
    python bili_at_monitor.py --check

    # 其它
    python bili_at_monitor.py --login            # 扫码登录
    python bili_at_monitor.py --outfile my.txt   # 换输出文件名（仍然是原地更新）
    python bili_at_monitor.py --days 7           # 只保留最近 7 天的 @
    python bili_at_monitor.py --status           # 看上次抓取时间/累计条数
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

__version__ = "1.0.0"

APP_DIR = os.path.dirname(os.path.abspath(__file__))
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Referer": "https://www.bilibili.com/",
    "Origin": "https://www.bilibili.com",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

AT_API = "https://api.bilibili.com/x/msgfeed/at"
REPLY_API = "https://api.bilibili.com/x/msgfeed/reply"
UNREAD_API = "https://api.bilibili.com/x/msgfeed/unread"
NAV_API = "https://api.bilibili.com/x/web-interface/nav"
VIEW_API = "https://api.bilibili.com/x/web-interface/view"
COMMENT_API = "https://api.bilibili.com/x/v2/reply"          # 顶层评论列表
SUB_COMMENT_API = "https://api.bilibili.com/x/v2/reply/reply"  # 某条评论下的回复
REPLY_ADD_API = "https://api.bilibili.com/x/v2/reply/add"    # 发表评论/回复（POST）
QR_GEN_API = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QR_POLL_API = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
FINGER_API = "https://api.bilibili.com/x/frontend/finger/spi"  # 取 buvid 风控指纹
FOLLOWERS_API = "https://api.bilibili.com/x/relation/followers"  # 我的粉丝列表

DEFAULT_OUTFILE = "bilibili_at.txt"
DEFAULT_STATE = os.path.join(APP_DIR, "_state.json")
DEFAULT_LOG = os.path.join(APP_DIR, "bili_at_monitor.log")
STOP_FILE = os.path.join(APP_DIR, ".stop_flag")

NONE_MARK = "0（本身即父级评论）"       # @ 的就是一级评论
UNKNOWN_MARK = "无法获取（原评论已删除/被折叠或权限不足）"

TYPE_NAMES = {1: "视频", 11: "动态", 12: "专栏", 17: "动态", 2: "话题"}

# ---------------- AI 法官（DeepSeek） ----------------
# 不内置任何密钥：请用 --ai-key、环境变量 DEEPSEEK_API_KEY 或 config.json 提供
DEFAULT_AI_KEY = ""
DEFAULT_AI_MODEL = "deepseek-flash"

# WBI 签名重排表（bilibili-API-collect 公开算法，2023 年 3 月起 B站风控使用）
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

# --------------------------------------------------------------------------
# 日志（同时输出到控制台和日志文件，带时间戳，便于后台运行排查）
# --------------------------------------------------------------------------

_LOG_FH = None
_QUIET = False


def set_quiet(flag: bool):
    global _QUIET
    _QUIET = flag


def log(msg: str, level: str = "i"):
    prefix = {"i": "[i]", "ok": "[√]", "warn": "[!]", "err": "[×]", "n": ""}.get(level, "[i]")
    line = "%s %s %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), prefix, msg)
    if not _QUIET:
        try:
            print(line)
        except Exception:
            pass
    global _LOG_FH
    try:
        if _LOG_FH is None:
            _LOG_FH = open(DEFAULT_LOG, "a", encoding="utf-8")
        _LOG_FH.write(line + "\n")
        _LOG_FH.flush()
    except Exception:
        pass


# --------------------------------------------------------------------------
# HTTP：SSL 兜底 + Cookie 注入 + 重试
# --------------------------------------------------------------------------

_HTTP_CTX = None
_SSL_WARNED = False


def _build_context(verify: bool):
    import ssl
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _friendly_ssl_error(exc: BaseException) -> bool:
    text = str(exc)
    return "CERTIFICATE_VERIFY_FAILED" in text or "certificate verify failed" in text


def ensure_ssl_ready(verbose: bool = True) -> bool:
    """探测一次 HTTPS 证书链；本机 Python 缺 CA 时自动降级为不校验。"""
    global _HTTP_CTX, _SSL_WARNED
    if _HTTP_CTX is not None:
        return True

    def _probe(ctx):
        req = urllib.request.Request(NAV_API, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=15, context=ctx):
                pass
        except urllib.error.HTTPError:
            return  # 能拿到 HTTP 响应就说明 TLS 握手成功

    try:
        _probe(_build_context(True))
        _HTTP_CTX = _build_context(True)
        return True
    except Exception as exc:
        if not _friendly_ssl_error(exc):
            _HTTP_CTX = _build_context(True)
            return True
        try:
            _probe(_build_context(False))
        except Exception:
            _HTTP_CTX = _build_context(True)
            return True
        _HTTP_CTX = _build_context(False)
        _SSL_WARNED = True
        if verbose:
            log("本机 Python 缺少 CA 证书，已自动切换为「不校验证书」模式继续运行。", "warn")
            log("    彻底解决：python -m pip install --upgrade certifi")
        return False


def http_get_json(url, cookie="", timeout=20, retries=3, delay=1.0, referer=None, verify=True):
    global _HTTP_CTX, _SSL_WARNED
    last_err = None
    for attempt in range(1, retries + 1):
        if _HTTP_CTX is None:
            _HTTP_CTX = _build_context(verify)
        if verify and _SSL_WARNED and _HTTP_CTX.verify_mode:
            import ssl
            if _HTTP_CTX.verify_mode == ssl.CERT_NONE:
                verify = False
        if not verify and _HTTP_CTX.verify_mode:
            import ssl
            if _HTTP_CTX.verify_mode != ssl.CERT_NONE:
                _HTTP_CTX = _build_context(False)
        headers = dict(HEADERS)
        if referer:
            headers["Referer"] = referer
        if cookie:
            headers["Cookie"] = cookie
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=_HTTP_CTX) as resp:
                raw = resp.read()
            return json.loads(raw.decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")[:200]
            except Exception:
                pass
            last_err = RuntimeError("HTTP %s %s" % (exc.code, body))
        except Exception as exc:
            if verify and _friendly_ssl_error(exc):
                if not _SSL_WARNED:
                    log("本机 Python 缺少 CA 证书，已自动切换为「不校验证书」模式继续运行。", "warn")
                    _SSL_WARNED = True
                verify = False
                _HTTP_CTX = _build_context(False)
                last_err = exc
                continue
            last_err = exc
        if attempt < retries:
            time.sleep(delay * attempt)
    raise RuntimeError("请求失败：%s\n    URL: %s" % (last_err, url))


# --------------------------------------------------------------------------
# WBI 签名 + buvid 风控指纹（2023.3 起 B站风控手段，防止 412 request was banned）
# --------------------------------------------------------------------------

class WbiSigner:
    """按 bilibili-API-collect 公开算法给 GET 请求参数做 WBI 签名。"""

    def __init__(self, img_key: str, sub_key: str):
        raw = img_key + sub_key
        self.mixin_key = "".join(raw[i] for i in MIXIN_KEY_ENC_TAB)[:32]

    def sign(self, params: dict) -> dict:
        import hashlib
        params = dict(params)
        params["wts"] = str(int(time.time()))
        cleaned = {}
        for k, v in params.items():
            cleaned[k] = "".join(ch for ch in str(v) if ch not in "!'()*")
        query = urllib.parse.urlencode(sorted(cleaned.items()))
        params["w_rid"] = hashlib.md5((query + self.mixin_key).encode()).hexdigest()
        return params

    @staticmethod
    def from_nav(payload: dict):
        wbi = ((payload.get("data") or {}).get("wbi_img")) or {}
        img = (wbi.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
        sub = (wbi.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
        if not img or not sub:
            return None
        return WbiSigner(img, sub)


_wbi_signer = None
_wbi_fetched_at = 0.0


def get_wbi_signer(cookie="", force=False) -> WbiSigner | None:
    """获取并缓存当天的 WBI 密钥（img_key/sub_key 每日更换，来自 nav 的 wbi_img）。"""
    global _wbi_signer, _wbi_fetched_at
    if not force and _wbi_signer and time.time() - _wbi_fetched_at < 3600:
        return _wbi_signer
    try:
        payload = http_get_json(NAV_API, cookie=cookie, retries=2, verify=False)
    except Exception:
        return _wbi_signer
    signer = WbiSigner.from_nav(payload)
    if signer:
        _wbi_signer = signer
        _wbi_fetched_at = time.time()
    return _wbi_signer


def sign_url(url: str, cookie="") -> str:
    """给 URL 的 query 参数加 w_rid/wts 签名（幂等：已签名则不再重复签名）。"""
    parsed = urllib.parse.urlsplit(url)
    params = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    if any(k == "w_rid" for k, _ in params):
        return url
    signer = get_wbi_signer(cookie)
    if not signer:
        return url
    signed = signer.sign(dict(params))
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path,
         urllib.parse.urlencode(signed), parsed.fragment))


_buvid = None
_buvid_fetched_at = 0.0
_buvid_last_try = 0.0


def get_buvid(force=False) -> str:
    """获取 buvid3/buvid4 风控指纹并缓存（公开接口 x/frontend/finger/spi）。"""
    global _buvid, _buvid_fetched_at, _buvid_last_try
    if not force and _buvid and time.time() - _buvid_fetched_at < 24 * 3600:
        return _buvid
    if not force and time.time() - _buvid_last_try < 600:
        return _buvid or ""
    _buvid_last_try = time.time()
    try:
        payload = http_get_json(FINGER_API, retries=2, verify=False)
        data = payload.get("data") or {}
        b3 = (data.get("b_3") or "").split("infoc")[0]
        b4 = (data.get("b_4") or "").split("infoc")[0]
        if b3:
            _buvid = b3
            _buvid_fetched_at = time.time()
        elif not _buvid:
            _buvid = ""
    except Exception:
        pass
    return _buvid


_bnut = None
_bnut_fetched_at = 0.0
_bnut_last_try = 0.0
_ticket = None
_ticket_fetched_at = 0.0
_ticket_last_try = 0.0
_TICKET_KEY = "XgwSnGZ1p"


def _capture_set_cookie(url: str, name: str, referer="https://www.bilibili.com/") -> str:
    """发一个请求，从 Set-Cookie 里取出指定字段（b_nut / bili_ticket 都是这么来的）。"""
    import http.cookiejar
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar),
        urllib.request.HTTPSHandler(context=_build_context(False)),
    )
    headers = dict(HEADERS)
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    try:
        opener.open(req, timeout=15).read()
    except Exception:
        return ""
    for ck in jar:
        if ck.name == name and ck.value:
            return ck.value
    return ""


def get_bnut(force=False) -> str:
    """b_nut 来自访问 www.bilibili.com 时下发的 Set-Cookie。"""
    global _bnut, _bnut_fetched_at, _bnut_last_try
    if not force and _bnut and time.time() - _bnut_fetched_at < 24 * 3600:
        return _bnut
    if not force and time.time() - _bnut_last_try < 600:
        return _bnut or ""
    _bnut_last_try = time.time()
    val = _capture_set_cookie("https://www.bilibili.com/", "b_nut")
    if val:
        _bnut = val
        _bnut_fetched_at = time.time()
    return _bnut or ""


def get_bili_ticket(force=False) -> str:
    """bili_ticket：3 天有效的 JWT，存在可降低风控概率。

    算法（bilibili-API-collect docs/misc/sign/bili_ticket.md）：
    对 "ts"+时间戳 做 HMAC-SHA256（密钥 XgwSnGZ1p），sign 为 hex，
    GET /bapis/bilibili.api.ticket.v1.Ticket/GenWebTicket?key_id=ec02&sign=..&ts=..
    """
    global _ticket, _ticket_fetched_at, _ticket_last_try
    if not force and _ticket and time.time() - _ticket_fetched_at < 3 * 24 * 3600 - 3600:
        return _ticket
    if not force and time.time() - _ticket_last_try < 600:
        return _ticket or ""
    _ticket_last_try = time.time()
    import hashlib
    import hmac
    ts = str(int(time.time()))
    sign = hmac.new(_TICKET_KEY.encode(), ("ts" + ts).encode(), hashlib.sha256).hexdigest()
    url = ("https://api.bilibili.com/bapis/bilibili.api.ticket.v1.Ticket/GenWebTicket"
           "?key_id=ec02&sign=%s&ts=%s" % (sign, ts))
    val = _capture_set_cookie(url, "bili_ticket")
    if val:
        _ticket = val
        _ticket_fetched_at = time.time()
    return _ticket or ""


def attach_buvid(cookie: str) -> str:
    """把风控相关 Cookie（buvid3 / b_nut / bili_ticket）补进 Cookie。

    ⚠️ 重要：默认**不要**自动调用。
    2026-10 实测发现：如果给登录 Cookie 硬塞一个与账号设备不匹配的 buvid3，
    B 站会把发表的评论判定为可疑并隐藏（评论返回 rpid 但评论区看不到）。
    保持用户原样的 SESSDATA + bili_jct 反而能正常发且可见。
    仅当明确需要（例如接口报 -352 风控）时才可选择性使用。
    """
    if not cookie:
        return cookie
    additions = []
    if "buvid3=" not in cookie:
        b3 = get_buvid()
        if b3:
            additions.append("buvid3=%s" % b3)
    if "b_nut=" not in cookie:
        bn = get_bnut()
        if bn:
            additions.append("b_nut=%s" % bn)
    if "bili_ticket=" not in cookie:
        tk = get_bili_ticket()
        if tk:
            additions.append("bili_ticket=%s" % tk)
    if not additions:
        return cookie
    return "%s; %s" % (cookie, "; ".join(additions))


class AuthError(RuntimeError):
    """Cookie 失效或未登录。"""


class CsrfError(RuntimeError):
    """缺少 bili_jct，无法进行写操作（发表评论）。"""


class CsrfMismatchError(RuntimeError):
    """bili_jct 与 SESSDATA 不是同一次登录产生的（B站返回 -111）。"""


class LevelTooLowError(RuntimeError):
    """账号等级不足，无法发表评论（B站返回 12074，通常要求 LV2）。"""


class DuplicateCommentError(RuntimeError):
    """内容与最近发过的评论重复（B站返回 12051「重复评论」）。

    固定文案重复发送时会触发；遇到时应当作「永久跳过」，不要重试。
    """


# B站要求发表评论的最低等级
MIN_COMMENT_LEVEL = 2


class CommentGone(RuntimeError):
    """评论不存在 / 不可见。"""


def http_post_form_json(url, data: dict, cookie="", timeout=20, retries=3, delay=1.0,
                        referer=None, verify=True):
    """application/x-www-form-urlencoded 的 POST（发表评论用）。"""
    global _HTTP_CTX, _SSL_WARNED
    body = urllib.parse.urlencode(data).encode("utf-8")
    last_err = None
    for attempt in range(1, retries + 1):
        if _HTTP_CTX is None:
            _HTTP_CTX = _build_context(verify)
        headers = dict(HEADERS)
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        if referer:
            headers["Referer"] = referer
        if cookie:
            headers["Cookie"] = cookie
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=_HTTP_CTX) as resp:
                raw = resp.read()
            return json.loads(raw.decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            text = ""
            try:
                text = exc.read().decode("utf-8", errors="replace")[:200]
            except Exception:
                pass
            last_err = RuntimeError("HTTP %s %s" % (exc.code, text))
        except Exception as exc:
            if verify and _friendly_ssl_error(exc):
                if not _SSL_WARNED:
                    log("本机 Python 缺少 CA 证书，已自动切换为「不校验证书」模式继续运行。", "warn")
                    _SSL_WARNED = True
                verify = False
                _HTTP_CTX = _build_context(False)
                last_err = exc
                continue
            last_err = exc
        if attempt < retries:
            time.sleep(delay * attempt)
    raise RuntimeError("POST 请求失败：%s\n    URL: %s" % (last_err, url))


def api_call(url, cookie="", timeout=20, retries=3, retry_delay=1.0, referer=None, verify=True):
    payload = http_get_json(url, cookie=cookie, timeout=timeout, retries=retries,
                            delay=retry_delay, referer=referer, verify=verify)
    code = payload.get("code")
    if code == 0:
        return payload
    msg = payload.get("message") or ""
    if code in (-101, -400, -403, 61000):
        raise AuthError("账号未登录或 Cookie 失效（code=%s, message=%s）" % (code, msg))
    raise RuntimeError("接口返回异常：code=%s message=%s\n    URL: %s" % (code, msg, url))


# --------------------------------------------------------------------------
# Cookie 处理
# --------------------------------------------------------------------------


def normalize_cookie(cookie: str) -> str:
    if not cookie:
        return ""
    cookie = cookie.replace("\r", "").replace("\n", "; ")
    cookie = re.sub(r"\s*;\s*", "; ", cookie).strip()
    cookie = re.sub(r"^;+", "", cookie).strip()
    cookie = re.sub(r";+$", "", cookie).strip()
    return cookie


def cookie_from_sessdata(sessdata: str) -> str:
    sessdata = (sessdata or "").strip()
    if not sessdata:
        return ""
    if "SESSDATA=" in sessdata:
        return normalize_cookie(sessdata)
    return "SESSDATA=" + urllib.parse.quote(sessdata, safe="%*,.")


def extract_cookie_value(cookie: str, key: str) -> str:
    for part in normalize_cookie(cookie).split("; "):
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip().lower() == key.lower():
                return urllib.parse.unquote(v.strip())
    return ""


def fix_double_prefix(cookie: str) -> str:
    """修掉 cookie.txt 里常见的 SESSDATA=SESSDATA=xxx 双前缀写法。

    双前缀会让请求头变成 SESSDATA=SESSDATA=xxx，虽然 B 站多数情况能容忍，
    但取值时会带上脏字符，这里统一清掉。
    """
    parts = []
    for part in normalize_cookie(cookie).split("; "):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        k, v = k.strip(), v.strip()
        while v.lower().startswith(k.lower() + "="):
            v = v[len(k) + 1:]
        parts.append("%s=%s" % (k, v))
    return "; ".join(parts)


def remove_cookie_field(cookie: str, key: str) -> str:
    """从 Cookie 串里删掉某个字段。"""
    kept = []
    for part in normalize_cookie(cookie).split("; "):
        if "=" not in part:
            continue
        k = part.split("=", 1)[0].strip()
        if k.lower() != key.lower():
            kept.append(part)
    return "; ".join(kept)


def _bare_cookie_text(raw: str) -> str:
    """cookie.txt 里可能写的是裸 SESSDATA 值（不带 key）。"""
    text = raw.strip()
    if "=" in text:
        # 有 key：交给 fix_double_prefix 处理
        return normalize_cookie(text)
    return "SESSDATA=" + urllib.parse.quote(text, safe="%*,.")


def load_cookie(args) -> str:
    sources = [
        getattr(args, "cookie", None),
        getattr(args, "sessdata", None),
        os.environ.get("BILI_COOKIE"),
        os.environ.get("BILI_SESSDATA"),
    ]
    for val in sources:
        if val:
            return fix_double_prefix(normalize_cookie(val))

    path = os.path.join(APP_DIR, "cookie.txt")
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
            raw = fh.read().strip()
        if raw:
            fixed = fix_double_prefix(_bare_cookie_text(raw))
            if extract_cookie_value(fixed, "SESSDATA"):
                return fixed

    state = load_state(getattr(args, "state", DEFAULT_STATE))
    state_cookie = fix_double_prefix(normalize_cookie(state.get("cookie") or ""))
    if extract_cookie_value(state_cookie, "SESSDATA"):
        return state_cookie
    return ""


def print_login_help():
    name = os.path.basename(sys.argv[0])
    log("没有可用的登录信息，请任选一种方式：", "err")
    print(
        "    1) 扫码登录：  python %s --login\n"
        "    2) 传 SESSDATA：python %s --sessdata 你的SESSDATA\n"
        "    3) 传整串 Cookie：python %s --cookie \"SESSDATA=xxx; bili_jct=xxx\"\n"
        "    4) 新建 cookie.txt，里面写一行 SESSDATA=xxx\n"
        "    获取方法：浏览器登录 bilibili.com → F12 → Application/存储 → Cookie →\n"
        "              复制 https://www.bilibili.com 下的 SESSDATA" % (name, name, name)
    )


# --------------------------------------------------------------------------
# 状态文件（Cookie + 累计记录 + 抓取进度）
# --------------------------------------------------------------------------


def load_state(path: str) -> dict:
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            pass
    return {"records": {}, "last_ts": 0, "cookie": ""}


def save_state(path: str, state: dict) -> str:
    """保存状态；对文件被占用做重试（状态丢失会导致重复回复）。"""
    return atomic_write(path, json.dumps(state, ensure_ascii=False, indent=2), "utf-8")


def save_cookie_to_state(cookie: str, state_path: str = DEFAULT_STATE):
    state = load_state(state_path)
    state["cookie"] = normalize_cookie(cookie)
    save_state(state_path, state)
    # 同时写进 cookie.txt：它的优先级高于 state，保证下次启动用的是新凭证
    try:
        cookie_path = os.path.join(APP_DIR, "cookie.txt")
        atomic_write(cookie_path, normalize_cookie(cookie) + "\n", "utf-8")
    except Exception as exc:
        log("写入 cookie.txt 失败（已保存到 %s）：%s" % (state_path, exc), "warn")


# --------------------------------------------------------------------------
# 扫码登录（纯离线二维码：qr_offline.py）
# --------------------------------------------------------------------------


def _load_qr_module():
    try:
        import qr_offline
        return qr_offline
    except Exception:
        return None


def _qr_html(data_uri: str, path: str, link: str) -> bool:
    try:
        html = (
            "<!doctype html><meta charset='utf-8'><title>B站扫码登录</title>"
            "<body style='font-family:sans-serif;text-align:center;padding:40px;background:#f6f7f9'>"
            "<h2>用「哔哩哔哩」手机客户端扫码登录</h2>"
            "<p>打开 App → 左上角头像 → 扫一扫</p>"
            "<img src='%s' width='320' height='320' style='background:#fff;padding:12px;border-radius:8px'>"
            "<p style='color:#888'>二维码有效期约 3 分钟，扫码后在手机上点「确认登录」。</p>"
            "<p style='color:#888;font-size:12px'>本页二维码为本地生成、内嵌图片，断网也能正常显示。</p>"
            "<p style='color:#bbb;font-size:12px;word-break:break-all'>%s</p></body>"
            % (data_uri, link)
        )
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(html)
        return True
    except Exception:
        return False


def qr_login(timeout_sec: int = 300) -> str:
    """扫码登录，返回可用的 Cookie 字符串。"""
    import base64

    payload = api_call(QR_GEN_API, referer="https://www.bilibili.com/")
    data = payload.get("data") or {}
    qr_url = data.get("url")
    qr_key = data.get("qrcode_key")
    if not qr_url or not qr_key:
        raise RuntimeError("获取二维码失败：%s" % json.dumps(payload, ensure_ascii=False)[:300])

    qr = _load_qr_module()
    if qr is None:
        log("未找到 qr_offline.py，无法生成离线二维码，可直接打开下面的链接扫码。", "warn")
        print("    登录链接：%s" % qr_url)
    else:
        code = qr.QRCode(qr_url)
        png = code.to_png_bytes(scale=8, border=4)
        png_path = os.path.join(APP_DIR, "qr_login.png")
        with open(png_path, "wb") as fh:
            fh.write(png)
        html_path = os.path.join(APP_DIR, "qr_login.html")
        data_uri = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
        _qr_html(data_uri, html_path, qr_url)
        log("二维码已生成：%s（离线，可放心断网）" % png_path, "ok")
        if sys.stdout.isatty():
            print(code.to_ascii(border=2, color=True))
        log("已打开二维码页面；若浏览器没弹出，请手动双击 %s" % html_path)
        try:
            import webbrowser
            webbrowser.open("file:///" + html_path.replace("\\", "/"))
        except Exception:
            pass
        print("    登录链接：%s" % qr_url)

    log("等待扫码（最长 %d 秒，Ctrl+C 取消）..." % timeout_sec)
    print("    请用手机 B 站 App 扫描上面打开的二维码；扫完记得在手机上点「确认登录」。")
    deadline = time.time() + timeout_sec
    notified = False
    last_code = None
    while time.time() < deadline:
        time.sleep(2)
        try:
            payload = api_call(QR_POLL_API + "?qrcode_key=" + urllib.parse.quote(qr_key),
                               referer="https://www.bilibili.com/")
        except Exception as exc:
            if not _QUIET:
                print("    (轮询失败，继续重试：%s)" % str(exc)[:60])
            continue
        d = payload.get("data") or {}
        code = d.get("code")
        if code != last_code:
            last_code = code
        if code == 0:
            cookie = _collect_set_cookie(qr_key) or _cookie_from_return_url(d.get("url") or "")
            if not cookie:
                raise RuntimeError("登录成功但没能取到 Cookie，请改用 --sessdata。")
            save_cookie_to_state(cookie)
            log("登录成功！Cookie 已保存到 cookie.txt 与 %s" % DEFAULT_STATE, "ok")
            log("接下来直接运行：python %s" % os.path.basename(sys.argv[0] or __file__), "ok")
            return cookie
        if code == 86038:
            # 过期是最常见的失败：链接只有约 3 分钟寿命，且退出后二维码立即作废
            raise RuntimeError(
                "二维码已过期（链接寿命约 3 分钟，且脚本退出后立即失效）。"
                "请重新执行 --login，并尽快用手机扫码确认。")
        if code == 86090 and not notified:
            log("已扫码！请在手机上点「确认登录」，脚本会继续等待 ...", "ok")
            notified = True
        elif code == 86101 and not _QUIET:
            remain = int(deadline - time.time())
            if remain % 20 < 2:
                print("    (还未检测到扫码，剩余 %d 秒；若没看到二维码请双击同目录的 qr_login.html)" % remain)
    raise RuntimeError(
        "等待扫码超时（%d 秒内没有完成登录）。常见原因：二维码已过期、手机没点确认、"
        "或扫的是上次留下的旧二维码。请重新执行 --login。" % timeout_sec)


def _cookie_from_return_url(url: str) -> str:
    if not url:
        return ""
    params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    pairs = []
    for key in ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5"):
        if params.get(key):
            pairs.append("%s=%s" % (key, params[key][0]))
    return "; ".join(pairs)


def _collect_set_cookie(qr_key: str) -> str:
    try:
        import http.cookiejar
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar),
            urllib.request.HTTPSHandler(context=_build_context(False)),
        )
        req = urllib.request.Request(
            QR_POLL_API + "?qrcode_key=" + urllib.parse.quote(qr_key),
            headers={**HEADERS, "Referer": "https://www.bilibili.com/"},
        )
        opener.open(req, timeout=20).read()
        pairs = ["%s=%s" % (c.name, c.value) for c in jar
                 if c.name in ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5")]
        return "; ".join(pairs)
    except Exception:
        return ""


# --------------------------------------------------------------------------
# 回复内容处理
# --------------------------------------------------------------------------

_MENTION_RE = re.compile(r"(?:(?<=^)|(?<=\s))@([^\s@]{1,20})(?=\s|$)")
# B 站回复框的引用前缀：「回复 @某人 :」整体属于 @ 信息，一起去掉
_QUOTE_PREFIX_RE = re.compile(
    r"^\s*回复\s*(?:(?<=^)|(?<=\s))@[^\s@]{1,20}\s*[:：]\s*|^\s*回复\s*[:：]?\s*")
# 像邮箱的片段（a@b.com）：B 站评论里偶尔会出现，不要误删
_EMAIL_TAIL_RE = re.compile(r"\.[A-Za-z]{2,6}$")


def strip_mentions(text: str) -> str:
    """去掉评论里的 @某人，例如「@我 你好呀」→「你好呀」。

    B 站的 @ 在评论正文里就是「@昵称 + 空格」的纯文本，按这个形式清理：
    要求 @ 出现在行首/空格之后，且昵称后面跟着空格或结束，
    这样「多@个@符」这类词中夹带的 @ 不会被误删。
    纯文本无法 100% 区分「@昵称」和「邮箱」，所以对形如 a@b.com 的片段做了排除。

    另外会去掉 B 站回复框自带的「回复 :」前缀和首尾多余标点。
    清理后如果什么都不剩（例如整条评论就是「@我」），返回空串，由调用方兜底。
    """
    if not text:
        return ""

    def _drop(match):
        if _EMAIL_TAIL_RE.search(match.group(1)):
            return match.group(0)          # 疑似邮箱，保留原样
        return " "

    cleaned = re.sub(r"\s+", " ", text)
    cleaned = _QUOTE_PREFIX_RE.sub("", cleaned)        # 「回复 @某人 :」整体去掉（要先做）
    cleaned = _MENTION_RE.sub(_drop, cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"^\s*[:：,，、\s]+", "", cleaned)
    cleaned = re.sub(r"[\s:：,，、]+$", "", cleaned)
    return cleaned.strip()


def _mentions_only(text: str) -> bool:
    """去掉所有 @提及 和标点后是否为空（用来判断「评论只有 @ 信息」）。"""
    if not text or not text.strip():
        return False
    body = _QUOTE_PREFIX_RE.sub("", text)              # 去掉「回复 @某人 :」/「回复 」
    body = _MENTION_RE.sub(" ", body)
    return re.sub(r"[\s:：,，、]+", "", body) == ""


def build_reply_message(ev: dict) -> str:
    """回复内容 = @ 我的那条评论正文去掉 @ 信息。

    · @ 本身是父级评论（一级评论）→ "0"
    · 评论只有 @ 信息（去掉后什么都不剩）→ "0"
    """
    if ev.get("parent_is_self"):
        return "0"
    raw = ev.get("content") or ""

    cleaned = strip_mentions(raw)
    if cleaned and _mentions_only(cleaned):
        # 例如「回复 @某人 :@xxx」——去掉前缀后仍只剩 @ 信息
        return "0"
    if cleaned:
        return cleaned
    if _mentions_only(raw):
        return "0"
    return ""


# --------------------------------------------------------------------------
# 父级评论抓取
# --------------------------------------------------------------------------


class CommentResolver:
    """根据 @ 通知里的评论 id，取这条评论的父级评论。

    规则：
        · 若这条 @ 本身就是一级评论（root == 0 或通知里 root_id == 0）→ 返回 0
        · 否则取 parent 指向的那条评论，返回其内容/作者/时间
        · 取不到（已删除、被折叠、无权限）→ 返回无法获取标记

    同时给每条事件补上回复所需的信息（父评论 rpid / root / 是否已回复）以及
    「回复」动作（发表评论）。
    """

    def __init__(self, cookie: str, timeout: int = 20, delay: float = 1.0, verify: bool = True,
                 my_uid: str = "", replied: dict = None):
        self.cookie = cookie
        self.timeout = timeout
        self.delay = delay
        self.verify = verify
        self.my_uid = str(my_uid or "")
        self.replied = replied if replied is not None else {}
        self.cache = {}
        self.bv_cache = {}
        self.csrf_failures = 0
        self.level_too_low = False
        self._csrf = extract_cookie_value(cookie, "bili_jct")
        # AI 判定（deepseek_judge.Judge）；为 None 时走旧的「去 @ 后原文」逻辑
        self.judge = None
        self._ai_cache = {}     # dry-run 时缓存生成结果，避免每轮重复烧 token
        # 次数/信用积分簿（user_credits.CreditBook）；为 None 时不启用扣减
        self.book = None

    # -- 工具 --------------------------------------------------------
    def _call(self, url):
        time.sleep(min(self.delay, 2.0))
        # 评论列表类接口一律先做 WBI 签名，避免被 412「request was banned」风控拦截
        if "x/v2/reply" in url:
            url = sign_url(url, self.cookie)
        return api_call(url, cookie=self.cookie, timeout=self.timeout,
                        retry_delay=self.delay, verify=self.verify,
                        referer="https://www.bilibili.com/")

    def bv_from_aid(self, aid) -> str:
        aid = str(aid)
        if aid in self.bv_cache:
            return self.bv_cache[aid]
        bvid = ""
        try:
            payload = self._call(VIEW_API + "?" + urllib.parse.urlencode({"aid": aid}))
            bvid = ((payload.get("data") or {}).get("bvid")) or ""
        except Exception:
            bvid = ""
        self.bv_cache[aid] = bvid
        return bvid

    # -- 父级评论 ----------------------------------------------------
    @staticmethod
    def _norm_reply(reply: dict) -> dict:
        content = (reply.get("content") or {}).get("message") or ""
        member = reply.get("member") or {}
        return {
            "content": re.sub(r"\s+", " ", content).strip(),
            "uname": (member.get("uname") or "未知用户").strip(),
            "mid": str(member.get("mid") or ""),
            "time": datetime.fromtimestamp(int(reply.get("ctime") or 0)).strftime("%Y-%m-%d %H:%M:%S")
            if reply.get("ctime") else "",
        }

    def get_comment(self, ctype, oid, rpid, root=0) -> dict:
        """在评论区里定位某条评论（rpid）。先查顶层列表，必要时查子评论列表。"""
        key = (ctype, str(oid), str(rpid))
        if key in self.cache:
            return self.cache[key]

        found = None
        # 1) 顶层评论列表：先按热度(sort=2)，再按时间(sort=0)
        #    新发的评论 0 赞，在热度排序里可能排在很后面，只翻热度会找不到
        for sort in (2, 0):
            if found:
                break
            for pn in (1, 2, 3):
                url = COMMENT_API + "?" + urllib.parse.urlencode({
                    "type": ctype, "oid": oid, "sort": sort, "ps": 20, "pn": pn,
                    "nohot": 1,
                })
                try:
                    payload = self._call(url)
                except Exception:
                    break
                replies = (payload.get("data") or {}).get("replies") or []
                if not replies:
                    break
                for r in replies:
                    if str(r.get("rpid")) == str(rpid):
                        found = r
                        break
                    for sub in (r.get("replies") or []):
                        if str(sub.get("rpid")) == str(rpid):
                            found = sub
                            break
                    if found:
                        break
                if found:
                    break

        # 2) 还没找到，并且知道 root，就去子评论列表里翻
        if found is None and root and str(root) != "0":
            for pn in (1, 2, 3, 4, 5):
                url = SUB_COMMENT_API + "?" + urllib.parse.urlencode({
                    "type": ctype, "oid": oid, "root": root, "ps": 20, "pn": pn,
                })
                try:
                    payload = self._call(url)
                except Exception:
                    break
                replies = (payload.get("data") or {}).get("replies") or []
                if not replies:
                    break
                for r in replies:
                    if str(r.get("rpid")) == str(rpid):
                        found = r
                        break
                if found:
                    break
        self.cache[key] = found
        return found

    def resolve_parent(self, item: dict, business_id, subject_id) -> dict:
        """解析父级评论，并给出回复所需的信息。"""
        source_id = int(item.get("source_id") or 0)
        root_id = int(item.get("root_id") or 0)
        ctype = self._comment_type(business_id)

        info = {
            "text": "",            # 用于 txt 展示
            "parent_rpid": 0,      # 回复目标评论 rpid
            "root_rpid": 0,        # 回复时要带的 root
            "parent_is_self": False,
            "target_desc": "",
            # AI 判定需要的原始文本
            "parent_content": "",
            "parent_author": "",
            "grandparent_content": "",
            "grandparent_author": "",
            # 被回复的那条评论是否为「直接评论视频」的一级评论
            "parent_is_direct": False,
            # @ 评论本身是否为「直接评论视频」的一级评论（AI 判定用）
            "at_is_direct": False,
            # @ 评论自身的 rpid / 所在楼 root（回复「@ 我的人」时要用）
            "at_rpid": source_id,
            "at_root": source_id,
        }

        if not source_id and not root_id:
            info["text"] = "无法获取（该 @ 不含评论 id，可能是动态正文 @）"
            return info
        if not ctype or not subject_id:
            info["text"] = UNKNOWN_MARK
            return info

        comment = None
        try:
            comment = self.get_comment(ctype, subject_id, source_id, root=root_id)
        except Exception:
            comment = None

        # 判定是否本身就是一级评论
        is_root_comment = (root_id == 0 and (comment is None or not comment.get("root")))
        if is_root_comment:
            info["text"] = NONE_MARK
            info["parent_is_self"] = True
            info["parent_rpid"] = source_id
            info["root_rpid"] = source_id
            info["target_desc"] = "@ 我的那条评论本身（一级评论）"
            info["parent_is_direct"] = True   # 本身就是直接评论视频
            info["at_is_direct"] = True       # @ 评论本身是一级评论
            info["parent_content"] = (item.get("source_content") or "").strip()
            return info

        parent_id = 0
        if comment is not None:
            parent_id = int(comment.get("parent") or 0)
        if not parent_id:
            parent_id = root_id

        if not parent_id:
            info["text"] = NONE_MARK
            info["parent_is_self"] = True
            info["parent_rpid"] = source_id
            info["root_rpid"] = source_id
            info["target_desc"] = "@ 我的那条评论本身（一级评论）"
            info["parent_is_direct"] = True   # 本身就是直接评论视频
            info["at_is_direct"] = True       # @ 评论本身是一级评论
            info["parent_content"] = (item.get("source_content") or "").strip()
            return info

        parent = None
        try:
            parent = self.get_comment(ctype, subject_id, parent_id, root=root_id or parent_id)
        except Exception:
            parent = None

        info["parent_rpid"] = parent_id
        info["root_rpid"] = int((comment or {}).get("root") or root_id or parent_id)
        info["target_desc"] = "父级评论 rpid=%s" % parent_id
        if not parent:
            info["text"] = UNKNOWN_MARK
            return info
        p = self._norm_reply(parent)
        text = "「%s」 —— %s (UID: %s)" % (p["content"] or "(空内容)", p["uname"], p["mid"] or "未知")
        if p["time"]:
            text += "  发布于 %s" % p["time"]
        info["text"] = text
        info["parent_content"] = p["content"]
        info["parent_author"] = p["uname"]

        # 判断「@ 评论本身」是不是「直接评论视频」的一级评论（root=parent=0）。
        # AI 模式据此决定是否分析：一级评论在评论视频，无法分析视频内容；
        # 只有「回复他人评论」的二级及以下评论，才有可辩证的对话内容。
        if root_id:
            info["at_is_direct"] = False        # 通知里带 root，说明是回复别人
        elif comment is not None:
            try:
                c_root = int(comment.get("root") or 0)
                c_parent = int(comment.get("parent") or 0)
            except Exception:
                c_root, c_parent = 0, 0
            info["at_is_direct"] = (c_root == 0 and c_parent == 0)
        else:
            info["at_is_direct"] = True         # 拿不到评论对象，但通知 root=0

        # @ 评论所在楼的 root（回复「@ 我的人」时作为 root 参数）
        try:
            info["at_root"] = int((comment or {}).get("root") or root_id or source_id)
        except Exception:
            info["at_root"] = root_id or source_id

        # 顺带记录「被回复的那条评论」的类型（展示/日志用）
        try:
            p_root = int((parent or {}).get("root") or 0)
            p_parent = int((parent or {}).get("parent") or 0)
        except Exception:
            p_root, p_parent = 0, 0
        info["parent_is_direct"] = (p_root == 0 and p_parent == 0)

        # 再往上取一条：父级评论所回复的那条评论（给 AI 做上下文）
        grand_id = 0
        try:
            grand_id = int((parent or {}).get("parent") or 0)
        except Exception:
            grand_id = 0
        if grand_id and grand_id != parent_id:
            try:
                grand = self.get_comment(ctype, subject_id, grand_id,
                                         root=info["root_rpid"] or grand_id)
            except Exception:
                grand = None
            if grand:
                g = self._norm_reply(grand)
                info["grandparent_content"] = g["content"]
                info["grandparent_author"] = g["uname"]
        return info

    # -- 回复 --------------------------------------------------------
    def _page_referer(self, ctype, oid) -> str:
        """按评论区类型给出「真实页面」的 Referer。

        实测：Referer 是视频页时评论正常可见；用站点首页做 Referer 时，
        部分评论会被判可疑而隐藏。所以这里尽量还原真实页面地址。
        """
        try:
            ctype = int(ctype)
        except Exception:
            ctype = 0
        if ctype == 1:      # 视频
            return "https://www.bilibili.com/video/av%s/" % oid
        if ctype == 12:     # 专栏
            return "https://www.bilibili.com/read/cv%s/" % oid
        if ctype in (11, 17):   # 动态
            return "https://t.bilibili.com/%s" % oid
        return "https://www.bilibili.com/"

    def add_reply(self, ctype, oid, message, parent_rpid, root_rpid=0) -> str:
        """发表回复，返回新评论 rpid。

        策略（2026-10 实测）：
          1. 先用最简表单（type/oid/message/plat/csrf）直接 POST —— 只需有效的
             SESSDATA + bili_jct，实测稳定成功且评论可见；
          2. 只有在返回 -352/-403（风控）时，才补上 WBI + DM 签名、
             statistics/gaia_source/csrf_token 再试一次。
        """
        if not self._csrf:
            self._csrf = extract_cookie_value(self.cookie, "bili_jct")
        if not self._csrf:
            raise CsrfError(
                "缺少 bili_jct（CSRF Token），无法发表评论。"
                "请执行 python bili_at_monitor.py --login 重新登录，"
                "或把浏览器 Cookie 里的 bili_jct 一并填入 cookie.txt。")

        data = {
            "type": ctype,
            "oid": oid,
            "message": message,
            "plat": 1,
            "csrf": self._csrf,
        }
        if parent_rpid:
            data["parent"] = parent_rpid
            data["root"] = root_rpid or parent_rpid

        payload = http_post_form_json(REPLY_ADD_API, data, cookie=self.cookie,
                                      timeout=self.timeout, delay=self.delay,
                                      verify=self.verify,
                                      referer=self._page_referer(ctype, oid))
        code = payload.get("code")

        # 被风控拦截时才升级为「WBI + DM 签名」重试一次
        if code in (-352, -403):
            log("  · 简单请求被风控拦截（%s），改用 WBI/DM 签名重试" % code, "warn")
            payload = self._add_reply_signed(ctype, oid, message, parent_rpid, root_rpid, data)
            code = payload.get("code")

        if code != 0:
            if code == -111:
                raise CsrfMismatchError(
                    "发表回复失败：CSRF 校验失败（-111）。"
                    "说明 cookie 里的 bili_jct 与 SESSDATA 不是同一次登录产生的。")
            if code == 12074:
                raise LevelTooLowError(
                    "发表回复失败：%s —— 当前账号等级不足，B站要求 LV2 及以上才能发表评论。"
                    % (payload.get("message") or "等级不足"))
            if code == 12051:
                raise DuplicateCommentError(
                    "发表回复失败：code=12051 重复评论（内容与最近发过的太像）")
            if code in (-403, -352):
                raise RuntimeError(
                    "发表回复被风控拦截：code=%s message=%s —— 已带 WBI/DM 签名重试仍失败，"
                    "通常是账号/设备被风控（可能返回 v_voucher 验证码）。"
                    "建议：隔一段时间再试、降低频率、或重新扫码登录换新会话。"
                    % (code, payload.get("message")))
            raise RuntimeError("发表回复失败：code=%s message=%s"
                               % (code, payload.get("message")))
        return str(((payload.get("data") or {}).get("rpid")) or "")

    def _add_reply_signed(self, ctype, oid, message, parent_rpid, root_rpid, base_data):
        """风控兜底：带 WBI + DM 签名（query）与统计字段（body）重试。"""
        import random
        dm_chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
        dm_img_str = "".join(random.sample(dm_chars, 2))
        query_params = {
            "dm_img_list": "[]",
            "dm_img_str": dm_img_str,
            "dm_cover_img_str": dm_img_str,
            "dm_img_inter": json.dumps({"ds": [], "wh": [0, 0, 0], "of": [0, 0, 0]}),
            "web_location": "1550101",
        }
        signer = get_wbi_signer(self.cookie, force=True)
        if signer:
            signed = signer.sign(query_params)
            url = REPLY_ADD_API + "?" + urllib.parse.urlencode(sorted(signed.items()))
        else:
            url = REPLY_ADD_API + "?" + urllib.parse.urlencode(query_params)

        data = dict(base_data)
        data.update({
            "statistics": json.dumps({"appId": 100, "platform": 5}),
            "gaia_source": "main_web",
            "csrf_token": self._csrf,
        })
        return http_post_form_json(url, data, cookie=self.cookie,
                                   timeout=self.timeout, delay=self.delay,
                                   verify=self.verify,
                                   referer=self._page_referer(ctype, oid))

    def find_my_reply_rpid(self, ctype, oid, parent_rpid, root_rpid) -> str:
        """找出「我回复这条父评论」的那条评论 rpid（对账 / 存在性验证用）。"""
        if not self.my_uid:
            return ""
        parent_rpid = str(parent_rpid or "")

        def _scan(replies):
            for r in replies or []:
                if (str(((r.get("member") or {}).get("mid")) or "") == self.my_uid
                        and str(r.get("parent") or "") == parent_rpid):
                    return str(r.get("rpid") or "")
            return ""

        try:
            payload = self._call(SUB_COMMENT_API + "?" + urllib.parse.urlencode(
                {"type": ctype, "oid": oid, "root": root_rpid, "ps": 20, "pn": 1}))
            found = _scan((payload.get("data") or {}).get("replies"))
            if found:
                return found
        except Exception:
            pass
        try:
            found_obj = self.get_comment(ctype, oid, parent_rpid, root=root_rpid) or {}
            return _scan(found_obj.get("replies"))
        except Exception:
            return ""

    def verify_on_platform(self, ctype, oid, parent_rpid, root_rpid,
                           find_replies: bool = True) -> dict:
        """到平台上核实这条父评论、以及我对它的回复是否真实存在。

        返回 {"parent_exists": bool, "reply_rpid": str, "checked": bool, "why": str}

        find_replies=False 时跳过「查我是否已回复」的额外请求（已取消去重逻辑，
        只在需要展示/对账时才查）。
        """
        info = {"parent_exists": False, "reply_rpid": "", "checked": False, "why": ""}
        if not ctype or not oid:
            return info
        parent_rpid = str(parent_rpid or "")
        try:
            parent_obj = self.get_comment(ctype, oid, parent_rpid,
                                          root=root_rpid or parent_rpid)
        except Exception:
            parent_obj = None
        info["checked"] = True
        if parent_obj:
            info["parent_exists"] = True
        if not find_replies:
            info["why"] = ("平台上存在这条评论" if parent_obj
                           else "平台上找不到这条评论（可能已删除/被折叠/无权限）")
            return info
        # 我的回复是否存在：既看该楼回复列表，也看父评论自己的嵌套回复
        rpid = ""
        try:
            rpid = self.find_my_reply_rpid(ctype, oid, parent_rpid, root_rpid)
        except Exception:
            rpid = ""
        if not rpid and parent_obj:
            rpid = self._scan_my_reply(parent_obj.get("replies"), parent_rpid)
        info["reply_rpid"] = rpid
        if rpid:
            info["why"] = "平台上已存在我回复该评论的记录（rpid=%s）" % rpid
        elif info["parent_exists"]:
            info["why"] = "平台上尚无我对该评论的回复"
        else:
            info["why"] = "平台上找不到这条父评论（可能已删除/被折叠/无权限）"
        return info

    def _scan_my_reply(self, replies, parent_rpid) -> str:
        parent_rpid = str(parent_rpid or "")
        for r in replies or []:
            if (str(((r.get("member") or {}).get("mid")) or "") == self.my_uid
                    and str(r.get("parent") or "") == parent_rpid):
                return str(r.get("rpid") or "")
        return ""

    # ---- 次数 / 信用（user_credits.CreditBook） ----
    @staticmethod
    def _target_ids(ev: dict, target: str):
        """返回 (rpid, root)：target=at 回复 @ 我的人，parent 回复父级评论。"""
        if target == "at":
            at = ev.get("at_rpid") or 0
            return at, (ev.get("at_root") or at)
        return ev.get("parent_rpid"), (ev.get("root_rpid") or ev.get("parent_rpid"))

    def is_follower(self, mid, refresh_seconds: int = 300):
        """判断某个 UID 是否关注了我（即是否在我的粉丝列表里）。

        返回 True / False；接口失败时返回 None（未知 → 调用方应放行）。
        粉丝列表缓存 refresh_seconds 秒，且最多翻 5 页（250 人）。
        """
        mid = str(mid or "")
        if not mid or not self.my_uid:
            return None
        now = time.time()
        cache = getattr(self, "_followers", None)
        if cache and now - cache.get("ts", 0) < refresh_seconds:
            return mid in cache["set"]
        mids = set()
        try:
            for pn in range(1, 6):
                url = FOLLOWERS_API + "?" + urllib.parse.urlencode({
                    "vmid": self.my_uid, "pn": pn, "ps": 50,
                })
                payload = self._call(url)
                data = payload.get("data") or {}
                lst = data.get("list") or []
                if not lst:
                    break
                for x in lst:
                    mids.add(str(x.get("mid") or ""))
                total = int(data.get("total") or 0)
                if total and len(mids) >= total:
                    break
                time.sleep(0.3)
        except Exception:
            return None
        self._followers = {"set": mids, "ts": now}
        return mid in mids

    def verify_reply(self, ctype, oid, target_rpid, target_root,
                     attempts: int = 3, wait: float = 1.5) -> bool:
        """发表后确认「我的回复确实出现在平台上」（B站可能有几秒延迟）。"""
        if not ctype or not oid or not target_rpid:
            return False
        for i in range(max(1, attempts)):
            try:
                if self.find_my_reply_rpid(ctype, oid, target_rpid, target_root):
                    return True
            except Exception:
                pass
            if i + 1 < attempts:
                time.sleep(wait)
        return False

    def _deliver_to_at(self, ev: dict, text: str, result: dict, do_reply: bool) -> dict:
        """把一段固定话术直接回复给「@ 我的人」，并更新 result。"""
        ctype = ev.get("reply_type")
        oid = ev.get("reply_oid")
        at_rpid, at_root = self._target_ids(ev, "at")
        info = self.verify_on_platform(ctype, oid, at_rpid, at_root, find_replies=False)
        if info["checked"] and not info["parent_exists"]:
            result.update(skipped=True, reason="找不到该 @ 评论，跳过")
            return result
        result.update(message=text, target="at", target_rpid=at_rpid,
                      target_root=at_root, attempted=True)
        if not do_reply:
            result["reason"] = "dry-run（加 --reply 才会真正发送）"
            return result
        time.sleep(min(self.delay, 2.0))
        result["reply_rpid"] = self.add_reply(ctype, oid, text, at_rpid, at_root)
        result["posted"] = True
        return result

    def maybe_reply(self, ev: dict, do_reply: bool, state: dict = None,
                    args=None) -> dict:
        """对一条 @ 事件：查资格 → 查积分 → 生成回复 → 平台验证 → 发送，并按规则扣减。"""
        from user_credits import CreditBook, pick_zero

        result = {
            "attempted": False, "posted": False, "skipped": False,
            "reason": "", "reply_rpid": "", "message": "", "dry_run": not do_reply,
            "ai_category": "", "ai_reason": "", "ai_calls": 0,
            "target": "parent", "credit_kind": "", "credit_value": "",
        }
        if not ev.get("replyable"):
            result["reason"] = "无可回复的目标（%s）" % (ev.get("parent") or "")
            result["skipped"] = True
            return result

        ctype = ev.get("reply_type")
        oid = ev.get("reply_oid")
        parent_rpid = ev.get("parent_rpid")
        root_rpid = ev.get("root_rpid") or parent_rpid
        uid = str(ev.get("mid") or "")
        book = getattr(self, "book", None)

        # ---- ①-a 关注校验：没关注本账号的人，不分析（不扣次数/信用） ----
        if (self.judge is not None and args is not None
                and not getattr(args, "ai_no_follow_check", False)):
            following = self.is_follower(uid)
            if following is False:
                from deepseek_judge import pick_reply, REPLY_NOT_FOLLOWER_VARIANTS
                text = pick_reply(REPLY_NOT_FOLLOWER_VARIANTS)
                result.update(ai_category="not_follower",
                              ai_reason="该用户未关注本账号，按规则不分析")
                return self._deliver_to_at(ev, text, result, do_reply)

        # ---- ①-b 今日安全回复配额：用完就告知（不扣次数/信用） ----
        if self.judge is not None and args is not None and state is not None:
            limit = int(getattr(args, "ai_max_per_day", 0) or 0)
            if limit > 0:
                now = time.time()
                used = len([t for t in _ai_times(state)
                            if isinstance(t, (int, float)) and now - t < 86400])
                if used >= limit:
                    today = datetime.now().strftime("%Y-%m-%d")
                    notices = state.get("quota_notice")
                    if not isinstance(notices, dict):
                        notices = {}
                        state["quota_notice"] = notices
                    if notices.get(uid) == today:
                        result.update(skipped=True,
                                      reason="今日已通知过该用户配额用完，留在队列")
                        return result
                    notices[uid] = today
                    from deepseek_judge import pick_reply, REPLY_QUOTA_VARIANTS
                    text = pick_reply(REPLY_QUOTA_VARIANTS).format(n=used, limit=limit)
                    result.update(ai_category="quota",
                                  ai_reason="今日配额已用完（%d/%d）" % (used, limit))
                    return self._deliver_to_at(ev, text, result, do_reply)

        # ---- ① 先记录 @ 人，再检查「次数 / 信用」是否为 0（信用优先） ----
        if book is not None and uid:
            rec = book.touch(uid, ev.get("nickname"))
            zero_kind = ""
            if rec["credit"] == 0:
                zero_kind = "credit"
            elif rec["times"] == 0:
                zero_kind = "times"
            if zero_kind:
                text = pick_zero(zero_kind)
                result.update(message=text, target="at", credit_kind=zero_kind,
                              ai_category="refused",
                              ai_reason=("%s为 0，拒绝回复" % ("信用" if zero_kind == "credit" else "次数")))
                at_rpid, at_root = self._target_ids(ev, "at")
                # 已取消「不重复回复」规则：不再检查是否回复过，只确认评论还在
                info_at = self.verify_on_platform(ctype, oid, at_rpid, at_root,
                                                  find_replies=False)
                if not info_at["parent_exists"]:
                    result.update(skipped=True, reason="找不到该 @ 评论，跳过")
                    return result
                result["attempted"] = True
                result["target_rpid"] = at_rpid
                result["target_root"] = at_root
                if not do_reply:
                    result["reason"] = "dry-run（加 --reply 才会真正发送）"
                    return result
                time.sleep(min(self.delay, 2.0))
                result["reply_rpid"] = self.add_reply(ctype, oid, text, at_rpid, at_root)
                result["posted"] = True
                result["reason"] = "已拒绝回复（%s为 0）" % ("信用" if zero_kind == "credit" else "次数")
                return result

        # ---- ② 平台验证（父级评论）：只确认评论还在（已取消「不重复回复」规则） ----
        info = self.verify_on_platform(ctype, oid, parent_rpid, root_rpid,
                                       find_replies=False)
        result["platform_checked"] = info["checked"]
        result["parent_exists"] = info["parent_exists"]

        if info["checked"] and not info["parent_exists"]:
            result["skipped"] = True
            result["reason"] = info["why"] + "，跳过回复"
            return result

        # ---- ③ 生成回复内容：AI 法官模式 / 旧的「去 @ 原文」模式 ----
        if self.judge is not None:
            cache_key = ev.get("id") or "%s:%s" % (ev.get("mid"), ev.get("ts"))
            decided = None
            # 先查本次运行缓存（dry-run 不重复烧 token），再查持久化缓存（重启也不重算）
            if not do_reply and cache_key in self._ai_cache:
                decided = self._ai_cache[cache_key]
            elif state is not None:
                saved = (state.get("ai_results") or {}).get(cache_key)
                if isinstance(saved, dict) and saved.get("text") and saved.get("category"):
                    decided = dict(saved)
                    decided["ai_calls"] = 0
                    decided["reason"] = (saved.get("reason") or "") + "（缓存）"
            if decided is None:
                decided = self.judge.build_reply(ev)
                if not do_reply:
                    self._ai_cache[cache_key] = decided
                if (state is not None and decided.get("text")
                        and not decided.get("skipped")):
                    state.setdefault("ai_results", {})[cache_key] = {
                        "category": decided.get("category"),
                        "text": decided.get("text"),
                        "reason": decided.get("reason"),
                        "reply_to": decided.get("reply_to", "parent"),
                        "ts": time.time(),
                    }
            result["ai_category"] = decided.get("category", "")
            result["ai_reason"] = decided.get("reason", "")
            result["ai_calls"] = decided.get("ai_calls", 0)
            if decided.get("skipped"):
                result["skipped"] = True
                result["reason"] = decided.get("reason") or "AI 判定跳过"
                return result
            message = (decided.get("text") or "").strip()
            target = decided.get("reply_to") or "parent"
        else:
            message = build_reply_message(ev)
            target = "parent"

        if not message:
            result["reason"] = "回复内容为空"
            result["skipped"] = True
            return result

        # ---- ④ 目标校验：闲聊/一级评论等回复给 @ 人，其余回复给父级评论 ----
        # 已取消「不重复回复」规则：这里只确认目标评论还在
        category = result["ai_category"]
        if target == "at":
            t_rpid, t_root = self._target_ids(ev, "at")
            info_t = self.verify_on_platform(ctype, oid, t_rpid, t_root,
                                             find_replies=False)
            if info_t["checked"] and not info_t["parent_exists"]:
                result.update(skipped=True, reason="找不到该 @ 评论，跳过")
                return result
        else:
            t_rpid, t_root = self._target_ids(ev, "parent")

        # ---- ⑤ 扣减：一级评论/闲聊/注入/未知 → 信用；正常分析 → 今日次数 ----
        if book is not None and uid:
            credit_branches = ("video_comment", "offtopic", "injection", "safety",
                               "", "refused")
            kind = "credit" if category in credit_branches else "times"
            cur = book.value(uid, kind)
            after = cur if book.is_free(uid) else max(0, cur - 1)
            message = message.rstrip() + "\n" + book.suffix(uid, kind, after)
            result["credit_kind"] = kind
            result["credit_value"] = book.show(uid, after)
            if do_reply:
                book.spend(uid, kind)

        result["message"] = message
        result["target"] = target
        result["target_rpid"] = t_rpid
        result["target_root"] = t_root
        result["attempted"] = True
        if not do_reply:
            result["reason"] = "dry-run（加 --reply 才会真正发送）"
            return result

        time.sleep(min(self.delay, 2.0))
        try:
            result["reply_rpid"] = self.add_reply(ctype, oid, message, t_rpid, t_root)
        except CsrfMismatchError:
            self.csrf_failures += 1
            raise
        except LevelTooLowError:
            self.level_too_low = True
            raise
        result["posted"] = True
        result["reason"] = "已回复（平台验证通过后发送）"
        return result

    @staticmethod
    def _comment_type(business_id):
        """B 站评论区类型代码；动态是 17，视频是 1，专栏 12，相簿 11。"""
        try:
            bid = int(business_id)
        except Exception:
            return None
        return bid if bid in (1, 2, 4, 5, 11, 12, 14, 17, 21, 22, 33) else None


# --------------------------------------------------------------------------
# 抓取 feed
# --------------------------------------------------------------------------


def fetch_feed(api_url: str, cookie: str, max_pages: int, timeout: int,
               delay: float, verify: bool, verbose: bool = True):
    items = []
    cursor_id = cursor_time = None
    page = 0
    while True:
        page += 1
        query = {"platform": "web", "build": "0", "mobi_app": "web",
                 "web_location": "333.1387"}
        if cursor_id:
            query["id"] = str(cursor_id)
        if cursor_time:
            query["reply_time"] = str(cursor_time)
        url = api_url + "?" + urllib.parse.urlencode(query)
        payload = api_call(url, cookie=cookie, timeout=timeout, retry_delay=delay,
                           verify=verify, referer="https://message.bilibili.com/")
        data = payload.get("data") or {}
        batch = data.get("items") or []
        if verbose:
            log("    [页 %d] 取到 %d 条（累计 %d）" % (page, len(batch), len(items) + len(batch)))
        for it in batch:
            it["_feed"] = "at" if api_url == AT_API else "reply"
            items.append(it)
        cursor = data.get("cursor") or {}
        cursor_id, cursor_time = cursor.get("id"), cursor.get("time")
        if cursor.get("is_end") or not batch or not cursor_id:
            break
        if max_pages and page >= max_pages:
            break
        time.sleep(delay)
    return items


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------

_BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")


def convert_av_to_bv(aid) -> str:
    try:
        aid_int = int(aid)
    except Exception:
        return ""
    if aid_int <= 0:
        return ""
    table = "fZodR9XQDSUm21yCkr6zBqiveYah8bt4xsWpHnJE7jL5VG3guMTKNPAwcF"
    s = [11, 10, 3, 8, 4, 6]
    x = (aid_int ^ 177451812) + 8728348608
    r = list("BV1  4 1 7  ")
    for i in range(6):
        r[s[i]] = table[x // (58 ** i) % 58]
    return "".join(r).replace(" ", "")


def bv_from_uri(uri: str) -> str:
    if not uri:
        return ""
    m = _BV_RE.search(uri)
    if m:
        return m.group(1)
    m = re.search(r"[?&]aid=(\d+)", uri) or re.search(r"/av(\d+)", uri)
    return convert_av_to_bv(m.group(1)) if m else ""


def extract_type(item: dict) -> str:
    return TYPE_NAMES.get(item.get("business_id")) or (item.get("business") or "未知").strip()


def parse_event(raw: dict, show_content: bool, resolver: CommentResolver = None) -> dict:
    user = raw.get("user") or {}
    item = raw.get("item") or {}
    ctype_name = extract_type(item)

    bv = ""
    for key in ("bvid", "bv_id"):
        val = item.get(key)
        if isinstance(val, str) and _BV_RE.fullmatch(val):
            bv = val
            break
    if not bv:
        bv = bv_from_uri(item.get("uri") or "") or bv_from_uri(item.get("native_uri") or "")
    if not bv and ctype_name == "视频" and resolver:
        m = re.search(r"[?&](?:oid|aid)=(\d+)", item.get("uri") or "")
        if m:
            bv = resolver.bv_from_aid(m.group(1))

    link = ("https://www.bilibili.com/video/%s" % bv) if bv else (item.get("uri") or "")

    ts = raw.get("reply_time") or item.get("at_time") or 0
    try:
        ts = int(ts)
    except Exception:
        ts = 0

    content = re.sub(r"\s+", " ", (item.get("source_content") or item.get("title") or "")).strip()

    parent_text = ""
    pinfo = {}
    if resolver is not None:
        try:
            pinfo = resolver.resolve_parent(item, item.get("business_id"), item.get("subject_id"))
            parent_text = pinfo.get("text") or ""
        except Exception as exc:
            parent_text = "获取父级评论出错：%s" % exc

    return {
        "id": str(raw.get("id") or ""),
        "feed": raw.get("_feed", "at"),
        "mid": str(user.get("mid") or ""),
        "nickname": (user.get("nickname") or "未知用户").strip(),
        "ts": ts,
        "bv": bv,
        "type": ctype_name,
        "content": content,
        "parent": parent_text,
        "parent_rpid": pinfo.get("parent_rpid") or 0,
        "root_rpid": pinfo.get("root_rpid") or 0,
        "parent_is_self": bool(pinfo.get("parent_is_self")),
        # AI 判定用的原始文本
        "parent_content": pinfo.get("parent_content") or "",
        "parent_author": pinfo.get("parent_author") or "",
        "grandparent_content": pinfo.get("grandparent_content") or "",
        "grandparent_author": pinfo.get("grandparent_author") or "",
        "parent_is_direct": bool(pinfo.get("parent_is_direct")),
        "at_is_direct": bool(pinfo.get("at_is_direct")),
        "at_rpid": pinfo.get("at_rpid") or 0,
        "at_root": pinfo.get("at_root") or 0,
        "reply_type": resolver._comment_type(item.get("business_id")) if resolver else None,
        "reply_oid": item.get("subject_id"),
        "replyable": bool(pinfo.get("parent_rpid")),
        "link": link,
        "collected_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# --------------------------------------------------------------------------
# 输出：固定文件，原地更新
# --------------------------------------------------------------------------


def atomic_write(path: str, text: str, encoding: str = "utf-8", retries: int = 5) -> str:
    """写文件并对「文件被占用」做重试与降级，返回真正写入的路径。

    Windows 上如果目标文件正被 Excel/编辑器/杀软打开，os.replace 会抛
    WinError 5（拒绝访问）。以前这会让整轮抓取失败（连回复都发不出去），
    所以这里：先重试几次 → 仍失败就退回直接覆盖写 → 再失败才换新文件名，保证数据不丢。
    """
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            tmp = "%s.tmp%d" % (path, os.getpid())
            with open(tmp, "w", encoding=encoding, newline="") as fh:
                fh.write(text)
            os.replace(tmp, path)
            return path
        except OSError as exc:
            last_err = exc
            try:
                if os.path.isfile(tmp):
                    os.remove(tmp)
            except Exception:
                pass
            if attempt < retries:
                time.sleep(0.4 * attempt)

    # 降级 1：直接覆盖写（不做原子替换）
    try:
        with open(path, "w", encoding=encoding, newline="") as fh:
            fh.write(text)
        log("目标文件曾被占用，已改为直接覆盖写入：%s" % path, "warn")
        return path
    except OSError as exc:
        last_err = exc

    # 降级 2：换一个不会冲突的文件名，保证数据不丢
    fallback = "%s.%s.new" % (os.path.splitext(path)[0],
                              datetime.now().strftime("%Y%m%d_%H%M%S"))
    try:
        with open(fallback, "w", encoding=encoding, newline="") as fh:
            fh.write(text)
        log("无法写入 %s（%s），已改写到 %s —— 请关闭正在占用它的程序。"
            % (path, last_err, fallback), "warn")
        return fallback
    except OSError as exc:
        raise RuntimeError("写入 %s 失败（%s），备用路径也失败（%s）" % (path, last_err, exc))


def render_txt(records, meta: dict, show_content: bool) -> str:
    lines = []
    lines.append("B站「@我的」事件记录（本文件原地更新，不会新建文件）")
    lines.append("登录账号：%s (UID: %s)" % (meta["uname"], meta["mid"]))
    lines.append("累计记录：%d 条" % len(records))
    lines.append("最后更新：%s" % meta["updated_at"])
    lines.append("抓取轮次：第 %d 轮（每 %s 秒一轮）" % (meta["round"], meta["interval"]))
    lines.append("说明：父级评论为「@ 我的那条评论」所回复的上一级评论；")
    lines.append("      若 @ 本身就是一级评论，则显示 %s。" % NONE_MARK)
    lines.append("=" * 78)

    if not records:
        lines.append("（暂无 @ 事件）")
    for idx, ev in enumerate(records, 1):
        lines.append("[%d] @人：%s (UID: %s)" % (idx, ev["nickname"], ev["mid"] or "未知"))
        lines.append("    @视频BV号：%s" % (ev["bv"] or "无（非视频类：%s）" % ev["type"]))
        lines.append("    父级评论：%s" % ev["parent"])
        if show_content and ev.get("content"):
            lines.append("    @我的内容：%s" % ev["content"])
        lines.append("-" * 78)
    return "\n".join(lines) + "\n"


def write_txt_inplace(path: str, records, meta: dict, show_content: bool) -> str:
    """原地更新（带占用重试）。返回实际写入路径。"""
    return atomic_write(path, render_txt(records, meta, show_content), "utf-8")


def write_csv(path: str, records) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["序号", "@人", "UID", "BV号", "类型", "父级评论", "@我的内容", "链接", "采集时间"])
    for idx, ev in enumerate(records, 1):
        writer.writerow([idx, ev["nickname"], ev["mid"], ev["bv"], ev["type"],
                         ev["parent"], ev.get("content", ""), ev["link"], ev["collected_at"]])
    return atomic_write(path, buf.getvalue(), "utf-8-sig")


REPLY_TABLE_HEADER = [
    "序号", "回复时间", "@人", "评论类型", "评论区oid", "回复目标父评论rpid",
    "根评论rpid", "回复内容", "@我的原评论", "是否实际发送", "回复成功",
    "新回复rpid", "结果说明", "视频链接",
]


def write_reply_table(path: str, replies) -> str:
    """把「已回复的父级评论」全部导出成表格（csv，Excel 可直接打开）。"""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(REPLY_TABLE_HEADER)
    ordered = sorted(replies.values(), key=lambda r: r.get("time") or "", reverse=True)
    for idx, r in enumerate(ordered, 1):
        writer.writerow([
            idx, r.get("time", ""), r.get("nickname", ""), r.get("type", ""),
            r.get("oid", ""), r.get("parent_rpid", ""), r.get("root_rpid", ""),
            r.get("message", ""), r.get("at_comment", ""),
            "是" if r.get("posted") else "否（dry-run/仅记录）",
            "是" if r.get("status") == "ok" else "否",
            r.get("reply_rpid", ""), r.get("reason", ""), r.get("link", ""),
        ])
    return atomic_write(path, buf.getvalue(), "utf-8-sig")


# --------------------------------------------------------------------------
# AI 用量统计 / 风控闸门
# --------------------------------------------------------------------------


def _ai_times(state: dict):
    usage = state.get("ai_usage")
    if not isinstance(usage, dict):
        usage = {}
        state["ai_usage"] = usage
    times = usage.get("times")
    if not isinstance(times, list):
        times = []
        usage["times"] = times
    return times


def ai_quota_check(state: dict, args, now: float = None):
    """返回 (ok, 原因)。控制「烧钱」与「被 B站风控」两类风险。"""
    now = now or time.time()
    times = [t for t in _ai_times(state) if isinstance(t, (int, float)) and now - t < 86400]
    state["ai_usage"]["times"] = times

    block_until = state.get("ai_blocked_until") or 0
    if now < block_until:
        return False, "风控冷却中，%s 之后才会继续" % datetime.fromtimestamp(block_until).strftime("%H:%M:%S")
    if times and now - max(times) < args.ai_min_interval:
        return False, "距上次回复不足 %d 秒（防刷屏）" % args.ai_min_interval
    hour = [t for t in times if now - t < 3600]
    if len(hour) >= args.ai_max_per_hour:
        return False, "本小时已回复 %d 条，达上限 %d" % (len(hour), args.ai_max_per_hour)
    if len(times) >= args.ai_max_per_day:
        return False, "今日已回复 %d 条，达上限 %d" % (len(times), args.ai_max_per_day)
    return True, ""


def ai_record_usage(state: dict, now: float = None):
    _ai_times(state).append(now or time.time())


def ai_interval_wait(state: dict, args) -> float:
    """还要等多少秒才满足最小间隔（<=0 表示已满足）。"""
    times = [t for t in _ai_times(state) if isinstance(t, (int, float))]
    if not times:
        return 0.0
    return max(0.0, float(args.ai_min_interval) - (time.time() - max(times)))


def ai_block(state: dict, seconds: int = 3600):
    state["ai_blocked_until"] = time.time() + seconds
    log("  · 已进入风控冷却：%d 秒内不再调用 AI 回复" % seconds, "warn")


# --------------------------------------------------------------------------
# 夜间静默 / 黑白名单
# --------------------------------------------------------------------------


def parse_uid_list(spec: str) -> set:
    """'123,456，789' -> {'123','456','789'}"""
    if not spec:
        return set()
    return {x.strip() for x in str(spec).replace("，", ",").split(",") if x.strip()}


def parse_quiet_hours(spec: str):
    """'23:00-07:00' -> (23*60, 7*60)；格式不对返回 None。

    兼容全角冒号/全角横线（中文输入法很容易打出「23：00-07：00」）。
    """
    if not spec:
        return None
    s = str(spec)
    for a, b in (("：", ":"), ("－", "-"), ("–", "-"), ("—", "-"), ("~", "-")):
        s = s.replace(a, b)
    s = s.strip()
    if "-" not in s:
        return None
    try:
        left, right = s.split("-", 1)
        h1, m1 = [int(x) for x in left.strip().split(":")]
        h2, m2 = [int(x) for x in right.strip().split(":")]
        return (h1 * 60 + m1) % 1440, (h2 * 60 + m2) % 1440
    except Exception:
        return None


def in_quiet_hours(spec: str, now: datetime = None) -> bool:
    """判断当前是否处于静默时段（支持 23:00-07:00 这种跨零点写法）。"""
    rng = parse_quiet_hours(spec)
    if not rng:
        return False
    now = now or datetime.now()
    cur = now.hour * 60 + now.minute
    start, end = rng
    if start == end:
        return False
    if start < end:
        return start <= cur < end
    return cur >= start or cur < end


# --------------------------------------------------------------------------
# 统计报表
# --------------------------------------------------------------------------


def build_stats(state: dict) -> dict:
    records = state.get("records") or {}
    replies = state.get("replies") or {}
    users = state.get("users") or {}
    queue = state.get("reply_queue") or {}
    ai_results = state.get("ai_results") or {}
    times = [t for t in ((state.get("ai_usage") or {}).get("times") or [])
             if isinstance(t, (int, float))]

    categories = {}
    for v in ai_results.values():
        if not isinstance(v, dict):
            continue
        c = v.get("category") or "未分类"
        categories[c] = categories.get(c, 0) + 1

    daily = {}
    for t in times:
        day = datetime.fromtimestamp(t).strftime("%Y-%m-%d")
        daily[day] = daily.get(day, 0) + 1

    top_users = []
    for uid, rec in users.items():
        if not isinstance(rec, dict):
            continue
        top_users.append({
            "uid": uid,
            "nickname": rec.get("nickname") or "",
            "times": rec.get("times"),
            "credit": rec.get("credit"),
            "at_count": int(rec.get("at_count") or 0),
        })
    top_users.sort(key=lambda x: -x["at_count"])

    ok = sum(1 for r in replies.values() if (r or {}).get("status") == "ok")
    return {
        "events": len(records),
        "replied": ok,
        "reply_rows": len(replies),
        "users": len(users),
        "pending": len(queue.get("pending") or []),
        "done": len(queue.get("done") or {}),
        "dropped": len(queue.get("dropped") or {}),
        "categories": categories,
        "daily": daily,
        "top_users": top_users[:10],
        "blocked_until": state.get("ai_blocked_until") or 0,
    }


CATEGORY_NAMES = {
    "argument": "论证", "facts": "事实", "opinion": "只有观点",
    "emotional": "情绪/骂人", "offtopic": "闲聊", "safety": "安全屏蔽",
    "injection": "提示词注入", "video_comment": "一级评论", "refused": "零值拒答",
    "not_follower": "未关注", "quota": "配额告知", "未分类": "未分类",
}


def render_stats(st: dict) -> str:
    lines = []
    lines.append("=" * 46)
    lines.append(" @ 回复机器人 · 统计报表")
    lines.append("=" * 46)
    lines.append("累计 @ 事件      : %d" % st["events"])
    lines.append("成功回复         : %d（台账 %d 行）" % (st["replied"], st["reply_rows"]))
    lines.append("@ 我的人         : %d" % st["users"])
    lines.append("队列：待回复 %d / 已回复 %d / 已丢弃 %d"
                 % (st["pending"], st["done"], st["dropped"]))
    if st.get("blocked_until") and st["blocked_until"] > time.time():
        lines.append("风控冷却至       : %s" % datetime.fromtimestamp(
            st["blocked_until"]).strftime("%Y-%m-%d %H:%M:%S"))

    lines.append("")
    lines.append("-- 判定分类分布 --")
    cats = st.get("categories") or {}
    if cats:
        total = sum(cats.values())
        for k, v in sorted(cats.items(), key=lambda kv: -kv[1]):
            lines.append("  %-10s %4d  %5.1f%%" % (CATEGORY_NAMES.get(k, k), v, v * 100.0 / total))
    else:
        lines.append("  （暂无数据）")

    lines.append("")
    lines.append("-- 每日回复数（最近 7 天） --")
    daily = st.get("daily") or {}
    if daily:
        days = sorted(daily.keys())[-7:]
        for d in days:
            lines.append("  %s  %s  %d" % (d, "█" * min(daily[d], 40), daily[d]))
    else:
        lines.append("  （暂无数据）")

    lines.append("")
    lines.append("-- @ 我最多的人（TOP10） --")
    tops = st.get("top_users") or []
    if tops:
        lines.append("  %-18s %-12s %-6s %-6s %s" % ("UID", "昵称", "次数", "信用", "@我次数"))
        for u in tops:
            lines.append("  %-18s %-12s %-6s %-6s %d" % (
                u["uid"], (u["nickname"] or "")[:10], u["times"], u["credit"], u["at_count"]))
    else:
        lines.append("  （暂无数据）")
    lines.append("=" * 46)
    return "\n".join(lines)


def export_stats_csv(path: str, st: dict) -> str:
    rows = [("section", "key", "value")]
    rows.append(("overview", "events", st["events"]))
    rows.append(("overview", "replied", st["replied"]))
    rows.append(("overview", "users", st["users"]))
    rows.append(("queue", "pending", st["pending"]))
    rows.append(("queue", "done", st["done"]))
    rows.append(("queue", "dropped", st["dropped"]))
    for k, v in (st.get("categories") or {}).items():
        rows.append(("category", k, v))
    for d, v in sorted((st.get("daily") or {}).items()):
        rows.append(("daily", d, v))
    for u in (st.get("top_users") or []):
        rows.append(("user", u["uid"], "%s|%s|%s|%s" % (
            u["nickname"], u["times"], u["credit"], u["at_count"])))
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerows(rows)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8-sig", newline="") as fh:
            fh.write(buf.getvalue())
        os.replace(tmp, path)
    except OSError:
        return ""
    return path


# --------------------------------------------------------------------------
# 待回复队列（持久化、按「事件」去重）
#   规则：
#     · 一个事件只回一条；回复成功并验证后写入 done，永不再次入队
#     · 不同事件可以对同一条评论各回一条（不再按评论去重）
#     · 未验证 / 暂时失败的事件留在队列里，下一轮继续
# --------------------------------------------------------------------------


def _json_safe_ev(ev: dict) -> dict:
    """只保留可 JSON 序列化的字段，避免 state 写盘失败。"""
    out = {}
    for k, v in (ev or {}).items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[k] = v
    return out


class ReplyQueue:
    """持久化待回复队列（存放在 state["reply_queue"]）。"""

    MAX_DONE = 2000          # done 表上限，超出按时间淘汰
    MAX_ATTEMPTS = 5         # 暂时失败的最多尝试次数

    def __init__(self, state: dict):
        self.state = state
        store = state.get("reply_queue")
        if not isinstance(store, dict):
            store = {}
            state["reply_queue"] = store
        self.pending = store.get("pending")
        if not isinstance(self.pending, list):
            self.pending = []
            store["pending"] = self.pending
        self.done = store.get("done")
        if not isinstance(self.done, dict):
            self.done = {}
            store["done"] = self.done
        self.dropped = store.get("dropped")
        if not isinstance(self.dropped, dict):
            self.dropped = {}
            store["dropped"] = self.dropped
        self.keys = {it.get("key") for it in self.pending if isinstance(it, dict)}
        self.replied = 0
        self.skipped = 0
        self.failed = 0

    def __len__(self):
        return len(self.pending)

    # ---- 入队 ----
    def enqueue(self, key, ev) -> bool:
        if not key:
            return False
        if key in self.keys or key in self.done or key in self.dropped:
            return False
        self.pending.append({
            "key": key,
            "ev": _json_safe_ev(ev),
            "added": time.time(),
            "attempts": 0,
        })
        self.keys.add(key)
        return True

    def update_event(self, key, ev):
        """用本轮抓到的更完整数据刷新队列里的快照。"""
        for item in self.pending:
            if item.get("key") == key:
                item["ev"] = _json_safe_ev(ev)
                return

    # ---- 取出 / 归还 ----
    def next_item(self, exclude_keys=None):
        exclude_keys = exclude_keys or set()
        for item in self.pending:
            if item.get("key") not in exclude_keys:
                return item
        return None

    def remove_item(self, item):
        try:
            self.pending.remove(item)
        except ValueError:
            pass
        self.keys.discard(item.get("key"))

    def retry_later(self, item, reason: str):
        """暂时失败：保留在队列里，累加尝试次数，超过上限就丢弃。"""
        item["attempts"] = int(item.get("attempts") or 0) + 1
        item["last_error"] = (reason or "")[:120]
        if item["attempts"] >= self.MAX_ATTEMPTS:
            self.remove_item(item)
            self.dropped[item.get("key")] = "多次失败：%s" % (reason or "")[:80]
            log("  · 队列放弃：%s" % (reason or "")[:60], "warn")

    # ---- 完成 ----
    def ack(self, key, result: dict, verified: bool):
        self.remove_item_for_key(key)
        self.done[key] = {
            "rpid": result.get("reply_rpid", ""),
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "verified": bool(verified),
            "target": result.get("target", "parent"),
            "target_rpid": result.get("target_rpid", ""),
        }
        self.replied += 1
        self._prune_done()

    def remove_item_for_key(self, key):
        for item in list(self.pending):
            if item.get("key") == key:
                try:
                    self.pending.remove(item)
                except ValueError:
                    pass
                self.keys.discard(key)
                return

    def drop(self, key, reason: str):
        self.remove_item_for_key(key)
        self.dropped[key] = (reason or "")[:120]

    def _prune_done(self):
        if len(self.done) <= self.MAX_DONE:
            return
        order = sorted(self.done.items(), key=lambda kv: str(kv[1].get("time") or ""))
        for key, _ in order[:len(self.done) - self.MAX_DONE]:
            self.done.pop(key, None)


def queue_reply(events: dict, keys, args, state: dict, resolver,
                round_no: int = 0, on_result=None) -> dict:
    """把待回复事件放进持久化队列，依次回复。

    events: {key: event}
    keys:   本轮待处理的 key 列表（顺序即入队顺序）
    on_result(key, ev, res): 每条处理完后的回调（记账/写日志用）
    """
    q = ReplyQueue(state)

    new_keys = 0
    for key in keys:
        ev = events.get(key) or {}
        if not ev.get("replyable"):
            continue
        if key in q.keys:
            q.update_event(key, ev)       # 已在队列里 → 用本轮数据刷新快照
            continue
        if q.enqueue(key, ev):
            new_keys += 1

    log("  · 待回复队列：%d 条（本轮新增 %d）" % (len(q), new_keys))
    stats = {"replied": 0, "skipped": 0, "failed": 0, "queued": len(q)}
    if not len(q):
        return stats

    # 夜间静默：整轮跳过，队列原样保留，等时段结束再回
    quiet = getattr(args, "quiet_hours", "") or ""
    if quiet and in_quiet_hours(quiet):
        log("  · 夜间静默中（%s），本轮不回复，队列保留 %d 条" % (quiet, len(q)))
        return stats

    allow_uids = parse_uid_list(getattr(args, "allow_users", ""))
    block_uids = parse_uid_list(getattr(args, "block_users", ""))

    seen = set()
    while True:
        item = q.next_item(seen)
        if item is None:
            break
        key = item.get("key")
        ev = item.get("ev") or {}
        seen.add(key)

        if key in q.done:                 # 已回复验证过 → 直接清掉
            q.remove_item(item)
            continue
        if not ev.get("replyable"):
            q.drop(key, "无可回复目标")
            stats["skipped"] += 1
            continue

        # 黑白名单：优先级 黑名单 > 白名单
        uid = str(ev.get("mid") or "")
        if uid and uid in block_uids:
            q.drop(key, "黑名单用户")
            stats["skipped"] += 1
            continue
        if allow_uids and uid not in allow_uids:
            q.drop(key, "不在白名单")
            stats["skipped"] += 1
            continue

        # AI 模式：先过「额度 / 频率 / 冷却」闸门，再花钱调模型
        # 「最小间隔」不丢任务：等一小会儿再继续，保证队列依次回复
        if resolver.judge is not None and args.reply:
            ok, why = ai_quota_check(state, args)
            if not ok and "不足" in why:
                wait = ai_interval_wait(state, args)
                if 0 < wait <= 120:
                    log("  · 间隔限制：等待 %.0f 秒后继续" % wait)
                    time.sleep(wait + 0.5)
                    ok, why = ai_quota_check(state, args)
            if not ok:
                # 「今日配额用完」不在这里拦：交给 maybe_reply 回固定告知话术
                if "今日已回复" in why:
                    pass
                else:
                    log("  · AI 回复跳过（留在队列）：%s" % why)
                    stats["skipped"] += 1
                    continue

        try:
            res = resolver.maybe_reply(ev, do_reply=args.reply, state=state, args=args)
        except CsrfMismatchError as exc:
            log("  · %s" % exc, "err")
            stats["failed"] += 1
            break
        except LevelTooLowError as exc:
            log("  · %s" % exc, "err")
            stats["failed"] += 1
            break
        except DuplicateCommentError as exc:
            log("  · %s（内容重复，放弃该事件）" % exc, "warn")
            q.drop(key, "重复评论 12051")
            stats["skipped"] += 1
            continue
        except Exception as exc:
            msg = str(exc)
            log("  · 回复异常（留在队列）：%s" % msg[:120], "err")
            if "风控" in msg or "-352" in msg or "-509" in msg:
                ai_block(state, 3600)
            q.retry_later(item, msg)
            stats["failed"] += 1
            continue

        if on_result:
            try:
                on_result(key, ev, res)
            except Exception as exc:
                log("  · 记账异常：%s" % exc, "warn")

        if res.get("posted"):
            # 平台验证：确认这条回复真的存在，确认后才永久出队
            ctype = ev.get("reply_type")
            oid = ev.get("reply_oid")
            target_rpid = res.get("target_rpid") or ev.get("parent_rpid")
            target_root = res.get("target_root") or ev.get("root_rpid")
            verified = resolver.verify_reply(ctype, oid, target_rpid, target_root)
            q.ack(key, res, verified)
            stats["replied"] += 1
            log("  · 已回复并出队（验证%s，队列剩余 %d）"
                % ("通过" if verified else "未通过但已记录", len(q)), "ok")
        elif res.get("skipped") and ("找不到" in (res.get("reason") or "")
                                     or "已删除" in (res.get("reason") or "")):
            q.drop(key, res.get("reason"))
            stats["skipped"] += 1
        else:
            # dry-run / 判定跳过：留在队列，下一轮再看
            stats["skipped"] += 1

    log("  · 队列处理完成：队列 %d，回复 %d，跳过 %d，失败 %d"
        % (stats["queued"], stats["replied"], stats["skipped"], stats["failed"]))
    return stats


def write_ai_log(path: str, entry: dict):
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


# --------------------------------------------------------------------------
# 一轮抓取
# --------------------------------------------------------------------------


def fetch_round(cookie: str, args, state: dict, resolver: CommentResolver, round_no: int) -> int:
    verify = not args.insecure
    log("开始第 %d 轮抓取 ..." % round_no)
    raw_items = fetch_feed(AT_API, cookie, args.pages, args.timeout, args.delay, verify,
                           verbose=not args.quiet)
    if args.include_replies:
        try:
            raw_reply = fetch_feed(REPLY_API, cookie, args.pages, args.timeout, args.delay,
                                   verify, verbose=not args.quiet)
            for it in raw_reply:
                item = it.get("item") or {}
                if (item.get("at_details") or []) or ("@" in (item.get("message") or "")):
                    raw_items.append(it)
        except Exception as exc:
            log("「回复我的」抓取失败，已跳过：%s" % exc, "warn")

    cutoff_ts = int((datetime.now() - timedelta(days=args.days)).timestamp()) if args.days > 0 else 0
    records = state.get("records") or {}
    replies = state.get("replies") or {}
    resolver.replied = replies
    new_count = 0
    to_reply = []

    # 每日/每周重置：把所有人的「次数」/「信用」恢复为 3
    if resolver.book is not None:
        resolver.book.refresh_all()

    for raw in raw_items:
        ev = parse_event(raw, args.show_content, resolver)
        if ev["feed"] == "reply" and not ev["bv"] and not ((raw.get("item") or {}).get("at_details")):
            continue
        if cutoff_ts and ev["ts"] and ev["ts"] < cutoff_ts:
            continue
        key = ev["id"] or ("%s:%s" % (ev["mid"], ev["ts"]))
        old = records.get(key)
        if old is None:
            records[key] = ev
            new_count += 1
            to_reply.append(key)
        else:
            # 旧记录可能缺少回复所需字段 → 用本次结果补全
            if not old.get("replyable"):
                for fld in ("parent", "parent_rpid", "root_rpid", "parent_is_self",
                            "replyable", "reply_type", "reply_oid", "content"):
                    if ev.get(fld) is not None:
                        old[fld] = ev.get(fld)
                old["collected_at"] = ev["collected_at"]
                if not old.get("bv") and ev["bv"]:
                    old["bv"] = ev["bv"]
            # 是否要回复由「平台验证」决定，本地台账只用于记录：
            # 每条可回复的记录都去平台确认一次（评论是否存在、我是否真的回复过）
            if old.get("replyable"):
                to_reply.append(key)

    # ---- 待回复队列：依次回复，确认后出队（单次队列内不重复回复同一评论） ----
    def _on_result(key, ev, res):
        # 用量按「真正发出的回复」计数（拒答/告知类不调 API，但同样要计入频率上限；
        # 关注校验、配额告知这类固定话术不计入，避免越说越超）
        if res.get("posted") and res.get("ai_category") not in ("not_follower", "quota"):
            ai_record_usage(state)

        entry = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "nickname": ev.get("nickname", ""),
            "type": TYPE_NAMES.get(ev.get("reply_type"), str(ev.get("reply_type") or "")),
            "oid": ev.get("reply_oid") or "",
            "parent_rpid": ev.get("parent_rpid") or "",
            "root_rpid": ev.get("root_rpid") or "",
            "message": res.get("message", ""),
            "posted": bool(res.get("posted")),
            "status": "ok" if res.get("posted") else ("skip" if res.get("skipped") else "dry"),
            "reply_rpid": res.get("reply_rpid", ""),
            "reason": res.get("reason", ""),
            "link": ev.get("link", ""),
            "event_id": ev.get("id", ""),
            "at_comment": ev.get("content", ""),
            "ai_category": res.get("ai_category", ""),
            "ai_reason": res.get("ai_reason", ""),
            "ai_calls": res.get("ai_calls", 0),
            "target": res.get("target", "parent"),
            "credit": "%s:%s" % (res.get("credit_kind", ""), res.get("credit_value", "")),
        }
        # AI 决策留痕（便于事后审计 / 调参）
        if resolver.judge is not None:
            write_ai_log(
                args.ai_log or os.path.join(args.outdir, "ai_decisions.jsonl"),
                {
                    "time": entry["time"],
                    "event_id": entry["event_id"],
                    "sender": ev.get("nickname", ""),
                    "parent_rpid": entry["parent_rpid"],
                    "category": entry["ai_category"],
                    "decision": entry["ai_reason"],
                    "reply": entry["message"],
                    "posted": entry["posted"],
                    "reply_rpid": entry["reply_rpid"],
                    "target": entry["target"],
                    "credit": entry["credit"],
                })
        prev = replies.get(key) or {}
        already_ok = prev.get("status") == "ok"
        if res.get("posted"):
            replies[key] = entry
            log("  · 已回复成功 → 新评论 rpid=%s：%s"
                % (res.get("reply_rpid"), res["message"][:40]), "ok")
        elif res.get("skipped"):
            if already_ok:
                log("  · 跳过：%s" % res.get("reason"))
            else:
                log("  · 跳过：%s" % res.get("reason"))
        else:
            if not already_ok:
                replies[key] = entry
            log("  · 待发送（dry-run，加 --reply 才会回复）：%s" % res.get("message", "")[:50])

    queue_stats = queue_reply(records, to_reply, args, state, resolver,
                              round_no=round_no, on_result=_on_result)
    state["last_queue"] = {"round": round_no, **queue_stats,
                           "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

    # ---- 平台对账：以平台为准，把「平台上确实存在我的回复」补记进台账 ----
    # 不再依赖本地记录做判断；这里只是把平台事实同步到表格里，方便查看。
    recovered = 0
    for key in to_reply:
        ev = records.get(key) or {}
        if not ev.get("replyable"):
            continue
        ctype = ev.get("reply_type")
        oid = ev.get("reply_oid")
        if not ctype or not oid:
            continue
        try:
            existing_rpid = resolver.find_my_reply_rpid(
                ctype, oid, ev.get("parent_rpid"), ev.get("root_rpid"))
        except Exception:
            existing_rpid = ""
        if existing_rpid:
            prev = replies.get(key) or {}
            if prev.get("status") == "ok":
                continue
            replies[key] = {
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "nickname": ev.get("nickname", ""),
                "type": TYPE_NAMES.get(ctype, str(ctype)),
                "oid": oid,
                "parent_rpid": ev.get("parent_rpid") or "",
                "root_rpid": ev.get("root_rpid") or "",
                "message": build_reply_message(ev),
                "at_comment": ev.get("content", ""),
                "posted": True,
                "status": "ok",
                "reply_rpid": existing_rpid,
                "reason": "平台对账：平台上已存在我对该评论的回复",
                "link": ev.get("link", ""),
                "event_id": ev.get("id", ""),
            }
            recovered += 1
            log("  · 平台对账：%s 的这条评论已有我的回复（rpid=%s），已补记台账"
                % (ev.get("nickname", ""), existing_rpid))
    if recovered:
        log("平台对账恢复 %d 条已回复记录。" % recovered, "ok")

    if resolver.csrf_failures:
        log("有 %d 条回复因 CSRF 校验失败（-111）没有发出去。" % resolver.csrf_failures, "err")
        log("原因：bili_jct 与 SESSDATA 不是同一次登录产生的。", "err")
        log("解决办法（任选一种）：", "err")
        log("  1) python %s --login   （扫码登录，自动写入配套的 SESSDATA + bili_jct）"
            % os.path.basename(sys.argv[0] or __file__), "err")
        log("  2) 在浏览器 F12 → Application → Cookies 里，把同一次登录的 "
            "SESSDATA 和 bili_jct 一起复制，然后：", "err")
        log("     python %s --update-cookie \"bili_jct=新的jct\"" % os.path.basename(sys.argv[0] or __file__), "err")
        log("     （若 SESSDATA 也过期了，就同时带上 SESSDATA=新的值，用分号隔开）", "err")

    state["records"] = records
    state["replies"] = replies
    state["last_ts"] = int(time.time())
    state["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    state["round"] = round_no
    state["cookie"] = normalize_cookie(cookie) if args.save_cookie else state.get("cookie", "")

    # 排序：时间倒序（时间字段仅用于排序，不写进文件）
    ordered = sorted(records.values(), key=lambda e: e.get("ts") or 0, reverse=True)
    meta = {
        "uname": state.get("uname", "未知"),
        "mid": state.get("mid", ""),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "round": round_no,
        "interval": args.interval,
    }
    out_path = os.path.join(args.outdir, args.outfile)
    os.makedirs(args.outdir, exist_ok=True)

    global _QUIET
    keep_quiet = _QUIET
    _QUIET = True                      # 写文件过程不刷屏
    write_txt_inplace(out_path, ordered, meta, args.show_content)
    if not args.no_csv:
        write_csv(os.path.splitext(out_path)[0] + ".csv", ordered)
    reply_path = args.reply_table or os.path.join(args.outdir, "replied_parents.csv")
    write_reply_table(reply_path, replies)
    if resolver.book is not None:
        resolver.book.export()
    _QUIET = keep_quiet

    save_state(args.state, state)
    log("本轮新增 %d 条，累计 %d 条；已原地更新 %s" % (new_count, len(ordered), out_path),
        "ok" if new_count else "i")
    log("回复表格：%s（共 %d 条记录，其中成功回复 %d 条）"
        % (reply_path, len(replies), sum(1 for r in replies.values() if r.get("status") == "ok")))
    if resolver.book is not None:
        log("次数/信用表格：%s（共 %d 位 @ 我的人）"
            % (resolver.book.table_path, len(resolver.book.users)))
    return new_count


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="抓取 B 站「@我的」事件及其父级评论，固定 txt 文件原地更新，常驻定时抓取。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python bili_at_monitor.py --login                 # 扫码登录\n"
            "  python bili_at_monitor.py --sessdata xxx          # 常驻，每 300 秒抓一次\n"
            "  python bili_at_monitor.py --sessdata xxx --once   # 只抓一次\n"
            "  python bili_at_monitor.py --stop                  # 停止后台实例\n"
            "  python bili_at_monitor.py --status                # 查看运行状态\n"
        ),
    )
    p.add_argument("--login", action="store_true", help="扫码登录并保存 Cookie")
    p.add_argument("--cookie", help="完整 Cookie 字符串")
    p.add_argument("--sessdata", help="只给 SESSDATA")
    p.add_argument("--check", action="store_true", help="只校验 Cookie 并显示未读数")
    p.add_argument("--status", action="store_true", help="显示累计条数与上次抓取时间")
    p.add_argument("--stats", action="store_true",
                   help="输出统计报表（分类分布 / 每日回复 / 用户榜 / 队列状态），并导出 stats.csv")
    p.add_argument("--quiet-hours", default="",
                   help='夜间静默时段，如 "23:00-07:00"（支持跨零点；此时段内不回复，队列保留）')
    p.add_argument("--allow-users", default="",
                   help="只回复这些 UID（逗号分隔）；留空表示不限")
    p.add_argument("--block-users", default="",
                   help="永不回复这些 UID（逗号分隔，优先级高于白名单）")

    p.add_argument("--interval", type=int, default=300, help="两轮抓取之间的间隔秒数（默认 300）")
    p.add_argument("--once", action="store_true", help="只抓一轮就退出（配合计划任务使用）")
    p.add_argument("--stop", action="store_true", help="给正在后台运行的实例发送停止信号")
    p.add_argument("--reset", action="store_true",
                   help="清除停止标记（续跑/清空历史记录），常驻模式必须没有停止标记才会运行")
    p.add_argument("--reset-records", action="store_true",
                   help="配合 --reset：连累计记录一起清空，txt 从头开始记")
    p.add_argument("--update-cookie", metavar="K=V;K=V", default="",
                   help="把额外的 Cookie 字段写入 cookie.txt（例如 --update-cookie \"bili_jct=xxx\"），"
                        "用于给已有的 SESSDATA 补上 bili_jct")
    p.add_argument("--auto-relogin", action="store_true",
                   help="登录失效时自动重新扫码登录（常驻模式适用，需要有人扫手机）")
    p.add_argument("--rounds", type=int, default=0, help="最多抓几轮后退出，0 = 一直运行")

    p.add_argument("--outdir", default=APP_DIR, help="输出目录（默认脚本所在目录）")
    p.add_argument("--outfile", default=DEFAULT_OUTFILE,
                   help="输出 txt 文件名，固定不变、原地更新（默认 %s）" % DEFAULT_OUTFILE)
    p.add_argument("--state", default=DEFAULT_STATE, help="状态文件路径")
    p.add_argument("--no-csv", action="store_true", help="不生成 csv")
    p.add_argument("--reply", action="store_true",
                   help="真正发送回复（默认不开：只检测并记录，属于 dry-run）")
    p.add_argument("--reply-table", default="",
                   help="回复记录表格路径（默认输出目录下 replied_parents.csv）")
    p.add_argument("--keep-timestamp-name", action="store_true",
                   help="恢复旧行为：每次生成带时间戳的新 txt（默认关闭）")
    p.add_argument("--show-content", action="store_true", help="在 txt 中附加 @ 我的内容")
    p.add_argument("--include-replies", action="store_true", help="同时抓「回复我的」中含 @ 的通知")
    p.add_argument("--pages", type=int, default=0, help="最多抓几页，0 = 抓到完")
    p.add_argument("--days", type=int, default=0, help="只保留最近 N 天的 @，0 = 不限")
    p.add_argument("--timeout", type=int, default=20, help="单次请求超时秒数（默认 20）")
    p.add_argument("--delay", type=float, default=1.0, help="请求间隔秒数（默认 1.0）")
    p.add_argument("--insecure", action="store_true", help="跳过 HTTPS 证书校验")
    p.add_argument("--quiet", action="store_true", help="少打印日志")

    # ---------------- AI 法官模式（DeepSeek） ----------------
    p.add_argument("--ai", action="store_true",
                   help="开启 AI 法官模式：收到 @ 时用 DeepSeek 分析父级评论并回复")
    p.add_argument("--ai-key", default=os.environ.get("DEEPSEEK_API_KEY", ""),
                   help="DeepSeek API Key（也可用环境变量 DEEPSEEK_API_KEY 或 config.json）")
    p.add_argument("--ai-model", default=DEFAULT_AI_MODEL,
                   help="模型名（默认 %s；可用 deepseek-flash / deepseek-v4-pro）" % DEFAULT_AI_MODEL)
    p.add_argument("--ai-max-len", type=int, default=400,
                   help="AI 回复最大字数（默认 400，B站上限 1000）")
    p.add_argument("--ai-min-interval", type=int, default=45,
                   help="两次 AI 回复的最小间隔秒数（默认 45，防刷屏）")
    p.add_argument("--ai-max-per-hour", type=int, default=8,
                   help="每小时最多 AI 回复几条（默认 8）")
    p.add_argument("--ai-max-per-day", type=int, default=40,
                   help="每天最多 AI 回复几条（默认 40）")
    p.add_argument("--ai-no-search", action="store_true",
                   help="关闭「事实类评论联网查证」（只靠模型自身知识）")
    p.add_argument("--ai-log", default="",
                   help="AI 决策日志路径（默认输出目录下 ai_decisions.jsonl）")
    p.add_argument("--credits-table", default="",
                   help="次数/信用表格路径（默认输出目录下 user_credits.csv）")
    p.add_argument("--free-user", default=os.environ.get("BILI_FREE_UID", ""),
                   help="特权 UID：该用户不扣次数/信用，回复中数值显示为 ∞（默认不启用）")
    p.add_argument("--ai-no-follow-check", action="store_true",
                   help="关闭「必须关注本账号才回复」的校验")
    return p.parse_args(argv)


def do_stop():
    try:
        with open(STOP_FILE, "w", encoding="utf-8") as fh:
            fh.write(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        log("已写入停止标记 %s；运行中的实例会在本轮结束后退出。" % STOP_FILE, "ok")
        return 0
    except Exception as exc:
        log("写入停止标记失败：%s" % exc, "err")
        return 1


def do_update_cookie(args) -> int:
    """把额外的 Cookie 字段合并进 cookie.txt（保留原有字段，不覆盖已有值）。"""
    path = os.path.join(APP_DIR, "cookie.txt")
    existing = ""
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
            existing = fh.read().strip()
    merged = fix_double_prefix(_bare_cookie_text(existing)) if existing else ""

    pairs = {}
    for part in normalize_cookie(args.update_cookie.replace(";", "; ")).split("; "):
        if "=" in part:
            k, v = part.split("=", 1)
            pairs[k.strip()] = v.strip()
    if not pairs:
        log("--update-cookie 参数格式不对，应形如 \"bili_jct=xxx\"", "err")
        return 1

    for k, v in pairs.items():
        old = extract_cookie_value(merged, k)
        if old and old != v:
            log("字段 %s 已存在，已用新值覆盖（旧值前 6 位：%s...）" % (k, old[:6]))
        merged = remove_cookie_field(merged, k)
        merged = (merged + "; " if merged else "") + "%s=%s" % (k, v)
        log("已写入 %s=%s..." % (k, v[:8]), "ok")

    with open(path, "w", encoding="utf-8") as fh:
        fh.write(merged + "\n")
    log("cookie.txt 已更新：%s" % path, "ok")
    log("当前字段：%s" % ", ".join(p.split("=")[0] for p in merged.split("; ")))
    return 0


def do_status(args):
    state = load_state(args.state)
    records = state.get("records") or {}
    log("状态文件：%s" % args.state)
    log("累计记录：%d 条" % len(records))
    log("上次抓取：%s（第 %s 轮）" % (state.get("last_run", "从未"), state.get("round", "-")))
    log("账号：%s (UID: %s)" % (state.get("uname", "未知"), state.get("mid", "")))
    replies = state.get("replies") or {}
    ok_cnt = sum(1 for r in replies.values() if r.get("status") == "ok")
    dry_cnt = sum(1 for r in replies.values() if r.get("status") == "dry")
    log("回复记录：共 %d 条（已成功回复 %d 条，dry-run 待发 %d 条）"
        % (len(replies), ok_cnt, dry_cnt))
    out_path = os.path.join(args.outdir, args.outfile)
    if os.path.isfile(out_path):
        log("输出文件：%s（%d 字节，最后修改 %s）" % (
            out_path, os.path.getsize(out_path),
            datetime.fromtimestamp(os.path.getmtime(out_path)).strftime("%Y-%m-%d %H:%M:%S")))
    else:
        log("输出文件尚未生成：%s" % out_path)
    if os.path.isfile(STOP_FILE):
        log("停止标记：存在 → 常驻模式不会启动（清除请用 --reset）", "warn")
    else:
        log("停止标记：无 → 可正常启动常驻模式")
    return 0


def do_reset(args):
    state = load_state(args.state)
    if os.path.isfile(STOP_FILE):
        try:
            os.remove(STOP_FILE)
            log("已清除停止标记 %s" % STOP_FILE, "ok")
        except Exception as exc:
            log("清除停止标记失败：%s" % exc, "err")
            return 1
    else:
        log("没有停止标记，无需清除。")
    if args.reset_records:
        state["records"] = {}
        state["round"] = 0
        save_state(args.state, state)
        log("已清空累计记录，下次抓取会重建 txt。", "ok")
    return 0


def run(argv=None) -> int:
    args = parse_args(argv)
    set_quiet(args.quiet)

    if args.stop:
        return do_stop()
    if args.status:
        return do_status(args)
    if args.stats:
        state = load_state(args.state)
        st = build_stats(state)
        print(render_stats(st))
        out = export_stats_csv(os.path.join(args.outdir, "stats.csv"), st)
        if out:
            print("已导出：%s" % out)
        return 0
    if args.reset:
        return do_reset(args)
    if args.update_cookie:
        return do_update_cookie(args)

    # 注意：这里故意不删除停止标记。
    # 常驻实例大多时间在睡眠中，若启动时把标记删掉，另一条 --stop 命令就会失效；
    # 所以标记由「运行中的实例退出时」或用户手动 --reset 清除。

    cookie = load_cookie(args)
    if args.login or not cookie:
        if args.login:
            try:
                cookie = qr_login()
                save_cookie_to_state(cookie, args.state)
                args.save_cookie = True
            except Exception as exc:
                log("扫码登录失败：%s" % exc, "err")
                print_login_help()
                return 2
        else:
            print_login_help()
            return 2
    else:
        args.save_cookie = False

    verify = not args.insecure
    if verify:
        ensure_ssl_ready(verbose=not args.quiet)

    # 校验登录态
    try:
        nav = api_call(NAV_API, cookie=cookie, timeout=args.timeout,
                       retry_delay=args.delay, verify=verify)
    except AuthError as exc:
        # 会话失效：后台常驻模式自动重新扫码登录（交互模式下用户可当场扫码）
        if args.auto_relogin and not args.once and not args.check:
            log("登录已失效（%s），尝试自动重新扫码登录 ..." % exc, "warn")
            try:
                cookie = qr_login()
                save_cookie_to_state(cookie, args.state)
                args.save_cookie = True
                nav = api_call(NAV_API, cookie=cookie, timeout=args.timeout,
                               retry_delay=args.delay, verify=verify)
                log("重新登录成功。", "ok")
            except Exception as exc2:
                log("自动重新登录失败：%s" % exc2, "err")
                print_login_help()
                return 3
        else:
            log(str(exc), "err")
            print_login_help()
            return 3
    except Exception as exc:
        log("校验登录状态失败：%s" % exc, "err")
        return 3

    nav_data = nav.get("data") or {}
    if not nav_data.get("isLogin"):
        log("Cookie 无效或已过期，请重新获取 SESSDATA。", "err")
        print_login_help()
        return 3
    uname = nav_data.get("uname") or "未知"
    mid = str(nav_data.get("mid") or "")
    level_info = nav_data.get("level_info") or {}
    try:
        cur_level = int(level_info.get("current_level") or 0)
    except Exception:
        cur_level = 0
    cur_exp = level_info.get("current_exp")
    next_exp = level_info.get("next_exp")
    log("已登录：%s (UID: %s)" % (uname, mid), "ok")
    if cur_level:
        log("账号等级：LV%d（经验 %s/%s）" % (cur_level, cur_exp, next_exp))
    can_comment = (cur_level == 0) or (cur_level >= MIN_COMMENT_LEVEL)
    if args.reply and not can_comment:
        log("账号等级不足：B站要求 LV%d 及以上才能发表评论，当前 LV%d —— 本次不会发送任何回复。"
            % (MIN_COMMENT_LEVEL, cur_level), "err")
        log("仍会正常抓取并写好回复台账（状态为待发送），等升级后重跑 --reply 即可补发。", "warn")

    if args.check:
        try:
            unread = api_call(UNREAD_API + "?mobi_app=web&build=0", cookie=cookie,
                              timeout=args.timeout, verify=verify)
            d = unread.get("data") or {}
            log("未读：@我 %s / 回复 %s / 点赞 %s / 系统 %s"
                % (d.get("at", "-"), d.get("reply", "-"), d.get("like", "-"), d.get("sys_msg", "-")))
        except Exception as exc:
            log("未读数获取失败（不影响抓取）：%s" % exc, "warn")
        has_csrf = bool(extract_cookie_value(cookie, "bili_jct"))
        log("Cookie 有效。")
        if has_csrf:
            log("bili_jct（CSRF Token）存在 → 可以发送回复（配合 --reply）。", "ok")
        else:
            log("缺少 bili_jct → 只能检测和记录，无法发送回复；"
                "请 --login 重新登录补齐。", "warn")
        return 0

    if args.keep_timestamp_name:
        args.outfile = "bilibili_at_%s.txt" % datetime.now().strftime("%Y%m%d_%H%M%S")

    state = load_state(args.state)
    state["uname"], state["mid"] = uname, mid
    resolver = CommentResolver(cookie, timeout=args.timeout, delay=args.delay, verify=verify,
                               my_uid=mid, replied=state.get("replies") or {})

    # ---- AI 法官模式 ----
    if args.ai:
        if not args.ai_key:
            log("未提供 DeepSeek API Key（--ai-key 或环境变量 DEEPSEEK_API_KEY）", "err")
            return 6
        try:
            from deepseek_judge import Judge
        except Exception as exc:
            log("无法加载 deepseek_judge.py：%s" % exc, "err")
            return 6
        resolver.judge = Judge(
            api_key=args.ai_key, model=args.ai_model,
            max_reply_len=args.ai_max_len,
            search_enabled=not args.ai_no_search,
            log_func=lambda msg, level="i": log(msg, level))
        log("[AI] 法官模式已启用：模型 %s｜联网查证 %s｜每小时≤%d 条｜每日≤%d 条｜最小间隔 %ds"
            % (args.ai_model, "开" if not args.ai_no_search else "关",
               args.ai_max_per_hour, args.ai_max_per_day, args.ai_min_interval), "ok")

        # 次数/信用积分簿（记录每个 @ 我的人）
        try:
            from user_credits import CreditBook
            table_path = args.credits_table or os.path.join(args.outdir, "user_credits.csv")
            state.setdefault("users", {})
            resolver.book = CreditBook(state["users"], table_path,
                                       free_uid=getattr(args, "free_user", ""))
            resolver.book.refresh_all()
            free_note = ("；特权 UID %s 不扣减（显示 ∞）" % resolver.book.free_uid
                         if resolver.book.free_uid else "")
            log("[AI] 次数/信用已启用：每天重置次数、每周重置信用（0~3）；表格 %s%s"
                % (table_path, free_note), "ok")
        except Exception as exc:
            log("[AI] 次数/信用初始化失败：%s" % exc, "err")
            return 6

    # 等级不够时不要再去撞 12074，只做 dry-run 记录
    if args.reply and not can_comment:
        args.reply = False
        resolver.level_blocked = True
    if args.reply and not extract_cookie_value(cookie, "bili_jct"):
        log("缺少 bili_jct（CSRF Token），无法发送回复 —— 已终止，避免你以为在回复其实没发。", "err")
        log("解决办法：执行 python %s --login 重新扫码登录（会自动存下 bili_jct），"
            % os.path.basename(sys.argv[0] or __file__), "err")
        log("或在 cookie.txt 里补上 bili_jct=xxx（浏览器 Cookie 里能拿到）。", "err")
        log("若只想先看不发，去掉 --reply 即可（默认就是 dry-run）。", "err")
        return 5

    if args.once:
        try:
            fetch_round(cookie, args, state, resolver, (state.get("round") or 0) + 1)
        except AuthError as exc:
            log(str(exc), "err")
            return 3
        except Exception as exc:
            log("抓取失败：%s" % exc, "err")
            return 4
        return 0

    # ---- 常驻模式 ----
    if os.path.isfile(STOP_FILE):
        log("检测到停止标记 %s，常驻模式不会启动。" % STOP_FILE, "warn")
        log("如需继续运行，请先执行 python %s --reset" % os.path.basename(sys.argv[0] or __file__))
        return 0
    log("进入常驻模式：每 %d 秒抓取一次；停止请执行 python %s --stop"
        % (args.interval, os.path.basename(sys.argv[0] or __file__)))
    log("输出文件：%s（原地更新，不会新建 txt）" % os.path.join(args.outdir, args.outfile))
    round_no = state.get("round") or 0
    start_round = round_no          # 基线固定在进入循环前，避免被 state 的更新带跑
    while True:
        round_no += 1
        try:
            fetch_round(cookie, args, state, resolver, round_no)
        except AuthError as exc:
            log("登录失效：%s" % exc, "err")
            log("请重新执行 --login 或更新 SESSDATA 后重启脚本。", "err")
            return 3
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            log("本轮抓取异常（不影响后续轮次）：%s" % exc, "warn")

        if args.rounds and (round_no - start_round) >= args.rounds:
            log("已达到指定轮数 %d，退出。" % args.rounds, "ok")
            break

        # 可被 --stop 提前打断的睡眠
        slept = 0
        while slept < args.interval:
            if os.path.isfile(STOP_FILE):
                break
            time.sleep(min(1, args.interval - slept))
            slept += 1
        if os.path.isfile(STOP_FILE):
            log("检测到停止标记，退出常驻模式。", "ok")
            try:
                os.remove(STOP_FILE)
            except Exception:
                pass
            break
    return 0


def _selftest():
    """内置小自检：python bili_at_monitor.py --selftest"""
    cases = [
        ("https://www.bilibili.com/video/BV1GJ411x7h7/?spm_id_from=333", "BV1GJ411x7h7"),
        ("bilibili://video/12345?bvid=BV1xx411c7mD", "BV1xx411c7mD"),
        ("https://www.bilibili.com/video/av170001", convert_av_to_bv(170001)),
        ("https://www.bilibili.com/opus/1073543151725051921", ""),
    ]
    ok = True
    for uri, expect in cases:
        got = bv_from_uri(uri)
        if got != expect:
            ok = False
        print("[%s] %s -> %s (期望 %s)" % ("OK " if got == expect else "BAD", uri[:58], got, expect))
    print("[i] av170001 -> %s" % convert_av_to_bv(170001))
    print("[i] 自检%s" % ("通过" if ok else "失败"))
    return 0 if ok else 1


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    try:
        sys.exit(run())
    except KeyboardInterrupt:
        log("已被用户中断（Ctrl+C）。", "warn")
        sys.exit(130)
