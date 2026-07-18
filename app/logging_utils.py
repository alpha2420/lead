"""Custom logging handler that pushes log records into a per-run queue for
SSE streaming to the browser."""

import logging
import queue

import pipeline as pl


class RunIdFilter(logging.Filter):
    """Only lets through log records emitted while this run_id was the
    thread-local "current run" (see pipeline.current_run_id). Without this,
    every RunQueueHandler attached to pipeline.py's single shared `log`
    logger receives every record regardless of which run emitted it, so two
    concurrent runs' SSE streams leaked each other's log lines."""

    def __init__(self, run_id: str):
        super().__init__()
        self.run_id = run_id

    def filter(self, record: logging.LogRecord) -> bool:
        return pl.get_current_run_id() == self.run_id


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
