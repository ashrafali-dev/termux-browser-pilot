"""Network request/response tracking via CDP Network domain.

Captures resource URLs, types, sizes, and timing for download tracking
and debugging. Works via CDP event listeners - no JS injection needed.
"""

import asyncio
import logging
import time

logger = logging.getLogger(__name__)


class NetworkTracker:
    """Track network requests and responses via CDP events."""

    MAX_RESPONSES = 5000  # Prevent unbounded memory growth

    def __init__(self, session):
        self.session = session
        self._requests = {}  # requestId -> request info
        self._responses = []  # completed responses
        self._tracking = False

    async def start(self):
        """Start tracking network requests."""
        if self._tracking:
            return
        self._tracking = True
        self.session.on("Network.requestWillBeSent", self._on_request)
        self.session.on("Network.responseReceived", self._on_response)
        self.session.on("Network.loadingFinished", self._on_loading_finished)
        self.session.on("Network.loadingFailed", self._on_loading_failed)

    async def stop(self):
        """Stop tracking and deregister event handlers."""
        self._tracking = False
        for event, handler in [
            ("Network.requestWillBeSent", self._on_request),
            ("Network.responseReceived", self._on_response),
            ("Network.loadingFinished", self._on_loading_finished),
            ("Network.loadingFailed", self._on_loading_failed),
        ]:
            self.session.off(event, handler)

    def _on_request(self, params):
        """Handle outgoing request."""
        # Evict stale pending requests (older than 5 min) to prevent leaks
        if len(self._requests) > 1000:
            now = time.time()
            stale = [
                k for k, v in self._requests.items()
                if now - v.get("timestamp", 0) > 300
            ]
            for k in stale:
                del self._requests[k]

        req_id = params.get("requestId", "")
        request = params.get("request", {})
        self._requests[req_id] = {
            "requestId": req_id,
            "url": request.get("url", ""),
            "method": request.get("method", "GET"),
            "type": params.get("type", "Other"),
            "timestamp": time.time(),
            "headers": request.get("headers", {}),
            "status": None,
            "mimeType": None,
            "size": 0,
            "finished": False,
            "failed": False,
        }

    def _on_response(self, params):
        """Handle response received."""
        req_id = params.get("requestId", "")
        response = params.get("response", {})
        if req_id in self._requests:
            self._requests[req_id]["status"] = response.get("status")
            self._requests[req_id]["mimeType"] = response.get("mimeType", "")
            self._requests[req_id]["size"] = response.get(
                "encodedDataLength", 0
            )
            self._requests[req_id]["responseHeaders"] = response.get(
                "headers", {}
            )

    def _append_response(self, entry):
        """Append response with bounded size."""
        self._responses.append(entry)
        if len(self._responses) > self.MAX_RESPONSES:
            # Drop oldest 10% when limit exceeded
            trim = self.MAX_RESPONSES // 10
            self._responses = self._responses[trim:]

    def _on_loading_finished(self, params):
        """Handle request completion."""
        req_id = params.get("requestId", "")
        if req_id in self._requests:
            entry = self._requests.pop(req_id)
            entry["finished"] = True
            encoded = params.get("encodedDataLength", 0)
            if encoded:
                entry["size"] = encoded
            self._append_response(entry)

    def _on_loading_failed(self, params):
        """Handle request failure."""
        req_id = params.get("requestId", "")
        if req_id in self._requests:
            entry = self._requests.pop(req_id)
            entry["failed"] = True
            entry["errorText"] = params.get("errorText", "")
            self._append_response(entry)

    def get_all(self):
        """Get all completed requests."""
        return list(self._responses)

    def get_by_type(self, resource_type):
        """Get requests by type (Document, Script, Image, etc.)."""
        return [
            r for r in self._responses
            if r.get("type", "").lower() == resource_type.lower()
        ]

    def get_downloads(self):
        """Get resources that look like downloads (non-page content)."""
        download_types = {
            "image", "media", "font", "stylesheet", "script", "other"
        }
        return [
            r for r in self._responses
            if r.get("type", "").lower() in download_types
        ]

    def get_urls(self):
        """Get just the URLs of all completed requests."""
        return [r["url"] for r in self._responses if r.get("url")]

    def get_failed(self):
        """Get failed requests."""
        return [r for r in self._responses if r.get("failed")]

    def clear(self):
        """Clear tracking data."""
        self._requests.clear()
        self._responses.clear()

    def summary(self):
        """Get a human-readable summary of tracked requests."""
        by_type = {}
        total_size = 0
        for r in self._responses:
            rtype = r.get("type", "Other")
            by_type.setdefault(rtype, []).append(r)
            total_size += r.get("size", 0)

        lines = [f"Total requests: {len(self._responses)}"]
        lines.append(f"Total size: {total_size / 1024:.1f} KB")
        for rtype, reqs in sorted(by_type.items()):
            size = sum(r.get("size", 0) for r in reqs)
            lines.append(f"  {rtype}: {len(reqs)} ({size / 1024:.1f} KB)")

        failed = self.get_failed()
        if failed:
            lines.append(f"Failed: {len(failed)}")
            for req in failed[:5]:
                lines.append(
                    f"  {req['url'][:80]} - {req.get('errorText', '?')}"
                )

        return "\n".join(lines)

    async def get_response_body(self, request_id):
        """Get the response body for a specific request.

        Returns str for text content, bytes for binary content.
        """
        try:
            result = await self.session.send(
                "Network.getResponseBody",
                {"requestId": request_id},
            )
            body = result.get("body", "")
            if result.get("base64Encoded"):
                import base64
                return base64.b64decode(body)
            return body
        except Exception as e:
            logger.debug("Could not get response body: %s", e)
            return None
