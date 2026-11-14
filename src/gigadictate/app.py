"""Windows application: wires hotkey hook, recorder, model, injector and tray together."""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import threading
from pathlib import Path

from . import __version__
from . import config as cfgmod
from . import hotkey as hk
from .audio import Recorder, WavRecorder
from .controller import Controller
from .engine import Engine, download_model, is_model_present

log = logging.getLogger("gigadictate")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def setup_logging() -> Path:
    path = cfgmod.app_dir() / "gigadictate.log"
    h = logging.handlers.RotatingFileHandler(path, maxBytes=1 << 20, backupCount=1, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(h)
    root.setLevel(logging.DEBUG if os.environ.get("GIGADICTATE_DEBUG") else logging.INFO)
    threading.excepthook = lambda a: log.error("uncaught in %s", a.thread.name if a.thread else "?",
                                                exc_info=(a.exc_type, a.exc_value, a.exc_traceback))
    sys.excepthook = lambda *a: log.error("uncaught", exc_info=a)
    return path


# ---------------------------------------------------------------- autostart
def _launch_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    pyw = Path(sys.executable).with_name("pythonw.exe")
    return f'"{pyw if pyw.exists() else sys.executable}" -m gigadictate'


def autostart_enabled() -> bool:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return winreg.QueryValueEx(k, cfgmod.APP_NAME)[0] == _launch_command()
    except OSError:
        return False


def set_autostart(on: bool) -> None:
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, cfgmod.APP_NAME, 0, winreg.REG_SZ, _launch_command())
        else:
            try:
                winreg.DeleteValue(k, cfgmod.APP_NAME)
            except FileNotFoundError:
                pass


# ---------------------------------------------------------------- application
class App:
    def __init__(self):
        from .win import api, inject, keyboard, tray

        self.api, self.tray_mod = api, tray
        self.log_path = setup_logging()
        log.info("GigaDictate %s starting (python %s)", __version__, sys.version.split()[0])
        self.cfg, problems = cfgmod.load()
        self.downloading = False

        fake_mic = os.environ.get("GIGADICTATE_FAKE_MIC")  # automated tests: WAV instead of a microphone
        rec_factory = (lambda: WavRecorder(fake_mic)) if fake_mic else (lambda: Recorder(self.cfg.input_device))

        self.tray = tray.Tray(cfgmod.APP_NAME, self.build_menu)
        self.engine = Engine(self.cfg.model, self.cfg.threads)
        self.injector = inject.Injector(self.cfg)
        self.controller = Controller(self.cfg, self.engine, rec_factory, self.injector, self.tray)
        c = self.controller
        self.machine = hk.HotkeyMachine(hk.parse_hotkey(self.cfg.hotkey),
                                        hk.Callbacks(c.request_start, c.request_stop, c.request_cancel),
                                        self.cfg.hold_threshold_ms / 1000)
        self.hook = keyboard.KeyboardHook(self.machine, allow_injected=bool(os.environ.get("GIGADICTATE_TEST_HOOK")))
        c.on_forced_stop = self.hook.force_stop
        self._update_hint()
        for p in problems:
            log.warning("config: %s", p)
        if problems:
            self.tray.notify("Настройки исправлены", problems[0], True)

    def _update_hint(self) -> None:
        self.tray.set_hint(f"{hk.display_name(self.cfg.hotkey)}: нажать — старт/стоп, удерживать — говорить")

    def run(self) -> int:
        try:
            self.hook.start()
        except OSError as e:
            self.tray_mod.message_box(f"Не удалось перехватить горячую клавишу: {e}", warning=True)
            return 1
        self.controller.start_threads()
        if is_model_present(self.cfg.model):
            self.controller.jobs.put(("load", None, None))
            self.tray.set_state("idle")
        else:
            self.start_download(self.cfg.model)
        if not self.cfg._extra.get("welcomed"):
            self.cfg._extra["welcomed"] = True
            cfgmod.save(self.cfg)
            self.tray.notify("GigaDictate запущен",
                             f"Поставьте курсор в любое поле ввода и нажмите {hk.display_name(self.cfg.hotkey)}. "
                             "Удерживайте клавишу, пока говорите, или нажмите один раз для старта и ещё раз для "
                             "остановки. Esc — отмена.")
        log.info("ready, hotkey=%s", self.cfg.hotkey)
        self.tray.run()
        log.info("exiting")
        self.hook.stop()
        self.controller.shutdown()
        return 0

    # ---------------------------------------------------------------- model download / switch
    def start_download(self, name: str) -> None:
        if self.downloading:
            return
        self.downloading = True
        self.tray.set_state("loading")
        self.tray.notify("Скачиваю модель распознавания", "Это нужно один раз (~220 МБ). Диктовка заработает сама.")

        def progress(done, total):
            self.tray.set_hint(f"Скачивание модели: {done * 100 // max(total, 1)}%")

        def work():
            try:
                download_model(name, progress=progress)
                self.tray.notify("Модель готова", "Можно диктовать.")
                if self.cfg.model == name:
                    self.controller.jobs.put(("load", None, None))
            except Exception as e:
                log.exception("download failed")
                self.tray.notify("Не удалось скачать модель",
                                 f"{e}\nПроверьте интернет и выберите модель в меню ещё раз.", True)
            finally:
                self.downloading = False
                self._update_hint()
                self.tray.set_state("idle")

        threading.Thread(target=work, name="download", daemon=True).start()

    def set_model(self, name: str) -> None:
        if name == self.cfg.model:
            return
        self.cfg.model = name
        cfgmod.save(self.cfg)
        new = Engine(name, self.cfg.threads)
        self.controller.jobs.put(("swap", None, new))
        if not is_model_present(name):
            self.start_download(name)
        else:
            self.controller.jobs.put(("load", None, None))

    # ---------------------------------------------------------------- menu
    def change_hotkey(self) -> None:
        self.tray.notify("Новая горячая клавиша", "Нажмите нужное сочетание. Esc — оставить как было.")

        def done(combo):  # hook thread
            if combo is None:
                self.tray.notify("Горячая клавиша не изменена", hk.display_name(self.cfg.hotkey))
                return
            err = hk.validate_combo(combo)
            if err:
                self.tray.notify("Это сочетание не подходит", err, True)
                return
            self.cfg.hotkey = hk.format_combo(combo)
            cfgmod.save(self.cfg)
            self.machine.set_combo(combo)
            self._update_hint()
            self.tray.notify("Горячая клавиша изменена", hk.display_name(self.cfg.hotkey))

        self.hook.start_capture(done)

    def _toggle(self, name: str):
        def f():
            setattr(self.cfg, name, not getattr(self.cfg, name))
            cfgmod.save(self.cfg)
        return f

    def _set(self, name: str, value):
        def f():
            setattr(self.cfg, name, value)
            cfgmod.save(self.cfg)
        return f

    def copy_last(self) -> None:
        from .win.inject import set_clipboard_text

        if self.controller.inserted:
            set_clipboard_text(self.controller.inserted[-1])

    def build_menu(self):
        from .config import MODELS
        from .win.tray import SEPARATOR, MenuItem as M

        cfg = self.cfg
        status = {"idle": "готов", "recording": "идёт запись", "processing": "распознаю",
                  "loading": "скачиваю модель" if self.downloading else "загрузка"}[self.tray.state]
        idle_unload = cfg.unload_after_idle_min > 0
        return [
            M(f"GigaDictate {__version__} — {status}", enabled=False),
            M(f"Горячая клавиша: {hk.display_name(cfg.hotkey)}", enabled=False),
            M("Изменить горячую клавишу…", self.change_hotkey),
            SEPARATOR,
            M("Модель", submenu=[M(label, (lambda n=n: self.set_model(n)), checked=cfg.model == n)
                                 for n, label in MODELS.items()]),
            M("Способ вставки", submenu=[
                M("Автоматически", self._set("insert_method", "auto"), cfg.insert_method == "auto"),
                M("Печатать символы", self._set("insert_method", "type"), cfg.insert_method == "type"),
                M("Через буфер обмена (Ctrl+V)", self._set("insert_method", "paste"), cfg.insert_method == "paste"),
            ]),
            M("Звуковые сигналы", self._toggle("sounds"), cfg.sounds),
            M("Голосовые команды «новая строка», «новый абзац»", self._toggle("voice_commands"), cfg.voice_commands),
            M("Пробел после текста", self._toggle("trailing_space"), cfg.trailing_space),
            M("Освобождать память при простое (10 мин)",
              self._set("unload_after_idle_min", 0 if idle_unload else 10), idle_unload),
            M("Запускать вместе с Windows", lambda: set_autostart(not autostart_enabled()), autostart_enabled()),
            SEPARATOR,
            M("Скопировать последний текст", self.copy_last if self.controller.inserted else None),
            M("Открыть папку настроек и журнала", lambda: self.tray_mod.open_folder(cfgmod.app_dir())),
            SEPARATOR,
            M("Выход", self.tray.quit),
        ]


def main() -> int:
    if sys.platform != "win32":
        print("GigaDictate работает только в Windows.", file=sys.stderr)
        return 2
    from .win import api

    try:  # crisp tray icon on high-DPI screens
        api.user32.SetProcessDpiAwarenessContext(api.c_void_p(-4))
    except Exception:
        pass
    mutex = api.CreateMutexW(None, False, "Local\\GigaDictate-SingleInstance")
    if api.last_error() == api.ERROR_ALREADY_EXISTS:
        from .win.tray import message_box

        message_box("GigaDictate уже запущен — значок микрофона в области уведомлений (возле часов).")
        return 0
    try:
        return App().run()
    except Exception as e:
        log.exception("fatal")
        from .win.tray import message_box

        message_box(f"GigaDictate не смог запуститься:\n{e}", warning=True)
        return 1
    finally:
        api.CloseHandle(mutex)
