#!/usr/bin/env python3
"""
NeoHeberg VPS 自动续期 + 开机守护

每次运行（GitHub Actions 每日一次）：
  0. 启用固定出口 IP 代理（NODE_LINK，可选；未配置则直连）
  1. 无头浏览器登录面板（每次重新登录，不持久化 cookie）
  2. 检查到期日，剩余天数 <= RENEW_DAYS 则续期（+31 天）
  3. 检查容器状态，已停止则开机
  4. Telegram 通知结果

环境变量：
  NEO_USER       面板用户名
  NEO_PASSWORD   面板密码
  NEO_VMID       实例 ID
  NODE_LINK      节点分享链接（可选，固定出口 IP 防风控）
  PROXY_SERVER   本地代理地址（由 setup_proxy.sh 导出，优先于 NODE_LINK 探测）
  TG_BOT_TOKEN   Telegram bot token（可留空则不通知）
  TG_CHAT_ID     Telegram chat id
  RENEW_DAYS     剩余多少天时续期，默认 10
"""

import os
import re
import sys
import json
import time
import urllib.request
import urllib.parse
import urllib.error
import socks_proxy  # 本地模块：NODE_LINK 代理（可选，未配置则直连）

BASE = "https://dash.neoheberg.fr"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 实例标识走环境变量（公开仓库不硬编码任何账号/服务信息）
USERNAME = os.environ.get("NEO_USER", "")
VMID = os.environ.get("NEO_VMID", "")
TYPE = os.environ.get("NEO_TYPE", "vps")
LABEL = os.environ.get("NEO_LABEL", "vps")

RENEW_DAYS = int(os.environ.get("RENEW_DAYS", "10"))
TRIGGER = os.environ.get("GITHUB_EVENT_NAME", "local")
RUN_URL = ""
if os.environ.get("GITHUB_RUN_ID"):
    RUN_URL = (f"{os.environ.get('GITHUB_SERVER_URL','https://github.com')}/"
               f"{os.environ.get('GITHUB_REPOSITORY','')}/actions/runs/"
               f"{os.environ['GITHUB_RUN_ID']}")

LOG = []


def mask(s, show=2):
    """日志脱敏：只留首尾各 show 位"""
    s = str(s)
    if not s:
        return "(空)"
    if len(s) <= show * 2:
        return "*" * len(s)
    return s[:show] + "*" * (len(s) - show * 2) + s[-show:]


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOG.append(line)


# ───────────────────────── HTTP ─────────────────────────

def request(path, cookie, data=None, ajax=False, timeout=45):
    h = {"User-Agent": UA, "Accept-Language": "fr-FR,fr;q=0.9"}
    if cookie:
        h["Cookie"] = cookie
    if ajax:
        h.update({"Accept": "application/json",
                  "X-Requested-With": "XMLHttpRequest",
                  "Referer": BASE + "/"})
    if data is not None:
        h["Content-Type"] = "application/x-www-form-urlencoded"
        h["Origin"] = BASE
        h["Referer"] = BASE + "/"
    req = urllib.request.Request(BASE + path, data=data, headers=h,
                                 method="POST" if data is not None else "GET")
    # 走代理 opener（未启用代理时即默认 opener，行为不变）
    return socks_proxy.get_opener().open(req, timeout=timeout)


def get_html(path, cookie):
    return request(path, cookie).read().decode("utf-8", "ignore")


def get_json(path, cookie):
    return json.loads(request(path, cookie, ajax=True).read().decode())


# ───────────────────────── 面板操作 ─────────────────────────

def panel_csrf(cookie):
    dash = get_html("/", cookie)
    m = re.search(r'CSRF\s*=\s*["\']([^"\']+)', dash)
    if not m:
        m = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', dash)
    return (m.group(1) if m else None), dash


def parse_expiry(html):
    """返回 (到期日字符串, 剩余天数 或 None)"""
    e = re.search(r'Échéance le\s*([0-9]{2}/[0-9]{2}/[0-9]{4})', html)
    j = re.search(r'(\d+)\s*jours?\s*restants?', html)
    return (e.group(1) if e else None), (int(j.group(1)) if j else None)


def session_ok(cookie):
    """cookie 是否仍然有效"""
    try:
        dash = get_html("/", cookie)
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, type(e).__name__
    if 'name="identifier"' in dash or "Connexion" in dash[:2000]:
        return False, "session 已过期（返回登录页）"
    if "Tableau de bord" not in dash and "Mes services" not in dash:
        return False, "未识别为面板页面"
    return True, "ok"


def renew(cookie, csrf):
    body = urllib.parse.urlencode(
        {"csrf_token": csrf, "type": TYPE, "id": VMID}).encode()
    r = request("/services/renew", cookie, data=body, timeout=120)
    html = r.read().decode("utf-8", "ignore")
    m = re.search(r'"message"\s*:\s*"([^"]*)"', html)
    msg = m.group(1).encode().decode("unicode_escape") if m else ""
    ok = "renouvel" in msg.lower()
    return ok, msg or f"HTTP {r.status}"


def power(cookie, csrf, signal):
    body = urllib.parse.urlencode(
        {"vmid": VMID, "signal": signal, "csrf_token": csrf}).encode()
    r = request("/app/services/vps-power.php", cookie, data=body, ajax=True, timeout=90)
    return json.loads(r.read().decode())


def vps_status(cookie):
    return get_json(f"/app/services/vps-stats.php?id={VMID}", cookie)


# ───────────────────────── cookie 刷新 ─────────────────────────
#
# ★ 2026-10 站点改版导致登录页 GET 返回【残缺 HTML】（无 </html>、无
#   <form>/<input>，稳定停在同一长度），表单元素根本不存在 →
#   page.fill 超时。这是服务端问题，脚本只能重试等它恢复。
#   同时确认：① 登录是两步流程（identifier → #goToPassword → password）
#            ② POST /login 需要 csrf_token
#            ③ /login 有速率限制，连续请求返回 429
#   因此这里加：重试 + 指数退避 + 429 识别 + CSRF 注入 + 诊断输出。

LOGIN_ATTEMPTS = int(os.environ.get("NEO_LOGIN_ATTEMPTS", "4"))
LOGIN_BACKOFF = [int(x) for x in
                 os.environ.get("NEO_LOGIN_BACKOFF", "15,45,120,300").split(",")]


def _backoff(attempt):
    """第 attempt 次失败（1 起）后应等待的秒数"""
    i = min(attempt - 1, len(LOGIN_BACKOFF) - 1)
    return LOGIN_BACKOFF[i] if LOGIN_BACKOFF else 30


def page_diag(page):
    """登录失败时抓页面状态，便于判断是站点异常还是选择器问题"""
    d = {}
    try:
        html = page.content()
        d["url"] = page.url
        d["title"] = page.title()[:60]
        d["html_len"] = len(html)
        d["html_closed"] = "</html>" in html
        d["has_identifier"] = 'name="identifier"' in html
        d["has_password"] = 'name="password"' in html
        d["inputs"] = page.evaluate("() => document.querySelectorAll('input').length")
        d["cap_widget"] = page.evaluate("() => document.querySelectorAll('cap-widget').length")
        d["rate_limited"] = "Trop de requ" in html
    except Exception as e:
        d["error"] = f"{type(e).__name__}: {str(e)[:80]}"
    return d


def diag_str(d):
    if not d:
        return "(无)"
    if "error" in d and len(d) == 1:
        return d["error"]
    parts = [f"{k}={d[k]}" for k in ("url", "html_len", "inputs", "cap_widget",
                                     "html_closed", "has_identifier") if k in d]
    return " ".join(parts)


def read_csrf(page):
    """从页面读 csrf_token（隐藏 input / meta / JS 变量都试）"""
    try:
        return page.evaluate("""() => {
            const i = document.querySelector('input[name="csrf_token"]');
            if (i && i.value) return i.value;
            const m = document.querySelector('meta[name="csrf-token"]');
            if (m && m.content) return m.content;
            if (window.CSRF_TOKEN) return window.CSRF_TOKEN;
            if (typeof window.CSRF === 'string') return window.CSRF;
            return '';
        }""") or ""
    except Exception:
        return ""


def refresh_cookie():
    """无头浏览器登录，返回新的 Cookie 头字符串（含重试/退避/CSRF）"""
    pw = os.environ.get("NEO_PASSWORD")
    if not pw:
        return None, "缺少 NEO_PASSWORD，无法自动刷新 cookie"

    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        return None, f"playwright 不可用: {type(e).__name__}"

    last_msg = "未尝试"
    last_diag = {}

    for attempt in range(1, LOGIN_ATTEMPTS + 1):
        if attempt > 1:
            wait = _backoff(attempt - 1)
            log(f"  第 {attempt}/{LOGIN_ATTEMPTS} 次登录尝试（等待 {wait}s）…")
            time.sleep(wait)
        else:
            log(f"  第 {attempt}/{LOGIN_ATTEMPTS} 次登录尝试…")

        cand, msg, diag = _login_once(sync_playwright, pw)
        last_msg, last_diag = msg, diag

        if cand and "__Host-NH-Remember" in cand:
            if attempt > 1:
                log(f"  ✅ 第 {attempt} 次尝试成功")
            hdr = "; ".join(f"{k}={v}" for k, v in cand.items())
            return hdr, "ok"

        log(f"  ✗ 尝试 {attempt} 失败: {msg}")
        if diag:
            log(f"    页面状态: {diag_str(diag)}")
        # ★ 站点页面残缺 / 表单缺失 → 只能等它恢复，继续重试
        if diag.get("rate_limited"):
            log("    站点返回 429（速率限制），拉长退避")
            time.sleep(30)

    hint = ""
    if last_diag and not last_diag.get("has_identifier", True):
        hint = ("（登录页表单缺失 —— 站点 GET /login 返回残缺 HTML，"
                "非本脚本问题；等站点恢复）")
    return None, f"{LOGIN_ATTEMPTS} 次尝试均失败，最后错误: {last_msg}{hint}"


def _login_once(sync_playwright, pw):
    """单次登录尝试。返回 (cookie_dict 或 None, 消息, 诊断 dict)"""
    cand = {}
    diag = {}
    launch_errors = []
    px = socks_proxy.playwright_proxy()
    with sync_playwright() as p:
        browser = None
        for kwargs in ({"channel": "chrome"}, {}):
            try:
                launch_kw = dict(headless=True,
                                 args=["--disable-blink-features=AutomationControlled",
                                       "--no-sandbox", "--disable-dev-shm-usage"],
                                 **kwargs)
                if px:
                    launch_kw["proxy"] = px
                browser = p.chromium.launch(**launch_kw)
                log(f"浏览器: {kwargs.get('channel') or 'bundled chromium'}"
                    f"{'（经代理）' if px else ''}")
                break
            except Exception as e:
                launch_errors.append(f"{kwargs.get('channel') or 'chromium'}: "
                                     f"{type(e).__name__}")
                browser = None
        if browser is None:
            return None, f"浏览器启动失败 ({', '.join(launch_errors)})", {}

        ctx = browser.new_context(
            user_agent=UA, viewport={"width": 1920, "height": 1080}, locale="fr-FR")
        page = ctx.new_page()
        try:
            page.goto(BASE + "/login", timeout=60000, wait_until="domcontentloaded")

            # ★ 先等表单出现 —— 站点残缺 HTML 时这里就会失败（不再盲填）
            try:
                page.wait_for_selector('input[name="identifier"]', timeout=45000)
            except Exception:
                diag = page_diag(page)
                return None, "登录表单未渲染（input[name=identifier] 不存在）", diag

            page.fill('input[name="identifier"]', USERNAME)
            page.click("#goToPassword")
            page.wait_for_timeout(1500)

            # 第二步：密码框
            try:
                page.wait_for_selector('input[name="password"]', timeout=25000)
            except Exception:
                diag = page_diag(page)
                return None, "第二步密码框未出现（#goToPassword 跳转失败）", diag

            page.evaluate("() => { const w=document.getElementById('cap-login'); if (w) w.solve(); }")

            token = ""
            for _ in range(30):
                page.wait_for_timeout(2000)
                token = page.evaluate(
                    "() => { const e=document.querySelector('input[name=\"cap-token\"]');"
                    " return e ? e.value : ''; }")
                if token and len(token) > 20:
                    break
            if not token:
                diag = page_diag(page)
                return None, "验证码求解超时（cap-token 未生成）", diag

            page.fill('input[name="password"]', pw)

            # ★ POST /login 需要 csrf_token；表单里没有就注入
            csrf = read_csrf(page)
            if csrf:
                log(f"  csrf_token: 已取到（{len(csrf)} 字符）")
            else:
                log("  csrf_token: 页面未提供（尝试直接提交）")
            page.evaluate("""(csrf) => {
                const f = document.querySelector('input[name="password"]').form;
                if (csrf && !f.querySelector('input[name="csrf_token"]')) {
                    const h = document.createElement('input');
                    h.type='hidden'; h.name='csrf_token'; h.value=csrf;
                    f.appendChild(h);
                }
                f.submit();
            }""", csrf)

            try:
                page.wait_for_url(re.compile(r"dash\.neoheberg\.fr/(\?.*)?$"),
                                  timeout=45000)
            except Exception:
                pass
            page.wait_for_timeout(2000)

            for c in ctx.cookies():
                if c["name"].startswith("__Host-NH"):
                    cand[c["name"]] = c["value"]

            if "__Host-NH-Remember" not in cand:
                diag = page_diag(page)
                # 站点是否给了明确的错误提示
                try:
                    err = page.evaluate("""() => {
                        const a = document.querySelector('[role=alert], .alert-card');
                        return a ? a.innerText.trim().slice(0,160) : '';
                    }""")
                    if err:
                        diag["site_error"] = re.sub(r"\s+", " ", err)
                except Exception:
                    pass
                return None, f"未取得 remember cookie（拿到 {list(cand)}）", diag
        finally:
            browser.close()

    return cand, "ok", diag



def rotate_secret(new_cookie):
    """【已停用】原用 GH_PAT 写回 NEO_COOKIE secret。
    现改为每次运行都重新无头登录，不再持久化 cookie，本函数保留仅为兼容旧调用。"""
    return False, "已停用（改为每次运行重新登录）"


# ───────────────────────── Telegram ─────────────────────────

def notify(text):
    tok = os.environ.get("TG_BOT_TOKEN")
    chat = os.environ.get("TG_CHAT_ID")
    if not tok or not chat:
        log("TG 未配置，跳过通知")
        return
    try:
        data = urllib.parse.urlencode(
            {"chat_id": chat, "text": text, "disable_web_page_preview": "true"}).encode()
        r = urllib.request.Request(
            f"https://api.telegram.org/bot{tok}/sendMessage", data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
        resp = json.loads(urllib.request.urlopen(r, timeout=30).read().decode())
        log(f"TG 通知: {'ok' if resp.get('ok') else resp}")
    except Exception as e:
        log(f"TG 通知失败: {type(e).__name__}: {str(e)[:120]}")


# ───────────────────────── 主流程 ─────────────────────────

def selftest():
    """自检：验证无头浏览器能启动并解出 cap-token（不做登录，不动 cookie）"""
    log("── 自检模式：测试无头浏览器 + Cap.js 求解 ──")
    # 代理与主流程保持一致：先 init，再取 playwright proxy
    try:
        socks_proxy.init()
        log(f"代理: {socks_proxy.describe()}")
    except Exception as e:
        log(f"代理初始化异常: {type(e).__name__}: {str(e)[:120]}")
    from playwright.sync_api import sync_playwright
    t0 = time.time()
    px = socks_proxy.playwright_proxy()
    try:
        with sync_playwright() as p:
            browser = None
            for kwargs in ({"channel": "chrome"}, {}):
                try:
                    lk = dict(headless=True,
                              args=["--disable-blink-features=AutomationControlled",
                                    "--no-sandbox", "--disable-dev-shm-usage"], **kwargs)
                    if px:
                        lk["proxy"] = px
                    browser = p.chromium.launch(**lk)
                    log(f"浏览器启动: {kwargs.get('channel') or 'bundled chromium'} "
                        f"({time.time()-t0:.1f}s){'（经代理）' if px else ''}")
                    break
                except Exception as e:
                    log(f"  {kwargs.get('channel') or 'chromium'} 启动失败: {type(e).__name__}")
                    browser = None
            if browser is None:
                raise RuntimeError("无可用浏览器")

            ctx = browser.new_context(user_agent=UA, viewport={"width": 1920, "height": 1080},
                                      locale="fr-FR")
            page = ctx.new_page()
            page.goto(BASE + "/login", timeout=60000, wait_until="domcontentloaded")

            # ★ 与主流程一致：先等表单渲染，再判断站点是否正常
            try:
                page.wait_for_selector('input[name="identifier"]', timeout=45000)
            except Exception:
                d = page_diag(page)
                log(f"❌ 登录表单未渲染 —— {diag_str(d)}")
                log("   若 html_closed=False / inputs=0：站点 GET /login 返回残缺 HTML（服务端问题）")
                browser.close()
                return 1

            page.fill('input[name="identifier"]', USERNAME)
            page.click("#goToPassword")
            page.wait_for_timeout(1500)
            try:
                page.wait_for_selector('input[name="password"]', timeout=25000)
            except Exception:
                log("❌ 第二步密码框未出现（#goToPassword 跳转失败）")
                browser.close()
                return 1

            page.evaluate("() => { const w=document.getElementById('cap-login'); if (w) w.solve(); }")
            token = ""
            for _ in range(30):
                page.wait_for_timeout(2000)
                token = page.evaluate(
                    "() => { const e=document.querySelector('input[name=\"cap-token\"]');"
                    " return e ? e.value : ''; }")
                if token and len(token) > 20:
                    break
            csrf = read_csrf(page)
            log(f"csrf_token: {'取到 ' + str(len(csrf)) + ' 字符' if csrf else '页面未提供'}")
            browser.close()

        if token and len(token) > 20:
            log(f"✅ 自检通过: cap-token 长度 {len(token)}（耗时 {time.time()-t0:.1f}s）")
            return 0
        log("❌ 自检失败: 未取到 cap-token")
        return 1
    except Exception as e:
        log(f"❌ 自检异常: {type(e).__name__}: {str(e)[:250]}")
        return 1


def main():
    if "--selftest" in sys.argv:
        return selftest()

    actions = []
    missing = [k for k, v in (("NEO_USER", USERNAME), ("NEO_VMID", VMID),
                              ("NEO_PASSWORD", os.environ.get("NEO_PASSWORD"))) if not v]
    if missing:
        log(f"缺少必要配置: {', '.join(missing)}")
        notify(f"❌ NeoHeberg: 缺少配置 {', '.join(missing)}")
        return 1

    log(f"配置: 账号={mask(USERNAME)} 实例={mask(VMID)} 续期阈值={RENEW_DAYS}天")

    # 0. 启用代理（固定出口 IP，降低风控；未配置则直连）
    try:
        socks_proxy.init()
        log(f"代理: {socks_proxy.describe()}")
    except Exception as e:
        log(f"代理初始化异常: {type(e).__name__}: {str(e)[:120]}")
        notify(f"❌ NeoHeberg: 代理初始化失败 {type(e).__name__}: {str(e)[:150]}")
        return 1

    # 1. 登录（每次运行都用无头浏览器登录，不依赖持久化 cookie）
    cookie = ""
    used_cookie_secret = False
    ck_secret = (os.environ.get("NEO_COOKIE") or "").strip()
    if ck_secret:
        ok, why = session_ok(ck_secret)
        log(f"预置 cookie 检查: {'有效' if ok else '失效'} — {why}")
        if ok:
            cookie = ck_secret
            used_cookie_secret = True

    if not cookie:
        log("无头浏览器登录…")
        cookie, msg = refresh_cookie()
        if not cookie:
            # 区分「站点异常」与「脚本/凭证问题」，便于判断要不要人工介入
            if "表单缺失" in msg or "残缺 HTML" in msg:
                notify(f"⚠️ NeoHeberg 登录失败（站点侧问题）\n"
                       f"登录页 GET /login 返回残缺 HTML，表单元素不存在。\n"
                       f"这是 NeoHeberg 服务端 bug，非脚本问题；已重试 "
                       f"{LOGIN_ATTEMPTS} 次。\n"
                       f"建议：等站点修复后自动恢复，或手动到面板续期。")
            else:
                notify(f"❌ NeoHeberg 任务失败：登录失败\n原因: {msg}")
            log(f"登录失败: {msg}")
            return 1
        log("登录成功")
        actions.append("无头登录成功")
    else:
        log("使用预置 cookie（本次未走浏览器）")

    # 2. 到期检查
    try:
        csrf, dash = panel_csrf(cookie)
    except Exception as e:
        notify(f"❌ NeoHeberg: 面板读取失败 {type(e).__name__}: {str(e)[:150]}")
        return 1

    exp, days = parse_expiry(dash)
    log(f"到期日: {exp}  剩余: {days} 天")
    if csrf is None:
        notify("❌ NeoHeberg: 未取到 CSRF token")
        return 1

    if days is not None and days <= RENEW_DAYS:
        try:
            rok, rmsg = renew(cookie, csrf)
            log(f"续期: {rok} — {rmsg}")
            actions.append(f"续期{'成功' if rok else '失败'}: {rmsg}")
        except Exception as e:
            log(f"续期异常: {type(e).__name__}: {str(e)[:150]}")
            actions.append(f"续期异常 {type(e).__name__}")
    else:
        log(f"未到续期阈值（{RENEW_DAYS} 天），跳过")
        actions.append(f"续期跳过（剩 {days} 天）")

    # 3. 容器状态
    status = "unknown"
    try:
        st = vps_status(cookie)
        status = (st.get("status") or "unknown").lower()
        log(f"容器状态: {status}  uptime={st.get('uptime')}s  "
            f"RAM={st.get('ram',{}).get('percent')}%")
    except Exception as e:
        log(f"状态查询失败: {type(e).__name__}: {str(e)[:120]}")

    if status in ("stopped", "halted", "shutdown"):
        try:
            res = power(cookie, csrf, "start")
            log(f"开机指令: {res}")
            actions.append("检测到停机 → 已下发开机")
            time.sleep(45)
            try:
                st2 = vps_status(cookie)
                s2 = (st2.get("status") or "?").lower()
                actions.append(f"开机后状态: {s2}")
                log(f"开机 45s 后: {s2}")
            except Exception:
                pass
        except Exception as e:
            log(f"开机失败: {type(e).__name__}: {str(e)[:150]}")
            actions.append(f"开机失败 {type(e).__name__}")
    elif status in ("starting", "stopping", "restarting"):
        log("容器处于过渡状态，不干预")
        actions.append(f"容器过渡中({status})，跳过")
    else:
        log("容器运行中，无需开机")
        actions.append(f"容器正常({status})")

    # 4. 重新读取到期日（续期后）
    try:
        _, dash2 = panel_csrf(cookie)
        exp2, days2 = parse_expiry(dash2)
    except Exception:
        exp2, days2 = exp, days

    # 5. 汇总
    body = "\n".join(f"• {a}" for a in actions)
    icon = "✅" if not any(
        ("失败" in a or "异常" in a) for a in actions) else "⚠️"
    text = (f"{icon} NeoHeberg 每日任务\n{body}\n"
            f"到期: {exp2} (剩 {days2} 天)\n"
            f"容器: {status}\n"
            f"代理: {socks_proxy.describe()}")
    if RUN_URL:
        text += f"\n{RUN_URL}"
    notify(text)

    log("完成")
    # 续期/开机类失败让 workflow 标红，便于发现
    hard_fail = any(("续期失败" in a or "开机失败" in a or "异常" in a) for a in actions)
    return 1 if hard_fail else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", "ignore")[:200]
        log(f"HTTPError {e.code}: {msg}")
        notify(f"❌ NeoHeberg 任务异常：HTTP {e.code}\n{msg[:180]}")
        sys.exit(1)
    except Exception as e:
        log(f"未捕获异常: {type(e).__name__}: {e}")
        notify(f"❌ NeoHeberg 任务异常：{type(e).__name__}: {str(e)[:180]}")
        sys.exit(1)
