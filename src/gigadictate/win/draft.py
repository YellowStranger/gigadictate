"""Replace a guarded selection in a native Windows text control, including while Ctrl is held."""
import ctypes as c
import logging
import time
from ctypes import wintypes as w

from . import api


class GuiInfo(c.Structure):
    _fields_ = [("size", w.DWORD), ("flags", w.DWORD),
                *((name, w.HWND) for name in ("active", "focus", "capture", "menu", "move", "caret")),
                ("rect", w.RECT)]


gui_info = api._fn(api.user32, "GetGUIThreadInfo", w.BOOL, w.DWORD, c.POINTER(GuiInfo))
class_name = api._fn(api.user32, "GetClassNameW", c.c_int, w.HWND, w.LPWSTR, c.c_int)
style = api._fn(api.user32, "GetWindowLongW", w.LONG, w.HWND, c.c_int)
send_timeout = api._fn(api.user32, "SendMessageTimeoutW", api.LRESULT, w.HWND, w.UINT,
                       w.WPARAM, w.LPARAM, w.UINT, w.UINT, c.POINTER(c.c_size_t))


def message(hwnd, msg, wp=0, lp=0):
    result = c.c_size_t()
    if not send_timeout(hwnd, msg, wp, lp, 2, 250, c.byref(result)):
        raise OSError("Поле ввода недоступно")
    return result.value


def focused(target):
    info = GuiInfo()
    info.size = c.sizeof(info)
    fg = api.GetForegroundWindow()
    if not fg or (api.GetAncestor(fg, api.GA_ROOT) or fg) != target:
        return None
    thread = api.GetWindowThreadProcessId(fg, None)
    if thread and gui_info(thread, c.byref(info)):
        return info.focus
    return None


def selection(hwnd):
    a, b = w.DWORD(), w.DWORD()
    message(hwnd, 0xB0, c.addressof(a), c.addressof(b))
    return a.value, b.value


def content(hwnd):
    size = message(hwnd, 14)
    if size > 1_000_000:
        raise ValueError("Слишком большой документ")
    buf = c.create_unicode_buffer(size + 1)
    message(hwnd, 13, size + 1, c.addressof(buf))
    return buf.value.encode("utf-16-le")


class Draft:
    @classmethod
    def open(cls, target):
        hwnd = focused(target)
        name = c.create_unicode_buffer(128)
        if not hwnd or not class_name(hwnd, name, 128):
            return None
        if name.value.lower() != "edit" and not name.value.lower().startswith("richedit"):
            return None
        if style(hwnd, -16) & (0x20 | 0x800 | 0x08000000):  # password, readonly, disabled
            return None
        try:
            return cls(target, hwnd)
        except (ValueError, OSError):
            return None

    def __init__(self, target, hwnd):
        self.target, self.hwnd = target, hwnd
        self.start, end = self.expected_selection = selection(hwnd)
        self.expected = content(hwnd)
        self.original = self.expected[self.start * 2:end * 2].decode("utf-16-le")
        self.changed = False

    def replace(self, text, restore=False):
        try:
            if (focused(self.target) != self.hwnd or selection(self.hwnd) != self.expected_selection
                    or content(self.hwnd) != self.expected):
                return False
            if restore and not self.changed:
                return True
            end = self.expected_selection[1]
            text = self.original if restore else text.replace("\n", "\r\n")
            buffer = c.create_unicode_buffer(text)
            message(self.hwnd, 0xB1, self.start, end)  # EM_SETSEL
            message(self.hwnd, 0xC2, 1, c.addressof(buffer))  # EM_REPLACESEL
            new_end = self.start + len(text.encode("utf-16-le")) // 2
            expected = self.expected[:self.start*2] + text.encode("utf-16-le") + self.expected[end*2:]
            if content(self.hwnd) != expected:
                return False
            self.expected = expected
            self.expected_selection = (new_end, new_end)
            self.changed = True
            if restore:
                message(self.hwnd, 0xB1, self.start, new_end)
            return True
        except (OSError, ValueError):
            return False


class AccessibleDraft:
    """Plain-text UIA edit controls. Store offsets and identity, never COM objects across threads."""
    @staticmethod
    def inspect(target):
        import uiautomation as uia

        if api.GetForegroundWindow() != target:
            raise ValueError("Фокус изменился")
        control = uia.GetFocusedControl()
        if not control or control.IsPassword or control.ControlType not in (uia.ControlType.EditControl, uia.ControlType.DocumentControl):
            raise ValueError("Нет доступного поля")
        value = control.GetPattern(uia.PatternId.ValuePattern)
        text = control.GetPattern(uia.PatternId.TextPattern)
        if not value or value.IsReadOnly or not text:
            raise ValueError("Поле не поддерживает редактирование")
        document = text.DocumentRange
        current = document.GetText(65537)
        if len(current) > 65536:
            raise ValueError("Нет доступного текстового диапазона")
        selected, = text.GetSelection()
        prefix = document.Clone()
        prefix.MoveEndpointByRange(1, selected, 0, 0)
        start = len(prefix.GetText())
        return (tuple(control.GetRuntimeId()), current, start, start + len(selected.GetText())), value, text

    @classmethod
    def open(cls, target, paste, timeout=0):
        try:
            import uiautomation as uia
            with uia.UIAutomationInitializerInThread():
                deadline = time.monotonic() + timeout
                while True:
                    try:
                        state, _, _ = cls.inspect(target)
                        break
                    except Exception:
                        if api.GetForegroundWindow() != target or time.monotonic() >= deadline:
                            raise
                        time.sleep(0.05)
                draft = cls()
                draft.paste = paste
                draft.target = target
                draft.initial = state
                draft.expected = state
                _, value, draft.start, end = state
                draft.original = value[draft.start:end]
                return draft
        except Exception:
            return None

    def pristine(self):
        # A provider can advertise editable ranges but refuse updates. Ordinary
        # typing is safe only if the original text, selection and identity survived.
        try:
            import uiautomation as uia
            with uia.UIAutomationInitializerInThread():
                return self.inspect(self.target)[0] == self.initial
        except Exception:
            return False

    def replace(self, text, restore=False):
        try:
            import uiautomation as uia
            with uia.UIAutomationInitializerInThread():
                state, field, pattern = self.inspect(self.target)
                if state != self.expected:
                    logging.getLogger(__name__).info("draft update: field state changed")
                    return False
                ident, current, _, end = state
                replacement = self.original if restore else text
                updated = current[:self.start] + replacement + current[end:]
                if updated == current and not restore:
                    return True
                # Chromium may expose ValuePattern but ignore SetValue for contenteditable.
                # Replace only the verified range using the editor's normal paste command.
                selected, = pattern.GetSelection()
                if self.start < state[2]:
                    selected.MoveEndpointByUnit(0, 0, self.start - state[2], 0)
                if selected.GetText() != current[self.start:end]:
                    logging.getLogger(__name__).info("draft update: range mismatch")
                    return False
                if state[2] != self.start:
                    selected.Select(0)
                    deadline = time.monotonic() + 0.75
                    while True:
                        now, _, _ = self.inspect(self.target)
                        if now == (ident, current, self.start, end):
                            break
                        if now != state or time.monotonic() >= deadline:
                            logging.getLogger(__name__).info("draft selection mismatch: expected %s:%s, got %s:%s",
                                                             self.start, end, now[2], now[3])
                            return False
                        time.sleep(0.025)
                if updated != current:
                    if replacement:
                        if not self.paste(replacement):
                            logging.getLogger(__name__).info("draft update: paste unavailable")
                            return False
                    else:
                        from .inject import _key, send_inputs
                        if not send_inputs([_key(8), _key(8, flags=api.KEYEVENTF_KEYUP)]):
                            return False
                deadline = time.monotonic() + 0.5
                while True:
                    actual, _, pattern = self.inspect(self.target)
                    if (actual[0] == ident and actual[1].replace("\xa0", " ") == updated.replace("\xa0", " ")
                            and actual[2] == actual[3] == self.start + len(replacement)):
                        break
                    if time.monotonic() >= deadline:
                        logging.getLogger(__name__).info("draft update: paste not confirmed")
                        return False
                    time.sleep(0.02)
                self.expected = actual
                if restore and replacement:
                    selected, = pattern.GetSelection()
                    selected.MoveEndpointByUnit(0, 0, -len(replacement), 0)
                    selected.Select(0)
                return True
        except Exception as exc:
            logging.getLogger(__name__).info("draft provider error: %s", type(exc).__name__)
            return False
