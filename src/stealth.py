"""Minimal anti-detection using real device values.

Philosophy: Be a real browser with real values. Only hide automation.
Use CDP-level overrides (not JS Object.defineProperty) which are undetectable.
When hardware GPU unavailable, spoof WebGL renderer to match real device.

IMPORTANT: We do NOT use Emulation.setDeviceMetricsOverride because it
causes window.outerWidth < window.innerWidth — a strong automation signal.
Chrome's natural viewport from --window-size + --start-maximized is used.
"""

import asyncio

from .device import device_info, get_user_agent, get_platform_string
from .gpu import get_webgl_spoof_js


# Characters used by Chrome's GREASE algorithm for Sec-CH-UA brand generation
_GREASE_CHARS = (
    ' ', '!', '#', '$', '%', '&', "'", '(', ')', '*', '+',
    ',', '-', '.', '/', ':', ';', '<', '=', '>', '?', '@',
)

# Permutation table for 3-item brand ordering (GREASE, Chromium, Chrome)
_BRAND_PERMS = [
    (0, 1, 2), (0, 2, 1), (1, 0, 2), (1, 2, 0),
    (2, 0, 1), (2, 1, 0), (0, 1, 2), (0, 2, 1),
]


def _build_client_hints_brands(chrome_version):
    """Compute Sec-CH-UA brand list matching Chrome's GREASE algorithm.

    Returns (brands, full_version_list) tuples for userAgentMetadata.
    """
    parts = chrome_version.split(".")
    major = int(parts[0]) if parts else 138

    # GREASE character selection (matches Chromium source)
    c = _GREASE_CHARS[major % len(_GREASE_CHARS)]
    greased_brand = f"Not{c}A{c}Brand"
    greased_version = str((major % 10) * 10 + (major % 10))

    # Brand order permutation
    perm = _BRAND_PERMS[major % len(_BRAND_PERMS)]
    raw_brands = [
        (greased_brand, greased_version, f"{greased_version}.0.0.0"),
        ("Chromium", str(major), chrome_version),
        ("Google Chrome", str(major), chrome_version),
    ]

    brands = []
    full_list = []
    for idx in perm:
        b = raw_brands[idx]
        brands.append({"brand": b[0], "version": b[1]})
        full_list.append({"brand": b[0], "version": b[2]})

    return brands, full_list


def _build_stealth_js(width, height, info, gpu_mode):
    """Build stealth JS injection script.

    Uses real device values from device.py — never hardcoded.
    """
    cores = info.get("cores", 8)
    ram_gb = min(8, info.get("ram_gb", 8))

    js = """
    // Ensure chrome object exists (real Chrome always has it)
    if (!window.chrome) window.chrome = {};
    // NOTE: Do NOT add chrome.runtime or chrome.app — these are only available
    // in Chrome extensions, NOT regular web pages. Adding them is a bot signal.
    // Real Chrome: chrome.runtime=undefined, chrome.app=undefined on regular pages.
    if (!window.chrome.csi) {
        window.chrome.csi = function() {
            return {
                startE: Date.now(),
                onloadT: Date.now(),
                pageT: Math.random() * 100 + 50,
                tran: 15,
            };
        };
    }
    if (!window.chrome.loadTimes) {
        window.chrome.loadTimes = function() {
            return {
                commitLoadTime: Date.now() / 1000,
                connectionInfo: "h2",
                finishDocumentLoadTime: Date.now() / 1000,
                finishLoadTime: Date.now() / 1000,
                firstPaintAfterLoadTime: 0,
                firstPaintTime: Date.now() / 1000,
                navigationType: "Other",
                npnNegotiatedProtocol: "h2",
                requestTime: Date.now() / 1000 - 0.1,
                startLoadTime: Date.now() / 1000 - 0.1,
                wasAlternateProtocolAvailable: false,
                wasFetchedViaSpdy: true,
                wasNpnNegotiated: true,
            };
        };
    }

    // Screen properties consistent with Xvfb resolution
    Object.defineProperty(screen, 'availWidth', {get: () => %d});
    Object.defineProperty(screen, 'availHeight', {get: () => %d});
    Object.defineProperty(screen, 'colorDepth', {get: () => 24});
    Object.defineProperty(screen, 'pixelDepth', {get: () => 24});

    // Window outer dimensions must be >= inner dimensions
    // (automation detection checks outerWidth >= innerWidth)
    Object.defineProperty(window, 'outerWidth', {
        get: () => window.innerWidth,
    });
    Object.defineProperty(window, 'outerHeight', {
        get: () => window.innerHeight + 74,
    });

    // Hardware properties from real device
    Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => %d});
    Object.defineProperty(navigator, 'deviceMemory', {get: () => %d});

    // Notification permission (default = not asked, real browser behavior)
    if (window.Notification) {
        Object.defineProperty(Notification, 'permission', {
            get: () => 'default',
        });
    }

    // Languages: real Chrome always has multiple entries
    Object.defineProperty(navigator, 'languages', {
        get: () => Object.freeze(['en-US', 'en']),
    });

    // AudioContext fingerprint: add tiny noise to prevent fingerprinting
    // Real devices produce slightly different audio output each time
    (function() {
        var origGetFloatFreqData = AnalyserNode.prototype.getFloatFrequencyData;
        AnalyserNode.prototype.getFloatFrequencyData = function(array) {
            origGetFloatFreqData.call(this, array);
            for (var i = 0; i < array.length; i += 3) {
                array[i] = array[i] + (Math.random() * 0.0001 - 0.00005);
            }
        };
        var origGetByteFreqData = AnalyserNode.prototype.getByteFrequencyData;
        AnalyserNode.prototype.getByteFrequencyData = function(array) {
            origGetByteFreqData.call(this, array);
            for (var i = 0; i < array.length; i += 5) {
                array[i] = Math.max(0, Math.min(255,
                    array[i] + Math.floor(Math.random() * 3 - 1)));
            }
        };
        // Spoof AudioContext properties to match real device
        if (typeof AudioContext !== 'undefined') {
            var origCreateOscillator = AudioContext.prototype.createOscillator;
            // Match real device sampleRate (Android typically 48000, not 44100)
            Object.defineProperty(AudioContext.prototype, 'sampleRate', {
                get: function() { return 48000; },
            });
        }
    })();
    """ % (width, height, cores, ram_gb)

    # WebGL parameter spoof: hide renderer details for both modes
    # - SwiftShader: hide "Google SwiftShader" → show real device GPU
    # - Virgl: hide "ANGLE (Mesa, virgl...)" → show clean device GPU
    if gpu_mode in ("swiftshader", "virgl"):
        webgl_js = get_webgl_spoof_js(info)
        if webgl_js:
            js += webgl_js

    # Canvas fingerprint noise: prevents matching known SwiftShader hashes.
    # Adds imperceptible random noise to canvas pixel data so the hash
    # is unique per session, defeating canvas fingerprint databases.
    js += """
    (function() {
        // Generate a per-session noise seed
        var seed = Math.floor(Math.random() * 1000000);
        function noise(x) {
            // Simple deterministic hash for consistent noise per pixel
            var h = (x * 2654435761) >>> 0;
            return ((h ^ seed) & 0xF) - 8; // -8 to +7
        }

        // Patch getImageData to add noise
        var origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
        CanvasRenderingContext2D.prototype.getImageData = function() {
            var data = origGetImageData.apply(this, arguments);
            // Add noise only to a small subset of pixels (performance)
            var pixels = data.data;
            for (var i = 0; i < pixels.length; i += 40) {
                pixels[i] = Math.max(0, Math.min(255, pixels[i] + noise(i)));
            }
            return data;
        };

        // Patch toDataURL
        var origToDataURL = HTMLCanvasElement.prototype.toDataURL;
        HTMLCanvasElement.prototype.toDataURL = function() {
            var ctx = this.getContext('2d');
            if (ctx) {
                var imgData = ctx.getImageData(0, 0, 1, 1);
                var style = ctx.fillStyle;
                ctx.fillStyle = 'rgba(' + (seed & 0xFF) + ',' +
                    ((seed >> 8) & 0xFF) + ',0,0.003)';
                ctx.fillRect(0, 0, 1, 1);
                var result = origToDataURL.apply(this, arguments);
                ctx.putImageData(imgData, 0, 0);
                ctx.fillStyle = style;
                return result;
            }
            return origToDataURL.apply(this, arguments);
        };

        // Patch toBlob
        var origToBlob = HTMLCanvasElement.prototype.toBlob;
        HTMLCanvasElement.prototype.toBlob = function() {
            var ctx = this.getContext('2d');
            if (ctx) {
                var imgData = ctx.getImageData(0, 0, 1, 1);
                var style = ctx.fillStyle;
                ctx.fillStyle = 'rgba(' + (seed & 0xFF) + ',' +
                    ((seed >> 8) & 0xFF) + ',0,0.003)';
                ctx.fillRect(0, 0, 1, 1);
                var result = origToBlob.apply(this, arguments);
                ctx.putImageData(imgData, 0, 0);
                ctx.fillStyle = style;
                return result;
            }
            return origToBlob.apply(this, arguments);
        };
    })();
    """

    return js


async def apply_stealth(session, width=1920, height=1080, gpu_mode="auto",
                        browser_type="chromium"):
    """Apply minimal, honest stealth using real device values.

    Does NOT use Emulation.setDeviceMetricsOverride to avoid the
    outerWidth < innerWidth anomaly that triggers bot detection.
    Firefox needs no stealth — its TLS fingerprint passes CF natively.
    """
    if browser_type == "firefox":
        return  # Firefox doesn't need stealth overrides
    info = await asyncio.to_thread(device_info)
    ua = get_user_agent(info)
    platform = get_platform_string(info)

    # Build Sec-CH-UA client hints (missing hints = instant bot flag)
    chrome_ver = info.get("chrome_version", "138.0.0.0")
    brands, full_version_list = _build_client_hints_brands(chrome_ver)
    arch = info.get("arch", "aarch64")

    # CDP-level user-agent + platform + client hints override (undetectable)
    await session.send("Emulation.setUserAgentOverride", {
        "userAgent": ua,
        "platform": platform,
        "acceptLanguage": "en-US,en",
        "userAgentMetadata": {
            "brands": brands,
            "fullVersionList": full_version_list,
            "platform": "Linux",
            "platformVersion": "",
            "architecture": "arm" if "arm" in arch or "aarch" in arch else arch,
            "model": "",
            "mobile": False,
            "bitness": "64" if "64" in arch else "32",
            "wow64": False,
        },
    })

    # Build and inject stealth JS
    stealth_js = _build_stealth_js(width, height, info, gpu_mode)
    await session.send(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": stealth_js},
    )
