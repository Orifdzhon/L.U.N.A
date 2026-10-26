"""GUI-thread dispatcher: ``post`` from any thread runs the callable on the GUI thread."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject, Qt, Signal, Slot


class QtUiThread(QObject):
    """Implements ``UiThread``. Create it on the GUI thread (it lives there)."""

    _posted = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        # Queued: delivered via the receiver's event loop even when emitted from another thread.
        self._posted.connect(self._run, Qt.ConnectionType.QueuedConnection)

    def post(self, fn: Callable[[], None]) -> None:
        self._posted.emit(fn)

    @Slot(object)
    def _run(self, fn: Callable[[], None]) -> None:
        fn()
