"""API keys dialog: Test / Save / Later (ADR 0010 D3). Never shows or logs a saved key."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from luna.config.secrets import SecretStoreError
from luna.ui.shell.keys import KeyCheck, KeyField, KeysPresenter, check_key
from luna.ui.shell.strings import t

logger = logging.getLogger(__name__)

RESTART_NOTE = t("keys.saved", "en")


class _Bridge(QObject):
    tested = Signal(str, str)  # env name, KeyCheck text (queued across threads)


class _Row:
    def __init__(self, field: KeyField, status: str, lang: str) -> None:
        self.field = field
        self.edit = QLineEdit()
        self.edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.edit.setPlaceholderText(t("keys.placeholder", lang))
        self.test = QPushButton(t("keys.test", lang))
        self.result = QLabel(status)


class KeysDialog(QDialog):
    """Non-modal dialog. ``run_check`` is injectable for tests (default: a real HTTP call)."""

    def __init__(
        self,
        presenter: KeysPresenter,
        parent: QWidget | None = None,
        *,
        run_check: Callable[[str, str], KeyCheck] = check_key,
        lang: str = "en",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(t("keys.title", lang))
        self._presenter = presenter
        self._lang = lang
        self._run_check = run_check
        self._bridge = _Bridge()
        self._bridge.tested.connect(self._on_tested)
        self.rows: dict[str, _Row] = {}
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(t("keys.intro", lang)))
        for field in presenter.fields:
            row = _Row(field, presenter.status(field), lang)
            self.rows[field.env_name] = row
            layout.addWidget(
                QLabel(field.label + (t("keys.required", lang) if field.required else ""))
            )
            line = QHBoxLayout()
            line.addWidget(row.edit)
            line.addWidget(row.test)
            layout.addLayout(line)
            layout.addWidget(row.result)
            row.test.clicked.connect(lambda _c=False, r=row: self.test_key(r.field.env_name))
        self.note = QLabel("")
        layout.addWidget(self.note)
        buttons = QHBoxLayout()
        self.save_button = QPushButton(t("keys.save", lang))
        self.later_button = QPushButton(t("keys.later", lang))
        buttons.addStretch(1)
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.later_button)
        layout.addLayout(buttons)
        self.save_button.clicked.connect(lambda _c=False: self.save())
        self.later_button.clicked.connect(lambda _c=False: self.close())

    def test_key(self, env_name: str) -> None:
        row = self.rows[env_name]
        key = row.edit.text().strip()
        if not key:
            row.result.setText(t("keys.enter_first", self._lang))
            return
        row.result.setText(t("keys.testing", self._lang))
        row.test.setEnabled(False)
        url = row.field.models_url

        def work() -> None:
            try:
                outcome = self._run_check(url, key)
            except Exception:
                outcome = KeyCheck.ERROR
            self._bridge.tested.emit(env_name, outcome.value)

        threading.Thread(target=work, name="keys-test", daemon=True).start()

    def _on_tested(self, env_name: str, text: str) -> None:
        row = self.rows[env_name]
        row.result.setText(text)
        row.test.setEnabled(True)

    def save(self) -> None:
        saved = False
        try:
            for row in self.rows.values():
                if self._presenter.save(row.field, row.edit.text()):
                    saved = True
                    row.edit.clear()
                    row.result.setText(self._presenter.status(row.field))
        except SecretStoreError as exc:
            logger.warning("[keys] %s", exc)
            self.note.setText(t("keys.save_failed", self._lang, error=exc))
            return
        self.note.setText(t("keys.saved" if saved else "keys.nothing", self._lang))
        if saved:
            logger.info("[keys] API keys saved")
