"""Keys dialog smoke tests on the offscreen platform (no network, no real store)."""

import time

import pytest

pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtWidgets import QApplication

from luna.config.secrets import InMemoryStore
from luna.config.settings import Settings
from luna.logs.redact import clear_secrets
from luna.ui.qt.keys_dialog import RESTART_NOTE, KeysDialog
from luna.ui.shell.keys import KeyCheck, KeysPresenter


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_secrets()
    yield
    clear_secrets()


def make(run_check=lambda url, key: KeyCheck.OK):
    store = InMemoryStore()
    presenter = KeysPresenter(Settings().with_api_keys({}), store)
    return KeysDialog(presenter, run_check=run_check), store


def test_save_stores_key_and_clears_field(app):
    dialog, store = make()
    dialog.rows["GROQ_API_KEY"].edit.setText("gk-dialog-key")
    dialog.save_button.click()
    assert store.values == {"GROQ_API_KEY": "gk-dialog-key"}
    assert dialog.rows["GROQ_API_KEY"].edit.text() == ""
    assert dialog.note.text() == RESTART_NOTE
    assert dialog.rows["GROQ_API_KEY"].result.text() == "saved"


def test_save_without_input(app):
    dialog, store = make()
    dialog.save_button.click()
    assert store.values == {}
    assert dialog.note.text() == "Nothing to save"


def test_test_button_shows_result(app):
    seen = []

    def run_check(url, key):
        seen.append((url, key))
        return KeyCheck.REJECTED

    dialog, _ = make(run_check)
    row = dialog.rows["GROQ_API_KEY"]
    row.edit.setText("gk-x-12345")
    dialog.test_key("GROQ_API_KEY")
    for _ in range(300):
        app.processEvents()
        if row.test.isEnabled():
            break
        time.sleep(0.01)
    assert row.result.text() == KeyCheck.REJECTED.value
    assert seen == [("https://api.groq.com/openai/v1/models", "gk-x-12345")]


def test_later_closes(app):
    dialog, _ = make()
    dialog.show()
    dialog.later_button.click()
    assert not dialog.isVisible()
