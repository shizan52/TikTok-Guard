"""A small in-process event bus: everything the desktop app should hear about as it happens (a new alert card, an
account's login progress, a scan that failed ...). Subscribers read with `since(last_id)` or block in `wait()`.

Events are `{"id": 17, "ts": "...Z", "type": "approval.created", "level": "info", "data": {...}}` - ids rise by one,
so a client that reconnects asks for "everything after id 17" and misses nothing that is still in the buffer."""
import threading
import time
from collections import deque


class EventBus:
    def __init__(self, capacity: int = 1000) -> None:
        self._events: deque = deque(maxlen=capacity)
        self._next = 1
        self._cond = threading.Condition()

    def publish(self, type: str, data: dict | None = None, level: str = "info") -> dict:
        with self._cond:
            event = {"id": self._next, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "type": type,
                     "level": level, "data": dict(data or {})}
            self._next += 1
            self._events.append(event)
            self._cond.notify_all()
        return event

    def last_id(self) -> int:
        with self._cond:
            return self._next - 1

    def since(self, last_id: int = 0, limit: int = 200) -> list:
        with self._cond:
            return [e for e in self._events if e["id"] > last_id][:limit]

    def wait(self, last_id: int, timeout: float = 25.0, limit: int = 200) -> list:
        """Events after `last_id`; blocks up to `timeout` seconds while there are none (long-poll / SSE)."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                fresh = [e for e in self._events if e["id"] > last_id][:limit]
                left = deadline - time.monotonic()
                if fresh or left <= 0:
                    return fresh
                self._cond.wait(min(left, 1.0))

    def wake_all(self) -> None:
        """Let blocked readers return now (server shutdown)."""
        with self._cond:
            self._cond.notify_all()
