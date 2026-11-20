import http.server
import os
import re
import socketserver
import sys
import threading
import time
from playwright.sync_api import sync_playwright
import random

"""手机号+密码"""
PHONE_NUMBER = os.getenv("LIDL_USER", "")
PASSWORD = os.getenv("LIDL_PASS", "")

if not PHONE_NUMBER or not PASSWORD:
    print("❌ 错误：未读取到环境变量 LIDL_USER 或 LIDL_PASS！")
    sys.exit(1)

TARGET_URL = "https://kundenkonto.lidl-connect.de/mein-lidl-connect/uebersicht.html"
MIN_INTERVAL = 1500
MAX_INTERVAL = 1800


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    """屏蔽 HTTP 访问日志"""
    def log_message(self, format, *args):
        pass


def start_dummy_server():
    """后台启动极简 HTTP 服务， Render Web Service 的端口健康检查"""
    port = int(os.getenv("PORT", 8080))
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("", port), QuietHandler) as httpd:
        print(f"   ✅ 检查端口已启动 (Port: {port})")
        httpd.serve_forever()


def handle_cookie_banner(page):
    """处理 Cookie 弹窗"""
    try:
        cookie_btn = page.locator("button").filter(has_text=re.compile(r"akzeptieren|zustimmen|accept", re.I)).first
        if cookie_btn.is_visible(timeout=3000):
            cookie_btn.click()
            print("   ✅ 已点击处理 Cookie 弹窗")
            time.sleep(1)
    except Exception:
        pass


def do_login_with_verification(page) -> bool:
    """带状态校验的登录流程"""
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] ✅ 开始执行登录流程...")

    handle_cookie_banner(page)

    phone_input = page.get_by_role("textbox", name="Mobilfunknummer")
    pass_input = page.get_by_role("textbox", name="Passwort")

    try:
        phone_input.wait_for(state="visible", timeout=8000)
        pass_input.wait_for(state="visible", timeout=8000)
    except Exception:
        print("   ❌ 无法定位登录输入框。")
        return False

    phone_input.click()
    phone_input.fill(PHONE_NUMBER)
    phone_input.dispatch_event("input")

    pass_input.click()
    pass_input.fill(PASSWORD)
    pass_input.dispatch_event("input")

    # if phone_input.input_value() != PHONE_NUMBER or len(pass_input.input_value()) == 0:
    #     print("   ❌ 账号或密码填充校验失败")
    #     return False

    print("正在提交登录...")
    login_btn = page.get_by_role("button", name="Einloggen")
    if login_btn.is_visible():
        login_btn.click()
    else:
        pass_input.press("Enter")

    refill_btn = page.get_by_role("button", name="Datenvolumen per Refill")
    try:
        refill_btn.wait_for(state="visible", timeout=15000)
        print("   ✅ 登录成功，检测到 'Datenvolumen per Refill' 按钮。")
        return True
    except Exception:
        print("   ❌ 登录提交后 15 秒内未检测到 Refill 按钮。")
        return False


def get_remaining_data_gb(page) -> float | None:
    """提取剩余流量数值"""
    try:
        elements = page.get_by_text(re.compile(r"\b\d+[,\.]\d+\s*GB\b")).all()
        target_text = None
        for el in elements:
            if el.is_visible():
                text = el.inner_text().strip()
                if len(text) < 15 and "Wochen" not in text and "€" not in text:
                    target_text = text
                    break

        if target_text:
            match = re.search(r"(\d+[,\.]?\d*)", target_text)
            if match:
                num_str = match.group(1).replace(",", ".")
                return float(num_str)
    except Exception as e:
        print(f"   ⚠️ 提取流量数值时异常: {e}")
    return None


def run_once(page):
    """单次检测流程"""
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n[{timestamp}] 🔄 开始访问 Lidl Connect...")

    page.goto(TARGET_URL, wait_until="domcontentloaded")
    time.sleep(2)

    # 检查是否触发维护页
    if page.get_by_text(re.compile(r"gewartet|Wartung", re.I)).is_visible(timeout=2000):
        print("   ⚠️ 警告：当前页面显示维护/拦截页面，跳过本轮检测。")
        return

    handle_cookie_banner(page)

    refill_btn = page.get_by_role("button", name="Datenvolumen per Refill")

    # 没按钮先走登录
    if not refill_btn.is_visible(timeout=3000):
        success = do_login_with_verification(page)
        if not success:
            print("   ⚠️ 本轮登录未成功，等待下次循环重试。")
            return
        refill_btn = page.get_by_role("button", name="Datenvolumen per Refill")

    # 检查数值
    if refill_btn.is_visible(timeout=2000):
        remaining_gb = get_remaining_data_gb(page)
        if remaining_gb is not None:
            print(f"   📊 当前剩余流量: {remaining_gb:.2f} GB")
            if remaining_gb < 1:
                print("   ⚡ 流量小于 1 GB，触发 Refill 点击！")
                refill_btn.click()
                print("   ✅ 已成功点击 'Datenvolumen per Refill'！")
                time.sleep(3)
            else:
                print("   👌 流量充足（ 1 GB），无需重置。")
        else:
            print("   ⚠️ 无法提取流量数值。")
    else:
        print("   ❌ 最终未找到 Refill 按钮。")


def main():
    # 1. 在后台线程启动 HTTP 端口响应 Render 健康检查
    threading.Thread(target=start_dummy_server, daemon=True).start()
    print("🚀 Lidl Connect 常驻自动监测服务已启动...")

    # 2. 每次循环独立启动并关闭浏览器，防止 Render 512MB 内存溢出
    while True:
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    headless=True,
                    slow_mo=500,
                    args=["--disable-blink-features=AutomationControlled"],
                )
                context = browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                    locale="de-DE",
                    viewport={"width": 1280, "height": 800},
                )
                page = context.new_page()
                run_once(page)
                browser.close()
        except Exception as e:
            print(f"⚠️ 本轮运行出现异常（将在下一次循环重试）: {e}")

        # 在最小和最大间隔之间随机生成一个等待时间
        sleep_time = random.randint(MIN_INTERVAL, MAX_INTERVAL)
        print(f"   ⏳ 本轮检测结束，将在 {sleep_time // 60} 分 {sleep_time % 60} 秒后进行下一次检测...")
        time.sleep(sleep_time)

if __name__ == "__main__":
    main()