#!/data/data/com.termux/files/usr/bin/python3
"""Test WebGL support."""
import asyncio, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.cdp import CDPSession
from src.stealth import apply_stealth
from src.commands import PageCommands
from src.screenshot import ScreenshotCommands

WEBGL_CHECK = """(function() {
    var c = document.createElement('canvas');
    var gl = c.getContext('webgl') || c.getContext('experimental-webgl');
    if (!gl) return {supported: false};
    var dbg = gl.getExtension('WEBGL_debug_renderer_info');
    return {
        supported: true,
        vendor: gl.getParameter(gl.VENDOR),
        renderer: gl.getParameter(gl.RENDERER),
        unmaskedVendor: dbg ? gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL) : null,
        unmaskedRenderer: dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : null,
        version: gl.getParameter(gl.VERSION)
    };
})()"""

async def main():
    import urllib.request
    with urllib.request.urlopen("http://127.0.0.1:9222/json/version", timeout=5) as r:
        ws_url = json.loads(r.read())["webSocketDebuggerUrl"]

    s = CDPSession(ws_url)
    await s.connect()
    await apply_stealth(s)
    page = PageCommands(s)
    ss = ScreenshotCommands(s)

    await page.navigate("https://example.com")
    print("Title:", await page.get_title())

    webgl = await page.evaluate(WEBGL_CHECK)
    print("WebGL:", json.dumps(webgl, indent=2))

    # Test on bot.sannysoft.com
    print("\nNavigating to bot.sannysoft.com...")
    await page.navigate("https://bot.sannysoft.com")
    await page.wait(3)
    title = await page.get_title()
    print(f"Title: {title}")
    outdir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    await ss.capture(os.path.join(outdir, "fingerprint_sannysoft.png"), full_page=True)
    print("Screenshot saved: fingerprint_sannysoft.png")

    await s.close()

asyncio.run(main())
