import json

import numpy as np
import pytest

from gigadictate import config as cfgmod
from gigadictate.audio import LinearResampler
from gigadictate.segmenter import FRAME, SR, Segmenter, has_speech, is_digital_silence, normalize
from gigadictate.textproc import apply_voice_commands, finalize, join_segments


# ------------------------------------------------------------------ config
def test_config_defaults_created(tmp_path):
    p = tmp_path / "config.json"
    cfg, problems = cfgmod.load(p)
    assert problems == [] and p.exists()
    assert json.loads(p.read_text("utf-8"))["hotkey"] == "right ctrl"


def test_config_invalid_values_fixed(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"hotkey": "a", "threads": "4", "sounds": 1, "model": "x", "tail_ms": -5,
                             "insert_method": "paste", "future_option": 42}), "utf-8")
    cfg, problems = cfgmod.load(p)
    assert cfg.hotkey == "right ctrl" and cfg.threads == 0 and cfg.sounds is True
    assert cfg.model == "gigaam-v3-e2e-rnnt" and cfg.tail_ms == 200
    assert cfg.insert_method == "paste"  # valid value is kept
    assert len(problems) == 5
    saved = json.loads(p.read_text("utf-8"))
    assert saved["future_option"] == 42  # unknown keys survive (newer version's settings)


def test_config_corrupted_file(tmp_path):
    p = tmp_path / "config.json"
    p.write_text("{ broken", "utf-8")
    cfg, problems = cfgmod.load(p)
    assert cfg.hotkey == "right ctrl" and problems
    assert (tmp_path / "config.json.broken").exists()


def test_config_bom(tmp_path):
    p = tmp_path / "config.json"
    p.write_bytes("﻿".encode() + json.dumps({"hotkey": "f9"}).encode())  # Notepad-saved
    cfg, problems = cfgmod.load(p)
    assert cfg.hotkey == "f9" and not problems


# ------------------------------------------------------------------ text
def test_join_lowercases_mid_sentence_cut():
    assert join_segments(["Мы продолжаем диктовку", "Чтобы проверить."]) == "Мы продолжаем диктовку чтобы проверить."
    assert join_segments(["Конец.", "Начало"]) == "Конец. Начало"
    assert join_segments(["Привет", "МГУ открыт."]) == "Привет МГУ открыт."  # acronyms untouched
    assert join_segments(["", "  a  b "]) == "a b"


@pytest.mark.parametrize("src,out", [
    ("говорит:«После", "говорит: «После"), ("в 15:30.", "в 15:30."), ("т.е. так", "т.е. так"),
    ("Да,нет", "Да, нет"), ("конец.Начало", "конец. Начало"), ("3.14", "3.14"), ("сайт.ру", "сайт.ру"),
])
def test_fix_spacing(src, out):
    from gigadictate.textproc import fix_spacing

    assert fix_spacing(src) == out


@pytest.mark.parametrize("src,out", [
    ("Первая строка. Новая строка. Вторая строка.", "Первая строка.\nВторая строка."),
    ("Привет, новая строка, как дела?", "Привет\nКак дела?"),
    ("Пункт один. Новый абзац. Пункт два.", "Пункт один.\n\nПункт два."),
    ("Список с новой строки молоко", "Список\nМолоко"),
    ("Обычный текст без команд.", "Обычный текст без команд."),
])
def test_voice_commands(src, out):
    assert apply_voice_commands(src) == out


def test_finalize():
    assert finalize(["Привет."], voice_commands=True, trailing_space=True) == "Привет. "
    assert finalize(["Привет. Новая строка."], voice_commands=True, trailing_space=True) == "Привет.\n"
    assert finalize(["", " "], voice_commands=True, trailing_space=True) == ""
    assert finalize(["Новая строка"], voice_commands=False, trailing_space=False) == "Новая строка"


# ------------------------------------------------------------------ audio utils
def tone(sec, amp=0.3, f=220):
    t = np.arange(int(sec * SR)) / SR
    return (amp * np.sin(2 * np.pi * f * t)).astype(np.float32)


def silence(sec, amp=0.0005):
    return (np.random.default_rng(0).standard_normal(int(sec * SR)) * amp).astype(np.float32)


def test_speech_gate():
    assert has_speech(tone(1))
    assert not has_speech(silence(2))
    assert not has_speech(np.zeros(100, dtype=np.float32))
    assert is_digital_silence(np.zeros(SR, dtype=np.float32))
    assert not is_digital_silence(silence(1))


def test_normalize():
    quiet = tone(1, amp=0.02)
    out = normalize(quiet)
    assert 0.1 < np.max(np.abs(out)) <= 0.7 + 1e-3
    loud = tone(1, amp=0.9)
    assert normalize(loud) is loud
    assert np.max(np.abs(normalize(silence(1, 0.0001)))) < 0.01  # gain capped, noise not blown up


def test_segmenter_short_never_cut():
    s = Segmenter()
    assert s.feed(tone(10)) == []
    assert len(s.flush()) == 10 * SR


def test_segmenter_cuts_at_pause():
    s = Segmenter(min_s=12, max_s=24)
    audio = np.concatenate([tone(14), silence(0.8), tone(5)])
    segs = []
    for i in range(0, len(audio), SR // 10):  # stream in 100 ms blocks like the mic
        segs += s.feed(audio[i:i + SR // 10])
    assert len(segs) == 1
    assert abs(len(segs[0]) / SR - 14.4) < 0.3  # middle of the pause
    rest = s.flush()
    assert abs((len(segs[0]) + len(rest)) - len(audio)) < FRAME  # nothing lost


def test_segmenter_hard_cut_without_pauses():
    s = Segmenter(min_s=12, max_s=24)
    segs = s.feed(tone(60))
    assert len(segs) >= 2 and all(12 * SR <= len(x) <= 24 * SR for x in segs)
    assert sum(map(len, segs)) + len(s.flush()) == 60 * SR


def test_segmenter_ignores_pause_before_min():
    s = Segmenter(min_s=12, max_s=24)
    segs = s.feed(np.concatenate([tone(5), silence(1), tone(8)]))
    assert segs == []


def test_segmenter_waits_for_growing_pause():
    s = Segmenter(min_s=12, max_s=24)
    assert s.feed(np.concatenate([tone(13), silence(0.6)])) == []  # pause may still grow
    assert len(s.feed(tone(1))) == 1


@pytest.mark.parametrize("rate", [44100, 48000, 22050, 8000])
def test_resampler_streaming_matches_length_and_pitch(rate):
    t = np.arange(rate * 2) / rate
    x = np.sin(2 * np.pi * 440 * t).astype(np.float32)
    r = LinearResampler(rate)
    out = np.concatenate([r(x[i:i + rate // 10]) for i in range(0, len(x), rate // 10)])
    assert abs(len(out) - 2 * SR) <= 2
    spec = np.abs(np.fft.rfft(out[:SR]))
    assert abs(np.argmax(spec) - 440) <= 2  # frequency preserved, no glitches at block joins


def test_no_advice_to_run_elevated():
    # An elevated GigaDictate would read config and models from a folder any normal program can write.
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for f in [root / "README.md", *(root / "src").rglob("*.py")]:
        assert "запускайте GigaDictate тоже от администратора" not in f.read_text(encoding="utf-8")
        assert "Запустите GigaDictate от администратора" not in f.read_text(encoding="utf-8")


def test_spoken_punctuation_and_literal_mode():
    from gigadictate.textproc import finalize
    parts = ["Привет, запятая друг. точка с запятой дальше вопросительный знак."]
    assert finalize(parts, voice_commands=True, trailing_space=False) == "Привет, друг; дальше?"
    assert finalize(parts, voice_commands=False, trailing_space=False) == parts[0]
