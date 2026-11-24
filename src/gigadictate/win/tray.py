"""Notification-area icon with a menu, balloon notifications and sounds (pure Win32)."""

from __future__ import annotations

import ctypes
import logging
import os
import queue
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .. import assets
from . import api

log = logging.getLogger(__name__)

WM_TRAY = api.WM_APP + 1
WM_REFRESH = api.WM_APP + 2
WM_CALL = api.WM_APP + 3


@dataclass
class MenuItem:
    label: str
    action: Callable[[], None] | None = None
    checked: bool = False
    enabled: bool = True
    submenu: list["MenuItem"] = field(default_factory=list)


SEPARATOR = MenuItem("-")

STATE_TEXT = {"idle": "готов", "recording": "идёт запись…", "processing": "распознаю…", "loading": "загрузка…"}


class Tray:
    def __init__(self, title: str, build_menu: Callable[[], list[MenuItem]], on_left_click: Callable[[], None] = None):
        self.title = title
        self.build_menu = build_menu
        self.on_left_click = on_left_click
        self.state = "loading"
        self.hint = ""
        self._calls: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._wndproc = api.WNDPROC(self._proc)
        self._msg_taskbar_created = api.RegisterWindowMessageW("TaskbarCreated")
        hinst = api.GetModuleHandleW(None)
        wc = api.WNDCLASSW()
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = hinst
        wc.lpszClassName = "GigaDictateTray"
        api.RegisterClassW(ctypes.byref(wc))
        # A hidden top-level window (not message-only: those miss the TaskbarCreated broadcast
        # that tells us to re-add the icon after Explorer restarts).
        self.hwnd = api.CreateWindowExW(0, "GigaDictateTray", title, 0, 0, 0, 0, 0, None, None, hinst, None)
        size = api.GetSystemMetrics(api.SM_CXSMICON) or 16
        self.icons = {}
        for name, color in assets.COLORS.items():
            data = assets.icon_resource(assets.render_icon_rgba(size, color))
            buf = ctypes.create_string_buffer(data, len(data))
            self.icons[name] = api.CreateIconFromResourceEx(buf, len(data), True, 0x00030000, size, size, 0)
        self._sound_dir = Path(tempfile.gettempdir()) / "GigaDictate"
        self._sound_dir.mkdir(exist_ok=True)
        for name, (freqs, dur) in assets.SOUNDS.items():
            p = self._sound_dir / f"{name}.wav"
            data = assets.tone_wav(freqs, dur)
            if not p.exists() or p.read_bytes() != data:
                p.write_bytes(data)
        from .preview import Preview
        self.caption = Preview(self.hwnd, hinst)
        self._add_icon()

    # ---------------------------------------------------------------- UI protocol (any thread)
    def set_state(self, state: str) -> None:
        self.state = state
        api.PostMessageW(self.hwnd, WM_REFRESH, 0, 0)

    def preview(self, text: str) -> None:
        self.call(lambda: self.caption.update(text))

    def set_hint(self, hint: str) -> None:
        self.hint = hint
        api.PostMessageW(self.hwnd, WM_REFRESH, 0, 0)

    def notify(self, title: str, text: str, error: bool = False) -> None:
        log.info("notify: %s: %s", title, text.replace("\n", " "))
        self.call(lambda: self._balloon(title, text, error))

    def play(self, sound: str) -> None:
        try:
            import winsound

            winsound.PlaySound(str(self._sound_dir / f"{sound}.wav"),
                               winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
        except Exception as e:
            log.debug("sound: %s", e)

    def call(self, fn: Callable[[], None]) -> None:
        """Run fn on the tray (main) thread."""
        self._calls.put(fn)
        api.PostMessageW(self.hwnd, WM_CALL, 0, 0)

    def quit(self) -> None:
        api.PostMessageW(self.hwnd, api.WM_CLOSE, 0, 0)

    def run(self) -> None:
        msg = api.wt.MSG()
        while api.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            api.TranslateMessage(ctypes.byref(msg))
            api.DispatchMessageW(ctypes.byref(msg))

    # ---------------------------------------------------------------- internals (tray thread)
    def _nid(self, flags: int) -> api.NOTIFYICONDATAW:
        nid = api.NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(nid)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = flags
        return nid

    def _tooltip(self) -> str:
        tip = f"{self.title} — {STATE_TEXT.get(self.state, self.state)}"
        if self.hint:
            tip += f"\n{self.hint}"
        return tip[:127]

    def _add_icon(self) -> None:
        nid = self._nid(api.NIF_MESSAGE | api.NIF_ICON | api.NIF_TIP)
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self.icons.get(self.state)
        nid.szTip = self._tooltip()
        if not api.Shell_NotifyIconW(api.NIM_ADD, ctypes.byref(nid)):
            api.Shell_NotifyIconW(api.NIM_MODIFY, ctypes.byref(nid))

    def _refresh(self) -> None:
        nid = self._nid(api.NIF_ICON | api.NIF_TIP)
        nid.hIcon = self.icons.get(self.state)
        nid.szTip = self._tooltip()
        api.Shell_NotifyIconW(api.NIM_MODIFY, ctypes.byref(nid))

    def _balloon(self, title: str, text: str, error: bool) -> None:
        nid = self._nid(api.NIF_INFO)
        nid.szInfoTitle = title[:63]
        nid.szInfo = text[:255]
        nid.dwInfoFlags = (api.NIIF_WARNING if error else api.NIIF_INFO) | api.NIIF_NOSOUND
        api.Shell_NotifyIconW(api.NIM_MODIFY, ctypes.byref(nid))

    def _show_menu(self) -> None:
        items = self.build_menu()
        actions: dict[int, Callable[[], None]] = {}
        menus = []

        def build(entries: list[MenuItem]):
            h = api.CreatePopupMenu()
            menus.append(h)
            for it in entries:
                if it.label == "-":
                    api.AppendMenuW(h, api.MF_SEPARATOR, 0, None)
                elif it.submenu:
                    api.AppendMenuW(h, api.MF_POPUP | (0 if it.enabled else api.MF_GRAYED), build(it.submenu),
                                    it.label)
                else:
                    cid = len(actions) + 1
                    if it.action:
                        actions[cid] = it.action
                    flags = api.MF_STRING | (api.MF_CHECKED if it.checked else 0)
                    if not it.enabled or not it.action:
                        flags |= api.MF_GRAYED
                    api.AppendMenuW(h, flags, cid, it.label)
            return h

        root = build(items)
        pt = api.wt.POINT()
        api.GetCursorPos(ctypes.byref(pt))
        api.SetForegroundWindow(self.hwnd)  # otherwise the menu doesn't close when clicking elsewhere
        cmd = api.TrackPopupMenu(root, api.TPM_RIGHTBUTTON | api.TPM_RETURNCMD | api.TPM_NONOTIFY,
                                 pt.x, pt.y, 0, self.hwnd, None)
        api.PostMessageW(self.hwnd, api.WM_NULL, 0, 0)
        api.DestroyMenu(root)  # destroys submenus too
        if cmd in actions:
            try:
                actions[cmd]()
            except Exception:
                log.exception("menu action failed")

    def _proc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TRAY:
                event = lparam & 0xFFFF
                if event in (api.WM_RBUTTONUP, api.WM_CONTEXTMENU):
                    self._show_menu()
                elif event == api.WM_LBUTTONUP:
                    (self.on_left_click or self._show_menu)()
                return 0
            if msg == WM_REFRESH:
                self._refresh()
                return 0
            if msg == WM_CALL:
                while not self._calls.empty():
                    try:
                        self._calls.get_nowait()()
                    except Exception:
                        log.exception("tray call failed")
                return 0
            if msg == self._msg_taskbar_created and msg:
                self._add_icon()  # Explorer restarted: our icon is gone, add it again
                return 0
            if msg == api.WM_CLOSE:
                api.Shell_NotifyIconW(api.NIM_DELETE, ctypes.byref(self._nid(0)))
                api.DestroyWindow(hwnd)
                return 0
            if msg == api.WM_DESTROY:
                api.PostQuitMessage(0)
                return 0
        except Exception:
            log.exception("tray window proc")
        return api.DefWindowProcW(hwnd, msg, wparam, lparam)


def message_box(text: str, title: str = "GigaDictate", warning: bool = False) -> None:
    api.MessageBoxW(None, text, title, (api.MB_ICONWARNING if warning else api.MB_ICONINFORMATION) | api.MB_SETFOREGROUND)


def open_folder(path: Path) -> None:
    os.startfile(str(path))  # type: ignore[attr-defined]
