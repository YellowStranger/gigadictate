"""GigaAM speech recognition through onnx-asr + onnxruntime (CPU, int8)."""

from __future__ import annotations

import gc
import hashlib
import logging
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

import numpy as np

log = logging.getLogger(__name__)

HF_REPO = "istupakov/gigaam-v3-onnx"
# A fixed commit, not the moving "main" branch: what we download is exactly what was tested.
HF_REVISION = "322c3b29492673eb7d0b434bfa9dfb8653e34d02"
HF_BASE_URL = f"https://huggingface.co/{HF_REPO}/resolve/{HF_REVISION}"
MODEL_FILES = {
    "gigaam-v3-e2e-rnnt": ["config.json", "LICENSE.txt", "v3_e2e_rnnt_vocab.txt",
                           "v3_e2e_rnnt_encoder.int8.onnx", "v3_e2e_rnnt_decoder.int8.onnx",
                           "v3_e2e_rnnt_joint.int8.onnx"],
    "gigaam-v3-e2e-ctc": ["config.json", "LICENSE.txt", "v3_e2e_ctc_vocab.txt", "v3_e2e_ctc.int8.onnx"],
}


# SHA-256 of every file at HF_REVISION; a download that doesn't match is thrown away.
MODEL_SHA256 = {
    "config.json": "0641fcf73f4af791c73f05083e38a658ff5dcbee3534a0c61396c10f4b97f0fe",
    "LICENSE.txt": "f00de6715714c7a63d08639cdbfaa40224eefc407302614bd19f1a8b98c875aa",
    "v3_e2e_rnnt_vocab.txt": "39abae20e692998290c574e606f11a9edef2902a1995463fcff63d1490cf22b7",
    "v3_e2e_rnnt_encoder.int8.onnx": "4e0e076a6076cd110277e529b8ac8f32cd5297f7fbebad5341ae8ddb7d00817b",
    "v3_e2e_rnnt_decoder.int8.onnx": "89014e134865615b91e037157e46e389b1271e6072460efc010ea08e61e23146",
    "v3_e2e_rnnt_joint.int8.onnx": "ade116563dbf66e503b0994efab6b5861412743e52bf31c39fc3fffa3783d5d1",
    "v3_e2e_ctc_vocab.txt": "142de7570b3de5b3035ce111a89c228e80e6085273731d944093ddf24fa539cd",
    "v3_e2e_ctc.int8.onnx": "2e3fcb7a7b66030336fd10c2fcfb033bd1dc7e1bf238fe5cfd83b1d0cfc9d28e",
}


class ModelMissing(RuntimeError):
    pass


def _bundled_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent / "models"
    return Path(__file__).resolve().parents[2] / "models"


def _user_root() -> Path:
    from .config import app_dir

    return app_dir() / "models"


def models_root() -> Path:
    """Where downloads go: next to the exe (portable build) if writable, else the user's app-data."""
    b = _bundled_root()
    try:
        b.mkdir(parents=True, exist_ok=True)
        probe = b / ".write-test"
        probe.write_bytes(b"")
        probe.unlink()
        return b
    except OSError:
        return _user_root()


def _complete(d: Path, name: str) -> bool:
    return all((d / f).is_file() and (d / f).stat().st_size > 0 for f in MODEL_FILES[name])


def model_dir(name: str, root: Path | None = None) -> Path:
    if root is not None:
        return root / name
    for r in (_bundled_root(), _user_root()):
        if _complete(r / name, name):
            return r / name
    return models_root() / name


def is_model_present(name: str, root: Path | None = None) -> bool:
    return _complete(model_dir(name, root), name)


class _NoDowngradeRedirect(urllib.request.HTTPRedirectHandler):
    """urllib follows https -> http redirects by default; never leave TLS once we started on it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if req.full_url.startswith("https:") and not newurl.startswith("https:"):
            raise urllib.error.HTTPError(newurl, code, "redirect away from https refused", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_NoDowngradeRedirect)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def download_model(name: str, root: Path | None = None,
                   progress: Callable[[int, int], None] | None = None,
                   base_url: str = HF_BASE_URL, sha256: dict[str, str] | None = None) -> Path:
    """Download into <dir>.partial, check every file's SHA-256, then rename, so neither an
    interrupted nor a tampered download ever looks like an installed model."""
    sha256 = MODEL_SHA256 if sha256 is None else sha256
    d = (root or models_root()) / name
    tmp = d.with_name(d.name + ".partial")
    tmp.mkdir(parents=True, exist_ok=True)
    files = MODEL_FILES[name]
    missing = [f for f in files if f not in sha256]
    if missing:
        raise ValueError(f"нет контрольной суммы для {missing}")
    sizes: dict[str, int] = {}
    for f in files:
        req = urllib.request.Request(f"{base_url}/{f}", method="HEAD")
        with _opener.open(req, timeout=30) as r:
            sizes[f] = int(r.headers.get("Content-Length") or 0)
    total, done = sum(sizes.values()), 0
    for f in files:
        dst = tmp / f
        if dst.exists() and sizes[f] and dst.stat().st_size == sizes[f] and _sha256_file(dst) == sha256[f]:
            done += sizes[f]  # left over from an interrupted run and intact
            continue
        h = hashlib.sha256()
        with _opener.open(f"{base_url}/{f}", timeout=60) as r, open(dst, "wb") as out:
            while True:
                block = r.read(1 << 20)
                if not block:
                    break
                out.write(block)
                h.update(block)
                done += len(block)
                if progress:
                    progress(done, total)
        if h.hexdigest() != sha256[f]:
            dst.unlink()
            raise IOError(f"{f}: контрольная сумма не совпадает, файл повреждён или подменён")
    if d.exists():
        import shutil

        shutil.rmtree(d)
    os.replace(tmp, d)
    return d


def default_threads() -> int:
    # Half of the logical cores (leave room for the user's apps), at most 4:
    # beyond 4 the int8 encoder barely gets faster but the PC gets busier.
    return max(1, min(4, (os.cpu_count() or 2) // 2))


class Engine:
    def __init__(self, name: str, threads: int = 0, root: Path | None = None):
        self.name = name
        self.threads = threads or default_threads()
        self.root = root
        self._model = None
        self._lock = threading.Lock()
        self.last_used = time.monotonic()

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def load(self) -> None:
        with self._lock:
            self._load_locked()

    def _load_locked(self) -> None:
        if self._model is not None:
            return
        if not is_model_present(self.name, self.root):
            raise ModelMissing(self.name)
        import onnx_asr
        import onnxruntime as rt

        so = rt.SessionOptions()
        so.intra_op_num_threads = self.threads
        so.inter_op_num_threads = 1
        so.execution_mode = rt.ExecutionMode.ORT_SEQUENTIAL
        # Without this onnxruntime worker threads busy-wait after every run and burn CPU.
        so.add_session_config_entry("session.intra_op.allow_spinning", "0")
        so.add_session_config_entry("session.inter_op.allow_spinning", "0")
        so.log_severity_level = 3
        t = time.monotonic()
        self._model = onnx_asr.load_model(
            self.name, model_dir(self.name, self.root), quantization="int8",
            sess_options=so, providers=["CPUExecutionProvider"],
        )
        log.info("model %s loaded in %.2fs (%d threads)", self.name, time.monotonic() - t, self.threads)
        self.last_used = time.monotonic()

    def recognize(self, audio: np.ndarray, sample_rate: int = 16000) -> str:
        with self._lock:
            self._load_locked()
            t = time.monotonic()
            text = self._model.recognize(np.ascontiguousarray(audio, dtype=np.float32), sample_rate=sample_rate)
            self.last_used = time.monotonic()
            log.info("recognized %.1fs audio in %.2fs -> %d chars",
                     len(audio) / sample_rate, self.last_used - t, len(text))
            return text

    def unload(self) -> None:
        with self._lock:
            if self._model is None:
                return
            self._model = None
        gc.collect()
        if sys.platform == "win32":
            try:  # give freed pages back to Windows right away
                import ctypes

                ctypes.windll.psapi.EmptyWorkingSet(ctypes.c_void_p(-1))  # -1 = current process
            except Exception:
                pass
        log.info("model unloaded")
