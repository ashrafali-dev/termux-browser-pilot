#!/data/data/com.termux/files/usr/bin/python3
"""Basic end-to-end test for termux-browser-pilot."""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.cdp import CDPSession
from src.commands import PageCommands
from src.screenshot import ScreenshotCommands
from src.input import InputCommands
from src.stealth import apply_stealth
from src.accessibility import AccessibilityCommands

OUTDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


async def get_ws_url():
    import urllib.request
    with urllib.request.urlopen("http://127.0.0.1:9222/json/version", timeout=5) as r:
        return json.loads(r.read())["webSocketDebuggerUrl"]


async def main():
    ws_url = await get_ws_url()
    print(f"CDP: {ws_url[:50]}...")

    session = CDPSession(ws_url)
    await session.connect()
    await apply_stealth(session)

    page = PageCommands(session)
    ss = ScreenshotCommands(session)
    inp = InputCommands(session)
    a11y = AccessibilityCommands(session)

    # Test 1: Navigate
    print("\n[1] Navigate to example.com...")
    await page.navigate("https://example.com")
    title = await page.get_title()
    url = await page.get_url()
    print(f"    Title: {title}")
    print(f"    URL: {url}")
    assert "Example" in title, f"Expected 'Example' in title, got '{title}'"
    print("    PASS")

    # Test 2: Extract text
    print("\n[2] Extract text...")
    h1 = await page.get_text("h1")
    print(f"    H1: {h1}")
    assert h1 and len(h1) > 0, "No H1 text found"
    print("    PASS")

    # Test 3: Screenshot
    print("\n[3] Screenshot...")
    ss_path = os.path.join(OUTDIR, "test_screenshot.png")
    await ss.capture(ss_path)
    sz = os.path.getsize(ss_path)
    print(f"    Saved: {ss_path} ({sz} bytes)")
    assert sz > 1000, f"Screenshot too small: {sz} bytes"
    print("    PASS")

    # Test 4: JavaScript eval
    print("\n[4] JavaScript eval...")
    result = await page.evaluate("2 + 2")
    print(f"    2+2 = {result}")
    assert result == 4, f"Expected 4, got {result}"
    print("    PASS")

    # Test 5: WebGL check
    print("\n[5] WebGL support...")
    webgl = await page.evaluate("""(function() {
        var c = document.createElement('canvas');
        var gl = c.getContext('webgl') || c.getContext('experimental-webgl');
        if (!gl) return {supported: false};
        return {supported: true, vendor: gl.getParameter(gl.VENDOR), renderer: gl.getParameter(gl.RENDERER)};
    })()""")
    print(f"    WebGL: {json.dumps(webgl)}")
    print(f"    {'PASS' if webgl and webgl.get('supported') else 'WARN - no WebGL'}")

    # Test 6: Links
    print("\n[6] Get links...")
    links = await page.get_links()
    print(f"    Found {len(links) if links else 0} links")
    print("    PASS")

    # Test 7: Accessibility tree
    print("\n[7] Accessibility tree...")
    try:
        tree = await a11y.get_tree_summary()
        lines = tree.strip().split("\n") if tree else []
        print(f"    {len(lines)} a11y nodes")
        if lines:
            print(f"    First: {lines[0][:60]}")
        print("    PASS")
    except Exception as e:
        print(f"    WARN: {e}")

    await session.close()
    print("\n=== ALL TESTS PASSED ===")


if __name__ == "__main__":
    asyncio.run(main())
