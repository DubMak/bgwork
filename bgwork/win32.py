"""Thin ctypes bindings for the Win32 calls bgwork needs."""
import ctypes
import ctypes.wintypes as W

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")
shcore = ctypes.WinDLL("shcore")

LRESULT = ctypes.c_ssize_t
GENERIC_ALL = 0x10000000

# messages
WM_ACTIVATE, WM_SETFOCUS, WM_KILLFOCUS = 0x0006, 0x0007, 0x0008
WM_KEYDOWN, WM_KEYUP, WM_CHAR = 0x0100, 0x0101, 0x0102
WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0104, 0x0105
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK = 0x0201, 0x0202, 0x0203
WM_RBUTTONDOWN, WM_RBUTTONUP, WM_RBUTTONDBLCLK = 0x0204, 0x0205, 0x0206
WM_MBUTTONDOWN, WM_MBUTTONUP, WM_MBUTTONDBLCLK = 0x0207, 0x0208, 0x0209
WM_MOUSEWHEEL, WM_MOUSEHWHEEL = 0x020A, 0x020E
MK_LBUTTON, MK_RBUTTON, MK_SHIFT, MK_CONTROL, MK_MBUTTON = 0x1, 0x2, 0x4, 0x8, 0x10

SW_RESTORE, SW_MAXIMIZE = 9, 3
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x1, 0x2, 0x10
HWND_TOP = 0
CWP_SKIPINVISIBLE, CWP_SKIPTRANSPARENT = 0x1, 0x4
GWL_EXSTYLE = -20
PW_RENDERFULLCONTENT = 2
DWMWA_EXTENDED_FRAME_BOUNDS = 9


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [("cb", W.DWORD), ("lpReserved", W.LPWSTR), ("lpDesktop", W.LPWSTR), ("lpTitle", W.LPWSTR),
                ("dwX", W.DWORD), ("dwY", W.DWORD), ("dwXSize", W.DWORD), ("dwYSize", W.DWORD),
                ("dwXCountChars", W.DWORD), ("dwYCountChars", W.DWORD), ("dwFillAttribute", W.DWORD),
                ("dwFlags", W.DWORD), ("wShowWindow", W.WORD), ("cbReserved2", W.WORD),
                ("lpReserved2", ctypes.c_void_p), ("hStdInput", W.HANDLE), ("hStdOutput", W.HANDLE),
                ("hStdError", W.HANDLE)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", W.HANDLE), ("hThread", W.HANDLE), ("dwProcessId", W.DWORD), ("dwThreadId", W.DWORD)]


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cbSize", W.DWORD), ("flags", W.DWORD), ("hwndActive", W.HWND), ("hwndFocus", W.HWND),
                ("hwndCapture", W.HWND), ("hwndMenuOwner", W.HWND), ("hwndMoveSize", W.HWND),
                ("hwndCaret", W.HWND), ("rcCaret", W.RECT)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", W.DWORD), ("biWidth", W.LONG), ("biHeight", W.LONG), ("biPlanes", W.WORD),
                ("biBitCount", W.WORD), ("biCompression", W.DWORD), ("biSizeImage", W.DWORD),
                ("biXPelsPerMeter", W.LONG), ("biYPelsPerMeter", W.LONG), ("biClrUsed", W.DWORD),
                ("biClrImportant", W.DWORD)]


WNDENUMPROC = ctypes.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)


def _sig(dll, name, restype, *argtypes):
    f = getattr(dll, name)
    f.restype = restype
    f.argtypes = list(argtypes)
    return f


CreateDesktopW = _sig(user32, "CreateDesktopW", W.HANDLE, W.LPCWSTR, W.LPCWSTR, ctypes.c_void_p, W.DWORD, W.DWORD, ctypes.c_void_p)
OpenDesktopW = _sig(user32, "OpenDesktopW", W.HANDLE, W.LPCWSTR, W.DWORD, W.BOOL, W.DWORD)
SetThreadDesktop = _sig(user32, "SetThreadDesktop", W.BOOL, W.HANDLE)
EnumDesktopWindows = _sig(user32, "EnumDesktopWindows", W.BOOL, W.HANDLE, WNDENUMPROC, W.LPARAM)
IsWindow = _sig(user32, "IsWindow", W.BOOL, W.HWND)
IsWindowVisible = _sig(user32, "IsWindowVisible", W.BOOL, W.HWND)
IsZoomed = _sig(user32, "IsZoomed", W.BOOL, W.HWND)
IsIconic = _sig(user32, "IsIconic", W.BOOL, W.HWND)
GetClassNameW = _sig(user32, "GetClassNameW", ctypes.c_int, W.HWND, W.LPWSTR, ctypes.c_int)
GetWindowTextW = _sig(user32, "GetWindowTextW", ctypes.c_int, W.HWND, W.LPWSTR, ctypes.c_int)
GetWindowThreadProcessId = _sig(user32, "GetWindowThreadProcessId", W.DWORD, W.HWND, ctypes.POINTER(W.DWORD))
GetWindowRect = _sig(user32, "GetWindowRect", W.BOOL, W.HWND, ctypes.POINTER(W.RECT))
GetWindowLongW = _sig(user32, "GetWindowLongW", W.LONG, W.HWND, ctypes.c_int)
ScreenToClient = _sig(user32, "ScreenToClient", W.BOOL, W.HWND, ctypes.POINTER(W.POINT))
ChildWindowFromPointEx = _sig(user32, "ChildWindowFromPointEx", W.HWND, W.HWND, W.POINT, W.UINT)
MenuItemFromPoint = _sig(user32, "MenuItemFromPoint", ctypes.c_int, W.HWND, W.HMENU, W.POINT)
PostMessageW = _sig(user32, "PostMessageW", W.BOOL, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
SendMessageTimeoutW = _sig(user32, "SendMessageTimeoutW", LRESULT, W.HWND, W.UINT, W.WPARAM, W.LPARAM, W.UINT, W.UINT, ctypes.POINTER(ctypes.c_size_t))
ShowWindow = _sig(user32, "ShowWindow", W.BOOL, W.HWND, ctypes.c_int)
SetWindowPos = _sig(user32, "SetWindowPos", W.BOOL, W.HWND, W.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, W.UINT)
SetActiveWindow = _sig(user32, "SetActiveWindow", W.HWND, W.HWND)
SetFocus = _sig(user32, "SetFocus", W.HWND, W.HWND)
SetForegroundWindow = _sig(user32, "SetForegroundWindow", W.BOOL, W.HWND)
AttachThreadInput = _sig(user32, "AttachThreadInput", W.BOOL, W.DWORD, W.DWORD, W.BOOL)
GetGUIThreadInfo = _sig(user32, "GetGUIThreadInfo", W.BOOL, W.DWORD, ctypes.POINTER(GUITHREADINFO))
GetKeyboardState = _sig(user32, "GetKeyboardState", W.BOOL, ctypes.POINTER(ctypes.c_ubyte))
SetKeyboardState = _sig(user32, "SetKeyboardState", W.BOOL, ctypes.POINTER(ctypes.c_ubyte))
VkKeyScanW = _sig(user32, "VkKeyScanW", ctypes.c_short, W.WCHAR)
MapVirtualKeyW = _sig(user32, "MapVirtualKeyW", W.UINT, W.UINT, W.UINT)
GetSystemMetrics = _sig(user32, "GetSystemMetrics", ctypes.c_int, ctypes.c_int)
GetWindowDC = _sig(user32, "GetWindowDC", W.HDC, W.HWND)
ReleaseDC = _sig(user32, "ReleaseDC", ctypes.c_int, W.HWND, W.HDC)
PrintWindow = _sig(user32, "PrintWindow", W.BOOL, W.HWND, W.HDC, W.UINT)
GetCurrentThreadId = _sig(kernel32, "GetCurrentThreadId", W.DWORD)
CreateProcessW = _sig(kernel32, "CreateProcessW", W.BOOL, W.LPCWSTR, W.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, W.BOOL, W.DWORD, ctypes.c_void_p, W.LPCWSTR, ctypes.POINTER(STARTUPINFOW), ctypes.POINTER(PROCESS_INFORMATION))
CloseHandle = _sig(kernel32, "CloseHandle", W.BOOL, W.HANDLE)
GetExitCodeProcess = _sig(kernel32, "GetExitCodeProcess", W.BOOL, W.HANDLE, ctypes.POINTER(W.DWORD))
CreateCompatibleDC = _sig(gdi32, "CreateCompatibleDC", W.HDC, W.HDC)
CreateCompatibleBitmap = _sig(gdi32, "CreateCompatibleBitmap", W.HBITMAP, W.HDC, ctypes.c_int, ctypes.c_int)
SelectObject = _sig(gdi32, "SelectObject", W.HGDIOBJ, W.HDC, W.HGDIOBJ)
DeleteObject = _sig(gdi32, "DeleteObject", W.BOOL, W.HGDIOBJ)
DeleteDC = _sig(gdi32, "DeleteDC", W.BOOL, W.HDC)
GetDIBits = _sig(gdi32, "GetDIBits", ctypes.c_int, W.HDC, W.HBITMAP, W.UINT, W.UINT, ctypes.c_void_p, ctypes.POINTER(BITMAPINFOHEADER), W.UINT)
DwmGetWindowAttribute = _sig(dwmapi, "DwmGetWindowAttribute", ctypes.c_long, W.HWND, W.DWORD, ctypes.c_void_p, W.DWORD)


def set_dpi_aware():
    try:
        shcore.SetProcessDpiAwareness(2)  # per-monitor
    except OSError:
        user32.SetProcessDPIAware()


def class_name(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    GetClassNameW(hwnd, buf, 256)
    return buf.value


def window_text(hwnd):
    buf = ctypes.create_unicode_buffer(512)
    GetWindowTextW(hwnd, buf, 512)
    return buf.value


def window_pid_tid(hwnd):
    pid = W.DWORD()
    tid = GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value, tid


def window_rect(hwnd):
    r = W.RECT()
    GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def frame_rect(hwnd):
    """Visible bounds (excludes the invisible resize borders Windows 10/11 adds)."""
    r = W.RECT()
    if DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(r), ctypes.sizeof(r)) == 0 \
            and r.right > r.left:
        return r.left, r.top, r.right, r.bottom
    return window_rect(hwnd)


def make_lparam(lo, hi):
    return ((hi & 0xFFFF) << 16) | (lo & 0xFFFF)


HTCLIENT, HTCAPTION, HTMINBUTTON, HTMAXBUTTON, HTCLOSE = 1, 2, 8, 9, 20
WM_SYSCOMMAND, SC_MINIMIZE, SC_MAXIMIZE, SC_CLOSE, SC_RESTORE = 0x0112, 0xF020, 0xF030, 0xF060, 0xF120
SWP_NOZORDER = 0x4


MN_GETHMENU, MN_SELECTITEM = 0x01E1, 0x01E5     # popup menu window (#32768) messages


def send(hwnd, msg, wparam=0, lparam=0, timeout=500):
    """SendMessage that gives up on a hung window; returns the result (0 on timeout)."""
    out = ctypes.c_size_t()
    SendMessageTimeoutW(hwnd, msg, wparam, lparam, 0x0002, timeout, ctypes.byref(out))   # SMTO_ABORTIFHUNG
    return out.value


def SendMessageTimeoutW_hit(hwnd, x, y):
    """WM_NCHITTEST for a screen point (0 if the window doesn't answer within 200 ms)."""
    out = ctypes.c_size_t()
    lp = ((y & 0xFFFF) << 16) | (x & 0xFFFF)
    if not SendMessageTimeoutW(hwnd, 0x0084, 0, lp, 0x0002, 200, ctypes.byref(out)):  # SMTO_ABORTIFHUNG
        return 0
    return ctypes.c_long(out.value & 0xFFFFFFFF).value
