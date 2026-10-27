"""Run blocking API calls off the GUI thread and deliver results back on the GUI thread."""
from __future__ import annotations

import itertools
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

from .client import ApiError, InternalError


class _Job(QRunnable):
    def __init__(self, runner: TaskRunner, task_id: int, fn: Callable[[], Any]):
        super().__init__()
        self.runner, self.task_id, self.fn = runner, task_id, fn

    def run(self) -> None:  # worker thread
        try:
            result, ok = self.fn(), True
        except ApiError as exc:
            result, ok = exc, False
        except Exception:  # never surface raw exception text (it could contain anything)
            result, ok = InternalError(), False
        self.runner.finished.emit(self.task_id, ok, result)


class TaskRunner(QObject):
    """`run(fn, on_ok, on_err)` executes fn in a thread pool; callbacks run on the GUI thread.

    The runner lives on the GUI thread, so the cross-thread `finished` signal is queued to it."""

    finished = Signal(int, bool, object)

    def __init__(self, parent: QObject | None = None, pool: QThreadPool | None = None):
        super().__init__(parent)
        self.pool = pool or QThreadPool.globalInstance()
        self._ids = itertools.count(1)
        self._pending: dict[int, tuple[Callable[[Any], None], Callable[[ApiError], None] | None]] = {}
        self.finished.connect(self._deliver)

    @property
    def pending(self) -> int:
        return len(self._pending)

    def run(self, fn: Callable[[], Any], on_ok: Callable[[Any], None],
            on_err: Callable[[ApiError], None] | None = None) -> int:
        task_id = next(self._ids)
        self._pending[task_id] = (on_ok, on_err)
        self.pool.start(_Job(self, task_id, fn))
        return task_id

    @Slot(int, bool, object)
    def _deliver(self, task_id: int, ok: bool, result: Any) -> None:
        callbacks = self._pending.pop(task_id, None)
        if callbacks is None:
            return
        on_ok, on_err = callbacks
        try:
            if ok:
                on_ok(result)
            elif on_err is not None:
                on_err(result)
        except RuntimeError:
            # The target widget was deleted while the request was in flight; nothing to update.
            pass
