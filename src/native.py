"""Native Firefox session — no automation framework.

Firefox passes Cloudflare challenges with zero stealth when started
as a regular browser. geckodriver/Marionette sets internal flags that
CF detects. This module controls Firefox entirely through xdotool
(native X11 input events) and a local callback HTTP server for JS
execution results. The same send()/on() API is exposed so command
modules work unchanged.
"""

import asyncio
import base64
import json
import logging
import os
import re
import time
import shutil
import tempfile
import threading
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler

logger = logging.getLogger(__name__)

FIREFOX_BIN = shutil.which("firefox") or "firefox"

# Default toolbar height for Firefox in Xvfb (updated on first JS call)
_DEFAULT_TOOLBAR_HEIGHT = 74


class _CallbackHandler(BaseHTTPRequestHandler):
    """HTTP handler that receives JS execution results."""

    _MAX_BODY = 10 * 1024 * 1024  # 10MB limit to prevent OOM

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (ValueError, TypeError):
            self.send_error(400, "Invalid Content-Length")
            return
        if length > self._MAX_BODY:
            self.send_error(413, "Payload too large")
            return
        # Validate secret token to prevent local SSRF
        path_parts = self.path.lstrip("/").split("/", 1)
        if len(path_parts) != 2:
            self.send_error(403, "Forbidden")
            return
        token, req_id = path_parts
        if not hasattr(self.server, "secret") or token != self.server.secret:
            self.send_error(403, "Forbidden")
            return
        body = self.rfile.read(length) if length else b""
        if hasattr(self.server, "results_lock"):
            with self.server.results_lock:
                # Cap stored results to prevent unbounded memory growth
                if len(self.server.results) > 100:
                    # Evict oldest entries
                    excess = len(self.server.results) - 50
                    for key in list(self.server.results)[:excess]:
                        del self.server.results[key]
                self.server.results[req_id] = body.decode("utf-8", errors="replace")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        origin = self.headers.get("Origin", "")
        if origin in ("null", "http://127.0.0.1", "http://localhost"):
            self.send_header("Access-Control-Allow-Origin", origin)
        else:
            self.send_header("Access-Control-Allow-Origin", "null")
        self.end_headers()
        self.wfile.write(b"ok")

    def do_OPTIONS(self):
        self.send_response(200)
        origin = self.headers.get("Origin", "")
        if origin in ("null", "http://127.0.0.1", "http://localhost"):
            self.send_header("Access-Control-Allow-Origin", origin)
        else:
            self.send_header("Access-Control-Allow-Origin", "null")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, fmt, *args):
        pass  # Suppress HTTP logs


class NativeFirefoxSession:
    """Control Firefox via xdotool + local HTTP callback.

    No geckodriver, no Marionette, no WebDriver, no BiDi.
    CF cannot detect any automation framework.
    """

    def __init__(self, display=":99", window_size="1920,1080",
                 user_data_dir=None, proxy=None):
        self._display = display
        self._window_size = window_size
        self._user_data_dir = user_data_dir
        self._proxy = proxy
        self._firefox_proc = None
        self._callback_server = None
        self._callback_port = None
        self._callback_thread = None
        self._results_lock = threading.Lock()
        self._event_handlers = {}
        self._disconnected = False
        self._console_open = False
        self._console_synced = False  # True after first exec syncs console state
        self._page_has_focus = False  # True after mouse click on page
        self._devtools_wid = None  # DevTools window ID (separate window mode)
        self._main_wid = None  # Main browser window ID
        self._viewport_offset = None
        self._viewport_offset_cache = None
        self._viewport_offset_cache_time = 0.0
        self._last_good_viewport_offset = None  # Last measured offset with console open (survives navigation)
        self._last_good_viewport_offset_closed = None  # Last measured offset with console closed
        self._js_lock = asyncio.Lock()  # Serialize JS execution (clipboard is global)
        self._js_available = True  # False when JS exec fails (CSP pages); reset on navigation

    def _cleanup_profile_locks(self):
        """Remove stale Firefox profile locks from previous crashed sessions."""
        import glob
        import subprocess
        # Only clean locks if Firefox is not currently running
        for name in ("firefox", "firefox-esr"):
            try:
                result = subprocess.run(
                    ["pgrep", "-x", name],
                    capture_output=True, text=True, timeout=3
                )
                if result.returncode == 0 and result.stdout.strip():
                    logger.debug("Firefox running (%s PID %s), skipping lock cleanup",
                                 name, result.stdout.strip().split()[0])
                    return
            except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
                pass
        profile_dir = self._user_data_dir
        if not profile_dir:
            # Find default profile directory
            moz_dir = os.path.expanduser("~/.mozilla/firefox")
            for pattern in ("*.default-default", "*.default"):
                matches = glob.glob(os.path.join(moz_dir, pattern))
                if matches:
                    profile_dir = matches[0]
                    break
        if profile_dir and os.path.isdir(profile_dir):
            for lock_name in ("lock", ".parentlock"):
                lock_path = os.path.join(profile_dir, lock_name)
                try:
                    os.remove(lock_path)
                    logger.debug("Removed stale lock: %s", lock_path)
                except (FileNotFoundError, OSError):
                    pass
            # Remove session restore files to prevent about:sessionrestore
            for pattern in ("sessionstore.jsonlz4",
                            "sessionstore-backups/*.jsonlz4"):
                for f in glob.glob(os.path.join(profile_dir, pattern)):
                    try:
                        os.remove(f)
                    except OSError:
                        pass
            # Disable session restore to avoid "about:sessionrestore" tab
            user_js = os.path.join(profile_dir, "user.js")
            prefs = {
                "browser.sessionstore.resume_from_crash": "false",
                "browser.startup.homepage_override.mstone": '"ignore"',
                "browser.tabs.warnOnClose": "false",
                "browser.shell.checkDefaultBrowser": "false",
                "datareporting.policy.dataSubmissionEnabled": "false",
                "toolkit.telemetry.reportingpolicy.firstRun": "false",
                # Allow paste in Web Console (bypasses "allow pasting" prompt)
                "devtools.selfxss.count": "100",
                # Disable console autocomplete (avoids corruption when typing JS)
                "devtools.editor.autoclosebrackets": "false",
                "devtools.webconsole.input.autocomplete": "false",
                # DevTools in separate window — prevents viewport resize and
                # focus stealing when console opens/closes (fixes dropdown bugs)
                "devtools.toolbox.host": '"window"',
                # Auto-download (no save-as dialog)
                "browser.download.folderList": "2",
                "browser.download.useDownloadDir": "true",
                # Allow popups (needed for OAuth flows like Google/Facebook/Apple)
                "dom.disable_open_during_load": "false",
                "dom.popup_allowed_events": (
                    '"change click dblclick auxclick mousedown mouseup '
                    'pointerdown pointerup notificationclick reset submit '
                    'touchend contextmenu"'
                ),
                "privacy.popups.disable_from_plugins": "0",
                "dom.popup_maximum": "100",
            }
            # Set download directory
            import json as _json
            dl_dir = os.path.join(os.path.expanduser("~/.tbp"), "downloads")
            os.makedirs(dl_dir, mode=0o700, exist_ok=True)
            prefs["browser.download.dir"] = _json.dumps(dl_dir)
            prefs["browser.helperApps.neverAsk.saveToDisk"] = (
                '"application/octet-stream,application/pdf,application/zip,'
                'application/gzip,text/csv,text/plain,image/png,image/jpeg,'
                'application/json,application/xml"'
            )
            # Add proxy prefs if configured
            if self._proxy:
                from urllib.parse import urlparse as _urlparse
                pp = _urlparse(self._proxy)
                proxy_host = pp.hostname or "127.0.0.1"
                proxy_port = str(pp.port or 1080)
                # Sanitize hostname to prevent user.js pref injection
                safe_host = _json.dumps(proxy_host)  # JSON-quoted string
                if pp.scheme in ("socks5", "socks", "socks5h"):
                    prefs["network.proxy.type"] = "1"
                    prefs["network.proxy.socks"] = safe_host
                    prefs["network.proxy.socks_port"] = proxy_port
                    prefs["network.proxy.socks_version"] = "5"
                    prefs["network.proxy.socks_remote_dns"] = "true"
                elif pp.scheme in ("http", "https"):
                    prefs["network.proxy.type"] = "1"
                    prefs["network.proxy.http"] = safe_host
                    prefs["network.proxy.http_port"] = proxy_port
                    prefs["network.proxy.ssl"] = safe_host
                    prefs["network.proxy.ssl_port"] = proxy_port
            try:
                existing = ""
                if os.path.exists(user_js):
                    with open(user_js) as f:
                        existing = f.read()
                with open(user_js, "a") as f:
                    for key, val in prefs.items():
                        line = f'user_pref("{key}", {val});'
                        if line not in existing:
                            f.write(line + "\n")
            except OSError as e:
                logger.debug("Could not write user.js: %s", e)

    async def connect(self):
        """Start Firefox and callback server."""
        from ._utils import require_binaries
        require_binaries("firefox", "xdotool", "xclip", "import")

        # Bind HTTPServer directly to port 0 (OS assigns free port).
        # Avoids TOCTOU race of finding port then re-binding.
        self._callback_secret = uuid.uuid4().hex
        self._callback_server = HTTPServer(
            ('127.0.0.1', 0), _CallbackHandler)
        self._callback_port = self._callback_server.server_address[1]
        self._callback_server.results = {}
        self._callback_server.results_lock = self._results_lock
        self._callback_server.secret = self._callback_secret
        self._callback_thread = threading.Thread(
            target=self._callback_server.serve_forever, daemon=True)
        self._callback_thread.start()

        # Clean stale locks and disable session restore
        self._cleanup_profile_locks()

        # Start Firefox
        env = os.environ.copy()
        env["DISPLAY"] = self._display
        env["LIBGL_ALWAYS_SOFTWARE"] = "1"
        env["MOZ_CRASHREPORTER_DISABLE"] = "1"

        w, h = self._window_size.split(",")
        args = [
            FIREFOX_BIN,
            "--no-remote",
            f"--width={w}",
            f"--height={h}",
            "about:blank",
        ]

        if self._user_data_dir:
            args.extend(["-profile", self._user_data_dir])

        self._firefox_proc = await asyncio.create_subprocess_exec(
            *args, env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.sleep(6)

        if self._firefox_proc.returncode is not None:
            raise RuntimeError("Firefox failed to start")

        # Discover the main browser window ID for window management
        await self._find_main_window()

        logger.info("Native Firefox started (pid %d), callback port %d, main_wid=%s",
                     self._firefox_proc.pid, self._callback_port, self._main_wid)
        return self

    async def _xdt(self, args, timeout=10):
        """Execute xdotool command with explicit DISPLAY and timeout."""
        env = os.environ.copy()
        env["DISPLAY"] = self._display
        proc = await asyncio.create_subprocess_exec(
            "xdotool", *args, env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            logger.warning("xdotool timed out after %ds: %s", timeout, args)
            return ""
        return out.decode().strip()

    @staticmethod
    def _safe_join_lines(lines):
        """Join multi-line JS into one line, inserting semicolons where needed.

        Simple space-join breaks code that relies on ASI (Automatic Semicolon
        Insertion). This inserts explicit semicolons between statements while
        preserving continuations (operators, braces, etc.).
        """
        cont_end = re.compile(r'[{(\[,;+\-*/=<>&|?:!~^%]$')
        cont_start = re.compile(r'^\s*(else|catch|finally|=>|[.?])\b')
        # Control flow with braceless body: if/for/while/else if ending with )
        ctrl_flow = re.compile(r'^\s*(?:if|for|while|else\s+if)\s*\(.*\)\s*$')
        parts = []
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith('//'):
                continue
            parts.append(stripped)
        if not parts:
            return ""
        out = [parts[0]]
        for i in range(1, len(parts)):
            prev, cur = parts[i - 1], parts[i]
            if ctrl_flow.match(prev):
                out.append(' ')
            elif cont_end.search(prev) or cont_start.match(cur):
                out.append(' ')
            elif prev.endswith((';', '{', '}')):
                out.append(' ')
            else:
                out.append('; ')
            out.append(cur)
        return ''.join(out)

    async def _exec_js(self, expression, timeout=60):
        """Execute JavaScript via browser console and read result.

        Uses copy() console helper (CSP-proof, no fetch needed):
        1. Wrap expression in try/catch + copy() with unique marker
        2. Paste JS into console via clipboard (reliable for any length)
        3. Poll clipboard for marker prefix to read result

        copy() is a Firefox DevTools console helper — always available
        regardless of page CSP restrictions. Serialized via lock since
        clipboard is global X11 state.
        """
        async with self._js_lock:
            return await self._exec_js_inner(expression, timeout)

    async def _exec_js_inner(self, expression, timeout=60):
        """Inner JS execution (must hold _js_lock).

        Strategy: eval() first, inline fallback.

        SyntaxError is a PARSE-TIME error — if the expression has invalid
        syntax when embedded inline as ({expr}), the entire code block
        (including try/catch/copy) is rejected by the parser. The copy()
        never runs, clipboard stays empty, and we time out silently.

        Fix: use eval(STRING) as primary method — always parseable because
        the expression is a string, not inline code. Fall back to inline
        ({expr}) only if eval is blocked (CSP-restricted sites).
        """
        lines = expression.strip().splitlines()
        expr = lines[0].strip() if len(lines) == 1 else self._safe_join_lines(lines)

        # Primary: eval() wrapper — always parseable, handles multi-statement code
        marker1 = f"TBP{uuid.uuid4().hex}"
        escaped_expr = json.dumps(expr)
        js_eval = (
            f"var _r;try{{_r=JSON.stringify({{r:eval({escaped_expr})}})}}catch(_e){{_r=JSON.stringify({{r:'ERR:'+_e.message}})}}"
            f"copy('{marker1}'+_r)"
        )
        result = await self._run_js_and_read(js_eval, marker1, timeout)

        # If eval is blocked by CSP, retry with inline ({expr}) for simple expressions
        if isinstance(result, str) and result.startswith("ERR:") and (
            "Content Security Policy" in result or "EvalError" in result
            or "call to eval()" in result
        ):
            marker2 = f"TBP{uuid.uuid4().hex}"
            js_inline = (
                f"var _r;try{{_r=JSON.stringify({{r:({expr})}})}}catch(_e){{_r=JSON.stringify({{r:'ERR:'+_e.message}})}}"
                f"copy('{marker2}'+_r)"
            )
            result = await self._run_js_and_read(js_inline, marker2, timeout)

        # Track JS availability for this page
        if isinstance(result, str) and result.startswith("ERR:Timeout"):
            self._js_available = False
        elif result is not None:
            self._js_available = True

        return result

    async def _run_js_and_read(self, js, marker, timeout=60):
        """Paste JS into console, execute, read result.

        Console is left open for efficiency (avoids toggle churn when
        multiple JS calls happen in sequence).  Call _close_console()
        before screenshots or page keyboard input.
        """
        req_id = marker[3:]  # Strip "TBP" prefix to get uuid

        # On first exec after daemon start, detect actual console state.
        # Console may be open from a previous daemon session (toggle-based
        # shortcuts make it impossible to force a state without detection).
        if not self._console_synced:
            await self._sync_console_state()

        # Open console if not already open (Ctrl+Shift+K toggle)
        if not self._console_open:
            # Focus main window first so Ctrl+Shift+K opens from browser
            await self._focus_main_window()
            await self._xdt(["key", "ctrl+shift+k"])
            await asyncio.sleep(1.0)
            self._console_open = True
            # In separate-window mode, find and track the DevTools window
            if not self._devtools_wid:
                await self._find_devtools_window()
            self._viewport_offset_cache = None
        elif self._page_has_focus:
            # Console is open but page has focus (after mouse click).
            # Focus main window first so console targets the right context.
            await self._focus_main_window()
            if self._devtools_wid:
                await self._ensure_devtools_focused()
            else:
                # Legacy docked mode: toggle console off then on to refocus
                await self._xdt(["key", "ctrl+shift+k"])
                await asyncio.sleep(0.3)
                await self._xdt(["key", "ctrl+shift+k"])
                await asyncio.sleep(0.5)
            self._page_has_focus = False
        elif self._devtools_wid:
            # Console open in separate window — ensure it's focused
            await self._ensure_devtools_focused()

        # Clear existing text and paste
        await self._xdt(["key", "ctrl+a"])
        await asyncio.sleep(0.1)
        pasted = await self._clipboard_paste(js)
        if not pasted:
            await self._xdt(["type", "--clearmodifiers", "--delay", "1", js])
        await asyncio.sleep(0.2)
        await self._xdt(["key", "ctrl+Return"])
        await asyncio.sleep(0.5)

        # Poll clipboard for result
        result = None
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            with self._results_lock:
                if req_id in self._callback_server.results:
                    raw = self._callback_server.results.pop(req_id)
                    try:
                        data = json.loads(raw)
                        result = data.get("r")
                    except (json.JSONDecodeError, ValueError):
                        result = raw
                    break

            clip = await self._clipboard_read()
            if clip.startswith(marker):
                raw = clip[len(marker):]
                try:
                    data = json.loads(raw)
                    result = data.get("r")
                except (json.JSONDecodeError, ValueError):
                    result = raw
                break

            await asyncio.sleep(0.3)

        if result is None and asyncio.get_running_loop().time() >= deadline:
            clip = await self._clipboard_read()
            clip_preview = repr(clip[:80]) if clip else "empty"
            logger.warning(
                "JS execution timed out (marker=%s, js_len=%d, clipboard=%s)",
                marker[:12], len(js), clip_preview,
            )
            return f"ERR:Timeout - JS execution did not return within {timeout}s"
        return result

    async def _clipboard_read(self):
        """Read text from X11 clipboard via xclip."""
        env = os.environ.copy()
        env["DISPLAY"] = self._display
        proc = await asyncio.create_subprocess_exec(
            "xclip", "-selection", "clipboard", "-o",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
        out, _ = await proc.communicate()
        return out.decode("utf-8", errors="replace").strip()

    async def _clipboard_paste(self, text):
        """Write text to clipboard and paste into focused field.

        Uses xclip without -loops (stays as clipboard owner) so that
        intermediate clipboard reads (from WM or DevTools) don't consume
        the content before Ctrl+V can paste it. Killed after paste.
        """
        env = os.environ.copy()
        env["DISPLAY"] = self._display
        try:
            proc = await asyncio.create_subprocess_exec(
                "xclip", "-selection", "clipboard",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
            )
            proc.stdin.write(text.encode("utf-8"))
            proc.stdin.close()
            await asyncio.sleep(0.3)

            await self._xdt(["key", "ctrl+v"])
            await asyncio.sleep(0.5)

            # Kill xclip now that paste is done (release clipboard ownership)
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=2)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
            return True
        except Exception as e:
            logger.debug("Clipboard paste failed: %s", e)
            return False

    async def _dismiss_popup(self):
        """Dismiss Firefox chrome popups (save password, save address, etc.).

        Sends Escape key which closes any active popup without affecting
        page state. Safe to call even when no popup is present.
        """
        await self._xdt(["key", "Escape"])
        await asyncio.sleep(0.15)

    async def _close_console(self):
        """Hide DevTools and refocus the main browser window.

        In separate-window mode: minimizes DevTools window (no viewport change).
        Console stays logically "open" — just not visible.
        Fallback: Ctrl+Shift+I toggle (legacy docked mode).
        """
        if not self._console_open:
            return
        if self._devtools_wid:
            # Separate window mode — minimize + focus main (no viewport change)
            await self._hide_devtools()
            # Console is still open in the minimized window — don't set False
            # so _run_js_and_read can reuse it without reopening
        else:
            # Legacy docked mode fallback
            await self._xdt(["key", "ctrl+shift+i"])
            await asyncio.sleep(0.8)
            self._console_open = False
            self._viewport_offset_cache = None
        await self._focus_main_window()

    async def _focus_main_window(self):
        """Focus the main browser window (not DevTools)."""
        wid = self._main_wid
        if not wid:
            # Try to discover it now if not yet known
            await self._find_main_window()
            wid = self._main_wid
        if not wid:
            return
        try:
            await self._xdt(["windowactivate", "--sync", wid])
            await asyncio.sleep(0.1)
        except Exception:
            pass

    async def _find_main_window(self):
        """Find the main Firefox browser window ID.

        Searches for the Firefox window by PID or name. Called once after
        Firefox starts to establish _main_wid for window management.
        """
        env = {**os.environ, "DISPLAY": self._display}
        # Try by PID first (most reliable)
        if self._firefox_proc and self._firefox_proc.pid:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "xdotool", "search", "--pid",
                    str(self._firefox_proc.pid), "--name", "",
                    env=env, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                out, _ = await proc.communicate()
                for wid in out.decode().strip().split("\n"):
                    wid = wid.strip()
                    if wid:
                        self._main_wid = wid
                        logger.debug("Found main window by PID: %s", wid)
                        return wid
            except Exception:
                pass
        # Fallback: search by Firefox window name
        try:
            proc = await asyncio.create_subprocess_exec(
                "xdotool", "search", "--name", "Mozilla Firefox",
                env=env, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await proc.communicate()
            for wid in out.decode().strip().split("\n"):
                wid = wid.strip()
                if wid:
                    self._main_wid = wid
                    logger.debug("Found main window by name: %s", wid)
                    return wid
        except Exception:
            pass
        logger.warning("Could not find main Firefox window ID")
        return None

    async def _find_devtools_window(self):
        """Find the DevTools window ID (separate window mode).

        Searches for windows with 'Developer Tools' in the title that
        aren't the main browser window.
        """
        env = {**os.environ, "DISPLAY": self._display}
        try:
            proc = await asyncio.create_subprocess_exec(
                "xdotool", "search", "--name", "Developer Tools",
                env=env, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await proc.communicate()
            for wid in out.decode().strip().split("\n"):
                wid = wid.strip()
                if wid and wid != self._main_wid:
                    self._devtools_wid = wid
                    logger.debug("Found DevTools window: %s", wid)
                    return wid
        except Exception:
            pass
        return None

    async def _hide_devtools(self):
        """Minimize DevTools window and focus main browser."""
        if self._devtools_wid:
            try:
                await self._xdt(["windowminimize", self._devtools_wid])
            except Exception:
                pass
        await self._focus_main_window()

    async def _ensure_devtools_focused(self):
        """Focus the DevTools window for JS input."""
        if self._devtools_wid:
            try:
                await self._xdt(["windowactivate", "--sync",
                                 self._devtools_wid])
                await asyncio.sleep(0.15)
                return True
            except Exception:
                pass
        return False

    async def _sync_console_state(self):
        """Detect actual console state on first exec after daemon restart.

        The console may be open from a previous daemon session. Since
        Ctrl+Shift+K is a toggle, we can't force a state — we must detect.
        Strategy: focus URL bar (safety net), toggle console, try a test
        JS exec via clipboard. If result comes back, console is open.
        If not, toggle again.
        """
        sync_marker = "TBP_SYNC_OK"

        # Safety: focus URL bar so any leaked text goes there, not the page
        await self._xdt(["key", "ctrl+l"])
        await asyncio.sleep(0.3)

        # Toggle console
        await self._xdt(["key", "ctrl+shift+k"])
        await asyncio.sleep(1.0)

        # Try a test execution — paste small JS that writes to X11 clipboard
        await self._xdt(["key", "ctrl+a"])
        await asyncio.sleep(0.1)
        test_js = f"copy('{sync_marker}')"
        await self._clipboard_paste(test_js)
        await asyncio.sleep(0.1)
        await self._xdt(["key", "ctrl+Return"])
        await asyncio.sleep(1.0)

        # Check X11 clipboard (same mechanism as _clipboard_read)
        clip = await self._clipboard_read()

        if sync_marker in clip:
            # Console is open and working
            self._console_open = True
        else:
            # Toggle closed the console — toggle again to open it
            await self._xdt(["key", "ctrl+shift+k"])
            await asyncio.sleep(1.0)
            self._console_open = True

        # In separate-window mode, find and track the DevTools window
        if not self._devtools_wid:
            await self._find_devtools_window()

        self._console_synced = True
        logger.info("Console state synced: open=%s, devtools_wid=%s",
                     self._console_open, self._devtools_wid)

    def _get_best_fallback_offset(self):
        """Pick best fallback viewport offset based on current console state."""
        if self._console_open:
            return self._last_good_viewport_offset or (0, _DEFAULT_TOOLBAR_HEIGHT)
        return (self._last_good_viewport_offset_closed
                or self._last_good_viewport_offset
                or (0, _DEFAULT_TOOLBAR_HEIGHT))

    async def _get_viewport_offset(self):
        """Get offset from CSS viewport coords to screen coords.

        xdotool uses screen coordinates; CDP uses CSS viewport coords.
        Uses Firefox's mozInnerScreenX/Y which give the screen coords of
        the content area top-left.

        Cached for 2 seconds — offset only changes when console opens/closes
        (which triggers invalidation via _console_state_changed). Within a
        single click sequence (mouseMoved → mousePressed → mouseReleased),
        the offset stays constant, so caching avoids 3 slow JS evals per click.

        On CSP-restricted pages (Google, etc.) where JS cannot execute at all,
        skips the JS attempt entirely (avoids opening console which causes
        page reflow). Uses last known good offset from a previous page.
        """
        now = time.monotonic()
        if (self._viewport_offset_cache is not None
                and now - self._viewport_offset_cache_time < 2.0):
            return self._viewport_offset_cache

        # Skip JS on pages where it's known to fail — avoids opening console
        # (which causes reflow of centered/flex content) just to time out.
        if not self._js_available:
            fallback = self._get_best_fallback_offset()
            self._viewport_offset_cache = fallback
            self._viewport_offset_cache_time = now
            return fallback

        try:
            # Short timeout (3s) — this is a simple expression, no need for 15s
            offset = await self._exec_js("""({
                x: typeof window.mozInnerScreenX !== 'undefined'
                    ? window.mozInnerScreenX
                    : window.screenX + (window.outerWidth - window.innerWidth) / 2,
                y: typeof window.mozInnerScreenY !== 'undefined'
                    ? window.mozInnerScreenY
                    : window.screenY + (window.outerHeight - window.innerHeight)
            })""", timeout=3)
            if offset and isinstance(offset, dict) and offset.get("y", 0) > 0:
                result = (int(offset["x"]), int(offset["y"]))
                self._viewport_offset_cache = result
                self._viewport_offset_cache_time = now
                # Persist for CSP fallback — track console state
                if self._console_open:
                    self._last_good_viewport_offset = result
                else:
                    self._last_good_viewport_offset_closed = result
                return result
        except Exception:
            pass

        # JS failed — mark page as no-JS to skip future attempts
        self._js_available = False
        fallback = self._get_best_fallback_offset()
        self._viewport_offset_cache = fallback
        self._viewport_offset_cache_time = now
        return fallback

    # ── CDP-compatible public API ──────────────────────────────────

    async def send(self, method, params=None, timeout=60):
        """Send a CDP-style command via native methods."""
        params = params or {}

        # No-ops
        if method in ("Page.enable", "Network.enable", "Runtime.enable",
                       "Emulation.setUserAgentOverride", "Accessibility.enable"):
            return {}

        handler = _NATIVE_HANDLERS.get(method)
        if handler:
            return await handler(self, params, timeout)

        raise NotImplementedError(
            f"No native translation for: {method}"
        )

    def on(self, event, handler):
        """Register an event handler (stored but native has no CDP events)."""
        if event not in self._event_handlers:
            self._event_handlers[event] = []
        self._event_handlers[event].append(handler)

    def off(self, event, handler):
        """Unregister an event handler."""
        if event in self._event_handlers:
            try:
                self._event_handlers[event].remove(handler)
            except ValueError:
                pass

    async def close(self):
        """Close Firefox and callback server."""
        if self._callback_server:
            await asyncio.to_thread(self._callback_server.shutdown)
            self._callback_server = None
        if self._firefox_proc and self._firefox_proc.returncode is None:
            self._firefox_proc.terminate()
            try:
                await asyncio.wait_for(self._firefox_proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._firefox_proc.kill()
                await self._firefox_proc.wait()
        self._firefox_proc = None
        self._disconnected = True

    async def delete_session(self):
        """No-op for native session (close handles everything)."""
        pass

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *exc):
        await self.close()


# ── Native method handlers ──────────────────────────────────

async def _navigate(session, params, timeout):
    """Page.navigate → JS location.assign (reliable) with address bar fallback."""
    url = params.get("url", "about:blank")
    # Strip newlines and control chars to prevent injection
    url = "".join(c for c in url if c >= " " and c != "\x7f")

    from ._utils import escape_js_string
    safe_url = escape_js_string(url)

    # Reset JS availability for new page
    session._js_available = True
    session._viewport_offset_cache = None

    # Primary: navigate via JS (works when console is available)
    try:
        await session._exec_js(f"location.assign('{safe_url}')", timeout=5)
        await asyncio.sleep(3)
        session._viewport_offset = None
        # Console may survive same-origin navigation — close it properly
        await session._close_console()
        return {"frameId": "native"}
    except Exception:
        pass

    # Fallback: address bar paste (for about: pages or if console isn't ready)
    await session._close_console()
    await session._xdt(["key", "ctrl+l"])
    await asyncio.sleep(0.3)
    await session._xdt(["key", "ctrl+a"])
    await asyncio.sleep(0.1)
    pasted = await session._clipboard_paste(url)
    if not pasted:
        await session._xdt(["type", "--clearmodifiers", "--delay", "0", "--", url])
    await asyncio.sleep(0.2)
    await session._xdt(["key", "Return"])

    await asyncio.sleep(3)
    session._viewport_offset = None
    return {"frameId": "native"}


async def _evaluate(session, params, timeout):
    """Runtime.evaluate → console JS execution."""
    expression = params.get("expression", "")

    result = await session._exec_js(expression, timeout=timeout)

    # Check for error strings from our try/catch wrapper
    if isinstance(result, str) and result.startswith("ERR:"):
        return {
            "result": {"type": "string", "value": result},
            "exceptionDetails": {
                "exception": {
                    "description": result[4:],
                    "value": result[4:],
                },
            },
        }

    return {"result": {"type": type(result).__name__, "value": result}}


async def _capture_screenshot(session, params, timeout):
    """Page.captureScreenshot → Xvfb import command.

    Captures only the main browser window (not root) to avoid focus changes
    that would close fullscreen modals (e.g., Upwork's air3-modal).
    """
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_path = tmp.name

    env = os.environ.copy()
    env["DISPLAY"] = session._display

    # Capture main window directly — avoids focus-changing _close_console()
    # which triggers blur events and closes modals on sites like Upwork.
    target_wid = session._main_wid

    # Move DevTools off-screen before capture to prevent it bleeding through.
    # windowmove doesn't change focus — safe for modal pages like Upwork.
    devtools_orig_pos = None
    if target_wid and session._devtools_wid:
        try:
            p = await asyncio.create_subprocess_exec(
                "xdotool", "getwindowgeometry", "--shell",
                session._devtools_wid,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await p.communicate()
            geo = {}
            for line in out.decode().strip().split("\n"):
                if "=" in line:
                    k, v = line.split("=", 1)
                    geo[k] = v
            devtools_orig_pos = (geo.get("X", "0"), geo.get("Y", "0"))
            # Move off-screen (far right)
            p = await asyncio.create_subprocess_exec(
                "xdotool", "windowmove", session._devtools_wid,
                "10000", "0",
                env=env,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await p.wait()
            await asyncio.sleep(0.05)
        except Exception:
            devtools_orig_pos = None

    if target_wid:
        import_args = [
            "import", "-window", target_wid,
            "-display", session._display, tmp_path,
        ]
    else:
        # Fallback: capture root (old behavior) if main WID unknown
        await session._close_console()
        await asyncio.sleep(0.2)
        import_args = [
            "import", "-window", "root",
            "-display", session._display, tmp_path,
        ]

    proc = await asyncio.create_subprocess_exec(
        *import_args, env=env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()

    # Move DevTools back to original position (no focus change)
    if devtools_orig_pos and session._devtools_wid:
        try:
            p = await asyncio.create_subprocess_exec(
                "xdotool", "windowmove", session._devtools_wid,
                devtools_orig_pos[0], devtools_orig_pos[1],
                env=env,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await p.wait()
        except Exception:
            pass

    data = ""
    try:
        if os.path.exists(tmp_path):
            with open(tmp_path, "rb") as f:
                data = base64.b64encode(f.read()).decode()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    return {"data": data}


async def _get_layout_metrics(session, params, timeout):
    """Page.getLayoutMetrics → JS evaluation."""
    result = await session._exec_js("""({
        contentSize: {width: document.documentElement.scrollWidth,
                      height: document.documentElement.scrollHeight},
        layoutViewport: {pageX: 0, pageY: 0,
                         clientWidth: window.innerWidth,
                         clientHeight: window.innerHeight},
        visualViewport: {pageX: 0, pageY: 0,
                         clientWidth: window.innerWidth,
                         clientHeight: window.innerHeight,
                         scale: 1},
    })""", timeout=timeout)
    return result or {}


async def _add_preload_script(session, params, timeout):
    """Page.addScriptToEvaluateOnNewDocument → no-op for native.

    Can't inject preload scripts without Marionette,
    but Firefox doesn't need stealth anyway.
    """
    return {"identifier": "native-noop"}


# ── Cookie handlers ──────────────────────────────────

async def _get_cookies(session, params, timeout):
    """Network.getCookies → JS execution."""
    result = await session._exec_js(
        "document.cookie.split('; ').filter(c=>c).map(c => {"
        "  var p = c.split('=');"
        "  return {name: p[0], value: p.slice(1).join('='),"
        "          domain: location.hostname};"
        "})", timeout=timeout)
    # Validate result is a list — JS timeout returns error string
    if not isinstance(result, list):
        return {"cookies": []}
    return {"cookies": result}


async def _set_cookie(session, params, timeout):
    """Network.setCookie → JS execution (single cookie)."""
    from ._utils import escape_js_string
    cookie = params.get("cookie", params)
    name = escape_js_string(cookie.get("name", ""))
    value = escape_js_string(cookie.get("value", ""))
    domain = escape_js_string(cookie.get("domain", ""))
    path = escape_js_string(cookie.get("path", "/"))
    secure = "Secure;" if cookie.get("secure") else ""
    await session._exec_js(
        f"document.cookie = '{name}={value}; path={path}; "
        f"domain={domain}; {secure}'", timeout=timeout)
    return {}


async def _set_cookies(session, params, timeout):
    """Network.setCookies → iterate and set each cookie."""
    cookies = params.get("cookies", [])
    for cookie in cookies:
        await _set_cookie(session, cookie, timeout)
    return {}


async def _delete_cookies(session, params, timeout):
    """Network.deleteCookies → expire a specific cookie."""
    from ._utils import escape_js_string
    name = escape_js_string(params.get("name", ""))
    if name:
        await session._exec_js(
            f"document.cookie = '{name}=; expires=Thu, 01 Jan 1970 "
            f"00:00:00 GMT; path=/;'", timeout=timeout)
    return {}


async def _clear_all_cookies(session, params, timeout):
    """Network.clearBrowserCookies → clear all document cookies."""
    await session._exec_js(
        "document.cookie.split(';').forEach(c=>{"
        "var n=c.split('=')[0].trim();"
        "document.cookie=n+'=;expires=Thu, 01 Jan 1970 00:00:00 GMT;path=/;';"
        "})", timeout=timeout)
    return {}


async def _print_to_pdf(session, params, timeout):
    """Page.printToPDF → not supported in native mode."""
    return {"data": ""}


# ── Input handlers (xdotool) ──────────────────────────────────

# Button mapping: CDP button name → xdotool button number
_BUTTON_MAP = {"left": "1", "middle": "2", "right": "3"}

# Key mapping: CDP key name → xdotool key name
_KEY_MAP = {
    "Enter": "Return",
    "Backspace": "BackSpace",
    "Tab": "Tab",
    "Escape": "Escape",
    "ArrowUp": "Up",
    "ArrowDown": "Down",
    "ArrowLeft": "Left",
    "ArrowRight": "Right",
    "Delete": "Delete",
    "Home": "Home",
    "End": "End",
    "PageUp": "Prior",
    "PageDown": "Next",
    "Space": "space",
    " ": "space",
}


async def _dispatch_mouse_event(session, params, timeout):
    """Input.dispatchMouseEvent → xdotool mouse commands.

    Converts CSS viewport coordinates to screen coordinates using
    the viewport offset (toolbar height). Console is NOT closed here
    on normal pages — keeping it open ensures CSS coords from
    getBoundingClientRect() remain valid (closing causes page reflow).

    On CSP pages (no JS), close console first so coordinates from
    screenshots (taken with console closed) match reality.
    """

    event_type = params.get("type", "")
    x = params.get("x", 0)
    y = params.get("y", 0)

    # On CSP pages, close console to avoid reflow offset mismatch.
    # Check BEFORE _get_viewport_offset to prevent it from opening console.
    if not session._js_available and session._console_open:
        await session._close_console()

    # Convert CSS viewport coords to screen coords
    offset = await session._get_viewport_offset()

    # If _get_viewport_offset just discovered this is a CSP page (opened
    # console to try JS, which failed), close console and use correct offset.
    if not session._js_available and session._console_open:
        await session._close_console()
        session._viewport_offset_cache = None  # Invalidate console-open offset
        offset = session._get_best_fallback_offset()

    screen_x = int(x + offset[0])
    screen_y = int(y + offset[1])

    if event_type == "mouseMoved":
        # No --sync: avoids hang when mouse is already at target position
        await session._xdt(["mousemove", str(screen_x), str(screen_y)])
        await asyncio.sleep(0.02)

    elif event_type == "mousePressed":
        button = _BUTTON_MAP.get(params.get("button", "left"), "1")
        # Skip redundant mousemove — mouseMoved already positioned cursor
        await session._xdt(["mousedown", button])
        session._page_has_focus = True  # Page element now has focus, not console

    elif event_type == "mouseReleased":
        button = _BUTTON_MAP.get(params.get("button", "left"), "1")
        await session._xdt(["mouseup", button])

    elif event_type == "mouseWheel":
        delta_y = params.get("deltaY", 0)
        # xdotool: button 4 = scroll up, 5 = scroll down
        # CDP: deltaY > 0 = scroll down, deltaY < 0 = scroll up
        if delta_y > 0:
            clicks = max(1, int(abs(delta_y) / 100))
            for _ in range(clicks):
                await session._xdt(["click", "5"])
        elif delta_y < 0:
            clicks = max(1, int(abs(delta_y) / 100))
            for _ in range(clicks):
                await session._xdt(["click", "4"])

    return {}


async def _dispatch_key_event(session, params, timeout):
    """Input.dispatchKeyEvent → xdotool key commands.

    Maps CDP key names and modifiers to xdotool equivalents.
    Only acts on keyDown/rawKeyDown to avoid duplicate events.
    Closes console first so keystrokes reach the page, not DevTools.
    """
    # Close console so keystrokes go to the page, not the console
    await session._close_console()

    event_type = params.get("type", "")
    key = params.get("key", "")
    text = params.get("text", "")
    modifiers = params.get("modifiers", 0)

    # Only act on keyDown/rawKeyDown (avoid duplicate from keyUp)
    if event_type not in ("keyDown", "rawKeyDown"):
        return {}

    # Build modifier prefix
    mod_parts = []
    if modifiers & 2:  # Ctrl
        mod_parts.append("ctrl")
    if modifiers & 8:  # Shift
        mod_parts.append("shift")
    if modifiers & 1:  # Alt
        mod_parts.append("alt")
    if modifiers & 4:  # Meta
        mod_parts.append("super")

    # Map key name
    xdt_key = _KEY_MAP.get(key, "")

    if xdt_key:
        # Special key (Enter, Backspace, etc.)
        if mod_parts:
            full_key = "+".join(mod_parts + [xdt_key])
        else:
            full_key = xdt_key
        await session._xdt(["key", full_key])
    elif text:
        # Regular character with optional modifiers
        if mod_parts:
            full_key = "+".join(mod_parts + [text])
            await session._xdt(["key", full_key])
        else:
            await session._xdt(["type", "--clearmodifiers", "--delay", "0",
                                text])
    elif key and len(key) == 1:
        # Single character key
        if mod_parts:
            full_key = "+".join(mod_parts + [key])
            await session._xdt(["key", full_key])
        else:
            await session._xdt(["type", "--clearmodifiers", "--delay", "0",
                                key])

    return {}


async def _insert_text(session, params, timeout):
    """Input.insertText → text input with mode support.

    Modes:
      clipboard: paste via Ctrl+V (default legacy behavior)
      xdotool: direct key-by-key via xdotool type (works on Google inputs)
      auto: clipboard paste, verify value was set, fallback to xdotool
    Closes console first so text goes to the focused page element.
    """
    await session._close_console()
    text = params.get("text", "")
    mode = params.get("mode", "auto")
    # Strip control chars except common whitespace (tab, newline)
    text = "".join(c for c in text if c >= " " or c in "\t\n")
    if not text:
        return {}

    if mode == "xdotool":
        await session._xdt(["type", "--clearmodifiers", "--delay", "1", text])
    elif mode == "clipboard":
        pasted = await session._clipboard_paste(text)
        if not pasted:
            await session._xdt(["type", "--clearmodifiers", "--delay", "1",
                                text])
    else:
        # auto: clipboard paste, verify, fallback to xdotool, then JS
        pasted = await session._clipboard_paste(text)
        if pasted:
            # Verify the value was actually set
            import asyncio as _aio
            await _aio.sleep(0.15)
            try:
                val = await session._exec_js(
                    "(document.activeElement && "
                    "(document.activeElement.value || "
                    "document.activeElement.textContent || '')).slice(0,50)"
                )
                if val and text[:20] in str(val):
                    return {}
            except Exception:
                pass
            # Paste didn't take effect — close console (exec_js reopened it)
            # and retry with xdotool
            await session._close_console()
            await session._xdt(["type", "--clearmodifiers", "--delay", "1",
                                text])
        else:
            await session._xdt(["type", "--clearmodifiers", "--delay", "1",
                                text])
        # Final verify + JS fallback if keyboard methods both failed
        try:
            val = await session._exec_js(
                "(document.activeElement && "
                "(document.activeElement.value || "
                "document.activeElement.textContent || '')).slice(0,50)"
            )
            if val and text[:20] in str(val):
                return {}
        except Exception:
            pass
        # Last resort: set value via JS on the focused element
        escaped = json.dumps(text)
        try:
            await session._exec_js(
                "(function(){"
                "var el=document.activeElement;"
                "if(!el||(!el.value&&el.value!==''))return false;"
                "var s=Object.getOwnPropertyDescriptor("
                "window.HTMLInputElement.prototype,'value')"
                "||Object.getOwnPropertyDescriptor("
                "window.HTMLTextAreaElement.prototype,'value');"
                "if(s&&s.set){s.set.call(el," + escaped + ");}"
                "else{el.value=" + escaped + ";}"
                "el.dispatchEvent(new Event('input',{bubbles:true}));"
                "el.dispatchEvent(new Event('change',{bubbles:true}));"
                "return true})()"
            )
        except Exception:
            pass
    return {}


# ── Accessibility handler ──────────────────────────────────

async def _get_ax_tree(session, params, timeout):
    """Accessibility.getFullAXTree → JS DOM walk.

    Builds a simplified accessibility tree by walking the DOM and
    extracting ARIA roles, labels, and text content.
    """
    result = await session._exec_js("""(function() {
        var nodes = [];
        var id = 1;
        function walk(el, depth) {
            if (depth > 8 || nodes.length > 200) { return; }
            var role = el.getAttribute && el.getAttribute('role') || '';
            if (!role) {
                var tag = (el.tagName || '').toLowerCase();
                var roleMap = {a:'link',button:'button',h1:'heading',
                    h2:'heading',h3:'heading',h4:'heading',input:'textbox',
                    select:'combobox',textarea:'textbox',img:'image',
                    nav:'navigation',main:'main',header:'banner',
                    footer:'contentinfo',form:'form',table:'table',
                    ul:'list',ol:'list',li:'listitem'};
                role = roleMap[tag] || tag;
            }
            var name = el.getAttribute && (el.getAttribute('aria-label') ||
                       el.getAttribute('alt') || el.getAttribute('title') || '');
            if (!name && el.textContent && el.children.length === 0) {
                name = el.textContent.trim().substring(0, 100);
            }
            if (role || name) {
                nodes.push({nodeId: String(id++),
                           role: {value: role},
                           name: {value: name}});
            }
            for (var i = 0; i < el.children.length; i++) {
                walk(el.children[i], depth + 1);
            }
        }
        if (document.body) { walk(document.body, 0); }
        return nodes;
    })()""", timeout=timeout)
    if not isinstance(result, list):
        return {"nodes": []}
    return {"nodes": result}


# Method dispatch table
_NATIVE_HANDLERS = {
    "Page.navigate": _navigate,
    "Runtime.evaluate": _evaluate,
    "Page.captureScreenshot": _capture_screenshot,
    "Page.addScriptToEvaluateOnNewDocument": _add_preload_script,
    "Page.getLayoutMetrics": _get_layout_metrics,
    "Page.printToPDF": _print_to_pdf,
    "Network.getCookies": _get_cookies,
    "Network.getAllCookies": _get_cookies,
    "Network.setCookie": _set_cookie,
    "Network.setCookies": _set_cookies,
    "Network.deleteCookies": _delete_cookies,
    "Network.clearBrowserCookies": _clear_all_cookies,
    "Input.dispatchMouseEvent": _dispatch_mouse_event,
    "Input.dispatchKeyEvent": _dispatch_key_event,
    "Input.insertText": _insert_text,
    "Accessibility.getFullAXTree": _get_ax_tree,
}
