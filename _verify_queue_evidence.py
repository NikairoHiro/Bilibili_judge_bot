# -*- coding: utf-8 -*-
"""验证：① 添加论据（@ 评论原话一律并入） ② 持久化队列（按事件去重、验证后出队）

全程离线：拦截 Judge.chat 与 maybe_reply，不真的调 API、不发评论。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bili_at_monitor as m
import deepseek_judge as dj

FAILS = []


def check(name, ok, detail=""):
    print(("  [OK ] " if ok else "  [BAD] ") + name + (("  " + str(detail)[:90]) if detail else ""))
    if not ok:
        FAILS.append(name)


# ==========================================================================
# 一、添加论据：@ 评论原话是否一律并入模型输入
# ==========================================================================
print("== 一、添加论据 ==")


class Recorder:
    """拦截 judge.chat，记录送进模型的 prompt。"""

    def __init__(self):
        self.prompts = []

    def __call__(self, messages, **kwargs):
        user = (messages or [{}])[-1].get("content", "")
        self.prompts.append(user)
        if "请判断下面这条评论的主要性质" in user:
            return ('{"category":"argument","claims":["2023年钢材价格下跌20%"],'
                    '"fallacies":[],"gist":"测试"}')
        return "你这段话是「正确」的。测试回复。"


def make_ev(content, parent="我认为这个方案长期成本更低，因为材料价格在下降。", direct=False):
    return {
        "id": "ev-1", "mid": "200", "nickname": "测试", "content": content,
        "parent_content": parent, "grandparent_content": "",
        "at_is_direct": direct, "reply_type": 1, "reply_oid": 1,
        "parent_rpid": 100, "root_rpid": 100,
    }


judge = dj.Judge("sk-fake", "deepseek-flash", max_reply_len=400,
                 search_enabled=True, log_func=lambda *a, **k: None)
rec = Recorder()
judge.chat = rec

# 1) 没有「视频信息」关键词，也要并入
MARK = "钢材价格2023年跌了20%（标记ABC123）"
res = judge.build_reply(make_ev("@我 补充材料：" + MARK))
first_prompt = rec.prompts[0] if rec.prompts else ""
check("无关键词也会把 @ 原话并入分类输入", MARK in first_prompt)
check("并入后确实走了 argument 分支", res.get("category") == "argument", res.get("category"))

# 2) 有「视频信息」关键词同样并入（不是靠关键词触发）
rec2 = Recorder()
judge.chat = rec2
judge.build_reply(make_ev("@我 视频信息：" + MARK))
check("带关键词也会并入（关键词不再是条件）", MARK in (rec2.prompts[0] if rec2.prompts else ""))

# 3) @ 原话与父级评论相同时不重复拼接
rec3 = Recorder()
judge.chat = rec3
same = "同一条内容只应出现一次（标记XYZ789）"
judge.build_reply(make_ev(same, parent=same))
p3 = rec3.prompts[0] if rec3.prompts else ""
check("原话与父级相同时只出现一次", p3.count("标记XYZ789") == 1, p3.count("标记XYZ789"))

# 4) 论点类会把论据拿去联网核对（看 reason 与搜索调用）
check("论点类标注了「论据已联网核对」", "论据已联网核对" in (res.get("reason") or ""),
      res.get("reason"))

# ==========================================================================
# 二、队列：按事件去重、验证后出队、跨事件可回同一评论
# ==========================================================================
print("\n== 二、回复队列 ==")


class StubResolver:
    judge = object()          # 非 None 即可，走 AI 配额闸门
    verify_result = True

    def __init__(self):
        self.posts = []

    def maybe_reply(self, ev, do_reply, state=None, args=None):
        self.posts.append(ev.get("id"))
        return {
            "posted": True, "skipped": False, "message": "测试回复",
            "reply_rpid": "r-%s" % ev.get("id"), "target": "parent",
            "target_rpid": ev.get("parent_rpid"),
            "target_root": ev.get("root_rpid"),
            "ai_category": "argument",
        }

    def verify_reply(self, ctype, oid, target_rpid, target_root):
        return self.verify_result


class Args:
    reply = True
    quiet = True
    ai_min_interval = 0
    ai_max_per_hour = 100
    ai_max_per_day = 100


def ev(eid, parent=100):
    return {"id": eid, "replyable": True, "reply_type": 1, "reply_oid": 1,
            "parent_rpid": parent, "root_rpid": parent, "mid": "200",
            "at_rpid": parent + 1, "at_root": parent, "nickname": "x"}


state = {}
events = {"e1": ev("e1"), "e2": ev("e2")}     # 两个不同事件，同一评论
resolver = StubResolver()
args = Args()

stats = m.queue_reply(events, ["e1", "e2"], args, state, resolver)
q = state.get("reply_queue") or {}
check("两个不同事件都能入队并回复（同一评论不回退成一条）",
      stats["replied"] == 2 and len(resolver.posts) == 2, resolver.posts)
check("验证通过后都出队", len(q.get("pending") or []) == 0, len(q.get("pending") or []))
check("done 里记了两条事件", len(q.get("done") or {}) == 2, list((q.get("done") or {}).keys()))
check("done 记录验证状态", all(v.get("verified") for v in (q.get("done") or {}).values()))

# 再跑一次同样的两个事件 → 不应再发
resolver.posts.clear()
stats2 = m.queue_reply(events, ["e1", "e2"], args, state, resolver)
check("已回复事件不会再次入队/再发", stats2["replied"] == 0 and not resolver.posts,
      "posted=%s" % resolver.posts)
check("队列本轮新增 0", stats2["queued"] == 0, stats2["queued"])

# 新增第三个事件，仍指向同一评论 → 应再发一条
events["e3"] = ev("e3")
stats3 = m.queue_reply(events, ["e3"], args, state, resolver)
check("新事件可对同一评论再发一条", stats3["replied"] == 1 and resolver.posts == ["e3"],
      resolver.posts)
check("done 累计三条", len(state["reply_queue"]["done"]) == 3)

# 验证失败时：仍然出队（避免重复发），但 verified=False
state_f = {}
r_f = StubResolver()
r_f.verify_result = False
m.queue_reply({"fx": ev("fx")}, ["fx"], args, state_f, r_f)
done_fx = state_f["reply_queue"]["done"].get("fx") or {}
check("验证不通过也会记录并出队（verified=False）",
      done_fx.get("verified") is False and not state_f["reply_queue"]["pending"],
      done_fx)

print()
if FAILS:
    print("[×] 失败 %d 项：%s" % (len(FAILS), FAILS))
    sys.exit(1)
print("[√] 队列 + 添加论据 全部验证通过")
