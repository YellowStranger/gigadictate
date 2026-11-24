"""Win32 declarations via ctypes. Every function gets explicit argtypes/restype:
without them 64-bit handles get truncated to 32 bits and crash at random."""

from __future__ import annotations

import ctypes
from ctypes import POINTER, Structure, Union, WINFUNCTYPE, byref, c_int, c_size_t, c_ssize_t, c_uint, c_void_p, sizeof
from ctypes import wintypes as wt

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

LRESULT = c_ssize_t
ULONG_PTR = c_size_t
HHOOK = c_void_p
HICON = c_void_p
HMENU = c_void_p
HGLOBAL = c_void_p

# ------------------------------------------------------------------ constants
WH_KEYBOARD_LL = 13
HC_ACTION = 0
WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x100, 0x101, 0x104, 0x105
WM_TIMER, WM_COMMAND, WM_DESTROY, WM_CLOSE, WM_NULL = 0x113, 0x111, 0x2, 0x10, 0x0
WM_LBUTTONUP, WM_RBUTTONUP, WM_CONTEXTMENU = 0x202, 0x205, 0x7B
WM_APP = 0x8000
WM_QUIT = 0x12
LLKHF_EXTENDED, LLKHF_INJECTED = 0x01, 0x10
VK_PACKET = 0xE7

INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x1, 0x2, 0x4
VK_SHIFT, VK_CONTROL, VK_MENU, VK_RETURN = 0x10, 0x11, 0x12, 0x0D
VK_LSHIFT, VK_RSHIFT, VK_LCONTROL, VK_RCONTROL, VK_LMENU, VK_RMENU, VK_LWIN, VK_RWIN = (
    0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0x5B, 0x5C)

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
GA_ROOT = 2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TokenElevation = 20
ERROR_ALREADY_EXISTS = 183

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO, NIF_SHOWTIP = 0x1, 0x2, 0x4, 0x10, 0x80
NIIF_INFO, NIIF_WARNING, NIIF_NOSOUND = 0x1, 0x2, 0x10
MF_STRING, MF_SEPARATOR, MF_POPUP, MF_CHECKED, MF_GRAYED = 0x0, 0x800, 0x10, 0x8, 0x1
TPM_RIGHTBUTTON, TPM_RETURNCMD, TPM_NONOTIFY = 0x2, 0x100, 0x80
MB_OK, MB_ICONINFORMATION, MB_ICONWARNING, MB_SETFOREGROUND = 0x0, 0x40, 0x30, 0x10000
SM_CXSMICON = 49

# ------------------------------------------------------------------ structures
class KBDLLHOOKSTRUCT(Structure):
    _fields_ = [("vkCode", wt.DWORD), ("scanCode", wt.DWORD), ("flags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class MOUSEINPUT(Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUTUNION(Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(Structure):
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


class GUID(Structure):
    _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD), ("Data4", ctypes.c_ubyte * 8)]


class NOTIFYICONDATAW(Structure):
    _fields_ = [("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", c_uint), ("uFlags", c_uint),
                ("uCallbackMessage", c_uint), ("hIcon", HICON), ("szTip", wt.WCHAR * 128),
                ("dwState", wt.DWORD), ("dwStateMask", wt.DWORD), ("szInfo", wt.WCHAR * 256),
                ("uVersion", c_uint), ("szInfoTitle", wt.WCHAR * 64), ("dwInfoFlags", wt.DWORD),
                ("guidItem", GUID), ("hBalloonIcon", HICON)]


WNDPROC = WINFUNCTYPE(LRESULT, wt.HWND, c_uint, wt.WPARAM, wt.LPARAM)
HOOKPROC = WINFUNCTYPE(LRESULT, c_int, wt.WPARAM, wt.LPARAM)


class WNDCLASSW(Structure):
    _fields_ = [("style", c_uint), ("lpfnWndProc", WNDPROC), ("cbClsExtra", c_int), ("cbWndExtra", c_int),
                ("hInstance", wt.HINSTANCE), ("hIcon", HICON), ("hCursor", c_void_p), ("hbrBackground", c_void_p),
                ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]


class TOKEN_ELEVATION(Structure):
    _fields_ = [("TokenIsElevated", wt.DWORD)]


def _fn(dll, name, restype, *argtypes):
    f = getattr(dll, name)
    f.restype = restype
    f.argtypes = list(argtypes)
    return f


# ------------------------------------------------------------------ functions
GetModuleHandleW = _fn(kernel32, "GetModuleHandleW", wt.HMODULE, wt.LPCWSTR)
GetCurrentThreadId = _fn(kernel32, "GetCurrentThreadId", wt.DWORD)
CreateMutexW = _fn(kernel32, "CreateMutexW", wt.HANDLE, c_void_p, wt.BOOL, wt.LPCWSTR)
CloseHandle = _fn(kernel32, "CloseHandle", wt.BOOL, wt.HANDLE)
GlobalAlloc = _fn(kernel32, "GlobalAlloc", HGLOBAL, c_uint, c_size_t)
GlobalLock = _fn(kernel32, "GlobalLock", c_void_p, HGLOBAL)
GlobalUnlock = _fn(kernel32, "GlobalUnlock", wt.BOOL, HGLOBAL)
GlobalSize = _fn(kernel32, "GlobalSize", c_size_t, HGLOBAL)
GlobalFree = _fn(kernel32, "GlobalFree", HGLOBAL, HGLOBAL)
OpenProcess = _fn(kernel32, "OpenProcess", wt.HANDLE, wt.DWORD, wt.BOOL, wt.DWORD)
GetCurrentProcess = _fn(kernel32, "GetCurrentProcess", wt.HANDLE)

OpenProcessToken = _fn(advapi32, "OpenProcessToken", wt.BOOL, wt.HANDLE, wt.DWORD, POINTER(wt.HANDLE))
GetTokenInformation = _fn(advapi32, "GetTokenInformation", wt.BOOL, wt.HANDLE, c_int, c_void_p, wt.DWORD,
                          POINTER(wt.DWORD))

SetWindowsHookExW = _fn(user32, "SetWindowsHookExW", HHOOK, c_int, HOOKPROC, wt.HINSTANCE, wt.DWORD)
UnhookWindowsHookEx = _fn(user32, "UnhookWindowsHookEx", wt.BOOL, HHOOK)
CallNextHookEx = _fn(user32, "CallNextHookEx", LRESULT, HHOOK, c_int, wt.WPARAM, wt.LPARAM)
GetMessageW = _fn(user32, "GetMessageW", wt.BOOL, POINTER(wt.MSG), wt.HWND, c_uint, c_uint)
PeekMessageW = _fn(user32, "PeekMessageW", wt.BOOL, POINTER(wt.MSG), wt.HWND, c_uint, c_uint, c_uint)
TranslateMessage = _fn(user32, "TranslateMessage", wt.BOOL, POINTER(wt.MSG))
DispatchMessageW = _fn(user32, "DispatchMessageW", LRESULT, POINTER(wt.MSG))
PostThreadMessageW = _fn(user32, "PostThreadMessageW", wt.BOOL, wt.DWORD, c_uint, wt.WPARAM, wt.LPARAM)
PostMessageW = _fn(user32, "PostMessageW", wt.BOOL, wt.HWND, c_uint, wt.WPARAM, wt.LPARAM)
PostQuitMessage = _fn(user32, "PostQuitMessage", None, c_int)
SetTimer = _fn(user32, "SetTimer", ULONG_PTR, wt.HWND, ULONG_PTR, c_uint, c_void_p)
KillTimer = _fn(user32, "KillTimer", wt.BOOL, wt.HWND, ULONG_PTR)
GetAsyncKeyState = _fn(user32, "GetAsyncKeyState", wt.SHORT, c_int)
SendInput = _fn(user32, "SendInput", c_uint, c_uint, POINTER(INPUT), c_int)
GetForegroundWindow = _fn(user32, "GetForegroundWindow", wt.HWND)
SetForegroundWindow = _fn(user32, "SetForegroundWindow", wt.BOOL, wt.HWND)
GetAncestor = _fn(user32, "GetAncestor", wt.HWND, wt.HWND, c_uint)
GetWindowThreadProcessId = _fn(user32, "GetWindowThreadProcessId", wt.DWORD, wt.HWND, POINTER(wt.DWORD))
IsWindow = _fn(user32, "IsWindow", wt.BOOL, wt.HWND)
OpenClipboard = _fn(user32, "OpenClipboard", wt.BOOL, wt.HWND)
CloseClipboard = _fn(user32, "CloseClipboard", wt.BOOL)
EmptyClipboard = _fn(user32, "EmptyClipboard", wt.BOOL)
GetClipboardData = _fn(user32, "GetClipboardData", wt.HANDLE, c_uint)
SetClipboardData = _fn(user32, "SetClipboardData", wt.HANDLE, c_uint, wt.HANDLE)
EnumClipboardFormats = _fn(user32, "EnumClipboardFormats", c_uint, c_uint)
CountClipboardFormats = _fn(user32, "CountClipboardFormats", c_int)
GetClipboardSequenceNumber = _fn(user32, "GetClipboardSequenceNumber", wt.DWORD)
RegisterClipboardFormatW = _fn(user32, "RegisterClipboardFormatW", c_uint, wt.LPCWSTR)
RegisterWindowMessageW = _fn(user32, "RegisterWindowMessageW", c_uint, wt.LPCWSTR)
RegisterClassW = _fn(user32, "RegisterClassW", wt.ATOM, POINTER(WNDCLASSW))
CreateWindowExW = _fn(user32, "CreateWindowExW", wt.HWND, wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, c_int, c_int,
                      c_int, c_int, wt.HWND, HMENU, wt.HINSTANCE, c_void_p)
SetWindowTextW = _fn(user32, "SetWindowTextW", wt.BOOL, wt.HWND, wt.LPCWSTR)
ShowWindow = _fn(user32, "ShowWindow", wt.BOOL, wt.HWND, c_int)
DestroyWindow = _fn(user32, "DestroyWindow", wt.BOOL, wt.HWND)
DefWindowProcW = _fn(user32, "DefWindowProcW", LRESULT, wt.HWND, c_uint, wt.WPARAM, wt.LPARAM)
CreatePopupMenu = _fn(user32, "CreatePopupMenu", HMENU)
AppendMenuW = _fn(user32, "AppendMenuW", wt.BOOL, HMENU, c_uint, ULONG_PTR, wt.LPCWSTR)
DestroyMenu = _fn(user32, "DestroyMenu", wt.BOOL, HMENU)
TrackPopupMenu = _fn(user32, "TrackPopupMenu", c_int, HMENU, c_uint, c_int, c_int, c_int, wt.HWND, c_void_p)
GetCursorPos = _fn(user32, "GetCursorPos", wt.BOOL, POINTER(wt.POINT))
CreateIconFromResourceEx = _fn(user32, "CreateIconFromResourceEx", HICON, c_void_p, wt.DWORD, wt.BOOL, wt.DWORD,
                               c_int, c_int, c_uint)
DestroyIcon = _fn(user32, "DestroyIcon", wt.BOOL, HICON)
GetSystemMetrics = _fn(user32, "GetSystemMetrics", c_int, c_int)
MessageBoxW = _fn(user32, "MessageBoxW", c_int, wt.HWND, wt.LPCWSTR, wt.LPCWSTR, c_uint)
Shell_NotifyIconW = _fn(shell32, "Shell_NotifyIconW", wt.BOOL, wt.DWORD, POINTER(NOTIFYICONDATAW))

assert sizeof(INPUT) == (40 if sizeof(c_void_p) == 8 else 28)
assert sizeof(KBDLLHOOKSTRUCT) == (24 if sizeof(c_void_p) == 8 else 20)


def last_error() -> int:
    return ctypes.get_last_error()


__all__ = [n for n in dir() if not n.startswith("_")] + ["byref", "sizeof"]
