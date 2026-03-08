"""MCP server for Termux Browser Pilot.

Exposes browser automation as tools for Claude Code and other
MCP-compatible AI assistants. Wraps the daemon's Unix socket API.

Usage:
    python -m src.mcp_server                    # stdio transport
    claude mcp add tbp -- python -m src.mcp_server  # register with Claude Code

All tools auto-start the daemon if not running.
"""

import os

from mcp.server.fastmcp import FastMCP
from . import __version__

mcp = FastMCP(
    "termux-browser-pilot",
    instructions=(
        "Browser automation for Termux/Android. Firefox passes Cloudflare "
        "natively. Use browser_goto to navigate, browser_text to read content, "
        "browser_click/type/press for interaction, browser_screenshot for "
        "visual capture, browser_a11y for accessibility tree."
    ),
)


async def _send(action, params=None, timeout=120):
    """Send command to daemon, auto-starting if needed."""
    from .client import send_command
    return await send_command(action, params, timeout=timeout)


def _result(resp):
    """Extract result or error from daemon response."""
    if resp.get("success"):
        return resp["data"]
    return {"error": resp.get("error", "Unknown error")}


# ── Navigation ──────────────────────────────────

@mcp.tool()
async def browser_goto(url: str, cloudflare: bool = False,
                       timeout: int = 45) -> dict:
    """Navigate browser to URL. Use cloudflare=True for CF-protected sites.

    Returns current URL and page title after navigation.
    """
    return _result(await _send("goto", {
        "url": url, "cloudflare": cloudflare, "timeout": timeout,
    }, timeout=timeout + 15))


@mcp.tool()
async def browser_back() -> dict:
    """Navigate back in browser history. Returns new URL and title."""
    return _result(await _send("back"))


@mcp.tool()
async def browser_forward() -> dict:
    """Navigate forward in browser history. Returns new URL and title."""
    return _result(await _send("forward"))


@mcp.tool()
async def browser_reload() -> dict:
    """Reload current page. Returns URL and title."""
    return _result(await _send("reload"))


# ── Content ──────────────────────────────────

@mcp.tool()
async def browser_text(selector: str = "", limit: int = 0) -> dict:
    """Get visible text content of the page or a specific element.

    Args:
        selector: Optional CSS selector to extract text from specific element.
        limit: Max characters to return (0 = unlimited).
    """
    params = {}
    if selector:
        params["selector"] = selector
    if limit > 0:
        params["limit"] = limit
    return _result(await _send("text", params))


@mcp.tool()
async def browser_html(selector: str = "", limit: int = 0) -> dict:
    """Get HTML source of the page or a specific element.

    Args:
        selector: Optional CSS selector.
        limit: Max characters to return (0 = unlimited).
    """
    params = {}
    if selector:
        params["selector"] = selector
    if limit > 0:
        params["limit"] = limit
    return _result(await _send("html", params))


@mcp.tool()
async def browser_links(limit: int = 100) -> dict:
    """Get all links on the current page. Returns list of {text, href}."""
    return _result(await _send("links", {"limit": limit}))


@mcp.tool()
async def browser_title() -> dict:
    """Get current page title."""
    return _result(await _send("title"))


@mcp.tool()
async def browser_url() -> dict:
    """Get current page URL."""
    return _result(await _send("url"))


@mcp.tool()
async def browser_eval(expression: str) -> dict:
    """Execute JavaScript in the browser and return the result.

    Args:
        expression: JavaScript expression to evaluate.
    """
    return _result(await _send("eval", {"expression": expression}))


@mcp.tool()
async def browser_a11y(limit: int = 0) -> dict:
    """Get the accessibility tree of the current page.

    Returns ARIA roles and names for all visible elements.
    Useful for understanding page structure without screenshots.
    """
    params = {}
    if limit > 0:
        params["limit"] = limit
    return _result(await _send("a11y", params))


# ── Smart Element Finding ──────────────────────────────────

@mcp.tool()
async def browser_find(text: str, role: str = "",
                       limit: int = 10) -> dict:
    """Find interactive elements by visible text content.

    Returns matching elements with their CSS selectors for clicking.
    Much easier than guessing CSS selectors — just search by what you see.

    Args:
        text: Text to search for (case-insensitive substring match).
        role: Optional filter: "link", "button", "input", etc.
        limit: Max results to return (default 10).
    """
    params = {"text": text, "limit": limit}
    if role:
        params["role"] = role
    return _result(await _send("find", params))


@mcp.tool()
async def browser_elements(kind: str, limit: int = 50) -> dict:
    """List interactive page elements by kind.

    Returns elements with CSS selectors ready for clicking/typing.

    Kinds:
      links — anchor elements: text, href, selector
      buttons — buttons and submit inputs: text, type, selector, disabled
      inputs — text inputs, textareas, selects (not hidden/submit): type, name, placeholder, value, selector, label
      forms — form elements: action, method, id, fields list
      headings — h1-h6: level, text, selector
      selects — dropdowns and comboboxes: selector, name, options, multiple
      images — img elements: src, alt, width, height, selector

    Args:
        kind: Element kind — one of: links, buttons, inputs, forms, headings, selects, images.
        limit: Max elements to return (default 50).
    """
    return _result(await _send("elements", {"kind": kind, "limit": limit}))


# ── Interaction ──────────────────────────────────

@mcp.tool()
async def browser_click(target: str = "", human: bool = False,
                        x: int = None, y: int = None,
                        button: str = "left", count: int = 1,
                        interval: float = 0.1,
                        keyboard_fallback: bool = True) -> dict:
    """Click an element by CSS selector or coordinates.

    Args:
        target: CSS selector of element to click. Optional if x/y given.
        x: X coordinate fallback when selector fails or for raw clicks.
        y: Y coordinate fallback when selector fails or for raw clicks.
        human: If True, use human-like Bezier curve mouse movement.
        button: Mouse button - "left", "right", or "middle" (default "left").
        count: Number of clicks (1=single, 2=double, 3=triple for paragraph select).
        interval: Seconds between multi-clicks (default 0.1).
        keyboard_fallback: If True and mouse click fails, try focus+Enter (default True).
    """
    params = {"human": human, "button": button, "count": count,
              "interval": interval, "keyboard_fallback": keyboard_fallback}
    if target:
        params["target"] = target
    if x is not None:
        params["x"] = x
    if y is not None:
        params["y"] = y
    return _result(await _send("click", params))


@mcp.tool()
async def browser_type(target: str = "", text: str = "",
                       x: int = None, y: int = None,
                       mode: str = "auto") -> dict:
    """Type text into an input element.

    Args:
        target: CSS selector of input/textarea element. Optional if x/y given.
        text: Text to type.
        x: X coordinate fallback when selector fails.
        y: Y coordinate fallback when selector fails.
        mode: "clipboard" (paste via Ctrl+V), "xdotool" (direct key-by-key),
              "auto" (clipboard + verify + xdotool fallback). Default "auto".
    """
    params = {"text": text, "mode": mode}
    if target:
        params["target"] = target
    if x is not None:
        params["x"] = x
    if y is not None:
        params["y"] = y
    return _result(await _send("type", params))


@mcp.tool()
async def browser_press(key: str) -> dict:
    """Press a keyboard key.

    Args:
        key: Key name (Enter, Tab, Escape, ArrowDown, etc.)
    """
    return _result(await _send("press", {"key": key}))


@mcp.tool()
async def browser_tab_to(text: str, max_tabs: int = 30,
                         enter: bool = False,
                         space: bool = False) -> dict:
    """Tab through page elements until finding one matching text, then optionally press Enter or Space.

    Useful for OAuth/login pages where click doesn't work on cross-origin iframes.
    On heavy JS sites (LinkedIn), JS eval may time out — the tool will report
    this and suggest using browser_press manually with visual confirmation.

    Args:
        text: Text to search for in focused element (case-insensitive, partial match).
        max_tabs: Maximum Tab presses before giving up (default 30).
        enter: If true, press Enter after finding the element.
        space: If true, press Space after finding the element (useful for buttons).
    """
    return _result(await _send("tab_to", {
        "text": text, "max_tabs": max_tabs, "enter": enter,
        "space": space,
    }))


@mcp.tool()
async def browser_focus(selector: str, enter: bool = False,
                        scroll: bool = True) -> dict:
    """Focus a page element via CSS selector using JS focus().

    More reliable than coordinate clicks for cross-origin iframes,
    dynamically positioned elements, or when tooltips interfere.
    Pierces shadow DOM. Uses scrollIntoView + element.focus(),
    then optionally Enter to activate.

    Args:
        selector: CSS selector of the element to focus.
        enter: If True, press Enter after focusing (activates links/buttons).
        scroll: If True, scroll element into view first (default True).
    """
    return _result(await _send("focus", {
        "selector": selector, "enter": enter, "scroll": scroll,
    }))


@mcp.tool()
async def browser_scroll(direction: str = "down",
                         amount: int = 300) -> dict:
    """Scroll the page.

    Args:
        direction: "down" or "up"
        amount: Pixels to scroll (default 300).
    """
    pixels = abs(amount) if direction == "down" else -abs(amount)
    return _result(await _send("scroll", {"amount": pixels}))


@mcp.tool()
async def browser_hover(target: str = "", x: int = None,
                        y: int = None) -> dict:
    """Hover over an element and return info about what's under the cursor.

    Use with x/y coordinates to inspect what element is at a position
    before clicking (verified targeting). Returns element tag, text, role, etc.

    Args:
        target: CSS selector of element to hover over.
        x: X coordinate for position-based hover.
        y: Y coordinate for position-based hover.
    """
    params = {}
    if target:
        params["target"] = target
    if x is not None:
        params["x"] = x
    if y is not None:
        params["y"] = y
    return _result(await _send("hover", params))


@mcp.tool()
async def browser_mouse_move(x: int = 0, y: int = 0,
                             path: str = "") -> dict:
    """Move mouse to coordinates and take screenshot with cursor crosshair.

    REQUIRED before browser_click(x, y). Arms the click at these coordinates.
    Returns screenshot with red crosshair showing where the cursor is.
    Verify the position visually, then call browser_click with same x, y.

    Args:
        x: X coordinate to move mouse to.
        y: Y coordinate to move mouse to.
        path: Optional screenshot path (default: cursor_preview.png).
    """
    params = {"x": x, "y": y}
    if path:
        params["path"] = path
    return _result(await _send("mouse_move", params))


@mcp.tool()
async def browser_mouse_locate(path: str = "") -> dict:
    """Get current mouse position and take screenshot with cursor crosshair.

    Arms browser_click at the current cursor coordinates.
    Use when you don't know exact coordinates but want to see where
    the cursor is and click at that position.

    Args:
        path: Optional screenshot path (default: cursor_preview.png).
    """
    params = {}
    if path:
        params["path"] = path
    return _result(await _send("mouse_locate", params))


# ── Screenshots ──────────────────────────────────

@mcp.tool()
async def browser_screenshot(path: str = "screenshot.png",
                             full_page: bool = False,
                             cursor: bool = False) -> dict:
    """Take a screenshot of the current page.

    Args:
        path: File path to save the PNG screenshot.
        full_page: If True, capture the entire scrollable page.
        cursor: If True, draw a red crosshair at the last mouse_move position.
    """
    return _result(await _send("screenshot", {
        "path": path, "full": full_page, "cursor": cursor,
    }))


@mcp.tool()
async def browser_pdf(
    path: str = "page.pdf",
    landscape: bool = False,
    scale: float = 0,
    margin_top: float = -1,
    margin_right: float = -1,
    margin_bottom: float = -1,
    margin_left: float = -1,
    page_ranges: str = "",
    print_background: bool = True,
    header_template: str = "",
    footer_template: str = "",
) -> dict:
    """Export current page as PDF with optional layout options.

    Args:
        path: File path to save the PDF.
        landscape: Landscape orientation (default portrait).
        scale: Scale factor 0.1-2.0 (0 = default).
        margin_top: Top margin in inches (-1 = default).
        margin_right: Right margin in inches (-1 = default).
        margin_bottom: Bottom margin in inches (-1 = default).
        margin_left: Left margin in inches (-1 = default).
        page_ranges: Page ranges like '1-3' (empty = all).
        print_background: Include background graphics.
        header_template: HTML header template.
        footer_template: HTML footer template.
    """
    params = {"path": path}
    if landscape:
        params["landscape"] = True
    if scale > 0:
        params["scale"] = scale
    if margin_top >= 0:
        params["margin_top"] = margin_top
    if margin_right >= 0:
        params["margin_right"] = margin_right
    if margin_bottom >= 0:
        params["margin_bottom"] = margin_bottom
    if margin_left >= 0:
        params["margin_left"] = margin_left
    if page_ranges:
        params["page_ranges"] = page_ranges
    if not print_background:
        params["print_background"] = False
    if header_template:
        params["header_template"] = header_template
    if footer_template:
        params["footer_template"] = footer_template
    return _result(await _send("pdf", params))


# ── Waiting ──────────────────────────────────

@mcp.tool()
async def browser_wait(seconds: float = 1) -> dict:
    """Wait for a specified number of seconds."""
    return _result(await _send("wait", {"seconds": seconds}))


@mcp.tool()
async def browser_wait_for(selector: str, timeout: int = 10) -> dict:
    """Wait for an element matching CSS selector to appear.

    Args:
        selector: CSS selector to wait for.
        timeout: Max seconds to wait (default 10).
    """
    return _result(await _send("waitfor", {
        "selector": selector, "timeout": timeout,
    }, timeout=timeout + 10))


# ── Cookies ──────────────────────────────────

@mcp.tool()
async def browser_cookies_list() -> dict:
    """List all browser cookies."""
    return _result(await _send("cookies", {"action": "list"}))


@mcp.tool()
async def browser_cookies_save(path: str = "cookies.json") -> dict:
    """Save browser cookies to a JSON file."""
    return _result(await _send("cookies", {
        "action": "save", "path": path,
    }))


@mcp.tool()
async def browser_cookies_load(path: str = "cookies.json") -> dict:
    """Load cookies from a JSON file into the browser."""
    return _result(await _send("cookies", {
        "action": "load", "path": path,
    }))


@mcp.tool()
async def browser_cookies_clear() -> dict:
    """Clear all browser cookies."""
    return _result(await _send("cookies", {"action": "clear"}))


# ── Tab Management ──────────────────────────────────

@mcp.tool()
async def browser_tab_new(url: str = "") -> dict:
    """Open a new browser tab, optionally navigating to URL.

    Args:
        url: Optional URL to open in the new tab.
    """
    params = {}
    if url:
        params["url"] = url
    return _result(await _send("tab_new", params))


@mcp.tool()
async def browser_tab_close() -> dict:
    """Close the current browser tab."""
    return _result(await _send("tab_close"))


@mcp.tool()
async def browser_tab_next() -> dict:
    """Switch to the next browser tab."""
    return _result(await _send("tab_next"))


@mcp.tool()
async def browser_tab_prev() -> dict:
    """Switch to the previous browser tab."""
    return _result(await _send("tab_prev"))


@mcp.tool()
async def browser_tab_goto(index: int) -> dict:
    """Switch to a specific tab by index (1-9).

    Args:
        index: Tab index (1 = first tab, 9 = last tab).
    """
    return _result(await _send("tab_goto", {"index": index}))


# ── Window/Popup Management ──────────────────────────────────

@mcp.tool()
async def browser_window_list() -> dict:
    """List all open browser windows (main + popups).

    Returns list of windows with title, size, and is_main flag.
    Use after OAuth buttons or window.open() to detect popups.
    """
    return _result(await _send("window_list"))


@mcp.tool()
async def browser_window_switch(index: int = -1, wid: str = "") -> dict:
    """Switch focus to a different browser window.

    Use browser_window_list first to see available windows.

    Args:
        index: Window index from browser_window_list (0-based).
        wid: Direct X11 window ID (alternative to index).
    """
    params = {}
    if index >= 0:
        params["index"] = index
    if wid:
        params["wid"] = wid
    return _result(await _send("window_switch", params))


@mcp.tool()
async def browser_window_close(force: bool = False) -> dict:
    """Close the current browser window (popup).

    Refuses to close main window unless force=True.
    After closing, focus returns to the remaining window.

    Args:
        force: Allow closing the main window (default False).
    """
    return _result(await _send("window_close", {"force": force}))


# ── Request Interception ──────────────────────────────────

@mcp.tool()
async def browser_block(patterns: list[str]) -> dict:
    """Block URLs matching patterns (substring match on fetch/XHR).

    Useful for blocking ads, trackers, or specific API calls.

    Args:
        patterns: List of URL substrings or domains to block.
    """
    return _result(await _send("block", {"patterns": patterns}))


@mcp.tool()
async def browser_unblock(patterns: list[str]) -> dict:
    """Remove URL patterns from the blocklist.

    Args:
        patterns: List of patterns to unblock.
    """
    return _result(await _send("unblock", {"patterns": patterns}))


@mcp.tool()
async def browser_blocklist() -> dict:
    """List all currently blocked URL patterns."""
    return _result(await _send("blocklist"))


# ── Multi-Step Macros ──────────────────────────────────

@mcp.tool()
async def browser_macro(steps: list[dict]) -> dict:
    """Execute a sequence of browser commands as a macro.

    Each step is {action, params, stop_on_error}.
    Example: [{"action": "goto", "params": {"url": "..."}},
              {"action": "click", "params": {"target": "button"}}]

    Args:
        steps: List of {action, params} objects to execute sequentially.
    """
    return _result(await _send("macro", {"steps": steps}, timeout=300))


# ── Console Log Capture ──────────────────────────────────

@mcp.tool()
async def browser_console_start() -> dict:
    """Start capturing browser console output (log, warn, error, info).

    Injects JS that monkey-patches console methods. Re-injects after
    navigation automatically. Call console_logs to read captured output.
    """
    return _result(await _send("console_start"))


@mcp.tool()
async def browser_console_stop() -> dict:
    """Stop capturing browser console output (stops re-injection after navigation)."""
    return _result(await _send("console_stop"))


@mcp.tool()
async def browser_console_logs(limit: int = 100,
                                clear: bool = False) -> dict:
    """Get captured console log messages.

    Args:
        limit: Max messages to return (default 100, most recent).
        clear: If True, clear the buffer after reading.
    """
    return _result(await _send("console_logs", {
        "limit": limit, "clear": clear,
    }))


@mcp.tool()
async def browser_console_clear() -> dict:
    """Clear all captured console log messages."""
    return _result(await _send("console_clear"))


# ── Downloads ──────────────────────────────────

@mcp.tool()
async def browser_downloads() -> dict:
    """List files in the browser download directory (~/.tbp/downloads/).

    Returns file names, sizes, and modification times.
    Firefox auto-downloads files here without showing a save dialog.
    """
    return _result(await _send("downloads"))


# ── Network Request Log ──────────────────────────────────

@mcp.tool()
async def browser_network_start() -> dict:
    """Start logging network requests via PerformanceObserver.

    Captures URLs, types, durations, and transfer sizes.
    Re-injects automatically after navigation.
    """
    return _result(await _send("network_start"))


@mcp.tool()
async def browser_network_stop() -> dict:
    """Stop logging network requests."""
    return _result(await _send("network_stop"))


@mcp.tool()
async def browser_network_logs(limit: int = 100,
                                clear: bool = False) -> dict:
    """Get captured network requests.

    Args:
        limit: Max entries to return (default 100, most recent).
        clear: If True, clear the buffer after reading.
    """
    return _result(await _send("network_logs", {
        "limit": limit, "clear": clear,
    }))


@mcp.tool()
async def browser_network_clear() -> dict:
    """Clear all captured network requests."""
    return _result(await _send("network_clear"))


# ── DOM Mutation Observer ──────────────────────────────────

@mcp.tool()
async def browser_observe_start() -> dict:
    """Start watching DOM mutations (childList, attributes, characterData).

    Captures element additions, removals, and attribute changes.
    Re-injects automatically after navigation.
    """
    return _result(await _send("observe_start"))


@mcp.tool()
async def browser_observe_stop() -> dict:
    """Stop watching DOM mutations."""
    return _result(await _send("observe_stop"))


@mcp.tool()
async def browser_mutations(limit: int = 100,
                             clear: bool = False) -> dict:
    """Get captured DOM mutations.

    Args:
        limit: Max entries to return (default 100, most recent).
        clear: If True, clear the buffer after reading.
    """
    return _result(await _send("mutations", {
        "limit": limit, "clear": clear,
    }))


@mcp.tool()
async def browser_mutations_clear() -> dict:
    """Clear all captured DOM mutations."""
    return _result(await _send("mutations_clear"))


# ── Element Screenshot ──────────────────────────────────

@mcp.tool()
async def browser_screenshot_element(target: str,
                                      path: str = "element.png") -> dict:
    """Screenshot a specific element by CSS selector.

    Captures only the element, not the full page.

    Args:
        target: CSS selector of the element to capture.
        path: File path to save the PNG.
    """
    return _result(await _send("screenshot_element", {
        "target": target, "path": path,
    }))


# ── Drag and Drop ──────────────────────────────────

@mcp.tool()
async def browser_drag(source: str, target: str = "",
                        dx: int = 0, dy: int = 0) -> dict:
    """Drag an element to a target element or by pixel offset.

    Performs smooth mouse drag with 10-step interpolation.

    Args:
        source: CSS selector of element to drag.
        target: CSS selector of drop target (optional).
        dx: Horizontal pixel offset (if no target).
        dy: Vertical pixel offset (if no target).
    """
    params = {"source": source}
    if target:
        params["target"] = target
    if dx:
        params["dx"] = dx
    if dy:
        params["dy"] = dy
    return _result(await _send("drag", params))


# ── Swipe Gesture ──────────────────────────────────

@mcp.tool()
async def browser_swipe(selector: str = "", x: int = 0, y: int = 0,
                         direction: str = "left", distance: int = 200,
                         speed: str = "normal", steps: int = 0) -> dict:
    """Perform a swipe gesture on an element or at coordinates.

    For touch/swipe UIs (carousels, sliders, Swiper.js). Unlike drag
    (for drag-and-drop), swipe uses realistic mouse movement that
    triggers pointer/touch event listeners.

    Args:
        selector: CSS selector of element to swipe on (optional).
        x: Start X coordinate (used if no selector, or as override).
        y: Start Y coordinate (used if no selector, or as override).
        direction: Swipe direction: left, right, up, down.
        distance: Swipe distance in pixels (default 200).
        speed: Movement speed: slow, normal, fast.
        steps: Number of intermediate move events (0=auto).
    """
    params = {"direction": direction, "distance": distance, "speed": speed}
    if selector:
        params["selector"] = selector
    if x:
        params["x"] = x
    if y:
        params["y"] = y
    if steps > 0:
        params["steps"] = steps
    return _result(await _send("swipe", params))


# ── Iframe Support ──────────────────────────────────

@mcp.tool()
async def browser_iframe_list() -> dict:
    """List all iframes on the current page.

    Returns iframe index, src, name, accessibility status, and CSS selector.
    """
    return _result(await _send("iframe_list"))


@mcp.tool()
async def browser_iframe_eval(selector: str, expression: str) -> dict:
    """Evaluate JavaScript inside a specific iframe.

    Args:
        selector: CSS selector of the iframe element.
        expression: JavaScript expression to evaluate in the iframe context.
    """
    return _result(await _send("iframe_eval", {
        "selector": selector, "expression": expression,
    }))


@mcp.tool()
async def browser_iframe_text(selector: str,
                               inner_selector: str = "") -> dict:
    """Get text content from inside an iframe.

    Args:
        selector: CSS selector of the iframe element.
        inner_selector: Optional CSS selector for specific element inside iframe.
    """
    params = {"selector": selector}
    if inner_selector:
        params["inner_selector"] = inner_selector
    return _result(await _send("iframe_text", params))


@mcp.tool()
async def browser_iframe_click(selector: str, target: str,
                               x: int = 0, y: int = 0) -> dict:
    """Click an element inside an iframe.

    For same-origin iframes, clicks by CSS selector inside the iframe.
    For cross-origin iframes, falls back to coordinate click within
    the iframe bounds. Use x/y offsets from iframe top-left corner
    to target specific elements in cross-origin iframes.

    Args:
        selector: CSS selector of the iframe element.
        target: CSS selector of the element to click inside the iframe.
        x: X offset from iframe top-left for cross-origin fallback.
        y: Y offset from iframe top-left for cross-origin fallback.
    """
    return _result(await _send("iframe_click", {
        "selector": selector, "target": target, "x": x, "y": y,
    }))


# ── File Upload ──────────────────────────────────

@mcp.tool()
async def browser_upload(selector: str, path: str) -> dict:
    """Upload a file to an input[type=file] element.

    Creates a File object from disk and sets it on the input.
    Max file size: 5MB.

    Args:
        selector: CSS selector of the file input element.
        path: Absolute path to the file to upload.
    """
    return _result(await _send("upload", {
        "selector": selector, "path": path,
    }))


# ── Geolocation Spoofing ──────────────────────────────────

@mcp.tool()
async def browser_geo_set(latitude: float, longitude: float,
                           accuracy: float = 100) -> dict:
    """Override browser geolocation (navigator.geolocation).

    Spoofs getCurrentPosition and watchPosition. Re-injected after navigation.

    Args:
        latitude: Latitude (-90 to 90).
        longitude: Longitude (-180 to 180).
        accuracy: Accuracy in meters (default 100).
    """
    return _result(await _send("geo_set", {
        "latitude": latitude, "longitude": longitude, "accuracy": accuracy,
    }))


@mcp.tool()
async def browser_geo_clear() -> dict:
    """Clear geolocation override (restore real behavior)."""
    return _result(await _send("geo_clear"))


# ── User Agent Switching ──────────────────────────────────

@mcp.tool()
async def browser_useragent_set(useragent: str) -> dict:
    """Override navigator.userAgent (JS-side). Re-injected after navigation.

    Note: This overrides the JS property only. The HTTP User-Agent header
    sent to servers is unchanged (would require browser restart).

    Args:
        useragent: The user agent string to spoof.
    """
    return _result(await _send("useragent_set", {"useragent": useragent}))


@mcp.tool()
async def browser_useragent_clear() -> dict:
    """Clear user agent override (restore original navigator.userAgent)."""
    return _result(await _send("useragent_clear"))


# ── Cookie Injection ──────────────────────────────────

@mcp.tool()
async def browser_cookie_set(name: str, value: str, domain: str = "",
                              path: str = "/", max_age: int | None = None,
                              secure: bool = False,
                              samesite: str = "") -> dict:
    """Set a cookie via document.cookie.

    Args:
        name: Cookie name.
        value: Cookie value.
        domain: Cookie domain (optional, defaults to current).
        path: Cookie path (default /).
        max_age: Expiration in seconds (optional).
        secure: Set secure flag.
        samesite: SameSite attribute (Strict, Lax, or None).
    """
    params = {"name": name, "value": value, "path": path}
    if domain:
        params["domain"] = domain
    if max_age is not None:
        params["max_age"] = max_age
    if secure:
        params["secure"] = True
    if samesite:
        params["samesite"] = samesite
    return _result(await _send("cookie_set", params))


# ── Local/Session Storage ──────────────────────────────────

@mcp.tool()
async def browser_storage_list(storage_type: str = "local",
                                limit: int = 100) -> dict:
    """List all items in localStorage or sessionStorage.

    Args:
        storage_type: 'local' or 'session'.
        limit: Max items to return (default 100).
    """
    return _result(await _send("storage", {
        "type": storage_type, "action": "list", "limit": limit,
    }))


@mcp.tool()
async def browser_storage_get(key: str,
                               storage_type: str = "local") -> dict:
    """Get a value from localStorage or sessionStorage.

    Args:
        key: Storage key to read.
        storage_type: 'local' or 'session'.
    """
    return _result(await _send("storage", {
        "type": storage_type, "action": "get", "key": key,
    }))


@mcp.tool()
async def browser_storage_set(key: str, value: str,
                               storage_type: str = "local") -> dict:
    """Set a value in localStorage or sessionStorage.

    Args:
        key: Storage key.
        value: Value to store.
        storage_type: 'local' or 'session'.
    """
    return _result(await _send("storage", {
        "type": storage_type, "action": "set", "key": key, "value": value,
    }))


@mcp.tool()
async def browser_storage_remove(key: str,
                                  storage_type: str = "local") -> dict:
    """Remove a key from localStorage or sessionStorage.

    Args:
        key: Storage key to remove.
        storage_type: 'local' or 'session'.
    """
    return _result(await _send("storage", {
        "type": storage_type, "action": "remove", "key": key,
    }))


@mcp.tool()
async def browser_storage_clear(storage_type: str = "local") -> dict:
    """Clear all items in localStorage or sessionStorage.

    Args:
        storage_type: 'local' or 'session'.
    """
    return _result(await _send("storage", {
        "type": storage_type, "action": "clear",
    }))


# ── Clipboard Access ──────────────────────────────────

@mcp.tool()
async def browser_clipboard_read() -> dict:
    """Read text from the Xvfb system clipboard.

    Useful for reading content after a copy operation on a web page.
    """
    return _result(await _send("clipboard_read"))


@mcp.tool()
async def browser_clipboard_write(text: str) -> dict:
    """Write text to the Xvfb system clipboard.

    Useful for setting clipboard before a Ctrl+V paste on a web page.

    Args:
        text: Text to write to clipboard.
    """
    return _result(await _send("clipboard_write", {"text": text}))


# ── Form Auto-fill ──────────────────────────────────

@mcp.tool()
async def browser_form_fill(fields: list) -> dict:
    """Fill multiple form fields at once.

    Handles text inputs, selects, checkboxes, and radio buttons.
    Dispatches input + change events for framework compatibility.

    Args:
        fields: List of {selector, value} objects. Max 100 fields.
                For checkboxes/radios, use true/false as value.
    """
    return _result(await _send("form_fill", {"fields": fields}))


# ── CSS Injection ──────────────────────────────────

@mcp.tool()
async def browser_css_inject(css: str, id: str = "") -> dict:
    """Inject a custom stylesheet into the page.

    Re-injected after navigation automatically.

    Args:
        css: CSS rules to inject (e.g. "body { background: red }").
        id: Optional stylesheet ID. Auto-generated if omitted.
    """
    params = {"css": css}
    if id:
        params["id"] = id
    return _result(await _send("css_inject", params))


@mcp.tool()
async def browser_css_remove(id: str = "") -> dict:
    """Remove injected stylesheet(s).

    Args:
        id: Stylesheet ID to remove. If empty, removes all.
    """
    params = {}
    if id:
        params["id"] = id
    return _result(await _send("css_remove", params))


@mcp.tool()
async def browser_css_list() -> dict:
    """List all injected custom stylesheets."""
    return _result(await _send("css_list"))


# ── Wait+Action ──────────────────────────────────

@mcp.tool()
async def browser_waitact(selector: str, action: str = "click",
                           value: str = "", timeout: int = 10) -> dict:
    """Wait for an element to appear then perform an action.

    Uses MutationObserver to detect element appearance.

    Args:
        selector: CSS selector to wait for.
        action: Action to perform: "click", "type", or "text".
        value: Text to type (required if action is "type").
        timeout: Max seconds to wait (default 10, max 120).
    """
    params = {"selector": selector, "action": action, "timeout": timeout}
    if value:
        params["value"] = value
    return _result(await _send("waitact", params, timeout=timeout + 15))


# ── Page Event Capture ──────────────────────────────────

@mcp.tool()
async def browser_events_start(types: list[str] | None = None) -> dict:
    """Start capturing DOM events on the page.

    Captures event type, target element, timestamp, and relevant data
    (value for input/change, key for keydown, action for submit).
    Re-injects after navigation automatically.

    Args:
        types: Event types to capture. Default: click, submit, input, change, keydown.
    """
    params = {}
    if types:
        params["types"] = types
    return _result(await _send("events_start", params))


@mcp.tool()
async def browser_events_stop() -> dict:
    """Stop capturing DOM events and remove listeners."""
    return _result(await _send("events_stop"))


@mcp.tool()
async def browser_events_logs(limit: int = 100,
                                clear: bool = False) -> dict:
    """Get captured DOM events.

    Args:
        limit: Max entries to return (default 100, most recent).
        clear: If True, clear the buffer after reading.
    """
    return _result(await _send("events_logs", {
        "limit": limit, "clear": clear,
    }))


@mcp.tool()
async def browser_events_clear() -> dict:
    """Clear all captured DOM events."""
    return _result(await _send("events_clear"))


# ── Viewport/Window Resize ──────────────────────────────────

@mcp.tool()
async def browser_viewport_set(width: int, height: int) -> dict:
    """Resize the browser window.

    Args:
        width: Window width in pixels (100-7680).
        height: Window height in pixels (100-4320).
    """
    return _result(await _send("viewport_set", {
        "width": width, "height": height,
    }))


@mcp.tool()
async def browser_viewport_get() -> dict:
    """Get current window and viewport dimensions.

    Returns window size, inner viewport size, and device pixel ratio.
    """
    return _result(await _send("viewport_get"))


# ── Page Search ──────────────────────────────────

@mcp.tool()
async def browser_search(query: str,
                          case_sensitive: bool = False) -> dict:
    """Find text on the page and highlight all matches.

    Returns match count. First match scrolled into view.
    Use browser_search_next/prev to navigate between matches.

    Args:
        query: Text to search for.
        case_sensitive: If True, match case exactly (default False).
    """
    params = {"query": query}
    if case_sensitive:
        params["case_sensitive"] = True
    return _result(await _send("search", params))


@mcp.tool()
async def browser_search_next() -> dict:
    """Navigate to the next search match and scroll into view."""
    return _result(await _send("search_next"))


@mcp.tool()
async def browser_search_prev() -> dict:
    """Navigate to the previous search match and scroll into view."""
    return _result(await _send("search_prev"))


@mcp.tool()
async def browser_search_clear() -> dict:
    """Clear all search highlights and reset search state."""
    return _result(await _send("search_clear"))


# ── Shadow DOM ──────────────────────────────────

@mcp.tool()
async def browser_shadow_query(selector: str) -> dict:
    """Find an element by CSS selector, piercing shadow DOM boundaries.

    Recursively searches through shadow roots of all elements.
    Returns element tag, id, className, text, and all attributes.

    Args:
        selector: CSS selector to search for.
    """
    return _result(await _send("shadow_query", {"selector": selector}))


@mcp.tool()
async def browser_shadow_text(selector: str) -> dict:
    """Get text content from an element inside shadow DOM.

    Args:
        selector: CSS selector (searched through shadow roots).
    """
    return _result(await _send("shadow_text", {"selector": selector}))


@mcp.tool()
async def browser_shadow_click(selector: str) -> dict:
    """Click an element inside shadow DOM.

    Args:
        selector: CSS selector (searched through shadow roots).
    """
    return _result(await _send("shadow_click", {"selector": selector}))


# ── Response Body Capture ──────────────────────────────────

@mcp.tool()
async def browser_responses_start() -> dict:
    """Start capturing fetch/XHR response bodies.

    Hooks fetch() and XMLHttpRequest to capture response text
    (truncated to 10KB per response, max 500 entries).
    Re-injects after navigation automatically.
    """
    return _result(await _send("responses_start"))


@mcp.tool()
async def browser_responses_stop() -> dict:
    """Stop capturing response bodies (stops re-injection)."""
    return _result(await _send("responses_stop"))


@mcp.tool()
async def browser_responses_logs(limit: int = 100,
                                  clear: bool = False) -> dict:
    """Get captured response bodies.

    Args:
        limit: Max entries to return (default 100, most recent).
        clear: If True, clear the buffer after reading.
    """
    return _result(await _send("responses_logs", {
        "limit": limit, "clear": clear,
    }))


@mcp.tool()
async def browser_responses_clear() -> dict:
    """Clear all captured response bodies."""
    return _result(await _send("responses_clear"))


# ── Multi-tab Session Management ──────────────────────────────────

@mcp.tool()
async def browser_session_save(name: str) -> dict:
    """Save all open tabs as a named session.

    Iterates through tabs (Ctrl+1-9), collects URL and title.
    Stored in ~/.tbp/sessions/{name}.json.

    Args:
        name: Session name (alphanumeric, hyphens, underscores only).
    """
    return _result(await _send("session_save", {"name": name}, timeout=60))


@mcp.tool()
async def browser_session_load(name: str, timeout: int = 45) -> dict:
    """Load a saved session (opens tabs and navigates to saved URLs).

    Args:
        name: Session name to load.
        timeout: Navigation timeout per tab in seconds (default 45).
    """
    return _result(await _send("session_load", {
        "name": name, "timeout": timeout,
    }, timeout=300))


@mcp.tool()
async def browser_session_list() -> dict:
    """List all saved multi-tab sessions."""
    return _result(await _send("session_list"))


@mcp.tool()
async def browser_session_delete(name: str) -> dict:
    """Delete a saved session.

    Args:
        name: Session name to delete.
    """
    return _result(await _send("session_delete", {"name": name}))


# ── HTTP Header Injection ──────────────────────────────────

@mcp.tool()
async def browser_headers_set(headers: dict) -> dict:
    """Set custom HTTP headers injected into fetch/XHR requests.

    Re-injected after navigation automatically.

    Args:
        headers: Dict of header name-value pairs (e.g. {"X-Custom": "value"}).
    """
    return _result(await _send("headers_set", {"headers": headers}))


@mcp.tool()
async def browser_headers_clear() -> dict:
    """Clear all custom HTTP headers and remove fetch/XHR hooks."""
    return _result(await _send("headers_clear"))


@mcp.tool()
async def browser_headers_list() -> dict:
    """List currently active custom HTTP headers."""
    return _result(await _send("headers_list"))


# ── Page Performance Metrics ──────────────────────────────────

@mcp.tool()
async def browser_perf() -> dict:
    """Get page performance metrics.

    Returns timing (first byte, DOM interactive, load complete),
    resource counts (scripts, stylesheets, images, iframes),
    and JS heap memory usage.
    """
    return _result(await _send("perf"))


# ── Element Attributes ──────────────────────────────────

@mcp.tool()
async def browser_attr_get(selector: str, name: str = "") -> dict:
    """Get attribute(s) from an element.

    Args:
        selector: CSS selector of the element.
        name: Attribute name. If empty, returns all attributes.
    """
    params = {"selector": selector}
    if name:
        params["name"] = name
    return _result(await _send("attr_get", params))


@mcp.tool()
async def browser_attr_set(selector: str, name: str,
                            value: str = "") -> dict:
    """Set an attribute on an element.

    Args:
        selector: CSS selector of the element.
        name: Attribute name to set.
        value: Attribute value.
    """
    return _result(await _send("attr_set", {
        "selector": selector, "name": name, "value": value,
    }))


@mcp.tool()
async def browser_attr_remove(selector: str, name: str) -> dict:
    """Remove an attribute from an element.

    Args:
        selector: CSS selector of the element.
        name: Attribute name to remove.
    """
    return _result(await _send("attr_remove", {
        "selector": selector, "name": name,
    }))


# ── Browser Profile Management ──────────────────────────────────

@mcp.tool()
async def browser_profile_save(name: str) -> dict:
    """Save current browser state (cookies + localStorage + URL) as a profile.

    Args:
        name: Profile name (alphanumeric, hyphens, underscores only).
    """
    return _result(await _send("profile_save", {"name": name}))


@mcp.tool()
async def browser_profile_load(name: str) -> dict:
    """Load a saved browser profile (restores cookies + localStorage).

    Args:
        name: Profile name to load.
    """
    return _result(await _send("profile_load", {"name": name}))


@mcp.tool()
async def browser_profile_list() -> dict:
    """List all saved browser profiles."""
    return _result(await _send("profile_list"))


@mcp.tool()
async def browser_profile_delete(name: str) -> dict:
    """Delete a saved browser profile.

    Args:
        name: Profile name to delete.
    """
    return _result(await _send("profile_delete", {"name": name}))


# ── Element highlight ──────────────────────────────────

@mcp.tool()
async def browser_highlight(selector: str, color: str = "red",
                             label: str = "") -> dict:
    """Highlight elements matching a CSS selector with colored outline.

    Args:
        selector: CSS selector for elements to highlight.
        color: CSS color for outline (default 'red').
        label: Optional tooltip label for highlighted elements.
    """
    params = {"selector": selector, "color": color}
    if label:
        params["label"] = label
    return _result(await _send("highlight", params))


@mcp.tool()
async def browser_highlight_clear(selector: str = "") -> dict:
    """Remove element highlights.

    Args:
        selector: CSS selector to clear (empty = clear all).
    """
    params = {}
    if selector:
        params["selector"] = selector
    return _result(await _send("highlight_clear", params))


# ── Cookie auto-login (auth sessions) ──────────────────────────────────

@mcp.tool()
async def browser_auth_save(name: str) -> dict:
    """Save cookies for current domain as a named auth session.

    Args:
        name: Session name (alphanumeric, -, _).
    """
    return _result(await _send("auth_save", {"name": name}))


@mcp.tool()
async def browser_auth_load(name: str, timeout: int = 45) -> dict:
    """Load auth session: restore cookies and navigate to saved URL.

    Args:
        name: Session name to load.
        timeout: Navigation timeout in seconds.
    """
    return _result(await _send("auth_load", {"name": name,
                                              "timeout": timeout}))


@mcp.tool()
async def browser_auth_list() -> dict:
    """List all saved auth sessions."""
    return _result(await _send("auth_list"))


@mcp.tool()
async def browser_auth_delete(name: str) -> dict:
    """Delete a saved auth session.

    Args:
        name: Session name to delete.
    """
    return _result(await _send("auth_delete", {"name": name}))


# ── Network throttling ──────────────────────────────────

@mcp.tool()
async def browser_throttle_set(preset: str = "",
                                latency: int = -1) -> dict:
    """Set network throttling to simulate slow connections.

    Args:
        preset: Throttle preset (3g, slow-3g, fast-3g, offline).
        latency: Custom latency in ms (if preset not used, -1 = unused).
    """
    params = {}
    if preset:
        params["preset"] = preset
    elif latency >= 0:
        params["latency"] = latency
    else:
        return {"error": "Provide preset or latency"}
    return _result(await _send("throttle_set", params))


@mcp.tool()
async def browser_throttle_clear() -> dict:
    """Remove network throttling."""
    return _result(await _send("throttle_clear"))


@mcp.tool()
async def browser_throttle_get() -> dict:
    """Get current network throttle configuration."""
    return _result(await _send("throttle_get"))


# ── Annotated screenshot ──────────────────────────────────

@mcp.tool()
async def browser_screenshot_annotate(
    path: str = "annotated.png",
    selector: str = "",
    max_elements: int = 25,
    full: bool = False,
) -> dict:
    """Screenshot with numbered badges on interactive elements + legend.

    Returns legend mapping numbers to CSS selectors, text, and positions.
    AI can use legend selectors for subsequent click/type actions.

    Args:
        path: Output file path.
        selector: CSS selector (default: all interactive elements).
        max_elements: Max elements to label (1-100).
        full: Full page screenshot.
    """
    params = {"path": path, "max": max_elements, "full": full}
    if selector:
        params["selector"] = selector
    return _result(await _send("screenshot_annotate", params))


# ── Page audit ──────────────────────────────────

@mcp.tool()
async def browser_audit() -> dict:
    """One-command page health report.

    Returns: title, URL, element count, links (total/external/broken),
    images (total/missing-alt/broken), forms, headings hierarchy,
    meta tags, scripts, styles, page size, load time, console errors.
    """
    return _result(await _send("audit"))


# ── Response mocking ──────────────────────────────────

@mcp.tool()
async def browser_mock_set(
    pattern: str,
    body: str,
    status: int = 200,
    content_type: str = "application/json",
) -> dict:
    """Add a response mock: matching fetch/XHR requests return fake data.

    Args:
        pattern: URL substring to match.
        body: Response body string.
        status: HTTP status code (default 200).
        content_type: Content-Type header (default application/json).
    """
    return _result(await _send("mock_set", {
        "pattern": pattern, "body": body,
        "status": status, "content_type": content_type,
    }))


@mcp.tool()
async def browser_mock_clear(pattern: str = "") -> dict:
    """Remove response mock(s).

    Args:
        pattern: URL pattern to remove (empty = clear all).
    """
    params = {}
    if pattern:
        params["pattern"] = pattern
    return _result(await _send("mock_clear", params))


@mcp.tool()
async def browser_mock_list() -> dict:
    """List all active response mocks."""
    return _result(await _send("mock_list"))


# ── DOM snapshot & diff ──────────────────────────────────

@mcp.tool()
async def browser_snapshot_take(name: str) -> dict:
    """Capture current page state as a named snapshot.

    Captures: URL, title, element count, text hash, form values,
    visible text (first 2000 chars). Use snapshot_diff to compare.

    Args:
        name: Snapshot name (alphanumeric).
    """
    return _result(await _send("snapshot_take", {"name": name}))


@mcp.tool()
async def browser_snapshot_diff(name1: str, name2: str) -> dict:
    """Compare two snapshots and return structured diff.

    Shows: URL/title changes, element count delta, text changes
    (word-level diff), form value changes.

    Args:
        name1: First (before) snapshot name.
        name2: Second (after) snapshot name.
    """
    return _result(await _send("snapshot_diff", {
        "name1": name1, "name2": name2,
    }))


@mcp.tool()
async def browser_snapshot_list() -> dict:
    """List all in-memory snapshots."""
    return _result(await _send("snapshot_list"))


@mcp.tool()
async def browser_snapshot_delete(name: str) -> dict:
    """Delete a named snapshot.

    Args:
        name: Snapshot name to delete.
    """
    return _result(await _send("snapshot_delete", {"name": name}))


# ── Double-click ──────────────────────────────────

@mcp.tool()
async def browser_dblclick(target: str, human: bool = False) -> dict:
    """Double-click an element by CSS selector.

    Dispatches a dblclick MouseEvent on the element.

    Args:
        target: CSS selector of element to double-click.
        human: If True, use xdotool double-click (human-like).
    """
    return _result(await _send("dblclick", {
        "target": target, "human": human,
    }))


# ── Select dropdown ──────────────────────────────────

@mcp.tool()
async def browser_select(selector: str, value: str = "",
                          label: str = "", index: int = -1) -> dict:
    """Select a dropdown option by value, label, or index.

    Dispatches change + input events for framework compatibility.

    Args:
        selector: CSS selector of the <select> element.
        value: Option value to select.
        label: Option visible text to select.
        index: Option index to select (-1 = unused).
    """
    params = {"selector": selector}
    if value:
        params["value"] = value
    elif label:
        params["label"] = label
    elif index >= 0:
        params["index"] = index
    else:
        return {"error": "Provide value, label, or index"}
    return _result(await _send("select", params))


# ── Checkbox / Radio ──────────────────────────────────

@mcp.tool()
async def browser_check(selector: str,
                          action: str = "check") -> dict:
    """Check, uncheck, or toggle a checkbox or radio button.

    Dispatches change + input events for framework compatibility.

    Args:
        selector: CSS selector of the checkbox/radio element.
        action: "check" (set checked), "uncheck" (clear), or "toggle".
    """
    return _result(await _send("check", {
        "selector": selector, "action": action,
    }))


# ── Input value ──────────────────────────────────

@mcp.tool()
async def browser_input_value(selector: str) -> dict:
    """Read the current value of an input, select, or textarea element.

    Returns value, tag name, and input type.

    Args:
        selector: CSS selector of the form element.
    """
    return _result(await _send("input_value", {"selector": selector}))


# ── Element state ──────────────────────────────────

@mcp.tool()
async def browser_element_state(selector: str) -> dict:
    """Query element visibility, enabled, checked, and editable state.

    Returns {visible, enabled, checked, editable, tag, type}.

    Args:
        selector: CSS selector of the element.
    """
    return _result(await _send("element_state", {"selector": selector}))


# ── Bounding box ──────────────────────────────────

@mcp.tool()
async def browser_bounding_box(selector: str) -> dict:
    """Get element position and dimensions via getBoundingClientRect.

    Returns {x, y, width, height, top, left, bottom, right, scrollX, scrollY}.

    Args:
        selector: CSS selector of the element.
    """
    return _result(await _send("bounding_box", {"selector": selector}))


# ── Scroll to element ──────────────────────────────────

@mcp.tool()
async def browser_scroll_to(selector: str,
                              block: str = "center") -> dict:
    """Scroll an element into view using scrollIntoView.

    Args:
        selector: CSS selector of the element.
        block: Vertical alignment: "center", "start", "end", "nearest".
    """
    return _result(await _send("scroll_to", {
        "selector": selector, "block": block,
    }))


# ── Set page content ──────────────────────────────────

@mcp.tool()
async def browser_set_content(html: str) -> dict:
    """Replace entire page content with raw HTML (no navigation).

    Useful for testing or rendering generated HTML.

    Args:
        html: HTML string to load into the page.
    """
    return _result(await _send("set_content", {"html": html}))


# ── Dialog handling (alert/confirm/prompt) ──────────────────────────────────

@mcp.tool()
async def browser_dialog_handle(accept: bool = True,
                                  prompt_text: str = "") -> dict:
    """Configure auto-handling of browser dialogs (alert/confirm/prompt).

    Monkey-patches window.alert/confirm/prompt to auto-respond.
    Re-injected after navigation. Use dialog_logs to see captured messages.

    Args:
        accept: Accept dialogs (True) or dismiss (False).
        prompt_text: Text to return for prompt() dialogs.
    """
    params = {"accept": accept}
    if prompt_text:
        params["prompt_text"] = prompt_text
    return _result(await _send("dialog_handle", params))


@mcp.tool()
async def browser_dialog_dismiss() -> dict:
    """Configure dialogs to be dismissed (shortcut for accept=False)."""
    return _result(await _send("dialog_dismiss"))


@mcp.tool()
async def browser_dialog_logs(limit: int = 100,
                                clear: bool = False) -> dict:
    """Get captured dialog messages (alert/confirm/prompt).

    Args:
        limit: Max entries to return (default 100).
        clear: If True, clear the buffer after reading.
    """
    return _result(await _send("dialog_logs", {
        "limit": limit, "clear": clear,
    }))


@mcp.tool()
async def browser_dialog_clear() -> dict:
    """Clear all captured dialog messages."""
    return _result(await _send("dialog_clear"))


# ── Wait for response ──────────────────────────────────

@mcp.tool()
async def browser_waitfor_response(pattern: str,
                                     timeout: int = 10) -> dict:
    """Wait for a fetch/XHR response matching a URL pattern.

    Polls response capture buffer for a response whose URL contains
    the pattern substring. Enables response capture if not already active.

    Args:
        pattern: URL substring to match against response URLs.
        timeout: Max seconds to wait (1-120, default 10).
    """
    return _result(await _send("waitfor_response", {
        "pattern": pattern, "timeout": timeout,
    }, timeout=timeout + 15))


# ── Status ──────────────────────────────────

@mcp.tool()
async def browser_status() -> dict:
    """Get daemon status: PID, browser type, current URL, uptime."""
    return _result(await _send("status"))


# ── Daemon Lifecycle ──────────────────────────────────

@mcp.tool()
async def browser_type_otp(digits: str, selector: str = "",
                           method: str = "auto",
                           delay: float = 0.15) -> dict:
    """Type OTP/verification digits into individual input fields.

    Auto-detects OTP input groups (maxlength=1, data-index, etc.) and
    enters each digit with human-like delays. Works with shadow DOM and
    cross-origin contexts.

    Args:
        digits: Numeric string to enter (e.g. "854698").
        selector: Optional CSS selector for OTP container element.
        method: "auto" (click+verify+fallback), "click_each" (click per
                field), "type" (Tab between fields).
        delay: Seconds between digits (default 0.15).
    """
    return _result(await _send("type_otp", {
        "digits": digits, "selector": selector,
        "method": method, "delay": delay,
    }))


@mcp.tool()
async def browser_detect_challenge() -> dict:
    """Detect CAPTCHAs, bot challenges, and security prompts on the page.

    Checks for reCAPTCHA, hCaptcha, FunCaptcha, Cloudflare Turnstile,
    DataDome, PerimeterX, generic security text, and OTP/2FA prompts.
    Returns {challenged: bool, signals: [{type, confidence, detail}]}.
    Informational — use the result to decide next steps (screenshot,
    ask user, solve, etc.).
    """
    return _result(await _send("detect_challenge"))


@mcp.tool()
async def browser_stop() -> dict:
    """Gracefully stop the browser daemon. Saves session and cleans up.

    The daemon will stop Firefox, remove the socket, and exit.
    Next browser command will auto-start a fresh daemon.
    """
    return _result(await _send("shutdown"))


@mcp.tool()
async def browser_restart() -> dict:
    """Restart the browser daemon (stop + start fresh).

    Useful when Firefox gets into a bad state, or to reset all state.
    """
    try:
        await _send("shutdown")
    except Exception:
        pass
    import asyncio as _asyncio
    socket_path = os.path.expanduser("~/.tbp/daemon.sock")
    for _ in range(30):
        if not os.path.exists(socket_path):
            break
        await _asyncio.sleep(0.5)
    return _result(await _send("status"))


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
