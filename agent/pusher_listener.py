"""Passive Pusher event sniffer via CDP.

Instead of opening a second WebSocket to Pusher (which would show up as a
duplicate connection for the same user), this module attaches to the
browser's existing Pusher connection through Chrome DevTools Protocol and
watches the frames it already receives.

Architecture:
  - A daemon thread opens a CDP session on the active page and enables
    Network domain events
  - It identifies the Pusher WebSocket by URL (ws-eu.pusher.com) and
    watches only frames on that specific connection
  - Any non-internal Pusher event sets a threading.Event that
    _wait_for_studies can react to instantly
  - If the CDP session drops (page navigated, tab closed), it reconnects
    automatically — the browser's own Pusher connection is never touched
"""

import json
import logging
import threading
import time

log = logging.getLogger(__name__)

PUSHER_WS_MARKER = "pusher.com"


class PusherSniffer:
    def __init__(self):
        self._event = threading.Event()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._connected = False
        self._last_event_ts: float = 0
        self._event_count: int = 0
        self._pusher_request_ids: set = set()

    # -- public API --

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="pusher-sniffer")
        self._thread.start()
        log.info("Pusher sniffer started")

    def stop(self):
        self._stop.set()
        log.info("Pusher sniffer stopped")

    def wait(self, timeout: float) -> bool:
        """Block until a Pusher event arrives or timeout expires.
        Returns True if an event was received."""
        got = self._event.wait(timeout)
        self._event.clear()
        return got

    def signal(self):
        self._event.set()

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def stats(self) -> dict:
        return {
            "connected": self._connected,
            "events": self._event_count,
            "last_event": self._last_event_ts,
        }

    # -- internals --

    def _loop(self):
        import pw_bridge
        backoff = 2
        while not self._stop.is_set():
            cdp = None
            try:
                cdp = pw_bridge.run(self._attach, timeout=10)
                self._connected = True
                backoff = 2
                log.info("CDP sniffer attached, tracking %d Pusher WS(s)",
                         len(self._pusher_request_ids))
                while not self._stop.wait(30):
                    try:
                        pw_bridge.run(lambda b: None, timeout=5)
                    except Exception:
                        break
            except Exception as e:
                log.warning("CDP sniffer attach failed: %s", e)
            finally:
                self._connected = False
                if cdp is not None:
                    try:
                        pw_bridge.run(lambda b, c=cdp: c.detach(), timeout=5)
                    except Exception:
                        pass
            if self._stop.is_set():
                break
            wait = min(backoff, 30)
            log.info("Sniffer reconnect in %ds", wait)
            if self._stop.wait(wait):
                break
            backoff = min(backoff * 2, 30)

    def _attach(self, bridge):
        """Called inside the pw_bridge worker thread."""
        page = bridge.active_page()
        cdp = page.context.new_cdp_session(page)
        self._pusher_request_ids = set()

        def on_ws_created(params):
            url = params.get("url", "")
            rid = params.get("requestId", "")
            if PUSHER_WS_MARKER in url:
                self._pusher_request_ids.add(rid)
                log.info("Pusher WS detected: %s (rid=%s)", url[:80], rid[:20])

        def on_frame(params):
            rid = params.get("requestId", "")
            if rid not in self._pusher_request_ids:
                return
            data = params.get("response", {}).get("payloadData", "")
            try:
                msg = json.loads(data)
            except (json.JSONDecodeError, TypeError):
                return
            event = msg.get("event", "")
            if (event
                    and not event.startswith("pusher:")
                    and not event.startswith("pusher_internal:")):
                channel = msg.get("channel", "")
                self._event_count += 1
                self._last_event_ts = time.time()
                log.info("Pusher event #%d: %s on %s", self._event_count,
                         event, channel)
                self._event.set()

        cdp.on("Network.webSocketCreated", on_ws_created)
        cdp.on("Network.webSocketFrameReceived", on_frame)
        cdp.send("Network.enable")
        return cdp


# Module-level singleton
_sniffer: PusherSniffer | None = None


def get_listener() -> PusherSniffer:
    global _sniffer
    if _sniffer is None:
        _sniffer = PusherSniffer()
    return _sniffer


def start():
    get_listener().start()


def stop():
    if _sniffer:
        _sniffer.stop()


def wait(timeout: float) -> bool:
    return get_listener().wait(timeout)
