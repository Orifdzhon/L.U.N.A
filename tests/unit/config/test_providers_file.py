"""providers.toml overlay: loader merge, validation, atomic writer, key names (ADR 0011 D2)."""

import tomllib

import pytest

from luna.config import providers_file
from luna.config.loader import load_settings
from luna.config.providers_file import providers_path, read_overlay, write_overlay
from luna.config.secrets import InMemoryStore, forget_keys, known_key_names
from luna.config.settings import ProviderSettings, Settings
from luna.core.errors import ConfigError

OVERLAY = """
[llm]
chain = ["mine", "groq"]

[llm.providers.mine]
base_url = "https://llm.example.com/v1"
model = "big-1"
api_key_env = "LUNA_MINE_API_KEY"
supports_tools = false
"""


def appdata(tmp_path):
    folder = tmp_path / "LUNA"
    folder.mkdir()
    return {"APPDATA": str(tmp_path)}, folder


def test_providers_path():
    assert providers_path({}) is None
    assert providers_path({"APPDATA": "C:/x"}).name == "providers.toml"


def test_loader_merges_defaults_luna_toml_providers(tmp_path):
    env, folder = appdata(tmp_path)
    (folder / "luna.toml").write_text(
        '[llm.providers.groq]\nmodel = "from-luna-toml"\n[app]\nlog_level = "DEBUG"\n',
        encoding="utf-8",
    )
    (folder / "providers.toml").write_text(OVERLAY, encoding="utf-8")
    s = load_settings(env=env, cwd=tmp_path, store=InMemoryStore())
    assert s.llm.chain == ["mine", "groq"]
    assert s.llm.providers["mine"].supports_tools is False
    assert s.llm.providers["groq"].model == "from-luna-toml"  # overlay did not touch it
    assert s.llm.providers["gemini"].model  # default kept
    assert s.app.log_level == "DEBUG"
    assert "LUNA_MINE_API_KEY" in s.api_key_names()


def test_loader_reads_saved_key_of_overlay_provider(tmp_path):
    env, folder = appdata(tmp_path)
    (folder / "providers.toml").write_text(OVERLAY, encoding="utf-8")
    store = InMemoryStore({"LUNA_MINE_API_KEY": "k-12345"})
    s = load_settings(env=env, cwd=tmp_path, store=store)
    assert s.api_key("LUNA_MINE_API_KEY").get_secret_value() == "k-12345"
    assert "mine" in s.available_providers()


@pytest.mark.parametrize(
    "text",
    [
        "[llm\nchain = ",  # bad TOML
        '[llm]\nchain = ["nope"]\n',  # unknown provider
        '[app]\nlog_level = "DEBUG"\n',  # not an [llm] table
        "[llm]\ntimeout_s = 3\n",  # not managed by the dialog
        '[llm.providers.x]\nbase_url = "https://x"\nmodel = "m"\napi_key = "secret"\n',
    ],
)
def test_invalid_overlay_names_the_file(tmp_path, text):
    env, folder = appdata(tmp_path)
    (folder / "providers.toml").write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match=r"providers.toml"):
        load_settings(env=env, cwd=tmp_path, store=InMemoryStore())


def test_no_appdata_means_no_overlay(tmp_path):
    assert load_settings(env={}, cwd=tmp_path) == Settings()
    assert read_overlay(None) == {}
    assert read_overlay(tmp_path / "missing.toml") == {}


def _providers():
    base = Settings()
    providers = dict(base.llm.providers)
    providers["mine"] = ProviderSettings(
        base_url="https://llm.example.com/v1",
        model="big-1",
        api_key_env="LUNA_MINE_API_KEY",
        supports_tools=False,
    )
    return base, providers


def test_write_round_trip_and_no_keys(tmp_path):
    base, providers = _providers()
    path = tmp_path / "LUNA" / "providers.toml"
    write_overlay(path, ["mine", "groq"], providers, base=base)
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# Managed by L.U.N.A.")
    data = tomllib.loads(text)
    assert data["llm"]["chain"] == ["mine", "groq"]
    assert data["llm"]["providers"]["mine"]["supports_tools"] is False
    assert "supports_tools" not in data["llm"]["providers"]["groq"]  # None is not written
    assert not list(path.parent.glob("*.tmp"))
    assert read_overlay(path) == data


def test_writer_validates_before_writing(tmp_path):
    base, providers = _providers()
    path = tmp_path / "providers.toml"
    path.write_text("# old\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="Invalid providers"):
        write_overlay(path, ["ghost"], providers, base=base)
    bad = dict(providers, mine=providers["mine"].model_copy(update={"base_url": "http://evil/v1"}))
    with pytest.raises(ConfigError, match="https"):
        write_overlay(path, ["mine"], bad, base=base)
    assert path.read_text(encoding="utf-8") == "# old\n"


def test_failed_replace_keeps_old_file_and_cleans_tmp(tmp_path, monkeypatch):
    base, providers = _providers()
    path = tmp_path / "providers.toml"
    path.write_text("# old\n", encoding="utf-8")

    def boom(src, dst):
        raise PermissionError("locked")

    monkeypatch.setattr(providers_file.os, "replace", boom)
    with pytest.raises(ConfigError, match="Cannot write"):
        write_overlay(path, ["mine"], providers, base=base)
    assert path.read_text(encoding="utf-8") == "# old\n"
    assert not (tmp_path / "providers.toml.tmp").exists()


def test_known_key_names_and_forget_covers_configured_names():
    names = known_key_names({"LUNA_MINE_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY"})
    assert names[:2] == ["GROQ_API_KEY", "GEMINI_API_KEY"]
    assert set(names) == {
        "GROQ_API_KEY",
        "GEMINI_API_KEY",
        "LUNA_MINE_API_KEY",
        "OPENROUTER_API_KEY",
    }
    store = InMemoryStore({"LUNA_MINE_API_KEY": "a-1234", "OTHER": "keep-1234"})
    assert forget_keys(store, names) == ["LUNA_MINE_API_KEY"]
    assert store.values == {"OTHER": "keep-1234"}


def test_forget_keys_cli_includes_overlay_names(tmp_path, monkeypatch, capsys):
    from luna.__main__ import main

    env, folder = appdata(tmp_path)
    (folder / "providers.toml").write_text(OVERLAY, encoding="utf-8")
    monkeypatch.setenv("APPDATA", env["APPDATA"])
    monkeypatch.chdir(tmp_path)
    store = InMemoryStore({"LUNA_MINE_API_KEY": "a-1234"})
    monkeypatch.setattr("luna.config.secrets.default_store", lambda: store)
    assert main(["--forget-keys"]) == 0
    assert store.values == {}
    assert "LUNA_MINE_API_KEY" in capsys.readouterr().err
