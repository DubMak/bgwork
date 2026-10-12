"""Fill and read Java forms by field name, through real clicks and typing.

Fields are found by their accessible name (apps expose names like "orders:header:customer_id";
the part after the last ":" is enough). Values go in as keystrokes followed by Tab, so the app
runs its own lookups and validation exactly as if a person typed them. Nothing is saved here.
"""
import re
import time

ROW_HEIGHT = 16          # Swing's default JTable row height (cell bounds aren't exposed)
SETTLE = 0.05           # focus moves before the app finishes filling the next cells; typing sooner loses keys
LOOKUP_TIMEOUT = 5.0


class FormError(Exception):
    pass


def short(name):
    return name.rsplit(":", 1)[-1]


def find_field(form, name):
    """Field entry for a full or short name (case-insensitive), or None."""
    fields = form["fields"]
    if name in fields:
        return name, fields[name]
    low = name.lower()
    hits = [(k, v) for k, v in fields.items() if short(k).lower() == low]
    return hits[0] if len(hits) == 1 else (None, None)


def same(a, b):
    """Loose comparison of a typed value with what the app shows (numbers, case, spaces)."""
    a, b = str(a).strip(), str(b).strip()
    if a.lower() == b.lower():
        return True
    if "/" in a + b and re.sub(r"\D", "", a) == re.sub(r"\D", "", b) != "":
        return True     # dates typed as digits (10112026) read back as 10/11/2026
    num = lambda s: float(re.sub(r"[,$\s]", "", s))
    try:
        return num(a) == num(b)
    except ValueError:
        return False


def blank_row(vals):
    """A grid row with nothing entered yet (some apps prefill a new line with 0.00)."""
    return all(not v.strip() or same(v, "0") for v in vals)


class FormFiller:
    def __init__(self, desk, top):
        self.desk, self.top, self.java = desk, top, desk.java
        self.log = []
        self.timing = {}
        self.path = []      # grid columns visited, for diagnosing

    # ------------------------------------------------------------ helpers
    def _windows(self):
        return {x.hwnd for x in self.desk.windows()}

    def _check_popup(self, before):
        new = [x for x in self.desk.windows() if x.hwnd not in before]
        if new:
            raise FormError(f"a window opened: {new[0].title or new[0].cls!r} — stopped there")

    def _settle(self, prev, before):
        """Wait until focus has left `prev` (the app finished handling the Tab, lookups included)."""
        end = time.time() + LOOKUP_TIMEOUT
        while True:
            cur = self.java.focused_info(self.top)      # blocks while the app is busy on its UI thread
            if cur is None or cur[:2] != prev[:2] or cur[3] != prev[3]:
                break
            if time.time() > end:
                self._check_popup(before)
                raise FormError(f"focus stayed on {short(prev[0]) or prev[1]!r} after Tab — the app may have rejected the value")
            time.sleep(0.02)
        if SETTLE:
            time.sleep(SETTLE)
        self._check_popup(before)

    def _t(self, k, t):
        now = time.time()
        self.timing[k] = round(self.timing.get(k, 0) + now - t, 3)
        return now

    def _type(self, value):
        """Replace the focused component's text with value, then Tab out of it."""
        t = time.time()
        before = self._windows()
        t = self._t("windows", t)
        prev = self.java.focused_info(self.top)
        if prev is None:
            raise FormError("nothing has keyboard focus")
        t = self._t("focus", t)
        self.java.select_focused(self.top)
        t = self._t("select", t)
        if value != "":
            self.desk.type_text(str(value))
        self.desk.press("tab")
        t = self._t("type", t)
        self._settle(prev, before)
        self._t("settle", t)

    # ------------------------------------------------------------ header fields
    def fill_field(self, form, name, value):
        full, f = find_field(form, name)
        if f is None:
            raise FormError(f"no field named {name!r}")
        # a field can unlock as earlier ones are filled (an order number after the customer), so don't
        # trust the snapshot's read-only flag: click it and see whether it takes focus
        t = time.time()
        before = self._windows()
        self.desk.click(f["x"], f["y"])
        end = time.time() + 1.0
        while True:
            cur = self.java.focused_info(self.top)
            if (cur and cur[0] == full) or time.time() > end:
                break
            time.sleep(0.02)
        self._check_popup(before)
        if not cur or cur[0] != full:
            raise FormError(f"{short(full)!r} didn't take focus (read-only, or locked until another field is filled?)")
        self._t("click", t)
        self._type(value)
        self.log.append(f"{short(full)} = {value!r}")

    # ------------------------------------------------------------ grid lines
    def _column_at(self, table, x):
        left = table["rect"][0]
        for i, wd in enumerate(table["widths"]):
            if left <= x < left + wd:
                return i
            left += wd
        return None

    def _cell_point(self, table, row, col):
        left = table["rect"][0] + sum(table["widths"][:col])
        top = table["rect"][1]
        return left + table["widths"][col] // 2, top + row * ROW_HEIGHT + ROW_HEIGHT // 2

    def _current_column(self, table):
        """Column of the cell being edited. The app can move focus again just after a lookup, so
        wait for two readings that agree."""
        last = None
        widest = max(table["widths"]) + 4
        for _ in range(40):
            cur = self.java.focused_info(self.top)
            col = None
            if cur and cur[1] != "table":
                l, t, r, b = cur[3]
                tl, tt, tr, tb = table["rect"]
                # between editors focus sits on the table itself (its rect isn't a cell), and after
                # a lookup the app may move it out of the grid (a header field's x isn't a column)
                if 0 < r - l <= widest and tt <= (t + b) // 2 <= tb:
                    col = self._column_at(table, (l + r) // 2)
            if col is not None and col == last:
                return col
            last = col
            time.sleep(0.03)
        return None

    def fill_line(self, table, row, values):
        """values: {column name: value}. Starts editing the first given column with a double-click,
        then Tabs across, typing where a value is given and leaving the app's defaults elsewhere."""
        cols = [c.lower() for c in table["columns"]]
        want = {}
        for k, v in values.items():
            if k.lower() not in cols:
                raise FormError(f"no grid column named {k!r} (columns: {', '.join(c for c in table['columns'] if c)})")
            want[cols.index(k.lower())] = v
        first = min(want)     # start at the first column given (the grid may open on an earlier column)
        t = time.time()
        before = self._windows()
        self.desk.click(*self._cell_point(table, row, first), clicks=2)
        self._check_popup(before)
        self._t("click", t)
        done = set()
        steps = 0
        jumped = False
        while len(done) < len(want):
            t = time.time()
            col = self._current_column(table)
            self._t("column", t)
            if col is None:
                cur = self.java.focused_info(self.top)
                where = f" — focus is on {short(cur[0]) or cur[1]!r}" if cur else ""
                raise FormError(f"line {row + 1}: focus left the grid{where}")
            if col in done:
                raise FormError(f"line {row + 1}: Tab didn't reach column {table['columns'][min(set(want) - done)]!r}")
            steps += 1
            if steps > len(cols) + 2:
                raise FormError(f"line {row + 1}: Tab went past the end of the row")
            self.path.append(table["columns"][col])
            if col in want:
                self._type(want[col])
                done.add(col)
                jumped = False
                self.log.append(f"line {row + 1} {table['columns'][col]} = {want[col]!r}")
            else:
                nxt = min(c for c in want if c not in done)
                gap = [c for c in range(col + 1, nxt) if table["widths"][c] > 0]
                if gap and nxt > col and not jumped:
                    # several cells to skip: double-click the target instead of Tabbing through
                    # them (the app keeps its defaults in the skipped cells either way)
                    jumped = True
                    t = time.time()
                    before = self._windows()
                    self.desk.click(*self._cell_point(table, row, nxt), clicks=2)
                    self._check_popup(before)
                    self._t("click", t)
                else:
                    self._type("")      # keep whatever the app filled in
        return True

    # ------------------------------------------------------------ whole form
    def fill(self, fields=None, lines=None):
        t0 = time.time()
        r = self._fill(fields or {}, lines or [])
        r["seconds"] = round(time.time() - t0, 1)
        r["timing"] = self.timing
        return r

    def _fill(self, fields, lines):
        t = time.time()
        form = self.java.form(self.top)       # positions don't move while filling, so read once
        self._t("read", t)
        for name, value in fields.items():
            self.fill_field(form, name, value)
        if lines:
            if not form["tables"]:
                raise FormError("this window has no grid")
            table = form["tables"][0]
            first = next((r for r, vals in enumerate(table["rows"]) if blank_row(vals)), len(table["rows"]))
            for i, values in enumerate(lines):
                self.fill_line(table, first + i, values)
        t = time.time()
        r = self.check(fields, lines)
        self._t("check", t)
        return r

    def check(self, fields, lines):
        """Read the form back and compare with what was asked for."""
        form = self.java.form(self.top)
        problems = []
        for name, value in fields.items():
            full, f = find_field(form, name)
            if f and not same(value, f["value"]):
                problems.append(f"{short(full)}: wanted {value!r}, shows {f['value']!r}")
        if lines and form["tables"]:
            t = form["tables"][0]
            filled = [r for r in t["rows"] if not blank_row(r)]
            cols = [c.lower() for c in t["columns"]]
            for i, values in enumerate(lines):
                if i >= len(filled):
                    problems.append(f"line {i + 1}: not in the grid")
                    continue
                for k, v in values.items():
                    shown = filled[i][cols.index(k.lower())]
                    if not same(v, shown):
                        problems.append(f"line {i + 1} {k}: wanted {v!r}, shows {shown!r}")
        return {"problems": problems, "form": summarize(form)}


def summarize(form):
    """Compact text of a form: non-empty fields, then grid rows with values."""
    out = []
    for k, f in form["fields"].items():
        flag = "" if f["editable"] else " (read-only)"
        out.append(f"{short(k)}: {f['value']!r}{flag}" + ("  ← focus" if f["focused"] else ""))
    shown = [l for l in sorted(form.get("labels", []), key=lambda l: (l["y"], l["x"])) if l["text"]]
    if shown:
        out.append("other text: " + " | ".join(l["text"] for l in shown))
    for t in form["tables"]:
        vis = [i for i, wd in enumerate(t["widths"]) if wd > 0] or range(len(t["columns"]))
        out.append("grid: " + " | ".join(t["columns"][i] for i in vis))
        for n, row in enumerate(t["rows"]):
            if any(row):
                out.append(f"  line {n + 1}: " + " | ".join(row[i] for i in vis))
    return "\n".join(out)
