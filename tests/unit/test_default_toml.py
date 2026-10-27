from pathlib import Path

from luna.config.loader import load_settings

DEFAULT = Path(__file__).resolve().parents[2] / "packaging" / "luna.default.toml"


def test_default_toml_loads_with_fresh_install_defaults(tmp_path):
    s = load_settings(DEFAULT, env={}, cwd=tmp_path)
    assert s.tts.enabled is True
    assert s.tts.provider == "edge"
    assert s.voice.listen is True
    assert s.wakeword.enabled is False
