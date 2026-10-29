"""Which Chromium browsers are installed, and which one opens the fresh login window.

The bot never uses the person's own browser profile (bfp refuses it): it launches the browser binary with a profile
folder of its own. So all that is needed from the installed browser is its executable - Chrome, Brave or Edge.

  BROWSER_PREFERENCE   auto (default) | chrome | brave | edge
  BFP_PATCHED_CHROMIUM an explicitly configured executable (the server's .env); 'auto' honours it first
"""
import os
import shutil
import sys
from pathlib import Path

KINDS = ("chrome", "brave", "edge")
NAMES = {"chrome": "Google Chrome", "brave": "Brave", "edge": "Microsoft Edge", "chromium": "Chromium"}


def kind_of(path: str | os.PathLike) -> str:
    """chrome | brave | edge | chromium, from the executable's name."""
    name = str(path).replace("\\", "/").lower()
    if "brave" in name:
        return "brave"
    if "msedge" in name or "microsoft-edge" in name or "microsoft edge" in name:
        return "edge"
    if "chrome" in name and "chromium" not in name:
        return "chrome"
    return "chromium"


def _windows_candidates(env: dict) -> list:
    roots = [env.get(k) for k in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA") if env.get(k)]
    rel = {"chrome": r"Google\Chrome\Application\chrome.exe",
           "brave": r"BraveSoftware\Brave-Browser\Application\brave.exe",
           "edge": r"Microsoft\Edge\Application\msedge.exe"}
    return [(kind, str(Path(root) / sub)) for kind, sub in rel.items() for root in roots]


def _registry_paths() -> list:
    """App Paths entries (a browser installed somewhere unusual) - Windows only, never fatal."""
    out: list = []
    try:
        import winreg
        for kind, exe in (("chrome", "chrome.exe"), ("brave", "brave.exe"), ("edge", "msedge.exe")):
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    with winreg.OpenKey(hive, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}") as key:
                        out.append((kind, winreg.QueryValue(key, None)))
                except OSError:
                    continue
    except Exception:  # noqa: BLE001
        pass
    return out


def _candidates(env: dict, platform: str, which) -> list:
    if platform.startswith("win"):
        return _windows_candidates(env) + _registry_paths()
    if platform == "darwin":
        return [("chrome", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                ("brave", "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"),
                ("edge", "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")]
    found = []
    for kind, names in (("chrome", ("google-chrome", "google-chrome-stable", "chrome")),
                        ("brave", ("brave-browser", "brave")), ("edge", ("microsoft-edge", "microsoft-edge-stable")),
                        ("chromium", ("chromium", "chromium-browser"))):
        for n in names:
            p = which(n)
            if p:
                found.append((kind, p))
    return found


def find_browsers(*, env: dict | None = None, platform: str | None = None, exists=os.path.exists,
                  which=shutil.which) -> list:
    """Every installed browser, best-known kinds first: [{kind, name, path}] - no duplicates."""
    env = dict(os.environ if env is None else env)
    seen, out = set(), []
    configured = env.get("BFP_PATCHED_CHROMIUM", "").strip()
    pairs = ([(kind_of(configured), configured)] if configured else []) + _candidates(env, platform or sys.platform, which)
    for kind, path in pairs:
        key = os.path.normcase(os.path.normpath(path))
        if path and key not in seen and exists(path):
            seen.add(key)
            out.append({"kind": kind, "name": NAMES.get(kind, kind), "path": path, "configured": path == configured})
    order = {k: i for i, k in enumerate(KINDS + ("chromium",))}
    return sorted(out, key=lambda b: (not b["configured"], order.get(b["kind"], 9)))


def choose(preference: str = "auto", **kw) -> dict | None:
    """The browser for a new login window. 'auto': the configured executable first, then Chrome, Brave, Edge.
    A named kind ('chrome') is used only if installed - no silent substitute for an explicit choice."""
    installed = find_browsers(**kw)
    pref = (preference or "auto").strip().lower()
    if pref in ("", "auto"):
        return installed[0] if installed else None
    return next((b for b in installed if b["kind"] == pref), None)
