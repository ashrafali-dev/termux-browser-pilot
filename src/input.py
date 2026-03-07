"""Keyboard and mouse input simulation via CDP."""

import asyncio
import json
import math
import random

from ._utils import escape_js_string

# Recursive shadow DOM querySelector — pierces shadow roots
_DEEP_QUERY_JS = (
    "function __tbp_dq(root,sel){"
    "var el=root.querySelector(sel);if(el)return el;"
    "var all=root.querySelectorAll('*');"
    "for(var i=0;i<all.length;i++){"
    "if(all[i].shadowRoot){"
    "var found=__tbp_dq(all[i].shadowRoot,sel);"
    "if(found)return found}}return null}"
)


def _bezier_curve(start, end, steps=None):
    """Generate human-like Bezier curve points between two positions.

    Uses a cubic Bezier with randomized control points to simulate
    natural hand movement. Varying speed (ease-in/ease-out).
    """
    sx, sy = start
    ex, ey = end
    dist = math.hypot(ex - sx, ey - sy)

    # More steps for longer distances, minimum 8
    if steps is None:
        steps = max(8, min(40, int(dist / 15)))

    # Random control points offset from the straight line
    spread = max(30, dist * 0.3)
    cp1x = sx + (ex - sx) * 0.25 + random.uniform(-spread, spread) * 0.5
    cp1y = sy + (ey - sy) * 0.25 + random.uniform(-spread, spread) * 0.5
    cp2x = sx + (ex - sx) * 0.75 + random.uniform(-spread, spread) * 0.3
    cp2y = sy + (ey - sy) * 0.75 + random.uniform(-spread, spread) * 0.3

    points = []
    for i in range(steps + 1):
        t = i / steps
        # Ease-in-out timing
        t = t * t * (3 - 2 * t)
        x = (1-t)**3 * sx + 3*(1-t)**2*t * cp1x + 3*(1-t)*t**2 * cp2x + t**3 * ex
        y = (1-t)**3 * sy + 3*(1-t)**2*t * cp1y + 3*(1-t)*t**2 * cp2y + t**3 * ey
        # Add micro-jitter (real hands aren't pixel-perfect)
        x += random.gauss(0, 0.5)
        y += random.gauss(0, 0.5)
        points.append((x, y))

    # Ensure final point is exact target
    points[-1] = (ex, ey)
    return points


class InputCommands:
    """Simulate keyboard and mouse events."""

    def __init__(self, session):
        self.session = session

    # --- Mouse ---

    async def click(self, selector=None, x=None, y=None, button="left",
                    click_count=1, delay=0.05):
        """Click on element by selector or coordinates (x/y fallback)."""
        if selector:
            try:
                x, y = await self._get_element_center(selector)
            except (ValueError, Exception):
                if x is None or y is None:
                    raise

        if x is None or y is None:
            raise ValueError("Must provide selector or x,y coordinates")

        valid_buttons = {"left", "right", "middle"}
        if button not in valid_buttons:
            raise ValueError(f"Invalid button '{button}', must be one of: {valid_buttons}")
        btn = button

        await self.session.send("Input.dispatchMouseEvent", {
            "type": "mouseMoved", "x": x, "y": y,
        })
        await asyncio.sleep(delay)

        await self.session.send("Input.dispatchMouseEvent", {
            "type": "mousePressed", "x": x, "y": y,
            "button": btn, "clickCount": click_count,
        })
        await asyncio.sleep(delay)

        await self.session.send("Input.dispatchMouseEvent", {
            "type": "mouseReleased", "x": x, "y": y,
            "button": btn, "clickCount": click_count,
        })

    async def double_click(self, selector=None, x=None, y=None):
        """Double-click an element."""
        await self.click(selector=selector, x=x, y=y, click_count=2)

    async def hover(self, selector=None, x=None, y=None):
        """Hover over an element."""
        if selector:
            try:
                x, y = await self._get_element_center(selector)
            except (ValueError, Exception):
                if x is None or y is None:
                    raise
        if x is None or y is None:
            raise ValueError("Must provide selector or x,y coordinates")
        await self.session.send("Input.dispatchMouseEvent", {
            "type": "mouseMoved", "x": x, "y": y,
        })

    async def scroll(self, x=0, y=0, delta_x=0, delta_y=300):
        """Scroll the page. Positive delta_y = scroll down (matches wheel event spec)."""
        await self.session.send("Input.dispatchMouseEvent", {
            "type": "mouseWheel", "x": x, "y": y,
            "deltaX": delta_x, "deltaY": delta_y,
        })

    # --- Keyboard ---

    async def type_text(self, text, delay=0.03, mode="auto"):
        """Type text. mode: 'clipboard' (paste), 'xdotool' (direct), 'auto' (paste+verify+fallback)."""
        await self.session.send("Input.insertText", {"text": text, "mode": mode})

    async def press_key(self, key, modifiers=0):
        """Press a special key (Enter, Tab, Escape, etc.)."""
        key_map = {
            "Enter": {"key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13},
            "Tab": {"key": "Tab", "code": "Tab", "windowsVirtualKeyCode": 9},
            "Escape": {"key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27},
            "Backspace": {"key": "Backspace", "code": "Backspace", "windowsVirtualKeyCode": 8},
            "ArrowUp": {"key": "ArrowUp", "code": "ArrowUp", "windowsVirtualKeyCode": 38},
            "ArrowDown": {"key": "ArrowDown", "code": "ArrowDown", "windowsVirtualKeyCode": 40},
            "ArrowLeft": {"key": "ArrowLeft", "code": "ArrowLeft", "windowsVirtualKeyCode": 37},
            "ArrowRight": {"key": "ArrowRight", "code": "ArrowRight", "windowsVirtualKeyCode": 39},
            "Space": {"key": " ", "code": "Space", "windowsVirtualKeyCode": 32},
        }

        info = key_map.get(key, {"key": key, "code": key, "windowsVirtualKeyCode": 0})
        await self.session.send("Input.dispatchKeyEvent", {
            "type": "rawKeyDown", "modifiers": modifiers, **info,
        })
        await self.session.send("Input.dispatchKeyEvent", {
            "type": "keyUp", "modifiers": modifiers, **info,
        })

    async def fill(self, selector, text, x=None, y=None, mode="auto"):
        """Focus element and fill with text (clears first). x/y fallback."""
        # Try JS-based fill first (most reliable — avoids console toggle
        # focus loss that breaks keyboard-based fill in native Firefox)
        if selector:
            safe_sel = escape_js_string(selector)
            escaped_text = json.dumps(text)
            try:
                result = await self.session.send("Runtime.evaluate", {
                    "expression": (
                        "(function(){"
                        f"{_DEEP_QUERY_JS}"
                        f"var el=__tbp_dq(document,'{safe_sel}');"
                        "if(!el)return 'NOT_FOUND';"
                        "el.focus();"
                        "var s=Object.getOwnPropertyDescriptor("
                        "window.HTMLInputElement.prototype,'value')"
                        "||Object.getOwnPropertyDescriptor("
                        "window.HTMLTextAreaElement.prototype,'value');"
                        f"if(s&&s.set){{s.set.call(el,{escaped_text});}}"
                        f"else{{el.value={escaped_text};}}"
                        "el.dispatchEvent(new Event('input',{bubbles:true}));"
                        "el.dispatchEvent(new Event('change',{bubbles:true}));"
                        "return el.value})()"
                    ),
                    "returnByValue": True,
                })
                val = result.get("result", {}).get("value", "")
                if val and text[:20] in str(val):
                    return
            except Exception:
                pass
        # Fallback: keyboard-based fill
        await self.click(selector=selector, x=x, y=y)
        await asyncio.sleep(0.1)
        # Select all + delete existing
        await self.session.send("Input.dispatchKeyEvent", {
            "type": "rawKeyDown", "key": "a", "code": "KeyA",
            "modifiers": 2,  # Ctrl
            "windowsVirtualKeyCode": 65,
        })
        await self.session.send("Input.dispatchKeyEvent", {
            "type": "keyUp", "key": "a", "modifiers": 2,
        })
        await self.press_key("Backspace")
        await asyncio.sleep(0.05)
        await self.type_text(text, mode=mode)

    # --- Human-like interaction ---

    async def human_move(self, x, y, from_x=None, from_y=None):
        """Move mouse along a realistic Bezier curve path.

        If from_x/from_y not given, starts from a random screen edge.
        """
        if from_x is None:
            from_x = random.randint(0, 400)
        if from_y is None:
            from_y = random.randint(0, 300)

        points = _bezier_curve((from_x, from_y), (x, y))
        for px, py in points:
            await self.session.send("Input.dispatchMouseEvent", {
                "type": "mouseMoved", "x": px, "y": py,
            })
            # Variable delay: faster in middle, slower near target
            await asyncio.sleep(random.uniform(0.005, 0.025))

    async def human_click(self, selector=None, x=None, y=None):
        """Click with human-like mouse movement and timing.

        Moves along a Bezier curve, pauses briefly, then clicks
        with realistic press/release timing. Offset is clamped to
        element bounds when a selector is used.
        """
        if selector:
            x, y = await self._get_element_center(selector)
            # Get element bounds to clamp offset
            safe_sel = escape_js_string(selector)
            bounds = await self.session.send("Runtime.evaluate", {
                "expression": f"""
                    (function() {{
                        {_DEEP_QUERY_JS}
                        var el = __tbp_dq(document, '{safe_sel}');
                        if (!el) return null;
                        var r = el.getBoundingClientRect();
                        return {{w: r.width, h: r.height}};
                    }})()
                """,
                "returnByValue": True,
            })
            bval = bounds.get("result", {}).get("value")
            if bval:
                max_off_x = max(1, bval["w"] * 0.3)
                max_off_y = max(1, bval["h"] * 0.3)
                x += random.uniform(-max_off_x, max_off_x)
                y += random.uniform(-max_off_y, max_off_y)
        else:
            if x is None or y is None:
                raise ValueError("Must provide selector or x,y coordinates")
            # For raw coordinates, small fixed offset
            x += random.uniform(-2, 2)
            y += random.uniform(-2, 2)

        # Move along curve
        await self.human_move(x, y)

        # Brief pause before clicking (human reaction time)
        await asyncio.sleep(random.uniform(0.05, 0.2))

        # Press with realistic timing
        await self.session.send("Input.dispatchMouseEvent", {
            "type": "mousePressed", "x": x, "y": y,
            "button": "left", "clickCount": 1,
        })
        # Hold duration varies (humans don't instant-release)
        await asyncio.sleep(random.uniform(0.04, 0.12))

        await self.session.send("Input.dispatchMouseEvent", {
            "type": "mouseReleased", "x": x, "y": y,
            "button": "left", "clickCount": 1,
        })

    # --- Helpers ---

    async def _get_element_center(self, selector):
        """Scroll element into view and get center coordinates.

        Pierces shadow DOM roots to find elements inside web components.
        """
        safe_sel = escape_js_string(selector)
        # Step 1: scroll element into view with instant behavior
        result = await self.session.send("Runtime.evaluate", {
            "expression": f"""
                (function() {{
                    {_DEEP_QUERY_JS}
                    var el = __tbp_dq(document, '{safe_sel}');
                    if (!el) return null;
                    el.scrollIntoView({{block: 'center', inline: 'center', behavior: 'instant'}});
                    void el.offsetHeight;
                    return true;
                }})()
            """,
            "returnByValue": True,
        })
        value = result.get("result", {}).get("value")
        if not value:
            raise ValueError(f"Element not found: {selector}")

        # Step 2: small wait for scroll to settle, then measure
        await asyncio.sleep(0.1)

        result = await self.session.send("Runtime.evaluate", {
            "expression": f"""
                (function() {{
                    {_DEEP_QUERY_JS}
                    var el = __tbp_dq(document, '{safe_sel}');
                    if (!el) return null;
                    var r = el.getBoundingClientRect();
                    return {{x: r.x + r.width/2, y: r.y + r.height/2}};
                }})()
            """,
            "returnByValue": True,
        })
        value = result.get("result", {}).get("value")
        if not value or not isinstance(value, dict) or "x" not in value:
            raise ValueError(f"Element not found or invalid response: {selector}")
        return value["x"], value["y"]
