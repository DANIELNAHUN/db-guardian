import logging
import queue
import threading
from collections.abc import Callable

logger = logging.getLogger(__name__)

_STOP = object()


class WorkQueue:
    """FIFO of jobs run one at a time by a single worker thread.

    A key can be pending only once: submitting it again before the worker
    picks it up is a no-op that returns False.
    """

    def __init__(self) -> None:
        self._queue: queue.Queue = queue.Queue()
        self._pending: set[str] = set()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._work, name="guardian-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 30) -> None:
        if not self._thread:
            return
        self._queue.put((None, _STOP))
        self._thread.join(timeout)
        self._thread = None

    def submit(self, key: str, fn: Callable[[], None]) -> bool:
        with self._lock:
            if key in self._pending:
                return False
            self._pending.add(key)
        self._queue.put((key, fn))
        return True

    def join(self) -> None:
        """Block until every submitted job has run (used by tests)."""
        self._queue.join()

    def _work(self) -> None:
        while True:
            key, fn = self._queue.get()
            try:
                if fn is _STOP:
                    return
                with self._lock:
                    self._pending.discard(key)
                fn()
            except Exception:
                logger.exception("job %s failed", key)
            finally:
                self._queue.task_done()
