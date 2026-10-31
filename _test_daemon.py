# -*- coding: utf-8 -*-
"""带假接口数据启动常驻实例，用来做进程级的后台/停止验证。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bili_at_monitor as m

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_test_out")

ROOT, SUB = 5001, 5002
COMMENTS = {
    ROOT: (0, 0, "楼主UP", 900, 1700000000, "这是父级评论的内容"),
    SUB: (ROOT, ROOT, "路人甲", 901, 1700000100, "二级评论"),
}


def fake_parent_lookup(self, ctype, oid, rpid, root=0):
    if int(rpid) not in COMMENTS:
        return None
    rt, pn, uname, mid, ctime, msg = COMMENTS[int(rpid)]
    return {"rpid": int(rpid), "root": rt, "parent": pn,
            "member": {"uname": uname, "mid": mid}, "ctime": ctime,
            "content": {"message": msg}}


m.CommentResolver.get_comment = fake_parent_lookup

_n = {"round": 0}


def fake_api_call(url, *a, **kw):
    if m.NAV_API in url:
        return {"code": 0, "data": {"isLogin": True, "uname": "守护进程测试", "mid": 9}}
    if m.UNREAD_API in url:
        return {"code": 0, "data": {"at": 0}}
    if m.AT_API in url:
        _n["round"] += 1
        return {"code": 0, "data": {"cursor": {"is_end": True, "id": 1, "time": 1}, "items": [{
            "id": 900 + _n["round"],
            "user": {"mid": 111, "nickname": "第%d轮的小明" % _n["round"]},
            "item": {"type": "at", "business_id": 1, "business": "视频",
                     "source_id": SUB, "root_id": ROOT, "subject_id": 170001,
                     "uri": "https://www.bilibili.com/video/BV1GJ411x7h7"},
            "reply_time": 1749474709, "_feed": "at",
        }]}}
    if m.REPLY_API in url:
        return {"code": 0, "data": {"cursor": {"is_end": True}, "items": []}}
    raise AssertionError(url)


m.api_call = fake_api_call
m.ensure_ssl_ready = lambda verbose=True: False

sys.exit(m.run(["--sessdata", "FAKE", "--outdir", OUT, "--outfile", "daemon.txt",
                "--state", os.path.join(OUT, "daemon_state.json"),
                "--interval", "2", "--delay", "0", "--quiet"]))
