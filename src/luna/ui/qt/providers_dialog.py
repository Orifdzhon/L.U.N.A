"""Models / providers dialog (ADR 0011 D5). Never shows or logs a saved key.

The logic lives in :class:`~luna.ui.shell.providers.ProvidersPresenter`; this module is widgets
only. *Test* runs ``check_provider`` in a worker thread; changes apply on the next start, so Save
offers "Restart now".
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from PySide6.QtCore import QObject, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from luna.config.secrets import SecretStoreError
from luna.core.errors import ConfigError
from luna.llm.presets import ProviderPreset
from luna.ui.shell.keys import KeyCheck
from luna.ui.shell.providers import (
    STT_NOTE,
    ProviderCheck,
    ProvidersPresenter,
    ToolSupport,
    check_provider,
)

logger = logging.getLogger(__name__)

RESTART_NOTE = "Saved. The new setup applies after a restart."
RESTART_LABEL = "Перезапустить L.U.N.A."
TOOLS_TEXT = {
    ToolSupport.YES: "tools: yes",
    ToolSupport.UNCLEAR: "tools: unclear (the model answered without calling the tool)",
    ToolSupport.NO: "tools: no",
}

CheckFn = Callable[[str, str, str | None], ProviderCheck]


def _default_check(base_url: str, model: str, key: str | None) -> ProviderCheck:
    return check_provider(base_url, model, key)


class _Bridge(QObject):
    checked = Signal(str, object)  # provider name, ProviderCheck (queued across threads)


class _CustomDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Custom provider (OpenAI-compatible)")
        form = QFormLayout(self)
        self.name = QLineEdit()
        self.name.setPlaceholderText("e.g. myserver")
        self.url = QLineEdit()
        self.url.setPlaceholderText("https://host/v1")
        self.model = QLineEdit()
        form.addRow("Name", self.name)
        form.addRow("Base URL", self.url)
        form.addRow("Model", self.model)
        ok = QPushButton("Add")
        ok.clicked.connect(lambda _c=False: self.accept())
        form.addRow(ok)


class ProvidersDialog(QDialog):
    """Non-modal. ``run_check``, ``open_url`` and ``on_restart`` are injectable for tests."""

    def __init__(
        self,
        presenter: ProvidersPresenter,
        parent: QWidget | None = None,
        *,
        run_check: CheckFn = _default_check,
        open_url: Callable[[str], object] = lambda url: QDesktopServices.openUrl(QUrl(url)),
        on_restart: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("L.U.N.A. - Models / providers")
        self.resize(780, 460)
        self._presenter = presenter
        self._run_check = run_check
        self._open_url = open_url
        self._on_restart = on_restart
        self._loading = False
        self._bridge = _Bridge()
        self._bridge.checked.connect(self._on_checked)

        outer = QVBoxLayout(self)
        body = QHBoxLayout()
        outer.addLayout(body)

        # left: the chain
        left = QVBoxLayout()
        left.addWidget(QLabel("Order (the first one answers first)"))
        self.list = QListWidget()
        left.addWidget(self.list)
        row = QHBoxLayout()
        self.up_button = QPushButton("Up")
        self.down_button = QPushButton("Down")
        self.remove_button = QPushButton("Remove")
        self.add_button = QPushButton("Add provider")
        for button in (self.up_button, self.down_button, self.remove_button, self.add_button):
            row.addWidget(button)
        left.addLayout(row)
        body.addLayout(left, 1)
        self.add_menu = QMenu(self.add_button)
        for preset in presenter.presets:
            action = self.add_menu.addAction(preset.label)
            action.triggered.connect(lambda _c=False, p=preset: self._add_preset(p))
        self.add_menu.addSeparator()
        self.add_menu.addAction("Custom (OpenAI-compatible)...").triggered.connect(
            lambda _c=False: self._ask_custom()
        )
        self.add_button.setMenu(self.add_menu)

        # right: the selected provider
        right = QVBoxLayout()
        self.title = QLabel("")
        self.preset_note = QLabel("")
        self.preset_note.setWordWrap(True)
        self.url_edit = QLineEdit()
        self.model_box = QComboBox()
        self.model_box.setEditable(True)
        self.fetch_button = QPushButton("Fetch models")
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("paste the key here")
        self.key_status = QLabel("")
        self.get_key_button = QPushButton("Get a key")
        self.test_button = QPushButton("Test")
        self.result = QLabel("")
        self.result.setWordWrap(True)
        self.warning = QLabel("")
        self.warning.setWordWrap(True)
        form = QFormLayout()
        form.addRow("Base URL", self.url_edit)
        model_row = QHBoxLayout()
        model_row.addWidget(self.model_box, 1)
        model_row.addWidget(self.fetch_button)
        form.addRow("Model", model_row)
        form.addRow("Key", self.key_edit)
        for widget in (self.title, self.preset_note):
            right.addWidget(widget)
        right.addLayout(form)
        right.addWidget(self.key_status)
        buttons = QHBoxLayout()
        buttons.addWidget(self.get_key_button)
        buttons.addWidget(self.test_button)
        right.addLayout(buttons)
        right.addWidget(self.result)
        right.addWidget(self.warning)
        right.addStretch(1)
        body.addLayout(right, 2)

        self.stt_note = QLabel(STT_NOTE)
        self.stt_note.setWordWrap(True)
        outer.addWidget(self.stt_note)
        self.note = QLabel("")
        self.note.setWordWrap(True)
        outer.addWidget(self.note)
        bottom = QHBoxLayout()
        bottom.addStretch(1)
        self.save_button = QPushButton("Save")
        self.restart_button = QPushButton(RESTART_LABEL)
        self.restart_button.setVisible(False)
        self.close_button = QPushButton("Close")
        for button in (self.save_button, self.restart_button, self.close_button):
            bottom.addWidget(button)
        outer.addLayout(bottom)

        self.list.currentRowChanged.connect(self._load_row)
        self.list.itemChanged.connect(self._on_item_changed)
        self.up_button.clicked.connect(lambda _c=False: self._move(-1))
        self.down_button.clicked.connect(lambda _c=False: self._move(1))
        self.remove_button.clicked.connect(lambda _c=False: self._remove())
        self.model_box.currentTextChanged.connect(self._on_model_changed)
        self.url_edit.editingFinished.connect(self._on_url_edited)
        self.fetch_button.clicked.connect(lambda _c=False: self.start_check(fetch_only=True))
        self.test_button.clicked.connect(lambda _c=False: self.start_check())
        self.get_key_button.clicked.connect(lambda _c=False: self._get_key())
        self.save_button.clicked.connect(lambda _c=False: self.save())
        self.restart_button.clicked.connect(lambda _c=False: self._restart())
        self.close_button.clicked.connect(lambda _c=False: self.close())
        self._refresh_list(0)

    # --- list ---------------------------------------------------------------------------------

    def _refresh_list(self, select: int) -> None:
        self._loading = True
        self.list.clear()
        for index, draft in enumerate(self._presenter.drafts):
            badge = self._presenter.badge(index)
            item = QListWidgetItem(f"{draft.name}  [{badge}]" if badge else draft.name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if draft.enabled else Qt.CheckState.Unchecked)
            self.list.addItem(item)
        self._loading = False
        if self._presenter.drafts:
            self.list.setCurrentRow(min(select, len(self._presenter.drafts) - 1))
            self._load_row(self.list.currentRow())
        else:
            self._load_row(-1)

    def _current(self) -> int | None:
        row = self.list.currentRow()
        return row if 0 <= row < len(self._presenter.drafts) else None

    def _load_row(self, row: int) -> None:
        if not 0 <= row < len(self._presenter.drafts):
            for widget in (self.title, self.preset_note, self.result, self.warning):
                widget.setText("")
            return
        presenter = self._presenter
        draft = presenter.drafts[row]
        self._loading = True
        badge = presenter.badge(row)
        self.title.setText(f"{draft.name}  [{badge}]" if badge else draft.name)
        self.preset_note.setText(presenter.note(row))
        self.url_edit.setText(draft.conf.base_url)
        self.url_edit.setReadOnly(not draft.custom)
        self.model_box.clear()
        self.model_box.addItem(draft.conf.model)
        self.model_box.setCurrentText(draft.conf.model)
        self.key_edit.clear()
        self.key_edit.setEnabled(presenter.needs_key(row))
        self.key_status.setText(presenter.key_status(row))
        self.get_key_button.setEnabled(presenter.key_url(row) is not None)
        self.result.setText("")
        self._loading = False
        self._show_warnings(row)

    def _show_warnings(self, row: int) -> None:
        self.warning.setText("\n".join(self._presenter.warnings(row)))

    def _on_item_changed(self, item: QListWidgetItem) -> None:
        if self._loading:
            return
        row = self.list.row(item)
        self._presenter.set_enabled(row, item.checkState() == Qt.CheckState.Checked)

    def _move(self, delta: int) -> None:
        row = self._current()
        if row is not None:
            self._refresh_list(self._presenter.move(row, delta))

    def _remove(self) -> None:
        row = self._current()
        if row is not None:
            self._presenter.remove(row)
            self._refresh_list(row)

    def _add_preset(self, preset: ProviderPreset) -> None:
        self.add_preset(preset)

    def add_preset(self, preset: ProviderPreset) -> None:
        self._refresh_list(self._presenter.add_from_preset(preset))

    def _ask_custom(self) -> None:
        dialog = _CustomDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.add_custom(dialog.name.text(), dialog.url.text(), dialog.model.text())

    def add_custom(self, name: str, url: str, model: str) -> bool:
        try:
            index = self._presenter.add_custom(name, url, model)
        except ValueError as exc:
            self.note.setText(f"Cannot add: {exc}")
            return False
        self._refresh_list(index)
        return True

    # --- the selected provider ----------------------------------------------------------------

    def _on_model_changed(self, text: str) -> None:
        row = self._current()
        if self._loading or row is None:
            return
        self._presenter.set_model(row, text)
        self._show_warnings(row)

    def _on_url_edited(self) -> None:
        row = self._current()
        if self._loading or row is None or self.url_edit.isReadOnly():
            return
        try:
            self._presenter.set_base_url(row, self.url_edit.text())
        except ValueError as exc:
            self.note.setText(f"Base URL: {exc}")
            return
        self._show_warnings(row)

    def _get_key(self) -> None:
        row = self._current()
        url = self._presenter.key_url(row) if row is not None else None
        if url:
            self._open_url(url)

    def start_check(self, *, fetch_only: bool = False) -> None:
        row = self._current()
        if row is None:
            return
        draft = self._presenter.drafts[row]
        key = self._presenter.key_for_test(row, self.key_edit.text())
        if self._presenter.needs_key(row) and not key:
            self.result.setText("Enter a key first")
            return
        model = "" if fetch_only else self.model_box.currentText().strip()
        if not fetch_only and not model:
            self.result.setText("Enter a model name first")
            return
        base_url, name = draft.conf.base_url, draft.name
        self.result.setText("Testing...")
        self.test_button.setEnabled(False)
        self.fetch_button.setEnabled(False)

        def work() -> None:
            try:
                outcome = self._run_check(base_url, model, key)
            except Exception:
                outcome = ProviderCheck(KeyCheck.ERROR)
            self._bridge.checked.emit(name, outcome)

        threading.Thread(target=work, name="providers-test", daemon=True).start()

    def _on_checked(self, name: str, check: ProviderCheck) -> None:
        self.test_button.setEnabled(True)
        self.fetch_button.setEnabled(True)
        names = [d.name for d in self._presenter.drafts]
        if name not in names:
            return  # the provider was removed while the test ran
        row = names.index(name)
        self._presenter.record_check(row, check)
        if self._current() != row:
            return
        parts = [check.key.value]
        if check.tools is not None:
            parts.append(TOOLS_TEXT[check.tools])
        if check.latency_ms is not None:
            parts.append(f"{check.latency_ms} ms")
        if check.models:
            parts.append(f"{len(check.models)} models")
            self._fill_models(check.models)
        self.result.setText(" - ".join(parts))
        self._show_warnings(row)

    def _fill_models(self, models: tuple[str, ...]) -> None:
        current = self.model_box.currentText().strip()
        self._loading = True
        self.model_box.clear()
        self.model_box.addItems(models)
        self.model_box.setCurrentText(current or (models[0] if models else ""))
        self._loading = False
        row = self._current()
        if row is not None and not current and models:
            self._presenter.set_model(row, models[0])  # an empty model gets the first suggestion
            self._show_warnings(row)

    # --- save ---------------------------------------------------------------------------------

    def save(self) -> bool:
        row = self._current()
        try:
            if row is not None and self._presenter.save_key(row, self.key_edit.text()):
                self.key_edit.clear()
                self.key_status.setText(self._presenter.key_status(row))
                logger.info("[keys] API key saved")
            self._presenter.save()
        except SecretStoreError as exc:
            logger.warning("[keys] %s", exc)
            self.note.setText(f"Could not save the key: {exc}")
            return False
        except ConfigError as exc:
            self.note.setText(f"Could not save: {exc}")
            return False
        self.note.setText(RESTART_NOTE)
        self.restart_button.setVisible(True)
        logger.info("[providers] providers.toml saved")
        return True

    def _restart(self) -> None:
        if self._on_restart is not None:
            self._on_restart()
        self.close()
