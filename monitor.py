# -*- coding: utf-8 -*-
"""
上汽大众任务雷达 · 云端版
========================================================
和本地版的区别：所有配置走「环境变量」，运行状态存在仓库里的 state.json。
配合 .github/workflows/monitor.yml，每 5 分钟自动跑一次，不需要你开电脑。

两个数据通道：
  A. 社区话题活动（免登录）  GET /community-api/search/activity
  B. V Star达人圈·热门任务   GET /community-api/koc/koc-task/list   ← 需要 KOC_TOKEN

环境变量：
  SERVERCHAN_SENDKEY   Server酱 SendKey（推荐，微信接收）
  PUSHPLUS_TOKEN       PushPlus token
  WECOM_WEBHOOK        企业微信群机器人 webhook
  KOC_TOKEN            达人圈 Access Token（不填则只跑通道 A）
  KEYWORDS             关键词白名单，逗号分隔，留空=全都推
  EXCLUDE_KEYWORDS     关键词黑名单，逗号分隔
  SOURCES              通道开关，如 community_activity,koc_task（默认两个都开）

仅用 Python 标准库。
"""

import hashlib
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(BASE_DIR, "state.json")

API_BASE = "https://m.svw-volkswagen.com/community-api"
APP_KEY = "SVW_COMMUNITY_H5"
TOPIC_URL = "https://m.svw-volkswagen.com/community/topic?topicId={}"
KOC_TASK_URL = "https://m.svw-volkswagen.com/community/koc/taskDetail?taskId={}"

CST = timezone(timedelta(hours=8))
HEARTBEAT_HOURS = 20  # 至少每 20 小时写一次 state，避免仓库长期无提交导致定时任务被停用

def log(msg):
    print("[{}] {}".format(datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)

def env(name, default=""):
    v = (os.environ.get(name) or "").strip()
    return v if v else default

# --------------------------------------------------------------------------
# 接口签名
# --------------------------------------------------------------------------
def _sign_source(params):
    parts = []
    for k in sorted(params.keys()):
        v = params[k]
        if v is None:
            continue
        enc = urllib.parse.quote(str(v), safe="") if v else v
        parts.append("{}={}".format(k, enc))
    r = "&".join(parts)
    if params:
        r += "&"
    for a, b in (("%20", "+"), ("'", "%27"), ("!", "%21"),
                 ("~", "%7E"), ("(", "%28"), (")", "%29")):
        r = r.replace(a, b)
    return r

def api_get(path, params=None, token=None):
    params = params or {}
    ts = int(time.time() * 1000)
    nonce = str(random.random())[2:]
    sign = hashlib.md5(
        (_sign_source(params) + str(ts) + nonce + APP_KEY).encode("utf-8")).hexdigest()
    url = API_BASE + path + (("?" + urllib.parse.urlencode(params)) if params else "")
    req = urllib.request.Request(url, method="GET")
    headers = {
        "X-App-Key": APP_KEY,
        "X-Nonce": nonce,
        "X-Timestamp": str(ts),
        "X-Sign": sign,
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://m.svw-volkswagen.com/community/",
        "Accept": "application/json, text/plain, */*",
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    }
    if token:
        headers["X-COP-accessToken"] = token
    for k, v in headers.items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore"))

def _strip_html(s):
    out, inside = [], False
    for ch in s:
        if ch == "<":
            inside = True
        elif ch == ">":
            inside = False
        elif not inside:
            out.append(ch)
    return " ".join("".join(out).split())

def _fmt_ts(ms):
    try:
        return datetime.fromtimestamp(int(ms) / 1000, CST).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return None

# --------------------------------------------------------------------------
# 采集
# --------------------------------------------------------------------------
def fetch_community():
    data = api_get("/search/activity", {"keyword": "", "pageNum": 1, "pageSize": 10})
    items, seen = [], set()
    for r in (data.get("rows") or []):
        aid = str(r.get("actId") or r.get("topicId") or "")
        if not aid or aid in seen:
            continue
        seen.add(aid)
        b, e = _fmt_ts(r.get("actBeginTime")), _fmt_ts(r.get("actEndTime"))
        period = ("活动时间：{} ~ {}".format(b, e) if b and e
                  else ("开始时间：{}".format(b) if b else ""))
        items.append({
            "uid": "community:" + aid,
            "source": "社区话题活动",
            "title": (r.get("actTitle") or "").strip() or "（无标题）",
            "desc": (r.get("topicIntro") or "").strip()[:300],
            "period": period,
            "link": TOPIC_URL.format(r.get("topicId") or ""),
        })
    items.sort(key=lambda x: x["uid"], reverse=True)
    return items

def fetch_koc(token):
    try:
        data = api_get("/koc/koc-task/list", None, token=token)
    except urllib.error.HTTPError as e:
        return None, ("auth" if e.code in (401, 403) else "HTTP {}".format(e.code))
    except Exception as e:
        return None, str(e)

    rows = data.get("data")
    if isinstance(rows, dict):
        rows = rows.get("list") or rows.get("records") or rows.get("rows") or []
    if not isinstance(rows, list):
        rows = []

    items, seen = [], set()
    for r in rows:
        if not isinstance(r, dict):
            continue
        tid = str(r.get("id") or r.get("taskId") or r.get("kocTaskId") or "")
        if not tid or tid in seen:
            continue
        seen.add(tid)
        title = (r.get("taskName") or r.get("name") or r.get("title") or "").strip()
        extra = []
        if r.get("submitCountLimitTask"):
            extra.append("限提交 {} 篇".format(r["submitCountLimitTask"]))
        if str(r.get("isUseCrowd") or "0") == "1":
            extra.append("仅对指定人群开放")
        items.append({
            "uid": "koc:" + tid,
            "source": "V Star达人圈·热门任务",
            "title": title or "（任务 {}）".format(tid),
            "desc": _strip_html(r.get("taskDesc") or r.get("taskDetail") or "")[:300],
            "period": " · ".join(extra),
            "link": KOC_TASK_URL.format(tid),
        })
    return items, None

# --------------------------------------------------------------------------
# 推送
# --------------------------------------------------------------------------
def _post_json(url, payload, timeout=20):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json;charset=utf-8")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "ignore")

def notify(title, content):
    results, delivered = [], False

    key = env("SERVERCHAN_SENDKEY")
    if key:
        try:
            body = urllib.parse.urlencode({"title": title, "desp": content}).encode()
            req = urllib.request.Request("https://sctapi.ftqq.com/{}.send".format(key),
                                         data=body, method="POST")
            with urllib.request.urlopen(req, timeout=20) as resp:
                results.append("Server酱: OK " + resp.read().decode("utf-8", "ignore")[:60])
            delivered = True
        except Exception as e:
            results.append("Server酱: 失败 {}".format(e))

    tok = env("PUSHPLUS_TOKEN")
    if tok:
        try:
            r = _post_json("https://www.pushplus.plus/send", {
                "token": tok, "title": title,
                "content": content.replace("\n", "<br/>"), "template": "html"})
            results.append("PushPlus: OK " + str(r)[:60])
            delivered = True
        except Exception as e:
            results.append("PushPlus: 失败 {}".format(e))

    hook = env("WECOM_WEBHOOK")
    if hook:
        try:
            r = _post_json(hook, {"msgtype": "markdown",
                                  "markdown": {"content": "**{}**\n{}".format(title, content)}})
            results.append("企业微信: OK " + str(r)[:60])
            delivered = True
        except Exception as e:
            results.append("企业微信: 失败 {}".format(e))

    for r in results:
        log("  推送 → " + r)
    if not results:
        log("✗ 没有配置任何推送渠道！请在仓库 Settings → Secrets 里添加 SERVERCHAN_SENDKEY")
    return delivered, bool(results)

# --------------------------------------------------------------------------
# 状态
# --------------------------------------------------------------------------
def load_state():
    if not os.path.exists(STATE_PATH):
        return {"seen": {}, "last_check": None, "last_heartbeat": None, "reminders": {}}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            s = json.load(f)
    except Exception:
        s = {}
    fixed = {}
    for k, v in (s.get("seen") or {}).items():
        fixed[k if ":" in k else "community:" + k] = v
    s["seen"] = fixed
    s.setdefault("reminders", {})
    s.setdefault("last_heartbeat", None)
    return s

def save_state(s):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1, sort_keys=True)

def matches(item, inc, exc):
    text = "{} {}".format(item.get("title") or "", item.get("desc") or "")
    if inc and not any(k in text for k in inc):
        return False
    if exc and any(k in text for k in exc):
        return False
    return True

def main():
    inc = [k.strip() for k in env("KEYWORDS").split(",") if k.strip()]
    exc = [k.strip() for k in env("EXCLUDE_KEYWORDS").split(",") if k.strip()]
    srcs = [s.strip() for s in env("SOURCES", "community_activity,koc_task").split(",") if s.strip()]

    state = load_state()
    seen = state["seen"]
    first_run = (len(seen) == 0)

    items, warns = [], []

    if "community_activity" in srcs:
        try:
            a = fetch_community()
            items += a
            log("通道A 社区话题活动：{} 条".format(len(a)))
        except Exception as e:
            warns.append("社区活动接口异常：{}".format(e))
            log("通道A 抓取失败：{}".format(e))

    if "koc_task" in srcs:
        token = env("KOC_TOKEN")
        if not token:
            log("通道B 达人圈：未配置 KOC_TOKEN，已跳过")
        else:
            k, err = fetch_koc(token)
            if err == "auth":
                warns.append("koc_token_expired")
                log("通道B 达人圈：token 已失效（HTTP 403）")
            elif err:
                warns.append("达人圈接口异常：{}".format(err))
                log("通道B 抓取失败：{}".format(err))
            else:
                items += k
                log("通道B 达人圈热门任务：{} 条".format(len(k)))

    new_items = [it for it in items if it["uid"] not in seen]

    if first_run:
        for it in items:
            seen[it["uid"]] = {"title": it["title"], "source": it["source"]}
        log("首次运行：已把 {} 条现有任务记为基线，从现在开始只推送新任务。".format(len(items)))
        notify("✅ 上汽大众任务雷达已上线",
               "云端监控已启动，每 5 分钟检查一次。\n"
               "当前已有 {} 条任务记为基线，之后一有新任务就会立刻推给你。".format(len(items)))
    elif not new_items:
        log("没有新任务。")
    else:
        log("发现 {} 条新任务！".format(len(new_items)))
        for it in new_items:
            seen[it["uid"]] = {"title": it["title"], "source": it["source"],
                               "notified_at": datetime.now(CST).isoformat()}
            lines = ["类型：{}".format(it["source"]), "任务：{}".format(it["title"])]
            if it.get("period"):
                lines.append(it["period"])
            if it.get("desc"):
                lines.append(it["desc"])
            lines.append("打开：{}".format(it["link"]))
            body = "\n".join(lines)
            log("--- 新任务 ---\n" + body)
            if matches(it, inc, exc):
                notify("【{}】{}".format(it["source"], it["title"][:36]), body)
            else:
                log("  （被关键词规则过滤，不推送）")

    if "koc_token_expired" in warns:
        last = state["reminders"].get("koc_token_expired")
        now = datetime.now(CST)
        if not last or (now - datetime.fromisoformat(last)).total_seconds() > 86400:
            notify("达人圈 Token 已失效，请更新",
                   "云端监控暂时读不到 V Star 达人圈的热门任务。\n"
                   "请重新获取 token，到仓库 Settings → Secrets 更新 KOC_TOKEN。\n"
                   "（社区话题活动通道不受影响）")
            state["reminders"]["koc_token_expired"] = now.isoformat()

    now = datetime.now(CST)
    hb = state.get("last_heartbeat")
    if not hb or (now - datetime.fromisoformat(hb)).total_seconds() > HEARTBEAT_HOURS * 3600:
        state["last_heartbeat"] = now.isoformat()

    state["last_check"] = now.isoformat()
    state["seen"] = seen
    save_state(state)
    log("完成。已记录任务总数：{}".format(len(seen)))
    return 0

if __name__ == "__&#8203;main__":
    sys.exit(main())
