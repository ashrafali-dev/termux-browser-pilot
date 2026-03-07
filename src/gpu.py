"""GPU rendering setup for stealth WebGL.

Manages virglrenderer-android lifecycle for real GPU passthrough.
Provides fallback WebGL renderer spoofing when hardware acceleration
is unavailable. All GPU info is auto-detected from device.py.
"""

import asyncio
import logging
import shutil

logger = logging.getLogger(__name__)


class VirglManager:
    """Manage virglrenderer-android server lifecycle."""

    def __init__(self):
        self._proc = None
        self._available = None

    def is_available(self):
        """Check if virglrenderer-android is installed."""
        if self._available is None:
            self._available = shutil.which(
                "virgl_test_server_android"
            ) is not None
        return self._available

    async def start(self):
        """Start virgl_test_server_android in background.

        Returns True if started successfully.
        """
        if not self.is_available():
            return False

        # Kill any existing virgl server
        kill_proc = await asyncio.create_subprocess_exec(
            "pkill", "-f", "virgl_test_server_android",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await kill_proc.wait()
        await asyncio.sleep(0.3)

        self._proc = await asyncio.create_subprocess_exec(
            "virgl_test_server_android",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.sleep(1.0)

        if self._proc.returncode is not None:
            logger.warning("virgl_test_server_android failed to start")
            self._proc = None
            return False

        logger.info("virgl started (pid %d)", self._proc.pid)
        return True

    async def stop(self):
        """Stop the virgl server."""
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                self._proc.kill()
                await self._proc.wait()
        self._proc = None

    def get_env(self):
        """Return environment variables for virgl-accelerated rendering."""
        return {
            "GALLIUM_DRIVER": "virpipe",
            "MESA_GL_VERSION_OVERRIDE": "4.0",
        }


def get_webgl_spoof_js(device_info):
    """Generate JS to spoof WebGL renderer using real device GPU info.

    Fallback when virgl is unavailable. Overrides getParameter for
    UNMASKED_VENDOR/RENDERER WebGL debug extension constants.

    Args:
        device_info: Dict from device.py with gpu.vendor and gpu.model.
    """
    gpu = device_info.get("gpu", {})
    vendor = gpu.get("vendor", "Qualcomm")
    model = gpu.get("model", "")

    if not model:
        return ""

    # Build renderer string matching real Android Chrome format.
    # Strip version suffixes like "v2" — real Chrome shows "Adreno (TM) 740"
    # not "Adreno (TM) 740v2".
    import re
    if vendor == "Qualcomm" and model:
        model_num = model.replace("Adreno", "").strip()
        # Remove trailing version suffix (v2, v3, etc.)
        model_num = re.sub(r'v\d+$', '', model_num)
        unmasked_renderer = f"Adreno (TM) {model_num}"
        unmasked_vendor = "Qualcomm"
    elif vendor == "ARM" and model:
        unmasked_renderer = model
        unmasked_vendor = "ARM"
    else:
        unmasked_renderer = model
        unmasked_vendor = vendor

    return f"""
    (function() {{
        // Override UNMASKED_VENDOR/RENDERER for both WebGL1 and WebGL2
        // Also override RENDERER (0x1F01) and VENDOR (0x1F00) to prevent
        // leaking "Google SwiftShader" or virgl strings.
        // Real Chrome returns "WebKit WebGL"/"WebKit" for these.
        // Also cap maxTextureSize to match real Android Chrome (8192).
        function patchGetParam(proto) {{
            var orig = proto.getParameter;
            proto.getParameter = function(p) {{
                if (p === 0x9245) return '{unmasked_vendor}';
                if (p === 0x9246) return '{unmasked_renderer}';
                if (p === 0x1F01) return 'WebKit WebGL';
                if (p === 0x1F00) return 'WebKit';
                // Cap maxTextureSize to match real device (virgl reports 16384)
                if (p === 0x0D33) return 8192;   // MAX_TEXTURE_SIZE
                if (p === 0x84CA) return 16384;   // MAX_RENDERBUFFER_SIZE
                return orig.call(this, p);
            }};
        }}
        patchGetParam(WebGLRenderingContext.prototype);
        if (typeof WebGL2RenderingContext !== 'undefined') {{
            patchGetParam(WebGL2RenderingContext.prototype);
        }}

        // Filter out extensions that virgl exposes but real Android Chrome doesn't
        // Real Android Chrome has ~27 WebGL1 extensions, virgl has ~35
        var extraExtensions = [
            'WEBGL_draw_buffers',
            'EXT_frag_depth',
            'EXT_shader_texture_lod',
            'OES_draw_buffers_indexed',
            'EXT_clip_control',
            'EXT_depth_clamp',
            'EXT_polygon_offset_clamp',
            'WEBGL_polygon_mode',
        ];
        function patchGetExtensions(proto) {{
            var orig = proto.getSupportedExtensions;
            proto.getSupportedExtensions = function() {{
                var exts = orig.call(this);
                if (!exts) return exts;
                return exts.filter(function(e) {{
                    return extraExtensions.indexOf(e) === -1;
                }});
            }};
        }}
        patchGetExtensions(WebGLRenderingContext.prototype);
        if (typeof WebGL2RenderingContext !== 'undefined') {{
            patchGetExtensions(WebGL2RenderingContext.prototype);
        }}
    }})();
    """
