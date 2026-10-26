from __future__ import annotations

import hashlib
import io
import os
import time
import zipfile
from pathlib import Path

import httpx
import pytest

from luna.core.errors import ProviderUnavailable
from luna.voice import wake_model
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
    (tmp_path / "models" / NAME / "am").mkdir(parents=True)
    (tmp_path / "models" / NAME / "am" / "final.mdl").write_bytes(b"1")

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
        f"{NAME}/NUL",
        f"{NAME}/aux.txt",
        f"{NAME}/trailing.",
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


def test_slow_drip_trips_deadline(tmp_path: Path) -> None:
    def drip():
        while True:
            yield b"x"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=drip())

    ticks = iter(range(0, 10_000_000, 29))  # one byte per 29 s of fake time
    s = WakeModelStore(
        tmp_path / "models",
        deadline_s=100,
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)),
        clock=lambda: float(next(ticks)),
    )
    with pytest.raises(ProviderUnavailable, match="timed out"):
        s.ensure()
    assert leftovers(tmp_path) == []


def test_streamed_download_cap_without_content_length(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=iter([b"x" * 100] * 50))

    s = WakeModelStore(
        tmp_path / "models",
        max_download_bytes=1000,
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ProviderUnavailable, match="aborted"):
        s.ensure()
    assert leftovers(tmp_path) == []


def test_content_length_precheck_stops_before_reading(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        def body():
            raise AssertionError("body must not be read")
            yield b""

        return httpx.Response(200, headers={"content-length": "999999"}, content=body())

    s = WakeModelStore(
        tmp_path / "models",
        max_download_bytes=1000,
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ProviderUnavailable, match="larger than expected"):
        s.ensure()
    assert leftovers(tmp_path) == []


def test_streamed_unpacked_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wake_model, "sum", lambda _items: 0, raising=False)  # skip header check
    server = Server(make_zip({f"{NAME}/big": b"0" * 5000}))
    with pytest.raises(ProviderUnavailable, match="too much data"):
        store(tmp_path, server, max_unpacked_bytes=1000).ensure()
    assert leftovers(tmp_path) == []


def test_empty_target_folder_is_replaced(tmp_path: Path) -> None:
    (tmp_path / "models" / NAME).mkdir(parents=True)
    server = Server(make_zip())
    s = store(tmp_path, server)
    assert not s.is_present()
    path = s.ensure()
    assert (path / "am" / "final.mdl").read_bytes() == b"model"
    assert leftovers(tmp_path) == []


def test_incomplete_target_folder_is_replaced(tmp_path: Path) -> None:
    target = tmp_path / "models" / NAME
    (target / "graph").mkdir(parents=True)
    (target / "graph" / "junk").write_bytes(b"1")
    server = Server(make_zip())
    path = store(tmp_path, server).ensure()
    assert (path / "am" / "final.mdl").exists()
    assert not (path / "graph").exists()
    assert leftovers(tmp_path) == []


def test_publish_lost_race_keeps_winner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    server = Server(make_zip())
    s = store(tmp_path, server)

    def racing_replace(src, dst):
        (Path(dst) / "am").mkdir(parents=True)
        (Path(dst) / "am" / "final.mdl").write_bytes(b"winner")
        raise PermissionError("lost the race")

    monkeypatch.setattr(wake_model.os, "replace", racing_replace)
    path = s.ensure()
    monkeypatch.undo()
    assert (path / "am" / "final.mdl").read_bytes() == b"winner"
    assert leftovers(tmp_path) == []


def test_publish_failure_is_mapped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = store(tmp_path, Server(make_zip()))

    def failing_replace(src, dst):
        raise PermissionError("denied")

    monkeypatch.setattr(wake_model.os, "replace", failing_replace)
    with pytest.raises(ProviderUnavailable, match="could not install"):
        s.ensure()


def test_is_present_survives_os_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def deny(self):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "is_file", deny)
    assert WakeModelStore(tmp_path / "models").is_present() is False


def test_stale_temp_dirs_are_swept(tmp_path: Path) -> None:
    models = tmp_path / "models"
    old = models / ".wake-model-old"
    fresh = models / ".wake-model-fresh"
    for d in (old, fresh):
        (d / "unpacked").mkdir(parents=True)
    past = time.time() - 7200
    os.utime(old, (past, past))
    store(tmp_path, Server(make_zip())).ensure()
    assert not old.exists()
    assert fresh.exists()


def test_extraction_oserror_is_mapped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def disk_full(self, *a, **k):
        raise OSError(28, "No space left on device")

    server = Server(make_zip())
    s = store(tmp_path, server)
    monkeypatch.setattr(WakeModelStore, "_extract_members", disk_full)
    with pytest.raises(ProviderUnavailable, match="free disk space"):
        s.ensure()
    assert leftovers(tmp_path) == []


def test_unsupported_compression_is_mapped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def unsupported(self, *a, **k):
        raise NotImplementedError("compression type 99")

    s = store(tmp_path, Server(make_zip()))
    monkeypatch.setattr(WakeModelStore, "_extract_members", unsupported)
    with pytest.raises(ProviderUnavailable, match="unsupported"):
        s.ensure()
