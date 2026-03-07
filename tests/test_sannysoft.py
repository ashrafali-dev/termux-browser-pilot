#!/data/data/com.termux/files/usr/bin/python3
"""Test bot detection on sannysoft with minimal stealth."""
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

    # First check native values
    fp = await page.evaluate("""(function() {
        return {
            webdriver: navigator.webdriver,
            platform: navigator.platform,
            ua: navigator.userAgent,
            plugins: navigator.plugins.length,
            pluginType: navigator.plugins instanceof PluginArray,
        };
    })()""")
    print(f"Pre-check: {json.dumps(fp)}")

    # Navigate to sannysoft
    print("Navigating to bot.sannysoft.com...")
    await page.navigate("https://bot.sannysoft.com")
    await page.wait(4)
    title = await page.get_title()
    print(f"Title: {title}")

    # Take screenshot
    await ss.capture(os.path.join(OUTDIR, "sannysoft_clean.png"), full_page=True)
    print("Screenshot: sannysoft_clean.png")

    # Check WebGL
    webgl = await page.evaluate("""(function() {
        var c = document.createElement('canvas');
        var gl = c.getContext('webgl') || c.getContext('experimental-webgl');
        if (!gl) return {supported: false};
        var dbg = gl.getExtension('WEBGL_debug_renderer_info');
        return {
            supported: true,
            unmaskedVendor: dbg ? gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL) : null,
            unmaskedRenderer: dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : null,
        };
    })()""")
    print(f"WebGL: {json.dumps(webgl)}")

    await s.close()

asyncio.run(main())
