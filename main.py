"""
PorxPy — Portfolio X-ray Python

Entry point. Prints a startup banner and runs the Flask development
server. The actual program metadata (name, version, build date) lives in
:mod:`porxpy.__init__` so the API and frontend stay in sync with it.

Run with::

    python main.py

then point a browser at http://127.0.0.1:5000/.
"""

from __future__ import annotations

import sys

from porxpy import NAME, VERSION, BUILD_DATE
from porxpy.app import create_app
from porxpy.config import BASE_DIR


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


def main() -> None:
    """Build the Flask app and start serving on 0.0.0.0:5000."""
    # Before anything prints, including the banner.
    _force_utf8_output()
    _print_banner()
    _legacy_check()
    app = create_app()
    app.run(host="0.0.0.0", port=5000, debug=True)


if __name__ == "__main__":
    main()
