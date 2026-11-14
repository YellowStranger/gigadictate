"""GigaAM speech recognition through onnx-asr + onnxruntime (CPU, int8)."""

from __future__ import annotations

import gc
import logging
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable

import numpy as np

log = logging.getLogger(__name__)

HF_REPO = "istupakov/gigaam-v3-onnx"
MODEL_FILES = {
    "gigaam-v3-e2e-rnnt": ["config.json", "LICENSE.txt", "v3_e2e_rnnt_vocab.txt",
                           "v3_e2e_rnnt_encoder.int8.onnx", "v3_e2e_rnnt_decoder.int8.onnx",
                           "v3_e2e_rnnt_joint.int8.onnx"],
    "gigaam-v3-e2e-ctc": ["config.json", "LICENSE.txt", "v3_e2e_ctc_vocab.txt", "v3_e2e_ctc.int8.onnx"],
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


def download_model(name: str, root: Path | None = None,
                   progress: Callable[[int, int], None] | None = None,
                   base_url: str = f"https://huggingface.co/{HF_REPO}/resolve/main") -> Path:
    """Download into <dir>.partial and rename at the end, so an interrupted download never
    leaves a half-written model that looks complete."""
    d = (root or models_root()) / name
    tmp = d.with_name(d.name + ".partial")
    tmp.mkdir(parents=True, exist_ok=True)
    files = MODEL_FILES[name]
    sizes: dict[str, int] = {}
    for f in files:
        req = urllib.request.Request(f"{base_url}/{f}", method="HEAD")
        with urllib.request.urlopen(req, timeout=30) as r:
            sizes[f] = int(r.headers.get("Content-Length") or 0)
    total, done = sum(sizes.values()), 0
    for f in files:
        dst = tmp / f
        if dst.exists() and sizes[f] and dst.stat().st_size == sizes[f]:
            done += sizes[f]
            continue
        with urllib.request.urlopen(f"{base_url}/{f}", timeout=60) as r, open(dst, "wb") as out:
            while True:
                block = r.read(1 << 20)
                if not block:
                    break
                out.write(block)
                done += len(block)
                if progress:
                    progress(done, total)
        if sizes[f] and dst.stat().st_size != sizes[f]:
            raise IOError(f"{f}: размер {dst.stat().st_size} != {sizes[f]}")
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
