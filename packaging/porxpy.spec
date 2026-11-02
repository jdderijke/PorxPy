# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the installed Windows build (v0.145.0).

Run through packaging/build.ps1, which also runs Inno Setup. By hand,
from the project root:

    python -m PyInstaller packaging/porxpy.spec --noconfirm

One FOLDER, not one file: a one-file exe unpacks itself to a temp
directory on every start, which costs seconds with pandas and
matplotlib aboard and trips virus scanners; the installer hides the
folder anyway.

What ships beside the code is only what config.APP_DIR is READ for —
the page and the released resource CSVs. Nothing of the user's
(portfolios, settings, cache) can end up in the bundle, because none
of it is listed here.
"""
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files

ROOT = Path(SPECPATH).parent

datas = [
    (str(ROOT / "fund_explorer.html"), "."),
    (str(ROOT / "resources" / "*.csv"), "resources"),
]
binaries, hiddenimports = [], []

# curl_cffi carries its own libcurl and a CA bundle as package data —
# the TLS impersonation in porxpy/yf_session.py fails without them.
for pkg in ("curl_cffi",):
    d, b, h = collect_all(pkg)
    datas += d; binaries += b; hiddenimports += h
# Certificate bundle, and reportlab's own fonts and data files.
datas += collect_data_files("certifi")
datas += collect_data_files("reportlab")

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports + ["porxpy.report", "porxpy.bundles"],
    excludes=["tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6",
              "IPython", "pytest", "notebook"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="PorxPy",
    icon=str(ROOT / "packaging" / "porxpy.ico"),
    # A console on purpose: it is where the banner, the log and any
    # error show, and closing it is how PorxPy is stopped.
    console=True,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="PorxPy", upx=False)
