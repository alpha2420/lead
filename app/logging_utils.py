"""Custom logging handler that pushes log records into a per-run queue for
SSE streaming to the browser."""

import logging
import queue


class RunQueueHandler(logging.Handler):
    def __init__(self, q: queue.Queue):
        super().__init__()
        self.q = q

    def emit(self, record: logging.LogRecord):
        self.q.put({
            "type":    "log",
            "level":   record.levelname,   # DEBUG / INFO / WARNING / ERROR
            "message": self.format(record),
        })
