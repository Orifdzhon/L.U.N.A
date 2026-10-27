"""Setup stage of scripts/build_installer.py and the shape of packaging/luna.iss (ADR 0010, D2)."""

from pathlib import Path
from xml.etree import ElementTree

import build_installer as bi
import pytest


def test_company_name_is_publisher() -> None:
    assert bi.COMPANY_NAME == "Orifdzhon"
    assert "StringStruct('CompanyName', 'Orifdzhon')" in bi.version_resource_text("0.1.0")


def test_find_iscc_per_user_winget_folder(tmp_path: Path) -> None:
    folder = tmp_path / "Programs" / "Inno Setup 6"
    folder.mkdir(parents=True)
    (folder / "ISCC.exe").write_bytes(b"")
    assert bi.find_iscc({"LOCALAPPDATA": str(tmp_path)}) == folder / "ISCC.exe"


def test_find_iscc_inno_setup_7_glob(tmp_path: Path) -> None:
    folder = tmp_path / "Programs" / "Inno Setup 7"
    folder.mkdir(parents=True)
    (folder / "ISCC.exe").write_bytes(b"")
    assert bi.find_iscc({"LOCALAPPDATA": str(tmp_path)}) == folder / "ISCC.exe"


def test_strip_build_paths_removes_user_and_repo() -> None:
    text = "see C:\\Users\\Someone\\proj\\x.py and D:/Work/Repo/build/y and c:/users/Other/z"
    cleaned = bi.strip_build_paths(text, (Path("D:/Work/Repo"),))
    assert "Someone" not in cleaned
    assert "Other" not in cleaned
    assert "Work" not in cleaned
    assert "<user>" in cleaned


def test_strip_build_paths_keeps_ordinary_text() -> None:
    text = "MIT License\nhttps://example.org/x"
    assert bi.strip_build_paths(text) == text


def test_setup_filename_per_edition() -> None:
    assert bi.setup_filename("0.1.0", "public") == "LUNA-Setup-0.1.0-x64.exe"
    assert bi.setup_filename("0.1.0", "personal") == "LUNA-Setup-0.1.0-personal-x64.exe"


def test_iscc_command_passes_defines() -> None:
    texts = [str(p) for p in bi.iscc_command(Path("ISCC.exe"), "1.2.3", "personal")]
    assert "/DAppVersion=1.2.3" in texts
    assert "/DEdition=personal" in texts
    assert "/DAppPublisher=Orifdzhon" in texts
    assert texts[-1].endswith("luna.iss")


def test_sha256_of(tmp_path: Path) -> None:
    f = tmp_path / "a.bin"
    f.write_bytes(b"abc")
    assert bi.sha256_of(f) == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_sandbox_config_text_sets_host_folder() -> None:
    template = (bi.PACKAGING / "sandbox" / "luna-test.wsb").read_text("utf-8")
    text = bi.sandbox_config_text(template, Path("E:/my & dist"))
    assert "<HostFolder>E:\\my &amp; dist</HostFolder>" in text
    assert "<ReadOnly>true</ReadOnly>" in text


def test_sandbox_template_is_wellformed_xml() -> None:
    root = ElementTree.fromstring((bi.PACKAGING / "sandbox" / "luna-test.wsb").read_text("utf-8"))
    assert root.tag == "Configuration"
    assert root.findtext("MappedFolders/MappedFolder/ReadOnly") == "true"


def test_iss_matches_adr_decisions() -> None:
    from luna.platform.windows.autostart import SHORTCUT_NAME

    iss = (bi.PACKAGING / "luna.iss").read_text("utf-8-sig")
    assert "PrivilegesRequired=lowest" in iss
    assert "DefaultDirName={autopf}\\L.U.N.A\n" in iss
    assert "AppMutex=LUNA-instance" in iss
    assert "CloseApplications=yes" in iss
    assert 'Name: "autostart"; Description: "{cm:AutostartTask}"; Flags: unchecked' in iss
    # Inno appends ".lnk" to the icon name, so the file equals the tray toggle's shortcut.
    assert 'Name: "{userstartup}\\' + SHORTCUT_NAME.removesuffix(".lnk") + '"' in iss
    assert 'Parameters: "--tray --minimized"' in iss
    assert "onlyifdoesntexist uninsneveruninstall" in iss
    assert "MB_DEFBUTTON2" in iss
    assert "UninstallSilent()" in iss
    assert "--forget-keys" in iss


def _stub_freeze(monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    monkeypatch.setattr(bi, "freeze", lambda e, v: calls.append("freeze"))
    monkeypatch.setattr(bi, "self_check", lambda: calls.append("check"))
    monkeypatch.setattr(bi, "generate_notices", lambda e, t: calls.append("notices"))
    monkeypatch.setattr(bi, "directory_size", lambda p: (0, 0))


def test_main_builds_setup_after_freeze(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[str] = []
    _stub_freeze(monkeypatch, calls)
    monkeypatch.setattr(bi, "find_iscc", lambda: Path("ISCC.exe"))
    monkeypatch.setattr(bi, "build_setup", lambda i, v, e: calls.append("setup") or tmp_path)
    monkeypatch.setattr(bi, "write_sandbox_config", lambda: calls.append("wsb") or tmp_path)
    assert bi.main(["--edition", "personal"]) == 0
    assert calls == ["freeze", "check", "notices", "setup", "wsb"]


def test_main_reports_missing_iscc(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_freeze(monkeypatch, [])
    monkeypatch.setattr(bi, "find_iscc", lambda: None)
    assert bi.main([]) == 1
