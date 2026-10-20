"""Passive Pusher event sniffer via Playwright's WebSocket API.

Instead of opening a second WebSocket to Pusher (which would show up as a
duplicate connection for the same user), this module piggybacks on the
browser's existing Pusher connection through Playwright's page.on("websocket")
listener and watches the frames it already receives.

Architecture:
  - A daemon thread registers a ``page.on("websocket")`` listener on the
    active page via pw_bridge
  - New WebSocket connections whose URL contains "pusher.com" are tracked;
    their ``framereceived`` events are parsed for non-internal Pusher events
  - Any such event sets a threading.Event that _wait_for_studies can react
    to instantly
  - A periodic heartbeat checks whether the active page has changed (tab
    recycled, browser reconnected) and re-attaches if needed
  - The browser's own Pusher connection is never touched

Event dispatching: Playwright fires WS frame callbacks only while its event
loop is being pumped (during any pw_bridge call).  _wait_for_studies
interleaves short ``wait_for_timeout`` pumps with event checks to keep
latency under ~1 s.  The heartbeat here only needs to detect page changes
(e.g. recycle_tab), not pump events for responsiveness.

Note: ``page.on("websocket")`` only fires for connections opened AFTER the
listener is registered, and ``ws.on("close")`` does NOT fire when the browser
closes the socket during page navigation (only for explicit JS closes).
The first page navigation (which _wait_for_studies does via _refresh_list)
triggers Pusher to reconnect, at which point the listener picks it up.
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
        self._listening_page = None

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
        # Short settle so the first cycle's page.goto gets its queue slot
        # before our attach does: otherwise attach waits behind a 10-30s
        # navigation and the 30s call timeout can still fire.
        self._stop.wait(5)
        backoff = 2
        while not self._stop.is_set():
            try:
                # 30s (not 10s) because the pw_bridge is a single-worker
                # queue: when attach lands behind a long page.goto or
                # screenshot burst, 10s was too tight and the sniffer never
                # connected for the entire run (seen: 13 consecutive timeouts
                # over ~5 minutes).
                pw_bridge.run(self._attach, timeout=30)
                self._connected = True
                backoff = 2
                log.info("Pusher WS listener attached")
                while not self._stop.wait(30):
                    try:
                        same = pw_bridge.run(self._check_page, timeout=5)
                        if not same:
                            log.info("Active page changed, re-attaching")
                            break
                    except Exception:
                        break
            except Exception as e:
                log.warning("Pusher listener attach failed: %s", e)
            finally:
                self._connected = False
            if self._stop.is_set():
                break
            wait = min(backoff, 30)
            log.info("Sniffer reconnect in %ds", wait)
            if self._stop.wait(wait):
                break
            backoff = min(backoff * 2, 30)

    def _check_page(self, bridge):
        return bridge.active_page() is self._listening_page

    def _attach(self, bridge):
        """Register WS frame listener on the active page.

        ``page.on("websocket")`` persists across navigations on the same
        Page object, so a single registration survives _refresh_list cycles.
        Each new Pusher WS that opens (after reload / navigation) fires
        on_ws again, and its ``framereceived`` callback is wired up fresh.
        """
        page = bridge.active_page()
        if page is self._listening_page:
            return
        self._listening_page = page
        sniffer = self

        def on_ws(ws):
            if PUSHER_WS_MARKER not in ws.url:
                return
            log.info("Pusher WS detected: %s", ws.url[:80])

            def on_frame(data):
                try:
                    msg = json.loads(data)
                except (json.JSONDecodeError, TypeError):
                    return
                event = msg.get("event", "")
                if (event
                        and not event.startswith("pusher:")
                        and not event.startswith("pusher_internal:")):
                    channel = msg.get("channel", "")
                    sniffer._event_count += 1
                    sniffer._last_event_ts = time.time()
                    log.info("Pusher event #%d: %s on %s",
                             sniffer._event_count, event, channel)
                    sniffer._event.set()

            ws.on("framereceived", on_frame)

        page.on("websocket", on_ws)


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
