# bgwork

Let Claude drive a Windows GUI app **in the background**, without a VM and without touching your mouse, keyboard or screen.

bgwork launches the app on a separate, hidden Win32 *desktop* (the same kernel object Windows uses for the lock screen / UAC prompt). Claude sees it through screenshots and controls it with its own virtual mouse and keyboard, delivered as window messages. Your real input desktop is never touched — keep working while Claude works.

A **viewer window** mirrors the hidden desktop live. You can click and type in it to use the app yourself, and tick **Pause Claude (I'm driving)** to take over without Claude interfering. The viewer refreshes 12 times a second while you or Claude are acting or the screen is changing, and twice a second when nothing is happening. These frames are only for the viewer; Claude takes a screenshot only when it needs to look.

```
Claude ──MCP stdio──> bgwork mcp ──HTTP 127.0.0.1 + token──> bgwork host ──> hidden desktop "bgwork"
                                                               └── viewer window (on your desktop)
```

## Setup

```bash
py -3.11 -m venv .venv
```

```bash
.venv\Scripts\pip install -r requirements.txt
```

## Use with Claude Code

Register the server for this project in `.mcp.json` (replace `<repo>` with this folder's full path, backslashes doubled):

```json
{
  "mcpServers": {
    "bgwork": {
      "command": "<repo>\\.venv\\Scripts\\python.exe",
      "args": ["-m", "bgwork", "mcp"],
      "env": { "PYTHONPATH": "<repo>" }
    }
  }
}
```

Or add it globally:

```bash
claude mcp add bgwork --scope user -e PYTHONPATH=<repo> -- <repo>\.venv\Scripts\python.exe -m bgwork mcp
```

For Claude Desktop, put the same `command` / `args` / `env` block into `claude_desktop_config.json` under `mcpServers`.

The MCP server starts the host + viewer automatically if they're not running. Then just ask Claude, e.g. *"launch C:\Program Files\MyApp\MyApp.exe with bgwork and export this week's report"*.

### Tools Claude gets

| tool | what it does |
|---|---|
| `screenshot` | capture the hidden desktop (scaled to ≤1280×800; all coordinates are in this image) |
| `click` | left/right/middle, double, with modifiers (`ctrl`, `shift+ctrl`…) |
| `move_mouse`, `drag`, `scroll` | hover, drag-and-drop/selection, wheel |
| `type_text` | unicode text; `\n` = Enter, `\t` = Tab |
| `press_key` | `enter`, `escape`, `ctrl+s`, `ctrl+shift+p`, `alt+f4`, `page_down`, `f5`… |
| `wait` | sleep then screenshot |
| `launch_app` | start an exe on the hidden desktop (maximized); no args = relaunch last app |
| `app_status`, `focus_window`, `stop_app` | processes/windows, bring a window to front, close everything |
| `ui_tree` | Java apps: list components (role, name, center point, state) |
| `set_text` | Java apps: replace a text field's contents directly |
| `read_form` | Java apps: every named field and grid row of the active window, as text |
| `fill_form` | Java apps: fill fields by name and add grid lines in one call (real typing + Tab, so lookups run), then read back and report mismatches (`screenshot=False` for text only). Never saves |
| `grab_report` | Click a button that opens a report in a viewer (e.g. an Export to Excel or Print button) and get the file instead: closes the viewer it opened on the hidden desktop, copies the file to `%LOCALAPPDATA%\bgwork\reports`, and returns spreadsheet rows as text |

Every action returns a fresh screenshot, so Claude sees the result immediately.

## Run the host yourself

```bash
bgwork.cmd --app "C:\Program Files\MyApp\MyApp.exe"
```

App arguments go after `--`, e.g. `bgwork.cmd --app C:\x\app.exe -- --user-data-dir=C:\temp\profile`. The viewer's **Launch app…** button does the same thing; the last app is remembered in `%LOCALAPPDATA%\bgwork\config.json`.

## Caveats

- **Single-instance apps.** Many Electron apps hand off to an already-running copy. If the app is open on your normal desktop, the new window appears *there*, not on the hidden desktop. Close it first, or give the background copy its own profile (`--user-data-dir=...` for most Electron/Chromium apps).
- **Input is posted, not injected.** This is what keeps your real mouse/keyboard free. It works with Electron/Chromium and standard Win32 controls (tested: clicks, double-click, right-click menus, drag-select, wheel, unicode typing, Ctrl/Shift/Alt shortcuts). Things that read *global* hardware state can misbehave: raw-input games, apps that poll `GetAsyncKeyState`, hover effects that rely on the real cursor position, and OS-level drag-and-drop between apps.
- **Java (Swing/AWT) apps** take a posted mouse message's position as screen coordinates (not client coordinates), so bgwork posts screen positions to Java windows. Java also reads held buttons with `GetKeyState`, so while a button is down bgwork attaches to the window's input queue and sets the button's key state; without it Java drops every drag. With both, clicks, double-clicks, right-click menus (including popups that spill outside the window), menu bars, dialogs and drag-select all work as real mouse input. Title-bar drags are done by bgwork moving the window itself, since Windows' own move loop follows the real mouse. The **Java Access Bridge** (`WindowsAccessBridge-64.dll`, from the app's bundled JRE, System32 or `JAVA_HOME`) powers `ui_tree`/`set_text`/`read_form`/`fill_form`, and can drive left clicks instead if you set `JAVA_CLICKS_VIA_BRIDGE = True` in `desktop.py`.
- **Form filling** (`bgwork/forms.py`) clicks each field by accessible name, types the value and presses Tab, then waits for focus to move on (the app blocks while it runs a lookup). A new window (a warning or lookup) stops the fill. Grid cells have no bounds in the Access Bridge, so their positions come from the header widths and a 16 px row height. The current cell is found from the focused editor's bounds. Readings where focus is on the table itself (between editors) are ignored, and the fill stops if focus leaves the grid. A line starts at the first column given, and a row holding only zeros counts as empty. Labels that were blank when the screen was first read are reported as "other text"; they hold looked-up values such as a customer's name. Cells several columns away are reached with a double-click instead of Tabbing through empty ones. A typical form (a header plus two grid lines) takes 5–7 s, read-back included.
- **Report capture** (`bgwork/reports.py`). Many apps write a report to a temp file (often Excel 2003 XML with an `.xls` name in `%TEMP%`) and ShellExecute it. `grab_report` notes which processes had windows before the click. Any new one is a viewer, and the report path is read from its command line; if that fails, it falls back to a report file newly written to `%TEMP%`. Only those new viewer processes (on the hidden desktop) are killed, because closing them across desktops with `WM_CLOSE` or the title-bar X doesn't work. Once the viewer is gone the app may minimize itself or jump to an off-screen position (e.g. x=1920), so bgwork restores those windows and moves them back onto the hidden desktop. `focus_window` does the same for any off-screen window.
- **Win32 popup menus** (right-click and app menus) place posted clicks at the cursor, which sits at (0,0) on the hidden desktop, so a click would just close the menu. bgwork finds the item under the point (`MenuItemFromPoint`), highlights it and sends Enter; hovering highlights items the same way.
- **Mouse-leave tracking** (`TrackMouseEvent`) fires straight away on the hidden desktop, because the real cursor is never over the window. Hover highlights on custom-drawn buttons (e.g. Sumatra's caption buttons) therefore don't show, and a stray leave between press and release cancels the click. bgwork lets the app handle the hover move before pressing, and drops jitter under the system drag threshold while a button is held.
- **Title-bar buttons** (minimize, maximize, close) run a tracking loop that follows the real mouse, so bgwork acts on the release itself and sends the window the matching `WM_SYSCOMMAND`.
- **Stop app** ends every process owning a window on the hidden desktop, hidden windows included, so a single-instance app that was closed to the background really restarts on the next launch.
- **Screen size** matches your primary monitor resolution.
- **UAC / elevated apps** can't be launched onto the hidden desktop from a non-elevated host.
- The host listens only on `127.0.0.1` and requires a random bearer token stored in `%LOCALAPPDATA%\bgwork\host.json`.
- Closing the viewer asks whether to also close the apps; if you leave them, the next host reattaches to the same hidden desktop.
