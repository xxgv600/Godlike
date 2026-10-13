#!/usr/bin/env python3
"""
Godlike 主机自动续期 + 开机脚本
2026-10-14 修复：面板前端已从 ultra.panel.godlike.host 迁移至 panel.godlike.host
"""

import os, sys, time, traceback, random, json, re, asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Tuple

import requests
import websockets
from playwright.sync_api import sync_playwright

# ---------- 配置 ----------
# 2026-10 更新：ultra.panel.godlike.host/login 已无登录表单，仅剩跳转链接
# 面板前端现位于 panel.godlike.host
FRONT_BASE = "https://panel.godlike.host"
API_BASE = "https://panel.godlike.host/api/v2"
LOGIN_URL = f"{FRONT_BASE}/login"
OUTPUT_DIR = Path("scripts/Godlike")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
CN_TZ = timezone(timedelta(hours=8))

# ---------- 工具函数 ----------
def cn_time():
    return datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")

def mask_email(email: str) -> str:
    """仅用于工作流日志"""
    if not email or "@" not in email:
        return "***"
    user, domain = email.split("@", 1)
    domain_parts = domain.split(".")
    masked_domain = "***." + domain_parts[-1] if len(domain_parts) >= 2 else "***"
    return f"{user[:3]}***@{masked_domain}"

def mask_server(server_id: str) -> str:
    """服务器ID脱敏：只显示前3位"""
    if not server_id:
        return "***"
    return f"{server_id[:3]}***"

def snapshot(name: str) -> str:
    return str(OUTPUT_DIR / f"{name}_{int(time.time())}.png")

def notify_tg(ok: bool, email: str = "", server: str = "",
              before: str = "", after: str = "",
              error_msg: str = "", screenshot: str = None):
    """TG通知使用真实邮箱和服务器ID"""
    token = os.environ.get("TG_BOT_TOKEN")
    chat_id = os.environ.get("TG_CHAT_ID")
    if not token or not chat_id:
        return

    msg = "✅ 续期+开机成功\n\n" if ok else "❌ 操作失败\n\n"
    if email:   msg += f"账号：{email}\n"
    if server:  msg += f"服务器：{server}\n"
    if ok:
        if after: msg += f"下次可续期：{after}\n"
    else:
        if error_msg: msg += f"原因：{error_msg}\n"
    msg += f"\n时间：{cn_time()}\nGodlike Host Auto Renew"

    try:
        if screenshot and Path(screenshot).exists():
            with open(screenshot, "rb") as f:
                requests.post(
                    f"https://api.telegram.org/bot{token}/sendPhoto",
                    data={"chat_id": chat_id, "caption": msg},
                    files={"photo": f}, timeout=30)
        else:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": msg, "disable_web_page_preview": True},
                timeout=30)
        print("[INFO] TG 通知已发送", flush=True)
    except Exception as e:
        print(f"[WARN] TG 通知发送失败: {e}", flush=True)

# ---------- 登录并获取 Bearer Token + UUID ----------
def login_and_get_token(user: str, pwd: str, proxy: str = None):
    bearer_token = None
    bearer_fallback = None  # 非 ptlc_ 的 Bearer token 备用
    full_uuid = None
    short_id = None
    fail_shot = None

    def on_request(request):
        nonlocal bearer_token, bearer_fallback, full_uuid, short_id
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            token = auth.replace("Bearer ", "").strip()
            if token:
                if "ptlc_" in token:
                    if bearer_token != token:
                        bearer_token = token
                elif not bearer_fallback:
                    bearer_fallback = token
        url = request.url
        m = re.search(r'/servers/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})', url)
        if m and not full_uuid:
            full_uuid = m.group(1)
            short_id = full_uuid.split('-')[0]

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            proxy={"server": proxy} if proxy else None,
            args=["--no-sandbox", "--disable-setuid-sandbox"]
        )
        context = browser.new_context(
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        page = context.new_page()
        page.on("request", on_request)

        try:
            print(f"[INFO] 打开登录页: {LOGIN_URL}", flush=True)
            page.goto(LOGIN_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)

            # 先用 JS 关 cookie 弹窗（如果有）
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
                page.wait_for_timeout(1000)
            except Exception as e:
                print(f"[WARN] cookie 弹窗处理失败: {e}", flush=True)

            # 用 JS 点 "Through login/password"（纯文本元素，JS 点最稳）
            try:
                clicked = page.evaluate("""() => {
                    const els = Array.from(document.querySelectorAll('*'));
                    for (const el of els) {
                        const t = (el.textContent || '').trim();
                        if (t === 'Through login/password' && el.children.length === 0) {
                            el.click();
                            return 'leaf:' + el.tagName;
                        }
                    }
                    for (const el of els) {
                        const t = (el.textContent || '').trim();
                        if (t === 'Through login/password') {
                            el.click();
                            return 'fallback:' + el.tagName;
                        }
                    }
                    return null;
                }""")
                print(f"[INFO] 点击切换入口: {clicked}", flush=True)
                page.wait_for_timeout(2000)
            except Exception as e:
                print(f"[WARN] 点击切换入口失败: {e}", flush=True)

            # 调试：页面上有几个 input
            try:
                n_inputs = page.evaluate("() => document.querySelectorAll('input').length")
                print(f"[INFO] 页面 input 数量: {n_inputs}", flush=True)
            except Exception:
                pass

            page.locator('input[placeholder="Username or Email"]').wait_for(state="visible", timeout=15000)
            page.locator('input[placeholder="Password"]').wait_for(state="visible", timeout=15000)
            page.fill('input[placeholder="Username or Email"]', user)
            page.fill('input[placeholder="Password"]', pwd)
            print("[INFO] 已填写账号密码，提交登录", flush=True)

            for sel in ['button[type="submit"]', 'button:has-text("Login")']:
                try:
                    btn = page.locator(sel).first
                    if btn.is_visible():
                        btn.click(timeout=5000)
                        break
                except:
                    pass

            page.wait_for_timeout(5000)

            for _ in range(5):
                for sel in ['button:has-text("Go to my server")', 'button:has-text("Skip")']:
                    try:
                        el = page.locator(sel)
                        if el.count() > 0 and el.first.is_visible():
                            el.first.click()
                            page.wait_for_timeout(2000)
                            break
                    except:
                        pass
                if '/server/' in page.url:
                    break
                page.wait_for_timeout(1000)

            print(f"[INFO] 登录后 URL: {page.url[:80]}", flush=True)

            if not short_id:
                if '/server/' in page.url:
                    parts = page.url.rstrip('/').split('/')
                    for i, p_part in enumerate(parts):
                        if p_part == 'server' and i+1 < len(parts) and len(parts[i+1]) == 8:
                            short_id = parts[i+1]
                            break
                if not short_id:
                    links = page.locator('a[href*="/server/"]')
                    for i in range(links.count()):
                        href = links.nth(i).get_attribute("href") or ""
                        parts = href.rstrip('/').split('/')
                        for j, p_part in enumerate(parts):
                            if p_part == 'server' and j+1 < len(parts) and len(parts[j+1]) == 8:
                                short_id = parts[j+1]
                                break
                        if short_id:
                            break

            if short_id and not bearer_token and not bearer_fallback:
                page.goto(f"{FRONT_BASE}/server/{short_id}", wait_until="domcontentloaded")
                page.wait_for_load_state("networkidle")
                page.wait_for_timeout(3000)

            if not full_uuid and short_id:
                try:
                    html = page.content()
                    m = re.search(
                        rf'{re.escape(short_id)}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{12}}',
                        html
                    )
                    if m:
                        full_uuid = m.group(0)
                except:
                    pass

            # 兜底：从 localStorage 找 token
            if not bearer_token and not bearer_fallback:
                try:
                    for key in ["token", "auth_token", "access_token", "api_token", "ptlc_token", "jwt"]:
                        val = page.evaluate(
                            f"() => {{ try {{ return localStorage.getItem('{key}'); }} catch(e) {{ return null; }} }}")
                        if val and len(str(val)) > 20:
                            bearer_fallback = str(val).strip()
                            print(f"[INFO] 从 localStorage 找到 token (key={key})", flush=True)
                            break
                except Exception as e:
                    print(f"[WARN] localStorage 读取失败: {e}", flush=True)

            # ptlc_ 优先，否则用备用 token
            final_token = bearer_token or bearer_fallback
            if final_token and not bearer_token:
                print("[INFO] 使用备用 Bearer token（非 ptlc_ 前缀）", flush=True)

            print(f"[INFO] Token: {'已获取' if final_token else '未获取'}, "
                  f"UUID: {'已获取' if full_uuid else '未获取'}, 短ID: {short_id or '未获取'}", flush=True)

            return final_token, full_uuid, short_id, fail_shot

        except Exception as e:
            print(f"[ERROR] 登录异常: {e}", flush=True)
            traceback.print_exc()
            try:
                fail_shot = snapshot("login_fail")
                page.screenshot(path=fail_shot)
                print(f"[INFO] 已保存失败截图: {fail_shot}", flush=True)
            except Exception:
                pass
            return None, None, None, fail_shot
        finally:
            context.close()
            browser.close()

# ---------- API 公共 headers ----------
def api_headers(bearer_token: str) -> dict:
    return {
        "Authorization": f"Bearer {bearer_token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Origin": FRONT_BASE,
        "Referer": f"{FRONT_BASE}/",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }

# ---------- 检查续期状态 ----------
def check_video_status(full_uuid: str, bearer_token: str) -> dict:
    url = f"{API_BASE}/servers/{full_uuid}/free-renewal/video/status?type=youtube_iter1&locale=en"
    try:
        resp = requests.get(url, headers=api_headers(bearer_token), timeout=30)
        return resp.json()
    except Exception as e:
        print(f"[ERROR] 状态查询失败: {e}", flush=True)
        return {}

# ---------- 调用 start API ----------
def call_video_start(full_uuid: str, bearer_token: str) -> dict:
    url = f"{API_BASE}/servers/{full_uuid}/free-renewal/video/start?locale=en"
    body = {"type": "youtube_iter1"}
    try:
        resp = requests.post(url, headers=api_headers(bearer_token), json=body, timeout=30)
        return resp.json()
    except Exception as e:
        print(f"[ERROR] Start API失败: {e}", flush=True)
        return {}

# ---------- 单次 update-time ----------
def call_update_once(full_uuid: str, bearer_token: str,
                     watch_uuid: str, renewal_id: int,
                     video_time: int) -> dict:
    url = f"{API_BASE}/servers/{full_uuid}/free-renewal/video/update-time?locale=en"
    body = {
        "uuid": watch_uuid,
        "renewal_uuid": watch_uuid,
        "renewal_id": renewal_id,
        "video_time_watched": video_time,
    }
    try:
        resp = requests.post(url, headers=api_headers(bearer_token), json=body, timeout=30)
        return resp.json()
    except Exception as e:
        print(f"[ERROR] update-time异常: {e}", flush=True)
        return {}

# ---------- 模拟视频观看 ----------
def simulate_video_watching(full_uuid: str, bearer_token: str,
                             watch_uuid: str, renewal_id: int) -> Tuple[bool, str]:
    steps = [30, 60, 90, 120, 150, 180, 210, 240]
    wait_seconds = 28
    total = len(steps) * wait_seconds

    print(f"[INFO] 开始续期（共{len(steps)}步 x {wait_seconds}s，预计{total}s）", flush=True)

    last_resp = {}
    for i, video_time in enumerate(steps):
        print(f"[INFO] 步骤 {i+1}/{len(steps)} 等待{wait_seconds}s → 上报{video_time}s", flush=True)
        time.sleep(wait_seconds)

        resp = call_update_once(full_uuid, bearer_token, watch_uuid, renewal_id, video_time)
        last_resp = resp

        success = resp.get("success", False)
        msg = resp.get("message", "")
        new_timer = resp.get("new_free_timer")

        if not success:
            if "Invalid time increment" in msg:
                current = resp.get("current_time", 0)
                if current > 0:
                    adjusted = current + 30
                    print(f"[WARN] 时间偏差，调整为{adjusted}s重试", flush=True)
                    time.sleep(2)
                    resp2 = call_update_once(full_uuid, bearer_token, watch_uuid, renewal_id, adjusted)
                    if resp2.get("success"):
                        last_resp = resp2
                        new_timer = resp2.get("new_free_timer")
                        steps[i+1:] = [adjusted + 30*(j+1) for j in range(len(steps)-i-1)]
                        if new_timer:
                            print(f"[INFO] 续期完成 new_free_timer={new_timer}", flush=True)
                            return True, new_timer
                        continue
                    return False, resp2.get("message", "重试失败")
            return False, msg

        if new_timer:
            print(f"[INFO] 续期完成 new_free_timer={new_timer}", flush=True)
            return True, new_timer

    new_timer = last_resp.get("new_free_timer")
    if last_resp.get("success"):
        return True, new_timer or "续期已提交"
    return False, last_resp.get("message", "未知错误")

# ---------- 获取 WS JWT ----------
def get_websocket_credentials(full_uuid: str, bearer_token: str) -> Tuple[Optional[str], Optional[str]]:
    url = f"{API_BASE}/servers/{full_uuid}/websocket?locale=en"
    try:
        resp = requests.get(url, headers=api_headers(bearer_token), timeout=30)
        if resp.status_code == 200:
            data = resp.json().get("data", resp.json())
            return data.get("token"), data.get("socket")
        print(f"[ERROR] WS凭证获取失败: {resp.status_code}", flush=True)
        return None, None
    except Exception as e:
        print(f"[ERROR] 获取WS凭证异常: {e}", flush=True)
        return None, None

# ---------- WebSocket 开机 ----------
async def ws_start_server(socket_url: str, jwt: str) -> str:
    print(f"[INFO] 连接 WebSocket...", flush=True)
    try:
        async with websockets.connect(
            socket_url,
            origin=FRONT_BASE,
            additional_headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
            },
            ping_interval=20,
            ping_timeout=20,
            close_timeout=10,
        ) as ws:
            await ws.send(json.dumps({"event": "auth", "args": [jwt]}))

            auth_success = False
            for _ in range(10):
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=10)
                    data = json.loads(msg)
                    if data.get("event") == "auth success":
                        auth_success = True
                        print("[INFO] WS认证成功", flush=True)
                        break
                except asyncio.TimeoutError:
                    break

            if not auth_success:
                print("[ERROR] WS认证失败", flush=True)
                return "auth_failed"

            current_status = "unknown"
            deadline = asyncio.get_event_loop().time() + 5
            while asyncio.get_event_loop().time() < deadline:
                remaining = deadline - asyncio.get_event_loop().time()
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=remaining)
                    data = json.loads(msg)
                    event = data.get("event", "")

                    if event == "status":
                        current_status = data.get("args", ["unknown"])[0]
                        print(f"[INFO] 服务器当前状态: {current_status}", flush=True)
                        break
                    elif event == "stats":
                        try:
                            state = json.loads(data["args"][0]).get("state", "")
                            if state:
                                current_status = state
                                print(f"[INFO] 服务器当前状态: {current_status}", flush=True)
                                break
                        except Exception:
                            pass
                except asyncio.TimeoutError:
                    break
                except Exception:
                    break

            if current_status in ("running", "starting"):
                print(f"[INFO] 服务器已在运行中（{current_status}），无需开机", flush=True)
                return "already_running"

            await ws.send(json.dumps({"event": "set state", "args": ["start"]}))
            print("[INFO] 已发送开机指令，等待启动...", flush=True)

            for _ in range(60):
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=5)
                    data = json.loads(msg)
                    event = data.get("event", "")

                    if event == "status":
                        new_status = data.get("args", [""])[0]
                        print(f"[INFO] 状态变更: {new_status}", flush=True)
                        if new_status in ("starting", "running"):
                            print(f"[INFO] 服务器启动成功（{new_status}）", flush=True)
                            return "started"
                    elif event == "stats":
                        try:
                            state = json.loads(data["args"][0]).get("state", "")
                            if state in ("starting", "running"):
                                print(f"[INFO] 服务器启动成功（{state}）", flush=True)
                                return "started"
                        except Exception:
                            pass
                except asyncio.TimeoutError:
                    continue
                except Exception:
                    break

            print("[WARN] 等待超时，开机指令已发送", flush=True)
            return "sent"

    except Exception as e:
        print(f"[ERROR] WebSocket异常: {e}", flush=True)
        return "error"


def start_server_via_ws(full_uuid: str, bearer_token: str) -> str:
    jwt, socket_url = get_websocket_credentials(full_uuid, bearer_token)
    if not jwt or not socket_url:
        print("[ERROR] 无法获取WS凭证", flush=True)
        return "error"
    try:
        return asyncio.run(ws_start_server(socket_url, jwt))
    except Exception as e:
        print(f"[ERROR] WS开机异常: {e}", flush=True)
        return "error"

# ---------- 单账号主流程 ----------
def process_account(key: str, proxy: str = None) -> bool:
    raw = os.environ.get(key, "").strip()
    if not raw:
        return True

    try:
        parts = raw.split("-----")
        user = parts[0].strip()
        pwd = parts[1].strip()
    except Exception:
        print(f"[ERROR] {key} 格式错误", flush=True)
        notify_tg(False, error_msg="格式错误")
        return False

    masked = mask_email(user)
    print(f"\n{'='*60}", flush=True)
    print(f"[INFO] 处理 {key} ({masked})", flush=True)
    print(f"{'='*60}", flush=True)

    # 1. 登录
    login_result = login_and_get_token(user, pwd, proxy)
    bearer_token, full_uuid, short_id = login_result[0], login_result[1], login_result[2]
    fail_shot = login_result[3] if len(login_result) > 3 else None
    if not bearer_token:
        print("[ERROR] 未能获取 Bearer Token", flush=True)
        notify_tg(False, email=user, error_msg="未能获取 Bearer Token", screenshot=fail_shot)
        return False
    if not full_uuid:
        print("[ERROR] 未能获取服务器 UUID", flush=True)
        notify_tg(False, email=user, error_msg="未能获取服务器 UUID", screenshot=fail_shot)
        return False

    masked_server = mask_server(short_id)
    print(f"[INFO] 登录成功 | 服务器: {masked_server}", flush=True)

    # 2. 检查续期状态
    status = check_video_status(full_uuid, bearer_token)
    can_watch = status.get("can_watch", None)
    time_until_next = status.get("time_until_next_video") or 0

    renew_ok = False
    cooldown_after = ""

    if not can_watch and time_until_next > 0:
        h = time_until_next // 3600
        m = (time_until_next % 3600) // 60
        cooldown_after = f"{h}h {m}m"
        print(f"[INFO] 已在冷却期，下次可续期: {cooldown_after}", flush=True)
        renew_ok = True
    else:
        # 3. 续期
        start_resp = call_video_start(full_uuid, bearer_token)
        if not start_resp.get("success"):
            err = start_resp.get("message", "start API失败")
            print(f"[ERROR] 续期启动失败: {err}", flush=True)
            notify_tg(False, email=user, server=short_id, error_msg=err)
            return False

        watch_uuid = start_resp.get("uuid", "")
        renewal_id = start_resp.get("renewal_id", 0)
        print(f"[INFO] 续期会话已建立 (renewal_id={renewal_id})", flush=True)

        renew_ok, result = simulate_video_watching(full_uuid, bearer_token, watch_uuid, renewal_id)

        if not renew_ok:
            print(f"[ERROR] 续期失败: {result}", flush=True)
            notify_tg(False, email=user, server=short_id, error_msg=result)
            return False

        time.sleep(2)
        status_after = check_video_status(full_uuid, bearer_token)
        time_until = status_after.get("time_until_next_video") or 0
        h = time_until // 3600
        m_min = (time_until % 3600) // 60
        cooldown_after = f"{h}h {m_min}m" if time_until > 0 else result
        print(f"[INFO] 续期成功，下次可续期: {cooldown_after}", flush=True)

    # 4. WS 开机
    print(f"[INFO] ── 开机 ──", flush=True)
    start_result = start_server_via_ws(full_uuid, bearer_token)

    start_note_map = {
        "already_running": "服务器已在运行中，无需开机",
        "started":         "开机成功",
        "sent":            "开机指令已发送（等待超时）",
        "auth_failed":     "WS认证失败",
        "error":           "开机异常",
    }
    start_note = start_note_map.get(start_result, "未知状态")
    print(f"[INFO] {start_note}", flush=True)

    # 5. TG 通知（使用真实邮箱和服务器ID）
    notify_tg(
        ok=renew_ok,
        email=user,
        server=short_id,
        after=f"{cooldown_after}\n{start_note}",
    )

    print(f"[INFO] {key} 处理完成", flush=True)
    return renew_ok

def main():
    proxy = os.environ.get("PROXY_SERVER", "")
    if proxy:
        print(f"[INFO] 使用代理", flush=True)

    accounts = [f"GODLIKE_{i}" for i in range(1, 6)]
    all_ok = True
    for idx, acc in enumerate(accounts):
        try:
            ok = process_account(acc, proxy if proxy else None)
            if not ok:
                all_ok = False
        except Exception as e:
            print(f"[FATAL] {acc} 崩溃: {e}", flush=True)
            traceback.print_exc()
            notify_tg(False, email=acc, error_msg=f"脚本崩溃: {str(e)[:200]}")
            all_ok = False
        if idx < len(accounts) - 1:
            time.sleep(random.randint(5, 15))

    if all_ok:
        print("\n[INFO] 所有账号处理成功", flush=True)
        sys.exit(0)
    else:
        print("\n[ERROR] 部分账号失败", flush=True)
        sys.exit(1)

if __name__ == "__main__":
    main()
