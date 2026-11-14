import http.server
import io
import struct
import threading
import wave
from functools import partial

import numpy as np
import pytest

from gigadictate import assets
from gigadictate.engine import MODEL_FILES, default_threads, download_model, is_model_present


@pytest.mark.parametrize("size", [16, 20, 24, 32, 48])
def test_icon_resource_layout(size):
    rgba = assets.render_icon_rgba(size, assets.COLORS["recording"])
    assert rgba.shape == (size, size, 4)
    assert rgba[0, 0, 3] == 0  # transparent corner (round badge)
    assert rgba[size // 2, size // 2, 3] == 255
    data = assets.icon_resource(rgba)
    hdr = struct.unpack("<IiiHHIIiiII", data[:40])
    assert hdr[0] == 40 and hdr[1] == size and hdr[2] == 2 * size and hdr[4] == 32
    assert len(data) == 40 + size * size * 4 + ((size + 31) // 32) * 4 * size


def test_icon_has_white_microphone():
    rgba = assets.render_icon_rgba(32, assets.COLORS["idle"])
    white = (rgba[..., :3] > 240).all(axis=-1) & (rgba[..., 3] > 0)
    assert 40 < white.sum() < 400


def test_sounds_are_valid_and_quiet():
    for freqs, dur in assets.SOUNDS.values():
        data = assets.tone_wav(freqs, dur)
        with wave.open(io.BytesIO(data)) as w:
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
        assert np.abs(pcm).max() < 0.25 * 32767
        assert abs(int(pcm[0])) < 300 and abs(int(pcm[-1])) < 300  # faded: no click


def test_default_threads_bounded():
    assert 1 <= default_threads() <= 4


class _Handler(http.server.SimpleHTTPRequestHandler):
    fail_once: set = set()

    def log_message(self, *a):
        pass


@pytest.fixture
def fake_hub(tmp_path):
    srv_dir = tmp_path / "hub"
    srv_dir.mkdir()
    for f in MODEL_FILES["gigaam-v3-e2e-ctc"]:
        (srv_dir / f).write_bytes(f.encode() * 1000)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), partial(_Handler, directory=str(srv_dir)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", srv_dir
    httpd.shutdown()


def test_download_model(tmp_path, fake_hub):
    url, _ = fake_hub
    root = tmp_path / "models"
    seen = []
    download_model("gigaam-v3-e2e-ctc", root, progress=lambda d, t: seen.append((d, t)), base_url=url)
    assert is_model_present("gigaam-v3-e2e-ctc", root)
    assert seen[-1][0] == seen[-1][1]
    assert not (root / "gigaam-v3-e2e-ctc.partial").exists()


def test_interrupted_download_is_not_mistaken_for_complete(tmp_path, fake_hub):
    url, srv = fake_hub
    root = tmp_path / "models"
    (srv / "v3_e2e_ctc.int8.onnx").unlink()  # server breaks halfway
    with pytest.raises(OSError):  # HTTP 404 from the hub
        download_model("gigaam-v3-e2e-ctc", root, base_url=url)
    assert not is_model_present("gigaam-v3-e2e-ctc", root)
    (srv / "v3_e2e_ctc.int8.onnx").write_bytes(b"x" * 5000)  # resumes and completes
    download_model("gigaam-v3-e2e-ctc", root, base_url=url)
    assert is_model_present("gigaam-v3-e2e-ctc", root)
