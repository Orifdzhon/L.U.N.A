from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

import httpx
import pytest

from luna.core.errors import ProviderUnavailable
from luna.voice.wake_model import MODEL_SHA256, MODEL_URL, WakeModelStore

NAME = "vosk-model-small-ru-0.22"


def make_zip(entries: dict[str, bytes] | None = None, *, symlink: str | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        if entries is None:
            entries = {f"{NAME}/am/final.mdl": b"model", f"{NAME}/conf/mfcc.conf": b"conf"}
        for name, data in entries.items():
            zf.writestr(name, data)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = 0o120777 << 16
            zf.writestr(info, "/etc/passwd")
    return buf.getvalue()


class Server:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self.body = body
        self.status = status
        self.calls = 0

    def factory(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.calls += 1
            assert str(request.url) == MODEL_URL
            return httpx.Response(self.status, content=self.body)

        return lambda: httpx.Client(transport=httpx.MockTransport(handler))


def store(tmp_path: Path, server: Server, body: bytes | None = None, **kw) -> WakeModelStore:
    sha = hashlib.sha256(server.body if body is None else body).hexdigest()
    return WakeModelStore(tmp_path / "models", sha256=sha, client_factory=server.factory(), **kw)


def leftovers(tmp_path: Path) -> list[str]:
    return [p.name for p in (tmp_path / "models").iterdir() if p.name != NAME]


def test_pinned_constants() -> None:
    assert f"https://alphacephei.com/vosk/models/{NAME}.zip" == MODEL_URL
    assert len(MODEL_SHA256) == 64


def test_downloads_and_extracts(tmp_path: Path) -> None:
    server = Server(make_zip())
    path = store(tmp_path, server).ensure()
    assert path == tmp_path / "models" / NAME
    assert (path / "am" / "final.mdl").read_bytes() == b"model"
    assert leftovers(tmp_path) == []


def test_idempotent_no_network_when_present(tmp_path: Path) -> None:
    server = Server(make_zip())
    s = store(tmp_path, server)
    s.ensure()
    assert server.calls == 1
    assert s.ensure() == s.model_path
    assert server.calls == 1


def test_present_model_needs_no_client(tmp_path: Path) -> None:
    (tmp_path / "models" / NAME).mkdir(parents=True)
    (tmp_path / "models" / NAME / "x").write_bytes(b"1")

    def boom() -> httpx.Client:
        raise AssertionError("network used")

    assert WakeModelStore(tmp_path / "models", client_factory=boom).ensure().is_dir()


def test_hash_mismatch(tmp_path: Path) -> None:
    server = Server(make_zip())
    with pytest.raises(ProviderUnavailable, match="SHA-256"):
        store(tmp_path, server, body=b"other").ensure()
    assert not (tmp_path / "models" / NAME).exists()
    assert leftovers(tmp_path) == []


def test_http_error(tmp_path: Path) -> None:
    with pytest.raises(ProviderUnavailable, match="HTTP 404"):
        store(tmp_path, Server(b"", 404), body=b"").ensure()
    assert leftovers(tmp_path) == []


def test_redirect_is_not_followed(tmp_path: Path) -> None:
    with pytest.raises(ProviderUnavailable, match="HTTP 302"):
        store(tmp_path, Server(b"", 302), body=b"").ensure()


def test_network_error(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    s = WakeModelStore(
        tmp_path / "models",
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ProviderUnavailable, match="internet"):
        s.ensure()
    assert leftovers(tmp_path) == []


def test_download_size_cap(tmp_path: Path) -> None:
    server = Server(make_zip())
    with pytest.raises(ProviderUnavailable, match="larger than expected"):
        store(tmp_path, server, max_download_bytes=10).ensure()
    assert leftovers(tmp_path) == []


def test_deadline(tmp_path: Path) -> None:
    ticks = iter(range(0, 10_000, 1000))
    server = Server(make_zip())
    with pytest.raises(ProviderUnavailable, match="timed out"):
        store(tmp_path, server, clock=lambda: float(next(ticks)), deadline_s=5).ensure()


@pytest.mark.parametrize(
    "bad",
    [
        "/abs/evil.txt",
        f"{NAME}/../../evil.txt",
        "../evil.txt",
        "C:/evil.txt",
        f"{NAME}\\..\\evil.txt",
        "other-top/file",
        "loose.txt",
    ],
)
def test_zip_slip_rejected(tmp_path: Path, bad: str) -> None:
    server = Server(make_zip({f"{NAME}/ok": b"1", bad: b"x"}))
    with pytest.raises(ProviderUnavailable, match="unsafe path"):
        store(tmp_path, server).ensure()
    assert leftovers(tmp_path) == []
    assert not (tmp_path / "evil.txt").exists()


def test_symlink_rejected(tmp_path: Path) -> None:
    server = Server(make_zip(symlink=f"{NAME}/link"))
    with pytest.raises(ProviderUnavailable, match="symbolic link"):
        store(tmp_path, server).ensure()
    assert leftovers(tmp_path) == []


def test_unpacked_size_cap(tmp_path: Path) -> None:
    server = Server(make_zip({f"{NAME}/big": b"0" * 5000}))
    with pytest.raises(ProviderUnavailable, match="too much data"):
        store(tmp_path, server, max_unpacked_bytes=1000).ensure()
    assert leftovers(tmp_path) == []


def test_corrupt_archive(tmp_path: Path) -> None:
    server = Server(b"not a zip")
    with pytest.raises(ProviderUnavailable, match="corrupt"):
        store(tmp_path, server).ensure()
    assert leftovers(tmp_path) == []


def test_missing_model_folder(tmp_path: Path) -> None:
    # Only a directory entry with another name is rejected by the path rule; an empty zip
    # has no model folder at all.
    server = Server(make_zip({}))
    with pytest.raises(ProviderUnavailable, match="no 'vosk-model"):
        store(tmp_path, server).ensure()


def test_model_dir_default_used_when_none(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert WakeModelStore().model_path == tmp_path / "LUNA" / "models" / NAME
