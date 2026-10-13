#!/usr/bin/env python3
"""
Godlike 主机自动续期 + 开机脚本 v2
2026-10-14 全新重写：改走 OAuth 登录流程
  旧版密码直登 POST /auth/login 已触发服务端 Internal Error（Laravel 密钥未配置）
  新流程：panel 登录页 → 点 "Authorization" → 跳转 WHMCS OAuth 授权页 →
          WHMCS 账号登录 → 授权确认 → 回调面板 → 抓取 Bearer Token
"""

import os, sys, time, traceback, random, json, re, asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Tuple

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

# ---------- 工具函数 ----------
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
    """用 JS 关掉 cookie 弹窗"""
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
    """按优先级找第一个可见的输入框，返回 selector 或 None"""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=timeout_ms):
                return sel
        except Exception:
            continue
    return None

# ---------- OAuth 登录 ----------
def oauth_login(user: str, pwd: str, proxy: str = None):
    """
    返回 (bearer_token, full_uuid, short_id, fail_screenshot)
    """
    bearer_token, bearer_fallback = None, None
    full_uuid, short_id, fail_shot = None, None, None

    def on_request(request):
        nonlocal bearer_token, bearer_fallback, full_uuid, short_id
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            token = auth.replace("Bearer ", "").strip()
            if token:
                if "ptlc_" in token:
                    bearer_token = token
                elif not bearer_fallback:
                    bearer_fallback = token
        m = re.search(r'/servers/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})', request.url)
        if m and not full_uuid:
            full_uuid = m.group(1)
            short_id = full_uuid.split('-')[0]

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            proxy={"server": proxy} if proxy else None,
            args=["--no-sandbox", "--disable-setuid-sandbox"])
        ctx = browser.new_context(extra_http_headers={"Accept-Language": "en-US,en;q=0.9"})
        page = ctx.new_page()
        page.on("request", on_request)

        try:
            # ---- 1. 打开面板登录页 ----
            print(f"[INFO] 打开面板登录页", flush=True)
            page.goto(LOGIN_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            dismiss_cookies(page)

            # ---- 2. 点 "Authorization" 大按钮 ----
            clicked = page.evaluate("""() => {
                const els = Array.from(document.querySelectorAll('*'));
                for (const el of els) {
                    const t = (el.textContent || '').trim();
                    if (t === 'Authorization' && el.children.length === 0) {
                        el.click(); return 'leaf:' + el.tagName;
                    }
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

            # ---- 3. 等待跳转到 WHMCS OAuth 页 ----
            page.wait_for_url(f"**{OAUTH_HOST}/clientarea/oauth/authorize.php**", timeout=20000)
            print(f"[INFO] 已到 OAuth 授权页: {page.url[:90]}", flush=True)
            page.wait_for_timeout(2000)
            dismiss_cookies(page)

            # ---- 4. 填写 WHMCS 邮箱密码 ----
            email_sel = find_visible_input(page, [
                'input[name="username"]', 'input[type="email"]',
                'input[name="email"]', 'input[type="text"]',
                'input[placeholder*="Email" i]',
            ])
            pwd_sel = find_visible_input(page, ['input[type="password"]', 'input[name="password"]'])
            print(f"[INFO] 邮箱框: {email_sel}, 密码框: {pwd_sel}", flush=True)
            if not email_sel or not pwd_sel:
                raise Exception("OAuth 页找不到邮箱/密码输入框")
            page.fill(email_sel, user)
            page.fill(pwd_sel, pwd)

            # ---- 5. 点 Login ----
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
            print(f"[INFO] OAuth 登录后 URL: {page.url[:90]}", flush=True)

            # ---- 6. 处理授权确认页（如果有 "Authorize"/"Allow" 按钮） ----
            for _ in range(2):
                authorized = False
                for sel in ['button:has-text("Authorize")', 'button:has-text("Allow")',
                            'button:has-text("授权")', 'input[value*="uthorize" i]']:
                    try:
                        btn = page.locator(sel).first
                        if btn.is_visible(timeout=3000):
                            btn.click(timeout=5000)
                            authorized = True
                            print(f"[INFO] 已点击授权确认: {sel}", flush=True)
                            page.wait_for_timeout(4000)
                            break
                    except Exception:
                        continue
                if not authorized:
                    break
            print(f"[INFO] 授权后 URL: {page.url[:90]}", flush=True)

            # ---- 7. 等待回到面板 ----
            try:
                page.wait_for_url(f"{FRONT_BASE}/**", timeout=20000)
                print(f"[INFO] 已回到面板: {page.url[:80]}", flush=True)
            except Exception:
                print(f"[WARN] 等待回面板超时，当前: {page.url[:80]}", flush=True)
            page.wait_for_timeout(3000)

            # ---- 7b. 尝试从 NextAuth session 端点拿 access token ----
            if not final_token:
                try:
                    sess = page.evaluate("""async () => {
                        try {
                            const r = await fetch('/api/auth/session');
                            if (!r.ok) return {error: 'http_' + r.status};
                            return await r.json();
                        } catch(e) { return {error: String(e)}; }
                    }""")
                    print(f"[INFO] session 端点返回: {str(sess)[:250]}", flush=True)
                    if isinstance(sess, dict):
                        for k in ["accessToken", "access_token", "token", "jwt", "idToken"]:
                            if sess.get(k) and len(str(sess[k])) > 20:
                                final_token = str(sess[k])
                                print(f"[INFO] 从 session 拿到 token (字段={k})", flush=True)
                                break
                        # 有些实现把 token 藏在 user 或 data 下
                        if not final_token:
                            for container in [sess.get("user"), sess.get("data")]:
                                if isinstance(container, dict):
                                    for k in ["accessToken", "access_token", "token"]:
                                        if container.get(k) and len(str(container[k])) > 20:
                                            final_token = str(container[k])
                                            print(f"[INFO] 从 session.user/data 拿到 token (字段={k})", flush=True)
                                            break
                                if final_token:
                                    break
                except Exception as e:
                    print(f"[WARN] session 端点查询失败: {e}", flush=True)

            # ---- 8. 从 URL 或链接提取 short_id ----
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

            # 进服务器页触发 API 请求，便于抓 token
            if short_id and not bearer_token and not bearer_fallback:
                page.goto(f"{FRONT_BASE}/server/{short_id}", wait_until="domcontentloaded")
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
                page.wait_for_timeout(3000)

            # 从页面 HTML 找 full_uuid
            if not full_uuid and short_id:
                try:
                    m = re.search(rf'{re.escape(short_id)}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{12}}',
                                  page.content())
                    if m:
                        full_uuid = m.group(0)
                except Exception:
                    pass

            # localStorage 兜底找 token
            if not bearer_token and not bearer_fallback:
                try:
                    for key in ["token", "auth_token", "access_token", "api_token", "jwt"]:
                        val = page.evaluate(
                            f"() => {{ try {{ return localStorage.getItem('{key}'); }} catch(e) {{ return null; }} }}")
                        if val and len(str(val)) > 20:
                            bearer_fallback = str(val).strip()
                            print(f"[INFO] localStorage 找到 token ({key})", flush=True)
                            break
                except Exception:
                    pass

            final_token = bearer_token or bearer_fallback

            # 抓取浏览器 Cookie（新版面板可能用 Cookie 会话而非 Bearer）
            cookies = {}
            try:
                for c in ctx.cookies():
                    cookies[c["name"]] = c["value"]
                print(f"[INFO] 抓到 {len(cookies)} 个 Cookie: {list(cookies.keys())[:6]}", flush=True)
            except Exception as e:
                print(f"[WARN] Cookie 获取失败: {e}", flush=True)

            print(f"[INFO] Token: {'OK' if final_token else '缺失'}, "
                  f"UUID: {'OK' if full_uuid else '缺失'}, 短ID: {short_id or '缺失'}", flush=True)
            return final_token, full_uuid, short_id, fail_shot, cookies

        except Exception as e:
            print(f"[ERROR] OAuth 登录异常: {e}", flush=True)
            traceback.print_exc()
            try:
                fail_shot = snapshot("oauth_fail")
                page.screenshot(path=fail_shot)
                print(f"[INFO] 失败截图: {fail_shot}", flush=True)
            except Exception:
                pass
            return None, None, None, fail_shot
        finally:
            ctx.close()
            browser.close()

# ---------- API ----------
# 会话级 Cookie（OAuth 登录后填入，API 调用时自动带上）
_api_cookies = {}

def api_headers(tok: str = "") -> dict:
    h = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Origin": FRONT_BASE,
        "Referer": f"{FRONT_BASE}/",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h

def _get(url: str, tok: str = "") -> dict:
    try:
        r = requests.get(url, headers=api_headers(tok), cookies=_api_cookies, timeout=30)
        return r.json()
    except Exception as e:
        print(f"[ERROR] GET 失败 {url.split('/api/v2')[-1][:40]}: {e}", flush=True)
        return {}

def _post(url: str, tok: str = "", body: dict = None) -> dict:
    try:
        r = requests.post(url, headers=api_headers(tok), cookies=_api_cookies,
                          json=body or {}, timeout=30)
        return r.json()
    except Exception as e:
        print(f"[ERROR] POST 失败 {url.split('/api/v2')[-1][:40]}: {e}", flush=True)
        return {}

def check_video_status(uuid: str, tok: str) -> dict:
    return _get(f"{API_BASE}/servers/{uuid}/free-renewal/video/status?type=youtube_iter1&locale=en", tok)

def call_video_start(uuid: str, tok: str) -> dict:
    return _post(f"{API_BASE}/servers/{uuid}/free-renewal/video/start?locale=en", tok, {"type": "youtube_iter1"})

def call_update_once(uuid: str, tok: str, watch_uuid: str, rid: int, vt: int) -> dict:
    return _post(f"{API_BASE}/servers/{uuid}/free-renewal/video/update-time?locale=en", tok,
                 {"uuid": watch_uuid, "renewal_uuid": watch_uuid,
                  "renewal_id": rid, "video_time_watched": vt})

def simulate_video_watching(uuid: str, tok: str, watch_uuid: str, rid: int):
    steps = [30, 60, 90, 120, 150, 180, 210, 240]
    wait_s = 28
    print(f"[INFO] 开始续期（{len(steps)}步 x {wait_s}s）", flush=True)
    last = {}
    for i, vt in enumerate(steps):
        print(f"[INFO] 步骤 {i+1}/{len(steps)} 等待{wait_s}s → 上报{vt}s", flush=True)
        time.sleep(wait_s)
        resp = call_update_once(uuid, tok, watch_uuid, rid, vt)
        last = resp
        if not resp.get("success", False):
            msg = resp.get("message", "")
            if "Invalid time increment" in msg and resp.get("current_time", 0) > 0:
                adj = resp["current_time"] + 30
                print(f"[WARN] 时间偏差，重试 {adj}s", flush=True)
                time.sleep(2)
                r2 = call_update_once(uuid, tok, watch_uuid, rid, adj)
                if r2.get("success"):
                    last = r2
                    steps[i+1:] = [adj + 30*(j+1) for j in range(len(steps)-i-1)]
                    if r2.get("new_free_timer"):
                        return True, r2["new_free_timer"]
                    continue
                return False, r2.get("message", "重试失败")
            return False, msg
        if resp.get("new_free_timer"):
            return True, resp["new_free_timer"]
    if last.get("success"):
        return True, last.get("new_free_timer") or "续期已提交"
    return False, last.get("message", "未知错误")

def get_ws_creds(uuid: str, tok: str):
    try:
        r = requests.get(f"{API_BASE}/servers/{uuid}/websocket?locale=en",
                         headers=api_headers(tok), cookies=_api_cookies, timeout=30)
        if r.status_code == 200:
            d = r.json().get("data", r.json())
            return d.get("token"), d.get("socket")
        print(f"[ERROR] WS凭证失败: {r.status_code} {r.text[:120]}", flush=True)
        return None, None
    except Exception as e:
        print(f"[ERROR] WS凭证异常: {e}", flush=True)
        return None, None

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

def start_via_ws(uuid: str, tok: str) -> str:
    jwt, url = get_ws_creds(uuid, tok)
    if not jwt or not url:
        return "error"
    try:
        return asyncio.run(ws_start_server(url, jwt))
    except Exception as e:
        print(f"[ERROR] WS开机异常: {e}", flush=True)
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

    login_res = oauth_login(user, pwd, proxy)
    tok, full_uuid, short_id = login_res[0], login_res[1], login_res[2]
    shot = login_res[3] if len(login_res) > 3 else None
    login_cookies = login_res[4] if len(login_res) > 4 else {}
    # 把浏览器 Cookie 注入 API 会话
    global _api_cookies
    _api_cookies = login_cookies or {}
    if _api_cookies:
        print(f"[INFO] API 将使用 {len(_api_cookies)} 个 Cookie 进行认证", flush=True)
    if not tok and not _api_cookies:
        print("[ERROR] 未能获取 Bearer Token 且无 Cookie", flush=True)
        notify_tg(False, email=user, error_msg="OAuth 登录未获取到认证信息", screenshot=shot)
        return False
    if not tok:
        print("[INFO] 无 Bearer Token，改用 Cookie 会话调用 API", flush=True)
        tok = ""
    if not full_uuid:
        print("[ERROR] 未能获取服务器 UUID", flush=True)
        notify_tg(False, email=user, error_msg="未获取到服务器 UUID", screenshot=shot)
        return False
    print(f"[INFO] 登录成功 | 服务器: {mask_server(short_id)}", flush=True)

    # 续期
    st = check_video_status(full_uuid, tok)
    can_watch, wait_s = st.get("can_watch"), st.get("time_until_next_video") or 0
    renew_ok, cooldown = False, ""
    if not can_watch and wait_s > 0:
        cooldown = f"{wait_s//3600}h {(wait_s%3600)//60}m"
        print(f"[INFO] 冷却期，下次可续期: {cooldown}", flush=True)
        renew_ok = True
    else:
        sr = call_video_start(full_uuid, tok)
        if not sr.get("success"):
            err = sr.get("message", "start API失败")
            print(f"[ERROR] 续期启动失败: {err}", flush=True)
            notify_tg(False, email=user, server=short_id, error_msg=err)
            return False
        print(f"[INFO] 续期会话已建立 (renewal_id={sr.get('renewal_id')})", flush=True)
        renew_ok, result = simulate_video_watching(full_uuid, tok, sr.get("uuid", ""), sr.get("renewal_id", 0))
        if not renew_ok:
            print(f"[ERROR] 续期失败: {result}", flush=True)
            notify_tg(False, email=user, server=short_id, error_msg=result)
            return False
        time.sleep(2)
        w = check_video_status(full_uuid, tok).get("time_until_next_video") or 0
        cooldown = f"{w//3600}h {(w%3600)//60}m" if w > 0 else str(result)
        print(f"[INFO] 续期成功，下次可续期: {cooldown}", flush=True)

    # 开机
    print("[INFO] ── 开机 ──", flush=True)
    r = start_via_ws(full_uuid, tok)
    note = {"already_running": "服务器已在运行中，无需开机", "started": "开机成功",
            "sent": "开机指令已发送（等待超时）", "auth_failed": "WS认证失败",
            "error": "开机异常"}.get(r, "未知状态")
    print(f"[INFO] {note}", flush=True)

    notify_tg(True, email=user, server=short_id, after=f"{cooldown}\n{note}")
    print(f"[INFO] {key} 处理完成", flush=True)
    return renew_ok

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
