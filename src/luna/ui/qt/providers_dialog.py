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
    ProviderCheck,
    ProvidersPresenter,
    check_provider,
)
from luna.ui.shell.strings import t

logger = logging.getLogger(__name__)

CHECK_KEYS = {
    KeyCheck.OK: "providers.check.ok",
    KeyCheck.REJECTED: "providers.check.rejected",
    KeyCheck.NO_CONNECTION: "providers.check.no_connection",
    KeyCheck.ERROR: "providers.check.error",
}

CheckFn = Callable[[str, str, str | None], ProviderCheck]


def _default_check(base_url: str, model: str, key: str | None) -> ProviderCheck:
    return check_provider(base_url, model, key)


class _Bridge(QObject):
    # provider name, base_url and model that were tested, ProviderCheck (queued across threads)
    checked = Signal(str, str, str, object)


class _CustomDialog(QDialog):
    def __init__(self, parent: QWidget | None = None, lang: str = "en") -> None:
        super().__init__(parent)
        self.setWindowTitle(t("providers.custom_title", lang))
        form = QFormLayout(self)
        self.name = QLineEdit()
        self.name.setPlaceholderText(t("providers.custom_name_hint", lang))
        self.url = QLineEdit()
        self.url.setPlaceholderText("https://host/v1")
        self.model = QLineEdit()
        form.addRow(t("providers.custom_name", lang), self.name)
        form.addRow(t("providers.base_url", lang), self.url)
        form.addRow(t("providers.model", lang), self.model)
        ok = QPushButton(t("providers.custom_add", lang))
        ok.clicked.connect(lambda _c=False: self.accept())
        form.addRow(ok)


class ProvidersDialog(QDialog):
    """Non-modal. ``run_check``, ``open_url`` and ``on_restart`` are injectable for tests."""

    def __init__(
        self,
        presenter: ProvidersPresenter,
        parent: QWidget | None = None,
        *,
        lang: str = "en",
        run_check: CheckFn = _default_check,
        open_url: Callable[[str], object] = lambda url: QDesktopServices.openUrl(QUrl(url)),
        on_restart: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self._lang = lang
        self.setWindowTitle(t("providers.title", lang))
        self.resize(780, 460)
        self._presenter = presenter
        self._run_check = run_check
        self._open_url = open_url
        self._on_restart = on_restart
        self._loading = False
        self._typed: dict[str, str] = {}  # keys typed per provider, saved together
        self._bridge = _Bridge()
        self._bridge.checked.connect(self._on_checked)

        outer = QVBoxLayout(self)
        body = QHBoxLayout()
        outer.addLayout(body)

        # left: the chain
        left = QVBoxLayout()
        left.addWidget(QLabel(t("providers.order", lang)))
        self.list = QListWidget()
        left.addWidget(self.list)
        row = QHBoxLayout()
        self.up_button = QPushButton(t("providers.up", lang))
        self.down_button = QPushButton(t("providers.down", lang))
        self.remove_button = QPushButton(t("providers.remove", lang))
        self.add_button = QPushButton(t("providers.add", lang))
        for button in (self.up_button, self.down_button, self.remove_button, self.add_button):
            row.addWidget(button)
        left.addLayout(row)
        body.addLayout(left, 1)
        self.add_menu = QMenu(self.add_button)
        for preset in presenter.presets:
            action = self.add_menu.addAction(preset.label)
            action.triggered.connect(lambda _c=False, p=preset: self._add_preset(p))
        self.add_menu.addSeparator()
        self.add_menu.addAction(t("providers.custom_item", lang)).triggered.connect(
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
        self.fetch_button = QPushButton(t("providers.fetch", lang))
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText(t("providers.key_placeholder", lang))
        self.key_status = QLabel("")
        self.get_key_button = QPushButton(t("providers.get_key", lang))
        self.test_button = QPushButton(t("providers.test", lang))
        self.result = QLabel("")
        self.result.setWordWrap(True)
        self.warning = QLabel("")
        self.warning.setWordWrap(True)
        form = QFormLayout()
        form.addRow(t("providers.base_url", lang), self.url_edit)
        model_row = QHBoxLayout()
        model_row.addWidget(self.model_box, 1)
        model_row.addWidget(self.fetch_button)
        form.addRow(t("providers.model", lang), model_row)
        form.addRow(t("providers.key", lang), self.key_edit)
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

        for label in (self.title, self.preset_note, self.key_status, self.result, self.warning):
            label.setTextFormat(Qt.TextFormat.PlainText)  # provider texts are never markup
        self.stt_note = QLabel(t("providers.stt_note", lang))
        self.stt_note.setWordWrap(True)
        outer.addWidget(self.stt_note)
        self.order_hint = QLabel(t("providers.order_hint", lang))
        self.order_hint.setWordWrap(True)
        self.order_hint.setVisible(presenter.order_overridden)
        outer.addWidget(self.order_hint)
        self.note = QLabel("")
        self.note.setTextFormat(Qt.TextFormat.PlainText)
        self.note.setWordWrap(True)
        outer.addWidget(self.note)
        bottom = QHBoxLayout()
        bottom.addStretch(1)
        self.save_button = QPushButton(t("providers.save", lang))
        self.restart_button = QPushButton(t("providers.restart", lang))
        self.restart_button.setVisible(False)
        self.close_button = QPushButton(t("providers.close", lang))
        for button in (self.save_button, self.restart_button, self.close_button):
            bottom.addWidget(button)
        outer.addLayout(bottom)

        self.list.currentRowChanged.connect(self._load_row)
        self.list.itemChanged.connect(self._on_item_changed)
        self.up_button.clicked.connect(lambda _c=False: self._move(-1))
        self.down_button.clicked.connect(lambda _c=False: self._move(1))
        self.remove_button.clicked.connect(lambda _c=False: self._remove())
        self.model_box.currentTextChanged.connect(self._on_model_changed)
        self.key_edit.textChanged.connect(self._on_key_typed)
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
        self.key_edit.setText(self._typed.get(draft.name, ""))
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
            self._typed.pop(self._presenter.drafts[row].name, None)
            self._presenter.remove(row)
            self._refresh_list(row)

    def _add_preset(self, preset: ProviderPreset) -> None:
        self.add_preset(preset)

    def add_preset(self, preset: ProviderPreset) -> None:
        self._refresh_list(self._presenter.add_from_preset(preset))

    def _ask_custom(self) -> None:
        dialog = _CustomDialog(self, self._lang)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.add_custom(dialog.name.text(), dialog.url.text(), dialog.model.text())

    def add_custom(self, name: str, url: str, model: str) -> bool:
        try:
            index = self._presenter.add_custom(name, url, model)
        except ValueError as exc:
            self.note.setText(t("providers.cannot_add", self._lang, error=exc))
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

    def _on_key_typed(self, text: str) -> None:
        row = self._current()
        if self._loading or row is None:
            return
        name = self._presenter.drafts[row].name
        if text.strip():
            self._typed[name] = text
        else:
            self._typed.pop(name, None)

    def _on_url_edited(self) -> None:
        row = self._current()
        if self._loading or row is None or self.url_edit.isReadOnly():
            return
        try:
            self._presenter.set_base_url(row, self.url_edit.text())
        except ValueError as exc:
            self.note.setText(t("providers.url_error", self._lang, error=exc))
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
            self.result.setText(t("providers.enter_key", self._lang))
            return
        model = "" if fetch_only else self.model_box.currentText().strip()
        if not fetch_only and not model:
            self.result.setText(t("providers.enter_model", self._lang))
            return
        base_url, name = draft.conf.base_url, draft.name
        self.result.setText(t("providers.testing", self._lang))
        self.test_button.setEnabled(False)
        self.fetch_button.setEnabled(False)

        def work() -> None:
            try:
                outcome = self._run_check(base_url, model, key)
            except Exception:
                outcome = ProviderCheck(KeyCheck.ERROR)
            try:
                self._bridge.checked.emit(name, base_url, model, outcome)
            except RuntimeError:  # the dialog was deleted while the test ran
                logger.debug("[providers] test result dropped: dialog is gone")

        threading.Thread(target=work, name="providers-test", daemon=True).start()

    def _on_checked(self, name: str, base_url: str, model: str, check: ProviderCheck) -> None:
        self.test_button.setEnabled(True)
        self.fetch_button.setEnabled(True)
        row = self._presenter.apply_check(name, base_url, model, check)
        if row is None:  # removed, or its URL / model changed while the test ran
            if self._current() is not None:
                self.result.setText("")
            return
        if self._current() != row:
            return
        lang = self._lang
        parts = [t(CHECK_KEYS[check.key], lang)]
        if check.tools is not None:
            parts.append(t(f"providers.tools.{check.tools.value}", lang))
        if check.latency_ms is not None:
            parts.append(t("providers.latency", lang, ms=check.latency_ms))
        if check.models:
            parts.append(t("providers.models_count", lang, n=len(check.models)))
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
        try:
            for name in list(self._typed):
                names = [d.name for d in self._presenter.drafts]
                if name in names and self._presenter.save_key(names.index(name), self._typed[name]):
                    logger.info("[keys] API key saved")
                self._typed.pop(name, None)
            self._presenter.save()
        except SecretStoreError as exc:
            logger.warning("[keys] %s", exc)
            self.note.setText(t("providers.cannot_save_key", self._lang, error=exc))
            return False
        except ConfigError as exc:
            self.note.setText(t("providers.cannot_save", self._lang, error=exc))
            return False
        row = self._current()
        if row is not None:
            self._load_row(row)  # clears the key field, refreshes the status
        self.note.setText(t("providers.saved", self._lang))
        self.restart_button.setVisible(True)
        logger.info("[providers] providers.toml saved")
        return True

    def _restart(self) -> None:
        if self._on_restart is not None:
            self._on_restart()
        self.close()
