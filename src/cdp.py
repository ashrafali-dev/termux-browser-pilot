"""CDP (Chrome DevTools Protocol) WebSocket client."""

import asyncio
import itertools
import json
import logging
import urllib.request

import websockets.exceptions
from websockets.asyncio.client import connect as ws_connect

logger = logging.getLogger(__name__)


class CDPClient:
    """Low-level CDP WebSocket client."""

    def __init__(self, ws_url):
        self.ws_url = ws_url
        self._ws = None
        self._id_counter = itertools.count(1)
        self._callbacks = {}
        self._event_handlers = {}
        self._listener_task = None
        self._disconnected = False
        self._last_error = None

    async def connect(self):
        """Connect to CDP WebSocket.

        Only enables Page and Network domains by default.
        Runtime domain is NOT enabled to avoid the consoleAPICalled
        detection signal used by Cloudflare/DataDome anti-bot systems.
        Runtime.evaluate works without Runtime.enable.
        """
        self._ws = await ws_connect(
            self.ws_url,
            max_size=50 * 1024 * 1024,  # 50MB for screenshots
        )
        self._listener_task = asyncio.create_task(self._listen())
        await self.send("Page.enable")
        await self.send("Network.enable")
        return self

    async def enable_runtime(self):
        """Explicitly enable Runtime domain.

        WARNING: Makes the session detectable by anti-bot systems
        that check for consoleAPICalled side effects. Only call if
        you need Runtime domain events (executionContextCreated, etc.).
        """
        await self.send("Runtime.enable")

    def _task_exception_callback(self, task):
        """Log exceptions from event handler tasks."""
        if not task.cancelled() and task.exception():
            logger.error("CDP event handler raised: %s", task.exception())

    async def _listen(self):
        """Listen for CDP messages."""
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, ValueError) as e:
                    logger.debug("Malformed CDP message: %s", e)
                    continue
                msg_id = msg.get("id")
                if msg_id is not None and msg_id in self._callbacks:
                    future = self._callbacks.pop(msg_id)
                    if not future.done():
                        future.set_result(msg)
                elif "method" in msg:
                    method = msg["method"]
                    if method in self._event_handlers:
                        for handler in list(self._event_handlers[method]):
                            if asyncio.iscoroutinefunction(handler):
                                task = asyncio.create_task(
                                    handler(msg.get("params", {}))
                                )
                                task.add_done_callback(
                                    self._task_exception_callback
                                )
                            else:
                                try:
                                    handler(msg.get("params", {}))
                                except Exception as e:
                                    logger.error(
                                        "Sync CDP event handler error: %s", e
                                    )
        except websockets.exceptions.ConnectionClosed:
            logger.debug("CDP WebSocket connection closed")
        except Exception as e:
            self._last_error = e
            logger.error("CDP listener error: %s", e)
        finally:
            self._disconnected = True
            # Clean up pending callbacks on disconnect
            for future in self._callbacks.values():
                if not future.done():
                    future.set_exception(
                        ConnectionError("CDP WebSocket disconnected")
                    )
            self._callbacks.clear()

    async def send(self, method, params=None, timeout=30):
        """Send a CDP command and wait for response."""
        if self._disconnected:
            raise ConnectionError(
                "CDP WebSocket disconnected"
                + (f": {self._last_error}" if self._last_error else "")
            )
        if not self._ws or self._ws.close_code is not None:
            raise ConnectionError("CDP WebSocket not connected")

        msg_id = next(self._id_counter)
        payload = {"id": msg_id, "method": method}
        if params is not None:
            payload["params"] = params

        future = asyncio.get_running_loop().create_future()
        self._callbacks[msg_id] = future

        await self._ws.send(json.dumps(payload))

        try:
            result = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._callbacks.pop(msg_id, None)
            raise TimeoutError(f"CDP command {method} timed out after {timeout}s")

        if "error" in result:
            raise RuntimeError(
                f"CDP error: {result['error'].get('message', result['error'])}"
            )
        return result.get("result", {})

    def on(self, event, handler):
        """Register an event handler."""
        if event not in self._event_handlers:
            self._event_handlers[event] = []
        self._event_handlers[event].append(handler)

    def off(self, event, handler):
        """Unregister an event handler."""
        if event in self._event_handlers:
            try:
                self._event_handlers[event].remove(handler)
            except ValueError:
                pass

    async def close(self):
        """Close the connection."""
        if self._listener_task:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
        if self._ws:
            await self._ws.close()

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *exc):
        await self.close()


class CDPSession:
    """High-level CDP session wrapping a target/page."""

    def __init__(self, browser_ws_url):
        self._browser_ws_url = browser_ws_url
        self._client = None

    async def connect(self):
        """Connect to the first available page target (non-blocking)."""
        base = self._browser_ws_url.split("/devtools")[0].replace("ws://", "http://")

        def _fetch(url):
            with urllib.request.urlopen(url, timeout=5) as resp:
                return json.loads(resp.read())

        targets = await asyncio.to_thread(_fetch, f"{base}/json/list")

        page_target = next((t for t in targets if t.get("type") == "page"), None)

        if not page_target:
            page_target = await asyncio.to_thread(
                _fetch, f"{base}/json/new?about:blank"
            )

        self._client = CDPClient(page_target["webSocketDebuggerUrl"])
        await self._client.connect()
        return self

    @property
    def client(self):
        return self._client

    async def send(self, method, params=None, timeout=30):
        return await self._client.send(method, params, timeout)

    def on(self, event, handler):
        self._client.on(event, handler)

    def off(self, event, handler):
        self._client.off(event, handler)

    async def close(self):
        if self._client:
            await self._client.close()

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *exc):
        await self.close()
