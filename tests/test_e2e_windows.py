"""End-to-end: start the real app (or the built .exe), press the hotkey like a user,
check that dictated text appears in a real window. A WAV file stands in for the mic.
Also measures RAM and idle CPU of the running app."""

import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes as wt
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "tests" / "data"

if sys.platform == "win32":
    from gigadictate.win import api
    from test_windows import TkWindow, raw_key


class PMC(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD)] + [
        (n, ctypes.c_size_t) for n in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                                       "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                                       "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]


def proc_stats(pid):
    h = ctypes.windll.kernel32.OpenProcess(0x1000 | 0x0010, False, pid)
    pmc = PMC()
    pmc.cb = ctypes.sizeof(pmc)
    ctypes.windll.psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb)
    c, e, k, u = (wt.FILETIME() for _ in range(4))
    ctypes.windll.kernel32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u))
    ctypes.windll.kernel32.CloseHandle(h)
    ft = lambda f: ((f.dwHighDateTime << 32) | f.dwLowDateTime) / 1e7  # noqa: E731
    return pmc.WorkingSetSize / 2**20, pmc.PagefileUsage / 2**20, ft(k) + ft(u)


@pytest.fixture
def app(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text('{"welcomed": true, "sounds": false}', "utf-8")
    env = dict(os.environ, GIGADICTATE_HOME=str(home), GIGADICTATE_FAKE_MIC=str(DATA / "short.wav"),
               GIGADICTATE_TEST_HOOK="1")
    exe = os.environ.get("GIGADICTATE_EXE")
    if exe:
        cmd = [exe]
    else:
        cmd = [sys.executable, "-m", "gigadictate"]
        env["PYTHONPATH"] = str(ROOT / "src")
    p = subprocess.Popen(cmd, env=env)
    log = home / "gigadictate.log"
    end = time.monotonic() + 60
    while time.monotonic() < end:
        if log.exists() and "model gigaam-v3-e2e-rnnt loaded" in log.read_text("utf-8", "replace"):
            break
        assert p.poll() is None, f"app exited: {log.read_text('utf-8', 'replace') if log.exists() else ''}"
        time.sleep(0.2)
    else:
        pytest.fail("app did not become ready: " + (log.read_text("utf-8", "replace") if log.exists() else ""))
    p.log = log
    yield p
    p.terminate()
    p.wait(10)
    print("\n--- app log ---\n" + log.read_text("utf-8", "replace"))


def test_hold_to_talk_types_text(app):
    win = TkWindow()
    try:
        # A real Win32 EDIT child, addressable across processes during push-to-talk.
        user32 = ctypes.windll.user32
        edit = wt.HWND(api.CreateWindowExW(0, "EDIT", "", 0x50000000, 0, 0, 240, 32,
                                           win.hwnd, None, None, None))
        user32.SetFocus(edit)
        def value():
            buf = ctypes.create_unicode_buffer(2048)
            user32.GetWindowTextW(edit, buf, len(buf))
            return buf.value

        raw_key(api.VK_RCONTROL)
        deadline = time.monotonic() + 15
        while "диктовки" not in value().lower():
            assert time.monotonic() < deadline, value()
            win.pump(0.1)
        win.pump(2.8)  # capture the rest even if the last preview precedes the last syllable
        raw_key(api.VK_RCONTROL, up=True)
        deadline = time.monotonic() + 12
        while not value().endswith(" "):
            assert time.monotonic() < deadline, value()
            win.pump(0.1)
        assert value().count("15:30") == 1
        api.SetWindowTextW(edit, "")
        raw_key(api.VK_RCONTROL)
        raw_key(api.VK_RCONTROL, up=True)
        deadline = time.monotonic() + 15
        while not value():
            assert time.monotonic() < deadline
            win.pump(0.1)
        raw_key(0x1B)
        raw_key(0x1B, up=True)
        win.pump(1)
        assert value() == ""
        # Without an editable target the card stays on top without taking focus.
        button = wt.HWND(api.CreateWindowExW(0, "BUTTON", "OK", 0x50000000, 0, 0, 80, 32,
                                             win.hwnd, None, None, None))
        user32.SetFocus(button)
        raw_key(api.VK_RCONTROL)
        win.pump(4.7)
        user32.FindWindowW.restype = wt.HWND
        card = wt.HWND(user32.FindWindowW("GigaDictateClipboard", None))
        assert card.value and user32.IsWindowVisible(card)
        raw_key(api.VK_RCONTROL, up=True)
        from gigadictate.win.inject import get_clipboard_text
        deadline = time.monotonic() + 12
        while "15:30" not in (get_clipboard_text() or ""):
            assert time.monotonic() < deadline
            win.pump(0.1)
        win.pump(0.3)
        assert not user32.IsWindowVisible(card)
        api.DestroyWindow(button)
        api.DestroyWindow(edit)
        win.text.focus_force()
        win.pump(0.1)

        # Tk text accepts Unicode typing but exposes no replaceable native EDIT range.
        from gigadictate.win.inject import set_clipboard_text
        set_clipboard_text("исходный буфер")
        raw_key(api.VK_RCONTROL)
        win.pump(4.7)
        raw_key(api.VK_RCONTROL, up=True)
        assert win.wait_text(lambda v: "15:30" in v, 15), win.value
        assert get_clipboard_text() == "исходный буфер"
        win.text.delete("1.0", "end")

        # a shortcut with the hotkey (Right Ctrl + A) is not a dictation
        win.text.insert("1.0", "abc")
        raw_key(api.VK_RCONTROL)
        raw_key(0x41)
        raw_key(0x41, up=True)
        raw_key(api.VK_RCONTROL, up=True)
        win.pump(3)
        assert win.value == "abc"
    finally:
        win.close()


def test_resources(app):
    ws, private, cpu0 = proc_stats(app.pid)
    time.sleep(10)
    _, _, cpu1 = proc_stats(app.pid)
    idle_cpu = (cpu1 - cpu0) / 10 * 100
    print(f"\nRAM: working set {ws:.0f} MB, private {private:.0f} MB; idle CPU {idle_cpu:.2f}% of one core")
    assert idle_cpu < 1.0
    assert private < 900


def test_browser_message_field(app):
    from playwright.sync_api import sync_playwright
    from gigadictate.win.inject import get_clipboard_text, set_clipboard_text

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=False)
        page = browser.new_page()
        page.set_content('<div role="textbox" aria-label="Сообщение" contenteditable="true">Черновик: </div>')
        page.evaluate("document.title = 'GigaDictate browser test'")
        page.bring_to_front()
        from test_windows import force_foreground
        user32 = ctypes.windll.user32
        user32.FindWindowW.restype = wt.HWND
        processes = browser.new_browser_cdp_session().send("SystemInfo.getProcessInfo")["processInfo"]
        browser_pid = next(item["id"] for item in processes if item["type"] == "browser")
        windows = []
        @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
        def collect(handle, _):
            pid = wt.DWORD()
            api.GetWindowThreadProcessId(handle, ctypes.byref(pid))
            name = ctypes.create_unicode_buffer(128)
            user32.GetClassNameW(wt.HWND(handle), name, 128)
            if pid.value == browser_pid and name.value == "Chrome_WidgetWin_1" and user32.IsWindowVisible(wt.HWND(handle)):
                windows.append(handle)
            return True
        user32.EnumWindows(collect, 0)
        assert windows, "Chrome window not found"
        hwnd = windows[0]
        force_foreground(hwnd)
        assert api.GetForegroundWindow() == hwnd
        editor = page.get_by_role("textbox")
        editor.click()
        editor.press("End")
        set_clipboard_text("исходный буфер")
        raw_key(api.VK_RCONTROL)
        try:
            page.wait_for_function('document.querySelector("[contenteditable]").innerText.includes("диктовки")',
                                   timeout=15000)
            page.wait_for_timeout(2600)
        finally:
            raw_key(api.VK_RCONTROL, up=True)
        page.wait_for_function('document.querySelector("[contenteditable]").innerText.includes("15:30")',
                               timeout=20000)
        deadline = time.monotonic() + 10
        while "dictation committed to editor" not in app.log.read_text("utf-8", "replace"):
            assert time.monotonic() < deadline, "final result did not reach the browser field"
            time.sleep(0.05)
        value = editor.inner_text()
        assert value.startswith("Черновик:") and value.count("15:30") == 1
        deadline = time.monotonic() + 5
        while get_clipboard_text() != "исходный буфер":
            assert time.monotonic() < deadline, "clipboard did not return to its initial state"
            time.sleep(0.05)
        browser.close()


def test_word_document_if_installed(app):
    import winreg
    from comtypes.client import CreateObject
    from test_windows import force_foreground

    try:
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"Word.Application\CLSID"))
    except FileNotFoundError:
        pytest.skip("Microsoft Word is not installed on this Windows machine")
    office = CreateObject("Word.Application", dynamic=True)
    try:
        office.Visible = True
        doc = office.Documents.Add()
        office.Activate()
        force_foreground(office.ActiveWindow.Hwnd)
        raw_key(api.VK_RCONTROL)
        try:
            time.sleep(4.8)
        finally:
            raw_key(api.VK_RCONTROL, up=True)
        deadline = time.monotonic() + 20
        while "15:30" not in doc.Content.Text:
            assert time.monotonic() < deadline
            time.sleep(0.1)
        assert doc.Content.Text.count("15:30") == 1
    finally:
        office.Quit(0)
