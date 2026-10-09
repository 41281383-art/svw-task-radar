# -*- coding: utf-8 -*-
"""
上汽大众超级App 任务监控提醒（双通道）
========================================================
通道 A｜社区话题活动（免登录，开箱即用）
    GET https://m.svw-volkswagen.com/community-api/search/activity
    覆盖社区「话题活动 / 征文活动」，最新在最前。

通道 B｜V Star 达人圈 - 热门任务（精准，但需要你的登录凭证）
    GET https://m.svw-volkswagen.com/community-api/koc/koc-task/list
    需要请求头 X-COP-accessToken（即网页版登录后的 localStorage.ACCESS_TOKEN）。
    获取方式见 README「获取达人圈 Token」。Token 过期后脚本会提醒你更新。

发现新任务 → 去重 → 推送微信 / 企业微信 / 邮件 / 桌面通知。

用法：
  python monitor.py                 循环运行（默认 10 分钟一次）
  python monitor.py --once          只检查一次（配合计划任务）
  python monitor.py --baseline      把当前已有任务标记为已读，不推送
  python monitor.py --test          发一条测试推送
  python monitor.py --koc-raw       打印达人圈任务接口的原始返回（排查用）

仅使用 Python 标准库，无需安装依赖。
"""

import argparse
import hashlib
import json
import os
import random
import smtplib
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
STATE_PATH = os.path.join(BASE_DIR, "state.json")
LOG_PATH = os.path.join(BASE_DIR, "monitor.log")
TOKEN_PATH = os.path.join(BASE_DIR, "koc_token.txt")

API_BASE = "https://m.svw-volkswagen.com/community-api"
APP_KEY = "SVW_COMMUNITY_H5"
TOPIC_URL = "https://m.svw-volkswagen.com/community/topic?topicId={}"
KOC_TASK_URL = "https://m.svw-volkswagen.com/community/koc/taskDetail?taskId={}"

CST = timezone(timedelta(hours=8))


# --------------------------------------------------------------------------
# 日志
# --------------------------------------------------------------------------
def log(msg):
    line = "[{}] {}".format(datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# --------------------------------------------------------------------------
# 接口签名：X-Sign = md5(签名串 + 时间戳ms + 随机数 + "SVW_COMMUNITY_H5")
#   GET 的签名串 = 按 key 升序的 k=v（value 做 encodeURIComponent）拼接 + "&"
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


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------
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


def _period(begin, end):
    b, e = _fmt_ts(begin), _fmt_ts(end)
    if b and e:
        return "活动时间：{} ~ {}".format(b, e)
    if b:
        return "开始时间：{}".format(b)
    return ""


# --------------------------------------------------------------------------
# 通道 A：社区话题活动（免登录）
# --------------------------------------------------------------------------
def fetch_community():
    data = api_get("/search/activity", {"keyword": "", "pageNum": 1, "pageSize": 10})
    rows = data.get("rows") or []
    items, seen = [], set()
    for r in rows:
        aid = str(r.get("actId") or r.get("topicId") or "")
        if not aid or aid in seen:
            continue
        seen.add(aid)
        items.append({
            "uid": "community:" + aid,
            "source": "社区话题活动",
            "title": (r.get("actTitle") or "").strip() or "（无标题）",
            "desc": (r.get("topicIntro") or "").strip()[:300],
            "period": _period(r.get("actBeginTime"), r.get("actEndTime")),
            "link": TOPIC_URL.format(r.get("topicId") or ""),
        })
    items.sort(key=lambda x: x["uid"], reverse=True)
    return items


# --------------------------------------------------------------------------
# 通道 B：V Star 达人圈 - 热门任务（需要登录 token）
# --------------------------------------------------------------------------
def _clean_token(raw):
    """从一个可能含说明文字的文件/字段里提取真正的 token。

    规则：逐行看，跳过 # 开头的注释；取第一行「不含空白字符且长度 >= 20」的内容。
    这样 koc_token.txt 里保留说明文字也不会被误当成 token。
    """
    for line in (raw or "").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if len(s) >= 20 and " " not in s and "\t" not in s:
            return s
    return ""


def load_koc_token():
    if os.path.exists(TOKEN_PATH):
        try:
            t = _clean_token(open(TOKEN_PATH, encoding="utf-8").read())
            if t:
                return t
        except Exception:
            pass
    return _clean_token(CFG_CACHE.get("koc_access_token") or "")


def fetch_koc(token):
    """返回 (items, error)；error 为 'auth' / 其它字符串 / None"""
    try:
        data = api_get("/koc/koc-task/list", None, token=token)
    except urllib.error.HTTPError as e:
        return None, "auth" if e.code in (401, 403) else "HTTP {}".format(e.code)
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
        desc = _strip_html((r.get("taskDesc") or r.get("taskDetail") or ""))[:300]
        extra = []
        if r.get("submitCountLimitTask"):
            extra.append("限提交 {} 篇".format(r["submitCountLimitTask"]))
        if str(r.get("isUseCrowd") or "0") == "1":
            extra.append("仅对指定人群开放")
        items.append({
            "uid": "koc:" + tid,
            "source": "V Star达人圈·热门任务",
            "title": title or "（任务 {}）".format(tid),
            "desc": desc,
            "period": " · ".join(extra),
            "link": KOC_TASK_URL.format(tid),
        })
    return items, None


# --------------------------------------------------------------------------
# 推送渠道
# --------------------------------------------------------------------------
def _post_json(url, payload, timeout=20):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json;charset=utf-8")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "ignore")


def push_serverchan(cfg, title, content):
    body = urllib.parse.urlencode({"title": title, "desp": content}).encode()
    req = urllib.request.Request(
        "https://sctapi.ftqq.com/{}.send".format(cfg["sendkey"].strip()),
        data=body, method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", "ignore")


def push_pushplus(cfg, title, content):
    return _post_json("https://www.pushplus.plus/send", {
        "token": cfg["token"].strip(), "title": title,
        "content": content.replace("\n", "<br/>"), "template": "html"})


def push_wecom(cfg, title, content):
    return _post_json(cfg["url"].strip(), {
        "msgtype": "markdown",
        "markdown": {"content": "**{}**\n{}".format(title, content)}})


def push_email(cfg, title, content):
    msg = MIMEText(content, "plain", "utf-8")
    msg["Subject"] = Header(title, "utf-8")
    msg["From"] = formataddr(("上汽大众任务监控", cfg["username"]))
    msg["To"] = cfg["to"]
    port = int(cfg.get("smtp_port") or 465)
    if port == 465:
        server = smtplib.SMTP_SSL(cfg["smtp_host"], port,
                                  context=ssl.create_default_context(), timeout=25)
    else:
        server = smtplib.SMTP(cfg["smtp_host"], port, timeout=25)
        server.starttls(context=ssl.create_default_context())
    with server:
        server.login(cfg["username"], cfg["password"])
        server.sendmail(cfg["username"], [x.strip() for x in cfg["to"].split(",")],
                        msg.as_string())
    return "sent"


def push_toast(title, content):
    if sys.platform != "win32":
        return "skip"
    ps = (
        "[void][Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime];"
        "$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
        "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
        "$n=$t.GetElementsByTagName('text');"
        "$n.Item(0).AppendChild($t.CreateTextNode('{t}'))|Out-Null;"
        "$n.Item(1).AppendChild($t.CreateTextNode('{c}'))|Out-Null;"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('上汽大众任务监控')"
        ".Show([Windows.UI.Notifications.ToastNotification]::new($t))"
    ).format(t=title.replace("'", "''")[:100], c=content.replace("'", "''")[:350])
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, timeout=25)
        return "shown"
    except Exception as e:
        return "failed: {}".format(e)


def notify(cfg, title, content):
    push = cfg.get("push") or {}
    results, delivered = [], False
    for name, fn in (("serverchan", push_serverchan), ("pushplus", push_pushplus),
                     ("wecom_webhook", push_wecom), ("email", push_email)):
        c = push.get(name) or {}
        if not c.get("enabled"):
            continue
        try:
            r = fn(c, title, content)
            results.append("{}: OK {}".format(name, str(r)[:70]))
            delivered = True
        except Exception as e:
            results.append("{}: 失败 {}".format(name, e))
    if (push.get("desktop_toast") or {}).get("enabled"):
        r = push_toast(title, content)
        results.append("desktop_toast: " + r)
        delivered = delivered or ("shown" in r)
    for r in results:
        log("  推送 → " + r)
    if not delivered:
        log("⚠ 没有任何推送真正送达：请检查 config.json 是否开启了至少一个渠道。")
    return delivered


# --------------------------------------------------------------------------
# 状态 / 配置
# --------------------------------------------------------------------------
def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


DEFAULT_CONFIG = {
    "interval_minutes": 10,
    "_说明": "keywords 为空=所有新任务都推送；填了就只推送标题/简介命中这些词的任务",
    "keywords": [],
    "exclude_keywords": [],
    "sources": {
        "community_activity": True,
        "koc_task": True
    },
    "koc_access_token": "可留空，改为把 token 粘贴到 koc_token.txt",
    "push": {
        "serverchan": {"enabled": False, "sendkey": "Server酱 SendKey（微信接收，推荐）"},
        "pushplus": {"enabled": False, "token": "PushPlus token"},
        "wecom_webhook": {"enabled": False, "url": "企业微信群机器人 webhook"},
        "email": {"enabled": False, "smtp_host": "smtp.qq.com", "smtp_port": 465,
                  "username": "you@qq.com", "password": "邮箱授权码", "to": "收件邮箱"},
        "desktop_toast": {"enabled": True}
    }
}

CFG_CACHE = {}


def load_config():
    if not os.path.exists(CONFIG_PATH):
        save_json(CONFIG_PATH, DEFAULT_CONFIG)
    cfg = load_json(CONFIG_PATH, dict(DEFAULT_CONFIG))
    for k, v in DEFAULT_CONFIG.items():
        cfg.setdefault(k, v)
    for k, v in DEFAULT_CONFIG["sources"].items():
        cfg["sources"].setdefault(k, v)
    CFG_CACHE.clear()
    CFG_CACHE.update(cfg)
    return cfg


def matches_filter(cfg, item):
    text = "{} {}".format(item.get("title") or "", item.get("desc") or "")
    inc = [k for k in (cfg.get("keywords") or []) if k]
    exc = [k for k in (cfg.get("exclude_keywords") or []) if k]
    if inc and not any(k in text for k in inc):
        return False
    if exc and any(k in text for k in exc):
        return False
    return True


def migrate_state(state):
    """兼容旧版 state：没有 source 前缀的 key 视为 community:"""
    fixed = {}
    for k, v in (state.get("seen") or {}).items():
        fixed[k if ":" in k else "community:" + k] = v
    state["seen"] = fixed
    return state


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def collect(cfg):
    items, warns = [], []
    src = cfg.get("sources") or {}

    if src.get("community_activity", True):
        try:
            a = fetch_community()
            items += a
            log("通道A 社区话题活动：{} 条".format(len(a)))
        except Exception as e:
            warns.append("社区活动接口异常：{}".format(e))
            log("通道A 抓取失败：{}".format(e))

    if src.get("koc_task", True):
        token = load_koc_token()
        if not token:
            log("通道B 达人圈：未配置 token，已跳过（见 README 获取 Token）")
        else:
            k, err = fetch_koc(token)
            if err == "auth":
                warns.append("koc_token_expired")
                log("通道B 达人圈：token 已失效（HTTP 403），需要更新 koc_token.txt")
            elif err:
                warns.append("达人圈接口异常：{}".format(err))
                log("通道B 抓取失败：{}".format(err))
            else:
                items += k
                log("通道B 达人圈热门任务：{} 条".format(len(k)))
    return items, warns


def _format_item(it):
    lines = ["类型：{}".format(it["source"]), "任务：{}".format(it["title"])]
    if it.get("period"):
        lines.append(it["period"])
    if it.get("desc"):
        lines.append(it["desc"])
    lines.append("打开：{}".format(it["link"]))
    return "\n".join(lines)


def run_once(cfg, baseline=False):
    state = migrate_state(load_json(STATE_PATH, {"seen": {}}))
    seen = state.get("seen") or {}
    state["reminders"] = state.get("reminders") or {}

    items, warns = collect(cfg)
    new_items = [it for it in items if it["uid"] not in seen]

    if baseline:
        for it in items:
            seen[it["uid"]] = {"title": it["title"], "source": it["source"]}
        log("已建立基线：{} 条任务标记为已读，之后只推送新增任务。".format(len(items)))
    elif not new_items:
        log("没有新任务。")
    else:
        log("发现 {} 条新任务！".format(len(new_items)))
        for it in new_items:
            seen[it["uid"]] = {"title": it["title"], "source": it["source"],
                               "notified_at": datetime.now(CST).isoformat()}
            body = _format_item(it)
            log("--- 新任务 ---\n" + body)
            if matches_filter(cfg, it):
                notify(cfg, "【{}】{}".format(it["source"], it["title"][:36]), body)
            else:
                log("  （被关键词规则过滤，不推送）")

    if "koc_token_expired" in warns:
        last = state["reminders"].get("koc_token_expired")
        now = datetime.now(CST)
        if not last or (now - datetime.fromisoformat(last)).total_seconds() > 86400:
            notify(cfg, "达人圈 Token 已失效，请更新",
                   "监控脚本暂时读不到 V Star 达人圈的热门任务。\n"
                   "请按 README 重新获取 token，粘贴到 koc_token.txt 覆盖保存即可。\n"
                   "（社区话题活动通道不受影响，仍在正常监控）")
            state["reminders"]["koc_token_expired"] = now.isoformat()

    state["seen"] = seen
    state["last_check"] = datetime.now(CST).isoformat()
    save_json(STATE_PATH, state)
    return 0


def main():
    ap = argparse.ArgumentParser(description="上汽大众超级App 任务监控提醒")
    ap.add_argument("--once", action="store_true", help="只检查一次")
    ap.add_argument("--baseline", action="store_true", help="建立基线，不推送历史任务")
    ap.add_argument("--test", action="store_true", help="发送测试推送")
    ap.add_argument("--koc-raw", action="store_true", help="打印达人圈接口原始返回")
    args = ap.parse_args()

    cfg = load_config()

    if args.koc_raw:
        token = load_koc_token()
        if not token:
            print("未配置 koc_token.txt，无法测试。")
            return 1
        try:
            print(json.dumps(api_get("/koc/koc-task/list", None, token=token),
                             ensure_ascii=False, indent=1)[:6000])
        except urllib.error.HTTPError as e:
            print("HTTP {} —— token 可能已失效".format(e.code))
        return 0

    if args.test:
        log("发送测试推送……")
        notify(cfg, "【测试】上汽大众任务监控",
               "收到这条消息说明推送渠道配置成功。\n时间：{}".format(
                   datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")))
        return 0

    if args.baseline:
        return run_once(cfg, baseline=True)
    if args.once:
        return run_once(cfg)

    interval = max(3, int(cfg.get("interval_minutes") or 10))
    log("监控已启动，每 {} 分钟检查一次（Ctrl+C 退出）".format(interval))
    first = True
    while True:
        try:
            if first and not os.path.exists(STATE_PATH):
                log("首次运行，自动建立基线。")
                run_once(cfg, baseline=True)
            else:
                run_once(cfg)
            first = False
        except KeyboardInterrupt:
            log("已手动停止。")
            return 0
        except Exception as e:
            log("本轮检查出错：{}".format(e))
        time.sleep(interval * 60 + random.randint(0, 30))


if __name__ == "__main__":
    sys.exit(main())
