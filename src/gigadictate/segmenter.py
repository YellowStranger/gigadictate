"""Cutting a live recording into pieces the model handles well.

GigaAM is trained on utterances up to ~25 s and attention cost grows
quadratically, so long dictations are split at pauses. Pieces are recognised
in the background *while the user is still talking*, so after the hotkey is
released only the last piece is left to process.
"""

from __future__ import annotations

import numpy as np

SR = 16000
FRAME = SR * 30 // 1000  # 30 ms


def frame_rms(audio: np.ndarray) -> np.ndarray:
    n = len(audio) // FRAME
    if n == 0:
        return np.zeros(0, dtype=np.float32)
    frames = audio[: n * FRAME].reshape(n, FRAME).astype(np.float32)
    return np.sqrt(np.mean(frames * frames, axis=1) + 1e-12)


def is_digital_silence(audio: np.ndarray) -> bool:
    """All (near) zeros: microphone access blocked by Windows privacy settings or muted device."""
    return len(audio) > 0 and float(np.max(np.abs(audio))) < 1e-4


def has_speech(audio: np.ndarray, min_level: float = 0.004) -> bool:
    """Cheap gate so that silence is never sent to the model (no hallucinated words)."""
    rms = frame_rms(audio)
    if len(rms) < 5:
        return False
    loud = np.sort(rms)[-max(3, len(rms) // 20):]  # top 5% frames
    return float(np.median(loud)) >= min_level


def normalize(audio: np.ndarray, target_peak: float = 0.7, max_gain: float = 8.0) -> np.ndarray:
    """Bring quiet microphones up to a normal level; never amplify noise more than max_gain."""
    if len(audio) == 0:
        return audio
    peak = float(np.percentile(np.abs(audio), 99.9))
    if peak <= 1e-6:
        return audio
    gain = min(max_gain, target_peak / peak)
    if gain <= 1.0:
        return audio
    return np.clip(audio * gain, -1.0, 1.0).astype(np.float32)


class Segmenter:
    def __init__(self, min_s: float = 12.0, max_s: float = 24.0, pause_ms: int = 450):
        self.min_frames = int(min_s * 1000 / 30)
        self.max_frames = int(max_s * 1000 / 30)
        self.pause_frames = max(1, pause_ms // 30)
        self._chunks: list[np.ndarray] = []
        self._len = 0

    def __len__(self) -> int:
        return self._len

    def feed(self, chunk: np.ndarray) -> list[np.ndarray]:
        if len(chunk):
            self._chunks.append(np.asarray(chunk, dtype=np.float32).reshape(-1))
            self._len += len(chunk)
        out = []
        while True:
            seg = self._try_cut()
            if seg is None:
                return out
            out.append(seg)

    def snapshot(self) -> np.ndarray:
        """Audio blocks are immutable after feed; the worker may retain this view."""
        return self._buffer()

    def flush(self) -> np.ndarray:
        buf = self._buffer()
        self._chunks, self._len = [], 0
        return buf

    # ------------------------------------------------------------------
    def _buffer(self) -> np.ndarray:
        if len(self._chunks) > 1:
            self._chunks = [np.concatenate(self._chunks)]
        return self._chunks[0] if self._chunks else np.zeros(0, dtype=np.float32)

    def _try_cut(self) -> np.ndarray | None:
        if self._len < self.min_frames * FRAME:
            return None
        buf = self._buffer()
        rms = frame_rms(buf)
        cut = self._find_pause(rms)
        if cut is None:
            if len(rms) < self.max_frames:
                return None
            cut = self._quietest(rms)
        seg, rest = buf[: cut * FRAME], buf[cut * FRAME:]
        self._chunks = [rest] if len(rest) else []
        self._len = len(rest)
        return seg

    def _find_pause(self, rms: np.ndarray) -> int | None:
        """Middle of the longest pause after min_s (and before max_s)."""
        floor, high = np.percentile(rms, [10, 90])
        # Pause = clearly above the noise floor is NOT reached, relative to how loud speech is.
        thr = max(0.003, min(float(floor) * 2.5, float(high) * 0.15))
        quiet = rms < thr
        at_limit = len(rms) >= self.max_frames
        runs: list[tuple[int, int]] = []  # (start, length)
        start = None
        for i, q in enumerate(quiet):
            if q and start is None:
                start = i
            elif not q and start is not None:
                runs.append((start, i - start))
                start = None
        if start is not None and at_limit:
            # A still-growing pause is only used when we must cut now anyway.
            runs.append((start, len(quiet) - start))
        best, best_len = None, 0
        for s, n in runs:
            mid = s + n // 2
            if n >= self.pause_frames and self.min_frames <= mid < self.max_frames and n > best_len:
                best, best_len = mid, n
        return best

    def _quietest(self, rms: np.ndarray) -> int:
        lo, hi = self.min_frames, min(len(rms), self.max_frames)
        k = 5
        smooth = np.convolve(rms, np.ones(k) / k, mode="same")
        return lo + int(np.argmin(smooth[lo:hi]))
