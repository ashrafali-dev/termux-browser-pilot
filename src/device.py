"""Detect real device information from the system.

Pulls actual hardware specs: screen resolution, GPU, CPU, RAM, etc.
Works cross-device on any Android running Termux (no root required).
"""

import os
import subprocess
import threading


def _run(cmd):
    """Run a command and return stripped output."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
        return r.stdout.strip()
    except Exception:
        return ""


def _read_file(path):
    """Read a sysfs/proc file."""
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return ""


def _getprop(key):
    """Read an Android system property."""
    return _run(["getprop", key])


def _detect_screen():
    """Detect real screen resolution from Android system.

    Strategy: Use DPI ratio (native vs current) + model database.
    Falls back to DPI-based estimation if model unknown.
    """
    model = _getprop("ro.product.model")
    native_dpi_str = _getprop("ro.sf.init.lcd_density")
    current_dpi_str = _getprop("ro.sf.lcd_density")
    native_dpi = int(native_dpi_str) if native_dpi_str.isdigit() else 0
    current_dpi = int(current_dpi_str) if current_dpi_str.isdigit() else 0

    # Known device database (model -> native width x height at native DPI)
    # Landscape width, portrait height (as Android reports)
    DEVICES = {
        # Samsung Galaxy S series
        "SM-S918B": (1440, 3088),  # S24 Ultra
        "SM-S928B": (1440, 3120),  # S25 Ultra
        "SM-S926B": (1440, 3120),  # S25+
        "SM-S921B": (1080, 2340),  # S25
        "SM-S916B": (1440, 3088),  # S23 Ultra (same as S918B)
        "SM-S911B": (1080, 2340),  # S23
        "SM-S908B": (1440, 3088),  # S22 Ultra
        # Samsung Galaxy A series
        "SM-A556B": (1080, 2340),  # A55
        "SM-A546B": (1080, 2340),  # A54
        # Google Pixel
        "Pixel 9 Pro": (1344, 2992),
        "Pixel 9": (1080, 2424),
        "Pixel 8 Pro": (1344, 2992),
        "Pixel 8": (1080, 2400),
        "Pixel 7 Pro": (1440, 3120),
        "Pixel 7": (1080, 2400),
        # OnePlus
        "CPH2581": (1440, 3168),  # OnePlus 12
        "CPH2449": (1440, 3216),  # OnePlus 11
    }

    # Try exact model match
    if model in DEVICES:
        native_w, native_h = DEVICES[model]
    else:
        # Try partial match (e.g., SM-S918 matches SM-S918B)
        native_w, native_h = None, None
        for key, val in DEVICES.items():
            if model and key.startswith(model[:7]):
                native_w, native_h = val
                break

    if native_w and native_h:
        # If device is running at lower DPI, scale down
        if native_dpi and current_dpi and current_dpi < native_dpi:
            scale = current_dpi / native_dpi
            return int(native_w * scale), int(native_h * scale)
        return native_w, native_h

    # Fallback: estimate from DPI
    # Common baseline: 160dpi = mdpi = ~320x480 base
    dpi = current_dpi or native_dpi or 400
    if dpi >= 560:
        return 1440, 3088  # xxxhdpi (flagship)
    elif dpi >= 400:
        return 1080, 2340  # xxhdpi (high-end)
    elif dpi >= 320:
        return 1080, 1920  # xhdpi (mid-range)
    else:
        return 720, 1280   # hdpi (budget)


def _detect_gpu():
    """Detect real GPU information from sysfs."""
    gpu = {}

    # Qualcomm Adreno (most Samsung/OnePlus/Pixel devices)
    model = _read_file("/sys/kernel/gpu/gpu_model")
    if not model:
        model = _read_file("/sys/class/kgsl/kgsl-3d0/gpu_model")
    if model:
        gpu["model"] = model
        gpu["vendor"] = "Qualcomm"
        max_clk = _read_file("/sys/kernel/gpu/gpu_max_clock")
        if not max_clk:
            max_clk = _read_file("/sys/class/kgsl/kgsl-3d0/max_clock_mhz")
        if max_clk:
            gpu["max_clock_mhz"] = max_clk

    # Mali (MediaTek/Exynos)
    if not model:
        mali_model = _read_file("/sys/class/misc/mali0/device/gpuinfo")
        if not mali_model:
            mali_model = _read_file("/sys/devices/platform/mali/gpuinfo")
        if mali_model:
            gpu["model"] = mali_model
            gpu["vendor"] = "ARM"

    # Fallback: getprop
    if not gpu.get("model"):
        vulkan = _getprop("ro.hardware.vulkan")
        if vulkan:
            gpu["vendor"] = vulkan.capitalize()
            gpu["model"] = vulkan

    # OpenGL ES version
    gles_raw = _getprop("ro.opengles.version")
    if gles_raw:
        try:
            v = int(gles_raw)
            major = (v >> 16) & 0xFF
            minor = v & 0xFF
            gpu["gles_version"] = f"{major}.{minor}"
        except ValueError:
            pass

    # SoC platform
    platform = _getprop("ro.board.platform")
    if platform:
        gpu["soc_platform"] = platform

    return gpu


def get_device_info():
    """Collect real device information from the system."""
    info = {}

    # Architecture
    info["arch"] = os.uname().machine

    # Android device
    info["model"] = _getprop("ro.product.model") or "Unknown"
    info["brand"] = (_getprop("ro.product.brand") or "Unknown").capitalize()
    info["manufacturer"] = _getprop("ro.product.manufacturer") or ""
    info["android_version"] = _getprop("ro.build.version.release") or ""
    info["build_id"] = _getprop("ro.build.display.id") or ""
    info["sdk"] = _getprop("ro.build.version.sdk") or ""

    # Screen (real resolution from device)
    ndpi = _getprop("ro.sf.init.lcd_density")
    cdpi = _getprop("ro.sf.lcd_density")
    info["native_dpi"] = int(ndpi) if ndpi.isdigit() else 0
    info["current_dpi"] = int(cdpi) if cdpi.isdigit() else 0
    screen_w, screen_h = _detect_screen()
    info["screen_width"] = screen_w
    info["screen_height"] = screen_h

    # GPU (real hardware)
    info["gpu"] = _detect_gpu()

    # CPU
    info["cores"] = os.cpu_count() or 4

    # RAM (GB)
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal"):
                    kb = int(line.split()[1])
                    info["ram_gb"] = round(kb / 1024 / 1024)
                    break
    except Exception:
        info["ram_gb"] = 4

    # Chromium version
    try:
        r = _run(["chromium-browser", "--version"])
        parts = r.split() if r else []
        info["chrome_version"] = parts[-1] if parts else "138.0.0.0"
    except Exception:
        info["chrome_version"] = "138.0.0.0"

    return info


def get_platform_string(info=None):
    """Return navigator.platform matching real architecture."""
    if info is None:
        info = get_device_info()
    arch = info["arch"]
    platform_map = {
        "aarch64": "Linux aarch64",
        "armv7l": "Linux armv7l",
        "x86_64": "Linux x86_64",
        "i686": "Linux i686",
    }
    return platform_map.get(arch, f"Linux {arch}")


def get_user_agent(info=None):
    """Build a realistic user-agent matching the real device."""
    if info is None:
        info = get_device_info()
    ver = info["chrome_version"]
    arch = info["arch"]
    return (
        f"Mozilla/5.0 (X11; Linux {arch}) "
        f"AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{ver} Safari/537.36"
    )


# Thread-safe cached device info
_cached_info = None
_cache_lock = threading.Lock()


def device_info():
    """Get cached device info (thread-safe)."""
    global _cached_info
    if _cached_info is None:
        with _cache_lock:
            if _cached_info is None:
                _cached_info = get_device_info()
    return _cached_info


def print_device_info():
    """Print device info summary (for CLI diagnostics)."""
    info = device_info()
    print(f"Device:  {info['brand']} {info['model']}")
    print(f"Android: {info['android_version']} (SDK {info['sdk']})")
    print(f"Arch:    {info['arch']}")
    print(f"Screen:  {info['screen_width']}x{info['screen_height']} "
          f"@ {info['current_dpi'] or info['native_dpi']}dpi")
    print(f"CPU:     {info['cores']} cores")
    print(f"RAM:     {info['ram_gb']}GB")
    gpu = info.get("gpu", {})
    if gpu.get("model"):
        print(f"GPU:     {gpu.get('vendor', '')} {gpu['model']}"
              f" (GLES {gpu.get('gles_version', '?')})")
    print(f"Chrome:  {info['chrome_version']}")
    print(f"UA:      {get_user_agent(info)}")
    print(f"Platform: {get_platform_string(info)}")
