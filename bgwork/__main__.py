"""bgwork — run a Windows GUI app on a hidden desktop that Claude can drive.

  python -m bgwork host [--app PATH] [--args "..."]   start the viewer + control API
  python -m bgwork mcp                                MCP server over stdio (for Claude)
"""
import argparse
import sys


def main():
    ap = argparse.ArgumentParser(prog="bgwork", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("host", help="start the hidden desktop, viewer and control API")
    h.add_argument("--app", help="program to launch on startup")
    h.add_argument("--args", default="", help='arguments for --app (use --args="--flag ..." if they start with -)')
    h.add_argument("app_args", nargs=argparse.REMAINDER, help="or put app arguments after --")
    h.add_argument("--no-viewer", action="store_true", help="run headless (API only)")
    h.add_argument("--desktop", default="bgwork", help="hidden desktop name")
    sub.add_parser("mcp", help="run the MCP server on stdio")
    a = ap.parse_args()

    if a.cmd == "mcp":
        from .mcp_server import main as mcp_main
        mcp_main()
        return

    from . import win32
    win32.set_dpi_aware()
    from .desktop import HiddenDesktop
    from .host import Host, clear_host_file

    host = Host(HiddenDesktop(a.desktop))
    host.serve()
    try:
        if a.app:
            import subprocess
            import threading
            extra = a.app_args[1:] if a.app_args[:1] == ["--"] else a.app_args
            args = " ".join(filter(None, [a.args, subprocess.list2cmdline(extra)]))
            threading.Thread(target=host.launch, args=(a.app, args), daemon=True).start()
        if a.no_viewer:
            import time
            print("bgwork host running headless; Ctrl+C to quit", file=sys.stderr)
            while True:
                time.sleep(3600)
        else:
            from .viewer import Viewer
            Viewer(host).run()
    except KeyboardInterrupt:
        pass
    finally:
        clear_host_file()


if __name__ == "__main__":
    main()
