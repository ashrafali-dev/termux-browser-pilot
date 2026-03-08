"""Browser and Xvfb lifecycle management."""

import asyncio
import atexit
import json
import logging
import os
import shutil
import tempfile
import urllib.error
import urllib.request

# Track temp dirs for crash cleanup
_temp_dirs_to_clean = set()


def _atexit_cleanup():
    """Clean up temp Chrome user-data-dirs on unclean exit."""
    for d in list(_temp_dirs_to_clean):
        try:
            shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass


atexit.register(_atexit_cleanup)


logger = logging.getLogger(__name__)

# Allow forcing single-process for devices where multi-process fails
_FORCE_SINGLE_PROCESS = os.environ.get(
    "TBP_SINGLE_PROCESS", ""
).lower() in ("1", "true", "yes")

# Default config
XVFB_DISPLAY = ":99"
XVFB_RESOLUTION = "1920x1080x24"
CDP_PORT = 9222
CHROMIUM_BIN = shutil.which("chromium-browser") or shutil.which("chromium") or "chromium-browser"
# Chromium flags for Termux — minimal set to avoid automation fingerprint
CHROMIUM_BASE_FLAGS = [
    # Required for Termux (no root, no namespaces, no /dev/shm)
    "--no-sandbox",
    "--no-zygote",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    # Single-process only if forced (causes OOM + CF detection issues)
    *(["--single-process"] if _FORCE_SINGLE_PROCESS else []),
    # Suppress "unsupported flag" info bar (avoids visible automation signal)
    "--test-type",
    # Anti-detection: prevents navigator.webdriver=true
    "--disable-blink-features=AutomationControlled",
    # Normal browser behavior
    "--start-maximized",
    "--lang=en-US,en",
    "--js-flags=--max-old-space-size=1024",
    # Safe stability flags (low detection risk)
    "--disable-breakpad",
    "--disable-component-update",
]


def _get_gl_flags(gpu_mode):
    """Return Chrome GL flags based on GPU rendering mode."""
    if gpu_mode == "virgl":
        # Use ANGLE with native GL backend for virgl GPU passthrough.
        # --use-gl=egl crashes in single-process; --use-angle=gl works.
        return [
            "--enable-webgl",
            "--use-gl=angle",
            "--use-angle=gl",
        ]
    else:
        # Fallback: SwiftShader software rendering
        return [
            "--enable-webgl",
            "--use-gl=angle",
            "--use-angle=swiftshader-webgl",
        ]


class BrowserPilot:
    """Manages Xvfb and browser (Chromium or Firefox) lifecycle."""

    def __init__(self, display=XVFB_DISPLAY, cdp_port=CDP_PORT,
                 headless_xvfb=True, chromium_bin=CHROMIUM_BIN,
                 window_size="1920,1080", user_data_dir=None,
                 gpu_mode="auto", browser_type="chromium", proxy=None):
        self.display = display
        self.cdp_port = cdp_port
        self.headless_xvfb = headless_xvfb
        self.chromium_bin = chromium_bin
        self.window_size = window_size
        self.browser_type = browser_type  # "chromium" or "firefox"
        self._gpu_mode = gpu_mode  # "auto", "virgl", "swiftshader"
        self._proxy = proxy  # Proxy URL for Chromium --proxy-server flag
        self._virgl = None
        self._xvfb_proc = None
        self._wm_proc = None  # Window manager (openbox)
        self._chrome_proc = None
        self._ws_url = None
        self._user_data_dir = None
        self._external_user_data_dir = user_data_dir  # Persistent profile
        self._owns_user_data_dir = False  # Whether we should clean it up

    async def start(self):
        """Start Xvfb + browser. Returns WS URL (Chromium) or None (Firefox)."""
        from ._utils import require_binaries
        require_binaries("Xvfb")
        if self.browser_type != "firefox":
            if not shutil.which(self.chromium_bin):
                raise RuntimeError(
                    f"Chromium not found at '{self.chromium_bin}'. "
                    f"Install with: pkg install chromium"
                )

        if self.headless_xvfb:
            await self._start_xvfb()

        if self.browser_type == "firefox":
            # Firefox: no geckodriver needed — NativeSession handles launch
            return None

        # Chromium path
        await self._setup_gpu()
        await self._start_chromium()
        self._ws_url = await self._wait_for_cdp()
        return self._ws_url

    async def _setup_gpu(self):
        """Resolve GPU rendering mode (auto-detect best available)."""
        from .gpu import VirglManager

        if self._gpu_mode == "swiftshader":
            return  # Explicitly requested software rendering

        self._virgl = VirglManager()
        if self._gpu_mode in ("auto", "virgl"):
            if self._virgl.is_available():
                started = await self._virgl.start()
                if started:
                    self._gpu_mode = "virgl"
                    logger.info("GPU: virgl (hardware-accelerated)")
                    return
                else:
                    logger.warning("Virgl failed to start, falling back")
            elif self._gpu_mode == "virgl":
                logger.warning(
                    "virglrenderer-android not installed. "
                    "Install with: pkg install virglrenderer-android"
                )

        self._gpu_mode = "swiftshader"
        self._virgl = None
        logger.info("GPU: SwiftShader (software rendering)")

    async def _start_xvfb(self):
        """Launch Xvfb virtual display (non-blocking)."""
        # Kill any existing Xvfb on this display
        proc = await asyncio.create_subprocess_exec(
            "pkill", "-f", f"Xvfb {self.display}( |$)",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        await asyncio.sleep(0.3)

        # Clean stale Xvfb lock files (left after OOM kills/crashes)
        display_num = self.display.lstrip(":")
        for stale in (f"/tmp/.X{display_num}-lock",
                      f"/tmp/.X11-unix/X{display_num}"):
            try:
                os.unlink(stale)
            except (FileNotFoundError, OSError):
                pass

        w, h = self.window_size.split(",")
        resolution = f"{w}x{h}x24"
        self._xvfb_proc = await asyncio.create_subprocess_exec(
            "Xvfb", self.display, "-screen", "0", resolution,
            "-ac", "-nolisten", "tcp",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        os.environ["DISPLAY"] = self.display
        await asyncio.sleep(0.5)

        # Start a lightweight window manager (required for window
        # minimize/activate/raise operations used by DevTools management).
        # Kill any existing openbox first.
        proc = await asyncio.create_subprocess_exec(
            "pkill", "-f", f"openbox.*{self.display}",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        env = os.environ.copy()
        env["DISPLAY"] = self.display
        openbox_bin = shutil.which("openbox")
        if openbox_bin:
            self._wm_proc = await asyncio.create_subprocess_exec(
                openbox_bin,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
            )
            await asyncio.sleep(0.3)
            logger.info("Window manager started (openbox, PID %d)",
                        self._wm_proc.pid)
        else:
            self._wm_proc = None
            logger.warning("openbox not found — window management may not work")

        if self._xvfb_proc.returncode is not None:
            raise RuntimeError("Xvfb failed to start")

        # Start lightweight WM (needed for keyboard shortcut routing)
        if shutil.which("openbox"):
            self._wm_proc = await asyncio.create_subprocess_exec(
                "openbox", env={**os.environ, "DISPLAY": self.display},
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.sleep(0.3)

    async def _start_chromium(self):
        """Launch Chromium with CDP enabled (non-blocking).

        Uses multi-process mode by default. If Chromium crashes within 3s,
        auto-retries with --single-process as fallback.
        """
        env = os.environ.copy()
        env["DISPLAY"] = self.display

        if self._gpu_mode == "virgl" and self._virgl:
            env.update(self._virgl.get_env())
            env.pop("LIBGL_ALWAYS_SOFTWARE", None)
        else:
            env["LIBGL_ALWAYS_SOFTWARE"] = "1"

        # Use persistent profile if provided, otherwise temp dir
        if self._external_user_data_dir:
            self._user_data_dir = self._external_user_data_dir
            os.makedirs(self._user_data_dir, exist_ok=True)
            self._owns_user_data_dir = False
        else:
            self._user_data_dir = tempfile.mkdtemp(prefix="tbp_chrome_")
            _temp_dirs_to_clean.add(self._user_data_dir)
            self._owns_user_data_dir = True

        if _FORCE_SINGLE_PROCESS:
            logger.warning(
                "Running in single-process mode (TBP_SINGLE_PROCESS=1). "
                "OOM risk higher; CF detection stricter."
            )

        await self._launch_chromium(env, extra_flags=[])

        # Auto-retry with --single-process if multi-process crashed quickly
        if self._chrome_proc.returncode is not None and not _FORCE_SINGLE_PROCESS:
            logger.warning(
                "Multi-process Chromium crashed. Retrying with --single-process."
            )
            self._clear_profile_locks()

            # First try: keep current GPU mode with single-process
            await self._launch_chromium(env, extra_flags=["--single-process"])

            # If virgl+single-process also failed, fall back to swiftshader
            if self._chrome_proc.returncode is not None and \
               self._gpu_mode == "virgl":
                logger.warning(
                    "Virgl+single-process failed. Falling back to SwiftShader."
                )
                self._gpu_mode = "swiftshader"
                env["LIBGL_ALWAYS_SOFTWARE"] = "1"
                env.pop("GALLIUM_DRIVER", None)
                env.pop("MESA_GL_VERSION_OVERRIDE", None)
                if self._virgl:
                    await self._virgl.stop()
                    self._virgl = None
                self._clear_profile_locks()
                await self._launch_chromium(
                    env, extra_flags=["--single-process"])

            if self._chrome_proc.returncode is not None:
                raise RuntimeError("Chromium failed to start (all modes)")

    def _clear_profile_locks(self):
        """Remove Chromium lock files left by a crashed process."""
        if not self._user_data_dir:
            return
        for lock_name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            lock_path = os.path.join(self._user_data_dir, lock_name)
            try:
                os.remove(lock_path)
            except FileNotFoundError:
                pass
            except OSError as e:
                logger.debug("Could not remove %s: %s", lock_name, e)

    async def _launch_chromium(self, env, extra_flags=None):
        """Launch Chromium subprocess."""
        gl_flags = _get_gl_flags(self._gpu_mode)
        proxy_flags = []
        if self._proxy:
            proxy_flags.append(f"--proxy-server={self._proxy}")

        args = [
            self.chromium_bin,
            f"--remote-debugging-port={self.cdp_port}",
            "--remote-debugging-address=127.0.0.1",
            f"--window-size={self.window_size}",
            f"--user-data-dir={self._user_data_dir}",
            *CHROMIUM_BASE_FLAGS,
            *gl_flags,
            *proxy_flags,
            *(extra_flags or []),
            "about:blank",
        ]

        self._chrome_proc = await asyncio.create_subprocess_exec(
            *args, env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.sleep(3)  # Give multi-process time to spawn renderers

    async def _wait_for_cdp(self, timeout=20):
        """Wait for CDP endpoint to be ready and return WS URL."""
        url = f"http://127.0.0.1:{self.cdp_port}/json/version"
        deadline = asyncio.get_running_loop().time() + timeout

        def _fetch():
            try:
                with urllib.request.urlopen(url, timeout=2) as resp:
                    return json.loads(resp.read()).get("webSocketDebuggerUrl", "")
            except (urllib.error.URLError, ConnectionRefusedError, OSError):
                return ""
            except Exception as e:
                logger.debug("Unexpected error polling CDP: %s", e)
                return ""

        while asyncio.get_running_loop().time() < deadline:
            ws_url = await asyncio.to_thread(_fetch)
            if ws_url:
                return ws_url
            await asyncio.sleep(0.5)

        raise TimeoutError(
            f"CDP not ready after {timeout}s on port {self.cdp_port}"
        )

    @property
    def ws_url(self):
        return self._ws_url

    async def stop(self):
        """Shut down Chromium and Xvfb gracefully (non-blocking, robust).

        Uses SIGTERM first, gives processes time to flush state, then SIGKILL.
        Cleans up temporary user-data-dir afterward.
        """
        for proc in (self._chrome_proc, self._wm_proc, self._xvfb_proc):
            if proc and proc.returncode is None:
                try:
                    proc.terminate()
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
                except Exception as e:
                    logger.warning("Error stopping process: %s", e)
        self._chrome_proc = None
        self._wm_proc = None
        self._xvfb_proc = None

        # Stop virgl server
        if self._virgl:
            try:
                await self._virgl.stop()
            except Exception as e:
                logger.debug("Error stopping virgl: %s", e)
            self._virgl = None

        # Clean up temp profile (not persistent ones)
        if self._owns_user_data_dir and self._user_data_dir and \
           os.path.isdir(self._user_data_dir):
            _temp_dirs_to_clean.discard(self._user_data_dir)
            try:
                await asyncio.to_thread(
                    shutil.rmtree, self._user_data_dir, ignore_errors=True
                )
            except Exception as e:
                logger.debug("Error cleaning user-data-dir: %s", e)
        self._user_data_dir = None

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *exc):
        await self.stop()
