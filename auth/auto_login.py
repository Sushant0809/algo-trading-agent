"""
Playwright-based automation for Zerodha KiteConnect daily OAuth login.
Flow: login URL → credentials → TOTP → capture request_token from redirect to 127.0.0.1

PREREQUISITE: Set Redirect URL to http://127.0.0.1 in your KiteConnect app at
https://developers.kite.trade/
"""
from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import parse_qs, urlparse

import pyotp

logger = logging.getLogger(__name__)

KITE_LOGIN_URL = "https://kite.zerodha.com/connect/login"


async def get_request_token_via_playwright(
    api_key: str,
    user_id: str,
    password: str,
    totp_secret: str,
    redirect_url: str = "http://127.0.0.1",
    headless: bool = True,
    timeout_ms: int = 60_000,
) -> str:
    from playwright.async_api import async_playwright

    login_url = f"{KITE_LOGIN_URL}?api_key={api_key}&v=3"
    totp_gen = pyotp.TOTP(totp_secret)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context()
        page = await context.new_page()

        # Non-blocking listener — fires for every request including navigations
        captured_token: list[str] = []

        def on_request(request):
            if "request_token" in request.url:
                token = _extract_token(request.url)
                if token and token not in captured_token:
                    captured_token.append(token)
                    logger.info(f"Captured request_token: {token[:8]}...")

        page.on("request", on_request)

        # Also abort requests to redirect_url to prevent chrome-error page
        await context.route(f"{redirect_url}/**", lambda route, _: route.abort())
        await context.route(f"{redirect_url}*", lambda route, _: route.abort())

        # --- Step 1: Navigate ---
        logger.info(f"Navigating to: {login_url}")
        await page.goto(login_url, timeout=timeout_ms)

        # --- Step 2: Credentials ---
        await page.wait_for_selector('input[type="text"]', timeout=timeout_ms)
        await page.fill('input[type="text"]', user_id)
        await page.fill('input[type="password"]', password)
        try:
            async with page.expect_navigation(wait_until="domcontentloaded", timeout=20_000):
                await page.click('button[type="submit"]')
        except Exception:
            pass
        logger.info("Credentials submitted.")

        # --- Step 3: TOTP ---
        await page.wait_for_selector('input[type="number"]', timeout=timeout_ms)
        otp_code = totp_gen.now()
        logger.info(f"Entering TOTP: {otp_code}")
        await page.fill('input[type="number"]', otp_code)

        # Use expect_request to capture the redirect URL containing request_token
        try:
            async with page.expect_request(
                lambda r: "request_token" in r.url, timeout=20_000
            ) as req_info:
                await page.click('button[type="submit"]')

            req = await req_info.value
            token = _extract_token(req.url)
            if token:
                await browser.close()
                logger.info(f"request_token captured via expect_request: {token[:8]}...")
                return token
        except Exception as e:
            logger.debug(f"expect_request approach: {e}")

        logger.info("TOTP submitted, polling for token...")

        # Fallback: poll captured_token from the on_request listener
        for _ in range(20):
            if captured_token:
                await browser.close()
                return captured_token[0]
            await asyncio.sleep(1)

        final_url = page.url
        token = _extract_token(final_url)
        if token:
            await browser.close()
            return token

        await browser.close()
        raise RuntimeError(
            f"Could not capture request_token. Final URL: {final_url}\n"
            "Ensure Redirect URL = 'http://127.0.0.1' at https://developers.kite.trade/"
        )


def _extract_token(url: str) -> str | None:
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    if "request_token" in params:
        return params["request_token"][0]
    match = re.search(r"request_token=([A-Za-z0-9]+)", url)
    return match.group(1) if match else None
