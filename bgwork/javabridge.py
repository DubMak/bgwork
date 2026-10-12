"""Drive Java (Swing/AWT) apps on the hidden desktop through the Java Access Bridge.

Swing decides which component a mouse message belongs to from the *real* cursor
position, so posted clicks miss (or land wherever your own mouse happens to be).
Keyboard focus and component actions don't depend on the cursor, so for Java
windows we find the component under the point with the Access Bridge and act on it
directly: press buttons, toggle checkboxes, focus text fields, select tree nodes and
table cells.

The bridge talks to the JVM with window messages, so it runs on its own thread
bound to the hidden desktop and pumps messages there.
"""
import ctypes
import ctypes.wintypes as W
import glob
import os
import queue
import threading
import time
from concurrent.futures import Future

from . import win32 as w

J64 = ctypes.c_int64      # JOBJECT64 on 64-bit bridges
CLICK_ROLES = {"push button", "check box", "radio button", "toggle button", "menu item", "menu",
               "check box menu item", "radio button menu item", "page tab", "hyperlink"}
TEXT_ROLES = {"text", "password text", "editbar", "spin box"}


class Info(ctypes.Structure):
    _fields_ = [("name", ctypes.c_wchar * 1024), ("description", ctypes.c_wchar * 1024),
                ("role", ctypes.c_wchar * 256), ("role_en_US", ctypes.c_wchar * 256),
                ("states", ctypes.c_wchar * 256), ("states_en_US", ctypes.c_wchar * 256),
                ("indexInParent", ctypes.c_int), ("childrenCount", ctypes.c_int),
                ("x", ctypes.c_int), ("y", ctypes.c_int), ("width", ctypes.c_int), ("height", ctypes.c_int),
                ("accessibleComponent", W.BOOL), ("accessibleAction", W.BOOL),
                ("accessibleSelection", W.BOOL), ("accessibleText", W.BOOL),
                ("accessibleInterfaces", W.BOOL)]


class ActionInfo(ctypes.Structure):
    _fields_ = [("name", ctypes.c_wchar * 256)]


class Actions(ctypes.Structure):
    _fields_ = [("count", ctypes.c_int), ("actions", ActionInfo * 256)]


class ActionsToDo(ctypes.Structure):
    _fields_ = [("count", ctypes.c_int), ("actions", ActionInfo * 32)]


class TextInfo(ctypes.Structure):
    _fields_ = [("charCount", ctypes.c_int), ("caretIndex", ctypes.c_int), ("indexAtPoint", ctypes.c_int)]


class TableInfo(ctypes.Structure):
    _fields_ = [("caption", J64), ("summary", J64), ("rowCount", ctypes.c_int), ("columnCount", ctypes.c_int),
                ("accessibleContext", J64), ("accessibleTable", J64)]


class CellInfo(ctypes.Structure):
    _fields_ = [("accessibleContext", J64), ("index", ctypes.c_int), ("row", ctypes.c_int),
                ("column", ctypes.c_int), ("rowExtent", ctypes.c_int), ("columnExtent", ctypes.c_int),
                ("isSelected", ctypes.c_ubyte)]


class Node:
    """A component snapshot. `ac` stays valid until the bridge releases it."""

    def __init__(self, vm, ac, info, path):
        self.vm, self.ac, self.path = vm, ac, path
        self.name = info.name
        self.desc = info.description
        self.role = info.role_en_US
        self.states = set(s for s in info.states_en_US.split(",") if s)
        self.rect = (info.x, info.y, info.x + info.width, info.y + info.height)
        self.kids = info.childrenCount
        self.has_action = bool(info.accessibleAction)
        self.has_text = bool(info.accessibleText)
        self.has_selection = bool(info.accessibleSelection)
        self.index = info.indexInParent

    def contains(self, x, y):
        l, t, r, b = self.rect
        return l <= x < r and t <= y < b

    @property
    def area(self):
        l, t, r, b = self.rect
        return max(0, r - l) * max(0, b - t)

    def label(self):
        s = self.role
        if self.name:
            s += f" {self.name!r}"
        l, t, r, b = self.rect
        s += f" @({(l + r) // 2},{(t + b) // 2}) {r - l}x{b - t}"
        flags = [f for f in ("focused", "selected", "checked", "expanded", "collapsed") if f in self.states]
        if "enabled" not in self.states:
            flags.append("disabled")
        if "editable" in self.states:
            flags.append("editable")
        return s + (" [" + ",".join(flags) + "]" if flags else "")


def _find_dll(exe_hint=None):
    names = ("WindowsAccessBridge-64.dll",)
    dirs = [os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")]
    if exe_hint:
        d = os.path.dirname(exe_hint)
        for _ in range(3):
            dirs += [os.path.join(d, "jre", "bin"), os.path.join(d, "bin"), os.path.join(d, "runtime", "bin")]
            d = os.path.dirname(d)
    for env in ("JAVA_HOME", "JRE_HOME"):
        if os.environ.get(env):
            dirs.append(os.path.join(os.environ[env], "bin"))
    for pat in (r"C:\Program Files\Java\*\bin", r"C:\Program Files\Eclipse Adoptium\*\bin",
                r"C:\Program Files\Microsoft\jdk*\bin", r"C:\Program Files\Zulu\*\bin"):
        dirs += glob.glob(pat)
    for d in dirs:
        for n in names:
            p = os.path.join(d, n)
            if os.path.isfile(p):
                return p
    return None


def _process_exe(pid):
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    h = k32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        n = W.DWORD(1024)
        return buf.value if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)) else None
    finally:
        k32.CloseHandle(h)


class JavaBridge:
    def __init__(self, desk):
        self.desk = desk
        self.dll = None
        self.error = None
        self._java = {}           # hwnd -> bool cache
        self._form_cache = {}     # hwnd -> (vm, {(kind, key): accessible context}) from the last form walk
        self._q = queue.Queue()
        self._thread = None

    # ------------------------------------------------------------- plumbing
    def _start(self, exe_hint):
        path = _find_dll(exe_hint)
        if not path:
            self.error = "Java Access Bridge DLL (WindowsAccessBridge-64.dll) not found"
            return False
        ready = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(path, ready), name="bgwork-jab", daemon=True)
        self._thread.start()
        ready.wait(10)
        return self.dll is not None

    def _run(self, path, ready):
        try:
            if not w.SetThreadDesktop(self.desk.hdesk):
                raise ctypes.WinError(ctypes.get_last_error())
            dll = ctypes.CDLL(path)
            self._declare(dll)
            dll.Windows_run()
            self._pump(1.0)          # let the bridge find the JVMs on this desktop
            self.dll = dll
        except Exception as e:  # noqa: BLE001
            self.error = f"Java Access Bridge failed to start: {e}"
        finally:
            ready.set()
        msg = W.MSG()
        user32 = ctypes.windll.user32
        while True:
            while user32.PeekMessageW(ctypes.byref(msg), 0, 0, 0, 1):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            try:
                fut, fn, args = self._q.get(timeout=0.01)
            except queue.Empty:
                continue
            if fut.set_running_or_notify_cancel():
                try:
                    fut.set_result(fn(*args))
                except BaseException as e:  # noqa: BLE001
                    fut.set_exception(e)

    @staticmethod
    def _pump(sec):
        msg = W.MSG()
        user32 = ctypes.windll.user32
        end = time.time() + sec
        while time.time() < end:
            while user32.PeekMessageW(ctypes.byref(msg), 0, 0, 0, 1):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            time.sleep(0.01)

    @staticmethod
    def _declare(d):
        L = ctypes.c_long
        d.isJavaWindow.argtypes = [W.HWND]
        d.getAccessibleContextFromHWND.argtypes = [W.HWND, ctypes.POINTER(L), ctypes.POINTER(J64)]
        d.getAccessibleContextInfo.argtypes = [L, J64, ctypes.POINTER(Info)]
        d.getAccessibleChildFromContext.argtypes = [L, J64, ctypes.c_int]
        d.getAccessibleChildFromContext.restype = J64
        d.getAccessibleParentFromContext.argtypes = [L, J64]
        d.getAccessibleParentFromContext.restype = J64
        d.getAccessibleContextWithFocus.argtypes = [W.HWND, ctypes.POINTER(L), ctypes.POINTER(J64)]
        d.getAccessibleActions.argtypes = [L, J64, ctypes.POINTER(Actions)]
        d.doAccessibleActions.argtypes = [L, J64, ctypes.POINTER(ActionsToDo), ctypes.POINTER(ctypes.c_int)]
        d.requestFocus.argtypes = [L, J64]
        d.setTextContents.argtypes = [L, J64, ctypes.c_wchar_p]
        d.releaseJavaObject.argtypes = [L, J64]
        d.addAccessibleSelectionFromContext.argtypes = [L, J64, ctypes.c_int]
        d.clearAccessibleSelectionFromContext.argtypes = [L, J64]
        d.selectTextRange.argtypes = [L, J64, ctypes.c_int, ctypes.c_int]
        d.getAccessibleTextInfo.argtypes = [L, J64, ctypes.POINTER(TextInfo), ctypes.c_int, ctypes.c_int]
        d.getAccessibleTextRange.argtypes = [L, J64, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p, ctypes.c_short]
        d.getAccessibleTableInfo.argtypes = [L, J64, ctypes.POINTER(TableInfo)]
        d.getAccessibleTableColumnHeader.argtypes = [L, J64, ctypes.POINTER(TableInfo)]
        d.getAccessibleTableCellInfo.argtypes = [L, J64, ctypes.c_int, ctypes.c_int, ctypes.POINTER(CellInfo)]

    def _call(self, fn, *args):
        if threading.current_thread() is self._thread:
            return fn(*args)
        fut = Future()
        self._q.put((fut, fn, args))
        return fut.result(timeout=30)

    # ------------------------------------------------------------- detection
    def is_java(self, hwnd):
        """True if hwnd is a Java top-level window (starts the bridge on first sight)."""
        if hwnd in self._java:
            return self._java[hwnd]
        cls = w.class_name(hwnd)
        java = cls.startswith("SunAwt")
        if java and self.dll is None and self._thread is None:
            pid, _tid = w.window_pid_tid(hwnd)
            self._start(_process_exe(pid))
        if java and self.dll is not None:
            java = bool(self._call(self.dll.isJavaWindow, hwnd))
        else:
            java = False
        self._java[hwnd] = java
        return java

    # ------------------------------------------------------------- tree access (bridge thread)
    def _info(self, vm, ac):
        i = Info()
        return i if self.dll.getAccessibleContextInfo(vm, ac, ctypes.byref(i)) else None

    def _root(self, hwnd):
        vm, ac = ctypes.c_long(), J64()
        if not self.dll.getAccessibleContextFromHWND(hwnd, ctypes.byref(vm), ctypes.byref(ac)):
            raise RuntimeError("no accessible context for this Java window")
        return vm.value, ac.value

    def _walk(self, hwnd, visit, max_nodes=4000):
        """Depth-first over showing components; visit(node, depth) returns False to skip children."""
        vm, root = self._root(hwnd)
        held = []
        count = [0]

        def go(ac, depth, path):
            info = self._info(vm, ac)
            if info is None or "showing" not in info.states_en_US:
                return
            count[0] += 1
            node = Node(vm, ac, info, path)
            if visit(node, depth) is False or count[0] >= max_nodes:
                return
            if node.kids > 500:      # huge tables/lists: don't enumerate every cell
                return
            for k in range(node.kids):
                c = self.dll.getAccessibleChildFromContext(vm, ac, k)
                if c:
                    held.append(c)
                    go(c, depth + 1, path + (k,))
        go(root, 0, ())
        return vm, held

    def _release(self, vm, objs, keep=()):
        keep = set(keep)
        for o in objs:
            if o not in keep:
                self.dll.releaseJavaObject(vm, o)

    def _at(self, hwnd, x, y):
        """Deepest, smallest showing component containing (x, y), plus its ancestors."""
        best = []

        def visit(node, depth):
            inside = node.contains(x, y) or depth == 0
            # a page tab's bounds are just its header, but its content is its child; an expanded
            # tree row's bounds are just its own line, but its children sit below it
            through = node.role == "page tab" and "selected" in node.states or "expanded" in node.states
            if not inside and not through:
                return False
            best.append((depth, node, inside))
            return True
        vm, held = self._walk(hwnd, visit)
        hits = [(d, n) for d, n, inside in best if inside]
        if not hits:
            self._release(vm, held)
            return None, []
        # an open popup menu floats above everything else in the window
        popup = next((n for _d, n in hits if n.role == "popup menu"), None)
        if popup is not None:
            hits = [(d, n) for d, n in hits if n.path[:len(popup.path)] == popup.path]
        maxd = max(d for d, _n in hits)
        deepest = min((n for d, n in hits if d == maxd), key=lambda n: n.area)
        chain = [n for _d, n, _i in best if n.path == deepest.path[:len(n.path)]]
        self._release(vm, held, keep=[n.ac for n in chain])
        return deepest, chain

    def _actions(self, node):
        a = Actions()
        if not node.has_action or not self.dll.getAccessibleActions(node.vm, node.ac, ctypes.byref(a)):
            return []
        return [a.actions[i].name for i in range(a.count)]

    def _do(self, node, name):
        todo = ActionsToDo()
        todo.count = 1
        todo.actions[0].name = name
        fail = ctypes.c_int(-1)
        return bool(self.dll.doAccessibleActions(node.vm, node.ac, ctypes.byref(todo), ctypes.byref(fail)))

    def _select_in_parent(self, node, chain):
        """Select node inside its direct parent when that's a tree, list or table. (Selecting in a
        page tab list would switch tabs, so never walk further up.)"""
        if len(chain) < 2:
            return None
        parent = chain[-2]
        if parent.has_selection and parent.role in ("tree", "list", "table"):
            self.dll.clearAccessibleSelectionFromContext(parent.vm, parent.ac)
            self.dll.addAccessibleSelectionFromContext(parent.vm, parent.ac, node.index)
            self.dll.requestFocus(parent.vm, parent.ac)
            return parent
        return None

    def _select(self, parent, node):
        self.dll.clearAccessibleSelectionFromContext(parent.vm, parent.ac)
        self.dll.addAccessibleSelectionFromContext(parent.vm, parent.ac, node.index)

    def _click(self, hwnd, x, y, clicks, right=False):
        """Returns (description, key to press afterwards or None)."""
        node, chain = self._at(hwnd, x, y)
        if node is None:
            return "nothing under the point", None
        key = None
        try:
            acts = self._actions(node)
            done = []
            parent = chain[-2] if len(chain) > 1 else None
            if right:
                # Java pops context menus at the real cursor, so select/focus the target and
                # open its menu from the keyboard instead (Shift+F10)
                if parent is not None and parent.has_selection and parent.role != "page tab list":
                    self._select(parent, node)
                    done.append(f"selected in {parent.role}")
                self.dll.requestFocus(node.vm, node.ac)
                done.append("context menu")
                key = "shift+f10"
            elif node.role == "page tab" and parent is not None and parent.has_selection:
                self._select(parent, node)        # tabs have no action; selecting one switches to it
                done.append("switched tab")
            elif parent is not None and any(n.role == "tree" for n in chain[:-1]):
                tree = next(n for n in reversed(chain) if n.role == "tree")
                if parent.has_selection:          # nested rows select through their parent row
                    self._select(parent, node)
                self.dll.requestFocus(tree.vm, tree.ac)
                done.append("selected in tree")
                branch = "collapsed" in node.states or "expanded" in node.states
                on_handle = node.rect[0] <= x < node.rect[0] + 20     # the +/- box left of the label
                if branch and "toggleexpand" in acts and (on_handle or clicks >= 2):
                    self._do(node, "toggleexpand")
                    done.append("toggled")
                elif clicks >= 2:
                    key = "enter"                 # double-click on a leaf opens it; Enter does the same
                    done.append("enter")
            elif node.role in TEXT_ROLES or node.has_text and "editable" in node.states:
                self.dll.requestFocus(node.vm, node.ac)
                done.append("focused")
                if clicks >= 2:       # double/triple click selects the field's text, like a real click
                    self.dll.selectTextRange(node.vm, node.ac, 0, 1 << 20)
                    done.append("selected text")
            elif node.role in CLICK_ROLES and acts:
                name = "click" if "click" in acts else acts[0]
                self._do(node, name)
                done.append(name)
            else:
                parent = self._select_in_parent(node, chain)
                if parent is not None:
                    done.append(f"selected in {parent.role}")
                elif acts and node.role not in ("panel", "scroll pane", "viewport", "root pane", "layered pane"):
                    self._do(node, acts[0])
                    done.append(acts[0])
                else:
                    self.dll.requestFocus(node.vm, node.ac)
                    done.append("focused")
            shown = "" if not acts or "focused" in done else \
                f" (actions: {', '.join(acts[:8])}{', …' if len(acts) > 8 else ''})"
            return f"{node.label()}: {', '.join(done) or 'no action'}{shown}", key
        finally:
            self._release(node.vm, [n.ac for n in chain])

    def _tree(self, hwnd, max_nodes):
        lines = []
        boring = {"panel", "scroll pane", "viewport", "root pane", "layered pane", "split pane", "filler",
                  "glass pane", "separator", "scroll bar"}

        def visit(node, depth):
            if node.role not in boring or node.name:
                lines.append("  " * min(depth, 12) + node.label())
            return True
        vm, held = self._walk(hwnd, visit, max_nodes)
        self._release(vm, held)
        return "\n".join(lines)

    def _focused(self, hwnd):
        vm, ac = ctypes.c_long(), J64()
        if not self.dll.getAccessibleContextWithFocus(hwnd, ctypes.byref(vm), ctypes.byref(ac)):
            return None
        info = self._info(vm.value, ac.value)
        out = Node(vm.value, ac.value, info, ()).label() if info else None
        self.dll.releaseJavaObject(vm.value, ac.value)
        return out

    def _set_text(self, hwnd, x, y, text):
        node, chain = self._at(hwnd, x, y)
        if node is None:
            return "nothing under the point"
        try:
            if not node.has_text:
                return f"{node.label()} is not a text field"
            self.dll.requestFocus(node.vm, node.ac)
            ok = self.dll.setTextContents(node.vm, node.ac, text)
            return f"{node.label()}: {'text set' if ok else 'setTextContents failed'}"
        finally:
            self._release(node.vm, [n.ac for n in chain])

    def _text(self, vm, ac):
        """Full text of a component with an accessible text interface ("" if none)."""
        ti = TextInfo()
        if not self.dll.getAccessibleTextInfo(vm, ac, ctypes.byref(ti), 0, 0) or ti.charCount <= 0:
            return ""
        n = min(ti.charCount, 4000)
        buf = ctypes.create_unicode_buffer(n + 1)
        if not self.dll.getAccessibleTextRange(vm, ac, 0, n - 1, buf, n + 1):
            return ""
        return buf.value

    def _cell(self, vm, table, r, c):
        """(value, rect) of one table cell; the value is the cell's text, else its accessible name."""
        ci = CellInfo()
        if not self.dll.getAccessibleTableCellInfo(vm, table, r, c, ctypes.byref(ci)) or not ci.accessibleContext:
            return "", None
        try:
            info = self._info(vm, ci.accessibleContext)
            if info is None:
                return "", None
            val = self._text(vm, ci.accessibleContext) if info.accessibleText else ""
            rect = (info.x, info.y, info.x + info.width, info.y + info.height)
            return (val or info.name).strip(), rect
        finally:
            self.dll.releaseJavaObject(vm, ci.accessibleContext)

    def _table(self, node, max_rows):
        vm = node.vm
        ti = TableInfo()
        if not self.dll.getAccessibleTableInfo(vm, node.ac, ctypes.byref(ti)):
            return None
        held = [o for o in (ti.caption, ti.summary, ti.accessibleContext) if o and o != node.ac]
        cols, widths = [], []
        hi = TableInfo()
        if self.dll.getAccessibleTableColumnHeader(vm, node.ac, ctypes.byref(hi)):
            held += [o for o in (hi.caption, hi.summary, hi.accessibleContext) if o]
            heads = [self._cell(vm, hi.accessibleTable, 0, c) for c in range(hi.columnCount)]
            cols = [v for v, _ in heads]
            widths = [rc[2] - rc[0] if rc else 0 for _, rc in heads]
            if hi.accessibleTable:
                held.append(hi.accessibleTable)
        cols += [""] * (ti.columnCount - len(cols))
        rows = []
        for r in range(min(ti.rowCount, max_rows)):
            rows.append([self._cell(vm, ti.accessibleTable, r, c) for c in range(ti.columnCount)])
        if ti.accessibleTable and ti.accessibleTable != node.ac:
            held.append(ti.accessibleTable)
        self._release(vm, set(held))
        # cell bounds don't come through the bridge, but header widths do; cells sit side by side
        return {"columns": cols, "widths": widths, "rows": [[v for v, _ in row] for row in rows],
                "rect": node.rect}

    def _field(self, node):
        l, t, r, b = node.rect
        return {"value": self._text(node.vm, node.ac).strip(), "x": (l + r) // 2, "y": (t + b) // 2,
                "editable": "editable" in node.states, "enabled": "enabled" in node.states,
                "focused": "focused" in node.states}

    def _label(self, node):
        l, t, r, b = node.rect
        text = node.name or (self._text(node.vm, node.ac) if node.has_text else "")
        return {"text": text.strip(), "x": (l + r) // 2, "y": (t + b) // 2}

    def _form(self, hwnd, max_rows=50):
        """Named text fields (value, centre, state) and tables of the window's showing content.
        The navigation tree is skipped. The components found are kept, so later reads of the same
        screen only fetch values (a full walk of a large form takes ~1.5 s, a re-read a fraction of that)."""
        cached = self._form_cache.get(hwnd)
        if cached:
            out = self._reread(cached, max_rows)
            if out is not None:
                return out
            self._drop_form(hwnd)
        fields, tables, labels, keep = {}, [], [], {}

        def visit(node, depth):
            if node.role == "tree":
                return False
            if node.role == "table":
                t = self._table(node, max_rows)
                if t:
                    tables.append(t)
                    keep[("table", len(tables) - 1)] = node.ac
                return False
            if node.has_text and node.name and node.role in TEXT_ROLES:
                fields[node.name] = self._field(node)
                keep[("field", node.name)] = node.ac
                return False
            if node.role == "label" and not node.name:
                # captions have fixed names; a label that's blank now shows looked-up values
                # (a customer's name, a status) once the form is filled
                keep[("label", len(labels))] = node.ac
                labels.append(self._label(node))
                return False
            return True
        vm, held = self._walk(hwnd, visit)
        self._release(vm, held, keep=keep.values())
        self._form_cache[hwnd] = (vm, keep)
        return {"fields": fields, "tables": tables, "labels": labels}

    def _reread(self, cached, max_rows):
        """Values of a cached screen, or None if any of its components went away or got hidden."""
        vm, keep = cached
        fields, tables, labels = {}, [], []
        for (kind, key), ac in keep.items():
            info = self._info(vm, ac)
            if info is None or "showing" not in info.states_en_US:
                return None
            node = Node(vm, ac, info, ())
            if kind == "field":
                fields[key] = self._field(node)
            elif kind == "label":
                labels.append(self._label(node))
            else:
                t = self._table(node, max_rows)
                if t is None:
                    return None
                tables.append(t)
        return {"fields": fields, "tables": tables, "labels": labels}

    def _drop_form(self, hwnd):
        vm, keep = self._form_cache.pop(hwnd, (None, {}))
        self._release(vm, keep.values())

    def _focused_info(self, hwnd):
        """(name, role, text, rect) of the focused component, or None."""
        vm, ac = ctypes.c_long(), J64()
        if not self.dll.getAccessibleContextWithFocus(hwnd, ctypes.byref(vm), ctypes.byref(ac)):
            return None
        try:
            info = self._info(vm.value, ac.value)
            if info is None:
                return None
            text = self._text(vm.value, ac.value) if info.accessibleText else ""
            return info.name, info.role_en_US, text, (info.x, info.y, info.x + info.width, info.y + info.height)
        finally:
            self.dll.releaseJavaObject(vm.value, ac.value)

    def _select_focused(self, hwnd):
        """Select all text in the focused component, so typing replaces it. True if it has text."""
        vm, ac = ctypes.c_long(), J64()
        if not self.dll.getAccessibleContextWithFocus(hwnd, ctypes.byref(vm), ctypes.byref(ac)):
            return False
        try:
            info = self._info(vm.value, ac.value)
            if info is None or not info.accessibleText:
                return False
            self.dll.selectTextRange(vm.value, ac.value, 0, 1 << 20)
            return True
        finally:
            self.dll.releaseJavaObject(vm.value, ac.value)

    # ------------------------------------------------------------- public
    def form(self, hwnd, max_rows=50):
        return self._call(self._form, hwnd, max_rows)

    def focused_info(self, hwnd):
        return self._call(self._focused_info, hwnd)

    def select_focused(self, hwnd):
        return self._call(self._select_focused, hwnd)

    def click(self, hwnd, x, y, clicks=1, right=False):
        return self._call(self._click, hwnd, x, y, clicks, right)

    def tree(self, hwnd, max_nodes=1500):
        return self._call(self._tree, hwnd, max_nodes)

    def focused(self, hwnd):
        return self._call(self._focused, hwnd)

    def set_text(self, hwnd, x, y, text):
        return self._call(self._set_text, hwnd, x, y, text)
