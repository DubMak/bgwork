"""bgwork host: owns the hidden desktop, shows the live viewer, serves the local control API.

The API listens on 127.0.0.1 only and requires a random bearer token. Port and token are
written to %LOCALAPPDATA%\\bgwork\\host.json so the MCP server can find them.
"""
import base64
import io
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .desktop import HiddenDesktop
from .forms import FormError, FormFiller, summarize
from . import reports
from .win32 import window_rect as w_rect

STATE_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "bgwork")
HOST_FILE = os.path.join(STATE_DIR, "host.json")
CONFIG_FILE = os.path.join(STATE_DIR, "config.json")

# Ops that drive the app. Blocked while the user has paused Claude in the viewer.
INPUT_OPS = {"click", "move", "drag", "mouse_down", "mouse_up", "scroll", "type", "key",
             "launch", "focus", "maximize", "stop", "set_text", "fill", "report"}


class Paused(Exception):
    pass


def load_config():
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


class Host:
    def __init__(self, desk: HiddenDesktop):
        self.desk = desk
        self.paused = False
        self.activity = ""            # last Claude action, shown in the viewer
        self.activity_at = 0.0
        self.claude_mouse = None      # (x, y, time) of Claude's last pointer position
        self.config = load_config()

    def _note(self, text, x=None, y=None):
        self.activity, self.activity_at = text, time.time()
        if x is not None:
            self.claude_mouse = (x, y, time.time())

    def launch(self, exe, args="", maximize=True):
        pid = self.desk.launch(exe, args, maximize=maximize)
        self.config.update(last_exe=exe, last_args=args)
        save_config(self.config)
        return pid

    def _java_point(self, p):
        d = self.desk
        if "x" in p and "y" in p:
            return int(p["x"]), int(p["y"])
        if d.active and d.java.is_java(d.active):       # default: the window input goes to
            l, t, r, b = w_rect(d.active)
            return (l + r) // 2, (t + b) // 2
        return d.width // 2, d.height // 2

    def handle(self, op, p):
        if op in INPUT_OPS and self.paused:
            raise Paused()
        d = self.desk
        if op == "status":
            return {"width": d.width, "height": d.height, "paused": self.paused,
                    "processes": d.running(), "windows": [x.to_dict() for x in d.windows()],
                    "last_exe": self.config.get("last_exe"), "last_args": self.config.get("last_args", "")}
        if op == "screenshot":
            buf = io.BytesIO()
            d.screenshot().save(buf, "PNG")
            return {"png": base64.b64encode(buf.getvalue()).decode(), "width": d.width, "height": d.height}
        if op == "windows":
            return {"windows": [x.to_dict() for x in d.windows()]}
        if op == "launch":
            exe = p.get("exe") or self.config.get("last_exe")
            if not exe:
                raise ValueError("no exe given and no previously launched app to reuse")
            args = p.get("args") if p.get("exe") else self.config.get("last_args", "")
            self._note(f"launch {os.path.basename(exe)}")
            return {"pid": self.launch(exe, args or "", p.get("maximize", True))}
        if op == "click":
            x, y = int(p["x"]), int(p["y"])
            self._note(f"{p.get('button', 'left')} click ×{p.get('clicks', 1)} at {x},{y}", x, y)
            what = d.click(x, y, p.get("button", "left"), int(p.get("clicks", 1)), p.get("modifiers", ""))
            return {"ok": True, "java": what}
        elif op == "report":
            x, y = int(p["x"]), int(p["y"])
            self._note(f"report button at {x},{y}", x, y)
            r = reports.grab(d, x, y, os.path.join(STATE_DIR, "reports"), float(p.get("timeout", 60)))
            r["text"] = reports.rows_text(r.pop("rows")) if r.get("rows") else None
            return r
        elif op in ("form", "fill"):
            top = d.java_window_at(*self._java_point(p))
            if not top:
                raise ValueError("no Java window there (forms currently support Java apps)")
            if op == "form":
                return {"text": summarize(d.java.form(top))} if p.get("text") else d.java.form(top)
            self._note("fill form")
            filler = FormFiller(d, top)
            try:
                r = filler.fill(p.get("fields") or {}, p.get("lines") or [])
            except FormError as e:
                return {"error": str(e), "done": filler.log, "path": filler.path, "timing": filler.timing, "form": summarize(d.java.form(top))}
            return {"done": filler.log, **r}
        elif op in ("ui_tree", "set_text"):
            x, y = int(p.get("x", d.width // 2)), int(p.get("y", d.height // 2))
            top = d.java_window_at(x, y)
            if not top:
                raise ValueError("no Java window at that point (ui_tree/set_text currently support Java apps)")
            if op == "ui_tree":
                return {"tree": d.java.tree(top, int(p.get("max_nodes", 1500))), "focused": d.java.focused(top)}
            self._note(f"set text {p['text'][:40]!r}", x, y)
            return {"result": d.java.set_text(top, x, y, p["text"])}
        elif op == "move":
            x, y = int(p["x"]), int(p["y"])
            self._note(f"move to {x},{y}", x, y)
            d.mouse_move(x, y)
        elif op == "mouse_down":
            x, y = int(p["x"]), int(p["y"])
            self._note(f"mouse down {x},{y}", x, y)
            d.mouse_move(x, y)
            d.mouse_down(x, y, p.get("button", "left"))
        elif op == "mouse_up":
            x, y = int(p["x"]), int(p["y"])
            self._note(f"mouse up {x},{y}", x, y)
            d.mouse_move(x, y)
            d.mouse_up(x, y, p.get("button", "left"))
        elif op == "drag":
            x1, y1, x2, y2 = (int(p[k]) for k in ("x1", "y1", "x2", "y2"))
            self._note(f"drag {x1},{y1} → {x2},{y2}", x2, y2)
            d.drag(x1, y1, x2, y2, p.get("button", "left"))
        elif op == "scroll":
            x, y = int(p["x"]), int(p["y"])
            self._note(f"scroll at {x},{y}", x, y)
            d.scroll(x, y, int(p.get("dy", 0)), int(p.get("dx", 0)))
        elif op == "type":
            text = p["text"]
            self._note(f"type {text[:40]!r}{'…' if len(text) > 40 else ''}")
            d.type_text(text)
        elif op == "key":
            self._note(f"key {p['combo']}")
            d.press(p["combo"])
        elif op == "focus":
            d.focus_window(int(p["hwnd"], 16) if isinstance(p["hwnd"], str) else int(p["hwnd"]))
        elif op == "maximize":
            return {"maximized": d.maximize()}
        elif op == "stop":
            self._note("stop app")
            return {"killed": d.stop_all()}
        else:
            raise ValueError(f"unknown op {op!r}")
        return {"ok": True}

    # ------------------------------------------------------------------ http
    def serve(self):
        token = secrets.token_urlsafe(24)
        host = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                if self.headers.get("Authorization") != f"Bearer {token}":
                    return self._send(401, {"error": "bad token"})
                if not self.path.startswith("/op/"):
                    return self._send(404, {"error": "not found"})
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    params = json.loads(self.rfile.read(n) or b"{}")
                    self._send(200, host.handle(self.path[4:], params))
                except Paused:
                    self._send(423, {"error": "The user has paused Claude in the bgwork viewer "
                                              "(they are using the app). Wait, then try again."})
                except Exception as e:  # noqa: BLE001 - report to client
                    self._send(400, {"error": f"{type(e).__name__}: {e}"})

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(HOST_FILE, "w", encoding="utf-8") as f:
            json.dump({"port": srv.server_address[1], "token": token, "pid": os.getpid()}, f)
        threading.Thread(target=srv.serve_forever, name="bgwork-api", daemon=True).start()
        return srv


def clear_host_file():
    try:
        with open(HOST_FILE, encoding="utf-8") as f:
            if json.load(f).get("pid") == os.getpid():
                os.remove(HOST_FILE)
    except (OSError, ValueError):
        pass
