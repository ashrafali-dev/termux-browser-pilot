"""Termux Browser Pilot - Browser automation for Termux/Android."""
__version__ = "0.1.0a1"

from .pilot import Pilot
from .browser import BrowserPilot
from .commands import PageCommands
from .input import InputCommands
from .screenshot import ScreenshotCommands
from .cookies import CookieCommands
from .accessibility import AccessibilityCommands
from .native import NativeFirefoxSession

# Optional: Chromium-only modules (require websockets)
try:
    from .cdp import CDPSession
    from .network import NetworkTracker
    from .cloudflare import CloudflareHandler
except ImportError:
    CDPSession = None
    NetworkTracker = None
    CloudflareHandler = None

__all__ = [
    "Pilot",
    "BrowserPilot",
    "CDPSession",
    "NativeFirefoxSession",
    "PageCommands",
    "InputCommands",
    "ScreenshotCommands",
    "CookieCommands",
    "NetworkTracker",
    "CloudflareHandler",
    "AccessibilityCommands",
]
