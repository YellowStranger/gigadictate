"""Dictation flow: record -> recognise (in the background, piece by piece) -> insert.

OS specifics are injected (recorder, engine, injector, ui), so the whole flow
is tested on any platform with fakes.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

import numpy as np

from .audio import SR, AudioError
from .config import Config
from .engine import ModelMissing
from .segmenter import Segmenter, has_speech, normalize
from .textproc import finalize

log = logging.getLogger(__name__)


class UI(Protocol):
    def set_state(self, state: str) -> None: ...  # idle | recording | processing
    def notify(self, title: str, text: str, error: bool = False) -> None: ...
    def preview(self, text: str) -> None: ...
    def play(self, sound: str) -> None: ...  # start | stop | cancel | error


class Injector(Protocol):
    def foreground(self) -> Any: ...
    def insert(self, text: str, target: Any) -> str: ...  # returns "" on success or a reason


@dataclass
class Session:
    target: Any
    t0: float
    recorder: Any
    segmenter: Segmenter = field(default_factory=Segmenter)
    parts: dict[int, str] = field(default_factory=dict)
    n_segments: int = 0
    samples: int = 0
    draft: Any = None
    deferred_focus: Any = None
    last_preview: int = 0
    preview_pending: bool = False
    peak: float = 0.0
    cancelled: bool = False
    error: str | None = None


class Controller:
    MIN_AUDIO_S = 0.3

    def __init__(self, cfg: Config, engine, recorder_factory: Callable[[], Any], injector: Injector, ui: UI,
                 clock: Callable[[], float] = time.monotonic):
        self.cfg = cfg
        self.engine = engine
        self.recorder_factory = recorder_factory
        self.injector = injector
        self.ui = ui
        self.clock = clock
        self.cmds: queue.Queue = queue.Queue()
        self.jobs: queue.Queue = queue.Queue()
        self.session: Session | None = None
        self.pending_finals = 0
        self._pending_lock = threading.Lock()
        self.on_forced_stop: Callable[[], None] = lambda: None
        self._stop_evt = threading.Event()
        self._threads: list[threading.Thread] = []
        self.inserted: list[str] = []  # last texts (for tests / "repeat last" menu)

    # ---------------------------------------------------------------- called from the hotkey thread
    def request_start(self) -> None:
        self.cmds.put("start")

    def request_stop(self) -> None:
        self.cmds.put("stop")

    def request_cancel(self) -> None:
        self.cmds.put("cancel")

    # ---------------------------------------------------------------- lifecycle
    def start_threads(self) -> None:
        for target, name in ((self._control_loop, "control"), (self._worker_loop, "asr-worker")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)

    def shutdown(self) -> None:
        self._stop_evt.set()
        self.jobs.put(None)
        if self.session:
            self.session.recorder.abort()

    def wait_idle(self, timeout: float = 30) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.session is None and self.pending_finals == 0 and self.cmds.empty() and self.jobs.empty():
                return True
            time.sleep(0.02)
        return False

    # ---------------------------------------------------------------- control thread
    def _control_loop(self) -> None:
        while not self._stop_evt.is_set():
            try:
                # Poll often only while recording; when idle just wake up now and then for unloading.
                cmd = self.cmds.get(timeout=0.1 if self.session else 2.0)
            except queue.Empty:
                cmd = None
            try:
                if isinstance(cmd, tuple):
                    session, text = cmd
                    session.preview_pending = False
                    if session is self.session and text:
                        if session.draft and not session.draft.replace(text):
                            session.draft = None
                        if not session.draft and not session.deferred_focus:
                            self.ui.preview(text)
                elif cmd:
                    getattr(self, f"_do_{cmd}")()
                self._pump()
                self._maybe_unload()
            except Exception:
                log.exception("control loop error")

    def step(self, cmd: str | None = None) -> None:
        """Synchronous variant of one control-loop iteration (unit tests)."""
        if cmd:
            getattr(self, f"_do_{cmd}")()
        self._pump()

    def _do_start(self) -> None:
        if self.session is not None:
            return
        if not self.engine.loaded:
            self.jobs.put(("load", None, None))  # warm up while the user speaks
        target = self.injector.foreground()
        rec = self.recorder_factory()
        try:
            rec.start()
        except AudioError as e:
            log.warning("%s", e)
            self.on_forced_stop()
            self.ui.play("error")
            self.ui.notify("Нет микрофона", f"{e}\nПроверьте, что микрофон подключён и выбран в Параметрах звука.", True)
            return
        self.session = Session(target=target, t0=self.clock(), recorder=rec)
        self.ui.set_state("recording")
        self.session.draft = self.injector.begin_draft(self.session.target)
        if not self.session.draft:
            self.session.deferred_focus = self.injector.typing_focus(target)
        if not self.session.draft and not self.session.deferred_focus:
            self.ui.preview("Слушаю…")
        if self.cfg.sounds:
            self.ui.play("start")

    def _do_stop(self) -> None:
        s = self.session
        if s is None:
            return
        self.session = None
        self.ui.preview("")
        audio = s.recorder.stop(self.cfg.tail_ms / 1000)
        self._feed(s, audio)
        self._submit(s, s.segmenter.flush())
        with self._pending_lock:
            self.pending_finals += 1
        self.jobs.put(("final", s, None))
        self.ui.set_state("processing")
        if self.cfg.sounds:
            self.ui.play("stop")

    def _do_cancel(self) -> None:
        s = self.session
        if s is None:
            return
        self.session = None
        self.ui.preview("")
        if s.draft:
            s.draft.replace("", restore=True)
        s.cancelled = True
        s.recorder.abort()
        self._refresh_state()
        if self.cfg.sounds:
            self.ui.play("cancel")

    def _pump(self) -> None:
        s = self.session
        if s is None:
            return
        self._feed(s, s.recorder.read())
        if (not s.preview_pending and s.samples - s.last_preview >= SR * 2
                and len(s.segmenter) and self.jobs.empty()):
            s.preview_pending = True
            s.last_preview = s.samples
            self.jobs.put(("preview", s, s.segmenter.snapshot()))
        if s.recorder.error:
            s.error = s.recorder.error
            self.ui.notify("Запись остановлена", s.error, True)
            self.on_forced_stop()
            self._do_stop()
        elif self.clock() - s.t0 >= self.cfg.max_recording_s:
            self.ui.notify("Запись остановлена", f"Достигнут лимит {self.cfg.max_recording_s} с на одну диктовку.")
            self.on_forced_stop()
            self._do_stop()

    def _feed(self, s: Session, chunk: np.ndarray) -> None:
        if not len(chunk):
            return
        s.samples += len(chunk)
        s.peak = max(s.peak, float(np.max(np.abs(chunk))))
        for seg in s.segmenter.feed(chunk):
            self._submit(s, seg)

    def _submit(self, s: Session, seg: np.ndarray) -> None:
        if len(seg):
            self.jobs.put(("seg", s, (s.n_segments, seg)))
            s.n_segments += 1

    def _maybe_unload(self) -> None:
        mins = self.cfg.unload_after_idle_min
        if (mins and self.engine.loaded and self.session is None and self.pending_finals == 0
                and self.jobs.empty() and time.monotonic() - self.engine.last_used > mins * 60):
            self.jobs.put(("unload", None, None))
            self.engine.last_used = time.monotonic()  # don't queue it twice

    def _refresh_state(self) -> None:
        if self.session is not None:
            self.ui.set_state("recording")
        elif self.pending_finals:
            self.ui.set_state("processing")
        else:
            self.ui.set_state("idle")

    # ---------------------------------------------------------------- ASR worker thread
    def _worker_loop(self) -> None:
        while True:
            job = self.jobs.get()
            if job is None:
                return
            self.run_job(job)

    def run_job(self, job) -> None:
        kind, s, payload = job
        try:
            if kind == "load":
                self.engine.load()
            elif kind == "swap":  # model changed in the menu
                old, self.engine = self.engine, payload
                old.unload()
            elif kind == "unload":
                if self.session is None and self.jobs.empty():
                    self.engine.unload()
            elif kind == "preview":
                draft = ""
                try:
                    if self.session is s and has_speech(payload):
                        partial = self.engine.recognize(normalize(payload), SR)
                        draft = finalize([*s.parts.values(), partial], voice_commands=self.cfg.voice_commands,
                                         trailing_space=False)
                finally:
                    self.cmds.put((s, draft))
            elif kind == "seg":
                self._recognize_segment(s, *payload)
            elif kind == "final":
                self._finalize(s)
        except ModelMissing:
            self.ui.notify("Модель не найдена", "Модель распознавания ещё скачивается или не скачана — прогресс виден при наведении на значок в трее.", True)
        except Exception as e:
            log.exception("job %s failed", kind)
            if kind != "seg":
                self.ui.notify("Ошибка", str(e), True)
            elif s is not None:
                s.error = str(e)
        finally:
            if kind == "final":
                with self._pending_lock:
                    self.pending_finals -= 1
                self._refresh_state()

    def _recognize_segment(self, s: Session, idx: int, audio: np.ndarray) -> None:
        if s.cancelled or not has_speech(audio):
            return
        s.parts[idx] = self.engine.recognize(normalize(audio), SR)

    def _finalize(self, s: Session) -> None:
        if s.cancelled:
            return
        if s.samples < self.MIN_AUDIO_S * SR:
            log.info("recording too short (%d samples), ignored", s.samples)
            return
        if s.peak < 1e-4:
            self.ui.play("error")
            self.ui.notify("Микрофон молчит",
                           "Пришла полная тишина. Проверьте: Параметры → Конфиденциальность → Микрофон "
                           "(доступ для классических приложений) и не выключен ли микрофон.", True)
            return
        text = finalize([s.parts[i] for i in sorted(s.parts)],
                        voice_commands=self.cfg.voice_commands, trailing_space=self.cfg.trailing_space)
        if not text:
            if s.draft:
                s.draft.replace("", restore=True)
            if s.error:
                self.ui.notify("Ошибка распознавания", s.error, True)
            else:
                self.ui.notify("Речь не распознана", "Попробуйте говорить ближе к микрофону.")
            return
        if s.draft and s.draft.replace(text):
            reason = ""
        elif s.deferred_focus:
            reason = self.injector.insert(text, s.target, focus=s.deferred_focus)
        else:
            reason = self.injector.clipboard(text)
        self.inserted.append(text)
        del self.inserted[:-5]
        if reason:
            self.ui.notify("Текст в буфере обмена", f"{reason}\nВставьте его сочетанием Ctrl+V.")
