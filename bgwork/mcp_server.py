"""MCP server (stdio) that lets Claude drive the app on bgwork's hidden desktop.

It's a thin client of the bgwork host. If the host isn't running it starts one, so
Claude can bring everything up by itself. Screenshots are downscaled to fit
1280x800; every coordinate Claude sends is in screenshot pixels and gets scaled back.
"""
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from PIL import Image as PILImage
import io

from .host import HOST_FILE

MAX_W, MAX_H = 1280, 800
SETTLE = 0.4   # seconds to let the UI react before the follow-up screenshot

INSTRUCTIONS = """\
bgwork runs a Windows desktop app on a hidden desktop. The user's own screen, mouse and
keyboard are never touched, so work freely without asking them to stay away from the computer.
The user can watch (and also use the app) through the bgwork viewer window.

Workflow: call `screenshot` first. Coordinates for every tool are pixels in the most recent
screenshot. Action tools return a fresh screenshot after the UI settles; check it before your
next step. If a tool says the user paused Claude, they're using the app, so wait and retry later.
If no app is running, use `launch_app` (omit exe_path to relaunch the last app).

Java (Swing/AWT) apps: `read_form` gives every field and grid value as text, and `fill_form`
enters a whole form in one call with real typing. Prefer them over clicking field by field.
"""

mcp = MCPServer("bgwork", instructions=INSTRUCTIONS)


class HostError(ToolError):   # ToolError messages reach Claude verbatim
    pass


class HostClient:
    def __init__(self):
        self.info = None
        self.scale = None   # desktop pixels per screenshot pixel

    def _load(self):
        try:
            with open(HOST_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def _start_host(self):
        exe = sys.executable
        pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
        flags = 0x00000008 | 0x00000200   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        subprocess.Popen([pyw if os.path.exists(pyw) else exe, "-m", "bgwork", "host"],
                         cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         creationflags=flags, close_fds=True)
        end = time.time() + 15
        while time.time() < end:
            info = self._load()
            if info and self._ping(info):
                return info
            time.sleep(0.3)
        raise HostError("Could not start the bgwork host. Start it manually: python -m bgwork host")

    def _ping(self, info):
        try:
            self._post(info, "status", {}, timeout=3)
            return True
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _post(info, op, params, timeout=90):
        req = urllib.request.Request(
            f"http://127.0.0.1:{info['port']}/op/{op}", data=json.dumps(params).encode(), method="POST",
            headers={"Authorization": f"Bearer {info['token']}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read()).get("error", str(e))
            except ValueError:
                msg = str(e)
            raise HostError(msg) from None

    def call(self, op, **params):
        if self.info is None:
            self.info = self._load()
        try:
            if self.info is None:
                raise urllib.error.URLError("no host")
            return self._post(self.info, op, params)
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            self.info = self._start_host()
            return self._post(self.info, op, params)

    # coordinate mapping -------------------------------------------------
    def ensure_scale(self):
        if self.scale is None:
            st = self.call("status")
            self.scale = max(st["width"] / MAX_W, st["height"] / MAX_H, 1.0)
        return self.scale

    def to_desktop(self, x, y):
        s = self.ensure_scale()
        return round(x * s), round(y * s)

    def screenshot(self):
        r = self.call("screenshot")
        img = PILImage.open(io.BytesIO(base64.b64decode(r["png"])))
        self.scale = max(img.width / MAX_W, img.height / MAX_H, 1.0)
        if self.scale > 1.0:
            img = img.resize((round(img.width / self.scale), round(img.height / self.scale)), PILImage.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return Image(data=buf.getvalue(), format="png")


host = HostClient()


def after(note):
    time.sleep(SETTLE)
    return [note, host.screenshot()]


@mcp.tool()
def screenshot():
    """Capture the hidden desktop where the app runs. Coordinates in other tools refer to this image."""
    return host.screenshot()


@mcp.tool()
def click(x: int, y: int, button: str = "left", double: bool = False, modifiers: str = ""):
    """Click at (x, y) in screenshot pixels.

    button: left | right | middle. double: double-click. modifiers: e.g. "ctrl", "shift", "ctrl+shift".
    """
    dx, dy = host.to_desktop(x, y)
    r = host.call("click", x=dx, y=dy, button=button, clicks=2 if double else 1, modifiers=modifiers)
    note = f"{'double-' if double else ''}{button} click at ({x}, {y})"
    if r.get("java"):
        note += f" — Java component: {r['java']}"
    return after(note)


@mcp.tool()
def ui_tree(x: int = -1, y: int = -1, max_nodes: int = 1500) -> str:
    """List the components of the Java app window at (x, y) (default: screen center): role, name,
    center point (desktop pixels, divide by the screenshot scale) and state. Use it to find fields
    and buttons by name in Java apps."""
    p = {"max_nodes": max_nodes}
    if x >= 0 and y >= 0:
        p.update(zip(("x", "y"), host.to_desktop(x, y)))
    r = host.call("ui_tree", **p)
    return f"screenshot scale: desktop px = screenshot px × {host.ensure_scale():.3f}\n" \
           f"focused: {r['focused']}\n{r['tree']}"


@mcp.tool()
def set_text(x: int, y: int, text: str):
    """Replace the contents of the Java text field at (x, y) directly (no typing). Some apps only run
    their lookups/validation on real keystrokes, so prefer click + type_text when that matters."""
    dx, dy = host.to_desktop(x, y)
    r = host.call("set_text", x=dx, y=dy, text=text)
    return after(r["result"])


@mcp.tool()
def read_form() -> str:
    """Read every named field, looked-up value ("other text", e.g. a customer's name) and grid row
    of the active Java window as text, e.g. `customer_id: '1042'` and
    `line 1: A-100 | 2 | 19.99 | ...`. Much faster and more exact than reading a screenshot.
    Field names here are what fill_form takes."""
    return host.call("form", text=True)["text"]


@mcp.tool()
def fill_form(fields: dict[str, str] | None = None, lines: list[dict[str, str]] | None = None,
              screenshot: bool = True):
    """Fill the active Java form in one call: clicks each field by name and types the value
    followed by Tab, so the app runs its own lookups and validation as if a person typed it.
    Then reads everything back and lists any value that didn't stick. Never saves.

    fields: {field name: value}, filled in the order given (some fields unlock only after an
        earlier one, e.g. an order number after the customer). Names come from read_form; the short
        name after the last ":" is enough.
    lines: grid rows to add, each {column header: value}, e.g.
        [{"Item": "A-100", "Qty": "2", "Price": "19.99"}]. They go into the first empty
        rows; columns not given keep the app's defaults.
    screenshot: False returns text only (cheaper). The read-back text already covers every
        field, grid line and looked-up value such as a customer's name. A stop always includes one.

    Stops at the first popup or rejected value and says where. Check the result before saving.
    """
    r = host.call("fill", fields=fields or {}, lines=lines or [])
    if r.get("error"):
        head = f"STOPPED: {r['error']}\nentered before stopping: {r['done']}"
    elif r["problems"]:
        head = f"filled in {r['seconds']}s, but these don't match:\n" + "\n".join(r["problems"])
    else:
        head = f"filled in {r['seconds']}s; every value reads back as entered"
    text = f"{head}\n\nform now:\n{r['form']}"
    return [text, host.screenshot()] if screenshot or r.get("error") else text


@mcp.tool()
def grab_report(x: int, y: int, timeout: float = 60.0) -> str:
    r"""Click a button that opens a report in a viewer (e.g. an Export to Excel or Print Preview button)
    and get the report file instead: waits for the file, closes the viewer (Excel, PDF reader)
    if it opened on the hidden desktop, and copies the file to %LOCALAPPDATA%\bgwork\reports.
    Spreadsheets come back as tab-separated rows (empty columns dropped); for a PDF, Read the file."""
    dx, dy = host.to_desktop(x, y)
    r = host.call("report", x=dx, y=dy, timeout=timeout)
    closed = ", ".join(r["closed_viewers"]) or "none on the hidden desktop"
    head = f"report file: {r['file']}\n(from {r['source']}; viewer closed: {closed})"
    return f"{head}\n\n{r['text']}" if r.get("text") else head


@mcp.tool()
def move_mouse(x: int, y: int):
    """Move the (virtual) pointer to (x, y), e.g. to reveal hover menus or tooltips."""
    host.call("move", **dict(zip(("x", "y"), host.to_desktop(x, y))))
    return after(f"moved to ({x}, {y})")


@mcp.tool()
def drag(start_x: int, start_y: int, end_x: int, end_y: int, button: str = "left"):
    """Press at the start point, move to the end point, release. For sliders, selections, drag-and-drop."""
    x1, y1 = host.to_desktop(start_x, start_y)
    x2, y2 = host.to_desktop(end_x, end_y)
    host.call("drag", x1=x1, y1=y1, x2=x2, y2=y2, button=button)
    return after(f"dragged ({start_x}, {start_y}) → ({end_x}, {end_y})")


@mcp.tool()
def scroll(x: int, y: int, direction: str = "down", amount: int = 3):
    """Scroll the mouse wheel over (x, y). direction: up | down | left | right. amount: wheel notches."""
    dx, dy = host.to_desktop(x, y)
    n = max(1, int(amount))
    v = {"up": (n, 0), "down": (-n, 0), "left": (0, -n), "right": (0, n)}.get(direction.lower())
    if v is None:
        raise ToolError("direction must be up, down, left or right")
    host.call("scroll", x=dx, y=dy, dy=v[0], dx=v[1])
    return after(f"scrolled {direction} {n} at ({x}, {y})")


@mcp.tool()
def type_text(text: str):
    """Type text into the focused control. Newlines press Enter, tabs press Tab. Click the field first."""
    host.call("type", text=text)
    return after(f"typed {len(text)} chars")


@mcp.tool()
def press_key(keys: str):
    """Press a key or shortcut, e.g. "enter", "escape", "tab", "ctrl+s", "ctrl+shift+p", "alt+f4", "page_down"."""
    host.call("key", combo=keys)
    return after(f"pressed {keys}")


@mcp.tool()
def wait(seconds: float = 1.0):
    """Wait for the app (loading, animations), then return a screenshot."""
    time.sleep(max(0.0, min(float(seconds), 30.0)))
    return host.screenshot()


@mcp.tool()
def launch_app(exe_path: str = "", args: str = ""):
    """Start a program on the hidden desktop (maximized). Omit exe_path to relaunch the last app with its last args."""
    r = host.call("launch", exe=exe_path, args=args)
    time.sleep(1.5)
    return [f"launched pid {r['pid']}", host.screenshot()]


@mcp.tool()
def app_status() -> str:
    """Running processes and visible windows on the hidden desktop, plus whether the user paused Claude."""
    st = host.call("status")
    return json.dumps({k: st[k] for k in ("paused", "processes", "windows", "last_exe", "last_args")}, indent=2)


@mcp.tool()
def focus_window(hwnd: str):
    """Bring a window (hwnd from app_status) to the front of the hidden desktop and focus it."""
    host.call("focus", hwnd=hwnd)
    return after(f"focused {hwnd}")


@mcp.tool()
def stop_app() -> str:
    """Close every program running on the hidden desktop."""
    return f"stopped pids {host.call('stop')['killed']}"


def main():
    mcp.run("stdio")
