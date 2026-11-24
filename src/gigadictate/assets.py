"""Icons and sounds generated in code: no image or audio files to ship, no Pillow."""

from __future__ import annotations

import io
import struct
import wave

import numpy as np

COLORS = {
    "idle": (0x5B, 0x6B, 0x7F),
    "recording": (0xE5, 0x39, 0x35),
    "processing": (0xF5, 0x9E, 0x0B),
    "loading": (0x9A, 0xA4, 0xB0),
}


def render_icon_rgba(size: int, color: tuple[int, int, int]) -> np.ndarray:
    """Round badge with a white microphone, anti-aliased by 4x supersampling."""
    ss = 4
    n = size * ss
    y, x = (np.mgrid[0:n, 0:n] + 0.5) / n  # 0..1
    cx = 0.5
    sw = max(0.035, 0.55 / size)  # stroke half-width: at least ~1 px on a 16 px icon
    badge = (x - cx) ** 2 + (y - 0.5) ** 2 <= 0.5 ** 2
    # capsule: mic head
    hw, top, bot = 0.13, 0.17, 0.53
    yc = np.clip(y, top + hw, bot - hw)
    head = (x - cx) ** 2 + (y - yc) ** 2 <= hw ** 2
    # U-shaped holder: ring segment below the head
    r = np.sqrt((x - cx) ** 2 + (y - 0.45) ** 2)
    holder = (np.abs(r - 0.22) <= sw) & (y >= 0.45)
    stem = (np.abs(x - cx) <= sw) & (y >= 0.66) & (y <= 0.80)
    foot = (np.abs(x - cx) <= 0.14) & (np.abs(y - 0.80) <= sw)
    mic = head | holder | stem | foot

    def down(mask):
        return mask.reshape(size, ss, size, ss).mean(axis=(1, 3))

    a_badge, a_mic = down(badge), down(mic & badge)
    rgba = np.zeros((size, size, 4), dtype=np.float32)
    for i, c in enumerate(color):
        rgba[..., i] = c * (1 - a_mic) + 255 * a_mic
    rgba[..., 3] = 255 * a_badge
    return rgba.round().astype(np.uint8)


def icon_resource(rgba: np.ndarray) -> bytes:
    """RT_ICON resource bytes (BITMAPINFOHEADER + BGRA bottom-up + AND mask) for CreateIconFromResourceEx."""
    h, w, _ = rgba.shape
    header = struct.pack("<IiiHHIIiiII", 40, w, h * 2, 1, 32, 0, w * h * 4, 0, 0, 0, 0)
    bgra = rgba[::-1, :, [2, 1, 0, 3]].tobytes()
    mask_row = ((w + 31) // 32) * 4
    return header + bgra + bytes(mask_row * h)


def tone_wav(freqs: list[float], dur_s: float = 0.07, volume: float = 0.18, sr: int = 22050) -> bytes:
    parts = []
    for f in freqs:
        t = np.arange(int(sr * dur_s)) / sr
        env = np.minimum(1, np.minimum(t, dur_s - t) / 0.01)  # 10 ms fade: no clicks
        parts.append(np.sin(2 * np.pi * f * t) * env)
    pcm = (np.concatenate(parts) * volume * 32767).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)
    return buf.getvalue()


SOUNDS = {
    "start": ([660, 990], 0.11),
    "stop": ([880, 660], 0.06),
    "cancel": ([440, 330], 0.07),
    "error": ([300, 300], 0.12),
}
