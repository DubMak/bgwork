"""Catch a report the app opens in a viewer (Excel, a PDF reader) and keep just the file.

Many apps write a report to a temp file and ask Windows to open it. On the hidden desktop
that viewer is useless, so: click the button, wait for the file, close the viewer if it opened
on the hidden desktop, and return the file (Excel XML spreadsheets come back as text rows).
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET

REPORT_EXTS = (".xls", ".xlsx", ".xml", ".csv", ".txt", ".pdf", ".htm", ".html")
NO_WINDOW = 0x08000000


def _cmdlines(pids):
    """{pid: (exe path, command line)} for the given pids."""
    if not pids:
        return {}
    flt = " or ".join(f"ProcessId={int(p)}" for p in pids)
    ps = (f"Get-CimInstance Win32_Process -Filter '{flt}' | "
          "Select-Object ProcessId,ExecutablePath,CommandLine | ConvertTo-Json -Compress")
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True,
                       text=True, timeout=20, creationflags=NO_WINDOW)
    try:
        rows = json.loads(r.stdout or "[]")
    except ValueError:
        return {}
    rows = [rows] if isinstance(rows, dict) else rows
    return {x["ProcessId"]: (x.get("ExecutablePath") or "", x.get("CommandLine") or "") for x in rows}


def _paths_in(cmdline):
    """Existing report files named on a command line."""
    cands = re.findall(r'"([^"]+)"', cmdline) + cmdline.split()
    return [c for c in cands if c.lower().endswith(REPORT_EXTS) and os.path.isfile(c)]


def _new_temp_files(since):
    tmp = tempfile.gettempdir()
    out = []
    for name in os.listdir(tmp):
        p = os.path.join(tmp, name)
        try:
            if name.lower().endswith(REPORT_EXTS) and os.path.isfile(p) and os.path.getmtime(p) >= since:
                out.append(p)
        except OSError:
            pass
    return sorted(out, key=os.path.getmtime)


def excel_xml_rows(path):
    """Rows of an Excel 2003 XML spreadsheet (a common format for Java apps' Excel exports), or None."""
    ns = "{urn:schemas-microsoft-com:office:spreadsheet}"
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return None
    if not root.tag.startswith(ns):
        return None
    rows = []
    for r in root.iter(ns + "Row"):
        row = []
        for c in r.iter(ns + "Cell"):
            idx = c.get(ns + "Index")
            if idx:
                row += [""] * (int(idx) - 1 - len(row))
            d = c.find(ns + "Data")
            row.append((d.text or "").strip() if d is not None else "")
        if any(row):
            rows.append(row)
    return rows


def grab(desk, x, y, out_dir, timeout=60.0):
    """Click (x, y), wait for the report it opens, close a viewer that opened on the hidden
    desktop, and copy the report file into out_dir."""
    before = {w.pid for w in desk.windows()}
    start = time.time() - 1
    desk.click(x, y)
    end = time.time() + timeout
    found, viewers, checked = None, {}, set()
    while time.time() < end and not found:
        time.sleep(0.5)
        new = {w.pid for w in desk.windows()} - before - checked
        for pid, (exe, cmd) in _cmdlines(new).items():
            viewers[pid] = exe
            paths = _paths_in(cmd)
            if paths:
                found = paths[0]
        checked |= new
        if not found:
            files = _new_temp_files(start)
            if files:
                found = files[-1]
                time.sleep(1.5)     # give the viewer a moment to show up so it can be closed
                more = {w.pid for w in desk.windows()} - before - checked
                viewers.update({p: e for p, (e, _c) in _cmdlines(more).items()})
    if not found:
        raise TimeoutError(f"no report file appeared within {timeout:.0f}s")
    # only viewers started by this click with a window on the hidden desktop: never the user's own
    closed = []
    for pid, exe in viewers.items():
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True,
                       creationflags=NO_WINDOW)
        closed.append(os.path.basename(exe) or str(pid))
    if closed:
        # the app can minimize itself when the viewer it handed off to disappears; put it back
        time.sleep(1.0)
        lost = desk.minimized_windows() + [x.hwnd for x in desk.windows()
                                           if x.left >= desk.width or x.right <= 0]
        for h in reversed(lost):
            desk.focus_window(h)      # restores and moves it back on screen
    os.makedirs(out_dir, exist_ok=True)
    dest = os.path.join(out_dir, time.strftime("%Y%m%d-%H%M%S-") + os.path.basename(found))
    shutil.copy2(found, dest)
    rows = excel_xml_rows(dest)
    return {"file": dest, "source": found, "closed_viewers": closed, "rows": rows}


def rows_text(rows, max_chars=40000):
    """Tab-separated text of the rows, dropping columns that are empty in every row."""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    keep = [i for i in range(width) if any(r[i] for r in rows[1:])] or range(width)
    text = "\n".join("\t".join(r[i] for i in keep) for r in rows)
    return text if len(text) <= max_chars else text[:max_chars] + "\n…(truncated; read the file for the rest)"
