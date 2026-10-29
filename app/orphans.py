"""Browsers a previous run left behind.

The app drives real browsers (one per account, kept open for a few minutes between jobs). If the app is killed - the console
window closed, the computer restarted mid-job, a crash - those browsers can outlive it, still holding the account's folder; the
next start would then find the folder "in use" and fail to open that account. At start-up we close such leftovers.

Only browsers that are certainly ours are touched: started by automation (they carry a remote-debugging flag - a person's own
browser never does) AND standing on one of this app's own profile folders. A person's everyday browser is never matched, even if
`BFP_USER_DATA_DIR` points at one of its profile folders.
"""
import json
import logging
import os
import subprocess
import sys

LOG = logging.getLogger("orphans")
BROWSER_NAMES = ("chrome.exe", "brave.exe", "msedge.exe", "chromium.exe", "chrome", "brave", "msedge", "chromium", "brave-browser")
AUTOMATION_FLAGS = ("--remote-debugging-port", "--remote-debugging-pipe")
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0          # CREATE_NO_WINDOW: a windowless program must not flash a console


def system_tool(name: str) -> str:
    """The full path of a Windows tool (powershell.exe, taskkill.exe): a program must not depend on what is in PATH - it can be started
    from a shortcut, a service or a script with a bare environment, and "could not list the running processes" would silently switch
    the leftover-browser clean-up off."""
    if sys.platform != "win32":
        return name
    root = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
    candidates = {"powershell": os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe"),
                  "taskkill": os.path.join(root, "System32", "taskkill.exe")}
    path = candidates.get(name)
    return path if path and os.path.exists(path) else name


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path.strip().strip('"'))).rstrip("\\/")


def user_data_dir_of(cmdline: str) -> str | None:
    """The folder named by --user-data-dir=... in a command line (quoted or not), or None."""
    marker = "--user-data-dir="
    i = cmdline.find(marker)
    if i < 0:
        return None
    rest = cmdline[i + len(marker):]
    if rest.startswith('"'):
        j = rest.find('"', 1)
        return rest[1:j] if j > 0 else rest[1:]
    # unquoted: up to the next " --" flag (a folder name may contain spaces); when the quote opened BEFORE the flag
    # ("--user-data-dir=C:\x y\" --next) a closing quote is left on the folder
    j = rest.find(" --")
    return (rest[:j] if j >= 0 else rest).strip().strip('"')


def select_stale(processes: list, profile_dirs: list, *, own_pid: int | None = None) -> list:
    """Of `processes` ([{pid, name, cmdline}]) the ones to close: an automation-launched browser on one of `profile_dirs`."""
    wanted = {_norm(p) for p in profile_dirs if p}
    chosen = []
    for p in processes:
        name, cmd = (p.get("name") or "").lower(), p.get("cmdline") or ""
        if p.get("pid") in (None, own_pid) or name not in BROWSER_NAMES:
            continue
        if not any(flag in cmd for flag in AUTOMATION_FLAGS):
            continue
        folder = user_data_dir_of(cmd)
        if folder and _norm(folder) in wanted:
            chosen.append(p)
    return chosen


def list_processes() -> list:
    """[{pid, name, cmdline}] of the running processes (empty when the system cannot be asked)."""
    try:
        if sys.platform == "win32":
            out = subprocess.run(
                [system_tool("powershell"), "-NoProfile", "-NonInteractive", "-Command",
                 # without this PowerShell writes the console's OEM code page: a folder of a person called "Usuário" comes back
                 # mangled, and the stale-browser clean-up and the window hand-over check never match their own folder
                 "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
                 "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'user-data-dir' } | "
                 "Select-Object ProcessId, Name, CommandLine | ConvertTo-Json -Compress"],
                capture_output=True, text=True, timeout=25, encoding="utf-8", errors="replace", creationflags=NO_WINDOW).stdout.strip()
            rows = json.loads(out) if out else []
            rows = [rows] if isinstance(rows, dict) else rows
            return [{"pid": r.get("ProcessId"), "name": r.get("Name"), "cmdline": r.get("CommandLine") or ""} for r in rows]
        out = subprocess.run(["ps", "-eo", "pid=,comm=,args="], capture_output=True, text=True, timeout=15).stdout
        rows = []
        for line in out.splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) == 3 and parts[0].isdigit():
                rows.append({"pid": int(parts[0]), "name": parts[1], "cmdline": parts[2]})
        return rows
    except Exception as exc:  # noqa: BLE001 - never a reason not to start
        LOG.warning("could not list the running processes: %s", exc)
        return []


def kill(pid: int) -> bool:
    try:
        if sys.platform == "win32":
            return subprocess.run([system_tool("taskkill"), "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=20,
                                  creationflags=NO_WINDOW).returncode == 0
        os.kill(pid, 9)
        return True
    except Exception as exc:  # noqa: BLE001
        LOG.warning("could not close browser process %s: %s", pid, exc)
        return False


def close_stale_browsers(profile_dirs: list, *, lister=list_processes, killer=kill) -> list:
    """Close the browsers a previous run left on `profile_dirs`. -> the pids that were closed."""
    closed = []
    for p in select_stale(lister(), profile_dirs, own_pid=os.getpid()):
        if killer(p["pid"]):
            closed.append(p["pid"])
            LOG.warning("closed a browser left over from an earlier run (pid %s, %s)", p["pid"], user_data_dir_of(p["cmdline"]))
    return closed


def our_profile_dirs() -> list:
    """The folders this app drives browsers on: the page's own account and every account added from the app."""
    import config
    dirs = [config.BFP_MASTER_USER_DATA_DIR]
    dirs += [a.get("user_data_dir") for a in config.load_accounts()]
    return [d for d in dirs if d]
