# Termux Browser Pilot v0.17.1

Real browser automation for Termux/Android. No root required.

Firefox (default) or Chromium on Xvfb — runs entirely on your phone.
Firefox passes Cloudflare natively via TLS fingerprint. Chromium uses
CDP stealth. Both use real device hardware info.

## Features

- **Persistent daemon** — browser stays running, sub-second commands
- **Passes Cloudflare** — Firefox: instant TLS-based pass; Chromium: Turnstile handler
- **No automation framework** — Firefox controlled via xdotool + clipboard JS execution
- **AI-friendly** — `--json` flag on all commands for structured output
- **Real device fingerprint** — auto-detects screen, GPU, CPU, arch, model
- **Human-like input** — Bezier curve mouse, realistic typing, clipboard paste
- **Session persistence** — auto-save/load cookies across restarts
- **Cookie management** — get/set/save/load, Netscape format import/export
- **Network tracking** — capture all request URLs, types, sizes (Chromium only)
- **Screenshots** — viewport or full page PNG, PDF export
- **Accessibility tree** — ARIA roles and names
- **Cross-device** — auto-adapts to any Android device's real specs
- **Smart element targeting** — find clickable elements by visible text
- **Idle timeout** — auto-shutdown daemon after configurable inactivity
- **Tab management** — open, close, switch between browser tabs
- **Proxy support** — HTTP/SOCKS5 proxy for Firefox and Chromium
- **Request interception** — block URLs/domains via JS fetch/XHR hooks
- **Multi-step macros** — chain commands into reusable JSON scripts
- **Console log capture** — capture console.log/warn/error/info output
- **Download management** — auto-download files to ~/.tbp/downloads/
- **Network request log** — capture URLs, types, durations, sizes
- **DOM mutation observer** — watch for element changes in real-time
- **Element screenshot** — capture specific elements by CSS selector
- **Drag and drop** — smooth mouse drag between elements or by offset
- **Iframe support** — list, eval, text, click inside same-origin iframes
- **File upload** — set files on input[type=file] via JS DataTransfer
- **Geolocation spoofing** — override navigator.geolocation coordinates
- **User agent switching** — optional override (default: real browser UA)
- **Cookie injection** — set cookies with domain, path, secure, SameSite
- **Local/session storage** — get, set, remove, clear, list items
- **Clipboard access** — read/write Xvfb system clipboard
- **Form auto-fill** — fill all form fields from JSON spec
- **Lightweight** — no Puppeteer/Playwright dependency

## Quick Start

### Install

```bash
bash setup.sh

# Or manually:
pkg install tur-repo x11-repo
pkg install firefox xorg-server-xvfb xdotool xclip openbox python3
pip install websockets  # only needed for Chromium mode
```

### CLI (Daemon Mode — Recommended)

Browser starts once, stays running. Commands are instant.

```bash
# Navigate (auto-starts daemon on first command)
tbp goto https://example.com
tbp goto https://audiogames.net -cf    # Cloudflare bypass

# Read content
tbp text                               # Page text
tbp links                              # All links
tbp eval "document.title"              # Run JavaScript
tbp a11y                               # Accessibility tree

# Interact
tbp click "button.submit"              # Click element
tbp type "input[name=q]" "hello"       # Type into field
tbp press Enter                        # Press key
tbp scroll --down                      # Scroll page
tbp find "Sign In"                     # Find elements by text
tbp find "Submit" --role button        # Filter by role

# Tab management
tbp tab new                            # Open new tab
tbp tab new https://example.com        # New tab with URL
tbp tab close                          # Close current tab
tbp tab next                           # Next tab
tbp tab prev                           # Previous tab
tbp tab goto 2                         # Switch to tab 2

# Block requests
tbp block ads.example.com tracker.js   # Block URL patterns
tbp blocklist                          # List blocked patterns
tbp unblock ads.example.com            # Unblock pattern

# Macros (JSON scripts)
tbp macro workflow.json                # Run multi-step macro

# Console log capture
tbp console start                      # Start capturing logs
tbp console stop                       # Stop capturing logs
tbp console logs                       # Show captured logs
tbp console logs --clear               # Show and clear buffer
tbp console clear                      # Clear log buffer

# Downloads
tbp downloads                          # List downloaded files

# Network request log
tbp network start                      # Start logging requests
tbp network stop                       # Stop logging requests
tbp network logs                       # Show captured requests
tbp network logs --clear               # Show and clear
tbp network clear                      # Clear request log

# DOM mutation observer
tbp observe start                      # Start watching mutations
tbp observe stop                       # Stop watching mutations
tbp observe logs                       # Show captured mutations
tbp observe clear                      # Clear mutations

# Element screenshot
tbp screenshot-element "div.hero"      # Screenshot specific element
tbp screenshot-element "#logo" logo.png

# Drag and drop
tbp drag ".item" --target ".dropzone"  # Drag to element
tbp drag ".slider" --dx 100            # Drag by offset

# Iframe support
tbp iframe list                        # List all iframes
tbp iframe eval "iframe#f1" "document.title"  # JS in iframe
tbp iframe text "iframe#f1"            # Get iframe text
tbp iframe click "iframe#f1" "button"  # Click in iframe

# File upload
tbp upload "input[type=file]" photo.jpg  # Upload file (max 5MB)

# Geolocation spoofing
tbp geo set 40.7128 -74.0060           # Set to New York
tbp geo set 51.5074 -0.1278 -a 50      # London, 50m accuracy
tbp geo clear                           # Clear override

# User agent switching (default: real browser UA — override is optional)
tbp useragent set "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
tbp ua clear                            # Restore real UA
# Note: Overrides JS-side navigator.userAgent only.
# HTTP User-Agent header stays real (requires restart to change).

# Cookie injection
tbp cookie-set session_id abc123          # Simple cookie
tbp cs token xyz --domain .example.com    # With domain
tbp cs pref dark --max-age 86400 --secure # Secure + 1 day

# Local/session storage
tbp storage list                          # List localStorage
tbp storage get mykey                     # Get value
tbp storage set mykey myvalue             # Set value
tbp storage remove mykey                  # Remove key
tbp storage clear                         # Clear all
tbp storage list --type session           # sessionStorage

# Clipboard
tbp clipboard read                        # Read clipboard
tbp clip write "Hello world"              # Write to clipboard

# Form auto-fill (JSON file: [{selector, value}, ...])
tbp form-fill form_data.json              # Fill all fields

# Proxy support (on start)
tbp start --proxy socks5://127.0.0.1:1080

# Screenshots
tbp screenshot page.png
tbp screenshot page.png --full         # Full page

# AI-friendly JSON output
tbp goto https://example.com --json
tbp text --json
tbp links --json

# Session management
tbp status                             # Daemon status
tbp cookies --save session.json        # Save cookies
tbp cookies --load session.json        # Load cookies
tbp stop                               # Graceful shutdown
```

### Python API

```python
import asyncio
from src.pilot import Pilot

async def main():
    # Firefox (default) — passes Cloudflare natively
    async with Pilot() as p:
        await p.goto_cf("https://audiogames.net")
        print(await p.title())
        await p.screenshot("page.png")

    # Chromium mode with session persistence
    async with Pilot(browser="chromium", session_file="session.json") as p:
        await p.goto("https://example.com")
        await p.type("input[name=q]", "hello")

asyncio.run(main())
```

### Daemon Client (Python)

```python
from src.client import send_command
import asyncio

async def main():
    # Auto-starts daemon if not running
    r = await send_command("goto", {"url": "https://example.com"})
    print(r["data"]["title"])

    r = await send_command("text")
    print(r["data"]["text"])

    r = await send_command("screenshot", {"path": "shot.png"})

asyncio.run(main())
```

### MCP Server (for Claude Code / AI Agents)

```bash
# Register with Claude Code
claude mcp add tbp -- tbp-mcp

# Or use .mcp.json (already included in repo)
# Tools: browser_goto, browser_text, browser_click, browser_type,
#        browser_screenshot, browser_a11y, browser_links, browser_eval,
#        browser_scroll, browser_hover, browser_press, browser_find,
#        browser_tab_*, browser_block, browser_unblock, browser_blocklist,
#        browser_macro, browser_wait, browser_wait_for, browser_cookies_*,
#        browser_console_*, browser_downloads, browser_network_*,
#        browser_observe_*, browser_mutations*, browser_screenshot_element,
#        browser_drag, browser_iframe_*, browser_upload, browser_geo_*,
#        browser_useragent_*, browser_cookie_set, browser_storage_*,
#        browser_clipboard_*, browser_form_fill, browser_status
```

Requires: `pip install "mcp[cli]>=1.0"` (or `pip install termux-browser-pilot[mcp]`).

## Feature Reference

### Navigation & Content
| Command | Description |
|---------|-------------|
| `goto URL [-cf]` | Navigate to URL. `-cf` for Cloudflare bypass |
| `back` / `forward` / `reload` | History navigation |
| `text [--selector S]` | Get visible page text |
| `html [--selector S]` | Get page HTML |
| `links [--limit N]` | List all links |
| `eval EXPRESSION` | Execute JavaScript |
| `title` / `url` | Get page title/URL |
| `find TEXT [--role R]` | Find interactive elements by visible text |
| `a11y` | Accessibility tree (ARIA roles and names) |

### Interaction
| Command | Description |
|---------|-------------|
| `click SELECTOR [--human]` | Click element. `--human` for Bezier curve |
| `type SELECTOR TEXT` | Type text into input |
| `press KEY` | Press keyboard key (Enter, Tab, Escape...) |
| `scroll [--up] [--amount N]` | Scroll page |
| `hover SELECTOR` | Hover over element |
| `drag SOURCE [--target T] [--dx N --dy N]` | Drag element |

### Tab Management
| Command | Description |
|---------|-------------|
| `tab new [URL]` | Open new tab |
| `tab close` | Close current tab |
| `tab next` / `tab prev` | Switch tabs |
| `tab goto INDEX` | Switch to tab 1-9 |

### Screenshots & Export
| Command | Description |
|---------|-------------|
| `screenshot [PATH] [--full]` | Screenshot (viewport or full page) |
| `screenshot-element SELECTOR [PATH]` | Screenshot specific element |
| `pdf [PATH]` | Export page as PDF |

### Request Blocking
| Command | Description |
|---------|-------------|
| `block PATTERNS...` | Block URL patterns (domains/substrings) |
| `unblock PATTERNS...` | Remove blocked patterns |
| `blocklist` | List current blocked patterns |

### Console Log Capture
| Command | Description |
|---------|-------------|
| `console start` | Start capturing console.log/warn/error/info |
| `console stop` | Stop capturing (disables re-injection) |
| `console logs [--clear] [--limit N]` | Show captured logs |
| `console clear` | Clear log buffer |

### Network Request Log
Uses PerformanceObserver (passive, no conflict with URL blocker).

| Command | Description |
|---------|-------------|
| `network start` | Start logging network requests |
| `network stop` | Stop logging |
| `network logs [--clear] [--limit N]` | Show requests (URL, type, duration, size) |
| `network clear` | Clear request log |

### DOM Mutation Observer
| Command | Description |
|---------|-------------|
| `observe start` | Start watching DOM mutations |
| `observe stop` | Stop watching |
| `observe logs [--clear] [--limit N]` | Show mutations (childList, attributes, etc.) |
| `observe clear` | Clear mutation buffer |

### Iframe Support
Works with same-origin iframes only. Cross-origin iframes are listed but inaccessible.

| Command | Description |
|---------|-------------|
| `iframe list` | List all iframes (index, src, accessibility) |
| `iframe eval SELECTOR EXPRESSION` | Execute JS inside iframe |
| `iframe text SELECTOR [--inner S]` | Get text from iframe |
| `iframe click SELECTOR TARGET` | Click element inside iframe |

### File Upload
Sets files on `<input type="file">` elements via JS DataTransfer API. Max 5MB.

| Command | Description |
|---------|-------------|
| `upload SELECTOR PATH` | Upload file to file input |

### Geolocation Spoofing
Overrides `navigator.geolocation.getCurrentPosition` and `watchPosition`.
Re-injected automatically after page navigation. Saves/restores originals on clear.

| Command | Description |
|---------|-------------|
| `geo set LAT LNG [-a ACCURACY]` | Set geolocation (accuracy default: 100m) |
| `geo clear` | Restore real geolocation behavior |

### User Agent Switching
**Default: real browser user agent.** Override is optional and JS-side only
(`navigator.userAgent`). HTTP User-Agent header sent to servers stays real.
Re-injected after navigation. Restored on clear.

| Command | Description |
|---------|-------------|
| `useragent set UA_STRING` | Override navigator.userAgent |
| `useragent clear` / `ua clear` | Restore real user agent |

### Cookie Injection
Sets cookies via `document.cookie`. Cannot set HttpOnly cookies (browser security).
Values are URL-encoded automatically. Name must not contain `=`, `;`, or newlines.

| Command | Description |
|---------|-------------|
| `cookie-set NAME VALUE [--domain D] [--path P]` | Set cookie |
| Options: `--max-age N`, `--secure`, `--samesite Strict\|Lax\|None` | |

### Local/Session Storage
| Command | Description |
|---------|-------------|
| `storage list [--type local\|session]` | List all items |
| `storage get KEY [--type ...]` | Get value by key |
| `storage set KEY VALUE [--type ...]` | Set key-value pair |
| `storage remove KEY [--type ...]` | Remove key |
| `storage clear [--type ...]` | Clear all items |

### Clipboard Access
Reads/writes the Xvfb virtual display clipboard (not the phone clipboard).
Useful for reading after a page copy, or setting before a Ctrl+V paste.

| Command | Description |
|---------|-------------|
| `clipboard read` | Read clipboard text |
| `clipboard write TEXT` | Write text to clipboard |

### Form Auto-fill
Fills multiple fields from a JSON file. Handles text inputs, selects,
checkboxes, and radios. Dispatches `input` + `change` events (React/Vue compatible).

```json
[
  {"selector": "#name", "value": "John Doe"},
  {"selector": "#email", "value": "john@example.com"},
  {"selector": "select#country", "value": "US"},
  {"selector": "#agree", "value": true}
]
```

| Command | Description |
|---------|-------------|
| `form-fill FILE` / `ff FILE` | Fill fields from JSON file (max 100) |

### CSS Injection
Inject, remove, and list custom stylesheets. Persists across scrolling and
re-injects after navigation.

| Command | Description |
|---------|-------------|
| `css inject "RULES" [--id NAME]` | Inject CSS (text or file path) |
| `css remove [--id NAME]` | Remove stylesheet (or all) |
| `css list` | List injected stylesheets |

**Example:** `tbp css inject "body{background:#111;color:#eee}" --id darkmode`

### Wait+Action
Wait for an element to appear in the DOM, then automatically perform an action.
Uses MutationObserver for efficient detection.

| Command | Description |
|---------|-------------|
| `waitact SELECTOR [--click]` | Wait then click (default) |
| `waitact SELECTOR --type TEXT` | Wait then type |
| `waitact SELECTOR --text` | Wait then get text |

**Timeout:** Default 10s, max 120s. Uses `--timeout` flag.

### Page Event Capture
Capture DOM events (click, submit, input, change, keydown) with target element info.
Re-injects after navigation automatically.

| Command | Description |
|---------|-------------|
| `events start [--types click submit ...]` | Start capturing events |
| `events stop` | Stop and remove listeners |
| `events logs [--limit N] [--clear]` | Show captured events |
| `events clear` | Clear event log |

### Viewport/Window Resize
Control browser window dimensions for responsive testing.

| Command | Description |
|---------|-------------|
| `viewport set WIDTH HEIGHT` / `vp set` | Resize window |
| `viewport get` / `vp get` | Get current dimensions |

Returns both window size (xdotool) and inner viewport (JS). Range: 100-7680 x 100-4320.

### Page Search
Find text on the page with visual highlighting. Navigate between matches.

| Command | Description |
|---------|-------------|
| `search find "text" [--case-sensitive]` | Find and highlight all matches |
| `search next` | Go to next match |
| `search prev` | Go to previous match |
| `search clear` | Clear highlights |

Highlights persist across scrolling. Current match shown in orange, others in yellow.
Re-injects after navigation if search is active.

### Shadow DOM Access
Query, read, and click elements inside shadow DOM boundaries.
Recursively traverses all shadow roots to find elements.

| Command | Description |
|---------|-------------|
| `shadow query SELECTOR` | Find element (returns tag, id, text, attributes) |
| `shadow text SELECTOR` | Get text content from shadow element |
| `shadow click SELECTOR` | Click element inside shadow root |

### Response Body Capture
Capture fetch/XHR response bodies for API inspection and debugging.
Bodies truncated to 10KB per response, max 500 entries.

| Command | Description |
|---------|-------------|
| `responses start` | Start capturing response bodies |
| `responses stop` | Stop capturing |
| `responses logs [--limit N] [--clear]` | Show captured responses |
| `responses clear` | Clear response log |

### Multi-tab Session Management
Save and restore all open browser tabs as named sessions.
Sessions stored in `~/.tbp/sessions/{name}.json`.

| Command | Description |
|---------|-------------|
| `session save NAME` | Save all open tabs |
| `session load NAME` | Restore tabs from session |
| `session list` | List saved sessions |
| `session delete NAME` | Delete a session |

**Note:** Tab detection uses Ctrl+1-9 shortcuts (max 9 tabs).

### HTTP Header Injection
Inject custom headers into all fetch() and XMLHttpRequest calls. Headers persist
across requests and re-inject after navigation. Useful for auth tokens, API keys,
or custom tracking headers.

| Command | Description |
|---------|-------------|
| `headers set Name:Value [...]` | Set one or more custom headers |
| `headers list` | List active custom headers |
| `headers clear` | Remove all custom headers and restore original fetch/XHR |

**Note:** Headers are injected JS-side only (via fetch/XHR interception). They do
not affect initial page load requests or non-JS resource fetches.

### Page Performance Metrics
Get timing and resource metrics from the Performance API.

| Command | Description |
|---------|-------------|
| `perf` | Show page performance metrics |

Returns: first byte time, DOM interactive, DOM content loaded, load complete,
DOM element count, script/stylesheet/image/iframe counts, JS heap memory usage.

### Element Attributes
Read, set, or remove HTML attributes on elements.

| Command | Description |
|---------|-------------|
| `attr get SELECTOR [--name NAME]` | Get one or all attributes |
| `attr set SELECTOR NAME VALUE` | Set an attribute |
| `attr remove SELECTOR NAME` | Remove an attribute |

### Browser Profile Management
Save and restore browser state (cookies + localStorage + URL) as named profiles.
Profiles are stored in `~/.tbp/profiles/<name>/`.

| Command | Description |
|---------|-------------|
| `profile save NAME` | Save current state as named profile |
| `profile load NAME` | Load saved profile (cookies + localStorage) |
| `profile list` | List all saved profiles |
| `profile delete NAME` | Delete a saved profile |

**Profile names:** Alphanumeric, hyphens, and underscores only.

### Element Highlight
Visual debugging — outline matching elements with colored borders.

| Command | Description |
|---------|-------------|
| `highlight add SELECTOR [--color red] [--label TEXT]` | Highlight elements |
| `highlight clear [SELECTOR]` | Remove highlights (all or specific) |

Highlights persist across navigation (re-injected after `goto`).

### PDF Export Options
Extended PDF export with layout control.

| Option | Description |
|--------|-------------|
| `--landscape` | Landscape orientation |
| `--scale 0.1-2.0` | Scale factor |
| `--margin-top/right/bottom/left N` | Margins in inches |
| `--page-ranges '1-3'` | Specific pages |
| `--no-background` | Skip background graphics |

Example: `tbp pdf report.pdf --landscape --scale 0.8 --margin-top 0.5`

### Cookie Auth Sessions
Save/restore authentication via cookies — auto-navigates to saved URL.

| Command | Description |
|---------|-------------|
| `auth save NAME` | Save cookies + URL as auth session |
| `auth load NAME` | Restore cookies and navigate to saved URL |
| `auth list` | List saved auth sessions |
| `auth delete NAME` | Delete auth session |

Sessions stored in `~/.tbp/auth/` with `0o600` permissions.

### Network Throttling
Simulate slow connections by adding latency to fetch/XHR requests.

| Command | Description |
|---------|-------------|
| `throttle set --preset 3g` | Apply throttle preset |
| `throttle set --latency 500` | Custom latency (ms) |
| `throttle clear` | Remove throttling |
| `throttle get` | Show current config |

Presets: `3g` (400ms), `slow-3g` (2000ms), `fast-3g` (150ms), `offline` (reject all).

### Annotated Screenshot (AI-First)
Screenshot with numbered badges on interactive elements + legend mapping numbers to CSS selectors.

| Command | Description |
|---------|-------------|
| `annotate [--path FILE] [--selector CSS] [--max N]` | Labeled screenshot |

AI workflow: `annotate` → read legend → `click` using selector from legend.

### Page Audit
One-command structured page health report.

| Command | Description |
|---------|-------------|
| `audit` | Full page health report |

Returns: elements, links (total/external/broken), images (alt/broken), forms, headings, meta, scripts, page size, load time, console errors.

### Response Mocking
Return fake responses for matching URL patterns. Useful for testing without real backends.

| Command | Description |
|---------|-------------|
| `mock set PATTERN BODY [--status N] [--content-type CT]` | Add mock |
| `mock clear [PATTERN]` | Remove mock(s) |
| `mock list` | List active mocks |

Mocks persist across navigation (re-injected after `goto`). Max 50 mocks.

### DOM Snapshot & Diff
Save page state, perform actions, then diff to verify changes.

| Command | Description |
|---------|-------------|
| `snapshot take NAME` | Capture page state |
| `snapshot diff NAME1 NAME2` | Compare two snapshots |
| `snapshot list` | List snapshots |
| `snapshot delete NAME` | Delete snapshot |

AI workflow: `snapshot take before` → actions → `snapshot take after` → `snapshot diff before after`.

### Double-click
| Command | Description |
|---------|-------------|
| `dblclick SELECTOR` / `dbl` | Double-click element (dispatches dblclick event) |
| `dblclick SELECTOR --human` | Use xdotool double-click |

### Select Dropdown
| Command | Description |
|---------|-------------|
| `select SELECTOR --value V` | Select option by value |
| `select SELECTOR --label L` | Select option by visible text |
| `select SELECTOR --index N` | Select option by index |

Dispatches change + input events for React/Vue compatibility.

### Check / Uncheck
| Command | Description |
|---------|-------------|
| `check SELECTOR` | Check a checkbox or radio |
| `check SELECTOR --action uncheck` | Uncheck |
| `check SELECTOR --action toggle` | Toggle |

### Input Value
| Command | Description |
|---------|-------------|
| `input-value SELECTOR` / `iv` | Read current value of input/select/textarea |

Returns `{value, tag, type}`.

### Element State
| Command | Description |
|---------|-------------|
| `element-state SELECTOR` / `es` | Query element visibility/enabled/checked state |

Returns `{visible, enabled, checked, editable, tag, type}`.

### Bounding Box
| Command | Description |
|---------|-------------|
| `bounding-box SELECTOR` / `bb` | Get element position and dimensions |

Returns `{x, y, width, height, top, left, bottom, right, scrollX, scrollY}`.

### Scroll To Element
| Command | Description |
|---------|-------------|
| `scroll-to SELECTOR` / `st` | Scroll element into view (default: center) |
| `scroll-to SELECTOR --block start` | Align to top |

Block options: center, start, end, nearest.

### Set Page Content
| Command | Description |
|---------|-------------|
| `set-content HTML` | Replace page with raw HTML (no navigation) |
| `set-content @file.html` | Load HTML from file |

### Dialog Handling (alert/confirm/prompt)
Auto-handle browser dialogs without blocking. Monkey-patches window.alert/confirm/prompt.
Re-injects after navigation.

| Command | Description |
|---------|-------------|
| `dialog handle` | Enable auto-accept for dialogs |
| `dialog handle --reject` | Auto-dismiss dialogs |
| `dialog handle --prompt-text "answer"` | Set prompt response |
| `dialog dismiss` | Shortcut for reject mode |
| `dialog logs [--limit N] [--clear]` | Show captured dialog messages |
| `dialog clear` | Clear dialog log |

### Wait for Response
Wait for a fetch/XHR response matching a URL pattern.

| Command | Description |
|---------|-------------|
| `waitfor-response PATTERN` / `wr` | Wait for matching response |
| `waitfor-response PATTERN --timeout 30` | Custom timeout (1-120s) |

Enables response capture automatically if not already active.

### Multi-step Macros
Chain commands into reusable JSON scripts (max 100 steps).

| Command | Description |
|---------|-------------|
| `macro FILE` | Run macro from JSON file |

### Session & Lifecycle
| Command | Description |
|---------|-------------|
| `start [--proxy URL] [--idle-timeout N]` | Start daemon |
| `stop` | Graceful shutdown |
| `status` | Show daemon status (PID, URL, uptime) |
| `cookies [--save F] [--load F] [--clear]` | Manage cookies |
| `downloads` / `dl` | List downloaded files |
| `kill` | Force-kill everything |

## Design Philosophy

**Be real, not fake.** Firefox runs as a normal browser — no geckodriver,
no Marionette, no WebDriver flags. Controlled via xdotool (X11 native
input) and clipboard-based JS execution through the Web Console.
Chromium uses real device hardware info with only `webdriver=false` hidden.

## Architecture

```
CLI (cli.py) ──Unix socket──→ Daemon (daemon.py)
                                ├── Pilot (unified API)
                                │    ├── BrowserPilot        Xvfb + openbox WM + browser lifecycle
                                │    ├── NativeFirefoxSession xdotool + clipboard JS (persistent profile)
                                │    ├── CDPSession          WebSocket CDP (Chromium)
                                │    ├── PageCommands        navigate, eval, text/html/links
                                │    ├── InputCommands       click, type, scroll, Bezier mouse
                                │    ├── ScreenshotCommands  PNG, full-page, PDF
                                │    ├── CookieCommands      get/set/save/load/export/import
                                │    ├── NetworkTracker      request capture (Chromium only)
                                │    ├── Accessibility       a11y tree
                                │    └── CloudflareHandler   Turnstile (Chromium only)
                                ├── DeviceInfo              auto-detect screen/GPU/CPU/model
                                └── Stealth                 CDP-level UA/platform (Chromium)
```

## Auto-Detected Device Info

| Property | Source | Example |
|----------|--------|---------|
| Model | `getprop ro.product.model` | SM-S918B |
| Screen | DPI ratio + model DB | 1080x2316 |
| GPU | `/sys/kernel/gpu/gpu_model` | Adreno740v2 |
| CPU cores | `os.cpu_count()` | 8 |
| RAM | `/proc/meminfo` | 11GB |
| Architecture | `uname -m` | aarch64 |

## Test Results (2026-02-24)

All 5 test sites pass with Firefox (default):

| Site | Result | Notes |
|------|--------|-------|
| example.com | PASS | Navigation, JS eval, links, text, scroll |
| bot.sannysoft.com | PASS | Navigation, scroll |
| audiogames.net | PASS | CF bypassed via TLS fingerprint |
| nowsecure.nl | PASS | CF bypassed via TLS fingerprint |
| platform.openai.com | PASS | Form filling, clipboard paste input |

### Firefox vs Chromium

| Feature | Firefox | Chromium |
|---------|---------|----------|
| Cloudflare | Instant (TLS) | Turnstile handler |
| audiogames.net | PASS | FAIL |
| Network tracking | Not available | Full CDP tracking |
| JS execution | Console + clipboard | CDP Runtime.evaluate |
| Stealth needed | None | UA + WebGL + canvas |

## File Structure

```
termux-browser-pilot/
├── cli.py                 # CLI entry point (daemon + legacy commands)
├── setup.sh               # One-command installer
├── pyproject.toml          # Python packaging
├── src/
│   ├── pilot.py           # Main unified API + session persistence
│   ├── daemon.py          # Persistent browser daemon (Unix socket)
│   ├── client.py          # Daemon client (send commands)
│   ├── native.py          # Firefox: xdotool + clipboard JS execution
│   ├── browser.py         # Xvfb + browser lifecycle
│   ├── cdp.py             # CDP WebSocket client (Chromium)
│   ├── commands.py        # Page commands (navigate, eval, text, html)
│   ├── input.py           # Keyboard/mouse + Bezier human-like input
│   ├── screenshot.py      # Screenshot/PDF capture
│   ├── stealth.py         # Anti-detection (Chromium only)
│   ├── cookies.py         # Cookie management + Netscape import/export
│   ├── cloudflare.py      # CF Turnstile handler (Chromium)
│   ├── network.py         # Network tracking (Chromium only)
│   ├── accessibility.py   # A11y tree access
│   ├── device.py          # Real device detection
│   ├── gpu.py             # WebGL renderer spoofing
│   ├── lock.py            # Session lock (single-instance)
│   ├── _utils.py          # Shared utilities (JS escaping, binary checks)
│   └── mcp_server.py      # MCP server for Claude Code integration
└── .mcp.json              # Claude Code auto-discovery config
```

## Requirements

- Termux on Android (aarch64)
- Firefox mode: `firefox`, `xorg-server-xvfb`, `xdotool`, `xclip`, `openbox`, `python3`
- Chromium mode: `chromium`, `xorg-server-xvfb`, `mesa`, `python3`, `websockets>=12.0`
- `openbox` (lightweight WM) is required for reliable keyboard shortcut routing in Xvfb

## License

MIT
