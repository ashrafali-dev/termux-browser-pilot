"""High-level browser automation commands."""

import asyncio
import logging
from urllib.parse import urlparse

from ._utils import escape_js_string

_ALLOWED_SCHEMES = {"http", "https"}

logger = logging.getLogger(__name__)


class PageCommands:
    """Navigate, wait, evaluate, extract content."""

    def __init__(self, session):
        self.session = session

    async def navigate(self, url, wait_until="load", timeout=30):
        """Navigate to URL and wait for page load.

        Args:
            wait_until: "load" (readyState=complete), "networkidle" (no
                requests for 500ms), "interactive" (readyState=interactive)
        """
        if not url or not isinstance(url, str):
            raise ValueError("URL must be a non-empty string")

        # Sanitize before parsing to prevent scheme detection bypass via
        # prepended control chars (e.g. "\tjavascript:...")
        url = "".join(c for c in url if c >= " " and c != "\x7f").strip()
        parsed = urlparse(url)
        if parsed.scheme and parsed.scheme not in _ALLOWED_SCHEMES:
            raise ValueError(
                f"URL scheme '{parsed.scheme}' not allowed. "
                f"Use http:// or https://"
            )

        if wait_until == "networkidle":
            return await self._navigate_networkidle(url, timeout)

        nav_result = await self.session.send("Page.navigate", {"url": url})

        if "errorText" in nav_result:
            raise RuntimeError(
                f"Navigation failed: {nav_result['errorText']}"
            )

        deadline = asyncio.get_running_loop().time() + timeout
        target_state = "complete" if wait_until == "load" else "interactive"

        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.5)
            try:
                state = await self.evaluate("document.readyState")
                if state in ("complete", "interactive") and \
                   (state == "complete" or target_state == "interactive"):
                    current_url = await self.evaluate("window.location.href")
                    if current_url == url or \
                       (current_url and current_url != "about:blank"):
                        await asyncio.sleep(0.3)
                        return
            except RuntimeError:
                pass
            except Exception:
                pass

        raise TimeoutError(f"Navigation to {url} timed out after {timeout}s")

    async def _navigate_networkidle(self, url, timeout=30, idle_time=0.5):
        """Navigate and wait until network is idle for idle_time seconds.

        Uses CDP Network events to track in-flight requests. Complete when
        zero requests pending for idle_time seconds.
        """
        pending = set()
        idle_start = None

        def on_request(params):
            req_id = params.get("requestId", "")
            if req_id:
                pending.add(req_id)

        def on_finished(params):
            pending.discard(params.get("requestId", ""))

        def on_failed(params):
            pending.discard(params.get("requestId", ""))

        self.session.on("Network.requestWillBeSent", on_request)
        self.session.on("Network.loadingFinished", on_finished)
        self.session.on("Network.loadingFailed", on_failed)

        try:
            nav_result = await self.session.send(
                "Page.navigate", {"url": url}
            )
            if "errorText" in nav_result:
                raise RuntimeError(
                    f"Navigation failed: {nav_result['errorText']}"
                )

            deadline = asyncio.get_running_loop().time() + timeout

            while asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.1)

                if len(pending) == 0:
                    if idle_start is None:
                        idle_start = asyncio.get_running_loop().time()
                    elif asyncio.get_running_loop().time() - idle_start >= idle_time:
                        try:
                            current = await self.evaluate(
                                "window.location.href"
                            )
                            if current and current != "about:blank":
                                return
                        except (RuntimeError, Exception):
                            pass
                else:
                    idle_start = None

            raise TimeoutError(
                f"Navigation to {url} timed out after {timeout}s"
            )
        finally:
            self.session.off("Network.requestWillBeSent", on_request)
            self.session.off("Network.loadingFinished", on_finished)
            self.session.off("Network.loadingFailed", on_failed)

    async def wait(self, seconds):
        """Wait for specified seconds."""
        await asyncio.sleep(seconds)

    async def wait_for_selector(self, selector, timeout=10):
        """Wait until a CSS selector is present in DOM."""
        deadline = asyncio.get_running_loop().time() + timeout
        safe_sel = escape_js_string(selector)
        while asyncio.get_running_loop().time() < deadline:
            try:
                result = await self.evaluate(
                    f"document.querySelector('{safe_sel}') !== null"
                )
                if result:
                    return True
            except (RuntimeError, Exception):
                # Context destroyed during navigation — retry on next tick
                pass
            await asyncio.sleep(0.3)
        raise TimeoutError(f"Selector '{selector}' not found in {timeout}s")

    async def evaluate(self, expression):
        """Evaluate JavaScript expression and return result."""
        result = await self.session.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            },
        )
        if "exceptionDetails" in result:
            exc = result["exceptionDetails"].get("exception", {})
            msg = exc.get("description", exc.get("value", "Unknown JS error"))
            raise RuntimeError(f"JS error: {msg}")
        remote = result.get("result", {})
        if remote.get("type") == "undefined":
            return None
        return remote.get("value", remote.get("description"))

    async def get_title(self):
        """Get page title."""
        return await self.evaluate("document.title")

    async def get_url(self):
        """Get current URL."""
        return await self.evaluate("window.location.href")

    async def get_text(self, selector=None):
        """Get text content of page or specific element."""
        if selector:
            safe_sel = escape_js_string(selector)
            return await self.evaluate(
                f"document.querySelector('{safe_sel}')?.innerText || ''"
            )
        return await self.evaluate("document.body.innerText")

    async def get_html(self, selector=None):
        """Get HTML of page or specific element."""
        if selector:
            safe_sel = escape_js_string(selector)
            return await self.evaluate(
                f"document.querySelector('{safe_sel}')?.outerHTML || ''"
            )
        return await self.evaluate("document.documentElement.outerHTML")

    async def get_attribute(self, selector, attribute):
        """Get an attribute value from an element."""
        safe_sel = escape_js_string(selector)
        safe_attr = escape_js_string(attribute)
        return await self.evaluate(
            f"document.querySelector('{safe_sel}')?.getAttribute('{safe_attr}')"
        )

    async def get_links(self):
        """Get all links on the page."""
        return await self.evaluate(
            "Array.from(document.querySelectorAll('a[href]')).map(function(a)"
            "{ return {text: (a.innerText||'').trim(), href: a.href}; })"
        )
