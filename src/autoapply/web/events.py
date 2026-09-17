"""In-process pub/sub used for Server-Sent Events."""

from __future__ import annotations

import json
import queue
import threading
import time
from collections import deque
from typing import Any


class EventBus:
    def __init__(self, history: int = 500):
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()
        self.history: deque[dict[str, Any]] = deque(maxlen=history)
        self._seq = 0

    def publish(self, event: dict[str, Any]) -> None:
        with self._lock:
            self._seq += 1
            ev = {"seq": self._seq, "ts": time.time(), **event}
            self.history.append(ev)
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(ev)
            except queue.Full:
                pass

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=1000)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    @staticmethod
    def sse(ev: dict[str, Any]) -> str:
        return f"id: {ev.get('seq', 0)}\nevent: {ev.get('kind', 'message')}\ndata: {json.dumps(ev, default=str)}\n\n"
