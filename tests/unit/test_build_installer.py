from pathlib import Path

import build_installer as bi
import pytest

import luna


def test_check_version_ok() -> None:
    assert bi.check_version("0.1.0", "0.1.0") == "0.1.0"


def test_check_version_mismatch() -> None:
    with pytest.raises(bi.BuildError, match="mismatch"):
        bi.check_version("0.1.0", "0.2.0")


def test_check_version_tag() -> None:
    assert bi.check_version("1.2.3", "1.2.3", "v1.2.3") == "1.2.3"
    with pytest.raises(bi.BuildError, match="tag"):
        bi.check_version("1.2.3", "1.2.3", "v1.2.4")


def test_repo_versions_agree() -> None:
    package = bi.read_package_version(bi.ROOT / "src" / "luna" / "__init__.py")
    assert package == luna.__version__
    assert bi.check_version(package, bi.read_pyproject_version(bi.ROOT / "pyproject.toml"))


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("0.1.0", (0, 1, 0, 0)),
        ("1.2.3", (1, 2, 3, 0)),
        ("1.2.3rc1", (1, 2, 3, 0)),
        ("2", (2, 0, 0, 0)),
    ],
)
def test_version_tuple(version: str, expected: tuple[int, int, int, int]) -> None:
    assert bi.version_tuple(version) == expected


def test_version_resource_text_is_valid_and_complete() -> None:
    text = bi.version_resource_text("0.1.0")
    assert "filevers=(0, 1, 0, 0)" in text
    assert "StringStruct('ProductName', 'L.U.N.A.')" in text
    assert "StringStruct('ProductVersion', '0.1.0')" in text
    assert "StringStruct('FileVersion', '0.1.0.0')" in text
    # PyInstaller evaluates this text with these names in scope.
    names = {
        name: (lambda *a, **k: (a, k))
        for name in (
            "VSVersionInfo",
            "FixedFileInfo",
            "StringFileInfo",
            "StringTable",
            "StringStruct",
            "VarFileInfo",
            "VarStruct",
        )
    }
    assert eval(text, names)


def test_find_iscc_env_override(tmp_path: Path) -> None:
    iscc = tmp_path / "ISCC.exe"
    found = bi.find_iscc({"ISCC": str(iscc)}, exists=lambda p: p == iscc)
    assert found == iscc


def test_find_iscc_standard_folder() -> None:
    env = {"ProgramFiles(x86)": r"C:\PF86", "ProgramFiles": r"C:\PF"}
    wanted = Path(r"C:\PF") / "Inno Setup 6" / "ISCC.exe"
    assert bi.find_iscc(env, exists=lambda p: p == wanted) == wanted


def test_find_iscc_missing() -> None:
    assert bi.find_iscc({"ProgramFiles": r"C:\PF"}, exists=lambda p: False) is None


def test_notices_ignore_packages_per_edition() -> None:
    assert "sherpa-onnx" in bi.notices_ignore_packages("public")
    assert "sherpa-onnx" not in bi.notices_ignore_packages("personal")
    assert "pyinstaller" in bi.notices_ignore_packages("personal")


def test_notices_header_mentions_required_components() -> None:
    header = (bi.PACKAGING / "NOTICES_HEADER.txt").read_text("utf-8")
    assert "PySide6" in header
    assert "LGPL-3.0" in header
    assert "edge-tts" in header


def test_directory_size(tmp_path: Path) -> None:
    (tmp_path / "a").write_bytes(b"12345")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b").write_bytes(b"123")
    assert bi.directory_size(tmp_path) == (8, 2)


def test_icon_is_valid_ico() -> None:
    data = (bi.PACKAGING / "luna.ico").read_bytes()
    assert data[:4] == b"\x00\x00\x01\x00"
    assert int.from_bytes(data[4:6], "little") >= 4


def test_spec_and_entry_compile() -> None:
    for name in ("luna.spec", "entry.py"):
        source = (bi.PACKAGING / name).read_text("utf-8")
        compile(source, name, "exec")


def test_main_rejects_version_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bi, "read_pyproject_version", lambda _p: "9.9.9")
    assert bi.main(["--skip-setup"]) == 1
