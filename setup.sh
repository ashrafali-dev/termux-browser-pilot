#!/usr/bin/env bash
# Termux Browser Pilot - One-command installer
set -e

echo "=== Termux Browser Pilot Installer ==="

# Ensure repos
pkg install -y x11-repo tur-repo 2>/dev/null || true

# Core dependencies (Firefox mode - default, passes Cloudflare)
echo "[1/4] Installing Firefox + Xvfb + tools..."
pkg install -y firefox xorg-server-xvfb xdotool xclip openbox imagemagick python3 ca-certificates 2>/dev/null

# Optional: Chromium mode (for CDP/network tracking)
echo "[2/4] Installing Chromium (optional)..."
pkg install -y chromium 2>/dev/null || echo "  Chromium not available, Firefox-only mode"

# Install Python package (creates 'tbp' command)
echo "[3/4] Installing termux-browser-pilot..."
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PIP_FLAGS=""
# Only use --break-system-packages outside of virtual environments
if [ -z "$VIRTUAL_ENV" ] && [ -z "$CONDA_DEFAULT_ENV" ]; then
    pip install --help 2>&1 | grep -q "break-system-packages" && PIP_FLAGS="--break-system-packages"
fi
if ! pip install $PIP_FLAGS "$SCRIPT_DIR" 2>&1; then
    echo "ERROR: pip install failed. Check output above." >&2
    exit 1
fi

# Optional: Chromium Python deps
echo "[4/4] Installing optional dependencies..."
pip install $PIP_FLAGS websockets 2>/dev/null || true

# Verify installation
if command -v tbp &>/dev/null; then
    echo ""
    echo "=== Installation complete ==="
    echo "Installed: $(tbp --version)"
    echo ""
    echo "Usage:"
    echo "  tbp goto https://example.com          # Navigate (auto-starts daemon)"
    echo "  tbp goto https://audiogames.net -cf    # Cloudflare bypass"
    echo "  tbp text                               # Read page text"
    echo "  tbp screenshot page.png                # Take screenshot"
    echo "  tbp --help                             # All commands"
else
    echo ""
    echo "WARNING: 'tbp' command not found on PATH." >&2
    echo "Try: pip install --break-system-packages $SCRIPT_DIR" >&2
    exit 1
fi
