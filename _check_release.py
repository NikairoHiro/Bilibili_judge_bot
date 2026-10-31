#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发布前自检：扫描源码里是否残留密钥 / Cookie / 个人标识。

    python _check_release.py

只检查会被提交的源码文件（.py/.js/.bat/.md/.json），不检查运行时数据文件。
"""

from __future__ import annotations

import os
import re
import sys

SOURCE_EXT = {".py", ".js", ".bat", ".md", ".json", ".txt", ".yml", ".yaml"}
SKIP_DIRS = {".git", "langup-ai-master", "__pycache__", "_test_out", ".venv", "venv"}
SKIP_FILES = {"cookie.txt", "_state.json", "gui_config.json", "user_credits.csv",
              "bilibili_at.txt", "bilibili_at.csv", "replied_parents.csv",
              "stats.csv", "ai_decisions.jsonl", "bili_at_monitor.log", "gui_run.log"}

PATTERNS = [
    ("DeepSeek Key", re.compile(r"sk-[A-Za-z0-9]{16,}")),
    ("SESSDATA 值", re.compile(r"SESSDATA=[0-9a-fA-F%]{20,}")),
    ("bili_jct 值", re.compile(r"bili_jct=[0-9a-fA-F]{32}")),
    ("绝对用户路径", re.compile(r"[A-Za-z]:\\\\?Users\\\\?[^\\\s]+")),
]


def iter_sources(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            if name in SKIP_FILES:
                continue
            if os.path.splitext(name)[1].lower() not in SOURCE_EXT:
                continue
            yield os.path.join(dirpath, name)


def main() -> int:
    root = os.path.dirname(os.path.abspath(__file__))
    problems = []
    checked = 0
    for path in iter_sources(root):
        checked += 1
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        rel = os.path.relpath(path, root)
        for label, pat in PATTERNS:
            for m in pat.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                problems.append((rel, line, label, m.group(0)[:40]))

    print("已检查 %d 个源码文件" % checked)
    if problems:
        print("\n发现疑似敏感信息：")
        for rel, line, label, sample in problems:
            print("  %-28s :%-4d %-14s %s" % (rel, line, label, sample))
        print("\n请清理后再发布。")
        return 1

    print("未发现残留密钥 / Cookie / 绝对用户路径。")

    # 提醒：这些运行时文件即便被 .gitignore 忽略，打包时仍可能被带上
    risky = [f for f in ("cookie.txt", "_state.json", "gui_config.json")
             if os.path.isfile(os.path.join(root, f))]
    if risky:
        print("\n注意：以下运行时文件含登录凭证，已被 .gitignore 排除；")
        print("      但如果你直接打包整个目录，它们仍会一起被拷走，请手动删除：")
        for f in risky:
            print("      -", f)
    print("\n发布前建议：")
    print("  1. 确认 git 仓库里没有 cookie.txt / _state.json / gui_config.json")
    print("  2. 如果你曾公开过旧版本，去 DeepSeek 控制台吊销旧 Key")
    print("  3. 删除 langup-ai-master / langup-ai-master.zip（第三方代码，非本项目）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
