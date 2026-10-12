"""HiddenDesktop: run GUI programs on an invisible Win32 desktop and drive them.

Nothing here touches the real mouse, keyboard or screen. Programs are started on a
separate desktop object (like a tiny extra session). Screenshots come from
PrintWindow, and input is delivered as window messages posted to the target windows.

All Win32 work runs on one worker thread that is bound to the hidden desktop
(SetThreadDesktop), so every public method is safe to call from any thread.
"""
import ctypes
import ctypes.wintypes as W
import os
import queue
import struct
import subprocess
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, asdict

from PIL import Image

from . import win32 as w
from .javabridge import JavaBridge
from .keys import parse_combo, vk_info, MODIFIER_VKS, VK_SHIFT, VK_CONTROL, VK_MENU

BACKGROUND = (24, 24, 28)
MENU_CLASS = "#32768"      # Win32 popup menu window
SKIP_CLASSES = {"SysShadow", "Progman", "WorkerW"}
# Posted mouse messages reach Java windows (frames, dialogs, popups) once they carry screen
# coordinates (see _post_mouse). True routes left clicks through the Access Bridge instead.
JAVA_CLICKS_VIA_BRIDGE = False


@dataclass
class Win:
    hwnd: int
    left: int
    top: int
    right: int
    bottom: int
    cls: str
    title: str
    pid: int

    def contains(self, x, y):
        return self.left <= x < self.right and self.top <= y < self.bottom

    def to_dict(self):
        d = asdict(self)
        d["hwnd"] = hex(self.hwnd)
        return d


class HiddenDesktop:
    def __init__(self, name="bgwork"):
        self.name = name
        self.hdesk = w.OpenDesktopW(name, 0, False, w.GENERIC_ALL) or \
            w.CreateDesktopW(name, None, None, 0, w.GENERIC_ALL, None)
        if not self.hdesk:
            raise ctypes.WinError(ctypes.get_last_error())
        self.width = w.GetSystemMetrics(0)
        self.height = w.GetSystemMetrics(1)
        self.procs = []           # list of (pid, hProcess, cmdline)
        self.active = None        # top-level hwnd we consider focused
        self.capture = None       # hwnd that owns the mouse while a button is held
        self.buttons = 0          # MK_* bits currently held
        self._btn_tid = None      # thread whose input we're attached to while a button is held
        self.menu_press = None    # (menu hwnd, item index) while a button is down on a Win32 menu
        self._menu_hover = None   # (menu hwnd, item index) last highlighted by hovering
        self._hover = None        # (hwnd, x, y) of the last buttonless move posted
        self._press_pt = None     # where the button went down, until the mouse moves past the drag threshold
        self.moving = None        # (top-level hwnd, grab dx, grab dy) while dragging a title bar
        self.nc_press = None      # (top-level hwnd, hit code) while pressing a title-bar button
        self.last_text = 0.0      # when WM_CHARs were last posted
        self.mouse = (self.width // 2, self.height // 2)
        self.java = JavaBridge(self)
        self._q = queue.Queue()
        self._thread = self._start_worker("bgwork-desktop", self._q, main=True)
        # Screenshots get a thread of their own so a capture (~50 ms) never holds up input.
        self._cap_q = queue.Queue()
        self._cap_thread = self._start_worker("bgwork-capture", self._cap_q)

    # ---------------------------------------------------------------- plumbing
    def _start_worker(self, name, q, main=False):
        ready = threading.Event()
        t = threading.Thread(target=self._run, args=(q, ready, main), name=name, daemon=True)
        t.start()
        ready.wait()
        return t

    def _run(self, q, ready, main):
        if not w.SetThreadDesktop(self.hdesk):
            raise ctypes.WinError(ctypes.get_last_error())
        if main:
            self._tid = w.GetCurrentThreadId()
        ready.set()
        while True:
            fut, fn, args, kw = q.get()
            if fut.set_running_or_notify_cancel():
                try:
                    fut.set_result(fn(*args, **kw))
                except BaseException as e:  # noqa: BLE001 - surfaced to caller
                    fut.set_exception(e)

    def _call(self, fn, *args, **kw):
        return self._on(self._thread, self._q, fn, *args, **kw)

    def _on(self, thread, q, fn, *args, **kw):
        if threading.current_thread() is thread:
            return fn(*args, **kw)
        fut = Future()
        q.put((fut, fn, args, kw))
        return fut.result(timeout=60)

    # ---------------------------------------------------------------- processes
    def launch(self, exe, args="", cwd=None, maximize=True, wait=15.0):
        """Start a program on the hidden desktop. Returns its pid."""
        exe = os.path.expandvars(os.path.expanduser(exe))
        if not os.path.isfile(exe):
            raise FileNotFoundError(exe)
        if isinstance(args, list):
            cmdline = subprocess.list2cmdline([exe] + args)
        else:   # a command-line string is passed through as typed
            cmdline = subprocess.list2cmdline([exe]) + (" " + args.strip() if args and args.strip() else "")
        si = w.STARTUPINFOW()
        si.cb = ctypes.sizeof(si)
        si.lpDesktop = self.name
        pi = w.PROCESS_INFORMATION()
        buf = ctypes.create_unicode_buffer(cmdline)
        if not w.CreateProcessW(None, buf, None, None, False, 0, None, cwd or os.path.dirname(exe),
                                ctypes.byref(si), ctypes.byref(pi)):
            raise ctypes.WinError(ctypes.get_last_error())
        w.CloseHandle(pi.hThread)
        self.procs.append((pi.dwProcessId, pi.hProcess, cmdline))
        if wait:
            self.wait_for_window(timeout=wait)
            if maximize:
                self.maximize()
        return pi.dwProcessId

    def running(self):
        out = []
        for pid, h, cmd in self.procs:
            code = W.DWORD()
            w.GetExitCodeProcess(h, ctypes.byref(code))
            out.append({"pid": pid, "cmdline": cmd, "running": code.value == 259})
        return out

    def stop_all(self):
        """Kill everything we launched plus anything else owning a window on the hidden desktop.
        Hidden windows count too: an app closed to its background (or launched by an earlier
        host run) keeps its process, and a single-instance app would just come back on relaunch."""
        pids = {pid for pid, _h, _c in self.procs} | self._call(self._desktop_pids)
        pids.discard(os.getpid())
        for pid in pids:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)
        for _pid, h, _c in self.procs:
            w.CloseHandle(h)
        self.procs.clear()
        self.active = None
        return sorted(pids)

    def _desktop_pids(self):
        """Processes owning any top-level window on the hidden desktop, visible or not."""
        pids = set()

        def cb(h, _):
            pids.add(w.window_pid_tid(h)[0])
            return True
        w.EnumDesktopWindows(self.hdesk, w.WNDENUMPROC(cb), 0)
        return pids

    def wait_for_window(self, timeout=15.0):
        end = time.time() + timeout
        while time.time() < end:
            wins = self.windows()
            if wins:
                time.sleep(0.5)   # let it finish first paint
                return wins[0]
            time.sleep(0.25)
        return None

    # ---------------------------------------------------------------- windows
    def windows(self):
        """Visible top-level windows on the hidden desktop, topmost first."""
        return self._call(self._windows)

    def _windows(self):
        out = []

        def cb(h, _):
            if w.IsWindowVisible(h) and not w.IsIconic(h):
                cls = w.class_name(h)
                if cls not in SKIP_CLASSES:
                    l, t, r, b = w.frame_rect(h)
                    if r - l > 1 and b - t > 1:
                        pid, _tid = w.window_pid_tid(h)
                        out.append(Win(h, l, t, r, b, cls, w.window_text(h), pid))
            return True

        w.EnumDesktopWindows(self.hdesk, w.WNDENUMPROC(cb), 0)
        return out

    def minimized_windows(self):
        def find():
            out = []

            def cb(h, _):
                if w.IsWindowVisible(h) and w.IsIconic(h):
                    out.append(h)
                return True
            w.EnumDesktopWindows(self.hdesk, w.WNDENUMPROC(cb), 0)
            return out
        return self._call(find)

    def maximize(self, hwnd=None):
        def do():
            targets = [hwnd] if hwnd else self.minimized_windows()
            if not hwnd:
                wins = self._windows()
                main = max(wins, key=lambda x: (x.right - x.left) * (x.bottom - x.top), default=None)
                if main:
                    targets.append(main.hwnd)
            for h in targets:
                w.ShowWindow(h, w.SW_MAXIMIZE)
            if targets:
                self._activate(targets[-1])
            return [hex(h) for h in targets]
        return self._call(do)

    def focus_window(self, hwnd):
        def do():
            if w.IsIconic(hwnd):
                w.ShowWindow(hwnd, w.SW_RESTORE)
            self._on_screen(hwnd)
            self._activate(hwnd)
        return self._call(do)

    def _on_screen(self, hwnd):
        """Bring a window back inside the hidden desktop if it ended up off it (an app restoring
        itself onto a second-monitor position leaves it where nothing can see it)."""
        l, t, r, b = w.frame_rect(hwnd)
        if 0 <= l < self.width and 0 <= t < self.height:
            return
        zoomed = w.IsZoomed(hwnd)
        if zoomed:
            w.ShowWindow(hwnd, w.SW_RESTORE)
            l, t, r, b = w.frame_rect(hwnd)
        x = min(max(0, l % self.width), max(0, self.width - (r - l)))
        y = min(max(0, t), max(0, self.height - (b - t)))
        w.SetWindowPos(hwnd, 0, x, y, 0, 0, w.SWP_NOSIZE | w.SWP_NOZORDER)
        if zoomed:
            w.ShowWindow(hwnd, w.SW_MAXIMIZE)

    def _activate(self, hwnd):
        """Make hwnd the active/focused window of its (hidden-desktop) thread."""
        _pid, tid = w.window_pid_tid(hwnd)
        already = tid in (getattr(self, "_attached_tid", None), self._btn_tid)   # inside a held key/button section
        if not already:
            w.AttachThreadInput(self._tid, tid, True)
        try:
            w.SetWindowPos(hwnd, w.HWND_TOP, 0, 0, 0, 0, w.SWP_NOMOVE | w.SWP_NOSIZE)
            w.SetForegroundWindow(hwnd)
            w.SetActiveWindow(hwnd)
            w.SetFocus(hwnd)
        finally:
            if not already:
                w.AttachThreadInput(self._tid, tid, False)
        w.PostMessageW(hwnd, w.WM_ACTIVATE, 1, 0)
        w.PostMessageW(hwnd, w.WM_SETFOCUS, 0, 0)
        self.active = hwnd

    # ---------------------------------------------------------------- screenshots
    def screenshot(self):
        """Composite image of the whole hidden desktop (PIL.Image, RGB)."""
        return self._on(self._cap_thread, self._cap_q, self._screenshot)

    def _screenshot(self):
        canvas = Image.new("RGB", (self.width, self.height), BACKGROUND)
        for win in reversed(self._windows()):
            img = self._capture_window(win.hwnd)
            if img is None:
                continue
            wl, wt, _, _ = w.window_rect(win.hwnd)
            crop = img.crop((win.left - wl, win.top - wt, win.right - wl, win.bottom - wt))
            canvas.paste(crop, (win.left, win.top))
        return canvas

    @staticmethod
    def _capture_window(hwnd):
        l, t, r, b = w.window_rect(hwnd)
        width, height = r - l, b - t
        if width <= 0 or height <= 0:
            return None
        hdc = w.GetWindowDC(hwnd)
        mdc = w.CreateCompatibleDC(hdc)
        bmp = w.CreateCompatibleBitmap(hdc, width, height)
        old = w.SelectObject(mdc, bmp)
        try:
            if not w.PrintWindow(hwnd, mdc, w.PW_RENDERFULLCONTENT):
                return None
            bih = w.BITMAPINFOHEADER()
            bih.biSize = ctypes.sizeof(bih)
            bih.biWidth, bih.biHeight = width, -height
            bih.biPlanes, bih.biBitCount = 1, 32
            buf = ctypes.create_string_buffer(width * height * 4)
            w.GetDIBits(mdc, bmp, 0, height, buf, ctypes.byref(bih), 0)
            return Image.frombuffer("RGB", (width, height), buf, "raw", "BGRX", 0, 1)
        finally:
            w.SelectObject(mdc, old)
            w.DeleteObject(bmp)
            w.DeleteDC(mdc)
            w.ReleaseDC(hwnd, hdc)

    # ---------------------------------------------------------------- mouse
    def _hit(self, x, y):
        """(top_level, target) windows under a desktop point."""
        top = next((win.hwnd for win in self._windows() if win.contains(x, y)), None)
        if top is None:
            return None, None
        target = top
        # Chromium/Electron handle input on the top-level window; classic Win32 apps want the child.
        if not w.class_name(top).startswith("Chrome_WidgetWin"):
            while True:
                pt = w.W.POINT(x, y)
                w.ScreenToClient(target, ctypes.byref(pt))
                child = w.ChildWindowFromPointEx(target, pt, w.CWP_SKIPINVISIBLE | w.CWP_SKIPTRANSPARENT)
                if not child or child == target:
                    break
                target = child
        return top, target

    def _modifier_bits(self, mods):
        bits = 0
        if VK_SHIFT in mods:
            bits |= w.MK_SHIFT
        if VK_CONTROL in mods:
            bits |= w.MK_CONTROL
        return bits

    def _post_mouse(self, hwnd, msg, x, y, wparam):
        pt = w.W.POINT(x, y)
        if not w.class_name(hwnd).startswith("SunAwt"):
            # Java takes a posted mouse message's position as screen coordinates, so it gets
            # them as-is (client coordinates only looked right on a window at the screen origin)
            w.ScreenToClient(hwnd, ctypes.byref(pt))
        w.PostMessageW(hwnd, msg, wparam, w.make_lparam(pt.x, pt.y))

    _BUTTONS = {
        "left": (w.WM_LBUTTONDOWN, w.WM_LBUTTONUP, w.WM_LBUTTONDBLCLK, w.MK_LBUTTON),
        "right": (w.WM_RBUTTONDOWN, w.WM_RBUTTONUP, w.WM_RBUTTONDBLCLK, w.MK_RBUTTON),
        "middle": (w.WM_MBUTTONDOWN, w.WM_MBUTTONUP, w.WM_MBUTTONDBLCLK, w.MK_MBUTTON),
    }

    def mouse_move(self, x, y, held=True):
        """held=False marks a hover (no button down at the source), which never drags a window."""
        def do():
            if self.moving and not held:
                return      # e.g. the viewer's mouse hovering while a scripted drag is moving a window
            self.mouse = (x, y)
            if self.moving:
                top, dx, dy = self.moving
                w.SetWindowPos(top, None, x - dx, y - dy, 0, 0, w.SWP_NOSIZE | w.SWP_NOZORDER | w.SWP_NOACTIVATE)
                return
            top, hwnd = self._hit(x, y)
            if top and w.class_name(top) == MENU_CLASS and not self.capture:
                # menus place posted moves at (0,0) (see mouse_down), so highlight the item directly
                idx = self._menu_item(top, x, y)
                if idx >= 0 and self._menu_hover != (top, idx):
                    w.send(top, w.MN_SELECTITEM, idx)
                    self._menu_hover = (top, idx)
                return
            hwnd = self.capture or hwnd
            if not hwnd:
                return
            if self._press_pt:
                # Jitter while a button is held isn't a drag yet (DragDetect works the same way).
                # Passing it on would re-arm mouse-leave tracking, which fires at once here (the
                # real cursor is elsewhere) and makes apps like Sumatra drop the pressed button.
                px, py = self._press_pt
                if abs(x - px) <= w.GetSystemMetrics(68) and abs(y - py) <= w.GetSystemMetrics(69):  # SM_CX/CYDRAG
                    return
                self._press_pt = None
            self._post_mouse(hwnd, w.WM_MOUSEMOVE, x, y, self.buttons)
            self._hover = None if self.buttons else (hwnd, x, y)
        return self._call(do)

    def _menu_item(self, menu, x, y):
        """Index of the item at a screen point in a Win32 popup menu window, or -1."""
        hmenu = w.send(menu, w.MN_GETHMENU)
        return w.MenuItemFromPoint(None, hmenu, w.W.POINT(x, y)) if hmenu else -1

    _CAPTION_BUTTONS = {w.HTMINBUTTON: w.SC_MINIMIZE, w.HTMAXBUTTON: w.SC_MAXIMIZE, w.HTCLOSE: w.SC_CLOSE}

    def mouse_down(self, x, y, button="left", mods=(), dbl=False):
        def do():
            top, hwnd = self._hit(x, y)
            self.mouse = (x, y)
            if not hwnd:
                return False
            if w.class_name(top) == MENU_CLASS:
                # A menu's modal loop places clicks at the message's cursor point, which is stuck
                # at (0,0) on this desktop, so a posted click lands "outside" and just closes the
                # menu. Pick the item under the point through the menu's own messages instead.
                idx = self._menu_item(top, x, y)
                if idx >= 0:
                    w.send(top, w.MN_SELECTITEM, idx)
                self.menu_press = (top, idx)
                return True
            if top != self.active and w.class_name(top) != "SunAwtWindow":   # Java popups never take focus
                self._activate(top)
                if w.class_name(top).startswith("SunAwt"):
                    # Java handles activation asynchronously; a popup opened by this press would
                    # close again when the late focus change lands, so let it settle first.
                    time.sleep(0.15)
            ht = self._nc_hit(top, x, y) if button == "left" and not dbl else None
            if ht == w.HTCAPTION:
                # Windows' own move loop follows the real mouse, so move the window ourselves
                l, t, _r, _b = w.window_rect(top)
                self.moving = (top, x - l, y - t)
                return True
            if ht in self._CAPTION_BUTTONS:
                # Same for the min/max/close buttons' tracking loop; act on release, like Windows
                self.nc_press = (top, ht)
                return True
            down, _up, dblmsg, bit = self._BUTTONS[button]
            self.buttons |= bit
            self.capture = hwnd
            self._sync_button_state(hwnd)
            wp = self.buttons | self._modifier_bits(mods)
            if self._hover != (hwnd, x, y):
                self._post_mouse(hwnd, w.WM_MOUSEMOVE, x, y, wp & ~bit)
                # Let the app handle the hover first: apps that track mouse-leave get WM_MOUSELEAVE
                # straight away here, and it must not land between the press and the release.
                time.sleep(0.05)
            self._hover = None
            self._post_mouse(hwnd, dblmsg if dbl else down, x, y, wp)
            self._press_pt = (x, y)
            return True
        return self._call(do)

    def mouse_up(self, x, y, button="left", mods=()):
        def do():
            if self.moving and button == "left":
                top, dx, dy = self.moving
                w.SetWindowPos(top, None, x - dx, y - dy, 0, 0, w.SWP_NOSIZE | w.SWP_NOZORDER | w.SWP_NOACTIVATE)
                self.moving = None
                self.mouse = (x, y)
                return
            if self.nc_press:
                top, ht = self.nc_press
                self.nc_press = None
                self.mouse = (x, y)
                if button == "left" and w.IsWindow(top) and self._nc_hit(top, x, y) == ht:
                    cmd = self._CAPTION_BUTTONS[ht]
                    if ht == w.HTMAXBUTTON and w.IsZoomed(top):
                        cmd = w.SC_RESTORE
                    w.PostMessageW(top, w.WM_SYSCOMMAND, cmd, 0)
                return
            if self.menu_press:
                menu, idx = self.menu_press
                self.menu_press = None
                self.mouse = (x, y)
                if idx >= 0 and w.IsWindow(menu) and self._menu_item(menu, x, y) == idx:
                    # Enter runs the highlighted item, or opens it if it's a submenu
                    w.PostMessageW(menu, w.WM_KEYDOWN, 0x0D, 0x001C0001)   # VK_RETURN
                    w.PostMessageW(menu, w.WM_KEYUP, 0x0D, 0xC01C0001)
                return
            self._press_pt = None
            hwnd = self.capture or self._hit(x, y)[1]
            _down, up, _dbl, bit = self._BUTTONS[button]
            self.buttons &= ~bit
            self.mouse = (x, y)
            self._sync_button_state(hwnd)
            if hwnd:
                self._post_mouse(hwnd, up, x, y, self.buttons | self._modifier_bits(mods))
            if not self.buttons:
                self.capture = None
        return self._call(do)

    _BUTTON_VKS = ((w.MK_LBUTTON, 0x01), (w.MK_RBUTTON, 0x02), (w.MK_MBUTTON, 0x04))

    def _sync_button_state(self, hwnd):
        """Mirror self.buttons into the key state of hwnd's thread. Java reads held buttons with
        GetKeyState, which posted messages never update, and drops every MOUSE_DRAGGED without them.
        As with modifiers, the shared state only lasts while attached, so the attachment spans
        from the first press to the last release."""
        if self.buttons and not self._btn_tid and hwnd:
            _pid, tid = w.window_pid_tid(hwnd)
            if tid and tid != self._tid and w.AttachThreadInput(self._tid, tid, True):
                self._btn_tid = tid
        if not self._btn_tid:
            return
        state = (ctypes.c_ubyte * 256)()
        w.GetKeyboardState(state)
        for bit, vk in self._BUTTON_VKS:
            state[vk] = 0x80 if self.buttons & bit else 0
        w.SetKeyboardState(state)
        if not self.buttons:
            if self._btn_tid != getattr(self, "_attached_tid", None):
                w.AttachThreadInput(self._tid, self._btn_tid, False)
            self._btn_tid = None

    def java_window_at(self, x, y):
        """Top-level Java window under the point, or None."""
        top = self._call(lambda: self._hit(x, y)[0])
        return top if top and self.java.is_java(top) else None

    def bridge_target(self, x, y):
        """Java window whose click at this point should go through the Access Bridge, or None
        (the default: posted clicks work on Java). Title-bar points are left alone so the window
        can still be dragged."""
        if not JAVA_CLICKS_VIA_BRIDGE:
            return None
        top = self.java_window_at(x, y)
        if not top:
            return None
        return None if self._call(self._nc_hit, top, x, y) != w.HTCLIENT else top

    @staticmethod
    def _nc_hit(hwnd, x, y):
        return w.SendMessageTimeoutW_hit(hwnd, x, y)

    def java_click(self, top, x, y, clicks=1, right=False):
        if top != self.active:
            self._call(self._activate, top)
        what, key = self.java.click(top, x, y, clicks, right)
        if key:
            time.sleep(0.05)      # let the selection land before the key reaches the focused tree
            self.press(key)
        return what

    def click(self, x, y, button="left", clicks=1, modifiers=""):
        """Returns a description of what was clicked for Java windows, else None."""
        mods = parse_combo(modifiers)[0] if modifiers else []
        if button == "left" and not mods:
            top = self.bridge_target(x, y)
            if top:
                return self.java_click(top, x, y, clicks, right=button == "right")
        with self._held_modifiers(mods):
            for i in range(clicks):
                self.mouse_down(x, y, button, mods, dbl=(i % 2 == 1))
                time.sleep(0.03)
                self.mouse_up(x, y, button, mods)
                time.sleep(0.06)

    def drag(self, x1, y1, x2, y2, button="left", steps=12):
        self.mouse_move(x1, y1)
        self.mouse_down(x1, y1, button)
        time.sleep(0.05)
        for i in range(1, steps + 1):
            self.mouse_move(round(x1 + (x2 - x1) * i / steps), round(y1 + (y2 - y1) * i / steps))
            time.sleep(0.02)
        self.mouse_up(x2, y2, button)

    def scroll(self, x, y, dy=0, dx=0):
        """dy/dx in wheel notches: dy>0 scrolls up (away from user), dx>0 scrolls right."""
        def do():
            top, hwnd = self._hit(x, y)
            if not hwnd:
                return
            self._post_mouse(hwnd, w.WM_MOUSEMOVE, x, y, self.buttons)
            target = top if w.class_name(top).startswith("Chrome_WidgetWin") else hwnd
            for msg, n in ((w.WM_MOUSEWHEEL, dy), (w.WM_MOUSEHWHEEL, dx)):
                step = 120 if n > 0 else -120
                for _ in range(abs(int(n))):
                    w.PostMessageW(target, msg, ((step & 0xFFFF) << 16) | self.buttons, w.make_lparam(x, y))
        self._call(do)

    # ---------------------------------------------------------------- keyboard
    def _key_target(self):
        hwnd = self.active if self.active and w.IsWindow(self.active) else None
        if hwnd is None:
            wins = self._windows()
            if not wins:
                return None, None
            hwnd = wins[0].hwnd
            self._activate(hwnd)
        _pid, tid = w.window_pid_tid(hwnd)
        gti = w.GUITHREADINFO()
        gti.cbSize = ctypes.sizeof(gti)
        if w.GetGUIThreadInfo(tid, ctypes.byref(gti)) and gti.hwndFocus:
            if not w.class_name(hwnd).startswith("Chrome_WidgetWin"):
                return gti.hwndFocus, tid
        return hwnd, tid

    def _held_modifiers(self, mods):
        return _HeldModifiers(self, mods)

    def _set_mod_state(self, mods, down):
        """Attach to the target's input queue and flip modifier key state.

        The attachment must stay in place until the target has processed the posted
        keys (shared key state is dropped on detach), so down=True attaches and
        down=False restores the state and detaches.
        """
        if down:
            _hwnd, tid = self._key_target()
            if not tid:
                return
            w.AttachThreadInput(self._tid, tid, True)
            self._attached_tid = tid
        elif not getattr(self, "_attached_tid", None):
            return
        state = (ctypes.c_ubyte * 256)()
        w.GetKeyboardState(state)
        for vk in mods:
            for v in MODIFIER_VKS.get(vk, (vk,)):
                state[v] = 0x80 if down else 0
        w.SetKeyboardState(state)
        if not down:
            if self._attached_tid != self._btn_tid:
                w.AttachThreadInput(self._tid, self._attached_tid, False)
            self._attached_tid = None

    def _post_key(self, hwnd, vk, down, alt_held):
        scan, ext = vk_info(vk)
        lp = 1 | (scan << 16) | (ext << 24)
        if alt_held:
            lp |= 1 << 29
        if not down:
            lp |= (1 << 30) | (1 << 31)
        sys = alt_held or vk == VK_MENU
        msg = (w.WM_SYSKEYDOWN if down else w.WM_SYSKEYUP) if sys else (w.WM_KEYDOWN if down else w.WM_KEYUP)
        w.PostMessageW(hwnd, msg, vk, lp)

    def key_event(self, vk, down, mods=()):
        """Single key down or up with the given modifier VKs considered held (viewer passthrough)."""
        def do():
            hwnd, _tid = self._key_target()
            if hwnd:
                self._post_key(hwnd, vk, down, VK_MENU in mods)
        with self._held_modifiers([m for m in mods if m != vk]):
            self._call(do)

    def press(self, combo):
        """Press a key combo such as 'ctrl+s', 'enter', 'alt+f4', 'ctrl+shift+p'."""
        self.press_vks(*parse_combo(combo))

    def press_vks(self, mods, keys):
        """Press keys (VK codes) with modifier VKs held."""
        mods = list(mods)

        def do():
            hwnd, _tid = self._key_target()
            if not hwnd:
                raise RuntimeError("no window to send keys to")
            alt = VK_MENU in mods
            for m in mods:
                self._post_key(hwnd, m, True, alt and m != VK_MENU)
            for k in keys:
                self._post_key(hwnd, k, True, alt)
                self._post_key(hwnd, k, False, alt)
            for m in reversed(mods):
                self._post_key(hwnd, m, False, alt and m != VK_MENU)
        with self._held_modifiers(mods):
            self._call(do)

    def type_text(self, text):
        """Type literal text (unicode ok). Newlines press Enter, tabs press Tab."""
        def chunk(s):
            hwnd, _tid = self._key_target()
            if not hwnd:
                raise RuntimeError("no window to type into")
            data = s.encode("utf-16-le")
            for unit in struct.unpack(f"<{len(data) // 2}H", data):   # surrogate pairs go as two WM_CHARs
                w.PostMessageW(hwnd, w.WM_CHAR, unit, 1)
            self.last_text = time.monotonic()
        buf = ""
        for ch in text:
            if ch in "\n\t":
                if buf:
                    self._call(chunk, buf)
                    buf = ""
                # TranslateMessage queues the key's WM_CHAR at the back of the target's queue,
                # so let earlier chars drain first and give the Enter/Tab time to land.
                time.sleep(0.08)
                self.press("enter" if ch == "\n" else "tab")
                time.sleep(0.08)
            elif ch != "\r":
                buf += ch
        if buf:
            self._call(chunk, buf)


class _HeldModifiers:
    """Make GetKeyState() in the target thread report modifiers as held.

    Posted WM_KEYDOWN messages don't update the key state, so apps that check
    GetKeyState(VK_CONTROL) (Chromium does) would miss the modifier. Attaching to the
    target thread's input queue lets us set that state directly.
    """

    def __init__(self, desk, mods):
        self.desk, self.mods = desk, list(mods)

    def __enter__(self):
        if self.mods:
            # chars still queued in the target would be read as ctrl+<char> once the
            # modifier state is set, so let recently typed text drain first
            wait = self.desk.last_text + 0.15 - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self.desk._call(self.desk._set_mod_state, self.mods, True)

    def __exit__(self, *exc):
        if self.mods:
            time.sleep(0.12)  # let the target process what we posted before releasing
            self.desk._call(self.desk._set_mod_state, self.mods, False)
