"""Real Windows behaviour: hook, SendInput into a real window, clipboard, tray icon resources.
Runs on the GitHub Actions windows runner (it has an interactive desktop)."""

import ctypes
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")

if sys.platform == "win32":
    from gigadictate import assets
    from gigadictate import hotkey as hk
    from gigadictate.config import Config
    from gigadictate.win import api, inject
    from gigadictate.win.keyboard import KeyboardHook


# ------------------------------------------------------------------ helpers
def raw_key(vk, up=False):
    """A key event WITHOUT our magic marker: looks to the hook like the user pressed it."""
    i = api.INPUT()
    i.type = api.INPUT_KEYBOARD
    i.u.ki = api.KEYBDINPUT(vk, 0, api.KEYEVENTF_KEYUP if up else 0, 0, 0)
    assert api.SendInput(1, ctypes.byref(i), ctypes.sizeof(api.INPUT)) == 1


def force_foreground(hwnd):
    # Windows only lets the process that got the last input steal focus: fake an input first.
    inject.send_inputs([inject._key(0x87), inject._key(0x87, flags=api.KEYEVENTF_KEYUP)])  # F24
    api.SetForegroundWindow(hwnd)


class TkWindow:
    _interpreter = None

    def __init__(self):
        import tkinter as tk

        if TkWindow._interpreter is None:
            TkWindow._interpreter = tk.Tk()
            TkWindow._interpreter.withdraw()
        self.root = tk.Toplevel(TkWindow._interpreter)
        self.root.title("GigaDictate test target")
        self.root.geometry("600x300+100+100")
        self.text = tk.Text(self.root)
        self.text.pack(fill="both", expand=True)
        self.pump(0.3)
        self.hwnd = api.GetAncestor(self.text.winfo_id(), api.GA_ROOT)
        force_foreground(self.hwnd)
        self.text.focus_force()
        self.pump(0.3)
        if inject._root(api.GetForegroundWindow()) != self.hwnd:
            self.close()
            pytest.skip("could not get keyboard focus (no interactive desktop)")

    def pump(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.root.update()
            time.sleep(0.01)

    def wait_text(self, predicate, timeout):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.pump(0.1)
            if predicate(self.value):
                return True
        return False

    @property
    def value(self):
        return self.text.get("1.0", "end-1c")

    def close(self):
        self.root.destroy()


@pytest.fixture
def win():
    w = TkWindow()
    yield w
    w.close()


# ------------------------------------------------------------------ tests
def test_struct_sizes():
    assert ctypes.sizeof(api.INPUT) == 40 and ctypes.sizeof(api.KBDLLHOOKSTRUCT) == 24


def test_icons_create():
    for color in assets.COLORS.values():
        data = assets.icon_resource(assets.render_icon_rgba(24, color))
        buf = ctypes.create_string_buffer(data, len(data))
        h = api.CreateIconFromResourceEx(buf, len(data), True, 0x00030000, 24, 24, 0)
        assert h, api.last_error()
        api.DestroyIcon(h)


def test_clipboard_roundtrip_unicode():
    s = "Привет, мир! Ёж 😀 «кавычки»\nстрока"
    inject.set_clipboard_text(s)
    assert inject.get_clipboard_text() == s


def _formats():
    names = set()
    with inject.Clipboard():
        fmt = 0
        while fmt := api.EnumClipboardFormats(fmt):
            buf = ctypes.create_unicode_buffer(256)
            if ctypes.windll.user32.GetClipboardFormatNameW(fmt, buf, 256):
                names.add(buf.value)
    return names


def test_fallback_clipboard_not_uploaded_to_cloud_but_kept_in_history():
    inject.set_clipboard_text("диктовка")
    names = _formats()
    assert "CanUploadToCloudClipboard" in names
    assert "CanIncludeInClipboardHistory" not in names  # still reachable through Win+V


def test_paste_clipboard_fully_private():
    inject.set_clipboard_text("диктовка", private=True)
    assert {"CanUploadToCloudClipboard", "CanIncludeInClipboardHistory",
            "ExcludeClipboardContentFromMonitorProcessing"} <= _formats()


def test_clipboard_snapshot_restore():
    inject.set_clipboard_text("старое содержимое")
    snap = inject.snapshot_clipboard()
    assert snap is not None
    inject.set_clipboard_text("временное", private=True)
    inject.restore_clipboard(snap)
    assert inject.get_clipboard_text() == "старое содержимое"


def test_hook_hold_and_toggle():
    events = []
    m = hk.HotkeyMachine(hk.parse_hotkey("right ctrl"),
                         hk.Callbacks(lambda: events.append("start"), lambda: events.append("stop"),
                                      lambda: events.append("cancel")), 0.35)
    hook = KeyboardHook(m, allow_injected=True)
    hook.start()
    try:
        raw_key(api.VK_RCONTROL)
        time.sleep(0.6)
        raw_key(api.VK_RCONTROL, up=True)
        time.sleep(0.2)
        assert events == ["start", "stop"]
        raw_key(api.VK_RCONTROL)
        raw_key(api.VK_RCONTROL, up=True)
        time.sleep(0.3)
        raw_key(api.VK_RCONTROL)
        raw_key(api.VK_RCONTROL, up=True)
        time.sleep(0.2)
        assert events == ["start", "stop", "start", "stop"]
        raw_key(api.VK_RCONTROL)
        raw_key(api.VK_RCONTROL, up=True)
        raw_key(hk.VK_ESCAPE)
        raw_key(hk.VK_ESCAPE, up=True)
        time.sleep(0.2)
        assert events[-2:] == ["start", "cancel"]
    finally:
        hook.stop()


def test_hook_swallows_trigger_key(win):
    events = []
    m = hk.HotkeyMachine(hk.parse_hotkey("ctrl+alt+f9"),
                         hk.Callbacks(lambda: events.append("start"), lambda: events.append("stop"),
                                      lambda: events.append("cancel")), 0.35)
    hook = KeyboardHook(m, allow_injected=True)
    hook.start()
    try:
        for vk in (api.VK_LCONTROL, api.VK_LMENU, 0x78):
            raw_key(vk)
        win.pump(0.5)
        for vk in (0x78, api.VK_LMENU, api.VK_LCONTROL):
            raw_key(vk, up=True)
        win.pump(0.3)
        assert events == ["start", "stop"]
        # Alt released after our hotkey must not leave Alt stuck or activate a menu
        assert not inject.modifiers_down()
    finally:
        hook.stop()


def test_type_unicode_into_window(win):
    inj = inject.Injector(Config(insert_method="type"))
    text = "Привет, мир! Ёлка 😀 15:30\nВторая строка "
    assert inj.insert(text, inj.foreground()) == ""
    assert win.wait_text(lambda v: v == text, 5), repr(win.value)


def test_paste_into_window_and_restore_clipboard(win):
    inject.set_clipboard_text("пользовательский буфер")
    inj = inject.Injector(Config(insert_method="paste"))
    text = "Длинный текст через буфер обмена. " * 5
    # As in the app: insertion runs on a worker thread while the target window keeps processing input.
    import threading

    result = []
    target = inj.foreground()
    t = threading.Thread(target=lambda: result.append(inj.insert(text, target)))
    t.start()
    assert win.wait_text(lambda v: v == text, 5), repr(win.value)
    while t.is_alive():
        win.pump(0.05)
    assert result == [""]
    assert inject.get_clipboard_text() == "пользовательский буфер"


def test_window_changed_goes_to_clipboard(win):
    inj = inject.Injector(Config())
    reason = inj.insert("текст", target=_other_window())  # dictation started in another window
    assert "другое окно" in reason
    assert inject.get_clipboard_text() == "текст"
    win.pump(0.3)
    assert win.value == ""


def _other_window():
    # the desktop shell window always exists and is never our test window
    return ctypes.windll.user32.GetShellWindow()


def test_waits_for_modifier_release(win):
    inj = inject.Injector(Config(insert_method="type"))
    raw_key(api.VK_RCONTROL)
    import threading

    threading.Timer(0.4, lambda: raw_key(api.VK_RCONTROL, up=True)).start()
    t = time.monotonic()
    assert inj.insert("абв", inj.foreground()) == ""
    assert time.monotonic() - t >= 0.35  # did not type while Ctrl was held (would be Ctrl+letters)
    assert win.wait_text(lambda v: v == "абв", 5)
