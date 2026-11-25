"""A non-activating clipboard caption. The fade timer runs only during transitions."""
import ctypes as c
import time
from ctypes import wintypes as w

from . import api

gdi = c.WinDLL("gdi32")
brush = api._fn(gdi, "CreateSolidBrush", w.HANDLE, w.DWORD)
region = api._fn(gdi, "CreateRoundRectRgn", w.HANDLE, *([c.c_int] * 6))
set_region = api._fn(api.user32, "SetWindowRgn", c.c_int, w.HWND, w.HANDLE, w.BOOL)
set_alpha = api._fn(api.user32, "SetLayeredWindowAttributes", w.BOOL, w.HWND, w.DWORD, w.BYTE, w.DWORD)
color = api._fn(gdi, "SetTextColor", w.DWORD, w.HDC, w.DWORD)
background = api._fn(gdi, "SetBkColor", w.DWORD, w.HDC, w.DWORD)
font = api._fn(gdi, "CreateFontW", w.HANDLE, *([c.c_int] * 5), *([w.DWORD] * 8), w.LPCWSTR)
send = api._fn(api.user32, "SendMessageW", api.LRESULT, w.HWND, w.UINT, w.WPARAM, w.LPARAM)


position = api._fn(api.user32, "SetWindowPos", w.BOOL, w.HWND, w.HWND, *([c.c_int] * 4), w.UINT)
screen = api._fn(api.user32, "ClientToScreen", w.BOOL, w.HWND, c.POINTER(w.POINT))
bounds = api._fn(api.user32, "GetWindowRect", w.BOOL, w.HWND, c.POINTER(w.RECT))


class MonitorInfo(c.Structure):
    _fields_ = [("size", w.DWORD), ("monitor", w.RECT), ("work", w.RECT), ("flags", w.DWORD)]


monitor_at = api._fn(api.user32, "MonitorFromRect", w.HANDLE, c.POINTER(w.RECT), w.DWORD)
monitor_info = api._fn(api.user32, "GetMonitorInfoW", w.BOOL, w.HANDLE, c.POINTER(MonitorInfo))


def work_area(rect):
    info = MonitorInfo()
    info.size = c.sizeof(info)
    if monitor_info(monitor_at(c.byref(rect), 2), c.byref(info)):
        return info.work


class Preview:
    def __init__(self, owner, instance, compact=False):
        self.started = 0
        self.alpha = self.goal = 0
        self.bg = brush(0x352921)
        self.proc = api.WNDPROC(self.dispatch)
        wc = api.WNDCLASSW()
        wc.lpfnWndProc, wc.hInstance = self.proc, instance
        wc.lpszClassName = "GigaDictateMeter" if compact else "GigaDictateClipboard"
        wc.hbrBackground = self.bg
        api.RegisterClassW(c.byref(wc))
        width = 100 if compact else min(540, api.GetSystemMetrics(0))
        height_px = 28 if compact else 100
        self.hwnd = api.CreateWindowExW(0x080800A8 if compact else 0x08080088, wc.lpszClassName, "Диктовка" if compact else "Диктовка в буфер обмена",
                                        0x80000000, (api.GetSystemMetrics(0)-width)//2,
                                        max(0, api.GetSystemMetrics(1)-180), width, height_px, owner, None, instance, None)
        set_region(self.hwnd, region(0, 0, width+1, height_px+1, 24, 24), True)
        rows = [(5, 15, 600, "")] if compact else [(14, 15, 600, "●   В буфер обмена"), (40, 20, 400, "")]
        for y, height, weight, label in rows:
            child = api.CreateWindowExW(0, "STATIC", label, 0x50000080, 8 if compact else 20, y, width-12 if compact else width-40, 52,
                                        self.hwnd, None, instance, None)
            face = font(-height, 0, 0, 0, weight, 0, 0, 0, 1, 0, 0, 5, 0, "Segoe UI")
            send(child, 0x30, face, 0)
            if label:
                self.header = child
            else:
                self.body = child

    def meter(self, target, level, anchor):
        from .draft import GuiInfo, gui_info
        if api.GetForegroundWindow() != target:
            self.update("")
            return
        info = GuiInfo()
        info.size = c.sizeof(info)
        if gui_info(0, c.byref(info)) and info.caret:
            point = w.POINT(info.rect.right, info.rect.bottom)
            if screen(info.caret, c.byref(point)):
                anchor = point.x, point.y
        if not anchor:
            rect = w.RECT()
            if not bounds(info.focus or target, c.byref(rect)):
                return
            anchor = rect.left + 12, rect.bottom - 56
        if not self.goal:
            self.started = time.monotonic()
        intro = time.monotonic() - self.started < 1
        width = 100 if intro else 42
        x, y = anchor
        # Clamp to the work area of the monitor containing the caret.
        rect = w.RECT(x, y, x+1, y+1)
        work = work_area(rect)
        if work:
            x = max(work.left, min(x, work.right-width))
            y = y+6 if y+34 < work.bottom else y-44
            y = max(work.top, min(y, work.bottom-28))
        position(self.hwnd, None, x, y, width, 28, 0x14)
        if getattr(self, "width", 100) != width:
            set_region(self.hwnd, region(0, 0, width+1, 29, 24, 24), True)
        self.width = width
        self.update("● Диктовка" if intro else ("●", "▂▃▂", "▃▆▃", "▅█▅")[level])

    def update(self, text):
        if text:
            line = " ".join(text.split())
            api.SetWindowTextW(self.body, line if len(line) <= 85 else "…" + line[-84:])
            if not self.alpha:
                set_alpha(self.hwnd, 0, 0, 2)
                api.ShowWindow(self.hwnd, 4)
        self.goal = 250 if text else 0
        if self.goal != self.alpha:
            api.SetTimer(self.hwnd, 1, 16, None)

    def dispatch(self, hwnd, msg, wp, lp):
        if msg == 0x138:  # WM_CTLCOLORSTATIC
            color(wp, 0xD2DE85 if lp == getattr(self, "header", None) else 0xFAF5F1)
            background(wp, 0x352921)
            return self.bg
        if msg == api.WM_TIMER:
            self.alpha += max(-32, min(32, self.goal-self.alpha))
            set_alpha(hwnd, 0, self.alpha, 2)
            if self.goal == self.alpha:
                api.KillTimer(hwnd, 1)
                if not self.alpha:
                    api.ShowWindow(hwnd, 0)
            return 0
        return api.DefWindowProcW(hwnd, msg, wp, lp)
