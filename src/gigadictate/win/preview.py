"""A non-activating clipboard caption. The fade timer runs only during transitions."""
import ctypes as c
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


class Preview:
    def __init__(self, owner, instance):
        self.alpha = self.goal = 0
        self.bg = brush(0x352921)
        self.proc = api.WNDPROC(self.dispatch)
        wc = api.WNDCLASSW()
        wc.lpfnWndProc, wc.hInstance = self.proc, instance
        wc.lpszClassName = "GigaDictateClipboard"
        wc.hbrBackground = self.bg
        api.RegisterClassW(c.byref(wc))
        width = min(540, api.GetSystemMetrics(0))
        self.hwnd = api.CreateWindowExW(0x08080088, wc.lpszClassName, "Диктовка в буфер обмена",
                                        0x80000000, (api.GetSystemMetrics(0)-width)//2,
                                        max(0, api.GetSystemMetrics(1)-180), width, 100, owner, None, instance, None)
        set_region(self.hwnd, region(0, 0, width+1, 101, 24, 24), True)
        for y, height, weight, label in [(14, 15, 600, "●   В буфер обмена"), (40, 20, 400, "")]:
            child = api.CreateWindowExW(0, "STATIC", label, 0x50000080, 20, y, width-40, 52,
                                        self.hwnd, None, instance, None)
            face = font(-height, 0, 0, 0, weight, 0, 0, 0, 1, 0, 0, 5, 0, "Segoe UI")
            send(child, 0x30, face, 0)
            if label:
                self.header = child
            else:
                self.body = child

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
