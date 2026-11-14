"""Microphone capture. The stream is opened only while dictating: no CPU use and
no "microphone in use" indicator the rest of the time."""

from __future__ import annotations

import collections
import logging
import threading
import time
import wave
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)
SR = 16000


class AudioError(RuntimeError):
    pass


class LinearResampler:
    """Streaming resampler to 16 kHz for devices that refuse 16 kHz capture."""

    def __init__(self, src_rate: int, dst_rate: int = SR):
        self.step = src_rate / dst_rate
        # Position of the next output sample in coordinates of buf = [prev, *chunk];
        # starts at 1.0 = the first real input sample.
        self.pos = 1.0
        self.prev = np.zeros(1, dtype=np.float32)

    def __call__(self, x: np.ndarray) -> np.ndarray:
        if self.step == 1.0:
            return x
        buf = np.concatenate([self.prev, x])  # buf[0] is the last sample of the previous chunk
        n = int(np.floor((len(buf) - 1 - self.pos) / self.step)) + 1
        if n <= 0:
            self.pos -= len(x)
            self.prev = buf[-1:]
            return np.zeros(0, dtype=np.float32)
        idx = self.pos + self.step * np.arange(n)
        out = np.interp(idx, np.arange(len(buf)), buf).astype(np.float32)
        self.pos = idx[-1] + self.step - len(x)
        self.prev = buf[-1:]
        return out


class Recorder:
    def __init__(self, device: str = ""):
        self.device_name = device
        self._q: collections.deque[np.ndarray] = collections.deque()
        self._stream = None
        self._resample: LinearResampler | None = None
        self.error: str | None = None
        self.level = 0.0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def _pick_device(self, sd) -> int | None:
        if not self.device_name:
            return None
        want = self.device_name.lower()
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0 and want in d["name"].lower():
                return i
        log.warning("input device %r not found, using default", self.device_name)
        return None

    def _open(self, sd):
        dev = self._pick_device(sd)
        try:
            stream = sd.InputStream(device=dev, samplerate=SR, channels=1, dtype="float32",
                                    blocksize=SR // 10, callback=self._callback)
            self._resample = None
        except Exception as e:  # device does not do 16 kHz -> capture natively and resample
            info = sd.query_devices(dev, "input")
            rate = int(info["default_samplerate"])
            log.info("16 kHz not supported (%s), capturing at %d Hz", e, rate)
            stream = sd.InputStream(device=dev, samplerate=rate, channels=1, dtype="float32",
                                    blocksize=rate // 10, callback=self._callback)
            self._resample = LinearResampler(rate)
        stream.start()
        return stream

    def start(self) -> None:
        import sounddevice as sd

        self._q.clear()
        self.error = None
        try:
            self._stream = self._open(sd)
        except Exception as first:
            # PortAudio caches the device list at startup: a headset plugged in later is
            # only visible after re-initialising it.
            log.info("opening microphone failed (%s), refreshing device list", first)
            try:
                sd._terminate()
                sd._initialize()
                self._stream = self._open(sd)
            except Exception as e:
                self._stream = None
                raise AudioError(f"Не удалось открыть микрофон: {e}") from e

    def _callback(self, indata, frames, time_info, status) -> None:
        if status and status.input_overflow:
            log.debug("input overflow")
        x = indata[:, 0].copy()
        if self._resample:
            x = self._resample(x)
        self._q.append(x)
        if len(x):
            self.level = float(np.sqrt(np.mean(x * x)))

    def read(self) -> np.ndarray:
        chunks = []
        while self._q:
            chunks.append(self._q.popleft())
        if self._stream is not None and not self._stream.active and self.error is None:
            self.error = "Микрофон отключился во время записи"
        return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)

    def stop(self, tail_s: float = 0.0) -> np.ndarray:
        if tail_s > 0 and self._stream is not None:
            time.sleep(tail_s)
        self._close()
        return self.read()

    def abort(self) -> None:
        self._close()
        self._q.clear()

    def _close(self) -> None:
        with self._lock:
            s, self._stream = self._stream, None
        if s is not None:
            try:
                s.stop()
                s.close()
            except Exception as e:
                log.debug("closing stream: %s", e)


class WavRecorder(Recorder):
    """Plays a WAV file as if it were the microphone (automated end-to-end tests)."""

    def __init__(self, path: str | Path, realtime: bool = True):
        super().__init__()
        with wave.open(str(path)) as w:
            assert w.getframerate() == SR and w.getnchannels() == 1 and w.getsampwidth() == 2
            self._data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
        self.realtime = realtime
        self._t0 = 0.0
        self._pos = 0
        self._open_ = False

    def start(self) -> None:
        self._t0, self._pos, self._open_ = time.monotonic(), 0, True

    def read(self) -> np.ndarray:
        if not self._open_:
            return np.zeros(0, dtype=np.float32)
        end = len(self._data) if not self.realtime else min(len(self._data), int((time.monotonic() - self._t0) * SR))
        out, self._pos = self._data[self._pos:end], max(self._pos, end)
        return out

    def stop(self, tail_s: float = 0.0) -> np.ndarray:
        out = self.read()
        self._open_ = False
        return out

    def abort(self) -> None:
        self._open_ = False
