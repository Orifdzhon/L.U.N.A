"""Providers dialog smoke tests on the offscreen platform (no network, no real store)."""

import time

import pytest

pytest.importorskip("PySide6.QtWidgets")

import shiboken6
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from luna.config.secrets import InMemoryStore
from luna.config.settings import Settings
from luna.llm.presets import load_presets
from luna.logs.redact import clear_secrets
from luna.ui.qt.providers_dialog import RESTART_NOTE, ProvidersDialog
from luna.ui.shell.keys import KeyCheck
from luna.ui.shell.providers import STT_NOTE, ProviderCheck, ProvidersPresenter, ToolSupport


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_secrets()
    yield
    clear_secrets()


CREATED = []


@pytest.fixture(autouse=True)
def _delete_dialogs():
    """Free the C++ widgets while the QApplication is alive (not at interpreter exit)."""
    yield
    while CREATED:
        dialog = CREATED.pop()
        dialog.close()
        shiboken6.delete(dialog)


def make(tmp_path, run_check=None, on_restart=None, opened=None):
    store = InMemoryStore()
    presenter = ProvidersPresenter(
        Settings().with_api_keys({}), store, load_presets(), tmp_path / "providers.toml"
    )
    kwargs = {"on_restart": on_restart}
    if run_check is not None:
        kwargs["run_check"] = run_check
    if opened is not None:
        kwargs["open_url"] = opened.append
    dialog = ProvidersDialog(presenter, **kwargs)
    CREATED.append(dialog)
    return dialog, store


def wait_idle(app, dialog):
    for _ in range(300):
        app.processEvents()
        if dialog.test_button.isEnabled():
            return
        time.sleep(0.01)
    raise AssertionError("check did not finish")


def test_lists_chain_with_badges_and_stt_note(app, tmp_path):
    dialog, _ = make(tmp_path)
    assert [dialog.list.item(i).text() for i in range(3)] == [
        "groq  [бесплатно]",
        "gemini  [бесплатно]",
        "local  [локально]",
    ]
    assert dialog.stt_note.text() == STT_NOTE
    assert dialog.url_edit.isReadOnly()  # preset URL is fixed
    assert dialog.key_status.text() == "not set"


def test_order_buttons_enable_and_remove(app, tmp_path):
    dialog, _ = make(tmp_path)
    presenter = dialog._presenter
    dialog.list.setCurrentRow(1)
    dialog.up_button.click()
    assert [d.name for d in presenter.drafts][:2] == ["gemini", "groq"]
    assert dialog.list.currentRow() == 0
    dialog.list.item(0).setCheckState(Qt.CheckState.Unchecked)
    assert presenter.drafts[0].enabled is False
    dialog.down_button.click()
    assert dialog.list.currentRow() == 1
    dialog.remove_button.click()
    assert len(presenter.drafts) == 2


def test_add_preset_and_custom(app, tmp_path):
    dialog, _ = make(tmp_path)
    preset = next(p for p in dialog._presenter.presets if p.id == "deepseek")
    dialog.add_preset(preset)
    assert dialog.list.currentItem().text() == "deepseek  [платно]"
    assert "Платный" in dialog.warning.text()
    assert dialog.model_box.currentText() == "deepseek-flash"
    assert dialog.add_custom("mine", "http://example.com/v1", "m") is False
    assert "https" in dialog.note.text()
    assert dialog.add_custom("mine", "https://llm.example.com/v1", "m") is True
    assert not dialog.url_edit.isReadOnly()
    assert dialog.key_edit.isEnabled()


def test_local_provider_has_no_key_field(app, tmp_path):
    dialog, _ = make(tmp_path)
    dialog.list.setCurrentRow(2)
    assert not dialog.key_edit.isEnabled() and dialog.key_status.text() == "no key needed"
    assert not dialog.get_key_button.isEnabled()


def test_get_key_opens_the_preset_page(app, tmp_path):
    opened = []
    dialog, _ = make(tmp_path, opened=opened)
    dialog.get_key_button.click()
    assert opened == ["https://console.groq.com/keys"]


def test_test_button_runs_in_a_worker_and_records_tools(app, tmp_path):
    seen = []

    def run_check(url, model, key):
        seen.append((url, model, key))
        return ProviderCheck(KeyCheck.OK, ToolSupport.NO, 123, ("m-a", "m-b"))

    dialog, _ = make(tmp_path, run_check)
    dialog.key_edit.setText("gk-x-12345")
    dialog.test_button.click()
    assert not dialog.test_button.isEnabled()
    wait_idle(app, dialog)
    assert seen == [("https://api.groq.com/openai/v1", "openai/gpt-oss-120b", "gk-x-12345")]
    assert dialog.result.text() == "OK - tools: no - 123 ms - 2 models"
    assert dialog._presenter.drafts[0].conf.supports_tools is False
    assert "инструменты" in dialog.warning.text()
    assert dialog.model_box.currentText() == "openai/gpt-oss-120b"  # kept; list filled
    assert dialog.model_box.count() == 2


def test_test_needs_a_key_and_a_model(app, tmp_path):
    dialog, _ = make(tmp_path, lambda *a: pytest.fail("must not run"))
    dialog.test_button.click()
    assert dialog.result.text() == "Enter a key first"
    dialog.key_edit.setText("k-12345678")
    dialog.model_box.setCurrentText("")
    dialog.test_button.click()
    assert dialog.result.text() == "Enter a model name first"


def test_fetch_models_fills_empty_model(app, tmp_path):
    def run_check(url, model, key):
        assert model == ""
        return ProviderCheck(KeyCheck.OK, models=("z-free:free", "a-free:free"))

    dialog, _ = make(tmp_path, run_check)
    dialog.add_preset(next(p for p in dialog._presenter.presets if p.id == "openrouter"))
    dialog.key_edit.setText("or-key-12345")
    dialog.fetch_button.click()
    wait_idle(app, dialog)
    assert dialog.model_box.currentText() == "z-free:free"
    assert dialog._presenter.drafts[-1].conf.model == "z-free:free"


def test_failed_check_is_shown(app, tmp_path):
    def run_check(url, model, key):
        raise RuntimeError("boom")

    dialog, _ = make(tmp_path, run_check)
    dialog.key_edit.setText("k-12345678")
    dialog.test_button.click()
    wait_idle(app, dialog)
    assert dialog.result.text() == KeyCheck.ERROR.value


def test_save_writes_file_key_and_offers_restart(app, tmp_path):
    restarted = []
    dialog, store = make(tmp_path, on_restart=lambda: restarted.append(1))
    assert not dialog.restart_button.isVisibleTo(dialog)
    dialog.key_edit.setText("gk-dialog-key")
    assert dialog.save() is True
    assert store.values == {"GROQ_API_KEY": "gk-dialog-key"}
    assert dialog.key_edit.text() == "" and dialog.key_status.text() == "saved"
    assert (tmp_path / "providers.toml").is_file()
    assert "gk-dialog-key" not in (tmp_path / "providers.toml").read_text(encoding="utf-8")
    assert dialog.note.text() == RESTART_NOTE
    assert dialog.restart_button.isVisibleTo(dialog)
    assert dialog.restart_button.text() == "Перезапустить L.U.N.A."
    dialog.restart_button.click()
    assert restarted == [1]


def test_save_error_is_shown_and_nothing_written(app, tmp_path):
    dialog, _ = make(tmp_path)
    for i in range(3):
        dialog.list.item(i).setCheckState(Qt.CheckState.Unchecked)
    assert dialog.save() is False
    assert "at least one" in dialog.note.text()
    assert not (tmp_path / "providers.toml").exists()
    assert not dialog.restart_button.isVisibleTo(dialog)


def test_close_button_closes(app, tmp_path):
    dialog, _ = make(tmp_path)
    dialog.show()
    dialog.close_button.click()
    assert not dialog.isVisible()
