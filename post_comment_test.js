#!/usr/bin/env node
/**
 * post_comment_test.js —— B站评论发送测试（Node.js 版，零依赖，Node 18+ 自带 fetch）
 *
 * 用法：
 *   node post_comment_test.js
 *   node post_comment_test.js --bvid BV1xx411c7mD
 *   node post_comment_test.js --bvid BV1xx411c7mD --message "你好"
 *   node post_comment_test.js --deepseek        # 需环境变量 DEEPSEEK_API_KEY
 *
 * 原理（2026-10 实测可用）：
 *   1. GET  /x/web-interface/nav            用 SESSDATA 验证登录
 *   2. GET  /x/web-interface/view?bvid=...  拿 aid（评论区的 oid）
 *   3. POST /x/v2/reply/add                 表单提交 type=1, oid=aid, message, plat=1, csrf=bili_jct
 *   4. GET  /x/v2/reply?sort=0              回读确认评论真的存在
 */

// ============================ 配置区 ============================
// 不要把 Cookie 写进源码：优先读环境变量，其次读同目录 cookie.txt
const fs = require("fs");
const path = require("path");

function loadCookiePair() {
  let sess = (process.env.BILI_SESSDATA || "").trim();
  let jct = (process.env.BILI_JCT || "").trim();
  if (sess && jct) return [sess, jct];
  try {
    const txt = fs.readFileSync(path.join(__dirname, "cookie.txt"), "utf8");
    const m1 = txt.match(/SESSDATA=([^;\s]+)/);
    const m2 = txt.match(/bili_jct=([0-9a-fA-F]+)/);
    if (m1) sess = sess || m1[1];
    if (m2) jct = jct || m2[1];
  } catch (e) {}
  return [sess, jct];
}

const [SESSDATA, BILI_JCT] = loadCookiePair();
const TARGET_BVID = "";
// ===============================================================

const UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36";

const COOKIE = `SESSDATA=${SESSDATA}; bili_jct=${BILI_JCT}`;

const ERROR_HINTS = {
  "-101": "账号未登录：SESSDATA 会话已失效，必须换新 Cookie",
  "-111": "CSRF 校验失败：bili_jct 与 SESSDATA 不是同一次登录的",
  "-352": "风控校验失败：需要签名或账号被风控",
  "-403": "权限不足：可能被风控拦截",
  "-509": "请求过于频繁",
  12002: "评论区已关闭",
  12006: "没有该评论",
  12015: "需要评论验证码",
  12016: "评论内容包含敏感信息",
  12025: "评论字数过多",
  12035: "该账号被 UP 主列入黑名单",
  12051: "重复评论，请勿刷屏",
  12074: "账号等级不足（需 LV2+）",
};

function headers(referer = "https://www.bilibili.com/") {
  return {
    "User-Agent": UA,
    Referer: referer,
    Origin: "https://www.bilibili.com",
    Accept: "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    Cookie: COOKIE,
  };
}

async function getJson(url, referer) {
  const res = await fetch(url, { headers: headers(referer) });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

async function postForm(url, data, referer) {
  const body = new URLSearchParams(data).toString();
  const h = headers(referer);
  h["Content-Type"] = "application/x-www-form-urlencoded";
  const res = await fetch(url, { method: "POST", headers: h, body });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

// 1) 验证登录
async function checkLogin() {
  const d = await getJson("https://api.bilibili.com/x/web-interface/nav");
  if (d.code !== 0 || !d.data?.isLogin) {
    console.log(`[×] 未登录：code=${d.code} message=${d.message}`);
    return null;
  }
  const li = d.data.level_info || {};
  console.log(
    `[√] 已登录：${d.data.uname} (UID: ${d.data.mid}) 等级 LV${li.current_level}`
  );
  return d.data;
}

// 2) bvid -> aid
async function getAid(bvid) {
  const d = await getJson(
    "https://api.bilibili.com/x/web-interface/view?bvid=" + bvid
  );
  if (d.code !== 0 || !d.data?.aid) {
    throw new Error(`获取视频失败：${d.code} ${d.message}`);
  }
  console.log(`[√] 视频：${d.data.title} | aid=${d.data.aid}`);
  return d.data.aid;
}

// 3) 可选：用 DeepSeek 生成评论
async function deepseekComment() {
  const key = process.env.DEEPSEEK_API_KEY;
  if (!key) return null;
  const prompt =
    "你是一个B站用户，请用一句自然、口语化的中文评论这个视频，" +
    "不超过40字，不要用引号，不要带任何前缀。";
  const res = await fetch("https://api.deepseek.com/chat/completions", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${key}`,
    },
    body: JSON.stringify({
      model: "deepseek-chat",
      messages: [{ role: "user", content: prompt }],
      temperature: 1.0,
      max_tokens: 100,
    }),
  });
  const d = await res.json();
  return d.choices?.[0]?.message?.content?.trim() || null;
}

// 4) 发评论
async function postComment(aid, message, bvid) {
  const url = "https://api.bilibili.com/x/v2/reply/add";
  const referer = `https://www.bilibili.com/video/${bvid}/`;
  const data = {
    type: 1,
    oid: aid,
    message,
    plat: 1,
    csrf: BILI_JCT,
  };
  console.log("[i] 发送中 ...");
  const d = await postForm(url, data, referer);
  console.log(`    -> code=${d.code} message=${d.message}`);
  if (d.code !== 0) {
    console.log("[×] 发送失败");
    const hint = ERROR_HINTS[String(d.code)];
    if (hint) console.log("    原因：" + hint);
    return null;
  }
  const rpid = String(d.data?.rpid ?? "");
  console.log(`[√] 发送成功！rpid=${rpid}`);
  return rpid;
}

// 5) 回读验证
async function verify(aid, rpid) {
  await new Promise((r) => setTimeout(r, 1200));
  const d = await getJson(
    `https://api.bilibili.com/x/v2/reply?type=1&oid=${aid}&sort=0&ps=20&pn=1`
  );
  const hit = (d.data?.replies || []).find((r) => String(r.rpid) === rpid);
  if (hit) {
    console.log(`[√] 评论区可见：${hit.member?.uname}：${hit.content?.message}`);
    return true;
  }
  console.log("[!] 列表里暂未找到（刚发可能有延迟）");
  return false;
}

async function main() {
  const argv = process.argv.slice(2);
  const arg = (name, def) => {
    const i = argv.indexOf(name);
    return i >= 0 && argv[i + 1] ? argv[i + 1] : def;
  };
  const bvid = arg("--bvid", TARGET_BVID);
  let message = arg("--message", null);
  if (!bvid) {
    console.log("[×] 缺少 --bvid（目标视频 BV 号，例如 --bvid BV1xx411c7mD）");
    return 2;
  }

  console.log("=".repeat(60));
  console.log("B站评论发送测试 (Node.js)");
  console.log("=".repeat(60));

  if (!(await checkLogin())) return 1;
  const aid = await getAid(bvid);

  if (!message) {
    if (argv.includes("--deepseek")) {
      try {
        message = await deepseekComment();
        if (message) console.log("[i] DeepSeek 生成：" + message);
      } catch (e) {
        console.log("[!] DeepSeek 调用失败：" + e.message);
      }
    }
    if (!message) message = `测试评论：来自 Node 脚本，${Date.now()}`;
  }

  console.log("[i] 将发送：" + message);
  const rpid = await postComment(aid, message, bvid);
  if (!rpid) return 1;
  await verify(aid, rpid);
  console.log("-".repeat(60));
  console.log(`完成。https://www.bilibili.com/video/${bvid}/#reply${rpid}`);
  return 0;
}

main()
  .then((code) => process.exit(code))
  .catch((e) => {
    console.error("[×] 异常：", e.message);
    process.exit(1);
  });
