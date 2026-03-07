"""Cloudflare challenge detection and Turnstile handling.

Supports both passive challenges (auto-resolve) and interactive
Turnstile checkbox via native X11 input (xdotool) for maximum
stealth, with CDP input fallback.

Key insight: CF Managed Challenge pages render Turnstile directly
in the page via a closed shadow DOM + dynamically created iframe.
The widget container and hidden input are in the main DOM; the
actual checkbox lives inside nested closed shadow roots. We use
coordinate-based clicking to interact regardless of DOM structure.
"""

import asyncio
import logging
import os
import random
import shutil

from ._utils import escape_js_string

logger = logging.getLogger(__name__)


CLOUDFLARE_TITLES = [
    "just a moment",
    "attention required",
    "please wait",
    "checking your browser",
    "verify you are human",
]

CLOUDFLARE_SELECTORS = [
    "#cf-challenge-running",
    "#challenge-running",
    ".cf-browser-verification",
    "#turnstile-wrapper",
    "iframe[src*='challenges.cloudflare.com']",
    "input[name='cf-turnstile-response']",
]


def _has_xdotool():
    """Check if xdotool is available for native X11 input."""
    return shutil.which("xdotool") is not None


class CloudflareHandler:
    """Detect and wait through Cloudflare challenges.

    Strategy for Managed Challenge pages (strict Turnstile):
    1. Detect via title + widget selectors
    2. Wait for Turnstile widget to render (creates elements dynamically)
    3. Find widget position via hidden response input's container
    4. Click using xdotool (native X11, undetectable) or CDP fallback
    5. Alternate methods on retries
    """

    def __init__(self, page_commands, input_commands=None, display=":99"):
        self.page = page_commands
        self.input = input_commands
        self.display = display
        self._use_xdotool = _has_xdotool()
        self._viewport_offset = None

    async def is_challenge_page(self):
        """Check if current page is a Cloudflare challenge."""
        try:
            title = await self.page.get_title()
            if title and any(cf in title.lower() for cf in CLOUDFLARE_TITLES):
                return True

            selectors_js = ", ".join(
                f"'{escape_js_string(s)}'" for s in CLOUDFLARE_SELECTORS
            )
            found = await self.page.evaluate(f"""
                (function() {{
                    var sels = [{selectors_js}];
                    return sels.some(function(s) {{
                        return document.querySelector(s) !== null;
                    }});
                }})()
            """)
            return bool(found)
        except (RuntimeError, Exception):
            return False

    async def _detect_widget_type(self):
        """Detect what kind of Turnstile widget is present.

        Returns: 'iframe', 'managed', or None.
        - 'iframe': Traditional iframe in main DOM
        - 'managed': Managed challenge with hidden input (shadow DOM iframe)
        """
        try:
            return await self.page.evaluate("""
                (function() {
                    if (document.querySelector(
                        'iframe[src*="challenges.cloudflare.com"]'))
                        return 'iframe';
                    if (document.querySelector(
                        'input[name="cf-turnstile-response"]'))
                        return 'managed';
                    return null;
                })()
            """)
        except (RuntimeError, Exception):
            return None

    async def _get_viewport_offset(self):
        """Get offset from screen coords to CSS viewport.

        xdotool uses screen coordinates; CSS positions are relative
        to the viewport. We need this offset to convert between them.
        """
        if self._viewport_offset is not None:
            return self._viewport_offset
        try:
            offset = await self.page.evaluate("""
                (function() {
                    return {
                        x: window.screenX +
                           (window.outerWidth - window.innerWidth) / 2,
                        y: window.screenY +
                           (window.outerHeight - window.innerHeight)
                    };
                })()
            """)
            if offset and offset.get("y", 0) > 0:
                self._viewport_offset = (
                    int(offset["x"]), int(offset["y"])
                )
                logger.debug("Viewport offset: %s", self._viewport_offset)
                return self._viewport_offset
        except Exception:
            pass
        # Default: Chrome maximized with toolbar ~74px
        self._viewport_offset = (0, 74)
        return self._viewport_offset

    async def _find_widget_position(self):
        """Find the Turnstile widget's checkbox position.

        Works for both iframe-based and managed challenge widgets.
        Returns (css_x, css_y) of the checkbox center, or None.
        """
        try:
            pos = await self.page.evaluate("""
                (function() {
                    // Method 1: Direct iframe in main DOM
                    var iframe = document.querySelector(
                        'iframe[src*="challenges.cloudflare.com"]');
                    if (iframe) {
                        var r = iframe.getBoundingClientRect();
                        if (r.width > 0 && r.height > 0)
                            return {x:r.x, y:r.y, w:r.width, h:r.height,
                                    method:'iframe'};
                    }

                    // Method 2: Managed challenge - find via hidden input
                    var input = document.querySelector(
                        'input[name="cf-turnstile-response"]');
                    if (input) {
                        // Walk up to find the widget container.
                        // Take the tightest container that's not full-page.
                        var el = input;
                        var best = null;
                        for (var i = 0; i < 8; i++) {
                            el = el.parentElement;
                            if (!el || el === document.body) break;
                            var r = el.getBoundingClientRect();
                            if (r.width <= 0 || r.height <= 0) continue;
                            // Skip full-page wrappers
                            if (r.width >= window.innerWidth * 0.95) continue;
                            if (r.height >= 30 && r.height <= 200) {
                                if (!best || r.width * r.height <
                                    best.w * best.h) {
                                    best = {x:r.x, y:r.y, w:r.width,
                                            h:r.height,
                                            method:'managed-container'};
                                }
                            }
                        }
                        if (best) return best;
                    }

                    // Method 3: Look for Turnstile-sized visible elements
                    // After Turnstile JS runs, it may create wrapper divs
                    var all = document.querySelectorAll(
                        'div[id^="cf-chl-widget"], .cf-turnstile');
                    for (var i = 0; i < all.length; i++) {
                        var r = all[i].getBoundingClientRect();
                        if (r.width >= 200 && r.height >= 40) {
                            return {x:r.x, y:r.y, w:r.width, h:r.height,
                                    method:'widget-selector'};
                        }
                    }

                    return null;
                })()
            """)
        except (RuntimeError, Exception) as e:
            logger.debug("Error finding widget position: %s", e)
            return None

        if not pos:
            return None

        # Checkbox is near the left side of the widget, vertically centered
        # Standard Turnstile checkbox: ~28px from left, center height
        cb_x = pos["x"] + min(28, pos["w"] * 0.09)
        cb_y = pos["y"] + pos["h"] * 0.5

        logger.debug(
            "Widget via %s: rect=(%d,%d,%d,%d) → checkbox=(%d,%d)",
            pos.get("method"), pos["x"], pos["y"],
            pos["w"], pos["h"], cb_x, cb_y,
        )
        return cb_x, cb_y

    # --- xdotool native input (primary, undetectable) ---

    async def _xdotool_click(self, css_x, css_y):
        """Click at CSS viewport coordinates using xdotool.

        Converts CSS coords to screen coords and performs a real
        X11 mouse click (XTEST protocol), indistinguishable from
        actual user input.
        """
        offset = await self._get_viewport_offset()
        screen_x = int(css_x + offset[0])
        screen_y = int(css_y + offset[1])

        # Human imprecision
        screen_x += random.randint(-3, 3)
        screen_y += random.randint(-3, 3)

        logger.debug(
            "xdotool click: CSS(%d,%d) → screen(%d,%d)",
            int(css_x), int(css_y), screen_x, screen_y,
        )

        # Move mouse first (humans don't teleport-click)
        env = os.environ.copy()
        env["DISPLAY"] = self.display
        proc = await asyncio.create_subprocess_exec(
            "xdotool", "mousemove", "--sync",
            str(screen_x), str(screen_y),
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        await asyncio.sleep(random.uniform(0.1, 0.3))

        # Click
        proc = await asyncio.create_subprocess_exec(
            "xdotool", "click", "1",
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()

    async def _xdotool_key(self, key):
        """Send a key press via xdotool (native X11 input)."""
        env = os.environ.copy()
        env["DISPLAY"] = self.display
        proc = await asyncio.create_subprocess_exec(
            "xdotool", "key", key,
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()

    # --- CDP input fallback ---

    async def _cdp_key(self, key):
        """Send key via CDP Input domain."""
        if self.input:
            await self.input.press_key(key)

    # --- Widget interaction methods ---

    async def click_widget_native(self):
        """Click Turnstile checkbox using xdotool native X11 input.

        This is the primary method — real X11 events are
        indistinguishable from actual user input.
        """
        pos = await self._find_widget_position()
        if not pos:
            return False

        await self._xdotool_click(pos[0], pos[1])
        return True

    async def click_widget_cdp(self):
        """Click Turnstile checkbox using CDP mouse input (fallback)."""
        if not self.input:
            return False

        pos = await self._find_widget_position()
        if not pos:
            return False

        await self.input.human_click(x=pos[0], y=pos[1])
        return True

    async def click_widget_keyboard(self):
        """Activate Turnstile via keyboard navigation (Tab + Space).

        Last resort — Tab through page elements to reach the widget,
        then Space to activate. Works when the widget is focusable.
        """
        widget_type = await self._detect_widget_type()
        if not widget_type:
            return False

        logger.debug("Keyboard attempt on %s widget", widget_type)

        # Wait for widget to initialize
        await asyncio.sleep(random.uniform(1.0, 2.0))

        # Tab through page elements
        # Managed challenge pages have fewer elements (~3-6 before widget)
        tab_count = random.randint(3, 10)
        for _ in range(tab_count):
            if self._use_xdotool:
                await self._xdotool_key("Tab")
            else:
                await self._cdp_key("Tab")
            await asyncio.sleep(random.uniform(0.15, 0.4))

        # Brief pause (reading time)
        await asyncio.sleep(random.uniform(0.3, 0.8))

        # Press Space to activate checkbox
        if self._use_xdotool:
            await self._xdotool_key("space")
        else:
            await self._cdp_key("Space")

        logger.debug("Sent Tab×%d + Space", tab_count)
        return True

    async def click_turnstile(self):
        """Click the Turnstile checkbox using best available method.

        Priority:
        1. xdotool native click (real X11 input, undetectable)
        2. CDP human_click (Bezier curve movement)
        3. Keyboard Tab+Space (accessibility interaction)
        """
        # Wait for widget to appear
        for _ in range(15):
            widget_type = await self._detect_widget_type()
            if widget_type:
                break
            await asyncio.sleep(1)
        else:
            logger.debug("No Turnstile widget found after 15s")
            return False

        logger.debug("Turnstile widget detected: %s", widget_type)

        # Wait for widget to fully render (Turnstile JS needs time
        # to create shadow DOM + iframe + challenge logic)
        await asyncio.sleep(random.uniform(2.0, 4.0))

        # Try methods in priority order
        if self._use_xdotool:
            result = await self.click_widget_native()
            if result:
                return True
            logger.debug("xdotool click failed, trying CDP")

        if self.input:
            result = await self.click_widget_cdp()
            if result:
                return True
            logger.debug("CDP click failed, trying keyboard")

        return await self.click_widget_keyboard()

    async def wait_for_pass(self, timeout=45, poll_interval=2):
        """Wait for Cloudflare challenge to resolve.

        Strategy:
        1. First wait for auto-resolve (passive/non-interactive)
        2. If still challenged, click the widget
        3. Alternate between methods on retries
        4. Increase wait between attempts (CF rate-limits)
        """
        if not await self.is_challenge_page():
            return True

        deadline = asyncio.get_running_loop().time() + timeout
        attempt = 0
        max_attempts = 6

        while asyncio.get_running_loop().time() < deadline:
            wait = poll_interval + random.uniform(-0.3, 0.5)
            await asyncio.sleep(wait)

            if not await self.is_challenge_page():
                await asyncio.sleep(1)
                return True

            if attempt < max_attempts:
                if attempt % 3 == 0:
                    # Primary: coordinate-based click
                    clicked = await self.click_turnstile()
                elif attempt % 3 == 1:
                    # Secondary: keyboard only
                    clicked = await self.click_widget_keyboard()
                else:
                    # Tertiary: longer wait + retry click
                    await asyncio.sleep(random.uniform(2, 4))
                    clicked = await self.click_turnstile()

                logger.debug(
                    "Turnstile attempt %d/%d (method=%d) clicked=%s",
                    attempt + 1, max_attempts, attempt % 3, clicked,
                )
                if clicked:
                    attempt += 1
                    # Increasing backoff: CF needs time to verify
                    backoff = random.uniform(3, 5) + attempt * 0.5
                    await asyncio.sleep(backoff)

        return False

    async def navigate_with_cf(self, url, timeout=60):
        """Navigate to URL and handle Cloudflare if present."""
        await self.page.navigate(url, wait_until="load", timeout=timeout)
        await asyncio.sleep(2)

        if await self.is_challenge_page():
            passed = await self.wait_for_pass(
                timeout=max(timeout - 10, 10)
            )
            if not passed:
                raise TimeoutError(
                    f"Cloudflare challenge not resolved in {timeout}s"
                )

        return await self.page.get_url()
