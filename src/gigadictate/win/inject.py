"""Putting recognised text into whatever window has focus."""

from __future__ import annotations

import ctypes
import logging
import time

from . import api
from .keyboard import INJECT_MAGIC

log = logging.getLogger(__name__)

_MODIFIERS = (api.VK_LSHIFT, api.VK_RSHIFT, api.VK_LCONTROL, api.VK_RCONTROL,
              api.VK_LMENU, api.VK_RMENU, api.VK_LWIN, api.VK_RWIN)
# GDI-handle formats cannot be copied as raw memory; everything else can.
_HANDLE_FORMATS = {2, 3, 9, 14, 0x80, 0x82, 0x83, 0x8E}
_SYNTHESIZED = {1, 7, 16}  # CF_TEXT, CF_OEMTEXT, CF_LOCALE: Windows regenerates them from CF_UNICODETEXT
_MAX_SNAPSHOT = 64 << 20


def _key(vk: int = 0, scan: int = 0, flags: int = 0) -> api.INPUT:
    i = api.INPUT()
    i.type = api.INPUT_KEYBOARD
    i.u.ki = api.KEYBDINPUT(vk, scan, flags, 0, INJECT_MAGIC)
    return i


def text_to_inputs(text: str) -> list[api.INPUT]:
    out: list[api.INPUT] = []
    for ch in text.replace("\r\n", "\n").replace("\r", "\n"):
        if ch == "\n":
            # Shift+Enter: a line break in chats (Telegram, Slack, browsers) instead of "send message"
            out += [_key(api.VK_LSHIFT), _key(api.VK_RETURN), _key(api.VK_RETURN, flags=api.KEYEVENTF_KEYUP),
                    _key(api.VK_LSHIFT, flags=api.KEYEVENTF_KEYUP)]
            continue
        data = ch.encode("utf-16-le")
        for j in range(0, len(data), 2):  # surrogate pairs (emoji) -> two code units
            unit = int.from_bytes(data[j:j + 2], "little")
            out += [_key(scan=unit, flags=api.KEYEVENTF_UNICODE),
                    _key(scan=unit, flags=api.KEYEVENTF_UNICODE | api.KEYEVENTF_KEYUP)]
    return out


def send_inputs(inputs: list[api.INPUT], batch: int = 64, pause_s: float = 0.004) -> bool:
    for i in range(0, len(inputs), batch):
        chunk = inputs[i:i + batch]
        arr = (api.INPUT * len(chunk))(*chunk)
        sent = api.SendInput(len(chunk), arr, ctypes.sizeof(api.INPUT))
        if sent != len(chunk):
            log.warning("SendInput sent %d of %d (error %d)", sent, len(chunk), api.last_error())
            return False
        time.sleep(pause_s)  # slow apps (Electron, remote desktops) drop keys if flooded
    return True


# ---------------------------------------------------------------- clipboard
class Clipboard:
    def __enter__(self):
        for _ in range(50):  # another program may hold the clipboard for a moment
            if api.OpenClipboard(None):
                return self
            time.sleep(0.02)
        raise OSError("буфер обмена занят другой программой")

    def __exit__(self, *exc):
        api.CloseClipboard()


def _global_from_bytes(data: bytes) -> int:
    h = api.GlobalAlloc(api.GMEM_MOVEABLE, max(1, len(data)))
    if not h:
        raise MemoryError
    p = api.GlobalLock(h)
    ctypes.memmove(p, data, len(data))
    api.GlobalUnlock(h)
    return h


def _set_format(fmt: int, data: bytes) -> None:
    h = _global_from_bytes(data)
    if not api.SetClipboardData(fmt, h):
        api.GlobalFree(h)  # ownership passes to the system only on success
        raise OSError(f"SetClipboardData({fmt}) failed: {api.last_error()}")


def snapshot_clipboard() -> list[tuple[int, bytes]] | None:
    """Copy of all clipboard formats, or None if it holds something we can't restore."""
    items: list[tuple[int, bytes]] = []
    total = 0
    with Clipboard():
        fmt = 0
        while True:
            fmt = api.EnumClipboardFormats(fmt)
            if not fmt:
                break
            if fmt in _SYNTHESIZED:
                continue
            if fmt in _HANDLE_FORMATS:
                if fmt == 2 or fmt == 14:  # bitmap / metafile: fine if a DIB / text copy also exists
                    continue
                return None
            h = api.GetClipboardData(fmt)
            if not h:
                return None  # delayed rendering failed
            size = api.GlobalSize(h)
            total += size
            if total > _MAX_SNAPSHOT:
                return None
            p = api.GlobalLock(h)
            if not p:
                return None
            try:
                items.append((fmt, ctypes.string_at(p, size)))
            finally:
                api.GlobalUnlock(h)
    return items


def set_clipboard_text(text: str, private: bool = False) -> int:
    """Dictated text never goes to the cloud clipboard. private=True (text only "in transit"
    for a paste) also keeps it out of Win+V history and clipboard monitors."""
    zero = (0).to_bytes(4, "little")
    with Clipboard():
        api.EmptyClipboard()
        _set_format(api.CF_UNICODETEXT, (text + "\0").encode("utf-16-le"))
        _set_format(api.RegisterClipboardFormatW("CanUploadToCloudClipboard"), zero)
        if private:
            _set_format(api.RegisterClipboardFormatW("ExcludeClipboardContentFromMonitorProcessing"), zero)
            _set_format(api.RegisterClipboardFormatW("CanIncludeInClipboardHistory"), zero)
    return api.GetClipboardSequenceNumber()


def get_clipboard_text() -> str | None:
    with Clipboard():
        h = api.GetClipboardData(api.CF_UNICODETEXT)
        if not h:
            return None
        p = api.GlobalLock(h)
        try:
            return ctypes.wstring_at(p)
        finally:
            api.GlobalUnlock(h)


def restore_clipboard(items: list[tuple[int, bytes]]) -> None:
    with Clipboard():
        api.EmptyClipboard()
        for fmt, data in items:
            try:
                _set_format(fmt, data)
            except OSError as e:
                log.debug("restore format %d: %s", fmt, e)


# ---------------------------------------------------------------- windows / processes
def _root(hwnd) -> int:
    return api.GetAncestor(hwnd, api.GA_ROOT) or hwnd if hwnd else 0


def _is_elevated_pid(pid: int | None) -> bool | None:
    h = api.GetCurrentProcess() if pid is None else api.OpenProcess(api.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        tok = api.wt.HANDLE()
        if not api.OpenProcessToken(h, api.TOKEN_QUERY, api.byref(tok)):
            return True if pid is not None else None  # no access to the token => higher integrity
        try:
            el = api.TOKEN_ELEVATION()
            n = api.wt.DWORD()
            if not api.GetTokenInformation(tok, api.TokenElevation, api.byref(el), ctypes.sizeof(el), api.byref(n)):
                return None
            return bool(el.TokenIsElevated)
        finally:
            api.CloseHandle(tok)
    finally:
        if pid is not None:
            api.CloseHandle(h)


def window_is_elevated(hwnd) -> bool:
    pid = api.wt.DWORD()
    api.GetWindowThreadProcessId(hwnd, api.byref(pid))
    return bool(pid.value) and bool(_is_elevated_pid(pid.value))


def modifiers_down() -> list[int]:
    return [vk for vk in _MODIFIERS if api.GetAsyncKeyState(vk) & 0x8000]


def wait_modifiers_released(timeout_s: float = 2.0) -> bool:
    """Typing while the user still holds Ctrl/Alt/Win would turn letters into shortcuts."""
    end = time.monotonic() + timeout_s
    while modifiers_down():
        if time.monotonic() > end:
            return False
        time.sleep(0.02)
    return True


class Injector:
    def __init__(self, cfg):
        self.cfg = cfg
        self.self_elevated = bool(_is_elevated_pid(None))
        self.restore_delay_s = 0.5

    def foreground(self):
        return _root(api.GetForegroundWindow())

    def begin_draft(self, target):
        from .draft import AccessibleDraft, Draft
        if target and (self.self_elevated or not window_is_elevated(target)):
            return Draft.open(target) or AccessibleDraft.open(target)
        return None

    def typing_focus(self, target):
        # Custom editor surfaces can accept ordinary keyboard input even when
        # they expose no editable document range to Windows accessibility.
        from .draft import focused, class_name, style
        hwnd = focused(target)
        if not hwnd or (not self.self_elevated and window_is_elevated(target)):
            return None
        buf = ctypes.create_unicode_buffer(128)
        class_name(hwnd, buf, len(buf))
        name = buf.value.lower()
        if name in {"button", "static", "listbox", "systreeview32", "syslistview32",
                    "toolbarwindow32", "shell_traywnd", "progman", "workerw"}:
            return None
        if (name == "edit" or name.startswith("richedit")) and style(hwnd, -16) & (0x20 | 0x800):
            return None
        try:
            import uiautomation as uia
            with uia.UIAutomationInitializerInThread():
                control = uia.GetFocusedControl()
                if control and (control.IsPassword or control.ControlTypeName in {
                        "ButtonControl", "MenuItemControl", "CheckBoxControl", "RadioButtonControl",
                        "ToolBarControl", "HyperlinkControl", "ListItemControl"}):
                    return None
                identity = tuple(control.GetRuntimeId()) if control else ()
        except Exception:
            identity = ()
        return hwnd, identity

    def clipboard(self, text):
        set_clipboard_text(text)
        return "Текст готов."

    def insert(self, text: str, target, *, focus=None) -> str:
        fg = _root(api.GetForegroundWindow())
        reason = ""
        if not fg or not target:
            reason = "Нет активного окна для вставки."
        elif fg != target:
            reason = "Пока шло распознавание, вы переключились в другое окно."
        elif not self.self_elevated and window_is_elevated(fg):
            reason = ("Окно запущено от имени администратора, Windows не даёт обычным программам вводить в него текст. "
                      "Текст в буфере обмена — вставьте его через Ctrl+V.")
        elif not wait_modifiers_released():
            reason = "Были зажаты Ctrl/Alt/Shift/Win — печатать в этот момент небезопасно."
        elif self.foreground() != target or (focus and self.typing_focus(target) != focus):
            reason = "Поле ввода изменилось."
        if reason:
            set_clipboard_text(text)
            return reason

        method = self.cfg.insert_method
        if method == "auto":
            method = "paste" if len(text) > self.cfg.paste_threshold_chars else "type"
        if method == "paste" and self._paste(text):
            return ""
        if send_inputs(text_to_inputs(text)):
            return ""
        set_clipboard_text(text)
        return "Не удалось напечатать текст в это окно."

    def _paste(self, text: str) -> bool:
        try:
            saved = snapshot_clipboard()
            if saved is None:
                log.info("clipboard holds data that can't be restored, typing instead")
                return False
            seq = set_clipboard_text(text, private=True)
            ok = send_inputs([_key(api.VK_LCONTROL), _key(0x56), _key(0x56, flags=api.KEYEVENTF_KEYUP),
                              _key(api.VK_LCONTROL, flags=api.KEYEVENTF_KEYUP)])
            # The target app reads the clipboard asynchronously; restoring too early pastes the old content.
            time.sleep(self.restore_delay_s)
            if api.GetClipboardSequenceNumber() == seq:
                restore_clipboard(saved)
            return ok
        except OSError as e:
            log.warning("paste failed: %s", e)
            return False
