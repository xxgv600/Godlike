#!/usr/bin/env python3
"""
Godlike 主机自动续期 + 开机脚本 v3
2026-10-14 重构：API 请求改在浏览器内 via fetch() 发起
  原因：新版面板 OAuth 登录后，/api/v2 不接受 Cookie（回 Unauthenticated.），
        且无 Bearer Token 可抓。浏览器内的 fetch 自动沿用页面自身的认证方式。
  流程：Playwright OAuth 登录 → 浏览器保持打开 →
        续期 API 全部经 page.evaluate(fetch) 调用 →
        WS 开机（仍用 websockets 库）→ 关闭浏览器
"""

import os, sys, time, traceback, random, json, re, asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
import websockets
from playwright.sync_api import sync_playwright

# ---------- 配置 ----------
FRONT_BASE = "https://panel.godlike.host"
API_BASE = "https://panel.godlike.host/api/v2"
LOGIN_URL = f"{FRONT_BASE}/login"
OAUTH_HOST = "godlike.host"
OUTPUT_DIR = Path("scripts/Godlike")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
CN_TZ = timezone(timedelta(hours=8))

# ---------- 工具 ----------
def cn_time():
    return datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")

def mask_email(email: str) -> str:
    if not email or "@" not in email:
        return "***"
    user, domain = email.split("@", 1)
    parts = domain.split(".")
    md = "***." + parts[-1] if len(parts) >= 2 else "***"
    return f"{user[:3]}***@{md}"

def mask_server(sid: str) -> str:
    return f"{sid[:3]}***" if sid else "***"

def snapshot(name: str) -> str:
    return str(OUTPUT_DIR / f"{name}_{int(time.time())}.png")

def notify_tg(ok: bool, email="", server="", after="", error_msg="", screenshot=None):
    token = os.environ.get("TG_BOT_TOKEN")
    chat_id = os.environ.get("TG_CHAT_ID")
    if not token or not chat_id:
        return
    msg = "✅ 续期+开机成功\n\n" if ok else "❌ 操作失败\n\n"
    if email:  msg += f"账号：{email}\n"
    if server: msg += f"服务器：{server}\n"
    if ok and after: msg += f"下次可续期：{after}\n"
    if not ok and error_msg: msg += f"原因：{error_msg}\n"
    msg += f"\n时间：{cn_time()}\nGodlike Host Auto Renew"
    try:
        if screenshot and Path(screenshot).exists():
            with open(screenshot, "rb") as f:
                requests.post(f"https://api.telegram.org/bot{token}/sendPhoto",
                              data={"chat_id": chat_id, "caption": msg},
                              files={"photo": f}, timeout=30)
        else:
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat_id, "text": msg, "disable_web_page_preview": True},
                          timeout=30)
        print("[INFO] TG 通知已发送", flush=True)
    except Exception as e:
        print(f"[WARN] TG 通知发送失败: {e}", flush=True)

def dismiss_cookies(page):
    try:
        closed = page.evaluate("""() => {
            const btns = Array.from(document.querySelectorAll('button'));
            for (const b of btns) {
                const t = (b.textContent || '').trim();
                if (t === 'Reject All' || t === 'Accept All') { b.click(); return t; }
            }
            return null;
        }""")
        if closed:
            print(f"[INFO] 已关闭 cookie 弹窗 ({closed})", flush=True)
        page.wait_for_timeout(800)
    except Exception:
        pass

def find_visible_input(page, selectors, timeout_ms=3000):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=timeout_ms):
                return sel
        except Exception:
            continue
    return None

# ---------- 浏览器内 API 调用 ----------
def api_fetch(page, method: str, url: str, body: dict = None) -> dict:
    """
    在浏览器上下文中用 fetch() 调 API，自动沿用页面的登录态。
    返回解析后的 JSON（失败返回 {}，并打印状态码）。
    """
    js_body = json.dumps(body) if body else "null"
    result = page.evaluate("""async ({method, url, jsBody}) => {
        try {
            const opts = {method: method, headers: {'Content-Type': 'application/json', 'Accept': 'application/json'}};
            if (jsBody) opts.body = jsBody;
            const r = await fetch(url, opts);
            const text = await r.text();
            let data = null;
            try { data = JSON.parse(text); } catch(e) { data = {raw: text.slice(0, 200)}; }
            return {status: r.status, data: data};
        } catch(e) {
            return {status: -1, data: {error: String(e)}};
        }
    }""", {"method": method, "url": url, "jsBody": js_body})
    status = result.get("status", -1)
    data = result.get("data") or {}
    if status != 200:
        print(f"[WARN] API {method} {url.split('/api/v2')[-1][:45]} → HTTP {status}: {str(data)[:120]}", flush=True)
    return data if isinstance(data, dict) else {}

# ---------- OAuth 登录（浏览器保持打开） ----------
class BrowserSession:
    def __init__(self, pw, browser, ctx, page):
        self.pw, self.browser, self.ctx, self.page = pw, browser, ctx, page
    def close(self):
        try:
            self.ctx.close()
        except Exception:
            pass
        try:
            self.browser.close()
        except Exception:
            pass
        try:
            self.pw.stop()
        except Exception:
            pass

def oauth_login(user: str, pwd: str, proxy: str = None):
    """
    返回 (BrowserSession|None, full_uuid|None, short_id|None, fail_shot|None)
    成功时浏览器保持打开，调用方负责 close()。
    """
    full_uuid, short_id, fail_shot = None, None, None
    uuid_re = re.compile(r'/servers/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})')

    def on_request(request):
        nonlocal full_uuid, short_id
        m = uuid_re.search(request.url)
        if m and not full_uuid:
            full_uuid = m.group(1)
            short_id = full_uuid.split('-')[0]

    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True,
                                 proxy={"server": proxy} if proxy else None,
                                 args=["--no-sandbox", "--disable-setuid-sandbox"])
    ctx = browser.new_context(extra_http_headers={"Accept-Language": "en-US,en;q=0.9"})
    page = ctx.new_page()
    page.on("request", on_request)
    sess = BrowserSession(pw, browser, ctx, page)

    try:
        print("[INFO] 打开面板登录页", flush=True)
        page.goto(LOGIN_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        dismiss_cookies(page)

        clicked = page.evaluate("""() => {
            const els = Array.from(document.querySelectorAll('*'));
            for (const el of els) {
                const t = (el.textContent || '').trim();
                if (t === 'Authorization' && el.children.length === 0) { el.click(); return 'leaf:' + el.tagName; }
            }
            for (const el of els) {
                const t = (el.textContent || '').trim();
                if (t === 'Authorization') { el.click(); return 'fallback:' + el.tagName; }
            }
            return null;
        }""")
        print(f"[INFO] 点击 Authorization: {clicked}", flush=True)
        if not clicked:
            raise Exception("找不到 Authorization 按钮")

        page.wait_for_url(f"**{OAUTH_HOST}/clientarea/oauth/authorize.php**", timeout=20000)
        print("[INFO] 已到 OAuth 授权页", flush=True)
        page.wait_for_timeout(2000)
        dismiss_cookies(page)

        email_sel = find_visible_input(page, ['input[name="username"]', 'input[type="email"]',
                                              'input[name="email"]', 'input[type="text"]'])
        pwd_sel = find_visible_input(page, ['input[type="password"]', 'input[name="password"]'])
        print(f"[INFO] 邮箱框: {email_sel}, 密码框: {pwd_sel}", flush=True)
        if not email_sel or not pwd_sel:
            raise Exception("OAuth 页找不到邮箱/密码输入框")
        page.fill(email_sel, user)
        page.fill(pwd_sel, pwd)

        logged = False
        for sel in ['button[type="submit"]', 'input[type="submit"]',
                    'button:has-text("Login")', 'button:has-text("Sign in")']:
            try:
                btn = page.locator(sel).first
                if btn.is_visible(timeout=3000):
                    btn.click(timeout=5000)
                    logged = True
                    print(f"[INFO] 已点 OAuth 登录: {sel}", flush=True)
                    break
            except Exception:
                continue
        if not logged:
            page.locator(pwd_sel).first.press("Enter")
            print("[INFO] 用回车提交 OAuth 登录", flush=True)
        page.wait_for_timeout(4000)

        # 授权确认页
        for _ in range(2):
            done = False
            for sel in ['button:has-text("Authorize")', 'button:has-text("Allow")',
                        'button:has-text("授权")', 'input[value*="uthorize" i]']:
                try:
                    btn = page.locator(sel).first
                    if btn.is_visible(timeout=3000):
                        btn.click(timeout=5000)
                        done = True
                        print(f"[INFO] 已点击授权确认: {sel}", flush=True)
                        page.wait_for_timeout(4000)
                        break
                except Exception:
                    continue
            if not done:
                break

        try:
            page.wait_for_url(f"{FRONT_BASE}/**", timeout=20000)
        except Exception:
            pass
        print(f"[INFO] 当前 URL: {page.url[:80]}", flush=True)
        if OAUTH_HOST in page.url or "authorize.php" in page.url:
            raise Exception("仍停留在 OAuth 页，登录可能失败（账号密码错误？）")
        page.wait_for_timeout(3000)

        # 提取 short_id
        if not short_id and '/server/' in page.url:
            parts = page.url.rstrip('/').split('/')
            for i, pp in enumerate(parts):
                if pp == 'server' and i + 1 < len(parts) and len(parts[i+1]) == 8:
                    short_id = parts[i+1]
                    break
        if not short_id:
            try:
                links = page.locator('a[href*="/server/"]')
                for i in range(min(links.count(), 10)):
                    href = links.nth(i).get_attribute("href") or ""
                    parts = href.rstrip('/').split('/')
                    for j, pp in enumerate(parts):
                        if pp == 'server' and j + 1 < len(parts) and len(parts[j+1]) == 8:
                            short_id = parts[j+1]
                            break
                    if short_id:
                        break
            except Exception:
                pass

        if short_id and not full_uuid:
            page.goto(f"{FRONT_BASE}/server/{short_id}", wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            page.wait_for_timeout(2000)

        if not full_uuid and short_id:
            try:
                m = re.search(rf'{re.escape(short_id)}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{12}}',
                              page.content())
                if m:
                    full_uuid = m.group(0)
            except Exception:
                pass

        print(f"[INFO] UUID: {'OK' if full_uuid else '缺失'}, 短ID: {short_id or '缺失'}", flush=True)
        if not full_uuid:
            raise Exception("未能获取服务器 UUID")
        return sess, full_uuid, short_id, None

    except Exception as e:
        print(f"[ERROR] OAuth 登录异常: {e}", flush=True)
        traceback.print_exc()
        try:
            fail_shot = snapshot("oauth_fail")
            page.screenshot(path=fail_shot)
        except Exception:
            pass
        sess.close()
        return None, None, None, fail_shot

# ---------- 续期（浏览器内 fetch） ----------
def check_video_status(page, uuid: str) -> dict:
    return api_fetch(page, "GET",
                     f"{API_BASE}/servers/{uuid}/free-renewal/video/status?type=youtube_iter1&locale=en")

def call_video_start(page, uuid: str) -> dict:
    return api_fetch(page, "POST",
                     f"{API_BASE}/servers/{uuid}/free-renewal/video/start?locale=en",
                     {"type": "youtube_iter1"})

def call_update_once(page, uuid: str, watch_uuid: str, rid: int, vt: int) -> dict:
    return api_fetch(page, "POST",
                     f"{API_BASE}/servers/{uuid}/free-renewal/video/update-time?locale=en",
                     {"uuid": watch_uuid, "renewal_uuid": watch_uuid,
                      "renewal_id": rid, "video_time_watched": vt})

def simulate_video_watching(page, uuid: str, watch_uuid: str, rid: int):
    steps = [30, 60, 90, 120, 150, 180, 210, 240]
    wait_s = 28
    print(f"[INFO] 开始续期（{len(steps)}步 x {wait_s}s）", flush=True)
    last = {}
    for i, vt in enumerate(steps):
        print(f"[INFO] 步骤 {i+1}/{len(steps)} 等待{wait_s}s → 上报{vt}s", flush=True)
        time.sleep(wait_s)
        resp = call_update_once(page, uuid, watch_uuid, rid, vt)
        last = resp
        if not resp.get("success", False):
            msg = resp.get("message", "")
            if "Invalid time increment" in msg and resp.get("current_time", 0) > 0:
                adj = resp["current_time"] + 30
                print(f"[WARN] 时间偏差，重试 {adj}s", flush=True)
                time.sleep(2)
                r2 = call_update_once(page, uuid, watch_uuid, rid, adj)
                if r2.get("success"):
                    last = r2
                    steps[i+1:] = [adj + 30*(j+1) for j in range(len(steps)-i-1)]
                    if r2.get("new_free_timer"):
                        return True, r2["new_free_timer"]
                    continue
                return False, r2.get("message", "重试失败")
            return False, msg or f"HTTP 非200（见上方 WARN）"
        if resp.get("new_free_timer"):
            return True, resp["new_free_timer"]
    if last.get("success"):
        return True, last.get("new_free_timer") or "续期已提交"
    return False, last.get("message", "未知错误")

# ---------- WS 开机 ----------
def get_ws_creds(page, uuid: str):
    data = api_fetch(page, "GET", f"{API_BASE}/servers/{uuid}/websocket?locale=en")
    d = data.get("data", data) if isinstance(data, dict) else {}
    tok, sock = d.get("token"), d.get("socket")
    if not tok or not sock:
        print(f"[ERROR] WS凭证获取失败: {str(data)[:120]}", flush=True)
    return tok, sock

async def ws_start_server(socket_url: str, jwt: str) -> str:
    print("[INFO] 连接 WebSocket...", flush=True)
    try:
        async with websockets.connect(socket_url, origin=FRONT_BASE,
                                      additional_headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"},
                                      ping_interval=20, ping_timeout=20, close_timeout=10) as ws:
            await ws.send(json.dumps({"event": "auth", "args": [jwt]}))
            ok = False
            for _ in range(10):
                try:
                    d = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                    if d.get("event") == "auth success":
                        ok = True
                        print("[INFO] WS认证成功", flush=True)
                        break
                except asyncio.TimeoutError:
                    break
            if not ok:
                return "auth_failed"
            status = "unknown"
            deadline = asyncio.get_event_loop().time() + 5
            while asyncio.get_event_loop().time() < deadline:
                try:
                    d = json.loads(await asyncio.wait_for(ws.recv(), timeout=deadline - asyncio.get_event_loop().time()))
                    if d.get("event") == "status":
                        status = d.get("args", ["unknown"])[0]
                        print(f"[INFO] 当前状态: {status}", flush=True)
                        break
                    if d.get("event") == "stats":
                        try:
                            s = json.loads(d["args"][0]).get("state", "")
                            if s:
                                status = s
                                print(f"[INFO] 当前状态: {status}", flush=True)
                                break
                        except Exception:
                            pass
                except Exception:
                    break
            if status in ("running", "starting"):
                return "already_running"
            await ws.send(json.dumps({"event": "set state", "args": ["start"]}))
            print("[INFO] 已发开机指令，等待启动...", flush=True)
            for _ in range(60):
                try:
                    d = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                    ev, args = d.get("event", ""), d.get("args", [])
                    ns = args[0] if ev == "status" and args else ""
                    if ev == "stats":
                        try:
                            ns = json.loads(args[0]).get("state", "")
                        except Exception:
                            ns = ""
                    if ns in ("starting", "running"):
                        print(f"[INFO] 服务器启动成功（{ns}）", flush=True)
                        return "started"
                except asyncio.TimeoutError:
                    continue
                except Exception:
                    break
            return "sent"
    except Exception as e:
        print(f"[ERROR] WebSocket异常: {e}", flush=True)
        return "error"

# ---------- 单账号 ----------
def process_account(key: str, proxy: str = None) -> bool:
    raw = os.environ.get(key, "").strip()
    if not raw:
        return True
    try:
        user, pwd = [s.strip() for s in raw.split("-----", 1)]
    except Exception:
        print(f"[ERROR] {key} 格式错误", flush=True)
        notify_tg(False, error_msg="格式错误")
        return False

    print(f"\n{'='*60}\n[INFO] 处理 {key} ({mask_email(user)})\n{'='*60}", flush=True)

    sess, full_uuid, short_id, shot = oauth_login(user, pwd, proxy)
    if not sess:
        notify_tg(False, email=user, error_msg="OAuth 登录失败", screenshot=shot)
        return False
    page = sess.page
    print(f"[INFO] 登录成功 | 服务器: {mask_server(short_id)}", flush=True)

    try:
        # 续期状态
        st = check_video_status(page, full_uuid)
        can_watch, wait_s = st.get("can_watch"), st.get("time_until_next_video") or 0
        renew_ok, cooldown = False, ""
        if not can_watch and wait_s > 0:
            cooldown = f"{wait_s//3600}h {(wait_s%3600)//60}m"
            print(f"[INFO] 冷却期，下次可续期: {cooldown}", flush=True)
            renew_ok = True
        else:
            sr = call_video_start(page, full_uuid)
            if not sr.get("success"):
                err = sr.get("message", "start API失败")
                print(f"[ERROR] 续期启动失败: {err}", flush=True)
                notify_tg(False, email=user, server=short_id, error_msg=err)
                return False
            print(f"[INFO] 续期会话已建立 (renewal_id={sr.get('renewal_id')})", flush=True)
            renew_ok, result = simulate_video_watching(page, full_uuid, sr.get("uuid", ""), sr.get("renewal_id", 0))
            if not renew_ok:
                print(f"[ERROR] 续期失败: {result}", flush=True)
                notify_tg(False, email=user, server=short_id, error_msg=result)
                return False
            time.sleep(2)
            w = check_video_status(page, full_uuid).get("time_until_next_video") or 0
            cooldown = f"{w//3600}h {(w%3600)//60}m" if w > 0 else str(result)
            print(f"[INFO] 续期成功，下次可续期: {cooldown}", flush=True)

        # 开机
        print("[INFO] ── 开机 ──", flush=True)
        jwt, sock_url = get_ws_creds(page, full_uuid)
        if not jwt or not sock_url:
            start_note = "WS凭证获取失败"
            print(f"[WARN] {start_note}", flush=True)
        else:
            try:
                r = asyncio.run(ws_start_server(sock_url, jwt))
            except Exception as e:
                print(f"[ERROR] WS开机异常: {e}", flush=True)
                r = "error"
            start_note = {"already_running": "服务器已在运行中，无需开机", "started": "开机成功",
                          "sent": "开机指令已发送（等待超时）", "auth_failed": "WS认证失败",
                          "error": "开机异常"}.get(r, "未知状态")
            print(f"[INFO] {start_note}", flush=True)

        notify_tg(True, email=user, server=short_id, after=f"{cooldown}\n{start_note}")
        print(f"[INFO] {key} 处理完成", flush=True)
        return renew_ok
    finally:
        sess.close()

def main():
    proxy = os.environ.get("PROXY_SERVER", "")
    if proxy:
        print("[INFO] 使用代理", flush=True)
    all_ok = True
    accounts = [f"GODLIKE_{i}" for i in range(1, 6)]
    for idx, acc in enumerate(accounts):
        try:
            if not process_account(acc, proxy or None):
                all_ok = False
        except Exception as e:
            print(f"[FATAL] {acc} 崩溃: {e}", flush=True)
            traceback.print_exc()
            notify_tg(False, email=acc, error_msg=f"脚本崩溃: {str(e)[:200]}")
            all_ok = False
        if idx < len(accounts) - 1:
            time.sleep(random.randint(5, 15))
    print("\n[INFO] 所有账号处理成功" if all_ok else "\n[ERROR] 部分账号失败", flush=True)
    sys.exit(0 if all_ok else 1)

if __name__ == "__main__":
    main()
