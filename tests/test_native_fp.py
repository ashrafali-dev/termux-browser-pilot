#!/data/data/com.termux/files/usr/bin/python3
"""Check native browser fingerprint WITHOUT any stealth overrides."""
import asyncio, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.cdp import CDPSession

CHECKS = """(function() {
    var result = {};
    result.userAgent = navigator.userAgent;
    result.platform = navigator.platform;
    result.webdriver = navigator.webdriver;
    result.webdriverType = typeof navigator.webdriver;
    result.webdriverDescriptor = JSON.stringify(
        Object.getOwnPropertyDescriptor(Navigator.prototype, 'webdriver')
    );
    result.hardwareConcurrency = navigator.hardwareConcurrency;
    result.deviceMemory = navigator.deviceMemory;
    result.maxTouchPoints = navigator.maxTouchPoints;
    result.languages = navigator.languages;
    result.language = navigator.language;
    result.cookieEnabled = navigator.cookieEnabled;
    result.doNotTrack = navigator.doNotTrack;
    result.pluginsLength = navigator.plugins.length;
    result.pluginNames = Array.from(navigator.plugins).map(p => p.name);
    result.pluginsIsPluginArray = navigator.plugins instanceof PluginArray;
    result.mimeTypesLength = navigator.mimeTypes.length;
    result.chromeExists = typeof window.chrome !== 'undefined';
    result.chromeRuntime = typeof window.chrome?.runtime;
    result.screenWidth = screen.width;
    result.screenHeight = screen.height;
    result.colorDepth = screen.colorDepth;
    result.pixelRatio = window.devicePixelRatio;

    // WebGL
    var c = document.createElement('canvas');
    var gl = c.getContext('webgl') || c.getContext('experimental-webgl');
    if (gl) {
        result.webglSupported = true;
        result.webglVendor = gl.getParameter(gl.VENDOR);
        result.webglRenderer = gl.getParameter(gl.RENDERER);
        var dbg = gl.getExtension('WEBGL_debug_renderer_info');
        if (dbg) {
            result.webglUnmaskedVendor = gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL);
            result.webglUnmaskedRenderer = gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL);
        }
        result.webglVersion = gl.getParameter(gl.VERSION);
    } else {
        result.webglSupported = false;
    }

    // Canvas
    var c2 = document.createElement('canvas');
    c2.width = 200; c2.height = 50;
    var ctx = c2.getContext('2d');
    ctx.textBaseline = 'top';
    ctx.font = '14px Arial';
    ctx.fillText('Canvas fingerprint', 2, 2);
    result.canvasHash = c2.toDataURL().length;

    return result;
})()"""

async def main():
    import urllib.request
    with urllib.request.urlopen("http://127.0.0.1:9222/json/version", timeout=5) as r:
        ws_url = json.loads(r.read())["webSocketDebuggerUrl"]

    s = CDPSession(ws_url)
    await s.connect()

    # DO NOT apply stealth - check native values
    page_from_cdp = await s.send("Runtime.evaluate", {
        "expression": CHECKS,
        "returnByValue": True,
    })
    fp = page_from_cdp.get("result", {}).get("value", {})
    print(json.dumps(fp, indent=2))
    await s.close()

asyncio.run(main())
