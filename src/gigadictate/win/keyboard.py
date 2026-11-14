"""Global hotkey via a low-level keyboard hook (needed for push-to-talk: RegisterHotKey
gives no key-up events and cannot bind a lone Right Ctrl).

The hook callback must return within ~300 ms or Windows silently removes the hook,
so it only runs the tiny state machine; everything slow happens elsewhere.
"""

from __future__ import annotations

import ctypes
import logging
import queue
import threading
from typing import Callable

from .. import hotkey as hk
from . import api

log = logging.getLogger(__name__)

INJECT_MAGIC = 0x47444354  # 'GDCT' in dwExtraInfo marks our own synthetic keys
_WM_WAKE = api.WM_APP + 7
_TICK_MS = 100
_REINSTALL_EVERY_TICKS = 10 * 60 * 1000 // _TICK_MS  # re-hook every 10 min in case Windows dropped it
_MENU_KEYS = {api.VK_LMENU, api.VK_RMENU, api.VK_LWIN, api.VK_RWIN}


def _normalize_vk(vk: int, scan: int, flags: int) -> int:
    if vk == api.VK_SHIFT:
        return api.VK_RSHIFT if scan == 0x36 else api.VK_LSHIFT
    if vk == api.VK_CONTROL:
        return api.VK_RCONTROL if flags & api.LLKHF_EXTENDED else api.VK_LCONTROL
    if vk == api.VK_MENU:
        return api.VK_RMENU if flags & api.LLKHF_EXTENDED else api.VK_LMENU
    return vk


def is_down(vk: int) -> bool:
    return bool(api.GetAsyncKeyState(vk) & 0x8000)


def send_keys(events: list[tuple[int, bool]]) -> int:
    arr = (api.INPUT * len(events))()
    for i, (vk, up) in enumerate(events):
        arr[i].type = api.INPUT_KEYBOARD
        arr[i].u.ki = api.KEYBDINPUT(vk, 0, api.KEYEVENTF_KEYUP if up else 0, 0, INJECT_MAGIC)
    return api.SendInput(len(events), arr, ctypes.sizeof(api.INPUT))


class KeyboardHook:
    def __init__(self, machine: hk.HotkeyMachine, allow_injected: bool = False):
        self.machine = machine
        self.allow_injected = allow_injected  # automated tests drive the hotkey with SendInput
        self.capture: hk.HotkeyCapture | None = None
        self._cmds: queue.Queue = queue.Queue()
        self._tid = 0
        self._hook = None
        self._proc = api.HOOKPROC(self._callback)  # keep a reference: GC'd callback = crash
        self._ready = threading.Event()
        self._fired = False  # combo was triggered during the current hold (for Alt/Win masking)
        self._thread = threading.Thread(target=self._run, name="keyboard-hook", daemon=True)
        self.error: str | None = None

    # ---------------------------------------------------------------- thread-safe API
    def start(self) -> None:
        self._thread.start()
        self._ready.wait(5)
        if self.error:
            raise OSError(self.error)

    def call(self, fn: Callable[[], None]) -> None:
        """Run fn on the hook thread (the only thread allowed to touch the machine)."""
        self._cmds.put(fn)
        if self._tid:
            api.PostThreadMessageW(self._tid, _WM_WAKE, 0, 0)

    def set_combo(self, combo: hk.Combo) -> None:
        self.call(lambda: self.machine.set_combo(combo))

    def force_stop(self) -> None:
        self.call(self.machine.force_stop)

    def start_capture(self, on_done: Callable[[hk.Combo | None], None]) -> None:
        def begin():
            self.machine.reset()

            def done(combo):
                self.capture = None
                on_done(combo)

            self.capture = hk.HotkeyCapture(done)

        self.call(begin)

    def stop(self) -> None:
        if self._tid:
            api.PostThreadMessageW(self._tid, api.WM_QUIT, 0, 0)
        self._thread.join(2)

    # ---------------------------------------------------------------- hook thread
    def _install(self) -> None:
        if self._hook:
            api.UnhookWindowsHookEx(self._hook)
        self._hook = api.SetWindowsHookExW(api.WH_KEYBOARD_LL, self._proc, api.GetModuleHandleW(None), 0)
        if not self._hook:
            raise OSError(f"SetWindowsHookEx failed: {api.last_error()}")

    def _run(self) -> None:
        msg = api.wt.MSG()
        api.PeekMessageW(api.byref(msg), None, 0, 0, 0)  # create the thread's message queue
        self._tid = api.GetCurrentThreadId()
        try:
            self._install()
        except OSError as e:
            self.error = str(e)
            self._ready.set()
            return
        timer = api.SetTimer(None, 0, _TICK_MS, None)
        self._ready.set()
        ticks = 0
        while api.GetMessageW(api.byref(msg), None, 0, 0) > 0:
            if msg.message == api.WM_TIMER:
                ticks += 1
                self._tick(ticks)
            elif msg.message == _WM_WAKE:
                self._drain()
            api.TranslateMessage(api.byref(msg))
            api.DispatchMessageW(api.byref(msg))
        api.KillTimer(None, timer)
        if self._hook:
            api.UnhookWindowsHookEx(self._hook)

    def _drain(self) -> None:
        while True:
            try:
                fn = self._cmds.get_nowait()
            except queue.Empty:
                return
            try:
                fn()
            except Exception:
                log.exception("hook command failed")

    def _tick(self, ticks: int) -> None:
        self._drain()
        if self.machine.down:
            self.machine.tick(is_down)
            if not self.machine.down:
                self._fired = False
        if ticks % _REINSTALL_EVERY_TICKS == 0 and not self.machine.down and self.machine.state == hk.IDLE:
            try:
                self._install()
            except OSError:
                log.exception("re-installing keyboard hook failed")

    def _callback(self, code: int, wparam: int, lparam: int) -> int:
        try:
            if code == api.HC_ACTION:
                k = api.KBDLLHOOKSTRUCT.from_address(lparam)
                injected = bool(k.flags & api.LLKHF_INJECTED)
                if not (k.dwExtraInfo == INJECT_MAGIC or k.vkCode == api.VK_PACKET
                        or (injected and not self.allow_injected)):
                    if self._handle(wparam, _normalize_vk(k.vkCode, k.scanCode, k.flags)):
                        return 1
        except Exception:
            log.exception("keyboard hook callback")
        return api.CallNextHookEx(None, code, wparam, lparam)

    def _handle(self, wparam: int, vk: int) -> bool:
        down = wparam in (api.WM_KEYDOWN, api.WM_SYSKEYDOWN)
        if self.capture is not None:
            return self.capture.key_down(vk) if down else self.capture.key_up(vk)
        m = self.machine
        if down:
            before = m.state
            swallow = m.key_down(vk)
            if m.satisfied() and m.state != before:
                self._fired = True
            return swallow
        swallow = m.key_up(vk)
        if not swallow and self._fired and vk in _MENU_KEYS and vk in m._combo_vks:
            # Releasing Alt/Win after using it in our hotkey would open the Start menu or
            # focus the app's menu bar. Replace the release with: dummy key + release.
            send_keys([(hk.VK_MASK, False), (hk.VK_MASK, True), (vk, True)])
            swallow = True
        if not m.down:
            self._fired = False
        return swallow
