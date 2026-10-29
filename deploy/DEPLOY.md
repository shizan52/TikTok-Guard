# Deploying the TikTok guard to a server

## What travels, what never does
`python run.py package --out <folder>` builds the bundle. It contains:

| in the bundle | notes |
|---|---|
| the bot's code (`app/`, `run.py`, `config.py`, `tests/`, `deploy/`) and `vendor/bfp.py` | an explicit allow-list: nothing else on the machine can sneak in |
| `profiles/<account>/state.json` + `profiles/<account>/indexeddb/` for **each account the bot uses** | **only TikTok's own data**: TikTok cookies, TikTok localStorage, TikTok IndexedDB |
| `data/bot.db` | your `/ai` rules, `/ai_context` text, seen-comment history (no secrets) |
| `.env.template` | settings carried over, **every secret left blank** |

It never contains: browsing history, saved passwords, autofill, bookmarks, extensions, `Local State`, the cookies /
local data of any other site, caches, your personal browser's folders, or `.env`. `package` verifies this itself
(`verify-bundle` re-checks any bundle later) and refuses to report success otherwise.

The bot also **refuses to drive or touch an everyday browser profile** (`%LOCALAPPDATA%\BraveSoftware\Brave-Browser\User Data`,
Chrome, Edge, Chromium, and the Linux/macOS equivalents) - bfp raises an error instead. Your own Brave keeps working
exactly as before: the bot only ever launches its own browser with its own profile folders, and only ever stops
processes that carry those folders' names.

> `profiles/*/state.json` hold live TikTok session cookies (that is what "logged in" is). Move the bundle over an encrypted
> channel (SFTP/SCP), never by e-mail/chat. `restore-profiles` deletes them after use. Build without them with
> `--no-sessions` and log in on the server instead (`python run.py add-account`).

## Server requirements (kept deliberately small)
* **2 vCPU / 3 GB RAM** is the comfortable size (1 vCPU / 2 GB works but has no headroom). One browser runs at a time,
  at below-normal priority, muted; nothing runs between scans except an idle Python process (~100 MB).
  Measured on the real page (headless Brave, no resource blocking):

  | scan | wall time | browser peak (private) | CPU | network |
  |---|---|---|---|---|
  | quiet (profile + post list only) | ~12 s | ~0.8 GB | ~20 CPU-s | ~5 MB |
  | opens 3 posts to read comments | ~55-62 s | ~1.1-1.2 GB | ~60-70 CPU-s | ~17-21 MB |

  The page is read every 120 s (`POLL_INTERVAL_SECONDS`), but most rounds are the quiet kind: a post is only reopened
  when its comment count changed.
* Python 3.11+, the Brave browser, outbound HTTPS. No GPU, no display (headless).
* Disk: < 1 GB; the bot cleans itself (see README: retention, screenshots, browser caches).

## Linux (Debian/Ubuntu)
```
scp -r bundle/ user@server:/tmp/tiktok-guard-bundle
ssh user@server
cd /tmp/tiktok-guard-bundle && sudo bash deploy/install_linux.sh      # 1st run: installs, creates .env, stops
sudo nano /opt/tiktok-guard/.env                                       # OPENROUTER_API_KEY, TELEGRAM_BOT_TOKEN
sudo bash deploy/install_linux.sh                                      # 2nd run: rebuilds profiles, starts the service
```
The systemd unit limits the bot (`Nice=10`, `CPUQuota=100%`, `MemoryMax=2600M`, `IOSchedulingClass=idle`) and kills any
leftover browser when it stops. Logs: `journalctl -u tiktok-guard -f` and `data/logs/bot.log`.

## Updating the code on a running server
Copy the changed files over the app folder (`app\`, `config.py`, ...) and **restart the service** (`nssm restart
tiktok-guard`). Python keeps the old code in memory until then: while testing the question feature, the still-old
service created one reply card with the pre-guard behaviour in the middle of a test. Never touch `.env`, `data\` or `profiles\`.

## Windows Server
```
# copy the bundle to the server (e.g. C:\apps\tiktok-guard), then in an elevated PowerShell there:
powershell -ExecutionPolicy Bypass -File deploy\install_windows.ps1 -NoStart   # venv, restore profiles, service (stopped)
powershell -ExecutionPolicy Bypass -File deploy\as_system.ps1 -Command "venv\Scripts\python.exe run.py doctor"
nssm start tiktok-guard
```
It registers ONE NSSM service, `tiktok-guard`, LocalSystem,
automatic start at boot, restart on failure (20 s delay), below-normal CPU priority, rotating logs in `data\logs\`.

**Run everything that opens the bot's browser profiles as LocalSystem - use `deploy\as_system.ps1`.** Chromium encrypts
a profile's cookies with the Windows user that wrote them (DPAPI). The profiles are restored by, and used by, the service
account; opening them as another user (e.g. an admin SSH session) cannot decrypt the cookies and would save a logged-out
state over them. `as_system.ps1 -Command "<cmd>"` runs one command as LocalSystem and prints its output.

**Shared server etiquette** (a server shared with other services): a browser is only launched when at least
`MIN_FREE_MB` (900) of memory is available - otherwise that 120 s slot is skipped, an approved reply waits up to 90 s and
then reports "short of memory, retry". The bot never touches another service, another browser profile or another folder.

## Telegram: only ONE copy may poll
Telegram allows a single `getUpdates` consumer per bot. **Stop the bot on this PC (`Ctrl+C` / stop the task) before the
server starts**, otherwise both fight (HTTP 409, logged as `telegram_conflict`).

## After it is up
1. Telegram: `/status` (monitoring is always on, time of the last check, AI budget, accounts), `/posts`. There is no
   `/scan`, `/pause` or `/resume`: the bot checks TikTok every 120 s by itself from the moment the service starts.
2. Check one account really posts: approve one real alert (or a test), look at the screenshot the bot sends.
3. Delete the bundle from the server and from anywhere it was staged.

## Knobs for a tight server (`.env`)
`POLL_INTERVAL_SECONDS=120` (the page is read every 120 s, start to start; set `POLL_INTERVAL_MAX_SECONDS=600` to let quiet
pages stretch the interval), `MIN_FREE_MB=900` (memory guard), `BFP_LOW_PRIORITY=1`, `BFP_MUTE_AUDIO=1` (both on by default), `AI_DAILY_REQUESTS=40`, `DB_MAX_MB=50`, `NOTIFY_BURST=5`.

`BROWSER_BLOCK_RESOURCES` is **empty on purpose**. It can refuse downloads (`media`, `image`, `url:<substring>`), but it was
measured to break the bot: with the video blocked, TikTok's post overlay never delivers its comment data (3 of 3 live scans
failed), while nothing blocked gave 5 of 5 clean scans. Sound is handled by `--mute-audio` instead, so the server never
plays anything. Leave it empty unless you re-test a full scan (`python run.py scan`) with your value.
