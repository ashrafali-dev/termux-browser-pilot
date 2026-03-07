#!/data/data/com.termux/files/usr/bin/bash
# Start Xvfb + Chromium for termux-browser-pilot
pkill -f "Xvfb :99" 2>/dev/null
pkill -f "chromium.*9222" 2>/dev/null
sleep 1

# Start Xvfb if not running
if ! pgrep -f "Xvfb :99" >/dev/null; then
    Xvfb :99 -screen 0 1920x1080x24 -ac -nolisten tcp &>/dev/null &
    sleep 1
fi

# Try virgl for GPU acceleration
if command -v virgl_test_server_android &>/dev/null; then
    pkill -f virgl_test_server_android 2>/dev/null
    virgl_test_server_android &>/dev/null &
    sleep 1
    export GALLIUM_DRIVER=virpipe
    export MESA_GL_VERSION_OVERRIDE=4.0
    GL_FLAGS="--enable-webgl --use-gl=egl --disable-gpu-sandbox"
    echo "GPU: virgl (hardware-accelerated)"
else
    export LIBGL_ALWAYS_SOFTWARE=1
    GL_FLAGS="--enable-webgl --use-gl=angle --use-angle=swiftshader-webgl"
    echo "GPU: SwiftShader (software rendering)"
fi

# Start Chromium headed on Xvfb (minimal flags for stealth)
DISPLAY=:99 chromium-browser \
    --no-sandbox \
    --no-zygote \
    --disable-setuid-sandbox \
    --disable-dev-shm-usage \
    --remote-debugging-port=9222 \
    --remote-debugging-address=127.0.0.1 \
    --disable-blink-features=AutomationControlled \
    --window-size=1920,1080 \
    --start-maximized \
    --js-flags=--max-old-space-size=1024 \
    --disable-breakpad \
    --disable-component-update \
    $GL_FLAGS \
    "about:blank" &>/dev/null &

sleep 4
curl -s http://127.0.0.1:9222/json/version
