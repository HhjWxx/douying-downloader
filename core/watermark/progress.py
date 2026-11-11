"""媒体实际工作量与独立心跳；百分比仅表示当前阶段，不估算整项任务。"""

import functools
import json
import os
import threading
import time
from contextvars import ContextVar

_current = ContextVar("watermark_progress", default=None)


class Progress:
    def __init__(self, sink):
        self.sink = sink
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.started = time.monotonic()
        self.event = {}
        self.last_emit = 0

    def stage(self, name):
        with self.lock:
            self.event = {"stage": name, "done": 0, "total": None, "unit": "",
                          "stage_done": False, "last_progress_at": time.time()}
            self._emit()

    def update(self, done, total, unit, finished=False):
        with self.lock:
            if done != self.event.get("done"):
                self.event["last_progress_at"] = time.time()
            self.event.update(done=done, total=total, unit=unit, stage_done=finished)
            if finished or time.monotonic() - self.last_emit >= 0.5:
                self._emit()

    def _emit(self):
        self.last_emit = time.monotonic()
        self.sink({**self.event, "elapsed_seconds": self.last_emit - self.started,
                   "updated_at": time.time()})

    def heartbeat(self):
        while not self.stopped.wait(2):
            with self.lock:
                if self.event:
                    self._emit()


def reporting(method):
    @functools.wraps(method)
    def wrapped(*args, **kwargs):
        if os.environ.get("DOUYIN_WATERMARK_PROGRESS") != "1":
            return method(*args, **kwargs)
        reporter = Progress(lambda event: print(json.dumps(event, ensure_ascii=False), flush=True))
        token = _current.set(reporter)
        thread = threading.Thread(target=reporter.heartbeat, daemon=True)
        thread.start()
        try:
            return method(*args, **kwargs)
        finally:
            reporter.stopped.set()
            thread.join()
            _current.reset(token)
    return wrapped


def stage(name):
    reporter = _current.get()
    if reporter:
        reporter.stage(name)


class MediaProgress:
    def __init__(self, stream, packets=False):
        self.reporter = _current.get()
        self.count = 0
        self.done = 0
        self.start = float((stream.start_time or 0) * stream.time_base)
        frames = stream.frames if not packets else 0
        duration = float(stream.duration * stream.time_base) if stream.duration else None
        self.unit = ("包" if packets else "帧") if frames or not duration else "秒"
        self.total = frames or duration
        if self.reporter:
            self.reporter.update(0, self.total, self.unit)

    def advance(self, item):
        if not self.reporter:
            return
        self.count += 1
        if self.unit in ("帧", "包"):
            self.done = self.count
        elif item.pts is not None:
            self.done = max(self.done, float(item.pts * item.time_base) - self.start)
        self.reporter.update(self.done, self.total, self.unit)

    def finish(self):
        if self.reporter:
            self.reporter.update(self.done, self.total, self.unit, finished=True)
