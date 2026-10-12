"""Live viewer for the hidden desktop. You can watch Claude and use the app yourself.

Your clicks and keys in this window are forwarded to the hidden desktop the same way
Claude's are, so the real mouse and keyboard are never redirected. Minimize it and
Claude carries on; capture pauses while it's minimized.
"""
import os
import queue
import re
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog

from PIL import Image, ImageTk

from .keys import VK_SHIFT, VK_CONTROL, VK_MENU

FPS = 12          # while something is happening
IDLE_FPS = 2      # when nothing has changed for a while
BUSY_SECS = 1.5   # how long after input or a change to stay at FPS
MOD_VKS = {16: VK_SHIFT, 160: VK_SHIFT, 161: VK_SHIFT, 17: VK_CONTROL, 162: VK_CONTROL, 163: VK_CONTROL,
           18: VK_MENU, 164: VK_MENU, 165: VK_MENU}
TK_BUTTONS = {1: "left", 2: "middle", 3: "right"}
ACCENT = "#d97757"


class Viewer:
    def __init__(self, host):
        self.host = host
        self.desk = host.desk
        self.root = tk.Tk()
        self.root.title("bgwork — hidden desktop")
        self.root.configure(bg="#18181c")
        self.root.geometry("1280x780")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        bar = tk.Frame(self.root, bg="#232329", padx=8, pady=6)
        bar.pack(side=tk.TOP, fill=tk.X)
        btn = dict(bg="#33333b", fg="#eee", activebackground="#44444d", activeforeground="#fff",
                   relief=tk.FLAT, padx=10, pady=3, bd=0, highlightthickness=0)
        tk.Button(bar, text="Launch app…", command=self.on_launch, **btn).pack(side=tk.LEFT)
        tk.Button(bar, text="Maximize", command=lambda: self._bg(self.desk.maximize), **btn).pack(side=tk.LEFT, padx=6)
        tk.Button(bar, text="Stop app", command=self.on_stop, **btn).pack(side=tk.LEFT)
        self.pause_var = tk.BooleanVar(value=False)
        tk.Checkbutton(bar, text="Pause Claude (I'm driving)", variable=self.pause_var, command=self.on_pause,
                       bg="#232329", fg="#eee", selectcolor="#33333b", activebackground="#232329",
                       activeforeground="#fff", highlightthickness=0).pack(side=tk.LEFT, padx=12)
        self.status = tk.Label(bar, text="", bg="#232329", fg="#aaa", anchor="e")
        self.status.pack(side=tk.RIGHT, fill=tk.X, expand=True)

        self.canvas = tk.Canvas(self.root, bg="#18181c", highlightthickness=0, cursor="arrow", takefocus=1)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.image_id = self.canvas.create_image(0, 0, anchor="nw")
        self.cursor_id = self.canvas.create_oval(0, 0, 0, 0, outline=ACCENT, width=3, state="hidden")
        # where your pointer lands on the hidden desktop, and what your last click actually hit
        self.you_ids = [self.canvas.create_line(0, 0, 0, 0, fill="#ff00ff", width=2, state="hidden") for _ in range(2)]
        self.hit_id = self.canvas.create_rectangle(0, 0, 0, 0, outline="#ff00ff", width=2, state="hidden")
        self.hit = None          # (left, top, right, bottom in desktop px, shown until)
        self.you = None          # last pointer position in desktop px
        self.photo = None

        # geometry of the drawn frame inside the canvas: offset + scale (desktop px per canvas px)
        self.view = (0, 0, 1.0)
        self.canvas_size = (1280, 740)
        self.frame = None
        self.frame_lock = threading.Lock()
        self.poked = 0.0          # last time you sent input from this window
        self.wake = threading.Event()   # cuts an idle wait short when input arrives
        self.visible = True
        self.mods = set()
        self.inputs = queue.Queue()
        self._last_move = 0.0
        self._move = None        # latest mouse position not yet sent to the desktop

        self.canvas.bind("<Configure>", lambda e: setattr(self, "canvas_size", (e.width, e.height)))
        self.canvas.bind("<Motion>", self.on_motion)
        self.canvas.bind("<Leave>", lambda e: setattr(self, "you", None))
        for n, name in TK_BUTTONS.items():
            self.canvas.bind(f"<ButtonPress-{n}>", lambda e, b=name: self.on_button(e, b, True))
            self.canvas.bind(f"<ButtonRelease-{n}>", lambda e, b=name: self.on_button(e, b, False))
            self.canvas.bind(f"<B{n}-Motion>", self.on_motion)
        self.canvas.bind("<Double-Button-1>", lambda e: self.on_button(e, "left", True, dbl=True))
        self.canvas.bind("<MouseWheel>", self.on_wheel)
        self.canvas.bind("<Shift-MouseWheel>", lambda e: self.on_wheel(e, horizontal=True))
        self.canvas.bind("<KeyPress>", self.on_key)
        self.canvas.bind("<KeyRelease>", self.on_key_release)
        self.canvas.bind("<FocusOut>", lambda e: self.mods.clear())
        self.root.bind("<Unmap>", lambda e: setattr(self, "visible", False) if e.widget is self.root else None)
        self.root.bind("<Map>", lambda e: setattr(self, "visible", True) if e.widget is self.root else None)

        threading.Thread(target=self._capture_loop, name="bgwork-capture", daemon=True).start()
        threading.Thread(target=self._input_loop, name="bgwork-viewer-input", daemon=True).start()
        self.root.after(30, self._tick)

    # ------------------------------------------------------------------ frames
    def _capture_loop(self):
        # Frames are only for this window (Claude takes a screenshot when it asks for one).
        # Capture fast while you or Claude are acting or the screen is changing, slowly otherwise.
        last, changed = None, 0.0
        while True:
            t = time.time()
            if self.visible:
                try:
                    img = self.desk.screenshot()
                    cw, ch = self.canvas_size
                    s = max(img.width / max(cw, 1), img.height / max(ch, 1))
                    size = (max(1, round(img.width / s)), max(1, round(img.height / s)))
                    small = img.resize(size, Image.BILINEAR) if size != img.size else img
                    frame = (small, s, ((cw - size[0]) // 2, (ch - size[1]) // 2))
                    key = (small.tobytes(), frame[1:])
                    if key != last:     # unchanged frames aren't redrawn
                        last, changed = key, t
                        with self.frame_lock:
                            self.frame = frame
                except Exception:  # noqa: BLE001 - keep the viewer alive
                    pass
            busy = t - max(changed, self.poked, self.host.activity_at) < BUSY_SECS
            period = 1 / (FPS if busy else IDLE_FPS) if self.visible else 0.3
            self.wake.wait(max(0.0, period - (time.time() - t)))
            self.wake.clear()

    def _tick(self):
        with self.frame_lock:
            frame, self.frame = self.frame, None
        if frame:
            img, s, (ox, oy) = frame
            self.photo = ImageTk.PhotoImage(img)
            self.canvas.itemconfigure(self.image_id, image=self.photo)
            self.canvas.coords(self.image_id, ox, oy)
            self.view = (ox, oy, s)
        self._draw_claude()
        self.root.after(1000 // (FPS * 2), self._tick)

    def _draw_claude(self):
        h = self.host
        cm = h.claude_mouse
        if cm and time.time() - cm[2] < 4:
            ox, oy, s = self.view
            x, y = ox + cm[0] / s, oy + cm[1] / s
            self.canvas.coords(self.cursor_id, x - 9, y - 9, x + 9, y + 9)
            self.canvas.itemconfigure(self.cursor_id, state="normal")
        else:
            self.canvas.itemconfigure(self.cursor_id, state="hidden")
        ox, oy, s = self.view
        if self.you:
            # drawn from the desktop coordinates we send, so any mismatch with your pointer shows
            x, y = ox + self.you[0] / s, oy + self.you[1] / s
            self.canvas.coords(self.you_ids[0], x - 20, y, x + 21, y)
            self.canvas.coords(self.you_ids[1], x, y - 20, x, y + 21)
        for i in self.you_ids:
            self.canvas.itemconfigure(i, state="normal" if self.you else "hidden")
        if self.hit and time.time() < self.hit[4]:
            l, t, r, b = self.hit[:4]
            self.canvas.coords(self.hit_id, ox + l / s, oy + t / s, ox + r / s, oy + b / s)
            self.canvas.itemconfigure(self.hit_id, state="normal")
        else:
            self.canvas.itemconfigure(self.hit_id, state="hidden")
        procs = [p for p in self.desk.procs]
        recent = h.activity if h.activity and time.time() - h.activity_at < 6 else ""
        parts = ["paused — you're driving" if h.paused else ""]
        if recent:
            parts.append(f"Claude: {recent}")
        if not procs and not recent:
            parts.append("No app launched yet")
        self.status.configure(text="   ".join(p for p in parts if p), fg=ACCENT if recent else "#aaa")

    # ------------------------------------------------------------------ input
    def _bg(self, fn, *args):
        self.poked = time.time()
        self.inputs.put((fn, args))
        self.wake.set()

    def _input_loop(self):
        while True:
            fn, args = self.inputs.get()
            try:
                fn(*args)
            except Exception as e:  # noqa: BLE001
                print("viewer input error:", e)

    def _to_desktop(self, e):
        ox, oy, s = self.view
        return round((e.x - ox) * s), round((e.y - oy) * s)

    def on_motion(self, e):
        now = time.time()
        if now - self._last_move < 0.025:
            return
        self._last_move = now
        self.you = self._to_desktop(e)
        # Only the latest position matters: if the input thread is behind, moves still queued
        # are replaced rather than replayed one by one.
        pending, self._move = self._move, (*self.you, bool(e.state & 0x700))   # held: Button1-3 masks
        if pending is None:
            self._bg(self._flush_move)

    def _flush_move(self):
        move, self._move = self._move, None
        if move:
            self.desk.mouse_move(*move)

    def on_button(self, e, button, down, dbl=False):
        self.canvas.focus_set()
        self._move = None    # the press brings its own position; a stale move must not land after it
        x, y = self._to_desktop(e)
        mods = sorted(self.mods)
        if button == "left" and not mods:
            # Java windows take mouse position from the real cursor (which is over this
            # viewer, hence clicks landing offset), so click them through the Access Bridge
            def java_or_post():
                top = self.desk.bridge_target(x, y)
                if top:
                    if down:
                        self._show_hit(self.desk.java_click(top, x, y, 2 if dbl else 1, right=button == "right"))
                elif down:
                    self.desk.mouse_down(x, y, button, mods, dbl)
                else:
                    self.desk.mouse_up(x, y, button, mods)
            self._bg(java_or_post)
        elif down:
            self._bg(self.desk.mouse_down, x, y, button, mods, dbl)
        else:
            self._bg(self.desk.mouse_up, x, y, button, mods)
        return "break"

    def _show_hit(self, what):
        """Outline the component a Java click landed on (parsed from '... @(cx,cy) WxH ...')."""
        m = re.search(r"@\((-?\d+),(-?\d+)\) (\d+)x(\d+)", what or "")
        if m:
            cx, cy, wd, ht = map(int, m.groups())
            self.hit = (cx - wd / 2, cy - ht / 2, cx + wd / 2, cy + ht / 2, time.time() + 1.5)

    def on_wheel(self, e, horizontal=False):
        x, y = self._to_desktop(e)
        notches = int(e.delta / 120) or (1 if e.delta > 0 else -1)
        if horizontal:
            self._bg(self.desk.scroll, x, y, 0, -notches)
        else:
            self._bg(self.desk.scroll, x, y, notches, 0)
        return "break"

    def on_key(self, e):
        vk = e.keycode
        if vk in MOD_VKS:
            self.mods.add(MOD_VKS[vk])
            return "break"
        mods = sorted(self.mods)
        printable = e.char and len(e.char) == 1 and ord(e.char) >= 32 and ord(e.char) != 127
        if printable and not (VK_CONTROL in self.mods or VK_MENU in self.mods):
            self._bg(self.desk.type_text, e.char)       # layout-correct text via WM_CHAR
        else:
            self._bg(self.desk.press_vks, mods, [vk])    # shortcuts and non-text keys
        return "break"

    def on_key_release(self, e):
        if e.keycode in MOD_VKS:
            self.mods.discard(MOD_VKS[e.keycode])
        return "break"

    # ------------------------------------------------------------------ toolbar
    def on_pause(self):
        self.host.paused = self.pause_var.get()

    def on_launch(self):
        cfg = self.host.config
        exe = filedialog.askopenfilename(title="Program to run on the hidden desktop",
                                         initialfile=cfg.get("last_exe") or "",
                                         filetypes=[("Programs", "*.exe"), ("All files", "*.*")])
        if not exe:
            return
        args = simpledialog.askstring("Arguments", "Command-line arguments (optional):",
                                      initialvalue=cfg.get("last_args", "") if exe == cfg.get("last_exe") else "",
                                      parent=self.root)
        if args is None:
            return

        def go():
            try:
                self.host.launch(os.path.normpath(exe), args)
            except Exception as e:  # noqa: BLE001
                self.root.after(0, lambda: messagebox.showerror("Launch failed", str(e)))
        self._bg(go)

    def on_stop(self):
        if messagebox.askyesno("Stop app", "Close every program running on the hidden desktop?"):
            self._bg(self.desk.stop_all)

    def on_close(self):
        if self.desk.procs or self.desk.windows():
            answer = messagebox.askyesnocancel(
                "Quit bgwork", "Programs are still running on the hidden desktop, where you can't see them.\n\n"
                               "Yes = close them and quit\nNo = quit and leave them running\nCancel = stay")
            if answer is None:
                return
            if answer:
                self.desk.stop_all()
        self.root.destroy()

    def run(self):
        self.root.mainloop()
