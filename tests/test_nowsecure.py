#!/data/data/com.termux/files/usr/bin/python3
"""Test Cloudflare bypass on nowsecure.nl and bot.incolumitas.com."""
import asyncio, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.cdp import CDPSession
from src.commands import PageCommands
from src.screenshot import ScreenshotCommands
from src.stealth import apply_stealth

OUTDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

async def main():
    import urllib.request
    with urllib.request.urlopen("http://127.0.0.1:9222/json/version", timeout=5) as r:
        ws_url = json.loads(r.read())["webSocketDebuggerUrl"]

    s = CDPSession(ws_url)
    await s.connect()
    await apply_stealth(s)
    page = PageCommands(s)
    ss = ScreenshotCommands(s)

    # Test 1: nowsecure.nl (Cloudflare protected)
    print("=== Test: nowsecure.nl ===")
    await page.navigate("https://nowsecure.nl", timeout=45)
    await page.wait(5)
    title = await page.get_title()
    url = await page.get_url()
    print(f"Title: {title}")
    print(f"URL: {url}")
    await ss.capture(os.path.join(OUTDIR, "nowsecure_result.png"))
    print("Screenshot: nowsecure_result.png")

    # Check if Cloudflare challenge or success
    body = await page.get_text()
    if body:
        print(f"Body preview: {body[:200]}")

    # Test 2: bot.incolumitas.com
    print("\n=== Test: bot.incolumitas.com ===")
    await page.navigate("https://bot.incolumitas.com", timeout=45)
    await page.wait(8)
    title2 = await page.get_title()
    url2 = await page.get_url()
    print(f"Title: {title2}")
    print(f"URL: {url2}")
    await ss.capture(os.path.join(OUTDIR, "incolumitas_result.png"), full_page=True)
    print("Screenshot: incolumitas_result.png")

    body2 = await page.get_text()
    if body2:
        print(f"Body preview: {body2[:300]}")

    await s.close()

asyncio.run(main())
