"""Persistent browser daemon — Unix socket server.

Keeps browser running between CLI commands. Auto-starts Xvfb + Firefox
once, then accepts JSON commands over ~/.tbp/daemon.sock.

Usage:
    python -m src.daemon start [--browser firefox]
    python -m src.daemon stop
"""

import asyncio
import json
import logging
import os
import random
import signal
import sys
import time

logger = logging.getLogger(__name__)

TBP_DIR = os.path.expanduser("~/.tbp")
SOCKET_PATH = os.path.join(TBP_DIR, "daemon.sock")
PID_PATH = os.path.join(TBP_DIR, "daemon.pid")
LOG_PATH = os.path.join(TBP_DIR, "daemon.log")
DOWNLOAD_DIR = os.path.join(TBP_DIR, "downloads")
FIREFOX_PROFILE_DIR = os.path.join(TBP_DIR, "firefox_profile")


def _draw_cursor_overlay(path, cx, cy):
    """Draw a red crosshair + circle at (cx, cy) on a screenshot PNG."""
    try:
        from PIL import Image, ImageDraw
        img = Image.open(path)
        draw = ImageDraw.Draw(img)
        r = 12  # crosshair radius
        color = (255, 0, 0, 255)
        # Crosshair lines
        draw.line([(cx - r, cy), (cx + r, cy)], fill=color, width=2)
        draw.line([(cx, cy - r), (cx, cy + r)], fill=color, width=2)
        # Circle
        draw.ellipse([(cx - r, cy - r), (cx + r, cy + r)],
                     outline=color, width=2)
        img.save(path)
    except ImportError:
        pass  # PIL not available — skip overlay


class Daemon:
    """Background daemon managing browser and socket server."""

    def __init__(self, browser="firefox", session_file=None,
                 idle_timeout=None, proxy=None):
        self._browser_type = browser
        self._session_file = session_file
        self._idle_timeout = idle_timeout  # Auto-shutdown after N seconds idle
        self._proxy = proxy  # SOCKS5/HTTP proxy (e.g. socks5://127.0.0.1:1080)
        self.pilot = None
        self._start_time = None
        self._last_activity = None
        self._server = None
        self._shutting_down = False
        self._cmd_lock = None  # Initialized in run() within event loop
        self._main_wid = None  # Main browser window ID (set after start)
        self._cursor_pos = None  # Last mouse_move position (x, y)

    async def run(self):
        """Main daemon entry point."""
        os.makedirs(TBP_DIR, mode=0o700, exist_ok=True)
        os.makedirs(DOWNLOAD_DIR, mode=0o700, exist_ok=True)

        # Write PID with restrictive permissions
        fd = os.open(PID_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(str(os.getpid()))

        # Start browser with persistent Firefox profile
        os.makedirs(FIREFOX_PROFILE_DIR, mode=0o700, exist_ok=True)
        # Ensure Firefox uses system CA certificates (fixes SSL errors on Termux)
        _user_js = os.path.join(FIREFOX_PROFILE_DIR, "user.js")
        _cert_pref = 'user_pref("security.enterprise_roots.enabled", true);'
        if os.path.exists(_user_js):
            _existing = open(_user_js).read()
            if "enterprise_roots" not in _existing:
                with open(_user_js, "a") as f:
                    f.write("\n" + _cert_pref + "\n")
        else:
            with open(_user_js, "w") as f:
                f.write(_cert_pref + "\n")
        from .pilot import Pilot
        self.pilot = Pilot(
            browser=self._browser_type,
            session_file=self._session_file,
            user_data_dir=FIREFOX_PROFILE_DIR,
            proxy=self._proxy,
        )
        await self.pilot.start()
        self._start_time = time.time()

        self._cmd_lock = asyncio.Lock()
        self._last_activity = time.time()
        logger.info("Browser started (%s)", self._browser_type)

        # Capture main window ID for popup detection
        try:
            self._main_wid = await _get_browser_wid(self.pilot._session)
            logger.info("Main browser WID: %s", self._main_wid)
        except Exception as e:
            logger.warning("Could not detect main WID: %s", e)

        # Auto-load saved cookies on startup for persistent sessions
        _auto_cookies = os.path.join(TBP_DIR, "auto_cookies.json")
        if os.path.exists(_auto_cookies):
            try:
                n = await self.pilot.load_cookies(_auto_cookies)
                logger.info("Auto-loaded %d cookies from %s", n, _auto_cookies)
            except Exception as e:
                logger.warning("Failed to auto-load cookies: %s", e)

        # Remove stale socket
        try:
            os.unlink(SOCKET_PATH)
        except FileNotFoundError:
            pass

        # Start socket server
        self._server = await asyncio.start_unix_server(
            self._handle_client, path=SOCKET_PATH,
            limit=32 * 1024 * 1024,  # 32MB limit for full-page screenshots
        )
        os.chmod(SOCKET_PATH, 0o600)

        logger.info("Daemon listening on %s", SOCKET_PATH)

        # Handle SIGTERM/SIGINT for graceful shutdown
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(
                sig, lambda: loop.call_soon(
                    lambda: asyncio.create_task(self.shutdown())))

        # Browser health watchdog — auto-shutdown if browser crashes
        asyncio.create_task(self._health_watchdog())

        # Idle timeout watchdog — auto-shutdown if no commands received
        if self._idle_timeout and self._idle_timeout > 0:
            asyncio.create_task(self._idle_watchdog())

        try:
            await self._server.serve_forever()
        except asyncio.CancelledError:
            pass
        finally:
            await self._cleanup()

    async def _handle_client(self, reader, writer):
        """Handle one client connection."""
        try:
            while True:
                try:
                    line = await reader.readuntil(b"\n")
                except asyncio.LimitOverrunError:
                    response = {"success": False, "error": "Message too large (>32MB)"}
                    writer.write(json.dumps(response).encode() + b"\n")
                    await writer.drain()
                    break
                except asyncio.IncompleteReadError:
                    break
                if not line:
                    break

                try:
                    request = json.loads(line)
                except json.JSONDecodeError:
                    response = {"success": False, "error": "Invalid JSON"}
                    writer.write(json.dumps(response).encode() + b"\n")
                    await writer.drain()
                    continue

                response = await self._dispatch(request)
                writer.write(json.dumps(response).encode() + b"\n")
                await writer.drain()

                # Shutdown after responding
                if request.get("action") == "shutdown":
                    break
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _dispatch(self, request):
        """Route command to handler."""
        action = request.get("action", "")
        params = request.get("params", {})
        req_id = request.get("id")

        handler = _HANDLERS.get(action)
        if not handler:
            available = sorted(_HANDLERS.keys())
            return {
                "id": req_id,
                "success": False,
                "error": f"Unknown action: {action}",
                "hint": f"Available: {', '.join(available)}",
            }

        try:
            self._last_activity = time.time()
            async with self._cmd_lock:
                result = await handler(self, params)
            return {"id": req_id, "success": True, "data": result}
        except Exception as e:
            logger.exception("Error handling %s", action)
            return {
                "id": req_id,
                "success": False,
                "error": str(e),
            }

    async def _health_watchdog(self):
        """Monitor browser process, auto-shutdown if it crashes."""
        while not self._shutting_down:
            await asyncio.sleep(10)
            if self.pilot and hasattr(self.pilot, "_browser"):
                bp = self.pilot._browser
                proc = getattr(bp, "_browser_proc", None) or getattr(bp, "proc", None)
                if proc and proc.returncode is not None:
                    logger.error("Browser process died (rc=%d), shutting down daemon",
                                 proc.returncode)
                    await self.shutdown()
                    return

    async def _idle_watchdog(self):
        """Auto-shutdown daemon after idle_timeout seconds of inactivity."""
        while not self._shutting_down:
            await asyncio.sleep(30)
            if self._last_activity:
                idle = time.time() - self._last_activity
                if idle >= self._idle_timeout:
                    logger.info("Idle timeout (%.0fs), shutting down", idle)
                    await self.shutdown()
                    return

    async def shutdown(self):
        """Initiate graceful shutdown."""
        if self._shutting_down:
            return
        self._shutting_down = True
        logger.info("Shutting down daemon...")
        if self._server:
            self._server.close()

    async def _cleanup(self):
        """Stop browser and remove state files."""
        if self.pilot:
            # Auto-save cookies before stopping for persistent sessions
            _auto_cookies = os.path.join(TBP_DIR, "auto_cookies.json")
            try:
                await self.pilot.save_cookies(_auto_cookies)
                logger.info("Auto-saved cookies to %s", _auto_cookies)
            except Exception as e:
                logger.warning("Failed to auto-save cookies: %s", e)
            try:
                await self.pilot.stop(save_session=self._session_file)
            except Exception as e:
                logger.warning("Error stopping pilot: %s", e)
        for path in (SOCKET_PATH, PID_PATH):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
        logger.info("Daemon stopped")


# ── Re-inject all active state after navigation/page replace ──


async def _reinject_active_state(daemon):
    """Re-inject all active monkey-patches after page navigation or replacement.

    Called after goto, set_content, or any operation that destroys JS state.
    Each injection is wrapped in try/except to avoid blocking on failure.
    """
    _injections = [
        (_block_patterns, _inject_blocker),
        (_console_capture_enabled, _inject_console_capture),
        (_network_capture_enabled, _inject_network_capture),
        (_mutation_observer_enabled, _inject_mutation_observer),
        (_geolocation_override, _inject_geolocation),
        (_useragent_override, _inject_useragent),
        (_custom_headers, _inject_headers),
        (_search_state.get("query"), _inject_search),
        (_response_capture_enabled, _inject_response_capture),
        (_custom_css, _inject_css),
        (_event_capture_enabled, _inject_event_capture),
        (_highlights, _inject_highlights),
        (_throttle_config, _inject_throttle),
        (_mocks, _inject_mocks),
        (_dialog_capture_enabled, _inject_dialog),
    ]
    for condition, injector in _injections:
        if condition:
            try:
                await injector(daemon)
            except Exception:
                pass


# ── Command handlers ──────────────────────────────────

async def _handle_goto(daemon, params):
    url = params.get("url")
    if not url:
        raise ValueError("Missing 'url' parameter")
    timeout = params.get("timeout", 45)
    if not isinstance(timeout, (int, float)) or timeout < 1 or timeout > 300:
        raise ValueError("'timeout' must be 1-300")
    cloudflare = params.get("cloudflare", False)

    if cloudflare:
        await daemon.pilot.goto_cf(url, timeout=timeout)
    else:
        await daemon.pilot.goto(url, timeout=timeout)

    await _reinject_active_state(daemon)

    return {
        "url": await daemon.pilot.url(),
        "title": await daemon.pilot.title(),
    }


async def _handle_back(daemon, params):
    await daemon.pilot.evaluate("history.back()")
    await daemon.pilot.wait(1)
    return {
        "url": await daemon.pilot.url(),
        "title": await daemon.pilot.title(),
    }


async def _handle_forward(daemon, params):
    await daemon.pilot.evaluate("history.forward()")
    await daemon.pilot.wait(1)
    return {
        "url": await daemon.pilot.url(),
        "title": await daemon.pilot.title(),
    }


async def _handle_reload(daemon, params):
    await daemon.pilot.evaluate("location.reload()")
    await daemon.pilot.wait(2)
    return {
        "url": await daemon.pilot.url(),
        "title": await daemon.pilot.title(),
    }


async def _handle_click(daemon, params):
    target = params.get("target")
    x = params.get("x")
    y = params.get("y")
    if not target and (x is None or y is None):
        raise ValueError("Missing 'target' parameter (CSS selector) or x/y coordinates")
    human = params.get("human", False)
    keyboard_fallback = params.get("keyboard_fallback", True)
    button = params.get("button", "left")
    count = params.get("count", 1)
    interval = params.get("interval", 0.1)

    # For raw coordinate clicks (no selector), require prior mouse_move
    # or mouse_locate to the same coordinates. This prevents blind clicks.
    use_xdotool = x is not None and y is not None and not target

    if use_xdotool:
        ix, iy = int(x), int(y)
        armed = daemon._cursor_pos
        if armed is None or armed != (ix, iy):
            armed_str = f"{armed[0]},{armed[1]}" if armed else "none"
            raise ValueError(
                f"Click at ({ix},{iy}) rejected — cursor armed at ({armed_str}). "
                f"Call browser_mouse_move({ix},{iy}) or browser_mouse_locate() first, "
                f"then verify the screenshot before clicking."
            )

    try:
        if use_xdotool:
            session = daemon.pilot._session
            btn_map = {"left": "1", "middle": "2", "right": "3"}
            btn_num = btn_map.get(button, "1")
            # Console already closed by mouse_move/locate — just click
            # Check if active window is a popup (not main browser)
            popup_wid = None
            try:
                env = {**os.environ, "DISPLAY": session._display}
                proc = await asyncio.create_subprocess_exec(
                    "xdotool", "getactivewindow",
                    env=env, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                out, _ = await proc.communicate()
                active_wid = out.decode().strip()
                if active_wid and active_wid != daemon._main_wid:
                    popup_wid = active_wid
            except Exception:
                pass
            click_args = ["click"]
            if popup_wid:
                click_args += ["--window", popup_wid]
            click_args.append(btn_num)
            for _ in range(count):
                await session._xdt(click_args)
                await asyncio.sleep(interval)
            # Reset armed position after click (must re-arm for next click)
            daemon._cursor_pos = None
        elif human:
            await daemon.pilot.human_click(selector=target, x=x, y=y)
        else:
            await daemon.pilot.click(
                selector=target, x=x, y=y, button=button, count=count,
                interval=interval,
            )
        result = {"clicked": target or f"({x},{y})", "method": "mouse"}
        # Report what element was actually clicked (skip for right-click —
        # Firefox context menu blocks JS execution; skip on CSP pages
        # where JS doesn't work to avoid long timeouts)
        js_ok = getattr(daemon.pilot._session, '_js_available', True)
        if x is not None and y is not None and button == "left" and js_ok:
            try:
                el_info = await daemon.pilot.evaluate(
                    f"(function(){{var el=document.elementFromPoint({x},{y});"
                    f"if(!el)return null;return{{"
                    f"tag:(el.tagName||'').toLowerCase(),"
                    f"text:(el.textContent||'').trim().slice(0,80),"
                    f"role:el.getAttribute('role')||''"
                    f"}}}})()"
                )
                if el_info:
                    result["element"] = el_info
            except Exception:
                pass
        return result
    except Exception as click_err:
        if not keyboard_fallback or not target:
            raise
        try:
            from ._utils import escape_js_string
            safe_sel = escape_js_string(target)
            focus_result = await daemon.pilot.evaluate(
                "(function(){"
                "var el=document.querySelector('" + safe_sel + "');"
                "if(!el)return null;"
                "el.scrollIntoView({block:'center'});"
                "el.focus();return true})()"
            )
            if focus_result:
                await asyncio.sleep(0.15)
                await daemon.pilot.press("Enter")
                return {
                    "clicked": target,
                    "method": "keyboard_fallback",
                    "original_error": str(click_err)[:100],
                }
        except Exception:
            pass
        raise click_err


async def _handle_type(daemon, params):
    target = params.get("target")
    text = params.get("text", "")
    x = params.get("x")
    y = params.get("y")
    mode = params.get("mode", "auto")
    if not target and (x is None or y is None):
        raise ValueError("Missing 'target' parameter (CSS selector) or x/y coordinates")

    await daemon.pilot.type(selector=target, text=text, x=x, y=y, mode=mode)
    return {"typed": text[:50], "target": target or f"({x},{y})"}


async def _handle_press(daemon, params):
    key = params.get("key", "")
    if not key:
        raise ValueError("Missing 'key' parameter")
    await daemon.pilot.press(key)
    return {"pressed": key}


async def _handle_scroll(daemon, params):
    amount = params.get("amount", -300)
    await daemon.pilot.scroll(delta_y=amount)
    return {"scrolled": amount}


async def _handle_hover(daemon, params):
    target = params.get("target")
    x = params.get("x")
    y = params.get("y")
    if not target and (x is None or y is None):
        raise ValueError("Missing 'target' parameter (CSS selector) or x/y coordinates")
    if target:
        await daemon.pilot.input.hover(selector=target)
    elif x is not None and y is not None:
        await daemon.pilot.input.hover(x=x, y=y)
    # Get element info at hover position via elementFromPoint
    element_info = None
    try:
        if x is not None and y is not None:
            element_info = await daemon.pilot.evaluate(
                f"(function(){{var el=document.elementFromPoint({x},{y});"
                f"if(!el)return null;return{{"
                f"tag:(el.tagName||'').toLowerCase(),"
                f"text:(el.textContent||'').trim().slice(0,100),"
                f"id:el.id||'',class:(el.className||'').toString().slice(0,100),"
                f"href:el.href||'',role:el.getAttribute('role')||'',"
                f"ariaLabel:el.getAttribute('aria-label')||''"
                f"}}}})()"
            )
    except Exception:
        pass
    result = {"hovered": target or f"({x},{y})"}
    if element_info:
        result["element"] = element_info
    return result


async def _mouse_screenshot(daemon, cx, cy, path=None):
    """Take screenshot with cursor crosshair at (cx, cy). Returns path."""
    from ._utils import validate_path
    if not path:
        path = validate_path("cursor_preview.png")
    else:
        path = validate_path(path)
    try:
        await daemon.pilot._session._dismiss_popup()
    except Exception:
        pass
    await daemon.pilot.screenshot(path, full_page=False)
    _draw_cursor_overlay(path, cx, cy)
    return path


async def _handle_mouse_move(daemon, params):
    x = params.get("x")
    y = params.get("y")
    if x is None or y is None:
        raise ValueError("Missing x/y coordinates")
    session = daemon.pilot._session
    # Close console so mouse lands on page, not DevTools
    try:
        await session._close_console()
    except Exception:
        pass
    await asyncio.sleep(0.15)
    # Screenshots are full-screen captures (include browser chrome),
    # so image coordinates ARE screen coordinates — no conversion needed.
    ix, iy = int(x), int(y)
    await session._xdt(["mousemove", str(ix), str(iy)])
    daemon._cursor_pos = (ix, iy)
    # Auto-screenshot with cursor overlay
    path = await _mouse_screenshot(daemon, ix, iy, params.get("path"))
    return {"moved_to": {"x": ix, "y": iy}, "screenshot": path}


async def _handle_mouse_locate(daemon, params):
    session = daemon.pilot._session
    # Close console so we read actual page mouse position
    try:
        await session._close_console()
    except Exception:
        pass
    await asyncio.sleep(0.15)
    # Get current mouse position via xdotool (screen coords = image coords)
    env = {**os.environ, "DISPLAY": session._display}
    proc = await asyncio.create_subprocess_exec(
        "xdotool", "getmouselocation",
        env=env, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    # Parse "x:123 y:456 screen:0 window:789"
    parts = out.decode().strip().split()
    mx = my = 0
    for p in parts:
        if p.startswith("x:"):
            mx = int(p[2:])
        elif p.startswith("y:"):
            my = int(p[2:])
    daemon._cursor_pos = (mx, my)
    # Auto-screenshot with cursor overlay
    path = await _mouse_screenshot(daemon, mx, my, params.get("path"))
    return {"position": {"x": mx, "y": my}, "screenshot": path}


async def _handle_text(daemon, params):
    selector = params.get("selector")
    limit = params.get("limit")
    text = await daemon.pilot.text(selector)
    truncated = False
    if limit and len(text) > limit:
        text = text[:limit]
        truncated = True
    return {"text": text, "truncated": truncated}


async def _handle_html(daemon, params):
    selector = params.get("selector")
    limit = params.get("limit")
    html = await daemon.pilot.html(selector)
    truncated = False
    if limit and len(html) > limit:
        html = html[:limit]
        truncated = True
    return {"html": html, "truncated": truncated}


async def _handle_title(daemon, params):
    return {"title": await daemon.pilot.title()}


async def _handle_url(daemon, params):
    return {"url": await daemon.pilot.url()}


async def _handle_links(daemon, params):
    limit = params.get("limit", 100)
    links = await daemon.pilot.links()
    return {"links": links[:limit] if limit else links}


async def _handle_eval(daemon, params):
    expression = params.get("expression", "")
    if not expression:
        raise ValueError("Missing 'expression' parameter")
    result = await daemon.pilot.evaluate(expression)
    return {"result": result}


async def _handle_screenshot(daemon, params):
    from ._utils import validate_path
    path = validate_path(params.get("path", "screenshot.png"))
    full = params.get("full", False)
    cursor = params.get("cursor", False)
    # Dismiss Firefox chrome popups (save password/address) for clean screenshot
    try:
        await daemon.pilot._session._dismiss_popup()
    except Exception:
        pass
    await daemon.pilot.screenshot(path, full_page=full)
    # Draw cursor crosshair overlay if requested and position is known
    if cursor and daemon._cursor_pos:
        _draw_cursor_overlay(path, daemon._cursor_pos[0], daemon._cursor_pos[1])
    result = {"path": path}
    if daemon._cursor_pos:
        result["cursor"] = {"x": daemon._cursor_pos[0], "y": daemon._cursor_pos[1]}
    return result


async def _handle_pdf(daemon, params):
    from ._utils import validate_path
    path = validate_path(params.get("path", "page.pdf"))
    options = {}
    if "landscape" in params:
        options["landscape"] = bool(params["landscape"])
    if "scale" in params:
        scale = float(params["scale"])
        if scale < 0.1 or scale > 2.0:
            raise ValueError("'scale' must be 0.1-2.0")
        options["scale"] = scale
    for margin_key in ("margin_top", "margin_right", "margin_bottom", "margin_left"):
        if margin_key in params:
            options[margin_key] = float(params[margin_key])
    if "page_ranges" in params:
        options["page_ranges"] = str(params["page_ranges"])
    if "print_background" in params:
        options["print_background"] = bool(params["print_background"])
    if "header_template" in params:
        options["header_template"] = str(params["header_template"])
    if "footer_template" in params:
        options["footer_template"] = str(params["footer_template"])
    await daemon.pilot.pdf(path, **options)
    return {"path": path}


async def _handle_wait(daemon, params):
    seconds = params.get("seconds", 1)
    if not isinstance(seconds, (int, float)) or seconds < 0 or seconds > 300:
        raise ValueError("'seconds' must be 0-300")
    await daemon.pilot.wait(seconds)
    return {"waited": seconds}


async def _handle_waitfor(daemon, params):
    selector = params.get("selector")
    timeout = params.get("timeout", 10)
    if not selector:
        raise ValueError("Missing 'selector' parameter")
    if not isinstance(timeout, (int, float)) or timeout < 1 or timeout > 300:
        raise ValueError("'timeout' must be 1-300")
    await daemon.pilot.wait_for(selector, timeout=timeout)
    return {"found": True, "selector": selector}


async def _handle_cookies(daemon, params):
    action = params.get("action", "list")

    if action == "clear":
        await daemon.pilot.clear_cookies()
        return {"cleared": True}
    elif action == "save":
        from ._utils import validate_path
        path = validate_path(params.get("path", "cookies.json"))
        n = await daemon.pilot.save_cookies(path)
        return {"saved": n, "path": path}
    elif action == "load":
        from ._utils import validate_path
        path = validate_path(params.get("path", "cookies.json"))
        n = await daemon.pilot.load_cookies(path)
        return {"loaded": n, "path": path}
    else:
        cookies = await daemon.pilot.get_all_cookies()
        return {"cookies": cookies}


async def _handle_a11y(daemon, params):
    limit = params.get("limit")
    tree = await daemon.pilot.a11y_tree()
    if limit:
        tree = tree[:limit]
    return {"tree": tree}


async def _handle_status(daemon, params):
    url = title = None
    try:
        url = await daemon.pilot.url()
        title = await daemon.pilot.title()
    except Exception:
        pass
    return {
        "pid": os.getpid(),
        "browser": daemon._browser_type,
        "url": url,
        "title": title,
        "uptime": round(time.time() - daemon._start_time, 1),
    }


async def _handle_find(daemon, params):
    """Find interactive elements by visible text."""
    text = params.get("text", "")
    if not text:
        raise ValueError("Missing 'text' parameter")
    role = params.get("role", "")
    limit = params.get("limit", 10)

    from ._utils import escape_js_string
    safe_text = escape_js_string(text)
    safe_role = escape_js_string(role)

    find_js = (
        "(function(){"
        "var tags='a,button,[role=button],[role=link],input[type=submit],"
        "input[type=button],[onclick],label,select,textarea,"
        "input:not([type=hidden]),summary,[tabindex]';"
        "var els=Array.from(document.querySelectorAll(tags));"
        "var results=[];"
        f"var st='{safe_text}'.toLowerCase();"
        f"var fr='{safe_role}';"
        "for(var i=0;i<els.length;i++){"
        "var el=els[i];"
        "if(el.offsetParent===null&&el.offsetWidth===0)continue;"
        "var txt=(el.innerText||el.value||el.getAttribute('aria-label')||'').trim();"
        "if(!txt||txt.toLowerCase().indexOf(st)===-1)continue;"
        "var tag=el.tagName.toLowerCase();"
        "var role=el.getAttribute('role')||(tag==='a'?'link':tag==='button'?'button':tag);"
        "if(fr&&role!==fr)continue;"
        "var sel='';"
        "function eid(id){return'#'+CSS.escape(id)}"
        "if(el.id){sel=eid(el.id)}"
        "else{"
        "var p=[];var c=el;"
        "while(c&&c!==document.body){"
        "var s=c.tagName.toLowerCase();"
        "if(c.id){p.unshift(eid(c.id));break}"
        "var idx=1;var sb=c.previousElementSibling;"
        "while(sb){if(sb.tagName===c.tagName)idx++;sb=sb.previousElementSibling}"
        "var cnt=0;var ch=c.parentElement?c.parentElement.children:[];"
        "for(var j=0;j<ch.length;j++){if(ch[j].tagName===c.tagName)cnt++}"
        "if(cnt>1)s+=':nth-of-type('+idx+')';"
        "p.unshift(s);c=c.parentElement}"
        "sel=p.join(' > ')}"
        "results.push({text:txt.substring(0,100),tag:tag,role:role,selector:sel})"
        "}"
        "return results})()"
    )

    results = await daemon.pilot.evaluate(find_js)
    if isinstance(results, list) and limit:
        results = results[:limit]
    return {"elements": results or []}


async def _handle_shutdown(daemon, params):
    asyncio.get_running_loop().call_soon(
        lambda: asyncio.create_task(daemon.shutdown())
    )
    return {"status": "shutting_down"}


# ── Tab management handlers ──────────────────────────────────

async def _handle_tab_new(daemon, params):
    """Open a new tab, optionally navigate to URL."""
    url = params.get("url", "")
    session = daemon.pilot._session
    if hasattr(session, '_xdt'):
        # Firefox: Ctrl+T opens new tab
        await session._close_console()
        await session._xdt(["key", "ctrl+t"])
        await asyncio.sleep(1)
        session._viewport_offset = None
        session._console_open = False
    if url:
        await daemon.pilot.goto(url)
    return {
        "url": await daemon.pilot.url(),
        "title": await daemon.pilot.title(),
    }


async def _handle_tab_close(daemon, params):
    """Close current tab."""
    session = daemon.pilot._session
    if hasattr(session, '_xdt'):
        await session._close_console()
        await session._xdt(["key", "ctrl+w"])
        await asyncio.sleep(0.5)
        session._viewport_offset = None
    url = title = ""
    try:
        url = await daemon.pilot.url()
        title = await daemon.pilot.title()
    except Exception:
        pass
    return {"url": url, "title": title}


async def _handle_tab_next(daemon, params):
    """Switch to next tab."""
    session = daemon.pilot._session
    if hasattr(session, '_xdt'):
        await session._close_console()
        await session._xdt(["key", "ctrl+Tab"])
        await asyncio.sleep(0.5)
        session._viewport_offset = None
        session._console_open = False
    return {
        "url": await daemon.pilot.url(),
        "title": await daemon.pilot.title(),
    }


async def _handle_tab_prev(daemon, params):
    """Switch to previous tab."""
    session = daemon.pilot._session
    if hasattr(session, '_xdt'):
        await session._close_console()
        await session._xdt(["key", "ctrl+shift+Tab"])
        await asyncio.sleep(0.5)
        session._viewport_offset = None
        session._console_open = False
    return {
        "url": await daemon.pilot.url(),
        "title": await daemon.pilot.title(),
    }


async def _handle_tab_goto(daemon, params):
    """Switch to tab by index (1-9)."""
    index = params.get("index", 1)
    if not isinstance(index, int) or index < 1 or index > 9:
        raise ValueError("'index' must be 1-9")
    session = daemon.pilot._session
    if hasattr(session, '_xdt'):
        await session._close_console()
        await session._xdt(["key", f"ctrl+{index}"])
        await asyncio.sleep(0.5)
        session._viewport_offset = None
        session._console_open = False
    return {
        "url": await daemon.pilot.url(),
        "title": await daemon.pilot.title(),
    }


# ── Request interception (URL blocking) ──────────────────────────────────

# In-memory blocklist persists for daemon lifetime
_block_patterns = []


async def _handle_block(daemon, params):
    """Add URL patterns to blocklist."""
    patterns = params.get("patterns", [])
    if isinstance(patterns, str):
        patterns = [patterns]
    if not patterns:
        raise ValueError("Missing 'patterns' (list of URL substrings or domains)")
    added = []
    for p in patterns:
        p = p.strip()
        if p and p not in _block_patterns:
            _block_patterns.append(p)
            added.append(p)
    # Inject blocker into current page
    if _block_patterns:
        await _inject_blocker(daemon)
    return {"blocked": _block_patterns[:], "added": added}


async def _handle_unblock(daemon, params):
    """Remove URL patterns from blocklist."""
    patterns = params.get("patterns", [])
    if isinstance(patterns, str):
        patterns = [patterns]
    removed = []
    for p in patterns:
        p = p.strip()
        if p in _block_patterns:
            _block_patterns.remove(p)
            removed.append(p)
    if not _block_patterns:
        # Restore originals and remove blocker
        await daemon.pilot.evaluate(
            "if(window.__tbp_orig_fetch)window.fetch=window.__tbp_orig_fetch;"
            "if(window.__tbp_orig_xhr_open){"
            "XMLHttpRequest.prototype.open=window.__tbp_orig_xhr_open;"
            "if(window.__tbp_orig_xhr_send)"
            "XMLHttpRequest.prototype.send=window.__tbp_orig_xhr_send}"
            "delete window.__tbp_block_patterns;"
            "delete window.__tbp_orig_fetch;"
            "delete window.__tbp_orig_xhr_open;"
            "delete window.__tbp_orig_xhr_send;"
        )
    return {"blocked": _block_patterns[:], "removed": removed}


async def _handle_blocklist(daemon, params):
    """List current blocked patterns."""
    return {"patterns": _block_patterns[:]}


async def _inject_blocker(daemon):
    """Inject JS that intercepts fetch/XHR and blocks matching URLs."""
    from ._utils import escape_js_string
    patterns_js = ",".join(f"'{escape_js_string(p)}'" for p in _block_patterns)
    js = (
        "(function(){"
        f"window.__tbp_block_patterns=[{patterns_js}];"
        "if(!window.__tbp_orig_fetch){"
        "window.__tbp_orig_fetch=window.fetch;"
        "window.fetch=function(u,o){"
        "var url=typeof u==='string'?u:(u&&u.url)||'';"
        "for(var i=0;i<window.__tbp_block_patterns.length;i++){"
        "if(url.indexOf(window.__tbp_block_patterns[i])!==-1)"
        "return Promise.reject(new Error('Blocked by tbp: '+url))}"
        "return window.__tbp_orig_fetch.apply(this,arguments)}}"
        "if(!window.__tbp_orig_xhr_open){"
        "window.__tbp_orig_xhr_open=XMLHttpRequest.prototype.open;"
        "window.__tbp_orig_xhr_send=XMLHttpRequest.prototype.send;"
        "XMLHttpRequest.prototype.open=function(m,u){"
        "for(var i=0;i<window.__tbp_block_patterns.length;i++){"
        "if(u.indexOf(window.__tbp_block_patterns[i])!==-1)"
        "{this.__tbp_blocked=true;return}}"
        "return window.__tbp_orig_xhr_open.apply(this,arguments)};"
        "XMLHttpRequest.prototype.send=function(){"
        "if(this.__tbp_blocked)return;"
        "return window.__tbp_orig_xhr_send.apply(this,arguments)}}"
        "})()"
    )
    await daemon.pilot.evaluate(js)


# ── Multi-step macro handler ──────────────────────────────────

async def _handle_macro(daemon, params):
    """Execute a sequence of commands (macro)."""
    steps = params.get("steps", [])
    if not steps:
        raise ValueError("Missing 'steps' (list of {action, params} objects)")
    if not isinstance(steps, list):
        raise ValueError("'steps' must be a list")
    if len(steps) > 100:
        raise ValueError("Macro limited to 100 steps")

    results = []
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            results.append({"step": i, "success": False, "error": "Step must be an object"})
            continue

        action = step.get("action", "")
        step_params = step.get("params", {})

        # Prevent recursive macros
        if action in ("macro", "shutdown"):
            results.append({"step": i, "success": False,
                            "error": f"'{action}' not allowed in macros"})
            continue

        handler = _HANDLERS.get(action)

        if not handler:
            results.append({"step": i, "success": False,
                            "error": f"Unknown action: {action}"})
            if step.get("stop_on_error", True):
                break
            continue

        try:
            result = await handler(daemon, step_params)
            results.append({"step": i, "success": True, "data": result})
        except Exception as e:
            results.append({"step": i, "success": False, "error": str(e)})
            if step.get("stop_on_error", True):
                break

    return {"results": results, "completed": len(results), "total": len(steps)}


# ── Console log capture ──────────────────────────────────

_console_capture_enabled = False


async def _handle_console_start(daemon, params):
    """Enable console log capture (monkey-patches console.log/warn/error/info)."""
    global _console_capture_enabled
    _console_capture_enabled = True
    await _inject_console_capture(daemon)
    return {"enabled": True}


async def _handle_console_stop(daemon, params):
    """Disable console log capture (stops re-injection after navigation)."""
    global _console_capture_enabled
    _console_capture_enabled = False
    await daemon.pilot.evaluate(
        "(function(){"
        "if(!window.__tbp_console_patched)return;"
        "delete window.__tbp_console_patched;"
        "delete window.__tbp_console;"
        "})()"
    )
    return {"enabled": False}


async def _handle_console_logs(daemon, params):
    """Get captured console logs."""
    clear = params.get("clear", False)
    limit = params.get("limit", 100)
    if not isinstance(limit, int) or limit < 1 or limit > 10000:
        limit = 100
    if clear:
        js = (
            "(function(){var l=window.__tbp_console||[];"
            f"var r=l.slice(-{limit});"
            "window.__tbp_console=[];return r})()"
        )
    else:
        js = f"(window.__tbp_console||[]).slice(-{limit})"
    result = await daemon.pilot.evaluate(js)
    logs = result if isinstance(result, list) else []
    return {"logs": logs, "count": len(logs)}


async def _handle_console_clear(daemon, params):
    """Clear captured console logs."""
    await daemon.pilot.evaluate("window.__tbp_console=[]")
    return {"cleared": True}


async def _inject_console_capture(daemon):
    """Inject JS to capture console.log/warn/error/info."""
    js = (
        "(function(){"
        "if(window.__tbp_console_patched)return;"
        "window.__tbp_console=[];"
        "window.__tbp_console_patched=true;"
        "['log','warn','error','info'].forEach(function(m){"
        "var orig=console[m];"
        "console[m]=function(){"
        "var args=Array.prototype.slice.call(arguments);"
        "var msg=args.map(function(a){"
        "try{return typeof a==='string'?a:JSON.stringify(a)}"
        "catch(e){return String(a)}"
        "}).join(' ');"
        "window.__tbp_console.push({level:m,message:msg,timestamp:Date.now()});"
        "if(window.__tbp_console.length>1000)"
        "window.__tbp_console=window.__tbp_console.slice(-500);"
        "return orig.apply(console,arguments)}"
        "})"
        "})()"
    )
    await daemon.pilot.evaluate(js)


# ── Download management ──────────────────────────────────

async def _handle_downloads(daemon, params):
    """List files in the download directory."""
    import stat
    files = []
    if os.path.isdir(DOWNLOAD_DIR):
        for name in sorted(os.listdir(DOWNLOAD_DIR)):
            path = os.path.join(DOWNLOAD_DIR, name)
            try:
                st = os.stat(path)
                if stat.S_ISREG(st.st_mode):
                    files.append({
                        "name": name,
                        "size": st.st_size,
                        "modified": round(st.st_mtime, 1),
                    })
            except OSError:
                pass
    return {"dir": DOWNLOAD_DIR, "files": files}


# ── Network request log (PerformanceObserver — no fetch/XHR patching) ────

_network_capture_enabled = False


async def _handle_network_start(daemon, params):
    """Enable network request logging via PerformanceObserver."""
    global _network_capture_enabled
    _network_capture_enabled = True
    await _inject_network_capture(daemon)
    return {"enabled": True}


async def _handle_network_stop(daemon, params):
    """Disable network request logging."""
    global _network_capture_enabled
    _network_capture_enabled = False
    await daemon.pilot.evaluate(
        "(function(){"
        "if(window.__tbp_net_observer)window.__tbp_net_observer.disconnect();"
        "delete window.__tbp_net_observer;"
        "delete window.__tbp_net_patched;"
        "delete window.__tbp_network;"
        "})()"
    )
    return {"enabled": False}


async def _handle_network_logs(daemon, params):
    """Get captured network requests."""
    clear = params.get("clear", False)
    limit = params.get("limit", 100)
    if not isinstance(limit, int) or limit < 1 or limit > 10000:
        limit = 100
    if clear:
        js = (
            "(function(){var l=window.__tbp_network||[];"
            f"var r=l.slice(-{limit});"
            "window.__tbp_network=[];return r})()"
        )
    else:
        js = f"(window.__tbp_network||[]).slice(-{limit})"
    result = await daemon.pilot.evaluate(js)
    logs = result if isinstance(result, list) else []
    return {"requests": logs, "count": len(logs)}


async def _handle_network_clear(daemon, params):
    """Clear captured network requests."""
    await daemon.pilot.evaluate("window.__tbp_network=[]")
    return {"cleared": True}


async def _inject_network_capture(daemon):
    """Inject PerformanceObserver to log network requests passively."""
    js = (
        "(function(){"
        "if(window.__tbp_net_patched)return;"
        "window.__tbp_network=[];"
        "window.__tbp_net_patched=true;"
        "var obs=new PerformanceObserver(function(list){"
        "list.getEntries().forEach(function(e){"
        "window.__tbp_network.push({"
        "url:e.name,type:e.initiatorType,"
        "duration:Math.round(e.duration),"
        "size:e.transferSize||0,"
        "timestamp:Math.round(e.startTime+performance.timeOrigin)});"
        "if(window.__tbp_network.length>500)"
        "window.__tbp_network=window.__tbp_network.slice(-250)"
        "})});"
        "obs.observe({type:'resource',buffered:true});"
        "window.__tbp_net_observer=obs"
        "})()"
    )
    await daemon.pilot.evaluate(js)


# ── DOM Mutation Observer ──────────────────────────────────

_mutation_observer_enabled = False


async def _handle_observe_start(daemon, params):
    """Start watching DOM mutations."""
    global _mutation_observer_enabled
    _mutation_observer_enabled = True
    await _inject_mutation_observer(daemon)
    return {"enabled": True}


async def _handle_observe_stop(daemon, params):
    """Stop watching DOM mutations."""
    global _mutation_observer_enabled
    _mutation_observer_enabled = False
    await daemon.pilot.evaluate(
        "(function(){"
        "if(window.__tbp_mut_observer)window.__tbp_mut_observer.disconnect();"
        "delete window.__tbp_mut_observer;"
        "delete window.__tbp_mut_patched;"
        "delete window.__tbp_mutations;"
        "})()"
    )
    return {"enabled": False}


async def _handle_mutations(daemon, params):
    """Get captured DOM mutations."""
    clear = params.get("clear", False)
    limit = params.get("limit", 100)
    if not isinstance(limit, int) or limit < 1 or limit > 10000:
        limit = 100
    if clear:
        js = (
            "(function(){var l=window.__tbp_mutations||[];"
            f"var r=l.slice(-{limit});"
            "window.__tbp_mutations=[];return r})()"
        )
    else:
        js = f"(window.__tbp_mutations||[]).slice(-{limit})"
    result = await daemon.pilot.evaluate(js)
    logs = result if isinstance(result, list) else []
    return {"mutations": logs, "count": len(logs)}


async def _handle_mutations_clear(daemon, params):
    """Clear captured DOM mutations."""
    await daemon.pilot.evaluate("window.__tbp_mutations=[]")
    return {"cleared": True}


async def _inject_mutation_observer(daemon):
    """Inject MutationObserver to log DOM changes."""
    js = (
        "(function(){"
        "if(window.__tbp_mut_patched)return;"
        "window.__tbp_mutations=[];"
        "window.__tbp_mut_patched=true;"
        "var obs=new MutationObserver(function(list){"
        "list.forEach(function(m){"
        "var t='';"
        "try{if(m.target.id)t='#'+m.target.id;"
        "else t=m.target.tagName?m.target.tagName.toLowerCase():''}catch(e){}"
        "var entry={type:m.type,target:t,timestamp:Date.now()};"
        "if(m.type==='childList'){"
        "entry.added=m.addedNodes.length;entry.removed=m.removedNodes.length}"
        "else if(m.type==='attributes'){"
        "entry.attribute=m.attributeName}"
        "window.__tbp_mutations.push(entry);"
        "if(window.__tbp_mutations.length>1000)"
        "window.__tbp_mutations=window.__tbp_mutations.slice(-500)"
        "})});"
        "obs.observe(document.body||document.documentElement,"
        "{childList:true,attributes:true,characterData:true,"
        "subtree:true,attributeOldValue:true});"
        "window.__tbp_mut_observer=obs"
        "})()"
    )
    await daemon.pilot.evaluate(js)


# ── Element screenshot ──────────────────────────────────

async def _handle_screenshot_element(daemon, params):
    """Screenshot a specific element by CSS selector."""
    target = params.get("target")
    if not target:
        raise ValueError("Missing 'target' parameter (CSS selector)")
    from ._utils import validate_path, escape_js_string
    path = validate_path(params.get("path", "element.png"))

    safe_sel = escape_js_string(target)
    rect = await daemon.pilot.evaluate(
        f"(function(){{var el=document.querySelector('{safe_sel}');"
        "if(!el)return null;var r=el.getBoundingClientRect();"
        "return {x:Math.round(r.x),y:Math.round(r.y),"
        "w:Math.round(r.width),h:Math.round(r.height)}"
        "})()"
    )
    if not rect or not isinstance(rect, dict):
        raise ValueError(f"Element not found: {target}")
    if rect.get("w", 0) <= 0 or rect.get("h", 0) <= 0:
        raise ValueError(f"Element has zero size: {target}")

    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        await daemon.pilot.screenshot(tmp_path)

        crop_x = max(0, rect["x"])
        crop_y = max(0, rect["y"])
        session = daemon.pilot._session
        if hasattr(session, '_get_viewport_offset'):
            offset = await session._get_viewport_offset()
            crop_x += offset[0]
            crop_y += offset[1]

        crop_spec = f"{rect['w']}x{rect['h']}+{crop_x}+{crop_y}"
        proc = await asyncio.create_subprocess_exec(
            "convert", tmp_path, "-crop", crop_spec, "+repage", path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        if proc.returncode != 0:
            raise RuntimeError("Image crop failed (ImageMagick convert)")
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    return {"path": path, "rect": rect}


# ── Drag and drop ──────────────────────────────────

async def _handle_drag(daemon, params):
    """Drag from source element to target element or offset."""
    source = params.get("source")
    if not source:
        raise ValueError("Missing 'source' parameter (CSS selector)")
    target = params.get("target", "")
    dx = params.get("dx", 0)
    dy = params.get("dy", 0)
    if not isinstance(dx, (int, float)) or not isinstance(dy, (int, float)):
        raise ValueError("'dx' and 'dy' must be numbers")

    from ._utils import escape_js_string

    safe_src = escape_js_string(source)
    src_rect = await daemon.pilot.evaluate(
        f"(function(){{var el=document.querySelector('{safe_src}');"
        "if(!el)return null;var r=el.getBoundingClientRect();"
        "return {x:r.x+r.width/2,y:r.y+r.height/2}"
        "})()"
    )
    if not src_rect or not isinstance(src_rect, dict):
        raise ValueError(f"Source element not found: {source}")

    if target:
        safe_tgt = escape_js_string(target)
        tgt_rect = await daemon.pilot.evaluate(
            f"(function(){{var el=document.querySelector('{safe_tgt}');"
            "if(!el)return null;var r=el.getBoundingClientRect();"
            "return {x:r.x+r.width/2,y:r.y+r.height/2}"
            "})()"
        )
        if not tgt_rect or not isinstance(tgt_rect, dict):
            raise ValueError(f"Target element not found: {target}")
        end_x = tgt_rect["x"]
        end_y = tgt_rect["y"]
    else:
        end_x = src_rect["x"] + dx
        end_y = src_rect["y"] + dy

    # Dispatch drag via JS events (works with NativeFirefoxSession)
    # Includes both MouseEvent chain (for mouse-based drag libs) and
    # HTML5 DragEvent chain (for native drag-and-drop)
    sx, sy = src_rect["x"], src_rect["y"]
    ex, ey = end_x, end_y
    tgt_sel = f"'{safe_tgt}'" if target else "null"
    steps = 10

    # Build move steps as array literal
    move_steps = ",".join(
        f"[{sx}+({ex}-{sx})*{i/steps},{sy}+({ey}-{sy})*{i/steps}]"
        for i in range(1, steps + 1)
    )

    js = (
        f"(function(){{var src=document.querySelector('{safe_src}');"
        f"if(!src)throw new Error('Source not found');"
        f"var tsel={tgt_sel};var tgt=tsel?document.querySelector(tsel):null;"
        f"var sx={sx},sy={sy},ex={ex},ey={ey};"
        f"var o={{bubbles:true,cancelable:true,view:window}};"
        # Mouse events for non-HTML5 drag libs
        f"src.dispatchEvent(new MouseEvent('mousedown',"
        f"Object.assign({{clientX:sx,clientY:sy,button:0}},o)));"
        # HTML5 dragstart
        f"try{{var dt=new DataTransfer();"
        f"src.dispatchEvent(new DragEvent('dragstart',"
        f"Object.assign({{dataTransfer:dt,clientX:sx,clientY:sy}},o)));"
        # Move steps
        f"var moves=[{move_steps}];"
        f"for(var i=0;i<moves.length;i++){{"
        f"src.dispatchEvent(new MouseEvent('mousemove',"
        f"Object.assign({{clientX:moves[i][0],clientY:moves[i][1]}},o)));"
        f"if(tgt)tgt.dispatchEvent(new DragEvent('dragover',"
        f"Object.assign({{dataTransfer:dt,clientX:moves[i][0],clientY:moves[i][1]}},o)))}}"
        # Drop + dragend
        f"if(tgt){{"
        f"tgt.dispatchEvent(new DragEvent('dragenter',Object.assign({{dataTransfer:dt}},o)));"
        f"tgt.dispatchEvent(new DragEvent('drop',"
        f"Object.assign({{dataTransfer:dt,clientX:ex,clientY:ey}},o)))}}"
        f"src.dispatchEvent(new DragEvent('dragend',"
        f"Object.assign({{dataTransfer:dt,clientX:ex,clientY:ey}},o)));"
        f"}}catch(e){{}}"  # DataTransfer may not be constructable in some browsers
        # Final mouseup
        f"src.dispatchEvent(new MouseEvent('mouseup',"
        f"Object.assign({{clientX:ex,clientY:ey,button:0}},o)))"
        f"}})()"
    )
    await daemon.pilot.evaluate(js)

    return {
        "source": source,
        "end_x": round(end_x),
        "end_y": round(end_y),
    }


# ── Swipe gesture ──────────────────────────────────

async def _handle_swipe(daemon, params):
    """Perform a swipe gesture via mouse down/move/up sequence.

    Unlike drag (which dispatches DragEvent chain for drag-and-drop),
    swipe uses realistic xdotool mouse movement that triggers
    pointer/touch event listeners in swipe libraries (Swiper.js, etc.).
    """
    selector = params.get("selector", "")
    start_x = params.get("x")
    start_y = params.get("y")

    if selector:
        from ._utils import escape_js_string
        safe_sel = escape_js_string(selector)
        rect = await daemon.pilot.evaluate(
            f"(function(){{var el=document.querySelector('{safe_sel}');"
            "if(!el)return null;var r=el.getBoundingClientRect();"
            "return {x:r.x+r.width/2,y:r.y+r.height/2}})()"
        )
        if not rect or not isinstance(rect, dict):
            raise ValueError(f"Element not found: {selector}")
        if start_x is None:
            start_x = rect["x"]
        if start_y is None:
            start_y = rect["y"]
    elif start_x is None or start_y is None:
        raise ValueError("Must provide 'selector' or both 'x' and 'y'")

    direction = params.get("direction", "left")
    distance = params.get("distance", 200)
    speed = params.get("speed", "normal")
    steps = params.get("steps", 0)

    dx_map = {"left": -1, "right": 1, "up": 0, "down": 0}
    dy_map = {"left": 0, "right": 0, "up": -1, "down": 1}
    if direction not in dx_map:
        raise ValueError(f"Invalid direction '{direction}', use: left/right/up/down")

    end_x = start_x + dx_map[direction] * distance
    end_y = start_y + dy_map[direction] * distance

    if steps <= 0:
        steps = {"slow": 30, "normal": 15, "fast": 8}.get(speed, 15)
    step_delay = {"slow": 0.03, "normal": 0.015, "fast": 0.005}.get(speed, 0.015)

    session = daemon.pilot._session
    offset = await session._get_viewport_offset()
    scr_sx = int(start_x + offset[0])
    scr_sy = int(start_y + offset[1])
    scr_ex = int(end_x + offset[0])
    scr_ey = int(end_y + offset[1])

    # Close console so mouse events reach the page
    await session._close_console()
    await asyncio.sleep(0.1)

    # Move to start, press, swipe, release
    await session._xdt(["mousemove", str(scr_sx), str(scr_sy)])
    await asyncio.sleep(0.05)
    await session._xdt(["mousedown", "1"])
    session._page_has_focus = True
    await asyncio.sleep(0.05)

    for i in range(1, steps + 1):
        t = i / steps
        t_eased = 1 - (1 - t) ** 2  # ease-out
        cx = scr_sx + (scr_ex - scr_sx) * t_eased
        cy = scr_sy + (scr_ey - scr_sy) * t_eased
        if i < steps:
            if direction in ("left", "right"):
                cy += random.gauss(0, 0.5)
            else:
                cx += random.gauss(0, 0.5)
        await session._xdt(["mousemove", str(int(cx)), str(int(cy))])
        await asyncio.sleep(step_delay)

    await session._xdt(["mouseup", "1"])

    return {
        "start": {"x": round(start_x), "y": round(start_y)},
        "end": {"x": round(end_x), "y": round(end_y)},
        "direction": direction,
        "distance": distance,
        "steps": steps,
    }


# ── Iframe support ──────────────────────────────────

async def _handle_iframe_list(daemon, params):
    """List all iframes on the page."""
    result = await daemon.pilot.evaluate(
        "(function(){var frames=document.querySelectorAll('iframe,frame');"
        "return Array.from(frames).map(function(f,i){"
        "var ok=true;try{f.contentDocument}catch(e){ok=false}"
        "return {index:i,src:f.src||'',name:f.name||'',id:f.id||'',"
        "accessible:ok,"
        "selector:f.id?'#'+f.id:(f.name?'iframe[name=\"'+f.name+'\"]'"
        ":'iframe:nth-of-type('+(i+1)+')')}"
        "})})()"
    )
    return {"iframes": result if isinstance(result, list) else []}


async def _handle_iframe_eval(daemon, params):
    """Evaluate JavaScript inside an iframe."""
    selector = params.get("selector")
    expression = params.get("expression", "")
    if not selector:
        raise ValueError("Missing 'selector' parameter (iframe CSS selector)")
    if not expression:
        raise ValueError("Missing 'expression' parameter")
    from ._utils import escape_js_string
    safe_sel = escape_js_string(selector)
    code_json = json.dumps(expression)
    result = await daemon.pilot.evaluate(
        f"(function(){{var f=document.querySelector('{safe_sel}');"
        "if(!f)throw new Error('Iframe not found');"
        "if(!f.contentDocument)throw new Error('Cross-origin iframe');"
        f"return f.contentWindow.eval({code_json})"
        "})()"
    )
    return {"result": result}


async def _handle_iframe_text(daemon, params):
    """Get text content from inside an iframe."""
    selector = params.get("selector")
    if not selector:
        raise ValueError("Missing 'selector' parameter (iframe CSS selector)")
    from ._utils import escape_js_string
    safe_sel = escape_js_string(selector)
    inner = params.get("inner_selector", "")
    if inner:
        safe_inner = escape_js_string(inner)
        js = (
            f"(function(){{var f=document.querySelector('{safe_sel}');"
            "if(!f||!f.contentDocument)throw new Error('Iframe not accessible');"
            f"var el=f.contentDocument.querySelector('{safe_inner}');"
            "return el?el.innerText:''"
            "})()"
        )
    else:
        js = (
            f"(function(){{var f=document.querySelector('{safe_sel}');"
            "if(!f||!f.contentDocument)throw new Error('Iframe not accessible');"
            "return f.contentDocument.body?f.contentDocument.body.innerText:''"
            "})()"
        )
    text = await daemon.pilot.evaluate(js)
    if text is None:
        text = ""
    limit = params.get("limit")
    truncated = False
    if limit and isinstance(limit, int) and len(text) > limit:
        text = text[:limit]
        truncated = True
    return {"text": text, "truncated": truncated}


async def _handle_iframe_click(daemon, params):
    """Click an element inside an iframe.

    For same-origin iframes, uses contentDocument.querySelector.
    For cross-origin iframes, falls back to coordinate-based click
    at the center of the iframe (use x/y offsets to target specific
    elements within the iframe).
    """
    selector = params.get("selector")
    target = params.get("target")
    x_offset = params.get("x", 0) or 0
    y_offset = params.get("y", 0) or 0
    if not selector:
        raise ValueError("Missing 'selector' parameter (iframe CSS selector)")
    if not target:
        raise ValueError("Missing 'target' parameter (element CSS selector inside iframe)")
    from ._utils import escape_js_string
    safe_sel = escape_js_string(selector)
    safe_target = escape_js_string(target)
    # Try same-origin first
    try:
        result = await daemon.pilot.evaluate(
            f"(function(){{var f=document.querySelector('{safe_sel}');"
            "if(!f)throw new Error('Iframe not found');"
            "try{if(!f.contentDocument)throw new Error('x')}catch(e){"
            "throw new Error('Cross-origin iframe')};"
            f"var el=f.contentDocument.querySelector('{safe_target}');"
            "if(!el)throw new Error('Element not found in iframe');"
            "el.click();return 'ok'})()"
        )
        return {"clicked": target, "iframe": selector, "method": "contentDocument"}
    except Exception:
        pass
    # Cross-origin fallback: get iframe bounding rect and click by coordinates
    rect = await daemon.pilot.evaluate(
        f"(function(){{var f=document.querySelector('{safe_sel}');"
        "if(!f)return null;"
        "var r=f.getBoundingClientRect();"
        "return {x:Math.round(r.x),y:Math.round(r.y),"
        "w:Math.round(r.width),h:Math.round(r.height)}})()"
    )
    if not rect:
        raise ValueError(f"Iframe not found: {selector}")
    # Click at center of iframe, or at x/y offset from iframe top-left
    click_x = rect["x"] + (x_offset if x_offset else rect["w"] // 2)
    click_y = rect["y"] + (y_offset if y_offset else rect["h"] // 2)
    session = daemon.pilot._session
    await session._close_console()
    await asyncio.sleep(0.1)
    await session._xdt(["mousemove", "--sync", str(click_x), str(click_y)])
    await asyncio.sleep(0.05)
    await session._xdt(["click", "1"])
    return {"clicked": target, "iframe": selector, "method": "coordinate",
            "x": click_x, "y": click_y}


# ── File upload ──────────────────────────────────

async def _handle_upload(daemon, params):
    """Set file on an input[type=file] element."""
    selector = params.get("selector")
    path = params.get("path")
    if not selector:
        raise ValueError("Missing 'selector' parameter (CSS selector for file input)")
    if not path:
        raise ValueError("Missing 'path' parameter (file path)")
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        raise ValueError(f"File not found: {path}")
    size = os.path.getsize(path)
    if size > 5 * 1024 * 1024:
        raise ValueError("File too large (max 5MB for JS-based upload)")

    import base64
    import mimetypes
    with open(path, "rb") as fh:
        content = base64.b64encode(fh.read()).decode()
    filename = os.path.basename(path)
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"

    from ._utils import escape_js_string
    safe_sel = escape_js_string(selector)
    safe_name = escape_js_string(filename)
    safe_mime = escape_js_string(mime)

    result = await daemon.pilot.evaluate(
        f"(function(){{var input=document.querySelector('{safe_sel}');"
        "if(!input)throw new Error('Element not found');"
        "if(input.type!=='file')throw new Error('Element is not a file input');"
        f"var b=atob('{content}');"
        "var a=new Uint8Array(b.length);"
        "for(var i=0;i<b.length;i++)a[i]=b.charCodeAt(i);"
        f"var file=new File([a],'{safe_name}',{{type:'{safe_mime}'}});"
        "var dt=new DataTransfer();dt.items.add(file);"
        "input.files=dt.files;"
        "input.dispatchEvent(new Event('input',{bubbles:true}));"
        "input.dispatchEvent(new Event('change',{bubbles:true}));"
        f"return {{name:'{safe_name}',size:a.length,type:'{safe_mime}'}}"
        "})()"
    )
    return {"uploaded": result}


# ── Geolocation spoofing ──────────────────────────────────

_geolocation_override = None


async def _handle_geo_set(daemon, params):
    """Set geolocation override."""
    global _geolocation_override
    lat = params.get("latitude")
    lng = params.get("longitude")
    if lat is None or lng is None:
        raise ValueError("Missing 'latitude' and/or 'longitude' parameters")
    if not isinstance(lat, (int, float)) or not isinstance(lng, (int, float)):
        raise ValueError("'latitude' and 'longitude' must be numbers")
    if lat < -90 or lat > 90:
        raise ValueError("'latitude' must be -90 to 90")
    if lng < -180 or lng > 180:
        raise ValueError("'longitude' must be -180 to 180")
    accuracy = params.get("accuracy", 100)
    if not isinstance(accuracy, (int, float)) or accuracy < 0:
        raise ValueError("'accuracy' must be a non-negative number")
    _geolocation_override = {"lat": lat, "lng": lng, "accuracy": accuracy}
    await _inject_geolocation(daemon)
    return {"latitude": lat, "longitude": lng, "accuracy": accuracy}


async def _handle_geo_clear(daemon, params):
    """Clear geolocation override."""
    global _geolocation_override
    _geolocation_override = None
    await daemon.pilot.evaluate(
        "(function(){"
        "if(window.__tbp_geo_patched){"
        "if(window.__tbp_geo_orig_gcp)"
        "navigator.geolocation.getCurrentPosition=window.__tbp_geo_orig_gcp;"
        "if(window.__tbp_geo_orig_wp)"
        "navigator.geolocation.watchPosition=window.__tbp_geo_orig_wp;"
        "delete window.__tbp_geo_orig_gcp;"
        "delete window.__tbp_geo_orig_wp;"
        "delete window.__tbp_geo_patched}"
        "})()"
    )
    return {"cleared": True}


async def _inject_geolocation(daemon):
    """Inject geolocation override (saves originals for restore)."""
    if not _geolocation_override:
        return
    lat = _geolocation_override["lat"]
    lng = _geolocation_override["lng"]
    acc = _geolocation_override["accuracy"]
    js = (
        "(function(){"
        "if(window.__tbp_geo_patched)return;"
        "window.__tbp_geo_patched=true;"
        "window.__tbp_geo_orig_gcp=navigator.geolocation.getCurrentPosition;"
        "window.__tbp_geo_orig_wp=navigator.geolocation.watchPosition;"
        "var wid=1;"
        "function mkpos(){return {coords:{"
        f"latitude:{lat},longitude:{lng},accuracy:{acc},"
        "altitude:null,altitudeAccuracy:null,heading:null,speed:null},"
        "timestamp:Date.now()}};"
        "navigator.geolocation.getCurrentPosition=function(s,e,o){"
        "setTimeout(function(){s(mkpos())},0)};"
        "navigator.geolocation.watchPosition=function(s,e,o){"
        "setTimeout(function(){s(mkpos())},0);return wid++};"
        "navigator.geolocation.clearWatch=function(){}"
        "})()"
    )
    await daemon.pilot.evaluate(js)


# ── User agent switching ──────────────────────────────────

_useragent_override = None


async def _handle_useragent_set(daemon, params):
    """Set user agent override (JS-side navigator.userAgent)."""
    global _useragent_override
    ua = params.get("useragent", "")
    if not ua:
        raise ValueError("Missing 'useragent' parameter")
    if not isinstance(ua, str):
        raise ValueError("'useragent' must be a string")
    _useragent_override = ua
    await _inject_useragent(daemon)
    return {"useragent": ua}


async def _handle_useragent_clear(daemon, params):
    """Clear user agent override (restore original)."""
    global _useragent_override
    _useragent_override = None
    await daemon.pilot.evaluate(
        "(function(){"
        "if(window.__tbp_ua_patched&&window.__tbp_orig_ua){"
        "Object.defineProperty(navigator,'userAgent',"
        "{get:function(){return window.__tbp_orig_ua},configurable:true});"
        "delete window.__tbp_ua_patched;"
        "delete window.__tbp_orig_ua}"
        "})()"
    )
    return {"cleared": True}


async def _inject_useragent(daemon):
    """Inject user agent override via Object.defineProperty."""
    if not _useragent_override:
        return
    from ._utils import escape_js_string
    safe_ua = escape_js_string(_useragent_override)
    js = (
        "(function(){"
        "if(!window.__tbp_ua_patched){"
        "window.__tbp_orig_ua=navigator.userAgent;"
        "window.__tbp_ua_patched=true}"
        f"Object.defineProperty(navigator,'userAgent',"
        f"{{get:function(){{return '{safe_ua}'}},configurable:true}})"
        "})()"
    )
    await daemon.pilot.evaluate(js)


# ── Cookie injection ──────────────────────────────────

async def _handle_cookie_set(daemon, params):
    """Set a cookie via document.cookie."""
    name = params.get("name")
    value = params.get("value", "")
    if not name:
        raise ValueError("Missing 'name' parameter")
    if not isinstance(name, str):
        raise ValueError("'name' must be a string")
    if not isinstance(value, str):
        raise ValueError("'value' must be a string")

    import re
    _bad = re.compile(r'[;\r\n]')
    if _bad.search(name) or "=" in name:
        raise ValueError("'name' must not contain =, ;, or newlines")

    import urllib.parse
    safe_value = urllib.parse.quote(value, safe="")
    parts = [f"{name}={safe_value}"]

    path = params.get("path", "/")
    if _bad.search(path):
        raise ValueError("'path' must not contain ; or newlines")
    parts.append(f"path={path}")
    domain = params.get("domain", "")
    if domain:
        if not isinstance(domain, str):
            raise ValueError("'domain' must be a string")
        if _bad.search(domain):
            raise ValueError("'domain' must not contain ; or newlines")
        parts.append(f"domain={domain}")
    max_age = params.get("max_age")
    if max_age is not None:
        if not isinstance(max_age, (int, float)):
            raise ValueError("'max_age' must be a number")
        parts.append(f"max-age={int(max_age)}")
    secure = params.get("secure", False)
    if secure:
        parts.append("secure")
    samesite = params.get("samesite", "")
    if samesite:
        if samesite not in ("Strict", "Lax", "None"):
            raise ValueError("'samesite' must be Strict, Lax, or None")
        parts.append(f"SameSite={samesite}")

    cookie_str = "; ".join(parts)
    from ._utils import escape_js_string
    safe_cookie = escape_js_string(cookie_str)
    await daemon.pilot.evaluate(f"document.cookie='{safe_cookie}'")
    return {"set": name, "cookie": cookie_str}


# ── Local/session storage ──────────────────────────────────

async def _handle_storage(daemon, params):
    """Manage localStorage or sessionStorage."""
    storage = params.get("type", "local")
    if storage not in ("local", "session"):
        raise ValueError("'type' must be 'local' or 'session'")
    action = params.get("action", "list")
    store_obj = "localStorage" if storage == "local" else "sessionStorage"

    if action == "get":
        key = params.get("key", "")
        if not key:
            raise ValueError("Missing 'key' parameter")
        from ._utils import escape_js_string
        safe_key = escape_js_string(key)
        result = await daemon.pilot.evaluate(
            f"{store_obj}.getItem('{safe_key}')"
        )
        return {"key": key, "value": result}

    elif action == "set":
        key = params.get("key", "")
        value = params.get("value", "")
        if not key:
            raise ValueError("Missing 'key' parameter")
        if not isinstance(value, str):
            value = str(value)
        from ._utils import escape_js_string
        safe_key = escape_js_string(key)
        safe_val = escape_js_string(value)
        result = await daemon.pilot.evaluate(
            f"(function(){{try{{{store_obj}.setItem('{safe_key}','{safe_val}');"
            "return 'ok'}catch(e){return e.message}})()"
        )
        if result != "ok":
            raise RuntimeError(f"Storage setItem failed: {result}")
        return {"key": key, "set": True}

    elif action == "remove":
        key = params.get("key", "")
        if not key:
            raise ValueError("Missing 'key' parameter")
        from ._utils import escape_js_string
        safe_key = escape_js_string(key)
        await daemon.pilot.evaluate(f"{store_obj}.removeItem('{safe_key}')")
        return {"key": key, "removed": True}

    elif action == "clear":
        await daemon.pilot.evaluate(f"{store_obj}.clear()")
        return {"cleared": True, "type": storage}

    elif action == "list":
        limit = params.get("limit", 100)
        if not isinstance(limit, int) or limit < 1 or limit > 10000:
            limit = 100
        result = await daemon.pilot.evaluate(
            f"(function(){{var s={store_obj};var r=[];"
            f"for(var i=0;i<Math.min(s.length,{limit});i++){{"
            "var k=s.key(i);r.push({key:k,value:s.getItem(k)})"
            "}return r})()"
        )
        return {"items": result if isinstance(result, list) else [],
                "type": storage}

    else:
        raise ValueError(f"Unknown action: {action}")


# ── Clipboard access ──────────────────────────────────

async def _handle_clipboard_read(daemon, params):
    """Read text from the Xvfb system clipboard."""
    display = os.environ.get("DISPLAY", ":99")
    session = daemon.pilot._session
    if hasattr(session, "_display"):
        display = session._display
    env = {**os.environ, "DISPLAY": display}
    try:
        proc = await asyncio.create_subprocess_exec(
            "xclip", "-selection", "clipboard", "-o",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except FileNotFoundError:
        raise RuntimeError("xclip not installed (required for clipboard)")
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError("Clipboard read timed out (5s)")
    if proc.returncode != 0:
        raise RuntimeError(f"Clipboard read failed: {stderr.decode().strip()}")
    return {"text": stdout.decode("utf-8", errors="replace")}


async def _handle_clipboard_write(daemon, params):
    """Write text to the Xvfb system clipboard."""
    text = params.get("text", "")
    if not isinstance(text, str):
        raise ValueError("'text' must be a string")
    display = os.environ.get("DISPLAY", ":99")
    session = daemon.pilot._session
    if hasattr(session, "_display"):
        display = session._display
    env = {**os.environ, "DISPLAY": display}
    try:
        proc = await asyncio.create_subprocess_exec(
            "xclip", "-selection", "clipboard",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except FileNotFoundError:
        raise RuntimeError("xclip not installed (required for clipboard)")
    try:
        _, stderr = await asyncio.wait_for(
            proc.communicate(text.encode("utf-8")), timeout=5)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError("Clipboard write timed out (5s)")
    if proc.returncode != 0:
        raise RuntimeError(f"Clipboard write failed: {stderr.decode().strip()}")
    return {"written": len(text)}


# ── Form auto-fill ──────────────────────────────────

async def _handle_form_fill(daemon, params):
    """Fill multiple form fields at once from a list."""
    fields = params.get("fields", [])
    if not fields or not isinstance(fields, list):
        raise ValueError("Missing 'fields' (list of {selector, value} objects)")
    if len(fields) > 100:
        raise ValueError("Max 100 fields per form fill")

    # Validate and sanitize
    clean = []
    for field in fields:
        if not isinstance(field, dict):
            continue
        selector = field.get("selector", "")
        if not selector or not isinstance(selector, str):
            continue
        clean.append({"selector": selector, "value": field.get("value", "")})

    if not clean:
        return {"filled": 0, "total": len(fields), "results": []}

    fields_json = json.dumps(clean)

    result = await daemon.pilot.evaluate(
        f"(function(){{var fields={fields_json};var r=[];"
        "fields.forEach(function(f,i){"
        "var el=document.querySelector(f.selector);"
        "if(!el){r.push({index:i,selector:f.selector,success:false,"
        "error:'Not found'});return}"
        "var tag=el.tagName.toLowerCase();"
        "if(tag==='select'){"
        "el.value=String(f.value);"
        "el.dispatchEvent(new Event('change',{bubbles:true}));"
        "r.push({index:i,selector:f.selector,success:true,type:'select'});"
        "return}"
        "if(el.type==='checkbox'||el.type==='radio'){"
        "var cs=Object.getOwnPropertyDescriptor("
        "HTMLInputElement.prototype,'checked');"
        "if(cs&&cs.set){cs.set.call(el,!!f.value)}else{el.checked=!!f.value}"
        "el.dispatchEvent(new Event('change',{bubbles:true}));"
        "r.push({index:i,selector:f.selector,success:true,type:el.type});"
        "return}"
        "var sv=String(f.value);"
        "if(el instanceof HTMLInputElement||el instanceof HTMLTextAreaElement){"
        "var proto=tag==='textarea'?HTMLTextAreaElement.prototype"
        ":HTMLInputElement.prototype;"
        "var ns=Object.getOwnPropertyDescriptor(proto,'value');"
        "if(ns&&ns.set){ns.set.call(el,sv)}else{el.value=sv}"
        "}else{el.value!==undefined?el.value=sv:el.textContent=sv}"
        "el.dispatchEvent(new Event('input',{bubbles:true}));"
        "el.dispatchEvent(new Event('change',{bubbles:true}));"
        "r.push({index:i,selector:f.selector,success:true,type:'filled'})"
        "});return r})()"
    )

    results = result if isinstance(result, list) else []
    filled = len([r for r in results if r.get("success")])
    # Dismiss save-password/address popups that Firefox shows after form fill
    if filled > 0:
        try:
            await daemon.pilot._session._dismiss_popup()
        except Exception:
            pass
    return {"filled": filled, "total": len(clean), "results": results}


# ── HTTP header injection ──────────────────────────────────

_custom_headers = {}


async def _handle_headers_set(daemon, params):
    """Set custom request headers via fetch/XHR interception."""
    global _custom_headers
    headers = params.get("headers", {})
    if not headers or not isinstance(headers, dict):
        raise ValueError("Missing 'headers' (dict of name: value)")
    for k, v in headers.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise ValueError("Header names and values must be strings")
    _custom_headers.update(headers)
    await _inject_headers(daemon)
    return {"headers": dict(_custom_headers)}


async def _handle_headers_clear(daemon, params):
    """Clear custom request headers."""
    global _custom_headers
    _custom_headers.clear()
    await daemon.pilot.evaluate(
        "(function(){"
        "if(window.__tbp_hdr_patched){"
        "if(window.__tbp_hdr_orig_fetch)window.fetch=window.__tbp_hdr_orig_fetch;"
        "if(window.__tbp_hdr_orig_xhr_open)"
        "XMLHttpRequest.prototype.open=window.__tbp_hdr_orig_xhr_open;"
        "if(window.__tbp_hdr_orig_xhr_send)"
        "XMLHttpRequest.prototype.send=window.__tbp_hdr_orig_xhr_send;"
        "delete window.__tbp_hdr_patched;"
        "delete window.__tbp_hdr_orig_fetch;"
        "delete window.__tbp_hdr_orig_xhr_open;"
        "delete window.__tbp_hdr_orig_xhr_send;"
        "delete window.__tbp_custom_headers}"
        "})()"
    )
    return {"cleared": True}


async def _handle_headers_list(daemon, params):
    """List current custom headers."""
    return {"headers": dict(_custom_headers)}


async def _inject_headers(daemon):
    """Inject fetch/XHR hooks to add custom headers to requests."""
    if not _custom_headers:
        return
    headers_json = json.dumps(_custom_headers)
    js = (
        "(function(){"
        f"window.__tbp_custom_headers={headers_json};"
        "if(window.__tbp_hdr_patched)return;"
        "window.__tbp_hdr_patched=true;"
        "window.__tbp_hdr_orig_fetch=window.fetch;"
        "window.fetch=function(u,o){"
        "o=o||{};o.headers=o.headers||{};"
        "if(o.headers instanceof Headers){"
        "var h=window.__tbp_custom_headers;"
        "for(var k in h)o.headers.set(k,h[k])"
        "}else{"
        "var h=window.__tbp_custom_headers;"
        "for(var k in h)o.headers[k]=h[k]}"
        "return window.__tbp_hdr_orig_fetch.call(this,u,o)};"
        "window.__tbp_hdr_orig_xhr_open=XMLHttpRequest.prototype.open;"
        "window.__tbp_hdr_orig_xhr_send=XMLHttpRequest.prototype.send;"
        "XMLHttpRequest.prototype.open=function(){"
        "this.__tbp_hdr_args=arguments;"
        "return window.__tbp_hdr_orig_xhr_open.apply(this,arguments)};"
        "XMLHttpRequest.prototype.send=function(){"
        "var h=window.__tbp_custom_headers;"
        "for(var k in h)try{this.setRequestHeader(k,h[k])}catch(e){}"
        "return window.__tbp_hdr_orig_xhr_send.apply(this,arguments)}"
        "})()"
    )
    await daemon.pilot.evaluate(js)


# ── Page performance metrics ──────────────────────────────────

async def _handle_perf(daemon, params):
    """Get page performance metrics."""
    result = await daemon.pilot.evaluate(
        "(function(){"
        "var p=performance;"
        "var t=p.timing||{};"
        "var nav=p.getEntriesByType('navigation')[0]||{};"
        "var r={};"
        "r.url=location.href;"
        "r.dom_content_loaded=Math.round(nav.domContentLoadedEventEnd"
        "||t.domContentLoadedEventEnd-t.navigationStart||0);"
        "r.load_complete=Math.round(nav.loadEventEnd"
        "||t.loadEventEnd-t.navigationStart||0);"
        "r.dom_interactive=Math.round(nav.domInteractive"
        "||t.domInteractive-t.navigationStart||0);"
        "r.first_byte=Math.round(nav.responseStart"
        "||t.responseStart-t.navigationStart||0);"
        "r.dom_elements=document.querySelectorAll('*').length;"
        "r.scripts=document.querySelectorAll('script').length;"
        "r.stylesheets=document.querySelectorAll('link[rel=stylesheet]').length;"
        "r.images=document.images.length;"
        "r.iframes=document.querySelectorAll('iframe').length;"
        "try{r.memory={"
        "used:Math.round(performance.memory.usedJSHeapSize/1048576),"
        "total:Math.round(performance.memory.totalJSHeapSize/1048576),"
        "limit:Math.round(performance.memory.jsHeapSizeLimit/1048576)"
        "}}catch(e){r.memory=null}"
        "return r})()"
    )
    return result if isinstance(result, dict) else {}


# ── Element attributes ──────────────────────────────────

async def _handle_attr_get(daemon, params):
    """Get attribute(s) from an element."""
    selector = params.get("selector")
    if not selector:
        raise ValueError("Missing 'selector' parameter")
    attr = params.get("name", "")
    from ._utils import escape_js_string
    safe_sel = escape_js_string(selector)

    if attr:
        safe_attr = escape_js_string(attr)
        result = await daemon.pilot.evaluate(
            f"(function(){{var el=document.querySelector('{safe_sel}');"
            f"if(!el)return{{__tbp_err:'not_found'}};return el.getAttribute('{safe_attr}')"
            "})()"
        )
        if isinstance(result, dict) and result.get("__tbp_err") == "not_found":
            raise ValueError(f"Element not found: {selector}")
        return {"selector": selector, "name": attr, "value": result}
    else:
        result = await daemon.pilot.evaluate(
            f"(function(){{var el=document.querySelector('{safe_sel}');"
            "if(!el)return null;var r={};"
            "for(var i=0;i<el.attributes.length;i++){"
            "r[el.attributes[i].name]=el.attributes[i].value}"
            "return r})()"
        )
        if result is None:
            raise ValueError(f"Element not found: {selector}")
        return {"selector": selector, "attributes": result}


async def _handle_attr_set(daemon, params):
    """Set an attribute on an element."""
    selector = params.get("selector")
    name = params.get("name")
    value = params.get("value", "")
    if not selector:
        raise ValueError("Missing 'selector' parameter")
    if not name:
        raise ValueError("Missing 'name' parameter")
    if not isinstance(name, str):
        raise ValueError("'name' must be a string")
    if not isinstance(value, str):
        value = str(value)
    from ._utils import escape_js_string
    safe_sel = escape_js_string(selector)
    safe_name = escape_js_string(name)
    safe_val = escape_js_string(value)
    result = await daemon.pilot.evaluate(
        f"(function(){{var el=document.querySelector('{safe_sel}');"
        f"if(!el)return null;el.setAttribute('{safe_name}','{safe_val}');"
        "return true})()"
    )
    if result is None:
        raise ValueError(f"Element not found: {selector}")
    return {"selector": selector, "name": name, "value": value}


async def _handle_attr_remove(daemon, params):
    """Remove an attribute from an element."""
    selector = params.get("selector")
    name = params.get("name")
    if not selector:
        raise ValueError("Missing 'selector' parameter")
    if not name:
        raise ValueError("Missing 'name' parameter")
    from ._utils import escape_js_string
    safe_sel = escape_js_string(selector)
    safe_name = escape_js_string(name)
    result = await daemon.pilot.evaluate(
        f"(function(){{var el=document.querySelector('{safe_sel}');"
        f"if(!el)return null;el.removeAttribute('{safe_name}');return true"
        "})()"
    )
    if result is None:
        raise ValueError(f"Element not found: {selector}")
    return {"selector": selector, "removed": name}


# ── Browser profile management ──────────────────────────────────

PROFILES_DIR = os.path.join(TBP_DIR, "profiles")


async def _handle_profile_save(daemon, params):
    """Save current browser state as a named profile."""
    name = params.get("name")
    if not name:
        raise ValueError("Missing 'name' parameter")
    if not isinstance(name, str):
        raise ValueError("'name' must be a string")
    import re
    if not re.match(r'^[a-zA-Z0-9_-]+$', name):
        raise ValueError("'name' must be alphanumeric (a-z, 0-9, _, -)")

    os.makedirs(PROFILES_DIR, mode=0o700, exist_ok=True)
    profile_dir = os.path.join(PROFILES_DIR, name)
    os.makedirs(profile_dir, mode=0o700, exist_ok=True)

    # Save cookies
    cookies_path = os.path.join(profile_dir, "cookies.json")
    n_cookies = await daemon.pilot.save_cookies(cookies_path)

    # Save localStorage
    storage = await daemon.pilot.evaluate(
        "(function(){var r={};try{"
        "for(var i=0;i<localStorage.length;i++){"
        "var k=localStorage.key(i);r[k]=localStorage.getItem(k)}"
        "}catch(e){}return r})()"
    )
    storage_path = os.path.join(profile_dir, "storage.json")
    with open(storage_path, "w") as f:
        json.dump(storage if isinstance(storage, dict) else {}, f)

    # Save current URL
    url = await daemon.pilot.url()
    meta = {"url": url, "timestamp": time.time()}
    meta_path = os.path.join(profile_dir, "meta.json")
    with open(meta_path, "w") as f:
        json.dump(meta, f)

    return {"name": name, "cookies": n_cookies,
            "storage_keys": len(storage) if isinstance(storage, dict) else 0,
            "path": profile_dir}


async def _handle_profile_load(daemon, params):
    """Load a saved browser profile."""
    name = params.get("name")
    if not name:
        raise ValueError("Missing 'name' parameter")
    import re
    if not re.match(r'^[a-zA-Z0-9_-]+$', name):
        raise ValueError("'name' must be alphanumeric (a-z, 0-9, _, -)")

    profile_dir = os.path.join(PROFILES_DIR, name)
    if not os.path.isdir(profile_dir):
        raise ValueError(f"Profile not found: {name}")

    result = {"name": name}

    # Load cookies
    cookies_path = os.path.join(profile_dir, "cookies.json")
    if os.path.isfile(cookies_path):
        n = await daemon.pilot.load_cookies(cookies_path)
        result["cookies"] = n

    # Load meta (check origin match before localStorage)
    meta_path = os.path.join(profile_dir, "meta.json")
    saved_url = ""
    if os.path.isfile(meta_path):
        with open(meta_path) as f:
            meta = json.load(f)
        saved_url = meta.get("url", "")
        result["url"] = saved_url

    # Warn if origin mismatch (localStorage is origin-scoped)
    current_url = await daemon.pilot.url()
    if saved_url and current_url:
        from urllib.parse import urlparse
        sp, cp = urlparse(saved_url), urlparse(current_url)
        saved_origin = (sp.scheme, sp.netloc)
        current_origin = (cp.scheme, cp.netloc)
        if sp.netloc and cp.netloc and saved_origin != current_origin:
            cur_str = f"{cp.scheme}://{cp.netloc}"
            sav_str = f"{sp.scheme}://{sp.netloc}"
            result["origin_warning"] = (
                f"Current origin ({cur_str}) differs from saved "
                f"origin ({sav_str}). localStorage may go to wrong origin."
            )

    # Load localStorage (batched into single evaluate)
    storage_path = os.path.join(profile_dir, "storage.json")
    if os.path.isfile(storage_path):
        with open(storage_path) as f:
            storage = json.load(f)
        if isinstance(storage, dict) and storage:
            storage_json = json.dumps(storage)
            await daemon.pilot.evaluate(
                "(function(){var d=" + storage_json + ";"
                "try{for(var k in d)localStorage.setItem(k,d[k])"
                "}catch(e){}})()"
            )
            result["storage_keys"] = len(storage)

    return result


async def _handle_profile_list(daemon, params):
    """List saved profiles."""
    import re
    profiles = []
    if os.path.isdir(PROFILES_DIR):
        for name in sorted(os.listdir(PROFILES_DIR)):
            if not re.match(r'^[a-zA-Z0-9_-]+$', name):
                continue
            pdir = os.path.join(PROFILES_DIR, name)
            if not os.path.isdir(pdir):
                continue
            meta_path = os.path.join(pdir, "meta.json")
            meta = {}
            if os.path.isfile(meta_path):
                with open(meta_path) as f:
                    meta = json.load(f)
            profiles.append({
                "name": name,
                "url": meta.get("url", ""),
                "timestamp": meta.get("timestamp", 0),
            })
    return {"profiles": profiles}


async def _handle_profile_delete(daemon, params):
    """Delete a saved profile."""
    name = params.get("name")
    if not name:
        raise ValueError("Missing 'name' parameter")
    import re
    if not re.match(r'^[a-zA-Z0-9_-]+$', name):
        raise ValueError("'name' must be alphanumeric")

    profile_dir = os.path.join(PROFILES_DIR, name)
    if not os.path.isdir(profile_dir):
        raise ValueError(f"Profile not found: {name}")

    import shutil
    shutil.rmtree(profile_dir)
    return {"deleted": name}


# ── Page search (find text + highlight) ──────────────────────────────────

_search_state = {"query": "", "index": 0, "count": 0}


async def _handle_search(daemon, params):
    """Find text on page and highlight matches."""
    global _search_state
    query = params.get("query", "")
    if not query:
        raise ValueError("Missing 'query' parameter")
    if not isinstance(query, str):
        raise ValueError("'query' must be a string")
    case_sensitive = bool(params.get("case_sensitive", False))
    _search_state = {"query": query, "index": 0, "count": 0,
                     "case_sensitive": case_sensitive}
    result = await _inject_search(daemon)
    _search_state["count"] = result.get("count", 0)
    _search_state["index"] = 0
    return {"query": query, "count": _search_state["count"], "index": 0}


async def _handle_search_next(daemon, params):
    """Navigate to next search match."""
    global _search_state
    if not _search_state.get("query"):
        raise ValueError("No active search. Use 'search' first.")
    count = _search_state["count"]
    if count == 0:
        return {"index": 0, "count": 0}
    _search_state["index"] = (_search_state["index"] + 1) % count
    idx = _search_state["index"]
    await daemon.pilot.evaluate(
        f"(function(){{var m=document.querySelectorAll('mark.__tbp_hl');"
        f"if(!m.length)return;m.forEach(function(e){{e.style.backgroundColor='yellow'}});"
        f"if(m[{idx}]){{m[{idx}].style.backgroundColor='orange';"
        f"m[{idx}].scrollIntoView({{block:'center',behavior:'smooth'}})}}}})() "
    )
    return {"index": idx, "count": count}


async def _handle_search_prev(daemon, params):
    """Navigate to previous search match."""
    global _search_state
    if not _search_state.get("query"):
        raise ValueError("No active search. Use 'search' first.")
    count = _search_state["count"]
    if count == 0:
        return {"index": 0, "count": 0}
    _search_state["index"] = (_search_state["index"] - 1) % count
    idx = _search_state["index"]
    await daemon.pilot.evaluate(
        f"(function(){{var m=document.querySelectorAll('mark.__tbp_hl');"
        f"if(!m.length)return;m.forEach(function(e){{e.style.backgroundColor='yellow'}});"
        f"if(m[{idx}]){{m[{idx}].style.backgroundColor='orange';"
        f"m[{idx}].scrollIntoView({{block:'center',behavior:'smooth'}})}}}})() "
    )
    return {"index": idx, "count": count}


async def _handle_search_clear(daemon, params):
    """Clear search highlights."""
    global _search_state
    _search_state = {"query": "", "index": 0, "count": 0}
    await daemon.pilot.evaluate(
        "(function(){var marks=document.querySelectorAll('mark.__tbp_hl');"
        "marks.forEach(function(m){var p=m.parentNode;"
        "p.replaceChild(document.createTextNode(m.textContent),m);"
        "p.normalize()})})()"
    )
    return {"cleared": True}


async def _inject_search(daemon):
    """Inject search highlighting JS."""
    query = _search_state.get("query", "")
    if not query:
        return {"count": 0}
    case_sensitive = _search_state.get("case_sensitive", False)
    safe_query = json.dumps(query)
    js = (
        "(function(){"
        # Clear previous highlights
        "var old=document.querySelectorAll('mark.__tbp_hl');"
        "old.forEach(function(m){var p=m.parentNode;"
        "p.replaceChild(document.createTextNode(m.textContent),m);"
        "p.normalize()});"
        # Add highlight CSS
        "if(!document.getElementById('__tbp_hl_css')){"
        "var s=document.createElement('style');s.id='__tbp_hl_css';"
        "s.textContent='mark.__tbp_hl{background:yellow;color:black;padding:0}';"
        "document.head.appendChild(s)}"
        # Walk text nodes and highlight matches
        f"var q={safe_query};"
        f"var ci={str(not case_sensitive).lower()};"
        "var count=0;"
        "if(!document.body)return{count:0};"
        "var walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);"
        "var nodes=[];while(walker.nextNode())nodes.push(walker.currentNode);"
        "for(var i=0;i<nodes.length;i++){"
        "var n=nodes[i];var txt=n.textContent;"
        "var search=ci?txt.toLowerCase():txt;"
        "var term=ci?q.toLowerCase():q;"
        "var idx=search.indexOf(term);if(idx===-1)continue;"
        "var frag=document.createDocumentFragment();"
        "var last=0;"
        "while(idx!==-1){"
        "if(idx>last)frag.appendChild(document.createTextNode(txt.slice(last,idx)));"
        "var mark=document.createElement('mark');"
        "mark.className='__tbp_hl';"
        "mark.textContent=txt.slice(idx,idx+term.length);"
        "frag.appendChild(mark);count++;"
        "last=idx+term.length;"
        "idx=search.indexOf(term,last)}"
        "if(last<txt.length)frag.appendChild(document.createTextNode(txt.slice(last)));"
        "n.parentNode.replaceChild(frag,n)}"
        # Scroll to first match
        "var first=document.querySelector('mark.__tbp_hl');"
        "if(first){first.style.backgroundColor='orange';"
        "first.scrollIntoView({block:'center',behavior:'smooth'})}"
        "return{count:count}})()"
    )
    result = await daemon.pilot.evaluate(js)
    return result if isinstance(result, dict) else {"count": 0}


# ── Shadow DOM access ──────────────────────────────────

_SHADOW_DEEP_QUERY_JS = (
    "function __tbp_dq(root,sel,d){"
    "if(d===undefined)d=5;if(d<=0)return null;"
    "var el=root.querySelector(sel);if(el)return el;"
    "var all=root.getElementsByTagName('*');"
    "for(var i=0;i<all.length;i++){"
    "if(all[i].shadowRoot){"
    "var f=__tbp_dq(all[i].shadowRoot,sel,d-1);"
    "if(f)return f}}return null}"
)


async def _handle_shadow_query(daemon, params):
    """Find element piercing shadow DOM boundaries."""
    selector = params.get("selector")
    if not selector:
        raise ValueError("Missing 'selector' parameter")
    from ._utils import escape_js_string
    safe_sel = escape_js_string(selector)
    result = await daemon.pilot.evaluate(
        f"(function(){{{_SHADOW_DEEP_QUERY_JS}"
        f"var el=__tbp_dq(document,'{safe_sel}');"
        "if(!el)return null;"
        "var r={tag:el.tagName.toLowerCase(),text:(el.textContent||'').slice(0,500)};"
        "r.id=el.id||'';r.className=String(el.className||'');"
        "var a={};for(var i=0;i<el.attributes.length;i++)"
        "{a[el.attributes[i].name]=el.attributes[i].value}"
        "r.attributes=a;return r})()"
    )
    if result is None:
        raise ValueError(f"Element not found (including shadow roots): {selector}")
    return result


async def _handle_shadow_text(daemon, params):
    """Get text from element piercing shadow DOM."""
    selector = params.get("selector")
    if not selector:
        raise ValueError("Missing 'selector' parameter")
    from ._utils import escape_js_string
    safe_sel = escape_js_string(selector)
    result = await daemon.pilot.evaluate(
        f"(function(){{{_SHADOW_DEEP_QUERY_JS}"
        f"var el=__tbp_dq(document,'{safe_sel}');"
        "if(!el)return null;return el.textContent})()"
    )
    if result is None:
        raise ValueError(f"Element not found (including shadow roots): {selector}")
    return {"selector": selector, "text": result}


async def _handle_shadow_click(daemon, params):
    """Click element piercing shadow DOM."""
    selector = params.get("selector")
    if not selector:
        raise ValueError("Missing 'selector' parameter")
    from ._utils import escape_js_string
    safe_sel = escape_js_string(selector)
    result = await daemon.pilot.evaluate(
        f"(function(){{{_SHADOW_DEEP_QUERY_JS}"
        f"var el=__tbp_dq(document,'{safe_sel}');"
        "if(!el)return null;el.scrollIntoView({block:'center'});"
        "el.click();return true})()"
    )
    if result is None:
        raise ValueError(f"Element not found (including shadow roots): {selector}")
    return {"selector": selector, "clicked": True}


# ── Response body capture ──────────────────────────────────

_response_capture_enabled = False


async def _handle_responses_start(daemon, params):
    """Start capturing fetch/XHR response bodies."""
    global _response_capture_enabled
    _response_capture_enabled = True
    await _inject_response_capture(daemon)
    return {"enabled": True}


async def _handle_responses_stop(daemon, params):
    """Stop capturing response bodies and restore originals."""
    global _response_capture_enabled
    _response_capture_enabled = False
    await daemon.pilot.evaluate(
        "(function(){"
        "if(window.__tbp_resp_patched){"
        "if(window.__tbp_resp_orig_fetch)window.fetch=window.__tbp_resp_orig_fetch;"
        "if(window.__tbp_resp_orig_xhr_open)"
        "XMLHttpRequest.prototype.open=window.__tbp_resp_orig_xhr_open;"
        "if(window.__tbp_resp_orig_xhr_send)"
        "XMLHttpRequest.prototype.send=window.__tbp_resp_orig_xhr_send;"
        "delete window.__tbp_resp_patched;"
        "delete window.__tbp_resp_orig_fetch;"
        "delete window.__tbp_resp_orig_xhr_open;"
        "delete window.__tbp_resp_orig_xhr_send}"
        "})()"
    )
    return {"enabled": False}


async def _handle_responses_logs(daemon, params):
    """Get captured response bodies."""
    limit = max(1, min(int(params.get("limit", 100)), 500))
    clear = bool(params.get("clear", False))
    result = await daemon.pilot.evaluate(
        f"(function(){{var r=window.__tbp_responses||[];"
        f"var out=r.slice(-{limit});"
        f"if({str(clear).lower()})window.__tbp_responses=r.slice(0,r.length-{limit});"
        "return out})()"
    )
    responses = result if isinstance(result, list) else []
    return {"responses": responses, "count": len(responses)}


async def _handle_responses_clear(daemon, params):
    """Clear captured responses."""
    await daemon.pilot.evaluate("window.__tbp_responses=[]")
    return {"cleared": True}


async def _inject_response_capture(daemon):
    """Inject fetch/XHR response body capture hooks."""
    js = (
        "(function(){"
        "if(!window.__tbp_responses)window.__tbp_responses=[];"
        "if(window.__tbp_resp_patched)return;"
        "window.__tbp_resp_patched=true;"
        # Save originals
        "window.__tbp_resp_orig_fetch=window.fetch;"
        "window.__tbp_resp_orig_xhr_open=XMLHttpRequest.prototype.open;"
        "window.__tbp_resp_orig_xhr_send=XMLHttpRequest.prototype.send;"
        # Patch fetch
        "window.fetch=function(u,o){"
        "var url=(typeof u==='string')?u:(u&&u.url)||'';"
        "return window.__tbp_resp_orig_fetch.apply(this,arguments).then(function(resp){"
        "var clone=resp.clone();"
        "clone.text().then(function(body){"
        "window.__tbp_responses.push({"
        "url:resp.url||url,status:resp.status,type:resp.headers.get('content-type')||'',"
        "body:body.slice(0,10240),size:body.length,timestamp:Date.now()});"
        "if(window.__tbp_responses.length>500)"
        "window.__tbp_responses=window.__tbp_responses.slice(-500)"
        "}).catch(function(){});"
        "return resp}).catch(function(e){"
        "window.__tbp_responses.push({url:url,status:0,type:'error',"
        "body:e.message||'',size:0,timestamp:Date.now()});"
        "throw e})};"
        # Patch XHR
        "XMLHttpRequest.prototype.open=function(){"
        "this.__tbp_url=(arguments[1]||'').toString();"
        "return window.__tbp_resp_orig_xhr_open.apply(this,arguments)};"
        "XMLHttpRequest.prototype.send=function(){"
        "var xhr=this;"
        "xhr.addEventListener('load',function(){"
        "var body=(xhr.responseText||'').slice(0,10240);"
        "window.__tbp_responses.push({"
        "url:xhr.responseURL||xhr.__tbp_url||'',status:xhr.status,"
        "type:xhr.getResponseHeader('content-type')||'',"
        "body:body,size:(xhr.responseText||'').length,timestamp:Date.now()});"
        "if(window.__tbp_responses.length>500)"
        "window.__tbp_responses=window.__tbp_responses.slice(-500)"
        "});"
        "return window.__tbp_resp_orig_xhr_send.apply(this,arguments)}"
        "})()"
    )
    await daemon.pilot.evaluate(js)


# ── Multi-tab session save/restore ──────────────────────────────────

SESSIONS_DIR = os.path.join(TBP_DIR, "sessions")


async def _handle_session_save(daemon, params):
    """Save all open tabs as a named session."""
    name = params.get("name")
    if not name:
        raise ValueError("Missing 'name' parameter")
    if not isinstance(name, str):
        raise ValueError("'name' must be a string")
    import re
    if not re.match(r'^[a-zA-Z0-9_-]+$', name):
        raise ValueError("'name' must be alphanumeric (a-z, 0-9, _, -)")

    session = daemon.pilot._session
    tabs = []

    # Collect current tab first
    url = await daemon.pilot.url()
    title = await daemon.pilot.title()
    tabs.append({"url": url, "title": title})

    # Try tabs 2-9 by switching and checking if URL changed
    original_url = url
    for i in range(2, 10):
        try:
            await session._xdt(["key", f"ctrl+{i}"])
            await asyncio.sleep(0.5)
            new_url = await daemon.pilot.url()
            new_title = await daemon.pilot.title()
            if new_url == tabs[-1]["url"] and new_title == tabs[-1]["title"]:
                # Same tab — no more tabs
                break
            tabs.append({"url": new_url, "title": new_title})
        except Exception:
            break

    # Return to first tab
    await session._xdt(["key", "ctrl+1"])
    await asyncio.sleep(0.3)

    # Save session
    os.makedirs(SESSIONS_DIR, mode=0o700, exist_ok=True)
    session_data = {"tabs": tabs, "timestamp": time.time()}
    session_path = os.path.join(SESSIONS_DIR, f"{name}.json")
    fd = os.open(session_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(session_data, f)

    return {"name": name, "tabs": len(tabs), "path": session_path}


async def _handle_session_load(daemon, params):
    """Load a saved session (open tabs and navigate)."""
    name = params.get("name")
    if not name:
        raise ValueError("Missing 'name' parameter")
    import re
    if not re.match(r'^[a-zA-Z0-9_-]+$', name):
        raise ValueError("'name' must be alphanumeric (a-z, 0-9, _, -)")

    session_path = os.path.join(SESSIONS_DIR, f"{name}.json")
    try:
        with open(session_path) as f:
            session_data = json.load(f)
    except FileNotFoundError:
        raise ValueError(f"Session not found: {name}")
    except json.JSONDecodeError:
        raise ValueError(f"Session file corrupted: {name}")

    if not isinstance(session_data, dict):
        raise ValueError("Invalid session format")
    tabs = session_data.get("tabs", [])
    if not isinstance(tabs, list) or not tabs:
        raise ValueError("Session has no tabs")
    for t in tabs:
        if not isinstance(t, dict) or "url" not in t:
            raise ValueError("Invalid tab entry in session")

    session = daemon.pilot._session

    # Navigate current tab to first URL
    timeout = params.get("timeout", 45)
    await daemon.pilot.goto(tabs[0]["url"], timeout=timeout)
    restored = 1

    # Open new tabs for remaining URLs
    for tab in tabs[1:]:
        try:
            await session._xdt(["key", "ctrl+t"])
            await asyncio.sleep(0.5)
            session._viewport_offset = None
            session._console_open = False
            await daemon.pilot.goto(tab["url"], timeout=timeout)
            restored += 1
        except Exception:
            break

    # Return to first tab
    await session._xdt(["key", "ctrl+1"])
    await asyncio.sleep(0.3)

    return {"name": name, "tabs_restored": restored, "tabs_total": len(tabs)}


async def _handle_session_list(daemon, params):
    """List saved sessions."""
    import re
    sessions = []
    if os.path.isdir(SESSIONS_DIR):
        for fname in sorted(os.listdir(SESSIONS_DIR)):
            if not fname.endswith(".json"):
                continue
            sname = fname[:-5]
            if not re.match(r'^[a-zA-Z0-9_-]+$', sname):
                continue
            spath = os.path.join(SESSIONS_DIR, fname)
            try:
                with open(spath) as f:
                    data = json.load(f)
                sessions.append({
                    "name": sname,
                    "tabs": len(data.get("tabs", [])),
                    "timestamp": data.get("timestamp", 0),
                })
            except (json.JSONDecodeError, OSError):
                continue
    return {"sessions": sessions}


async def _handle_session_delete(daemon, params):
    """Delete a saved session."""
    name = params.get("name")
    if not name:
        raise ValueError("Missing 'name' parameter")
    import re
    if not re.match(r'^[a-zA-Z0-9_-]+$', name):
        raise ValueError("'name' must be alphanumeric")

    session_path = os.path.join(SESSIONS_DIR, f"{name}.json")
    if not os.path.isfile(session_path):
        raise ValueError(f"Session not found: {name}")

    os.unlink(session_path)
    return {"deleted": name}


# ── CSS injection ──────────────────────────────────

_custom_css = {}
_css_counter = 0


async def _handle_css_inject(daemon, params):
    """Inject a custom stylesheet."""
    global _custom_css, _css_counter
    css = params.get("css", "")
    if not css or not isinstance(css, str):
        raise ValueError("Missing 'css' parameter (string of CSS rules)")
    css_id = params.get("id", "")
    if not css_id:
        _css_counter += 1
        css_id = f"tbp_{_css_counter}"
    if not isinstance(css_id, str):
        raise ValueError("'id' must be a string")
    import re
    if not re.match(r'^[a-zA-Z0-9_-]+$', css_id):
        raise ValueError("'id' must be alphanumeric (a-z, 0-9, _, -)")
    _custom_css[css_id] = css
    safe_css = json.dumps(css)
    safe_id = json.dumps(f"__tbp_css_{css_id}")
    await daemon.pilot.evaluate(
        f"(function(){{var id={safe_id};"
        "var el=document.getElementById(id);"
        "if(!el){el=document.createElement('style');el.id=id;"
        "document.head.appendChild(el)}"
        f"el.textContent={safe_css};"
        "})()"
    )
    return {"id": css_id, "injected": True}


async def _handle_css_remove(daemon, params):
    """Remove injected stylesheet(s)."""
    global _custom_css
    css_id = params.get("id", "")
    if css_id:
        safe_id = json.dumps(f"__tbp_css_{css_id}")
        await daemon.pilot.evaluate(
            f"(function(){{var el=document.getElementById({safe_id});"
            "if(el)el.remove()})()"
        )
        _custom_css.pop(css_id, None)
        return {"removed": css_id}
    else:
        # Remove all
        for cid in list(_custom_css.keys()):
            safe_id = json.dumps(f"__tbp_css_{cid}")
            await daemon.pilot.evaluate(
                f"(function(){{var el=document.getElementById({safe_id});"
                "if(el)el.remove()})()"
            )
        removed = list(_custom_css.keys())
        _custom_css.clear()
        return {"removed": removed}


async def _handle_css_list(daemon, params):
    """List injected stylesheets."""
    styles = []
    for cid, css in _custom_css.items():
        styles.append({"id": cid, "length": len(css)})
    return {"styles": styles}


async def _inject_css(daemon):
    """Re-inject all custom CSS after navigation."""
    for css_id, css in _custom_css.items():
        safe_css = json.dumps(css)
        safe_id = json.dumps(f"__tbp_css_{css_id}")
        await daemon.pilot.evaluate(
            f"(function(){{var id={safe_id};"
            "var el=document.getElementById(id);"
            "if(!el){el=document.createElement('style');el.id=id;"
            "document.head.appendChild(el)}"
            f"el.textContent={safe_css};"
            "})()"
        )


# ── Wait+Action ──────────────────────────────────

async def _handle_waitact(daemon, params):
    """Wait for element to appear then perform action (Python polling)."""
    selector = params.get("selector")
    if not selector:
        raise ValueError("Missing 'selector' parameter")
    action = params.get("action", "click")
    if action not in ("click", "type", "text"):
        raise ValueError("'action' must be 'click', 'type', or 'text'")
    if action == "type":
        value = params.get("value", "")
        if not isinstance(value, str):
            raise ValueError("'value' must be a string")
    timeout = params.get("timeout", 10)
    if not isinstance(timeout, (int, float)) or timeout < 1 or timeout > 120:
        raise ValueError("'timeout' must be 1-120")

    from ._utils import escape_js_string
    safe_sel = escape_js_string(selector)

    # Poll for element existence (NativeFirefoxSession can't await Promises)
    check_js = f"!!document.querySelector('{safe_sel}')"
    deadline = time.time() + timeout
    found = False
    while time.time() < deadline:
        result = await daemon.pilot.evaluate(check_js)
        if result:
            found = True
            break
        await asyncio.sleep(0.5)

    if not found:
        raise ValueError(f"Timeout waiting for: {selector}")

    # Element found — perform action
    if action == "click":
        await daemon.pilot.evaluate(
            f"(function(){{var el=document.querySelector('{safe_sel}');"
            "if(el){el.scrollIntoView({block:'center'});el.click()}})()"
        )
        return {"clicked": True, "selector": selector}
    elif action == "type":
        safe_val = escape_js_string(params.get("value", ""))
        await daemon.pilot.evaluate(
            f"(function(){{var el=document.querySelector('{safe_sel}');"
            "if(!el)return;"
            "el.scrollIntoView({block:'center'});"
            "var proto=el instanceof HTMLTextAreaElement"
            "?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;"
            "var set=Object.getOwnPropertyDescriptor(proto,'value').set;"
            f"set.call(el,'{safe_val}');"
            "el.dispatchEvent(new Event('input',{bubbles:true}));"
            "el.dispatchEvent(new Event('change',{bubbles:true}))})()"
        )
        return {"typed": True, "selector": selector}
    else:  # text
        text = await daemon.pilot.evaluate(
            f"(function(){{var el=document.querySelector('{safe_sel}');"
            "return el?el.textContent:''})()"
        )
        return {"text": text or "", "selector": selector}


# ── Page event capture ──────────────────────────────────

_event_capture_enabled = False
_event_types = []

_DEFAULT_EVENT_TYPES = ["click", "submit", "input", "change", "keydown"]


async def _handle_events_start(daemon, params):
    """Start capturing DOM events."""
    global _event_capture_enabled, _event_types
    types = params.get("types", _DEFAULT_EVENT_TYPES)
    if not isinstance(types, list):
        raise ValueError("'types' must be a list of event type strings")
    import re
    for t in types:
        if not isinstance(t, str) or not re.match(r'^[a-zA-Z]{1,30}$', t):
            raise ValueError(f"Invalid event type: {t!r} (a-z only, max 30 chars)")
    _event_types = types
    _event_capture_enabled = True
    await _inject_event_capture(daemon)
    return {"enabled": True, "types": types}


async def _handle_events_stop(daemon, params):
    """Stop capturing DOM events and remove listeners."""
    global _event_capture_enabled
    _event_capture_enabled = False
    await daemon.pilot.evaluate(
        "(function(){"
        "if(window.__tbp_evt_patched&&window.__tbp_evt_handlers){"
        "for(var i=0;i<window.__tbp_evt_handlers.length;i++){"
        "var h=window.__tbp_evt_handlers[i];"
        "document.removeEventListener(h.type,h.fn,true)}"
        "delete window.__tbp_evt_patched;"
        "delete window.__tbp_evt_handlers}"
        "})()"
    )
    return {"enabled": False}


async def _handle_events_logs(daemon, params):
    """Get captured events."""
    limit = max(1, min(int(params.get("limit", 100)), 500))
    clear = bool(params.get("clear", False))
    result = await daemon.pilot.evaluate(
        f"(function(){{var r=window.__tbp_events||[];"
        f"var out=r.slice(-{limit});"
        f"if({str(clear).lower()})window.__tbp_events=r.slice(0,r.length-{limit});"
        "return out})()"
    )
    events = result if isinstance(result, list) else []
    return {"events": events, "count": len(events)}


async def _handle_events_clear(daemon, params):
    """Clear captured events."""
    await daemon.pilot.evaluate("window.__tbp_events=[]")
    return {"cleared": True}


async def _inject_event_capture(daemon):
    """Inject DOM event capture listeners."""
    if not _event_types:
        return
    types_json = json.dumps(_event_types)
    js = (
        "(function(){"
        "if(!window.__tbp_events)window.__tbp_events=[];"
        # Remove old handlers before re-injecting
        "if(window.__tbp_evt_patched&&window.__tbp_evt_handlers){"
        "for(var i=0;i<window.__tbp_evt_handlers.length;i++){"
        "var h=window.__tbp_evt_handlers[i];"
        "document.removeEventListener(h.type,h.fn,true)}}"
        "window.__tbp_evt_patched=true;"
        "window.__tbp_evt_handlers=[];"
        f"var types={types_json};"
        "for(var i=0;i<types.length;i++){"
        "(function(t){"
        "var fn=function(e){"
        "var tgt=e.target;"
        "var sel=tgt.tagName?tgt.tagName.toLowerCase():'';"
        "if(tgt.id)sel+='#'+tgt.id;"
        "if(tgt.className&&typeof tgt.className==='string')"
        "sel+='.'+tgt.className.trim().split(/\\s+/).join('.');"
        "var entry={type:t,target:sel,timestamp:Date.now()};"
        "if(t==='input'||t==='change')entry.value=(tgt.value||'').slice(0,200);"
        "if(t==='keydown')entry.key=e.key||'';"
        "if(t==='submit')entry.action=tgt.action||'';"
        "window.__tbp_events.push(entry);"
        "if(window.__tbp_events.length>500)"
        "window.__tbp_events=window.__tbp_events.slice(-500)};"
        "document.addEventListener(t,fn,true);"
        "window.__tbp_evt_handlers.push({type:t,fn:fn})"
        "})(types[i])}"
        "})()"
    )
    await daemon.pilot.evaluate(js)


# ── Viewport/window resize ──────────────────────────────────


async def _get_browser_wid(session):
    """Get the browser's main window ID by PID (safer than getactivewindow).

    Returns the largest window (by geometry) to skip hidden helper windows.
    """
    firefox_proc = getattr(session, '_firefox_proc', None)
    if not firefox_proc or firefox_proc.returncode is not None:
        raise RuntimeError("Browser process not running")
    pid = firefox_proc.pid
    env = {**os.environ, "DISPLAY": session._display}
    proc = await asyncio.create_subprocess_exec(
        "xdotool", "search", "--pid", str(pid),
        env=env, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    wids = stdout.decode().strip().split()
    if not wids:
        raise RuntimeError(f"No window found for browser PID {pid}")
    if len(wids) == 1:
        return wids[0]
    # Pick largest window (main browser, not hidden helpers)
    best_wid, best_area = wids[0], 0
    for wid in wids:
        proc = await asyncio.create_subprocess_exec(
            "xdotool", "getwindowgeometry", wid,
            env=env, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await proc.communicate()
        for line in out.decode().splitlines():
            if "Geometry:" in line:
                parts = line.split(":")[1].strip().split("x")
                if len(parts) == 2:
                    area = int(parts[0]) * int(parts[1])
                    if area > best_area:
                        best_area = area
                        best_wid = wid
    return best_wid


async def _get_all_browser_wids(session):
    """Get all Firefox window IDs with titles and geometry."""
    firefox_proc = getattr(session, '_firefox_proc', None)
    if not firefox_proc or firefox_proc.returncode is not None:
        raise RuntimeError("Browser process not running")
    pid = firefox_proc.pid
    env = {**os.environ, "DISPLAY": session._display}
    proc = await asyncio.create_subprocess_exec(
        "xdotool", "search", "--pid", str(pid),
        env=env, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    wids = stdout.decode().strip().split()
    if not wids:
        return []
    windows = []
    for wid in wids:
        name_proc = await asyncio.create_subprocess_exec(
            "xdotool", "getwindowname", wid,
            env=env, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        name_out, _ = await name_proc.communicate()
        title = name_out.decode().strip()
        geo_proc = await asyncio.create_subprocess_exec(
            "xdotool", "getwindowgeometry", wid,
            env=env, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        geo_out, _ = await geo_proc.communicate()
        width = height = 0
        for line in geo_out.decode().splitlines():
            if "Geometry:" in line:
                parts = line.split(":")[1].strip().split("x")
                if len(parts) == 2:
                    width, height = int(parts[0]), int(parts[1])
        if width < 100 or height < 100:
            continue
        windows.append({
            "wid": wid, "title": title, "width": width, "height": height,
        })
    return windows


async def _handle_window_list(daemon, params):
    """List all browser windows with titles."""
    session = daemon.pilot._session
    windows = await _get_all_browser_wids(session)
    main_wid = daemon._main_wid
    for w in windows:
        w["is_main"] = (w["wid"] == main_wid)
    return {"windows": windows, "count": len(windows)}


async def _handle_window_switch(daemon, params):
    """Switch focus to a specific window by index or WID."""
    session = daemon.pilot._session
    index = params.get("index")
    wid = params.get("wid")
    if wid is None and index is None:
        raise ValueError("Must provide 'index' or 'wid'")
    if wid is None:
        windows = await _get_all_browser_wids(session)
        if not isinstance(index, int) or index < 0 or index >= len(windows):
            raise ValueError(f"'index' must be 0-{len(windows)-1}")
        wid = windows[index]["wid"]
    env = {**os.environ, "DISPLAY": session._display}
    await session._close_console()
    # Use xdotool windowfocus + windowactivate for reliable switching
    await asyncio.create_subprocess_exec(
        "xdotool", "windowfocus", "--sync", wid,
        env=env, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await asyncio.sleep(0.3)
    await asyncio.create_subprocess_exec(
        "xdotool", "windowactivate", "--sync", wid,
        env=env, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await asyncio.sleep(0.5)
    session._console_open = False
    session._console_synced = False
    session._viewport_offset = None
    url = title = ""
    try:
        url = await daemon.pilot.url()
        title = await daemon.pilot.title()
    except Exception:
        pass
    return {"switched_to": wid, "url": url, "title": title}


async def _handle_window_close(daemon, params):
    """Close current window (refuses main unless force=True)."""
    session = daemon.pilot._session
    force = params.get("force", False)
    env = {**os.environ, "DISPLAY": session._display}
    proc = await asyncio.create_subprocess_exec(
        "xdotool", "getactivewindow",
        env=env, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    current_wid = out.decode().strip()
    if current_wid == daemon._main_wid and not force:
        raise ValueError(
            "Cannot close main window. Use force=True or switch windows first."
        )
    await session._close_console()
    await session._xdt(["key", "ctrl+w"])
    await asyncio.sleep(0.5)
    session._console_open = False
    session._console_synced = False
    session._viewport_offset = None
    url = title = ""
    try:
        url = await daemon.pilot.url()
        title = await daemon.pilot.title()
    except Exception:
        pass
    return {"closed": current_wid, "url": url, "title": title}


async def _handle_viewport_set(daemon, params):
    """Set browser window/viewport size."""
    width = params.get("width")
    height = params.get("height")
    if not width or not height:
        raise ValueError("Missing 'width' and 'height' parameters")
    width = int(width)
    height = int(height)
    if width < 100 or width > 7680 or height < 100 or height > 4320:
        raise ValueError("Dimensions must be 100-7680 x 100-4320")

    session = daemon.pilot._session
    wid = await _get_browser_wid(session)
    # Resize by window ID (not active window — avoids targeting wrong window)
    proc = await asyncio.create_subprocess_exec(
        "xdotool", "windowsize", wid, str(width), str(height),
        env={**os.environ, "DISPLAY": session._display},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"xdotool resize failed: {stderr.decode().strip()}")
    await asyncio.sleep(0.3)

    # Get actual viewport from JS
    inner = await daemon.pilot.evaluate(
        "(function(){return{inner_width:window.innerWidth,"
        "inner_height:window.innerHeight}})()"
    )
    result = {"width": width, "height": height}
    if isinstance(inner, dict):
        result["inner_width"] = inner.get("inner_width", 0)
        result["inner_height"] = inner.get("inner_height", 0)
    return result


async def _handle_viewport_get(daemon, params):
    """Get current window/viewport dimensions."""
    session = daemon.pilot._session
    wid = await _get_browser_wid(session)
    # Get window geometry by ID (not active window)
    proc = await asyncio.create_subprocess_exec(
        "xdotool", "getwindowgeometry", wid,
        env={**os.environ, "DISPLAY": session._display},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    # Parse "  Position: X,Y\n  Geometry: WxH\n"
    window_w, window_h = 0, 0
    for line in stdout.decode().splitlines():
        line = line.strip()
        if line.startswith("Geometry:"):
            parts = line.split(":")[1].strip().split("x")
            if len(parts) == 2:
                window_w, window_h = int(parts[0]), int(parts[1])

    inner = await daemon.pilot.evaluate(
        "(function(){return{inner_width:window.innerWidth,"
        "inner_height:window.innerHeight,"
        "outer_width:window.outerWidth,"
        "outer_height:window.outerHeight,"
        "device_pixel_ratio:window.devicePixelRatio||1}})()"
    )
    result = {"window_width": window_w, "window_height": window_h}
    if isinstance(inner, dict):
        result.update(inner)
    return result


# ── Element highlight/outline ──────────────────────────────────

_highlights = {}


async def _handle_highlight(daemon, params):
    """Highlight elements matching a CSS selector with colored outline."""
    selector = params.get("selector")
    if not selector:
        raise ValueError("Missing 'selector' parameter")
    color = params.get("color", "red")
    if not isinstance(color, str) or len(color) > 50:
        raise ValueError("'color' must be a CSS color string (max 50 chars)")
    label = params.get("label", "")
    if not isinstance(label, str) or len(label) > 200:
        raise ValueError("'label' must be a string (max 200 chars)")

    from ._utils import escape_js_string
    safe_sel = escape_js_string(selector)
    safe_color = escape_js_string(color)
    safe_label = escape_js_string(label)

    js = (
        f"(function(){{var els=document.querySelectorAll('{safe_sel}');"
        "var count=els.length;"
        "for(var i=0;i<els.length;i++){var el=els[i];"
        "if(!el.__tbp_orig_outline){"
        "el.__tbp_orig_outline=el.style.outline||'';"
        "el.__tbp_orig_outline_offset=el.style.outlineOffset||'';"
        "el.__tbp_orig_title=el.title||''}"
        f"el.style.outline='3px solid {safe_color}';"
        "el.style.outlineOffset='2px';"
        f"if('{safe_label}')el.title='{safe_label}'"
        "}return count})()"
    )
    count = await daemon.pilot.evaluate(js)
    _highlights[selector] = {"color": color, "label": label}
    return {"selector": selector, "color": color, "count": count or 0}


async def _handle_highlight_clear(daemon, params):
    """Remove highlights from elements."""
    global _highlights
    from ._utils import escape_js_string
    selector = params.get("selector", "")

    if selector:
        safe_sel = escape_js_string(selector)
        await daemon.pilot.evaluate(
            f"(function(){{var els=document.querySelectorAll('{safe_sel}');"
            "for(var i=0;i<els.length;i++){var el=els[i];"
            "if(el.__tbp_orig_outline!==undefined){"
            "el.style.outline=el.__tbp_orig_outline;"
            "el.style.outlineOffset=el.__tbp_orig_outline_offset;"
            "el.title=el.__tbp_orig_title;"
            "delete el.__tbp_orig_outline;"
            "delete el.__tbp_orig_outline_offset;"
            "delete el.__tbp_orig_title}}})()"
        )
        _highlights.pop(selector, None)
        return {"cleared": selector}
    else:
        # Clear all highlights
        for sel in list(_highlights.keys()):
            safe_sel = escape_js_string(sel)
            await daemon.pilot.evaluate(
                f"(function(){{var els=document.querySelectorAll('{safe_sel}');"
                "for(var i=0;i<els.length;i++){var el=els[i];"
                "if(el.__tbp_orig_outline!==undefined){"
                "el.style.outline=el.__tbp_orig_outline;"
                "el.style.outlineOffset=el.__tbp_orig_outline_offset;"
                "el.title=el.__tbp_orig_title;"
                "delete el.__tbp_orig_outline;"
                "delete el.__tbp_orig_outline_offset;"
                "delete el.__tbp_orig_title}}})()"
            )
        cleared = list(_highlights.keys())
        _highlights.clear()
        return {"cleared": cleared}


async def _inject_highlights(daemon):
    """Re-inject highlights after navigation."""
    from ._utils import escape_js_string
    for selector, info in _highlights.items():
        color = info["color"] if isinstance(info, dict) else info
        label = info.get("label", "") if isinstance(info, dict) else ""
        safe_sel = escape_js_string(selector)
        safe_color = escape_js_string(color)
        safe_label = escape_js_string(label)
        await daemon.pilot.evaluate(
            f"(function(){{var els=document.querySelectorAll('{safe_sel}');"
            "for(var i=0;i<els.length;i++){var el=els[i];"
            "if(!el.__tbp_orig_outline){"
            "el.__tbp_orig_outline=el.style.outline||'';"
            "el.__tbp_orig_outline_offset=el.style.outlineOffset||'';"
            "el.__tbp_orig_title=el.title||''}"
            f"el.style.outline='3px solid {safe_color}';"
            "el.style.outlineOffset='2px';"
            f"if('{safe_label}')el.title='{safe_label}'"
            "}})()"
        )


# ── Cookie auto-login (auth sessions) ──────────────────────────────────

AUTH_DIR = os.path.join(TBP_DIR, "auth")


async def _handle_auth_save(daemon, params):
    """Save cookies for current domain as a named auth session."""
    name = params.get("name")
    if not name:
        raise ValueError("Missing 'name' parameter")
    if not isinstance(name, str):
        raise ValueError("'name' must be a string")
    import re
    if not re.match(r'^[a-zA-Z0-9_-]+$', name):
        raise ValueError("'name' must be alphanumeric (a-z, 0-9, _, -)")

    url = await daemon.pilot.url()
    from urllib.parse import urlparse
    parsed = urlparse(url)
    domain = parsed.netloc

    cookies = await daemon.pilot.get_cookies()

    os.makedirs(AUTH_DIR, mode=0o700, exist_ok=True)
    auth_data = {
        "name": name,
        "domain": domain,
        "url": url,
        "cookies": cookies,
        "timestamp": time.time(),
    }
    auth_path = os.path.join(AUTH_DIR, f"{name}.json")
    fd = os.open(auth_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(auth_data, f)

    return {
        "name": name,
        "domain": domain,
        "cookies": len(cookies),
        "path": auth_path,
    }


async def _handle_auth_load(daemon, params):
    """Load a saved auth session (restore cookies and navigate)."""
    name = params.get("name")
    if not name:
        raise ValueError("Missing 'name' parameter")
    import re
    if not re.match(r'^[a-zA-Z0-9_-]+$', name):
        raise ValueError("'name' must be alphanumeric (a-z, 0-9, _, -)")

    auth_path = os.path.join(AUTH_DIR, f"{name}.json")
    try:
        with open(auth_path) as f:
            auth_data = json.load(f)
    except FileNotFoundError:
        raise ValueError(f"Auth session not found: {name}")
    except json.JSONDecodeError:
        raise ValueError(f"Auth session file corrupted: {name}")

    if not isinstance(auth_data, dict):
        raise ValueError("Invalid auth session format")
    cookies = auth_data.get("cookies", [])
    if not isinstance(cookies, list):
        raise ValueError("Invalid cookies in auth session")
    url = auth_data.get("url", "")
    if not url or not isinstance(url, str):
        raise ValueError("Auth session has no URL")

    # Navigate to the saved URL first (cookies need matching domain)
    timeout = params.get("timeout", 45)
    await daemon.pilot.goto(url, timeout=timeout)

    # Load cookies (filter to known keys to avoid TypeError)
    _COOKIE_KEYS = {
        "name", "value", "domain", "path", "secure", "httpOnly",
        "sameSite", "expires", "url",
    }
    loaded = 0
    for cookie in cookies:
        if not isinstance(cookie, dict) or not all(
            k in cookie for k in ("name", "value", "domain")
        ):
            continue
        try:
            filtered = {k: v for k, v in cookie.items() if k in _COOKIE_KEYS}
            await daemon.pilot.set_cookie(**filtered)
            loaded += 1
        except Exception:
            pass

    # Reload to apply cookies
    await daemon.pilot.evaluate("location.reload()")
    await daemon.pilot.wait(2)

    return {
        "name": name,
        "domain": auth_data.get("domain", ""),
        "cookies_loaded": loaded,
        "url": await daemon.pilot.url(),
    }


async def _handle_auth_list(daemon, params):
    """List saved auth sessions."""
    import re
    sessions = []
    if os.path.isdir(AUTH_DIR):
        for fname in sorted(os.listdir(AUTH_DIR)):
            if not fname.endswith(".json"):
                continue
            sname = fname[:-5]
            if not re.match(r'^[a-zA-Z0-9_-]+$', sname):
                continue
            spath = os.path.join(AUTH_DIR, fname)
            try:
                with open(spath) as f:
                    data = json.load(f)
                sessions.append({
                    "name": sname,
                    "domain": data.get("domain", ""),
                    "url": data.get("url", ""),
                    "cookies": len(data.get("cookies", [])),
                    "timestamp": data.get("timestamp", 0),
                })
            except (json.JSONDecodeError, OSError):
                continue
    return {"sessions": sessions}


async def _handle_auth_delete(daemon, params):
    """Delete a saved auth session."""
    name = params.get("name")
    if not name:
        raise ValueError("Missing 'name' parameter")
    import re
    if not re.match(r'^[a-zA-Z0-9_-]+$', name):
        raise ValueError("'name' must be alphanumeric")

    auth_path = os.path.join(AUTH_DIR, f"{name}.json")
    if not os.path.isfile(auth_path):
        raise ValueError(f"Auth session not found: {name}")

    os.unlink(auth_path)
    return {"deleted": name}


# ── Network throttling ──────────────────────────────────

_throttle_config = None

_THROTTLE_PRESETS = {
    "3g": 400,
    "slow-3g": 2000,
    "fast-3g": 150,
    "offline": -1,
}


async def _handle_throttle_set(daemon, params):
    """Set network throttling (adds latency to fetch/XHR requests)."""
    global _throttle_config
    preset = params.get("preset", "")
    latency = params.get("latency", 0)

    if preset:
        if preset not in _THROTTLE_PRESETS:
            raise ValueError(
                f"Unknown preset: {preset}. "
                f"Available: {', '.join(_THROTTLE_PRESETS.keys())}"
            )
        latency = _THROTTLE_PRESETS[preset]
    elif "latency" in params:
        latency = int(latency)
        if latency < 0 or latency > 30000:
            raise ValueError("'latency' must be 0-30000 (ms)")
    else:
        raise ValueError("Provide 'preset' (3g/slow-3g/fast-3g/offline) or 'latency' (ms)")

    _throttle_config = {"preset": preset or "custom", "latency": latency}
    await _inject_throttle(daemon)
    return _throttle_config.copy()


async def _handle_throttle_clear(daemon, params):
    """Remove network throttling."""
    global _throttle_config
    _throttle_config = None
    await daemon.pilot.evaluate(
        "(function(){"
        "if(window.__tbp_throttle_patched){"
        "window.__tbp_throttle_delay=0;"
        "window.__tbp_throttle_offline=false}"
        "})()"
    )
    return {"cleared": True}


async def _handle_throttle_get(daemon, params):
    """Get current throttle configuration."""
    if _throttle_config:
        return _throttle_config.copy()
    return {"preset": "none", "latency": 0}


async def _inject_throttle(daemon):
    """Inject fetch/XHR delay patching for network throttling."""
    if not _throttle_config:
        return
    latency = _throttle_config["latency"]
    offline = latency < 0

    js = (
        "(function(){"
        f"window.__tbp_throttle_delay={abs(latency)};"
        f"window.__tbp_throttle_offline={'true' if offline else 'false'};"
        "if(window.__tbp_throttle_patched)return;"
        "window.__tbp_throttle_patched=true;"
        # Patch fetch
        "var origFetch=window.fetch;"
        "window.fetch=function(){"
        "if(window.__tbp_throttle_offline)"
        "return Promise.reject(new TypeError('Network throttled: offline'));"
        "var args=arguments;var self=this||window;"
        "var d=window.__tbp_throttle_delay||0;"
        "if(d===0)return origFetch.apply(self,args);"
        "return new Promise(function(resolve){"
        "setTimeout(function(){resolve(origFetch.apply(self,args))},d)})};"
        # Patch XHR
        "var origSend=XMLHttpRequest.prototype.send;"
        "XMLHttpRequest.prototype.send=function(){"
        "if(window.__tbp_throttle_offline){"
        "var x=this;setTimeout(function(){"
        "x.dispatchEvent(new Event('error'))},0);return}"
        "var args=arguments;var self=this;"
        "var d=window.__tbp_throttle_delay||0;"
        "if(d===0){origSend.apply(self,args);return}"
        "setTimeout(function(){origSend.apply(self,args)},d)}"
        "})()"
    )
    await daemon.pilot.evaluate(js)


# ── Annotated screenshot ──────────────────────────────────

_ANNOTATE_DEFAULT_SELECTOR = (
    "a,button,[role=button],[role=link],input,select,textarea,"
    "input[type=submit],input[type=button],[onclick],summary,[tabindex]"
)


async def _handle_screenshot_annotate(daemon, params):
    """Take screenshot with numbered badge overlays on interactive elements."""
    from ._utils import validate_path, escape_js_string
    selector = params.get("selector", _ANNOTATE_DEFAULT_SELECTOR)
    safe_sel = escape_js_string(selector)
    path = validate_path(params.get("path", "annotated.png"))
    max_els = params.get("max", 25)
    if not isinstance(max_els, int) or max_els < 1 or max_els > 100:
        max_els = 25
    full = params.get("full", False)

    # Step 1: Inject badges and collect legend
    inject_js = (
        f"(function(){{var els=document.querySelectorAll('{safe_sel}');"
        "var legend=[];var container=document.createElement('div');"
        "container.id='__tbp_annotate_container';"
        "container.style.cssText='position:absolute;top:0;left:0;z-index:99999;pointer-events:none';"
        "document.body.appendChild(container);"
        f"var max={max_els};"
        "for(var i=0;i<els.length&&legend.length<max;i++){var el=els[i];"
        "if(el.offsetParent===null&&el.offsetWidth===0)continue;"
        "var r=el.getBoundingClientRect();"
        "if(r.width===0||r.height===0)continue;"
        "var num=legend.length+1;"
        "var tag=el.tagName.toLowerCase();"
        "var txt=(el.innerText||el.value||el.getAttribute('aria-label')||'').trim().substring(0,60);"
        # Build selector for this element
        "var esc=CSS.escape||function(s){return s.replace(/([\\[\\]()#.:>+~,\"'\\\\=^$!])/g,'\\\\$1')};"
        "var sel='';"
        "if(el.id)sel='#'+esc(el.id);"
        "else{var p=[];var c=el;"
        "while(c&&c!==document.body&&c!==document.documentElement){"
        "var s=c.tagName.toLowerCase();"
        "if(c.id){p.unshift('#'+esc(c.id));break}"
        "var idx=1;var sb=c.previousElementSibling;"
        "while(sb){if(sb.tagName===c.tagName)idx++;sb=sb.previousElementSibling}"
        "var cnt=0;var ch=c.parentElement?c.parentElement.children:[];"
        "for(var j=0;j<ch.length;j++){if(ch[j].tagName===c.tagName)cnt++}"
        "if(cnt>1)s+=':nth-of-type('+idx+')';"
        "p.unshift(s);c=c.parentElement}"
        "sel=p.join(' > ')}"
        "if(!sel)sel=tag;"
        # Create badge
        "var badge=document.createElement('div');"
        "var sx=window.scrollX;var sy=window.scrollY;"
        "badge.style.cssText='position:absolute;left:'+(r.left+sx)+'px;top:'+(r.top+sy)+"
        "'px;width:20px;height:20px;border-radius:50%;background:#e74c3c;color:#fff;"
        "font-size:11px;font-weight:bold;text-align:center;line-height:20px;"
        "font-family:monospace;box-shadow:0 1px 3px rgba(0,0,0,0.5);z-index:99999';"
        "badge.textContent=num;"
        "container.appendChild(badge);"
        "legend.push({num:num,selector:sel,text:txt,tag:tag,"
        "rect:{x:Math.round(r.x),y:Math.round(r.y),"
        "w:Math.round(r.width),h:Math.round(r.height)}})}"
        "return legend})()"
    )
    legend = await daemon.pilot.evaluate(inject_js)
    if not isinstance(legend, list):
        legend = []

    # Step 2: Take screenshot
    try:
        await daemon.pilot.screenshot(path, full_page=full)
    finally:
        # Step 3: Remove badges (always, even if screenshot fails)
        await daemon.pilot.evaluate(
            "(function(){var c=document.getElementById('__tbp_annotate_container');"
            "if(c)c.remove()})()"
        )

    return {"path": path, "elements": len(legend), "legend": legend}


# ── Page audit ──────────────────────────────────

async def _handle_audit(daemon, params):
    """One-command structured page health report."""
    audit_js = (
        "(function(){"
        "var r={};"
        "r.title=document.title||'';"
        "r.url=location.href;"
        "r.lang=document.documentElement.lang||'';"
        "r.doctype=document.doctype?document.doctype.name:'';"
        "r.element_count=document.getElementsByTagName('*').length;"
        # Links
        "var links=document.querySelectorAll('a');"
        "var ext=0;var empty=0;"
        "for(var i=0;i<links.length;i++){"
        "var h=links[i].getAttribute('href');"
        "if(h===null||h==='')empty++;"
        "else if(links[i].hostname&&links[i].hostname!==location.hostname)ext++}"
        "r.links={total:links.length,external:ext,empty_href:empty};"
        # Images
        "var imgs=document.querySelectorAll('img');"
        "var noalt=0;var broken_img=0;"
        "for(var i=0;i<imgs.length;i++){"
        "if(!imgs[i].alt)noalt++;"
        "if(imgs[i].complete&&imgs[i].naturalWidth===0)broken_img++}"
        "r.images={total:imgs.length,missing_alt:noalt,broken:broken_img};"
        # Forms
        "var forms=document.querySelectorAll('form');"
        "var fd=[];"
        "for(var i=0;i<forms.length;i++){"
        "fd.push({action:forms[i].action||'',method:forms[i].method||'get',"
        "inputs:forms[i].querySelectorAll('input,select,textarea').length})}"
        "r.forms={total:forms.length,details:fd.slice(0,10)};"
        # Headings
        "var h1=document.querySelectorAll('h1').length;"
        "var h2=document.querySelectorAll('h2').length;"
        "var h3=document.querySelectorAll('h3').length;"
        "var h4=document.querySelectorAll('h4').length;"
        "var h5=document.querySelectorAll('h5').length;"
        "var h6=document.querySelectorAll('h6').length;"
        "r.headings={h1:h1,h2:h2,h3:h3,h4:h4,h5:h5,h6:h6};"
        # Meta
        "var getMeta=function(n){var m=document.querySelector('meta[name=\"'+n+'\"]')||"
        "document.querySelector('meta[property=\"'+n+'\"]');"
        "return m?m.getAttribute('content'):''};"
        "r.meta={description:getMeta('description'),"
        "viewport:getMeta('viewport'),"
        "charset:(document.characterSet||''),"
        "og_title:getMeta('og:title')};"
        # Scripts & styles
        "var scripts=document.querySelectorAll('script');"
        "var ext_scripts=0;"
        "for(var i=0;i<scripts.length;i++){if(scripts[i].src)ext_scripts++}"
        "r.scripts={total:scripts.length,external:ext_scripts};"
        "var styles=document.querySelectorAll('style');"
        "var style_links=document.querySelectorAll('link[rel=stylesheet]');"
        "r.styles={inline:styles.length,external:style_links.length};"
        # Page size
        "r.page_size=document.documentElement.outerHTML.length;"
        # Load time
        "try{var n=performance.getEntriesByType('navigation')[0];"
        "r.load_time=n?Math.round(n.loadEventEnd-n.startTime):-1}catch(e){r.load_time=-1}"
        # Console errors (if capture enabled)
        "var cl=window.__tbp_console||[];"
        "var errs=0;for(var i=0;i<cl.length;i++){if(cl[i].level==='error')errs++}"
        "r.console_errors=errs;"
        "return r})()"
    )
    result = await daemon.pilot.evaluate(audit_js)
    if not isinstance(result, dict):
        result = {"error": "Audit failed"}
    return result


# ── Response mocking ──────────────────────────────────

_mocks = []


async def _handle_mock_set(daemon, params):
    """Add/replace a response mock for matching URL patterns."""
    pattern = params.get("pattern", "")
    if not pattern or not isinstance(pattern, str):
        raise ValueError("Missing 'pattern' parameter (URL substring to match)")
    body = params.get("body", "")
    if not isinstance(body, str):
        raise ValueError("'body' must be a string")
    try:
        status = int(params.get("status", 200))
    except (ValueError, TypeError):
        raise ValueError("'status' must be a number (100-599)")
    if status < 100 or status > 599:
        raise ValueError("'status' must be 100-599")
    content_type = params.get("content_type", "application/json")
    if not isinstance(content_type, str) or len(content_type) > 100:
        raise ValueError("'content_type' must be a string (max 100 chars)")

    # Replace if pattern exists, else add
    for i, m in enumerate(_mocks):
        if m["pattern"] == pattern:
            _mocks[i] = {
                "pattern": pattern, "body": body,
                "status": status, "content_type": content_type,
            }
            break
    else:
        if len(_mocks) >= 50:
            raise ValueError("Max 50 mocks allowed")
        _mocks.append({
            "pattern": pattern, "body": body,
            "status": status, "content_type": content_type,
        })

    await _inject_mocks(daemon)
    return {"mocks": len(_mocks), "pattern": pattern}


async def _handle_mock_clear(daemon, params):
    """Remove response mock(s)."""
    pattern = params.get("pattern", "")
    if pattern:
        removed = False
        for i, m in enumerate(_mocks):
            if m["pattern"] == pattern:
                _mocks.pop(i)
                removed = True
                break
        if not removed:
            raise ValueError(f"Mock not found: {pattern}")
        # Update JS mocks list
        if _mocks:
            await _inject_mocks(daemon)
        else:
            await daemon.pilot.evaluate(
                "(function(){"
                "if(window.__tbp_mock_patched){"
                "window.__tbp_mock_list=[]}"
                "})()"
            )
        return {"cleared": pattern, "remaining": len(_mocks)}
    else:
        count = len(_mocks)
        _mocks.clear()
        await daemon.pilot.evaluate(
            "(function(){"
            "if(window.__tbp_mock_patched){"
            "window.__tbp_mock_list=[]}"
            "})()"
        )
        return {"cleared": "all", "count": count}


async def _handle_mock_list(daemon, params):
    """List current response mocks."""
    return {"mocks": [m.copy() for m in _mocks]}


async def _inject_mocks(daemon):
    """Inject fetch/XHR mock patching."""
    if not _mocks:
        return
    mocks_json = json.dumps(_mocks)
    js = (
        f"(function(){{window.__tbp_mock_list={mocks_json};"
        "if(window.__tbp_mock_patched)return;"
        "window.__tbp_mock_patched=true;"
        # Patch fetch
        "var origFetch=window.__tbp_mock_orig_fetch||window.fetch;"
        "window.__tbp_mock_orig_fetch=origFetch;"
        "window.fetch=function(u,o){"
        "var url=typeof u==='string'?u:(u&&u.url)||'';"
        "var mocks=window.__tbp_mock_list||[];"
        "for(var i=0;i<mocks.length;i++){"
        "if(url.indexOf(mocks[i].pattern)!==-1){"
        "return Promise.resolve(new Response(mocks[i].body,"
        "{status:mocks[i].status,"
        "headers:{'Content-Type':mocks[i].content_type}}))}}"
        "return origFetch.apply(this||window,arguments)};"
        # Patch XHR
        "var origOpen=window.__tbp_mock_orig_open||XMLHttpRequest.prototype.open;"
        "var origSend=window.__tbp_mock_orig_send||XMLHttpRequest.prototype.send;"
        "window.__tbp_mock_orig_open=origOpen;"
        "window.__tbp_mock_orig_send=origSend;"
        "XMLHttpRequest.prototype.open=function(m,u){"
        "this.__tbp_mock_url=u;"
        "return origOpen.apply(this,arguments)};"
        "XMLHttpRequest.prototype.send=function(){"
        "var url=this.__tbp_mock_url||'';"
        "var mocks=window.__tbp_mock_list||[];"
        "for(var i=0;i<mocks.length;i++){"
        "if(url.indexOf(mocks[i].pattern)!==-1){"
        "var x=this;var mk=mocks[i];"
        "Object.defineProperty(x,'status',{configurable:true,get:function(){return mk.status}});"
        "Object.defineProperty(x,'statusText',{configurable:true,get:function(){return 'OK'}});"
        "Object.defineProperty(x,'responseText',{configurable:true,get:function(){return mk.body}});"
        "Object.defineProperty(x,'response',{configurable:true,get:function(){return mk.body}});"
        "Object.defineProperty(x,'readyState',{configurable:true,get:function(){return 4}});"
        "x.getResponseHeader=function(n){"
        "if(n.toLowerCase()==='content-type')return mk.content_type;return null};"
        "x.getAllResponseHeaders=function(){return 'content-type: '+mk.content_type+'\\r\\n'};"
        "setTimeout(function(){"
        "x.dispatchEvent(new Event('readystatechange'));"
        "x.dispatchEvent(new Event('load'))},0);return}}"
        "return origSend.apply(this,arguments)}"
        "})()"
    )
    await daemon.pilot.evaluate(js)


# ── DOM snapshot & diff ──────────────────────────────────

_snapshots = {}


async def _handle_snapshot_take(daemon, params):
    """Capture current page state as a named snapshot."""
    name = params.get("name")
    if not name or not isinstance(name, str):
        raise ValueError("Missing 'name' parameter")
    import re
    if not re.match(r'^[a-zA-Z0-9_-]+$', name):
        raise ValueError("'name' must be alphanumeric (a-z, 0-9, _, -)")
    if len(_snapshots) >= 50:
        raise ValueError("Max 50 snapshots (delete some first)")

    snap_js = (
        "(function(){"
        "var r={};"
        "r.url=location.href;"
        "r.title=document.title||'';"
        "r.element_count=document.getElementsByTagName('*').length;"
        "var bt=document.body?document.body.innerText:'';"
        "r.text_length=bt.length;"
        # Simple hash: sum of char codes mod large prime
        "var h=0;for(var i=0;i<bt.length;i++){h=((h%2147483647)*31+bt.charCodeAt(i))%2147483647}"
        "r.text_hash=h;"
        "r.visible_text=bt.substring(0,2000);"
        "r.links_count=document.querySelectorAll('a[href]').length;"
        "r.images_count=document.querySelectorAll('img').length;"
        # Form values
        "var forms=document.querySelectorAll('form');"
        "var fd=[];"
        "for(var i=0;i<Math.min(forms.length,10);i++){var f=forms[i];"
        "var vals={};var inputs=f.querySelectorAll('input,select,textarea');"
        "for(var j=0;j<inputs.length;j++){var inp=inputs[j];"
        "var n=inp.name||inp.id||('input_'+j);"
        "vals[n]=(inp.value||'').substring(0,200)}"
        "fd.push({action:f.action||'',method:f.method||'get',values:vals})}"
        "r.forms=fd;"
        "return r})()"
    )
    snap_data = await daemon.pilot.evaluate(snap_js)
    if not isinstance(snap_data, dict):
        raise ValueError("Failed to capture snapshot")

    snap_data["timestamp"] = time.time()
    _snapshots[name] = snap_data

    return {
        "name": name,
        "url": snap_data.get("url", ""),
        "title": snap_data.get("title", ""),
        "element_count": snap_data.get("element_count", 0),
        "text_length": snap_data.get("text_length", 0),
    }


async def _handle_snapshot_diff(daemon, params):
    """Compare two named snapshots and return structured diff."""
    name1 = params.get("name1", "")
    name2 = params.get("name2", "")
    if not name1 or not name2:
        raise ValueError("Missing 'name1' and 'name2' parameters")
    if name1 not in _snapshots:
        raise ValueError(f"Snapshot not found: {name1}")
    if name2 not in _snapshots:
        raise ValueError(f"Snapshot not found: {name2}")

    s1 = _snapshots[name1]
    s2 = _snapshots[name2]

    diff = {
        "name1": name1,
        "name2": name2,
        "url_changed": s1.get("url") != s2.get("url"),
        "title_changed": s1.get("title") != s2.get("title"),
        "element_count_delta": s2.get("element_count", 0) - s1.get("element_count", 0),
        "text_changed": s1.get("text_hash") != s2.get("text_hash"),
        "text_length_delta": s2.get("text_length", 0) - s1.get("text_length", 0),
        "links_count_delta": s2.get("links_count", 0) - s1.get("links_count", 0),
        "images_count_delta": s2.get("images_count", 0) - s1.get("images_count", 0),
    }

    # URL details if changed
    if diff["url_changed"]:
        diff["url_before"] = s1.get("url", "")
        diff["url_after"] = s2.get("url", "")
    if diff["title_changed"]:
        diff["title_before"] = s1.get("title", "")
        diff["title_after"] = s2.get("title", "")

    # Text word diff (simple set diff)
    if diff["text_changed"]:
        words1 = set(s1.get("visible_text", "").split())
        words2 = set(s2.get("visible_text", "").split())
        added = words2 - words1
        removed = words1 - words2
        diff["words_added"] = len(added)
        diff["words_removed"] = len(removed)
        # Sample of changes (first 20 words)
        diff["sample_added"] = sorted(added)[:20]
        diff["sample_removed"] = sorted(removed)[:20]

    # Form value changes
    forms1 = s1.get("forms", [])
    forms2 = s2.get("forms", [])
    diff["forms_count_delta"] = len(forms2) - len(forms1)
    form_changes = []
    for i in range(min(len(forms1), len(forms2))):
        v1 = forms1[i].get("values", {})
        v2 = forms2[i].get("values", {})
        if v1 != v2:
            changed_fields = {}
            all_keys = set(list(v1.keys()) + list(v2.keys()))
            for k in all_keys:
                if v1.get(k) != v2.get(k):
                    changed_fields[k] = {"before": v1.get(k, ""), "after": v2.get(k, "")}
            if changed_fields:
                form_changes.append({"form_index": i, "changes": changed_fields})
    diff["form_changes"] = form_changes

    return diff


async def _handle_snapshot_list(daemon, params):
    """List all in-memory snapshots."""
    snaps = []
    for name, data in _snapshots.items():
        snaps.append({
            "name": name,
            "url": data.get("url", ""),
            "timestamp": data.get("timestamp", 0),
        })
    return {"snapshots": snaps}


async def _handle_snapshot_delete(daemon, params):
    """Delete a named snapshot from memory."""
    name = params.get("name", "")
    if not name:
        raise ValueError("Missing 'name' parameter")
    if name not in _snapshots:
        raise ValueError(f"Snapshot not found: {name}")
    del _snapshots[name]
    return {"deleted": name}


# ── Double-click ──────────────────────────────────

async def _handle_dblclick(daemon, params):
    """Double-click an element."""
    target = params.get("target")
    if not target:
        raise ValueError("Missing 'target' parameter (CSS selector)")
    human = params.get("human", False)
    from ._utils import escape_js_string
    safe = escape_js_string(target)

    if human:
        # Full native event chain with correct detail counts
        js = (
            f"(function(){{var el=document.querySelector('{safe}');"
            f"if(!el)throw new Error('Element not found');"
            f"el.scrollIntoView({{block:'center'}});"
            f"var b={{bubbles:true,cancelable:true,view:window}};"
            f"var o1=Object.assign({{detail:1}},b);"
            f"var o2=Object.assign({{detail:2}},b);"
            f"el.dispatchEvent(new MouseEvent('mousedown',o1));"
            f"el.dispatchEvent(new MouseEvent('mouseup',o1));"
            f"el.dispatchEvent(new MouseEvent('click',o1));"
            f"el.dispatchEvent(new MouseEvent('mousedown',o2));"
            f"el.dispatchEvent(new MouseEvent('mouseup',o2));"
            f"el.dispatchEvent(new MouseEvent('click',o2));"
            f"el.dispatchEvent(new MouseEvent('dblclick',o2))"
            f"}})()"
        )
        await daemon.pilot.evaluate(js)
    else:
        await daemon.pilot.evaluate(
            f"(function(){{var el=document.querySelector('{safe}');"
            "if(!el)throw new Error('Element not found');"
            "el.scrollIntoView({block:'center'});"
            "el.dispatchEvent(new MouseEvent('dblclick',{bubbles:true,cancelable:true}))"
            "})()"
        )
    return {"dblclicked": target, "human": human}


# ── Select dropdown ──────────────────────────────────

async def _handle_select(daemon, params):
    """Select dropdown option by value, label, or index."""
    selector = params.get("selector")
    if not selector:
        raise ValueError("Missing 'selector' parameter")
    from ._utils import escape_js_string
    safe_sel = escape_js_string(selector)

    if "value" in params:
        safe_val = escape_js_string(str(params["value"]))
        js = (
            f"(function(){{var el=document.querySelector('{safe_sel}');"
            "if(!el||el.tagName!=='SELECT')throw new Error('Not a select element');if(el.disabled)throw new Error('Select is disabled');"
            f"var found=false;for(var i=0;i<el.options.length;i++){{"
            f"if(el.options[i].value==='{safe_val}'){{found=true;"
            f"if(el.options[i].disabled)throw new Error('Option is disabled');"
            f"break}}}}"
            f"if(!found)throw new Error('Option not found');"
            "var prev=el.selectedIndex;"
            f"el.value='{safe_val}';"
            "var changed=el.selectedIndex!==prev;"
            "if(changed){"
            "el.dispatchEvent(new Event('input',{bubbles:true}));"
            "el.dispatchEvent(new Event('change',{bubbles:true}))}"
            "return{value:el.value,selectedIndex:el.selectedIndex,"
            "text:el.options[el.selectedIndex]?el.options[el.selectedIndex].text:'',"
            "changed:changed}})()"
        )
    elif "label" in params:
        safe_label = escape_js_string(str(params["label"]))
        js = (
            f"(function(){{var el=document.querySelector('{safe_sel}');"
            "if(!el||el.tagName!=='SELECT')throw new Error('Not a select element');if(el.disabled)throw new Error('Select is disabled');"
            "var prev=el.selectedIndex;var found=false;"
            "for(var i=0;i<el.options.length;i++){"
            f"if(!el.options[i].disabled&&el.options[i].text.trim()==='{safe_label}')"
            "{el.selectedIndex=i;found=true;break}}"
            "if(!found){for(var i=0;i<el.options.length;i++){"
            f"if(!el.options[i].disabled&&el.options[i].text.trim().indexOf('{safe_label}')!==-1)"
            "{el.selectedIndex=i;found=true;break}}}"
            "if(!found)throw new Error('Option not found');"
            "var changed=el.selectedIndex!==prev;"
            "if(changed){"
            "el.dispatchEvent(new Event('input',{bubbles:true}));"
            "el.dispatchEvent(new Event('change',{bubbles:true}))}"
            "return{value:el.value,selectedIndex:el.selectedIndex,"
            "text:el.options[el.selectedIndex].text,changed:changed}})()"
        )
    elif "index" in params:
        idx = int(params["index"])
        js = (
            f"(function(){{var el=document.querySelector('{safe_sel}');"
            "if(!el||el.tagName!=='SELECT')throw new Error('Not a select element');if(el.disabled)throw new Error('Select is disabled');"
            f"if({idx}<0||{idx}>=el.options.length)throw new Error('Index out of range');"
            f"if(el.options[{idx}].disabled)throw new Error('Option is disabled');"
            "var prev=el.selectedIndex;"
            f"el.selectedIndex={idx};"
            "var changed=el.selectedIndex!==prev;"
            "if(changed){"
            "el.dispatchEvent(new Event('input',{bubbles:true}));"
            "el.dispatchEvent(new Event('change',{bubbles:true}))}"
            "return{value:el.value,selectedIndex:el.selectedIndex,"
            "text:el.options[el.selectedIndex].text,changed:changed}})()"
        )
    else:
        raise ValueError("Provide 'value', 'label', or 'index' parameter")

    result = await daemon.pilot.evaluate(js)
    if isinstance(result, dict):
        result["selector"] = selector
        return result
    return {"selector": selector, "selected": True}


# ── Checkbox/radio toggle ──────────────────────────────────

async def _handle_check(daemon, params):
    """Check, uncheck, or toggle a checkbox/radio element."""
    selector = params.get("selector")
    if not selector:
        raise ValueError("Missing 'selector' parameter")
    action = params.get("action", "check")
    if action not in ("check", "uncheck", "toggle"):
        raise ValueError("'action' must be 'check', 'uncheck', or 'toggle'")

    from ._utils import escape_js_string
    safe = escape_js_string(selector)

    # Use el.click() for native behavior — triggers full event chain
    # including click, input, change. Only click if needed.
    if action == "toggle":
        condition_js = "true"  # always click to toggle
    elif action == "check":
        condition_js = "!el.checked"  # click only if unchecked
    else:
        condition_js = "el.checked"  # click only if checked

    js = (
        f"(function(){{var el=document.querySelector('{safe}');"
        f"if(!el)throw new Error('Element not found');"
        f"if(el.disabled)throw new Error('Element is disabled');"
        f"var t=el.type?el.type.toLowerCase():'';"
        f"if(t!=='checkbox'&&t!=='radio')throw new Error('Not a checkbox/radio');"
        f"var prev=el.checked;"
        f"if({condition_js}){{"
        f"if(t==='radio'&&prev){{el.checked=false;"
        f"el.dispatchEvent(new Event('input',{{bubbles:true}}));"
        f"el.dispatchEvent(new Event('change',{{bubbles:true}}))}}"
        f"else{{el.click()}}}}"
        f"return{{checked:el.checked,type:t,changed:el.checked!==prev}}}})()"
    )
    result = await daemon.pilot.evaluate(js)
    if isinstance(result, dict):
        result["selector"] = selector
        result["action"] = action
        return result
    return {"selector": selector, "action": action}


# ── Input value ──────────────────────────────────

async def _handle_input_value(daemon, params):
    """Read current value of an input/select/textarea element."""
    selector = params.get("selector")
    if not selector:
        raise ValueError("Missing 'selector' parameter")
    from ._utils import escape_js_string
    safe = escape_js_string(selector)
    js = (
        f"(function(){{var el=document.querySelector('{safe}');"
        "if(!el)throw new Error('Element not found');"
        "return{value:el.value||'',tag:el.tagName.toLowerCase(),"
        "type:el.type||'',name:el.name||''}})()"
    )
    result = await daemon.pilot.evaluate(js)
    if isinstance(result, dict):
        return result
    return {"value": "", "error": "Failed to read value"}


# ── Element state ──────────────────────────────────

async def _handle_element_state(daemon, params):
    """Query element visibility, enabled, checked, editable state."""
    selector = params.get("selector")
    if not selector:
        raise ValueError("Missing 'selector' parameter")
    from ._utils import escape_js_string
    safe = escape_js_string(selector)
    js = (
        f"(function(){{var el=document.querySelector('{safe}');"
        "if(!el)return{exists:false};"
        "var cs=getComputedStyle(el);"
        "var visible=!!(el.offsetWidth||el.offsetHeight||el.getClientRects().length)"
        "&&cs.visibility!=='hidden'&&cs.display!=='none';"
        "return{exists:true,visible:visible,"
        "enabled:!el.disabled,"
        "checked:!!el.checked,"
        "editable:!el.readOnly&&!el.disabled,"
        "tag:el.tagName.toLowerCase(),"
        "type:el.type||'',"
        "text:(el.innerText||'').trim().substring(0,100)}})()"
    )
    result = await daemon.pilot.evaluate(js)
    if isinstance(result, dict):
        return result
    return {"exists": False}


# ── Bounding box ──────────────────────────────────

async def _handle_bounding_box(daemon, params):
    """Get element position and dimensions."""
    selector = params.get("selector")
    if not selector:
        raise ValueError("Missing 'selector' parameter")
    from ._utils import escape_js_string
    safe = escape_js_string(selector)
    js = (
        f"(function(){{var el=document.querySelector('{safe}');"
        "if(!el)throw new Error('Element not found');"
        "var r=el.getBoundingClientRect();"
        "return{x:Math.round(r.x),y:Math.round(r.y),"
        "width:Math.round(r.width),height:Math.round(r.height),"
        "top:Math.round(r.top),left:Math.round(r.left),"
        "bottom:Math.round(r.bottom),right:Math.round(r.right),"
        "scroll_x:Math.round(window.scrollX),"
        "scroll_y:Math.round(window.scrollY)}})()"
    )
    result = await daemon.pilot.evaluate(js)
    if isinstance(result, dict):
        return result
    raise ValueError("Failed to get bounding box")


# ── Scroll to element ──────────────────────────────────

async def _handle_scroll_to(daemon, params):
    """Scroll an element into view."""
    selector = params.get("selector")
    if not selector:
        raise ValueError("Missing 'selector' parameter")
    block = params.get("block", "center")
    if block not in ("center", "start", "end", "nearest"):
        raise ValueError("'block' must be 'center', 'start', 'end', or 'nearest'")
    from ._utils import escape_js_string
    safe = escape_js_string(selector)
    safe_block = escape_js_string(block)
    js = (
        f"(function(){{var el=document.querySelector('{safe}');"
        "if(!el)throw new Error('Element not found');"
        f"el.scrollIntoView({{block:'{safe_block}',behavior:'smooth'}});"
        "var r=el.getBoundingClientRect();"
        "return{x:Math.round(r.x),y:Math.round(r.y)}})()"
    )
    result = await daemon.pilot.evaluate(js)
    return result if isinstance(result, dict) else {"scrolled": True}


# ── Set content ──────────────────────────────────

async def _handle_set_content(daemon, params):
    """Load raw HTML content without navigation."""
    html = params.get("html", "")
    if not html or not isinstance(html, str):
        raise ValueError("Missing 'html' parameter")
    safe_html = json.dumps(html)
    await daemon.pilot.evaluate(
        f"(function(){{document.open();document.write({safe_html});"
        "document.close()})()"
    )
    await daemon.pilot.wait(0.5)
    # Re-inject monkey-patches wiped by document.open/write/close
    await _reinject_active_state(daemon)
    title = await daemon.pilot.title()
    return {"title": title, "length": len(html)}


# ── Dialog handling (alert/confirm/prompt) ──────────────────────────────────

_dialog_capture_enabled = False
_dialog_config = {"accept": True, "prompt_text": ""}


async def _handle_dialog_handle(daemon, params):
    """Enable dialog handling and configure responses."""
    global _dialog_capture_enabled, _dialog_config
    accept = params.get("accept", True)
    prompt_text = params.get("prompt_text", "")
    if not isinstance(prompt_text, str):
        raise ValueError("'prompt_text' must be a string")
    _dialog_config = {"accept": bool(accept), "prompt_text": prompt_text}
    _dialog_capture_enabled = True
    await _inject_dialog(daemon)
    return {"enabled": True, "accept": _dialog_config["accept"],
            "prompt_text": _dialog_config["prompt_text"]}


async def _handle_dialog_dismiss(daemon, params):
    """Enable dialog handling with dismiss (cancel) responses."""
    global _dialog_capture_enabled, _dialog_config
    _dialog_config = {"accept": False, "prompt_text": ""}
    _dialog_capture_enabled = True
    await _inject_dialog(daemon)
    return {"enabled": True, "accept": False}


async def _handle_dialog_logs(daemon, params):
    """Get captured dialog messages."""
    clear = bool(params.get("clear", False))
    limit = max(1, min(int(params.get("limit", 100)), 500))
    result = await daemon.pilot.evaluate(
        f"(function(){{var r=window.__tbp_dialogs||[];"
        f"var out=r.slice(-{limit});"
        f"if({str(clear).lower()})window.__tbp_dialogs=r.slice(0,r.length-{limit});"
        "return out})()"
    )
    dialogs = result if isinstance(result, list) else []
    return {"dialogs": dialogs, "count": len(dialogs)}


async def _handle_dialog_clear(daemon, params):
    """Clear captured dialog messages."""
    await daemon.pilot.evaluate("window.__tbp_dialogs=[]")
    return {"cleared": True}


async def _inject_dialog(daemon):
    """Inject alert/confirm/prompt monkey-patches."""
    accept_val = "true" if _dialog_config.get("accept", True) else "false"
    prompt_val = json.dumps(_dialog_config.get("prompt_text", ""))
    js = (
        f"(function(){{window.__tbp_dialog_accept={accept_val};"
        f"window.__tbp_dialog_prompt={prompt_val};"
        "if(window.__tbp_dialog_patched)return;"
        "window.__tbp_dialog_patched=true;"
        "if(!window.__tbp_dialogs)window.__tbp_dialogs=[];"
        # Patch alert
        "var origAlert=window.alert;"
        "window.alert=function(msg){"
        "window.__tbp_dialogs.push({type:'alert',message:String(msg||''),"
        "timestamp:Date.now()});"
        "if(window.__tbp_dialogs.length>200)"
        "window.__tbp_dialogs=window.__tbp_dialogs.slice(-100)};"
        # Patch confirm
        "var origConfirm=window.confirm;"
        "window.confirm=function(msg){"
        "window.__tbp_dialogs.push({type:'confirm',message:String(msg||''),"
        "accepted:window.__tbp_dialog_accept,timestamp:Date.now()});"
        "if(window.__tbp_dialogs.length>200)"
        "window.__tbp_dialogs=window.__tbp_dialogs.slice(-100);"
        "return window.__tbp_dialog_accept};"
        # Patch prompt
        "var origPrompt=window.prompt;"
        "window.prompt=function(msg,def){"
        "var val=window.__tbp_dialog_accept?window.__tbp_dialog_prompt:null;"
        "window.__tbp_dialogs.push({type:'prompt',message:String(msg||''),"
        "default_value:String(def||''),response:val,timestamp:Date.now()});"
        "if(window.__tbp_dialogs.length>200)"
        "window.__tbp_dialogs=window.__tbp_dialogs.slice(-100);"
        "return val}"
        "})()"
    )
    await daemon.pilot.evaluate(js)


# ── Wait for response ──────────────────────────────────

async def _handle_waitfor_response(daemon, params):
    """Wait for fetch/XHR response matching URL pattern (Python polling)."""
    pattern = params.get("pattern", "")
    if not pattern or not isinstance(pattern, str):
        raise ValueError("Missing 'pattern' parameter (URL substring)")
    timeout = params.get("timeout", 10)
    if not isinstance(timeout, (int, float)) or timeout < 1 or timeout > 120:
        raise ValueError("'timeout' must be 1-120")

    from ._utils import escape_js_string
    safe_pattern = escape_js_string(pattern)

    # Ensure response capture is active
    if not _response_capture_enabled:
        await _inject_response_capture(daemon)

    # Mark current response count so we only match NEW responses
    mark_js = "(window.__tbp_responses||[]).length"
    start_idx = await daemon.pilot.evaluate(mark_js)
    if not isinstance(start_idx, int):
        start_idx = 0

    # Poll for matching response
    deadline = time.time() + timeout
    while time.time() < deadline:
        check_js = (
            f"(function(){{var r=window.__tbp_responses||[];"
            f"for(var i={start_idx};i<r.length;i++){{"
            f"if(r[i].url&&r[i].url.indexOf('{safe_pattern}')!==-1)"
            "return r[i]}return null})()"
        )
        result = await daemon.pilot.evaluate(check_js)
        if result and isinstance(result, dict):
            return {"matched": True, "response": result}
        await asyncio.sleep(0.5)

    raise ValueError(f"Timeout waiting for response matching: {pattern}")


async def _handle_tab_to(daemon, params):
    """Tab through focusable elements until target text is found.

    Uses JS eval with a short timeout (5s) to check focused element.
    If JS is slow/frozen (common on heavy sites like LinkedIn), falls
    back to xdotool-based approach: tabs a fixed count then activates.
    """
    text = params.get("text", "")
    if not text:
        raise ValueError("Missing 'text' parameter")
    max_tabs = params.get("max_tabs", 30)
    enter = params.get("enter", False)
    space = params.get("space", False)
    text_lower = text.lower()

    js_check = (
        "(function(){"
        "var el=document.activeElement;"
        "if(!el)return '';"
        "return (el.textContent||'').trim().slice(0,200)"
        "+'|'+(el.getAttribute('aria-label')||'')"
        "+'|'+(el.getAttribute('title')||'')"
        "+'|'+(el.getAttribute('value')||'')"
        "+'|'+(el.getAttribute('placeholder')||'')"
        "})()"
    )

    js_failed = False

    for i in range(max_tabs):
        await daemon.pilot.press("Tab")
        await asyncio.sleep(0.15)

        if js_failed:
            # JS is broken on this page — skip eval, just keep tabbing
            continue

        # Try JS eval with short timeout (5s instead of default 60s)
        try:
            info = await asyncio.wait_for(
                daemon.pilot.evaluate(js_check),
                timeout=5,
            )
        except (asyncio.TimeoutError, Exception):
            # JS eval failed — mark as broken and switch to fallback
            js_failed = True
            logger.warning("tab_to: JS eval timed out, switching to xdotool-only mode")
            continue

        if info and text_lower in str(info).lower():
            result = {"found": True, "tabs": i + 1, "element_info": str(info)[:200]}
            if enter:
                await asyncio.sleep(0.1)
                await daemon.pilot.press("Enter")
                result["entered"] = True
            if space:
                await asyncio.sleep(0.1)
                await daemon.pilot.press("Space")
                result["spaced"] = True
            return result

    if js_failed:
        # Fallback: couldn't verify focus via JS. Return info about the
        # attempt so the caller knows what happened.
        raise ValueError(
            f"JS eval unavailable on this page. Tabbed {max_tabs} times "
            f"but could not verify focus on '{text}'. "
            f"Use browser_press with key='Tab' manually + browser_screenshot "
            f"to visually confirm focus, then browser_press key='Enter' to activate."
        )

    raise ValueError(f"Could not find element with text '{text}' after {max_tabs} tabs")


async def _handle_focus(daemon, params):
    """Focus an element via JS and optionally activate it."""
    selector = params.get("selector", "")
    if not selector:
        raise ValueError("Missing 'selector' parameter")
    enter = params.get("enter", False)
    scroll = params.get("scroll", True)
    from ._utils import escape_js_string
    safe_sel = escape_js_string(selector)
    focus_js = (
        "(function(){"
        "function dq(root,sel){"
        "var el=root.querySelector(sel);if(el)return el;"
        "var all=root.querySelectorAll('*');"
        "for(var i=0;i<all.length;i++){"
        "if(all[i].shadowRoot){"
        "var found=dq(all[i].shadowRoot,sel);"
        "if(found)return found}}return null}"
        "var el=dq(document,'" + safe_sel + "');"
        "if(!el)return {error:'Element not found'};"
    )
    if scroll:
        focus_js += "el.scrollIntoView({block:'center',behavior:'instant'});"
    focus_js += (
        "el.focus();"
        "return {"
        "tag:(el.tagName||'').toLowerCase(),"
        "text:(el.textContent||'').trim().slice(0,100),"
        "type:el.type||'',"
        "href:el.href||'',"
        "role:el.getAttribute('role')||''"
        "}})()"
    )
    result = await daemon.pilot.evaluate(focus_js)
    if isinstance(result, dict) and result.get("error"):
        raise ValueError(result["error"])
    if enter:
        await asyncio.sleep(0.15)
        await daemon.pilot.press("Enter")
        if isinstance(result, dict):
            result["entered"] = True
    return result or {"focused": selector}


# ── OTP digit entry ──────────────────────────────────

async def _handle_type_otp(daemon, params):
    """Type OTP/verification digits one-by-one into individual input fields.

    Detects OTP input groups (maxlength=1 inputs) and fills each digit
    with human-like delays. Works in both main page and cross-origin iframe
    contexts (uses xdotool key for Firefox).

    Params:
        digits: string of digits to enter (e.g. "854698")
        selector: optional CSS selector for the OTP container
        method: "auto" (try type+verify+fallback), "click_each" (click per
                field), "type" (Tab between fields). Default "auto".
        delay: seconds between digits (default 0.15)
    """
    digits = params.get("digits", "")
    if not digits or not digits.strip().isdigit():
        raise ValueError("Missing or invalid 'digits' (must be numeric string)")
    digits = digits.strip()
    selector = params.get("selector", "")
    method = params.get("method", "auto")
    delay = params.get("delay", 0.15)

    # Close console so xdotool keystrokes reach page inputs
    session = daemon.pilot._session
    if hasattr(session, '_close_console'):
        await session._close_console()

    # Try to detect OTP input fields via JS (may timeout after page transitions)
    try:
        fields_info = await _otp_detect_fields(daemon, selector)
    except Exception:
        # JS eval timed out — use blind Tab+key method
        return await _otp_blind_method(daemon, digits, delay)

    if not isinstance(fields_info, dict) or fields_info.get("count", 0) == 0:
        # No fields detected — try blind method
        return await _otp_blind_method(daemon, digits, delay)

    fields = fields_info["fields"]
    n_fields = len(fields)
    n_digits = len(digits)

    if n_digits > n_fields:
        raise ValueError(
            f"Too many digits ({n_digits}) for {n_fields} input fields"
        )

    entered = []
    import asyncio as _aio

    if method in ("click_each", "auto"):
        # Close console for reliable keystrokes
        session2 = daemon.pilot._session
        if hasattr(session2, '_close_console'):
            await session2._close_console()
            await _aio.sleep(0.2)

        # Click each field and type the digit
        for i, digit in enumerate(digits):
            f = fields[i]
            if not isinstance(f, dict) or "x" not in f or "y" not in f:
                return {"error": f"Invalid field data at index {i}: {f}"}
            await daemon.pilot.input.click(x=f["x"], y=f["y"])
            await _aio.sleep(0.05)
            await daemon.pilot.press("Backspace")
            await _aio.sleep(0.02)
            await daemon.pilot.press(digit)
            entered.append({"index": i, "digit": digit})
            if i < n_digits - 1:
                await _aio.sleep(delay)

        return {"entered": n_digits, "method": "click_each"}

    # "type" method — Tab between fields
    return await _otp_tab_method(daemon, digits, fields, delay, selector)


async def _otp_detect_fields(daemon, selector):
    """Detect OTP fields via JS eval. May raise on timeout."""
    detect_js = (
        "(function(){"
        "var sel='%s';"
        "var root=sel?document.querySelector(sel):document;"
        "if(!root)root=document;"
        "var inputs=root.querySelectorAll("
        "'input[maxlength=\"1\"],"
        "input[data-index],"
        "input[autocomplete=\"one-time-code\"],"
        "input.otp-input,"
        "input.code-input,"
        "input.pin-input,"
        "input.verification-input'"
        ");"
        "if(!inputs.length){"
        "inputs=root.querySelectorAll('input[type=\"tel\"][maxlength=\"1\"],"
        "input[type=\"number\"][maxlength=\"1\"],"
        "input[type=\"text\"][maxlength=\"1\"]')}"
        "if(!inputs.length){"
        "inputs=root.querySelectorAll('input[aria-label*=\"digit\"],"
        "input[aria-label*=\"code\"],"
        "input[aria-label*=\"Input\"]')}"
        "var r=[];"
        "for(var i=0;i<inputs.length;i++){"
        "var inp=inputs[i];"
        "var rect=inp.getBoundingClientRect();"
        "r.push({i:i,x:rect.x+rect.width/2,y:rect.y+rect.height/2,"
        "w:rect.width,h:rect.height,"
        "id:inp.id||'',name:inp.name||'',"
        "ariaLabel:inp.getAttribute('aria-label')||'',"
        "maxlen:inp.maxLength||0,type:inp.type||'text',"
        "val:inp.value||''})}"
        "return{count:r.length,fields:r}})()"
    ) % selector.replace("'", "\\'")

    return await daemon.pilot.evaluate(detect_js)


async def _otp_tab_method(daemon, digits, fields, delay, selector):
    """Enter OTP digits using Tab to advance between fields."""
    import asyncio as _aio
    # Close console for reliable keystrokes
    session = daemon.pilot._session
    if hasattr(session, '_close_console'):
        await session._close_console()
        await _aio.sleep(0.3)

    if fields:
        # Click first field
        f = fields[0]
        if isinstance(f, dict) and "x" in f and "y" in f:
            await daemon.pilot.input.click(x=f["x"], y=f["y"])
        await _aio.sleep(0.15)

    for i, digit in enumerate(digits):
        await daemon.pilot.press("Backspace")
        await _aio.sleep(0.02)
        # Use press() which maps to xdotool key for each digit
        await daemon.pilot.press(digit)
        if i < len(digits) - 1:
            await _aio.sleep(delay)

    return {"entered": len(digits), "method": "tab"}


async def _otp_blind_method(daemon, digits, delay):
    """Enter OTP digits without JS — Tab from page focus, type via xdotool key.

    Used as fallback when JS eval times out (e.g. after page transitions).
    Relies on browser Tab order reaching OTP inputs.
    """
    import asyncio as _aio
    session = daemon.pilot._session
    if hasattr(session, '_close_console'):
        await session._close_console()
        await _aio.sleep(0.3)

    # Click on page content area to ensure focus (approx center-top)
    await daemon.pilot.input.click(x=700, y=250)
    await _aio.sleep(0.2)

    # Tab to first input
    await daemon.pilot.press("Tab")
    await _aio.sleep(0.2)

    # Type each digit (auto-advances in most OTP implementations)
    for i, digit in enumerate(digits):
        await daemon.pilot.press(digit)
        if i < len(digits) - 1:
            await _aio.sleep(delay)

    return {"entered": len(digits), "method": "blind"}


# ── Challenge / bot detection ──────────────────────────────────

async def _handle_detect_challenge(daemon, params):
    """Detect CAPTCHAs, bot challenges, and security prompts on page.

    Checks for reCAPTCHA, hCaptcha, FunCaptcha, Cloudflare Turnstile,
    DataDome, generic security text, and OTP/2FA prompts.
    Returns {challenged: bool, signals: [{type, confidence, detail}]}.
    """
    detect_js = (
        "(function(){"
        "var s=[];"
        "function a(t,c,d){s.push({type:t,confidence:c,detail:d})}"
        "function q(sel){return !!document.querySelector(sel)}"
        "if(q('.g-recaptcha,#recaptcha'))a('recaptcha','high','widget');"
        "if(q('iframe[src*=\"recaptcha\"]'))a('recaptcha','high','iframe');"
        "if(q('.h-captcha'))a('hcaptcha','high','widget');"
        "if(q('iframe[src*=\"hcaptcha\"]'))a('hcaptcha','high','iframe');"
        "if(q('#funcaptcha'))a('funcaptcha','high','widget');"
        "if(q('iframe[src*=\"arkoselabs\"]'))a('funcaptcha','high','iframe');"
        "if(q('.cf-turnstile'))a('cloudflare','medium','widget');"
        "if(q('iframe[src*=\"challenges.cloudflare\"]'))a('cloudflare','high','iframe');"
        "var t=(document.title||'').toLowerCase();"
        "if(t.indexOf('just a moment')>=0||t.indexOf('checking your browser')>=0)"
        "a('cloudflare','high','title');"
        "if(q('iframe[src*=\"ddc.\"]'))a('datadome','high','iframe');"
        "if(q('.captcha-container,.audio-captcha-play-button'))a('datadome','high','widget');"
        "if(q('iframe[src*=\"perimeterx\"]'))a('perimeterx','high','iframe');"
        "if(q('#px-captcha'))a('perimeterx','high','widget');"
        "var h=document.title.toLowerCase()+' ';"
        "try{var el=document.querySelector('h1,h2,.heading,main');if(el)h+=el.textContent.toLowerCase().slice(0,500)}catch(e){}"
        "var sp=['blocked','access denied','security check','verify','captcha'];"
        "for(var i=0;i<sp.length;i++){if(h.indexOf(sp[i])>=0)a('security_text','medium',sp[i])}"
        "var otp=document.querySelectorAll('input[maxlength=\"1\"]');"
        "if(otp.length>=4)a('otp','high',otp.length+' fields');"
        "var op=['verification code','enter the code','one-time','passcode','two-factor','2fa','sms code'];"
        "for(var j=0;j<op.length;j++){if(h.indexOf(op[j])>=0)a('otp_prompt','medium',op[j])}"
        "return{challenged:s.length>0,signals:s,url:location.href,title:document.title}"
        "})()"
    )

    result = await daemon.pilot.evaluate(detect_js)
    if not isinstance(result, dict):
        return {"challenged": False, "signals": [], "error": "eval failed"}
    return result


_HANDLERS = {
    "goto": _handle_goto,
    "back": _handle_back,
    "forward": _handle_forward,
    "reload": _handle_reload,
    "click": _handle_click,
    "type": _handle_type,
    "press": _handle_press,
    "scroll": _handle_scroll,
    "hover": _handle_hover,
    "mouse_move": _handle_mouse_move,
    "mouse_locate": _handle_mouse_locate,
    "text": _handle_text,
    "html": _handle_html,
    "title": _handle_title,
    "url": _handle_url,
    "links": _handle_links,
    "eval": _handle_eval,
    "screenshot": _handle_screenshot,
    "pdf": _handle_pdf,
    "wait": _handle_wait,
    "waitfor": _handle_waitfor,
    "cookies": _handle_cookies,
    "a11y": _handle_a11y,
    "find": _handle_find,
    "tab_new": _handle_tab_new,
    "tab_close": _handle_tab_close,
    "tab_next": _handle_tab_next,
    "tab_prev": _handle_tab_prev,
    "tab_goto": _handle_tab_goto,
    "block": _handle_block,
    "unblock": _handle_unblock,
    "blocklist": _handle_blocklist,
    "macro": _handle_macro,
    "console_start": _handle_console_start,
    "console_stop": _handle_console_stop,
    "console_logs": _handle_console_logs,
    "console_clear": _handle_console_clear,
    "downloads": _handle_downloads,
    "network_start": _handle_network_start,
    "network_stop": _handle_network_stop,
    "network_logs": _handle_network_logs,
    "network_clear": _handle_network_clear,
    "observe_start": _handle_observe_start,
    "observe_stop": _handle_observe_stop,
    "mutations": _handle_mutations,
    "mutations_clear": _handle_mutations_clear,
    "screenshot_element": _handle_screenshot_element,
    "drag": _handle_drag,
    "swipe": _handle_swipe,
    "iframe_list": _handle_iframe_list,
    "iframe_eval": _handle_iframe_eval,
    "iframe_text": _handle_iframe_text,
    "iframe_click": _handle_iframe_click,
    "upload": _handle_upload,
    "geo_set": _handle_geo_set,
    "geo_clear": _handle_geo_clear,
    "useragent_set": _handle_useragent_set,
    "useragent_clear": _handle_useragent_clear,
    "cookie_set": _handle_cookie_set,
    "storage": _handle_storage,
    "clipboard_read": _handle_clipboard_read,
    "clipboard_write": _handle_clipboard_write,
    "form_fill": _handle_form_fill,
    "headers_set": _handle_headers_set,
    "headers_clear": _handle_headers_clear,
    "headers_list": _handle_headers_list,
    "perf": _handle_perf,
    "attr_get": _handle_attr_get,
    "attr_set": _handle_attr_set,
    "attr_remove": _handle_attr_remove,
    "profile_save": _handle_profile_save,
    "profile_load": _handle_profile_load,
    "profile_list": _handle_profile_list,
    "profile_delete": _handle_profile_delete,
    "search": _handle_search,
    "search_next": _handle_search_next,
    "search_prev": _handle_search_prev,
    "search_clear": _handle_search_clear,
    "shadow_query": _handle_shadow_query,
    "shadow_text": _handle_shadow_text,
    "shadow_click": _handle_shadow_click,
    "responses_start": _handle_responses_start,
    "responses_stop": _handle_responses_stop,
    "responses_logs": _handle_responses_logs,
    "responses_clear": _handle_responses_clear,
    "session_save": _handle_session_save,
    "session_load": _handle_session_load,
    "session_list": _handle_session_list,
    "session_delete": _handle_session_delete,
    "css_inject": _handle_css_inject,
    "css_remove": _handle_css_remove,
    "css_list": _handle_css_list,
    "waitact": _handle_waitact,
    "events_start": _handle_events_start,
    "events_stop": _handle_events_stop,
    "events_logs": _handle_events_logs,
    "events_clear": _handle_events_clear,
    "viewport_set": _handle_viewport_set,
    "viewport_get": _handle_viewport_get,
    "highlight": _handle_highlight,
    "highlight_clear": _handle_highlight_clear,
    "auth_save": _handle_auth_save,
    "auth_load": _handle_auth_load,
    "auth_list": _handle_auth_list,
    "auth_delete": _handle_auth_delete,
    "throttle_set": _handle_throttle_set,
    "throttle_clear": _handle_throttle_clear,
    "throttle_get": _handle_throttle_get,
    "screenshot_annotate": _handle_screenshot_annotate,
    "audit": _handle_audit,
    "mock_set": _handle_mock_set,
    "mock_clear": _handle_mock_clear,
    "mock_list": _handle_mock_list,
    "snapshot_take": _handle_snapshot_take,
    "snapshot_diff": _handle_snapshot_diff,
    "snapshot_list": _handle_snapshot_list,
    "snapshot_delete": _handle_snapshot_delete,
    "dblclick": _handle_dblclick,
    "select": _handle_select,
    "check": _handle_check,
    "input_value": _handle_input_value,
    "element_state": _handle_element_state,
    "bounding_box": _handle_bounding_box,
    "scroll_to": _handle_scroll_to,
    "set_content": _handle_set_content,
    "dialog_handle": _handle_dialog_handle,
    "dialog_dismiss": _handle_dialog_dismiss,
    "dialog_logs": _handle_dialog_logs,
    "dialog_clear": _handle_dialog_clear,
    "waitfor_response": _handle_waitfor_response,
    "status": _handle_status,
    "shutdown": _handle_shutdown,
    "tab_to": _handle_tab_to,
    "focus": _handle_focus,
    "window_list": _handle_window_list,
    "window_switch": _handle_window_switch,
    "window_close": _handle_window_close,
    "type_otp": _handle_type_otp,
    "detect_challenge": _handle_detect_challenge,
}


# ── CLI entry point ──────────────────────────────────

def _daemonize():
    """Fork into background daemon process."""
    # First fork
    pid = os.fork()
    if pid > 0:
        return pid  # Parent returns child PID

    # Child: new session
    os.setsid()
    os.umask(0o077)

    # Second fork (prevent terminal reacquisition)
    pid = os.fork()
    if pid > 0:
        os._exit(0)

    # Redirect stdio
    sys.stdin.close()
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)

    # Log to file
    os.makedirs(TBP_DIR, exist_ok=True)
    log_fd = os.open(LOG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.dup2(log_fd, 1)
    os.dup2(log_fd, 2)
    os.close(devnull)
    os.close(log_fd)

    return 0  # We are the daemon


def main():
    """Entry point for python -m src.daemon."""
    import argparse
    parser = argparse.ArgumentParser(description="TBP daemon")
    parser.add_argument("action", choices=["start", "stop", "status", "run"])
    parser.add_argument("--browser", "-b", default="firefox")
    parser.add_argument("--session", "-s", default=None)
    parser.add_argument("--foreground", "-f", action="store_true",
                        help="Run in foreground (don't daemonize)")
    parser.add_argument("--idle-timeout", type=int, default=None,
                        help="Auto-shutdown after N seconds idle (0=disabled)")
    parser.add_argument("--proxy", default=None,
                        help="Proxy URL (http://host:port or socks5://host:port)")
    args = parser.parse_args()

    if args.action == "run":
        # Run in foreground (used internally)
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(message)s")
        daemon = Daemon(browser=args.browser, session_file=args.session,
                        idle_timeout=args.idle_timeout, proxy=args.proxy)
        asyncio.run(daemon.run())

    elif args.action == "start":
        # Check if already running
        from ._utils import read_pid_file
        pid = read_pid_file(PID_PATH)
        if pid:
            try:
                os.kill(pid, 0)
                print(f"Daemon already running (PID {pid})")
                return
            except (ProcessLookupError, PermissionError):
                # Stale PID, clean up
                for p in (SOCKET_PATH, PID_PATH):
                    try:
                        os.unlink(p)
                    except FileNotFoundError:
                        pass

        if args.foreground:
            logging.basicConfig(level=logging.INFO,
                                format="%(asctime)s %(levelname)s %(message)s")
            daemon = Daemon(browser=args.browser, session_file=args.session,
                            idle_timeout=args.idle_timeout, proxy=args.proxy)
            asyncio.run(daemon.run())
        else:
            child_pid = _daemonize()
            if child_pid > 0:
                # Parent: wait for socket
                print(f"Starting daemon (PID {child_pid})...")
                for _ in range(60):
                    if os.path.exists(SOCKET_PATH):
                        print(f"Daemon ready. Browser: {args.browser}")
                        return
                    time.sleep(0.5)
                print("Warning: daemon may still be starting (socket not found yet)")
            else:
                # Daemon process
                logging.basicConfig(
                    level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.StreamHandler()],
                )
                daemon = Daemon(browser=args.browser, session_file=args.session,
                                idle_timeout=args.idle_timeout, proxy=args.proxy)
                asyncio.run(daemon.run())

    elif args.action == "stop":
        from ._utils import read_pid_file
        pid = read_pid_file(PID_PATH)
        if not pid:
            print("No daemon running")
            return
        try:
            os.kill(pid, signal.SIGTERM)
            print(f"Sent SIGTERM to daemon (PID {pid})")
            for _ in range(20):
                try:
                    os.kill(pid, 0)
                    time.sleep(0.5)
                except ProcessLookupError:
                    print("Daemon stopped")
                    return
            print("Warning: daemon may still be shutting down")
        except ProcessLookupError:
            print("Daemon already stopped (stale PID)")
            for p in (SOCKET_PATH, PID_PATH):
                try:
                    os.unlink(p)
                except FileNotFoundError:
                    pass

    elif args.action == "status":
        from ._utils import read_pid_file
        pid = read_pid_file(PID_PATH)
        if not pid:
            print("No daemon running")
            return
        try:
            os.kill(pid, 0)
            print(f"Daemon running (PID {pid})")
            print(f"Socket: {SOCKET_PATH}")
        except (ProcessLookupError, PermissionError):
            print("Daemon not running (stale PID file)")


if __name__ == "__main__":
    main()
