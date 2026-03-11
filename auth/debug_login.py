"""
Debug script: runs Playwright login step-by-step and takes screenshots.
Run with: .venv/bin/python auth/debug_login.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import get_settings


async def debug_login():
    from playwright.async_api import async_playwright
    import pyotp

    settings = get_settings()
    api_key = settings.kite_api_key
    user_id = settings.zerodha_user_id
    password = settings.zerodha_password
    totp_secret = settings.zerodha_totp_secret

    login_url = f"https://kite.zerodha.com/connect/login?api_key={api_key}&v=3"
    screenshots = Path("./logs/debug_screenshots")
    screenshots.mkdir(parents=True, exist_ok=True)

    captured_token = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        # Non-blocking observer — just watch all requests
        def on_request(request):
            url = request.url
            if "request_token" in url:
                import re
                match = re.search(r"request_token=([A-Za-z0-9]+)", url)
                if match:
                    captured_token.append(match.group(1))
                    print(f"\n✅ REQUEST OBSERVED with request_token: {match.group(1)[:12]}...")

        page.on("request", on_request)

        print(f"[1] Navigating to: {login_url}")
        await page.goto(login_url, timeout=30_000)
        await page.screenshot(path=str(screenshots / "01_login_page.png"))

        print("[2] Filling credentials and submitting...")
        await page.fill('input[type="text"]', user_id)
        await page.fill('input[type="password"]', password)

        try:
            async with page.expect_navigation(wait_until="domcontentloaded", timeout=20_000):
                await page.click('button[type="submit"]')
        except Exception as e:
            print(f"    Nav exception (ok): {e}")

        await page.screenshot(path=str(screenshots / "02_after_creds.png"))
        print(f"[2] URL: {page.url}")

        print("[3] Waiting for TOTP input...")
        await page.wait_for_selector('input[type="number"]', timeout=20_000)
        otp = pyotp.TOTP(totp_secret).now()
        print(f"[3] OTP: {otp}")
        await page.fill('input[type="number"]', otp)
        await page.screenshot(path=str(screenshots / "03_totp_filled.png"))

        print("[3] Submitting TOTP and watching for redirect...")

        # Watch for failed navigations too (connection refused to 127.0.0.1)
        try:
            async with page.expect_navigation(wait_until="domcontentloaded", timeout=20_000):
                await page.click('button[type="submit"]')
        except Exception as e:
            print(f"    Nav exception after TOTP (could be redirect refused): {type(e).__name__}: {str(e)[:100]}")

        await asyncio.sleep(2)
        await page.screenshot(path=str(screenshots / "04_after_totp.png"))
        print(f"[3] URL after TOTP: {page.url}")

        # Check for error messages on the page
        error_text = ""
        for selector in ['.error', '.alert', '[class*="error"]', '[class*="Error"]']:
            try:
                el = page.locator(selector).first
                if await page.locator(selector).count() > 0:
                    error_text = await el.inner_text()
                    print(f"    Error element ({selector}): {error_text}")
            except Exception:
                pass

        # Print page text to see what's shown
        try:
            body_text = await page.locator('body').inner_text()
            print(f"\n[PAGE TEXT]\n{body_text[:1000]}")
        except Exception:
            pass

        print(f"\n[4] Captured tokens: {captured_token}")
        print(f"[4] Screenshots in: ./logs/debug_screenshots/")

        if captured_token:
            print(f"\n✅ SUCCESS: request_token = {captured_token[0]}")
        else:
            print("\n❌ No request_token captured — redirect URL may not be set correctly")
            print("   Go to https://developers.kite.trade/ → your app → set Redirect URL = http://127.0.0.1")

        await asyncio.sleep(3)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(debug_login())
