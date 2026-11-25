"""Replace a guarded selection in a native Windows text control, including while Ctrl is held."""
import ctypes as c
import logging
import time
from ctypes import wintypes as w
from os.path import commonprefix

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
        if not control or control.IsPassword or control.ControlType not in (uia.ControlType.EditControl, uia.ControlType.DocumentControl, uia.ControlType.ComboBoxControl):
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
        # Empty search boxes can expose an accessibility label as DocumentRange
        # even though their editable value and selection are both empty.
        if not value.Value and not selected.GetText():
            current, start = "", 0
        return (tuple(control.GetRuntimeId()), current, start, start + len(selected.GetText())), value, text

    @classmethod
    def open(cls, target, paste, timeout=0):
        try:
            import uiautomation as uia
            with uia.UIAutomationInitializerInThread():
                deadline = time.monotonic() + timeout
                while True:
                    try:
                        state, _, pattern = cls.inspect(target)
                        break
                    except Exception:
                        if api.GetForegroundWindow() != target or time.monotonic() >= deadline:
                            raise
                        time.sleep(0.05)
                draft = cls()
                draft.paste = paste
                draft.target = target
                draft.chrome = timeout > 0
                draft.anchor = cls.location(pattern, uia.GetFocusedControl())
                draft.initial = state
                draft.pending = None
                draft.expected = state
                _, value, draft.start, end = state
                draft.original = value[draft.start:end]
                return draft
        except Exception:
            return None

    @staticmethod
    def location(pattern, control):
        try:
            selected, = pattern.GetSelection()
            rect = selected.textRange.GetBoundingRectangles()
            if rect and len(rect) >= 4:
                x, y, width, height = rect[-4:]
                if height > 0:
                    return int(x + width), int(y + height)
            box = control.BoundingRectangle
            return int(box.left + 8), int(box.bottom)
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
                def same_state(left, right):
                    return (left[0] == right[0] and left[2:] == right[2:]
                            and left[1].replace("\xa0", " ") == right[1].replace("\xa0", " "))

                def confirms(actual, intended):
                    if same_state(actual, intended):
                        return True
                    # Chrome search can immediately extend our paste with a selected
                    # suggestion. Only adopt that suffix if the complete intended
                    # value precedes it, in the same control, at document end.
                    ident, value, caret, end = intended
                    aid, shown, left, right = actual
                    # The address bar trims a trailing space on paste, including
                    # the optional separator added when dictation is committed.
                    if (self.chrome and aid == ident and caret == end == len(value)
                            and value.endswith(" ") and shown == value.rstrip(" ")
                            and left == right == len(shown)):
                        return True
                    return (self.chrome and aid == ident and caret == end == len(value)
                            and left == caret < right == len(shown)
                            and shown[:left].replace("\xa0", " ") == value.replace("\xa0", " "))

                if self.pending and confirms(state, self.pending):
                    self.expected = state
                    self.pending = None
                if not same_state(state, self.expected):
                    logging.getLogger(__name__).info("draft update: field state changed")
                    return False
                ident, current, _, end = state
                replacement = self.original if restore else text
                updated = current[:self.start] + replacement + current[end:]
                if updated == current and not restore:
                    return True
                # Chromium may expose ValuePattern but ignore SetValue for contenteditable.
                # Replace only the verified range using the editor's normal paste command.
                # Most results extend the previous hypothesis. Keep its confirmed
                # prefix in place and select only the suffix the model revised.
                old = current[self.start:end].replace("\xa0", " ")
                keep = (len(commonprefix([old, replacement])) if not restore else 0)
                keep = min(keep, state[2] - self.start)
                # Omnibox sanitizes leading whitespace from clipboard text.
                # Include a verified preceding letter in the replacement so the
                # space between two hypotheses survives Chrome's paste filter.
                if self.chrome and replacement[keep:keep+1].isspace():
                    while keep and replacement[keep:keep+1].isspace():
                        keep -= 1
                edit_start = self.start + keep
                selected, = pattern.GetSelection()
                if edit_start < state[2]:
                    selected.MoveEndpointByUnit(0, 0, edit_start - state[2], 0)
                if selected.GetText().replace("\xa0", " ") != current[edit_start:end].replace("\xa0", " "):
                    logging.getLogger(__name__).info("draft update: range mismatch")
                    return False
                if state[2] != edit_start:
                    selected.Select(0)
                    deadline = time.monotonic() + 0.75
                    while True:
                        now, _, _ = self.inspect(self.target)
                        if same_state(now, (ident, current, edit_start, end)):
                            break
                        if not same_state(now, state) or time.monotonic() >= deadline:
                            logging.getLogger(__name__).info("draft selection mismatch: expected %s:%s, got %s:%s",
                                                             edit_start, end, now[2], now[3])
                            return False
                        time.sleep(0.025)
                if updated != current:
                    caret = self.start + len(replacement)
                    self.pending = (ident, updated, caret, caret)
                    if replacement[keep:]:
                        if not self.paste(replacement[keep:]):
                            logging.getLogger(__name__).info("draft update: paste unavailable")
                            return False
                    else:
                        from .inject import _key, send_inputs
                        if not send_inputs([_key(8), _key(8, flags=api.KEYEVENTF_KEYUP)]):
                            return False
                deadline = time.monotonic() + 0.5
                while True:
                    actual, _, pattern = self.inspect(self.target)
                    if confirms(actual, (ident, updated, self.start + len(replacement), self.start + len(replacement))):
                        break
                    if time.monotonic() >= deadline:
                        logging.getLogger(__name__).info(
                            "draft update: paste not confirmed; stable id=%s, intended=%d, observed=%d, "
                            "common prefix=%d, expected caret=%d, selection=%d:%d",
                            actual[0] == ident, len(updated), len(actual[1]),
                            len(commonprefix([actual[1], updated])), self.start + len(replacement),
                            actual[2], actual[3])
                        return False
                    time.sleep(0.02)
                self.pending = None
                self.anchor = self.location(pattern, uia.GetFocusedControl())
                self.expected = actual
                if restore and replacement:
                    selected, = pattern.GetSelection()
                    selected.MoveEndpointByUnit(0, 0, -len(replacement), 0)
                    selected.Select(0)
                return True
        except Exception as exc:
            logging.getLogger(__name__).info("draft provider error: %s", type(exc).__name__)
            return False
