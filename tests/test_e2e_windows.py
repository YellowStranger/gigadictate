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
        raw_key(api.VK_RCONTROL)
        win.pump(4.6)  # "speak" while the key is held (the WAV is 4.2 s)
        raw_key(api.VK_RCONTROL, up=True)
        assert win.wait_text(lambda v: "диктовки" in v.lower(), 20), win.value
        print("typed:", repr(win.value))
        assert "15:30" in win.value and win.value.endswith(" ")

        # toggle mode: tap, speak, tap
        win.text.delete("1.0", "end")
        raw_key(api.VK_RCONTROL)
        raw_key(api.VK_RCONTROL, up=True)
        win.pump(4.6)
        raw_key(api.VK_RCONTROL)
        raw_key(api.VK_RCONTROL, up=True)
        assert win.wait_text(lambda v: "диктовки" in v.lower(), 20), win.value

        # Esc cancels: nothing typed, Esc not delivered
        win.text.delete("1.0", "end")
        raw_key(api.VK_RCONTROL)
        raw_key(api.VK_RCONTROL, up=True)
        win.pump(1.5)
        raw_key(0x1B)
        raw_key(0x1B, up=True)
        win.pump(3)
        assert win.value == ""

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
