"""Screenshot capture via CDP."""

import asyncio
import base64
import logging
import os

logger = logging.getLogger(__name__)


class ScreenshotCommands:
    """Capture screenshots and PDF."""

    def __init__(self, session):
        self.session = session

    async def capture(self, path=None, full_page=False, quality=80,
                      fmt="png"):
        """Take a screenshot, save to path or return bytes."""
        params = {"format": fmt}

        if fmt == "jpeg":
            params["quality"] = quality

        if full_page:
            # Get full page dimensions
            metrics = await self.session.send(
                "Page.getLayoutMetrics"
            )
            content = metrics.get("cssContentSize",
                                  metrics.get("contentSize", {}))
            width = content.get("width", 1920)
            height = content.get("height", 1080)

            params["clip"] = {
                "x": 0, "y": 0,
                "width": width, "height": height,
                "scale": 1,
            }

        result = await self.session.send(
            "Page.captureScreenshot", params
        )

        img_data = base64.b64decode(result["data"])

        if path:
            def _save():
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "wb") as f:
                    f.write(img_data)
            await asyncio.to_thread(_save)
            return path

        return img_data

    async def capture_pdf(self, path=None, **options):
        """Capture page as PDF with optional parameters.

        Options: landscape, scale, margin_top/right/bottom/left (inches),
        page_ranges, print_background, header_template, footer_template.
        """
        pdf_params = {
            "printBackground": options.get("print_background", True),
            "preferCSSPageSize": True,
        }
        if "landscape" in options:
            pdf_params["landscape"] = options["landscape"]
        if "scale" in options:
            pdf_params["scale"] = options["scale"]
        if "margin_top" in options:
            pdf_params["marginTop"] = options["margin_top"]
        if "margin_right" in options:
            pdf_params["marginRight"] = options["margin_right"]
        if "margin_bottom" in options:
            pdf_params["marginBottom"] = options["margin_bottom"]
        if "margin_left" in options:
            pdf_params["marginLeft"] = options["margin_left"]
        if "page_ranges" in options:
            pdf_params["pageRanges"] = options["page_ranges"]
        if "header_template" in options:
            pdf_params["displayHeaderFooter"] = True
            pdf_params["headerTemplate"] = options["header_template"]
        if "footer_template" in options:
            pdf_params["displayHeaderFooter"] = True
            pdf_params["footerTemplate"] = options["footer_template"]
        result = await self.session.send(
            "Page.printToPDF",
            pdf_params,
        )

        pdf_data = base64.b64decode(result["data"])

        if path:
            def _save():
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "wb") as f:
                    f.write(pdf_data)
            await asyncio.to_thread(_save)
            return path

        return pdf_data

    @staticmethod
    async def capture_xvfb(path, display=":99"):
        """Capture the full X11 display including mouse cursor.

        Uses ImageMagick's 'import' command to capture the entire Xvfb
        screen. Unlike CDP screenshots, this includes the mouse cursor,
        Chrome's window decorations, and everything visible on the display.

        Useful for verifying mouse click positions and visual debugging.
        """
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        env = os.environ.copy()
        env["DISPLAY"] = display
        proc = await asyncio.create_subprocess_exec(
            "import", "-window", "root", path,
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.warning("X11 screenshot failed: %s", stderr.decode()[:200])
            return None
        return path

    async def periodic_capture(self, directory, interval=5, max_count=60,
                                prefix="debug"):
        """Take screenshots every N seconds for debugging.

        Saves timestamped PNGs to directory. Use as a background task.
        """
        os.makedirs(directory, exist_ok=True)
        for i in range(max_count):
            try:
                path = os.path.join(directory, f"{prefix}_{i:04d}.png")
                await self.capture(path)
            except Exception as e:
                logger.debug("Debug screenshot %d failed: %s", i, e)
            await asyncio.sleep(interval)
