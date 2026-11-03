"""
PorxPy — Portfolio X-ray Python

Entry point. Prints a startup banner and runs the Flask development
server. The actual program metadata (name, version, build date) lives in
:mod:`porxpy.__init__` so the API and frontend stay in sync with it.

Run with::

    python main.py

then point a browser at http://127.0.0.1:5000/.

The installed Windows build (v0.145.0) runs this same file, frozen by
PyInstaller, and takes the other branch of :func:`main`: no debug
server, this PC only, the user's data under %LOCALAPPDATA%, and the
browser opened for them. See ``packaging/``.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import threading
import time
import urllib.request
import webbrowser

from porxpy import NAME, VERSION, BUILD_DATE
from porxpy.config import (BASE_DIR, DATA_DIR, FROZEN, RESOURCES_DIR,
                           SHIPPED_RESOURCES_DIR, data_dir_id)
# porxpy.app is imported inside main(), not here: importing it loads the
# resource CSVs, and an installed build's first run has to copy them
# into the data directory (_prepare_data_dir) BEFORE that happens, or
# it starts with every vocabulary empty.


def _force_utf8_output() -> None:
    """Make stdout and stderr survive the diagnostics this app prints.

    The app's console output is not plain ASCII — resolution notes carry
    "->" as an arrow, money is printed with currency symbols, the banner
    and several tables use box-drawing characters. On Windows that is a
    problem the moment stdout is not a UTF-8 console: the default there
    is cp1252, which cannot encode any of them, and ``print`` raises
    ``UnicodeEncodeError``.

    That is not a cosmetic failure. The prints happen inside request
    handlers, so the exception propagates out of the view, Flask returns
    a 500 with no CORS headers, and the browser reports it as
    "NetworkError when attempting to fetch resource" — a message that
    says nothing about an encoding and sends you looking at the network.

    Redirecting output is enough to trigger it (``python main.py > log``
    gives cp1252 even when the console itself is UTF-8), so this is not
    hypothetical for anyone who logs a run to a file or runs the server
    under a supervisor.

    ``errors="replace"`` rather than a hard switch: a character that
    still cannot be written becomes "?" in the log, which is a cosmetic
    loss, where raising is a failed request.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            # Not a reconfigurable text stream (a pytest capture, a pipe
            # wrapper). Nothing to do, and nothing worth failing over.
            pass


def _print_banner() -> None:
    """Print the startup banner with name, version, and build date."""
    line = "=" * 55
    print()
    print(line)
    print(f"  {NAME}  v{VERSION}  (built {BUILD_DATE})")
    print("  Portfolio X-ray Python")
    print(line)
    print()


def _legacy_check() -> None:
    """Warn if pre-0.12 files are detected on disk.

    0.12.0 changed the on-disk layout: cache files now live under
    cache/listings/ and cache/funds/ (split by ticker vs ISIN); the
    three separate override files were consolidated into overrides.json;
    portfolio entries were slimmed to {ticker, shares}; CSVs moved to
    resources/. No automatic migration is performed — the user is
    expected to use Settings → Reset to wipe legacy state. This check
    surfaces the situation so the wipe is discoverable rather than
    leaving stale files cluttering the project root.
    """
    legacy_files = [
        BASE_DIR / "asset_class_overrides.json",
        BASE_DIR / "breakdown_source_overrides.json",
        BASE_DIR / "fund_structure.json",
        BASE_DIR / "country_codes.csv",
        BASE_DIR / "country_currency.csv",
    ]
    legacy_loose_cache = [
        fp for fp in (BASE_DIR / "cache").glob("*.json")
        # Anything directly at cache/ that isn't a known shared pseudo-cache
        # (FX_*, FXH_*, _symbol_info, _symbol_aliases) is a pre-0.12 leftover.
        if not (fp.stem.startswith("FX_") or fp.stem.startswith("FXH_")
                or fp.stem in ("_symbol_info", "_symbol_aliases"))
    ] if (BASE_DIR / "cache").exists() else []

    present = [fp for fp in legacy_files if fp.exists()]
    if not present and not legacy_loose_cache:
        return

    print("  NOTICE: pre-0.12 files detected on disk.")
    if present:
        for fp in present:
            print(f"    - {fp.name}")
    if legacy_loose_cache:
        print(f"    - cache/*.json  ({len(legacy_loose_cache)} loose files)")
    print("  These are left in place. The 0.12.0 layout uses:")
    print("    overrides.json (consolidated), cache/listings/, cache/funds/, resources/")
    print("  Open Settings → Danger zone to wipe legacy state and start fresh.")
    print()


def _prepare_data_dir() -> None:
    """Make a separate data directory usable on first run.

    The program files are read-only, so the resource CSVs the app writes
    aliases into are copied to the user's data directory, where they are
    read from then on. Only files that are MISSING are copied: a file
    already there carries the user's own aliases, and overwriting it
    with the released copy would silently throw those away. The flip
    side — an upgrade's newer released CSVs do not reach an existing
    install — is stated in GETTING_STARTED §2 and on the WISHLIST.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RESOURCES_DIR.mkdir(parents=True, exist_ok=True)
    for src in sorted(SHIPPED_RESOURCES_DIR.glob("*.csv")):
        dst = RESOURCES_DIR / src.name
        if not dst.exists():
            shutil.copy2(src, dst)
            print(f"  first run: {src.name} -> {dst.parent}")


def _porxpy_answering(port: int) -> bool:
    """True when THIS PorxPy — the same data directory — already serves the port.

    Not merely "a PorxPy": a source checkout's dev server on 5000 is a
    PorxPy too, and until v0.145.1 an installed build that found one
    opened the browser on it and quit, so the user saw the checkout's
    portfolios and took them for the installed app's. Another copy on
    the port is a port that is taken, nothing more.
    """
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/meta", timeout=1.5) as r:
            meta = json.loads(r.read().decode("utf-8")) or {}
        return meta.get("name") == NAME and meta.get("data_id") == data_dir_id()
    except Exception:
        return False


# Where a running copy says which port it took (v0.145.1; a source run
# writes it too since v0.146.0). The port is not always 5000 — the other
# kind of PorxPy may hold that one — so a second start has to be told
# where to look rather than assume. Gitignored: in a checkout DATA_DIR
# is the project root.
_RUNNING_FP = DATA_DIR / "running.json"


def _running_port() -> int | None:
    try:
        return int(json.loads(_RUNNING_FP.read_text(encoding="utf-8"))["port"])
    except Exception:
        return None


def _port_free(port: int) -> bool:
    """True when nothing accepts connections on this port.

    Tested by connecting rather than by binding: on Windows a second
    process can bind a port another one is serving on without an error
    (CLAUDE.md, "Check before starting another one"), so a bind test
    would say yes to a port that is very much taken.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) != 0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _claim_port(preferred: int) -> tuple[int | None, int]:
    """Decide where to serve, shared by the installed build and the source run.

    One routine for both since v0.146.0. Until then only the installed
    build looked before it bound: the source run took 5000 regardless,
    and on Windows a second process binds a port another one is already
    serving on without an error — so starting from PyCharm while the
    installed copy held 5000 left two servers on one port, each request
    answered by whichever won, with nothing on screen to say so.

    Args:
        preferred: The port to take when nothing holds it.

    Returns:
        ``(running, port)``. ``running`` is the port on which THIS
        PorxPy (same data directory) already answers, or ``None``; when
        it is set the caller should not start a second server. ``port``
        is the one to serve on otherwise: ``preferred`` when it is free,
        else the next free one up to nine above it — so the page keeps
        one address from start to start, and the browser keeps the
        page's own settings (the chosen portfolio, level selectors) per
        address — and any free port last.
    """
    # Ourselves first: on the port the running copy recorded, then on the
    # one we would take.
    for p in dict.fromkeys(x for x in (_running_port(), preferred) if x):
        if _porxpy_answering(p):
            return p, p
    port = preferred
    if not _port_free(port):
        port = next((p for p in range(port + 1, port + 10) if _port_free(p)),
                    None) or _free_port()
    return None, port


def _record_port(port: int) -> None:
    try:
        _RUNNING_FP.write_text(json.dumps({"port": port, "pid": os.getpid()}),
                               encoding="utf-8")
    except OSError:
        pass                        # only costs a second start its shortcut


def _run_installed() -> None:
    """Serve the installed build: this PC only, no debugger, browser opened.

    Started twice, it does not start a second server — the second start
    opens the browser on the first one and exits, so a double-click on
    the shortcut is never a way to end up with two copies of the data
    being written. Port 5000 when it is free (the frontend asks the page
    it was served from, so any port works), another one when something
    else holds it. ``PORXPY_PORT`` picks one explicitly.
    """
    running, port = _claim_port(int(os.environ.get("PORXPY_PORT") or 5000))
    if running:
        url = f"http://127.0.0.1:{running}/"
        print(f"  PorxPy is already running — opening {url}")
        print("  (this window closes by itself; the running PorxPy has its own)")
        webbrowser.open(url)
        time.sleep(4)
        return
    _record_port(port)

    url = f"http://127.0.0.1:{port}/"
    print(f"  Your data:  {DATA_DIR}")
    print(f"  Open:       {url}")
    print()
    print("  Close this window to stop PorxPy.")
    print()
    from porxpy.app import create_app
    app = create_app()
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    # Loopback only, unlike the dev server's 0.0.0.0: an installed copy
    # has no reason to answer other machines on the network, and the
    # API it serves can rewrite the user's portfolios. No reloader, no
    # debugger — the Werkzeug debugger executes code typed into its page.
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False,
            threaded=True)


def _run_source() -> None:
    """Serve from a source checkout: the dev server with the reloader.

    The port is chosen by :func:`_claim_port` like the installed build's,
    with one difference that follows from the reloader. Werkzeug's
    reloader runs this file twice: a watcher process that binds the
    socket, and a child (``WERKZEUG_RUN_MAIN=true``) that serves on the
    socket it inherits and is restarted on every save. The child must
    not look again — to it the port is held, by its own parent — so it
    takes the port the watcher chose, passed down in ``PORXPY_PORT``.

    A copy of THIS checkout already serving is not started twice: it
    says where it is and stops, because two servers writing one data
    directory is the failure the check exists to prevent.
    """
    _legacy_check()
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        port = int(os.environ.get("PORXPY_PORT") or 5000)
    else:
        running, port = _claim_port(int(os.environ.get("PORXPY_PORT") or 5000))
        if running:
            print(f"  PorxPy (this checkout) is already running on "
                  f"http://127.0.0.1:{running}/ — not starting a second one.")
            return
        if port != 5000:
            print(f"  Port 5000 is taken by something else (the installed "
                  f"PorxPy, perhaps) — serving on {port} instead.")
        print(f"  Open:       http://127.0.0.1:{port}/")
        print()
        os.environ["PORXPY_PORT"] = str(port)   # inherited by the reloader child
        _record_port(port)
    # Imported only now: it loads every resource CSV, which a start that
    # is about to stop because the port is ours already should not wait for.
    from porxpy.app import create_app
    app = create_app()
    app.run(host="0.0.0.0", port=port, debug=True)


def main() -> None:
    """Build the Flask app and serve it.

    From source: :func:`_run_source`, the development server with the
    reloader. Installed (frozen): :func:`_run_installed`.
    """
    # Before anything prints, including the banner.
    _force_utf8_output()
    # Whenever the data lives apart from the program: always when
    # installed, and in a source run pointed at PORXPY_DATA_DIR (v0.146.0)
    # — which until then started with every vocabulary empty, and failed
    # its first bundle import writing into a resources folder that did
    # not exist. A plain source run reads and writes the shipped files
    # in place, so there is nothing to copy.
    if RESOURCES_DIR.resolve() != SHIPPED_RESOURCES_DIR.resolve():
        _prepare_data_dir()
    _print_banner()
    if FROZEN:
        _run_installed()
        return
    _run_source()


if __name__ == "__main__":
    main()
