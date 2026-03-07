#!/usr/bin/env python3
"""Termux Browser Pilot - CLI entry point.

Daemon-backed commands (persistent browser):
    tbp start                        Start browser daemon
    tbp stop                         Stop daemon
    tbp status                       Show daemon status
    tbp goto URL [-cf]               Navigate
    tbp click SELECTOR               Click element
    tbp type SELECTOR TEXT           Type into element
    tbp text [SELECTOR]              Get page text
    tbp screenshot [PATH]            Take screenshot
    tbp eval EXPRESSION              Run JavaScript
    tbp links                        List links
    tbp a11y                         Accessibility tree

Legacy commands (spawn browser per command):
    tbp navigate URL [-cf] [-s PATH] Navigate + screenshot
    tbp fingerprint                  Bot detection check
"""

import argparse
import asyncio
import json
import os
import sys

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


# ── Daemon-backed commands ──────────────────────────────────

async def cmd_start(args):
    """Start the browser daemon."""
    from src.client import is_daemon_running
    if is_daemon_running():
        print("Daemon already running")
        return

    if args.foreground:
        from src.daemon import Daemon
        import logging
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(message)s")
        daemon = Daemon(browser=args.browser,
                        idle_timeout=getattr(args, 'idle_timeout', None),
                        proxy=getattr(args, 'proxy', None))
        await daemon.run()
    else:
        # Launch daemon as subprocess
        cmd = [sys.executable, "-m", "src.daemon", "start",
               "--browser", args.browser]
        if getattr(args, 'idle_timeout', None):
            cmd += ["--idle-timeout", str(args.idle_timeout)]
        if getattr(args, 'proxy', None):
            cmd += ["--proxy", args.proxy]
        proc = await asyncio.create_subprocess_exec(*cmd)
        await proc.wait()


async def cmd_stop(args):
    """Stop the browser daemon."""
    from src.client import is_daemon_running, send_command
    if not is_daemon_running():
        print("No daemon running")
        return
    try:
        resp = await send_command("shutdown", timeout=10)
        if resp.get("success"):
            print("Daemon shutting down...")
            # Wait for process to exit
            await asyncio.sleep(2)
            print("Done")
        else:
            print(f"Error: {resp.get('error')}", file=sys.stderr)
    except (ConnectionError, OSError):
        print("Daemon stopped")


async def cmd_status(args):
    """Show daemon status."""
    from src.client import is_daemon_running, send_command
    if not is_daemon_running():
        _output(args, {"success": False, "error": "No daemon running"})
        return
    resp = await send_command("status", browser=args.browser)
    if _json_mode(args):
        _output(args, resp)
    elif resp.get("success"):
        d = resp["data"]
        print(f"PID: {d['pid']}")
        print(f"Browser: {d['browser']}")
        print(f"URL: {d.get('url', 'N/A')}")
        print(f"Title: {d.get('title', 'N/A')}")
        print(f"Uptime: {d['uptime']}s")
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_goto(args):
    """Navigate to URL (daemon-backed)."""
    from src.client import send_command
    params = {
        "url": args.url,
        "timeout": args.timeout,
        "cloudflare": getattr(args, "cloudflare", False),
    }
    resp = await send_command("goto", params, timeout=args.timeout + 10,
                              browser=args.browser)
    if _json_mode(args):
        _output(args, resp)
    elif resp.get("success"):
        d = resp["data"]
        print(f"Title: {d['title']}")
        print(f"URL: {d['url']}")
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_click_d(args):
    """Click element (daemon-backed)."""
    from src.client import send_command
    params = {"target": args.target, "human": getattr(args, "human", False)}
    resp = await send_command("click", params, browser=args.browser)
    if _json_mode(args):
        _output(args, resp)
    elif resp.get("success"):
        print(f"Clicked: {args.target}")
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_type_d(args):
    """Type into element (daemon-backed)."""
    from src.client import send_command
    params = {"target": args.target, "text": args.text}
    resp = await send_command("type", params, browser=args.browser)
    if _json_mode(args):
        _output(args, resp)
    elif resp.get("success"):
        print(f"Typed into {args.target}")
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_text_d(args):
    """Get page text (daemon-backed)."""
    from src.client import send_command
    params = {"selector": getattr(args, "selector", None),
              "limit": getattr(args, "limit", None)}
    resp = await send_command("text", params, browser=args.browser)
    if _json_mode(args):
        _output(args, resp)
    elif resp.get("success"):
        print(resp["data"]["text"])
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_html_d(args):
    """Get page HTML (daemon-backed)."""
    from src.client import send_command
    params = {"selector": getattr(args, "selector", None),
              "limit": getattr(args, "limit", None)}
    resp = await send_command("html", params, browser=args.browser)
    if _json_mode(args):
        _output(args, resp)
    elif resp.get("success"):
        print(resp["data"]["html"])
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_links_d(args):
    """List links (daemon-backed)."""
    from src.client import send_command
    params = {"limit": getattr(args, "limit", 100)}
    resp = await send_command("links", params, browser=args.browser)
    if _json_mode(args):
        _output(args, resp)
    elif resp.get("success"):
        for link in resp["data"]["links"]:
            print(f"{link.get('text', '')[:60]:60s} {link.get('href', '')}")
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_eval_d(args):
    """Evaluate JavaScript (daemon-backed)."""
    from src.client import send_command
    params = {"expression": args.expression}
    resp = await send_command("eval", params, browser=args.browser)
    if _json_mode(args):
        _output(args, resp)
    elif resp.get("success"):
        result = resp["data"]["result"]
        if isinstance(result, (dict, list)):
            print(json.dumps(result, indent=2))
        else:
            print(result)
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_screenshot_d(args):
    """Take screenshot (daemon-backed)."""
    from src.client import send_command
    path = os.path.abspath(getattr(args, "path", None) or "screenshot.png")
    params = {"path": path, "full": getattr(args, "full", False)}
    resp = await send_command("screenshot", params, browser=args.browser)
    if _json_mode(args):
        _output(args, resp)
    elif resp.get("success"):
        print(f"Saved: {resp['data']['path']}")
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_press_d(args):
    """Press key (daemon-backed)."""
    from src.client import send_command
    resp = await send_command("press", {"key": args.key}, browser=args.browser)
    if _json_mode(args):
        _output(args, resp)
    elif resp.get("success"):
        print(f"Pressed: {args.key}")
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_scroll_d(args):
    """Scroll page (daemon-backed)."""
    from src.client import send_command
    amount = abs(args.amount) if args.down else -abs(args.amount)
    resp = await send_command("scroll", {"amount": amount},
                              browser=args.browser)
    if _json_mode(args):
        _output(args, resp)
    elif resp.get("success"):
        print(f"Scrolled: {amount}px")
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_wait_d(args):
    """Wait seconds (daemon-backed)."""
    from src.client import send_command
    resp = await send_command("wait", {"seconds": args.seconds},
                              browser=args.browser)
    if _json_mode(args):
        _output(args, resp)


async def cmd_waitfor_d(args):
    """Wait for selector (daemon-backed)."""
    from src.client import send_command
    params = {"selector": args.selector, "timeout": args.timeout}
    resp = await send_command("waitfor", params, timeout=args.timeout + 10,
                              browser=args.browser)
    if _json_mode(args):
        _output(args, resp)
    elif resp.get("success"):
        print(f"Found: {args.selector}")
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_cookies_d(args):
    """Manage cookies (daemon-backed)."""
    from src.client import send_command
    if args.clear:
        resp = await send_command("cookies", {"action": "clear"},
                                  browser=args.browser)
    elif args.save:
        resp = await send_command("cookies", {"action": "save",
                                  "path": os.path.abspath(args.save)},
                                  browser=args.browser)
    elif args.load:
        resp = await send_command("cookies", {"action": "load",
                                  "path": os.path.abspath(args.load)},
                                  browser=args.browser)
    else:
        resp = await send_command("cookies", {"action": "list"},
                                  browser=args.browser)

    if _json_mode(args):
        _output(args, resp)
    elif resp.get("success"):
        d = resp["data"]
        if "cookies" in d:
            for c in d["cookies"]:
                print(f"  {c.get('domain', ''):30s} {c.get('name', '')[:30]:30s} = "
                      f"{c.get('value', '')[:40]}")
        elif "saved" in d:
            print(f"Saved {d['saved']} cookies to {d['path']}")
        elif "loaded" in d:
            print(f"Loaded {d['loaded']} cookies from {d['path']}")
        elif "cleared" in d:
            print("Cookies cleared")
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_a11y_d(args):
    """Accessibility tree (daemon-backed)."""
    from src.client import send_command
    params = {"limit": getattr(args, "limit", None)}
    resp = await send_command("a11y", params, browser=args.browser)
    if _json_mode(args):
        _output(args, resp)
    elif resp.get("success"):
        print(resp["data"]["tree"])
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)


# ── Legacy commands (spawn browser per command) ──────────────

async def cmd_navigate(args):
    """Navigate (legacy — spawns new browser)."""
    from src.pilot import Pilot
    session_file = getattr(args, "session", None)
    async with Pilot(session_file=session_file, browser=args.browser) as pilot:
        if args.cloudflare:
            url = await pilot.goto_cf(args.url, timeout=args.timeout)
            print(f"Final URL: {url}")
        else:
            await pilot.goto(args.url, timeout=args.timeout)

        title = await pilot.title()
        current_url = await pilot.url()
        print(f"Title: {title}")
        print(f"URL: {current_url}")

        if args.screenshot:
            await pilot.screenshot(args.screenshot)
            print(f"Screenshot: {args.screenshot}")

        if args.text:
            text = await pilot.text()
            print(f"\n{text[:2000]}")


async def cmd_fingerprint(args):
    """Check browser fingerprint for bot detection."""
    from src.pilot import Pilot
    async with Pilot(browser=args.browser) as pilot:
        url = args.url or "https://bot.sannysoft.com"
        await pilot.goto(url, timeout=args.timeout)
        await pilot.wait(3)
        path = args.screenshot or "fingerprint.png"
        await pilot.screenshot(path, full_page=True)
        title = await pilot.title()
        print(f"Title: {title}")
        print(f"Screenshot: {path}")


async def cmd_device(args):
    """Show detected device info."""
    from src.device import print_device_info
    print_device_info()


async def cmd_kill(args):
    """Force-kill any running browser session."""
    from src.lock import SessionLock, DEFAULT_LOCK_PATH
    # Also stop daemon if running
    from src.client import is_daemon_running
    from src.daemon import PID_PATH as DAEMON_PID_PATH, SOCKET_PATH as DAEMON_SOCK
    if is_daemon_running():
        from src._utils import read_pid_file
        dpid = read_pid_file(DAEMON_PID_PATH)
        if dpid:
            try:
                import signal as _sig
                os.kill(dpid, _sig.SIGTERM)
                print(f"Sent SIGTERM to daemon (PID {dpid})")
            except ProcessLookupError:
                pass
        for p in (DAEMON_SOCK, DAEMON_PID_PATH):
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass

    if os.path.exists(DEFAULT_LOCK_PATH):
        from src._utils import read_pid_file
        pid = read_pid_file(DEFAULT_LOCK_PATH)
        if pid:
            try:
                import signal
                os.kill(pid, signal.SIGTERM)
                print(f"Sent SIGTERM to PID {pid}")
            except ProcessLookupError:
                print("Process already dead")
        else:
            print("Invalid PID file")
        try:
            os.unlink(DEFAULT_LOCK_PATH)
        except FileNotFoundError:
            pass
        print("Lock released")
    else:
        print("No active session found")


# ── Output helpers ──────────────────────────────────

def _json_mode(args):
    return getattr(args, "json", False)


def _output(args, data):
    if _json_mode(args):
        print(json.dumps(data))
    elif isinstance(data, dict):
        if data.get("success") is False:
            print(f"Error: {data.get('error', 'Unknown')}", file=sys.stderr)
        elif "data" in data:
            for k, v in data["data"].items():
                if isinstance(v, str) and len(v) > 200:
                    print(v)
                else:
                    print(f"{k}: {v}")


# ── Argument parser ──────────────────────────────────

def main():
    # Common args inherited by all subcommands
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--timeout", type=int, default=45)
    common.add_argument("--browser", "-b", choices=["firefox", "chromium", "auto"],
                        default="auto")
    common.add_argument("--json", "-j", action="store_true",
                        help="JSON output (for AI agents)")

    parser = argparse.ArgumentParser(
        prog="tbp",
        description="Termux Browser Pilot - Browser automation for Termux",
        parents=[common],
    )
    from src import __version__
    parser.add_argument("--version", "-V", action="version", version=f"tbp {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    # ── Daemon lifecycle ──
    p = sub.add_parser("start", help="Start browser daemon", parents=[common])
    p.add_argument("--foreground", "-f", action="store_true")
    p.add_argument("--idle-timeout", type=int, default=None,
                    help="Auto-shutdown after N seconds of inactivity")
    p.add_argument("--proxy", default=None,
                    help="Proxy URL (http://host:port or socks5://host:port)")
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("stop", help="Stop browser daemon", parents=[common])
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("status", help="Show daemon status", parents=[common])
    p.set_defaults(func=cmd_status)

    # ── Daemon-backed navigation ──
    p = sub.add_parser("goto", aliases=["go"], help="Navigate to URL", parents=[common])
    p.add_argument("url")
    p.add_argument("--cloudflare", "-cf", action="store_true")
    p.set_defaults(func=cmd_goto)

    p = sub.add_parser("back", help="Navigate back", parents=[common])
    p.set_defaults(func=lambda a: send_simple(a, "back"))

    p = sub.add_parser("forward", help="Navigate forward", parents=[common])
    p.set_defaults(func=lambda a: send_simple(a, "forward"))

    p = sub.add_parser("reload", help="Reload page", parents=[common])
    p.set_defaults(func=lambda a: send_simple(a, "reload"))

    # ── Daemon-backed interaction ──
    p = sub.add_parser("click", help="Click element", parents=[common])
    p.add_argument("target", help="CSS selector")
    p.add_argument("--human", action="store_true", help="Human-like movement")
    p.set_defaults(func=cmd_click_d)

    p = sub.add_parser("type", help="Type into element", parents=[common])
    p.add_argument("target", help="CSS selector")
    p.add_argument("text", help="Text to type")
    p.set_defaults(func=cmd_type_d)

    p = sub.add_parser("press", help="Press keyboard key", parents=[common])
    p.add_argument("key", help="Key name (Enter, Tab, Escape...)")
    p.set_defaults(func=cmd_press_d)

    p = sub.add_parser("scroll", help="Scroll page", parents=[common])
    p.add_argument("--amount", type=int, default=300, help="Pixels")
    p.add_argument("--down", action="store_true", default=True)
    p.add_argument("--up", dest="down", action="store_false")
    p.set_defaults(func=cmd_scroll_d)

    # ── Daemon-backed content ──
    p = sub.add_parser("text", help="Get page text", parents=[common])
    p.add_argument("--selector", "-s")
    p.add_argument("--limit", "-l", type=int)
    p.set_defaults(func=cmd_text_d)

    p = sub.add_parser("html", help="Get page HTML", parents=[common])
    p.add_argument("--selector", "-s")
    p.add_argument("--limit", "-l", type=int)
    p.set_defaults(func=cmd_html_d)

    p = sub.add_parser("links", help="List page links", parents=[common])
    p.add_argument("--limit", "-l", type=int, default=100)
    p.set_defaults(func=cmd_links_d)

    p = sub.add_parser("eval", help="Evaluate JavaScript", parents=[common])
    p.add_argument("expression")
    p.set_defaults(func=cmd_eval_d)

    p = sub.add_parser("a11y", help="Accessibility tree", parents=[common])
    p.add_argument("--limit", "-l", type=int)
    p.set_defaults(func=cmd_a11y_d)

    p = sub.add_parser("find", help="Find elements by visible text", parents=[common])
    p.add_argument("text", help="Text to search for")
    p.add_argument("--role", "-r", default="", help="Filter by role (link, button, etc.)")
    p.add_argument("--limit", "-l", type=int, default=10)
    p.set_defaults(func=cmd_find_d)

    # ── Tab management ──
    p = sub.add_parser("tab", help="Tab management", parents=[common])
    tab_sub = p.add_subparsers(dest="tab_action", required=True)
    tp = tab_sub.add_parser("new", help="Open new tab")
    tp.add_argument("url", nargs="?", default="", help="Optional URL to open")
    tp = tab_sub.add_parser("close", help="Close current tab")
    tp = tab_sub.add_parser("next", help="Switch to next tab")
    tp = tab_sub.add_parser("prev", help="Switch to previous tab")
    tp = tab_sub.add_parser("goto", help="Switch to tab by index (1-9)")
    tp.add_argument("index", type=int, help="Tab index (1-9)")
    p.set_defaults(func=cmd_tab_d)

    # ── Request blocking ──
    p = sub.add_parser("block", help="Block URL patterns", parents=[common])
    p.add_argument("patterns", nargs="+", help="URL substrings/domains to block")
    p.set_defaults(func=cmd_block_d)

    p = sub.add_parser("unblock", help="Unblock URL patterns", parents=[common])
    p.add_argument("patterns", nargs="+", help="URL substrings/domains to unblock")
    p.set_defaults(func=cmd_unblock_d)

    p = sub.add_parser("blocklist", help="List blocked patterns", parents=[common])
    p.set_defaults(func=cmd_blocklist_d)

    # ── Multi-step macros ──
    p = sub.add_parser("macro", help="Run macro from JSON file", parents=[common])
    p.add_argument("file", help="JSON file with steps array")
    p.set_defaults(func=cmd_macro_d)

    # ── Console log capture ──
    p = sub.add_parser("console", help="Console log capture", parents=[common])
    console_sub = p.add_subparsers(dest="console_action", required=True)
    console_sub.add_parser("start", help="Start capturing console logs")
    console_sub.add_parser("stop", help="Stop capturing console logs")
    cp = console_sub.add_parser("logs", help="Show captured logs")
    cp.add_argument("--limit", "-l", type=int, default=100)
    cp.add_argument("--clear", "-c", action="store_true")
    console_sub.add_parser("clear", help="Clear captured logs")
    p.set_defaults(func=cmd_console_d)

    # ── Downloads ──
    p = sub.add_parser("downloads", aliases=["dl"],
                        help="List downloaded files", parents=[common])
    p.set_defaults(func=cmd_downloads_d)

    # ── Network request log ──
    p = sub.add_parser("network", aliases=["net"],
                        help="Network request log", parents=[common])
    net_sub = p.add_subparsers(dest="network_action", required=True)
    net_sub.add_parser("start", help="Start capturing network requests")
    net_sub.add_parser("stop", help="Stop capturing network requests")
    np = net_sub.add_parser("logs", help="Show captured requests")
    np.add_argument("--limit", "-l", type=int, default=100)
    np.add_argument("--clear", "-c", action="store_true")
    net_sub.add_parser("clear", help="Clear captured requests")
    p.set_defaults(func=cmd_network_d)

    # ── DOM mutation observer ──
    p = sub.add_parser("observe", help="DOM mutation observer", parents=[common])
    obs_sub = p.add_subparsers(dest="observe_action", required=True)
    obs_sub.add_parser("start", help="Start watching DOM mutations")
    obs_sub.add_parser("stop", help="Stop watching DOM mutations")
    op = obs_sub.add_parser("logs", help="Show captured mutations")
    op.add_argument("--limit", "-l", type=int, default=100)
    op.add_argument("--clear", "-c", action="store_true")
    obs_sub.add_parser("clear", help="Clear captured mutations")
    p.set_defaults(func=cmd_observe_d)

    # ── Element screenshot ──
    p = sub.add_parser("screenshot-element", aliases=["sse"],
                        help="Screenshot specific element", parents=[common])
    p.add_argument("target", help="CSS selector of element")
    p.add_argument("path", nargs="?", default="element.png")
    p.set_defaults(func=cmd_screenshot_element_d)

    # ── Iframe support ──
    p = sub.add_parser("iframe", help="Iframe operations", parents=[common])
    iframe_sub = p.add_subparsers(dest="iframe_action", required=True)
    iframe_sub.add_parser("list", help="List all iframes")
    ip = iframe_sub.add_parser("eval", help="Evaluate JS inside iframe")
    ip.add_argument("selector", help="Iframe CSS selector")
    ip.add_argument("expression", help="JavaScript expression")
    ip = iframe_sub.add_parser("text", help="Get text from iframe")
    ip.add_argument("selector", help="Iframe CSS selector")
    ip.add_argument("--inner", "-i", help="Inner element CSS selector")
    ip = iframe_sub.add_parser("click", help="Click element inside iframe")
    ip.add_argument("selector", help="Iframe CSS selector")
    ip.add_argument("target", help="Element CSS selector inside iframe")
    p.set_defaults(func=cmd_iframe_d)

    # ── File upload ──
    p = sub.add_parser("upload", help="Upload file to input element", parents=[common])
    p.add_argument("selector", help="CSS selector for file input")
    p.add_argument("path", help="File path to upload")
    p.set_defaults(func=cmd_upload_d)

    # ── Geolocation spoofing ──
    p = sub.add_parser("geo", help="Geolocation spoofing", parents=[common])
    geo_sub = p.add_subparsers(dest="geo_action", required=True)
    gp = geo_sub.add_parser("set", help="Set geolocation override")
    gp.add_argument("latitude", type=float, help="Latitude (-90 to 90)")
    gp.add_argument("longitude", type=float, help="Longitude (-180 to 180)")
    gp.add_argument("--accuracy", "-a", type=float, default=100,
                     help="Accuracy in meters (default: 100)")
    geo_sub.add_parser("clear", help="Clear geolocation override")
    p.set_defaults(func=cmd_geo_d)

    # ── User agent switching ──
    p = sub.add_parser("useragent", aliases=["ua"],
                        help="User agent switching", parents=[common])
    ua_sub = p.add_subparsers(dest="ua_action", required=True)
    up = ua_sub.add_parser("set", help="Set user agent override")
    up.add_argument("useragent", help="User agent string")
    ua_sub.add_parser("clear", help="Clear user agent override")
    p.set_defaults(func=cmd_useragent_d)

    # ── Cookie injection ──
    p = sub.add_parser("cookie-set", aliases=["cs"],
                        help="Set a cookie", parents=[common])
    p.add_argument("name", help="Cookie name")
    p.add_argument("value", help="Cookie value")
    p.add_argument("--domain", "-d", default="", help="Cookie domain")
    p.add_argument("--path", default="/", help="Cookie path (default: /)")
    p.add_argument("--max-age", type=int, default=None,
                    help="Max age in seconds")
    p.add_argument("--secure", action="store_true", help="Secure flag")
    p.add_argument("--samesite", choices=["Strict", "Lax", "None"],
                    default="", help="SameSite attribute")
    p.set_defaults(func=cmd_cookie_set_d)

    # ── Local/session storage ──
    p = sub.add_parser("storage", help="Local/session storage", parents=[common])
    p.add_argument("--type", "-t", choices=["local", "session"],
                    default="local", help="Storage type (default: local)")
    storage_sub = p.add_subparsers(dest="storage_action", required=True)
    sp = storage_sub.add_parser("list", help="List all items")
    sp.add_argument("--limit", "-l", type=int, default=100)
    sp = storage_sub.add_parser("get", help="Get value by key")
    sp.add_argument("key", help="Storage key")
    sp = storage_sub.add_parser("set", help="Set key-value pair")
    sp.add_argument("key", help="Storage key")
    sp.add_argument("value", help="Storage value")
    sp = storage_sub.add_parser("remove", help="Remove key")
    sp.add_argument("key", help="Storage key")
    storage_sub.add_parser("clear", help="Clear all items")
    p.set_defaults(func=cmd_storage_d)

    # ── Clipboard access ──
    p = sub.add_parser("clipboard", aliases=["clip"],
                        help="Clipboard read/write", parents=[common])
    clip_sub = p.add_subparsers(dest="clip_action", required=True)
    clip_sub.add_parser("read", help="Read clipboard text")
    cp = clip_sub.add_parser("write", help="Write text to clipboard")
    cp.add_argument("text", help="Text to write")
    p.set_defaults(func=cmd_clipboard_d)

    # ── Form auto-fill ──
    p = sub.add_parser("form-fill", aliases=["ff"],
                        help="Fill form fields from JSON file", parents=[common])
    p.add_argument("file", help="JSON file with [{selector, value}, ...]")
    p.set_defaults(func=cmd_form_fill_d)

    # ── CSS injection ──
    p = sub.add_parser("css", help="Inject/remove custom CSS", parents=[common])
    css_sub = p.add_subparsers(dest="css_action", required=True)
    cp = css_sub.add_parser("inject", help="Inject CSS (string or file)")
    cp.add_argument("css", help="CSS text or file path")
    cp.add_argument("--id", help="Stylesheet ID (auto-generated if omitted)")
    css_sub.add_parser("list", help="List injected stylesheets")
    cp = css_sub.add_parser("remove", help="Remove stylesheet(s)")
    cp.add_argument("--id", help="Stylesheet ID (omit to remove all)")
    p.set_defaults(func=cmd_css_d)

    # ── Wait+Action ──
    p = sub.add_parser("waitact", aliases=["wa"],
                        help="Wait for element then act", parents=[common])
    p.add_argument("selector", help="CSS selector to wait for")
    p.add_argument("--click", "-c", action="store_true",
                    help="Click when found (default)")
    p.add_argument("--type", "-t", dest="type_text", metavar="TEXT",
                    help="Type text when found")
    p.add_argument("--text", dest="get_text", action="store_true",
                    help="Get text when found")
    p.set_defaults(func=cmd_waitact_d)

    # ── Page event capture ──
    p = sub.add_parser("events", help="Page event capture", parents=[common])
    evt_sub = p.add_subparsers(dest="events_action", required=True)
    ep = evt_sub.add_parser("start", help="Start capturing events")
    ep.add_argument("--types", nargs="+",
                     help="Event types (default: click submit input change keydown)")
    evt_sub.add_parser("stop", help="Stop capturing events")
    ep = evt_sub.add_parser("logs", help="Show captured events")
    ep.add_argument("--limit", "-l", type=int, default=100)
    ep.add_argument("--clear", "-c", action="store_true")
    evt_sub.add_parser("clear", help="Clear captured events")
    p.set_defaults(func=cmd_events_d)

    # ── Viewport/window resize ──
    p = sub.add_parser("viewport", aliases=["vp"],
                        help="Viewport/window resize", parents=[common])
    vp_sub = p.add_subparsers(dest="viewport_action", required=True)
    vp = vp_sub.add_parser("set", help="Set window size")
    vp.add_argument("width", type=int, help="Width in pixels")
    vp.add_argument("height", type=int, help="Height in pixels")
    vp_sub.add_parser("get", help="Get current dimensions")
    p.set_defaults(func=cmd_viewport_d)

    # ── Page search ──
    p = sub.add_parser("search", help="Find text on page", parents=[common])
    search_sub = p.add_subparsers(dest="search_action", required=True)
    sp = search_sub.add_parser("find", help="Search for text")
    sp.add_argument("query", help="Text to search for")
    sp.add_argument("--case-sensitive", "-cs", action="store_true")
    search_sub.add_parser("next", help="Go to next match")
    search_sub.add_parser("prev", help="Go to previous match")
    search_sub.add_parser("clear", help="Clear search highlights")
    p.set_defaults(func=cmd_search_d)

    # ── Shadow DOM ──
    p = sub.add_parser("shadow", help="Shadow DOM operations", parents=[common])
    shadow_sub = p.add_subparsers(dest="shadow_action", required=True)
    sp = shadow_sub.add_parser("query", help="Find element in shadow DOM")
    sp.add_argument("selector", help="CSS selector")
    sp = shadow_sub.add_parser("text", help="Get text from shadow DOM element")
    sp.add_argument("selector", help="CSS selector")
    sp = shadow_sub.add_parser("click", help="Click element in shadow DOM")
    sp.add_argument("selector", help="CSS selector")
    p.set_defaults(func=cmd_shadow_d)

    # ── Response body capture ──
    p = sub.add_parser("responses", aliases=["resp"],
                        help="Response body capture", parents=[common])
    resp_sub = p.add_subparsers(dest="responses_action", required=True)
    resp_sub.add_parser("start", help="Start capturing responses")
    resp_sub.add_parser("stop", help="Stop capturing responses")
    rp = resp_sub.add_parser("logs", help="Show captured responses")
    rp.add_argument("--limit", "-l", type=int, default=100)
    rp.add_argument("--clear", "-c", action="store_true")
    resp_sub.add_parser("clear", help="Clear captured responses")
    p.set_defaults(func=cmd_responses_d)

    # ── Multi-tab sessions ──
    p = sub.add_parser("session", help="Multi-tab session management",
                        parents=[common])
    sess_sub = p.add_subparsers(dest="session_action", required=True)
    sp = sess_sub.add_parser("save", help="Save all tabs as session")
    sp.add_argument("name", help="Session name")
    sp = sess_sub.add_parser("load", help="Load a saved session")
    sp.add_argument("name", help="Session name")
    sp = sess_sub.add_parser("delete", help="Delete a session")
    sp.add_argument("name", help="Session name")
    sess_sub.add_parser("list", help="List saved sessions")
    p.set_defaults(func=cmd_session_d)

    # ── HTTP header injection ──
    p = sub.add_parser("headers", help="Custom HTTP headers", parents=[common])
    hdr_sub = p.add_subparsers(dest="headers_action", required=True)
    hp = hdr_sub.add_parser("set", help="Set custom headers")
    hp.add_argument("headers", nargs="+", help="Headers as Name:Value pairs")
    hdr_sub.add_parser("clear", help="Clear all custom headers")
    hdr_sub.add_parser("list", help="List custom headers")
    p.set_defaults(func=cmd_headers_d)

    # ── Page performance metrics ──
    p = sub.add_parser("perf", help="Page performance metrics", parents=[common])
    p.set_defaults(func=cmd_perf_d)

    # ── Element attributes ──
    p = sub.add_parser("attr", help="Element attribute operations", parents=[common])
    attr_sub = p.add_subparsers(dest="attr_action", required=True)
    ap = attr_sub.add_parser("get", help="Get attribute(s)")
    ap.add_argument("selector", help="CSS selector")
    ap.add_argument("--name", "-n", help="Attribute name (omit for all)")
    ap = attr_sub.add_parser("set", help="Set attribute")
    ap.add_argument("selector", help="CSS selector")
    ap.add_argument("name", help="Attribute name")
    ap.add_argument("value", help="Attribute value")
    ap = attr_sub.add_parser("remove", help="Remove attribute")
    ap.add_argument("selector", help="CSS selector")
    ap.add_argument("name", help="Attribute name")
    p.set_defaults(func=cmd_attr_d)

    # ── Browser profile management ──
    p = sub.add_parser("profile", help="Browser profile management", parents=[common])
    prof_sub = p.add_subparsers(dest="profile_action", required=True)
    pp = prof_sub.add_parser("save", help="Save current state as profile")
    pp.add_argument("name", help="Profile name (alphanumeric, -, _)")
    pp = prof_sub.add_parser("load", help="Load a saved profile")
    pp.add_argument("name", help="Profile name")
    pp = prof_sub.add_parser("delete", help="Delete a profile")
    pp.add_argument("name", help="Profile name")
    prof_sub.add_parser("list", help="List saved profiles")
    p.set_defaults(func=cmd_profile_d)

    # ── Element highlight ──
    p = sub.add_parser("highlight", aliases=["hl"],
                        help="Highlight page elements", parents=[common])
    hl_sub = p.add_subparsers(dest="highlight_action", required=True)
    hp = hl_sub.add_parser("add", help="Highlight elements")
    hp.add_argument("selector", help="CSS selector")
    hp.add_argument("--color", "-c", default="red", help="Outline color")
    hp.add_argument("--label", "-l", default="", help="Tooltip label")
    hp = hl_sub.add_parser("clear", help="Clear highlight(s)")
    hp.add_argument("selector", nargs="?", default="", help="Selector to clear (all if omitted)")
    p.set_defaults(func=cmd_highlight_d)

    # ── Cookie auto-login (auth) ──
    p = sub.add_parser("auth", help="Auth session management", parents=[common])
    auth_sub = p.add_subparsers(dest="auth_action", required=True)
    ap = auth_sub.add_parser("save", help="Save cookies as auth session")
    ap.add_argument("name", help="Session name")
    ap = auth_sub.add_parser("load", help="Load auth session (cookies + navigate)")
    ap.add_argument("name", help="Session name")
    ap = auth_sub.add_parser("delete", help="Delete auth session")
    ap.add_argument("name", help="Session name")
    auth_sub.add_parser("list", help="List auth sessions")
    p.set_defaults(func=cmd_auth_d)

    # ── Network throttling ──
    p = sub.add_parser("throttle", help="Network throttling", parents=[common])
    thr_sub = p.add_subparsers(dest="throttle_action", required=True)
    tp = thr_sub.add_parser("set", help="Set throttle preset or latency")
    tp.add_argument("--preset", "-p", help="3g, slow-3g, fast-3g, offline")
    tp.add_argument("--latency", "-l", type=int, help="Custom latency (ms)")
    thr_sub.add_parser("clear", help="Remove throttling")
    thr_sub.add_parser("get", help="Get current throttle config")
    p.set_defaults(func=cmd_throttle_d)

    # ── Annotated screenshot ──
    p = sub.add_parser("annotate", aliases=["ann"],
                        help="Screenshot with numbered element labels", parents=[common])
    p.add_argument("--path", "-o", default="annotated.png", help="Output file")
    p.add_argument("--selector", "-s", default="", help="CSS selector (default: interactive)")
    p.add_argument("--max", "-m", type=int, default=25, help="Max elements (1-100)")
    p.add_argument("--full", "-f", action="store_true", help="Full page screenshot")
    p.set_defaults(func=cmd_annotate_d)

    # ── Page audit ──
    p = sub.add_parser("audit", help="Page health report", parents=[common])
    p.set_defaults(func=cmd_audit_d)

    # ── Response mocking ──
    p = sub.add_parser("mock", help="Response mocking for testing", parents=[common])
    mock_sub = p.add_subparsers(dest="mock_action", required=True)
    mp = mock_sub.add_parser("set", help="Add/replace a response mock")
    mp.add_argument("pattern", help="URL substring to match")
    mp.add_argument("body", help="Response body (string)")
    mp.add_argument("--status", type=int, default=200, help="HTTP status code")
    mp.add_argument("--content-type", default="application/json", help="Content-Type")
    mp = mock_sub.add_parser("clear", help="Remove mock(s)")
    mp.add_argument("pattern", nargs="?", default="", help="Pattern to clear (all if omitted)")
    mock_sub.add_parser("list", help="List current mocks")
    p.set_defaults(func=cmd_mock_d)

    # ── DOM snapshot ──
    p = sub.add_parser("snapshot", aliases=["snap"],
                        help="DOM snapshot & diff", parents=[common])
    snap_sub = p.add_subparsers(dest="snapshot_action", required=True)
    sp = snap_sub.add_parser("take", help="Capture current page state")
    sp.add_argument("name", help="Snapshot name")
    sp = snap_sub.add_parser("diff", help="Compare two snapshots")
    sp.add_argument("name1", help="First snapshot")
    sp.add_argument("name2", help="Second snapshot")
    sp = snap_sub.add_parser("delete", help="Delete a snapshot")
    sp.add_argument("name", help="Snapshot name")
    snap_sub.add_parser("list", help="List snapshots")
    p.set_defaults(func=cmd_snapshot_d)

    # ── Double-click ──
    p = sub.add_parser("dblclick", aliases=["dbl"],
                        help="Double-click element", parents=[common])
    p.add_argument("target", help="CSS selector")
    p.set_defaults(func=cmd_dblclick_d)

    # ── Select dropdown ──
    p = sub.add_parser("select", help="Select dropdown option", parents=[common])
    p.add_argument("selector", help="CSS selector for <select> element")
    p.add_argument("--value", "-v", help="Select by value")
    p.add_argument("--label", "-l", help="Select by label text")
    p.add_argument("--index", "-i", type=int, help="Select by index")
    p.set_defaults(func=cmd_select_d)

    # ── Checkbox/radio ──
    p = sub.add_parser("check", help="Toggle checkbox/radio", parents=[common])
    p.add_argument("selector", help="CSS selector")
    p.add_argument("--action", "-a", default="check",
                    choices=["check", "uncheck", "toggle"])
    p.set_defaults(func=cmd_check_d)

    # ── Input value ──
    p = sub.add_parser("input-value", aliases=["iv"],
                        help="Read input field value", parents=[common])
    p.add_argument("selector", help="CSS selector")
    p.set_defaults(func=cmd_input_value_d)

    # ── Element state ──
    p = sub.add_parser("element-state", aliases=["es"],
                        help="Query element state", parents=[common])
    p.add_argument("selector", help="CSS selector")
    p.set_defaults(func=cmd_element_state_d)

    # ── Bounding box ──
    p = sub.add_parser("bounding-box", aliases=["bb"],
                        help="Get element position/size", parents=[common])
    p.add_argument("selector", help="CSS selector")
    p.set_defaults(func=cmd_bounding_box_d)

    # ── Scroll to ──
    p = sub.add_parser("scroll-to", aliases=["st"],
                        help="Scroll element into view", parents=[common])
    p.add_argument("selector", help="CSS selector")
    p.add_argument("--block", default="center",
                    choices=["center", "start", "end", "nearest"])
    p.set_defaults(func=cmd_scroll_to_d)

    # ── Set content ──
    p = sub.add_parser("set-content", help="Load raw HTML", parents=[common])
    p.add_argument("html", help="HTML string to load")
    p.set_defaults(func=cmd_set_content_d)

    # ── Dialog handling ──
    p = sub.add_parser("dialog", help="Handle alert/confirm/prompt", parents=[common])
    dlg_sub = p.add_subparsers(dest="dialog_action", required=True)
    dp = dlg_sub.add_parser("accept", help="Accept dialogs (default)")
    dp.add_argument("--prompt-text", default="", help="Text for prompt responses")
    dlg_sub.add_parser("dismiss", help="Dismiss/cancel dialogs")
    dp = dlg_sub.add_parser("logs", help="Show captured dialogs")
    dp.add_argument("--limit", "-l", type=int, default=100)
    dp.add_argument("--clear", "-c", action="store_true")
    dlg_sub.add_parser("clear", help="Clear dialog logs")
    p.set_defaults(func=cmd_dialog_d)

    # ── Wait for response ──
    p = sub.add_parser("waitfor-response", aliases=["wr"],
                        help="Wait for network response", parents=[common])
    p.add_argument("pattern", help="URL substring to match")
    p.add_argument("--wait-timeout", "-t", type=int, default=10,
                    dest="wait_timeout", help="Timeout in seconds")
    p.set_defaults(func=cmd_waitfor_response_d)

    # ── Drag and drop ──
    p = sub.add_parser("drag", help="Drag element to target", parents=[common])
    p.add_argument("source", help="CSS selector of source element")
    p.add_argument("--target", "-t", help="CSS selector of target element")
    p.add_argument("--dx", type=int, default=0, help="X offset (pixels)")
    p.add_argument("--dy", type=int, default=0, help="Y offset (pixels)")
    p.set_defaults(func=cmd_drag_d)

    p = sub.add_parser("hover", help="Hover over element", parents=[common])
    p.add_argument("target", help="CSS selector")
    p.set_defaults(func=lambda a: send_simple_target(a, "hover"))

    p = sub.add_parser("title", help="Get page title", parents=[common])
    p.set_defaults(func=lambda a: send_simple(a, "title"))

    p = sub.add_parser("url", help="Get current URL", parents=[common])
    p.set_defaults(func=lambda a: send_simple(a, "url"))

    # ── Daemon-backed screenshots ──
    p = sub.add_parser("screenshot", aliases=["ss"], help="Take screenshot", parents=[common])
    p.add_argument("path", nargs="?", default="screenshot.png")
    p.add_argument("--full", "-f", action="store_true")
    p.set_defaults(func=cmd_screenshot_d)

    p = sub.add_parser("pdf", help="Export page as PDF", parents=[common])
    p.add_argument("path", nargs="?", default="page.pdf")
    p.add_argument("--landscape", action="store_true", help="Landscape orientation")
    p.add_argument("--scale", type=float, help="Scale 0.1-2.0")
    p.add_argument("--margin-top", type=float, help="Top margin (inches)")
    p.add_argument("--margin-right", type=float, help="Right margin (inches)")
    p.add_argument("--margin-bottom", type=float, help="Bottom margin (inches)")
    p.add_argument("--margin-left", type=float, help="Left margin (inches)")
    p.add_argument("--page-ranges", help="Page ranges (e.g. '1-3')")
    p.add_argument("--no-background", action="store_true", help="Skip background graphics")
    p.set_defaults(func=cmd_pdf_d)

    # ── Daemon-backed waits ──
    p = sub.add_parser("wait", help="Wait seconds", parents=[common])
    p.add_argument("seconds", type=float)
    p.set_defaults(func=cmd_wait_d)

    p = sub.add_parser("waitfor", help="Wait for selector", parents=[common])
    p.add_argument("selector")
    p.set_defaults(func=cmd_waitfor_d)

    # ── Daemon-backed cookies ──
    p = sub.add_parser("cookies", help="Manage cookies", parents=[common])
    p.add_argument("--save", help="Save to file")
    p.add_argument("--load", help="Load from file")
    p.add_argument("--clear", action="store_true")
    p.set_defaults(func=cmd_cookies_d)

    # ── Legacy commands ──
    p = sub.add_parser("navigate", aliases=["nav"],
                        help="Navigate (legacy, spawns browser)", parents=[common])
    p.add_argument("url")
    p.add_argument("--screenshot", "-s")
    p.add_argument("--text", "-t", action="store_true")
    p.add_argument("--cloudflare", "-cf", action="store_true")
    p.add_argument("--session")
    p.set_defaults(func=cmd_navigate)

    p = sub.add_parser("fingerprint", aliases=["fp"],
                        help="Check bot fingerprint", parents=[common])
    p.add_argument("--url", "-u")
    p.add_argument("--screenshot", "-s", default="fingerprint.png")
    p.set_defaults(func=cmd_fingerprint)

    p = sub.add_parser("device", aliases=["info"],
                        help="Show detected device info", parents=[common])
    p.set_defaults(func=cmd_device)

    p = sub.add_parser("kill", help="Force-kill everything", parents=[common])
    p.set_defaults(func=cmd_kill)

    args = parser.parse_args()

    # Resolve browser
    if args.browser == "auto":
        args.browser = "firefox"

    try:
        asyncio.run(args.func(args))
    except (ConnectionError, RuntimeError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)


async def cmd_pdf_d(args):
    """Export page as PDF (daemon-backed)."""
    from src.client import send_command
    path = os.path.abspath(getattr(args, "path", None) or "page.pdf")
    params = {"path": path}
    if getattr(args, "landscape", False):
        params["landscape"] = True
    if getattr(args, "scale", None) is not None:
        params["scale"] = args.scale
    if getattr(args, "margin_top", None) is not None:
        params["margin_top"] = args.margin_top
    if getattr(args, "margin_right", None) is not None:
        params["margin_right"] = args.margin_right
    if getattr(args, "margin_bottom", None) is not None:
        params["margin_bottom"] = args.margin_bottom
    if getattr(args, "margin_left", None) is not None:
        params["margin_left"] = args.margin_left
    if getattr(args, "page_ranges", None):
        params["page_ranges"] = args.page_ranges
    if getattr(args, "no_background", False):
        params["print_background"] = False
    resp = await send_command("pdf", params, browser=args.browser)
    if _json_mode(args):
        _output(args, resp)
    elif resp.get("success"):
        print(f"Saved: {resp['data']['path']}")
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_find_d(args):
    """Find elements by visible text (daemon-backed)."""
    from src.client import send_command
    params = {"text": args.text, "limit": args.limit}
    if args.role:
        params["role"] = args.role
    resp = await send_command("find", params, browser=args.browser)
    if _json_mode(args):
        _output(args, resp)
    elif resp.get("success"):
        elements = resp["data"].get("elements", [])
        if not elements:
            print("No elements found")
        else:
            for i, el in enumerate(elements, 1):
                print(f"{i}. [{el.get('role', '')}] \"{el.get('text', '')}\"")
                print(f"   selector: {el.get('selector', '')}")
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_tab_d(args):
    """Tab management (daemon-backed)."""
    from src.client import send_command
    action = f"tab_{args.tab_action}"
    params = {}
    if args.tab_action == "new" and getattr(args, "url", ""):
        params["url"] = args.url
    elif args.tab_action == "goto":
        params["index"] = args.index
    resp = await send_command(action, params, browser=args.browser)
    if _json_mode(args):
        _output(args, resp)
    elif resp.get("success"):
        d = resp["data"]
        if "title" in d:
            print(f"Title: {d['title']}")
        if "url" in d:
            print(f"URL: {d['url']}")
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_block_d(args):
    """Block URL patterns (daemon-backed)."""
    from src.client import send_command
    resp = await send_command("block", {"patterns": args.patterns},
                              browser=args.browser)
    if _json_mode(args):
        _output(args, resp)
    elif resp.get("success"):
        d = resp["data"]
        print(f"Added: {d.get('added', [])}")
        print(f"Blocked: {d.get('blocked', [])}")
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_unblock_d(args):
    """Unblock URL patterns (daemon-backed)."""
    from src.client import send_command
    resp = await send_command("unblock", {"patterns": args.patterns},
                              browser=args.browser)
    if _json_mode(args):
        _output(args, resp)
    elif resp.get("success"):
        d = resp["data"]
        print(f"Removed: {d.get('removed', [])}")
        print(f"Blocked: {d.get('blocked', [])}")
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_blocklist_d(args):
    """List blocked patterns (daemon-backed)."""
    from src.client import send_command
    resp = await send_command("blocklist", browser=args.browser)
    if _json_mode(args):
        _output(args, resp)
    elif resp.get("success"):
        patterns = resp["data"].get("patterns", [])
        if not patterns:
            print("No blocked patterns")
        else:
            for p in patterns:
                print(f"  - {p}")
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_macro_d(args):
    """Run macro from JSON file (daemon-backed)."""
    from src.client import send_command
    path = os.path.abspath(args.file)
    if not os.path.exists(path):
        print(f"Error: File not found: {path}", file=sys.stderr)
        sys.exit(1)
    with open(path) as f:
        steps = json.load(f)
    if not isinstance(steps, list):
        print("Error: Macro file must contain a JSON array of steps",
              file=sys.stderr)
        sys.exit(1)
    resp = await send_command("macro", {"steps": steps}, browser=args.browser,
                              timeout=300)
    if _json_mode(args):
        _output(args, resp)
    elif resp.get("success"):
        d = resp["data"]
        print(f"Completed: {d.get('completed', 0)}/{d.get('total', 0)} steps")
        for r in d.get("results", []):
            status = "OK" if r.get("success") else "FAIL"
            msg = r.get("error", "") if not r.get("success") else ""
            print(f"  Step {r['step']}: {status} {msg}")
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_console_d(args):
    """Console log capture (daemon-backed)."""
    from src.client import send_command
    action = args.console_action
    if action == "start":
        resp = await send_command("console_start", browser=args.browser)
    elif action == "stop":
        resp = await send_command("console_stop", browser=args.browser)
    elif action == "logs":
        params = {"limit": getattr(args, "limit", 100),
                  "clear": getattr(args, "clear", False)}
        resp = await send_command("console_logs", params, browser=args.browser)
    elif action == "clear":
        resp = await send_command("console_clear", browser=args.browser)
    else:
        resp = {"success": False, "error": f"Unknown action: {action}"}

    if _json_mode(args):
        _output(args, resp)
    elif resp.get("success"):
        d = resp["data"]
        if "enabled" in d:
            print("Console capture " + ("enabled" if d["enabled"] else "disabled"))
        elif "logs" in d:
            logs = d["logs"]
            if not logs:
                print("No console logs captured")
            else:
                for log in logs:
                    level = log.get("level", "log").upper()
                    msg = log.get("message", "")
                    print(f"[{level}] {msg}")
        elif "cleared" in d:
            print("Console logs cleared")
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_network_d(args):
    """Network request log (daemon-backed)."""
    from src.client import send_command
    action = args.network_action
    if action == "start":
        resp = await send_command("network_start", browser=args.browser)
    elif action == "stop":
        resp = await send_command("network_stop", browser=args.browser)
    elif action == "logs":
        params = {"limit": getattr(args, "limit", 100),
                  "clear": getattr(args, "clear", False)}
        resp = await send_command("network_logs", params, browser=args.browser)
    elif action == "clear":
        resp = await send_command("network_clear", browser=args.browser)
    else:
        resp = {"success": False, "error": f"Unknown action: {action}"}

    if _json_mode(args):
        _output(args, resp)
    elif resp.get("success"):
        d = resp["data"]
        if "enabled" in d:
            print("Network capture " + ("enabled" if d["enabled"] else "disabled"))
        elif "requests" in d:
            reqs = d["requests"]
            if not reqs:
                print("No network requests captured")
            else:
                for r in reqs:
                    size = r.get("size", 0)
                    dur = r.get("duration", 0)
                    print(f"  {r.get('type', ''):8s} {dur:5d}ms {size:>8d}B {r.get('url', '')[:80]}")
        elif "cleared" in d:
            print("Network log cleared")
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_observe_d(args):
    """DOM mutation observer (daemon-backed)."""
    from src.client import send_command
    action = args.observe_action
    if action == "start":
        resp = await send_command("observe_start", browser=args.browser)
    elif action == "stop":
        resp = await send_command("observe_stop", browser=args.browser)
    elif action == "logs":
        params = {"limit": getattr(args, "limit", 100),
                  "clear": getattr(args, "clear", False)}
        resp = await send_command("mutations", params, browser=args.browser)
    elif action == "clear":
        resp = await send_command("mutations_clear", browser=args.browser)
    else:
        resp = {"success": False, "error": f"Unknown action: {action}"}

    if _json_mode(args):
        _output(args, resp)
    elif resp.get("success"):
        d = resp["data"]
        if "enabled" in d:
            print("Mutation observer " + ("enabled" if d["enabled"] else "disabled"))
        elif "mutations" in d:
            muts = d["mutations"]
            if not muts:
                print("No mutations captured")
            else:
                for m in muts:
                    detail = ""
                    if m.get("type") == "childList":
                        detail = f"+{m.get('added', 0)} -{m.get('removed', 0)}"
                    elif m.get("type") == "attributes":
                        detail = m.get("attribute", "")
                    print(f"  {m.get('type', ''):14s} {m.get('target', ''):20s} {detail}")
        elif "cleared" in d:
            print("Mutations cleared")
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_screenshot_element_d(args):
    """Screenshot element (daemon-backed)."""
    from src.client import send_command
    path = os.path.abspath(getattr(args, "path", None) or "element.png")
    params = {"target": args.target, "path": path}
    resp = await send_command("screenshot_element", params, browser=args.browser)
    if _json_mode(args):
        _output(args, resp)
    elif resp.get("success"):
        print(f"Saved: {resp['data']['path']}")
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_drag_d(args):
    """Drag element (daemon-backed)."""
    from src.client import send_command
    params = {"source": args.source}
    if getattr(args, "target", None):
        params["target"] = args.target
    if getattr(args, "dx", 0):
        params["dx"] = args.dx
    if getattr(args, "dy", 0):
        params["dy"] = args.dy
    resp = await send_command("drag", params, browser=args.browser)
    if _json_mode(args):
        _output(args, resp)
    elif resp.get("success"):
        d = resp["data"]
        print(f"Dragged {args.source} to ({d['end_x']}, {d['end_y']})")
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_iframe_d(args):
    """Iframe operations (daemon-backed)."""
    from src.client import send_command
    action = args.iframe_action
    if action == "list":
        resp = await send_command("iframe_list", browser=args.browser)
    elif action == "eval":
        params = {"selector": args.selector, "expression": args.expression}
        resp = await send_command("iframe_eval", params, browser=args.browser)
    elif action == "text":
        params = {"selector": args.selector}
        if getattr(args, "inner", None):
            params["inner_selector"] = args.inner
        resp = await send_command("iframe_text", params, browser=args.browser)
    elif action == "click":
        params = {"selector": args.selector, "target": args.target}
        resp = await send_command("iframe_click", params, browser=args.browser)
    else:
        resp = {"success": False, "error": f"Unknown action: {action}"}

    if _json_mode(args):
        _output(args, resp)
    elif resp.get("success"):
        d = resp["data"]
        if "iframes" in d:
            iframes = d["iframes"]
            if not iframes:
                print("No iframes found")
            else:
                for f in iframes:
                    acc = "OK" if f.get("accessible") else "CROSS-ORIGIN"
                    print(f"  {f['index']}. [{acc}] {f.get('selector', '')}  src={f.get('src', '')[:60]}")
        elif "result" in d:
            result = d["result"]
            if isinstance(result, (dict, list)):
                print(json.dumps(result, indent=2))
            else:
                print(result)
        elif "text" in d:
            print(d["text"])
        elif "clicked" in d:
            print(f"Clicked {d['clicked']} in {d['iframe']}")
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_upload_d(args):
    """Upload file to input element (daemon-backed)."""
    from src.client import send_command
    path = os.path.abspath(args.path)
    params = {"selector": args.selector, "path": path}
    resp = await send_command("upload", params, browser=args.browser)
    if _json_mode(args):
        _output(args, resp)
    elif resp.get("success"):
        d = resp["data"].get("uploaded", {})
        print(f"Uploaded: {d.get('name', '')} ({d.get('size', 0)} bytes)")
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_geo_d(args):
    """Geolocation spoofing (daemon-backed)."""
    from src.client import send_command
    action = args.geo_action
    if action == "set":
        params = {"latitude": args.latitude, "longitude": args.longitude}
        if getattr(args, "accuracy", None) is not None:
            params["accuracy"] = args.accuracy
        resp = await send_command("geo_set", params, browser=args.browser)
    elif action == "clear":
        resp = await send_command("geo_clear", browser=args.browser)
    else:
        resp = {"success": False, "error": f"Unknown action: {action}"}

    if _json_mode(args):
        _output(args, resp)
    elif resp.get("success"):
        d = resp["data"]
        if "latitude" in d:
            print(f"Geolocation set: {d['latitude']}, {d['longitude']} (accuracy: {d['accuracy']}m)")
        elif "cleared" in d:
            print("Geolocation override cleared")
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_useragent_d(args):
    """User agent switching (daemon-backed)."""
    from src.client import send_command
    action = args.ua_action
    if action == "set":
        params = {"useragent": args.useragent}
        resp = await send_command("useragent_set", params, browser=args.browser)
    elif action == "clear":
        resp = await send_command("useragent_clear", browser=args.browser)
    else:
        resp = {"success": False, "error": f"Unknown action: {action}"}

    if _json_mode(args):
        _output(args, resp)
    elif resp.get("success"):
        d = resp["data"]
        if "useragent" in d:
            print(f"User agent set: {d['useragent'][:80]}")
        elif "cleared" in d:
            print("User agent override cleared")
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_cookie_set_d(args):
    """Set a cookie (daemon-backed)."""
    from src.client import send_command
    params = {"name": args.name, "value": args.value}
    if getattr(args, "domain", ""):
        params["domain"] = args.domain
    if getattr(args, "path", None):
        params["path"] = args.path
    if getattr(args, "max_age", None) is not None:
        params["max_age"] = args.max_age
    if getattr(args, "secure", False):
        params["secure"] = True
    if getattr(args, "samesite", ""):
        params["samesite"] = args.samesite
    resp = await send_command("cookie_set", params, browser=args.browser)
    if _json_mode(args):
        _output(args, resp)
    elif resp.get("success"):
        print(f"Cookie set: {args.name}")
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_storage_d(args):
    """Storage operations (daemon-backed)."""
    from src.client import send_command
    action = args.storage_action
    storage_type = getattr(args, "type", "local") or "local"
    params = {"type": storage_type, "action": action}
    if action in ("get", "set", "remove"):
        params["key"] = args.key
    if action == "set":
        params["value"] = args.value
    if action == "list":
        params["limit"] = getattr(args, "limit", 100)
    resp = await send_command("storage", params, browser=args.browser)
    if _json_mode(args):
        _output(args, resp)
    elif resp.get("success"):
        d = resp["data"]
        if "value" in d:
            print(f"{d.get('key', '')}: {d['value']}")
        elif "items" in d:
            items = d["items"]
            if not items:
                print(f"No {storage_type} storage items")
            else:
                for item in items:
                    print(f"  {item.get('key', ''):30s} = {str(item.get('value', ''))[:50]}")
        elif "set" in d:
            print(f"Set: {d['key']}")
        elif "removed" in d:
            print(f"Removed: {d['key']}")
        elif "cleared" in d:
            print(f"Cleared {d.get('type', 'local')} storage")
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_clipboard_d(args):
    """Clipboard operations (daemon-backed)."""
    from src.client import send_command
    action = args.clip_action
    if action == "read":
        resp = await send_command("clipboard_read", browser=args.browser)
    elif action == "write":
        resp = await send_command("clipboard_write", {"text": args.text},
                                  browser=args.browser)
    else:
        resp = {"success": False, "error": f"Unknown action: {action}"}

    if _json_mode(args):
        _output(args, resp)
    elif resp.get("success"):
        d = resp["data"]
        if "text" in d:
            print(d["text"])
        elif "written" in d:
            print(f"Written {d['written']} chars to clipboard")
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_form_fill_d(args):
    """Form auto-fill (daemon-backed)."""
    from src.client import send_command
    path = os.path.abspath(args.file)
    if not os.path.exists(path):
        print(f"Error: File not found: {path}", file=sys.stderr)
        sys.exit(1)
    with open(path) as f:
        fields = json.load(f)
    if not isinstance(fields, list):
        print("Error: File must contain a JSON array of {selector, value}",
              file=sys.stderr)
        sys.exit(1)
    resp = await send_command("form_fill", {"fields": fields},
                              browser=args.browser)
    if _json_mode(args):
        _output(args, resp)
    elif resp.get("success"):
        d = resp["data"]
        print(f"Filled: {d.get('filled', 0)}/{d.get('total', 0)} fields")
        for r in d.get("results", []):
            status = "OK" if r.get("success") else "FAIL"
            msg = r.get("error", "") if not r.get("success") else r.get("type", "")
            print(f"  {r.get('selector', ''):30s} {status} {msg}")
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_headers_d(args):
    """HTTP header injection (daemon-backed)."""
    from src.client import send_command
    action = args.headers_action
    if action == "set":
        headers = {}
        for pair in args.headers:
            if ":" not in pair:
                print(f"Error: Invalid header format '{pair}' (use Name:Value)",
                      file=sys.stderr)
                sys.exit(1)
            k, v = pair.split(":", 1)
            headers[k.strip()] = v.strip()
        resp = await send_command("headers_set", {"headers": headers},
                                  browser=args.browser)
    elif action == "clear":
        resp = await send_command("headers_clear", browser=args.browser)
    elif action == "list":
        resp = await send_command("headers_list", browser=args.browser)
    else:
        resp = {"success": False, "error": f"Unknown action: {action}"}

    if _json_mode(args):
        _output(args, resp)
    elif resp.get("success"):
        d = resp["data"]
        if "headers" in d:
            h = d["headers"]
            if not h:
                print("No custom headers set")
            else:
                for k, v in h.items():
                    print(f"  {k}: {v}")
        elif "cleared" in d:
            print("Custom headers cleared")
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_perf_d(args):
    """Page performance metrics (daemon-backed)."""
    from src.client import send_command
    resp = await send_command("perf", browser=args.browser)
    if _json_mode(args):
        _output(args, resp)
    elif resp.get("success"):
        d = resp["data"]
        print(f"URL: {d.get('url', '')}")
        print(f"First byte: {d.get('first_byte', 0)}ms")
        print(f"DOM interactive: {d.get('dom_interactive', 0)}ms")
        print(f"DOM content loaded: {d.get('dom_content_loaded', 0)}ms")
        print(f"Load complete: {d.get('load_complete', 0)}ms")
        print(f"DOM elements: {d.get('dom_elements', 0)}")
        print(f"Scripts: {d.get('scripts', 0)}")
        print(f"Stylesheets: {d.get('stylesheets', 0)}")
        print(f"Images: {d.get('images', 0)}")
        print(f"Iframes: {d.get('iframes', 0)}")
        mem = d.get("memory")
        if mem:
            print(f"JS heap: {mem.get('used', 0)}MB / {mem.get('total', 0)}MB "
                  f"(limit {mem.get('limit', 0)}MB)")
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_attr_d(args):
    """Element attribute operations (daemon-backed)."""
    from src.client import send_command
    action = args.attr_action
    if action == "get":
        params = {"selector": args.selector}
        if getattr(args, "name", None):
            params["name"] = args.name
        resp = await send_command("attr_get", params, browser=args.browser)
    elif action == "set":
        params = {"selector": args.selector, "name": args.name,
                  "value": args.value}
        resp = await send_command("attr_set", params, browser=args.browser)
    elif action == "remove":
        params = {"selector": args.selector, "name": args.name}
        resp = await send_command("attr_remove", params, browser=args.browser)
    else:
        resp = {"success": False, "error": f"Unknown action: {action}"}

    if _json_mode(args):
        _output(args, resp)
    elif resp.get("success"):
        d = resp["data"]
        if "attributes" in d:
            attrs = d["attributes"]
            if not attrs:
                print("No attributes")
            else:
                for k, v in attrs.items():
                    print(f"  {k}={v}")
        elif "removed" in d:
            print(f"Removed: {d['removed']} from {d['selector']}")
        elif "name" in d:
            if action == "get":
                print(f"{d.get('name', '')}: {d.get('value', '')}")
            else:
                print(f"Set {d.get('name', '')}={d.get('value', '')} on {d.get('selector', '')}")
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_profile_d(args):
    """Browser profile management (daemon-backed)."""
    from src.client import send_command
    action = args.profile_action
    if action == "save":
        resp = await send_command("profile_save", {"name": args.name},
                                  browser=args.browser)
    elif action == "load":
        resp = await send_command("profile_load", {"name": args.name},
                                  browser=args.browser)
    elif action == "list":
        resp = await send_command("profile_list", browser=args.browser)
    elif action == "delete":
        resp = await send_command("profile_delete", {"name": args.name},
                                  browser=args.browser)
    else:
        resp = {"success": False, "error": f"Unknown action: {action}"}

    if _json_mode(args):
        _output(args, resp)
    elif resp.get("success"):
        d = resp["data"]
        if "profiles" in d:
            profiles = d["profiles"]
            if not profiles:
                print("No saved profiles")
            else:
                for p in profiles:
                    print(f"  {p['name']:20s} {p.get('url', '')[:50]}")
        elif "deleted" in d:
            print(f"Deleted profile: {d['deleted']}")
        elif "cookies" in d:
            print(f"Profile '{d['name']}': {d.get('cookies', 0)} cookies, "
                  f"{d.get('storage_keys', 0)} storage keys")
            if "url" in d:
                print(f"  URL: {d['url']}")
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_search_d(args):
    """Page search (daemon-backed)."""
    from src.client import send_command
    action = args.search_action
    if action == "find":
        params = {"query": args.query}
        if getattr(args, "case_sensitive", False):
            params["case_sensitive"] = True
        resp = await send_command("search", params, browser=args.browser)
    elif action == "next":
        resp = await send_command("search_next", browser=args.browser)
    elif action == "prev":
        resp = await send_command("search_prev", browser=args.browser)
    elif action == "clear":
        resp = await send_command("search_clear", browser=args.browser)
    else:
        resp = {"success": False, "error": f"Unknown action: {action}"}

    if _json_mode(args):
        _output(args, resp)
    elif resp.get("success"):
        d = resp["data"]
        if "count" in d:
            print(f"Found: {d['count']} matches")
            if d["count"] > 0:
                print(f"Position: {d.get('index', 0) + 1}/{d['count']}")
        elif "cleared" in d:
            print("Search cleared")
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_shadow_d(args):
    """Shadow DOM operations (daemon-backed)."""
    from src.client import send_command
    action = args.shadow_action
    if action == "query":
        resp = await send_command("shadow_query", {"selector": args.selector},
                                  browser=args.browser)
    elif action == "text":
        resp = await send_command("shadow_text", {"selector": args.selector},
                                  browser=args.browser)
    elif action == "click":
        resp = await send_command("shadow_click", {"selector": args.selector},
                                  browser=args.browser)
    else:
        resp = {"success": False, "error": f"Unknown action: {action}"}

    if _json_mode(args):
        _output(args, resp)
    elif resp.get("success"):
        d = resp["data"]
        if "tag" in d:
            print(f"Tag: {d['tag']}")
            if d.get("id"):
                print(f"ID: {d['id']}")
            if d.get("text"):
                print(f"Text: {d['text'][:200]}")
        elif "text" in d:
            print(d["text"])
        elif "clicked" in d:
            print(f"Clicked: {d.get('selector', '')}")
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_responses_d(args):
    """Response body capture (daemon-backed)."""
    from src.client import send_command
    action = args.responses_action
    if action == "start":
        resp = await send_command("responses_start", browser=args.browser)
    elif action == "stop":
        resp = await send_command("responses_stop", browser=args.browser)
    elif action == "logs":
        params = {"limit": getattr(args, "limit", 100),
                  "clear": getattr(args, "clear", False)}
        resp = await send_command("responses_logs", params, browser=args.browser)
    elif action == "clear":
        resp = await send_command("responses_clear", browser=args.browser)
    else:
        resp = {"success": False, "error": f"Unknown action: {action}"}

    if _json_mode(args):
        _output(args, resp)
    elif resp.get("success"):
        d = resp["data"]
        if "enabled" in d:
            print("Response capture " + ("enabled" if d["enabled"] else "disabled"))
        elif "responses" in d:
            resps = d["responses"]
            if not resps:
                print("No responses captured")
            else:
                for r in resps:
                    body_preview = (r.get("body", "")[:60] or "").replace("\n", " ")
                    print(f"  {r.get('status', 0):3d} {r.get('size', 0):>8d}B "
                          f"{r.get('url', '')[:50]} {body_preview}")
        elif "cleared" in d:
            print("Response log cleared")
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_session_d(args):
    """Multi-tab session management (daemon-backed)."""
    from src.client import send_command
    action = args.session_action
    if action == "save":
        resp = await send_command("session_save", {"name": args.name},
                                  browser=args.browser, timeout=60)
    elif action == "load":
        resp = await send_command("session_load", {"name": args.name,
                                  "timeout": args.timeout},
                                  browser=args.browser, timeout=300)
    elif action == "list":
        resp = await send_command("session_list", browser=args.browser)
    elif action == "delete":
        resp = await send_command("session_delete", {"name": args.name},
                                  browser=args.browser)
    else:
        resp = {"success": False, "error": f"Unknown action: {action}"}

    if _json_mode(args):
        _output(args, resp)
    elif resp.get("success"):
        d = resp["data"]
        if "sessions" in d:
            sessions = d["sessions"]
            if not sessions:
                print("No saved sessions")
            else:
                for s in sessions:
                    print(f"  {s['name']:20s} {s.get('tabs', 0)} tabs")
        elif "deleted" in d:
            print(f"Deleted session: {d['deleted']}")
        elif "tabs_restored" in d:
            print(f"Restored {d['tabs_restored']}/{d['tabs_total']} tabs")
        elif "tabs" in d:
            print(f"Saved session '{d['name']}' with {d['tabs']} tabs")
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_css_d(args):
    """CSS injection (daemon-backed)."""
    from src.client import send_command
    action = args.css_action
    if action == "inject":
        css_text = args.css
        # If it's a .css file path, read it
        if css_text.endswith(".css") and os.path.isfile(css_text):
            with open(css_text) as f:
                css_text = f.read()
        params = {"css": css_text}
        if getattr(args, "id", None):
            params["id"] = args.id
        resp = await send_command("css_inject", params, browser=args.browser)
    elif action == "remove":
        params = {}
        if getattr(args, "id", None):
            params["id"] = args.id
        resp = await send_command("css_remove", params, browser=args.browser)
    elif action == "list":
        resp = await send_command("css_list", browser=args.browser)
    else:
        resp = {"success": False, "error": f"Unknown action: {action}"}

    if _json_mode(args):
        _output(args, resp)
    elif resp.get("success"):
        d = resp["data"]
        if "injected" in d:
            print(f"Injected CSS: {d.get('id', '')}")
        elif "removed" in d:
            r = d["removed"]
            if isinstance(r, list):
                print(f"Removed {len(r)} stylesheets")
            else:
                print(f"Removed: {r}")
        elif "styles" in d:
            styles = d["styles"]
            if not styles:
                print("No custom CSS injected")
            else:
                for s in styles:
                    print(f"  {s['id']:20s} {s['length']} chars")
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_waitact_d(args):
    """Wait+action (daemon-backed)."""
    from src.client import send_command
    params = {"selector": args.selector, "timeout": args.timeout}
    if getattr(args, "click", False):
        params["action"] = "click"
    elif getattr(args, "type_text", None):
        params["action"] = "type"
        params["value"] = args.type_text
    elif getattr(args, "get_text", False):
        params["action"] = "text"
    else:
        params["action"] = "click"
    resp = await send_command("waitact", params, browser=args.browser,
                              timeout=args.timeout + 15)
    if _json_mode(args):
        _output(args, resp)
    elif resp.get("success"):
        d = resp["data"]
        if "clicked" in d:
            print(f"Clicked: {args.selector}")
        elif "typed" in d:
            print(f"Typed into: {args.selector}")
        elif "text" in d:
            print(d["text"])
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_events_d(args):
    """Page event capture (daemon-backed)."""
    from src.client import send_command
    action = args.events_action
    if action == "start":
        params = {}
        if getattr(args, "types", None):
            params["types"] = args.types
        resp = await send_command("events_start", params, browser=args.browser)
    elif action == "stop":
        resp = await send_command("events_stop", browser=args.browser)
    elif action == "logs":
        params = {"limit": getattr(args, "limit", 100),
                  "clear": getattr(args, "clear", False)}
        resp = await send_command("events_logs", params, browser=args.browser)
    elif action == "clear":
        resp = await send_command("events_clear", browser=args.browser)
    else:
        resp = {"success": False, "error": f"Unknown action: {action}"}

    if _json_mode(args):
        _output(args, resp)
    elif resp.get("success"):
        d = resp["data"]
        if "enabled" in d:
            state = "enabled" if d["enabled"] else "disabled"
            types = d.get("types", [])
            print(f"Event capture {state}" +
                  (f": {', '.join(types)}" if types else ""))
        elif "events" in d:
            evts = d["events"]
            if not evts:
                print("No events captured")
            else:
                for e in evts:
                    extra = ""
                    if e.get("key"):
                        extra = f" key={e['key']}"
                    elif e.get("value"):
                        extra = f" val={e['value'][:30]}"
                    print(f"  {e.get('type', ''):10s} {e.get('target', ''):30s}{extra}")
        elif "cleared" in d:
            print("Events cleared")
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_viewport_d(args):
    """Viewport/window resize (daemon-backed)."""
    from src.client import send_command
    action = args.viewport_action
    if action == "set":
        params = {"width": args.width, "height": args.height}
        resp = await send_command("viewport_set", params, browser=args.browser)
    elif action == "get":
        resp = await send_command("viewport_get", browser=args.browser)
    else:
        resp = {"success": False, "error": f"Unknown action: {action}"}

    if _json_mode(args):
        _output(args, resp)
    elif resp.get("success"):
        d = resp["data"]
        if "inner_width" in d:
            print(f"Window: {d.get('width', d.get('window_width', 0))}x"
                  f"{d.get('height', d.get('window_height', 0))}")
            print(f"Viewport: {d.get('inner_width', 0)}x{d.get('inner_height', 0)}")
            if "device_pixel_ratio" in d:
                print(f"DPR: {d['device_pixel_ratio']}")
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_downloads_d(args):
    """List downloads (daemon-backed)."""
    from src.client import send_command
    resp = await send_command("downloads", browser=args.browser)
    if _json_mode(args):
        _output(args, resp)
    elif resp.get("success"):
        d = resp["data"]
        print(f"Directory: {d['dir']}")
        files = d.get("files", [])
        if not files:
            print("No downloads")
        else:
            for f in files:
                size = f["size"]
                if size >= 1024 * 1024:
                    size_str = f"{size / 1024 / 1024:.1f}MB"
                elif size >= 1024:
                    size_str = f"{size / 1024:.1f}KB"
                else:
                    size_str = f"{size}B"
                print(f"  {f['name']:40s} {size_str:>10s}")
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_highlight_d(args):
    """Manage element highlights (daemon-backed)."""
    from src.client import send_command
    action = args.highlight_action
    if action == "add":
        params = {"selector": args.selector, "color": args.color}
        if args.label:
            params["label"] = args.label
        resp = await send_command("highlight", params, browser=args.browser)
        if _json_mode(args):
            _output(args, resp)
        elif resp.get("success"):
            d = resp["data"]
            print(f"Highlighted {d['count']} elements ({d['color']}): {d['selector']}")
        else:
            print(f"Error: {resp.get('error')}", file=sys.stderr)
    elif action == "clear":
        params = {}
        if args.selector:
            params["selector"] = args.selector
        resp = await send_command("highlight_clear", params, browser=args.browser)
        if _json_mode(args):
            _output(args, resp)
        elif resp.get("success"):
            print(f"Cleared: {resp['data']['cleared']}")
        else:
            print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_auth_d(args):
    """Manage auth sessions (daemon-backed)."""
    from src.client import send_command
    action = args.auth_action
    if action == "save":
        resp = await send_command("auth_save", {"name": args.name},
                                  browser=args.browser)
        if _json_mode(args):
            _output(args, resp)
        elif resp.get("success"):
            d = resp["data"]
            print(f"Saved auth '{d['name']}': {d['cookies']} cookies for {d['domain']}")
        else:
            print(f"Error: {resp.get('error')}", file=sys.stderr)
    elif action == "load":
        resp = await send_command("auth_load", {"name": args.name},
                                  browser=args.browser)
        if _json_mode(args):
            _output(args, resp)
        elif resp.get("success"):
            d = resp["data"]
            print(f"Loaded auth '{d['name']}': {d['cookies_loaded']} cookies")
            print(f"URL: {d['url']}")
        else:
            print(f"Error: {resp.get('error')}", file=sys.stderr)
    elif action == "delete":
        resp = await send_command("auth_delete", {"name": args.name},
                                  browser=args.browser)
        if _json_mode(args):
            _output(args, resp)
        elif resp.get("success"):
            print(f"Deleted: {resp['data']['deleted']}")
        else:
            print(f"Error: {resp.get('error')}", file=sys.stderr)
    elif action == "list":
        resp = await send_command("auth_list", browser=args.browser)
        if _json_mode(args):
            _output(args, resp)
        elif resp.get("success"):
            sessions = resp["data"].get("sessions", [])
            if not sessions:
                print("No saved auth sessions")
            else:
                for s in sessions:
                    print(f"  {s['name']:20s} {s['domain']:30s} "
                          f"cookies={s['cookies']}")
        else:
            print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_throttle_d(args):
    """Manage network throttling (daemon-backed)."""
    from src.client import send_command
    action = args.throttle_action
    if action == "set":
        params = {}
        if getattr(args, "preset", None):
            params["preset"] = args.preset
        elif getattr(args, "latency", None) is not None:
            params["latency"] = args.latency
        else:
            print("Error: provide --preset or --latency", file=sys.stderr)
            return
        resp = await send_command("throttle_set", params, browser=args.browser)
        if _json_mode(args):
            _output(args, resp)
        elif resp.get("success"):
            d = resp["data"]
            print(f"Throttle: {d['preset']} ({d['latency']}ms)")
        else:
            print(f"Error: {resp.get('error')}", file=sys.stderr)
    elif action == "clear":
        resp = await send_command("throttle_clear", browser=args.browser)
        if _json_mode(args):
            _output(args, resp)
        elif resp.get("success"):
            print("Throttle cleared")
        else:
            print(f"Error: {resp.get('error')}", file=sys.stderr)
    elif action == "get":
        resp = await send_command("throttle_get", browser=args.browser)
        if _json_mode(args):
            _output(args, resp)
        elif resp.get("success"):
            d = resp["data"]
            print(f"Throttle: {d['preset']} ({d['latency']}ms)")
        else:
            print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_annotate_d(args):
    """Annotated screenshot with numbered element labels (daemon-backed)."""
    from src.client import send_command
    path = os.path.abspath(args.path)
    params = {"path": path, "max": args.max, "full": args.full}
    if args.selector:
        params["selector"] = args.selector
    resp = await send_command("screenshot_annotate", params, browser=args.browser)
    if _json_mode(args):
        _output(args, resp)
    elif resp.get("success"):
        d = resp["data"]
        print(f"Saved: {d['path']} ({d['elements']} elements)")
        for el in d.get("legend", []):
            print(f"  [{el['num']:2d}] {el['tag']:10s} {el['selector'][:50]}")
            if el.get("text"):
                print(f"       \"{el['text'][:50]}\"")
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_audit_d(args):
    """Page health audit (daemon-backed)."""
    from src.client import send_command
    resp = await send_command("audit", browser=args.browser)
    if _json_mode(args):
        _output(args, resp)
    elif resp.get("success"):
        d = resp["data"]
        print(f"Title: {d.get('title', '')}")
        print(f"URL: {d.get('url', '')}")
        print(f"Elements: {d.get('element_count', 0)}")
        print(f"Page size: {d.get('page_size', 0)} bytes")
        lt = d.get("load_time", -1)
        if lt >= 0:
            print(f"Load time: {lt}ms")
        links = d.get("links", {})
        print(f"Links: {links.get('total', 0)} (ext: {links.get('external', 0)}, "
              f"empty: {links.get('empty_href', 0)})")
        imgs = d.get("images", {})
        print(f"Images: {imgs.get('total', 0)} (no-alt: {imgs.get('missing_alt', 0)}, "
              f"broken: {imgs.get('broken', 0)})")
        h = d.get("headings", {})
        print(f"Headings: h1={h.get('h1', 0)} h2={h.get('h2', 0)} "
              f"h3={h.get('h3', 0)} h4={h.get('h4', 0)}")
        print(f"Forms: {d.get('forms', {}).get('total', 0)}")
        print(f"Console errors: {d.get('console_errors', 0)}")
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_mock_d(args):
    """Manage response mocks (daemon-backed)."""
    from src.client import send_command
    action = args.mock_action
    if action == "set":
        params = {
            "pattern": args.pattern,
            "body": args.body,
            "status": args.status,
            "content_type": args.content_type,
        }
        resp = await send_command("mock_set", params, browser=args.browser)
        if _json_mode(args):
            _output(args, resp)
        elif resp.get("success"):
            d = resp["data"]
            print(f"Mock set: {d['pattern']} ({d['mocks']} total)")
        else:
            print(f"Error: {resp.get('error')}", file=sys.stderr)
    elif action == "clear":
        params = {}
        if args.pattern:
            params["pattern"] = args.pattern
        resp = await send_command("mock_clear", params, browser=args.browser)
        if _json_mode(args):
            _output(args, resp)
        elif resp.get("success"):
            d = resp["data"]
            print(f"Cleared: {d['cleared']}")
        else:
            print(f"Error: {resp.get('error')}", file=sys.stderr)
    elif action == "list":
        resp = await send_command("mock_list", browser=args.browser)
        if _json_mode(args):
            _output(args, resp)
        elif resp.get("success"):
            mocks = resp["data"].get("mocks", [])
            if not mocks:
                print("No response mocks")
            else:
                for m in mocks:
                    print(f"  {m['pattern']:30s} → {m['status']} "
                          f"({m['content_type']})")
        else:
            print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_snapshot_d(args):
    """Manage DOM snapshots (daemon-backed)."""
    from src.client import send_command
    action = args.snapshot_action
    if action == "take":
        resp = await send_command("snapshot_take", {"name": args.name},
                                  browser=args.browser)
        if _json_mode(args):
            _output(args, resp)
        elif resp.get("success"):
            d = resp["data"]
            print(f"Snapshot '{d['name']}': {d['element_count']} elements, "
                  f"{d['text_length']} chars")
        else:
            print(f"Error: {resp.get('error')}", file=sys.stderr)
    elif action == "diff":
        resp = await send_command("snapshot_diff",
                                  {"name1": args.name1, "name2": args.name2},
                                  browser=args.browser)
        if _json_mode(args):
            _output(args, resp)
        elif resp.get("success"):
            d = resp["data"]
            print(f"Diff: {d['name1']} → {d['name2']}")
            if d.get("url_changed"):
                print(f"  URL: {d.get('url_before', '')} → {d.get('url_after', '')}")
            if d.get("title_changed"):
                print(f"  Title: {d.get('title_before', '')} → {d.get('title_after', '')}")
            delta = d.get("element_count_delta", 0)
            if delta:
                print(f"  Elements: {'+' if delta > 0 else ''}{delta}")
            if d.get("text_changed"):
                print(f"  Text changed: +{d.get('words_added', 0)} "
                      f"-{d.get('words_removed', 0)} words")
            fc = d.get("form_changes", [])
            if fc:
                print(f"  Form changes: {len(fc)}")
            if not any([d.get("url_changed"), d.get("title_changed"),
                       delta, d.get("text_changed"), fc]):
                print("  No changes detected")
        else:
            print(f"Error: {resp.get('error')}", file=sys.stderr)
    elif action == "delete":
        resp = await send_command("snapshot_delete", {"name": args.name},
                                  browser=args.browser)
        if _json_mode(args):
            _output(args, resp)
        elif resp.get("success"):
            print(f"Deleted: {resp['data']['deleted']}")
        else:
            print(f"Error: {resp.get('error')}", file=sys.stderr)
    elif action == "list":
        resp = await send_command("snapshot_list", browser=args.browser)
        if _json_mode(args):
            _output(args, resp)
        elif resp.get("success"):
            snaps = resp["data"].get("snapshots", [])
            if not snaps:
                print("No snapshots")
            else:
                for s in snaps:
                    print(f"  {s['name']:20s} {s['url'][:50]}")
        else:
            print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_dblclick_d(args):
    """Double-click element (daemon-backed)."""
    from src.client import send_command
    resp = await send_command("dblclick", {"target": args.target},
                              browser=args.browser)
    if _json_mode(args):
        _output(args, resp)
    elif resp.get("success"):
        print(f"Double-clicked: {resp['data']['dblclicked']}")
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_select_d(args):
    """Select dropdown option (daemon-backed)."""
    from src.client import send_command
    params = {"selector": args.selector}
    if args.value is not None:
        params["value"] = args.value
    elif args.label is not None:
        params["label"] = args.label
    elif args.index is not None:
        params["index"] = args.index
    else:
        print("Error: provide --value, --label, or --index", file=sys.stderr)
        return
    resp = await send_command("select", params, browser=args.browser)
    if _json_mode(args):
        _output(args, resp)
    elif resp.get("success"):
        d = resp["data"]
        print(f"Selected: {d.get('text', '')} (value={d.get('value', '')})")
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_check_d(args):
    """Toggle checkbox/radio (daemon-backed)."""
    from src.client import send_command
    resp = await send_command("check", {"selector": args.selector,
                                         "action": args.action},
                              browser=args.browser)
    if _json_mode(args):
        _output(args, resp)
    elif resp.get("success"):
        d = resp["data"]
        print(f"{d.get('action', 'check').capitalize()}ed: checked={d.get('checked')}")
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_input_value_d(args):
    """Read input field value (daemon-backed)."""
    from src.client import send_command
    resp = await send_command("input_value", {"selector": args.selector},
                              browser=args.browser)
    if _json_mode(args):
        _output(args, resp)
    elif resp.get("success"):
        d = resp["data"]
        print(f"Value: {d.get('value', '')}")
        print(f"Tag: {d.get('tag', '')} Type: {d.get('type', '')}")
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_element_state_d(args):
    """Query element state (daemon-backed)."""
    from src.client import send_command
    resp = await send_command("element_state", {"selector": args.selector},
                              browser=args.browser)
    if _json_mode(args):
        _output(args, resp)
    elif resp.get("success"):
        d = resp["data"]
        if not d.get("exists"):
            print("Element not found")
        else:
            flags = []
            if d.get("visible"):
                flags.append("visible")
            if d.get("enabled"):
                flags.append("enabled")
            if d.get("checked"):
                flags.append("checked")
            if d.get("editable"):
                flags.append("editable")
            print(f"State: {', '.join(flags) or 'none'}")
            print(f"Tag: {d.get('tag', '')} Type: {d.get('type', '')}")
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_bounding_box_d(args):
    """Get element bounding box (daemon-backed)."""
    from src.client import send_command
    resp = await send_command("bounding_box", {"selector": args.selector},
                              browser=args.browser)
    if _json_mode(args):
        _output(args, resp)
    elif resp.get("success"):
        d = resp["data"]
        print(f"Position: ({d.get('x', 0)}, {d.get('y', 0)})")
        print(f"Size: {d.get('width', 0)}x{d.get('height', 0)}")
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_scroll_to_d(args):
    """Scroll element into view (daemon-backed)."""
    from src.client import send_command
    resp = await send_command("scroll_to", {"selector": args.selector,
                                             "block": args.block},
                              browser=args.browser)
    if _json_mode(args):
        _output(args, resp)
    elif resp.get("success"):
        print(f"Scrolled to: {args.selector}")
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_set_content_d(args):
    """Load raw HTML content (daemon-backed)."""
    from src.client import send_command
    resp = await send_command("set_content", {"html": args.html},
                              browser=args.browser)
    if _json_mode(args):
        _output(args, resp)
    elif resp.get("success"):
        d = resp["data"]
        print(f"Loaded: {d.get('length', 0)} bytes, title: {d.get('title', '')}")
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_dialog_d(args):
    """Manage dialog handling (daemon-backed)."""
    from src.client import send_command
    action = args.dialog_action
    if action == "accept":
        params = {"accept": True}
        if args.prompt_text:
            params["prompt_text"] = args.prompt_text
        resp = await send_command("dialog_handle", params, browser=args.browser)
        if _json_mode(args):
            _output(args, resp)
        elif resp.get("success"):
            print("Dialog handler: accept")
        else:
            print(f"Error: {resp.get('error')}", file=sys.stderr)
    elif action == "dismiss":
        resp = await send_command("dialog_dismiss", browser=args.browser)
        if _json_mode(args):
            _output(args, resp)
        elif resp.get("success"):
            print("Dialog handler: dismiss")
        else:
            print(f"Error: {resp.get('error')}", file=sys.stderr)
    elif action == "logs":
        params = {"limit": args.limit}
        if args.clear:
            params["clear"] = True
        resp = await send_command("dialog_logs", params, browser=args.browser)
        if _json_mode(args):
            _output(args, resp)
        elif resp.get("success"):
            dialogs = resp["data"].get("dialogs", [])
            if not dialogs:
                print("No dialogs captured")
            else:
                for d in dialogs:
                    print(f"  [{d.get('type', '?')}] {d.get('message', '')[:80]}")
        else:
            print(f"Error: {resp.get('error')}", file=sys.stderr)
    elif action == "clear":
        resp = await send_command("dialog_clear", browser=args.browser)
        if _json_mode(args):
            _output(args, resp)
        elif resp.get("success"):
            print("Dialog logs cleared")
        else:
            print(f"Error: {resp.get('error')}", file=sys.stderr)


async def cmd_waitfor_response_d(args):
    """Wait for network response (daemon-backed)."""
    from src.client import send_command
    resp = await send_command("waitfor_response",
                              {"pattern": args.pattern, "timeout": args.wait_timeout},
                              browser=args.browser)
    if _json_mode(args):
        _output(args, resp)
    elif resp.get("success"):
        d = resp["data"]
        r = d.get("response", {})
        print(f"Matched: {r.get('url', '')[:80]}")
        print(f"Status: {r.get('status', '?')}")
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)


async def send_simple(args, action):
    """Send simple daemon command with no extra params."""
    from src.client import send_command
    resp = await send_command(action, browser=args.browser)
    if _json_mode(args):
        _output(args, resp)
    elif resp.get("success"):
        d = resp["data"]
        if "title" in d:
            print(f"Title: {d['title']}")
        if "url" in d:
            print(f"URL: {d['url']}")
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)


async def send_simple_target(args, action):
    """Send daemon command with a target parameter."""
    from src.client import send_command
    resp = await send_command(action, {"target": args.target},
                              browser=args.browser)
    if _json_mode(args):
        _output(args, resp)
    elif resp.get("success"):
        print(f"{action.capitalize()}: {args.target}")
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)


if __name__ == "__main__":
    main()
