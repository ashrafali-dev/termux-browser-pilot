"""Main Pilot class - unified API tying all modules together."""

import asyncio
import logging
import os

from .browser import BrowserPilot
from .native import NativeFirefoxSession
from .commands import PageCommands
from .screenshot import ScreenshotCommands
from .input import InputCommands
from .stealth import apply_stealth
from .accessibility import AccessibilityCommands
from .cookies import CookieCommands
from .lock import SessionLock

# Optional: Chromium-only modules (require websockets)
try:
    from .cdp import CDPSession
    from .cloudflare import CloudflareHandler
    from .network import NetworkTracker
except ImportError:
    CDPSession = None
    CloudflareHandler = None
    NetworkTracker = None

logger = logging.getLogger(__name__)


class Pilot:
    """Main entry point for browser automation.

    Usage:
        async with Pilot() as pilot:
            await pilot.goto("https://example.com")
            await pilot.screenshot("page.png")
            title = await pilot.title()
    """

    def __init__(self, cdp_port=9222, display=":99", headless_xvfb=True,
                 session_file=None, user_data_dir=None, gpu_mode="auto",
                 window_size="auto", browser="auto", proxy=None):
        """Initialize Pilot.

        Args:
            session_file: Path to auto-save/load cookies on start/stop.
                Use .json for CDP format, .txt for Netscape format.
            user_data_dir: Persistent browser profile directory. When set,
                browser state (cookies, localStorage) persists across runs.
            gpu_mode: "auto" (try virgl, fallback swiftshader+spoof),
                "virgl" (force hardware), "swiftshader" (force software).
                Only used for Chromium.
            window_size: "auto" (detect from device), or "WIDTHxHEIGHT".
                Auto uses device screen in landscape orientation.
            browser: "firefox" (default, passes CF natively),
                "chromium", or "auto" (= firefox).
            proxy: Proxy URL (http://host:port or socks5://host:port).
        """
        # Resolve browser choice
        if browser == "auto":
            browser = "firefox"
        self._browser_type = browser
        self._proxy = proxy

        # Resolve window size from device if auto
        if window_size == "auto":
            window_size = self._detect_window_size()

        self._browser = BrowserPilot(
            display=display,
            cdp_port=cdp_port,
            headless_xvfb=headless_xvfb,
            user_data_dir=user_data_dir,
            gpu_mode=gpu_mode,
            window_size=window_size,
            browser_type=browser,
            proxy=proxy,
        )
        self._lock = SessionLock()
        self._session = None
        self._session_file = session_file
        self.page = None
        self.screenshot_cmd = None
        self.input = None
        self.accessibility = None
        self.cloudflare = None
        self.cookies = None
        self.network = None

    @staticmethod
    def _detect_window_size():
        """Detect window size from device screen in landscape.

        Uses device.py screen detection. Falls back to 1920,1080.
        Caps at 1920x1080 to avoid excessive resource usage.
        """
        try:
            from .device import device_info
            info = device_info()
            sw = info.get("screen_width", 1920)
            sh = info.get("screen_height", 1080)
            # Use landscape orientation (wider dimension first)
            w = max(sw, sh)
            h = min(sw, sh)
            # Cap at reasonable maximum for Xvfb performance
            if w > 1920:
                scale = 1920 / w
                w = 1920
                h = int(h * scale)
            return f"{w},{h}"
        except Exception:
            return "1920,1080"

    async def start(self):
        """Start browser and connect."""
        self._lock.acquire()
        try:
            result = await self._browser.start()
            try:
                await self._init_session(result)
            except Exception:
                # Clean up browser/Xvfb if session init fails
                await self._browser.stop()
                raise
        except Exception:
            self._lock.release()
            raise
        return self

    async def _init_session(self, result):
        """Initialize session and command modules (called from start)."""
        if self._browser_type == "firefox":
            # Native Firefox — no automation framework, passes CF
            self._session = NativeFirefoxSession(
                display=self._browser.display,
                window_size=self._browser.window_size,
                user_data_dir=self._browser._external_user_data_dir,
                proxy=self._proxy,
            )
            await self._session.connect()
        else:
            # result is CDP WebSocket URL
            self._session = CDPSession(result)
            await self._session.connect()

        # Apply stealth (skips for Firefox — not needed)
        try:
            parts = self._browser.window_size.split(",")
            w = int(parts[0])
            h = int(parts[1]) if len(parts) >= 2 else 1080
        except (ValueError, IndexError):
            w, h = 1920, 1080
        await apply_stealth(
            self._session, width=w, height=h,
            gpu_mode=self._browser._gpu_mode,
            browser_type=self._browser_type,
        )

        # Initialize command modules
        self.page = PageCommands(self._session)
        self.screenshot_cmd = ScreenshotCommands(self._session)
        self.input = InputCommands(self._session)
        self.accessibility = AccessibilityCommands(self._session)
        self.cloudflare = CloudflareHandler(self.page, self.input,
                                                    display=self._browser.display)
        self.cookies = CookieCommands(self._session)

        # NetworkTracker only works with CDP events (Chromium)
        if self._browser_type != "firefox":
            self.network = NetworkTracker(self._session)
            await self.network.start()

        # Auto-load session cookies if configured
        if self._session_file:
            try:
                if os.path.exists(self._session_file):
                    if self._session_file.endswith(".txt"):
                        n = await self.cookies.import_netscape(self._session_file)
                    else:
                        n = await self.cookies.load(self._session_file)
                    if n:
                        logger.info("Loaded %d cookies from %s", n, self._session_file)
            except Exception as e:
                logger.warning("Error loading session: %s", e)

    async def stop(self, save_session=None):
        """Shut down everything gracefully.

        Args:
            save_session: If a path is given, save cookies to that file
                before shutting down. Ensures session persistence.
        """
        # Save session state before closing CDP
        if isinstance(save_session, str) and save_session and self.cookies and self._session:
            try:
                if save_session.endswith(".txt"):
                    await self.cookies.export_netscape(save_session)
                else:
                    await self.cookies.save(save_session)
                logger.info("Session saved to %s", save_session)
            except Exception as e:
                logger.warning("Error saving session: %s", e)

        # Stop network tracker
        try:
            if self.network:
                await self.network.stop()
        except Exception as e:
            logger.warning("Error stopping network tracker: %s", e)

        # Close browser session
        try:
            if self._session:
                await self._session.close()
                # For Firefox: also delete WebDriver session (stops Firefox)
                if self._browser_type == "firefox" and hasattr(self._session, 'delete_session'):
                    await self._session.delete_session()
        except Exception as e:
            logger.warning("Error closing session during stop: %s", e)
        finally:
            # Graceful browser shutdown (SIGTERM -> wait -> SIGKILL)
            try:
                await self._browser.stop()
            except Exception as e:
                logger.warning("Error stopping browser during stop: %s", e)
            finally:
                self._lock.release()

    # --- Navigation ---

    async def goto(self, url, timeout=45):
        """Navigate to URL."""
        await self.page.navigate(url, timeout=timeout)

    async def goto_idle(self, url, timeout=45):
        """Navigate and wait for network idle (best for heavy JS pages)."""
        await self.page.navigate(url, wait_until="networkidle", timeout=timeout)

    async def goto_cf(self, url, timeout=60):
        """Navigate with Cloudflare challenge wait.

        Firefox passes CF natively via TLS fingerprint — no widget
        clicking needed. Just navigate and wait for auto-resolve.
        Chromium uses the full CF handler (detect + click Turnstile).
        """
        if self._browser_type == "firefox":
            return await self._firefox_cf_navigate(url, timeout)
        return await self.cloudflare.navigate_with_cf(url, timeout)

    async def _firefox_cf_navigate(self, url, timeout=60):
        """Firefox CF navigation — wait for auto-resolve only.

        Firefox's native TLS fingerprint passes CF challenges without
        any widget interaction. Just wait for the challenge to resolve.
        Uses a single deadline from the start to avoid exceeding timeout.
        """
        deadline = asyncio.get_running_loop().time() + timeout
        nav_timeout = max(10, timeout - 10)
        await self.page.navigate(url, wait_until="load", timeout=nav_timeout)
        await asyncio.sleep(2)

        # Check if still on CF challenge page (by title only — DOM selectors
        # leave remnants even after challenge resolves)
        cf_titles = ["just a moment", "attention required", "please wait",
                     "checking your browser", "verify you are human"]
        while asyncio.get_running_loop().time() < deadline:
            title = await self.page.get_title()
            if title and not any(cf in title.lower() for cf in cf_titles):
                return await self.page.get_url()
            await asyncio.sleep(2)

        # Timed out but return URL anyway — might have resolved
        return await self.page.get_url()

    # --- Content ---

    async def title(self):
        return await self.page.get_title()

    async def url(self):
        return await self.page.get_url()

    async def text(self, selector=None):
        return await self.page.get_text(selector)

    async def html(self, selector=None):
        return await self.page.get_html(selector)

    async def links(self):
        return await self.page.get_links()

    async def evaluate(self, js):
        return await self.page.evaluate(js)

    # --- Interaction ---

    async def click(self, selector=None, x=None, y=None, button="left",
                    count=1, interval=0.1):
        if count == 1:
            await self.input.click(
                selector=selector, x=x, y=y, button=button,
            )
            return
        # Multi-click: resolve coordinates once, move once, then
        # press/release N times (avoids repeated JS eval + mousemove)
        if selector and x is None and y is None:
            x, y = await self.input._get_element_center(selector)
        # Single mousemove
        await self.input.session.send("Input.dispatchMouseEvent", {
            "type": "mouseMoved", "x": x, "y": y,
        })
        await asyncio.sleep(0.05)
        for i in range(count):
            cc = min(i + 1, count) if count <= 3 else 1
            await self.input.session.send("Input.dispatchMouseEvent", {
                "type": "mousePressed", "x": x, "y": y,
                "button": button, "clickCount": cc,
            })
            await asyncio.sleep(0.03)
            await self.input.session.send("Input.dispatchMouseEvent", {
                "type": "mouseReleased", "x": x, "y": y,
                "button": button, "clickCount": cc,
            })
            if i < count - 1:
                await asyncio.sleep(interval)

    async def human_click(self, selector=None, x=None, y=None):
        return await self.input.human_click(selector=selector, x=x, y=y)

    async def type(self, selector=None, text="", x=None, y=None, mode="auto"):
        return await self.input.fill(selector=selector, text=text, x=x, y=y, mode=mode)

    async def press(self, key):
        return await self.input.press_key(key)

    async def scroll(self, delta_y=300):
        return await self.input.scroll(delta_y=delta_y)

    # --- Screenshots ---

    async def screenshot(self, path="screenshot.png", full_page=False):
        return await self.screenshot_cmd.capture(path, full_page=full_page)

    async def debug_screenshots(self, directory="debug_shots", interval=5,
                                  max_count=60):
        """Start periodic debug screenshots (background task)."""
        return asyncio.create_task(
            self.screenshot_cmd.periodic_capture(
                directory, interval, max_count,
            )
        )

    async def pdf(self, path="page.pdf", **options):
        return await self.screenshot_cmd.capture_pdf(path, **options)

    # --- Cookies ---

    async def get_cookies(self, urls=None):
        return await self.cookies.get(urls)

    async def get_all_cookies(self):
        """Get all cookies including HttpOnly (uses Network.getAllCookies)."""
        return await self.cookies.get_all()

    async def set_cookie(self, name, value, domain, **kwargs):
        return await self.cookies.set(name, value, domain, **kwargs)

    async def clear_cookies(self):
        return await self.cookies.clear()

    async def save_cookies(self, path="cookies.json"):
        return await self.cookies.save(path)

    async def load_cookies(self, path="cookies.json"):
        return await self.cookies.load(path)

    async def export_cookies(self, path="cookies.txt"):
        return await self.cookies.export_netscape(path)

    async def import_cookies(self, path="cookies.txt"):
        return await self.cookies.import_netscape(path)

    # --- Network tracking (sync - Chromium only, no CDP calls needed) ---

    def get_requests(self):
        return self.network.get_all() if self.network else []

    def get_download_urls(self):
        return self.network.get_downloads() if self.network else []

    def get_network_summary(self):
        if not self.network:
            return "Network tracking not available (Firefox native mode)"
        return self.network.summary()

    # --- Waits ---

    async def wait(self, seconds):
        return await self.page.wait(seconds)

    async def wait_for(self, selector, timeout=10):
        return await self.page.wait_for_selector(selector, timeout)

    # --- Accessibility ---

    async def a11y_tree(self):
        return await self.accessibility.get_tree_summary()

    async def __aenter__(self):
        await self.start()
        return self

    async def exit(self, save_session=None):
        """Gracefully exit browser with optional session save.

        Args:
            save_session: Path to save cookies. If None, uses session_file
                from __init__. Pass False to skip saving entirely.
        """
        save_path = save_session
        if save_path is None:
            save_path = self._session_file
        if save_path is False:
            save_path = None
        await self.stop(save_session=save_path)

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        try:
            await self.stop(save_session=self._session_file)
        except Exception:
            if exc_type is None:
                raise  # Only propagate cleanup error if no original error
