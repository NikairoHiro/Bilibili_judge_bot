#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
user_credits.py —— @人「次数 / 信用」积分簿

规则（对应需求）：
  · 每个 @ 我的人（按 UID）一行记录，初始「次数=3，信用=3」
  · 每天把所有人的「次数」重置为 3
  · 每周把所有人的「信用」重置为 3
  · 两个值永远限制在 [0, 3]
  · 可选的「特权 UID」（环境变量 BILI_FREE_UID 或 --free-user）不扣减，
    回复里的 n 显示为 ∞；默认不启用

扣减约定：
  · 一级评论 / 闲聊 / 无法归类  → 扣「信用」
  · 正常分析回答              → 扣「今日次数」
"""

from __future__ import annotations

import csv
import io
import os
from datetime import datetime

CREDIT_MAX = 3


def parse_uids(spec) -> set:
    """把 '123,456，789' / 列表 解析成 {"123","456","789"}。"""
    if not spec:
        return set()
    if isinstance(spec, (list, tuple, set)):
        items = spec
    else:
        items = str(spec).replace("，", ",").split(",")
    return {str(x).strip() for x in items if str(x).strip()}


# 管理员名单：不扣次数/信用、数值显示 ∞。
# 默认读环境变量 BILI_ADMINS（逗号分隔）；命令行 --admins 会覆盖它。
ADMINS = parse_uids(os.environ.get("BILI_ADMINS", ""))
# 兼容旧变量名
ADMINS |= parse_uids(os.environ.get("BILI_FREE_UID", ""))
FREE_UID = next(iter(ADMINS), "")

SUFFIX_CREDIT = "信用-1，当前信用为{n}/3"
SUFFIX_TIMES = "今日次数-1，当前次数为{n}/3"
# 多套等价说法，避免 B站「重复评论 12051」
ZERO_CREDIT_VARIANTS = (
    "你的信用已经为 0 了，这时候来找我是「错误」的，等信用恢复再来吧。",
    "你的信用已经见底了，这时候找我是「错误」的，先把信用养回来再说。",
    "你的信用是 0，现在找我评判是「错误」的，等恢复了再来。",
)
ZERO_TIMES_VARIANTS = (
    "你今天找我的次数已经用完了，这时候再来找我是「错误」的，明天再来吧。",
    "今天的次数用光了，这时候找我是「错误」的，明天再来。",
    "你今天的次数已经为 0，再找我是「错误」的，明天再来吧。",
)
ZERO_CREDIT = ZERO_CREDIT_VARIANTS[0]
ZERO_TIMES = ZERO_TIMES_VARIANTS[0]


def pick_zero(kind: str) -> str:
    """随机取一套零值拒答文案。"""
    import random
    return random.choice(ZERO_CREDIT_VARIANTS if kind == "credit" else ZERO_TIMES_VARIANTS)


def today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def week_str() -> str:
    y, w, _ = datetime.now().isocalendar()
    return "%d-W%02d" % (y, w)


def clamp(value) -> int:
    """把任意输入夹到 [0, 3]；非法值按 3 处理。"""
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = CREDIT_MAX
    return max(0, min(CREDIT_MAX, value))


def _norm_uid(uid) -> str:
    uid = str(uid or "").strip()
    return uid


class CreditBook:
    """管理所有 @ 人的次数/信用，并可导出成 CSV 表格。

    admins：管理员名单（不扣减、数值显示 ∞）。
      默认取环境变量 BILI_ADMINS；命令行 --admins 可覆盖。
      free_uid 参数为兼容旧版保留，等价于往名单里加一个人。
    """

    def __init__(self, users: dict = None, table_path: str = "", admins=None,
                 free_uid=None):
        self.users = users if isinstance(users, dict) else {}
        self.table_path = table_path
        if admins is None:
            base = set(ADMINS)
        else:
            base = parse_uids(admins)
        base |= parse_uids(free_uid)
        self.admin_uids = base

    @property
    def free_uid(self):
        """兼容旧代码：返回名单里的第一个人（没有则空串）。"""
        return next(iter(self.admin_uids), "") if self.admin_uids else ""

    # ---------------- 记录 / 重置 ----------------
    def ensure(self, uid, nickname: str = "") -> dict:
        uid = _norm_uid(uid)
        rec = self.users.get(uid)
        if not isinstance(rec, dict):
            rec = {}
        rec["times"] = clamp(rec.get("times", CREDIT_MAX))
        rec["credit"] = clamp(rec.get("credit", CREDIT_MAX))
        rec["day"] = str(rec.get("day") or "")
        rec["week"] = str(rec.get("week") or "")
        rec["nickname"] = str(rec.get("nickname") or nickname or "")
        try:
            rec["at_count"] = max(0, int(rec.get("at_count") or 0))
        except (TypeError, ValueError):
            rec["at_count"] = 0
        rec["last"] = str(rec.get("last") or "")
        self.users[uid] = rec
        return rec

    def refresh(self, uid) -> dict:
        """按天/周重置后返回记录。"""
        rec = self.ensure(uid)
        t, w = today_str(), week_str()
        if rec["day"] != t:
            rec["times"] = CREDIT_MAX
            rec["day"] = t
        if rec["week"] != w:
            rec["credit"] = CREDIT_MAX
            rec["week"] = w
        rec["times"] = clamp(rec["times"])
        rec["credit"] = clamp(rec["credit"])
        return rec

    def refresh_all(self):
        """把所有已记录用户按天/周重置（常驻运行时每轮调用一次）。"""
        for uid in list(self.users.keys()):
            self.refresh(uid)

    def touch(self, uid, nickname: str = "") -> dict:
        """记录一次 @（累加计数、更新昵称/时间），并做每日/每周重置。"""
        rec = self.refresh(uid)
        if nickname:
            rec["nickname"] = str(nickname)
        rec["at_count"] = int(rec.get("at_count") or 0) + 1
        rec["last"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return rec

    # ---------------- 查询 / 扣减 ----------------
    def is_free(self, uid) -> bool:
        return _norm_uid(uid) in self.admin_uids

    def value(self, uid, kind: str) -> int:
        rec = self.refresh(uid)
        return rec["credit"] if kind == "credit" else rec["times"]

    def spend(self, uid, kind: str) -> int:
        """扣 1 点并返回扣完后的值；FREE_UID 不扣。"""
        rec = self.refresh(uid)
        if self.is_free(uid):
            return rec["credit"] if kind == "credit" else rec["times"]
        if kind == "credit":
            rec["credit"] = clamp(rec["credit"] - 1)
            return rec["credit"]
        rec["times"] = clamp(rec["times"] - 1)
        return rec["times"]

    def show(self, uid, value: int) -> str:
        """FREE_UID 显示 ∞，其他人显示数字。"""
        return "∞" if self.is_free(uid) else str(clamp(value))

    def suffix(self, uid, kind: str, value: int) -> str:
        template = SUFFIX_CREDIT if kind == "credit" else SUFFIX_TIMES
        return template.format(n=self.show(uid, value))

    # ---------------- 导出表格 ----------------
    TABLE_HEADER = ["UID", "昵称", "次数", "信用", "统计日期", "统计周", "累计@次数", "最后互动"]

    def export(self, path: str = "") -> str:
        path = path or self.table_path
        if not path:
            return ""
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(self.TABLE_HEADER)
        for uid in sorted(self.users.keys(), key=lambda x: (len(x), x)):
            if not str(uid).isdigit():
                continue        # 只导出数字 UID，避免误把别的字段当用户
            rec = self.ensure(uid)
            writer.writerow([
                uid, rec["nickname"], rec["times"], rec["credit"],
                rec["day"], rec["week"], rec["at_count"], rec["last"],
            ])
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8-sig", newline="") as fh:
                fh.write(buf.getvalue())
            os.replace(tmp, path)
        except OSError:
            # 文件被占用时直接覆盖写
            try:
                with open(path, "w", encoding="utf-8-sig", newline="") as fh:
                    fh.write(buf.getvalue())
            except OSError:
                return ""
        return path
