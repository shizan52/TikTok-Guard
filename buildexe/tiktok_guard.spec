# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller recipe for TikTok Guard (run it through packaging/build.py, which also tests and checks the result).

dist/TikTok Guard/
    TikTok Guard.exe                 the app (no console window)
    TikTok Guard Diagnostics.exe     the same program as a console program: --self-test + a report file, for support
    _internal/                       Python, the libraries, the screen (app/web), the browser driver, client_defaults.env

What is deliberately NOT in it: .env (keys), data/, network/ (accounts, cookies), bfp_runs/, screenshots, tests, docs, the 109 MB
`patchright` package (the proven engine is `patched`: the person's own Edge/Chrome/Brave over its debugging port).
"""
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules
from PyInstaller.utils.win32.versioninfo import (FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct,
                                                 VSVersionInfo)

HERE = Path(SPECPATH)                                    # noqa: F821  (provided by PyInstaller)
PROJECT = HERE.parent
BFP_DIR = os.environ.get("BFP_DIR", r"..\bfp")
VERSION = os.environ["TG_VERSION"]                       # set by build.py, e.g. 1.0.0

parts = [int(x) for x in VERSION.split(".")] + [0, 0, 0, 0]
version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=tuple(parts[:4]), prodvers=tuple(parts[:4]), mask=0x3F, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0),
    kids=[StringFileInfo([StringTable("040904B0", [
        StringStruct("CompanyName", "TikTok Guard"),
        StringStruct("FileDescription", "TikTok Guard - comment desk"),
        StringStruct("FileVersion", VERSION),
        StringStruct("InternalName", "TikTok Guard"),
        StringStruct("OriginalFilename", "TikTok Guard.exe"),
        StringStruct("ProductName", "TikTok Guard"),
        StringStruct("ProductVersion", VERSION),
        StringStruct("LegalCopyright", "Copyright (c) 2026 TikTok Guard"),
    ])]), VarFileInfo([VarStruct("Translation", [1033, 1200])])])

pw_datas, pw_binaries, pw_hidden = collect_all("playwright")          # includes driver/node.exe + the driver's JavaScript package

a = Analysis(
    [str(HERE / "entry.py")],
    pathex=[str(PROJECT), BFP_DIR],
    binaries=pw_binaries,
    datas=[(str(PROJECT / "app" / "web"), "app/web"), (str(HERE / "client_defaults.env"), "."), *pw_datas],
    hiddenimports=["bfp", "greenlet", "pyee", *pw_hidden, *collect_submodules("app")],
    hookspath=[], runtime_hooks=[],
    excludes=["patchright", "rebrowser_playwright", "camoufox", "nuitka", "tkinter", "_tkinter", "matplotlib", "numpy", "scipy", "pandas",
              "IPython", "jupyter", "notebook", "PyQt5", "PyQt6", "PySide2", "PySide6", "pytest", "setuptools", "pkg_resources", "test"],
    noarchive=False,
)
# The installed `patchright` ships a PyInstaller hook of its own that copies its whole 108 MB driver whenever playwright is imported,
# even though `patchright` is excluded above: drop everything of it (the playwright driver collected above does the work).
def _not_patchright(entry):
    return not entry[0].replace("\\", "/").lower().startswith("patchright")


a.datas = [d for d in a.datas if _not_patchright(d)]
a.binaries = [b for b in a.binaries if _not_patchright(b)]
a.pure = [p for p in a.pure if _not_patchright(p)]
pyz = PYZ(a.pure)

common = dict(exclude_binaries=True, icon=str(HERE / "icon.ico"), version=version_info, upx=False, strip=False)
app_exe = EXE(pyz, a.scripts, [], name="TikTok Guard", console=False, **common)
diagnostics_exe = EXE(pyz, a.scripts, [], name="TikTok Guard Diagnostics", console=True, **common)
coll = COLLECT(app_exe, diagnostics_exe, a.binaries, a.datas, upx=False, name="TikTok Guard")
