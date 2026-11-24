"""Dictation flow with the REAL GigaAM model and recorded speech; OS parts are fakes."""

import time
from pathlib import Path

import numpy as np
import pytest

from gigadictate.audio import AudioError, WavRecorder
from gigadictate.config import Config
from gigadictate.controller import Controller
from gigadictate.engine import Engine, is_model_present, models_root

DATA = Path(__file__).parent / "data"
MODEL = "gigaam-v3-e2e-rnnt"

needs_model = pytest.mark.skipif(not is_model_present(MODEL), reason="model not downloaded")


class FakeUI:
    def __init__(self):
        self.states, self.notes, self.sounds = [], [], []

    def preview(self, text):
        self.draft = text

    def set_state(self, s):
        self.states.append(s)

    def notify(self, title, text, error=False):
        self.notes.append((title, text, error))

    def play(self, s):
        self.sounds.append(s)


class FakeInjector:
    def __init__(self, fail_reason=""):
        self.texts = []
        self.fail_reason = fail_reason

    def foreground(self):
        return "window-1"

    def insert(self, text, target):
        self.texts.append((text, target))
        return self.fail_reason


@pytest.fixture(scope="module")
def engine():
    e = Engine(MODEL)
    if is_model_present(MODEL):
        e.load()
    return e


def run(engine, wav, cfg=None, injector=None, cancel=False):
    cfg = cfg or Config()
    ui, inj = FakeUI(), injector or FakeInjector()
    c = Controller(cfg, engine, lambda: WavRecorder(DATA / wav, realtime=False), inj, ui)
    c.step("start")
    if cancel:
        c.step("cancel")
    else:
        c.step("stop")
    while not c.jobs.empty():
        c.run_job(c.jobs.get())
    return c, ui, inj


def words(s):
    return [w.strip(".,!?:;«»").lower() for w in s.split()]


@needs_model
def test_short_phrase(engine):
    c, ui, inj = run(engine, "short.wav")
    (text, target), = inj.texts
    assert target == "window-1"
    assert "проверка диктовки" in text.lower() and "15:30" in text
    assert text.endswith(" ")
    assert ui.states == ["recording", "processing", "idle"]
    assert ui.sounds == ["start", "stop"]


@needs_model
def test_long_dictation_is_segmented_and_complete(engine):
    c, ui, inj = run(engine, "long.wav")
    (text, _), = inj.texts
    w = words(text)
    # 46 s of speech: 3 repetitions of 4 sentences; nothing may be lost at the cuts
    assert w.count("длинная") == 3 and w.count("задержка") == 3
    assert w.count("кусок") == 3
    assert len(w) == 96  # 3 x 32 words, exact: nothing lost or duplicated at the cuts


@needs_model
def test_voice_commands(engine):
    c, ui, inj = run(engine, "commands.wav")
    (text, _), = inj.texts
    assert "\n" in text and "\n\n" in text
    assert "строка" not in text.lower() and "абзац" not in text.lower()


@needs_model
def test_voice_commands_disabled(engine):
    c, ui, inj = run(engine, "commands.wav", cfg=Config(voice_commands=False))
    assert "новая строка" in inj.texts[0][0].lower()


@needs_model
def test_quiet_microphone_still_recognized(engine):
    c, ui, inj = run(engine, "quiet.wav")
    assert inj.texts and "диктовки" in inj.texts[0][0].lower()


@needs_model
def test_noise_only_inserts_nothing(engine):
    c, ui, inj = run(engine, "silence.wav")
    assert inj.texts == []
    assert ui.notes[0][0] == "Речь не распознана"
    assert ui.states[-1] == "idle"


def test_digital_zeros_warns_about_privacy(engine):
    c, ui, inj = run(engine, "zeros.wav")
    assert inj.texts == []
    assert "Конфиденциальность" in ui.notes[0][1] and ui.notes[0][2] is True


def test_cancel_inserts_nothing(engine):
    c, ui, inj = run(engine, "short.wav", cancel=True)
    assert inj.texts == [] and ui.sounds == ["start", "cancel"] and ui.states[-1] == "idle"


def test_too_short_tap_ignored(engine):
    class Tiny(WavRecorder):
        def read(self):
            return np.zeros(0, dtype=np.float32)

        def stop(self, tail_s=0):
            return np.full(1000, 0.1, dtype=np.float32)

    ui, inj = FakeUI(), FakeInjector()
    c = Controller(Config(), engine, lambda: Tiny(DATA / "short.wav"), inj, ui)
    c.step("start")
    c.step("stop")
    while not c.jobs.empty():
        c.run_job(c.jobs.get())
    assert inj.texts == [] and ui.notes == []


@needs_model
def test_clipboard_fallback_is_reported(engine):
    c, ui, inj = run(engine, "short.wav", injector=FakeInjector("Окно сменилось"))
    assert ui.notes[0][0] == "Текст в буфере обмена"


@needs_model
def test_sounds_off(engine):
    c, ui, inj = run(engine, "short.wav", cfg=Config(sounds=False))
    assert ui.sounds == []


def test_no_microphone(engine):
    class NoMic(WavRecorder):
        def start(self):
            raise AudioError("нет устройств")

    ui, inj = FakeUI(), FakeInjector()
    c = Controller(Config(), engine, lambda: NoMic(DATA / "short.wav"), inj, ui)
    forced = []
    c.on_forced_stop = lambda: forced.append(1)
    c.step("start")
    assert c.session is None and forced == [1] and ui.sounds == ["error"] and ui.notes[0][2]
    c.step("stop")  # the matching stop from the hotkey must be harmless
    assert c.jobs.empty()


def test_device_lost_mid_recording(engine):
    class Unplugged(WavRecorder):
        def read(self):
            out = super().read()
            self.error = "Микрофон отключился во время записи"
            return out

    ui, inj = FakeUI(), FakeInjector()
    c = Controller(Config(), engine, lambda: Unplugged(DATA / "short.wav", realtime=False), inj, ui)
    forced = []
    c.on_forced_stop = lambda: forced.append(1)
    c.step("start")
    assert c.session is None and forced == [1]  # stopped and what was heard is still processed
    while not c.jobs.empty():
        c.run_job(c.jobs.get())
    if is_model_present(MODEL):
        assert inj.texts


def test_max_duration(engine):
    clock = [0.0]
    ui, inj = FakeUI(), FakeInjector()
    c = Controller(Config(max_recording_s=5), engine, lambda: WavRecorder(DATA / "short.wav", realtime=False),
                   inj, ui, clock=lambda: clock[0])
    forced = []
    c.on_forced_stop = lambda: forced.append(1)
    c.step("start")
    clock[0] = 6
    c.step()
    assert c.session is None and forced == [1] and "лимит" in ui.notes[0][1]


def test_double_start_ignored(engine):
    ui, inj = FakeUI(), FakeInjector()
    made = []

    def factory():
        made.append(1)
        return WavRecorder(DATA / "short.wav", realtime=False)

    c = Controller(Config(), engine, factory, inj, ui)
    c.step("start")
    c.step("start")
    assert len(made) == 1


@needs_model
def test_threaded_end_to_end_and_new_recording_during_processing(engine):
    ui, inj = FakeUI(), FakeInjector()
    c = Controller(Config(), engine, lambda: WavRecorder(DATA / "short.wav", realtime=False), inj, ui)
    c.start_threads()
    try:
        c.request_start()
        c.request_stop()
        c.request_start()  # user starts the next phrase immediately
        c.request_stop()
        assert c.wait_idle(60)
    finally:
        c.shutdown()
    assert len(inj.texts) == 2 and ui.states[-1] == "idle"


@needs_model
def test_idle_unload_and_reload():
    e = Engine(MODEL)
    e.load()
    ui, inj = FakeUI(), FakeInjector()
    c = Controller(Config(unload_after_idle_min=1), e, lambda: WavRecorder(DATA / "short.wav", realtime=False),
                   inj, ui)
    e.last_used = time.monotonic() - 120
    c._maybe_unload()
    c.run_job(c.jobs.get())
    assert not e.loaded
    c.step("start")
    c.step("stop")
    while not c.jobs.empty():
        c.run_job(c.jobs.get())
    assert e.loaded and inj.texts  # reloaded transparently on the next dictation


def test_models_root_exists():
    assert models_root().name == "models"


@needs_model
def test_live_preview_before_release(engine):
    ui, inj = FakeUI(), FakeInjector()
    c = Controller(Config(), engine, lambda: WavRecorder(DATA / "short.wav", realtime=False), inj, ui)
    c.start_threads()
    try:
        c.request_start()
        deadline = time.monotonic() + 10
        while "диктовки" not in getattr(ui, "draft", "").lower() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert "диктовки" in ui.draft.lower()
        assert ui.sounds == ["start"] and not inj.texts
        c.request_stop()
        assert c.wait_idle()
        assert ui.draft == "" and len(inj.texts) == 1
        assert ui.sounds == ["start", "stop"]
    finally:
        c.shutdown()
