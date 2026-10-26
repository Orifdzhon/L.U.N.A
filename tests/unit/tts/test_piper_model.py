"""Tests for luna.tts.piper_model (pinned download, SHA-256, safe tar.bz2 extraction)."""

from __future__ import annotations

import hashlib
import io
import tarfile

import httpx
import pytest

from luna.core.errors import ProviderUnavailable
from luna.tts.piper_model import VOICES, PiperModelStore, VoiceSpec

KEY = "xx_XX-test-medium"
FOLDER = f"vits-piper-{KEY}"


def add_file(tf: tarfile.TarFile, name: str, data: bytes = b"data") -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    tf.addfile(info, io.BytesIO(data))


def good_members(tf: tarfile.TarFile) -> None:
    info = tarfile.TarInfo(FOLDER)
    info.type = tarfile.DIRTYPE
    tf.addfile(info)
    add_file(tf, f"{FOLDER}/{KEY}.onnx", b"onnx" * 10)
    add_file(tf, f"{FOLDER}/tokens.txt", b"a 1\n")
    add_file(tf, f"{FOLDER}/espeak-ng-data/ru_dict", b"dict")


def make_tar(members=good_members) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:bz2") as tf:
        members(tf)
    return buf.getvalue()


def spec_for(data: bytes) -> VoiceSpec:
    return VoiceSpec(KEY, "ru", hashlib.sha256(data).hexdigest(), len(data))


class Served:
    """httpx client factory serving one body; counts requests."""

    def __init__(self, body: bytes, status: int = 200) -> None:
        self.body, self.status, self.requests = body, status, []

    def __call__(self) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(str(request.url))
            return httpx.Response(self.status, content=self.body)

        return httpx.Client(transport=httpx.MockTransport(handler))


def store(
    tmp_path, data: bytes, served: Served | None = None, **kw
) -> tuple[PiperModelStore, Served]:
    served = served or Served(data)
    spec = spec_for(data)
    return (
        PiperModelStore(tmp_path / "models", voices={KEY: spec}, client_factory=served, **kw),
        served,
    )


def test_pinned_catalogue_is_consistent():
    assert set(VOICES) == {"ru_RU-irina-medium", "en_US-kristin-medium"}
    for key, spec in VOICES.items():
        assert spec.key == key
        assert len(spec.sha256) == 64
        assert spec.url == (
            "https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/"
            f"vits-piper-{key}.tar.bz2"
        )
    assert VOICES["ru_RU-irina-medium"].lang == "ru"
    assert VOICES["en_US-kristin-medium"].lang == "en"


def test_downloads_verifies_and_unpacks(tmp_path):
    data = make_tar()
    st, served = store(tmp_path, data)
    path = st.ensure(KEY)
    assert path == tmp_path / "models" / FOLDER
    assert (path / f"{KEY}.onnx").read_bytes() == b"onnx" * 10
    assert (path / "tokens.txt").is_file()
    assert (path / "espeak-ng-data").is_dir()
    assert served.requests == [spec_for(data).url]
    # no temp leftovers
    assert [p.name for p in (tmp_path / "models").iterdir()] == [FOLDER]


def test_second_ensure_is_idempotent_and_offline(tmp_path):
    data = make_tar()
    st, served = store(tmp_path, data)
    first = st.ensure(KEY)
    second = st.ensure(KEY)
    assert first == second
    assert len(served.requests) == 1
    assert st.is_present(KEY)


def test_hash_mismatch_fails_and_leaves_nothing(tmp_path):
    data = make_tar()
    served = Served(data + b"tampered")
    st, _ = store(tmp_path, data, served)
    with pytest.raises(ProviderUnavailable, match="SHA-256"):
        st.ensure(KEY)
    assert not st.is_present(KEY)
    assert list((tmp_path / "models").iterdir()) == []


def test_http_error_status(tmp_path):
    data = make_tar()
    st, _ = store(tmp_path, data, Served(b"", status=404))
    with pytest.raises(ProviderUnavailable, match="HTTP 404"):
        st.ensure(KEY)


def test_network_error(tmp_path):
    def factory() -> httpx.Client:
        def handler(request):
            raise httpx.ConnectError("boom")

        return httpx.Client(transport=httpx.MockTransport(handler))

    data = make_tar()
    st = PiperModelStore(tmp_path / "m", voices={KEY: spec_for(data)}, client_factory=factory)
    with pytest.raises(ProviderUnavailable, match="internet connection"):
        st.ensure(KEY)


def test_download_size_cap(tmp_path):
    data = make_tar()
    st, _ = store(tmp_path, data, max_download_bytes=10)
    with pytest.raises(ProviderUnavailable, match="larger than expected"):
        st.ensure(KEY)


def test_download_deadline(tmp_path):
    data = make_tar()
    ticks = iter(range(0, 10_000, 100))
    st, _ = store(tmp_path, data, deadline_s=50, clock=lambda: float(next(ticks)))
    with pytest.raises(ProviderUnavailable, match="timed out"):
        st.ensure(KEY)


def _install_bad(tmp_path, members, match, **kw):
    data = make_tar(members)
    st, _ = store(tmp_path, data, **kw)
    with pytest.raises(ProviderUnavailable, match=match):
        st.ensure(KEY)
    assert not st.is_present(KEY)
    assert list((tmp_path / "models").iterdir()) == []  # temp dir cleaned, nothing published


@pytest.mark.parametrize(
    "name",
    [
        f"{FOLDER}/../evil.txt",
        "../evil.txt",
        "/abs/evil.txt",
        "C:/evil.txt",
        f"{FOLDER}\\evil.txt",
        "other-folder/file.txt",
        f"{FOLDER}/con",
        f"{FOLDER}/aux.txt",
        f"{FOLDER}/trailing.",
    ],
)
def test_path_traversal_and_unsafe_names_rejected(tmp_path, name):
    def members(tf):
        good_members(tf)
        add_file(tf, name)

    _install_bad(tmp_path, members, "unsafe path")
    assert not (tmp_path / "evil.txt").exists()


@pytest.mark.parametrize(
    "kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.FIFOTYPE]
)
def test_links_and_special_files_rejected(tmp_path, kind):
    def members(tf):
        good_members(tf)
        info = tarfile.TarInfo(f"{FOLDER}/link")
        info.type = kind
        info.linkname = "/etc/passwd"
        tf.addfile(info)

    _install_bad(tmp_path, members, "link or special")


def test_too_many_entries(tmp_path):
    _install_bad(tmp_path, good_members, "too many entries", max_entries=2)


def test_unpacked_size_cap(tmp_path):
    _install_bad(tmp_path, good_members, "too much data", max_unpacked_bytes=20)


def test_missing_expected_files(tmp_path):
    def members(tf):
        info = tarfile.TarInfo(FOLDER)
        info.type = tarfile.DIRTYPE
        tf.addfile(info)
        add_file(tf, f"{FOLDER}/tokens.txt")

    _install_bad(tmp_path, members, "lacks the expected files")


def test_corrupt_archive(tmp_path):
    data = b"this is not a tar.bz2 at all" * 10
    st, _ = store(tmp_path, data)
    with pytest.raises(ProviderUnavailable, match="corrupt"):
        st.ensure(KEY)
    assert list((tmp_path / "models").iterdir()) == []


def test_unknown_voice(tmp_path):
    st, _ = store(tmp_path, make_tar())
    with pytest.raises(ProviderUnavailable, match="unknown Piper voice"):
        st.ensure("nope")


def test_partial_folder_is_not_a_present_voice_and_is_replaced(tmp_path):
    data = make_tar()
    st, _ = store(tmp_path, data)
    junk = tmp_path / "models" / FOLDER
    junk.mkdir(parents=True)
    (junk / "tokens.txt").write_text("half")
    assert not st.is_present(KEY)
    st.ensure(KEY)
    assert st.is_present(KEY)


def test_models_dir_not_creatable(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    data = make_tar()
    st = PiperModelStore(blocker / "sub", voices={KEY: spec_for(data)}, client_factory=Served(data))
    with pytest.raises(ProviderUnavailable, match="models folder"):
        st.ensure(KEY)
